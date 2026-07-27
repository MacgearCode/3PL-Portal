# 3PL Portal

A customer-facing visibility portal + billing automation for Macgear's new **3PL (third-party logistics) service**. Macgear receives, stores, and dispatches stock it does **not** own, on behalf of customers, and charges handling/storage fees. Everything runs through **NetSuite**.

## Status
Real app scaffolded and running locally (`./run.ps1`). NetSuite data access **validated** against
live Skriva data (`docs/netsuite_validation.md`) — all 6 views + 5 billing charges proven in SuiteQL.
Postgres schema is v1 (`db/01_schema.sql`). Billing engine implemented & verified (June demo run =
$16,765, math checks out). NetSuite integration refactored to **n8n + RESTlet** (app never calls NS).
**Only remaining go-live step:** deploy the RESTlet + n8n node and set creds/`SYNC_TOKEN` (see
`docs/netsuite_integration.md`) — all app-side code is done. First customer (Mova) stock expected **end of July 2026**.

## Build (the real app, in `app/`)
FastAPI + SQLAlchemy, `DATABASE_URL` (SQLite local / Postgres droplet). `app/main.py` (auth, customer
switcher, 6 views, billing run, admin console, token `/admin/ingest` + `/admin/billing/*`), `models.py`,
`service.py` (read views), `billing.py` (the 5 charges), `netsuite.py` (ingest_* upserts; app never calls NS),
`perms.py` (roles + view permissions), `security.py` (pbkdf2 + signed cookie), `seed.py`.
Deploy pattern = vendor-credit-claims app (Docker on n8n droplet, weekly n8n sync job).

## UI / brand (re-themed 2026-07-26)
**Macgear palette, white app + optional dark mode.** Blue `#25A9E1` = accent, grey `#58585A` ramp = ink
and chrome. Everything is driven off CSS custom properties in `app/static/style.css` (`:root` +
`[data-theme="dark"]`) — no hardcoded colours anywhere in the CSS or templates, so both themes come free.
Two blues by necessity: core `#25A9E1` is 2.4:1 on white and can't carry text, so it's accent-only
(`--brand`) and `--brand-strong` (blue-700 `#16739C`) carries links + filled buttons; `--on-brand` is the
text colour on a filled button and flips to near-black in dark mode where the button goes bright.
Dark mode: `data-theme` on `<html>`, no-flash inline script in `base.html`, topbar toggle, localStorage
with OS-preference fallback. The sidebar and admin header are now **white in light mode** (they used to be
hardcoded dark teal in both). `prototype/portal.html` was deliberately **not** re-themed — it's still teal.
Responsive: off-canvas nav drawer ≤860px; chart's segmented control drops to its own line ≤560px.

**Container number** — `inboundshipment.externaldocumentnumber` (e.g. `CSNU8516117`), stored as
`inbound_shipment.container_no` and shown on **Stock on order** and **Item receipts** (both also
searchable/exportable by it). Distinct from `container_type`, which has no NetSuite field and is
always null. Receipts reach it by outer-joining the shipment on `ns_inbound_shipment` inside the
existing query, so the view stays at 2 queries. Needs `ensure_columns()` (verified against a
pre-existing DB) + a RESTlet redeploy.

**Paged list views (2026-07-27)** — Item receipts / Fulfilments / Invoices grow without bound
(~470 receipts a year for Mova alone), so they're **date-windowed + paged**: default **last 30
days**, selector `14d / 30d / 90d / This period / All`, **Load more** appends the next 100 rows via
`GET /c/{slug}/{view}/rows` (fetch + append; the button is a real link with a bigger `n=`, so it
degrades to a full page load without JS). Stock on hand / Stock on order are naturally bounded
(latest snapshot, open POs) and left alone.
Three things are deliberately **server-side, covering ALL history, not the rendered page** — a
partial answer on views people reconcile invoices against is worse than a slow one:
- **Search** (`?q=`) runs in SQL and *ignores the date window* (the UI says "across all history").
  The old client-side `filterTable()` now only serves the small views, where every row is present.
- **Footer totals** come from SQL aggregates (`total` / `qty_total` / `docs`), not `rows|sum`.
- **Export CSV** streams the whole selection in 1,000-row chunks — no row cap to truncate at.

