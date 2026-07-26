"""Seed the two known customers, Mova's validated rate card, and (locally) demo cache rows.

Run via `python -m app.seed` (run.ps1 does this on first launch). Idempotent: skips if a
customer already exists.

Demo cache rows (fake invoices/receipts/fulfilments) let the 6 portal views + billing render
before NetSuite is wired — useful locally, but they MUST NOT be planted on a real deploy where
n8n ingests live data. Set env `SEED_DEMO=0` (the Docker entrypoint does) to seed only the
customers, rate cards, and the admin/internal users — a clean base for the sandbox/prod sync.

Internal ids are the live values validated 2026-06-26 (docs/netsuite_validation.md).
"""
import math
import os
from datetime import date, timedelta

from sqlalchemy import select

from .db import Base, SessionLocal, engine, ensure_columns
from .models import (BillingLine, BillingRun, Customer, InboundShipment, Item,
                     ItemFulfilment, ItemFulfilmentLine, ItemReceipt, ItemReceiptLine,
                     Invoice, InvoiceLine, PoLine, PurchaseOrder, RateCard, RateCardLine,
                     StockOnHand, User)
from .security import hash_password

# Mova's rate card from the brief (charge_type, label, rate, basis).
MOVA_RATES = [
    ("container_unload", "Container unload — 40ft loose stacked", 1500.00, "per_container"),
    ("putaway",          "Putaway (per unit)",                       1.00, "per_unit"),
    ("storage",          "Storage (per pallet / week)",              4.50, "per_pallet_week"),
    ("picking_so",       "Picking — sales order (per unit)",         1.00, "per_unit"),
    ("picking_vrma",     "Picking — VRMA buy-in (per unit)",         1.00, "per_unit"),
]


def _rate_card(db, customer, rates, effective_from=date(2026, 1, 1)):
    rc = RateCard(customer_id=customer.id, effective_from=effective_from)
    db.add(rc)
    db.flush()
    for ct, label, rate, basis in rates:
        db.add(RateCardLine(rate_card_id=rc.id, charge_type=ct, label=label,
                            rate=rate, basis=basis))


