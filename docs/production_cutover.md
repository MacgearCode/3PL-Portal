# Production cutover — sandbox → NetSuite production

> ## ✅ EXECUTED 2026-07-27 — the read path is LIVE
> RESTlet deployed to production, n8n repointed, cache purged, full sync run. All 6 visibility
> views are populated with real Mova 3PL data and reconcile against NetSuite.
> **Billing write path: configured 2026-08-05** (§1a → customer `11066`; §1b → charge items now
> live in the app). Not yet fired against production. See the *Billing roadmap* in `CLAUDE.md`.
>
> **What actually bit, in order:**
> 1. **Item receipts came back empty with no error.** The integration role was missing
>    *Transactions → Item Receipt*; NetSuite row-filters silently rather than failing (§7). Cost a
>    wasted RESTlet redeploy chasing a code-level theory first. **Check §7's permission table
>    before debugging anything.**
> 2. **Expected receipt was blank** — the RESTlet never selected `tl.expectedreceiptdate`, so the
>    Incoming chart had nothing to plot. Fixed and redeployed.
> 3. **Two columns could never populate** — the receipts' inbound shipment and the fulfilments'
>    source document were simply never returned by the RESTlet. Both added.
> 4. `expecteddeliverydate` is **NULL on all 12 production shipments**, so the PO line's
>    `expectedreceiptdate` is the only working ETA — which is exactly why fix 2 mattered.
>
> ### Follow-up, 2026-08-01
> 5. **The container-unload charge was billing in the wrong week.** `actualdeliverydate` turns out
>    to be NULL on **all 36** shipments (not just `expecteddeliverydate`), so the RESTlet's
>    `lastmodifieddate` fallback always applied — and it's a bulk-edit timestamp, not a delivery.
>    The 9 containers unloaded 20 Jul read as 30 Jul: **0 containers billed in the 20–26 Jul week,
>    22 in the next.** The charge is now dated off the item receipt's `trandate`. Same root cause
>    family as fix 4 — the `inboundshipment` header's dates are simply not populated in this
>    account. Don't trust any of them for billing.
> 6. **A stale sandbox invoice is still in the production cache** — §2a, targeted purge. ✅ **No
>    longer needs a manual purge as of 2026-08-05**: `ingest_invoices` now prunes invoices the
>    pull stops returning, so the next full lane clears it. The prune is guarded on a non-empty
>    pull — see the permission trap in §7; an unguarded one would delete every invoice the first
>    time a permission blipped.
>
> Retain this document: it is the record of the verified production ids and the setup, and the
> template for onboarding the next 3PL customer.

Companion to `deploy.md` (which covers the sandbox build). This is the **flip to production**:
what to change, what to verify first, and the two things that will silently corrupt billing if
skipped. Everything below was verified against **live production** on 2026-07-26/27 via SuiteQL.

---

## 0. Verified production internal ids

These are confirmed, not assumed — each was read out of production.

| What | Value | Note |
|---|---|---|
| Account id (n8n `ACCOUNT_ID`) | `840974` | drop the `_SB1` sandbox suffix |
| 3PL location | **`49`** — "warehouse 3PL" | full name `AU2 – Melbourne Warehouse : warehouse 3PL` |
| Brand class (MOVA) | **`237`** | same as sandbox |
| `location_scoped` | **True** | ✅ verified: class 237 also carries Macgear-owned stock at `AU2 – Melbourne Warehouse` (e.g. PO `POAU002082`). Without the location filter that stock leaks in and massively over-bills putaway. |
| Vendor / supplier (`ns_supplier_id`) | **`10872`** — "Mova Technologies (AU) ($AUD)" | this is the `entity` on every open 3PL PO. **Careful:** 4 Mova vendors exist — `10504` is the **$USD** one, `10688` is NZ, `10749` is "Mova Collaborators". Use `10872`. |
| Subsidiary (`ns_subsidiary_id`) | **`2`** — MacGear AU | needed for draft invoices |
| Units-per-pallet field (`UPP_FIELD`) | **`custitem_pallet_quantity`** | ✅ populated (`12`) on all 5 on-hand 3PL items. `custitem_pallet_layer_quantity` is a *different* field (per layer) — do not use it. |
| Customer (`ns_customer_id`) | **`11066`** — "Spacewalker Technology Hong Kong Co., Limited" (`03431`) | ✅ decided 2026-08-05. Currency **AUD**. An existing trading entity, so tick `invoice_items_only` (§1). |
| Charge items | **in the app**, not n8n — Settings → Charge items | ✅ 2026-08-05. `CHARGE_ITEMS` deleted from the Code node; sandbox ids `55070–55074` never existed in production. Mapping table in `CLAUDE.md`. |

