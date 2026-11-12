"""Quote → order conversion with an approval gate (S3-F02) and an append-only order event trail.

An order is *proposed* (from a quote, a repeat-order proposal, an import or a connector) and only becomes
*confirmed* when a user with ``approve_order`` confirms it. Separation of duties mirrors quote approval:
the proposer may not confirm their own proposal unless the organisation explicitly allows self-approval.
Prices on an order are copied from the approved quote document — they are never recomputed or invented.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.errors import ForbiddenError, NotFoundError, StateError, ValidationFailed
from app.db.base import utcnow
from app.models.intelligence import Order, OrderEvent, OrderLine
from app.models.org import Organization
from app.models.quote import Quote
from app.services import audit
from app.services.lifecycle import effective_lifecycle_status
from app.services.money import quantize
from app.services.quotes import get_version

CONVERTIBLE_LIFECYCLE = {"approved", "sent", "acknowledged", "awaiting_customer", "won"}
TRANSITIONS: dict[str, set[str]] = {
    "proposed": {"confirmed", "cancelled"},
    "confirmed": {"fulfilled", "cancelled"},
    "fulfilled": set(),
    "cancelled": set(),
}


def allocate_number(db: Session, organization_id: uuid.UUID) -> str:
    seq = db.execute(
        text("UPDATE organizations SET order_seq = order_seq + 1 WHERE id = :org RETURNING order_seq"),
        {"org": str(organization_id)},
    ).scalar_one()
    return f"SO-{utcnow().year}-{int(seq):05d}"


def get_order(db: Session, organization_id: uuid.UUID, order_id: uuid.UUID) -> Order:
    order = db.scalar(select(Order).where(Order.id == order_id, Order.organization_id == organization_id))
    if order is None:
        raise NotFoundError("Order not found")
    return order


def _event(order: Order, kind: str, *, actor: uuid.UUID | None, source: str, detail: str | None) -> OrderEvent:
    ev = OrderEvent(
        organization_id=order.organization_id,
        order_id=order.id,
        kind=kind,
        detail=detail,
        actor_user_id=actor,
        source=source,
        occurred_at=utcnow(),
    )
    order.events.append(ev)
    return ev


def recompute_totals(order: Order, org: Organization) -> None:
    subtotal = Decimal("0")
    tax = Decimal("0")
    for line in order.lines:
        net = quantize(line.quantity * line.unit_price, order.currency, org.rounding_mode)
        line_tax = quantize(net * line.tax_rate_pct / Decimal(100), order.currency, org.rounding_mode)
        line.line_total = net + line_tax
        subtotal += net
        tax += line_tax
    order.subtotal = subtotal
    order.tax_total = tax
    order.grand_total = subtotal + tax


def propose_from_quote(
    db: Session,
    *,
    org: Organization,
    quote: Quote,
    actor_user_id: uuid.UUID,
    ip: str | None,
    version_no: int | None = None,
    notes: str | None = None,
) -> Order:
    """Create a *proposed* order from an approved quote version. Requires a later confirmation step."""
    lifecycle = effective_lifecycle_status(quote)
    if lifecycle not in CONVERTIBLE_LIFECYCLE:
        raise StateError(f"Only approved quotes can be converted to orders (quote is '{lifecycle}')")
    version = get_version(quote, version_no or quote.current_version_no)
    if version.version_no != quote.current_version_no:
        # SEC3-09: an earlier approved version is superseded by the current one and must not be ordered from
        raise StateError("Only the current approved version of a quote can be converted to an order")
    if version.status != "approved":
        raise StateError("Only an approved quote version can be converted to an order")
    existing = db.scalar(
        select(Order).where(
            Order.organization_id == org.id,
            Order.quote_id == quote.id,
            Order.status.in_(("proposed", "confirmed", "fulfilled")),
        )
    )
    if existing is not None:
        raise StateError(f"Quote already has an open order {existing.number}")
    order = Order(
        organization_id=org.id,
        number=allocate_number(db, org.id),
        customer_id=quote.customer_id,
        quote_id=quote.id,
        quote_version_no=version.version_no,
        currency=quote.currency,
        status="proposed",
        source="quote",
        created_by=actor_user_id,
        notes=notes,
    )
    for ln in version.lines:
        if ln.unit_price is None:
            raise StateError("Approved quote contains an unpriced line; cannot convert")
        order.lines.append(
            OrderLine(
                organization_id=org.id,
                line_no=ln.line_no,
                product_id=ln.product_id,
                sku=ln.sku,
                description=ln.description,
                quantity=ln.quantity,
                unit=ln.unit,
                unit_price=ln.unit_price * (Decimal(1) - ln.discount_pct / Decimal(100)),
                cost_price=ln.cost_price,
                tax_rate_pct=ln.tax_rate_pct,
                # amounts are copied from the approved document, never re-derived: a re-rounded discounted unit
                # price can differ from the approved line total by one minor unit
                line_total=ln.line_total,
            )
        )
    order.subtotal = version.subtotal - version.discount_total
    order.tax_total = version.tax_total
    order.grand_total = version.grand_total
    db.add(order)
    db.flush()
    _event(
        order,
        "proposed",
        actor=actor_user_id,
        source="quote",
        detail=f"from quote {quote.number} v{version.version_no}",
    )
    db.flush()
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=actor_user_id,
        action="order.propose",
        entity_type="order",
        entity_id=order.id,
        ip=ip,
        summary={"quote_id": str(quote.id), "version_no": version.version_no, "number": order.number},
    )
    return order


def create_manual(
    db: Session,
    *,
    org: Organization,
    actor_user_id: uuid.UUID | None,
    ip: str | None,
    customer_id: uuid.UUID | None,
    currency: str,
    lines: list[dict[str, Any]],
    source: str,
    external_ref: str | None = None,
    notes: str | None = None,
) -> Order:
    """Proposed order from explicit lines (repeat-order proposal, CSV import, connector). Still needs confirmation."""
    if not lines:
        raise ValidationFailed("An order needs at least one line", field="lines")
    order = Order(
        organization_id=org.id,
        number=allocate_number(db, org.id),
        customer_id=customer_id,
        currency=currency,
        status="proposed",
        source=source,
        external_ref=external_ref,
        created_by=actor_user_id,
        notes=notes,
    )
    for i, ln in enumerate(lines, start=1):
        order.lines.append(
            OrderLine(
                organization_id=org.id,
                line_no=i,
                product_id=ln.get("product_id"),
                sku=ln.get("sku"),
                description=str(ln.get("description") or ln.get("sku") or f"Line {i}"),
                quantity=Decimal(str(ln["quantity"])),
                unit=str(ln.get("unit") or "EA"),
                unit_price=Decimal(str(ln["unit_price"])),
                cost_price=ln.get("cost_price"),
                tax_rate_pct=Decimal(
                    str(org.default_tax_rate if ln.get("tax_rate_pct") is None else ln["tax_rate_pct"])
                ),  # an explicit 0 % stays 0 % (SEC3-04)
            )
        )
    recompute_totals(order, org)
    db.add(order)
    db.flush()
    _event(order, "proposed", actor=actor_user_id, source=source, detail=notes)
    db.flush()
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=actor_user_id,
        action="order.propose",
        entity_type="order",
        entity_id=order.id,
        ip=ip,
        summary={"source": source, "number": order.number, "external_ref": external_ref},
    )
    return order


def transition(
    db: Session,
    *,
    org: Organization,
    order: Order,
    to_status: str,
    actor_user_id: uuid.UUID,
    ip: str | None,
    reason: str | None,
) -> Order:
    if to_status not in TRANSITIONS.get(order.status, set()):
        raise StateError(f"Cannot move an order from '{order.status}' to '{to_status}'")
    if to_status == "confirmed":
        if order.created_by == actor_user_id and not org.allow_self_approval:
            raise ForbiddenError("You cannot confirm an order you proposed yourself", action="approve_order")
        if not order.lines:
            raise StateError("Order has no lines")
        order.confirmed_at = utcnow()
        order.confirmed_by = actor_user_id
    if to_status == "cancelled" and not (reason and reason.strip()):
        raise ValidationFailed("Cancelling an order requires a reason", field="reason")
    order.status = to_status
    _event(order, to_status, actor=actor_user_id, source="manual", detail=reason)
    db.flush()
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=actor_user_id,
        action=f"order.{to_status}",
        entity_type="order",
        entity_id=order.id,
        ip=ip,
        summary={"number": order.number, "reason": reason},
    )
    return order