def _seed_mova_demo(db, mova):
    """A month of plausible cached activity so every view renders with real-looking numbers.

    All dates are offsets from the Monday of the current week (`M`), not fixed calendar
    dates — the overview's chart series are anchored to the wall clock, so hardcoded dates
    would age out and leave the dashboard empty a few weeks after seeding.
    """
    today = date.today()
    M = today - timedelta(days=today.weekday())          # Monday of this week

    def d(days):
        return M + timedelta(days=days)

    items = [
        Item(customer_id=mova.id, ns_item_id="90001", sku="010201AA000437",
             description="MOVA Z50 Ultra Wet and Dry Robotic Vacuum", units_per_pallet=48),
        Item(customer_id=mova.id, ns_item_id="90002", sku="010901AA000024",
             description="MOVA Robotic Lawn Mower 1000", units_per_pallet=60),
    ]
    db.add_all(items)

    # Inbound shipments (containers) — three landed over the last three weeks, two still
    # in transit. The in-transit pair carries the outstanding PO stock + its expected
    # receipt date, and is what the "Incoming" chart series buckets by week.
    db.add_all([
        InboundShipment(customer_id=mova.id, ns_shipment_id="IS5001",
                        shipment_number="ISMOV0001", container_type="40ft loose stacked",
                        expected_date=d(-21), received_date=d(-21), status="received"),
        InboundShipment(customer_id=mova.id, ns_shipment_id="IS5002",
                        shipment_number="ISMOV0002", container_type="40ft loose stacked",
                        expected_date=d(-14), received_date=d(-13), status="received"),
        InboundShipment(customer_id=mova.id, ns_shipment_id="IS5003",
                        shipment_number="ISMOV0003", container_type="40ft loose stacked",
                        expected_date=d(-7), received_date=d(-5), status="received"),
        InboundShipment(customer_id=mova.id, ns_shipment_id="IS5004",
                        shipment_number="ISMOV0004", container_type="40ft loose stacked",
                        expected_date=d(9), received_date=None, status="in transit"),
        InboundShipment(customer_id=mova.id, ns_shipment_id="IS5005",
                        shipment_number="ISMOV0005", container_type="40ft loose stacked",
                        expected_date=d(23), received_date=None, status="in transit"),
    ])

    # Open POs (stock on order) — partially received. The first PO's outstanding lines sit
    # on the two in-transit containers, so they inherit a real ETA. The second PO has no
    # container yet and no line-level date, which is the "unscheduled" case the Incoming
    # chart reports separately instead of dropping.
    po = PurchaseOrder(customer_id=mova.id, ns_po_id="PO7001", tranid="POAU010001",
                       trandate=d(-30), status="open")
    po2 = PurchaseOrder(customer_id=mova.id, ns_po_id="PO7002", tranid="POAU010002",
                        trandate=d(-3), status="open")
    db.add_all([po, po2])
    db.flush()
    db.add_all([
        PoLine(purchase_order_id=po.id, ns_item_id="90001", qty_ordered=12000,
               qty_received=9000, ns_inbound_shipment="ISMOV0004"),
        PoLine(purchase_order_id=po.id, ns_item_id="90002", qty_ordered=8000,
               qty_received=5500, ns_inbound_shipment="ISMOV0005"),
        PoLine(purchase_order_id=po2.id, ns_item_id="90001", qty_ordered=2000,
               qty_received=0),
    ])

    # Item receipts (putaway) — the three landed containers plus one this week. Quantities
    # tie back to the PO lines above (90001: 6000+3000=9000, 90002: 4000+1500=5500).
    for ns_id, tranid, day, qty1, qty2 in [
        ("IR8001", "IRAU020001", d(-21), 6000, 0),
        ("IR8002", "IRAU020002", d(-13), 0, 4000),
        ("IR8003", "IRAU020003", d(-5), 3000, 0),
        ("IR8004", "IRAU020004", min(d(1), today), 0, 1500),
    ]:
        r = ItemReceipt(customer_id=mova.id, ns_receipt_id=ns_id, tranid=tranid, trandate=day)
        db.add(r)
        db.flush()
        if qty1:
            db.add(ItemReceiptLine(item_receipt_id=r.id, ns_item_id="90001", qty=qty1))
        if qty2:
            db.add(ItemReceiptLine(item_receipt_id=r.id, ns_item_id="90002", qty=qty2))

    # Weekly stock-on-hand snapshots (storage). Pallets = ceil(qoh / units_per_pallet).
    # The last snapshot is stamped today so the SOH view reads as live.
    upp = {"90001": 48, "90002": 60}
    for snap_date, oh1, oh2 in [(d(-21), 6000, 0), (d(-14), 5400, 0),
                                (d(-7), 7800, 3800), (today, 8200, 5100)]:
        for ns_item, oh in (("90001", oh1), ("90002", oh2)):
            if oh:
                db.add(StockOnHand(
                    customer_id=mova.id, snapshot_date=snap_date, ns_item_id=ns_item,
                    qty_on_hand=oh, units_per_pallet=upp[ns_item],
                    pallets=math.ceil(oh / upp[ns_item])))

    # Fulfilments (picking) — SO dispatches plus one VRMA buy-in.
    for ns_id, tranid, day, src, ns_item, qty in [
        ("IF9001", "IFAU030001", d(-19), "SO", "90001", 600),
        ("IF9002", "IFAU030002", d(-12), "SO", "90001", 400),
        ("IF9003", "IFAU030003", d(-4), "VRMA", "90002", 200),
        ("IF9004", "IFAU030004", min(d(2), today), "SO", "90001", 350),
    ]:
        f = ItemFulfilment(customer_id=mova.id, ns_fulfilment_id=ns_id, tranid=tranid,
                           trandate=day, source_type=src)
        db.add(f)
        db.flush()
        db.add(ItemFulfilmentLine(item_fulfilment_id=f.id, ns_item_id=ns_item, qty=qty))

    # A prior invoice (service charges already raised), with its charge-line breakdown
    # so the customer can drill through and see exactly what was billed. Lines sum to total.
    # Its period sits just before the 4-week window the dashboard shows, so it never
    # collides with the re-billing guard on a week the user might run.
    inv = Invoice(customer_id=mova.id, ns_invoice_id="INV6001", tranid="INAU040001",
                  trandate=d(-21), status="Open", total=12450.00)
    db.add(inv)
    db.flush()
    charge_lines = [
        ("container_unload", "Container unload — 40ft loose stacked", 1, 1500.00, 1500.00),
        ("putaway",          "Putaway (per unit)",                 6000, 1.00, 6000.00),
        ("storage",          "Storage (per pallet / week)",        1000, 4.50, 4500.00),
        ("picking_so",       "Picking — sales order (per unit)",    420, 1.00, 420.00),
        ("picking_vrma",     "Picking — VRMA buy-in (per unit)",     30, 1.00, 30.00),
    ]
    for ct, desc, qty, rate, amt in charge_lines:
        db.add(InvoiceLine(invoice_id=inv.id, charge_type=ct, description=desc,
                           qty=qty, rate=rate, amount=amt))

    # The completed billing run that produced INV6001 — shows the full loop
    # (run -> pushed -> invoiced) linked to the synced invoice via ns_invoice_id.
    run = BillingRun(customer_id=mova.id, period_start=d(-28), period_end=d(-22),
                     status="invoiced", ns_invoice_id=inv.ns_invoice_id)
    db.add(run)
    db.flush()
    for ct, desc, qty, rate, amt in charge_lines:
        db.add(BillingLine(billing_run_id=run.id, charge_type=ct, description=desc,
                           qty=qty, rate=rate, amount=amt))


