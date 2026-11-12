"""Connector framework (S3-F06): adapters declare scopes, every sync is idempotent, and any change that would
overwrite local master data goes to a conflict queue instead of being applied silently.

Shipped adapters make no network calls (no paid resources): ``erp_stub`` and ``csv`` accept records *pushed*
to the sync endpoint by an operator or an external system. Pull adapters for real ERPs are documented as
future work in docs/KNOWN_LIMITATIONS.md.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
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
from app.services.comms.providers import valid_email
from app.services.intelligence import data_io
from app.services.intelligence import orders as order_service
from app.services.intelligence.inputs import MAX_PRICE, MAX_QTY, FieldError, decimal_field, order_line, text_field
from app.services.money import is_supported_currency

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
            ConnectorConnection.organization_id == org.id,
            ConnectorConnection.kind == kind,
            ConnectorConnection.status == "active",
        )
    ):
        raise StateError(f"An active '{kind}' connector already exists")
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
    scope_key = data_io.scope_key("connector", conn.id, entity)
    request_hash = hashlib.sha256(json.dumps(records, sort_keys=True, default=str).encode()).hexdigest()
    existing = data_io.find_run(db, org.id, scope_key, idempotency_key)
    if existing is not None:
        # idempotent replay of the same payload: the previous outcome is authoritative (different payload → 409)
        return data_io.replay_or_conflict(existing, request_hash)
    run = SyncRun(
        organization_id=org.id,
        connection_id=conn.id,
        kind="connector",
        scope_key=scope_key,
        request_hash=request_hash,
        idempotency_key=idempotency_key,
        entity=entity,
        status="running",
        received=len(records),
    )
    try:
        with db.begin_nested():  # savepoint: a lost race must not poison the surrounding transaction
            db.add(run)
            db.flush()
    except IntegrityError:
        # SEC3-16: a concurrent request with the same key committed first; answer as a sequential replay would
        winner = data_io.find_run(db, org.id, scope_key, idempotency_key)
        if winner is None:
            raise
        return data_io.replay_or_conflict(winner, request_hash)
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


def _reject(rejected: list[dict[str, Any]], index: int, reason: str) -> None:
    rejected.append({"index": index, "reason": reason[:200]})


def _record(rec: Any) -> dict[str, Any]:
    if not isinstance(rec, dict):
        raise FieldError("record must be an object")
    return rec


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
    for i, raw in enumerate(records):
        try:
            rec = _record(raw)
            sku = text_field(rec.get("sku"), name="sku", max_len=100, required=True) or ""
            remote: dict[str, Any] = {}
            for f in PRODUCT_MASTER_FIELDS:
                if rec.get(f) is None:
                    continue
                if f in ("list_price", "cost_price"):
                    remote[f] = str(decimal_field(rec[f], name=f, minimum=Decimal(0), maximum=MAX_PRICE))
                else:
                    remote[f] = text_field(rec[f], name=f, max_len=16 if f == "unit" else 500, required=True)
            if not remote:
                raise FieldError("no master fields supplied")
        except FieldError as exc:
            _reject(rejected, i, str(exc))
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
    for i, raw in enumerate(records):
        try:
            rec = _record(raw)
            sku = text_field(rec.get("sku"), name="sku", max_len=100, required=True) or ""
            qty = decimal_field(rec.get("quantity"), name="quantity", minimum=Decimal(0), maximum=MAX_QTY)
        except FieldError:
            _reject(rejected, i, "sku and non-negative quantity required")
            continue
        observed = _parse_ts(rec.get("observed_at"))
        if observed is None:
            _reject(rejected, i, "observed_at must be a timezone-aware ISO-8601 timestamp")
            continue
        if observed > now:
            _reject(rejected, i, "observed_at in the future")
            continue
        product = None
        if active is not None:
            product = db.scalar(
                select(Product).where(Product.catalog_version_id == active.id, Product.sku_normalized == sku.upper())
            )
        if product is None:
            _reject(rejected, i, "unknown sku")
            continue
        db.add(
            StockSnapshot(
                organization_id=org.id,
                product_id=product.id,
                source=conn.kind,
                quantity=qty,
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
    for i, raw in enumerate(records):
        try:
            rec = _record(raw)
            ref = text_field(rec.get("reference"), name="reference", max_len=100, required=True) or ""
            name = text_field(rec.get("name"), name="name", max_len=200, required=True) or ""
            email = text_field(rec.get("email"), name="email", max_len=320)
            cur_raw = text_field(rec.get("default_currency"), name="default_currency", max_len=3)
            cur = cur_raw.upper() if cur_raw else None
            if email and not valid_email(email):
                raise FieldError("invalid email")
            if cur and not is_supported_currency(cur):
                raise FieldError(f"unsupported currency {cur}")
        except FieldError as exc:
            _reject(rejected, i, str(exc))
            continue
        remote = {"name": name, "email": email, "default_currency": cur}
        existing = db.scalar(select(Customer).where(Customer.organization_id == org.id, Customer.reference == ref))
        if existing is None:
            try:
                with db.begin_nested():
                    db.add(Customer(organization_id=org.id, reference=ref, **remote))
                    db.flush()
            except DBAPIError:
                _reject(rejected, i, "record could not be stored")
                continue
            applied += 1
            continue
        if existing.deleted_at is not None:
            _reject(rejected, i, "customer is being deleted; no further processing")  # SEC3-02
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
    default_tax = Decimal(org.default_tax_rate)
    for i, raw in enumerate(records):
        try:
            rec = _record(raw)
            ref = text_field(rec.get("external_ref"), name="external_ref", max_len=100, required=True) or ""
            cur_raw = text_field(rec.get("currency"), name="currency", max_len=3)
            currency = (cur_raw or org.default_currency).upper()
            if not is_supported_currency(currency):
                raise FieldError(f"unsupported currency {currency}")
            lines = rec.get("lines")
            if not isinstance(lines, list) or not lines or len(lines) > 500:
                raise FieldError("lines must be a non-empty list (max 500)")
            parsed = [order_line(_record(ln), default_tax=default_tax) for ln in lines]
            cust_ref = text_field(rec.get("customer_reference"), name="customer_reference", max_len=100)
        except FieldError as exc:
            _reject(rejected, i, str(exc))
            continue
        if db.scalar(select(Order.id).where(Order.organization_id == org.id, Order.external_ref == ref)):
            applied += 1  # already known → idempotent no-op
            continue
        customer = None
        if cust_ref:
            customer = db.scalar(
                select(Customer).where(
                    Customer.organization_id == org.id, Customer.reference == cust_ref, Customer.deleted_at.is_(None)
                )
            )
            if customer is None:
                _reject(rejected, i, "unknown customer_reference")
                continue
        try:
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
        except DBAPIError:
            _reject(rejected, i, "record could not be stored")
            continue
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
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
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
                try:
                    if f in ("list_price", "cost_price"):
                        new_price = decimal_field(val, name=f, minimum=Decimal(0), maximum=MAX_PRICE)
                        before[f], after[f] = str(getattr(product, f)), str(new_price)
                        setattr(product, f, new_price)
                    elif f in ("description", "unit"):
                        text = text_field(val, name=f, max_len=16 if f == "unit" else 500, required=True)
                        before[f], after[f] = getattr(product, f), text
                        setattr(product, f, text)
                except FieldError as exc:
                    raise StateError(f"Remote value cannot be applied: {exc}") from exc
        elif conflict.entity == "customers":
            customer = db.scalar(
                select(Customer).where(Customer.organization_id == org.id, Customer.reference == conflict.entity_key)
            )
            if customer is None or customer.deleted_at is not None:
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
            "before": before,
            "after": after,
        },
    )
    return conflict
