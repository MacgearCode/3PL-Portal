# 3PL Portal — Data Model & NetSuite Sync Spec (v1)

> **Status: v1 — validated and LIVE on NetSuite production since 2026-07-27.**
> Every SuiteQL query behind this model has been run against live data; the authoritative,
> validated queries and field names live in **`docs/netsuite_validation.md`** — treat that as the
> source of truth over any prose here. The shipped schema is `db/01_schema.sql` plus the
> `ensure_columns()` migrations in `app/db.py` (columns added after v1: `stock_on_hand.synced_at`,
> `item_receipt.po_tranid`, `po_line.ns_inbound_shipment`, `inbound_shipment.expected_date`,
> `inbound_shipment.container_no`, `customer.location_scoped`, and the `app_user` reset columns).
>
> Two model facts that were open questions in v0 and are now settled:
> - **Stock isolation is per-customer**, via `customer.location_scoped` — brand class alone for
>   Skriva, class **AND** the 3PL location for Mova (whose brand also covers Macgear-owned stock).
> - **Dates come from the PO line** (`expectedreceiptdate`); the inbound shipment's
>   `expecteddeliverydate` is NULL on every production shipment, so it is only an override, never
>   the primary source.

## Design principles

1. **NetSuite is the system of record.** Postgres is a **read cache** that powers the portal and feeds
   the billing run. We never write operational stock data back to NetSuite from the portal — the only
   write-back is **draft invoices** (phase 2), created via REST against the customer record.
2. **Multi-tenant from day one.** Every fact row is keyed by `customer_id`. Mova and Skriva are just two
   rows in `customer`. New 3PL customers = new `customer` + `rate_card` rows, no schema change.
3. **Mirror, don't transform.** Cache tables mirror the shape of the NetSuite transactions (PO, inbound
   shipment, item receipt, fulfilment, invoice) plus a stock-on-hand snapshot. The 6 portal views and the
   5 billing charges are all derived from these.
4. **Idempotent sync.** Every sync upserts on the NetSuite internal id. Re-running a sync never duplicates.

## The two customer shapes (why the model must flex)

| | **Mova** (target) | **Skriva** (reference, live now) |
|---|---|---|
| Stock location | dedicated `3PL Warehouse` (Melbourne) | main `Auckland` warehouse |
| Brand tag | `MOVA` (class `237`, the regular brand — no dedicated 3PL brand) | `SKRIVA STYLUS` (class `236`) |
| Item example | regular SKUs, e.g. `010201AA000437` | `S-STYCASE-WHITE` |
| Separate 3PL location? | **yes** | **no** ($0 on main warehouse) |

The cache must isolate a customer's stock by **(location AND/OR brand)**, because Skriva proves you can't
rely on a dedicated location alone. So `customer` carries **both** an optional `netsuite_location_id` and a
`brand_tag`, and the sync filter for each customer is `location = X` **and/or** `brand = Y` — whichever
uniquely identifies that customer's 3PL stock. **Settled:** the flag is `customer.location_scoped`
— `True` for Mova (class `237` **and** location `49`), `False` for Skriva (class `236` only, since
its stock legitimately spans Auckland + Christchurch). The RESTlet's `locClause()` applies the
location filter only when the flag is set, and the flag travels via `/admin/sync-config`, so
onboarding a customer needs no n8n edit.

## The 6 visibility views → source mapping

| # | Portal view | Cache table(s) | NetSuite source | Filter |
|---|---|---|---|---|
| 1 | Stock on order | `purchase_order` + `po_line` | open Purchase Orders | supplier = customer's supplier, location = 3PL loc, status = open |
| 2 | Item receipts | `item_receipt` + `item_receipt_line` | Item Receipts | location = 3PL loc, brand = brand_tag |
| 3 | Stock on hand | `stock_on_hand` (snapshot) | inventory balance | location = 3PL loc, brand = brand_tag |
| 4 | Item fulfilments | `item_fulfilment` + `..._line` | Item Fulfilments | from SO on customer **OR** from VRMA on supplier |
| 5 | Invoices | `invoice` + `invoice_line` | Invoices | customer = customer record |
| 6 | Rate card | `rate_card` + `rate_card_line` | (config, not NetSuite) | per customer, effective-dated |

## The 5 billing charges → source mapping

| Charge | Rate (Mova) | Basis | Derived from |
|---|---|---|---|
| Container unload (40ft loose) | $1,500 | per container | count of containers whose **earliest `item_receipt.trandate`** falls in the period, reached via `item_receipt.ns_inbound_shipment` (see below) |
| Putaway | $1.00 | per unit | sum of `item_receipt_line.qty` in period |
| Storage | $4.50 | per pallet/week | **avg daily pallets** over the period × weeks (`ceil(units_on_hand / units_per_pallet)` totalled per snapshot day, averaged) |
| Picking — SO | $1.00 | per unit | sum of `item_fulfilment_line.qty` where source = SO |
| Picking — VRMA | $1.00 | per unit | sum of `item_fulfilment_line.qty` where source = VRMA |
| Shipping | per shipping rate card | — | out of scope for v1 auto-billing |

