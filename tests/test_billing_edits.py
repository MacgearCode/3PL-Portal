"""Draft editing, the charge-item mapping, and invoice sync-back.

Background (why this test exists)
---------------------------------
Three changes land together here (2026-08-05), and each one has a way of going quietly wrong:

1. **Draft editing.** The earlier decision was "edit the invoice in NetSuite, never the
   portal", precisely so a billing run stayed a faithful record of what the rate card
   produced. Two weeks of live 3PL work made portal-side editing necessary anyway. The
   property that decision protected is now enforced structurally instead: an edit never
   overwrites `computed_qty` / `computed_rate` / `computed_amount`, and a *removed* computed
   line is kept at amount 0 rather than deleted. A charge that silently disappears is the
   exact fault that put 9 containers ($13,500) off an invoice for a fortnight.

2. **The charge-item catalogue.** `charge_type -> NetSuite item id` used to be a constant in
   the n8n Code node, holding sandbox ids that do not exist in production. It now lives in
   the app, is stamped onto each line at save time, and a run whose line has no item is
   refused *at queue time in the portal* rather than failing inside n8n overnight.

3. **Invoice sync-back.** Line edits made in NetSuite already reached the cache; nothing ever
   compared them to the run that raised them, and no run ever left `pushed`. The comparison
   is `run_variance()`, and `_advance_pushed_runs()` moves the status on. The prune that keeps
   the cache honest has to survive the account's nastiest failure mode: a missing permission
   returns an EMPTY RESULT SET from a successful query, so an empty pull must never be read
   as "everything was deleted".

Tests the endpoint helpers directly rather than over HTTP — httpx/TestClient is not in
requirements.txt and this app's tests don't take that dependency.

Runnable two ways:
    python tests/test_billing_edits.py        # prints PASS / exits non-zero on failure
    pytest tests/test_billing_edits.py
"""
import asyncio
import os
import sys
from datetime import date, datetime

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SYNC_TOKEN"] = "test-token"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import main, netsuite, service  # noqa: E402
from app.billing import compute_billing  # noqa: E402
from app.db import Base, SessionLocal, engine, ensure_columns  # noqa: E402
from app.models import (BillingLine, BillingRun, ChargeItem, Customer, Invoice,  # noqa: E402
                        InvoiceLine, RateCard, RateCardLine, User)
from app.seed import MOVA_RATES  # noqa: E402

MON, SUN = date(2026, 7, 20), date(2026, 7, 26)
RECEIPTS = [{"ns_receipt_id": f"IR{i}", "tranid": f"IR00000{i}", "trandate": "2026-07-20",
             "ns_inbound_shipment": f"INBSHIP9{i}", "po_tranid": None,
             "lines": [{"ns_item_id": "50001", "qty": 700}]} for i in (1, 2)]


