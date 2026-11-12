from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Date, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Catalog(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "catalogs"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    active_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("catalog_versions.id", ondelete="SET NULL", use_alter=True),
    )

    versions: Mapped[list[CatalogVersion]] = relationship(
        back_populates="catalog",
        foreign_keys="CatalogVersion.catalog_id",
        order_by="CatalogVersion.version_no",
    )


class CatalogVersion(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "catalog_versions"
    __table_args__ = (UniqueConstraint("catalog_id", "version_no", name="uq_catalog_version_no"),)

    catalog_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("catalogs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="imported")  # imported|active|archived
    source_file_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("uploaded_files.id", ondelete="SET NULL")
    )
    column_mapping: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    import_report: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    catalog: Mapped[Catalog] = relationship(back_populates="versions", foreign_keys=[catalog_id])
    products: Mapped[list[Product]] = relationship(back_populates="version", cascade="all, delete-orphan")


class Product(UUIDPrimaryKeyMixin, TenantMixin, Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("catalog_version_id", "sku_normalized", name="uq_product_version_sku"),
        Index("ix_products_org_version_sku", "organization_id", "catalog_version_id", "sku_normalized"),
    )

    catalog_version_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("catalog_versions.id", ondelete="CASCADE"), nullable=False
    )
    sku: Mapped[str] = mapped_column(String(100), nullable=False)
    sku_normalized: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False, default="EA")
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    list_price: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    cost_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    stock_qty: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    flags: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)

    version: Mapped[CatalogVersion] = relationship(back_populates="products")
