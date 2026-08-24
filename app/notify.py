"""Outbound notifications. The app holds no SMTP/Graph creds — it POSTs the composed email
to an n8n webhook, which sends it (see docs/email_delivery.md). When N8N_RESET_WEBHOOK_URL is
unset (local dev) the email is logged to the console instead, so ./run.ps1 works fully
offline. Stdlib only.

The APP composes the subject and body; n8n only sends. Deliberate: this repo has already
paid for the opposite arrangement once — CHARGE_ITEMS lived in the n8n Code node holding
sandbox item ids, "a standing cutover trap that made 'go to production' mean 'edit the
workflow'". Email copy is the same category of thing: business content that should be in
git, diffable, reviewable and testable, not lost on a workflow re-import. The payload also
carries the structured fields, so n8n CAN build its own template later without an app change.

Body is PLAIN TEXT. The personal note is admin-authored free text, and plain text removes the
escaping question entirely. If anyone switches n8n to an HTML body, `note` must be escaped
there or an admin typing "<" breaks the email.
"""
import json
import logging
import os
import urllib.request

log = logging.getLogger("threepl.notify")

INVITE = "invite"
RESET = "reset"

# NOTE: read at IMPORT time. Setting these after uvicorn has started has no effect — the
# usual cause of "why didn't it send" when testing locally.
WEBHOOK_URL = os.environ.get("N8N_RESET_WEBHOOK_URL", "")
# Shared secret sent as a header for the n8n Webhook node's header-auth. Reuses SYNC_TOKEN
# if a dedicated one isn't set, so a single secret can cover both integration directions.
WEBHOOK_TOKEN = os.environ.get("N8N_WEBHOOK_TOKEN", "") or os.environ.get("SYNC_TOKEN", "")

PORTAL_NAME = "Macgear 3PL Portal"


def in_words(minutes: int) -> str:
    """Expiry as copy, not as a number — this lands in the email the customer reads."""
    if minutes >= 2880:                     # 2 days or more
        return f"{round(minutes / 1440)} days"
    if minutes >= 1440:
        return "24 hours"
    if minutes >= 120:
        return f"{round(minutes / 60)} hours"
    return f"{minutes} minutes"


def _compose(*, purpose: str, url: str, note: str, invited_by: str,
             expires_in: str) -> tuple[str, str]:
    """Return (subject, plain-text body)."""
    if purpose == INVITE:
        subject = f"Your access to the {PORTAL_NAME}"
        lines = [
            f"You've been given access to the {PORTAL_NAME}, where you can see your stock on"
            " hand, incoming containers, receipts, dispatches and invoices.",
        ]
        if note:
            who = invited_by or "Macgear"
            lines += ["", f"{who} added a note:", "", f"    {note}"]
        lines += [
            "",
            "Open this link to choose your password and sign in:",
            "",
            f"    {url}",
            "",
            f"The link works once and expires in {expires_in}. You choose the password"
            " yourself — nobody at Macgear can see it.",
        ]
    else:
        subject = f"Reset your {PORTAL_NAME} password"
        lines = [
            f"Someone asked to reset the {PORTAL_NAME} password for this address.",
        ]
        if note:
            lines += ["", f"{invited_by or 'Macgear'} added a note:", "", f"    {note}"]
        lines += [
            "",
            "Open this link to choose a new password:",
            "",
            f"    {url}",
            "",
            f"The link works once and expires in {expires_in}. If you didn't ask for this you"
            " can ignore this email — your current password still works.",
        ]

    if invited_by:
        lines += ["", f"Sent by {invited_by}, Macgear Group."]
    else:
        lines += ["", "Macgear Group."]
    return subject, "\n".join(lines)


def _post(url: str, body: bytes, headers: dict) -> int:
    """The only socket in this module — the seam the tests stub. Returns the HTTP status."""
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()
        return resp.status


def send_account_link(*, to: str, url: str, purpose: str = RESET, note: str = "",
                      invited_by: str = "", expires_min: int = 45) -> bool:
    """Compose an invite / password-reset email and hand it to n8n to send.

    Returns True when the webhook ACCEPTED the request — that is not proof of delivery, so
    the caller must keep showing the copyable link either way. Never raises: a delivery
    failure must not 500 an admin form, and must not reveal (via /forgot) whether an
    address exists.
    """
    expires_in = in_words(expires_min)
    subject, body = _compose(purpose=purpose, url=url, note=note,
                             invited_by=invited_by, expires_in=expires_in)

    if not WEBHOOK_URL:
        # Local dev: log the whole email so the copy can actually be reviewed. Reported as
        # delivered=False so the admin UI says "email isn't configured here".
        log.warning("N8N_RESET_WEBHOOK_URL unset — %s email for %s not sent.\n"
                    "Subject: %s\n%s", purpose, to, subject, body)
        return False

    payload = {"app": "3pl-portal", "purpose": purpose, "to": to,
               "subject": subject, "body": body, "url": url, "note": note,
               "invited_by": invited_by,
               # A no-reply sender is only humane if a reply reaches a human.
               "reply_to": invited_by, "expires_in": expires_in}
    headers = {"Content-Type": "application/json"}
    if WEBHOOK_TOKEN:
        headers["X-Sync-Token"] = WEBHOOK_TOKEN
    try:
        status = _post(WEBHOOK_URL, json.dumps(payload).encode(), headers)
    except Exception as e:  # noqa: BLE001 — log and swallow; see docstring
        log.error("%s email webhook failed for %s: %s", purpose, to, e)
        return False
    log.info("%s email handed to n8n for %s (HTTP %s)", purpose, to, status)
    return True