**Query performance** — line collections are `lazy="select"`, so `for r in rows: for l in r.lines`
was an N+1. Measured at 2 years of Mova's cadence (936 receipts / 1,872 lines): receipts view
937 queries / 425 ms → **2 queries / 8 ms**, overview 1,514 / 582 ms → **67 / 40 ms**, HTML
531 KB → 38 KB. Fixes: paged views query lines joined to their parent; `selectinload()` in
`billing.py` (called 5× per overview) and `stock_on_order()`; `received_per_week()` sums in SQL;
`overview()` uses `recent_receipts/recent_fulfilments` with a real `LIMIT` instead of loading all
history and slicing `[:2]`/`[:3]`. **This matters more in prod than the numbers suggest** — those
were SQLite (in-process); Postgres pays a network round trip per query.

**Overview chart** — one card, three selectable series (segmented pills, default = Billing, choice
remembered in localStorage): *Received* (units received per week, last 4), *Incoming* (outstanding
on-order units bucketed by expected arrival, next 4 — forecast bars are visually distinct; anything that
can't sit in a bar is named in the note as **overdue / due later / no expected date**, never silently
dropped, so the bars always reconcile with the "units on order" KPI), *Billing* (charges per week, last 4).
**ETA source:** `tl.expectedreceiptdate` on the PO line (the RESTlet omitted it until 2026-07-26 — that's
why Expected receipt was blank in prod and Incoming was empty); an inbound shipment's
`expecteddeliverydate` overrides it at read time when the line is on a container.
Still no chart library — server-rendered divs with inline heights; `chartTab()` just swaps a class.
All three series and the topbar billing week anchor to **`date.today()`** (they used to anchor to the
cache's latest activity date, which a forward-looking series can't share). Consequence: `app/seed.py`
demo dates are now offsets from the current Monday, so the demo never ages out.

## Auth, roles & admin (built)
Per-user login (email + pbkdf2 password, signed-cookie session). Table `app_user`. **Roles:** `admin`
(full + user/rate-card management), `internal` (all customers + billing run, no admin), `customer`
(locked to one `customer_id`, visibility views only — no billing run). **Per-user view permissions:**
default by role, overridable per user (`app_user.allowed_views` JSON; NULL = role default; see `perms.py`).
**Admin console** (`/admin/users`, `/admin/customers`, admin-only): create/edit/deactivate users (assign
role + customer + visible views), and edit per-customer rate cards — a rate change
writes a NEW effective-dated `RateCard` so past billing runs reprice correctly.
**Password reset / set-password:** public **Forgot password** flow (`GET/POST /forgot`, `GET/POST /reset`)
and an admin **"Get set-password link"** button — admins no longer type passwords. New users are created
with an empty `password_hash` (login blocked) and given a link to set their own. Tokens are single-use:
only `sha256(token)` + expiry are stored on `app_user` (`reset_token_hash`, `reset_expires_at`, ~45 min).
On create / on button press the admin console **displays the link for the admin to copy** and send however
they like (Teams, normal email) — no external dependency. Automatic email is optional/best-effort: if
`N8N_RESET_WEBHOOK_URL` is set the link is also POSTed to an n8n webhook (`app/notify.py`) that mails it;
unset = link only shown in the UI + logged to console. `/forgot` never reveals whether an address exists.
(n8n email delivery was deferred — the copy-the-link UI is the working path; wire the webhook later if wanted.)
Seeded logins (dev — CHANGE): admin@macgeargroup.com/admin123,
ops@macgeargroup.com/internal123, viewer@mova.com/mova123. Auth is always on now (no shared-password mode).

## Validated NetSuite facts (2026-06-26)
Brand = NetSuite **classification** (store class id, not text). Mova uses its **regular MOVA brand** —
class `237` (MOVA) @ location `49` (warehouse 3PL); Skriva class `236` @ location `2` (Auckland).
**Model change (2026-07-22):** dropped the dedicated `3PL - Mova` brand (`253`) and dedicated 3PL SKUs —
Mova stock is tagged with its normal MOVA brand (`237`) + regular SKUs, same as Skriva.
**Stock isolation is now per-customer, via `customer.location_scoped`:**
- `location_scoped=True` (Mova): the brand class *also* covers Macgear-owned stock, so every NetSuite
  read filters by **class AND the dedicated 3PL location** (`237` + loc `49`). Without the location
  filter, all MOVA-branded receipts/fulfilments/POs across every warehouse leak into the portal
  (and would massively over-bill putaway). This is the bug we hit right after the 253→237 switch.
