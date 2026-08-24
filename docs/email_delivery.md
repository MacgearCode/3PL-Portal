# Email delivery — invites and password resets

The portal emails two things: an **invite** (a new user sets their own password, link good for
7 days) and a **password reset** (45 minutes). Both are single-use links to `/reset?token=…`.

**The app holds no mail credentials.** It composes the subject and body and POSTs them to an
n8n webhook, which does the sending. Until that webhook is configured, the portal still works
— it shows the link on screen for the admin to copy, and logs it to the console. Nothing
breaks if you never finish this setup; you just keep copying links by hand.

> **Why the app owns the email copy, not n8n.** This repo has already paid for the opposite
> arrangement once: `CHARGE_ITEMS` lived in the n8n Code node holding sandbox item ids that
> did not exist in production, which made "go to production" mean "edit the workflow". Email
> copy is the same category — business content that belongs in git, diffable and testable.
> n8n's job here is transport only. The payload does carry the structured fields, so a future
> HTML template in n8n is possible without an app change; if you do that, **HTML-escape
> `note`** (it is admin-authored free text; the app sends plain text precisely to avoid this).

---

## Part 1 — the sending mailbox (M365 / Azure)

The sender is chosen entirely inside n8n, so this part is independent of the app.

> 📋 **Handing Part 1 to IT?** `docs/it_request_portal_mailer.md` is a forwardable version of
> this whole section — the three tasks, the roles each needs, and what to send back.

**A shared mailbox `noreply@macgeargroup.com`, sent via Graph app-only, from its own dedicated
app registration.** A shared mailbox needs no licence and cannot be signed into — which is
what "no reply" should mean. App-only means **no person is in the loop**: nothing here breaks
when someone's password changes, MFA re-prompts, or they leave.

> **Why not reuse an existing credential.** The n8n Microsoft Outlook node uses *delegated*
> OAuth and always sends as the signed-in mailbox — today that is Aaron's personal mailbox,
> which is exactly the coupling this is meant to remove. And why a **new** app registration
> rather than *Macgear Claude Agent*: that client id is shared with other Graph automations, so
> an access policy scoped to `noreply@` could break them. A dedicated app makes the blast
> radius exactly one mailbox that only ever sends invites.

### 1. The mailbox

> ⚠️ **`noreply@macgeargroup.com` already exists on this tenant** as an unlicensed / inactive
> user (as at 2026-08-12), so **check what it is before creating anything.** You cannot create a
> shared mailbox on an address another object already holds — it fails with "address already in
> use" — and in Entra a *soft-deleted* user keeps its addresses until purged from **Deleted
> users**, so deleting isn't an instant fix either.

**Two words that sound alarming and mean different things here:**

- **"Inactive" / sign-in blocked — irrelevant.** App-only Graph never signs in as the user, so a
  blocked account with a working mailbox sends perfectly. This is why app-only was chosen.
- **"Unlicensed" — only matters if it means no mailbox.** Shared mailboxes are *always* listed
  as unlicensed in **Active users**, which is the most likely explanation. A user object with no
  mailbox at all fails with `MailboxNotEnabledForRESTAPI`.

**The definitive check** (30 seconds): Exchange admin centre `admin.exchange.microsoft.com` →
**Recipients → Mailboxes**, and look at `noreply@`'s *Recipient type*:

