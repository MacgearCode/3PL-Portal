"""The PO line's own expected-receipt date must survive ingest and reach the chart.

Background (why this test exists)
---------------------------------
The RESTlet's purchaseOrders() SuiteQL originally selected only item + quantities, so
po_line.expected_date was ALWAYS NULL in production. The Stock-on-order "Expected receipt"
column was therefore blank for every line not yet on an inbound shipment, and — once the
Incoming chart shipped — those units silently fell out of the bars into the footnote,
because incoming_per_week() can only bucket a row that has an ETA.

tl.expectedreceiptdate IS selectable (validated against live NetSuite 2026-07-26). This
test locks in the whole path: ingest keeps the line's date, the shipment's date still wins
when there is one, and incoming_per_week() sorts each row into the right bucket.

Runnable two ways:
    python tests/test_incoming_eta.py         # prints PASS / exits non-zero on failure
    pytest tests/test_incoming_eta.py
"""
import os
import sys
from datetime import date, timedelta

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import Base, SessionLocal, engine  # noqa: E402
from app import netsuite, service  # noqa: E402
from app.models import Customer  # noqa: E402

IMAP = {"50000": "SKU-A", "50001": "SKU-B"}

TODAY = date.today()
MON = TODAY - timedelta(days=TODAY.weekday())          # Monday of this week


def _d(days):
    return (MON + timedelta(days=days)).strftime("%d/%m/%Y")   # RESTlet's dd/mm/yyyy


def _fresh_customer(slug):
    """Both tests share one in-memory engine for the module, so each needs its own
    customer — slug is unique."""
    Base.metadata.create_all(engine)
    db = SessionLocal()
    cust = Customer(slug=slug, name="Mova", ns_customer_id="10000",
                    ns_supplier_id="10872", ns_location_id="49", ns_class_id="237")
    db.add(cust)
    db.commit()
    return db, cust


def test_po_line_expected_date_reaches_the_incoming_chart():
    db, cust = _fresh_customer("mova-buckets")

    # Four open lines, one per outcome the chart has to distinguish.
    netsuite.ingest(db, cust, "purchase_orders", [
        {"ns_po_id": "1", "tranid": "PO-1", "trandate": "2026-07-01", "status": "open",
         "lines": [{"ns_item_id": "50000", "qty_ordered": 265, "qty_received": 0,
                    "expected_date": _d(9)}]},        # week 2 of the window
        {"ns_po_id": "2", "tranid": "PO-2", "trandate": "2026-07-01", "status": "open",
         "lines": [{"ns_item_id": "50001", "qty_ordered": 100, "qty_received": 0,
                    "expected_date": _d(-9)}]},       # overdue
        {"ns_po_id": "3", "tranid": "PO-3", "trandate": "2026-07-01", "status": "open",
         "lines": [{"ns_item_id": "50001", "qty_ordered": 50, "qty_received": 0,
                    "expected_date": _d(60)}]},       # beyond the window
        {"ns_po_id": "4", "tranid": "PO-4", "trandate": "2026-07-01", "status": "open",
         "lines": [{"ns_item_id": "50001", "qty_ordered": 30, "qty_received": 0}]},   # no ETA
    ])

    rows = {r["tranid"]: r for r in service.stock_on_order(db, cust.id, IMAP)}
    assert rows["PO-1"]["expected"] == MON + timedelta(days=9), \
        "the PO line's own expectedreceiptdate must survive ingest"
    assert rows["PO-4"]["expected"] is None, "a line with no ETA must stay None"

    weeks = [service.week_bounds(TODAY + timedelta(days=7 * i)) for i in range(4)]
    buckets, rest = service.incoming_per_week(list(rows.values()), weeks)
    assert buckets[1]["total"] == 265, "a dated line must land in its own week's bar"
    assert sum(b["total"] for b in buckets) == 265
    assert rest == {"overdue": 100.0, "later": 50.0, "undated": 30.0}, \
        f"remainders must be classified separately, got {rest}"

    # Every outstanding unit is either in a bar or named in the remainder — the chart can
    # never disagree with the "units on order" KPI.
    assert sum(b["total"] for b in buckets) + sum(rest.values()) == 445

    db.close()


def test_shipment_eta_still_beats_the_po_line_date():
    """A booked container's ETA is more authoritative than the line's planning date."""
    db, cust = _fresh_customer("mova-shipment")
    netsuite.ingest(db, cust, "purchase_orders", [
        {"ns_po_id": "9", "tranid": "PO-9", "trandate": "2026-07-01", "status": "open",
         "lines": [{"ns_item_id": "50000", "qty_ordered": 265, "qty_received": 0,
                    "expected_date": _d(9)}]},
    ])
    netsuite.ingest(db, cust, "inbound_shipments", [
        {"ns_shipment_id": "IS1", "shipment_number": "INSHIP-1", "expected_date": _d(16),
         "status": "in transit", "lines": [{"po_tranid": "PO-9", "ns_item_id": "50000"}]},
    ])
    row = service.stock_on_order(db, cust.id, IMAP)[0]
    assert row["shipment"] == "INSHIP-1"
    assert row["expected"] == MON + timedelta(days=16), \
        "the shipment's expected date must win over the PO line's"
    db.close()


if __name__ == "__main__":
    test_po_line_expected_date_reaches_the_incoming_chart()
    test_shipment_eta_still_beats_the_po_line_date()
    print("PASS - PO-line ETA reaches the Incoming chart; shipment ETA still wins.")
