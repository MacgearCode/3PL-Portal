"""Read-side helpers: turn cache tables into the 6 portal views + overview.

Items are resolved to SKUs via a per-customer ns_item_id -> sku map so the portal shows
human SKUs, not NetSuite internal ids. The "current billing week" is the wall-clock
Monday–Sunday week; latest_activity_date() is kept for callers that want the cache's own
high-water mark, but the overview no longer anchors to it (the incoming-units series is
forward-looking and has to agree with the calendar).
"""
import math
import re
from datetime import date, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from .billing import active_rate_card, compute_billing
from .models import (BillingLine, BillingRun, ChargeItem, Customer, InboundShipment, Item,
                     ItemFulfilment, ItemFulfilmentLine, ItemReceipt, ItemReceiptLine,
                     Invoice, InvoiceLine, PurchaseOrder, RateCardLine, StockOnHand)

# Line collections default to lazy="select", so "for r in rows: for l in r.lines" fires one
# query per parent row. At two years of Mova's cadence that was 937 queries / 425 ms for the
# receipts view and 1,514 / 582 ms for the overview. selectinload() collapses each of those to
# one extra query. It matters far more on the droplet than it looks locally: SQLite is
# in-process, but Postgres pays a network round trip per query.
_RECEIPT_LINES = selectinload(ItemReceipt.lines)
_FULFILMENT_LINES = selectinload(ItemFulfilment.lines)


def item_map(db: Session, customer_id: int) -> dict[str, str]:
    rows = db.scalars(select(Item).where(Item.customer_id == customer_id)).all()
    return {i.ns_item_id: i.sku for i in rows}


def item_names(db: Session, customer_id: int) -> dict[str, str]:
    rows = db.scalars(select(Item).where(Item.customer_id == customer_id)).all()
    return {i.ns_item_id: (i.description or i.sku) for i in rows}


# --- charge items (the NetSuite service item catalogue) ----------------------
# Macgear's live `3PL - *` production items, read off the item list 2026-08-05. Used to
# bootstrap an empty `charge_item` table; after that the admin console is the source of
# truth and this constant is never consulted again (so an id corrected in the UI stays
# corrected). The five mapped charge_types replace the sandbox ids 55070-55074 that were
# hardcoded in the n8n node — those never existed in production.
#
# picking_so and picking_vrma BOTH map to `3PL - Picking` (23563): picking is billed at
# $1.00/unit regardless of whether the stock left on a sales order or a VRMA buy-in, so
# they are one line on the invoice. The portal still computes them separately, because the
# split is what tells you which dispatch path was used.
DEFAULT_CHARGE_ITEMS = [
    # (ns_item_id, name, charge_type, sort_order)
    ("57082", "3PL - container unload", "container_unload", 10),
    ("23560", "3PL - Putaway Fee", "putaway", 20),
    ("23561", "3PL - Storage", "storage", 30),
    ("23563", "3PL - Picking", "picking_so", 40),
    ("36281", "3PL - Pick / Pack Service Charge", None, 50),
    ("23565", "3PL - Freight", "shipping", 60),
    ("23562", "3PL - Kitting", None, 70),
    ("23564", "3PL - Packaging", None, 80),
    ("23566", "3PL - System Fee", None, 90),
    ("23567", "3PL - Additional Labour", None, 100),
]
# charge_types that share another item's mapping. Kept out of ChargeItem.charge_type (which
# is one-per-item) so the catalogue stays a clean 1:1 map and this stays an explicit,
# reviewable statement about billing rather than a duplicate row someone might "tidy up".
CHARGE_TYPE_ALIASES = {"picking_vrma": "picking_so"}


def bootstrap_charge_items(db: Session) -> int:
    """Insert the default catalogue if the table is empty. Idempotent; never overwrites."""
    if db.scalar(select(func.count()).select_from(ChargeItem)):
        return 0
    for ns_id, name, ct, order in DEFAULT_CHARGE_ITEMS:
        db.add(ChargeItem(ns_item_id=ns_id, name=name, charge_type=ct, sort_order=order))
    db.commit()
    return len(DEFAULT_CHARGE_ITEMS)


