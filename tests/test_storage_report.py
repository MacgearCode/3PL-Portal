"""Storage report tests — the page that explains the storage charge (`service.storage_report`).

Background (why this test exists)
---------------------------------
Storage is the only one of the five charges whose quantity is an **average** rather than a count
of documents you can go and look at. Every other line on an invoice can be traced back — count
the containers, sum the receipt lines — but "2,300 pallet-weeks" is a figure nobody can
reconstruct, and the SOH snapshot it comes from is overwritten in place every ~15 minutes.

The report exists to show that working. Which makes one property non-negotiable: **it must
agree with the invoice.** A report quoting numbers of its own, drifting a little from what was
billed, would be worse than no report at all — it would be used to defend a figure it does not
actually compute. So everything pallet-shaped in the report comes from `billing.daily_pallets()`
/ `billing.pallets_of()`, and the tests below assert the reconciliation rather than trusting it.

The other thing under test is the treatment of a day the sync never covered. Storage averages
over the days *present* (a week with a Saturday outage bills the average of the other six, not
6/7 of it), so a missing day must stay visibly missing all the way through — absent from the
average, a gap in the chart, and a dimmed row in the table. Rendering it as a zero would both
misdraw the chart and contradict the bill.

Runnable two ways:
    python tests/test_storage_report.py       # prints PASS / exits non-zero on failure
    pytest tests/test_storage_report.py
"""
import math
import os
import sys
from datetime import date, timedelta

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import service  # noqa: E402
from app.billing import compute_billing  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import (Customer, Item, ItemFulfilment, ItemFulfilmentLine,  # noqa: E402
                        ItemReceipt, ItemReceiptLine, RateCard, RateCardLine, StockOnHand)
from app.seed import MOVA_RATES  # noqa: E402

MON, SUN = date(2026, 7, 20), date(2026, 7, 26)