| What you find | What to do |
|---|---|
| **Shared** | ✅ Nothing to do — skip to step 2. Just confirm the display name is `Macgear Group` (that's the sender name customers see) and fix it if not. |
| **User mailbox**, unlicensed | Convert it: select the mailbox → **Convert to shared mailbox**. Keeps the address and all settings, needs no licence afterwards. Do this promptly — an unlicensed user mailbox is in a ~30-day grace period before it's removed. |
| **Not listed under Mailboxes at all** | There is no mailbox, only an Entra user object holding the address. Either (a) assign an Exchange Online Plan 1 licence, wait for the mailbox to provision (minutes), **Convert to shared mailbox**, then remove the licence — shared mailboxes need none; or (b) delete the user **and purge it** from Entra → Deleted users, then create the shared mailbox fresh. (a) is safer: it never puts the address in a soft-deleted limbo. |

Only if it genuinely doesn't exist: M365 admin centre → **Teams & groups → Shared mailboxes →
Add a shared mailbox**, name `Macgear Group`, address `noreply@macgeargroup.com`.

⚠️ **Name is what recipients see** as the sender's display name — Graph takes the From display
name from the mailbox, not from the payload. Nobody needs to be a member of the mailbox;
app-only access doesn't go through membership.

### 2. Register a dedicated app

portal.azure.com → **App registrations → New registration**.

- Name: `Macgear Portal Mailer`
- Supported account types: **single tenant**
- Redirect URI: leave blank (client-credentials needs none)

Then, on the new app:

1. **API permissions → Add a permission → Microsoft Graph → Application permissions →
   `Mail.Send`** → Add. Then **Grant admin consent for Macgear Group**. It must show
   *Granted* with type **Application** — a Delegated `Mail.Send` will not work here.
2. **Certificates & secrets → New client secret.** Copy the value immediately, it is shown
   once. Set the longest expiry offered and **diarise it** — when it lapses, invites stop
   sending silently (the portal logs the failure and keeps showing the copyable link, so
   nobody sees an error page).
3. Note the **Application (client) ID** and confirm the **Directory (tenant) ID** is
   `9370e6f0-7dde-4255-9bbe-6af58d8e0dd4`.

> ✅ **Done, 2026-08-24.** `Macgear Portal Mailer` client id is
> **`dfbb1ab2-7014-490c-91be-950112bd468f`**, tenant `9370e6f0-7dde-4255-9bbe-6af58d8e0dd4`.
> A client id is not a credential — it is useless without the secret, and it has to be quoted
> in the access-policy cmdlets below, so it lives here in git. **The secret is typed into the
> n8n credential and nowhere else:** not in this repo, not in the app's `.env`, not in
> `settings.json`. The app never talks to Graph, so it has no use for it.

### 3. ⚠️ Scope the app to that one mailbox

Application `Mail.Send` is **tenant-wide by default** — as granted, the app can send as *any*
mailbox at Macgear, including every staff member's. An Exchange application access policy
restricts it to one.

This is the step that makes the setup actually secure, and it is PowerShell-only — there is no
GUI for it.

**Who runs it, and where:** any workstation with internet — it's a one-time Microsoft 365 tenant
change, **nothing to do with the droplet**. The portal never calls Graph or Exchange; it only
POSTs to n8n. Nothing here is repeated at deploy time.

**Permissions needed** — hand this to whoever did steps 1 and 2 if you don't hold these roles:

| Cmdlet | Role |
|---|---|
| `New-DistributionGroup` | Recipient Management |
| `New-ApplicationAccessPolicy` | **Organization Management** (Exchange Administrator / Global Administrator) |

⚠️ Exchange *hides* cmdlets your role doesn't grant, so insufficient permissions show up as
"the term `New-ApplicationAccessPolicy` is not recognized" rather than an access-denied error.
`Install-Module` itself needs no elevation (`-Scope CurrentUser` writes to your profile).

**This step can be deferred** — without it, everything still works; the app-only permission is
just broader than it needs to be (it could send as any mailbox at Macgear). So invites can be
tested and even go live first. Don't leave it undone.

```powershell
Install-Module ExchangeOnlineManagement -Scope CurrentUser -Force
Connect-ExchangeOnline -UserPrincipalName aaron@macgeargroup.com

# A mail-enabled security group holding only the mailboxes this app may send as.
New-DistributionGroup -Name "Portal Mailer Senders" -Alias portal-mailer-senders `
    -PrimarySmtpAddress portal-mailer-senders@macgeargroup.com -Type Security
Add-DistributionGroupMember -Identity portal-mailer-senders -Member noreply@macgeargroup.com

New-ApplicationAccessPolicy `
    -AppId "dfbb1ab2-7014-490c-91be-950112bd468f" `
    -PolicyScopeGroupId portal-mailer-senders@macgeargroup.com `
    -AccessRight RestrictAccess `
    -Description "Macgear Portal Mailer may only send as noreply@"
```

Verify it grants the one mailbox and denies everything else:

```powershell
Test-ApplicationAccessPolicy -Identity noreply@macgeargroup.com -AppId "dfbb1ab2-7014-490c-91be-950112bd468f"
Test-ApplicationAccessPolicy -Identity aaron@macgeargroup.com   -AppId "dfbb1ab2-7014-490c-91be-950112bd468f"
```

Expect `Granted` then `Denied`. Policy changes can take up to ~30 minutes to apply, so if the
first n8n test returns `ErrorAccessDenied`, wait and retry before changing anything.

> This policy restricts **application** permissions only. It has no effect on the delegated
> credential the birthday notifier uses, so that keeps working regardless — and because this is
> a brand-new app id, nothing else is touched either way.

### If you want invites working before doing any of this

Point the **Send via Graph** node at n8n's **Microsoft Outlook** node instead, using the
existing delegated credential. The sender is then whichever mailbox that credential is
consented as (currently Aaron's), with Reply-To still set to the inviting admin. Everything
else — the app, the links, the copy — is unchanged, and swapping to `noreply@` later is one
node edit.

---

## Part 2 — the n8n workflow

### 4. Import it

n8n → **Workflows → Import from File** → `n8n/3pl_account_email.json` in this repo.

Both credentials import as `REPLACE_ME` and show red. That's expected — credentials are never
exported.

### 5. Create the two credentials

**a. Header Auth** — on the *Webhook* node, field **Credential for Header Auth**.

| Field | Value |
|---|---|
| Name | `3PL Portal Webhook Token` |
| Header Name | `X-Sync-Token` |
| Header Value | the app's `SYNC_TOKEN` from the droplet `.env` |

The app sends `N8N_WEBHOOK_TOKEN` if set, otherwise falls back to `SYNC_TOKEN`. Whichever it
sends must equal this value. Header auth is used rather than an IF node comparing a literal so
the secret is encrypted with `N8N_ENCRYPTION_KEY`, never lands in a workflow export, and a bad
token is rejected with 403 before any node runs.

**b. OAuth2 API** (generic, client-credentials) — on the *Send via Graph* node.

| Field | Value |
|---|---|
| Name | `Macgear Portal Mailer (client credentials)` |
| Grant Type | **Client Credentials** |
| Access Token URL | `https://login.microsoftonline.com/9370e6f0-7dde-4255-9bbe-6af58d8e0dd4/oauth2/v2.0/token` |
| Client ID | `dfbb1ab2-7014-490c-91be-950112bd468f` (`Macgear Portal Mailer`) |
| Client Secret | the secret from Part 1 step 2 — typed here, stored nowhere else |
| Scope | `https://graph.microsoft.com/.default` |
| Authentication | Body |

⚠️ Scope must be exactly `.default` — Graph app-only rejects individual scopes.
⚠️ Use the **Macgear Portal Mailer** app, not *Macgear Claude Agent*. The access policy in
Part 1 step 3 is scoped to the new app id, so the old one would send unrestricted.

### 6. Save, then Activate

**The production URL does not exist until the workflow is Active.**

### 7. Copy the production URL

Open the Webhook node and copy the **Production URL**:

```
https://n8n.macgeargroup.com/webhook/3pl-account-email
```

⚠️ The **Test URL** is `/webhook-test/…` and only fires while *Listen for test event* is open.
Pointing the app at the test URL is the single most common way this looks broken.

---

## Part 3 — point the app at it

### 8. Test from localhost first (no commit, no deploy)

`notify.py` reads its environment **at import**, so these must be set *before* uvicorn starts,
in the same PowerShell session. Open *Listen for test event* in n8n first.

```powershell
cd "C:\Users\aaron\Desktop\Claude Projects\3PL Portal"
$env:N8N_RESET_WEBHOOK_URL = "https://n8n.macgeargroup.com/webhook-test/3pl-account-email"
$env:N8N_WEBHOOK_TOKEN     = "<the header credential value>"
$env:PUBLIC_BASE_URL       = "http://localhost:8000"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

Create a user with your own address, press **Send invite**, and check: the page says *Invite
emailed to …*, the n8n execution is green, and the mail arrives (check Junk — the first send
from a brand-new mailbox containing a link often lands there). The link will point at
`localhost`, which is fine for proving delivery.

### 9. Then the droplet

Order matters: **import and activate the workflow before adding the env var.** With the URL set
and no workflow behind it, every send is a 404 that the app swallows by design — so it fails
silently.

In `/opt/threepl/3PL-Portal/.env`:

```
N8N_RESET_WEBHOOK_URL=http://n8n:5678/webhook/3pl-account-email
N8N_WEBHOOK_TOKEN=
INVITE_TOKEN_TTL_MIN=10080
```

Prefer the **internal** address over the public one — both containers share
`SHARED_NETWORK`, the same reasoning as `APP_BASE = 'http://threepl:8000'` in the sync node, and
it avoids a hairpin through Caddy plus a dependency on public DNS/TLS. Confirm the n8n service
name with `docker ps` first.

```bash
docker compose up -d          # re-reads env_file; --build only if code changed
docker compose logs --tail=80 threepl
```

⚠️ **Check `PUBLIC_BASE_URL` is actually set.** If it's blank the link is built from
`request.base_url`, which behind Caddy can come out as `http://threepl:8000/` — unopenable.
That used to be caught by an admin copy-pasting the link; now it goes straight to a customer.

---

## The payload contract

What the app POSTs, so the workflow can be rebuilt from scratch. Source of truth is
`app/notify.py`.

| Field | Example | Notes |
|---|---|---|
| `app` | `3pl-portal` | Lets one webhook serve other Macgear apps later |
| `purpose` | `invite` \| `reset` | Drives nothing in n8n; useful for filtering executions |
| `to` | `sam@mova.com` | |
| `subject` | `Your access to the Macgear 3PL Portal` | Composed by the app |
| `body` | *(multi-line plain text)* | Composed by the app. **Plain text, not HTML** |
| `url` | `https://3pl.macgeargroup.com/reset?token=…` | Single-use |
| `note` | `Hi Sam, as discussed Tuesday.` | Optional, admin-typed, already inside `body` |
| `invited_by` | `aaron@macgeargroup.com` | The admin who pressed the button |
| `reply_to` | `aaron@macgeargroup.com` | Set as the Reply-To header |
| `expires_in` | `7 days` \| `45 minutes` | Words, not minutes — it's copy |

Header: `X-Sync-Token: <token>`. Timeout: 10s. The app reads **only the HTTP status** and
ignores the response body.

---

## Triage

| Symptom | Cause |
|---|---|
| App log `invite email webhook failed … 404` | Workflow not **Activated**, or you used the `/webhook-test/` URL |
| `… 403` | Header credential value ≠ what the app sends, or the header isn't `X-Sync-Token` |
| Timeout / name not known | Wrong host; on the droplet, containers not on the same `SHARED_NETWORK`, or wrong service name |
| n8n execution red on the send node | Credential not re-picked after import, secret expired, or the access policy excludes that mailbox (`Test-ApplicationAccessPolicy`) |
| Graph returns `ErrorAccessDenied` | The application access policy is denying it — check the group contains the sending mailbox |
| Graph returns `MailboxNotEnabledForRESTAPI` | `noreply@` has no real mailbox — it's an Entra user object or an unlicensed user mailbox past its grace period. See Part 1 step 1 |
| Graph returns `ErrorInvalidUser` / `ResourceNotFound` | The address in the node's URL doesn't resolve at all — typo, or the object was deleted and not yet purged |
| Execution green, no email | Wrong mailbox in the Graph URL; then check the recipient's Junk folder |
| Execution green, email arrives **minutes late** | Normal for a new sending mailbox with no reputation. Seen on the first live test (2026-08-24): two resets sent at 14:10 landed several minutes later, which read as a delivery failure and sent us chasing the webhook. Wait 5 minutes and check Junk **before** changing anything |
| Two links in the inbox, one says invalid | Working as intended — minting kills the previous token, so only the newest link is live |
| Email arrives, link 404s or points at `threepl:8000` | `PUBLIC_BASE_URL` unset or wrong |
| No app log line at all | `N8N_RESET_WEBHOOK_URL` blank, or set after uvicorn had already started |
| Admin sees "nothing was emailed" every time | Same as the line above — that message *is* the "delivery didn't happen" signal |