def charge_items(db: Session, active_only: bool = True) -> list[ChargeItem]:
    stmt = select(ChargeItem).order_by(ChargeItem.sort_order, ChargeItem.name)
    if active_only:
        stmt = stmt.where(ChargeItem.active == True)  # noqa: E712
    return list(db.scalars(stmt).all())


def charge_item_for(db: Session, charge_type: str) -> ChargeItem | None:
    """The item a computed charge invoices against, following the alias map."""
    ct = CHARGE_TYPE_ALIASES.get(charge_type, charge_type)
    return db.scalar(select(ChargeItem).where(ChargeItem.charge_type == ct,
                                              ChargeItem.active == True))  # noqa: E712


def charge_type_by_item(db: Session) -> dict[str, str]:
    """ns_item_id -> charge_type, for tagging synced invoice lines back to a charge."""
    return {ci.ns_item_id: ci.charge_type
            for ci in db.scalars(select(ChargeItem)).all() if ci.charge_type}


def unmapped_charge_types(db: Session) -> list[str]:
    """Charge types that are actually priced somewhere but have no active NetSuite item.

    Surfaced in the admin console: each one is a charge that will compute happily, save to a
    draft, and then be refused at queue time — better to say so before the week is billed.
    """
    priced = {l.charge_type for l in db.scalars(
        select(RateCardLine).where(RateCardLine.rate != 0)).all()}
    return sorted(ct for ct in priced if charge_item_for(db, ct) is None)


def storage_rate(db: Session, customer_id: int) -> float:
    card = active_rate_card(db, customer_id, date.today())
    if not card:
        return 0.0
    for l in card.lines:
        if l.charge_type == "storage":
            return float(l.rate)
    return 0.0


# --- the 6 views -------------------------------------------------------------
def stock_on_order(db: Session, customer_id: int, imap: dict,
                   names: dict | None = None) -> list[dict]:
    names = names or {}
    pos = db.scalars(
        select(PurchaseOrder).where(PurchaseOrder.customer_id == customer_id,
                                    PurchaseOrder.status != "closed")
        .options(selectinload(PurchaseOrder.lines))
        .order_by(PurchaseOrder.trandate.desc())).all()
    # A PO line that's been added to an inbound shipment (container) gets the shipment's
    # doc number + its (authoritative) expected-receipt date and status surfaced here.
    shipments = {s.shipment_number: s for s in db.scalars(
        select(InboundShipment).where(InboundShipment.customer_id == customer_id)).all()
        if s.shipment_number}
    out = []
    for po in pos:
        for l in po.lines:
            outstanding = float(l.qty_ordered or 0) - float(l.qty_received or 0)
            if outstanding <= 0:
                continue
            ship = shipments.get(l.ns_inbound_shipment) if l.ns_inbound_shipment else None
            # Prefer the shipment's expected date once the line is on a container;
            # else fall back to the PO line's own expected date.
            expected = (ship.expected_date if ship and ship.expected_date
                        else l.expected_date)
            out.append({"tranid": po.tranid, "trandate": po.trandate, "status": po.status,
                        "sku": imap.get(l.ns_item_id, l.ns_item_id),
                        "name": names.get(l.ns_item_id, ""),
                        "ordered": float(l.qty_ordered or 0),
                        "received": float(l.qty_received or 0),
                        "outstanding": outstanding, "expected": expected,
                        "shipment": l.ns_inbound_shipment,
                        "container": ship.container_no if ship else None,
                        "shipment_status": ship.status if ship else None})
    return out


# --- list-view windowing / paging / search ------------------------------------
# Receipts, fulfilments and invoices grow without bound (Mova alone: ~470 receipts a year), so
# these views are windowed by date and paged rather than rendering all of history. Search
# deliberately ignores the window and runs over the whole history in SQL — a filter that
# silently only covered the visible page would report "not found" for a document that exists,
# which is unacceptable on a billing-adjacent view.
LIST_WINDOWS = ("14", "30", "90", "period", "all")
DEFAULT_WINDOW = "30"
PAGE_SIZE = 100


