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

QUOTE_STATUSES = ("draft", "pending_approval", "approved", "withdrawn", "expired")
VERSION_STATUSES = ("draft", "pending_approval", "approved", "rejected", "superseded")


class Quote(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "quotes"
    __table_args__ = (
        UniqueConstraint("organization_id", "number", name="uq_quote_org_number"),
        Index("ix_quotes_org_status", "organization_id", "status"),
        Index("ix_quotes_org_customer", "organization_id", "customer_id"),
    )

    number: Mapped[str] = mapped_column(String(32), nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL")
    )
    rfq_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("rfqs.id", ondelete="SET NULL"))
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    current_version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    valid_until: Mapped[date | None] = mapped_column(Date)
    withdrawn_reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    versions: Mapped[list[QuoteVersion]] = relationship(
        back_populates="quote", cascade="all, delete-orphan", order_by="QuoteVersion.version_no"
    )


class QuoteVersion(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "quote_versions"
    __table_args__ = (UniqueConstraint("quote_id", "version_no", name="uq_quote_version_no"),)

    quote_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("quotes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    notes: Mapped[str | None] = mapped_column(Text)
    terms: Mapped[str | None] = mapped_column(Text)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(18, 3), nullable=False, default=Decimal("0"))
    discount_total: Mapped[Decimal] = mapped_column(Numeric(18, 3), nullable=False, default=Decimal("0"))
    tax_total: Mapped[Decimal] = mapped_column(Numeric(18, 3), nullable=False, default=Decimal("0"))
    grand_total: Mapped[Decimal] = mapped_column(Numeric(18, 3), nullable=False, default=Decimal("0"))
    pricing_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    submitted_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pdf_storage_key: Mapped[str | None] = mapped_column(String(400))
    pdf_sha256: Mapped[str | None] = mapped_column(String(64))

    quote: Mapped[Quote] = relationship(back_populates="versions")
    lines: Mapped[list[QuoteLine]] = relationship(
        back_populates="version", cascade="all, delete-orphan", order_by="QuoteLine.line_no"
    )
    decisions: Mapped[list[ApprovalDecision]] = relationship(
        back_populates="version",
        cascade="all, delete-orphan",
        order_by="ApprovalDecision.decided_at",
    )


class QuoteLine(UUIDPrimaryKeyMixin, TenantMixin, Base):
    __tablename__ = "quote_lines"
    __table_args__ = (
        UniqueConstraint("quote_version_id", "line_no", name="uq_quote_line_no"),
        CheckConstraint("quantity > 0", name="ck_quote_line_qty_positive"),
        CheckConstraint("discount_pct >= 0 AND discount_pct <= 100", name="ck_quote_line_discount"),
        CheckConstraint("tax_rate_pct >= 0 AND tax_rate_pct <= 100", name="ck_quote_line_tax"),
    )

    quote_version_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("quote_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("products.id", ondelete="SET NULL")
    )
    rfq_item_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("rfq_items.id", ondelete="SET NULL")
    )
    sku: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 3), nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False, default="EA")
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    price_source: Mapped[str] = mapped_column(String(16), nullable=False, default="none")  # catalog|manual|none
    cost_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    discount_pct: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False, default=Decimal("0"))
    tax_rate_pct: Mapped[Decimal] = mapped_column(Numeric(6, 3), nullable=False, default=Decimal("0"))
    line_subtotal: Mapped[Decimal] = mapped_column(Numeric(18, 3), nullable=False, default=Decimal("0"))
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(18, 3), nullable=False, default=Decimal("0"))
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(18, 3), nullable=False, default=Decimal("0"))
    line_total: Mapped[Decimal] = mapped_column(Numeric(18, 3), nullable=False, default=Decimal("0"))
    is_custom: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    unit_mismatch_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    margin_override_reason: Mapped[str | None] = mapped_column(Text)
    margin_override_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    version: Mapped[QuoteVersion] = relationship(back_populates="lines")


class ApprovalDecision(UUIDPrimaryKeyMixin, TenantMixin, Base):
    __tablename__ = "approval_decisions"

    quote_version_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("quote_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    decision: Mapped[str] = mapped_column(String(16), nullable=False)  # approved|rejected|withdrawn
    reason: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    version: Mapped[QuoteVersion] = relationship(back_populates="decisions")
