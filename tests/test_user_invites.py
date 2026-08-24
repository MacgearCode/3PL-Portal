"""User invites, password-reset links, and the admin guards around them.

Background (why this test exists)
---------------------------------
Onboarding a portal user used to mean "create the account, copy a link out of the admin
console, paste it into Teams". Making that an emailed invite changes four things that each
have a way of going quietly wrong:

1. **Creating a user must not email anybody.** It used to auto-mint a set-password link the
   moment the account was saved. Harmless while nothing was ever sent; the day mail works
   that becomes "the customer is emailed before the admin has checked role, customer or
   permissions, and before a typo in the address has been noticed". The redirect to
   `?msg=created` with no token minted is the headline behaviour of this change.

2. **An invite and a reset are different lifetimes.** One 45-minute TTL served both, which is
   unusable for a link emailed to someone who reads it after lunch. `reset_purpose` picks the
   TTL at mint time and the wording afterwards — and it must NEVER affect validity, which is
   `reset_expires_at` alone. That is what makes NULL (a token minted before the column
   existed, always a 45-minute reset) safe to read as 'reset'.

3. **Delivery is best-effort, and the copyable link is the fallback.** So a webhook failure
   has to leave the token persisted and return the link anyway, and must never raise — a 500
   from `/forgot` would reveal whether an address exists, which is the one thing that endpoint
   is designed not to do. The `/forgot` throttle has the same constraint: it must not change
   the response, or it becomes the enumeration oracle itself.

4. **The last active admin must survive.** Unlike Vendor Credit Claims there is no
   shared-password fallback here ("auth is always on now"), so demoting, disabling or
   deleting the final admin can only be undone with psql on the droplet.

Tests the endpoint helpers directly rather than over HTTP — httpx/TestClient is not in
requirements.txt and this app's tests don't take that dependency. The interesting assertions
all live in helpers that return data (`_issue_reset_link`, `_user_status`, `notify`), which is
why those helpers exist in that shape.

Runnable two ways:
    python tests/test_user_invites.py         # prints PASS / exits non-zero on failure
    pytest tests/test_user_invites.py
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SYNC_TOKEN"] = "test-token"
# Deterministic links, so request.base_url is never consulted.
os.environ["PUBLIC_BASE_URL"] = "https://portal.test"
# Set so the SENDING path is exercised rather than the log-only branch.
os.environ["N8N_RESET_WEBHOOK_URL"] = "https://n8n.test/webhook/x"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import main, notify  # noqa: E402
# Both modules read those two env vars at IMPORT time, and under `pytest tests` another test
# module has already imported `app.main` before this file runs — so the env sets above arrive
# too late and the link comes out built from request.base_url. Pin the values on the modules
# as well; the env sets stay for the `python tests/test_user_invites.py` path.
main.PUBLIC_BASE_URL = "https://portal.test"
notify.WEBHOOK_URL = "https://n8n.test/webhook/x"
from app.db import Base, SessionLocal, engine, ensure_columns  # noqa: E402
from app.models import Customer, User  # noqa: E402
from app.security import hash_password, hash_token  # noqa: E402

# Fixture credential, never a real one — 12 chars so it clears the 10-char minimum.
_FIXTURE_PW = "z" * 12
ADMIN = "admin@macgeargroup.com"


def _fresh():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    ensure_columns()
    db = SessionLocal()
    db.add(Customer(slug="mova", name="Mova", ns_customer_id="11066",
                    ns_supplier_id="10872", ns_location_id="49", ns_class_id="237"))
    db.add(User(email=ADMIN, role="admin",
                password_hash=hash_password(_FIXTURE_PW), active=True))
    db.commit()
    return db


class _FakeForm(dict):
    """Starlette hands routes a multidict; admin_user_save reads the `views` checkboxes with
    .getlist(), so a plain dict isn't enough."""

    def getlist(self, key):
        v = self.get(key, [])
        return v if isinstance(v, list) else [v]


