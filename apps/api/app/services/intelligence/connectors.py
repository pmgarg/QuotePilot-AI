"""Connector framework (S3-F06): adapters declare scopes, every sync is idempotent, and any change that would
overwrite local master data goes to a conflict queue instead of being applied silently.

Shipped adapters make no network calls (no paid resources): ``erp_stub`` and ``csv`` accept records *pushed*
to the sync endpoint by an operator or an external system. Pull adapters for real ERPs are documented as
future work in docs/KNOWN_LIMITATIONS.md.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ForbiddenError, NotFoundError, PayloadTooLarge, StateError, ValidationFailed
from app.db.base import utcnow
from app.models.catalog import Product
from app.models.customer import Customer
from app.models.intelligence import ConnectorConnection, Order, StockSnapshot, SyncConflict, SyncRun
from app.models.org import Organization
from app.services import audit
from app.services.catalog_import import active_version_for_org
from app.services.intelligence import orders as order_service
from app.services.money import is_supported_currency, parse_decimal

KINDS = {"erp_stub", "csv"}
SCOPES = {"read:products", "read:stock", "read:orders", "read:customers"}
ENTITY_SCOPE = {
    "products": "read:products",
    "stock": "read:stock",
    "orders": "read:orders",
    "customers": "read:customers",
}
PRODUCT_MASTER_FIELDS = ("description", "unit", "list_price", "cost_price")
CUSTOMER_MASTER_FIELDS = ("name", "email", "default_currency")


def get_connection(db: Session, org_id: uuid.UUID, connection_id: uuid.UUID) -> ConnectorConnection:
    c = db.scalar(
        select(ConnectorConnection).where(
            ConnectorConnection.id == connection_id, ConnectorConnection.organization_id == org_id
        )
    )
    if c is None:
        raise NotFoundError("Connector not found")
    return c


def create_connection(
    db: Session,
    *,
    org: Organization,
    actor_user_id: uuid.UUID,
    ip: str | None,
    kind: str,
    name: str,
    scopes: list[str],
) -> ConnectorConnection:
    if kind not in KINDS:
        raise ValidationFailed(f"Unsupported connector kind '{kind}'", field="kind")
    bad = sorted(set(scopes) - SCOPES)
    if bad:
        raise ValidationFailed(f"Unknown scope(s): {', '.join(bad)}", field="scopes")
    if not scopes:
        raise ValidationFailed("At least one scope is required", field="scopes")
    if db.scalar(
        select(ConnectorConnection.id).where(
            ConnectorConnection.organization_id == org.id, ConnectorConnection.kind == kind
        )
    ):
        raise StateError(f"A '{kind}' connector already exists")
    conn = ConnectorConnection(
        organization_id=org.id, kind=kind, name=name, scopes=sorted(set(scopes)), connected_by=actor_user_id
    )
    db.add(conn)
    db.flush()
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=actor_user_id,
        action="connector.create",
        entity_type="connector",
        entity_id=conn.id,
        ip=ip,
        summary={"kind": kind, "scopes": conn.scopes},
    )
    return conn


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else None
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else None
    return None


def sync(
    db: Session,
    *,
    org: Organization,
    conn: ConnectorConnection,
    actor_user_id: uuid.UUID | None,
    ip: str | None,
    entity: str,
    idempotency_key: str,
    records: list[dict[str, Any]],
) -> SyncRun:
    scope = ENTITY_SCOPE.get(entity)
    if scope is None:
        raise ValidationFailed("entity must be products, stock, orders or customers", field="entity")
    if scope not in conn.scopes:
        raise ForbiddenError(f"Connector lacks scope '{scope}'", action=scope)
    if conn.status != "active":
        raise StateError("Connector is disabled")
    if len(records) > get_settings().max_sync_records:
        raise PayloadTooLarge("Too many records in one sync")
    existing = db.scalar(
        select(SyncRun).where(SyncRun.organization_id == org.id, SyncRun.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return existing  # idempotent replay: the previous outcome is authoritative
    run = SyncRun(
        organization_id=org.id,
        connection_id=conn.id,
        kind="connector",
        idempotency_key=idempotency_key,
        entity=entity,
        status="running",
        received=len(records),
    )
    db.add(run)
    db.flush()
    handler = {
        "products": _sync_products,
        "stock": _sync_stock,
        "orders": _sync_orders,
        "customers": _sync_customers,
    }[entity]
    applied, conflicts, rejected = handler(db, org, conn, run, records, actor_user_id, ip)
    run.applied, run.conflicts = applied, conflicts
    run.rejected = len(rejected)
    run.report = {"rejected": rejected[:200]}
    run.status = "completed" if not rejected else ("partial" if applied or conflicts else "failed")
    run.finished_at = utcnow()
    conn.last_sync_at = run.finished_at
    conn.last_error = None if run.status != "failed" else "all records rejected"
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=actor_user_id,
        action="connector.sync",
        entity_type="sync_run",
        entity_id=run.id,
        ip=ip,
        summary={
            "connector": str(conn.id),
            "entity": entity,
            "received": run.received,
            "applied": applied,
            "conflicts": conflicts,
            "rejected": run.rejected,
        },
    )
    return run


def _conflict(
    db: Session,
    run: SyncRun,
    *,
    entity: str,
    key: str,
    local: dict[str, Any],
    remote: dict[str, Any],
    fields: list[str],
) -> None:
    db.add(
        SyncConflict(
            organization_id=run.organization_id,
            sync_run_id=run.id,
            entity=entity,
            entity_key=key,
            local_value=local,
            remote_value=remote,
            fields=fields,
            status="open",
        )
    )


def _same(field: str, local: Any, remote: Any) -> bool:
    if field in ("list_price", "cost_price"):
        if local is None or remote is None:
            return local is None and remote is None
        return Decimal(str(local)) == Decimal(str(remote))
    return str(local) == str(remote)


def _sync_products(
    db: Session,
    org: Organization,
    conn: ConnectorConnection,
    run: SyncRun,
    records: list[dict[str, Any]],
    actor: uuid.UUID | None,
    ip: str | None,
) -> tuple[int, int, list[dict[str, Any]]]:
    """Master data is never overwritten: differences become conflicts; unknown SKUs are staged as conflicts too."""
    active = active_version_for_org(db, org.id)
    rejected: list[dict[str, Any]] = []
    conflicts = applied = 0
    for i, rec in enumerate(records):
        sku = str(rec.get("sku") or "").strip()
        if not sku:
            rejected.append({"index": i, "reason": "sku required"})
            continue
        remote: dict[str, Any] = {}
        for f in PRODUCT_MASTER_FIELDS:
            if f in rec and rec[f] is not None:
                if f in ("list_price", "cost_price"):
                    d = parse_decimal(rec[f])
                    if d is None or d < 0:
                        rejected.append({"index": i, "reason": f"{f} invalid"})
                        remote = {}
                        break
                    remote[f] = str(d)
                else:
                    remote[f] = str(rec[f])[:500]
        if not remote:
            if not any(r.get("index") == i for r in rejected):
                rejected.append({"index": i, "reason": "no master fields supplied"})
            continue
        product = None
        if active is not None:
            product = db.scalar(
                select(Product).where(Product.catalog_version_id == active.id, Product.sku_normalized == sku.upper())
            )
        if product is None:
            _conflict(db, run, entity="products", key=sku, local={}, remote=remote, fields=["new"])
            conflicts += 1
            continue
        local = {
            "description": product.description,
            "unit": product.unit,
            "list_price": str(product.list_price),
            "cost_price": str(product.cost_price) if product.cost_price is not None else None,
        }
        diff = [f for f in remote if not _same(f, local.get(f), remote[f])]
        if diff:
            _conflict(db, run, entity="products", key=sku, local=local, remote=remote, fields=diff)
            conflicts += 1
        else:
            applied += 1  # identical — nothing to change
    return applied, conflicts, rejected


def _sync_stock(
    db: Session,
    org: Organization,
    conn: ConnectorConnection,
    run: SyncRun,
    records: list[dict[str, Any]],
    actor: uuid.UUID | None,
    ip: str | None,
) -> tuple[int, int, list[dict[str, Any]]]:
    active = active_version_for_org(db, org.id)
    rejected: list[dict[str, Any]] = []
    applied = 0
    now = utcnow()
    for i, rec in enumerate(records):
        sku = str(rec.get("sku") or "").strip()
        qty = parse_decimal(rec.get("quantity"))
        observed = _parse_ts(rec.get("observed_at"))
        if not sku or qty is None or qty < 0:
            rejected.append({"index": i, "reason": "sku and non-negative quantity required"})
            continue
        if observed is None:
            rejected.append({"index": i, "reason": "observed_at must be a timezone-aware ISO-8601 timestamp"})
            continue
        if observed > now:
            rejected.append({"index": i, "reason": "observed_at in the future"})
            continue
        product = None
        if active is not None:
            product = db.scalar(
                select(Product).where(Product.catalog_version_id == active.id, Product.sku_normalized == sku.upper())
            )
        if product is None:
            rejected.append({"index": i, "reason": f"unknown sku {sku}"})
            continue
        db.add(
            StockSnapshot(
                organization_id=org.id,
                product_id=product.id,
                source=conn.kind,
                quantity=Decimal(qty),
                observed_at=observed,
                recorded_at=now,
            )
        )
        applied += 1
    return applied, 0, rejected


def _sync_customers(
    db: Session,
    org: Organization,
    conn: ConnectorConnection,
    run: SyncRun,
    records: list[dict[str, Any]],
    actor: uuid.UUID | None,
    ip: str | None,
) -> tuple[int, int, list[dict[str, Any]]]:
    rejected: list[dict[str, Any]] = []
    applied = conflicts = 0
    for i, rec in enumerate(records):
        ref = str(rec.get("reference") or "").strip()
        name = str(rec.get("name") or "").strip()
        if not ref or not name:
            rejected.append({"index": i, "reason": "reference and name required"})
            continue
        cur = str(rec.get("default_currency") or "").upper() or None
        if cur and not is_supported_currency(cur):
            rejected.append({"index": i, "reason": f"unsupported currency {cur}"})
            continue
        remote = {"name": name[:200], "email": rec.get("email"), "default_currency": cur}
        existing = db.scalar(select(Customer).where(Customer.organization_id == org.id, Customer.reference == ref))
        if existing is None:
            db.add(Customer(organization_id=org.id, reference=ref[:100], **remote))
            applied += 1
            continue
        local = {f: getattr(existing, f) for f in CUSTOMER_MASTER_FIELDS}
        diff = [f for f in CUSTOMER_MASTER_FIELDS if remote.get(f) is not None and remote[f] != local.get(f)]
        if diff:
            _conflict(db, run, entity="customers", key=ref, local=local, remote=remote, fields=diff)
            conflicts += 1
        else:
            applied += 1
    return applied, conflicts, rejected


def _sync_orders(
    db: Session,
    org: Organization,
    conn: ConnectorConnection,
    run: SyncRun,
    records: list[dict[str, Any]],
    actor: uuid.UUID | None,
    ip: str | None,
) -> tuple[int, int, list[dict[str, Any]]]:
    """External orders arrive as *proposed* orders (external_ref idempotent) and still need confirmation."""
    rejected: list[dict[str, Any]] = []
    applied = 0
    for i, rec in enumerate(records):
        ref = str(rec.get("external_ref") or "").strip()
        currency = str(rec.get("currency") or org.default_currency).upper()
        lines = rec.get("lines") or []
        if not ref or not isinstance(lines, list) or not lines:
            rejected.append({"index": i, "reason": "external_ref and non-empty lines required"})
            continue
        if not is_supported_currency(currency):
            rejected.append({"index": i, "reason": f"unsupported currency {currency}"})
            continue
        if db.scalar(select(Order.id).where(Order.organization_id == org.id, Order.external_ref == ref)):
            applied += 1  # already known → idempotent no-op
            continue
        parsed: list[dict[str, Any]] = []
        ok = True
        for ln in lines:
            qty = parse_decimal(ln.get("quantity")) if isinstance(ln, dict) else None
            price = parse_decimal(ln.get("unit_price")) if isinstance(ln, dict) else None
            if qty is None or qty <= 0 or price is None or price < 0:
                ok = False
                break
            parsed.append(
                {
                    "sku": ln.get("sku"),
                    "description": ln.get("description") or ln.get("sku"),
                    "quantity": qty,
                    "unit": ln.get("unit") or "EA",
                    "unit_price": price,
                    "tax_rate_pct": parse_decimal(ln.get("tax_rate_pct")) or Decimal(org.default_tax_rate),
                }
            )
        if not ok:
            rejected.append({"index": i, "reason": "every line needs a positive quantity and non-negative unit_price"})
            continue
        customer = None
        if rec.get("customer_reference"):
            customer = db.scalar(
                select(Customer).where(
                    Customer.organization_id == org.id, Customer.reference == str(rec["customer_reference"])
                )
            )
            if customer is None:
                rejected.append({"index": i, "reason": "unknown customer_reference"})
                continue
        with db.begin_nested():
            order_service.create_manual(
                db,
                org=org,
                actor_user_id=actor,
                ip=ip,
                customer_id=customer.id if customer else None,
                currency=currency,
                lines=parsed,
                source="connector",
                external_ref=ref,
                notes=f"connector {conn.kind}",
            )
        applied += 1
    return applied, 0, rejected


def resolve_conflict(
    db: Session,
    *,
    org: Organization,
    conflict: SyncConflict,
    resolution: str,
    actor_user_id: uuid.UUID,
    ip: str | None,
    note: str | None,
) -> SyncConflict:
    if conflict.status != "open":
        raise StateError("Conflict already resolved")
    if resolution not in ("keep_local", "apply_remote", "dismiss"):
        raise ValidationFailed("resolution must be keep_local, apply_remote or dismiss", field="resolution")
    if resolution == "apply_remote":
        if conflict.entity == "products":
            active = active_version_for_org(db, org.id)
            product = (
                db.scalar(
                    select(Product).where(
                        Product.catalog_version_id == active.id,
                        Product.sku_normalized == conflict.entity_key.strip().upper(),
                    )
                )
                if active is not None
                else None
            )
            if product is None:
                raise StateError("Remote value is a new SKU; import it through a catalog version instead")
            for f in conflict.fields:
                val = conflict.remote_value.get(f)
                if f in ("list_price", "cost_price"):
                    setattr(product, f, Decimal(val) if val is not None else None)
                elif f in ("description", "unit"):
                    setattr(product, f, val)
        elif conflict.entity == "customers":
            customer = db.scalar(
                select(Customer).where(Customer.organization_id == org.id, Customer.reference == conflict.entity_key)
            )
            if customer is None:
                raise StateError("Customer no longer exists")
            for f in conflict.fields:
                if f in CUSTOMER_MASTER_FIELDS:
                    setattr(customer, f, conflict.remote_value.get(f))
    conflict.status = {"keep_local": "resolved_local", "apply_remote": "resolved_remote", "dismiss": "dismissed"}[
        resolution
    ]
    conflict.resolved_by = actor_user_id
    conflict.resolved_at = utcnow()
    conflict.resolution_note = note
    db.flush()
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=actor_user_id,
        action="sync_conflict.resolve",
        entity_type="sync_conflict",
        entity_id=conflict.id,
        ip=ip,
        summary={
            "resolution": resolution,
            "entity": conflict.entity,
            "key": conflict.entity_key,
            "fields": conflict.fields,
        },
    )
    return conflict