> **Storage is the tricky one.** "Per pallet per week" needs a defensible weekly pallet figure.
> **Current decision (2026-06-27):** now that SOH refreshes every ~15 min and persists one row per
> day, bill the **average of the daily pallet totals** across the billing week × the number of weeks
> the period spans (`billing.py`). This is the "avg daily pallets" model — more accurate than a single
> weekly reading and robust to how often we snapshot. With only one snapshot in the period it degrades
> to "that reading held all week" (the original v0 weekly-snapshot behaviour). **Critical:** never *sum*
> every snapshot — at daily/intraday cadence that overcharges ~7×.
> **Corollary (2026-08-01):** if the period contains *no* snapshot at all — any week before the
> sync started — storage computes to **$0** with nothing to show for it. `billing.py` now raises a
> warning in that case rather than letting the charge vanish.

> **Container unload is dated by the receipt, not the shipment (2026-08-01).** The obvious source —
> `inbound_shipment.received_date` — is unusable. In production `actualdeliverydate` is NULL on all
> 36 Mova shipments, so the RESTlet falls back to `lastmodifieddate`, which is whenever someone last
> *touched* the record: the 9 containers unloaded 20 Jul carried 30 Jul, so the 20–26 Jul week billed
> 0 containers and the next billed 22. The item receipt's `trandate` is the physical unload and does
> not move when a record is edited, so the charge is **idempotent**.
> Mechanics: `MIN(item_receipt.trandate)` grouped by `item_receipt.ns_inbound_shipment` over **all**
> history, then count the shipments whose first-receipt date lands in the period. Grouping over all
> history (not just the period) is what makes a container split across two receipts in different
> weeks bill **once, in the earlier week** — never twice.
> Cost of the approach: it makes `ns_inbound_shipment` billing-critical when the RESTlet treats it
> as a best-effort column. That is covered by warnings, not by silence — see below.

### Under-billing must be visible (`BillingResult.warnings`)

A charge that computes to zero looks identical to "no activity". Three cases are therefore called
out on the billing preview and echoed into the n8n log by `/admin/billing/generate`:

| Warning | Means |
|---|---|
| receipts in the period with no container linked | the RESTlet's best-effort shipment lookup blanked; $1,500/container at risk |
| containers marked received in NetSuite with no item receipt | not billable in **any** period until a receipt exists |
| no SOH snapshot inside the period | storage silently uncharged for the week |

## Dispatch procedure (how stock leaves the 3PL)

Two NetSuite paths move stock out, and they record **different economic events** — pick by the
reality, not by convenience (billing is identical either way: picking is $1.00/unit for both).

**Decision rule: is Macgear taking ownership of (paying for) the stock?**

| Answer | Procedure | Effect |
|---|---|---|
| **Yes — Macgear buys it (DEFAULT / majority case)** | **VRMA on the vendor (customer-as-supplier) account**, then a **normal-price PO** into the regular warehouse | $0 VRMA clears the 3PL receipt and removes it from the customer's held inventory; the buy-in PO brings it onto Macgear's books as owned stock at cost. Fulfilment source = **VRMA**. |
| No — just dispatching the customer's goods on their behalf | **$0 Sales Order on the customer account** | Pure logistics, ownership stays with the customer. Fulfilment source = **SO**. |

**Failure mode to avoid:** using an SO when Macgear is actually purchasing ships the stock off the
customer's 3PL holding but brings **nothing onto Macgear's books** → phantom inventory + unrecognised
COGS. When Macgear is buying, always use **VRMA + buy-in PO**. The VRMA fulfilment is captured off the
inventory **ASSET line** (single negative line, no positive counterpart — see `netsuite_validation.md`);
the old "sum positives only" rule dropped VRMAs entirely.

## Sync design

- **Mechanism:** NetSuite REST/SuiteQL via Token-Based Auth (TBA), pulled on a schedule into Postgres.
- **Cadence (current):** two n8n lanes off the same Code node (`netsuite/n8n_3pl_sync.js`, mode-driven).
  - **Fast lane — `stock_on_hand` only, every 15 min** (`mode:"soh"`, no billing writes): keeps the portal's
    SOH view near-live. Today's row is overwritten in place; the view shows a "● live · updated N min ago" stamp.
  - **Full lane — all 6 entities + draft-invoice writes, daily + the weekly billing window** (`mode:"full"`):
    transactional tables (PO, receipts, fulfilments, invoices, inbound shipments) and the billing push.
    Daily SOH rows accumulate as history that the storage charge averages.
- **Runner:** n8n scheduled workflow on the droplet (same pattern as the birthday notifier), calling the
  app's sync endpoints, **or** an in-app APScheduler job. Decide at scaffold time.
- **Watermark:** each sync stores `last_synced_at` + last seen `lastmodifieddate` in `sync_log`; incremental
  pulls use `WHERE lastmodifieddate > watermark` where the record type supports it; full refresh for SOH snapshot.
- **Idempotency:** upsert on `netsuite_id`. Lines replaced wholesale per parent transaction on each sync.

## Open questions to resolve against live NetSuite

1. Exact record/field names in SuiteQL for: inbound shipment received status & date, item receipt → brand,
   inventory balance by location+brand, fulfilment source (SO vs VRMA) discriminator.
2. How "brand" is stored on the item (custom field id? `class`/`custitem_*`?) — drives the brand filter.
3. Skriva's actual brand tag + whether its stock is isolable without a dedicated location.
4. Units-per-pallet field id on the item record.
5. Container = one inbound shipment? **Confirmed 1:1 in production 2026-07-31** — each of the 22
   received shipments has exactly one item receipt (verified through `previoustransactionlinelink`
   on the shared PO line). The charge no longer assumes it, though: it counts distinct shipments
   off their receipts, so a 2-receipt container is still one charge.
