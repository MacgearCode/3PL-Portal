"""Billing engine — derive the weekly 3PL service charges from cached NetSuite data.

Replaces the manual weekly saved searches (brief, priority 2). Given a customer and a
period, computes one billing line per charge type off the active rate card:

  container_unload  count of containers first receipted in period       x per_container
  putaway           sum of item-receipt-line units in period            x per_unit
  storage           avg daily pallets x weeks in period                 x per_pallet_week
  picking_so        sum of SO-fulfilment units in period                x per_unit
  picking_vrma      sum of VRMA-fulfilment units in period              x per_unit

Pallets = ceil(qty_on_hand / units_per_pallet) per item, totalled per snapshot day; the
daily totals are averaged and scaled by the weeks the period spans (SOH is now a near-live
snapshot refreshed every ~15 min, so summing every snapshot would overcharge ~7x).
Fulfilment units are positive-only — the cache already stores positives, but we guard here too.

Anything that would silently UNDER-bill is collected into `BillingResult.warnings` and shown
on the preview. A charge quietly computing to zero is how 9 containers went missing for a
fortnight; a missing charge must be visible, not absent.

This module is pure: it reads the cache and returns a result. Persisting a BillingRun
and pushing a draft invoice to NetSuite are separate steps (service / netsuite layers).
"""
import json
import math
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .models import (Customer, InboundShipment, ItemFulfilment, ItemFulfilmentLine,
                     ItemReceipt, ItemReceiptLine, RateCard, RateCardLine, StockOnHand)


@dataclass
class ComputedLine:
    charge_type: str
    label: str
    qty: float
    rate: float
    amount: float
    basis: str
    source_refs: list = field(default_factory=list)


@dataclass
class BillingResult:
    customer_id: int
    period_start: date
    period_end: date
    lines: list  # list[ComputedLine]
    warnings: list = field(default_factory=list)  # under-billing risks, surfaced on the preview

    @property
    def total(self) -> float:
        return round(sum(l.amount for l in self.lines), 2)


def active_rate_card(db: Session, customer_id: int, on: date) -> RateCard | None:
    """The rate card effective on `on` for the customer (latest effective_from <= on)."""
    cards = db.scalars(
        select(RateCard).where(RateCard.customer_id == customer_id)
        .order_by(RateCard.effective_from.desc())).all()
    for c in cards:
        if c.effective_from <= on and (c.effective_to is None or c.effective_to >= on):
            return c
    return None


def _rates(card: RateCard) -> dict[str, RateCardLine]:
    return {l.charge_type: l for l in card.lines}


def _names(items: list, limit: int = 10) -> str:
    """Comma-list for a warning message, truncated so a systemic fault can't flood the page."""
    head = ", ".join(str(i) for i in items[:limit])
    return head if len(items) <= limit else f"{head} (+{len(items) - limit} more)"


