"""Roles and view-level permissions.

Roles:
  admin     — Macgear; full access incl. user management + rate-card settings.
  internal  — Macgear staff; sees all customers + billing run; no user management.
  customer  — locked to one customer_id; visibility views only (no billing run).

Each user's visible views default by role but can be overridden per user
(User.allowed_views, a JSON list). NULL/empty => use the role default.
"""
import json

# All view keys (must match main.NAV / TITLES).
VIEW_KEYS = ["overview", "stock_on_order", "item_receipts", "stock_on_hand",
             "fulfilments", "invoices", "rate_card", "storage_report", "billing"]
# Customers never see the billing run (Macgear-internal). `storage_report` is internal by
# DEFAULT but not forbidden — unlike billing it exposes nothing a customer shouldn't see (it
# explains their own storage charge), so it can be granted per user in /admin/users.
CUSTOMER_VIEWS = ["overview", "stock_on_order", "item_receipts", "stock_on_hand",
                  "fulfilments", "invoices", "rate_card"]
ROLE_DEFAULT_VIEWS = {"admin": VIEW_KEYS, "internal": VIEW_KEYS, "customer": CUSTOMER_VIEWS}
ROLES = ["admin", "internal", "customer"]


def role_default(role: str) -> list[str]:
    return list(ROLE_DEFAULT_VIEWS.get(role, CUSTOMER_VIEWS))


def effective_views(user) -> list[str]:
    """Resolved visible views: per-user override if set, else the role default."""
    if user.allowed_views:
        try:
            chosen = [v for v in json.loads(user.allowed_views) if v in VIEW_KEYS]
            if chosen:
                return chosen
        except Exception:
            pass
    return role_default(user.role)


def normalize_allowed(role: str, selected: list[str]) -> str | None:
    """Store NULL when the selection equals the role default (so defaults keep flowing),
    otherwise a JSON list of the chosen views (in canonical order)."""
    chosen = [v for v in VIEW_KEYS if v in set(selected)]
    # customers can never be granted billing
    if role == "customer":
        chosen = [v for v in chosen if v != "billing"]
    if chosen == role_default(role):
        return None
    return json.dumps(chosen)


def is_internal(user) -> bool:
    return user.role in ("admin", "internal")


def is_admin(user) -> bool:
    return user.role == "admin"
