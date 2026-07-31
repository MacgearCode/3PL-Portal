"""ORM models for the 3PL portal cache + billing.

Mirrors db/01_schema.sql. All fact tables are keyed by customer_id (multi-tenant).
ns_* fields hold NetSuite internal ids / document numbers. "Brand" == NetSuite
classification id (ns_class_id), validated 2026-06-26 (see docs/netsuite_validation.md).
"""
from datetime import date, datetime

from sqlalchemy import (Boolean, Date, DateTime, ForeignKey, Integer, Numeric,
                        String, Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

# Stable charge-type keys the billing engine switches on.
CHARGE_TYPES = ("container_unload", "putaway", "storage", "picking_so",
                "picking_vrma", "shipping")


# --- config / dimensions -----------------------------------------------------
class Customer(Base):
    __tablename__ = "customer"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String, unique=True)          # 'mova', 'skriva'
    name: Mapped[str] = mapped_column(String)
    ns_customer_id: Mapped[str] = mapped_column(String)            # invoicing + $0 SOs
    ns_supplier_id: Mapped[str | None] = mapped_column(String, nullable=True)  # $0 POs, VRMA picks
    ns_location_id: Mapped[str | None] = mapped_column(String, nullable=True)  # 3PL location
    ns_class_id: Mapped[str | None] = mapped_column(String, nullable=True)     # "brand" classification
    ns_subsidiary_id: Mapped[str | None] = mapped_column(String, nullable=True)
    brand_label: Mapped[str | None] = mapped_column(String, nullable=True)     # display, e.g. 'MOVA'
    location_label: Mapped[str | None] = mapped_column(String, nullable=True)  # display, e.g. '3PL Warehouse · Melbourne'
    # True: 3PL stock lives in a dedicated location (ns_location_id) so NetSuite reads filter by
    # class AND location (e.g. Mova — its regular MOVA brand also covers Macgear-owned stock, so
    # brand alone would leak non-3PL activity). False: brand class is 3PL-exclusive and stock may
    # span locations, so scope by class only (e.g. Skriva — Auckland + Christchurch). See db.py.
    location_scoped: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    rate_cards: Mapped[list["RateCard"]] = relationship(back_populates="customer")


class Item(Base):
    __tablename__ = "item"
    __table_args__ = (UniqueConstraint("customer_id", "ns_item_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id"))
    ns_item_id: Mapped[str] = mapped_column(String)
    sku: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    units_per_pallet: Mapped[int | None] = mapped_column(Integer, nullable=True)


