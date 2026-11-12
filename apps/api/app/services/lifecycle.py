"""Quote lifecycle after approval (S2-F01). Only recorded facts move a quote; nothing is inferred."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import StateError, ValidationFailed
from app.db.base import utcnow
from app.models.followup import QuoteStatusEvent
from app.models.quote import Quote
from app.services import audit

# allowed manual/system transitions: from → {to: requires_evidence}
TRANSITIONS: dict[str, dict[str, bool]] = {
    "approved": {"sent": False, "lost": True},
    "sent": {"acknowledged": True, "awaiting_customer": False, "won": True, "lost": True},
    "acknowledged": {"awaiting_customer": False, "won": True, "lost": True},
    "awaiting_customer": {"won": True, "lost": True, "acknowledged": True},
    "expired": {"lost": True},
}
TERMINAL = {"won", "lost", "withdrawn"}


def effective_lifecycle_status(quote: Quote, today: datetime | None = None) -> str:
    """Adds expiry on top of recorded status: approved/sent/awaiting quotes past validity are 'expired'."""
    now = today or utcnow()
    if quote.status in ("approved", "sent", "acknowledged", "awaiting_customer") and quote.valid_until:
        if quote.valid_until < now.date():
            return "expired"
    return quote.status


def allowed_transitions(quote: Quote) -> dict[str, bool]:
    return dict(TRANSITIONS.get(effective_lifecycle_status(quote), {}))


def transition(
    db: Session,
    *,
    quote: Quote,
    to_status: str,
    actor_user_id: uuid.UUID | None,
    source: str,
    evidence: str | None,
    occurred_at: datetime | None,
    ip: str | None,
    expected_lock_version: int | None = None,
) -> QuoteStatusEvent:
    from app.services.quotes import _check_lock

    current = effective_lifecycle_status(quote)
    options = TRANSITIONS.get(current, {})
    if to_status not in options:
        raise StateError(f"Cannot move a quote from '{current}' to '{to_status}'")
    if options[to_status] and not (evidence and evidence.strip()):
        raise ValidationFailed(
            f"Moving to '{to_status}' requires evidence of what actually happened "
            "(e.g. 'customer replied by phone on …')",
            field="evidence",
        )
    if expected_lock_version is not None:
        _check_lock(db, quote, expected_lock_version)
    when = occurred_at or utcnow()
    if when > utcnow():
        raise ValidationFailed("occurred_at cannot be in the future", field="occurred_at")
    event = QuoteStatusEvent(
        organization_id=quote.organization_id,
        quote_id=quote.id,
        from_status=current,
        to_status=to_status,
        source=source,
        evidence=(evidence or "").strip()[:2000] or None,
        actor_user_id=actor_user_id,
        occurred_at=when,
        created_at=utcnow(),
    )
    db.add(event)
    quote.status = to_status
    db.flush()
    audit.record(
        db,
        organization_id=quote.organization_id,
        actor_user_id=actor_user_id,
        action="quote.transition",
        entity_type="quote",
        entity_id=quote.id,
        ip=ip,
        summary={"from": current, "to": to_status, "source": source, "has_evidence": bool(evidence)},
    )
    return event


def status_events(db: Session, quote: Quote) -> list[QuoteStatusEvent]:
    return list(
        db.scalars(
            select(QuoteStatusEvent)
            .where(QuoteStatusEvent.quote_id == quote.id, QuoteStatusEvent.organization_id == quote.organization_id)
            .order_by(QuoteStatusEvent.occurred_at)
        )
    )
