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
from app.core.errors import AppError, RateLimited
from app.db.base import utcnow
from app.db.session import set_tenant, set_worker_scope
from app.models.followup import FollowUpSchedule, JobRun
from app.models.org import Organization
from app.services.comms.providers import ProviderError, scrub_error

logger = logging.getLogger(__name__)
Handler = Callable[[Session, Organization, dict[str, Any]], None]
MAX_BACKOFF = timedelta(hours=1)


def backoff(attempt: int) -> timedelta:
    # the exponent is capped before the power is computed: enqueue(max_attempts=...) is unbounded (QA O-1)
    return min(timedelta(seconds=30 * (2 ** min(max(attempt - 1, 0), 20))), MAX_BACKOFF)


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


def enqueue_due_schedules(
    db: Session, *, org_id: uuid.UUID | None = None, limit: int = 200, scan_cap: int = 5000
) -> int:
    """Scheduler tick: one job per due pending schedule, idempotent per schedule+due time.

    With `org_id` (API path) only that tenant is scanned under the normal tenant scope — the cross-tenant worker
    scope is never bound on a request session (S2-SEC-01). Without it (the worker process) every tenant is scanned.
    Schedules that already have a job are skipped by paging past them (keyset on due_at, id), so a pile of
    stuck rows in one tenant cannot starve the others (S2-SEC-03)."""
    now = utcnow()
    enqueued = scanned = 0
    last: tuple[Any, Any] | None = None
    while enqueued < limit and scanned < scan_cap:
        if org_id is None:
            set_worker_scope(db)
        else:
            set_tenant(db, org_id, None)
        after_due, after_id = last if last is not None else (None, None)
        rows = db.execute(
            text(
                "SELECT id, organization_id, due_at FROM follow_up_schedules "
                "WHERE status = 'pending' AND due_at <= :now "
                "AND (CAST(:org AS uuid) IS NULL OR organization_id = CAST(:org AS uuid)) "
                "AND (CAST(:after_due AS timestamptz) IS NULL "
                "     OR (due_at, id) > (CAST(:after_due AS timestamptz), CAST(:after_id AS uuid))) "
                "ORDER BY due_at, id LIMIT 200"
            ),
            {"now": now, "org": org_id, "after_due": after_due, "after_id": after_id},
        ).all()
        if not rows:
            break
        for sid, row_org, due_at in rows:
            set_tenant(db, row_org, None)
            job = enqueue(
                db,
                org_id=row_org,
                kind="schedule_followup",
                idempotency_key=f"schedule:{sid}:{due_at.isoformat()}",
                payload={"schedule_id": str(sid)},
            )
            if job is not None:
                enqueued += 1
                if enqueued >= limit:
                    break
        scanned += len(rows)
        last = (rows[-1][2], rows[-1][0])
    db.commit()
    return enqueued


def due_purge_requests(db: Session, limit: int = 200) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """(request id, organisation id) of approved deletion requests whose grace period is over and that are not on
    legal hold — across tenants. Worker process only: this module is the single place that binds the cross-tenant
    worker scope (S2-SEC-01); callers switch to the owning tenant's scope before touching anything else."""
    set_worker_scope(db)
    rows = db.execute(
        text(
            "SELECT id, organization_id FROM deletion_requests WHERE status = 'approved' AND legal_hold = false "
            "AND purge_after <= now() ORDER BY purge_after LIMIT :limit"
        ),
        {"limit": limit},
    ).all()
    return [(rid, org_id) for rid, org_id in rows]


