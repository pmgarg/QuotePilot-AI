from __future__ import annotations

import uuid

from fastapi import APIRouter, Query
from sqlalchemy import or_, select

from app.api.deps import DB, PrincipalDep, clean_param
from app.core.errors import NotFoundError, ValidationFailed
from app.models.customer import Customer
from app.schemas.common import CustomerIn, CustomerOut
from app.services import audit
from app.services.authz import Permission
from app.services.money import is_supported_currency

router = APIRouter(prefix="/customers", tags=["customers"])


@router.get("", response_model=list[CustomerOut])
def list_customers(
    principal: PrincipalDep, db: DB, q: str | None = Query(default=None, max_length=100)
) -> list[CustomerOut]:
    stmt = select(Customer).where(Customer.organization_id == principal.org_id, Customer.deleted_at.is_(None))
    q = clean_param(q)
    if q:
        like = f"%{q.replace('%', r'\%').replace('_', r'\_')}%"
        stmt = stmt.where(or_(Customer.name.ilike(like, escape="\\"), Customer.reference.ilike(like, escape="\\")))
    rows = db.scalars(stmt.order_by(Customer.name).limit(200)).all()
    return [CustomerOut.model_validate(c) for c in rows]


def _validate(payload: CustomerIn) -> None:
    if payload.default_currency and not is_supported_currency(payload.default_currency):
        raise ValidationFailed("Unsupported currency", field="default_currency")
    if payload.email and "@" not in payload.email:
        raise ValidationFailed("Invalid email", field="email")


@router.post("", response_model=CustomerOut, status_code=201)
def create_customer(payload: CustomerIn, principal: PrincipalDep, db: DB) -> CustomerOut:
    principal.require(Permission.EDIT_QUOTES)
    _validate(payload)
    customer = Customer(organization_id=principal.org_id, **payload.model_dump())
    if customer.default_currency:
        customer.default_currency = customer.default_currency.upper()
    db.add(customer)
    db.flush()
    audit.record(
        db,
        organization_id=principal.org_id,
        actor_user_id=principal.user.id,
        action="customer.create",
        entity_type="customer",
        entity_id=customer.id,
        ip=principal.ip,
        summary={"name": customer.name},
    )
    return CustomerOut.model_validate(customer)


@router.get("/{customer_id}", response_model=CustomerOut)
def get_customer(customer_id: uuid.UUID, principal: PrincipalDep, db: DB) -> CustomerOut:
    customer = db.scalar(
        select(Customer).where(Customer.id == customer_id, Customer.organization_id == principal.org_id)
    )
    if customer is None:
        raise NotFoundError("Customer not found")
    return CustomerOut.model_validate(customer)


@router.patch("/{customer_id}", response_model=CustomerOut)
def update_customer(customer_id: uuid.UUID, payload: CustomerIn, principal: PrincipalDep, db: DB) -> CustomerOut:
    principal.require(Permission.EDIT_QUOTES)
    _validate(payload)
    customer = db.scalar(
        select(Customer).where(Customer.id == customer_id, Customer.organization_id == principal.org_id)
    )
    if customer is None:
        raise NotFoundError("Customer not found")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(customer, k, v.upper() if k == "default_currency" and v else v)
    db.flush()
    audit.record(
        db,
        organization_id=principal.org_id,
        actor_user_id=principal.user.id,
        action="customer.update",
        entity_type="customer",
        entity_id=customer.id,
        ip=principal.ip,
        summary={"fields": sorted(payload.model_dump(exclude_unset=True).keys())},
    )
    return CustomerOut.model_validate(customer)
