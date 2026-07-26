// n8n Code node — 3PL Portal NetSuite sync (mode: "Run Once for All Items").
// Signs NetSuite Token-Based Auth itself (OAuth1, HMAC-SHA256), calls the RESTlet
// (netsuite/3pl_restlet.js), and talks to the app's token-authed endpoints. No app/AI/MCP
// touches NetSuite — this node + the RESTlet are the only NetSuite communication.
//
// Two schedules feed this same node (mode comes from a Set node in front of each):
//   FAST lane — Schedule Trigger (every 15 min) -> Set {mode:"soh"} -> this node.
//               Pulls only stock_on_hand and skips the billing-push writes, so the portal's
//               SOH view stays near-live without re-pulling invoices/POs/etc 96x/day.
//   FULL lane — Schedule Trigger (daily + the weekly billing window) -> Set {mode:"full"}
//               (or no Set node at all) -> this node. Pulls all 6 entities and does the
//               draft-invoice writes, exactly as before.
// With no Set node / no mode, it defaults to "full" so existing single-schedule wiring is unchanged.

const crypto = require('crypto');

// ---------- CONFIG ----------
// PREFER ENVIRONMENT VARIABLES FOR THE SECRETS. A Code node's body is stored in the n8n
// workflow record and travels in every workflow export/backup as PLAIN TEXT — unlike an n8n
// credential, it is not encrypted with N8N_ENCRYPTION_KEY. Pasting live NetSuite keys inline
// therefore leaks them to anyone who can read the workflow or its backups.
// Set these on the n8n container instead (see docs/production_cutover.md §3):
//   NS_ACCOUNT_ID NS_CONSUMER_KEY NS_CONSUMER_SECRET NS_TOKEN_ID NS_TOKEN_SECRET
//   NS_RESTLET_SCRIPT NS_RESTLET_DEPLOY THREEPL_SYNC_TOKEN
// env() falls back to the inline value when a var is unset, or when $env access is blocked
// (N8N_BLOCK_ENV_ACCESS_IN_NODE=true) — so existing inline wiring keeps working unchanged.
//
// To move to PRODUCTION, swap only the NetSuite-side values: ACCOUNT_ID (drop _SB1), the 4 TBA
// secrets (prod integration+token), RESTLET_SCRIPT/DEPLOY (deploy the RESTlet in prod), and the
// CHARGE_ITEMS ids (items created in prod will have different internalids). App side is unchanged.
function env(name, fallback) {
  try {
    var v = $env[name];
    return (v === undefined || v === null || v === '') ? fallback : String(v);
  } catch (e) { return fallback; }   // $env access blocked by n8n config
}
const ACCOUNT_ID      = env('NS_ACCOUNT_ID', '840974_SB1');   // host = lowercase, _ -> -; realm = as-is
const CONSUMER_KEY    = env('NS_CONSUMER_KEY',    'REPLACE_CONSUMER_KEY');
const CONSUMER_SECRET = env('NS_CONSUMER_SECRET', 'REPLACE_CONSUMER_SECRET');
const TOKEN_ID        = env('NS_TOKEN_ID',        'REPLACE_TOKEN_ID');
const TOKEN_SECRET    = env('NS_TOKEN_SECRET',    'REPLACE_TOKEN_SECRET');
const RESTLET_SCRIPT  = env('NS_RESTLET_SCRIPT', '1343');     // "script=" in the RESTlet deploy URL
const RESTLET_DEPLOY  = env('NS_RESTLET_DEPLOY', '1');        // "deploy=" in the RESTlet deploy URL
const APP_BASE        = 'http://threepl:8000';       // app on the shared Docker network (n8n-docker-caddy_default)
const SYNC_TOKEN      = env('THREEPL_SYNC_TOKEN', 'REPLACE_SYNC_TOKEN');  // == SYNC_TOKEN in the app's .env
const SINCE           = '2025-01-01';                // incremental floor for dated reads
// Fail loudly rather than firing unsigned/mis-signed requests at NetSuite all night.
for (const [k, v] of Object.entries({ CONSUMER_KEY, CONSUMER_SECRET, TOKEN_ID, TOKEN_SECRET, SYNC_TOKEN })) {
  if (String(v).indexOf('REPLACE_') === 0) {
    throw new Error(k + ' is not configured — set the matching env var on the n8n container '
                      + '(or fill the inline fallback). See docs/production_cutover.md §3.');
  }
}