- `location_scoped=False` (Skriva): the brand class is 3PL-exclusive and stock spans locations
  (Auckland + Christchurch), so reads scope by **class only** — a location filter would drop rows.
The RESTlet's `locClause(p, alias)` applies the location filter iff `location_scoped`; the flag rides
in the customer record via `/admin/sync-config` (n8n spreads it into RESTlet params — **no n8n edit**).
**Deploy:** (1) in `/admin/customers` set Mova's Brand class id `237`, Brand label `MOVA`, and **tick
"Isolate 3PL stock to the location above"**; (2) redeploy `netsuite/3pl_restlet.js` in NetSuite. Skriva customer `10496`, vendor
`10503`, item `S-STYCASE-WHITE`=`50101`. Picking source SO-vs-VRMA = fulfilment `entity` (customer vs
vendor); `createdfrom` is NOT selectable in SuiteQL. Open-PO test = `quantityshiprecv < quantity`.
Fulfilments: count units off the **ASSET line, ABS(qty)** (SO emits +COGS/−ASSET pair, VRMA emits a lone −ASSET line; old "sum positives" dropped every VRMA — corrected 2026-06-27). Skriva invoices are $0 product invoices, so the
**service-charge invoice is greenfield**. `inboundshipment` table exists. REST metadata catalog is 403
(permission); discover fields via `SELECT *`. Another 3PL customer already live (ClassVR) — multi-tenant confirmed.

## The business model
- Macgear does **not** buy or own the stock — it transacts it and charges a fee for receiving, storing, dispatching.
- Stock for the flagship customer (Mova) lives in a dedicated **3PL Warehouse** location (Melbourne); items keep their **regular `MOVA` brand** (class `237`) and **regular SKUs** — no dedicated 3PL brand/SKU.
- NetSuite setup: new `3PL warehouse` location; customer record (for invoicing + $0 dispatch sales orders); supplier record (for $0 POs to receive stock); items are the normal MOVA-branded items with units-per-pallet populated. What makes stock "3PL" is the **location** (49), not a special brand.

## Processes (mirror these in any data model)
- **Receiving:** $0 PO on supplier account against 3PL location → inbound shipment per container → receipt on arrival.
- **Storage:** stock on hand in 3PL location, brand `MOVA`. Charged per pallet per week. Pallets = `units on hand ÷ units per pallet`.
- **Dispatching:** two paths — see **Dispatch procedure** below for which to use.
- **Billing (weekly, against customer record):** the 5 charge sources below.

