from __future__ import annotations

import uuid

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from sqlalchemy import select

from app.api.deps import DB, PrincipalDep, SettingsDep, clean_param
from app.api.routers.catalogs import read_upload
from app.core.errors import NotFoundError, UnsupportedMedia, ValidationFailed
from app.models.catalog import Product
from app.models.customer import Customer
from app.models.rfq import RFQ, RFQItem
from app.schemas.common import (
    CandidateOut,
    QuoteOut,
    RFQItemCreate,
    RFQItemOut,
    RFQItemUpdate,
    RFQOut,
    RFQSummary,
    RFQTextIn,
)
from app.services import quotes as quote_service
from app.services import rfqs as rfq_service
from app.services.authz import Permission
from app.services.money import is_supported_currency

router = APIRouter(prefix="/rfqs", tags=["rfqs"])


ITEM_FIELDS = (
    "id",
    "line_no",
    "raw_text",
    "source_ref",
    "sku_hint",
    "description",
    "quantity",
    "unit_raw",
    "unit_normalized",
    "match_status",
    "selected_product_id",
    "unit_mismatch",
    "unit_confirmed",
    "is_duplicate",
    "duplicate_confirmed",
    "is_custom",
    "flags",
    "notes",
)


def _candidates(item: RFQItem, products: dict[uuid.UUID, Product]) -> list[CandidateOut]:
    return [
        CandidateOut(
            product_id=c.product_id,
            sku=products[c.product_id].sku,
            description=products[c.product_id].description,
            unit=products[c.product_id].unit,
            currency=products[c.product_id].currency,
            list_price=products[c.product_id].list_price,
            category=c.category,
            score=c.score,
            reason=c.reason,
            matched_on=c.matched_on,
            rank=c.rank,
        )
        for c in item.candidates
        if c.product_id in products
    ]


def _item_dict(item: RFQItem, products: dict[uuid.UUID, Product]) -> RFQItemOut:
    data = {f: getattr(item, f) for f in ITEM_FIELDS}
    data["candidates"] = _candidates(item, products)
    return RFQItemOut.model_validate(data)


def _products_for(db: DB, items: list[RFQItem]) -> dict[uuid.UUID, Product]:
    ids = {c.product_id for item in items for c in item.candidates}
    if not ids:
        return {}
    return {p.id: p for p in db.scalars(select(Product).where(Product.id.in_(ids))).all()}


def _rfq_out(db: DB, rfq: RFQ) -> RFQOut:
    db.refresh(rfq)
    products = _products_for(db, list(rfq.items))
    data = {
        f: getattr(rfq, f)
        for f in (
            "id",
            "customer_id",
            "reference",
            "source_kind",
            "source_file_id",
            "status",
            "failure_reason",
            "currency",
            "catalog_version_id",
            "parse_meta",
            "created_at",
            "updated_at",
        )
    }
    data["items"] = [_item_dict(i, products) for i in rfq.items]
    return RFQOut.model_validate(data)


def _resolve_customer(db: DB, org_id: uuid.UUID, customer_id: uuid.UUID | None) -> Customer | None:
    if customer_id is None:
        return None
    customer = db.scalar(select(Customer).where(Customer.id == customer_id, Customer.organization_id == org_id))
    if customer is None:
        raise NotFoundError("Customer not found")
    return customer


def _currency(explicit: str | None, customer: Customer | None, default: str) -> str:
    cur = (
        explicit or (customer.default_currency if customer and customer.default_currency else None) or default
    ).upper()
    if not is_supported_currency(cur):
        raise ValidationFailed("Unsupported currency", field="currency")
    return cur


@router.post("", response_model=RFQOut, status_code=201)
async def create_rfq(
    request: Request,
    principal: PrincipalDep,
    db: DB,
    settings: SettingsDep,
    file: UploadFile | None = File(default=None),
    customer_id: str | None = Form(default=None),
    reference: str | None = Form(default=None),
    currency: str | None = Form(default=None),
) -> RFQOut:
    principal.require(Permission.EDIT_QUOTES)
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        try:
            body = RFQTextIn.model_validate(await request.json())
        except ValueError as exc:
            raise ValidationFailed("Invalid JSON body") from exc
        customer = _resolve_customer(db, principal.org_id, body.customer_id)
        rfq = rfq_service.create_rfq_from_text(
            db,
            organization_id=principal.org_id,
            actor_user_id=principal.user.id,
            ip=principal.ip,
            text=body.text,
            customer_id=customer.id if customer else None,
            reference=body.reference,
            currency=_currency(body.currency, customer, principal.organization.default_currency),
        )
        return _rfq_out(db, rfq)
    if file is None:
        raise UnsupportedMedia("Send multipart/form-data with a file, or application/json with text.")
    cust_uuid: uuid.UUID | None = None
    if customer_id:
        try:
            cust_uuid = uuid.UUID(customer_id)
        except ValueError as exc:
            raise ValidationFailed("customer_id must be a UUID", field="customer_id") from exc
    customer = _resolve_customer(db, principal.org_id, cust_uuid)
    content = await read_upload(file, settings.max_upload_bytes)
    rfq = rfq_service.create_rfq_from_file(
        db,
        organization_id=principal.org_id,
        actor_user_id=principal.user.id,
        ip=principal.ip,
        filename=file.filename or "rfq",
        content=content,
        customer_id=customer.id if customer else None,
        reference=reference,
        currency=_currency(currency, customer, principal.organization.default_currency),
    )
    return _rfq_out(db, rfq)