---

## 1. Three decisions to make before the write path works

Reads (all 6 portal views) work without any of these. Only the **draft-invoice push** is blocked.

> ## ✅ (a) and (b) RESOLVED 2026-08-05 — both are recorded below as history.
> **Bill-to: customer `11066` — "Spacewalker Technology Hong Kong Co., Limited"** (customer
> number `03431`), Mova's HK entity, currency **AUD**. Not one of the four listed below, and not
> a new dedicated record. Because it is an **existing trading entity**, Mova is set
> `invoice_items_only` so the portal's Invoices view shows only invoices carrying a 3PL charge
> item — otherwise Spacewalker's ordinary product invoices appear in the customer's portal.
> **Charge items: mapped in the app, not in n8n.** `CHARGE_ITEMS` is gone from the Code node;
> the mapping lives in `charge_item` and is edited at Settings → Charge items. The container
> unload item **now exists** (`57082`), `23560` has been renamed "Putaway Fee", and the
> duplicate `3PL - Storage Charge` (`36282`) is no longer in the item list. Current mapping is
> the table in `CLAUDE.md` § *Charge items*.
>
> **Remaining to fire the first push:** set `11066` on Mova in `/admin/customers`, tick
> "Only show invoices carrying a 3PL charge item", and redeploy `netsuite/3pl_restlet.js`
> (the invoice read changed: item id, currency, payment fields, item filter).

### a) Which customer record gets the 3PL service invoice? *(answered: `11066`)*
Production has four Mova customers, none of which is obviously a 3PL billing entity:

| id | name | subsidiary |
|---|---|---|
| 10501 | Mova AU Online | MacGear AU |
| 10502 | Mova NZ Online | MacGear NZ |
| 10567 | Mova DOA replacements AU | MacGear AU |
| 10859 | Mova DOA replacements NZ | MacGear NZ |

These look like sales-channel and warranty accounts. A dedicated "Mova 3PL" customer is probably
the right answer — the service invoice and the $0 dispatch sales orders both key off it, so mixing
it with an online-sales account will muddy both. Whatever you pick goes in `/admin/customers` →
Mova → **NetSuite customer id**. *(In the event: `11066`, the HK parent entity, was chosen over
all of these and over creating a new record.)*

### b) Which production items back the five charge types? *(answered — see the box above)*
`CHARGE_ITEMS` in the n8n node maps `charge_type → NetSuite invoice item id`. The sandbox ids
(`55070–55074`) are sandbox-only. Production already has a 3PL service item set — but the names
don't line up 1:1 and **there is no container-unload item at all**:

| Candidate | id | Likely charge_type |
|---|---|---|
| `3PL - Receipt of Goods` (display "Receipt of Goods") | 23560 | `putaway`? |
| `3PL - Storage` | 23561 | `storage`? |
| `3PL - Storage Charge` | 36282 | `storage`? (duplicate — pick one) |
| `3PL - Picking` | 23563 | `picking_so` / `picking_vrma`? |
| `3PL - Pick / Pack Service Charge` | 36281 | `picking_*`? (duplicate — pick one) |
| `3PL - Kitting` / `Packaging` / `Freight` / `System Fee` / `Additional Labour` | 23562/23564/23565/23566/23567 | not used by the current rate card |
| **container unload** | — | **none exists — create one** |

Also note `3PL test item` (`55170`) exists in production — a leftover test artifact, don't map to it.

Two charge types can share one item if you'd rather not create a "Container unload" item — but
then the invoice won't itemise it, and the portal's line detail won't match the NetSuite invoice.
Recommend creating one non-inventory item, "3PL - Container Unload".
*(In the event: the item was created — `57082` — and `23560` renamed to "Putaway Fee". The
duplicates resolved themselves: `36282` is gone from the list, and `36281` is kept as an ad-hoc
item rather than mapped to `picking_*`. `picking_so` and `picking_vrma` share `23563`.)*