## Dispatch procedure (how stock leaves the 3PL)
**Decision rule — is Macgear taking ownership of (paying for) the stock?**
- **Yes → standard/default path: VRMA on the vendor (customer-as-supplier) account, then a normal-price PO into the regular warehouse.** The $0 VRMA clears the 3PL receipt and removes it from the customer's held inventory; the buy-in PO brings it onto Macgear's books as owned stock at cost. This is the **majority case**.
- **No (just dispatching the customer's goods on their behalf) → $0 Sales Order on the customer account.** Pure logistics; ownership never transfers to Macgear.

**Why the default matters / failure mode:** using an SO when Macgear is actually buying ships the stock off the customer's 3PL holding but brings nothing onto Macgear's books → phantom inventory + unrecognised COGS. So when in doubt and Macgear is purchasing, use VRMA+PO. Billing is **neutral** to the choice — picking is $1.00/unit for both SO and VRMA (see rate card) — so always choose by the accounting reality, never by what's billable. Both paths show in the portal Fulfilments view (tagged SO vs VRMA) and both deduct SOH.

## Rate card (Mova)
| Charge | Rate | Basis |
|---|---|---|
| Container unload — 40ft loose stacked | $1,500 | per container (inbound shipments received) |
| Putaway | $1.00 | per unit (item receipts vs 3PL loc, brand MOVA) |
| Storage | $4.50 | per pallet / week (units on hand ÷ units/pallet) |
| Picking | $1.00 | per unit (item fulfilments — SO **and** VRMA) |
| Shipping | — | per agreed shipping rate card |

## What Mova needs to see (the 6 visibility views)
Stock on order (open POs) · Item receipts · Stock on hand · Item fulfilments (SO + VRMA) · Invoices · Rate card.

## Priorities (per brief)
1. **Visibility portal for the customer** — the priority.
2. **Automate the billing** — replace manual weekly saved searches with a draft-invoice run.
3. **Multi-tenant** — more 3PL customers are lined up; not a one-off.

## Existing reference customer
**Skriva** (NZ subsidiary) — same model at tiny scale, live in **prod + sandbox** (good for first NetSuite wiring/testing). Item `S-STYCASE-WHITE`. Difference: all transacted at $0 on the main **Auckland** warehouse, no separate 3PL location.

## Recommended approach (decided so far)
- **Build an external web app**, not an in-NetSuite Suitelet or raw Customer Center. Reason: multiple customers coming = it's a small product needing real UX + branding.
- **Fits existing Macgear stack:** FastAPI + Postgres on the n8n droplet (same pattern as the promos / vendor-credit-claims app), weekly billing job in n8n (same as birthday notifier).
- **NetSuite connection:** read via REST / **SuiteQL** (Token-Based Auth) on a schedule into a Postgres cache (the 6 views are the planned saved searches as SuiteQL). Billing automation writes **draft invoices** to the customer record via REST for approval.
- **Phasing:** (1) read-only visibility views → (2) weekly draft-invoice automation → (3) multi-tenant onboarding (Skriva + next customers, per-customer rate cards).

## Prototype
`prototype/portal.html` — self-contained clickable SPA, dummy data, all 6 views + Overview dashboard + a "Billing run" view demonstrating the automation. Customer switcher toggles Mova / Skriva to show multi-tenancy. Published as a claude.ai Artifact. To iterate: edit the file and re-publish to the same URL.

## NetSuite integration — n8n + RESTlet (app never calls NetSuite)
**The droplet app holds no NetSuite credentials and makes no NetSuite calls. No AI/MCP at runtime**
(MCP was dev-time validation only). All NS comms are server-to-server: **n8n signs TBA → RESTlet**
(`netsuite/3pl_restlet.js`), same pattern as the vendor-credit-claims app. See `docs/netsuite_integration.md`.
- **Reads:** n8n calls RESTlet (runs validated SuiteQL) → POSTs rows to token-authed `POST /admin/ingest`
  ({customer, entity, rows}); `app/netsuite.py` `ingest_*` upsert into the cache (invoices+lines, POs,
  receipts, fulfilments, stock_on_hand; inbound_shipments TODO). NetSuite is source of truth — invoices
  (status/edits/payments) come from the sync.
- **Two sync lanes (mode-driven, same Code node):** FAST = `stock_on_hand` only every **15 min**
  (`mode:"soh"`, no writes) → portal SOH view is near-live ("● live · updated N min ago"). FULL = all 6
  entities + billing push, daily/weekly (`mode:"full"`/default). SOH ingest uses **replace semantics**
  (items not in the pull are zeroed; zero-qty hidden from the view) and stamps `synced_at`. Today's SOH
  row is overwritten in place; older days persist as daily history. **Storage billing = AVG daily pallets
  × weeks** (`billing.py`) — never sum every snapshot (would overcharge ~7× at this cadence).
- **Writes:** "Queue for NetSuite" sets `billing_run.status='ready_to_push'` (no NS call). n8n polls
  `GET /admin/billing/pending`, creates the **draft** invoice via the RESTlet `create_invoice` action,
  then `POST /admin/billing/pushed` ({run_id, ns_invoice_id}) → status `pushed`. Next read-sync pulls the
  real invoice; the run links to it via `ns_invoice_id`. Statuses: draft→ready_to_push→pushed→invoiced.
- **Re-billing guard:** a period already queued/pushed/invoiced can't be re-saved or re-queued.
- Customers drill the Invoices list → per-invoice charge-line detail (`/c/{slug}/invoice/{id}`, customer-scoped).
- Artifacts: `netsuite/3pl_restlet.js`, `netsuite/n8n_3pl_sync.js`. App needs only env `SYNC_TOKEN`.

## Notes
- Aaron (aaron@macgeargroup.com) is the owner. Mid warehouse relocation in Melbourne; in Bali 9–20 July 2026.
- NetSuite MCP is available in this environment (`mcp__claude_ai_NetSuite__authenticate`) for live data once ready.
