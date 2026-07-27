"""NetSuite ingest — the app's side of the n8n + RESTlet integration.

IMPORTANT: the droplet app does NOT talk to NetSuite. It holds no NetSuite credentials
and makes no outbound NetSuite calls. All NetSuite communication is server-to-server via:

    n8n (scheduler, signs Token-Based Auth)  ->  NetSuite RESTlet (netsuite/3pl_restlet.js)

  READS : n8n calls the RESTlet (runs the validated SuiteQL), then POSTs the rows to this
          app's token-authed /admin/ingest endpoint, which calls the ingest_* functions below.
  WRITES: clicking "Push" marks a billing run ready_to_push. n8n polls /admin/billing/pending,
          calls the RESTlet to create the DRAFT invoice, then POSTs the new id to
          /admin/billing/pushed. The app stores only the id — the next read-sync pulls the
          real invoice back, so status/edits/payments stay accurate.

No AI, no MCP, nothing interactive at runtime. (The Claude NetSuite MCP was a dev-time tool
used only to validate the SuiteQL — see docs/netsuite_validation.md — never the app.)

Row contracts (what /admin/ingest accepts as {customer, entity, rows}) are documented per
ingest_* function and produced by the RESTlet. dates may be 'YYYY-MM-DD' or 'dd/mm/yyyy'.
"""
import math
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (Customer, InboundShipment, Invoice, InvoiceLine, Item,
                     ItemFulfilment, ItemFulfilmentLine, ItemReceipt, ItemReceiptLine,
                     PoLine, PurchaseOrder, StockOnHand, SyncLog)


# --- helpers -----------------------------------------------------------------
def _date(s):
    if not s:
        return None
    head = str(s).split("T")[0].split(" ")[0]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(head, fmt).date()
        except ValueError:
            continue
    return None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _upsert(db: Session, model, ns_field: str, ns_value: str, **cols):
    obj = db.scalar(select(model).where(getattr(model, ns_field) == ns_value))
    if obj is None:
        obj = model(**{ns_field: ns_value}, **cols)
        db.add(obj)
    else:
        for k, v in cols.items():
            setattr(obj, k, v)
    return obj


# --- ingest functions (one per cache entity) ---------------------------------
def ingest_items(db: Session, c: Customer, rows: list[dict]) -> int:
    """The item master. rows: [{ns_item_id, sku, description, units_per_pallet?}].
    Resolves fact rows' NetSuite internal ids to human SKUs/descriptions in the portal."""
    for r in rows:
        per = r.get("units_per_pallet")
        _upsert(db, Item, "ns_item_id", str(r["ns_item_id"]),
                customer_id=c.id, sku=r.get("sku") or str(r["ns_item_id"]),
                description=r.get("description"),
                units_per_pallet=int(per) if per not in (None, "") else None)
    return len(rows)


def ingest_invoices(db: Session, c: Customer, rows: list[dict]) -> int:
    """rows: [{ns_invoice_id, tranid, trandate, status, total,
              lines:[{charge_type?, description, qty, rate, amount}]}]"""
    for r in rows:
        inv = _upsert(db, Invoice, "ns_invoice_id", str(r["ns_invoice_id"]),
                      customer_id=c.id, tranid=r.get("tranid"),
                      trandate=_date(r.get("trandate")), status=r.get("status"),
                      total=_num(r.get("total")))
        db.flush()
        for l in list(inv.lines):
            db.delete(l)
        for ln in r.get("lines", []):
            db.add(InvoiceLine(invoice_id=inv.id, charge_type=ln.get("charge_type"),
                               description=ln.get("description"), qty=_num(ln.get("qty")),
                               rate=_num(ln.get("rate")), amount=_num(ln.get("amount"))))
    return len(rows)