class _FakeRequest:
    def __init__(self, form=None, query=None):
        self.headers = {}
        self.query_params = query or {}
        self._form = _FakeForm(form or {})
        self.base_url = "https://ignored.test/"
        self.client = None

    async def form(self):
        return self._form


def _admin_row(db):
    return db.scalar(main.select(User).where(User.email == ADMIN))


_REAL_CUR = main.cur
_REAL_POST = notify._post
_REAL_URL = notify.WEBHOOK_URL


def _patch_cur(email=ADMIN):
    main.cur = lambda request: User(email=email, role="admin",
                                    password_hash="x", active=True, id=1)


def teardown_module(module=None):
    main.cur = _REAL_CUR
    notify._post = _REAL_POST
    notify.WEBHOOK_URL = _REAL_URL


class _Sent:
    """Captures what would have gone to n8n, without opening a socket."""

    def __init__(self, fail=False):
        self.calls = []
        self._fail = fail

    def __call__(self, url, body, headers):
        self.calls.append({"url": url, "headers": headers,
                           "payload": json.loads(body.decode())})
        if self._fail:
            raise OSError("connection refused")
        return 200

    @property
    def last(self):
        return self.calls[-1]["payload"]


def _capture(fail=False):
    sent = _Sent(fail)
    notify._post = sent
    notify.WEBHOOK_URL = "https://n8n.test/webhook/x"
    return sent


def _new_user(db, email="sam@mova.com", pw_hash=""):
    u = User(email=email, password_hash=pw_hash, role="customer", active=True)
    db.add(u)
    db.commit()
    return u


# --- creating a user emails nothing ------------------------------------------
def test_creating_a_user_does_not_email_or_mint_a_token():
    """The headline behaviour change. If only one test in this file survives, it's this one."""
    db = _fresh()
    _patch_cur()
    sent = _capture()
    req = _FakeRequest(form={"email": "sam@mova.com", "role": "customer", "active": "on"})
    resp = asyncio.run(main.admin_user_save(req, None, db))

    u = db.scalar(main.select(User).where(User.email == "sam@mova.com"))
    assert u is not None, "the account should still be created"
    assert u.password_hash == "", "a new user has no credential until they set one"
    assert u.reset_token_hash is None, "creating a user must not mint a link"
    assert not sent.calls, "creating a user must not email anybody"
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/admin/users/{u.id}?msg=created", \
        "should land on the account so the admin can review it and press Send invite"


# --- purpose and TTL ---------------------------------------------------------
def test_an_invite_gets_the_seven_day_ttl_and_stamps_its_purpose():
    db = _fresh()
    _patch_cur()
    _capture()
    u = _new_user(db)
    link, delivered = main._issue_reset_link(db, u, _FakeRequest(), purpose=main.INVITE)

    assert delivered is True
    assert link.startswith("https://portal.test/reset?token="), link
    assert u.reset_purpose == "invite"
    expected = datetime.utcnow() + timedelta(minutes=10080)
    assert abs((u.reset_expires_at - expected).total_seconds()) < 60, \
        f"invite should last 7 days, got {u.reset_expires_at}"


def test_a_reset_gets_forty_five_minutes():
    db = _fresh()
    _patch_cur()
    _capture()
    u = _new_user(db, pw_hash=hash_password(_FIXTURE_PW))
    main._issue_reset_link(db, u, _FakeRequest(), purpose=main.RESET)

    assert u.reset_purpose == "reset"
    expected = datetime.utcnow() + timedelta(minutes=45)
    assert abs((u.reset_expires_at - expected).total_seconds()) < 60, \
        f"reset should last 45 minutes, got {u.reset_expires_at}"


