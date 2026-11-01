"""PostgreSQL-backed job queue (ADR-012 amendment): SKIP LOCKED claims, idempotency keys, capped exponential
backoff (30 s · 2^n, max 1 h, 5 attempts) and a dead-letter state. Payloads carry ids only — never message bodies."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.base import utcnow
from app.db.session import set_tenant, set_worker_scope
from app.models.followup import FollowUpSchedule, JobRun
from app.models.org import Organization
from app.services.comms.providers import ProviderError

logger = logging.getLogger(__name__)
Handler = Callable[[Session, Organization, dict[str, Any]], None]
MAX_BACKOFF = timedelta(hours=1)


def backoff(attempt: int) -> timedelta:
    return min(timedelta(seconds=30 * (2 ** max(attempt - 1, 0))), MAX_BACKOFF)


def enqueue(
    db: Session,
    *,
    org_id: uuid.UUID,
    kind: str,
    idempotency_key: str,
    payload: dict[str, Any],
    run_at: Any | None = None,
    max_attempts: int = 5,
) -> JobRun | None:
    """Insert a job unless the same idempotency key exists (returns None then)."""
    existing = db.scalar(
        select(JobRun).where(JobRun.organization_id == org_id, JobRun.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return None
    job = JobRun(
        organization_id=org_id,
        kind=kind,
        idempotency_key=idempotency_key,
        payload=payload,
        status="queued",
        next_run_at=run_at or utcnow(),
        max_attempts=max_attempts,
    )
    db.add(job)
    db.flush()
    return job


def _handle_send_draft(db: Session, org: Organization, payload: dict[str, Any]) -> None:
    from app.models.followup import CommunicationDraft
    from app.services.followup.service import send_draft

    draft = db.get(CommunicationDraft, uuid.UUID(payload["draft_id"]))
    if draft is None:
        return
    send_draft(
        db, org=org, draft=draft, actor=None, ip=None, mode="automation", idempotency_key=payload.get("idempotency_key")
    )


def _handle_schedule(db: Session, org: Organization, payload: dict[str, Any]) -> None:
    from app.services.followup.service import process_due_schedule

    sched = db.get(FollowUpSchedule, uuid.UUID(payload["schedule_id"]))
    if sched is None:
        return
    process_due_schedule(db, org=org, schedule=sched)


def _handle_sync_inbound(db: Session, org: Organization, payload: dict[str, Any]) -> None:
    from app.services.followup.service import sync_inbound

    sync_inbound(db, org=org)


def _handle_purge(db: Session, org: Organization, payload: dict[str, Any]) -> None:
    from app.services.intelligence import retention

    req = retention.get_request(db, org.id, uuid.UUID(payload["request_id"]))
    if req.status == "executed":
        return
    retention.purge(db, org, req)


HANDLERS: dict[str, Handler] = {
    "purge_customer": _handle_purge,
    "send_draft": _handle_send_draft,
    "schedule_followup": _handle_schedule,
    "sync_inbound": _handle_sync_inbound,
}


def enqueue_due_schedules(db: Session) -> int:
    """Scheduler tick (owner-free): one job per due pending schedule, idempotent per schedule+due time."""
    now = utcnow()
    set_worker_scope(db)
    rows = db.execute(
        text(
            "SELECT id, organization_id, due_at FROM follow_up_schedules "
            "WHERE status = 'pending' AND due_at <= :now ORDER BY due_at LIMIT 200"
        ),
        {"now": now},
    ).all()
    n = 0
    for sid, org_id, due_at in rows:
        set_tenant(db, org_id, None)
        if (
            enqueue(
                db,
                org_id=org_id,
                kind="schedule_followup",
                idempotency_key=f"schedule:{sid}:{due_at.isoformat()}",
                payload={"schedule_id": str(sid)},
            )
            is not None
        ):
            n += 1
    db.commit()
    return n


def claim_next(db: Session, worker_id: str) -> JobRun | None:
    set_worker_scope(db)
    row = db.execute(
        text(
            "UPDATE job_runs SET status = 'running', locked_at = now(), locked_by = :w, attempts = attempts + 1 "
            "WHERE id = (SELECT id FROM job_runs WHERE status = 'queued' AND next_run_at <= now() "
            "ORDER BY next_run_at FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING id"
        ),
        {"w": worker_id},
    ).first()
    if row is None:
        db.rollback()
        return None
    job_id = row[0]
    db.commit()
    set_worker_scope(db)
    job = db.get(JobRun, job_id)
    db.commit()
    return job


def run_job(db: Session, job: JobRun) -> None:
    job_id, org_id = job.id, job.organization_id
    db.expire_all()
    set_tenant(db, org_id, None)
    job = db.get(JobRun, job_id) or job
    org = db.get(Organization, job.organization_id)
    handler = HANDLERS.get(job.kind)
    try:
        if org is None or handler is None:
            raise RuntimeError(f"no handler for {job.kind}")
        handler(db, org, job.payload)
        job.status = "succeeded"
        job.finished_at = utcnow()
        job.last_error = None
        db.commit()
    except Exception as exc:  # noqa: BLE001 - every failure is recorded and either retried or dead-lettered
        db.rollback()
        set_tenant(db, org_id, None)
        job = db.get(JobRun, job_id) or job
        retryable = (
            isinstance(exc, ProviderError)
            and exc.retryable
            or not isinstance(exc, ProviderError)
            and not isinstance(exc, (ValueError, LookupError))
        )
        job.last_error = f"{type(exc).__name__}: {str(exc)[:400]}"
        if retryable and job.attempts < job.max_attempts:
            job.status = "queued"
            job.next_run_at = utcnow() + backoff(job.attempts)
        else:
            job.status = "dead" if retryable else "failed"
            job.finished_at = utcnow()
        db.commit()
        logger.warning(
            "job failed",
            extra={"extra": {"job_id": str(job.id), "kind": job.kind, "status": job.status, "attempts": job.attempts}},
        )


def run_once(db: Session, worker_id: str | None = None) -> int:
    """Process every claimable job once (used by tests and `python -m app.workers.runner --once`)."""
    wid = worker_id or get_settings().followup_worker_id
    processed = 0
    while True:
        job = claim_next(db, wid)
        if job is None:
            return processed
        run_job(db, job)
        processed += 1