def reclaim_abandoned(
    db: Session, org_id: uuid.UUID | None = None, *, kind: str | None = None, count_dead: bool = True
) -> int:
    """Visibility timeout: a job `running` for longer than `job_visibility_timeout_seconds` belongs to a worker that
    died. It goes back to `queued` (attempt count preserved, so the cap still bounds retries) or, when its attempts
    are exhausted, to `dead` through the same terminal path as a failed run (`_fail_schedule`, so a schedule becomes
    `failed` and an admin retry re-arms it). Each transition is one conditional UPDATE ... RETURNING: two concurrent
    reclaimers cannot both win a row (the second re-evaluates the predicate after the first commits). `org_id`
    restricts the work to one tenant under the normal tenant scope; the worker scope is bound only here.
    State changes of jobs are recorded as structured log lines (no actor exists to audit), like `run_job` does.
    `kind` restricts the reclaim to one job kind; `count_dead=False` makes the return value the number of jobs put
    back to `queued` only (used by the purge scheduler, which re-queues dead purge jobs itself)."""
    timeout = get_settings().job_visibility_timeout_seconds
    params = {"org": org_id, "t": timeout, "kind": kind}
    reclaimed = 0
    if org_id is None:
        set_worker_scope(db)
    else:
        set_tenant(db, org_id, None)
    requeued = db.execute(
        text(
            "UPDATE job_runs SET status = 'queued', next_run_at = now(), locked_at = NULL, locked_by = NULL, "
            "last_error = 'abandoned: worker lost the job (visibility timeout)' "
            "WHERE status = 'running' AND locked_at < now() - make_interval(secs => :t) "
            "AND (CAST(:org AS uuid) IS NULL OR organization_id = CAST(:org AS uuid)) "
            "AND (CAST(:kind AS text) IS NULL OR kind = CAST(:kind AS text)) "
            "AND attempts < max_attempts RETURNING id, kind"
        ),
        params,
    ).all()
    dead = db.execute(
        text(
            "UPDATE job_runs SET status = 'dead', finished_at = now(), "
            "last_error = 'abandoned: worker lost the job and attempts are exhausted' "
            "WHERE status = 'running' AND locked_at < now() - make_interval(secs => :t) "
            "AND (CAST(:org AS uuid) IS NULL OR organization_id = CAST(:org AS uuid)) "
            "AND (CAST(:kind AS text) IS NULL OR kind = CAST(:kind AS text)) "
            "AND attempts >= max_attempts RETURNING id, organization_id"
        ),
        params,
    ).all()
    db.commit()
    for job_id, kind in requeued:
        reclaimed += 1
        logger.warning(
            "job reclaimed",
            extra={"extra": {"job_id": str(job_id), "kind": kind, "status": "queued", "timeout_s": timeout}},
        )
    for job_id, job_org in dead:
        reclaimed += 1 if count_dead else 0
        set_tenant(db, job_org, None)
        job = db.get(JobRun, job_id)
        if job is not None:
            _fail_schedule(db, job)
            db.commit()
            logger.warning(
                "job reclaimed",
                extra={"extra": {"job_id": str(job_id), "kind": job.kind, "status": "dead", "attempts": job.attempts}},
            )
    return reclaimed


def claim_next(db: Session, worker_id: str, org_id: uuid.UUID | None = None) -> JobRun | None:
    """Claim one due job (SKIP LOCKED). `org_id` restricts the claim to one tenant under the normal tenant scope."""

    def scope() -> None:
        if org_id is None:
            set_worker_scope(db)
        else:
            set_tenant(db, org_id, None)

    scope()
    row = db.execute(
        text(
            "UPDATE job_runs SET status = 'running', locked_at = now(), locked_by = :w, attempts = attempts + 1 "
            "WHERE id = (SELECT id FROM job_runs WHERE status = 'queued' AND next_run_at <= now() "
            "AND (CAST(:org AS uuid) IS NULL OR organization_id = CAST(:org AS uuid)) "
            "ORDER BY next_run_at FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING id"
        ),
        {"w": worker_id, "org": org_id},
    ).first()
    if row is None:
        db.rollback()
        return None
    job_id = row[0]
    db.commit()
    scope()
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
        # A refusal by our own rules (consent withdrawn, opt-out, wrong state, validation) is deterministic: retrying
        # it five times only burns attempts, so it is dead-lettered at once. Transient provider failures and the
        # hourly rate limit are retried with backoff.
        policy_refusal = isinstance(exc, AppError) and not isinstance(exc, RateLimited)
        if isinstance(exc, ProviderError):
            retryable = exc.retryable
        elif policy_refusal or isinstance(exc, (ValueError, LookupError)):
            retryable = False
        else:
            retryable = True
        job.last_error = scrub_error(f"{type(exc).__name__}: {exc}")
        if retryable and job.attempts < job.max_attempts:
            job.status = "queued"
            job.next_run_at = utcnow() + backoff(job.attempts)
        else:
            job.status = "dead" if (retryable or policy_refusal) else "failed"
            job.finished_at = utcnow()
            _fail_schedule(db, job)
        db.commit()
        logger.warning(
            "job failed",
            extra={"extra": {"job_id": str(job.id), "kind": job.kind, "status": job.status, "attempts": job.attempts}},
        )


def _fail_schedule(db: Session, job: JobRun) -> None:
    """A schedule whose job gave up must not stay 'pending' (it would look scheduled and be re-scanned forever):
    it becomes 'failed' with the reason visible; an admin retry of the job re-arms it."""
    if job.kind != "schedule_followup":
        return
    try:
        sched = db.get(FollowUpSchedule, uuid.UUID(str(job.payload.get("schedule_id"))))
    except ValueError:
        return
    if sched is not None and sched.status == "pending":
        sched.status = "failed"
        sched.last_error = (job.last_error or "job failed")[:500]


def run_once(
    db: Session, worker_id: str | None = None, *, org_id: uuid.UUID | None = None, max_jobs: int | None = None
) -> int:
    """Reclaim abandoned jobs, then process claimable jobs once (tests and the worker tick). With `org_id` only that
    tenant's jobs are claimed, under the normal tenant scope; `max_jobs` bounds the work done in one call."""
    wid = worker_id or get_settings().followup_worker_id
    reclaim_abandoned(db, org_id)
    processed = 0
    while max_jobs is None or processed < max_jobs:
        job = claim_next(db, wid, org_id)
        if job is None:
            break
        run_job(db, job)
        processed += 1
    return processed
