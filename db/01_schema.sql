-- 3PL Portal — Postgres schema (v1)
-- Status: NetSuite-derived columns validated against live data 2026-06-26 (see docs/netsuite_validation.md).
-- Convention: ns_* columns hold NetSuite internal ids / tranids. All facts keyed by customer_id (multi-tenant).
-- "Brand" in the brief == NetSuite classification id (ns_class_id), NOT free text.

-- ---------------------------------------------------------------------------
-- Config / dimensions
-- ---------------------------------------------------------------------------

CREATE TABLE customer (
    id                  SERIAL PRIMARY KEY,
    slug                TEXT NOT NULL UNIQUE,           -- 'mova', 'skriva' (used in URLs / auth)
    name                TEXT NOT NULL,
    ns_customer_id      TEXT NOT NULL,                  -- NetSuite customer internalid (invoicing + $0 SOs). Skriva=10496
    ns_supplier_id      TEXT,                           -- NetSuite vendor internalid ($0 POs, VRMA picks). Skriva=10503
    ns_location_id      TEXT,                           -- 3PL location internalid. Mova=49; Skriva=2 (Auckland, shared)
    ns_class_id         TEXT,                           -- NetSuite classification id = "brand". Mova=253; Skriva=236
    ns_subsidiary_id    TEXT,                           -- 2=MacGear AU (Mova), 3=MacGear NZ (Skriva)
    brand_label         TEXT,                           -- display label, e.g. 'Mova 3PL'
    location_label      TEXT,                           -- display label, e.g. '3PL Warehouse · Melbourne'
    -- TRUE when the bill-to record also carries non-3PL trade (Mova invoices to Spacewalker
    -- HK 11066): the invoice read is then narrowed to invoices containing a charge_item.
    invoice_items_only  BOOLEAN NOT NULL DEFAULT FALSE,
    active              BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE item (
    id                  SERIAL PRIMARY KEY,
    customer_id         INTEGER NOT NULL REFERENCES customer(id),
    ns_item_id          TEXT NOT NULL,
    sku                 TEXT NOT NULL,                  -- e.g. 'S-STYCASE-WHITE'
    description         TEXT,
    units_per_pallet    INTEGER,                        -- drives storage pallet calc
    UNIQUE (customer_id, ns_item_id)
);

-- Effective-dated rate card so historic billing runs reprice correctly.
CREATE TABLE rate_card (
    id                  SERIAL PRIMARY KEY,
    customer_id         INTEGER NOT NULL REFERENCES customer(id),
    effective_from      DATE NOT NULL,
    effective_to        DATE,                           -- NULL = current
    UNIQUE (customer_id, effective_from)
);

-- charge_type is the stable key the billing engine switches on.
CREATE TABLE rate_card_line (
    id                  SERIAL PRIMARY KEY,
    rate_card_id        INTEGER NOT NULL REFERENCES rate_card(id) ON DELETE CASCADE,
    charge_type         TEXT NOT NULL,                  -- 'container_unload' | 'putaway' | 'storage' | 'picking_so' | 'picking_vrma' | 'shipping'
    label               TEXT NOT NULL,                  -- human label shown in portal rate-card view
    rate                NUMERIC(12,2) NOT NULL,
    basis               TEXT NOT NULL,                  -- 'per_container' | 'per_unit' | 'per_pallet_week'
    UNIQUE (rate_card_id, charge_type)
);

-- ---------------------------------------------------------------------------
-- Cached NetSuite transactions (mirror; upsert on ns_*_id)
-- ---------------------------------------------------------------------------

-- View 1: Stock on order (open POs)
CREATE TABLE purchase_order (
    id                  SERIAL PRIMARY KEY,
    customer_id         INTEGER NOT NULL REFERENCES customer(id),
    ns_po_id            TEXT NOT NULL UNIQUE,
    tranid              TEXT,                           -- document number
    trandate            DATE,
    status              TEXT,                           -- open / partially received / closed
    ns_lastmodified     TIMESTAMPTZ,
    synced_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE po_line (
    id                  SERIAL PRIMARY KEY,
    purchase_order_id   INTEGER NOT NULL REFERENCES purchase_order(id) ON DELETE CASCADE,
    ns_item_id          TEXT NOT NULL,
    qty_ordered         NUMERIC(14,2),
    qty_received        NUMERIC(14,2),
    expected_date       DATE
);

-- Billing: container unload. One inbound shipment == one container (validate).
CREATE TABLE inbound_shipment (
    id                  SERIAL PRIMARY KEY,
    customer_id         INTEGER NOT NULL REFERENCES customer(id),
    ns_shipment_id      TEXT NOT NULL UNIQUE,
    shipment_number     TEXT,
    container_type      TEXT,                           -- e.g. '40ft loose stacked' -> drives unload rate
    received_date       DATE,                           -- NULL until received
    status              TEXT,
    ns_lastmodified     TIMESTAMPTZ,
    synced_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- View 2 + billing putaway: item receipts
CREATE TABLE item_receipt (
    id                  SERIAL PRIMARY KEY,
    customer_id         INTEGER NOT NULL REFERENCES customer(id),
    ns_receipt_id       TEXT NOT NULL UNIQUE,
    tranid              TEXT,
    trandate            DATE,
    ns_inbound_shipment TEXT,                           -- link back to inbound_shipment if present
    po_tranid           TEXT,                           -- source PO doc number (via previoustransactionlinelink)
    ns_lastmodified     TIMESTAMPTZ,
    synced_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE item_receipt_line (
    id                  SERIAL PRIMARY KEY,
    item_receipt_id     INTEGER NOT NULL REFERENCES item_receipt(id) ON DELETE CASCADE,
    ns_item_id          TEXT NOT NULL,
    qty                 NUMERIC(14,2) NOT NULL
);

-- View 3 + billing storage: stock-on-hand snapshots (one row per item per day).
-- Today's row is overwritten in place by the ~15-min SOH sync (near-live view); older
-- days persist as daily history that billing averages into pallet-weeks. synced_at is the
-- "live as at" time. Items that drop to zero are written as qty 0 (replace semantics), so
-- a shipped-out SKU doesn't show its last non-zero qty forever.
CREATE TABLE stock_on_hand (
    id                  SERIAL PRIMARY KEY,
    customer_id         INTEGER NOT NULL REFERENCES customer(id),
    snapshot_date       DATE NOT NULL,
    ns_item_id          TEXT NOT NULL,
    qty_on_hand         NUMERIC(14,2) NOT NULL,
    units_per_pallet    INTEGER,                        -- snapshotted so historic pallet calc is stable
    pallets             NUMERIC(14,2),                  -- ceil(qty_on_hand / units_per_pallet), computed at sync
    synced_at           TIMESTAMP,                      -- when this row was last written by a sync
    UNIQUE (customer_id, snapshot_date, ns_item_id)
);

-- View 4 + billing picking: item fulfilments (SO and VRMA)
CREATE TABLE item_fulfilment (
    id                  SERIAL PRIMARY KEY,
    customer_id         INTEGER NOT NULL REFERENCES customer(id),
    ns_fulfilment_id    TEXT NOT NULL UNIQUE,
    tranid              TEXT,
    trandate            DATE,
    source_type         TEXT NOT NULL,                  -- 'SO' (customer dispatch) | 'VRMA' (Macgear buy-in)
    ns_source_id        TEXT,                           -- internalid of the originating SO or VRMA
    ns_lastmodified     TIMESTAMPTZ,
    synced_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE item_fulfilment_line (
    id                  SERIAL PRIMARY KEY,
    item_fulfilment_id  INTEGER NOT NULL REFERENCES item_fulfilment(id) ON DELETE CASCADE,
    ns_item_id          TEXT NOT NULL,
    qty                 NUMERIC(14,2) NOT NULL
);

-- View 5: invoices (3PL service charges raised on the customer)
CREATE TABLE invoice (
    id                  SERIAL PRIMARY KEY,
    customer_id         INTEGER NOT NULL REFERENCES customer(id),
    ns_invoice_id       TEXT NOT NULL UNIQUE,
    tranid              TEXT,
    trandate            DATE,
    status              TEXT,                           -- open / paid
    total               NUMERIC(14,2),
    -- Transaction currency. The push never sets it (NetSuite sources it from the customer
    -- record), so this is the only way a mismatch with the AUD rate card becomes visible.
    currency            TEXT,
    amount_remaining    NUMERIC(14,2),                  -- foreignamountunpaid
    due_date            DATE,
    -- NetSuite memo. The portal stamps "3PL charges <from>-<to>" on every draft it creates —
    -- the only field on the record saying which WEEK is billed (trandate is when it was
    -- raised). Fallback period source for invoices with no billing_run linked.
    memo                TEXT,
    -- Period billed, assigned BY HAND in the portal. PORTAL-OWNED: the sync never writes
    -- these, so an assignment survives every re-sync. Needed because an invoice raised
    -- manually in NetSuite carries its period nowhere — trandate is the raise date and may be
    -- deliberately backdated to fall inside payment terms.
    period_start        DATE,
    period_end          DATE,
    ns_lastmodified     TIMESTAMPTZ,
    synced_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE invoice_line (
    id                  SERIAL PRIMARY KEY,
    invoice_id          INTEGER NOT NULL REFERENCES invoice(id) ON DELETE CASCADE,
    -- The NetSuite item. Descriptions are free text and get edited in NetSuite, so the item
    -- is what matches a line back to the billing line that raised it (service.run_variance).
    ns_item_id          TEXT,
    charge_type         TEXT,                           -- derived from ns_item_id via charge_item
    description         TEXT,
    qty                 NUMERIC(14,2),
    rate                NUMERIC(12,2),
    amount              NUMERIC(14,2)
);

-- ---------------------------------------------------------------------------
-- Billing automation (phase 2)
-- ---------------------------------------------------------------------------

CREATE TABLE billing_run (
    id                  SERIAL PRIMARY KEY,
    customer_id         INTEGER NOT NULL REFERENCES customer(id),
    period_start        DATE NOT NULL,
    period_end          DATE NOT NULL,
    status              TEXT NOT NULL DEFAULT 'draft',  -- draft | ready_to_push | pushed | invoiced
    ns_invoice_id       TEXT,                           -- set once a draft invoice is created in NetSuite
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Explicit period close, independent of status. A locked run can still be queued and pushed,
    -- but its lines are frozen: neither a re-save nor the scheduled auto-generate will recompute
    -- them. NULL = open draft. period_start/period_end are always a whole Mon-Sun week.
    locked_at           TIMESTAMPTZ,
    locked_by           TEXT,                           -- user email that closed the period
    -- Set the first time a human edits the draft's lines; blocks a recompute from silently
    -- discarding those edits (main._recompute_blocked).
    edited_at           TIMESTAMPTZ,
    edited_by           TEXT,
    -- Written by the invoice sync when the pushed invoice needs a human (it has vanished
    -- from NetSuite). Never acted on automatically — a run's status is not rolled back.
    sync_note           TEXT,
    UNIQUE (customer_id, period_start, period_end)
);
CREATE TABLE billing_line (
    id                  SERIAL PRIMARY KEY,
    billing_run_id      INTEGER NOT NULL REFERENCES billing_run(id) ON DELETE CASCADE,
    charge_type         TEXT NOT NULL,
    description         TEXT,
    qty                 NUMERIC(14,2),
    rate                NUMERIC(12,2),
    amount              NUMERIC(14,2),
    source_refs         JSONB,                          -- audit: which receipts/fulfilments/snapshots fed this line
    -- Draft editing. qty/rate/amount are what will be invoiced; computed_* are what the rate
    -- card produced and are never overwritten by an edit, so the difference stays auditable.
    -- origin: 'computed' | 'edited' | 'manual' | 'removed'. A removed COMPUTED line is kept
    -- at amount 0 rather than deleted — a charge that silently vanishes is the exact failure
    -- this module exists to prevent.
    origin              TEXT NOT NULL DEFAULT 'computed',
    computed_qty        NUMERIC(14,2),
    computed_rate       NUMERIC(12,2),
    computed_amount     NUMERIC(14,2),
    ns_item_id          TEXT                            -- NetSuite item this line invoices against
);

-- The NetSuite service items a 3PL invoice line can be raised against (the `3PL - *` set).
-- Replaced the CHARGE_ITEMS map hardcoded in the n8n Code node, whose ids were sandbox-only.
-- charge_type non-NULL = the automated charge this item invoices; NULL = ad-hoc lines only.
-- Global, not per-customer: these are Macgear's own items, one set per NetSuite account.
CREATE TABLE charge_item (
    id                  SERIAL PRIMARY KEY,
    ns_item_id          TEXT NOT NULL UNIQUE,
    name                TEXT NOT NULL,                  -- e.g. '3PL - Putaway Fee'
    charge_type         TEXT,                           -- unique among non-NULL values
    default_rate        NUMERIC(12,2),
    active              BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order          INTEGER NOT NULL DEFAULT 100
);
CREATE UNIQUE INDEX charge_item_charge_type_uq ON charge_item (charge_type)
    WHERE charge_type IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Sync bookkeeping
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- Users / auth (multi-tenant: customer users scoped to one customer_id)
-- ---------------------------------------------------------------------------
CREATE TABLE app_user (              -- "user" is reserved in Postgres
    id                  SERIAL PRIMARY KEY,
    email               TEXT NOT NULL UNIQUE,
    password_hash       TEXT NOT NULL,                  -- pbkdf2_sha256$iter$salt$hash
    role                TEXT NOT NULL DEFAULT 'customer',-- admin | internal | customer
    customer_id         INTEGER REFERENCES customer(id),-- NULL for internal/admin
    allowed_views       TEXT,                           -- JSON list of view keys; NULL = role default
    active              BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login          TIMESTAMPTZ,
    reset_token_hash    TEXT,                           -- SHA-256 of a single-use reset/set-password token
    reset_expires_at    TIMESTAMPTZ                     -- NULL when no active token
);

CREATE TABLE sync_log (
    id                  SERIAL PRIMARY KEY,
    entity              TEXT NOT NULL,                  -- 'purchase_order', 'item_receipt', ...
    customer_id         INTEGER REFERENCES customer(id),
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ,
    watermark           TIMESTAMPTZ,                    -- max ns_lastmodified seen
    rows_upserted       INTEGER,
    status              TEXT,                           -- ok | error
    error               TEXT
);

CREATE INDEX idx_po_customer            ON purchase_order(customer_id, status);
CREATE INDEX idx_receipt_customer_date  ON item_receipt(customer_id, trandate);
CREATE INDEX idx_fulfil_customer_date   ON item_fulfilment(customer_id, trandate, source_type);
CREATE INDEX idx_soh_customer_date      ON stock_on_hand(customer_id, snapshot_date);
CREATE INDEX idx_invoice_customer_date  ON invoice(customer_id, trandate);
