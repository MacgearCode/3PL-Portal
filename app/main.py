"""FastAPI app: Mova 3PL customer visibility portal + billing run + admin console.

Auth is per-user (email + password, pbkdf2). Roles: admin / internal / customer
(see app/perms.py). Customer users are locked to their own customer and the visibility
views; the billing run and admin console are Macgear-internal. The /admin/ingest and
/admin/billing/* endpoints are token-authed for n8n (the app never calls NetSuite itself).
"""
import json
import os
from datetime import date, datetime, timedelta

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import netsuite, perms, service
from .billing import compute_billing, result_to_run_kwargs
from .db import Base, SessionLocal, engine, ensure_columns, get_db
from .models import (BillingLine, BillingRun, Customer, Invoice, RateCard, RateCardLine, User)
from .notify import send_reset_email
from .security import hash_password, hash_token, make_reset_token, sign, unsign, verify_password

HERE = os.path.dirname(os.path.abspath(__file__))
Base.metadata.create_all(engine)
ensure_columns()

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
                 "/admin/billing/pending", "/admin/billing/pushed"}


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
    elif view == "item_receipts":
        ctx["rows"] = service.item_receipts(db, cust.id, imap, service.item_names(db, cust.id))
    elif view == "stock_on_hand":
        ctx["rows"] = service.stock_on_hand(db, cust.id, imap, service.item_names(db, cust.id))
        ctx["soh_synced_at"] = service.soh_synced_at(db, cust.id)
    elif view == "fulfilments":
        ctx["rows"] = service.fulfilments(db, cust.id, imap)
    elif view == "invoices":
        ctx["rows"] = service.invoices(db, cust.id)
    elif view == "rate_card":
        ctx["rows"] = service.rate_card_lines(db, cust.id)
    elif view == "billing":
        ps = request.query_params.get("from") or wk_start.isoformat()
        pe = request.query_params.get("to") or wk_end.isoformat()
        ps_d, pe_d = date.fromisoformat(ps), date.fromisoformat(pe)
        res = compute_billing(db, cust, ps_d, pe_d)
        ctx.update(period_start=ps, period_end=pe, result=res,
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
        # a run already pushed/invoiced for this exact period blocks re-billing
        same = next((r for r in runs if r.period_start == ps_d and r.period_end == pe_d), None)
        ctx["locked_run"] = same if (same and same.status in (
            "ready_to_push", "pushed", "invoiced")) else None
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
    ctx.update(title=f"Invoice {inv.tranid or inv.ns_invoice_id}",
               sub="3PL service charges — line detail",
               invoice=inv, lines=lines)
    return templates.TemplateResponse(request, "portal.html", ctx)


@app.post("/c/{slug}/billing/run")
async def create_billing_run(slug: str, request: Request, db: Session = Depends(get_db)):
    user = cur(request)
    cust = _get_customer(db, slug)
    if not cust or not perms.is_internal(user):
        return RedirectResponse("/", status_code=303)
    form = await request.form()
    ps = date.fromisoformat(form["period_start"])
    pe = date.fromisoformat(form["period_end"])
    res = compute_billing(db, cust, ps, pe)
    existing = db.scalar(select(BillingRun).where(
        BillingRun.customer_id == cust.id, BillingRun.period_start == ps,
        BillingRun.period_end == pe))
    if existing and existing.status in ("ready_to_push", "pushed", "invoiced"):
        # re-billing guard: this period is already queued/pushed to NetSuite
        return RedirectResponse(
            f"/c/{slug}/billing?from={ps}&to={pe}&msg=already-invoiced", status_code=303)
    if existing:
        for l in list(existing.lines):
            db.delete(l)
        run = existing
    else:
        run = BillingRun(customer_id=cust.id, period_start=ps, period_end=pe)
        db.add(run)
        db.flush()
    for kw in result_to_run_kwargs(res):
        db.add(BillingLine(billing_run_id=run.id, **kw))
    run.status = "draft"
    db.commit()
    return RedirectResponse(f"/c/{slug}/billing?from={ps}&to={pe}&msg=saved", status_code=303)


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
    run.status = "ready_to_push"
    db.commit()
    return RedirectResponse(f"/c/{slug}/billing?{qs}&msg=queued", status_code=303)


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
    return JSONResponse({"customers": [
        {"slug": c.slug, "ns_customer_id": c.ns_customer_id, "ns_supplier_id": c.ns_supplier_id,
         "ns_location_id": c.ns_location_id, "ns_class_id": c.ns_class_id,
         "ns_subsidiary_id": c.ns_subsidiary_id, "location_scoped": bool(c.location_scoped)}
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
        out.append({
            "run_id": r.id, "customer": c.slug, "ns_customer_id": c.ns_customer_id,
            "ns_subsidiary_id": c.ns_subsidiary_id, "ns_location_id": c.ns_location_id,
            "period_start": r.period_start.isoformat(), "period_end": r.period_end.isoformat(),
            "lines": [{"charge_type": l.charge_type, "description": l.description,
                       "qty": float(l.qty or 0), "rate": float(l.rate or 0),
                       "amount": float(l.amount or 0)} for l in r.lines]})
    return JSONResponse({"pending": out})


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
