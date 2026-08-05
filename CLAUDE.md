# 3PL Portal

A customer-facing visibility portal + billing automation for Macgear's new **3PL (third-party logistics) service**. Macgear receives, stores, and dispatches stock it does **not** own, on behalf of customers, and charges handling/storage fees. Everything runs through **NetSuite**.

## Status — ✅ LIVE on NetSuite **production** (2026-07-27)
The RESTlet is deployed in the production account, the n8n lanes are wired, and a **full sync has
run successfully**. All 6 visibility views are populated with real Mova 3PL data and verified
against NetSuite. Postgres schema is v1 (`db/01_schema.sql`) + `ensure_columns()` migrations.
The cutover record — verified prod ids, TBA/role setup, the cache purge, and the traps that were
actually hit — is `docs/production_cutover.md`.

**First production data (Mova, week of 20–26 Jul 2026):** 5 SKUs / 6,369 units / 533 pallets on
hand, 9 receipts (6,369 units) off 9 containers (`INBSHIP91`–`99`), 4 open PO lines. No dispatches yet.

**As at 2026-08-01 (re-read from production):** **36 inbound shipments — 22 received / 14 in
transit** (`INBSHIP91`–`102` + `INBSHIP106`–`129`; ids 103–105 are out of scope). A second batch
of 13 containers landed 28–31 Jul: receipts dated **31 Jul**, 7,109 units. Total on hand
**13,478 units / 1,112 pallets** across 12 SKUs (= 6,369 + 7,109 exactly; nothing dispatched, no
shrinkage). Storage at 1,112 pallets is **$5,004/week**.
⚠️ Three SKUs have `custitem_pallet_quantity` **NULL** — `52856` / `52857` / `52858`
(`20010100002916`, `20010100003083`, `20010100003085`), 8,000 units on order between them. They
hold no stock yet, but the moment they receive they contribute **0 pallets** to storage. Populate
the field in NetSuite before that batch lands.

**Billing write path — configured 2026-08-05, not yet fired in anger.** The two blockers in
`docs/production_cutover.md` §1 are resolved: the bill-to is **customer `11066` — Spacewalker
Technology Hong Kong Co., Limited** (`03431`), Mova's HK entity, currency **AUD**; and the
charge items are mapped to production ids in the app (Settings → **Charge items**), replacing
the sandbox `CHARGE_ITEMS` map that used to sit in the n8n node. Set `11066` on Mova in
`/admin/customers` and redeploy the RESTlet, then the first real push can go. Skriva remains
live as the reference customer.

## Billing roadmap (built 2026-08-01)
Shape: **whole Mon–Sun period → auto-generate the draft at period end → review → close → manual
push to NetSuite.** Nothing ever posts automatically.

| Piece | State |
|---|---|
| Manual push only ("Queue for NetSuite" → n8n → **draft** invoice, approved by a human in NS) | ✅ no auto-post exists |
| One run per period (`billing_run` unique on customer+period) | ✅ |
| Re-billing guard — a period at `ready_to_push`/`pushed`/`invoiced` can't be re-saved or re-queued | ✅ |
| **Week-aligned periods** — the UI submits `?week=<any date>` and snaps to that Mon–Sun week; a hand-edited `?from=/?to=` that isn't a whole week is **rejected with a message**, never silently snapped. A non-week POST is refused (`msg=bad-period`) | ✅ built |
| **Explicit period close** — `billing_run.locked_at` / `locked_by`, "Close period" on a draft. A closed run **can still be queued and pushed**; what it cannot do is be recomputed, by a re-save or by the scheduled generate | ✅ built |
| **Auto-generate the draft at period end** — `POST /admin/billing/generate` (token-authed), called at the END of the n8n full lane. Targets `week_bounds(today − 7d)` = the most recently completed week; idempotent (existing run → skipped, never recomputed); plants nothing when there's no billable activity; lands at `draft` | ✅ built |
| **Edit the draft before pushing** | ✅ built 2026-08-05 — **reverses the earlier "NetSuite-side only" decision**, which two weeks of live 3PL work made untenable. See *Draft editing* below. |
| **Charge items live in the app** (`charge_item`, Settings → Charge items) | ✅ built 2026-08-05 — retired the `CHARGE_ITEMS` constant in the n8n node |
| **Invoice edits sync back with a variance** | ✅ built 2026-08-05 — see *Invoice sync-back* below |

