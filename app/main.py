"""FastAPI app: Mova 3PL customer visibility portal + billing run + admin console.

Auth is per-user (email + password, pbkdf2). Roles: admin / internal / customer
(see app/perms.py). Customer users are locked to their own customer and the visibility
views; the billing run and admin console are Macgear-internal. The /admin/ingest and
/admin/billing/* endpoints are token-authed for n8n (the app never calls NetSuite itself).
"""
import csv
import io
import json
import os
from datetime import date, datetime, timedelta

from fastapi import Depends, FastAPI, Request
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import netsuite, perms, service
from .billing import compute_billing, result_to_run_kwargs
from .db import Base, SessionLocal, engine, ensure_columns, get_db
from .models import (CHARGE_TYPES, BillingLine, BillingRun, ChargeItem, Customer, Invoice,
                     RateCard, RateCardLine, User)
from .notify import send_reset_email
from .security import hash_password, hash_token, make_reset_token, sign, unsign, verify_password

HERE = os.path.dirname(os.path.abspath(__file__))
Base.metadata.create_all(engine)
ensure_columns()
# Populate the NetSuite charge-item catalogue on a fresh db. No-op once it holds anything,
# so an id corrected in the admin console is never reverted by a restart.
with SessionLocal() as _s:
    service.bootstrap_charge_items(_s)

app = FastAPI(title="Macgear 3PL Portal")
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))
templates.env.filters["money"] = lambda v: "" if v is None else f"{v:,.2f}"
templates.env.filters["money0"] = lambda v: "" if v is None else f"{round(v):,}"
templates.env.filters["qty"] = lambda v: "" if v is None else f"{v:,.0f}"
templates.env.filters["d"] = lambda v: v.strftime("%d %b %Y") if v else ""
templates.env.filters["dshort"] = lambda v: v.strftime("%d %b") if v else ""


def _ago(v):
    """Relative freshness for the live SOH sync. v is a UTC datetime (or a date
    fallback). Timezone-agnostic on purpose — avoids showing UTC clock time to an
    AEST user, and a large value flags a stalled sync."""
    if v is None:
        return ""
    if not isinstance(v, datetime):
        return v.strftime("%d %b")
    secs = (datetime.utcnow() - v).total_seconds()
    if secs < 90:
        return "just now"
    mins = secs / 60
    if mins < 60:
        return f"{int(mins)} min ago"
    hrs = mins / 60
    if hrs < 24:
        return f"{int(hrs)} hr{'s' if int(hrs) != 1 else ''} ago"
    return v.strftime("%d %b")


templates.env.filters["ago"] = _ago
_CHIP = {"received": "c-good", "shipped": "c-good", "paid": "c-good", "paid in full": "c-good",
         "open": "c-info", "in transit": "c-info", "picking": "c-warn", "overdue": "c-crit"}
templates.env.filters["chip"] = lambda s: _CHIP.get((s or "").lower(), "c-neutral")

NAV = [
    ("Visibility", [("overview", "Overview", "grid"),
                    ("stock_on_order", "Stock on order", "truck"),
                    ("item_receipts", "Item receipts", "in"),
                    ("stock_on_hand", "Stock on hand", "box"),
                    ("fulfilments", "Fulfilments", "out"),
                    ("invoices", "Invoices", "doc")]),
    ("Account", [("rate_card", "Rate card", "tag")]),
    ("Macgear internal", [("billing", "Billing run", "calc")]),
]
TITLES = {
    "overview": ("Overview", "Live snapshot of your stock and charges"),
    "stock_on_order": ("Stock on order", "Open purchase orders inbound to the 3PL warehouse"),
    "item_receipts": ("Item receipts", "Stock received and put away into the 3PL location"),
    "stock_on_hand": ("Stock on hand", "Current inventory held on your behalf"),
    "fulfilments": ("Fulfilments", "Outbound dispatches — sales orders and VRMA transfers"),
    "invoices": ("Invoices", "3PL service charges billed to your account"),
    "rate_card": ("Rate card", "Agreed 3PL handling and storage rates"),
    "billing": ("Weekly billing run", "Automated charge calculation from NetSuite — Macgear internal"),
}
VALID_VIEWS = {k for _, items in NAV for k, *_ in items}
VIEW_LABELS = {k: lbl for _, items in NAV for k, lbl, _ in items}
ICONS = {
    "grid": '<path d="M3 3h7v7H3zM14 3h7v7h-7zM14 14h7v7h-7zM3 14h7v7H3z" stroke="currentColor" stroke-width="1.6"/>',
    "truck": '<path d="M2 6h11v9H2zM13 9h4l3 3v3h-7z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><circle cx="6" cy="17" r="1.6" stroke="currentColor" stroke-width="1.5"/><circle cx="17" cy="17" r="1.6" stroke="currentColor" stroke-width="1.5"/>',
    "in": '<path d="M12 3v9m0 0 4-4m-4 4-4-4M4 16v4h16v-4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>',
    "out": '<path d="M12 13V4m0 0 4 4m-4-4-4 4M4 16v4h16v-4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>',
    "box": '<path d="M3 7.5 12 3l9 4.5v9L12 21l-9-4.5z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M3 7.5 12 12l9-4.5M12 12v9" stroke="currentColor" stroke-width="1.3"/>',
    "doc": '<path d="M6 3h8l4 4v14H6z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M14 3v4h4M9 13h6M9 17h6" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>',
    "tag": '<path d="M4 4h7l9 9-7 7-9-9z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><circle cx="8" cy="8" r="1.4" fill="currentColor"/>',
    "calc": '<rect x="5" y="3" width="14" height="18" rx="2" stroke="currentColor" stroke-width="1.6"/><path d="M8 7h8M8 11h2M12 11h4M8 15h2M8 18h2M14 14v4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>',
}
templates.env.globals.update(NAV=NAV, ICONS=ICONS, VIEW_LABELS=VIEW_LABELS, ROLES=perms.ROLES)

# Charge rows used when (re)building a rate card that has none yet.
DEFAULT_CHARGES = [
    ("container_unload", "Container unload — 40ft loose stacked", "per_container"),
    ("putaway", "Putaway (per unit)", "per_unit"),
    ("storage", "Storage (per pallet / week)", "per_pallet_week"),
    ("picking_so", "Picking — sales order (per unit)", "per_unit"),
    ("picking_vrma", "Picking — VRMA buy-in (per unit)", "per_unit"),
]


def _billing_period(params, default: tuple[date, date]) -> tuple[date, date, str | None]:
    """Resolve the billing period from the query string as a whole Monday–Sunday week.

    Billing is locked to whole weeks. Storage is priced in pallet-weeks and the re-billing
    guard keys on the exact (period_start, period_end) pair, so an arbitrary range both
    mis-prices storage and lets two overlapping runs bill the same receipts (20–26 Jul and
    24–26 Jul could both exist, each charging the same putaway).

    Two accepted forms:
      ?week=YYYY-MM-DD   any date in the wanted week — snapped to its Mon–Sun bounds. This is
                         what the UI submits, so the UI cannot produce an invalid period.
      ?from=&to=         the pre-existing contract (redirects, saved runs, bookmarks). Must be
                         exactly a Monday and that Monday + 6, otherwise it is REJECTED back to
                         the default week with a message — never silently snapped, or a stale
                         bookmark would quietly bill a period other than the one it names.
    """
    wk = params.get("week")
    if wk:
        try:
            return (*service.week_bounds(date.fromisoformat(wk)), None)
        except ValueError:
            return (*default, f"Ignored an unreadable week ({wk}).")
    frm, to = params.get("from"), params.get("to")
    if not (frm or to):
        return (*default, None)
    try:
        ps, pe = date.fromisoformat(frm), date.fromisoformat(to)
    except (TypeError, ValueError):
        return (*default, "Ignored an unreadable billing period.")
    if ps.weekday() != 0 or pe != ps + timedelta(days=6):
        mon, sun = service.week_bounds(ps)
        return (mon, sun, f"{ps} – {pe} is not a whole Monday–Sunday week. "
                          f"Showing {mon} – {sun} instead.")
    return ps, pe, None