def window_since(window: str, today: date | None = None) -> date | None:
    """First date a list-view window includes. None = all history."""
    today = today or date.today()
    if window == "all":
        return None
    if window == "period":
        return week_bounds(today)[0]
    days = {"14": 14, "30": 30, "90": 90}.get(window, 30)
    return today - timedelta(days=days - 1)


def _text_match(q: str, item_col, doc_cols, imap: dict, names: dict):
    """OR-match q across document columns plus any item whose SKU/description contains it.

    imap/names are already loaded once per request, so resolving SKU text in Python keeps this
    to a single `ns_item_id IN (...)` term instead of another join.
    """
    like = f"%{q.lower()}%"
    ql = q.lower()
    ns_ids = {ns for ns, v in imap.items() if v and ql in str(v).lower()}
    ns_ids |= {ns for ns, v in names.items() if v and ql in str(v).lower()}
    ors = [func.lower(c).like(like) for c in doc_cols]
    if ns_ids:
        ors.append(item_col.in_(sorted(ns_ids)))
    return or_(*ors)


def item_receipts(db: Session, customer_id: int, imap: dict, names: dict | None = None,
                  *, since: date | None = None, q: str | None = None,
                  limit: int | None = None, offset: int = 0) -> dict:
    """Receipt LINES, newest first.

    Returns {rows, total, qty_total, docs} where total/qty_total/docs describe the WHOLE
    matching set, not the page — a footer that silently summed only the loaded rows would
    misreport against the putaway charge.

    Two queries regardless of volume (lines joined to their receipt); it used to be one query
    per receipt — 937 at two years of Mova's cadence.
    """
    names = names or {}
    conds = [ItemReceipt.customer_id == customer_id]
    if since is not None:
        conds.append(ItemReceipt.trandate >= since)
    if q:
        conds.append(_text_match(
            q, ItemReceiptLine.ns_item_id,
            [ItemReceipt.tranid, ItemReceipt.po_tranid, ItemReceipt.ns_inbound_shipment,
             InboundShipment.container_no],
            imap, names))
    # Outer-joined so the container number comes back in the same query (still 2 total) and a
    # receipt not on a shipment is still listed. Matched on the shipment DOC number, which is
    # what ingest_item_receipts stores on the receipt.
    ship_on = ((InboundShipment.shipment_number == ItemReceipt.ns_inbound_shipment)
               & (InboundShipment.customer_id == ItemReceipt.customer_id))
    join = (ItemReceiptLine.__table__
            .join(ItemReceipt.__table__, ItemReceiptLine.item_receipt_id == ItemReceipt.id)
            .outerjoin(InboundShipment.__table__, ship_on))
    agg = db.execute(
        select(func.count(), func.coalesce(func.sum(ItemReceiptLine.qty), 0),
               func.count(func.distinct(ItemReceipt.id)))
        .select_from(join).where(*conds)).one()
    stmt = (select(ItemReceiptLine, ItemReceipt, InboundShipment.container_no)
            .join(ItemReceipt, ItemReceiptLine.item_receipt_id == ItemReceipt.id)
            .outerjoin(InboundShipment, ship_on)
            .where(*conds)
            .order_by(ItemReceipt.trandate.desc(), ItemReceipt.id.desc(), ItemReceiptLine.id))
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)
    rows = [{"tranid": r.tranid, "trandate": r.trandate,
             "shipment": r.ns_inbound_shipment, "container": container, "po": r.po_tranid,
             "sku": imap.get(l.ns_item_id, l.ns_item_id),
             "name": names.get(l.ns_item_id, ""), "qty": float(l.qty)}
            for l, r, container in db.execute(stmt).all()]
    return {"rows": rows, "total": agg[0] or 0, "qty_total": float(agg[1] or 0),
            "docs": agg[2] or 0}


def recent_receipts(db: Session, customer_id: int, imap: dict, limit: int) -> list[dict]:
    """The N newest receipt lines — for the overview's Recent activity panel, which used to
    load every receipt in history and then slice the first two off the front."""
    return item_receipts(db, customer_id, imap, limit=limit)["rows"]


