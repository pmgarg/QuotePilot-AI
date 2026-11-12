"""Stage 3 routes: orders & conversion, repeat-order proposals, KPIs, FX table, CSV data I/O, connectors &
conflict queue, stock labels, customer profiles and the retention workflow. Server-authorized, CSRF-protected
(mutations), audited; cross-tenant ids return 404, role denials 403."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, File, Query, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import func, select

from app.api.deps import DB, PrincipalDep, SettingsDep, clean_param
from app.core.errors import ForbiddenError, NotFoundError, PayloadTooLarge, StateError, ValidationFailed
from app.db.base import utcnow
from app.models.catalog import Product
from app.models.customer import Customer
from app.models.followup import Contact
from app.models.intelligence import (
    ConnectorConnection,
    DeletionRequest,
    FxRate,
    Order,
    OrderProposal,
    SyncConflict,
    SyncRun,
)
from app.models.quote import Quote
from app.schemas.common import (
    ConflictOut,
    ConflictResolveIn,
    ConnectorIn,
    ConnectorOut,
    ContactOut,
    CustomerOut,
    CustomerProfileOut,
    DecisionIn,
    DeletionRequestIn,
    DeletionRequestOut,
    FxRateIn,
    FxRateOut,
    LegalHoldIn,
    OrderFromQuoteIn,
    OrderLineOut,
    OrderLinePrivileged,
    OrderOut,
    OrderSummary,
    OrderTransitionIn,
    ProposalOut,
    QuoteSummary,
    StockOut,
    SyncIn,
    SyncRunOut,
)
from app.services import audit
from app.services import quotes as quote_service
from app.services.authz import Permission, has_permission
from app.services.catalog_import import active_version_for_org
from app.services.customers import get_active_customer
from app.services.intelligence import connectors as connector_service
from app.services.intelligence import data_io, kpis, repeat_orders, retention, stock
from app.services.intelligence import orders as order_service
from app.services.money import is_supported_currency

router = APIRouter(tags=["intelligence"])


def _order_out(order: Order, principal: PrincipalDep) -> OrderOut:
    out = OrderOut.model_validate(order)
    cls = OrderLinePrivileged if has_permission(principal.role, Permission.VIEW_COSTS) else OrderLineOut
    out.lines = [cls.model_validate(ln) for ln in order.lines]
    return out


# ------------------------------------------------------------------ orders
@router.get("/orders", response_model=list[OrderSummary])
def list_orders(
    principal: PrincipalDep,
    db: DB,
    status: str | None = Query(default=None, max_length=16),
    customer_id: uuid.UUID | None = None,
) -> list[OrderSummary]:
    stmt = select(Order).where(Order.organization_id == principal.org_id)
    status = clean_param(status)
    if status:
        stmt = stmt.where(Order.status == status)
    if customer_id:
        stmt = stmt.where(Order.customer_id == customer_id)
    rows = db.scalars(stmt.order_by(Order.created_at.desc()).limit(200)).all()
    return [OrderSummary.model_validate(o) for o in rows]


@router.get("/orders/{order_id}", response_model=OrderOut)
def get_order(order_id: uuid.UUID, principal: PrincipalDep, db: DB) -> OrderOut:
    return _order_out(order_service.get_order(db, principal.org_id, order_id), principal)


@router.post("/quotes/{quote_id}/orders", response_model=OrderOut, status_code=201)
def propose_order_from_quote(
    quote_id: uuid.UUID, payload: OrderFromQuoteIn, principal: PrincipalDep, db: DB
) -> OrderOut:
    principal.require(Permission.EDIT_QUOTES)
    quote = quote_service.get_quote(db, principal.org_id, quote_id)
    order = order_service.propose_from_quote(
        db,
        org=principal.organization,
        quote=quote,
        actor_user_id=principal.user.id,
        ip=principal.ip,
        version_no=payload.version_no,
        notes=payload.notes,
    )
    return _order_out(order, principal)


def _transition(order_id: uuid.UUID, to: str, payload: OrderTransitionIn, principal: PrincipalDep, db: DB) -> OrderOut:
    order = order_service.get_order(db, principal.org_id, order_id)
    order = order_service.transition(
        db,
        org=principal.organization,
        order=order,
        to_status=to,
        actor_user_id=principal.user.id,
        ip=principal.ip,
        reason=payload.reason,
    )
    return _order_out(order, principal)


@router.post("/orders/{order_id}/confirm", response_model=OrderOut)
def confirm_order(order_id: uuid.UUID, payload: OrderTransitionIn, principal: PrincipalDep, db: DB) -> OrderOut:
    principal.require(Permission.APPROVE_ORDER)
    return _transition(order_id, "confirmed", payload, principal, db)


@router.post("/orders/{order_id}/fulfil", response_model=OrderOut)
def fulfil_order(order_id: uuid.UUID, payload: OrderTransitionIn, principal: PrincipalDep, db: DB) -> OrderOut:
    principal.require(Permission.EDIT_QUOTES)
    return _transition(order_id, "fulfilled", payload, principal, db)


@router.post("/orders/{order_id}/cancel", response_model=OrderOut)
def cancel_order(order_id: uuid.UUID, payload: OrderTransitionIn, principal: PrincipalDep, db: DB) -> OrderOut:
    principal.require(Permission.APPROVE_ORDER)
    return _transition(order_id, "cancelled", payload, principal, db)


# --------------------------------------------------------------- proposals
@router.get("/order-proposals", response_model=list[ProposalOut])
def list_proposals(
    principal: PrincipalDep, db: DB, status: str | None = Query(default=None, max_length=16)
) -> list[ProposalOut]:
    stmt = select(OrderProposal).where(OrderProposal.organization_id == principal.org_id)
    status = clean_param(status)
    if status:
        stmt = stmt.where(OrderProposal.status == status)
    return [
        ProposalOut.model_validate(p)
        for p in db.scalars(stmt.order_by(OrderProposal.created_at.desc()).limit(200)).all()
    ]


@router.post("/customers/{customer_id}/order-proposals", response_model=ProposalOut | None, status_code=201)
def generate_proposal(customer_id: uuid.UUID, principal: PrincipalDep, db: DB) -> ProposalOut | None:
    principal.require(Permission.EDIT_QUOTES)
    customer = db.scalar(
        select(Customer).where(
            Customer.id == customer_id, Customer.organization_id == principal.org_id, Customer.deleted_at.is_(None)
        )
    )
    if customer is None:
        raise NotFoundError("Customer not found")
    p = repeat_orders.generate(
        db, org=principal.organization, customer=customer, actor_user_id=principal.user.id, ip=principal.ip
    )
    return ProposalOut.model_validate(p) if p is not None else None


@router.post("/order-proposals/{proposal_id}/approve", response_model=ProposalOut)
def approve_proposal(proposal_id: uuid.UUID, payload: DecisionIn, principal: PrincipalDep, db: DB) -> ProposalOut:
    principal.require(Permission.APPROVE_ORDER)
    p = repeat_orders.get_proposal(db, principal.org_id, proposal_id)
    return ProposalOut.model_validate(
        repeat_orders.decide(
            db,
            org=principal.organization,
            proposal=p,
            approve=True,
            actor_user_id=principal.user.id,
            ip=principal.ip,
            reason=payload.reason,
        )
    )


@router.post("/order-proposals/{proposal_id}/reject", response_model=ProposalOut)
def reject_proposal(proposal_id: uuid.UUID, payload: DecisionIn, principal: PrincipalDep, db: DB) -> ProposalOut:
    principal.require(Permission.APPROVE_ORDER)
    p = repeat_orders.get_proposal(db, principal.org_id, proposal_id)
    return ProposalOut.model_validate(
        repeat_orders.decide(
            db,
            org=principal.organization,
            proposal=p,
            approve=False,
            actor_user_id=principal.user.id,
            ip=principal.ip,
            reason=payload.reason,
        )
    )


# -------------------------------------------------------------------- KPIs
@router.get("/kpis")
def get_kpis(
    principal: PrincipalDep,
    db: DB,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict[str, Any]:
    today = utcnow().date()
    rng = kpis.Range(date_from or today - timedelta(days=90), date_to or today)
    if rng.date_from > rng.date_to:
        raise ValidationFailed("date_from must be on or before date_to", field="date_from")
    if (rng.date_to - rng.date_from).days > 366 * 3:
        raise ValidationFailed("Range cannot exceed three years", field="date_to")
    return kpis.compute(db, principal.organization, rng, today=today)


@router.get("/fx-rates", response_model=list[FxRateOut])
def list_fx(principal: PrincipalDep, db: DB) -> list[FxRateOut]:
    rows = db.scalars(
        select(FxRate).where(FxRate.organization_id == principal.org_id).order_by(FxRate.as_of.desc()).limit(500)
    ).all()
    return [FxRateOut.model_validate(r) for r in rows]


@router.post("/fx-rates", response_model=FxRateOut, status_code=201)
def add_fx(payload: FxRateIn, principal: PrincipalDep, db: DB) -> FxRateOut:
    principal.require(Permission.MANAGE_CONNECTORS)
    base, quote = payload.base_currency.upper(), payload.quote_currency.upper()
    if base == quote or not is_supported_currency(base) or not is_supported_currency(quote):
        raise ValidationFailed("Unsupported or identical currency pair", field="quote_currency")
    if payload.as_of > utcnow().date():
        raise ValidationFailed("as_of cannot be in the future", field="as_of")
    existing = db.scalar(
        select(FxRate).where(
            FxRate.organization_id == principal.org_id,
            FxRate.base_currency == base,
            FxRate.quote_currency == quote,
            FxRate.as_of == payload.as_of,
        )
    )
    if existing is not None:
        raise StateError("A rate for that pair and date already exists")
    r = FxRate(
        organization_id=principal.org_id,
        base_currency=base,
        quote_currency=quote,
        rate=payload.rate,
        as_of=payload.as_of,
        source=payload.source,
        created_by=principal.user.id,
        created_at=utcnow(),
    )
    db.add(r)
    db.flush()
    audit.record(
        db,
        organization_id=principal.org_id,
        actor_user_id=principal.user.id,
        action="fx_rate.create",
        entity_type="fx_rate",
        entity_id=r.id,
        ip=principal.ip,
        summary={"pair": f"{base}/{quote}", "rate": str(payload.rate), "as_of": payload.as_of.isoformat()},
    )
    return FxRateOut.model_validate(r)


# ------------------------------------------------------------- CSV data I/O
EXPORTS = {"customers", "orders", "products"}


def _csv_response(data: bytes, filename: str) -> Response:
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


@router.get("/data/export/{entity}")
def export_data(entity: str, principal: PrincipalDep, db: DB) -> Response:
    if entity not in EXPORTS:
        raise NotFoundError("Unknown export")
    if not has_permission(principal.role, Permission.EXPORT_DATA):
        audit.record(
            db,
            organization_id=principal.org_id,
            actor_user_id=principal.user.id,
            action="export.denied",
            entity_type="export",
            ip=principal.ip,
            summary={"entity": entity, "role": principal.role},
        )
        db.commit()
        raise ForbiddenError("Bulk export is not permitted for your role", action="export_data")
    include_costs = has_permission(principal.role, Permission.VIEW_COSTS)
    if entity == "customers":
        data = data_io.export_customers(db, principal.org_id)
    elif entity == "orders":
        data = data_io.export_orders(db, principal.org_id, include_costs=include_costs)
    else:
        data = data_io.export_products(db, principal.org_id, include_costs=include_costs)
    audit.record(
        db,
        organization_id=principal.org_id,
        actor_user_id=principal.user.id,
        action=f"export.{entity}",
        entity_type="export",
        ip=principal.ip,
        summary={"entity": entity, "bytes": len(data), "include_costs": include_costs},
    )
    return _csv_response(data, f"{entity}-{utcnow().date().isoformat()}.csv")


@router.post("/data/import/{entity}", response_model=SyncRunOut, status_code=201)
async def import_data(
    entity: str,
    request: Request,
    principal: PrincipalDep,
    db: DB,
    settings: SettingsDep,
    file: UploadFile = File(...),  # noqa: B008
    idempotency_key: str = Query(min_length=1, max_length=160),
) -> SyncRunOut:
    principal.require(Permission.IMPORT_DATA)
    if entity not in ("customers", "orders"):
        raise NotFoundError("Unknown import (products are imported through catalog versions)")
    raw = await file.read(data_io.MAX_CSV_BYTES + 1)
    if len(raw) > data_io.MAX_CSV_BYTES:
        raise PayloadTooLarge("CSV larger than 5 MB")
    import hashlib

    existing = data_io.find_run(db, principal.org_id, data_io.scope_key("csv_import", None, entity), idempotency_key)
    if existing is not None:
        return SyncRunOut.model_validate(data_io.replay_or_conflict(existing, hashlib.sha256(raw).hexdigest()))
    fn = data_io.import_customers if entity == "customers" else data_io.import_orders
    run = fn(
        db,
        org=principal.organization,
        actor_user_id=principal.user.id,
        ip=principal.ip,
        raw=raw,
        idempotency_key=idempotency_key,
    )
    return SyncRunOut.model_validate(run)


@router.get("/data/runs", response_model=list[SyncRunOut])
def list_runs(principal: PrincipalDep, db: DB) -> list[SyncRunOut]:
    principal.require(Permission.IMPORT_DATA)
    rows = db.scalars(
        select(SyncRun)
        .where(SyncRun.organization_id == principal.org_id)
        .order_by(SyncRun.created_at.desc())
        .limit(100)
    ).all()
    return [SyncRunOut.model_validate(r) for r in rows]


@router.get("/data/runs/{run_id}/failed.csv")
def failed_rows(run_id: uuid.UUID, principal: PrincipalDep, db: DB) -> Response:
    principal.require(Permission.IMPORT_DATA)
    run = db.scalar(select(SyncRun).where(SyncRun.id == run_id, SyncRun.organization_id == principal.org_id))
    if run is None:
        raise NotFoundError("Import run not found")
    return _csv_response(data_io.failed_rows_csv(run), f"failed-rows-{run.id}.csv")


# -------------------------------------------------------------- connectors
@router.get("/connectors", response_model=list[ConnectorOut])
def list_connectors(principal: PrincipalDep, db: DB) -> list[ConnectorOut]:
    principal.require(Permission.MANAGE_CONNECTORS)
    rows = db.scalars(select(ConnectorConnection).where(ConnectorConnection.organization_id == principal.org_id)).all()
    return [ConnectorOut.model_validate(c) for c in rows]


@router.post("/connectors", response_model=ConnectorOut, status_code=201)
def create_connector(payload: ConnectorIn, principal: PrincipalDep, db: DB) -> ConnectorOut:
    principal.require(Permission.MANAGE_CONNECTORS)
    c = connector_service.create_connection(
        db,
        org=principal.organization,
        actor_user_id=principal.user.id,
        ip=principal.ip,
        kind=payload.kind,
        name=payload.name,
        scopes=payload.scopes,
    )
    return ConnectorOut.model_validate(c)


@router.post("/connectors/{connector_id}/sync", response_model=SyncRunOut, status_code=201)
def sync_connector(connector_id: uuid.UUID, payload: SyncIn, principal: PrincipalDep, db: DB) -> SyncRunOut:
    principal.require(Permission.MANAGE_CONNECTORS)
    conn = connector_service.get_connection(db, principal.org_id, connector_id)
    run = connector_service.sync(
        db,
        org=principal.organization,
        conn=conn,
        actor_user_id=principal.user.id,
        ip=principal.ip,
        entity=payload.entity,
        idempotency_key=payload.idempotency_key,
        records=payload.records,
    )
    return SyncRunOut.model_validate(run)


@router.post("/connectors/{connector_id}/disable", response_model=ConnectorOut)
def disable_connector(connector_id: uuid.UUID, principal: PrincipalDep, db: DB) -> ConnectorOut:
    principal.require(Permission.MANAGE_CONNECTORS)
    conn = connector_service.get_connection(db, principal.org_id, connector_id)
    conn.status = "disabled"
    audit.record(
        db,
        organization_id=principal.org_id,
        actor_user_id=principal.user.id,
        action="connector.disable",
        entity_type="connector",
        entity_id=conn.id,
        ip=principal.ip,
    )
    return ConnectorOut.model_validate(conn)


@router.post("/connectors/{connector_id}/enable", response_model=ConnectorOut)
def enable_connector(connector_id: uuid.UUID, principal: PrincipalDep, db: DB) -> ConnectorOut:
    """Undo a disable (SEC3-07). Refused with 409 while another connector of the same kind is active."""
    principal.require(Permission.MANAGE_CONNECTORS)
    conn = connector_service.get_connection(db, principal.org_id, connector_id)
    if conn.status != "active":
        other = db.scalar(
            select(ConnectorConnection.id).where(
                ConnectorConnection.organization_id == principal.org_id,
                ConnectorConnection.kind == conn.kind,
                ConnectorConnection.status == "active",
            )
        )
        if other is not None:
            raise StateError(f"Another '{conn.kind}' connector is active; disable it first")
        conn.status = "active"
        audit.record(
            db,
            organization_id=principal.org_id,
            actor_user_id=principal.user.id,
            action="connector.enable",
            entity_type="connector",
            entity_id=conn.id,
            ip=principal.ip,
        )
    return ConnectorOut.model_validate(conn)


@router.get("/sync-conflicts", response_model=list[ConflictOut])
def list_conflicts(
    principal: PrincipalDep, db: DB, status: str | None = Query(default="open", max_length=16)
) -> list[ConflictOut]:
    principal.require(Permission.MANAGE_CONNECTORS)
    stmt = select(SyncConflict).where(SyncConflict.organization_id == principal.org_id)
    status = clean_param(status)
    if status:
        stmt = stmt.where(SyncConflict.status == status)
    rows = db.scalars(stmt.order_by(SyncConflict.created_at.desc()).limit(200)).all()
    return [ConflictOut.model_validate(c) for c in rows]


@router.post("/sync-conflicts/{conflict_id}/resolve", response_model=ConflictOut)
def resolve_conflict(
    conflict_id: uuid.UUID, payload: ConflictResolveIn, principal: PrincipalDep, db: DB
) -> ConflictOut:
    principal.require(Permission.MANAGE_CONNECTORS)
    c = db.scalar(
        select(SyncConflict).where(SyncConflict.id == conflict_id, SyncConflict.organization_id == principal.org_id)
    )
    if c is None:
        raise NotFoundError("Conflict not found")
    return ConflictOut.model_validate(
        connector_service.resolve_conflict(
            db,
            org=principal.organization,
            conflict=c,
            resolution=payload.resolution,
            actor_user_id=principal.user.id,
            ip=principal.ip,
            note=payload.note,
        )
    )


# ------------------------------------------------------------------- stock
@router.get("/stock", response_model=list[StockOut])
def stock_levels(
    principal: PrincipalDep, db: DB, q: str | None = Query(default=None, max_length=100)
) -> list[StockOut]:
    active = active_version_for_org(db, principal.org_id)
    if active is None:
        return []
    stmt = select(Product).where(Product.catalog_version_id == active.id)
    q = clean_param(q)
    if q:
        like = f"%{q.replace('%', r'\%').replace('_', r'\_')}%"
        stmt = stmt.where(Product.sku.ilike(like, escape="\\") | Product.description.ilike(like, escape="\\"))
    products = db.scalars(stmt.order_by(Product.sku).limit(200)).all()
    latest = stock.latest_snapshots(db, principal.org_id, [p.id for p in products])
    return [StockOut(product_id=p.id, sku=p.sku, **stock.label(latest.get(p.id))) for p in products]


# -------------------------------------------------------- customer profile
@router.get("/customers/{customer_id}/profile", response_model=CustomerProfileOut)
def customer_profile(customer_id: uuid.UUID, principal: PrincipalDep, db: DB) -> CustomerProfileOut:
    customer = get_active_customer(
        db,
        principal.org_id,
        customer_id,
        include_deleted=has_permission(principal.role, Permission.MANAGE_RETENTION),
    )
    contacts = db.scalars(select(Contact).where(Contact.customer_id == customer.id)).all()
    quotes = db.scalars(
        select(Quote).where(Quote.customer_id == customer.id).order_by(Quote.created_at.desc()).limit(50)
    ).all()
    orders = db.scalars(
        select(Order).where(Order.customer_id == customer.id).order_by(Order.created_at.desc()).limit(50)
    ).all()
    open_props = db.scalar(
        select(func.count()).where(OrderProposal.customer_id == customer.id, OrderProposal.status == "proposed")
    )
    won = sum(1 for q in quotes if quote_service.effective_status(q) == "won")
    stats: dict[str, Any] = {
        "quotes": len(quotes),
        "won_quotes": won,
        "orders": len(orders),
        "confirmed_orders": sum(1 for o in orders if o.status in ("confirmed", "fulfilled")),
        "last_order_at": max((o.confirmed_at for o in orders if o.confirmed_at), default=None),
    }
    if has_permission(principal.role, Permission.VIEW_COSTS):
        margins: list[Decimal] = [
            (ln.unit_price - ln.cost_price) / ln.unit_price * 100
            for o in orders
            if o.status in ("confirmed", "fulfilled")
            for ln in o.lines
            if ln.cost_price is not None and ln.unit_price
        ]
        stats["avg_margin_pct"] = (
            str((sum(margins, Decimal(0)) / len(margins)).quantize(Decimal("0.1"))) if margins else None
        )
    return CustomerProfileOut(
        customer=CustomerOut.model_validate(customer),
        contacts=[ContactOut.model_validate(c) for c in contacts],
        quotes=[
            QuoteSummary(
                id=q.id,
                number=q.number,
                customer_id=q.customer_id,
                customer_name=customer.name,
                currency=q.currency,
                status=q.status,
                effective_status=quote_service.effective_status(q),
                current_version_no=q.current_version_no,
                grand_total=quote_service.current_version(q).grand_total,
                valid_until=q.valid_until,
                created_at=q.created_at,
                updated_at=q.updated_at,
            )
            for q in quotes
        ],
        orders=[OrderSummary.model_validate(o) for o in orders],
        open_proposals=int(open_props or 0),
        stats=stats,
    )


# --------------------------------------------------------------- retention
@router.get("/retention/requests", response_model=list[DeletionRequestOut])
def list_deletion_requests(principal: PrincipalDep, db: DB) -> list[DeletionRequestOut]:
    principal.require(Permission.MANAGE_RETENTION)
    rows = db.scalars(
        select(DeletionRequest)
        .where(DeletionRequest.organization_id == principal.org_id)
        .order_by(DeletionRequest.created_at.desc())
        .limit(200)
    ).all()
    return [DeletionRequestOut.model_validate(r) for r in rows]


@router.post("/retention/requests", response_model=DeletionRequestOut, status_code=201)
def create_deletion_request(payload: DeletionRequestIn, principal: PrincipalDep, db: DB) -> DeletionRequestOut:
    principal.require(Permission.EDIT_QUOTES)
    r = retention.request_deletion(
        db,
        org=principal.organization,
        actor_user_id=principal.user.id,
        ip=principal.ip,
        customer_id=payload.customer_id,
        reason=payload.reason,
    )
    return DeletionRequestOut.model_validate(r)


def _decide(
    request_id: uuid.UUID, approve: bool, payload: DecisionIn, principal: PrincipalDep, db: DB
) -> DeletionRequestOut:
    principal.require(Permission.MANAGE_RETENTION)
    r = retention.get_request(db, principal.org_id, request_id)
    return DeletionRequestOut.model_validate(
        retention.decide(
            db,
            org=principal.organization,
            req=r,
            approve=approve,
            actor_user_id=principal.user.id,
            ip=principal.ip,
            reason=payload.reason,
        )
    )


@router.post("/retention/requests/{request_id}/approve", response_model=DeletionRequestOut)
def approve_deletion(request_id: uuid.UUID, payload: DecisionIn, principal: PrincipalDep, db: DB) -> DeletionRequestOut:
    return _decide(request_id, True, payload, principal, db)


@router.post("/retention/requests/{request_id}/reject", response_model=DeletionRequestOut)
def reject_deletion(request_id: uuid.UUID, payload: DecisionIn, principal: PrincipalDep, db: DB) -> DeletionRequestOut:
    return _decide(request_id, False, payload, principal, db)


@router.post("/retention/requests/{request_id}/legal-hold", response_model=DeletionRequestOut)
def legal_hold(request_id: uuid.UUID, payload: LegalHoldIn, principal: PrincipalDep, db: DB) -> DeletionRequestOut:
    principal.require(Permission.MANAGE_RETENTION)
    r = retention.get_request(db, principal.org_id, request_id)
    return DeletionRequestOut.model_validate(
        retention.set_legal_hold(
            db, org=principal.organization, req=r, hold=payload.hold, actor_user_id=principal.user.id, ip=principal.ip
        )
    )