def _fresh():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    ensure_columns()
    db = SessionLocal()
    service.bootstrap_charge_items(db)
    cust = Customer(slug="mova", name="Mova", ns_customer_id="11066",
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
    """What the edit routes touch: a form body, a session user, a token header."""

    def __init__(self, form=None, email="ops@macgeargroup.com", token="test-token"):
        self.headers = {"X-Sync-Token": token} if token else {}
        self.query_params = {}
        self._form = form or {}
        self._email = email

    async def form(self):
        return self._form


def _as_user(email="ops@macgeargroup.com"):
    """main.cur() reads the signed cookie; the edit routes only use it for role + email."""
    return User(email=email, role="internal", password_hash="x", active=True)


# The edit routes read the logged-in user through main.cur(), which wants a signed cookie.
# Swap it for a stub, and put it back afterwards — pytest runs every test module in ONE
# process against ONE engine (see the slug collision in test_shipment_link), so a patch left
# in place here would silently follow the next module in.
_REAL_CUR = main.cur


def _patch_cur(monkey_email="ops@macgeargroup.com"):
    main.cur = lambda request: _as_user(monkey_email)


def teardown_module(module=None):
    main.cur = _REAL_CUR


def _saved_run(db, cust):
    """A saved draft for MON..SUN with real computed lines."""
    netsuite.ingest(db, cust, "item_receipts", RECEIPTS)
    run = main._persist_billing_run(db, cust, MON, SUN, compute_billing(db, cust, MON, SUN))
    db.commit()
    return run


def _line(run, charge_type):
    return next(l for l in run.lines if l.charge_type == charge_type)


# --- the charge-item catalogue -----------------------------------------------
def test_catalogue_bootstraps_the_live_production_items():
    db, _ = _fresh()
    items = {ci.ns_item_id: ci for ci in service.charge_items(db)}
    assert len(items) == 10, f"all ten 3PL items should be present, got {len(items)}"
    # The five that back an automated charge — these are the production ids, replacing the
    # sandbox 55070-55074 that were hardcoded in the n8n node and never existed in prod.
    assert items["57082"].charge_type == "container_unload"
    assert items["23560"].charge_type == "putaway"
    assert items["23561"].charge_type == "storage"
    assert items["23563"].charge_type == "picking_so"
    assert items["23565"].charge_type == "shipping"
    # ...and the ad-hoc ones, available to a manual line but not derived from any charge.
    for ns_id in ("36281", "23562", "23564", "23566", "23567"):
        assert items[ns_id].charge_type is None, f"{ns_id} should be ad-hoc only"
    db.close()


def test_vrma_picking_resolves_through_the_alias_to_the_picking_item():
    """picking_so and picking_vrma bill at the same rate against one NetSuite item — the
    portal splits them because the split says which dispatch path was used, not because
    NetSuite needs two lines."""
    db, _ = _fresh()
    assert service.charge_item_for(db, "picking_vrma").ns_item_id == "23563"
    assert service.charge_item_for(db, "picking_so").ns_item_id == "23563"
    db.close()


def test_saved_lines_carry_their_netsuite_item():
    db, cust = _fresh()
    run = _saved_run(db, cust)
    assert _line(run, "putaway").ns_item_id == "23560"
    assert _line(run, "container_unload").ns_item_id == "57082"
    db.close()


def test_queue_is_refused_when_a_charge_has_no_item():
    """The failure has to land in front of the person who pressed the button. It used to be
    an undefined item id reaching NetSuite inside n8n, at whatever hour the lane ran."""
    db, cust = _fresh()
    run = _saved_run(db, cust)
    db.query(ChargeItem).filter(ChargeItem.charge_type == "putaway").delete()
    for l in run.lines:
        if l.charge_type == "putaway":
            l.ns_item_id = None
    db.commit()
    assert main._unmapped_lines(db, run) == ["putaway"]

    _patch_cur()
    resp = main.queue_billing_run("mova", run.id, _FakeRequest(), db)
    assert "msg=no-item" in resp.headers["location"], resp.headers["location"]
    assert run.status == "draft", "a run that cannot be invoiced must not leave the portal"
    db.close()


def test_unmapped_line_is_healed_from_the_catalogue_rather_than_re_billed():
    """A draft saved before the catalogue was filled in should become pushable by mapping
    the item — not by re-billing the week (which the close-period lock may well forbid)."""
    db, cust = _fresh()
    run = _saved_run(db, cust)
    for l in run.lines:
        l.ns_item_id = None
    db.commit()
    assert main._unmapped_lines(db, run) == [], "everything is mapped in the catalogue"
    assert _line(run, "putaway").ns_item_id == "23560", "resolved in place"
    db.close()


# --- draft editing ------------------------------------------------------------
def test_editing_a_line_keeps_the_rate_card_figures_alongside():
    db, cust = _fresh()
    run = _saved_run(db, cust)
    line = _line(run, "putaway")
    was_qty, was_amount = float(line.qty), float(line.amount)

    _patch_cur()
    form = {"line_id": str(line.id), "qty": "1000", "rate": "1.00",
            "description": "Putaway (agreed adjustment)"}
    asyncio.run(main.edit_billing_line("mova", run.id, _FakeRequest(form), db))

    assert float(line.qty) == 1000 and float(line.amount) == 1000.0
    assert line.origin == "edited"
    assert float(line.computed_qty) == was_qty, "the rate card's quantity must survive the edit"
    assert float(line.computed_amount) == was_amount, "...and its amount"
    assert line.variance == round(1000.0 - was_amount, 2)
    assert run.edited_at and run.edited_by == "ops@macgeargroup.com"
    db.close()


def test_removing_a_computed_line_leaves_it_on_the_record_at_zero():
    """Deleting it outright would make an unbilled charge invisible, which is the whole
    failure this module is built around."""
    db, cust = _fresh()
    run = _saved_run(db, cust)
    line = _line(run, "container_unload")
    computed = float(line.computed_amount)

    _patch_cur()
    asyncio.run(main.remove_billing_line("mova", run.id, _FakeRequest({"line_id": str(line.id)}), db))

    assert line.origin == "removed" and float(line.amount) == 0
    assert line.billable is False
    assert float(line.computed_amount) == computed, "what it would have billed stays visible"
    assert line in run.lines, "the line is not deleted"
    db.close()


def test_a_removed_line_can_be_restored_to_its_rate_card_figures():
    db, cust = _fresh()
    run = _saved_run(db, cust)
    line = _line(run, "container_unload")
    qty, rate, amount = float(line.qty), float(line.rate), float(line.amount)

    _patch_cur()
    asyncio.run(main.remove_billing_line("mova", run.id, _FakeRequest({"line_id": str(line.id)}), db))
    asyncio.run(main.restore_billing_line("mova", run.id, _FakeRequest({"line_id": str(line.id)}), db))

    assert line.origin == "computed"
    assert (float(line.qty), float(line.rate), float(line.amount)) == (qty, rate, amount)
    db.close()


def test_manual_line_is_added_against_a_chosen_item_and_deleted_outright():
    """Ad-hoc charges (labour, kitting, freight...) are entered by a human — nothing in the
    cache derives them, so there is nothing to compute or to vary from."""
    db, cust = _fresh()
    run = _saved_run(db, cust)
    _patch_cur()
    form = {"ns_item_id": "23567", "description": "Additional labour — reslip 2 pallets",
            "qty": "3", "rate": "65.00"}
    asyncio.run(main.add_billing_line("mova", run.id, _FakeRequest(form), db))

    added = next(l for l in run.lines if l.origin == "manual")
    assert added.ns_item_id == "23567" and float(added.amount) == 195.0
    assert added.charge_type == "adhoc:23567", "never NULL — charge_type is the line's identity"
    assert added.computed_amount is None, "a manual line has no rate-card figure to differ from"

    asyncio.run(main.remove_billing_line("mova", run.id, _FakeRequest({"line_id": str(added.id)}), db))
    assert added not in run.lines, "a manual line IS deleted — nothing computed is being hidden"
    db.close()


def test_a_bad_item_or_quantity_is_refused():
    db, cust = _fresh()
    run = _saved_run(db, cust)
    _patch_cur()
    before = len(run.lines)
    for form in ({"ns_item_id": "99999", "qty": "1", "rate": "5"},      # unknown item
                 {"ns_item_id": "23567", "qty": "0", "rate": "5"},      # zero qty
                 {"ns_item_id": "23567", "qty": "x", "rate": "5"}):     # not a number
        resp = asyncio.run(main.add_billing_line("mova", run.id, _FakeRequest(form), db))
        assert "msg=bad-line" in resp.headers["location"]
    assert len(run.lines) == before
    db.close()


def test_editing_is_refused_on_a_closed_or_pushed_run():
    db, cust = _fresh()
    run = _saved_run(db, cust)
    line = _line(run, "putaway")
    _patch_cur()
    for freeze in ("locked", "pushed"):
        if freeze == "locked":
            run.locked_at = datetime(2026, 7, 27, 9, 0)
        else:
            run.locked_at, run.status = None, "pushed"
        db.commit()
        form = {"line_id": str(line.id), "qty": "1", "rate": "1"}
        resp = asyncio.run(main.edit_billing_line("mova", run.id, _FakeRequest(form), db))
        assert "msg=not-editable" in resp.headers["location"], \
            f"a {freeze} run must not be editable — what was reviewed is what gets pushed"
    db.close()


def test_recompute_is_blocked_on_an_edited_draft_unless_the_discard_is_explicit():
    db, cust = _fresh()
    run = _saved_run(db, cust)
    _patch_cur()
    asyncio.run(main.add_billing_line("mova", run.id, _FakeRequest(
        {"ns_item_id": "23562", "qty": "1", "rate": "250"}), db))

    assert main._recompute_blocked(run) == "has-edits"
    assert main._recompute_blocked(run, allow_discard=True) is None
    # ...but a discard never unlocks a period that is genuinely frozen.
    run.locked_at = datetime(2026, 7, 27, 9, 0)
    assert main._recompute_blocked(run, allow_discard=True) == "already-locked"
    db.close()


def test_recompute_with_discard_clears_the_edit_flag_and_the_manual_lines():
    db, cust = _fresh()
    run = _saved_run(db, cust)
    _patch_cur()
    asyncio.run(main.add_billing_line("mova", run.id, _FakeRequest(
        {"ns_item_id": "23562", "qty": "1", "rate": "250"}), db))
    assert run.edited_at is not None

    run = main._persist_billing_run(db, cust, MON, SUN, compute_billing(db, cust, MON, SUN))
    db.commit()
    assert run.edited_at is None and run.edited_by is None
    assert not [l for l in run.lines if l.origin != "computed"]
    db.close()


def test_pending_payload_carries_item_ids_and_omits_removed_lines():
    import json
    db, cust = _fresh()
    run = _saved_run(db, cust)
    _patch_cur()
    asyncio.run(main.remove_billing_line("mova", run.id, _FakeRequest(
        {"line_id": str(_line(run, "container_unload").id)}), db))
    run.status = "ready_to_push"
    db.commit()

    payload = json.loads(bytes(main.admin_billing_pending(_FakeRequest(), db).body).decode())
    lines = payload["pending"][0]["lines"]
    assert all(l["ns_item_id"] for l in lines), "n8n no longer maps charge_type -> item itself"
    assert not [l for l in lines if l["charge_type"] == "container_unload"], \
        "a removed line must not reach NetSuite"
    assert payload["pending"][0]["total"] == round(sum(l["amount"] for l in lines), 2)
    db.close()


# --- invoice sync-back --------------------------------------------------------
def _push(db, run, ns_invoice_id="90001"):
    run.status, run.ns_invoice_id = "pushed", ns_invoice_id
    db.commit()


def _invoice_rows(total, lines, status="Open", ns_id="90001"):
    return [{"ns_invoice_id": ns_id, "tranid": "INV1234", "trandate": "2026-07-27",
             "status": status, "total": total, "currency": "AUD",
             "amount_remaining": total, "due_date": "2026-08-26",
             "ns_lastmodified": "2026-07-27 09:15:00", "lines": lines}]


def test_invoice_lines_are_tagged_with_a_charge_type_via_the_item():
    db, cust = _fresh()
    netsuite.ingest(db, cust, "invoices", _invoice_rows(1400.0, [
        {"ns_item_id": "23560", "description": "Putaway", "qty": 1400, "rate": 1, "amount": 1400}]))
    line = db.query(InvoiceLine).one()
    assert line.ns_item_id == "23560" and line.charge_type == "putaway", \
        "matched by item, not by description — descriptions get edited in NetSuite"
    inv = db.query(Invoice).one()
    assert inv.currency == "AUD" and float(inv.amount_remaining) == 1400.0
    assert inv.due_date == date(2026, 8, 26)
    assert inv.ns_lastmodified == datetime(2026, 7, 27, 9, 15)
    db.close()


def test_an_edit_made_in_netsuite_shows_as_a_variance_and_does_not_rewrite_the_run():
    db, cust = _fresh()
    run = _saved_run(db, cust)
    putaway = float(_line(run, "putaway").amount)
    containers = float(_line(run, "container_unload").amount)
    _push(db, run)

    # Someone edits the invoice in NetSuite: one container credited off, putaway untouched.
    netsuite.ingest(db, cust, "invoices", _invoice_rows(putaway + containers - 1500, [
        {"ns_item_id": "23560", "description": "Putaway", "qty": 1400, "rate": 1,
         "amount": putaway},
        {"ns_item_id": "57082", "description": "Container unload", "qty": 1, "rate": 1500,
         "amount": containers - 1500}]))

    v = service.run_variance(db, run)
    assert v["differs"] is True
    assert v["delta"] == -1500.0, f"the invoice is $1,500 lighter, got {v['delta']}"
    changed = next(r for r in v["rows"] if r["state"] == "changed")
    assert changed["delta"] == -1500.0
    assert float(_line(run, "container_unload").amount) == containers, \
        "the run stays the record of what the rate card produced"
    db.close()


def test_a_line_added_in_netsuite_appears_in_the_variance():
    db, cust = _fresh()
    run = _saved_run(db, cust)
    putaway = float(_line(run, "putaway").amount)
    containers = float(_line(run, "container_unload").amount)
    _push(db, run)
    netsuite.ingest(db, cust, "invoices", _invoice_rows(putaway + containers + 400, [
        {"ns_item_id": "23560", "description": "Putaway", "qty": 1400, "rate": 1, "amount": putaway},
        {"ns_item_id": "57082", "description": "Container unload", "qty": 2, "rate": 1500,
         "amount": containers},
        {"ns_item_id": "23564", "description": "Packaging", "qty": 8, "rate": 50, "amount": 400}]))
    v = service.run_variance(db, run)
    added = next(r for r in v["rows"] if r["state"] == "added")
    assert added["ns_amount"] == 400 and added["run_amount"] is None
    db.close()


def test_a_pushed_run_advances_to_invoiced_once_netsuite_accepts_it():
    """Nothing did this before — only seed.py ever wrote 'invoiced', so every real run sat
    at 'pushed' forever and the status stopped meaning anything after the push."""
    db, cust = _fresh()
    run = _saved_run(db, cust)
    _push(db, run)

    netsuite.ingest(db, cust, "invoices", _invoice_rows(100.0, [], status="Pending Approval"))
    assert run.status == "pushed", "still awaiting approval in NetSuite"

    netsuite.ingest(db, cust, "invoices", _invoice_rows(100.0, [], status="Open"))
    assert run.status == "invoiced"
    db.close()


def test_an_invoice_deleted_in_netsuite_flags_the_run_but_never_rolls_its_status_back():
    db, cust = _fresh()
    run = _saved_run(db, cust)
    _push(db, run)
    netsuite.ingest(db, cust, "invoices", _invoice_rows(100.0, []))
    assert run.status == "invoiced"

    # A different invoice comes back; ours is gone from NetSuite.
    netsuite.ingest(db, cust, "invoices", _invoice_rows(50.0, [], ns_id="90002"))
    assert db.query(Invoice).count() == 1, "the deleted invoice is pruned from the cache"
    assert run.sync_note and "no longer in NetSuite" in run.sync_note
    assert run.status == "invoiced", \
        "re-billing a week is a human decision, never a sync side effect"
    db.close()


def test_ledger_signed_invoice_lines_are_flipped_to_match_the_header():
    """transactionline stores a CustInvc's revenue lines as credits, so quantity and
    netamount arrive NEGATIVE under a POSITIVE foreigntotal. Unfixed, the portal showed every
    line negative beneath a positive total, and run_variance() called every line 'changed'."""
    db, cust = _fresh()
    netsuite.ingest(db, cust, "invoices", _invoice_rows(15624.0, [
        {"ns_item_id": "57082", "description": "Container unload", "qty": -9, "rate": 1500,
         "amount": -13500},
        {"ns_item_id": "23560", "description": "Putaway", "qty": -2124, "rate": 1,
         "amount": -2124}]))
    lines = {l.ns_item_id: l for l in db.query(InvoiceLine).all()}
    assert float(lines["57082"].qty) == 9 and float(lines["57082"].amount) == 13500
    assert float(lines["23560"].qty) == 2124 and float(lines["23560"].amount) == 2124
    db.close()


def test_correctly_signed_lines_are_left_alone():
    """Idempotence: once the RESTlet negates at source the app must not flip them back."""
    db, cust = _fresh()
    netsuite.ingest(db, cust, "invoices", _invoice_rows(13500.0, [
        {"ns_item_id": "57082", "description": "Container unload", "qty": 9, "rate": 1500,
         "amount": 13500}]))
    line = db.query(InvoiceLine).one()
    assert float(line.qty) == 9 and float(line.amount) == 13500
    db.close()


def test_a_credit_line_keeps_its_opposite_sign():
    """All-or-nothing on the SET, never per line. A discount is legitimately the opposite
    sign to its neighbours — normalising line by line would flip it into a charge."""
    db, cust = _fresh()
    netsuite.ingest(db, cust, "invoices", _invoice_rows(13000.0, [
        {"ns_item_id": "57082", "description": "Container unload", "qty": -9, "rate": 1500,
         "amount": -13500},
        {"ns_item_id": "23567", "description": "Goodwill credit", "qty": 1, "rate": 500,
         "amount": 500}]))
    lines = {l.ns_item_id: float(l.amount) for l in db.query(InvoiceLine).all()}
    assert lines["57082"] == 13500, "the charge is flipped positive"
    assert lines["23567"] == -500, "the credit stays a credit"
    assert round(sum(lines.values()), 2) == 13000.0, "and the set still reconciles to the header"
    db.close()


# --- which week does an invoice bill? ----------------------------------------
def test_invoice_period_comes_from_the_run_that_pushed_it():
    """trandate is when the invoice was RAISED, not the week it covers — INAU250127 is dated
    in August and bills 27 Jul-2 Aug."""
    db, cust = _fresh()
    run = _saved_run(db, cust)
    _push(db, run)
    netsuite.ingest(db, cust, "invoices", _invoice_rows(100.0, []))
    inv = db.query(Invoice).one()
    assert service.invoice_period(db, inv) == ((MON, SUN), "run", (MON, SUN))
    assert service.invoice_periods(db, cust.id)["90001"] == (MON, SUN)
    db.close()


def test_invoice_period_falls_back_to_the_memo_for_an_unlinked_invoice():
    """An invoice raised by hand in NetSuite has no billing_run pointing at it. The memo the
    portal stamps ('3PL charges <from>-<to>') is the only period source left."""
    db, cust = _fresh()
    rows = _invoice_rows(100.0, [])
    rows[0]["memo"] = "3PL charges 2026-07-27–2026-08-02"
    netsuite.ingest(db, cust, "invoices", rows)
    inv = db.query(Invoice).one()
    assert inv.memo == "3PL charges 2026-07-27–2026-08-02"
    assert service.invoice_period(db, inv) == ((date(2026, 7, 27), date(2026, 8, 2)),
                                               "memo", None)
    db.close()


def test_invoice_with_no_period_anywhere_reports_none_rather_than_guessing():
    """Never infer the period from trandate. INAU250127 was backdated to 31 Jul to fall inside
    payment terms while billing 27 Jul-2 Aug — a guess off trandate would file it in the
    wrong week and look authoritative doing it."""
    db, cust = _fresh()
    netsuite.ingest(db, cust, "invoices", _invoice_rows(100.0, []))
    inv = db.query(Invoice).one()
    assert service.invoice_period(db, inv) == (None, None, None)
    assert service.invoice_periods(db, cust.id) == {}
    db.close()


def test_a_period_can_be_assigned_by_hand_and_snaps_to_a_whole_week():
    """The case that matters: INAU249588 and INAU250127 were raised manually in NetSuite, so
    they have no run and no memo. Hand assignment is the only period they will ever have."""
    db, cust = _fresh()
    netsuite.ingest(db, cust, "invoices", _invoice_rows(100.0, []))
    inv = db.query(Invoice).one()
    _patch_cur()
    # A Thursday inside the 27 Jul - 2 Aug week; must snap to Mon-Sun.
    asyncio.run(main.set_invoice_period("mova", inv.id, _FakeRequest({"week": "2026-07-30"}), db))
    assert (inv.period_start, inv.period_end) == (date(2026, 7, 27), date(2026, 8, 2))
    period, source, _ = service.invoice_period(db, inv)
    assert period == (date(2026, 7, 27), date(2026, 8, 2)) and source == "manual"
    db.close()


def test_a_hand_assigned_period_survives_a_re_sync():
    """period_start/end are portal-owned. If the invoice ingest ever wrote them, the next
    sync would silently wipe every manual attribution."""
    db, cust = _fresh()
    netsuite.ingest(db, cust, "invoices", _invoice_rows(100.0, []))
    inv = db.query(Invoice).one()
    _patch_cur()
    asyncio.run(main.set_invoice_period("mova", inv.id, _FakeRequest({"week": "2026-07-20"}), db))

    netsuite.ingest(db, cust, "invoices", _invoice_rows(250.0, [
        {"ns_item_id": "23560", "description": "Putaway", "qty": 250, "rate": 1, "amount": 250}]))
    db.refresh(inv)
    assert float(inv.total) == 250.0, "the sync did update what it owns"
    assert (inv.period_start, inv.period_end) == (MON, SUN), "...and left what it doesn't alone"
    db.close()


def test_a_hand_assigned_period_overrides_the_run_and_the_disagreement_is_visible():
    db, cust = _fresh()
    run = _saved_run(db, cust)          # period MON..SUN (20-26 Jul)
    _push(db, run)
    netsuite.ingest(db, cust, "invoices", _invoice_rows(100.0, []))
    inv = db.query(Invoice).one()
    assert service.invoice_period(db, inv)[1] == "run"

    _patch_cur()
    asyncio.run(main.set_invoice_period("mova", inv.id, _FakeRequest({"week": "2026-07-29"}), db))
    period, source, run_period = service.invoice_period(db, inv)
    assert source == "manual" and period == (date(2026, 7, 27), date(2026, 8, 2))
    assert run_period == (MON, SUN), \
        "the run's own period is returned too, so the page can flag the disagreement"
    db.close()


def test_clearing_a_hand_assigned_period_falls_back_to_the_memo():
    db, cust = _fresh()
    rows = _invoice_rows(100.0, [])
    rows[0]["memo"] = "3PL charges 2026-07-20–2026-07-26"
    netsuite.ingest(db, cust, "invoices", rows)
    inv = db.query(Invoice).one()
    _patch_cur()
    asyncio.run(main.set_invoice_period("mova", inv.id, _FakeRequest({"week": "2026-08-05"}), db))
    assert service.invoice_period(db, inv)[1] == "manual"

    asyncio.run(main.set_invoice_period("mova", inv.id, _FakeRequest({"clear": "1"}), db))
    period, source, _ = service.invoice_period(db, inv)
    assert source == "memo" and period == (MON, SUN)
    assert inv.period_start is None and inv.period_end is None
    db.close()


def test_an_unreadable_week_is_refused_rather_than_stored():
    db, cust = _fresh()
    netsuite.ingest(db, cust, "invoices", _invoice_rows(100.0, []))
    inv = db.query(Invoice).one()
    _patch_cur()
    for form in ({"week": "not-a-date"}, {"week": ""}, {}):
        resp = asyncio.run(main.set_invoice_period("mova", inv.id, _FakeRequest(form), db))
        assert "msg=bad-week" in resp.headers["location"]
    assert inv.period_start is None
    db.close()


# --- deleting a run ----------------------------------------------------------
def test_admin_can_delete_a_run_and_the_period_becomes_billable_again():
    db, cust = _fresh()
    run = _saved_run(db, cust)
    run_id = run.id
    _patch_cur()
    main.cur = lambda request: User(email="admin@macgeargroup.com", role="admin",
                                    password_hash="x", active=True)
    resp = main.delete_billing_run("mova", run_id, _FakeRequest(), db)
    assert "msg=run-deleted" in resp.headers["location"]
    assert db.get(BillingRun, run_id) is None
    assert not db.query(BillingLine).filter(BillingLine.billing_run_id == run_id).count(), \
        "lines go with the run"
    assert main._existing_run(db, cust.id, MON, SUN) is None
    db.close()


def test_deleting_a_pushed_run_warns_that_the_week_can_be_billed_twice():
    db, cust = _fresh()
    run = _saved_run(db, cust)
    _push(db, run)
    main.cur = lambda request: User(email="admin@macgeargroup.com", role="admin",
                                    password_hash="x", active=True)
    resp = main.delete_billing_run("mova", run.id, _FakeRequest(), db)
    assert "msg=run-deleted-pushed" in resp.headers["location"], \
        "the flash must say the NetSuite invoice survives and the period is re-billable"
    db.close()


def test_a_non_admin_cannot_delete_a_run():
    """Recomputing or closing a period is reversible; this is not."""
    db, cust = _fresh()
    run = _saved_run(db, cust)
    _patch_cur()                       # role 'internal'
    resp = main.delete_billing_run("mova", run.id, _FakeRequest(), db)
    assert "msg=not-allowed" in resp.headers["location"]
    assert db.get(BillingRun, run.id) is not None
    db.close()


def test_an_empty_invoice_pull_deletes_nothing():
    """THE guard. A missing transaction permission does not error in NetSuite — it
    row-filters and returns an empty result set from a successful query. Without this, one
    permission blip wipes every invoice and detaches every run from the invoice it created,
    which is one re-push away from billing a customer twice."""
    db, cust = _fresh()
    run = _saved_run(db, cust)
    _push(db, run)
    netsuite.ingest(db, cust, "invoices", _invoice_rows(100.0, []))

    netsuite.ingest(db, cust, "invoices", [])
    assert db.query(Invoice).count() == 1, "an empty pull is 'no news', not 'all deleted'"
    assert not run.sync_note, "and it must not raise a false alarm on the run"
    db.close()


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL  {name}: {e}")
    teardown_module()
    print(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'}")
    sys.exit(1 if failures else 0)