def test_the_purpose_comes_from_account_state_not_the_form():
    """A user who already has a credential gets a 45-minute reset even if the request asks for
    an invite — the purpose is never client-controlled."""
    db = _fresh()
    _patch_cur()
    sent = _capture()
    u = _new_user(db, pw_hash=hash_password(_FIXTURE_PW))
    main._send_link_response(_FakeRequest(form={"note": "", "purpose": "invite"}), db, u, "")

    assert u.reset_purpose == "reset"
    assert sent.last["purpose"] == "reset"
    assert sent.last["expires_in"] == "45 minutes"


def test_a_null_purpose_is_read_as_a_reset():
    """Guards the migration rule: a token minted before reset_purpose existed WAS a reset."""
    db = _fresh()
    u = _new_user(db)
    u.reset_token_hash = hash_token("legacy-token")
    u.reset_expires_at = datetime.utcnow() + timedelta(minutes=30)
    u.reset_purpose = None
    db.commit()

    assert main._purpose_of(u) == "reset"
    assert main._purpose_for_token(db, "legacy-token") == "reset"


# --- token lifecycle ---------------------------------------------------------
def test_a_token_is_single_use():
    db = _fresh()
    _patch_cur()
    _capture()
    u = _new_user(db)
    link, _ = main._issue_reset_link(db, u, _FakeRequest(), purpose=main.INVITE)
    token = link.split("token=")[1]

    form = {"token": token, "password": _FIXTURE_PW, "password2": _FIXTURE_PW}
    resp = asyncio.run(main.reset_submit(_FakeRequest(form=form), db))
    assert resp.status_code == 303, "first use should succeed"
    db.refresh(u)
    assert u.password_hash, "the credential should now be set"
    assert u.reset_token_hash is None and u.reset_expires_at is None
    assert u.reset_purpose is None, "a spent token should leave nothing behind"

    assert main._user_for_reset_token(db, token) is None, "second use must be refused"


def test_an_expired_invite_is_rejected_but_still_knows_it_was_an_invite():
    """An expired invite must not tell a customer to go and reset something they never had."""
    db = _fresh()
    _patch_cur()
    _capture()
    u = _new_user(db)
    link, _ = main._issue_reset_link(db, u, _FakeRequest(), purpose=main.INVITE)
    token = link.split("token=")[1]
    u.reset_expires_at = datetime.utcnow() - timedelta(minutes=1)
    db.commit()

    assert main._user_for_reset_token(db, token) is None, "expired token must not validate"
    assert main._purpose_for_token(db, token) == "invite", \
        "the expired page still needs invite wording"


def test_disabling_an_account_kills_its_outstanding_link():
    """Otherwise someone holding an invite sets a password, is told they're all set, and
    bounces straight off the sign-in page."""
    db = _fresh()
    _patch_cur()
    _capture()
    u = _new_user(db)
    link, _ = main._issue_reset_link(db, u, _FakeRequest(), purpose=main.INVITE)
    token = link.split("token=")[1]
    assert main._user_for_reset_token(db, token) is not None

    u.active = False
    db.commit()
    assert main._user_for_reset_token(db, token) is None


def test_a_new_link_kills_the_previous_one():
    db = _fresh()
    _patch_cur()
    _capture()
    u = _new_user(db)
    first, _ = main._issue_reset_link(db, u, _FakeRequest(), purpose=main.INVITE)
    second, _ = main._issue_reset_link(db, u, _FakeRequest(), purpose=main.INVITE)

    assert first != second
    assert main._user_for_reset_token(db, first.split("token=")[1]) is None
    assert main._user_for_reset_token(db, second.split("token=")[1]) is not None


def test_an_inactive_account_gets_no_token_at_all():
    """Minting one would let someone set a credential they still can't sign in with."""
    db = _fresh()
    _patch_cur()
    sent = _capture()
    u = _new_user(db)
    u.active = False
    db.commit()

    link, delivered = main._issue_reset_link(db, u, _FakeRequest(), purpose=main.INVITE)
    assert link == "" and delivered is False
    assert u.reset_token_hash is None
    assert not sent.calls


