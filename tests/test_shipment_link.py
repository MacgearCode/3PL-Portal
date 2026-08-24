"""Regression test for the disappearing inbound-shipment / expected-receipt columns.

Background (why this test exists)
---------------------------------
The Stock-on-order view shows, per open PO line, an inbound-shipment number and an
expected-receipt date. Neither comes from the purchase_orders pull (the RESTlet's PO query
returns only item + qty) — they are stamped onto the PO line by the *later* inbound_shipments
ingest. Because ingest_purchase_orders deletes+recreates every PO line (replace semantics), a
naive rebuild blanks both fields on every sync, and they only reappear if inbound_shipments
succeeds in the SAME run. When that (heaviest) read errored/timed out, the columns disappeared
until the next good full run — visibly "coming back" only when the n8n code node was re-run.

The fix carries the stamped values forward across the PO delete+recreate. This test locks that in:
after a healthy full sync, a subsequent purchase_orders re-sync WITHOUT inbound_shipments must
leave the shipment number and expected date intact.

Runnable two ways:
    python tests/test_shipment_link.py        # prints PASS / exits non-zero on failure
    pytest tests/test_shipment_link.py         # if pytest is ever added
"""
import os
import sys

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import Base, SessionLocal, engine  # noqa: E402
from app import netsuite, service  # noqa: E402
from app.models import Customer  # noqa: E402

PO_ROWS = [{
    "ns_po_id": "9001", "tranid": "PO-9001", "trandate": "2026-06-01", "status": "open",
    "lines": [{"ns_item_id": "50101", "qty_ordered": 100, "qty_received": 0}],
}]
SHIP_ROWS = [{
    "ns_shipment_id": "IS1", "shipment_number": "INSHIP-1", "expected_date": "2026-07-20",
    "status": "in transit",
    "lines": [{"po_tranid": "PO-9001", "ns_item_id": "50101"}],
}]
IMAP = {"50101": "S-MOVA-1"}


def _fresh_customer():
    # Every module here shares one in-memory DB, so start from empty or the customer insert
    # collides with whatever an earlier module left behind (UNIQUE on customer.slug).
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    db = SessionLocal()
    cust = Customer(slug="mova", name="Mova", ns_customer_id="10000",
                    ns_supplier_id="10001", ns_location_id="49", ns_class_id="237")
    db.add(cust)
    db.commit()
    return db, cust


def test_shipment_link_survives_failed_inbound_shipments_pass():
    db, cust = _fresh_customer()

    # 1) Healthy full sync: PO then shipments -> columns populated.
    netsuite.ingest(db, cust, "purchase_orders", PO_ROWS)
    netsuite.ingest(db, cust, "inbound_shipments", SHIP_ROWS)
    row = service.stock_on_order(db, cust.id, IMAP)[0]
    assert row["shipment"] == "INSHIP-1", "healthy sync should populate the shipment number"
    assert str(row["expected"]) == "2026-07-20", "healthy sync should populate the expected date"

    # 2) Next sync re-runs purchase_orders but inbound_shipments FAILS (not called).
    #    The stamped values must be carried forward, not blanked.
    netsuite.ingest(db, cust, "purchase_orders", PO_ROWS)
    row = service.stock_on_order(db, cust.id, IMAP)[0]
    assert row["shipment"] == "INSHIP-1", "shipment number must survive a failed inbound_shipments pass"
    assert str(row["expected"]) == "2026-07-20", "expected date must survive a failed inbound_shipments pass"

    db.close()


if __name__ == "__main__":
    test_shipment_link_survives_failed_inbound_shipments_pass()
    print("PASS - shipment link + expected date survive a failed inbound_shipments pass.")