def ingest_purchase_orders(db: Session, c: Customer, rows: list[dict]) -> int:
    """rows: [{ns_po_id, tranid, trandate, status,
              lines:[{ns_item_id, qty_ordered, qty_received, expected_date,
                      ns_inbound_shipment?}]}]

    REPLACE SEMANTICS: the RESTlet returns the full set of *open* POs (lines with
    quantityshiprecv < quantity), with no incremental floor — so this list is the complete
    current open-PO picture. A PO that's been fully received drops out of the pull entirely;
    we must therefore PRUNE any cached PO for this customer not in the pull, or it lingers in
    'stock on order' forever showing its old outstanding. (Partial receipts stay in the pull
    with an updated qty_received, so their outstanding just shrinks.)
    """
    seen: set[str] = set()
    for r in rows:
        ns_po = str(r["ns_po_id"])
        seen.add(ns_po)
        po = _upsert(db, PurchaseOrder, "ns_po_id", ns_po,
                     customer_id=c.id, tranid=r.get("tranid"),
                     trandate=_date(r.get("trandate")), status=r.get("status"))
        db.flush()
        # PRESERVE THE SHIPMENT LINK across this delete+recreate. ns_inbound_shipment is not in
        # the PO pull — it's stamped onto the line by the LATER inbound_shipments ingest. A naive
        # rebuild blanks it every run, so if that pass errors/times out (it's the heaviest read)
        # the Stock-on-order shipment column vanishes until the next good full run. Snapshot it
        # (keyed by item, effectively unique per PO — see the match in ingest_inbound_shipments)
        # and carry it forward when the incoming pull doesn't supply it.
        # expected_date DOES come from the PO pull (tl.expectedreceiptdate); the carry-forward is
        # belt-and-braces for a row that arrives without one. Note the shipment's own
        # expecteddeliverydate is never written here — service.stock_on_order() prefers it at
        # READ time, so a booked container's ETA still wins over the line's planning date.
        prior = {l.ns_item_id: (l.ns_inbound_shipment, l.expected_date) for l in po.lines}
        for l in list(po.lines):
            db.delete(l)
        for ln in r.get("lines", []):
            ns_item = str(ln["ns_item_id"])
            kept_ship, kept_expected = prior.get(ns_item, (None, None))
            db.add(PoLine(purchase_order_id=po.id, ns_item_id=ns_item,
                          qty_ordered=_num(ln.get("qty_ordered")),
                          qty_received=_num(ln.get("qty_received")),
                          expected_date=_date(ln.get("expected_date")) or kept_expected,
                          ns_inbound_shipment=ln.get("ns_inbound_shipment") or kept_ship))
    # Drop POs that are no longer open (fully received/closed → absent from the pull).
    for po in db.scalars(select(PurchaseOrder).where(
            PurchaseOrder.customer_id == c.id)).all():
        if po.ns_po_id not in seen:
            db.delete(po)
    return len(rows)


def ingest_item_receipts(db: Session, c: Customer, rows: list[dict]) -> int:
    """rows: [{ns_receipt_id, tranid, trandate, ns_inbound_shipment, po_tranid,
              lines:[{ns_item_id, qty}]}]"""
    for r in rows:
        rec = _upsert(db, ItemReceipt, "ns_receipt_id", str(r["ns_receipt_id"]),
                      customer_id=c.id, tranid=r.get("tranid"),
                      trandate=_date(r.get("trandate")),
                      ns_inbound_shipment=r.get("ns_inbound_shipment"),
                      po_tranid=r.get("po_tranid"))
        db.flush()
        for l in list(rec.lines):
            db.delete(l)
        for ln in r.get("lines", []):
            db.add(ItemReceiptLine(item_receipt_id=rec.id, ns_item_id=str(ln["ns_item_id"]),
                                   qty=_num(ln.get("qty")) or 0))
    return len(rows)


def ingest_item_fulfilments(db: Session, c: Customer, rows: list[dict]) -> int:
    """rows: [{ns_fulfilment_id, tranid, trandate, source_type('SO'|'VRMA'),
              ns_source_id, lines:[{ns_item_id, qty}]}]"""
    for r in rows:
        f = _upsert(db, ItemFulfilment, "ns_fulfilment_id", str(r["ns_fulfilment_id"]),
                    customer_id=c.id, tranid=r.get("tranid"),
                    trandate=_date(r.get("trandate")),
                    source_type=r.get("source_type", "SO"), ns_source_id=r.get("ns_source_id"))
        db.flush()
        for l in list(f.lines):
            db.delete(l)
        for ln in r.get("lines", []):
            db.add(ItemFulfilmentLine(item_fulfilment_id=f.id, ns_item_id=str(ln["ns_item_id"]),
                                      qty=_num(ln.get("qty")) or 0))
    return len(rows)


