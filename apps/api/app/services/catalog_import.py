"""Catalog import: validated, versioned, immutable (LLD § Catalog import algorithm)."""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import NotFoundError, PayloadTooLarge, StateError, ValidationFailed
from app.models.catalog import Catalog, CatalogVersion, Product
from app.models.files import UploadedFile
from app.services import audit
from app.services.money import MAX_STORED_PRICE, is_supported_currency, parse_decimal
from app.services.parsing.limits import check_size, check_xlsx_zip, sniff_kind
from app.services.parsing.sandbox import ParseFailure, run_parser
from app.services.storage import get_storage, make_key
from app.services.units import normalize_sku, normalize_unit

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
REQUIRED_FIELDS = ("sku", "description", "list_price")
OPTIONAL_FIELDS = ("unit", "currency", "cost_price", "stock_qty", "valid_from", "valid_to")
ALL_FIELDS = REQUIRED_FIELDS + OPTIONAL_FIELDS
MAX_REPORT_ENTRIES = 500
_PRICE_Q = Decimal("0.000001")
_STOCK_Q = Decimal("0.001")


def _too_large(value: Decimal, quantum: Decimal) -> bool:
    """True when the value, rounded as it will be stored, does not fit the NUMERIC(18, n) column (SEC4-2)."""
    return abs(value) >= MAX_STORED_PRICE or abs(value.quantize(quantum)) >= MAX_STORED_PRICE


@dataclass
class ImportReport:
    total_rows: int = 0
    imported: int = 0
    skipped: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False

    def error(self, row: int, message: str) -> None:
        self.skipped += 1
        if len(self.errors) < MAX_REPORT_ENTRIES:
            self.errors.append({"row": row, "message": message})

    def warn(self, row: int, message: str) -> None:
        if len(self.warnings) < MAX_REPORT_ENTRIES:
            self.warnings.append({"row": row, "message": message})

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_rows": self.total_rows,
            "imported": self.imported,
            "skipped": self.skipped,
            "errors": self.errors,
            "warnings": self.warnings,
            "truncated": self.truncated,
            "errors_truncated": len(self.errors) >= MAX_REPORT_ENTRIES,
        }


def _is_formula_like(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value[:1] in FORMULA_PREFIXES
        and not re.fullmatch(r"-?\d+(?:[.,]\d+)?", value.strip())
    )