def compute_billing(db: Session, customer: Customer, period_start: date, period_end: date,
                    warn: bool = True) -> BillingResult:
    """Compute the period's charges. `warn=False` skips the under-billing checks and their
    queries — for the overview chart, which calls this 5x a page load and only reads totals.
    Never pass warn=False anywhere a human is deciding whether to invoice."""
    card = active_rate_card(db, customer.id, period_end)
    if card is None:
        return BillingResult(customer.id, period_start, period_end, [])
    rates = _rates(card)
    lines: list[ComputedLine] = []
    warnings: list[str] = []

    def add(charge_type: str, qty: float, refs: list):
        rc = rates.get(charge_type)
        if rc is None or qty == 0:
            return
        amount = round(qty * float(rc.rate), 2)
        lines.append(ComputedLine(charge_type, rc.label, qty, float(rc.rate),
                                  amount, rc.basis, refs))

    # --- container unload: containers first receipted in period ---------------
    # Dated by the ITEM RECEIPT's trandate, NOT the shipment header.
    #
    # Why: NetSuite leaves `inboundshipment.actualdeliverydate` NULL on every Mova 3PL
    # shipment (all 36, verified in production 2026-07-31), so the RESTlet's fallback to
    # `lastmodifieddate` was dating the charge to whenever someone last *touched* the record.
    # For the 9 containers unloaded 2026-07-20 that was 2026-07-30 10:04-10:08 — a manual bulk
    # pass ten days and one month-boundary later. Result: the 20-26 Jul week billed 0 containers
    # and the following week billed 22. The receipt trandate is the physical unload and, unlike
    # lastmodifieddate, does not move when the record is edited, so the charge is idempotent.
    #
    # A container is charged in the week of its EARLIEST receipt (MIN over ALL history, not just
    # this period), so one split across two receipts in different periods is billed once, in the
    # first — never twice.
    first_receipt = db.execute(
        select(ItemReceipt.ns_inbound_shipment, func.min(ItemReceipt.trandate))
        .where(ItemReceipt.customer_id == customer.id,
               ItemReceipt.ns_inbound_shipment != None,                 # noqa: E711
               ItemReceipt.trandate != None)                            # noqa: E711
        .group_by(ItemReceipt.ns_inbound_shipment)).all()
    containers = sorted(
        ship for ship, first in first_receipt if period_start <= first <= period_end)
    add("container_unload", len(containers), containers)

    # --- putaway: item-receipt units in period --------------------------------
    # selectinload: the line walk below is otherwise one query per receipt, and the overview
    # runs compute_billing five times per page load. Loading strategy only — same arithmetic.
    receipts = db.scalars(
        select(ItemReceipt).where(
            ItemReceipt.customer_id == customer.id,
            ItemReceipt.trandate >= period_start,
            ItemReceipt.trandate <= period_end)
        .options(selectinload(ItemReceipt.lines))).all()
    putaway_units = sum(float(l.qty) for r in receipts for l in r.lines)
    add("putaway", putaway_units, [r.tranid or r.ns_receipt_id for r in receipts])

    if warn:
        # `ns_inbound_shipment` used to be a purely cosmetic column on the receipts view, fetched
        # by the RESTlet in a try/catch that blanks it rather than losing the receipts
        # (3pl_restlet.js). The container charge above now depends on it, so a blank is no longer
        # harmless: it silently drops $1,500 a container. Surface it instead of absorbing it.
        unlinked = [r.tranid or r.ns_receipt_id for r in receipts if not r.ns_inbound_shipment]
        if unlinked:
            warnings.append(
                f"{len(unlinked)} receipt(s) in this period are not linked to a container, so no "
                f"container-unload charge was raised for them: {_names(unlinked)}. Check the "
                f"inbound shipment on the receipt in NetSuite.")

        # Containers NetSuite says have landed but which no receipt points at — they will never be
        # charged in any period. Not scoped to this period: it's a standing under-bill either way.
        # `received_date`'s VALUE is untrusted (see the container block), but its PRESENCE is a
        # reliable "status is received/partiallyReceived" flag, which is all this needs.
        receipted = {ship for ship, _ in first_receipt}
        landed = db.scalars(
            select(InboundShipment).where(
                InboundShipment.customer_id == customer.id,
                InboundShipment.received_date != None)).all()            # noqa: E711
        stranded = sorted(s.shipment_number or s.ns_shipment_id for s in landed
                          if (s.shipment_number or s.ns_shipment_id) not in receipted)
        if stranded:
            warnings.append(
                f"{len(stranded)} container(s) are marked received in NetSuite but have no item "
                f"receipt, so they are not billable in any period: {_names(stranded)}.")

    # --- storage: average daily pallets x weeks in period ---------------------
    # SOH is now snapshotted often (every ~15 min, collapsed to one row per item per
    # day by the unique key), so we can't just sum every snapshot — that would bill
    # one pallet-week per *day* (~7x). Instead: total pallets per distinct day, average
    # those daily totals, then scale by the number of weeks the period spans. With a
    # single snapshot in the period this degrades to "that reading held all period",
    # matching the old weekly-snapshot behaviour. (docs/data_model.md: avg daily pallets.)
    snaps = db.scalars(
        select(StockOnHand).where(
            StockOnHand.customer_id == customer.id,
            StockOnHand.snapshot_date >= period_start,
            StockOnHand.snapshot_date <= period_end)).all()
    daily_pallets: dict[date, float] = {}
    for s in snaps:
        if s.pallets is not None:
            pallets = float(s.pallets)
        elif s.units_per_pallet:
            pallets = math.ceil(float(s.qty_on_hand) / s.units_per_pallet)
        else:
            pallets = 0.0
        daily_pallets[s.snapshot_date] = daily_pallets.get(s.snapshot_date, 0.0) + pallets
    if daily_pallets:
        avg_daily = sum(daily_pallets.values()) / len(daily_pallets)
        weeks = ((period_end - period_start).days + 1) / 7.0
        pallet_weeks = round(avg_daily * weeks, 2)
        snap_refs = [f"avg {round(avg_daily, 2)} pallets/day over {len(daily_pallets)} "
                     f"snapshot day(s) x {round(weeks, 3)} week(s)",
                     *sorted(d.isoformat() for d in daily_pallets)]
        add("storage", pallet_weeks, snap_refs)
    elif warn:
        # No snapshot inside the period — storage silently computes to nothing. True for any week
        # before the sync started (production's first snapshot is 2026-07-27), so the week the
        # stock actually landed bills no storage at all unless a snapshot is backfilled.
        warnings.append(
            "No stock-on-hand snapshot exists inside this period, so no storage was charged. "
            "Storage cannot be computed for a week the sync did not cover.")

    # --- picking: SO and VRMA fulfilment units in period ----------------------
    for charge_type, source in (("picking_so", "SO"), ("picking_vrma", "VRMA")):
        fulfils = db.scalars(
            select(ItemFulfilment).where(
                ItemFulfilment.customer_id == customer.id,
                ItemFulfilment.source_type == source,
                ItemFulfilment.trandate >= period_start,
                ItemFulfilment.trandate <= period_end)
            .options(selectinload(ItemFulfilment.lines))).all()
        units = sum(max(0.0, float(l.qty)) for f in fulfils for l in f.lines)
        add(charge_type, units, [f.tranid or f.ns_fulfilment_id for f in fulfils])

    return BillingResult(customer.id, period_start, period_end, lines, warnings)


def result_to_run_kwargs(res: BillingResult) -> list[dict]:
    """Shape ComputedLines into BillingLine column dicts (source_refs -> JSON)."""
    return [{
        "charge_type": l.charge_type, "description": l.label, "qty": l.qty,
        "rate": l.rate, "amount": l.amount, "source_refs": json.dumps(l.source_refs),
    } for l in res.lines]
