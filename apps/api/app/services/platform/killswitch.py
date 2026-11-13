"""Kill switch (S4-F04 / S4-A07): a tenant switch (owner/admin, audited) or the platform-wide environment flag stops
every assistant turn and workflow step immediately; running workflows are marked `killed`."""

from __future__ import annotations

import uuid
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import KillSwitchEngaged, StateError
from app.db.base import utcnow
from app.models.platform import KillSwitch, WorkflowRun
from app.services import audit


def active_switch(db: Session, org_id: uuid.UUID) -> KillSwitch | None:
    return db.scalar(select(KillSwitch).where(KillSwitch.organization_id == org_id, KillSwitch.released_at.is_(None)))


def is_engaged(db: Session, org_id: uuid.UUID) -> bool:
    return get_settings().platform_kill_switch or active_switch(db, org_id) is not None


def check(db: Session, org_id: uuid.UUID) -> None:
    if is_engaged(db, org_id):
        raise KillSwitchEngaged("Automation is stopped by the kill switch")


def engage(db: Session, *, org_id: uuid.UUID, actor_user_id: uuid.UUID, ip: str | None, reason: str) -> KillSwitch:
    if active_switch(db, org_id) is not None:
        raise StateError("Kill switch is already engaged")
    now = utcnow()
    terminated = cast(
        "CursorResult[Any]",
        db.execute(
            update(WorkflowRun)
            .where(
                WorkflowRun.organization_id == org_id,
                WorkflowRun.status.in_(("running", "awaiting_approval")),
            )
            .values(status="killed", finished_at=now, failure_explanation=f"kill switch engaged: {reason[:200]}")
        ),
    ).rowcount
    sw = KillSwitch(
        organization_id=org_id,
        scope="org",
        reason=reason,
        engaged_by=actor_user_id,
        engaged_at=now,
        runs_terminated=int(terminated),
    )
    db.add(sw)
    db.flush()
    audit.record(
        db,
        organization_id=org_id,
        actor_user_id=actor_user_id,
        action="kill_switch.engage",
        entity_type="kill_switch",
        entity_id=sw.id,
        ip=ip,
        summary={"reason": reason, "runs_terminated": int(terminated)},
    )
    return sw


def release(db: Session, *, org_id: uuid.UUID, actor_user_id: uuid.UUID, ip: str | None) -> KillSwitch:
    sw = active_switch(db, org_id)
    if sw is None:
        raise StateError("Kill switch is not engaged")
    sw.released_at = utcnow()
    sw.released_by = actor_user_id
    db.flush()
    audit.record(
        db,
        organization_id=org_id,
        actor_user_id=actor_user_id,
        action="kill_switch.release",
        entity_type="kill_switch",
        entity_id=sw.id,
        ip=ip,
    )
    return sw
