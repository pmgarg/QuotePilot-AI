"""Audit events are written inside the caller's transaction so they commit atomically with the change."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.core.logging import redact, request_id_var
from app.models.audit import AuditEvent


def record(
    db: Session,
    *,
    organization_id: uuid.UUID | None,
    actor_user_id: uuid.UUID | None,
    action: str,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    ip: str | None = None,
    summary: dict[str, Any] | None = None,
) -> AuditEvent:
    event = AuditEvent(
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        request_id=request_id_var.get(),
        ip=ip,
        summary=redact(summary or {}),
    )
    db.add(event)
    db.flush()
    return event