Statuses: `draft → ready_to_push → pushed → invoiced`, with `locked_at` an orthogonal freeze flag.

**Deleting a run** (`POST /c/{slug}/billing/delete/{run_id}`, **admin only**) removes it and its
lines from the portal at any status. It never touches NetSuite — an invoice already created
there survives. ⚠️ It also **un-blocks re-billing that week**: the guard keys on a run existing
for the period, so once deleted the period looks unbilled and can be pushed again, i.e. a
second invoice. That's what makes it useful for clearing sandbox leftovers and dangerous
afterwards, hence admin-only, a confirm that names the invoice, and a flash that says the
period is now re-billable. Every other billing action is internal-role and reversible; this one
is neither.

## Draft editing (built 2026-08-05)
Once a run is saved, the billing page shows **the run's own lines, not a fresh preview** — after
a push the two routinely differ, and a recomputed preview above the variance table was
misleading on the screen people reconcile invoices against. While it's an open draft the lines
are editable: change qty/rate, remove a line, or add an ad-hoc charge off the item catalogue.

The property the old "NetSuite-side only" rule protected — *the run is a faithful record of what
the rate card produced* — is now enforced structurally rather than by convention:
- **`computed_qty` / `computed_rate` / `computed_amount` are never overwritten by an edit.**
  `qty`/`rate`/`amount` are what gets invoiced; the computed trio is what the engine said. The
  difference shows as a per-line variance and in the "Rate card" column.
- **A removed COMPUTED line is kept at `origin='removed'`, amount 0** — struck through, with a
  Restore button — never deleted. A manual line *is* deleted, since nothing computed is being
  hidden. A charge that silently disappears is the exact fault that put 9 containers ($13,500)
  off an invoice for a fortnight.
- **A hand-edited draft cannot be silently recomputed.** `_recompute_blocked()` gains a third
  reason, `has-edits`; only an explicit "Recompute, discarding edits" (with a confirm) lifts it,
  and it never unlocks a closed or pushed period. The scheduled generate skips existing runs
  anyway.
- Editing is refused outright on a closed or pushed run — that's what "Close period" is for.

**Ad-hoc charges are deliberately NOT wired into the billing engine.** The rate card automates
five charges; the account carries ten items (kitting, packaging, freight, system fee, additional
labour, pick/pack). Nothing in the cache derives those, so there is nothing to automate — they
are entered per week by a human against the item they invoice against. Don't "finish the job" by
adding them to `billing.py`.

## Charge items (built 2026-08-05)
`charge_item` is the app's copy of Macgear's `3PL - *` NetSuite service items, managed at
**Settings → Charge items**. It replaced the `CHARGE_ITEMS` map hardcoded in the n8n Code node,
which held **sandbox ids `55070–55074` that do not exist in production** — a standing cutover
trap that made "go to production" mean "edit the workflow". Two reasons it had to move app-side:
n8n cannot supply a per-line item for an ad-hoc charge, and the mapping is a business decision
that belongs in a UI.

| charge_type | Item | id |
|---|---|---|
| `container_unload` | 3PL - container unload | `57082` |
| `putaway` | 3PL - Putaway Fee | `23560` |
| `storage` | 3PL - Storage | `23561` |
| `picking_so` **and** `picking_vrma` | 3PL - Picking | `23563` |
| `shipping` | 3PL - Freight | `23565` |

Ad-hoc only: Pick/Pack `36281`, Kitting `23562`, Packaging `23564`, System Fee `23566`,
Additional Labour `23567`. `picking_vrma` reaches `23563` through `service.CHARGE_TYPE_ALIASES`
rather than a duplicate row — picking bills at $1.00/unit whichever dispatch path was used, so
it's one invoice line; the portal splits the two because the split says which path was taken.