def stock_on_hand(db: Session, customer_id: int, imap: dict,
                  names: dict | None = None) -> list[dict]:
    """Latest snapshot per item, with storage/week derived from the rate card.
    The snapshot is refreshed in place every ~15 min, so rows carry synced_at
    (use soh_synced_at() for the single "live as at" time shown in the portal).
    Items currently at zero on hand are dropped from the view."""
    rate = storage_rate(db, customer_id)
    names = names or {}
    latest = db.scalar(
        select(StockOnHand.snapshot_date).where(StockOnHand.customer_id == customer_id)
        .order_by(StockOnHand.snapshot_date.desc()).limit(1))
    if latest is None:
        return []
    rows = db.scalars(
        select(StockOnHand).where(StockOnHand.customer_id == customer_id,
                                  StockOnHand.snapshot_date == latest)).all()
    out = []
    for s in rows:
        if float(s.qty_on_hand or 0) == 0:      # zeroed-out (shipped to nil) — hide from view
            continue
        pallets = (float(s.pallets) if s.pallets is not None else
                   (math.ceil(float(s.qty_on_hand) / s.units_per_pallet)
                    if s.units_per_pallet else 0))
        out.append({"sku": imap.get(s.ns_item_id, s.ns_item_id),
                    "name": names.get(s.ns_item_id, ""),
                    "qty_on_hand": float(s.qty_on_hand),
                    "units_per_pallet": s.units_per_pallet, "pallets": pallets,
                    "storage_per_week": round(pallets * rate, 2),
                    "snapshot_date": s.snapshot_date, "synced_at": s.synced_at})
    return out


def soh_synced_at(db: Session, customer_id: int):
    """The 'live as at' time for stock on hand — most recent synced_at on the latest
    snapshot day. Falls back to the snapshot date if synced_at was never written."""
    latest = db.scalar(
        select(StockOnHand.snapshot_date).where(StockOnHand.customer_id == customer_id)
        .order_by(StockOnHand.snapshot_date.desc()).limit(1))
    if latest is None:
        return None
    return db.scalar(
        select(func.max(StockOnHand.synced_at)).where(
            StockOnHand.customer_id == customer_id,
            StockOnHand.snapshot_date == latest)) or latest


def fulfilments(db: Session, customer_id: int, imap: dict, names: dict | None = None,
                *, since: date | None = None, q: str | None = None,
                limit: int | None = None, offset: int = 0) -> dict:
    """Fulfilment LINES, newest first. Same shape and 2-query cost as item_receipts()."""
    names = names or {}
    conds = [ItemFulfilment.customer_id == customer_id]
    if since is not None:
        conds.append(ItemFulfilment.trandate >= since)
    if q:
        conds.append(_text_match(
            q, ItemFulfilmentLine.ns_item_id,
            [ItemFulfilment.tranid, ItemFulfilment.ns_source_id, ItemFulfilment.source_type],
            imap, names))
    join = (ItemFulfilmentLine.__table__
            .join(ItemFulfilment.__table__,
                  ItemFulfilmentLine.item_fulfilment_id == ItemFulfilment.id))
    agg = db.execute(
        select(func.count(), func.coalesce(func.sum(ItemFulfilmentLine.qty), 0),
               func.count(func.distinct(ItemFulfilment.id)))
        .select_from(join).where(*conds)).one()
    stmt = (select(ItemFulfilmentLine, ItemFulfilment)
            .join(ItemFulfilment, ItemFulfilmentLine.item_fulfilment_id == ItemFulfilment.id)
            .where(*conds)
            .order_by(ItemFulfilment.trandate.desc(), ItemFulfilment.id.desc(),
                      ItemFulfilmentLine.id))
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)
    rows = [{"tranid": f.tranid, "trandate": f.trandate,
             "source": f.source_type, "ref": f.ns_source_id,
             "sku": imap.get(l.ns_item_id, l.ns_item_id), "qty": float(l.qty)}
            for l, f in db.execute(stmt).all()]
    return {"rows": rows, "total": agg[0] or 0, "qty_total": float(agg[1] or 0),
            "docs": agg[2] or 0}


