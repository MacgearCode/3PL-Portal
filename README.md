# Macgear 3PL Portal

Customer visibility portal + weekly billing automation for Macgear's 3PL service
(receive / store / dispatch stock Macgear doesn't own, charge handling & storage fees).
NetSuite-backed, multi-tenant. First customer: **Mova**. Reference: **Skriva**.

**Status: LIVE on NetSuite production since 2026-07-27** — all 6 visibility views are syncing real
data. The billing write path (auto-generated draft invoice → manual push) is the remaining work;
see the *Billing roadmap* in `CLAUDE.md`.

| Doc | What's in it |
|---|---|
| `CLAUDE.md` | business brief, current status, billing roadmap, UI/brand, hard-won NetSuite gotchas |
| `docs/netsuite_validation.md` | the **validated** SuiteQL — source of truth for field names and joins |
| `docs/netsuite_integration.md` | n8n + RESTlet architecture and endpoint contracts |
| `docs/production_cutover.md` | the production cutover record: verified ids, TBA/role setup, cache purge, what went wrong |
| `docs/data_model.md` | schema + sync design (v1) |
| `docs/deploy.md` | droplet + sandbox deploy walkthrough |

## Run locally
```powershell
./run.ps1
```
Opens http://127.0.0.1:8000. First run creates a SQLite DB and seeds the two customers,
Mova's rate card, a month of demo cache data so all views render, and the dev logins below.
Auth is always on — sign in as `admin@macgeargroup.com` / `admin123` (CHANGE for any real use).

**Password reset / set-password:** users recover access via a self-service **Forgot password** link
on the sign-in page; new users created in the admin console get a **set-password** link instead of a
typed password. The link is single-use and expires (`RESET_TOKEN_TTL_MIN`). The admin console **shows
the link for the admin to copy** and send however they like — no email setup required. Optionally, if
`N8N_RESET_WEBHOOK_URL` is set the link is also emailed via an n8n webhook; unset = link is only shown
in the UI and printed to the console.

## Layout
```
app/
  main.py        FastAPI app: auth, customer switcher, 6 views, billing run, admin console,
                 token-authed /admin/ingest + /admin/billing/* (n8n)
  models.py      ORM (mirrors db/01_schema.sql) — the read cache + billing tables
  service.py     read-side: cache -> the 6 portal views + overview tiles
  billing.py     billing engine: cache + rate card -> the 5 weekly service charges
  netsuite.py    ingest layer: upserts rows n8n pushes in (the app never calls NetSuite)
  perms.py       roles + per-user view permissions
  security.py    pbkdf2 password hashing + signed-cookie sessions + reset-token helpers
  notify.py      send_reset_email: POSTs the reset link to an n8n webhook (or logs it locally)
  seed.py        seeds Mova/Skriva + rate cards + users (+ demo cache unless SEED_DEMO=0)
  templates/ static/   server-rendered portal UI
                 _rows.html = table-row macros shared by the full page and the Load more
                 fetch (_rows_partial.html), so row markup exists once
db/01_schema.sql  canonical Postgres DDL (v1, validated columns)
netsuite/      3pl_restlet.js (deployed in NetSuite) + n8n_3pl_sync.js (n8n Code node)
```

## NetSuite integration (app never calls NetSuite)
All NetSuite comms are server-to-server: **n8n signs Token-Based Auth → a RESTlet** runs the
validated SuiteQL / creates draft invoices. The droplet app holds no NetSuite credentials and
runs no AI/MCP. See `docs/netsuite_integration.md` for the architecture and `docs/deploy.md` for
the sandbox-first deploy walkthrough.

## Deploy
Docker on the n8n droplet behind Caddy, `DATABASE_URL` -> Postgres. `docker compose up -d --build`
(see `docker-compose.yml`, `.env.example`, `docs/deploy.md`). A scheduled n8n Code node POSTs the
synced rows to `/admin/ingest` and drains the billing-push queue.

## Config (env)
| var | purpose |
|---|---|
| `DATABASE_URL` | Postgres URL on the droplet (default: local SQLite) |
| `APP_SECRET` | session-cookie signing key (set a long random one) |
| `SYNC_TOKEN` | shared secret for `/admin/ingest` + `/admin/billing/*` (must match the n8n node) |
| `PUBLIC_BASE_URL` | public origin used to build password-reset links (e.g. `https://3pl.macgeargroup.com`) |
| `N8N_RESET_WEBHOOK_URL` | n8n webhook that emails the reset link; unset = link logged to console (local dev) |
| `N8N_WEBHOOK_TOKEN` | secret sent as `X-Sync-Token` to that webhook (blank = reuse `SYNC_TOKEN`) |
| `RESET_TOKEN_TTL_MIN` | minutes a reset/set-password link stays valid (default 45) |
| `SEED_DEMO` | `0` on a real deploy (no fake cache rows); `1`/unset plants demo data locally |
| `PGPASSWORD` | bundled-Postgres password (compose); keep in sync with `DATABASE_URL` |
| `SHARED_NETWORK` | name of the Docker network shared with Caddy + n8n |

The app needs **no** NetSuite credentials — TBA creds live only in the n8n Code node.
