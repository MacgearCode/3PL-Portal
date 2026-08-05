"""Database engine + session setup.

Local dev defaults to a file-based SQLite db (zero setup — just run.ps1).
On the droplet set DATABASE_URL to the managed Postgres, e.g.
    postgresql+psycopg2://user:pass@host:5432/threepl
The ORM models are portable; db/01_schema.sql is the canonical Postgres DDL.
"""
import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
os.makedirs(DATA_DIR, exist_ok=True)

DATABASE_URL = os.environ.get(
    "DATABASE_URL", f"sqlite:///{os.path.join(DATA_DIR, 'app.db')}")

# check_same_thread only matters for SQLite; harmless to branch on the scheme.
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, echo=False, future=True, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency that yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_columns():
    """Tiny idempotent migration (no Alembic): add columns introduced after a DB was first
    created. create_all() never alters existing tables, so without this newer columns (e.g.
    the live-SOH `synced_at`, the reset-token columns) would be missing on an existing
    SQLite/Postgres db. Both engines accept `ALTER TABLE ... ADD COLUMN`; we only add when
    absent. Lives here (not main.py) so `python -m app.seed` runs it BEFORE any query — the
    seed starts before the app imports, so migrating in main.py alone would crash the seed."""
    additions = {"stock_on_hand": {"synced_at": "TIMESTAMP"},
                 "item_receipt": {"po_tranid": "VARCHAR"},
                 "po_line": {"ns_inbound_shipment": "VARCHAR"},
                 "inbound_shipment": {"expected_date": "DATE", "container_no": "VARCHAR"},
                 "app_user": {"reset_token_hash": "VARCHAR", "reset_expires_at": "TIMESTAMP"},
                 # location_scoped: added when the regular-brand model landed (2026-07-22). Existing
                 # rows get NULL (read as False); the sync coerces bool(). BOOLEAN is portable to both
                 # SQLite (INTEGER affinity) and Postgres; no DEFAULT so Postgres doesn't reject `0`.
                 "customer": {"location_scoped": "BOOLEAN",
                              # Narrow the invoice read to 3PL charge items (2026-08-05) —
                              # needed once a customer bills to a record that also trades.
                              "invoice_items_only": "BOOLEAN"},
                 # Explicit period close (2026-08-01). NULL = still an open draft, recomputable.
                 "billing_run": {"locked_at": "TIMESTAMP", "locked_by": "VARCHAR",
                                 # Draft editing + invoice sync-back (2026-08-05).
                                 "edited_at": "TIMESTAMP", "edited_by": "VARCHAR",
                                 "sync_note": "TEXT"},
                 # Draft editing (2026-08-05). Rows written before this have origin NULL, which
                 # is treated as 'computed' by every reader (BillingLine.billable, the pending
                 # payload's `or "computed"`, and the edit route's origin test), so no backfill
                 # is required — but keep that true if you add a reader.
                 "billing_line": {"origin": "VARCHAR", "computed_qty": "NUMERIC(14,2)",
                                  "computed_rate": "NUMERIC(12,2)",
                                  "computed_amount": "NUMERIC(14,2)", "ns_item_id": "VARCHAR"},
                 # Richer invoice sync-back (2026-08-05).
                 "invoice": {"currency": "VARCHAR", "amount_remaining": "NUMERIC(14,2)",
                             "due_date": "DATE", "memo": "VARCHAR"},
                 "invoice_line": {"ns_item_id": "VARCHAR"}}
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, cols in additions.items():
            if not insp.has_table(table):
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in cols.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
