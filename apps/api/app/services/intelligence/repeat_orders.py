"""Repeat-order assistant (S3-F03): rule-based proposals computed from *this tenant's own* confirmed order
history. Nothing is ordered automatically — a proposal becomes an order only after an approver decides, and
that order still passes the normal confirmation gate. Every proposal carries human-readable evidence."""

from __future__ import annotations

import statistics
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError, StateError, ValidationFailed
from app.db.base import utcnow
from app.models.catalog import Product
from app.models.customer import Customer
from app.models.intelligence import Order, OrderLine, OrderProposal
from app.models.org import Organization
from app.services import audit
from app.services.catalog_import import active_version_for_org
from app.services.intelligence import orders as order_service

MIN_OCCURRENCES = 2
HORIZON_DAYS = 14


def _history(db: Session, org_id: uuid.UUID, customer_id: uuid.UUID) -> list[Order]:
    return list(
        db.scalars(
            select(Order)
            .where(
                Order.organization_id == org_id,
                Order.customer_id == customer_id,
                Order.status.in_(("confirmed", "fulfilled")),
            )
            .order_by(Order.confirmed_at)
        ).all()
    )


def analyse(db: Session, org: Organization, customer: Customer, now: datetime | None = None) -> list[dict[str, Any]]:
    """Return candidate repeat lines: SKUs ordered >= MIN_OCCURRENCES times whose predicted next date is near."""
    now = now or utcnow()
    history = _history(db, org.id, customer.id)
    by_sku: dict[str, list[tuple[datetime, OrderLine]]] = defaultdict(list)
    for o in history:
        when = o.confirmed_at or o.created_at
        for ln in o.lines:
            if ln.sku:
                by_sku[ln.sku].append((when, ln))
    candidates: list[dict[str, Any]] = []
    for sku, entries in by_sku.items():
        if len(entries) < MIN_OCCURRENCES:
            continue
        entries.sort(key=lambda e: e[0])
        dates = [e[0] for e in entries]
        gaps = [(b - a).days for a, b in zip(dates, dates[1:], strict=False)]
        interval = max(int(statistics.median(gaps)), 1) if gaps else None
        if interval is None:
            continue
        predicted = dates[-1] + timedelta(days=interval)
        if predicted > now + timedelta(days=HORIZON_DAYS):
            continue
        qty = statistics.median([e[1].quantity for e in entries])
        last = entries[-1][1]
        candidates.append(
            {
                "sku": sku,
                "description": last.description,
                "unit": last.unit,
                "quantity": str(Decimal(qty)),
                "times_ordered": len(entries),
                "median_interval_days": interval,
                "last_ordered_at": dates[-1].date().isoformat(),
                "predicted_at": predicted.date().isoformat(),
                "last_unit_price": str(last.unit_price),
                "last_currency": history[-1].currency,
            }
        )
    candidates.sort(key=lambda c: c["predicted_at"])
    return candidates


def generate(
    db: Session, *, org: Organization, customer: Customer, actor_user_id: uuid.UUID, ip: str | None
) -> OrderProposal | None:
    """Create one *proposed* repeat-order proposal for the customer (or None if there is nothing to propose)."""
    open_existing = db.scalar(
        select(OrderProposal).where(
            OrderProposal.organization_id == org.id,
            OrderProposal.customer_id == customer.id,
            OrderProposal.status == "proposed",
        )
    )
    if open_existing is not None:
        raise StateError("Customer already has an open proposal; decide it first")
    candidates = analyse(db, org, customer)
    if not candidates:
        return None
    currency = customer.default_currency or org.default_currency
    evidence = [
        f"{c['sku']} ordered {c['times_ordered']}× every ~{c['median_interval_days']} days; "
        f"last on {c['last_ordered_at']}, next expected {c['predicted_at']}"
        for c in candidates
    ]
    evidence.append("Source: this organisation's confirmed orders only; no external data was used.")
    proposal = OrderProposal(
        organization_id=org.id,
        customer_id=customer.id,
        kind="repeat",
        status="proposed",
        lines=candidates,
        evidence=evidence,
        currency=currency,
    )
    db.add(proposal)
    db.flush()
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=actor_user_id,
        action="order_proposal.generate",
        entity_type="order_proposal",
        entity_id=proposal.id,
        ip=ip,
        summary={"customer_id": str(customer.id), "lines": len(candidates)},
    )
    return proposal


def get_proposal(db: Session, org_id: uuid.UUID, proposal_id: uuid.UUID) -> OrderProposal:
    p = db.scalar(select(OrderProposal).where(OrderProposal.id == proposal_id, OrderProposal.organization_id == org_id))
    if p is None:
        raise NotFoundError("Proposal not found")
    return p


def decide(
    db: Session,
    *,
    org: Organization,
    proposal: OrderProposal,
    approve: bool,
    actor_user_id: uuid.UUID,
    ip: str | None,
    reason: str | None,
) -> OrderProposal:
    if proposal.status != "proposed":
        raise StateError(f"Proposal is already {proposal.status}")
    if not approve and not (reason and reason.strip()):
        raise ValidationFailed("Rejecting a proposal requires a reason", field="reason")
    proposal.decided_by = actor_user_id
    proposal.decided_at = utcnow()
    proposal.decision_reason = reason
    if not approve:
        proposal.status = "rejected"
    else:
        # Price from the *current active catalog* where the SKU still exists; otherwise the last paid price,
        # explicitly labelled so the confirming approver can see it.
        active = active_version_for_org(db, org.id)
        lines: list[dict[str, Any]] = []
        for c in proposal.lines:
            product = None
            if active is not None:
                product = db.scalar(
                    select(Product).where(
                        Product.catalog_version_id == active.id,
                        Product.sku_normalized == str(c["sku"]).strip().upper(),
                        Product.currency == proposal.currency,
                    )
                )
            if product is not None:
                lines.append(
                    {
                        "product_id": product.id,
                        "sku": product.sku,
                        "description": product.description,
                        "unit": product.unit,
                        "quantity": c["quantity"],
                        "unit_price": product.list_price,
                        "cost_price": product.cost_price,
                    }
                )
            else:
                lines.append(
                    {
                        "sku": c["sku"],
                        "description": f"{c['description']} (priced from last order — verify)",
                        "unit": c["unit"],
                        "quantity": c["quantity"],
                        "unit_price": c["last_unit_price"],
                    }
                )
        order = order_service.create_manual(
            db,
            org=org,
            actor_user_id=actor_user_id,
            ip=ip,
            customer_id=proposal.customer_id,
            currency=proposal.currency,
            lines=lines,
            source="repeat",
            notes=f"repeat-order proposal {proposal.id}",
        )
        proposal.status = "converted"
        proposal.order_id = order.id
    db.flush()
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=actor_user_id,
        action="order_proposal.approve" if approve else "order_proposal.reject",
        entity_type="order_proposal",
        entity_id=proposal.id,
        ip=ip,
        summary={"reason": reason, "order_id": str(proposal.order_id) if proposal.order_id else None},
    )
    return proposal