class RateCard(Base):
    __tablename__ = "rate_card"
    __table_args__ = (UniqueConstraint("customer_id", "effective_from"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id"))
    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    customer: Mapped["Customer"] = relationship(back_populates="rate_cards")
    lines: Mapped[list["RateCardLine"]] = relationship(
        back_populates="rate_card", cascade="all, delete-orphan")


class RateCardLine(Base):
    __tablename__ = "rate_card_line"
    __table_args__ = (UniqueConstraint("rate_card_id", "charge_type"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    rate_card_id: Mapped[int] = mapped_column(ForeignKey("rate_card.id"))
    charge_type: Mapped[str] = mapped_column(String)               # see CHARGE_TYPES
    label: Mapped[str] = mapped_column(String)
    rate: Mapped[float] = mapped_column(Numeric(12, 2))
    basis: Mapped[str] = mapped_column(String)                     # per_container|per_unit|per_pallet_week

    rate_card: Mapped["RateCard"] = relationship(back_populates="lines")


# --- cached NetSuite transactions --------------------------------------------
class PurchaseOrder(Base):
    __tablename__ = "purchase_order"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id"))
    ns_po_id: Mapped[str] = mapped_column(String, unique=True)
    tranid: Mapped[str | None] = mapped_column(String, nullable=True)
    trandate: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str | None] = mapped_column(String, nullable=True)
    ns_lastmodified: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    lines: Mapped[list["PoLine"]] = relationship(
        back_populates="po", cascade="all, delete-orphan")


class PoLine(Base):
    __tablename__ = "po_line"
    id: Mapped[int] = mapped_column(primary_key=True)
    purchase_order_id: Mapped[int] = mapped_column(ForeignKey("purchase_order.id"))
    ns_item_id: Mapped[str] = mapped_column(String)
    qty_ordered: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    qty_received: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    expected_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Inbound shipment (container) this line has been added to, if any — its doc number.
    # The shipment carries the authoritative expected-receipt date (InboundShipment.expected_date).
    ns_inbound_shipment: Mapped[str | None] = mapped_column(String, nullable=True)
    po: Mapped["PurchaseOrder"] = relationship(back_populates="lines")


class InboundShipment(Base):
    __tablename__ = "inbound_shipment"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id"))
    ns_shipment_id: Mapped[str] = mapped_column(String, unique=True)
    shipment_number: Mapped[str | None] = mapped_column(String, nullable=True)
    container_type: Mapped[str | None] = mapped_column(String, nullable=True)  # "40ft loose stacked" — no NS field, always null
    container_no: Mapped[str | None] = mapped_column(String, nullable=True)    # NS externaldocumentnumber, e.g. CSNU8516117
    expected_date: Mapped[date | None] = mapped_column(Date, nullable=True)  # expected receipt/delivery
    received_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str | None] = mapped_column(String, nullable=True)
    ns_lastmodified: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ItemReceipt(Base):
    __tablename__ = "item_receipt"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id"))
    ns_receipt_id: Mapped[str] = mapped_column(String, unique=True)
    tranid: Mapped[str | None] = mapped_column(String, nullable=True)
    trandate: Mapped[date | None] = mapped_column(Date, nullable=True)
    ns_inbound_shipment: Mapped[str | None] = mapped_column(String, nullable=True)
    po_tranid: Mapped[str | None] = mapped_column(String, nullable=True)  # source PO doc number
    ns_lastmodified: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    lines: Mapped[list["ItemReceiptLine"]] = relationship(
        back_populates="receipt", cascade="all, delete-orphan")


class ItemReceiptLine(Base):
    __tablename__ = "item_receipt_line"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_receipt_id: Mapped[int] = mapped_column(ForeignKey("item_receipt.id"))
    ns_item_id: Mapped[str] = mapped_column(String)
    qty: Mapped[float] = mapped_column(Numeric(14, 2))
    receipt: Mapped["ItemReceipt"] = relationship(back_populates="lines")


class StockOnHand(Base):
    __tablename__ = "stock_on_hand"
    __table_args__ = (UniqueConstraint("customer_id", "snapshot_date", "ns_item_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id"))
    snapshot_date: Mapped[date] = mapped_column(Date)
    ns_item_id: Mapped[str] = mapped_column(String)
    qty_on_hand: Mapped[float] = mapped_column(Numeric(14, 2))
    units_per_pallet: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pallets: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    # When this row was last written by a sync. Today's row is overwritten in place each
    # ~15-min run, so this is the "live as at" time the portal shows for stock on hand.
    synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ItemFulfilment(Base):
    __tablename__ = "item_fulfilment"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id"))
    ns_fulfilment_id: Mapped[str] = mapped_column(String, unique=True)
    tranid: Mapped[str | None] = mapped_column(String, nullable=True)
    trandate: Mapped[date | None] = mapped_column(Date, nullable=True)
    source_type: Mapped[str] = mapped_column(String)               # 'SO' | 'VRMA'
    ns_source_id: Mapped[str | None] = mapped_column(String, nullable=True)
    ns_lastmodified: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    lines: Mapped[list["ItemFulfilmentLine"]] = relationship(
        back_populates="fulfilment", cascade="all, delete-orphan")


class ItemFulfilmentLine(Base):
    __tablename__ = "item_fulfilment_line"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_fulfilment_id: Mapped[int] = mapped_column(ForeignKey("item_fulfilment.id"))
    ns_item_id: Mapped[str] = mapped_column(String)
    qty: Mapped[float] = mapped_column(Numeric(14, 2))             # positive units only (see validation notes)
    fulfilment: Mapped["ItemFulfilment"] = relationship(back_populates="lines")


class Invoice(Base):
    __tablename__ = "invoice"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id"))
    ns_invoice_id: Mapped[str] = mapped_column(String, unique=True)
    tranid: Mapped[str | None] = mapped_column(String, nullable=True)
    trandate: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str | None] = mapped_column(String, nullable=True)
    total: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    ns_lastmodified: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    lines: Mapped[list["InvoiceLine"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan")


class InvoiceLine(Base):
    __tablename__ = "invoice_line"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoice.id"))
    charge_type: Mapped[str | None] = mapped_column(String, nullable=True)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    qty: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    rate: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    invoice: Mapped["Invoice"] = relationship(back_populates="lines")


# --- billing automation -------------------------------------------------------
class BillingRun(Base):
    __tablename__ = "billing_run"
    __table_args__ = (UniqueConstraint("customer_id", "period_start", "period_end"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id"))
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String, default="draft")  # draft|ready_to_push|pushed|invoiced
    ns_invoice_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # Explicit period close, independent of status: a locked run may still be queued and pushed,
    # but its computed lines are frozen and will not be recomputed by a re-save or by the
    # scheduled auto-generate. NULL = open draft.
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String, nullable=True)  # user email
    lines: Mapped[list["BillingLine"]] = relationship(
        back_populates="run", cascade="all, delete-orphan")


class BillingLine(Base):
    __tablename__ = "billing_line"
    id: Mapped[int] = mapped_column(primary_key=True)
    billing_run_id: Mapped[int] = mapped_column(ForeignKey("billing_run.id"))
    charge_type: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    qty: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    rate: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    source_refs: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON audit
    run: Mapped["BillingRun"] = relationship(back_populates="lines")


class User(Base):
    # 'user' is reserved in Postgres, so the table is app_user.
    __tablename__ = "app_user"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String, unique=True)
    password_hash: Mapped[str] = mapped_column(String)
    role: Mapped[str] = mapped_column(String, default="customer")   # admin|internal|customer
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customer.id"), nullable=True)
    allowed_views: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list; NULL = role default
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_login: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Single-use password-reset / set-password link: store only the token's SHA-256 digest.
    reset_token_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    reset_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SyncLog(Base):
    __tablename__ = "sync_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity: Mapped[str] = mapped_column(String)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customer.id"), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    watermark: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rows_upserted: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str | None] = mapped_column(String, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