def recent_fulfilments(db: Session, customer_id: int, imap: dict, limit: int) -> list[dict]:
    """The N newest fulfilment lines — see recent_receipts()."""
    return fulfilments(db, customer_id, imap, limit=limit)["rows"]


_MEMO_PERIOD = re.compile(r"(\d{4}-\d{2}-\d{2})\s*[–\-—to]{1,3}\s*(\d{4}-\d{2}-\d{2})")


def _period_from_memo(memo: str | None) -> tuple[date, date] | None:
    """Pull the billed period out of a NetSuite memo, e.g. '3PL charges 2026-07-27–2026-08-02'."""
    if not memo:
        return None
    m = _MEMO_PERIOD.search(memo)
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1)), date.fromisoformat(m.group(2))
    except ValueError:
        return None


def invoice_periods(db: Session, customer_id: int) -> dict[str, tuple[date, date]]:
    """ns_invoice_id -> the (start, end) week the invoice bills.

    An invoice's `trandate` is when it was RAISED, not the period it covers — INAU250127 is
    dated in August and bills 27 Jul–2 Aug. Without this the portal showed a date that
    answered the wrong question, and there was no way to tell which week an invoice was for.

    Two sources, in order of trust:
      1. The `billing_run` that pushed it (`ns_invoice_id`) — authoritative, and the period is
         the run's own, not something parsed out of text.
      2. The invoice memo, which the portal stamps as '3PL charges <from>–<to>' at create
         time. This is the only route for an invoice raised by hand in NetSuite, and it's why
         `createInvoice` writes that memo at all.
    """
    out: dict[str, tuple[date, date]] = {}
    for inv in db.scalars(select(Invoice).where(Invoice.customer_id == customer_id)).all():
        if (p := _period_from_memo(inv.memo)):
            out[inv.ns_invoice_id] = p
    for run in db.scalars(select(BillingRun).where(
            BillingRun.customer_id == customer_id,
            BillingRun.ns_invoice_id != None)).all():                # noqa: E711
        out[run.ns_invoice_id] = (run.period_start, run.period_end)
    return out


def invoices(db: Session, customer_id: int, *, since: date | None = None,
             q: str | None = None, limit: int | None = None, offset: int = 0) -> dict:
    """Invoice headers, newest first. qty_total is the summed invoice amount."""
    conds = [Invoice.customer_id == customer_id]
    if since is not None:
        conds.append(Invoice.trandate >= since)
    if q:
        like = f"%{q.lower()}%"
        conds.append(or_(func.lower(Invoice.tranid).like(like),
                         func.lower(Invoice.status).like(like)))
    agg = db.execute(
        select(func.count(), func.coalesce(func.sum(Invoice.total), 0))
        .select_from(Invoice.__table__).where(*conds)).one()
    stmt = (select(Invoice).where(*conds)
            .order_by(Invoice.trandate.desc(), Invoice.id.desc()))
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)
    rows = db.scalars(stmt).all()
    periods = invoice_periods(db, customer_id)
    # `period` is the pair the table renders; period_start/period_end are the same thing
    # flattened, because the CSV export writes row values straight out and a tuple would
    # land in Excel as "(datetime.date(...), ...)".
    return {"rows": [{"id": i.id, "tranid": i.tranid, "trandate": i.trandate,
                      "status": i.status, "period": periods.get(i.ns_invoice_id),
                      "period_start": (periods.get(i.ns_invoice_id) or (None, None))[0],
                      "period_end": (periods.get(i.ns_invoice_id) or (None, None))[1],
                      "total": float(i.total) if i.total is not None else None}
                     for i in rows],
            "total": agg[0] or 0, "qty_total": float(agg[1] or 0), "docs": agg[0] or 0}