Lines are stamped with their item **at save time**, not at push time, so a run pushed months
later invoices against the item that was mapped when it was billed (same reasoning as
effective-dated rate cards). **A run with an unmapped line is refused at "Queue for NetSuite"**,
naming the charge — it used to fail inside n8n overnight as an undefined item id.

## Invoice sync-back (built 2026-08-05)
⚠️ **`transactionline` stores a customer invoice's revenue lines as CREDITS**, so `quantity`
and `netamount` come back **negative** while the header's `foreigntotal` is positive. Every
line rendered negative under a positive total, and `run_variance()` compared a positive run
against a negative invoice and called every line "changed". Two-layer fix, both of which
**negate, never `ABS()`** — a genuine credit line (a discount, a credited container) is stored
positive, so `ABS()` would flip it into a charge and silently inflate the invoice:
- The RESTlet selects `-quantity` / `-netamount` (same place the fulfilment ASSET-line sign is
  normalised — NetSuite storage conventions belong there, not in the app).
- `netsuite._orient_lines()` flips the whole line SET if it disagrees in direction with the
  header total. All-or-nothing on the set, so a legitimate credit keeps its opposite sign. It
  is idempotent, which means the app and RESTlet can be deployed in either order and a
  corrected feed is never double-flipped.

**An invoice's `trandate` is when it was RAISED, not the week it bills.** `INAU250127` bills
27 Jul–2 Aug and was **deliberately backdated to 31 Jul** to fall inside payment terms — so
never infer a period from `trandate`, in code or by eye. Both invoice views show **Period
billed**, resolved by `service._resolve_period()` in this order:
1. **`invoice.period_start/end` — assigned by hand in the portal.** PORTAL-OWNED columns: the
   sync never writes them, so an assignment survives every re-sync. Internal-only form on the
   invoice page; snaps any submitted date to its Mon–Sun week (billing is priced in whole
   weeks everywhere, so a ragged range could never line up with a run). Wins over a linked
   run — the only reason to set it on an invoice that has one is to correct it — and the page
   **flags the disagreement** rather than hiding it.
2. The `billing_run` that pushed it.
3. The invoice memo, which is why `createInvoice` stamps `3PL charges <from>–<to>`.

NetSuite has **no billed-period field**, so 1 is the only period an invoice raised by hand
there will ever have (`INAU249588` = 20–26 Jul, `INAU250127` = 27 Jul–2 Aug were both entered
this way). Invoices pushed from the portal get 2 and 3 for free. CSV export carries the period
as separate `Period from` / `Period to` columns.

Line edits made in NetSuite always reached the cache (`ingest_invoices` rebuilds lines every
full lane). What was missing was everything that made them *visible*:
- The RESTlet now returns the line's **item internal id**, plus `currency`,
  `foreignamountunpaid`, `duedate` and `lastmodifieddate`. `InvoiceLine.charge_type` is derived
  from the item through `charge_item` — **matching on item, never description**, because
  descriptions are free text and get edited too.
- `service.run_variance()` compares a pushed run to its invoice line by line and renders a panel
  on the billing page: changed / added-in-NetSuite / not-on-the-invoice, with a total delta.
  **The run's own lines are never overwritten by it** — NetSuite is truth for what was billed,
  the run stays the record of what Macgear asked for.
- **`pushed → invoiced` now actually happens.** Nothing set `invoiced` before except `seed.py`,
  so every real run sat at `pushed` forever. `_advance_pushed_runs()` moves it once the synced
  invoice leaves a pending-approval/rejected state.
- Invoices are **pruned** when NetSuite stops returning them (this also finally clears the stale
  sandbox invoice, `production_cutover.md` §2a — `invoice` was upsert-only and could not clear
  itself). ⚠️ **The prune is guarded on a non-empty pull and must stay that way.** A missing
  transaction permission returns an empty result set from a *successful* query, so an unguarded
  prune would wipe every invoice and detach every run from the invoice it created — one re-push
  away from double-billing. A run whose invoice vanishes gets `sync_note` and keeps its status;
  re-billing a week is a human decision, never a sync side effect.
