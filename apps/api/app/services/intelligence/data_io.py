"""CSV import/export for customers, orders and products (S3-F05).

* Exports are formula-safe (``csv_export.write_csv``) and role-aware: cost/margin columns only for roles with
  ``view_costs``; bulk exports require ``export_data`` and are audited (denials too — S3-A06).
* Imports validate every row independently; valid rows are applied, invalid rows are reported with their row
  number and reason and can be downloaded as a CSV to fix and re-upload (partial-import recovery, S3-A07).
* Product *import* is the Stage 1 catalog import (versioned, with mapping and report); this module exports.
"""

from __future__ import annotations

import csv
import io
import uuid
from collections import OrderedDict
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import PayloadTooLarge, ValidationFailed
from app.db.base import utcnow
from app.models.catalog import Product
from app.models.customer import Customer
from app.models.intelligence import Order, SyncRun
from app.models.org import Organization
from app.services import audit
from app.services.catalog_import import active_version_for_org
from app.services.comms.providers import valid_email
from app.services.exports.csv_export import write_csv
from app.services.intelligence import orders as order_service
from app.services.money import is_supported_currency, parse_decimal

CUSTOMER_COLUMNS = ("name", "reference", "email", "default_currency", "notes")
ORDER_COLUMNS = (
    "order_ref",
    "customer_reference",
    "sku",
    "description",
    "quantity",
    "unit",
    "unit_price",
    "currency",
    "tax_rate_pct",
)
MAX_CSV_BYTES = 5 * 1024 * 1024


# ----------------------------------------------------------------- exports
def export_customers(db: Session, org_id: uuid.UUID) -> bytes:
    rows = db.scalars(
        select(Customer)
        .where(Customer.organization_id == org_id, Customer.deleted_at.is_(None))
        .order_by(Customer.name)
    ).all()
    return write_csv(
        ("id", *CUSTOMER_COLUMNS, "created_at"),
        [(c.id, c.name, c.reference, c.email, c.default_currency, c.notes, c.created_at.isoformat()) for c in rows],
    )


def export_orders(db: Session, org_id: uuid.UUID, *, include_costs: bool) -> bytes:
    rows = db.scalars(select(Order).where(Order.organization_id == org_id).order_by(Order.number)).all()
    header = [
        "order_number",
        "status",
        "source",
        "customer_id",
        "currency",
        "line_no",
        "sku",
        "description",
        "quantity",
        "unit",
        "unit_price",
        "tax_rate_pct",
        "line_total",
    ]
    if include_costs:
        header += ["cost_price", "margin_pct"]
    out: list[list[object]] = []
    for o in rows:
        for ln in o.lines:
            row: list[object] = [
                o.number,
                o.status,
                o.source,
                o.customer_id,
                o.currency,
                ln.line_no,
                ln.sku,
                ln.description,
                ln.quantity,
                ln.unit,
                ln.unit_price,
                ln.tax_rate_pct,
                ln.line_total,
            ]
            if include_costs:
                margin = None
                if ln.cost_price is not None and ln.unit_price:
                    margin = ((ln.unit_price - ln.cost_price) / ln.unit_price * 100).quantize(Decimal("0.01"))
                row += [ln.cost_price, margin]
            out.append(row)
    return write_csv(header, out)


def export_products(db: Session, org_id: uuid.UUID, *, include_costs: bool) -> bytes:
    active = active_version_for_org(db, org_id)
    header = ["sku", "description", "unit", "currency", "list_price", "valid_from", "valid_to"]
    if include_costs:
        header.append("cost_price")
    if active is None:
        return write_csv(header, [])
    rows = db.scalars(select(Product).where(Product.catalog_version_id == active.id).order_by(Product.sku)).all()
    out = []
    for p in rows:
        row: list[object] = [p.sku, p.description, p.unit, p.currency, p.list_price, p.valid_from, p.valid_to]
        if include_costs:
            row.append(p.cost_price)
        out.append(row)
    return write_csv(header, out)


# ----------------------------------------------------------------- imports
def _read_rows(raw: bytes, required: tuple[str, ...]) -> tuple[list[str], list[dict[str, str]]]:
    if len(raw) > MAX_CSV_BYTES:
        raise PayloadTooLarge("CSV larger than 5 MB")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationFailed("CSV must be UTF-8 encoded") from exc
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip().lower() for h in (reader.fieldnames or [])]
    reader.fieldnames = header
    missing = [c for c in required if c not in header]
    if missing:
        raise ValidationFailed(f"Missing required column(s): {', '.join(missing)}", field="header")
    rows: list[dict[str, str]] = []
    cap = get_settings().max_import_rows
    for i, row in enumerate(reader):
        if i >= cap:
            raise PayloadTooLarge(f"CSV has more than {cap} rows")
        rows.append({k: (v or "").strip() for k, v in row.items() if k})
    return header, rows


def _start_run(db: Session, org_id: uuid.UUID, entity: str, key: str, received: int) -> SyncRun:
    run = SyncRun(
        organization_id=org_id,
        connection_id=None,
        kind="csv_import",
        idempotency_key=key,
        entity=entity,
        status="running",
        received=received,
    )
    db.add(run)
    db.flush()
    return run


def _finish(run: SyncRun, applied: int, failed: list[dict[str, Any]], header: list[str]) -> SyncRun:
    run.applied = applied
    run.rejected = len(failed)
    run.status = "completed" if not failed else ("partial" if applied else "failed")
    run.report = {"header": header, "failed_rows": failed[:500], "failed_total": len(failed)}
    run.finished_at = utcnow()
    return run