// Customers to sync are NOT hardcoded here — they're fetched from the app's
// /admin/sync-config (managed in the admin console: Customers > + Add customer).
// Add/edit a customer there and the next run picks it up; no node edit needed.
// Mode from the upstream Set node: "soh" = fast lane (SOH only, no writes); anything
// else (incl. missing) = full lane. Read defensively so the node also works standalone.
let MODE = 'full';
try { const inp = $input.first(); if (inp && inp.json && inp.json.mode) MODE = String(inp.json.mode); } catch (e) {}
const FAST = MODE === 'soh';
// inbound_shipments runs AFTER purchase_orders: it stamps the PO->shipment link onto PO lines
// (which the purchase_orders ingest rebuilds each run), so the order matters.
const READ_ENTITIES = FAST
  ? ['stock_on_hand']
  : ['items', 'invoices', 'purchase_orders', 'inbound_shipments', 'item_receipts',
     'item_fulfilments', 'stock_on_hand'];
const DO_WRITES = !FAST;   // billing draft-invoice push runs on the full lane only
// charge_type -> NetSuite item internalid (for draft-invoice lines on push). Sandbox items.
const CHARGE_ITEMS = { container_unload: '55070', putaway: '55071',
                       storage: '55072', picking_so: '55073', picking_vrma: '55074' };
// units-per-pallet custom item field id; blank = don't pull it. Needed for storage pallet/charge calc.
const UPP_FIELD = 'custitem_pallet_quantity';
// --------------------------------------------------------------------------------------------