- **Invoice-view filtering**: `customer.invoice_items_only` narrows the invoice read to invoices
  carrying a 3PL charge item. Needed because Mova bills to `11066`, an existing trading entity —
  without it, Spacewalker's ordinary product invoices would appear in Mova's customer portal.
  Leave it **off** for Skriva, whose 3PL invoices are $0 product invoices with no charge item on
  them at all; filtering there would empty the view.

Why `generate` runs last in the full lane: it must see all six reads land first, or it bills a
week whose receipts haven't arrived. It resolves the week from the app's own clock (`today − 7d`
lands in the previous week on **any** weekday), so a daily full lane just re-attempts and skips —
a failed Monday self-heals on Tuesday instead of losing the week.

**Under-billing is surfaced, never silent** (`BillingResult.warnings`, rendered on the preview and
echoed into the n8n log): receipts in the period with no container linked, containers marked
received in NetSuite that no receipt points at, and **no SOH snapshot inside the period** (which
otherwise drops the storage charge to nothing with no trace). A charge computing to zero must be
visible — that's the whole lesson of the container bug below.

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

## Validated NetSuite facts (2026-06-26, re-confirmed against **production** 2026-07-27)
**Production ids in use** (all read out of the live account, not assumed): account `840974`,
subsidiary `2` (MacGear AU), Mova class `237` @ location `49` (`AU2 – Melbourne Warehouse :
warehouse 3PL`), Mova vendor/supplier **`10872`** = "Mova Technologies (AU) **($AUD)**" — note
`10504` is the **$USD** entity and `10688` is NZ, easy to grab by mistake. Units-per-pallet field
is `custitem_pallet_quantity` (`custitem_pallet_layer_quantity` is a *different*, per-layer field).
Mova's **3PL billing customer record is `11066` — "Spacewalker Technology Hong Kong Co.,
Limited"** (customer number `03431`), decided 2026-08-05. Its currency is **AUD**, so the AUD
rate card invoices at face value; the sync pulls `currency` back on every invoice and the
billing page warns if a returned invoice isn't AUD, so a future change to that record can't
silently mis-bill. It is an existing trading entity, hence `invoice_items_only` (see above).
None of the four Mova customers (`10501` AU Online, `10502` NZ Online, `10567`/`10859` DOA
replacements) was a 3PL billing entity.

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

**⚠️ A missing transaction permission is indistinguishable from "no data yet."** NetSuite does not
error when the integration role lacks a transaction permission — it row-filters and returns an
**empty result set from a successful query**, so the sync logs a clean run that ingested 0 rows.
Hit for real on go-live: Item receipts was empty while SOH and Stock-on-order were perfect, with
nothing in the n8n error log. Cause was a missing **Transactions → Item Receipt** grant. Same
applies to subsidiary access. Discriminator: `"ingested": 0` **with no** error item = fix the role,
not the code. Per-view permission table in `docs/production_cutover.md` §7.

**Cosmetic columns must never be able to fail a read.** Two views lost their whole dataset (and the
charge derived from it) to an all-or-nothing lookup for a decorative column. Receipts' `po_tranid`
was a correlated subquery in the SELECT list; both it and the fulfilment source lookup are now
separate, id-scoped and `try/catch`'d, so a failure blanks one column instead of dropping every row.
⚠️ **But `ns_inbound_shipment` is no longer cosmetic** (2026-08-01) — the container-unload charge
now depends on it, so a blank is a $1,500-per-container under-bill. The `try/catch` stays (losing
the receipts would also lose putaway), and the billing preview **warns** on any receipt in the
period with no shipment linked. Don't remove that warning.