def invoice_with_lines(db: Session, customer_id: int, invoice_id: int):
    """An invoice (scoped to the customer) plus its charge lines, or (None, [])."""
    inv = db.get(Invoice, invoice_id)
    if not inv or inv.customer_id != customer_id:
        return None, []
    lines = db.scalars(
        select(InvoiceLine).where(InvoiceLine.invoice_id == inv.id)
        .order_by(InvoiceLine.id)).all()
    rows = [{"charge_type": l.charge_type, "description": l.description,
             "qty": float(l.qty) if l.qty is not None else None,
             "rate": float(l.rate) if l.rate is not None else None,
             "amount": float(l.amount) if l.amount is not None else None} for l in lines]
    return inv, rows


def invoice_period(db: Session, inv: Invoice) -> tuple[date, date] | None:
    """The week a single invoice bills — the run that pushed it, else its memo."""
    run = db.scalar(select(BillingRun).where(
        BillingRun.customer_id == inv.customer_id,
        BillingRun.ns_invoice_id == inv.ns_invoice_id))
    return (run.period_start, run.period_end) if run else _period_from_memo(inv.memo)


def run_variance(db: Session, run: BillingRun) -> dict | None:
    """Compare a pushed run against the NetSuite invoice it created, line by line.

    NetSuite is the source of truth for what was actually billed — anyone may edit the draft
    invoice after the portal creates it, and the sync already pulls those edits back. What
    was missing was any comparison: the run said $33,000 and the invoice said $31,500 and
    nothing in the portal ever noticed. The run's own lines are deliberately NOT overwritten
    by this — they stay the record of what Macgear asked for, and the difference is shown.

    Matched on ns_item_id (resolved to charge_type), not description: descriptions are free
    text in NetSuite and are routinely edited, item is not. Lines NetSuite has that the run
    doesn't (someone added a charge in NetSuite) come back as `added`.

    Returns None when there's nothing to compare — no invoice synced back yet.
    """
    if not run.ns_invoice_id:
        return None
    inv = db.scalar(select(Invoice).where(Invoice.customer_id == run.customer_id,
                                          Invoice.ns_invoice_id == run.ns_invoice_id))
    if inv is None:
        return None

    def key(charge_type, ns_item_id):
        """Group by the item that will actually be invoiced, so the portal's separate
        picking_so / picking_vrma lines compare against NetSuite's single Picking line."""
        return ns_item_id or CHARGE_TYPE_ALIASES.get(charge_type, charge_type) or "?"

    ours: dict[str, dict] = {}
    for l in run.lines:
        if not l.billable:
            continue
        item = l.ns_item_id or (getattr(charge_item_for(db, l.charge_type), "ns_item_id", None))
        k = key(l.charge_type, item)
        e = ours.setdefault(k, {"label": l.description or l.charge_type, "qty": 0.0, "amount": 0.0})
        e["qty"] += float(l.qty or 0)
        e["amount"] += float(l.amount or 0)

    theirs: dict[str, dict] = {}
    for l in inv.lines:
        k = key(l.charge_type, l.ns_item_id)
        e = theirs.setdefault(k, {"label": l.description or l.charge_type or "—",
                                  "qty": 0.0, "amount": 0.0})
        e["qty"] += float(l.qty or 0)
        e["amount"] += float(l.amount or 0)

    rows = []
    for k in list(ours) + [k for k in theirs if k not in ours]:
        a, b = ours.get(k), theirs.get(k)
        rows.append({
            "label": (a or b)["label"],
            "run_qty": a["qty"] if a else None, "run_amount": a["amount"] if a else None,
            "ns_qty": b["qty"] if b else None, "ns_amount": b["amount"] if b else None,
            "delta": round((b["amount"] if b else 0.0) - (a["amount"] if a else 0.0), 2),
            "state": "added" if not a else "removed" if not b else
                     ("changed" if round(a["amount"] - b["amount"], 2) else "match"),
        })
    run_total = round(sum(r["run_amount"] or 0 for r in rows), 2)
    ns_total = float(inv.total) if inv.total is not None else None
    return {
        "invoice": inv, "rows": rows, "run_total": run_total, "ns_total": ns_total,
        "delta": None if ns_total is None else round(ns_total - run_total, 2),
        "differs": any(r["state"] != "match" for r in rows),
    }