### c) Confirm the container-unload volume
The first billing run will charge **9 containers × $1,500 = $13,500** (see §5), off receipts
`IR023981`–`IR023989` dated **2026-07-20**, 6,369 units. Confirm that's nine physical container
unloads and not one delivery split across nine shipment records — it's the largest line on the
invoice by some margin. (The 1:1 shipment→receipt mapping *is* confirmed in the data; what only you
can confirm is that nine records means nine physical unloads.)

**A second batch has since landed:** 13 containers, receipts dated **2026-07-31**, 7,109 units →
another **$19,500** in the 27 Jul–2 Aug week. Same question applies.

### d) Populate `custitem_pallet_quantity` on three SKUs
`52856` / `52857` / `52858` (`20010100002916`, `20010100003083`, `20010100003085`) have the field
**NULL**, with 8,000 units on order between them. Storage is `ceil(units ÷ units_per_pallet)`, so a
NULL contributes **0 pallets** — those units would be stored free. They hold no stock yet, so fix
it in NetSuite before that batch receives.

---

## 2. ⚠️ Purge the cache before the first production sync

**This is the one step that silently corrupts billing if skipped.**

The app's Postgres cache currently holds **sandbox-derived rows**. Most ingests upsert by NetSuite
internal id and **never prune**:

| Entity | On re-sync | Sandbox rows survive? |
|---|---|---|
| `purchase_orders` | prunes anything absent from the pull | no ✅ |
| `stock_on_hand` | replaces **today's** snapshot only | **older days persist** ⚠️ |
| `item_receipts` | upsert only | **yes** ⚠️ |
| `item_fulfilments` | upsert only | **yes** ⚠️ |
| `inbound_shipments` | upsert only | **yes** ⚠️ |
| `invoices` | upsert (rebuilds lines) | **yes** ⚠️ |
| `items` | upsert only | **yes** (harmless — stray SKUs) |