# --- auth --------------------------------------------------------------------
APP_SECRET = os.environ.get("APP_SECRET", "") or "dev-insecure-secret-change-me"
SYNC_TOKEN = os.environ.get("SYNC_TOKEN", "")
COOKIE_NAME = "threepl_session"
# Absolute base for links we email out (behind the Caddy proxy request.base_url is unreliable).
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
RESET_TOKEN_TTL_MIN = int(os.environ.get("RESET_TOKEN_TTL_MIN", "45"))
# token-authed server-to-server endpoints (n8n) + public reset flow bypass the login cookie
_EXEMPT_EXACT = {"/login", "/logout", "/forgot", "/reset",
                 "/admin/ingest", "/admin/sync-config",
                 "/admin/billing/pending", "/admin/billing/pushed",
                 "/admin/billing/generate"}


def cur(request: Request) -> User | None:
    return getattr(request.state, "user", None)


def _token_ok(request: Request) -> bool:
    return bool(SYNC_TOKEN) and request.headers.get("X-Sync-Token") == SYNC_TOKEN


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path.startswith("/static") or path in _EXEMPT_EXACT:
        return await call_next(request)
    uid = unsign(request.cookies.get(COOKIE_NAME, ""), APP_SECRET)
    user = None
    if uid and uid.isdigit():
        db = SessionLocal()
        try:
            user = db.get(User, int(uid))
        finally:
            db.close()
    if not user or not user.active:
        return RedirectResponse("/login", status_code=303)
    request.state.user = user
    return await call_next(request)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if unsign(request.cookies.get(COOKIE_NAME, ""), APP_SECRET):
        return RedirectResponse("/", status_code=303)
    notice = ("Your password has been set — please sign in."
              if request.query_params.get("msg") == "reset" else "")
    return templates.TemplateResponse(request, "login.html", {"error": "", "notice": notice})


@app.post("/login")
async def login_submit(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    email = (form.get("email", "") or "").strip().lower()
    pw = form.get("password", "")
    user = db.scalar(select(User).where(User.email == email))
    if not user or not user.active or not verify_password(pw, user.password_hash):
        return templates.TemplateResponse(request, "login.html",
                                          {"error": "Incorrect email or password."},
                                          status_code=401)
    user.last_login = datetime.utcnow()
    db.commit()
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE_NAME, sign(str(user.id), APP_SECRET),
                    max_age=60 * 60 * 24 * 14, httponly=True, samesite="lax")
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    return resp


# --- password reset / set-password (public, single-use token) ----------------
def _issue_reset_link(db: Session, user: User, request: Request) -> str:
    """Mint a single-use token, persist only its hash + expiry, attempt to email the link,
    and return the link so the admin UI can show it for manual copy (email delivery is
    best-effort / optional — see app/notify.py)."""
    raw = make_reset_token()
    user.reset_token_hash = hash_token(raw)
    user.reset_expires_at = datetime.utcnow() + timedelta(minutes=RESET_TOKEN_TTL_MIN)
    db.commit()
    base = PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
    link = f"{base}/reset?token={raw}"
    send_reset_email(user.email, link)
    return link


def _user_for_reset_token(db: Session, token: str) -> User | None:
    if not token:
        return None
    user = db.scalar(select(User).where(User.reset_token_hash == hash_token(token)))
    if not user or not user.reset_expires_at or user.reset_expires_at < datetime.utcnow():
        return None
    return user


@app.get("/forgot", response_class=HTMLResponse)
def forgot_form(request: Request):
    return templates.TemplateResponse(request, "forgot_password.html", {"sent": False})


