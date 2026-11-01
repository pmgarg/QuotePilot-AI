from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
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

RFQ_STATUSES = ("uploaded", "parsing", "parsed", "failed", "quoted")
MATCH_STATUSES = ("exact", "likely", "ambiguous", "unmatched", "resolved", "custom")


class RFQ(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "rfqs"

    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL")
    )
    reference: Mapped[str | None] = mapped_column(String(100))
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False)  # pdf|xlsx|csv|text
    source_file_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("uploaded_files.id", ondelete="SET NULL")
    )
    pasted_text: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="uploaded")
    failure_reason: Mapped[str | None] = mapped_column(String(500))
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    catalog_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("catalog_versions.id", ondelete="SET NULL")
    )
    parse_meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    items: Mapped[list[RFQItem]] = relationship(
        back_populates="rfq", cascade="all, delete-orphan", order_by="RFQItem.line_no"
    )


class RFQItem(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "rfq_items"
    __table_args__ = (
        UniqueConstraint("rfq_id", "line_no", name="uq_rfq_item_line"),
        CheckConstraint("quantity IS NULL OR quantity > 0", name="ck_rfq_item_qty_positive"),
    )

    rfq_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("rfqs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_ref: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    sku_hint: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))
    unit_raw: Mapped[str | None] = mapped_column(String(32))
    unit_normalized: Mapped[str | None] = mapped_column(String(16))
    match_status: Mapped[str] = mapped_column(String(16), nullable=False, default="unmatched")
    selected_product_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("products.id", ondelete="SET NULL")
    )
    unit_mismatch: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    unit_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_duplicate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    duplicate_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_custom: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    flags: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    notes: Mapped[str | None] = mapped_column(Text)

    rfq: Mapped[RFQ] = relationship(back_populates="items")
    candidates: Mapped[list[MatchCandidate]] = relationship(
        back_populates="item", cascade="all, delete-orphan", order_by="MatchCandidate.rank"
    )


class MatchCandidate(UUIDPrimaryKeyMixin, TenantMixin, Base):
    __tablename__ = "match_candidates"

    rfq_item_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("rfq_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)  # exact|likely|ambiguous
    score: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    reason: Mapped[str] = mapped_column(String(200), nullable=False)
    matched_on: Mapped[str] = mapped_column(String(32), nullable=False)  # sku|sku_token|description

    item: Mapped[RFQItem] = relationship(back_populates="candidates")