So sandbox receipts, fulfilments and shipments would sit alongside the production ones and be
**billed a second time** — putaway, picking and container-unload all over-charge. Stale SOH days
also skew the storage average (it's avg daily pallets × weeks).

Sandbox is a refresh copy of production, so some internal ids *collide* and some *diverge* —
neither case saves you. Purge.

**Timing:** purge **after** n8n is pointed at production but **before** the first successful full
run. A sandbox-configured run firing in between just re-contaminates the cache. Disabling the
schedule triggers while you cut over is the simplest way to guarantee that.

On the droplet:

```bash
cd /opt/threepl/3PL-Portal
docker compose exec threepl-db psql -U threepl -d threepl -c "
  TRUNCATE item_receipt_line, item_receipt,
           item_fulfilment_line, item_fulfilment,
           po_line, purchase_order,
           inbound_shipment,
           invoice_line, invoice,
           stock_on_hand,
           billing_line, billing_run,
           item, sync_log;"
```

Deliberately **not** truncated: `customer`, `rate_card`, `rate_card_line`, `app_user` — that's your
configuration and logins, which you want to keep. Take a backup first:

```bash
docker compose exec threepl-db pg_dump -U threepl threepl > ~/threepl-pre-prod-$(date +%F).sql
```

Truncating `billing_run` also clears any sandbox test runs, which matters: a period already
queued/pushed/invoiced is **locked against re-billing**, so a leftover sandbox run for the current
week would block the real one.

### 2a. Residual sandbox invoice — targeted purge (2026-08-01, ⏳ NOT YET RUN)

A cached invoice left over from sandbox development is still showing in the production portal, so
either §2 missed `invoice` or a sandbox-configured run re-contaminated it afterwards. `invoice` is
**upsert-only and never prunes** (§2 table), so it will not clear itself on any future sync.

Nothing in the app deletes cached rows — there is no purge endpoint and no invoice files are ever
generated, so this is a DB-only operation on the droplet. Do it **targeted**, not by truncation,
now that real production invoices may exist alongside it.

```bash
cd /opt/threepl/3PL-Portal

# 1. Backup FIRST — non-negotiable.
docker compose exec threepl-db pg_dump -U threepl threepl > ~/threepl-pre-invoice-purge-$(date +%F).sql

# 2. Look before deleting. A sandbox leftover is one whose tranid does not exist in prod NetSuite.
docker compose exec threepl-db psql -U threepl -d threepl -c "
  SELECT i.id, c.slug, i.ns_invoice_id, i.tranid, i.trandate, i.status, i.total, i.synced_at,
         (SELECT count(*) FROM invoice_line l WHERE l.invoice_id = i.id) AS lines
  FROM invoice i JOIN customer c ON c.id = i.customer_id ORDER BY i.trandate;"

docker compose exec threepl-db psql -U threepl -d threepl -c "
  SELECT r.id, c.slug, r.period_start, r.period_end, r.status, r.ns_invoice_id,
         r.locked_at, r.locked_by, r.created_at
  FROM billing_run r JOIN customer c ON c.id = r.customer_id ORDER BY r.period_start;"

# 3. Delete the identified invoice by its ns_invoice_id. invoice_line is ON DELETE CASCADE.
docker compose exec threepl-db psql -U threepl -d threepl -c "
  DELETE FROM invoice WHERE ns_invoice_id = '<the id from step 2>';"

# 4. Only if a leftover billing_run blocks a week you still need to bill (a run at
#    ready_to_push/pushed/invoiced, or one with locked_at set, refuses to be recomputed):
docker compose exec threepl-db psql -U threepl -d threepl -c "
  DELETE FROM billing_run WHERE id = <run id>;"     -- billing_line cascades
```

While connected, capture the two facts the billing preview depends on:

```bash
# Which days actually have an SOH snapshot? Storage is avg daily pallets over the days PRESENT,
# so a week with no snapshot bills no storage (the preview now warns, but confirm the dates).
docker compose exec threepl-db psql -U threepl -d threepl -c "
  SELECT c.slug, s.snapshot_date, count(*) AS skus, sum(s.pallets) AS pallets
  FROM stock_on_hand s JOIN customer c ON c.id = s.customer_id
  GROUP BY c.slug, s.snapshot_date ORDER BY s.snapshot_date;"

# Did the container fix land? 20-26 Jul must show 9 containers; 27 Jul-2 Aug must show 13, not 22.
docker compose exec threepl-db psql -U threepl -d threepl -c "
  SELECT r.ns_inbound_shipment, min(r.trandate) AS first_receipt
  FROM item_receipt r JOIN customer c ON c.id = r.customer_id
  WHERE c.slug = 'mova' GROUP BY r.ns_inbound_shipment ORDER BY 2, 1;"
```

### 2b. Schema addition for the period lock

`billing_run.locked_at` / `locked_by` are added by `ensure_columns()` (`app/db.py`) on app
startup — no manual DDL. Redeploy the app and they appear; `db/01_schema.sql` carries them for
fresh environments.

---

## 3. Connection & secrets setup

Eight secrets/ids are involved. Four are NetSuite's, four are ours.

| Value | Where it lives | How to get / rotate |
|---|---|---|
| `NS_CONSUMER_KEY` / `NS_CONSUMER_SECRET` | n8n | Integration record (§3.2). **Shown once.** Rotate = "Reset Credentials" on the integration. |
| `NS_TOKEN_ID` / `NS_TOKEN_SECRET` | n8n | Access Token (§3.5). **Shown once.** Rotate = revoke + create a new token. |
| `NS_ACCOUNT_ID`, `NS_RESTLET_SCRIPT`, `NS_RESTLET_DEPLOY` | n8n | not secret, but must match prod (`840974`, and the deployment's ids) |
| `SYNC_TOKEN` | app `.env` **and** n8n | `openssl rand -hex 32`. Must be identical in both — change together. |
| `APP_SECRET` | app `.env` | `openssl rand -hex 32`. **Rotating logs everyone out** (it signs session cookies). |
| `PGPASSWORD` | app `.env` (twice — also inside `DATABASE_URL`) | `openssl rand -hex 32`. See the gotcha in §3.8. |

### 3.1 Enable the features (production)
Setup > Company > **Enable Features** > SuiteCloud tab:
- **Token-Based Authentication** ✅
- **SuiteScript** → Server SuiteScript ✅ (RESTlets)
- **REST Web Services** ✅
- **SuiteAnalytics Workbook** ✅ — the RESTlet runs `N/query.runSuiteQLPaged`, which needs it.

### 3.2 Integration record → consumer key/secret
Setup > Integration > **Manage Integrations** > New
- Name: `Macgear 3PL Portal Sync`
- State: **Enabled**
- Authentication: tick **Token-Based Authentication**. Untick *TBA: Authorization Flow*,
  *User Credentials* and *OAuth 2.0* — the node signs OAuth 1.0 itself and needs nothing else.
- Save → **Consumer Key and Consumer Secret are displayed exactly once.** Copy them now. If you
  lose them the only path is *Reset Credentials*, which invalidates the old pair.

### 3.3 Integration role
Setup > Users/Roles > **Manage Roles** > New — e.g. `3PL Portal Integration`. Grant:

| Area | Permission | Level |
|---|---|---|
| Setup | **Log in using Access Tokens** | Full — *mandatory for TBA; nothing works without it* |
| Setup | REST Web Services | Full |
| Setup | SuiteScript | Full |
| Reports | **SuiteAnalytics Workbook** | View — *required for `N/query` SuiteQL* |
| Transactions | Purchase Order, Item Receipt, Item Fulfilment, Sales Order | View |
| Transactions | **Invoice** | **Create** — needed for the draft-invoice push |
| Lists | Items, Inventory (inventory balance), Locations, Classes, Customers, Vendors | View |
| Lists | Inbound Shipment | View |

⚠️ **OneWorld subsidiary access.** Production is OneWorld (MacGear AU `2`, MacGear NZ `3`,
Rewarding Concepts AU `5` / NZ `6`). On the role's **Subsidiaries** setting, grant at least
**MacGear AU (2)**. If the role can't see the subsidiary, the SuiteQL reads return **zero rows with
no error** — which looks exactly like "no 3PL activity yet" and is very easy to misdiagnose. Tick
*Cross-Subsidiary Record Viewing* if you later add customers in other subsidiaries.

### 3.4 Integration user
Assign that role to a **dedicated employee record** (e.g. `3pl-integration@macgeargroup.com`), not
your own login. Tokens are tied to user + role, so a personal token dies when your access changes,
and audit trails will attribute every sync to you. The user needs no password if it never logs in
interactively.

### 3.5 Access token → token id/secret
Setup > Users/Roles > **Access Tokens** > New
- Application Name: the integration from §3.2
- User: the integration user from §3.4
- Role: the role from §3.3
- Save → **Token ID and Token Secret are displayed exactly once.** Copy them now.

**No, the sandbox token cannot be reused.** All four TBA values are account-specific:
- **Token id/secret** are scoped to a *specific account* and to the (integration, user, role) triple
  inside it. Sandbox and production are different accounts on different RESTlet hosts, so a sandbox
  token has nothing to authenticate against in production. Generate a fresh pair here.
- **Consumer key/secret** likewise — and they were never going to match: NetSuite **resets a
  sandbox's integration credentials on every sandbox refresh**, so sandbox and prod consumer keys
  differ by design.

⚠️ **Sandbox → production does not propagate.** Refreshes copy prod → sandbox, never the reverse.
So anything you built *in the sandbox* is absent from production: the integration **role** (§3.3),
the integration **user** (§3.4), the script, and the deployment. If the role/user only exist in
sandbox, the Access Token screen won't offer them and you'll have to back out mid-setup — create
them in production first.

Also: a new script + deployment means **new internal ids**, so `NS_RESTLET_SCRIPT` and
`NS_RESTLET_DEPLOY` both change (sandbox was `1343` / `1`). Leaving the sandbox values gives a 404 —
or, worse, silently invokes some unrelated production script.

### 3.6 RESTlet deployment audience
Customization > Scripting > Scripts > (your RESTlet) > **Deployments**:
- Status **Released**
- **Audience → Roles**: add the integration role. *This is separate from the role's permissions —
  a fully-permissioned role still gets a 403 if it isn't in the deployment audience.*
- Copy `script=` and `deploy=` from the **External URL** into `NS_RESTLET_SCRIPT` / `NS_RESTLET_DEPLOY`.

### 3.7 Put the secrets on n8n, not in the Code node
A Code node's body is stored in the workflow record and travels in **every workflow export/backup
as plain text** — unlike an n8n *credential*, it is not encrypted with `N8N_ENCRYPTION_KEY`. So
don't paste live NetSuite keys inline. The node now reads them from the environment, falling back
to the inline constants when unset:

```yaml
# n8n's docker-compose.yml
services:
  n8n:
    environment:
      - NS_ACCOUNT_ID=840974
      - NS_CONSUMER_KEY=...
      - NS_CONSUMER_SECRET=...
      - NS_TOKEN_ID=...
      - NS_TOKEN_SECRET=...
      - NS_RESTLET_SCRIPT=...
      - NS_RESTLET_DEPLOY=1
      - THREEPL_SYNC_TOKEN=...
```
Then `docker compose up -d n8n`. Keep those in n8n's own `.env`, not in the compose file.

If your n8n has `N8N_BLOCK_ENV_ACCESS_IN_NODE=true`, `$env` reads throw — the node catches that and
uses the inline fallback, so either style works. To use env vars, set that to `false`.

The node now **throws immediately** if any secret is still `REPLACE_...`, rather than firing
mis-signed requests at NetSuite every 15 minutes all night.

### 3.8 App secrets — two gotchas
- **`PGPASSWORD` after first boot.** `POSTGRES_PASSWORD` only takes effect when Postgres
  *initialises its volume*. Changing it in `.env` later does **not** change the existing password —
  the app just fails to connect. To rotate properly:
  ```bash
  docker compose exec threepl-db psql -U threepl -d threepl \
    -c "ALTER USER threepl WITH PASSWORD 'new-password';"
  # then update BOTH PGPASSWORD and the password inside DATABASE_URL in .env
  docker compose up -d threepl
  ```
- **`SYNC_TOKEN` must change in both places at once.** Until n8n and the app agree, every ingest
  gets rejected and the sync logs auth failures. Update `.env`, `docker compose up -d threepl`,
  then update n8n.

### 3.9 Prove each hop before scheduling anything
Work outwards; each step isolates one failure domain.

```bash
# 1. App up and the token accepted? (on the droplet)
curl -s -H "X-Sync-Token: $SYNC_TOKEN" http://localhost:8000/admin/sync-config | head
#    -> JSON listing Mova with its ns ids.  401/403 => SYNC_TOKEN mismatch.
#    Mova absent => no brand class set (§4.11) — only class-scoped customers are returned.

# 2. n8n -> app on the shared Docker network?
docker exec -it n8n curl -s -o /dev/null -w '%{http_code}\n' \
  -H "X-Sync-Token: $SYNC_TOKEN" http://threepl:8000/admin/sync-config
#    -> 200. Anything else = SHARED_NETWORK / container-name problem, not NetSuite.
```

3. **n8n → NetSuite:** execute the Code node manually (full lane). Read the failure, don't guess:

| Symptom | Almost always means |
|---|---|
| `401` + `INVALID_LOGIN_ATTEMPT` | wrong consumer/token pair, or `NS_ACCOUNT_ID` (realm) wrong — prod is `840974`, no `_SB1` |
| `401` + `INVALID_LOGIN` after a correct-looking setup | role missing **Log in using Access Tokens** |
| `403` | role not in the **deployment audience** (§3.6), or missing a record permission |
| `200` but `{"error":"unknown action: …"}` | **auth is fine** — our RESTlet answered. Wrong `action` name. |
| `200`, no error, **zero rows everywhere** | ⚠️ almost certainly **subsidiary access** (§3.3) — not "no data" |
| `INSUFFICIENT_PERMISSION` on SuiteQL only | **SuiteAnalytics Workbook** not enabled/granted |
| Signature errors on some runs only | clock skew on the n8n host — OAuth timestamps must be within ~5 min |

4. Then follow the read-first verification in §6 before letting any invoice through.

---

## 4. Cutover steps

### NetSuite (production)
1. **Enable features:** Setup > Company > Enable Features > SuiteCloud → Token-Based
   Authentication + SuiteScript/RESTlets.
2. **RESTlet** — you've done this. Verify it's the **current** version: the PO query must select
   `tl.expectedreceiptdate`. Without it the Expected Receipt column stays blank and the Incoming
   chart stays empty (that was the bug fixed in `bf01c33`). Test in §6.1.
3. **Integration record** → Consumer Key/Secret. **Access Token** → Token ID/Secret. The role needs
   SuiteQL/REST access **and** invoice-create permission.
4. **Create the container-unload item** (§1b) and note all five item ids.
5. Decide/create the 3PL billing customer (§1a).

### App (droplet)
6. Confirm `.env` has `SEED_DEMO=0` — otherwise a fresh boot plants fake demo rows into production.
7. Confirm `SYNC_TOKEN` is set and matches what you'll put in the n8n node.
8. **Change the seeded passwords** if you haven't (`admin123` / `internal123` / `mova123`).
9. `git pull && docker compose up -d --build` to get the ETA fix and the re-theme.
10. **Purge the cache (§2).**
11. `/admin/customers` → Mova: brand class `237`, brand label `MOVA`, 3PL location `49`,
    supplier `10872`, subsidiary `2`, customer id from §1a, and **tick "Isolate 3PL stock to the
    location above"** (`location_scoped`).
12. Check Mova's **rate card** is the agreed production pricing: container unload $1,500/container,
    putaway $1.00/unit, storage $4.50/pallet/week, picking $1.00/unit (SO and VRMA).

### n8n
13. In the Code node, change **only** the NetSuite-side constants:
    - `ACCOUNT_ID` → `840974`
    - the four TBA secrets → production integration + token
    - `RESTLET_SCRIPT` / `RESTLET_DEPLOY` → from the production deployment's External URL
    - `CHARGE_ITEMS` → the five production item ids (§1b)
    - `UPP_FIELD` → `custitem_pallet_quantity` (already correct)
    - `SYNC_TOKEN` → matches the app's `.env`
    - `APP_BASE` → unchanged (`http://threepl:8000`)
14. Leave `READ_ENTITIES` order alone — `inbound_shipments` **must** run after `purchase_orders`,
    because it stamps the PO→shipment link onto lines that the PO ingest rebuilds each run.
15. Confirm both lanes exist: fast (`mode:"soh"`, every 15 min) and full (`mode:"full"`, daily).

---

## 5. What the first production sync should produce

Verified against live data 2026-07-26. Use these to confirm the sync worked before invoicing.

**Portal views**
- **Stock on hand** — 5 SKUs, **6,369 units**, **533 pallets** (all items 12/pallet):
  | SKU | Item | On hand | Pallets |
  |---|---|---|---|
  | 010201AA001019 | MOVA S70 Ultra Roller | 447 | 38 |
  | 010201AA001014 | MOVA S70 Pro Roller | 155 | 13 |
  | 010201AA000984 | MOVA S70 Roller | 2,145 | 179 |
  | 010201AA001082 | MOVA E50 Pro Ultra | 1,888 | 158 |
  | 010201AA001159 | MOVA E50 Ultra | 1,734 | 145 |
- **Item receipts** — 9 receipts (`IR023981`–`IR023989`), trandate **20/07/2026**, 6,369 units total
- **Stock on order** — 4 open lines across `POAU002108/2109/2110`, **2,145 units**, all with
  expected receipt **28/08/2026**
- **Inbound shipments** — 12 (`INBSHIP91`–`102`): 91–99 `received`, 100–102 `in transit`
- **Fulfilments** — none yet (no dispatches, so no picking charges)

**Billing preview for the week 20–26 Jul 2026** — expect **$19,869.00**:

| Charge | Basis | Amount |
|---|---|---|
| Container unload | 9 containers (first receipt 20/07) × $1,500 | $13,500.00 |
| Putaway | 6,369 units × $1.00 | $6,369.00 |
| Storage | **no SOH snapshot in this week** — see below | $0.00 |
| Picking (SO + VRMA) | 0 | $0.00 |
| **Total** | | **$19,869.00** |

⚠️ **Storage is $0 for this first week, not $2,398.50 as originally predicted.** Storage is
*average daily pallets × weeks* over the snapshot days **present in the period**, and the first
production snapshot is dated **27 Jul** — after this week closed. There is nothing in 20–26 Jul to
average, so the charge doesn't arise. The preview now warns rather than leaving it invisible.
The stock was physically there from the 20th, so this **under-bills by about a week (~$2,400)**.
Two defensible options, and it's a commercial call:
- **Accept $0** and start storage from 27 Jul. Cleanest.
- **Backfill one snapshot dated 2026-07-20** from the receipted quantities (6,369 units ÷ 12 =
  531 pallets), which makes the week bill as "that reading held all week" — exactly the documented
  single-snapshot degradation. 531 × $4.50 = **$2,389.50**, taking the week to **$22,258.50**.

**Week 27 Jul – 2 Aug 2026** — the second batch: **13** containers (receipts dated 31/07) ×
$1,500 = $19,500, putaway 7,109 × $1.00 = $7,109, plus storage over whatever snapshot days exist.
If you see **22** containers in this week, the container fix has not been deployed — that's the
old `lastmodifieddate` behaviour stacking both batches into one week.

---

## 6. Verification order — read first, invoice last

1. **Prove the RESTlet directly** before wiring the schedule. Run the n8n node manually (or curl the
   RESTlet) with `{"action":"purchase_orders", ...}` for Mova and confirm each line carries
   `expected_date`. If it's absent, the deployed RESTlet predates `bf01c33` — redeploy it.
2. **One full-lane run**, then walk all 6 views against §5. Numbers must match before you trust any
   charge.
3. **Billing run → Preview only.** Compare to the §5 table. Do **not** queue for NetSuite yet.
4. Confirm §1c (the 9 containers) and §1a/b (customer + items).
5. Only then **Queue for NetSuite** → n8n creates a **draft** invoice → review it in NetSuite before
   approving. Nothing is ever posted automatically.
6. Let the fast lane run a few cycles and confirm the SOH view shows "● live · updated N min ago".

---

## 7. ⚠️ A missing transaction permission looks exactly like "no data yet"

**Hit for real on 2026-07-27.** The Item receipts view was empty (sidebar badge `0`) while Stock on
hand and Stock on order were perfect — and **nothing appeared in the n8n error log**.

Cause: the integration role was missing **Transactions → Item Receipt**. NetSuite does not raise an
error for that. It applies row-level filtering and returns an **empty result set from a successful
query**, so the sync reports a clean run that ingested zero rows.

This is the second silent-zero-rows failure mode in this integration (subsidiary access, §3.3, is
the other). Both are dangerous specifically because billing is driven off these reads: a silently
empty `item_receipts` costs the entire putaway charge ($6,369 in week one), and a silently empty
`inbound_shipments` costs the container-unload charge ($13,500).

**How to tell it apart from a genuine failure.** In the n8n execution output the ingest endpoint
echoes a count:
```json
{"customer":"mova","entity":"item_receipts","ingested":0}
```
- `ingested: 0` **with no** `{step:"read", level:"error", …}` item → the query succeeded and returned
  nothing → **permission filtering** (or subsidiary access). Fix the role, not the code.
- an `error` item → a genuine query/permission exception, message included.

**Per-view → permission it needs.** Grant all of these up front; each one silently empties its view:

| Empty view | Missing permission |
|---|---|
| Item receipts | Transactions → **Item Receipt** (View) |
| Fulfilments | Transactions → **Item Fulfilment** (View) — *and* Sales Order |
| Stock on order | Transactions → **Purchase Order** (View) |
| Inbound shipment column / container-unload charge | Lists → **Inbound Shipment** (View) |
| Invoices | Transactions → **Invoice** (Create, which implies view) |
| Stock on hand | Lists → Items + Inventory |

Role changes take effect immediately — **no RESTlet redeploy**, just re-run the full lane.

**Rule of thumb:** any view that is empty while others populate, with a clean n8n run, is a role
permission — not a bug, not missing data. Check the badge counts against §5 before trusting a
billing preview.

---

## 8. Known cosmetic gaps (not blockers)

- **`expecteddeliverydate` is NULL on all 12 production inbound shipments.** The Expected Receipt
  column therefore comes from the PO line's `expectedreceiptdate` — which is exactly why the
  `bf01c33` fix is required. If a shipment later gets a delivery date it takes precedence.
- **`container_type` is always null** — `inboundshipment` has no native container-type field, so the
  portal can't show "40ft loose stacked" even though the rate card names it. Cosmetic only; the
  charge is per container regardless.
- **Container-unload dates come from `lastmodifieddate`** (only ~3% of received shipments populate
  `actualdeliverydate`). Editing a received shipment later moves its date; a
  `partiallyReceived → received` transition re-dates it into that week. Re-run affected periods if
  that happens.
