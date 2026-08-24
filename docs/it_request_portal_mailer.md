# IT request — mail sender for the Macgear 3PL Portal

Forwardable brief. Three tasks, all one-time Microsoft 365 tenant configuration. Nothing runs on
a server; the application never calls Microsoft Graph or Exchange itself.

> **Status 2026-08-24 — delivery is live; two items still need someone with the right roles.**
> The mailbox exists and sends, and `Macgear Portal Mailer` is registered as
> **`dfbb1ab2-7014-490c-91be-950112bd468f`** with a client secret in place. Invites and
> password resets were delivered end-to-end on 2026-08-24. **Outstanding:**
>
> 1. **The display name is still "No Reply" — please change it to `Macgear Group`** (task 1
>    below). Aaron cannot: Exchange PowerShell can't see the object from his session
>    (`Get-Mailbox` returns *couldn't be found on SY6P282A02DC004*, while Graph sends as it
>    perfectly), and he has no access to the mailbox properties in the admin centre. This is
>    what every customer sees as the sender, and "No Reply" as a display name reads like bulk
>    mail — it hurts inbox placement on exactly the emails that must not be missed.
> 2. **The application access policy** (task 3 below). `New-ApplicationAccessPolicy` is absent
>    from Aaron's Exchange session, i.e. he lacks Organization Management.
>
> Task 2 is done — nothing needed there.

**Goal:** the 3PL Portal (`3pl.macgeargroup.com`) must email customer invites and password-reset
links from `noreply@macgeargroup.com`, with no user account in the loop.

---

## 1. The `noreply@macgeargroup.com` mailbox

An Entra user object with this address already exists — display name **"No Reply"**, unlicensed,
never signed in. It needs to end up as a **shared mailbox** (no licence, cannot be signed into).

Check `admin.exchange.microsoft.com` → **Recipients → Mailboxes** → search `noreply`:

| Recipient type shown | Action |
|---|---|
| **Shared** | Nothing to do. |
| **User mailbox** | Select it → **Convert to shared mailbox**. Please do this promptly — an unlicensed user mailbox is in a ~30-day grace period. |
| **Not listed** | No mailbox exists. Assign an Exchange Online Plan 1 licence, wait for provisioning, **Convert to shared mailbox**, then remove the licence. Please avoid deleting and recreating the Entra object — a soft-deleted user holds its addresses until purged. |

**Also please change the display name from "No Reply" to `Macgear Group`** (Exchange admin centre
→ the mailbox → General → Display name). This is the sender name customers will see; "No Reply"
as a display name reads like bulk mail and hurts inbox placement. It must be set on the
**Exchange** object, not the Entra one, to reach the From header.

## 2. A dedicated app registration

`portal.azure.com` → **App registrations → New registration**

- Name: `Macgear Portal Mailer`
- Single tenant. No redirect URI (client-credentials flow needs none).

Then on that app:

1. **API permissions → Microsoft Graph → Application permissions → `Mail.Send`** → Add →
   **Grant admin consent**. It must show *Granted*, type **Application**. A *Delegated*
   `Mail.Send` will not work for this.
2. **Certificates & secrets → New client secret.** Longest available expiry. Please send the
   secret value **and** the Application (client) ID to Aaron securely — the value is shown only
   once.

> Please create a **new** registration rather than adding to the existing *Macgear Claude Agent*
> app. That client id is shared with other Graph automations, and the access policy in task 3
> would restrict them too.

## 3. Restrict the app to that one mailbox

Application `Mail.Send` is tenant-wide by default — as granted, the app could send as any mailbox
at Macgear. This scopes it to `noreply@` only. PowerShell only; there is no GUI equivalent.

Requires **Organization Management** (Exchange Administrator or Global Administrator) for
`New-ApplicationAccessPolicy`, and Recipient Management for `New-DistributionGroup`.

```powershell
Install-Module ExchangeOnlineManagement -Scope CurrentUser -Force
Connect-ExchangeOnline

# Mail-enabled security group holding only the mailboxes this app may send as.
New-DistributionGroup -Name "Portal Mailer Senders" -Alias portal-mailer-senders `
    -PrimarySmtpAddress portal-mailer-senders@macgeargroup.com -Type Security
Add-DistributionGroupMember -Identity portal-mailer-senders -Member noreply@macgeargroup.com

New-ApplicationAccessPolicy `
    -AppId "dfbb1ab2-7014-490c-91be-950112bd468f" `
    -PolicyScopeGroupId portal-mailer-senders@macgeargroup.com `
    -AccessRight RestrictAccess `
    -Description "Macgear Portal Mailer may only send as noreply@"
```

Verify — expect `Granted` then `Denied`:

```powershell
Test-ApplicationAccessPolicy -Identity noreply@macgeargroup.com -AppId "dfbb1ab2-7014-490c-91be-950112bd468f"
Test-ApplicationAccessPolicy -Identity aaron@macgeargroup.com   -AppId "dfbb1ab2-7014-490c-91be-950112bd468f"
```

Notes:

- Allow up to ~30 minutes for the policy to take effect.
- This restricts **application** permissions only. It has no effect on any delegated/OAuth
  credential, so existing user-authenticated automations are unaffected.
- ⚠️ Exchange hides cmdlets your role doesn't grant, so insufficient permissions appear as
  *"the term `New-ApplicationAccessPolicy` is not recognized"*, not as an access-denied error.
- If the single-mailbox scope can be applied without creating a group, that's fine too — the
  group is just the documented, reliable way to express it.

---

## What Aaron needs back

1. Confirmation `noreply@macgeargroup.com` is a **shared mailbox** with display name
   **Macgear Group**.
2. The **Application (client) ID** and **client secret** for `Macgear Portal Mailer`.
3. Confirmation the access policy is in place (or that task 3 is pending — it doesn't block
   testing, it just leaves the permission broader than intended).

Tenant ID for reference: `9370e6f0-7dde-4255-9bbe-6af58d8e0dd4`.
