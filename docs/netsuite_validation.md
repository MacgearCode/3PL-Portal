# NetSuite Validation — proven against live data (2026-06-26)

All queries below were **run successfully against the live NetSuite account** via SuiteQL (Skriva
reference customer). This is the de-risked basis for the sync layer. Mova-specific ids are marked TODO
until Mova items/transactions exist (~end Jul 2026).

## Access notes
- `ns_runCustomSuiteQL` works. The **metadata catalog endpoint is permission-blocked** (HTTP 403,
  "REST Web Services" feature) — discover fields with `SELECT *` / `SELECT` probes instead.
- `BUILTIN.DF(col)` resolves an internal id to its display label — use freely in SELECT.
- **`createdfrom` is NOT selectable** in SuiteQL here (throws "unexpected SuiteScript error"). Do not use it.
- **Container number = `inboundshipment.externaldocumentnumber`.** Validated production
  2026-07-27: populated on all 12 live Mova shipments with ISO container ids (`CSNU8516117`,
  `OOCU7885472`, `COTU4966527`…). Not to be confused with `container_type` ("40ft loose stacked"),
  which has **no** native field on `inboundshipment` and is always null. Surfaced on both Stock on
  order (via the PO line's shipment) and Item receipts (via `ItemReceipt.ns_inbound_shipment`).
- **Fulfilment → source document (the portal's "Reference" column).** `createdfrom` is not
  selectable, so walk `previoustransactionlinelink` (fulfilment = `nextdoc`). Validated production
  2026-07-27: **27/27 Skriva fulfilments** resolved to their sales order (`SalesOrd` → `SO328496`…),
  and vendor-entity fulfilments resolve to `VendAuth` → `VRMA000327`…
  ```sql
  SELECT ptll.nextdoc ff, MIN(src.tranid) ref
  FROM previoustransactionlinelink ptll
  JOIN transaction src ON src.id = ptll.previousdoc
  WHERE ptll.nextdoc IN (:fulfilment_ids)
  GROUP BY ptll.nextdoc
  ```
  **Do not filter on `src.type`** — the previous doc is the source order whichever type it is, and
  guessing wrong (e.g. `RtnAuth` instead of `VendAuth`) silently returns nothing for every VRMA,
  which is Mova's default dispatch path. `GROUP BY` + `MIN()` is required: there is one link row
  per fulfilment line.
- **There is no receipt → inbound-shipment field.** `transaction.inboundshipment` does not exist
  ("Unknown identifier"), and there's no direct link. Reach it through the source PO
  (validated production 2026-07-27, all 9 Mova 3PL receipts resolved 1:1 to `INBSHIP91`–`99`):
  ```sql
  SELECT ptll.nextdoc receipt, MIN(po.tranid) po_tranid, MIN(s.shipmentnumber) shipment
  FROM previoustransactionlinelink ptll
  JOIN transaction po ON po.id = ptll.previousdoc
  LEFT JOIN inboundshipmentitem isi ON isi.purchaseordertransaction = po.id
  LEFT JOIN inboundshipment s ON s.id = isi.inboundshipment
  WHERE po.type='PurchOrd' AND ptll.nextdoc IN (:receipt_ids)
  GROUP BY ptll.nextdoc
  ```
  Keep the shipment joins **LEFT** so a receipt whose PO isn't on a container still returns its
  `po_tranid`. Run it as its own id-scoped query, **not** as a correlated subquery in the receipts
  SELECT list — as a subquery it makes the whole receipts read (and the putaway charge with it)
  all-or-nothing on two cosmetic columns.
- Transient `HTTP 502 Bad gateway` happens — just retry after a moment.
- `SELECT *` is supported on `item`; `WITH`/CTEs are not; string concat is `||`; dates via `TO_DATE`.

## Resolved internal ids

| Thing | Id | Notes |
|---|---|---|
| Subsidiary — MacGear AU | `2` | Mova lives here |
| Subsidiary — MacGear NZ | `3` | Skriva lives here |
| Location — AU2 Melbourne Warehouse | `34` | parent of the 3PL location |
| **Location — warehouse 3PL** | **`49`** | `AU2 – Melbourne Warehouse : warehouse 3PL` → **Mova's 3PL location** |
| Location — NZ2 Auckland | `2` | Skriva's location (no dedicated 3PL loc) |
| Location — ClassVR 3PL | `22`, `29` | **another existing 3PL customer** — confirms multi-tenant |
| Class/brand — SKRIVA STYLUS | `236` | Skriva's brand tag |
| **Class/brand — MOVA** | **`237`** | Mova's brand tag — the **regular MOVA brand** (305 items). Dedicated `3PL - Mova` (`253`) is dropped as of 2026-07-22 (held only a test item). |
| Skriva customer | `10496` | entityid `03191` "Skriva Stylus" |
| Skriva vendor | `10503` | entityid `V01157` |
| Skriva item (white) | `50101` | `S-STYCASE-WHITE`; (blue = `38693`) |

> **Brand is the NetSuite `classification`, not a free-text field.** Store the class **id** per customer,
> not a string. The stock-isolation filter is **per-customer** (`customer.location_scoped`): scope by
> `class = Y` always, **AND** `location = X` only when the brand class also covers non-3PL stock
> (Mova: class `237` + loc `49`). Brand-exclusive customers whose stock spans locations (Skriva:
> Auckland + Christchurch) scope by class alone — a location filter would drop their rows.

## Item record fields (from `SELECT * FROM item WHERE id=50101`)
- `class` = brand (236). `subsidiary`, `totalquantityonhand` present on the item.
- **Units-per-pallet candidate: `custitem_pallet_quantity`** (null on Skriva — Skriva isn't palletised).
  Also `custitem_pallet_layer_quantity` and `custitem_mcg_item_master_qty` (="120", master carton qty).
  **TODO: confirm which field Mova populates** once Mova items exist; the brief says units/pallet is set on Mova items.

## Transaction taxonomy (validated)
Customer (10496): `SalesOrd`, `ItemShip` (fulfilment), `CustInvc`. Vendor (10503): `PurchOrd`, `ItemRcpt`, `VendBill`.
VRMA = type **`VendAuth`** (none in Skriva history — it's the Mova "Macgear buys-in" path).

`transactionline` exposes everything needed: `item`, `quantity`, `quantityshiprecv`, `location`, `class`,
`netamount`, `rate`, `linesequencenumber`. Filter item lines with `mainline='F' AND taxline='F'`.

### Important data nuances
- **Item fulfilments emit paired +qty / −qty lines** for the same item (item line vs inventory-impact line).
  For pick-fee counting **sum positive quantities only** — do NOT net them (would give 0).
- **Open PO test:** PO line where `quantityshiprecv < quantity`. Status-independent and reliable.
- **`transactionline.expectedreceiptdate` IS selectable on PO lines** (validated 2026-07-26 against live
  prod: open Mova 3PL lines at loc 49 all returned `28/08/2026`). It was originally left out of the
  RESTlet's PO query, which is why the portal's **Expected receipt** column was blank for every line not
  yet on an inbound shipment — and why those units never reached the Incoming chart. Now selected.
  Precedence: the **inbound shipment's `expecteddeliverydate` still wins** when the line is on a container
  (resolved at read time in `service.stock_on_order`); the line's date is the fallback.
- **Picking source (SO vs VRMA):** discriminate by the fulfilment's `entity` — customer id ⇒ SO pick,
  vendor id ⇒ VRMA pick. (Can't use `createdfrom`.)
- **Skriva invoices are $0 product invoices, not service charges.** Skriva isn't billed 3PL fees in NetSuite
  today. ⇒ **the service-charge invoice is greenfield**; our billing run defines the charge lines from scratch.

## Validated query library (the sync queries)

> Parameterise `:loc` (3PL location), `:class` (brand), `:cust` (customer id), `:vend` (vendor id),
> `:from`/`:to` (period). Skriva test values: loc=2, class=236, cust=10496, vend=10503.

**View 1 — Stock on order (open POs):**
```sql
SELECT t.id, t.tranid, t.trandate, tl.item, BUILTIN.DF(tl.item) item_name,
       tl.quantity ordered, tl.quantityshiprecv received,
       (tl.quantity - tl.quantityshiprecv) outstanding,
       tl.expectedreceiptdate expected
FROM transaction t JOIN transactionline tl ON tl.transaction = t.id
WHERE t.type='PurchOrd' AND t.entity=:vend AND tl.location=:loc
  AND tl.mainline='F' AND tl.taxline='F' AND tl.quantityshiprecv < tl.quantity
```

**View 2 / Putaway charge — item receipts:**
```sql
SELECT t.id, t.tranid, t.trandate, tl.item, BUILTIN.DF(tl.item) item_name, tl.quantity
FROM transaction t JOIN transactionline tl ON tl.transaction = t.id
WHERE t.type='ItemRcpt' AND tl.location=:loc AND tl.class=:class
  AND tl.mainline='F' AND tl.taxline='F' AND t.trandate BETWEEN :from AND :to
```

**View 3 / Storage charge — stock on hand:**
```sql
SELECT item, BUILTIN.DF(item) item_name, quantityonhand, quantityavailable
FROM inventorybalance WHERE location=:loc AND item IN (/* customer's items */)
-- pallets = CEIL(quantityonhand / units_per_pallet); snapshot weekly for billing
```

**View 4 / Picking charge — fulfilments (ASSET line, ABS qty):**
> CORRECTION (2026-06-27): the original "sum positives only" was wrong — it dropped every VRMA.
> A customer SO fulfilment emits a +qty COGS / −qty ASSET line pair, but a VRMA fulfilment (ship
> back to the supplier) emits a SINGLE **negative** ASSET line with no positive counterpart, so
> `tl.quantity > 0` returned nothing for VRMAs even though stock left NetSuite. The ASSET line is
> the real inventory movement in both cases (negative = leaving), so filter to it and take ABS().
```sql
SELECT t.id, t.tranid, t.trandate, t.entity,
       CASE WHEN t.entity=:cust THEN 'SO' ELSE 'VRMA' END source,
       tl.item, BUILTIN.DF(tl.item) item_name, ABS(tl.quantity) qty
FROM transaction t JOIN transactionline tl ON tl.transaction = t.id
JOIN item i ON i.id = tl.item
WHERE t.type='ItemShip' AND t.entity IN (:cust, :vend) AND i.class=:class
  AND tl.mainline='F' AND tl.taxline='F'
  AND tl.accountinglinetype='ASSET' AND tl.quantity IS NOT NULL AND tl.quantity <> 0
  AND t.trandate BETWEEN :from AND :to
```

**View 5 — invoices on the customer:**
```sql
SELECT t.id, t.tranid, t.trandate, BUILTIN.DF(t.status) status, t.foreigntotal total
FROM transaction t WHERE t.type='CustInvc' AND t.entity=:cust ORDER BY t.trandate DESC
```

**Container-unload charge + Stock-on-order link — inbound shipments (VALIDATED 2026-06-30):**
Run against production (NANOLEAF brand, class 31, which has real inbound shipments). Key gotchas:
`shipmentstatus` is **already a text label** ('received') — do NOT wrap in `BUILTIN.DF`.
`inboundshipmentitem` has **no `item` column**: reach the PO line (hence item) via
`shipmentitemtransaction = transactionline.uniquekey`, and the PO header via `purchaseordertransaction`.
The shipment→item FK column is `inboundshipment` (not `shipment`). Implemented in RESTlet `inbound_shipments`.
```sql
-- Member lines (shipment id + PO doc + item — feeds PoLine.ns_inbound_shipment + expected receipt):
SELECT isi.inboundshipment shipment, po.tranid po_tranid, tl.item
FROM inboundshipmentitem isi
JOIN transactionline tl ON tl.uniquekey = isi.shipmentitemtransaction
JOIN item i ON i.id = tl.item
LEFT JOIN transaction po ON po.id = isi.purchaseordertransaction
WHERE i.class = :class
-- Headers for the shipment ids found above (dates are dd/mm/yyyy):
SELECT id, shipmentnumber, expecteddeliverydate, actualdeliverydate, shipmentstatus status
FROM inboundshipment WHERE id IN (:shipment_ids)
```
No native `container_type` field exists on `inboundshipment` (returned as null; the unload charge
counts containers regardless). Other header fields available if needed: `externaldocumentnumber`,
`billoflading`, `expectedshippingdate`, `actualshippingdate`, `vesselnumber`, `shipmentcreateddate`.

**Container-unload trigger date (important):** `actualdeliverydate` is populated on only **2 of 78**
received shipments account-wide — useless as the "received" trigger. `shipmentstatus` is the reliable
signal (values: `received` / `partiallyReceived` / `inTransit`). So the RESTlet sets `received_date`
= `actualdeliverydate || lastmodifieddate` **only** for received/partiallyReceived shipments (both
date fields are 100% populated); in-transit shipments get null and aren't billed. `billing.py` counts
shipments with `received_date` in the period. Caveat: `lastmodifieddate` moves if a received shipment
is later edited, and a partiallyReceived→received transition re-dates it — re-run affected periods.

## Remaining TODO (need Mova data, ~end Jul 2026)
1. Confirm Mova item `custitem_pallet_quantity` is the units/pallet field and is populated.
2. ~~`inboundshipment` field names~~ **DONE (2026-06-30)** — validated against production; RESTlet
   `inbound_shipments` query corrected. Still worth a final smoke-test against the first real **Mova**
   shipment once one exists (class `237` MOVA), as the queries were proven on the NANOLEAF brand.
3. First real VRMA fulfilment to confirm the entity-based SO/VRMA discriminator on Mova.
4. ~~Confirm Mova's exact class id~~ **RESOLVED (2026-07-22):** Mova uses its **regular MOVA brand `237`**
   (305 items, regular SKUs). The dedicated `3PL - Mova` `253` brand and dedicated 3PL SKUs are dropped —
   3PL stock is isolated by `location = 49 AND class = 237`, the same shape as Skriva.
