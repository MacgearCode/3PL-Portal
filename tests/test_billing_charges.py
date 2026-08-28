"""Charge-engine tests — container-unload dating, storage averaging, under-bill warnings.

Background (why this test exists)
---------------------------------
`billing.py` had no test coverage at all, and a wrong-week charge shipped to production
because of it. The container-unload charge counted inbound shipments by `received_date`,
which the RESTlet derives from COALESCE(actualdeliverydate, lastmodifieddate). In production
`actualdeliverydate` is NULL on every Mova 3PL shipment, so the fallback always applied — and
`lastmodifieddate` is the timestamp of whoever last touched the record, not a delivery. Nine
containers physically unloaded 2026-07-20 carried a lastmodifieddate of 2026-07-30, so the
20-26 Jul week billed **0** containers ($0 instead of $13,500) and the following week billed
**22** — across a month boundary.

The charge is now dated off the item receipt's `trandate`, reached via
`item_receipt.ns_inbound_shipment`. That field never moves when a record is edited, so the
charge is idempotent. These tests lock in the dating, the once-only rule for a container split
across periods, the storage average, and the warnings that make an under-bill visible.

Runnable two ways:
    python tests/test_billing_charges.py       # prints PASS / exits non-zero on failure
    pytest tests/test_billing_charges.py       # if pytest is ever added

Note if pytest is ever added: every test file here builds its own customer against the same
in-memory DB, so a single pytest process needs a reset fixture — `_fresh_customer()` below
drop_all's first, but `test_incoming_eta` / `test_shipment_link` don't, and they'd hit the
unique-slug constraint on leftover rows. Pre-existing; run the files standalone until then.
"""
import math
import os
import sys
from datetime import date

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import Base, SessionLocal, engine  # noqa: E402
from app import netsuite, service  # noqa: E402
from app.billing import compute_billing  # noqa: E402
from app.models import Customer, RateCard, RateCardLine, StockOnHand  # noqa: E402
from app.seed import MOVA_RATES  # noqa: E402

WEEK_A = (date(2026, 7, 20), date(2026, 7, 26))   # the week the 9 containers landed
WEEK_B = (date(2026, 7, 27), date(2026, 8, 2))    # where lastmodifieddate wrongly put them