def rate_card_lines(db: Session, customer_id: int) -> list[dict]:
    card = active_rate_card(db, customer_id, date.today())
    if not card:
        return []
    order = {"container_unload": 0, "putaway": 1, "storage": 2,
             "picking_so": 3, "picking_vrma": 4, "shipping": 5}
    return [{"label": l.label, "rate": float(l.rate), "basis": l.basis}
            for l in sorted(card.lines, key=lambda x: order.get(x.charge_type, 9))]


# --- nav + week anchoring ----------------------------------------------------
def nav_counts(db: Session, customer_id: int, imap: dict) -> dict:
    return {
        "stock_on_order": len({r["tranid"] for r in stock_on_order(db, customer_id, imap)}),
        "item_receipts": db.scalar(select(func.count()).select_from(ItemReceipt)
                                   .where(ItemReceipt.customer_id == customer_id)),
        # SKUs actually on hand (latest snapshot, qty>0) — NOT len(imap): the item master now
        # holds the customer's whole brand catalog (class-scoped, location-agnostic), so len(imap)
        # would show the full range (e.g. 305 MOVA SKUs) even with zero 3PL stock on hand.
        "stock_on_hand": len(stock_on_hand(db, customer_id, imap)),
        "fulfilments": db.scalar(select(func.count()).select_from(ItemFulfilment)
                                 .where(ItemFulfilment.customer_id == customer_id)),
        "invoices": db.scalar(select(func.count()).select_from(Invoice)
                              .where(Invoice.customer_id == customer_id)),
    }


def latest_activity_date(db: Session, customer_id: int) -> date | None:
    candidates = [
        db.scalar(select(func.max(ItemReceipt.trandate)).where(ItemReceipt.customer_id == customer_id)),
        db.scalar(select(func.max(ItemFulfilment.trandate)).where(ItemFulfilment.customer_id == customer_id)),
        db.scalar(select(func.max(InboundShipment.received_date)).where(InboundShipment.customer_id == customer_id)),
        db.scalar(select(func.max(StockOnHand.snapshot_date)).where(StockOnHand.customer_id == customer_id)),
    ]
    dates = [d for d in candidates if d]
    return max(dates) if dates else None


def week_bounds(d: date) -> tuple[date, date]:
    """Monday–Sunday week containing d."""
    mon = d - timedelta(days=d.weekday())
    return mon, mon + timedelta(days=6)


def received_per_week(db: Session, customer_id: int, weeks: list[tuple[date, date]]) -> list[dict]:
    """Item-receipt units per week — the same count billing charges putaway on
    (billing.py), but as a series instead of a single period.

    Summed in SQL: one query per week rather than one per week plus one per receipt.
    """
    out = []
    for s, e in weeks:
        total = db.scalar(
            select(func.coalesce(func.sum(ItemReceiptLine.qty), 0))
            .select_from(ItemReceiptLine.__table__.join(
                ItemReceipt.__table__, ItemReceiptLine.item_receipt_id == ItemReceipt.id))
            .where(ItemReceipt.customer_id == customer_id,
                   ItemReceipt.trandate >= s, ItemReceipt.trandate <= e))
        out.append({"label": s.strftime("%d %b"), "total": float(total or 0)})
    return out


def incoming_per_week(soo: list[dict], weeks: list[tuple[date, date]]) -> tuple[list[dict], dict]:
    """Bucket outstanding on-order units into the given weeks by expected arrival.

    Works off stock_on_order() rows, which already resolve the authoritative ETA (the
    inbound shipment's expected date when the line is on a container, else the PO line's
    own expectedreceiptdate).

    Anything that can't land in a bar is counted in the returned `rest` dict rather than
    silently dropped, so the bars always reconcile with the "units on order" KPI:
      overdue  — ETA already past (late, not undated: it needs chasing, not scheduling)
      later    — ETA beyond the 4-week window
      undated  — no ETA at all on the line or its shipment
    """
    buckets = [{"label": s.strftime("%d %b"), "total": 0.0} for s, _ in weeks]
    rest = {"overdue": 0.0, "later": 0.0, "undated": 0.0}
    first, last = weeks[0][0], weeks[-1][1]
    for r in soo:
        exp, qty = r.get("expected"), r["outstanding"]
        if not exp:
            rest["undated"] += qty
        elif exp < first:
            rest["overdue"] += qty
        elif exp > last:
            rest["later"] += qty
        else:
            for i, (s, e) in enumerate(weeks):
                if s <= exp <= e:
                    buckets[i]["total"] += qty
                    break
    return buckets, rest