@app.post("/forgot", response_class=HTMLResponse)
async def forgot_submit(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    email = (form.get("email", "") or "").strip().lower()
    user = db.scalar(select(User).where(User.email == email))
    if user and user.active:
        _issue_reset_link(db, user, request)
    # Identical response whether or not the address matched — no account enumeration.
    return templates.TemplateResponse(request, "forgot_password.html", {"sent": True})


@app.get("/reset", response_class=HTMLResponse)
def reset_form(request: Request, token: str = "", db: Session = Depends(get_db)):
    user = _user_for_reset_token(db, token)
    return templates.TemplateResponse(request, "reset_password.html",
                                      {"invalid": user is None,
                                       "token": token if user else "", "error": ""})


@app.post("/reset")
async def reset_submit(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    token = form.get("token", "")
    pw = form.get("password", "")
    pw2 = form.get("password2", "")
    user = _user_for_reset_token(db, token)
    if not user:
        return templates.TemplateResponse(request, "reset_password.html",
                                          {"invalid": True, "token": "", "error": ""})
    if len(pw) < 10 or pw != pw2:
        return templates.TemplateResponse(
            request, "reset_password.html",
            {"invalid": False, "token": token,
             "error": "Passwords must match and be at least 10 characters."},
            status_code=400)
    user.password_hash = hash_password(pw)
    user.reset_token_hash = None
    user.reset_expires_at = None
    db.commit()
    return RedirectResponse("/login?msg=reset", status_code=303)


# --- helpers -----------------------------------------------------------------
def _customers(db: Session):
    return db.scalars(select(Customer).where(Customer.active == True)  # noqa: E712
                      .order_by(Customer.name)).all()


def _get_customer(db: Session, slug: str) -> Customer | None:
    return db.scalar(select(Customer).where(Customer.slug == slug))


def _portal_ctx(request: Request, db: Session, customer: Customer, view: str) -> dict:
    """Shared sidebar/topbar context for portal pages."""
    user = cur(request)
    allowed = perms.effective_views(user)
    imap = service.item_map(db, customer.id)
    # Wall-clock week, matching service.overview() — the topbar label and the chart
    # must never disagree about which week "this week" is.
    wk_start, wk_end = service.week_bounds(date.today())
    title, sub = TITLES[view]
    return {"customer": customer,
            "customers": _customers(db) if perms.is_internal(user) else [customer],
            "counts": service.nav_counts(db, customer.id, imap),
            "view": view, "title": title, "sub": sub, "allowed": allowed,
            "is_internal": perms.is_internal(user), "is_admin": perms.is_admin(user),
            "current_user": user,
            "week_label": f"{wk_start.strftime('%d')}–{wk_end.strftime('%d %b %Y')}",
            "_imap": imap, "_wk": (wk_start, wk_end)}


# --- portal ------------------------------------------------------------------
@app.get("/")
def home(request: Request, db: Session = Depends(get_db)):
    user = cur(request)
    if perms.is_internal(user):
        custs = _customers(db)
        if not custs:
            return JSONResponse({"error": "no customers — run python -m app.seed"}, status_code=500)
        return RedirectResponse(f"/c/{custs[0].slug}/overview", status_code=303)
    own = db.get(Customer, user.customer_id) if user.customer_id else None
    if not own:
        return JSONResponse({"error": "user has no customer assigned"}, status_code=403)
    first = perms.effective_views(user)[0] if perms.effective_views(user) else "overview"
    return RedirectResponse(f"/c/{own.slug}/{first}", status_code=303)


# Views that grow without bound, so they're date-windowed + paged rather than rendering all
# of history. Stock on hand / stock on order are naturally small (latest snapshot, open POs).
PAGED_VIEWS = ("item_receipts", "fulfilments", "invoices")


def _paged(db: Session, cust: Customer, view: str, imap: dict, qp) -> dict:
    """Shared window/search/page handling for the three unbounded list views.

    A search deliberately drops the date window and runs over all history: a filter that only
    covered the visible window would report "not found" for a document that exists, which is
    not acceptable on views people reconcile invoices against.
    """
    window = qp.get("window") or service.DEFAULT_WINDOW
    if window not in service.LIST_WINDOWS:
        window = service.DEFAULT_WINDOW
    q = (qp.get("q") or "").strip()
    try:
        shown = max(service.PAGE_SIZE, int(qp.get("n") or service.PAGE_SIZE))
    except ValueError:
        shown = service.PAGE_SIZE
    shown = min(shown, 10_000)                      # hard ceiling on one response
    since = None if q else service.window_since(window)

    names = service.item_names(db, cust.id)
    if view == "item_receipts":
        res = service.item_receipts(db, cust.id, imap, names, since=since, q=q or None,
                                   limit=shown)
    elif view == "fulfilments":
        res = service.fulfilments(db, cust.id, imap, names, since=since, q=q or None,
                                  limit=shown)
    else:
        res = service.invoices(db, cust.id, since=since, q=q or None, limit=shown)
    return {"rows": res["rows"], "total": res["total"], "qty_total": res["qty_total"],
            "docs": res["docs"], "window": window, "q": q, "shown": shown,
            "since": since, "next_n": shown + service.PAGE_SIZE,
            "has_more": res["total"] > len(res["rows"]), "paged": True}


@app.get("/c/{slug}/{view}/rows", response_class=HTMLResponse)
def portal_rows(slug: str, view: str, request: Request, db: Session = Depends(get_db)):
    """Just the <tr> rows for a paged view — what the Load more button fetches and appends.
    Same permission checks as the full page; renders the same macros, so there's one copy of
    the row markup."""
    user = cur(request)
    cust = _get_customer(db, slug)
    if not cust or view not in PAGED_VIEWS:
        return HTMLResponse("", status_code=404)
    if not perms.is_internal(user) and cust.id != user.customer_id:
        return HTMLResponse("", status_code=403)
    if view not in perms.effective_views(user):
        return HTMLResponse("", status_code=403)
    imap = service.item_map(db, cust.id)
    ctx = _paged(db, cust, view, imap, request.query_params)
    # only the newly-revealed tail, so the client appends instead of re-rendering
    try:
        have = max(0, int(request.query_params.get("have") or 0))
    except ValueError:
        have = 0
    ctx["rows"] = ctx["rows"][have:]
    ctx.update(view=view, customer=cust)
    return templates.TemplateResponse(request, "_rows_partial.html", ctx)


@app.get("/c/{slug}/{view}/export.csv")
def portal_export(slug: str, view: str, request: Request, db: Session = Depends(get_db)):
    """CSV of the ENTIRE current selection, not just the loaded page — these views get
    reconciled against invoices, so a partial export would be worse than none."""
    user = cur(request)
    cust = _get_customer(db, slug)
    if not cust or view not in PAGED_VIEWS:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not perms.is_internal(user) and cust.id != user.customer_id:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if view not in perms.effective_views(user):
        return JSONResponse({"error": "forbidden"}, status_code=403)

    cols = {
        "item_receipts": [("Receipt", "tranid"), ("PO", "po"), ("Inbound shipment", "shipment"),
                          ("Container", "container"), ("SKU", "sku"), ("Description", "name"),
                          ("Qty", "qty"), ("Date", "trandate")],
        "fulfilments": [("Fulfilment", "tranid"), ("Type", "source"), ("Reference", "ref"),
                        ("SKU", "sku"), ("Qty", "qty"), ("Date", "trandate")],
        "invoices": [("Invoice", "tranid"), ("Date raised", "trandate"),
                     ("Period from", "period_start"), ("Period to", "period_end"),
                     ("Amount", "total"), ("Status", "status")],
    }[view]
    window = request.query_params.get("window") or service.DEFAULT_WINDOW
    if window not in service.LIST_WINDOWS:
        window = service.DEFAULT_WINDOW
    q = (request.query_params.get("q") or "").strip() or None
    since = None if q else service.window_since(window)
    imap = service.item_map(db, cust.id)
    names = service.item_names(db, cust.id)
    fetch = {"item_receipts": service.item_receipts,
             "fulfilments": service.fulfilments}.get(view)

    def cell(v):
        if v is None:
            return ""
        if isinstance(v, float) and v.is_integer():
            return int(v)            # 700 rather than 700.0 — this lands in Excel
        return v

    def stream():
        """Paged in CHUNKS rather than one capped fetch: the toolbar promises the whole
        selection, so a silent truncation at some row limit would be worse than a slow
        download (people reconcile invoices off this file)."""
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\r\n")
        w.writerow([h for h, _ in cols])
        yield "﻿" + buf.getvalue()                  # BOM so Excel reads UTF-8
        offset, CHUNK = 0, 1000
        while True:
            if fetch:
                res = fetch(db, cust.id, imap, names, since=since, q=q,
                            limit=CHUNK, offset=offset)
            else:
                res = service.invoices(db, cust.id, since=since, q=q,
                                       limit=CHUNK, offset=offset)
            rows = res["rows"]
            if not rows:
                return
            buf.seek(0); buf.truncate(0)
            for r in rows:
                w.writerow([cell(r.get(k)) for _, k in cols])
            yield buf.getvalue()
            offset += len(rows)
            if offset >= res["total"]:
                return

    name = f"{cust.slug}-{view.replace('_', '-')}-{date.today().isoformat()}.csv"
    return StreamingResponse(stream(), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/c/{slug}/{view}", response_class=HTMLResponse)
def portal(slug: str, view: str, request: Request, db: Session = Depends(get_db)):
    user = cur(request)
    cust = _get_customer(db, slug)
    if not cust or view not in VALID_VIEWS:
        return RedirectResponse("/", status_code=303)
    # customer users are locked to their own customer
    if not perms.is_internal(user) and cust.id != user.customer_id:
        return RedirectResponse("/", status_code=303)
    allowed = perms.effective_views(user)
    if view not in allowed:
        return RedirectResponse(f"/c/{slug}/{allowed[0]}", status_code=303)

    ctx = _portal_ctx(request, db, cust, view)
    imap = ctx.pop("_imap")
    wk_start, wk_end = ctx.pop("_wk")

    if view == "overview":
        ctx["data"] = service.overview(db, cust, imap)
    elif view == "stock_on_order":
        ctx["rows"] = service.stock_on_order(db, cust.id, imap, service.item_names(db, cust.id))
    elif view in PAGED_VIEWS:
        ctx.update(_paged(db, cust, view, imap, request.query_params))
    elif view == "stock_on_hand":
        ctx["rows"] = service.stock_on_hand(db, cust.id, imap, service.item_names(db, cust.id))
        ctx["soh_synced_at"] = service.soh_synced_at(db, cust.id)
    elif view == "rate_card":
        ctx["rows"] = service.rate_card_lines(db, cust.id)
    elif view == "billing":
        ps_d, pe_d, period_err = _billing_period(request.query_params, (wk_start, wk_end))
        ps, pe = ps_d.isoformat(), pe_d.isoformat()
        res = compute_billing(db, cust, ps_d, pe_d)
        if period_err:
            res.warnings.insert(0, period_err)
        ctx.update(period_start=ps, period_end=pe, result=res,
                   period_label=f"{ps_d.strftime('%a %d %b')} – {pe_d.strftime('%a %d %b %Y')}",
                   prev_week=(ps_d - timedelta(days=7)).isoformat(),
                   next_week=(ps_d + timedelta(days=7)).isoformat(),
                   msg=request.query_params.get("msg", ""))
        runs = db.scalars(
            select(BillingRun).where(BillingRun.customer_id == cust.id)
            .order_by(BillingRun.created_at.desc())).all()
        ctx["runs"] = runs
        # map each pushed/invoiced run to its synced invoice (for a drill-through link)
        inv_by_ns = {i.ns_invoice_id: i.id for i in db.scalars(
            select(Invoice).where(Invoice.customer_id == cust.id)).all()}
        ctx["run_invoice"] = {r.id: inv_by_ns.get(r.ns_invoice_id)
                              for r in runs if r.ns_invoice_id}
        # a run already queued/pushed, or an explicitly closed period, blocks re-billing
        same = next((r for r in runs if r.period_start == ps_d and r.period_end == pe_d), None)
        ctx["block_reason"] = _recompute_blocked(same)
        # 'has-edits' is not a lock — the draft is still editable and pushable, it just can't
        # be silently recomputed. Only the two real freezes make the preview read-only.
        ctx["locked_run"] = same if ctx["block_reason"] in (
            "already-invoiced", "already-locked") else None
        # Once a run exists for the period IT is what the page is about, not the live preview:
        # the preview is what the rate card says right now, the run is what was (or will be)
        # invoiced, and after a push those two routinely differ. Showing a recomputed preview
        # above a variance table was actively misleading on the one screen people reconcile
        # invoices against. `period_run` renders the run's own lines; `draft_run` additionally
        # says they may be edited.
        ctx["period_run"] = same
        ctx["draft_run"] = same if (same and same.status == "draft" and not same.locked_at) else None
        ctx["charge_items"] = service.charge_items(db)
        # Variance against the NetSuite invoice, for whichever run is on screen or the most
        # recent pushed one — this is where an edit made in NetSuite after the push shows up.
        target = same or next((r for r in runs if r.ns_invoice_id), None)
        ctx["variance"] = service.run_variance(db, target) if target else None
        ctx["variance_run"] = target if ctx["variance"] else None
    return templates.TemplateResponse(request, "portal.html", ctx)


@app.get("/c/{slug}/invoice/{invoice_id}", response_class=HTMLResponse)
def invoice_detail(slug: str, invoice_id: int, request: Request, db: Session = Depends(get_db)):
    user = cur(request)
    cust = _get_customer(db, slug)
    if not cust:
        return RedirectResponse("/", status_code=303)
    if not perms.is_internal(user) and cust.id != user.customer_id:
        return RedirectResponse("/", status_code=303)
    if "invoices" not in perms.effective_views(user):
        return RedirectResponse(f"/c/{slug}/overview", status_code=303)
    inv, lines = service.invoice_with_lines(db, cust.id, invoice_id)
    if not inv:
        return RedirectResponse(f"/c/{slug}/invoices", status_code=303)
    ctx = _portal_ctx(request, db, cust, "invoices")
    ctx.pop("_imap"); ctx.pop("_wk")
    period, source, run_period = service.invoice_period(db, inv)
    ctx.update(title=f"Invoice {inv.tranid or inv.ns_invoice_id}",
               sub="3PL service charges — line detail",
               invoice=inv, lines=lines, invoice_period=period,
               period_source=source, run_period=run_period,
               msg=request.query_params.get("msg", ""))
    return templates.TemplateResponse(request, "portal.html", ctx)


@app.post("/c/{slug}/invoice/{invoice_id}/period")
async def set_invoice_period(slug: str, invoice_id: int, request: Request,
                             db: Session = Depends(get_db)):
    """Assign (or clear) the week an invoice bills, by hand.

    NetSuite has no field for the billed period, so an invoice raised manually there carries
    it nowhere: `trandate` is the raise date, and INAU250127 shows why that can't stand in for
    it — it was backdated to 31 Jul to fall inside payment terms while billing 27 Jul–2 Aug.

    Snapped to a whole Monday–Sunday week from whatever date is submitted, the same rule the
    billing period picker uses (`_billing_period`). Billing is priced in whole weeks
    throughout, so an invoice attributed to a ragged range could never line up with a run.

    Macgear-internal only — it's an attribution the customer reads, not one they set. Stored
    on portal-owned columns the sync never writes, so it survives every re-sync.
    """
    user = cur(request)
    cust = _get_customer(db, slug)
    if not cust or not perms.is_internal(user):
        return RedirectResponse("/", status_code=303)
    inv = db.get(Invoice, invoice_id)
    if not inv or inv.customer_id != cust.id:
        return RedirectResponse(f"/c/{slug}/invoices", status_code=303)
    form = await request.form()
    back = f"/c/{slug}/invoice/{invoice_id}"
    if form.get("clear") is not None:
        inv.period_start = inv.period_end = None
        db.commit()
        return RedirectResponse(f"{back}?msg=period-cleared", status_code=303)
    try:
        anchor = date.fromisoformat(form["week"])
    except (KeyError, TypeError, ValueError):
        return RedirectResponse(f"{back}?msg=bad-week", status_code=303)
    inv.period_start, inv.period_end = service.week_bounds(anchor)
    db.commit()
    return RedirectResponse(f"{back}?msg=period-set", status_code=303)


def _existing_run(db: Session, customer_id: int, ps: date, pe: date) -> BillingRun | None:
    return db.scalar(select(BillingRun).where(
        BillingRun.customer_id == customer_id, BillingRun.period_start == ps,
        BillingRun.period_end == pe))


def _recompute_blocked(run: BillingRun | None, allow_discard: bool = False) -> str | None:
    """Why this period's lines may not be (re)computed — a `msg=` code, or None if it's free.

    Three independent guards: the run has left the portal for NetSuite, the period has been
    explicitly closed, or a human has hand-edited the draft. All must hold for the manual
    save AND the scheduled auto-generate, so the reason lives here rather than in either
    caller. `allow_discard` lifts only the edit guard — the operator has confirmed on the
    form that recomputing throws their manual lines away; it never unlocks a closed or
    pushed period.
    """
    if run is None:
        return None
    if run.status in ("ready_to_push", "pushed", "invoiced"):
        return "already-invoiced"
    if run.locked_at:
        return "already-locked"
    if run.edited_at and not allow_discard:
        return "has-edits"
    return None


def _persist_billing_run(db: Session, cust: Customer, ps: date, pe: date, res) -> BillingRun:
    """Write a computed result to a draft run, replacing any existing lines for the period.

    Callers MUST have checked `_recompute_blocked()` first — this only writes. Manual and
    edited lines go with everything else: a recompute is a clean rebuild from the rate card,
    which is exactly why `_recompute_blocked` refuses to reach here on an edited run unless
    the operator explicitly confirmed the discard.
    """
    run = _existing_run(db, cust.id, ps, pe)
    if run:
        for l in list(run.lines):
            db.delete(l)
        run.edited_at = run.edited_by = None
    else:
        run = BillingRun(customer_id=cust.id, period_start=ps, period_end=pe)
        db.add(run)
        db.flush()
    item_by_charge = {ct: ci.ns_item_id for ct in CHARGE_TYPES
                      if (ci := service.charge_item_for(db, ct))}
    for kw in result_to_run_kwargs(res, item_by_charge):
        db.add(BillingLine(billing_run_id=run.id, **kw))
    run.status = "draft"
    return run


def _editable_run(db: Session, slug: str, run_id: int, request: Request):
    """(run, customer) for a draft a Macgear user may edit, or (None, None).

    Editing is refused on anything that has left the portal or been closed — the whole point
    of "Close period" is that what you reviewed is what gets pushed.
    """
    user = cur(request)
    run = db.get(BillingRun, run_id)
    cust = _get_customer(db, slug)
    if not cust or not run or run.customer_id != cust.id or not perms.is_internal(user):
        return None, None
    if run.status != "draft" or run.locked_at:
        return None, None
    return run, cust


def _mark_edited(run: BillingRun, request: Request):
    user = cur(request)
    run.edited_at = datetime.utcnow()
    run.edited_by = user.email if user else None


def _amount(qty, rate) -> float:
    return round(float(qty) * float(rate), 2)


@app.post("/c/{slug}/billing/run")
async def create_billing_run(slug: str, request: Request, db: Session = Depends(get_db)):
    user = cur(request)
    cust = _get_customer(db, slug)
    if not cust or not perms.is_internal(user):
        return RedirectResponse("/", status_code=303)
    form = await request.form()
    try:
        ps = date.fromisoformat(form["period_start"])
        pe = date.fromisoformat(form["period_end"])
    except (KeyError, TypeError, ValueError):
        return RedirectResponse(f"/c/{slug}/billing?msg=bad-period", status_code=303)
    # Billing periods are whole Mon–Sun weeks. The form is server-rendered from an already
    # aligned period, so this only fires on a hand-crafted POST — refuse rather than bill a
    # range whose storage would be mis-prorated and whose run could overlap another.
    if ps.weekday() != 0 or pe != ps + timedelta(days=6):
        return RedirectResponse(f"/c/{slug}/billing?msg=bad-period", status_code=303)
    # "Recompute" on an already-edited draft ticks this, having been warned that the manual
    # lines go. Nothing else can lift the edit guard.
    discard = form.get("discard_edits") is not None
    blocked = _recompute_blocked(_existing_run(db, cust.id, ps, pe), allow_discard=discard)
    if blocked:
        return RedirectResponse(
            f"/c/{slug}/billing?from={ps}&to={pe}&msg={blocked}", status_code=303)
    _persist_billing_run(db, cust, ps, pe, compute_billing(db, cust, ps, pe))
    db.commit()
    msg = "recomputed" if discard else "saved"
    return RedirectResponse(f"/c/{slug}/billing?from={ps}&to={pe}&msg={msg}", status_code=303)


# --- draft editing ------------------------------------------------------------
# Reverses the earlier "edit in NetSuite only" decision (CLAUDE.md). The rule it protected
# still holds and is enforced structurally, not by convention: a computed line's own
# arithmetic is never overwritten. qty/rate/amount become what will be invoiced;
# computed_qty/computed_amount keep what the rate card produced, so every draft can show
# what was changed and by how much, and "why is this invoice not 22 x $1,500?" stays
# answerable months later.
@app.post("/c/{slug}/billing/line/{run_id}")
async def edit_billing_line(slug: str, run_id: int, request: Request,
                            db: Session = Depends(get_db)):
    """Update one line's qty / rate / description on a draft run."""
    run, cust = _editable_run(db, slug, run_id, request)
    if not run:
        return RedirectResponse(f"/c/{slug}/billing?msg=not-editable", status_code=303)
    form = await request.form()
    line = db.get(BillingLine, int(form.get("line_id") or 0))
    qs = f"from={run.period_start}&to={run.period_end}"
    if not line or line.billing_run_id != run.id:
        return RedirectResponse(f"/c/{slug}/billing?{qs}&msg=bad-line", status_code=303)
    try:
        qty = round(float(form.get("qty")), 2)
        rate = round(float(form.get("rate")), 2)
    except (TypeError, ValueError):
        return RedirectResponse(f"/c/{slug}/billing?{qs}&msg=bad-line", status_code=303)
    if qty < 0 or rate < 0:
        return RedirectResponse(f"/c/{slug}/billing?{qs}&msg=bad-line", status_code=303)
    desc = (form.get("description") or "").strip()
    line.qty, line.rate, line.amount = qty, rate, _amount(qty, rate)
    if desc:
        line.description = desc
    # A computed line that's been touched becomes 'edited' and keeps its computed_* values.
    # A manual line stays manual — there is nothing to vary from. `origin is None` covers rows
    # saved before draft editing existed: they are computed lines, just from before the column.
    if line.origin in ("computed", "removed", None):
        line.origin = "edited"
    _mark_edited(run, request)
    db.commit()
    return RedirectResponse(f"/c/{slug}/billing?{qs}&msg=line-saved", status_code=303)


@app.post("/c/{slug}/billing/line/{run_id}/add")
async def add_billing_line(slug: str, run_id: int, request: Request,
                           db: Session = Depends(get_db)):
    """Add an ad-hoc charge line to a draft run, off the NetSuite charge-item catalogue.

    This is the flexibility the first fortnight of live 3PL work turned out to need: the rate
    card automates five charges, but the account carries ten items (kitting, packaging,
    freight, additional labour, system fee) that come up case by case. They are deliberately
    NOT wired into the billing engine — nothing derives them from cached NetSuite data, so
    there is nothing to automate. They are entered by a human, per week, against the item
    they will invoice against.
    """
    run, cust = _editable_run(db, slug, run_id, request)
    if not run:
        return RedirectResponse(f"/c/{slug}/billing?msg=not-editable", status_code=303)
    form = await request.form()
    qs = f"from={run.period_start}&to={run.period_end}"
    item = db.scalar(select(ChargeItem).where(
        ChargeItem.ns_item_id == (form.get("ns_item_id") or "").strip()))
    try:
        qty = round(float(form.get("qty")), 2)
        rate = round(float(form.get("rate")), 2)
    except (TypeError, ValueError):
        return RedirectResponse(f"/c/{slug}/billing?{qs}&msg=bad-line", status_code=303)
    if not item or qty <= 0 or rate < 0:
        return RedirectResponse(f"/c/{slug}/billing?{qs}&msg=bad-line", status_code=303)
    desc = (form.get("description") or "").strip() or item.name
    db.add(BillingLine(
        billing_run_id=run.id,
        # Manual lines carry the item's charge_type when it has one (so a hand-added storage
        # correction still groups with storage in the variance), else a stable `adhoc:<id>`
        # key — never NULL, because charge_type is the run's own line identity.
        charge_type=item.charge_type or f"adhoc:{item.ns_item_id}",
        description=desc, qty=qty, rate=rate, amount=_amount(qty, rate),
        source_refs=json.dumps([f"added manually by {(cur(request) or User()).email or '?'}"]),
        origin="manual", ns_item_id=item.ns_item_id))
    _mark_edited(run, request)
    db.commit()
    return RedirectResponse(f"/c/{slug}/billing?{qs}&msg=line-added", status_code=303)


@app.post("/c/{slug}/billing/line/{run_id}/remove")
async def remove_billing_line(slug: str, run_id: int, request: Request,
                              db: Session = Depends(get_db)):
    """Drop a line from a draft run.

    A manual line is deleted outright. A COMPUTED line is not — it's kept at origin='removed'
    with amount 0, still visible on the draft with its computed figure alongside. A charge
    the engine raised and a human then dropped is exactly the kind of thing that must leave a
    trace: silently vanishing charges are what put 9 containers ($13,500) off an invoice for
    a fortnight without anyone noticing.
    """
    run, cust = _editable_run(db, slug, run_id, request)
    if not run:
        return RedirectResponse(f"/c/{slug}/billing?msg=not-editable", status_code=303)
    form = await request.form()
    line = db.get(BillingLine, int(form.get("line_id") or 0))
    qs = f"from={run.period_start}&to={run.period_end}"
    if not line or line.billing_run_id != run.id:
        return RedirectResponse(f"/c/{slug}/billing?{qs}&msg=bad-line", status_code=303)
    if line.origin == "manual":
        db.delete(line)
    else:
        line.origin, line.qty, line.amount = "removed", 0, 0
    _mark_edited(run, request)
    db.commit()
    return RedirectResponse(f"/c/{slug}/billing?{qs}&msg=line-removed", status_code=303)


@app.post("/c/{slug}/billing/line/{run_id}/restore")
async def restore_billing_line(slug: str, run_id: int, request: Request,
                               db: Session = Depends(get_db)):
    """Put a removed computed line back at its rate-card figures."""
    run, cust = _editable_run(db, slug, run_id, request)
    if not run:
        return RedirectResponse(f"/c/{slug}/billing?msg=not-editable", status_code=303)
    form = await request.form()
    line = db.get(BillingLine, int(form.get("line_id") or 0))
    qs = f"from={run.period_start}&to={run.period_end}"
    if not line or line.billing_run_id != run.id or line.computed_amount is None:
        return RedirectResponse(f"/c/{slug}/billing?{qs}&msg=bad-line", status_code=303)
    line.qty, line.rate = line.computed_qty, line.computed_rate
    line.amount = line.computed_amount
    line.origin = "computed"
    _mark_edited(run, request)
    db.commit()
    return RedirectResponse(f"/c/{slug}/billing?{qs}&msg=line-restored", status_code=303)


@app.post("/c/{slug}/billing/delete/{run_id}")
def delete_billing_run(slug: str, run_id: int, request: Request, db: Session = Depends(get_db)):
    """Delete a saved billing run and its lines.

    Portal-side only — it never touches NetSuite. An invoice already created there stays
    exactly where it is; what's removed is the portal's record of the run and, with it, the
    link between them.

    ⚠️ **Deleting a run that reached NetSuite un-blocks re-billing that week.** The re-billing
    guard keys on the existence of a run for the period, so once the run is gone the period
    looks unbilled and can be computed, queued and pushed again — a second invoice for the
    same week. That is exactly what makes this useful for clearing sandbox leftovers, and
    exactly what makes it dangerous afterwards, so the template names the invoice in the
    confirm and the flash says the period is now re-billable.

    Admin-only, unlike the rest of the billing actions: recomputing or closing a period is
    reversible, this is not.
    """
    if not perms.is_admin(cur(request)):
        return RedirectResponse(f"/c/{slug}/billing?msg=not-allowed", status_code=303)
    run = db.get(BillingRun, run_id)
    cust = _get_customer(db, slug)
    if not cust or not run or run.customer_id != cust.id:
        return RedirectResponse(f"/c/{slug}/billing?msg=bad-run", status_code=303)
    qs = f"from={run.period_start}&to={run.period_end}"
    had_invoice = run.ns_invoice_id
    db.delete(run)          # lines go with it (cascade="all, delete-orphan")
    db.commit()
    msg = "run-deleted-pushed" if had_invoice else "run-deleted"
    return RedirectResponse(f"/c/{slug}/billing?{qs}&msg={msg}", status_code=303)


@app.post("/c/{slug}/billing/lock/{run_id}")
def lock_billing_run(slug: str, run_id: int, request: Request, db: Session = Depends(get_db)):
    """Close a period: freeze a draft run's computed lines.

    Deliberately separate from queueing. A locked run can still be pushed to NetSuite — the
    point is that what you reviewed is what gets pushed, and neither a re-save nor the Monday
    auto-generate can quietly recompute it underneath you.
    """
    user = cur(request)
    run = db.get(BillingRun, run_id)
    cust = _get_customer(db, slug)
    if not cust or not run or run.customer_id != cust.id or not perms.is_internal(user):
        return RedirectResponse("/", status_code=303)
    qs = f"from={run.period_start}&to={run.period_end}"
    if run.locked_at:
        return RedirectResponse(f"/c/{slug}/billing?{qs}&msg=already-locked", status_code=303)
    run.locked_at = datetime.utcnow()
    run.locked_by = user.email if user else None
    db.commit()
    return RedirectResponse(f"/c/{slug}/billing?{qs}&msg=locked", status_code=303)


@app.post("/c/{slug}/billing/push/{run_id}")
def queue_billing_run(slug: str, run_id: int, request: Request, db: Session = Depends(get_db)):
    """Queue a draft billing run for NetSuite. The app does NOT call NetSuite — it marks the
    run 'ready_to_push'; n8n picks it up (/admin/billing/pending), creates the DRAFT invoice
    via the RESTlet, and posts the new id back (/admin/billing/pushed)."""
    user = cur(request)
    run = db.get(BillingRun, run_id)
    cust = _get_customer(db, slug)
    if not cust or not run or run.customer_id != cust.id or not perms.is_internal(user):
        return RedirectResponse("/", status_code=303)
    qs = f"from={run.period_start}&to={run.period_end}"
    if run.status in ("ready_to_push", "pushed", "invoiced"):
        return RedirectResponse(f"/c/{slug}/billing?{qs}&msg=already-queued", status_code=303)
    # Every billable line must resolve to a NetSuite item BEFORE it leaves the portal. The
    # push used to fail inside n8n (undefined item id -> a NetSuite error in a log nobody
    # reads overnight, run stuck at ready_to_push); refusing here puts the failure in front
    # of the person who pressed the button, with the charge named.
    if (missing := _unmapped_lines(db, run)):
        return RedirectResponse(f"/c/{slug}/billing?{qs}&msg=no-item&ct={missing[0]}",
                                status_code=303)
    if not cust.ns_customer_id:
        return RedirectResponse(f"/c/{slug}/billing?{qs}&msg=no-bill-to", status_code=303)
    run.status = "ready_to_push"
    db.commit()
    return RedirectResponse(f"/c/{slug}/billing?{qs}&msg=queued", status_code=303)


def _unmapped_lines(db: Session, run: BillingRun) -> list[str]:
    """Charge types on this run whose line has no NetSuite item to invoice against.

    Lines are stamped with an item at save time, but a run saved before the catalogue was
    filled in (or against a charge type nobody mapped) carries NULL — so fall back to the
    catalogue here before declaring it unmappable, which lets an older draft be fixed by
    mapping the item rather than by re-billing the week.
    """
    missing = []
    for l in run.lines:
        if not l.billable:
            continue
        if not l.ns_item_id:
            item = service.charge_item_for(db, l.charge_type)
            if item is None:
                missing.append(l.charge_type)
            else:
                l.ns_item_id = item.ns_item_id     # heal it in place
    return missing


# --- admin console (admin role only) -----------------------------------------
def _deny_non_admin(request: Request):
    return None if perms.is_admin(cur(request)) else RedirectResponse("/", status_code=303)


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request, db: Session = Depends(get_db)):
    if (r := _deny_non_admin(request)):
        return r
    users = db.scalars(select(User).order_by(User.role, User.email)).all()
    cust_names = {c.id: c.name for c in db.scalars(select(Customer)).all()}
    rows = [{"u": u, "customer": cust_names.get(u.customer_id, "—"),
             "views": len(perms.effective_views(u)),
             "custom": bool(u.allowed_views)} for u in users]
    return templates.TemplateResponse(request, "admin_users.html",
                                      {"rows": rows, "section": "users"})


def _user_form_ctx(request: Request, db: Session, u: User | None,
                   notice: str = "", reset_link: str = "") -> dict:
    selected = perms.effective_views(u) if u else perms.role_default("customer")
    return {"section": "users", "u": u, "customers": _customers(db),
            "view_keys": perms.VIEW_KEYS, "selected": selected,
            "role": u.role if u else "customer",
            "notice": notice, "reset_link": reset_link}


@app.get("/admin/users/{user_id}", response_class=HTMLResponse)
@app.get("/admin/users/new", response_class=HTMLResponse)
def admin_user_form(request: Request, user_id: int | None = None, db: Session = Depends(get_db)):
    if (r := _deny_non_admin(request)):
        return r
    u = db.get(User, user_id) if user_id else None
    return templates.TemplateResponse(request, "admin_user_form.html",
                                      _user_form_ctx(request, db, u))


@app.post("/admin/users/{user_id}")
@app.post("/admin/users/new")
async def admin_user_save(request: Request, user_id: int | None = None,
                          db: Session = Depends(get_db)):
    if (r := _deny_non_admin(request)):
        return r
    form = await request.form()
    email = (form.get("email", "") or "").strip().lower()
    role = form.get("role", "customer")
    if role not in perms.ROLES:
        role = "customer"
    cust_id = form.get("customer_id") or None
    cust_id = int(cust_id) if (cust_id and role == "customer") else None
    selected = form.getlist("views")
    allowed = perms.normalize_allowed(role, selected)
    active = form.get("active") == "on"

    u = db.get(User, user_id) if user_id else None
    invite = False
    if u is None:
        if not email:
            return RedirectResponse("/admin/users/new", status_code=303)
        # New users have no password — they set their own via a set-password link.
        u = User(email=email, password_hash="")
        db.add(u)
        invite = True
    elif email:
        u.email = email
    u.role = role
    u.customer_id = cust_id
    u.allowed_views = allowed
    u.active = active
    db.commit()
    if invite:
        link = _issue_reset_link(db, u, request)   # the "set your password" link
        return templates.TemplateResponse(request, "admin_user_form.html", _user_form_ctx(
            request, db, u,
            notice="User created. Send them the set-password link below (also emailed if email is configured).",
            reset_link=link))
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/users/{user_id}/send-reset")
def admin_user_send_reset(user_id: int, request: Request, db: Session = Depends(get_db)):
    if (r := _deny_non_admin(request)):
        return r
    u = db.get(User, user_id)
    if not u:
        return RedirectResponse("/admin/users", status_code=303)
    link = _issue_reset_link(db, u, request)
    return templates.TemplateResponse(request, "admin_user_form.html", _user_form_ctx(
        request, db, u,
        notice="Set-password link generated. Copy it below and send it to the user (also emailed if email is configured).",
        reset_link=link))


@app.post("/admin/users/{user_id}/delete")
def admin_user_delete(user_id: int, request: Request, db: Session = Depends(get_db)):
    if (r := _deny_non_admin(request)):
        return r
    u = db.get(User, user_id)
    me = cur(request)
    if u and u.id != me.id:          # never delete yourself
        db.delete(u)
        db.commit()
    return RedirectResponse("/admin/users", status_code=303)


@app.get("/admin/customers", response_class=HTMLResponse)
def admin_customers(request: Request, db: Session = Depends(get_db)):
    if (r := _deny_non_admin(request)):
        return r
    return templates.TemplateResponse(request, "admin_customers.html",
                                      {"section": "customers", "customers": _customers(db)})


@app.get("/admin/charge-items", response_class=HTMLResponse)
def admin_charge_items(request: Request, db: Session = Depends(get_db)):
    """The NetSuite service items 3PL invoice lines are raised against.

    Replaces the CHARGE_ITEMS constant that used to live in the n8n Code node, where the ids
    were sandbox-only and going to production meant editing a workflow. Two jobs: map the
    five automated charges to their items, and expose the rest of the `3PL - *` set for
    ad-hoc lines on a draft.
    """
    if (r := _deny_non_admin(request)):
        return r
    return templates.TemplateResponse(request, "admin_charge_items.html", {
        "section": "charge_items", "items": service.charge_items(db, active_only=False),
        "charge_types": CHARGE_TYPES, "aliases": service.CHARGE_TYPE_ALIASES,
        "unmapped": service.unmapped_charge_types(db),
        "saved": request.query_params.get("saved"), "error": ""})


@app.post("/admin/charge-items")
async def admin_charge_items_save(request: Request, db: Session = Depends(get_db)):
    if (r := _deny_non_admin(request)):
        return r
    form = await request.form()
    # Existing rows: charge_type / active / sort. charge_type is one-item-per-charge, so
    # assigning it to a second item clears it from the first rather than silently creating
    # an ambiguous mapping that charge_item_for() would resolve arbitrarily.
    for item in service.charge_items(db, active_only=False):
        ct = (form.get(f"ct_{item.id}") or "").strip() or None
        if ct and ct not in CHARGE_TYPES:
            ct = None
        if ct and ct != item.charge_type:
            for other in db.scalars(select(ChargeItem).where(
                    ChargeItem.charge_type == ct, ChargeItem.id != item.id)).all():
                other.charge_type = None
        item.charge_type = ct
        item.active = form.get(f"active_{item.id}") is not None
        try:
            item.sort_order = int(form.get(f"sort_{item.id}") or item.sort_order)
        except ValueError:
            pass
    # Optional new row.
    ns_id = (form.get("new_ns_item_id") or "").strip()
    name = (form.get("new_name") or "").strip()
    if ns_id and name and not db.scalar(
            select(ChargeItem).where(ChargeItem.ns_item_id == ns_id)):
        db.add(ChargeItem(ns_item_id=ns_id, name=name, sort_order=200))
    db.commit()
    return RedirectResponse("/admin/charge-items?saved=1", status_code=303)


def _safe_rate(raw) -> float:
    try:
        return round(float(raw), 2)
    except (TypeError, ValueError):
        return 0.0


def _blank_charges():
    return [{"charge_type": ct, "label": lbl, "basis": b, "rate": 0.0}
            for ct, lbl, b in DEFAULT_CHARGES]


@app.get("/admin/customers/new", response_class=HTMLResponse)
def admin_customer_new(request: Request, db: Session = Depends(get_db)):
    if (r := _deny_non_admin(request)):
        return r
    return templates.TemplateResponse(request, "admin_customer_form.html",
                                      {"section": "customers", "c": Customer(),
                                       "charges": _blank_charges(), "is_new": True, "error": ""})


@app.post("/admin/customers/new", response_class=HTMLResponse)
async def admin_customer_create(request: Request, db: Session = Depends(get_db)):
    if (r := _deny_non_admin(request)):
        return r
    form = await request.form()
    slug = (form.get("slug", "") or "").strip().lower()
    name = (form.get("name", "") or "").strip()
    ns_customer_id = (form.get("ns_customer_id", "") or "").strip()
    location_scoped = form.get("location_scoped") is not None  # checkbox: present only when ticked
    invoice_items_only = form.get("invoice_items_only") is not None

    error = ""
    if not slug or not all(ch.isalnum() or ch == "-" for ch in slug):
        error = "Slug is required and may contain only lowercase letters, numbers and hyphens."
    elif not name:
        error = "Name is required."
    elif not ns_customer_id:
        error = "NetSuite customer id is required."
    elif db.scalar(select(Customer).where(Customer.slug == slug)):
        error = f"Slug '{slug}' is already taken."
    if error:
        # re-render with what they typed so nothing is lost
        c = Customer(slug=slug, name=name, ns_customer_id=ns_customer_id,
                     ns_supplier_id=form.get("ns_supplier_id", "").strip() or None,
                     ns_location_id=form.get("ns_location_id", "").strip() or None,
                     ns_class_id=form.get("ns_class_id", "").strip() or None,
                     ns_subsidiary_id=form.get("ns_subsidiary_id", "").strip() or None,
                     brand_label=form.get("brand_label", "").strip() or None,
                     location_scoped=location_scoped,
                     invoice_items_only=invoice_items_only,
                     location_label=form.get("location_label", "").strip() or None)
        charges = [{**ch, "rate": _safe_rate(form.get(f"rate_{ch['charge_type']}"))}
                   for ch in _blank_charges()]
        return templates.TemplateResponse(request, "admin_customer_form.html",
                                          {"section": "customers", "c": c, "charges": charges,
                                           "is_new": True, "error": error}, status_code=400)

    c = Customer(slug=slug, name=name, ns_customer_id=ns_customer_id,
                 ns_supplier_id=form.get("ns_supplier_id", "").strip() or None,
                 ns_location_id=form.get("ns_location_id", "").strip() or None,
                 ns_class_id=form.get("ns_class_id", "").strip() or None,
                 ns_subsidiary_id=form.get("ns_subsidiary_id", "").strip() or None,
                 brand_label=form.get("brand_label", "").strip() or None,
                 location_scoped=location_scoped,
                 invoice_items_only=invoice_items_only,
                 location_label=form.get("location_label", "").strip() or None)
    db.add(c)
    db.flush()
    # seed an initial effective-dated rate card from the submitted rates (default 0)
    card = RateCard(customer_id=c.id, effective_from=date.today())
    db.add(card)
    db.flush()
    for ct, lbl, b in DEFAULT_CHARGES:
        db.add(RateCardLine(rate_card_id=card.id, charge_type=ct, label=lbl,
                            basis=b, rate=_safe_rate(form.get(f"rate_{ct}"))))
    db.commit()
    return RedirectResponse(f"/admin/customers/{c.id}?saved=1", status_code=303)


@app.get("/admin/customers/{cust_id}", response_class=HTMLResponse)
def admin_customer_form(cust_id: int, request: Request, db: Session = Depends(get_db)):
    if (r := _deny_non_admin(request)):
        return r
    c = db.get(Customer, cust_id)
    if not c:
        return RedirectResponse("/admin/customers", status_code=303)
    card = service.active_rate_card(db, c.id, date.today())
    if card:
        charges = [{"charge_type": l.charge_type, "label": l.label, "basis": l.basis,
                    "rate": float(l.rate)} for l in sorted(card.lines, key=lambda x: x.charge_type)]
    else:
        charges = _blank_charges()
    return templates.TemplateResponse(request, "admin_customer_form.html",
                                      {"section": "customers", "c": c, "charges": charges,
                                       "is_new": False, "error": ""})


@app.post("/admin/customers/{cust_id}")
async def admin_customer_save(cust_id: int, request: Request, db: Session = Depends(get_db)):
    if (r := _deny_non_admin(request)):
        return r
    c = db.get(Customer, cust_id)
    if not c:
        return RedirectResponse("/admin/customers", status_code=303)
    form = await request.form()
    c.name = form.get("name", c.name).strip()
    c.brand_label = form.get("brand_label", "").strip() or None
    c.location_label = form.get("location_label", "").strip() or None
    c.ns_customer_id = form.get("ns_customer_id", "").strip()
    c.ns_supplier_id = form.get("ns_supplier_id", "").strip() or None
    c.ns_location_id = form.get("ns_location_id", "").strip() or None
    c.ns_class_id = form.get("ns_class_id", "").strip() or None
    c.ns_subsidiary_id = form.get("ns_subsidiary_id", "").strip() or None
    c.location_scoped = form.get("location_scoped") is not None  # checkbox: present only when ticked
    c.invoice_items_only = form.get("invoice_items_only") is not None

    # Rate-card edit: if any rate changed, create a NEW effective-dated card (today)
    # and close the previous one — so historical billing runs still reprice correctly.
    current = service.active_rate_card(db, c.id, date.today())
    base = ({l.charge_type: l for l in current.lines} if current else {})
    new_rates = {}
    changed = False
    charge_types = list(base.keys()) or [ct for ct, _, _ in DEFAULT_CHARGES]
    for ct in charge_types:
        raw = form.get(f"rate_{ct}")
        if raw is None:
            continue
        try:
            val = round(float(raw), 2)
        except ValueError:
            continue
        new_rates[ct] = val
        if not current or ct not in base or float(base[ct].rate) != val:
            changed = True
    if changed:
        today = date.today()
        if current and current.effective_from == today:
            # editing again same day — just update the lines in place
            for l in current.lines:
                if l.charge_type in new_rates:
                    l.rate = new_rates[l.charge_type]
        else:
            if current:
                current.effective_to = today
            card = RateCard(customer_id=c.id, effective_from=today)
            db.add(card)
            db.flush()
            for ct in charge_types:
                meta = base.get(ct)
                label = meta.label if meta else dict((x[0], x[1]) for x in DEFAULT_CHARGES).get(ct, ct)
                basis = meta.basis if meta else dict((x[0], x[2]) for x in DEFAULT_CHARGES).get(ct, "per_unit")
                db.add(RateCardLine(rate_card_id=card.id, charge_type=ct, label=label,
                                    basis=basis, rate=new_rates.get(ct, 0.0)))
    db.commit()
    return RedirectResponse(f"/admin/customers/{cust_id}?saved=1", status_code=303)


# --- n8n integration endpoints (token-authed, server-to-server) --------------
# The app never calls NetSuite. n8n signs TBA, calls the RESTlet, and uses these.
@app.get("/admin/sync-config")
def admin_sync_config(request: Request, db: Session = Depends(get_db)):
    """The customer list the n8n sync loops over — so adding a customer in the admin
    console (not editing the node) is all that's needed to start syncing it. Only
    customers with a brand class are returned (the reads are class-scoped)."""
    if not _token_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    custs = db.scalars(select(Customer).where(Customer.active == True)  # noqa: E712
                       .order_by(Customer.slug)).all()
    # charge_item_ids rides along so the RESTlet can narrow the invoice read to invoices
    # carrying a 3PL charge item — needed because Mova bills to Spacewalker HK (11066), an
    # existing trading entity whose other invoices must not appear in the customer portal.
    # Applied per customer via invoice_items_only, so Skriva (whose invoices are $0 product
    # invoices with no charge item on them) still sees all of its own.
    item_ids = [ci.ns_item_id for ci in service.charge_items(db)]
    return JSONResponse({"charge_item_ids": item_ids, "customers": [
        {"slug": c.slug, "ns_customer_id": c.ns_customer_id, "ns_supplier_id": c.ns_supplier_id,
         "ns_location_id": c.ns_location_id, "ns_class_id": c.ns_class_id,
         "ns_subsidiary_id": c.ns_subsidiary_id, "location_scoped": bool(c.location_scoped),
         "invoice_items_only": bool(c.invoice_items_only)}
        for c in custs if c.ns_class_id]})


@app.post("/admin/ingest")
async def admin_ingest(request: Request, db: Session = Depends(get_db)):
    """Upsert rows fetched from NetSuite by n8n. Body: {customer: slug, entity, rows:[...]}.
    Entities: invoices, purchase_orders, item_receipts, item_fulfilments,
    inbound_shipments, stock_on_hand (see app/netsuite.py for row contracts)."""
    if not _token_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    cust = _get_customer(db, (payload.get("customer") or "").strip())
    entity = payload.get("entity")
    rows = payload.get("rows")
    if not cust:
        return JSONResponse({"error": "unknown customer"}, status_code=404)
    if entity not in netsuite.INGEST or not isinstance(rows, list):
        return JSONResponse({"error": "bad entity or rows"}, status_code=400)
    n = netsuite.ingest(db, cust, entity, rows)
    return JSONResponse({"customer": cust.slug, "entity": entity, "ingested": n})


@app.get("/admin/billing/pending")
def admin_billing_pending(request: Request, db: Session = Depends(get_db)):
    """Runs queued for NetSuite. n8n creates a draft invoice from each, then posts back."""
    if not _token_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    runs = db.scalars(select(BillingRun).where(BillingRun.status == "ready_to_push")).all()
    out = []
    for r in runs:
        c = db.get(Customer, r.customer_id)
        # ns_item_id per line, resolved app-side. This is what retired the CHARGE_ITEMS map
        # hardcoded in the n8n Code node: n8n no longer knows anything about which item backs
        # which charge, so moving sandbox -> production (or adding an ad-hoc charge) is an
        # admin-console edit rather than a workflow edit. Removed lines are omitted entirely.
        lines = [{"charge_type": l.charge_type, "ns_item_id": l.ns_item_id,
                  "description": l.description, "qty": float(l.qty or 0),
                  "rate": float(l.rate or 0), "amount": float(l.amount or 0),
                  "origin": l.origin or "computed"}
                 for l in r.lines if l.billable]
        out.append({
            "run_id": r.id, "customer": c.slug, "ns_customer_id": c.ns_customer_id,
            "ns_subsidiary_id": c.ns_subsidiary_id, "ns_location_id": c.ns_location_id,
            "period_start": r.period_start.isoformat(), "period_end": r.period_end.isoformat(),
            "total": round(sum(l["amount"] for l in lines), 2), "lines": lines})
    return JSONResponse({"pending": out})


@app.post("/admin/billing/generate")
async def admin_billing_generate(request: Request, db: Session = Depends(get_db)):
    """Auto-generate the draft billing run for a completed week, for every active customer.

    Called by the n8n FULL lane at the END of its run (after all six reads land) so the week's
    receipts and fulfilments are in the cache before anything is computed. Never on the 15-min
    SOH lane.

    Generates only, never pushes: the run lands at `draft` for a human to review and queue.
    Idempotent — a period that already has a run is skipped rather than recomputed, so a
    reviewed or closed draft is safe from a re-run. Optional body: {"week": "YYYY-MM-DD"} to
    target the week containing that date (backfill); default is the most recently *completed*
    week, i.e. the one before the current one.
    """
    if not _token_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    anchor = payload.get("week") if isinstance(payload, dict) else None
    try:
        target = date.fromisoformat(anchor) if anchor else date.today() - timedelta(days=7)
    except (TypeError, ValueError):
        return JSONResponse({"error": f"unreadable week: {anchor}"}, status_code=400)
    ps, pe = service.week_bounds(target)

    out = []
    for cust in db.scalars(select(Customer).where(Customer.active == True)).all():  # noqa: E712
        row = {"customer": cust.slug, "period_start": ps.isoformat(),
               "period_end": pe.isoformat()}
        existing = _existing_run(db, cust.id, ps, pe)
        if existing:
            row.update(generated=False, skipped=_recompute_blocked(existing) or "run-exists",
                       run_id=existing.id, status=existing.status)
        else:
            res = compute_billing(db, cust, ps, pe)
            if not res.lines:
                # No billable activity — don't plant an empty run that a human has to dismiss.
                row.update(generated=False, skipped="no-billable-activity")
            else:
                run = _persist_billing_run(db, cust, ps, pe, res)
                row.update(generated=True, run_id=run.id, total=res.total,
                           lines=len(res.lines))
            # Warnings are the whole point of surfacing under-bills — put them in the n8n log
            # too, not just the portal, so a zero charge is visible without opening the app.
            if res.warnings:
                row["warnings"] = res.warnings
        out.append(row)
    db.commit()
    return JSONResponse({"week": [ps.isoformat(), pe.isoformat()], "customers": out})


@app.post("/admin/billing/pushed")
async def admin_billing_pushed(request: Request, db: Session = Depends(get_db)):
    """n8n reports the draft invoice it created. Body: {run_id, ns_invoice_id}."""
    if not _token_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    payload = await request.json()
    run = db.get(BillingRun, int(payload.get("run_id", 0)))
    ns_id = str(payload.get("ns_invoice_id", "")).strip()
    if not run or not ns_id:
        return JSONResponse({"error": "run_id and ns_invoice_id required"}, status_code=400)
    run.ns_invoice_id = ns_id
    run.status = "pushed"
    db.commit()
    return JSONResponse({"run_id": run.id, "status": run.status, "ns_invoice_id": ns_id})