# --- delivery ----------------------------------------------------------------
def test_the_payload_carries_the_note_purpose_and_inviting_admin():
    db = _fresh()
    _patch_cur()
    sent = _capture()
    u = _new_user(db)
    main._send_link_response(_FakeRequest(), db, u, "Hi Sam, as discussed Tuesday.")

    p = sent.last
    assert p["to"] == "sam@mova.com"
    assert p["purpose"] == "invite"
    assert p["expires_in"] == "7 days"
    assert p["note"] == "Hi Sam, as discussed Tuesday."
    assert "Hi Sam, as discussed Tuesday." in p["body"], "the note must reach the email body"
    assert p["invited_by"] == ADMIN
    assert p["reply_to"] == ADMIN, \
        "a no-reply sender is only humane if replies reach a person"
    assert p["url"].startswith("https://portal.test/reset?token=")
    assert sent.calls[-1]["headers"]["X-Sync-Token"] == "test-token", \
        "should fall back to SYNC_TOKEN when N8N_WEBHOOK_TOKEN is unset"
    assert sent.calls[-1]["headers"]["Content-Type"] == "application/json"


def test_the_invite_and_reset_emails_read_differently():
    inv_subj, inv_body = notify._compose(purpose=notify.INVITE, url="https://x/1", note="",
                                         invited_by="a@b.com", expires_in="7 days")
    res_subj, res_body = notify._compose(purpose=notify.RESET, url="https://x/1", note="",
                                         invited_by="", expires_in="45 minutes")
    assert inv_subj != res_subj
    assert "given access" in inv_body
    assert "didn't ask for this" in res_body, \
        "a reset must tell an unexpecting recipient they can ignore it"


def test_a_delivery_failure_never_raises_and_still_returns_the_link():
    """The property the whole copy-the-link fallback UI depends on."""
    db = _fresh()
    _patch_cur()
    _capture(fail=True)
    u = _new_user(db)
    link, delivered = main._issue_reset_link(db, u, _FakeRequest(), purpose=main.INVITE)

    assert delivered is False
    assert link.startswith("https://portal.test/reset?token=")
    assert u.reset_token_hash is not None, \
        "the token must persist so the admin can send the link by hand"


def test_no_webhook_configured_reports_undelivered_without_calling_out():
    sent = _capture()
    notify.WEBHOOK_URL = ""
    ok = notify.send_account_link(to="sam@mova.com", url="https://x/1",
                                  purpose=notify.INVITE, expires_min=10080)
    assert ok is False, "local dev must report 'not emailed', so the UI shows the link"
    assert not sent.calls


def test_expiry_is_worded_not_printed():
    assert notify.in_words(10080) == "7 days"
    assert notify.in_words(45) == "45 minutes"
    assert notify.in_words(1440) == "24 hours"


# --- admin guards ------------------------------------------------------------
def test_a_duplicate_email_is_a_message_not_a_five_hundred():
    db = _fresh()
    _patch_cur()
    _capture()
    _new_user(db, email="sam@mova.com")
    req = _FakeRequest(form={"email": "sam@mova.com", "role": "customer", "active": "on"})
    resp = asyncio.run(main.admin_user_save(req, None, db))

    assert resp.status_code == 400
    count = db.scalar(main.select(main.func.count()).select_from(User)
                      .where(User.email == "sam@mova.com"))
    assert count == 1, "the duplicate must not have been created"


def test_the_last_active_admin_cannot_be_demoted_or_disabled():
    db = _fresh()
    _patch_cur()
    a = _admin_row(db)
    assert main._only_active_admin(db, a) is True

    demote = _FakeRequest(form={"email": a.email, "role": "internal", "active": "on"})
    assert asyncio.run(main.admin_user_save(demote, a.id, db)).status_code == 400
    db.refresh(a)
    assert a.role == "admin", "the demotion must not have been applied"

    disable = _FakeRequest(form={"email": a.email, "role": "admin"})   # no active=on
    assert asyncio.run(main.admin_user_save(disable, a.id, db)).status_code == 400
    db.refresh(a)
    assert a.active is True