def ingest_inbound_shipments(db: Session, c: Customer, rows: list[dict]) -> int:
    """rows: [{ns_shipment_id, shipment_number, container_type, container_no, expected_date,
              received_date, status, lines?:[{ns_item_id, po_tranid}]}]

    Member `lines` are optional and link the shipment back to the PO lines it contains, so the
    Stock-on-order view can show each open line's inbound shipment + expected receipt date. This
    stamps PoLine.ns_inbound_shipment, so it must run AFTER purchase_orders in the sync (which
    rebuilds PO lines each run); the n8n READ_ENTITIES order ensures that.
    """
    for r in rows:
        _upsert(db, InboundShipment, "ns_shipment_id", str(r["ns_shipment_id"]),
                customer_id=c.id, shipment_number=r.get("shipment_number"),
                container_type=r.get("container_type"),
                container_no=r.get("container_no"),
                expected_date=_date(r.get("expected_date")),
                received_date=_date(r.get("received_date")), status=r.get("status"))
        num = r.get("shipment_number")
        for m in r.get("lines", []):
            po_tranid, ns_item = m.get("po_tranid"), str(m.get("ns_item_id") or "")
            if not (num and po_tranid and ns_item):
                continue
            line = db.scalar(select(PoLine).join(PurchaseOrder).where(
                PurchaseOrder.customer_id == c.id, PurchaseOrder.tranid == po_tranid,
                PoLine.ns_item_id == ns_item))
            if line:
                line.ns_inbound_shipment = num
    return len(rows)


def ingest_stock_on_hand(db: Session, c: Customer, rows: list[dict]) -> int:
    """A full current-state snapshot for today, written in place each ~15-min sync.

    rows: [{ns_item_id, qty_on_hand, units_per_pallet?}]. NetSuite's inventorybalance returns
    MULTIPLE rows per item (per status/bin, incl. +/- pairs), so first aggregate to one net qty
    per item — that keeps the (customer, today, item) snapshot key unique and yields the true
    on-hand. Pallets = ceil(qty/units_per_pallet); units_per_pallet falls back to the Item record.

    REPLACE SEMANTICS: this is treated as the complete on-hand set for the customer *as at now*.
    An item whose stock has dropped to zero usually disappears from inventorybalance entirely
    (no row), so any of today's existing rows NOT in this pull are zeroed — otherwise a fully
    shipped-out SKU would show its last non-zero qty forever in the near-live view. (If NetSuite
    instead returns explicit qty=0 rows, those are handled the same way, so this is correct either
    way.) Older days' snapshots are left untouched as history for billing.
    """
    now = datetime.utcnow()
    today = date.today()
    upp = {i.ns_item_id: i.units_per_pallet
           for i in db.scalars(select(Item).where(Item.customer_id == c.id)).all()}
    agg: dict[str, dict] = {}
    for r in rows:
        ns_item = str(r["ns_item_id"])
        a = agg.setdefault(ns_item, {"qty": 0.0, "per": None})
        a["qty"] += _num(r.get("qty_on_hand")) or 0
        if a["per"] is None and r.get("units_per_pallet"):
            a["per"] = r.get("units_per_pallet")

    def _pallets(qty, per):
        return math.ceil(qty / per) if per and qty > 0 else (0 if per else None)

    existing_today = {s.ns_item_id: s for s in db.scalars(select(StockOnHand).where(
        StockOnHand.customer_id == c.id, StockOnHand.snapshot_date == today)).all()}

    for ns_item, a in agg.items():
        qty = a["qty"]
        per = a["per"] or upp.get(ns_item)
        pallets = _pallets(qty, per)
        existing = existing_today.get(ns_item)
        if existing:
            existing.qty_on_hand, existing.units_per_pallet = qty, per
            existing.pallets, existing.synced_at = pallets, now
        else:
            db.add(StockOnHand(customer_id=c.id, snapshot_date=today, ns_item_id=ns_item,
                               qty_on_hand=qty, units_per_pallet=per, pallets=pallets,
                               synced_at=now))

    # Zero today's rows for items no longer reported on hand (replace semantics).
    for ns_item, s in existing_today.items():
        if ns_item not in agg and float(s.qty_on_hand or 0) != 0:
            s.qty_on_hand = 0
            s.pallets = 0 if s.units_per_pallet else None
            s.synced_at = now
    return len(agg)


INGEST = {
    "items": ingest_items,
    "invoices": ingest_invoices,
    "purchase_orders": ingest_purchase_orders,
    "item_receipts": ingest_item_receipts,
    "item_fulfilments": ingest_item_fulfilments,
    "inbound_shipments": ingest_inbound_shipments,
    "stock_on_hand": ingest_stock_on_hand,
}


def ingest(db: Session, customer: Customer, entity: str, rows: list[dict]) -> int:
    """Dispatch + upsert + log. Returns rows ingested. Raises KeyError on unknown entity."""
    fn = INGEST[entity]
    log = SyncLog(entity=entity, customer_id=customer.id)
    db.add(log)
    n = fn(db, customer, rows)
    log.finished_at = datetime.utcnow()
    log.rows_upserted = n
    log.status = "ok"
    db.commit()
    return n
