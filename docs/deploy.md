# Deploy to the droplet — sandbox first, then production

> **Production went live 2026-07-27.** For the production account specifically — verified internal
> ids, the TBA/integration/role/token setup, the cache purge, and the failure modes actually hit —
> use **`production_cutover.md`**, which is the record of what was done. This document remains the
> general/sandbox walkthrough and the starting point for a fresh environment.

The app never calls NetSuite (see `netsuite_integration.md`). Deploying is therefore just:
ship the web app, deploy the RESTlet in NetSuite, point the n8n Code node at it. Do it all
against the **NetSuite sandbox** first, then flip a handful of constants to go to production.

## 0. What the sandbox actually lets you test
- **Skriva** is live in the sandbox (customer `10496`, vendor `10503`, location `2`, class `236`)
  — this is your real end-to-end sync test. Internal ids usually match prod in a refresh copy,
  but confirm them in the sandbox UI before trusting them.
- **Mova is live in production as of 2026-07-27** (stock landed 24 Jul: 9 containers / 6,369 units).
  Supplier `10872`, class `237`, location `49`, subsidiary `2` — see `production_cutover.md` §0.
  Its `ns_customer_id` is **`11066`** ("Spacewalker Technology Hong Kong Co., Limited", currency
  AUD), decided 2026-08-05. The seed still ships `TBD`, so set it in `/admin/customers` on any
  fresh environment.
- The 3PL service items must exist in whichever account you're pointing at for `create_invoice`
  to work. They are **no longer listed in the n8n node** — since 2026-08-05 the mapping lives in
  the app (`charge_item`, admin console → **Charge items**) and each pending billing line carries
  its own `ns_item_id`. On a sandbox, remap the ids there; no workflow edit. Skriva's rate card is
  seeded at $0, so a Skriva billing run produces a $0 draft — perfect for proving the push loop
  without real charges.

## 1. App on the droplet (Docker, behind Caddy)
1. Copy this repo to the droplet (the GitHub repo, see README) and `cd` in.
2. `cp .env.example .env` and fill it in:
   - `APP_SECRET`, `SYNC_TOKEN`, `PGPASSWORD` → `openssl rand -hex 32` (use the same password in `DATABASE_URL`).
   - `SHARED_NETWORK` → the network Caddy + n8n are on (`docker network ls`).
   - Leave `SEED_DEMO=0` so no fake cache rows are planted.
   - `PUBLIC_BASE_URL` → the real public origin. **Not optional.** Blank means invite and
     reset links get built from `request.base_url`, which behind Caddy comes out as
     `http://threepl:8000/` — unopenable, and it now goes straight to a customer's inbox.
   - `N8N_RESET_WEBHOOK_URL` → leave **blank** for now; see §1.5.
3. `docker compose up -d --build`. First boot creates the schema and seeds Mova/Skriva + rate cards
   + the admin/internal users. **Log in and change the seeded passwords immediately.**
4. Add a Caddy site block (mirrors the promos app) and reload Caddy:
   ```
   3pl.macgeargroup.com {
       reverse_proxy threepl:8000
   }
   ```
   The app has its own per-user login, so Caddy basic-auth is optional here (unlike promos).
   To add an outer gate anyway: `basic_auth { aaron <bcrypt-hash> }` (`caddy hash-password`).

## 1.5 Email delivery for invites + password resets
Optional, and safe to leave until after the app is up — without it the admin console still
shows every invite/reset link for you to copy and send by hand.

Full walkthrough (shared `noreply@` mailbox, the Exchange application access policy, the n8n
workflow import, and how to test it from localhost before deploying):
**`docs/email_delivery.md`**.

⚠️ Order matters: **import and activate the n8n workflow before** setting
`N8N_RESET_WEBHOOK_URL`. With the URL set and nothing behind it, every send is a 404 that the
app swallows by design — so it fails silently.

## 2. RESTlet in the NetSuite **sandbox**
1. Sandbox → Setup > Company > Enable Features → SuiteCloud: tick **Token-Based Authentication**
   and **SuiteScript / RESTlets**.
2. Customization > Scripting > Scripts > New → upload `netsuite/3pl_restlet.js`, type **RESTlet**,
   POST function = `post`. Create a **Deployment**, status **Released**, on an integration role that
   can run SuiteQL and create invoices. Copy the `script=` and `deploy=` ids from the External URL.

## 3. TBA credentials (sandbox)
1. Setup > Integration > Manage Integrations > New → enable Token-Based Auth → save the
   **Consumer Key/Secret**.
2. Setup > Users/Roles > Access Tokens > New → that integration + the integration role →
   save the **Token ID/Secret**.
3. Note the **sandbox Account ID** — it looks like `1234567_SB1`.

## 4. n8n Code node (sandbox values)
In `netsuite/n8n_3pl_sync.js` fill the constants:
- `ACCOUNT_ID = '1234567_SB1'` — the node lowercases + swaps `_`→`-` for the URL host automatically.
- `CONSUMER_KEY/SECRET`, `TOKEN_ID/SECRET`, `RESTLET_SCRIPT`, `RESTLET_DEPLOY` from steps 2–3.
- `APP_BASE = 'http://threepl:8000'`, `SYNC_TOKEN` = the app's `SYNC_TOKEN`.
- `CUSTOMERS`: not in the node — fetched from the app's `/admin/sync-config`, so adding a customer
  in the admin console is all that's needed.
- Charge items: **not in the node either** (the old `CHARGE_ITEMS` constant was removed 2026-08-05).
  Each pending billing line arrives from `/admin/billing/pending` carrying its own `ns_item_id`,
  resolved from the app's catalogue. Remap ids in the admin console → **Charge items**.

Wire: **Schedule Trigger → this Code node.** Run once manually first and read the node output
(it returns one item per read/push step, with `error` keys on any failure).

## 5. Verify the loop (sandbox)
- Reads: after a manual run, the portal's Skriva views (POs, receipts, fulfilments, invoices,
  stock-on-hand) should reflect sandbox data.
- Writes: queue a Skriva billing run in the portal → it shows `ready_to_push` →
  `GET /admin/billing/pending` lists it → next n8n run creates a $0 draft invoice in the sandbox
  and posts the id back → the run flips to `pushed` and links to the invoice.

## 6. Go to production
Flip only the NetSuite-side constants in the n8n node: `ACCOUNT_ID` (prod, no `_SB1`), the four
TBA creds (prod integration + token), and the prod `RESTLET_SCRIPT`/`RESTLET_DEPLOY` (deploy the
same RESTlet in prod). Add Mova to `CUSTOMERS` once its NetSuite records exist, and set Mova's real
`ns_customer_id`/`ns_supplier_id` in the app (Admin → Customers). The app and its `SYNC_TOKEN` don't
change between environments.