def import_customers(
    db: Session, *, org: Organization, actor_user_id: uuid.UUID, ip: str | None, raw: bytes, idempotency_key: str
) -> SyncRun:
    header, rows = _read_rows(raw, ("name",))
    run = _start_run(db, org.id, "customers", idempotency_key, len(rows))
    existing_refs = {
        r for (r,) in db.execute(select(Customer.reference).where(Customer.organization_id == org.id)) if r
    }
    applied = 0
    failed: list[dict[str, Any]] = []
    for idx, row in enumerate(rows, start=2):
        errors: list[str] = []
        if not row.get("name"):
            errors.append("name is required")
        cur = (row.get("default_currency") or "").upper() or None
        if cur and not is_supported_currency(cur):
            errors.append(f"unsupported currency {cur}")
        if row.get("email") and not valid_email(row["email"]):
            errors.append("invalid email")
        ref = row.get("reference") or None
        if ref and ref in existing_refs:
            errors.append(f"reference {ref} already exists")
        if errors:
            failed.append({"row": idx, "errors": errors, "values": row})
            continue
        db.add(
            Customer(
                organization_id=org.id,
                name=row["name"][:200],
                reference=ref[:100] if ref else None,
                email=row.get("email") or None,
                default_currency=cur,
                notes=(row.get("notes") or None),
            )
        )
        if ref:
            existing_refs.add(ref)
        applied += 1
    db.flush()
    _finish(run, applied, failed, header)
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=actor_user_id,
        action="import.customers",
        entity_type="sync_run",
        entity_id=run.id,
        ip=ip,
        summary={"received": run.received, "applied": applied, "rejected": len(failed)},
    )
    return run


def import_orders(
    db: Session, *, org: Organization, actor_user_id: uuid.UUID, ip: str | None, raw: bytes, idempotency_key: str
) -> SyncRun:
    """Each distinct ``order_ref`` becomes one *proposed* order (confirmation still required)."""
    header, rows = _read_rows(raw, ("order_ref", "sku", "quantity", "unit_price"))
    run = _start_run(db, org.id, "orders", idempotency_key, len(rows))
    customers = {
        c.reference: c
        for c in db.scalars(select(Customer).where(Customer.organization_id == org.id, Customer.deleted_at.is_(None)))
        if c.reference
    }
    existing_refs = {r for (r,) in db.execute(select(Order.external_ref).where(Order.organization_id == org.id)) if r}
    grouped: OrderedDict[str, list[tuple[int, dict[str, str]]]] = OrderedDict()
    failed: list[dict[str, Any]] = []
    for idx, row in enumerate(rows, start=2):
        if not row.get("order_ref"):
            failed.append({"row": idx, "errors": ["order_ref is required"], "values": row})
            continue
        grouped.setdefault(row["order_ref"], []).append((idx, row))
    applied = 0
    for ref, items in grouped.items():
        errors: list[tuple[int, str]] = []
        lines: list[dict[str, Any]] = []
        currency = (items[0][1].get("currency") or org.default_currency).upper()
        if not is_supported_currency(currency):
            errors.append((items[0][0], f"unsupported currency {currency}"))
        if ref in existing_refs:
            errors.append((items[0][0], f"order_ref {ref} already imported"))
        cust_ref = items[0][1].get("customer_reference") or ""
        customer = customers.get(cust_ref) if cust_ref else None
        if cust_ref and customer is None:
            errors.append((items[0][0], f"unknown customer_reference {cust_ref}"))
        for idx, row in items:
            qty = parse_decimal(row.get("quantity"))
            price = parse_decimal(row.get("unit_price"))
            tax = parse_decimal(row.get("tax_rate_pct")) if row.get("tax_rate_pct") else Decimal(org.default_tax_rate)
            if qty is None or qty <= 0:
                errors.append((idx, "quantity must be a positive number"))
            if price is None or price < 0:
                errors.append((idx, "unit_price must be a non-negative number"))
            if tax is None or tax < 0 or tax > 100:
                errors.append((idx, "tax_rate_pct must be between 0 and 100"))
            if (row.get("currency") or currency).upper() != currency:
                errors.append((idx, "all lines of an order must share one currency"))
            if not row.get("sku") and not row.get("description"):
                errors.append((idx, "sku or description required"))
            if qty is not None and price is not None and tax is not None:
                lines.append(
                    {
                        "sku": row.get("sku") or None,
                        "description": row.get("description") or row.get("sku"),
                        "quantity": qty,
                        "unit": row.get("unit") or "EA",
                        "unit_price": price,
                        "tax_rate_pct": tax,
                    }
                )
        if errors:
            for idx, row in items:
                failed.append(
                    {"row": idx, "errors": [m for (i, m) in errors if i == idx] or ["order rejected"], "values": row}
                )
            continue
        with db.begin_nested():
            order_service.create_manual(
                db,
                org=org,
                actor_user_id=actor_user_id,
                ip=ip,
                customer_id=customer.id if customer else None,
                currency=currency,
                lines=lines,
                source="import",
                external_ref=ref,
                notes=f"CSV import {idempotency_key}",
            )
        existing_refs.add(ref)
        applied += len(items)
    _finish(run, applied, failed, header)
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=actor_user_id,
        action="import.orders",
        entity_type="sync_run",
        entity_id=run.id,
        ip=ip,
        summary={"received": run.received, "applied": applied, "rejected": len(failed), "orders": len(grouped)},
    )
    return run


def failed_rows_csv(run: SyncRun) -> bytes:
    header: list[str] = list(run.report.get("header") or [])
    rows = run.report.get("failed_rows") or []
    return write_csv(
        [*header, "_row", "_errors"],
        [[r["values"].get(h, "") for h in header] + [r["row"], "; ".join(r["errors"])] for r in rows],
    )
