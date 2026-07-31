"""Billing period discipline — Mon-Sun weeks, the close-period lock, and auto-generate.

Background (why this test exists)
---------------------------------
The billing view used to take two free `<input type="date">` fields, so any range could be
billed. That is wrong in two ways: storage is priced in pallet-weeks and gets pro-rated by
`((end-start).days+1)/7`, and the re-billing guard keys on the exact (period_start, period_end)
pair — so 20-26 Jul and 24-26 Jul could both exist as runs, each charging the same receipts.
Periods are now always a whole Monday-Sunday week.

Separately, a `draft` run used to be silently wiped and recomputed on every re-save, so what a
human reviewed was not necessarily what got pushed. `locked_at` closes a period: the lines
freeze, but the run can still be queued and pushed.

Tests the endpoint helpers directly rather than over HTTP — httpx/TestClient is not in
requirements.txt and this app's tests don't take that dependency.

Runnable two ways:
    python tests/test_billing_period.py        # prints PASS / exits non-zero on failure
    pytest tests/test_billing_period.py        # if pytest is ever added
"""
import asyncio
import os
import sys
from datetime import date, datetime, timedelta

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SYNC_TOKEN"] = "test-token"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import main, netsuite, service  # noqa: E402
from app.billing import compute_billing  # noqa: E402
from app.db import Base, SessionLocal, engine, ensure_columns  # noqa: E402
from app.models import BillingRun, Customer, RateCard, RateCardLine  # noqa: E402
from app.seed import MOVA_RATES  # noqa: E402

MON, SUN = date(2026, 7, 20), date(2026, 7, 26)
DEFAULT = (date(2026, 7, 27), date(2026, 8, 2))


def _fresh_customer():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    ensure_columns()
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


class _FakeRequest:
    """The two things admin_billing_generate touches: a token header and a JSON body."""

    def __init__(self, token="test-token", body=None):
        self.headers = {"X-Sync-Token": token} if token else {}
        self._body = body if body is not None else {}

    async def json(self):
        return self._body


def _generate(db, token="test-token", body=None):
    res = asyncio.run(main.admin_billing_generate(_FakeRequest(token, body), db))
    import json as _json
    return _json.loads(bytes(res.body).decode())


# --- period resolution -------------------------------------------------------
def test_week_param_snaps_any_date_to_its_monday_sunday_bounds():
    for probe in ("2026-07-20", "2026-07-23", "2026-07-26"):
        ps, pe, err = main._billing_period({"week": probe}, DEFAULT)
        assert (ps, pe) == (MON, SUN), f"{probe} should resolve to {MON}..{SUN}, got {ps}..{pe}"
        assert err is None


def test_aligned_from_to_is_accepted_unchanged():
    ps, pe, err = main._billing_period({"from": "2026-07-20", "to": "2026-07-26"}, DEFAULT)
    assert (ps, pe) == (MON, SUN) and err is None


def test_misaligned_from_to_is_rejected_not_silently_billed():
    """A stale bookmark must not quietly bill a period other than the one it names."""
    ps, pe, err = main._billing_period({"from": "2026-07-24", "to": "2026-07-26"}, DEFAULT)
    assert (ps, pe) == (MON, SUN), "falls back to the whole week containing the start"
    assert err and "not a whole Monday–Sunday week" in err, f"must say why, got: {err}"


def test_non_monday_start_is_rejected():
    _, _, err = main._billing_period({"from": "2026-07-21", "to": "2026-07-27"}, DEFAULT)
    assert err, "a 7-day range that doesn't start on Monday is still not a billing week"


def test_no_params_uses_the_default_week():
    assert main._billing_period({}, DEFAULT) == (*DEFAULT, None)


def test_unreadable_dates_fall_back_with_a_message():
    for params in ({"week": "not-a-date"}, {"from": "2026-13-99", "to": "2026-07-26"}):
        ps, pe, err = main._billing_period(params, DEFAULT)
        assert (ps, pe) == DEFAULT and err, f"{params} should fall back with a message"


# --- the close-period lock ---------------------------------------------------
def test_recompute_blocked_covers_both_guards():
    assert main._recompute_blocked(None) is None, "no run yet — free to compute"
    assert main._recompute_blocked(BillingRun(status="draft")) is None
    for status in ("ready_to_push", "pushed", "invoiced"):
        assert main._recompute_blocked(BillingRun(status=status)) == "already-invoiced"
    locked = BillingRun(status="draft")
    locked.locked_at = datetime(2026, 8, 1, 9, 0)
    assert main._recompute_blocked(locked) == "already-locked", \
        "a closed draft must not be recomputable"