**⚠️ `inboundshipment` header dates are unusable for billing. Date the container charge off the
ITEM RECEIPT.** Verified against production 2026-07-31: **`actualdeliverydate` is NULL on all 36**
Mova 3PL shipments, so the RESTlet's `COALESCE(actualdeliverydate, lastmodifieddate)` fallback
always applies — and `lastmodifieddate` is the timestamp of whoever last *touched* the record, not
a delivery. All 36 clustered onto four bulk-edit timestamps; the 9 containers physically unloaded
**20 Jul** read as **30 Jul**. Consequence before the fix: the 20–26 Jul week billed **0**
containers ($0 instead of $13,500) and the next week billed **22** ($33,000), across a month
boundary. `expecteddeliverydate` is no better — NULL on `91`–`102` and a blanket `2026-07-28` on
`106`–`129`.
`billing.py` now counts containers via `item_receipt.ns_inbound_shipment`, dated by
`MIN(item_receipt.trandate)` per shipment across **all** history — so a container split across two
receipts in different weeks is charged once, in the earlier, and the charge is **idempotent**
(a trandate doesn't move when someone edits the record; a lastmodifieddate does).
`inbound_shipment.received_date` is now only a received/not-received **flag** (its presence, not
its value) plus a display date. Don't restore it as a billing trigger.
Prod `shipmentstatus` strings are exactly `received` (22) and `inTransit` (14) — no
`partiallyReceived`, no padding; the code's test matches.

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
| Container unload — 40ft loose stacked | $1,500 | per container (dated by its item receipt's `trandate` — **not** the shipment header, see above) |
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
> ⚠️ **Historical artifact — the real app has moved past it.** It's still the old teal theme and has
> none of the paged/windowed list views, the selectable overview chart, or the container/reference
> columns. Use `app/` as the reference for how the portal actually looks and behaves; the prototype
> is kept only as the original clickable pitch.

## NetSuite integration — n8n + RESTlet (app never calls NetSuite)
**The droplet app holds no NetSuite credentials and makes no NetSuite calls. No AI/MCP at runtime**
(MCP was dev-time validation only). All NS comms are server-to-server: **n8n signs TBA → RESTlet**
(`netsuite/3pl_restlet.js`), same pattern as the vendor-credit-claims app. See `docs/netsuite_integration.md`.
- **Reads:** n8n calls RESTlet (runs validated SuiteQL) → POSTs rows to token-authed `POST /admin/ingest`
  ({customer, entity, rows}); `app/netsuite.py` `ingest_*` upsert into the cache (invoices+lines, POs,
  receipts, fulfilments, stock_on_hand, inbound_shipments). NetSuite is source of truth — invoices
  (status/edits/payments) come from the sync.
- **Two sync lanes (mode-driven, same Code node):** FAST = `stock_on_hand` only every **15 min**
  (`mode:"soh"`, no writes) → portal SOH view is near-live ("● live · updated N min ago"). FULL = all 6
  entities + billing push, daily/weekly (`mode:"full"`/default). SOH ingest uses **replace semantics**
  (items not in the pull are zeroed; zero-qty hidden from the view) and stamps `synced_at`. Today's SOH
  row is overwritten in place; older days persist as daily history. **Storage billing = AVG daily pallets
  × weeks** (`billing.py`) — never sum every snapshot (would overcharge ~7× at this cadence).
- **Writes:** the full lane also POSTs `/admin/billing/generate` **after all six reads**, which
  drafts the previous Mon–Sun week per customer (idempotent; see *Billing roadmap*). It generates
  only — a fresh draft is deliberately not pushed in the same pass.
  "Queue for NetSuite" sets `billing_run.status='ready_to_push'` (no NS call). n8n polls
  `GET /admin/billing/pending`, creates the **draft** invoice via the RESTlet `create_invoice` action,
  then `POST /admin/billing/pushed` ({run_id, ns_invoice_id}) → status `pushed`. Next read-sync pulls the
  real invoice; the run links to it via `ns_invoice_id`. Statuses: draft→ready_to_push→pushed→invoiced.
- **Re-billing guard:** a period already queued/pushed/invoiced, or explicitly closed
  (`locked_at`), can't be re-saved or recomputed. Both reasons come from
  `main._recompute_blocked()` — one place, shared by the manual save and the scheduled generate.
- Customers drill the Invoices list → per-invoice charge-line detail (`/c/{slug}/invoice/{id}`, customer-scoped).
- Artifacts: `netsuite/3pl_restlet.js`, `netsuite/n8n_3pl_sync.js`. App needs only env `SYNC_TOKEN`.

## Notes
- Aaron (aaron@macgeargroup.com) is the owner. Mid warehouse relocation in Melbourne; in Bali 9–20 July 2026.
- NetSuite MCP is available in this environment (`mcp__claude_ai_NetSuite__authenticate`) for live data once ready.
