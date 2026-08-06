# NetSuite integration — live architecture

> **LIVE on the NetSuite production account since 2026-07-27.** Reads are running on both lanes.
> The billing write path is **configured** as of 2026-08-05 (bill-to `11066`, charge items mapped
> in the app) but the first automated push to production has not been fired yet. See the *Billing
> roadmap* in `CLAUDE.md`. Cutover record + verified ids: `production_cutover.md`.
>
> ⚠️ Both `netsuite/3pl_restlet.js` and `netsuite/n8n_3pl_sync.js` changed on 2026-08-05, and the
> RESTlet again on 2026-08-06 (invoice line signs + `t.memo`). Redeploy both when pulling those.

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
  - `GET  /admin/sync-config` — the customer list to loop over (+ `location_scoped`,
    `invoice_items_only`) and `charge_item_ids`, the 3PL service item ids the invoice read uses to
    narrow itself. Managed in the admin console, so neither is hardcoded in the node.
  - `GET  /admin/billing/pending` — billing runs queued (`ready_to_push`) with customer ns ids and
    lines. **Each line carries its own `ns_item_id`** (resolved app-side from the charge-item
    catalogue), plus `origin` (`computed`/`edited`/`manual`); lines a human removed are omitted.
    The app refuses to queue a run whose line has no item, so n8n should never see a null one.
  - `POST /admin/billing/pushed` — `{run_id, ns_invoice_id}` → marks the run pushed and links the invoice.
  - `POST /admin/billing/generate` — drafts the previous Mon–Sun week per customer; idempotent.
    Called at the END of the full lane, after all six reads land.
- **Portal-internal routes** (session-authed, not for n8n), added with the paged list views:
  `GET /c/{slug}/{view}/rows` returns bare `<tr>`s for the Load more button;
  `GET /c/{slug}/{view}/export.csv` streams the whole current selection. Both enforce the same
  customer scoping as the page.

## Invoice lifecycle (why reads are authoritative)
Draft generated → optionally **edited in the portal** → closed → queued → n8n creates a **draft**
invoice in NetSuite → a person approves/edits it there → status moves Open→Paid/Overdue, credits
may be raised. All of that lives in NetSuite, so the portal's Invoices view is **synced from
NetSuite** (read action `invoices`, incl. lines). The app stores only `billing_run.ns_invoice_id`
to link a run to its invoice — never a frozen copy. A period already queued/pushed/invoiced is
locked against re-billing, as is a draft that has been hand-edited (unless the operator explicitly
discards the edits).

Since 2026-08-05 the loop closes properly:
- **Edits made in NetSuite come back with a variance.** `service.run_variance()` matches invoice
  lines to billing lines on `ns_item_id` and shows changed / added-in-NetSuite / not-on-the-invoice
  with a total delta. The run's own lines are **never** overwritten — NetSuite is truth for what was
  billed, the run stays the record of what the rate card produced.
- **`pushed` → `invoiced` now happens** (`netsuite._advance_pushed_runs`), once the synced invoice
  leaves a pending-approval/rejected status. Before this, nothing but `seed.py` ever wrote
  `invoiced`, so every real run stalled at `pushed`.
- **A vanished invoice is flagged, not acted on.** If NetSuite stops returning an invoice a run
  created, the run gets `sync_note` and keeps its status. Re-billing the week is a human decision.

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
   (account id, keys/token, script/deploy ids, `APP_BASE`, `SYNC_TOKEN`). Customer NetSuite ids and
   the charge-item mapping are **not** in the node — both come from the app (`/admin/sync-config`
   and the per-line `ns_item_id` on `/admin/billing/pending`), so adding a customer or remapping an
   item is an admin-console edit, never a workflow edit. The `CHARGE_ITEMS` constant that used to
   live here was removed 2026-08-05; don't reintroduce it (ad-hoc invoice lines pick their item per
   line, which a charge_type-keyed constant can't express).
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
a per-hop smoke test and an error-signature table, the expected first-sync numbers, and the
**cache purge that must happen before the first production sync** (most ingests upsert without
pruning, so sandbox receipts/fulfilments/shipments would be billed a second time).
The decisions that were open there are now closed (2026-08-05): the 3PL billing customer is
`11066`, the charge items are mapped in the app, and the container-unload item exists (`57082`).
`invoice` now prunes on a non-empty pull, so the stale sandbox invoice clears itself.

**Secrets:** the n8n Code node reads the four TBA secrets + `SYNC_TOKEN` from **environment
variables** (`NS_CONSUMER_KEY`, `NS_CONSUMER_SECRET`, `NS_TOKEN_ID`, `NS_TOKEN_SECRET`,
`NS_ACCOUNT_ID`, `NS_RESTLET_SCRIPT`, `NS_RESTLET_DEPLOY`, `THREEPL_SYNC_TOKEN`), falling back to
inline constants when unset. A Code node body is stored and exported as plain text — unlike an n8n
credential it is **not** encrypted with `N8N_ENCRYPTION_KEY` — so live keys should not be pasted
inline. The node throws on startup if any secret is still `REPLACE_…`.