def test_locked_draft_survives_a_resave_attempt():
    db, cust = _fresh_customer()
    netsuite.ingest(db, cust, "item_receipts", [{
        "ns_receipt_id": "IR1", "tranid": "IR000001", "trandate": "2026-07-20",
        "ns_inbound_shipment": "INBSHIP91", "po_tranid": None,
        "lines": [{"ns_item_id": "50001", "qty": 715}]}])
    run = main._persist_billing_run(db, cust, MON, SUN, compute_billing(db, cust, MON, SUN))
    db.commit()
    original = {l.charge_type: float(l.amount) for l in run.lines}
    assert original["container_unload"] == 1500.0

    # Close the period, then let more activity land in the same week.
    run.locked_at, run.locked_by = datetime(2026, 8, 1, 9, 0), "ops@macgeargroup.com"
    db.commit()
    netsuite.ingest(db, cust, "item_receipts", [{
        "ns_receipt_id": "IR2", "tranid": "IR000002", "trandate": "2026-07-21",
        "ns_inbound_shipment": "INBSHIP92", "po_tranid": None,
        "lines": [{"ns_item_id": "50001", "qty": 715}]}])

    assert main._recompute_blocked(main._existing_run(db, cust.id, MON, SUN)) == "already-locked"
    frozen = {l.charge_type: float(l.amount) for l in
              main._existing_run(db, cust.id, MON, SUN).lines}
    assert frozen == original, "the closed run's lines must not have moved"
    db.close()


# --- auto-generate -----------------------------------------------------------
def _seed_last_week(db, cust):
    """Receipts in the week the generate endpoint targets (today - 7d)."""
    ps, _ = service.week_bounds(date.today() - timedelta(days=7))
    netsuite.ingest(db, cust, "item_receipts", [{
        "ns_receipt_id": "IR1", "tranid": "IR000001", "trandate": ps.isoformat(),
        "ns_inbound_shipment": "INBSHIP91", "po_tranid": None,
        "lines": [{"ns_item_id": "50001", "qty": 715}]}])
    return service.week_bounds(date.today() - timedelta(days=7))


def test_generate_requires_the_sync_token():
    db, _ = _fresh_customer()
    assert _generate(db, token=None).get("error") == "unauthorized"
    assert _generate(db, token="wrong").get("error") == "unauthorized"
    db.close()


def test_generate_creates_a_draft_for_the_completed_week():
    db, cust = _fresh_customer()
    ps, pe = _seed_last_week(db, cust)
    out = _generate(db)
    assert out["week"] == [ps.isoformat(), pe.isoformat()], "targets the previous whole week"
    row = out["customers"][0]
    assert row["generated"] is True and row["total"] == 1500.0 + 715.0
    run = main._existing_run(db, cust.id, ps, pe)
    assert run and run.status == "draft", "generated runs must land as draft, never queued"
    db.close()


def test_generate_is_idempotent():
    db, cust = _fresh_customer()
    _seed_last_week(db, cust)
    first = _generate(db)["customers"][0]
    second = _generate(db)["customers"][0]
    assert first["generated"] is True
    assert second["generated"] is False and second["skipped"] == "run-exists"
    assert db.query(BillingRun).count() == 1, "a second call must not create another run"
    db.close()


def test_generate_skips_a_closed_period_and_says_so():
    db, cust = _fresh_customer()
    ps, pe = _seed_last_week(db, cust)
    _generate(db)
    run = main._existing_run(db, cust.id, ps, pe)
    run.locked_at = datetime(2026, 8, 1, 9, 0)
    db.commit()
    assert _generate(db)["customers"][0]["skipped"] == "already-locked"
    db.close()


def test_generate_plants_nothing_when_there_is_no_activity():
    db, cust = _fresh_customer()
    row = _generate(db)["customers"][0]
    assert row["generated"] is False and row["skipped"] == "no-billable-activity"
    assert db.query(BillingRun).count() == 0
    db.close()


def test_generate_backfills_a_named_week():
    db, cust = _fresh_customer()
    netsuite.ingest(db, cust, "item_receipts", [{
        "ns_receipt_id": "IR1", "tranid": "IR000001", "trandate": "2026-07-22",
        "ns_inbound_shipment": "INBSHIP91", "po_tranid": None,
        "lines": [{"ns_item_id": "50001", "qty": 715}]}])
    out = _generate(db, body={"week": "2026-07-24"})
    assert out["week"] == [MON.isoformat(), SUN.isoformat()], "any date in the week works"
    assert out["customers"][0]["generated"] is True
    db.close()


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    for t in TESTS:
        t()
        print(f"  ok  {t.__name__}")
    print(f"PASS - {len(TESTS)} billing period/lock/generate tests.")