# --- overview ----------------------------------------------------------------
def overview(db: Session, customer: Customer, imap: dict) -> dict:
    soh = stock_on_hand(db, customer.id, imap)
    soo = stock_on_order(db, customer.id, imap)
    # Anchored to the wall clock: the forward-looking series is meaningless against a
    # data-derived anchor, and mixing the two would put the three chart series on
    # different calendars.
    anchor = date.today()
    wk_start, wk_end = week_bounds(anchor)

    past_weeks = [week_bounds(anchor - timedelta(days=7 * i)) for i in range(3, -1, -1)]
    next_weeks = [week_bounds(anchor + timedelta(days=7 * i)) for i in range(4)]

    # current week charge breakdown + 4-week history, both from the billing engine
    # warn=False: the chart only reads totals, and the under-billing checks cost two extra
    # queries per call — 10 pointless Postgres round trips a page load. The billing view, where
    # the warnings actually matter, computes with them on.
    cur = compute_billing(db, customer, wk_start, wk_end, warn=False)
    by_type = {l.charge_type: l for l in cur.lines}
    history = [{"label": s.strftime("%d %b"),
                "total": compute_billing(db, customer, s, e, warn=False).total}
               for s, e in past_weeks]
    received = received_per_week(db, customer.id, past_weeks)
    incoming, incoming_rest = incoming_per_week(soo, next_weeks)

    # LIMIT in SQL, not slice-after-loading-everything: this pair used to pull every receipt
    # and fulfilment in history to show five rows.
    recent = (recent_receipts(db, customer.id, imap, 2) +
              recent_fulfilments(db, customer.id, imap, 3))
    for r in recent:
        r["kind"] = "Receipt" if "shipment" in r else "Fulfilment"
    recent.sort(key=lambda r: r["trandate"] or date.min, reverse=True)

    return {
        "brand": customer.brand_label or "", "location": customer.location_label or "",
        "skus": len(soh),   # SKUs on hand, not the whole brand catalog (see nav_counts)
        "soh_synced_at": soh_synced_at(db, customer.id),
        "units_on_hand": sum(r["qty_on_hand"] for r in soh),
        "pallets": sum(r["pallets"] for r in soh),
        "storage_per_week": sum(r["storage_per_week"] for r in soh),
        "units_on_order": sum(r["outstanding"] for r in soo),
        "open_pos": len({r["tranid"] for r in soo}),
        "week_start": wk_start, "week_end": wk_end,
        "week_total": cur.total,
        "week_lines": [{"type": ct, "label": (by_type[ct].label if ct in by_type else lbl),
                        "qty": (by_type[ct].qty if ct in by_type else 0),
                        "amount": (by_type[ct].amount if ct in by_type else 0.0)}
                       for ct, lbl in [("container_unload", "Container unload"),
                                       ("putaway", "Putaway"), ("storage", "Storage"),
                                       ("picking_so", "Picking — SO"),
                                       ("picking_vrma", "Picking — VRMA")]],
        # three selectable chart series; *_max is floored at 1 so the bar-height
        # division in the template can never hit zero
        "history": history,
        "history_max": max((h["total"] for h in history), default=0) or 1,
        "received": received,
        "received_max": max((h["total"] for h in received), default=0) or 1,
        "incoming": incoming,
        "incoming_max": max((h["total"] for h in incoming), default=0) or 1,
        "incoming_rest": incoming_rest,   # overdue / later / undated — see the chart note
        "recent": recent,
    }