@router.get("", response_model=list[RFQSummary])
def list_rfqs(
    principal: PrincipalDep,
    db: DB,
    status: str | None = Query(default=None, max_length=16),
    q: str | None = Query(default=None, max_length=100),
) -> list[RFQSummary]:
    stmt = select(RFQ).where(RFQ.organization_id == principal.org_id)
    status = clean_param(status)
    if status:
        stmt = stmt.where(RFQ.status == status)
    q = clean_param(q)
    if q:
        like = f"%{q.replace('%', r'\%').replace('_', r'\_')}%"
        stmt = stmt.where(RFQ.reference.ilike(like, escape="\\"))
    rows = db.scalars(stmt.order_by(RFQ.created_at.desc()).limit(200)).all()
    return [RFQSummary.model_validate(r) for r in rows]


@router.get("/{rfq_id}", response_model=RFQOut)
def get_rfq(rfq_id: uuid.UUID, principal: PrincipalDep, db: DB) -> RFQOut:
    return _rfq_out(db, rfq_service.get_rfq(db, principal.org_id, rfq_id))


@router.post("/{rfq_id}/reparse", response_model=RFQOut)
def reparse(rfq_id: uuid.UUID, principal: PrincipalDep, db: DB) -> RFQOut:
    principal.require(Permission.EDIT_QUOTES)
    rfq = rfq_service.get_rfq(db, principal.org_id, rfq_id)
    rfq_service.parse_rfq(db, rfq, actor_user_id=principal.user.id, ip=principal.ip)
    from app.services import audit

    audit.record(
        db,
        organization_id=principal.org_id,
        actor_user_id=principal.user.id,
        action="rfq.reparse",
        entity_type="rfq",
        entity_id=rfq.id,
        ip=principal.ip,
        summary={"status": rfq.status},
    )
    return _rfq_out(db, rfq)


@router.post("/{rfq_id}/items", response_model=RFQItemOut, status_code=201)
def add_item(rfq_id: uuid.UUID, payload: RFQItemCreate, principal: PrincipalDep, db: DB) -> RFQItemOut:
    principal.require(Permission.EDIT_QUOTES)
    rfq = rfq_service.get_rfq(db, principal.org_id, rfq_id)
    item = rfq_service.add_item(
        db,
        rfq,
        actor_user_id=principal.user.id,
        ip=principal.ip,
        description=payload.description,
        quantity=payload.quantity,
        unit=payload.unit,
        sku_hint=payload.sku_hint,
    )
    return _item_out(db, item)


def _item_out(db: DB, item: RFQItem) -> RFQItemOut:
    db.refresh(item)
    return _item_dict(item, _products_for(db, [item]))


@router.patch("/{rfq_id}/items/{item_id}", response_model=RFQItemOut)
def update_item(
    rfq_id: uuid.UUID, item_id: uuid.UUID, payload: RFQItemUpdate, principal: PrincipalDep, db: DB
) -> RFQItemOut:
    principal.require(Permission.EDIT_QUOTES)
    rfq = rfq_service.get_rfq(db, principal.org_id, rfq_id)
    item = rfq_service.get_item(db, rfq, item_id)
    fields = payload.model_fields_set
    unset = object
    item = rfq_service.update_item(
        db,
        rfq,
        item,
        actor_user_id=principal.user.id,
        ip=principal.ip,
        selected_product_id=payload.selected_product_id if "selected_product_id" in fields else unset,
        quantity=payload.quantity if "quantity" in fields else unset,
        unit=payload.unit if "unit" in fields else unset,
        description=payload.description if "description" in fields else unset,
        unit_confirmed=payload.unit_confirmed,
        duplicate_confirmed=payload.duplicate_confirmed,
        is_custom=payload.is_custom,
        notes=payload.notes if "notes" in fields else unset,
    )
    return _item_out(db, item)


@router.delete("/{rfq_id}/items/{item_id}", status_code=204)
def delete_item(rfq_id: uuid.UUID, item_id: uuid.UUID, principal: PrincipalDep, db: DB) -> None:
    principal.require(Permission.EDIT_QUOTES)
    rfq = rfq_service.get_rfq(db, principal.org_id, rfq_id)
    item = rfq_service.get_item(db, rfq, item_id)
    rfq_service.delete_item(db, rfq, item, actor_user_id=principal.user.id, ip=principal.ip)


@router.post("/{rfq_id}/generate-quote", response_model=QuoteOut, status_code=201)
def generate_quote(rfq_id: uuid.UUID, principal: PrincipalDep, db: DB) -> QuoteOut:
    principal.require(Permission.EDIT_QUOTES)
    rfq = rfq_service.get_rfq(db, principal.org_id, rfq_id)
    quote = quote_service.generate_from_rfq(
        db, org=principal.organization, actor_user_id=principal.user.id, ip=principal.ip, rfq=rfq
    )
    from app.api.routers.quotes import quote_out

    return quote_out(db, principal, quote)