def seed_users(db):
    """Seed an admin + a demo Mova customer user if no users exist (idempotent)."""
    if db.query(User).count() > 0:
        return
    mova = db.scalar(select(Customer).where(Customer.slug == "mova"))
    db.add(User(email="admin@macgeargroup.com", password_hash=hash_password("admin123"),
                role="admin"))
    db.add(User(email="ops@macgeargroup.com", password_hash=hash_password("internal123"),
                role="internal"))
    if mova:
        db.add(User(email="viewer@mova.com", password_hash=hash_password("mova123"),
                    role="customer", customer_id=mova.id))
    db.commit()
    print("Seeded users (CHANGE THESE PASSWORDS):")
    print("  admin@macgeargroup.com / admin123   (admin)")
    print("  ops@macgeargroup.com   / internal123 (internal)")
    print("  viewer@mova.com        / mova123      (customer · Mova)")


def seed():
    Base.metadata.create_all(engine)
    ensure_columns()   # add columns to a pre-existing db before we query them
    db = SessionLocal()
    try:
        if db.query(Customer).count() > 0:
            print("Customers already present — skipping customer/demo seed.")
            seed_users(db)
            return
        # Mova: regular MOVA brand (237) also covers Macgear-owned stock, so isolate 3PL activity by
        # the dedicated 3PL Warehouse location (49) too → location_scoped=True.
        mova = Customer(slug="mova", name="Mova", ns_customer_id="TBD",
                        ns_supplier_id="TBD", ns_location_id="49", ns_class_id="237",
                        ns_subsidiary_id="2", brand_label="MOVA", location_scoped=True,
                        location_label="3PL Warehouse · Melbourne")
        # Skriva: SKRIVA STYLUS brand (236) is 3PL-exclusive and its stock spans NZ locations
        # (Auckland + Christchurch), so scope by brand class only → location_scoped=False.
        skriva = Customer(slug="skriva", name="Skriva Stylus", ns_customer_id="10496",
                          ns_supplier_id="10503", ns_location_id="2", ns_class_id="236",
                          ns_subsidiary_id="3", brand_label="Skriva Stylus", location_scoped=False,
                          location_label="Auckland warehouse (no separate 3PL location)")
        db.add_all([mova, skriva])
        db.flush()
        _rate_card(db, mova, MOVA_RATES)
        # Skriva isn't billed 3PL fees today (validated); give it the same card shape at
        # nominal rates so its billing view demonstrates multi-tenant without implying real charges.
        _rate_card(db, skriva, [(ct, label, 0.00, basis) for ct, label, _, basis in MOVA_RATES])
        if os.environ.get("SEED_DEMO", "1") != "0":
            _seed_mova_demo(db, mova)
            print("Seeded customers (Mova, Skriva), rate cards, and Mova demo data.")
        else:
            print("Seeded customers (Mova, Skriva) and rate cards (SEED_DEMO=0: no demo cache).")
        db.commit()
        seed_users(db)
    finally:
        db.close()


if __name__ == "__main__":
    seed()
