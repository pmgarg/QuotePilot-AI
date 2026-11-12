"""Retention & deletion workflow (S3-F08): request → approve (owner/admin, not the requester) → immediate soft
delete → worker purge after the grace period. Quotes and orders are kept for commercial record-keeping with
the customer pseudonymised; contacts (personal data) are removed. Every step is audited; legal hold blocks it."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, cast

from sqlalchemy import delete, select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ForbiddenError, NotFoundError, StateError, ValidationFailed
from app.db.base import utcnow
from app.db.session import set_tenant
from app.models.customer import Customer
from app.models.followup import CommunicationDraft, Contact, FollowUpSchedule
from app.models.intelligence import DeletionRequest, SyncConflict, SyncRun
from app.models.org import Organization
from app.models.quote import Quote, QuoteVersion
from app.services import audit
from app.services.storage import get_storage


def get_request(db: Session, org_id: uuid.UUID, request_id: uuid.UUID) -> DeletionRequest:
    r = db.scalar(
        select(DeletionRequest).where(DeletionRequest.id == request_id, DeletionRequest.organization_id == org_id)
    )
    if r is None:
        raise NotFoundError("Deletion request not found")
    return r


def request_deletion(
    db: Session, *, org: Organization, actor_user_id: uuid.UUID, ip: str | None, customer_id: uuid.UUID, reason: str
) -> DeletionRequest:
    customer = db.scalar(select(Customer).where(Customer.id == customer_id, Customer.organization_id == org.id))
    if customer is None:
        raise NotFoundError("Customer not found")
    if customer.deleted_at is not None:
        raise StateError("Customer is already deleted")
    if not reason.strip():
        raise ValidationFailed("A reason is required", field="reason")
    if db.scalar(
        select(DeletionRequest.id).where(
            DeletionRequest.organization_id == org.id,
            DeletionRequest.subject_id == customer_id,
            DeletionRequest.status.in_(("requested", "approved")),
        )
    ):
        raise StateError("An open deletion request already exists for this customer")
    req = DeletionRequest(
        organization_id=org.id,
        subject_type="customer",
        subject_id=customer_id,
        reason=reason.strip(),
        status="requested",
        requested_by=actor_user_id,
    )
    db.add(req)
    db.flush()
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=actor_user_id,
        action="retention.request",
        entity_type="deletion_request",
        entity_id=req.id,
        ip=ip,
        summary={"customer_id": str(customer_id)},
    )
    return req


def decide(
    db: Session,
    *,
    org: Organization,
    req: DeletionRequest,
    approve: bool,
    actor_user_id: uuid.UUID,
    ip: str | None,
    reason: str | None,
) -> DeletionRequest:
    if req.status != "requested":
        raise StateError(f"Request is already {req.status}")
    if req.legal_hold and approve:
        raise StateError("Subject is under legal hold")
    if approve and req.requested_by == actor_user_id and not org.allow_self_approval:
        raise ForbiddenError("You cannot approve your own deletion request", action="manage_retention")
    req.decided_by = actor_user_id
    req.decided_at = utcnow()
    if not approve:
        if not (reason and reason.strip()):
            raise ValidationFailed("Rejecting requires a reason", field="reason")
        req.status = "rejected"
        req.summary = {"rejection_reason": reason}
    else:
        req.status = "approved"
        req.purge_after = utcnow() + timedelta(days=get_settings().retention_grace_days)
        soft = _soft_delete(db, org.id, req.subject_id)
        req.summary = {"soft_deleted": soft}
    db.flush()
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=actor_user_id,
        action="retention.approve" if approve else "retention.reject",
        entity_type="deletion_request",
        entity_id=req.id,
        ip=ip,
        summary={
            "customer_id": str(req.subject_id),
            "purge_after": req.purge_after.isoformat() if req.purge_after else None,
        },
    )
    return req


def set_legal_hold(
    db: Session, *, org: Organization, req: DeletionRequest, hold: bool, actor_user_id: uuid.UUID, ip: str | None
) -> DeletionRequest:
    if req.status == "executed":
        raise StateError("Already purged")
    req.legal_hold = hold
    db.flush()
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=actor_user_id,
        action="retention.legal_hold",
        entity_type="deletion_request",
        entity_id=req.id,
        ip=ip,
        summary={"hold": hold},
    )
    return req


def _soft_delete(db: Session, org_id: uuid.UUID, customer_id: uuid.UUID) -> dict[str, Any]:
    """Immediately stop processing: hide the customer, cancel pending follow-ups, opt out contacts."""
    now = utcnow()
    customer = db.scalar(select(Customer).where(Customer.id == customer_id, Customer.organization_id == org_id))
    if customer is None:
        return {}
    customer.deleted_at = now
    quote_ids = select(Quote.id).where(Quote.organization_id == org_id, Quote.customer_id == customer_id)
    schedules = cast(
        "CursorResult[Any]",
        db.execute(
            update(FollowUpSchedule)
            .where(
                FollowUpSchedule.organization_id == org_id,
                FollowUpSchedule.quote_id.in_(quote_ids),
                FollowUpSchedule.status == "pending",
            )
            .values(status="cancelled", last_error="customer deletion requested")
        ),
    ).rowcount
    drafts = cast(
        "CursorResult[Any]",
        db.execute(
            update(CommunicationDraft)
            .where(
                CommunicationDraft.organization_id == org_id,
                CommunicationDraft.quote_id.in_(quote_ids),
                CommunicationDraft.status.in_(("draft", "approved")),
            )
            .values(status="cancelled")
        ),
    ).rowcount
    contacts = cast(
        "CursorResult[Any]",
        db.execute(
            update(Contact)
            .where(
                Contact.organization_id == org_id, Contact.customer_id == customer_id, Contact.opted_out_at.is_(None)
            )
            .values(opted_out_at=now, opt_out_source="deletion")
        ),
    ).rowcount
    return {"schedules_cancelled": int(schedules), "drafts_cancelled": int(drafts), "contacts_opted_out": int(contacts)}


REDACTED = {"redacted": "erased"}
MAX_PURGE_JOBS = 10


def _redact_copies(db: Session, org: Organization, customer: Customer, short: str) -> dict[str, int]:
    """SEC3-03: remove the subject's identifiers from copies outside the customer row — the connector conflict
    queue, import-run reports (failed rows) and approved quote documents (snapshot + stored PDF)."""
    counts = {"conflicts_redacted": 0, "import_rows_redacted": 0, "documents_pseudonymised": 0}
    if customer.reference:
        for conflict in db.scalars(
            select(SyncConflict).where(
                SyncConflict.organization_id == org.id,
                SyncConflict.entity == "customers",
                SyncConflict.entity_key == customer.reference,
            )
        ):
            conflict.local_value = dict(REDACTED)
            conflict.remote_value = dict(REDACTED)
            conflict.entity_key = f"erased-{short}"
            if conflict.status == "open":
                conflict.status = "dismissed"
                conflict.resolution_note = "customer erased"
            counts["conflicts_redacted"] += 1
    needles = [v.lower() for v in (customer.reference, customer.email, customer.name) if v]
    if needles:
        for run in db.scalars(select(SyncRun).where(SyncRun.organization_id == org.id, SyncRun.kind == "csv_import")):
            report = dict(run.report or {})
            rows = []
            changed = False
            for failed in report.get("failed_rows") or []:
                blob = " ".join(str(v).lower() for v in (failed.get("values") or {}).values())
                if any(n in blob for n in needles):
                    failed = {**failed, "values": dict(REDACTED)}
                    changed = True
                    counts["import_rows_redacted"] += 1
                rows.append(failed)
            if changed:
                report["failed_rows"] = rows
                run.report = report
    storage = get_storage()
    for version in db.scalars(
        select(QuoteVersion)
        .join(Quote, Quote.id == QuoteVersion.quote_id)
        .where(Quote.organization_id == org.id, Quote.customer_id == customer.id)
    ):
        snap = version.document_snapshot
        if isinstance(snap, dict) and snap.get("customer"):
            version.document_snapshot = {**snap, "customer": {"name": f"Deleted customer {short}", "reference": None}}
            counts["documents_pseudonymised"] += 1
        if version.pdf_storage_key:
            # the stored PDF carries the customer name: it is deleted and re-rendered from the pseudonymised
            # snapshot on the next download (erasure takes precedence over byte-identity for this one field)
            try:
                storage.delete(version.pdf_storage_key)
            except OSError:
                pass
            version.pdf_storage_key = None
            version.pdf_sha256 = None
    return counts


def purge(db: Session, org: Organization, req: DeletionRequest) -> dict[str, Any]:
    """Irreversible: pseudonymise the customer, delete contacts and drafts, redact copies of the subject's
    identifiers. Quotes/orders keep the customer id and their commercial content."""
    if req.status != "approved":
        raise StateError("Only approved requests are purged")
    if req.legal_hold:
        raise StateError("Legal hold")
    short = str(req.subject_id)[:8]
    customer = db.scalar(select(Customer).where(Customer.id == req.subject_id, Customer.organization_id == org.id))
    copies = _redact_copies(db, org, customer, short) if customer is not None else {}
    contacts = cast(
        "CursorResult[Any]",
        db.execute(delete(Contact).where(Contact.organization_id == org.id, Contact.customer_id == req.subject_id)),
    ).rowcount
    quote_ids = select(Quote.id).where(Quote.organization_id == org.id, Quote.customer_id == req.subject_id)
    drafts = cast(
        "CursorResult[Any]",
        db.execute(
            delete(CommunicationDraft).where(
                CommunicationDraft.organization_id == org.id, CommunicationDraft.quote_id.in_(quote_ids)
            )
        ),
    ).rowcount
    db.execute(
        update(Customer)
        .where(Customer.id == req.subject_id, Customer.organization_id == org.id)
        .values(name=f"Deleted customer {short}", email=None, notes=None, reference=None)
    )
    db.execute(
        text("UPDATE rfqs SET pasted_text = NULL WHERE organization_id = :org AND customer_id = :cid"),
        {"org": str(org.id), "cid": str(req.subject_id)},
    )
    req.status = "executed"
    req.executed_at = utcnow()
    req.summary = {
        **(req.summary or {}),
        "contacts_deleted": int(contacts),
        "drafts_deleted": int(drafts),
        **copies,
    }
    db.flush()
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=None,
        action="retention.purged",
        entity_type="deletion_request",
        entity_id=req.id,
        ip=None,
        summary={"customer_id": str(req.subject_id), **req.summary},
    )
    return req.summary


def enqueue_due_purges(db: Session) -> int:
    """Worker tick: queue one purge job per approved request whose grace period has elapsed. A request whose
    previous purge job ended without executing it (dead-lettered, e.g. while a legal hold was in place) gets a new
    job, so an approved erasure cannot silently stall (SEC3-01); after MAX_PURGE_JOBS it is flagged instead."""
    from app.services.jobs import queue

    n = 0
    for rid, org_id in queue.due_purge_requests(db):
        set_tenant(db, org_id, None)
        prior = [
            st
            for (st,) in db.execute(
                text(
                    "SELECT status FROM job_runs WHERE organization_id = :o AND kind = 'purge_customer' "
                    "AND payload->>'request_id' = :r"
                ),
                {"o": str(org_id), "r": str(rid)},
            )
        ]
        if any(st in ("queued", "running") for st in prior):
            continue
        if len(prior) >= MAX_PURGE_JOBS:
            db.execute(
                text(
                    "UPDATE deletion_requests SET summary = summary || CAST(:s AS jsonb) "
                    "WHERE id = :r AND organization_id = :o"
                ),
                {"s": '{"purge_stalled": true}', "r": str(rid), "o": str(org_id)},
            )
            continue
        key = f"purge:{rid}" if not prior else f"purge:{rid}:{len(prior)}"
        if (
            queue.enqueue(
                db, org_id=org_id, kind="purge_customer", idempotency_key=key, payload={"request_id": str(rid)}
            )
            is not None
        ):
            n += 1
    db.commit()
    return n