def _fresh_customer():
    """A clean in-memory DB with Mova's real rate card. Each test gets its own."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    db = SessionLocal()
    cust = Customer(slug="mova", name="Mova", ns_customer_id="10000", ns_location_id="49")
    db.add(cust)
    db.flush()
    rc = RateCard(customer_id=cust.id, effective_from=date(2026, 1, 1))
    db.add(rc)
    db.flush()
    for ct, label, rate, basis in MOVA_RATES:
        db.add(RateCardLine(rate_card_id=rc.id, charge_type=ct, label=label,
                            rate=rate, basis=basis))
    db.add(Item(customer_id=cust.id, ns_item_id="50001", sku="MOVA-A", units_per_pallet=12))
    db.add(Item(customer_id=cust.id, ns_item_id="50002", sku="MOVA-B", units_per_pallet=24))
    db.add(Item(customer_id=cust.id, ns_item_id="52856", sku="MOVA-NULL", units_per_pallet=None))
    db.commit()
    return db, cust


def _snapshot(db, cust, day, items):
    """Write a backdated SOH snapshot directly — netsuite.ingest_stock_on_hand always writes
    date.today() (it's the near-live lane) so it cannot produce historical days."""
    for ns_item, (qty, per) in items.items():
        db.add(StockOnHand(customer_id=cust.id, snapshot_date=day, ns_item_id=ns_item,
                           qty_on_hand=qty, units_per_pallet=per,
                           pallets=(math.ceil(qty / per) if per else None)))
    db.commit()


def _report(db, cust, period=(MON, SUN)):
    return service.storage_report(db, cust.id, period[0], period[1],
                                  service.item_map(db, cust.id),
                                  service.item_names(db, cust.id))


def _week(db, cust, per_day, item="50001", per=12):
    """One snapshot per given weekday offset: {0: pallets_mon, 1: pallets_tue, ...}."""
    for offset, pallets in per_day.items():
        _snapshot(db, cust, MON + timedelta(days=offset), {item: (pallets * per, per)})


def test_report_reconciles_to_the_invoice():
    """The whole point of the page. If these two ever disagree, the report is a liability."""
    db, cust = _fresh_customer()
    _week(db, cust, {0: 1103, 1: 1259, 2: 1305, 3: 1205, 4: 1180, 5: 1180, 6: 1180})

    rep = _report(db, cust)
    billed = next(l for l in compute_billing(db, cust, MON, SUN).lines
                  if l.charge_type == "storage")
    assert rep["pallet_weeks"] == billed.qty, (rep["pallet_weeks"], billed.qty)
    assert rep["storage_cost"] == billed.amount, (rep["storage_cost"], billed.amount)
    assert rep["avg_pallets"] == billed.qty, "a whole week is 1.0 weeks, so avg == pallet-weeks"
    db.close()


def test_a_day_the_sync_missed_is_a_gap_not_a_zero():
    """A day with no snapshot is unknown, not empty.

    Storage averages over the days present, so the report must too: Sat/Sun missing from a week
    holding 1,000 pallets means the week bills 1,000, not 5/7 of it. The day still appears in
    the series (7 calendar days, `missing=True`) so the gap is visible rather than absent.
    """
    db, cust = _fresh_customer()
    _week(db, cust, {0: 1000, 1: 1000, 2: 1000, 3: 1000, 4: 1000})     # Sat + Sun never synced

    rep = _report(db, cust)
    assert [x["missing"] for x in rep["days"]] == [False] * 5 + [True] * 2
    assert [x["pallets"] for x in rep["days"][5:]] == [None, None], "not zero — unknown"
    assert (rep["days_present"], rep["days_expected"]) == (5, 7)
    assert rep["avg_pallets"] == 1000.0, "averaged over the 5 days that exist, not diluted by 7"
    billed = next(l for l in compute_billing(db, cust, MON, SUN).lines
                  if l.charge_type == "storage")
    assert rep["pallet_weeks"] == billed.qty, "and it still matches the bill"
    db.close()


def test_peak_and_low_ignore_the_missing_days():
    """`min()` over a series containing None is a TypeError in py3 — and a silent 0 if guarded
    the lazy way. Peak/low describe the days actually observed."""
    db, cust = _fresh_customer()
    _week(db, cust, {0: 900, 1: 1400, 2: 1100})                        # Thu-Sun never synced

    rep = _report(db, cust)
    assert rep["peak"]["pallets"] == 1400 and rep["peak"]["date"] == MON + timedelta(days=1)
    assert rep["low"]["pallets"] == 900 and rep["low"]["date"] == MON
    db.close()


def test_per_sku_columns_sum_to_the_day_totals():
    """The by-SKU table is the same pallets sliced differently, so every column must add up.
    Both use billing.pallets_of(); this catches the two ever drifting apart."""
    db, cust = _fresh_customer()
    for i in range(7):
        d = MON + timedelta(days=i)
        _snapshot(db, cust, d, {"50001": (715 + i * 10, 12), "50002": (2400, 24)})

    rep = _report(db, cust)
    for i, day in enumerate(rep["days"]):
        col = sum(s["per_day"][i] for s in rep["skus"])
        assert col == day["pallets"], (day["date"], col, day["pallets"])
    db.close()


def test_a_sku_with_no_pallet_quantity_stays_in_the_table_at_zero():
    """Three real Mova SKUs have custitem_pallet_quantity NULL and bill 0 pallets however much
    stock they hold. Dropping them from the table is how that keeps going unnoticed — the whole
    reason the report is worth having is that it makes the zero visible."""
    db, cust = _fresh_customer()
    _snapshot(db, cust, MON, {"50001": (1200, 12), "52856": (8000, None)})

    rep = _report(db, cust)
    by_sku = {s["sku"]: s for s in rep["skus"]}
    assert "MOVA-NULL" in by_sku, "a SKU billing nothing must not vanish from the report"
    assert by_sku["MOVA-NULL"]["no_pallet_qty"] is True
    assert by_sku["MOVA-NULL"]["peak"] == 0.0
    assert rep["days"][0]["pallets"] == 100.0, "only the SKU with a pallet quantity is counted"
    assert rep["days"][0]["units"] == 9200.0, "but its units are still on hand and shown"
    db.close()


def test_movement_series_is_dated_by_the_document():
    """Receipts and dispatches come from trandate, so a quiet day is a real 0 — unlike a
    missing snapshot. They are what answer 'why did Wednesday jump'."""
    db, cust = _fresh_customer()
    _week(db, cust, {i: 1000 for i in range(7)})
    r = ItemReceipt(customer_id=cust.id, ns_receipt_id="IR1", tranid="IR1",
                    trandate=MON + timedelta(days=2))
    db.add(r)
    db.flush()
    db.add(ItemReceiptLine(item_receipt_id=r.id, ns_item_id="50001", qty=4800))
    f = ItemFulfilment(customer_id=cust.id, ns_fulfilment_id="IF1", tranid="IF1",
                       trandate=MON + timedelta(days=4), source_type="SO")
    db.add(f)
    db.flush()
    db.add(ItemFulfilmentLine(item_fulfilment_id=f.id, ns_item_id="50001", qty=900))
    db.commit()

    rep = _report(db, cust)
    assert rep["days"][2]["received"] == 4800 and rep["days"][4]["dispatched"] == 900
    assert rep["days"][0]["received"] == 0.0, "a quiet day is a real zero, not a gap"
    assert (rep["units_received"], rep["units_dispatched"]) == (4800.0, 900.0)
    db.close()


def test_trend_covers_twelve_weeks_ending_with_the_selected_one():
    """A single week's pallet figure says nothing about direction. Weeks the sync never reached
    come back at 0 with days_present 0, which is what lets the chart draw them as gaps."""
    db, cust = _fresh_customer()
    for w in range(3):                                   # only the last 3 weeks were synced
        ws = MON - timedelta(weeks=w)
        for i in range(7):
            _snapshot(db, cust, ws + timedelta(days=i), {"50001": ((100 + w * 10) * 12, 12)})

    rep = _report(db, cust)
    assert len(rep["trend"]) == service.TREND_WEEKS == 12
    assert rep["trend"][-1]["current"] is True, "the selected week is the last bar"
    assert rep["trend"][-1]["week_start"] == MON
    assert [t["days_present"] for t in rep["trend"]] == [0] * 9 + [7, 7, 7]
    assert [t["avg_pallets"] for t in rep["trend"][:9]] == [0.0] * 9
    assert rep["trend"][-1]["avg_pallets"] == 100.0
    db.close()


def test_an_unsynced_week_reports_nothing_rather_than_zero_pallets():
    """Any week before the sync started. The page must say there is nothing to report, not
    draw a flat week at zero — that reads as an empty warehouse."""
    db, cust = _fresh_customer()
    _week(db, cust, {i: 1000 for i in range(7)})

    rep = _report(db, cust, (MON - timedelta(weeks=4), SUN - timedelta(weeks=4)))
    assert rep["days_present"] == 0
    assert rep["avg_pallets"] == 0.0 and rep["pallet_weeks"] == 0.0
    assert all(x["missing"] for x in rep["days"])
    assert rep["peak"] is None and rep["low"] is None, "nothing observed, nothing to quote"
    assert rep["skus"] == []
    # The bar-height divisors must never be 0 — the template divides by them.
    assert rep["pallets_max"] and rep["units_max"] and rep["move_max"] and rep["trend_max"]
    db.close()


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    for t in TESTS:
        t()
        print(f"  ok  {t.__name__}")
    print(f"PASS - {len(TESTS)} storage report tests.")
