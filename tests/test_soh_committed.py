"""Stock on hand: the Online (committed) / 3PL split (added 2026-11-17).

Mova wants their 3PL stock split in two: what is committed to open sales orders at the 3PL
location (held for their ONLINE channel) and the remainder (plain 3PL stock), plus the total.
The properties that matter:
- Online + 3PL always equals Total on hand, and neither goes negative.
- A feed with no committed figure (an older RESTlet, or the committed lookup failing) is
  "not known" (None, shown as a dash), never 0 Online.
- Storage still bills on the full on-hand: committed stock is physically stored.

Runnable two ways:
    python tests/test_soh_committed.py
    pytest tests/test_soh_committed.py
"""
import os
import sys

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import netsuite, service  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import Customer, Item, StockOnHand  # noqa: E402


def _fresh():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    db = SessionLocal()
    cust = Customer(slug="mova", name="Mova", ns_customer_id="10000", ns_location_id="49")
    db.add(cust)
    db.flush()
    db.add(Item(customer_id=cust.id, ns_item_id="1", sku="A", units_per_pallet=10))
    db.add(Item(customer_id=cust.id, ns_item_id="2", sku="B", units_per_pallet=10))
    db.commit()
    return db, cust


def _rows(db, cust):
    return {r["sku"]: r for r in service.stock_on_hand(db, cust.id, {"1": "A", "2": "B"})}


def test_split_sums_to_total():
    db, cust = _fresh()
    netsuite.ingest_stock_on_hand(db, cust, [
        {"ns_item_id": "1", "qty_on_hand": 100, "qty_committed": 30},
        {"ns_item_id": "2", "qty_on_hand": 50, "qty_committed": 0}])
    db.commit()
    r = _rows(db, cust)
    assert (r["A"]["qty_online"], r["A"]["qty_3pl"], r["A"]["qty_on_hand"]) == (30, 70, 100)
    assert (r["B"]["qty_online"], r["B"]["qty_3pl"]) == (0, 50)
    # storage is still on the full on-hand
    assert r["A"]["pallets"] == 10


def test_committed_is_capped_at_on_hand():
    db, cust = _fresh()
    netsuite.ingest_stock_on_hand(db, cust, [
        {"ns_item_id": "1", "qty_on_hand": 20, "qty_committed": 25}])
    db.commit()
    r = _rows(db, cust)["A"]
    assert (r["qty_online"], r["qty_3pl"]) == (20, 0)


def test_feed_without_committed_is_unknown_not_zero():
    db, cust = _fresh()
    netsuite.ingest_stock_on_hand(db, cust, [{"ns_item_id": "1", "qty_on_hand": 100}])
    db.commit()
    r = _rows(db, cust)["A"]
    assert r["qty_online"] is None and r["qty_3pl"] is None
    assert db.query(StockOnHand).one().qty_committed is None


def test_resync_updates_committed_in_place():
    db, cust = _fresh()
    netsuite.ingest_stock_on_hand(db, cust, [
        {"ns_item_id": "1", "qty_on_hand": 100, "qty_committed": 30}])
    db.commit()
    netsuite.ingest_stock_on_hand(db, cust, [
        {"ns_item_id": "1", "qty_on_hand": 90, "qty_committed": 20}])
    db.commit()
    r = _rows(db, cust)["A"]
    assert (r["qty_online"], r["qty_3pl"]) == (20, 70)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