def test_a_second_admin_makes_demotion_allowable_again():
    db = _fresh()
    _patch_cur()
    db.add(User(email="two@macgeargroup.com", role="admin",
                password_hash=hash_password(_FIXTURE_PW), active=True))
    db.commit()
    a = _admin_row(db)
    assert main._only_active_admin(db, a) is False

    resp = asyncio.run(main.admin_user_save(
        _FakeRequest(form={"email": a.email, "role": "internal", "active": "on"}), a.id, db))
    assert resp.status_code == 303
    db.refresh(a)
    assert a.role == "internal"


def test_the_last_active_admin_cannot_be_deleted():
    db = _fresh()
    _patch_cur()
    a = _admin_row(db)
    resp = main.admin_user_delete(a.id, _FakeRequest(), db)
    assert resp.status_code == 400
    assert db.get(User, a.id) is not None


# --- status chip -------------------------------------------------------------
def test_the_status_chip_separates_never_invited_from_invited():
    """The old two-state chip said "invite pending" for both, which hid accounts that were
    created weeks ago and never actually sent anything."""
    db = _fresh()
    never = _new_user(db, email="never@mova.com")
    assert main._user_status(never)[1] == "not invited"

    invited = _new_user(db, email="waiting@mova.com")
    invited.reset_token_hash = "x"
    invited.reset_expires_at = datetime.utcnow() + timedelta(days=6)
    db.commit()
    assert main._user_status(invited)[1] == "invited"
    assert "expires" in main._user_status(invited)[2]

    lapsed = _new_user(db, email="lapsed@mova.com")
    lapsed.reset_token_hash = "y"
    lapsed.reset_expires_at = datetime.utcnow() - timedelta(days=1)
    db.commit()
    assert main._user_status(lapsed)[1] == "not invited", \
        "an expired invite is as good as never sent — it needs re-sending"

    signed_in = _new_user(db, email="in@mova.com", pw_hash=hash_password(_FIXTURE_PW))
    assert main._user_status(signed_in)[1] == "active"

    signed_in.active = False
    db.commit()
    assert main._user_status(signed_in)[1] == "disabled"


# --- /forgot: throttle without becoming an enumeration oracle ----------------
def test_forgot_throttles_repeated_requests_for_one_address():
    db = _fresh()
    _capture()
    main._forgot_hits.clear()
    u = _new_user(db, email="sam@mova.com", pw_hash=hash_password(_FIXTURE_PW))

    minted = []
    for _ in range(5):
        before = u.reset_token_hash
        asyncio.run(main.forgot_submit(_FakeRequest(form={"email": u.email}), db))
        db.refresh(u)
        minted.append(u.reset_token_hash != before)
    assert sum(minted) == 3, f"3 mints per address per 15 min, got {sum(minted)}"


def test_forgot_is_indistinguishable_for_known_unknown_and_throttled():
    """If the throttle (or a missing account) changed the page, /forgot would become exactly
    the account-enumeration oracle its single generic response exists to prevent."""
    db = _fresh()
    _capture()
    main._forgot_hits.clear()
    _new_user(db, email="sam@mova.com", pw_hash=hash_password(_FIXTURE_PW))

    def body(email):
        r = asyncio.run(main.forgot_submit(_FakeRequest(form={"email": email}), db))
        return r.status_code, r.body

    known = body("sam@mova.com")
    unknown = body("nobody@example.com")
    for _ in range(4):
        throttled = body("sam@mova.com")

    assert known == unknown, "a known address must look exactly like an unknown one"
    assert known == throttled, "a throttled request must look exactly like a fresh one"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except Exception as e:                      # noqa: BLE001
                failures += 1
                print(f"FAIL  {name}: {e}")
    teardown_module()
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
