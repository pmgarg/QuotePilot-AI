"""Stage 3 — customer & order intelligence: orders, stock snapshots, FX, connector sync, deletion requests."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin

ORDER_STATUSES = ("proposed", "confirmed", "fulfilled", "cancelled")
PROPOSAL_STATUSES = ("proposed", "approved", "rejected", "converted")


class Order(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("organization_id", "number", name="uq_order_org_number"),
        Index("ix_orders_org_customer", "organization_id", "customer_id"),
        Index("ix_orders_org_status", "organization_id", "status"),
    )

    number: Mapped[str] = mapped_column(String(32), nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL")
    )
    quote_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("quotes.id", ondelete="SET NULL")
    )
    quote_version_no: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="proposed")
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")  # quote|repeat|import|connector
    external_ref: Mapped[str | None] = mapped_column(String(100))
    subtotal: Mapped[Decimal] = mapped_column(Numeric(18, 3), nullable=False, default=Decimal("0"))
    tax_total: Mapped[Decimal] = mapped_column(Numeric(18, 3), nullable=False, default=Decimal("0"))
    grand_total: Mapped[Decimal] = mapped_column(Numeric(18, 3), nullable=False, default=Decimal("0"))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    notes: Mapped[str | None] = mapped_column(Text)

    lines: Mapped[list[OrderLine]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="OrderLine.line_no"
    )
    events: Mapped[list[OrderEvent]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="OrderEvent.occurred_at"
    )


class OrderLine(UUIDPrimaryKeyMixin, TenantMixin, Base):
    __tablename__ = "order_lines"
    __table_args__ = (
        UniqueConstraint("order_id", "line_no", name="uq_order_line_no"),
        CheckConstraint("quantity > 0", name="ck_order_line_qty_positive"),
    )

    order_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("products.id", ondelete="SET NULL")
    )
    sku: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 3), nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False, default="EA")
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    cost_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    tax_rate_pct: Mapped[Decimal] = mapped_column(Numeric(6, 3), nullable=False, default=Decimal("0"))
    line_total: Mapped[Decimal] = mapped_column(Numeric(18, 3), nullable=False, default=Decimal("0"))

    order: Mapped[Order] = relationship(back_populates="lines")


class OrderEvent(UUIDPrimaryKeyMixin, TenantMixin, Base):
    __tablename__ = "order_events"

    order_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # proposed|confirmed|fulfilled|cancelled|synced|note
    detail: Mapped[str | None] = mapped_column(Text)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    order: Mapped[Order] = relationship(back_populates="events")


class OrderProposal(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """Repeat-order / cross-sell proposals derived from this tenant's own history; never auto-executed."""

    __tablename__ = "order_proposals"
    __table_args__ = (Index("ix_order_proposals_org_customer", "organization_id", "customer_id"),)

    customer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # repeat|cross_sell
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="proposed")
    lines: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    evidence: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    order_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))


class StockSnapshot(UUIDPrimaryKeyMixin, TenantMixin, Base):
    """Stock is only ever reported from an explicitly connected, timestamped source (S3-F07)."""

    __tablename__ = "stock_snapshots"
    __table_args__ = (Index("ix_stock_org_product_observed", "organization_id", "product_id", "observed_at"),)

    product_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # csv_connector|erp_stub|manual
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 3), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FxRate(UUIDPrimaryKeyMixin, TenantMixin, Base):
    """Manually maintained reporting rates: 1 base = rate quote, valid from as_of (S3-F04)."""

    __tablename__ = "fx_rates"
    __table_args__ = (
        UniqueConstraint("organization_id", "base_currency", "quote_currency", "as_of", name="uq_fx_org_pair_date"),
        CheckConstraint("rate > 0", name="ck_fx_rate_positive"),
    )

    base_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    quote_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="manual")
    created_by: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ConnectorConnection(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "connector_connections"
    __table_args__ = (UniqueConstraint("organization_id", "kind", name="uq_connector_org_kind"),)

    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # csv|erp_stub
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list
    )  # read:products, read:stock, read:orders, write:orders
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(500))
    connected_by: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))


class SyncRun(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "sync_runs"
    __table_args__ = (UniqueConstraint("organization_id", "idempotency_key", name="uq_sync_run_idem"),)

    connection_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("connector_connections.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="connector")  # connector|csv_import
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    entity: Mapped[str] = mapped_column(String(32), nullable=False)  # products|stock|orders|customers
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="running"
    )  # running|completed|failed|partial
    received: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    applied: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    conflicts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    report: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SyncConflict(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """A connector change that would overwrite local master data waits here for a human decision (S3-F06)."""

    __tablename__ = "sync_conflicts"
    __table_args__ = (Index("ix_sync_conflicts_org_status", "organization_id", "status"),)

    sync_run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("sync_runs.id", ondelete="CASCADE"), nullable=False
    )
    entity: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_key: Mapped[str] = mapped_column(String(200), nullable=False)  # sku / external ref
    local_value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    remote_value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    fields: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="open"
    )  # open|resolved_local|resolved_remote|dismissed
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_note: Mapped[str | None] = mapped_column(Text)


class DeletionRequest(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """Retention / erasure workflow: request → (approved) → soft-deleted → purged by the worker after a grace period."""

    __tablename__ = "deletion_requests"
    __table_args__ = (Index("ix_deletion_org_status", "organization_id", "status"),)

    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)  # customer|contact
    subject_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="requested"
    )  # requested|approved|rejected|executed
    requested_by: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purge_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    legal_hold: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