def _parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    s = str(value).strip()[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        for fmt in ("%d/%m/%Y", "%d.%m.%Y", "%m/%d/%Y"):
            try:
                from datetime import datetime

                return datetime.strptime(s, fmt).date()  # noqa: DTZ007
            except ValueError:
                continue
    return None


def resolve_mapping(header: list[Any], mapping: dict[str, Any]) -> dict[str, int]:
    """Mapping values may be column indexes (int) or header names (case-insensitive)."""
    norm = [re.sub(r"\s+", " ", str(h or "")).strip().lower() for h in header]
    resolved: dict[str, int] = {}
    for field_name in ALL_FIELDS:
        v = mapping.get(field_name)
        if v in (None, ""):
            continue
        if isinstance(v, int) or (isinstance(v, str) and v.isdigit()):
            idx = int(v)
            if idx < 0 or idx >= len(header):
                raise ValidationFailed(f"Column index {idx} for '{field_name}' is out of range")
            resolved[field_name] = idx
        else:
            key = str(v).strip().lower()
            if key not in norm:
                raise ValidationFailed(f"Column '{v}' for '{field_name}' not found in header")
            resolved[field_name] = norm.index(key)
    missing = [f for f in REQUIRED_FIELDS if f not in resolved]
    if missing:
        raise ValidationFailed(f"Mapping is missing required fields: {', '.join(missing)}")
    return resolved


def _rows_from_parsed(parsed: dict[str, Any], kind: str) -> tuple[list[list[Any]], bool]:
    if kind == "csv":
        return parsed.get("rows", []), bool(parsed.get("truncated"))
    sheets = parsed.get("sheets", [])
    rows = sheets[0]["rows"] if sheets else []
    return rows, bool(parsed.get("truncated"))


def build_products(
    rows: list[list[Any]],
    mapping: dict[str, int],
    *,
    default_currency: str,
    organization_id: uuid.UUID,
    version_id: uuid.UUID,
) -> tuple[list[Product], ImportReport]:
    report = ImportReport()
    seen: set[str] = set()
    products: list[Product] = []
    for ridx, row in enumerate(rows[1:], start=2):
        if not any(c not in (None, "") for c in row):
            continue
        report.total_rows += 1

        def cell(name: str, row: list[Any] = row) -> Any:
            idx = mapping.get(name)
            return row[idx] if idx is not None and idx < len(row) else None

        flags: list[str] = []
        sku_raw = cell("sku")
        sku = str(sku_raw).strip() if sku_raw not in (None, "") else ""
        if not sku:
            report.error(ridx, "missing sku")
            continue
        sku_norm = normalize_sku(sku)
        if not sku_norm:
            report.error(ridx, "sku is empty after normalisation")
            continue
        if len(sku) > 100:
            report.error(ridx, "sku longer than 100 characters")
            continue
        if sku_norm in seen:
            report.error(ridx, f"duplicate sku '{sku}' (first occurrence kept)")
            continue
        description = str(cell("description") or "").strip()
        if not description:
            report.error(ridx, "missing description")
            continue
        price = parse_decimal(cell("list_price"))
        if price is None:
            report.error(ridx, "list price is not a number")
            continue
        if price < 0:
            report.error(ridx, "list price is negative")
            continue
        if _too_large(price, _PRICE_Q):
            report.error(ridx, "list price is too large")
            continue
        currency_raw = cell("currency")
        currency = str(currency_raw).strip().upper() if currency_raw not in (None, "") else default_currency
        if not is_supported_currency(currency):
            report.error(ridx, f"unsupported currency '{currency}'")
            continue
        cost = None
        if cell("cost_price") not in (None, ""):
            cost = parse_decimal(cell("cost_price"))
            if cost is None or cost < 0:
                report.error(ridx, "cost price is not a valid non-negative number")
                continue
            if _too_large(cost, _PRICE_Q):
                report.error(ridx, "cost price is too large")
                continue
        stock = None
        if cell("stock_qty") not in (None, ""):
            stock = parse_decimal(cell("stock_qty"))
            if stock is not None and (stock < 0 or _too_large(stock, _STOCK_Q)):
                stock = None  # a negative or absurd stock figure is not a fact about stock (SEC6-3)
            if stock is None:
                report.warn(ridx, "stock quantity unreadable; left unknown")
        unit_raw = cell("unit")
        unit = normalize_unit(str(unit_raw)) if unit_raw not in (None, "") else "EA"
        if unit is None:
            unit = str(unit_raw).strip().upper()[:16]
            flags.append("unit_unknown")
            report.warn(ridx, f"unknown unit '{unit_raw}' kept as-is")
        for name in ("sku", "description", "unit"):
            if _is_formula_like(cell(name)):
                flags.append("formula_like")
                report.warn(ridx, f"{name} looks like a spreadsheet formula; stored as text")
                break
        vf, vt = _parse_date(cell("valid_from")), _parse_date(cell("valid_to"))
        if vf and vt and vf > vt:
            report.error(ridx, "valid_from is after valid_to")
            continue
        seen.add(sku_norm)
        products.append(
            Product(
                organization_id=organization_id,
                catalog_version_id=version_id,
                sku=sku[:100],
                sku_normalized=sku_norm[:100],
                description=description[:2000],
                unit=unit,
                currency=currency,
                list_price=price.quantize(_PRICE_Q),
                cost_price=cost.quantize(_PRICE_Q) if cost is not None else None,
                stock_qty=stock.quantize(_STOCK_Q) if stock is not None else None,
                valid_from=vf,
                valid_to=vt,
                flags=flags,
            )
        )
        report.imported += 1
    return products, report


def import_catalog(
    db: Session,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    ip: str | None,
    filename: str,
    content: bytes,
    mapping: dict[str, Any],
    catalog_name: str | None,
    default_currency: str,
) -> CatalogVersion:
    settings = get_settings()
    check_size(len(content), settings.max_upload_bytes)
    kind = sniff_kind(filename, content[:4096])
    if kind not in ("csv", "xlsx"):
        from app.core.errors import UnsupportedMedia

        raise UnsupportedMedia("Catalog import accepts CSV or XLSX files.")
    storage = get_storage()
    key = make_key(organization_id, "catalog", filename)
    storage.put(key, content)
    path = storage.local_path(key)
    tmp_path: Path | None = None
    if path is None:
        import tempfile

        fd, name = tempfile.mkstemp(suffix=f".{kind}")
        with open(fd, "wb") as fh:
            fh.write(content)
        tmp_path = path = Path(name)
    try:
        if kind == "xlsx":
            check_xlsx_zip(path, settings.xlsx_max_uncompressed_bytes, settings.xlsx_max_compression_ratio)
        try:
            parsed = run_parser(kind, path)
        except ParseFailure as exc:
            raise ValidationFailed(f"Could not read the file: {exc.message}") from exc
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
    rows, truncated = _rows_from_parsed(parsed, kind)
    if truncated:
        raise PayloadTooLarge(
            f"The file has more than {settings.max_catalog_rows} rows; split it into smaller imports."
        )
    if not rows:
        raise ValidationFailed("The file contains no rows.")
    resolved = resolve_mapping(rows[0], mapping)

    upload = UploadedFile(
        organization_id=organization_id,
        kind="catalog",
        original_name=filename[:255],
        content_type="text/csv"
        if kind == "csv"
        else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        storage_key=key,
        uploaded_by=actor_user_id,
    )
    db.add(upload)
    db.flush()

    name = (catalog_name or "Main catalog").strip()[:200]
    catalog = db.scalar(select(Catalog).where(Catalog.organization_id == organization_id, Catalog.name == name))
    if catalog is None:
        catalog = Catalog(organization_id=organization_id, name=name)
        db.add(catalog)
        db.flush()
    next_no = (
        db.scalar(
            select(func.coalesce(func.max(CatalogVersion.version_no), 0)).where(CatalogVersion.catalog_id == catalog.id)
        )
        or 0
    ) + 1
    version = CatalogVersion(
        organization_id=organization_id,
        catalog_id=catalog.id,
        version_no=next_no,
        status="imported",
        source_file_id=upload.id,
        column_mapping={k: int(v) for k, v in resolved.items()},
        created_by=actor_user_id,
    )
    db.add(version)
    db.flush()
    products, report = build_products(
        rows,
        resolved,
        default_currency=default_currency,
        organization_id=organization_id,
        version_id=version.id,
    )
    report.truncated = truncated
    if report.imported == 0:
        raise ValidationFailed("No valid product rows were found.", report=report.as_dict())
    db.add_all(products)
    version.row_count = report.imported
    version.import_report = report.as_dict()
    db.flush()
    audit.record(
        db,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        action="catalog.import",
        entity_type="catalog_version",
        entity_id=version.id,
        ip=ip,
        summary={
            "catalog": catalog.name,
            "version_no": next_no,
            "imported": report.imported,
            "skipped": report.skipped,
        },
    )
    return version


def activate_version(
    db: Session,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    ip: str | None,
    version_id: uuid.UUID,
) -> CatalogVersion:
    version = db.scalar(
        select(CatalogVersion).where(CatalogVersion.id == version_id, CatalogVersion.organization_id == organization_id)
    )
    if version is None:
        raise NotFoundError("Catalog version not found")
    if version.status == "active":
        raise StateError("Version is already active")
    catalog = db.get(Catalog, version.catalog_id)
    assert catalog is not None
    if catalog.active_version_id:
        prev = db.get(CatalogVersion, catalog.active_version_id)
        if prev is not None:
            prev.status = "archived"
    catalog.active_version_id = version.id
    version.status = "active"
    db.flush()
    audit.record(
        db,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        action="catalog.activate",
        entity_type="catalog_version",
        entity_id=version.id,
        ip=ip,
        summary={"version_no": version.version_no},
    )
    return version


def active_version_for_org(db: Session, organization_id: uuid.UUID) -> CatalogVersion | None:
    catalog = db.scalar(
        select(Catalog)
        .where(Catalog.organization_id == organization_id, Catalog.active_version_id.is_not(None))
        .order_by(Catalog.updated_at.desc())  # most recently activated catalog wins when several exist
    )
    if catalog is None or catalog.active_version_id is None:
        return None
    return db.get(CatalogVersion, catalog.active_version_id)
