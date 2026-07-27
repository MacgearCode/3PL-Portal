# NetSuite integration — live architecture

> **LIVE on the NetSuite production account since 2026-07-27.** Reads are running on both lanes;
> the billing write path is not yet configured (see the *Billing roadmap* in `CLAUDE.md`).
> Cutover record + verified ids: `production_cutover.md`.

**The droplet app never talks to NetSuite.** It holds no NetSuite credentials and makes no
outbound NetSuite calls. There is **no AI and no MCP** anywhere in the runtime. All NetSuite
communication is server-to-server between **n8n** (which signs Token-Based Auth) and a
**RESTlet** deployed in NetSuite — the same proven pattern as the vendor-credit-claims app.

> The Claude NetSuite MCP was used **only at development time**, by the engineer, to validate
> the SuiteQL against live data (see `netsuite_validation.md`). It is a browser-OAuth claude.ai
> connector, is not in the codebase, and cannot run on the droplet.

```
                 ┌─────────────── n8n (scheduler, signs OAuth1 TBA) ───────────────┐
   READS:  RESTlet(action=invoices|purchase_orders|item_receipts|…) ──rows──▶ POST /admin/ingest ──▶ Postgres cache
   WRITES: GET /admin/billing/pending ──▶ RESTlet(action=create_invoice) ──id──▶ POST /admin/billing/pushed
                 └──────────────────────────────────────────────────────────────┘
        app (FastAPI on droplet): exposes token-authed endpoints only — never calls NetSuite
        RESTlet (netsuite/3pl_restlet.js): runs the validated SuiteQL / creates draft invoices
```

## Components
- **`netsuite/3pl_restlet.js`** — deployed in NetSuite. READ actions run the validated SuiteQL
  and return ingest-shaped rows; `create_invoice` creates a **draft** invoice from billing lines.
- **`netsuite/n8n_3pl_sync.js`** — n8n Code node. Signs TBA, loops customers × entities → POSTs to
  `/admin/ingest`; then drains `/admin/billing/pending`, creates each draft invoice, posts the id back.
- **App endpoints** (token-authed via `X-Sync-Token: $SYNC_TOKEN`):
  - `POST /admin/ingest` — `{customer: slug, entity, rows[]}` → upsert (see `app/netsuite.py` for row contracts).
    Responds `{customer, entity, ingested: N}` — **`ingested: 0` with no error item in the n8n run
    means the SuiteQL succeeded and returned nothing**, i.e. a role permission or subsidiary-access
    problem, not a code bug. See `production_cutover.md` §7.
  - `GET  /admin/billing/pending` — billing runs queued (`ready_to_push`) with lines + customer ns ids.
  - `POST /admin/billing/pushed` — `{run_id, ns_invoice_id}` → marks the run pushed and links the invoice.
- **Portal-internal routes** (session-authed, not for n8n), added with the paged list views:
  `GET /c/{slug}/{view}/rows` returns bare `<tr>`s for the Load more button;
  `GET /c/{slug}/{view}/export.csv` streams the whole current selection. Both enforce the same
  customer scoping as the page.

## Invoice lifecycle (why reads are authoritative)
Queue a run → n8n creates a **draft** invoice in NetSuite → a person approves/edits it there →
status moves Open→Paid/Overdue, credits may be raised. All of that lives in NetSuite, so the
portal's Invoices view is **synced from NetSuite** (read action `invoices`, incl. lines). The app
stores only `billing_run.ns_invoice_id` to link a run to its invoice — never a frozen copy.
A period already queued/pushed/invoiced is locked against re-billing.

## Deploy (one-time)
1. **Enable** SuiteCloud features: Token-Based Authentication + RESTlets.
2. **Deploy the RESTlet:** Scripting > Scripts > New, upload `netsuite/3pl_restlet.js`, Type=RESTlet,
   POST=`post`, status Released, to an integration role that can run SuiteQL and create invoices.
   Copy the External URL's `script=` / `deploy=` ids.
3. **TBA creds:** Integration record (Token-Based Auth) → consumer key/secret; Access Token → token id/secret;
   note the Account ID (realm).
4. **App env (droplet):** set `SYNC_TOKEN` to a long random secret (the app rejects ingest/billing calls
   without it). The app needs NO NetSuite credentials.
5. **n8n:** Schedule Trigger → Code node with `netsuite/n8n_3pl_sync.js`; fill the constants
   (account id, keys/token, script/deploy ids, `APP_BASE`, `SYNC_TOKEN`, each customer's NetSuite ids,
   and `CHARGE_ITEMS` mapping each charge_type → its NetSuite invoice item id).
6. **Cadence — two lanes feeding the same Code node** (mode set by a Set node in front of each):
   - **Fast lane (every 15 min):** Schedule Trigger → Set `{mode:"soh"}` → Code node. Pulls only
     `stock_on_hand` and skips the billing-push writes, keeping the portal's SOH view near-live without
     re-pulling invoices/POs/receipts/fulfilments 96×/day. SOH SuiteQL is one grouped query per customer
     (cheap on governance). `inventorybalance` is real-time in NetSuite, so 15-min polling sees genuine change.
   - **Full lane (daily + the weekly billing window):** Schedule Trigger → Set `{mode:"full"}` (or no Set
     node) → Code node. Pulls all 6 entities and drains the billing-push queue. With no `mode`, the node
     defaults to full, so any pre-existing single-schedule wiring keeps working unchanged.

## Validated NetSuite ids (from `netsuite_validation.md`)
Mova: location `49`, class `237` (regular MOVA brand — dedicated `253` dropped 2026-07-22),
vendor `10872` (Mova Technologies (AU) **$AUD** — not the $USD `10504`), subsidiary `2` (MacGear AU).
Skriva: customer `10496`, vendor `10503`, location `2`, class `236`.
`inbound_shipments` read action validated 2026-06-30.

## Going to production
See **`production_cutover.md`** — verified prod ids, the full TBA/integration/role/token setup with
a per-hop smoke test and an error-signature table, the three decisions still open (3PL billing
customer, `CHARGE_ITEMS` remap, container-unload item), the expected first-sync numbers, and the
**cache purge that must happen before the first production sync** (most ingests upsert without
pruning, so sandbox receipts/fulfilments/shipments would be billed a second time).

**Secrets:** the n8n Code node reads the four TBA secrets + `SYNC_TOKEN` from **environment
variables** (`NS_CONSUMER_KEY`, `NS_CONSUMER_SECRET`, `NS_TOKEN_ID`, `NS_TOKEN_SECRET`,
`NS_ACCOUNT_ID`, `NS_RESTLET_SCRIPT`, `NS_RESTLET_DEPLOY`, `THREEPL_SYNC_TOKEN`), falling back to
inline constants when unset. A Code node body is stored and exported as plain text — unlike an n8n
credential it is **not** encrypted with `N8N_ENCRYPTION_KEY` — so live keys should not be pasted
inline. The node throws on startup if any secret is still `REPLACE_…`.
