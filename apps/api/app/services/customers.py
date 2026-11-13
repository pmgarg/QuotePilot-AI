"""Customer lookups shared by routers. A customer whose deletion was approved (`deleted_at` set) is no longer
processed: writes and ordinary reads behave as if it did not exist (SEC3-02)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models.customer import Customer


def get_active_customer(
    db: Session, organization_id: uuid.UUID, customer_id: uuid.UUID, *, include_deleted: bool = False
) -> Customer:
    """404 for another tenant's id, an unknown id, and — unless `include_deleted` — a customer being erased."""
    customer = db.scalar(
        select(Customer).where(Customer.id == customer_id, Customer.organization_id == organization_id)
    )
    if customer is None or (customer.deleted_at is not None and not include_deleted):
        raise NotFoundError("Customer not found")
    return customer