def _fresh_customer():
    """A clean in-memory DB with Mova's real rate card. Each test gets its own."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    db = SessionLocal()
    cust = Customer(slug="mova", name="Mova", ns_customer_id="10000",
                    ns_supplier_id="10872", ns_location_id="49", ns_class_id="237")
    db.add(cust)
    db.flush()
    rc = RateCard(customer_id=cust.id, effective_from=date(2026, 1, 1))
    db.add(rc)
    db.flush()
    for ct, label, rate, basis in MOVA_RATES:
        db.add(RateCardLine(rate_card_id=rc.id, charge_type=ct, label=label,
                            rate=rate, basis=basis))
    db.commit()
    return db, cust


def _line(res, charge_type):
    return next((l for l in res.lines if l.charge_type == charge_type), None)


def _receipt(ns_id, tranid, trandate, shipment, qty=715, item="50001"):
    return {"ns_receipt_id": ns_id, "tranid": tranid, "trandate": trandate,
            "ns_inbound_shipment": shipment, "po_tranid": None,
            "lines": [{"ns_item_id": item, "qty": qty}]}


def _shipment(ns_id, number, received_date, status="received"):
    return {"ns_shipment_id": ns_id, "shipment_number": number, "container_type": None,
            "container_no": None, "expected_date": None, "received_date": received_date,
            "status": status, "lines": []}


def _snapshot(db, cust, day, items):
    """Write a backdated SOH snapshot directly.

    NOT via netsuite.ingest_stock_on_hand — that deliberately always writes to date.today()
    (it's the near-live 15-min lane), so it cannot produce the historical days billing reads.
    `pallets` is precomputed at sync time in production, so precompute it here too; pass
    units_per_pallet=None to leave it NULL and exercise billing.py's recompute fallback.
    """
    for ns_item, (qty, per) in items.items():
        db.add(StockOnHand(customer_id=cust.id, snapshot_date=day, ns_item_id=ns_item,
                           qty_on_hand=qty, units_per_pallet=per,
                           pallets=(math.ceil(qty / per) if per else None)))
    db.commit()


def test_container_charge_follows_the_receipt_not_the_shipment_header():
    """The production regression: receipt in week A, shipment header dated week B -> bills in A."""
    db, cust = _fresh_customer()
    # Nine containers, receipts all dated 20 Jul (week A), shipment headers stamped 30 Jul
    # (week B) exactly as production had them.
    netsuite.ingest(db, cust, "item_receipts", [
        _receipt(f"IR{i}", f"IR02398{i}", "2026-07-20", f"INBSHIP9{i}") for i in range(1, 10)])
    netsuite.ingest(db, cust, "inbound_shipments", [
        _shipment(f"9{i}", f"INBSHIP9{i}", "2026-07-30") for i in range(1, 10)])

    a = compute_billing(db, cust, *WEEK_A)
    assert _line(a, "container_unload").qty == 9, "week A must bill all 9 containers"
    assert _line(a, "container_unload").amount == 13500.00, "9 x $1,500"
    assert _line(a, "putaway").qty == 9 * 715, "putaway already worked; must not regress"

    b = compute_billing(db, cust, *WEEK_B)
    assert _line(b, "container_unload") is None, \
        "week B must bill NO containers — this is the bug that shipped"
    db.close()


def test_container_split_across_periods_is_charged_once_in_the_earlier():
    """Two receipts for one container in different weeks -> one charge, in the first week."""
    db, cust = _fresh_customer()
    netsuite.ingest(db, cust, "item_receipts", [
        _receipt("IR1", "IR000001", "2026-07-22", "INBSHIP91", qty=400),
        _receipt("IR2", "IR000002", "2026-07-29", "INBSHIP91", qty=315),
    ])

    a = compute_billing(db, cust, *WEEK_A)
    b = compute_billing(db, cust, *WEEK_B)
    assert _line(a, "container_unload").qty == 1, "charged in the week of the earliest receipt"
    assert _line(b, "container_unload") is None, "must not be charged a second time"
    # Putaway still follows each receipt's own units — only the container is once-only.
    assert _line(a, "putaway").qty == 400
    assert _line(b, "putaway").qty == 315
    db.close()


def test_receipt_with_no_container_link_warns_instead_of_silently_under_billing():
    db, cust = _fresh_customer()
    netsuite.ingest(db, cust, "item_receipts", [
        _receipt("IR1", "IR000001", "2026-07-20", "INBSHIP91"),
        _receipt("IR2", "IR000002", "2026-07-21", None),      # RESTlet lookup blanked it
    ])
    res = compute_billing(db, cust, *WEEK_A)
    assert _line(res, "container_unload").qty == 1, "only the linked container is charged"
    assert any("not linked to a container" in w and "IR000002" in w for w in res.warnings), \
        f"unlinked receipt must be surfaced, got: {res.warnings}"
    db.close()


def test_received_container_with_no_receipt_is_flagged_as_unbillable():
    db, cust = _fresh_customer()
    netsuite.ingest(db, cust, "item_receipts", [
        _receipt("IR1", "IR000001", "2026-07-20", "INBSHIP91")])
    netsuite.ingest(db, cust, "inbound_shipments", [
        _shipment("91", "INBSHIP91", "2026-07-30"),
        _shipment("92", "INBSHIP92", "2026-07-30"),          # landed, but nothing receipts it
        _shipment("100", "INBSHIP100", None, status="in transit"),   # not yet landed: no warning
    ])
    res = compute_billing(db, cust, *WEEK_A)
    warn = next((w for w in res.warnings if "no item receipt" in w), None)
    assert warn and "INBSHIP92" in warn, f"stranded container must be surfaced, got: {res.warnings}"
    assert "INBSHIP100" not in warn, "in-transit containers are not yet billable and not a fault"
    assert "INBSHIP91" not in warn, "a receipted container is fine"
    db.close()


def test_storage_averages_daily_pallet_totals_and_scales_by_weeks():
    db, cust = _fresh_customer()
    # Two SKUs, two snapshot days. units_per_pallet=12, so ceil() applies per SKU per day.
    #   20 Jul: ceil(715/12)=60 + ceil(700/12)=59  = 119 pallets
    #   21 Jul: ceil(715/12)=60 + ceil(400/12)=34  =  94 pallets
    # avg = 106.5 pallets/day; a Mon-Sun period is exactly 1.0 weeks -> 106.5 pallet-weeks.
    _snapshot(db, cust, date(2026, 7, 20), {"50001": (715, 12), "50002": (700, 12)})
    _snapshot(db, cust, date(2026, 7, 21), {"50001": (715, 12), "50002": (400, 12)})
    res = compute_billing(db, cust, *WEEK_A)
    storage = _line(res, "storage")
    assert storage.qty == 106.5, f"avg of daily totals (119, 94), not their sum: got {storage.qty}"
    assert storage.amount == round(106.5 * 4.50, 2) == 479.25
    assert not any("stock-on-hand snapshot" in w for w in res.warnings)
    db.close()


def test_storage_with_one_snapshot_holds_for_the_whole_period():
    """Documented degradation: a single reading is treated as having held all period."""
    db, cust = _fresh_customer()
    _snapshot(db, cust, date(2026, 7, 22), {"50001": (6369, 12)})
    res = compute_billing(db, cust, *WEEK_A)
    assert _line(res, "storage").qty == 531.0, "ceil(6369/12)=531 pallets x 1.0 week"
    db.close()


def test_storage_warns_when_no_snapshot_covers_the_period():
    """The 20-26 Jul situation: sync started 27 Jul, so that week has no snapshot at all."""
    db, cust = _fresh_customer()
    _snapshot(db, cust, date(2026, 7, 27), {"50001": (6369, 12)})
    res = compute_billing(db, cust, *WEEK_A)
    assert _line(res, "storage") is None, "no snapshot in period -> no storage line"
    assert any("stock-on-hand snapshot" in w for w in res.warnings), \
        f"a silently missing storage charge must be surfaced, got: {res.warnings}"
    db.close()


def test_storage_ignores_items_with_no_units_per_pallet():
    """A SKU with custitem_pallet_quantity NULL contributes 0 pallets — and must be obvious."""
    db, cust = _fresh_customer()
    # 52856 mirrors a real production SKU whose custitem_pallet_quantity is NULL: pallets is
    # left NULL, billing.py's fallback finds no units_per_pallet either, and it bills 0.
    _snapshot(db, cust, date(2026, 7, 22), {"50001": (1200, 12), "52856": (3000, None)})
    res = compute_billing(db, cust, *WEEK_A)
    assert _line(res, "storage").qty == 100.0, "only the SKU with a pallet quantity is billed"
    db.close()


def test_storage_breakdown_explains_the_billed_average():
    """The day-by-day working shown under the storage line on the run and the invoice.

    Storage is the only charge whose quantity is an average rather than a count of documents,
    so "1,201.71 pallet-weeks" cannot be traced back from the invoice on its own. The
    breakdown must reconcile EXACTLY to the billed quantity — a second implementation that
    drifted from billing.py would be worse than showing nothing.
    """
    db, cust = _fresh_customer()
    per_day = {20: 1103, 21: 1259, 22: 1305, 23: 1205, 24: 1180, 25: 1180, 26: 1180}
    for d, pallets in per_day.items():
        _snapshot(db, cust, date(2026, 7, d), {"50001": (pallets * 12, 12)})

    bd = service.storage_breakdown(db, cust.id, *WEEK_A)
    assert [x["text"] for x in bd["days"]] == [
        "M=1103", "T=1259", "W=1305", "Th=1205", "F=1180", "Sa=1180", "Su=1180"], bd["days"]
    assert bd["days_present"] == bd["days_expected"] == 7
    assert bd["weeks"] == 1.0
    assert bd["pallet_weeks"] == _line(compute_billing(db, cust, *WEEK_A), "storage").qty,         "the breakdown must reconcile to the quantity actually billed"
    db.close()


def test_source_refs_record_the_per_day_pallet_counts():
    """The frozen audit trail on the billing line, not just which dates contributed.

    source_refs used to hold a bare list of ISO dates, which said a snapshot existed on each
    day and nothing about what it read — useless for answering "how did you get 106.5?".
    """
    db, cust = _fresh_customer()
    _snapshot(db, cust, date(2026, 7, 20), {"50001": (715, 12), "50002": (700, 12)})
    _snapshot(db, cust, date(2026, 7, 21), {"50001": (715, 12), "50002": (400, 12)})
    refs = _line(compute_billing(db, cust, *WEEK_A), "storage").source_refs
    assert refs[1:] == ["2026-07-20 M=119", "2026-07-21 T=94"], refs
    assert "avg 106.5 pallets/day over 2 snapshot day(s)" in refs[0]
    db.close()


def test_storage_breakdown_flags_a_week_the_sync_only_partly_covered():
    """A sync outage drops a day entirely, and the average is then over the days that exist.

    That is the right arithmetic, but from the figures alone it is indistinguishable from a
    full week — so the counts are carried out for the page to say so.
    """
    db, cust = _fresh_customer()
    for d in (20, 21, 22, 23, 24):                      # Fri-Sun never synced
        _snapshot(db, cust, date(2026, 7, d), {"50001": (1200, 12)})
    bd = service.storage_breakdown(db, cust.id, *WEEK_A)
    assert (bd["days_present"], bd["days_expected"]) == (5, 7)
    assert bd["avg"] == 100.0, "averaged over the 5 days that exist, not diluted by 7"
    assert bd["pallet_weeks"] == _line(compute_billing(db, cust, *WEEK_A), "storage").qty
    db.close()


def test_storage_breakdown_is_none_when_the_period_has_no_snapshot():
    """Same case billing already warns about — the page must show nothing, not a zero week."""
    db, cust = _fresh_customer()
    _snapshot(db, cust, date(2026, 7, 27), {"50001": (6369, 12)})
    assert service.storage_breakdown(db, cust.id, *WEEK_A) is None
    db.close()


def test_warn_false_skips_the_checks_but_not_the_charges():
    """The overview chart calls compute_billing 5x and reads only totals — the warning queries
    are pure cost there. Amounts must be identical either way."""
    db, cust = _fresh_customer()
    netsuite.ingest(db, cust, "item_receipts", [
        _receipt("IR1", "IR000001", "2026-07-20", "INBSHIP91"),
        _receipt("IR2", "IR000002", "2026-07-21", None)])       # would warn
    netsuite.ingest(db, cust, "inbound_shipments", [
        _shipment("92", "INBSHIP92", "2026-07-30")])            # would warn

    loud = compute_billing(db, cust, *WEEK_A)
    quiet = compute_billing(db, cust, *WEEK_A, warn=False)
    assert loud.warnings and not quiet.warnings
    assert quiet.total == loud.total, "suppressing warnings must not change what is billed"
    assert ([(l.charge_type, l.qty, l.amount) for l in quiet.lines]
            == [(l.charge_type, l.qty, l.amount) for l in loud.lines])
    db.close()


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    for t in TESTS:
        t()
        print(f"  ok  {t.__name__}")
    print(f"PASS - {len(TESTS)} billing charge tests.")