const host = ACCOUNT_ID.toLowerCase().replace(/_/g, '-');
const RESTLET_BASE = `https://${host}.restlets.api.netsuite.com/app/site/hosting/restlet.nl`;
const helpers = this.helpers;
const pct = (s) => encodeURIComponent(String(s)).replace(/[!*'()]/g,
  (c) => '%' + c.charCodeAt(0).toString(16).toUpperCase());

function authHeader(method, baseUrl, queryParams) {
  const oauth = {
    oauth_consumer_key: CONSUMER_KEY, oauth_token: TOKEN_ID,
    oauth_signature_method: 'HMAC-SHA256',
    oauth_timestamp: Math.floor(Date.now() / 1000).toString(),
    oauth_nonce: crypto.randomBytes(16).toString('hex'), oauth_version: '1.0',
  };
  const all = Object.assign({}, queryParams, oauth);
  const paramStr = Object.keys(all).sort().map((k) => pct(k) + '=' + pct(all[k])).join('&');
  const base = [method.toUpperCase(), pct(baseUrl), pct(paramStr)].join('&');
  const signingKey = pct(CONSUMER_SECRET) + '&' + pct(TOKEN_SECRET);
  const signature = crypto.createHmac('sha256', signingKey).update(base).digest('base64');
  const headerParams = Object.assign({}, oauth, { oauth_signature: signature });
  return 'OAuth realm="' + ACCOUNT_ID + '", ' + Object.keys(headerParams).sort()
    .map((k) => pct(k) + '="' + pct(headerParams[k]) + '"').join(', ');
}

// Call the RESTlet (POST + JSON body; only script/deploy are signed query params).
async function restlet(body) {
  const q = { script: RESTLET_SCRIPT, deploy: RESTLET_DEPLOY };
  const url = RESTLET_BASE + '?' + Object.keys(q).map((k) => pct(k) + '=' + pct(q[k])).join('&');
  const resp = await helpers.httpRequest({
    method: 'POST', url, body,
    headers: { Authorization: authHeader('POST', RESTLET_BASE, q), 'Content-Type': 'application/json' },
    json: true, returnFullResponse: true, ignoreHttpStatusErrors: true,
  });
  if (resp.statusCode !== 200 || !resp.body || resp.body.error) {
    throw new Error('RESTlet ' + body.action + ': ' + JSON.stringify(resp.body));
  }
  return resp.body.data;
}

const appHeaders = { 'X-Sync-Token': SYNC_TOKEN, 'Content-Type': 'application/json' };
const out = [];

// Customer list (NetSuite ids per customer) comes from the app, so adding a customer
// in the admin console is all it takes to start syncing them. Only customers with a
// brand class are returned (the reads are class-scoped).
const cfg = await helpers.httpRequest({
  method: 'GET', url: `${APP_BASE}/admin/sync-config`, headers: appHeaders, json: true });
const CUSTOMERS = cfg.customers || [];

// 1) READS: pull each entity per customer and push to /admin/ingest
const readFailures = [];   // {customer, entity} for each read that errored this run
for (const c of CUSTOMERS) {
  for (const entity of READ_ENTITIES) {
    try {
      const params = Object.assign({ action: entity, since: SINCE }, c);
      if (entity === 'items' && UPP_FIELD) params.upp_field = UPP_FIELD;
      const rows = await restlet(params);
      const r = await helpers.httpRequest({
        method: 'POST', url: `${APP_BASE}/admin/ingest`, headers: appHeaders,
        body: { customer: c.slug, entity, rows }, json: true });
      out.push({ json: r });
    } catch (e) {
      readFailures.push({ customer: c.slug, entity });
      out.push({ json: { step: 'read', level: 'error', customer: c.slug, entity, error: String(e.message || e) } });
    }
  }
}

// The Stock-on-order shipment number + expected-receipt date are stamped onto PO lines by the
// inbound_shipments read. If purchase_orders synced but inbound_shipments failed for a customer,
// those columns won't refresh this run (the app now carries forward the last stamped values so
// they don't blank out — but a persistent failure means they go stale). Surface it loudly so it
// doesn't stay invisible: this summary item is easy to alert on downstream (level:'warn').
for (const c of CUSTOMERS) {
  const poFailed = readFailures.some((f) => f.customer === c.slug && f.entity === 'purchase_orders');
  const shipFailed = readFailures.some((f) => f.customer === c.slug && f.entity === 'inbound_shipments');
  if (shipFailed && !poFailed) {
    out.push({ json: { step: 'summary', level: 'warn', customer: c.slug,
      warning: 'inbound_shipments read failed while purchase_orders succeeded — Stock-on-order '
             + 'shipment/expected-receipt columns will not refresh until the next successful run.' } });
  }
}

// 2) WRITES: create a draft invoice for each queued billing run, then report it back.
//    Full lane only — the 15-min SOH lane must not poll/push billing.
if (DO_WRITES) try {
  const pending = (await helpers.httpRequest({
    method: 'GET', url: `${APP_BASE}/admin/billing/pending`, headers: appHeaders, json: true })).pending || [];
  for (const run of pending) {
    try {
      const lines = run.lines.map((l) => ({ item_id: CHARGE_ITEMS[l.charge_type],
        description: l.description, qty: l.qty, rate: l.rate }));
      const res = await restlet({ action: 'create_invoice', ns_customer_id: run.ns_customer_id,
        ns_subsidiary_id: run.ns_subsidiary_id, ns_location_id: run.ns_location_id,
        memo: `3PL charges ${run.period_start}–${run.period_end}`, lines });
      const back = await helpers.httpRequest({
        method: 'POST', url: `${APP_BASE}/admin/billing/pushed`, headers: appHeaders,
        body: { run_id: run.run_id, ns_invoice_id: res.ns_invoice_id }, json: true });
      out.push({ json: back });
    } catch (e) {
      out.push({ json: { step: 'push', run_id: run.run_id, error: String(e.message || e) } });
    }
  }
} catch (e) {
  out.push({ json: { step: 'pending', error: String(e.message || e) } });
}

return out;
