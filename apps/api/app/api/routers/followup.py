"""Stage 2 routes: contacts, follow-up policy & templates, lifecycle transitions, drafts, schedules, timeline,
integrations and the dead-letter view. Every state change is server-authorized, CSRF-protected and audited."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.deps import DB, PrincipalDep, SettingsDep, clean_param
from app.core.errors import ForbiddenError, NotFoundError, ValidationFailed
from app.db.base import utcnow
from app.models.followup import (
    CommunicationDraft,
    CommunicationEvent,
    Contact,
    FollowUpSchedule,
    IntegrationConnection,
    JobRun,
    MessageTemplate,
)
from app.schemas.common import (
    CancelIn,
    CommEventOut,
    ContactIn,
    ContactOut,
    DraftCreate,
    DraftOut,
    DraftUpdate,
    IntegrationCreate,
    IntegrationOut,
    JobOut,
    OptOutIn,
    PolicyOut,
    PolicyUpdate,
    QuoteOut,
    RescheduleIn,
    ScheduleOut,
    SimulatedInboundIn,
    StatusEventOut,
    TemplateOut,
    TemplateUpdate,
    TransitionIn,
)
from app.services import audit
from app.services import lifecycle as lifecycle_service
from app.services import quotes as quote_service
from app.services.authz import Permission, has_permission
from app.services.comms.providers import InboundMessage, valid_email
from app.services.customers import get_active_customer
from app.services.followup import service as fu
from app.services.followup import templates as tpl
from app.services.jobs import queue

router = APIRouter(tags=["follow-up"])


# ---------------------------------------------------------------- contacts
@router.get("/customers/{customer_id}/contacts", response_model=list[ContactOut])
def list_contacts(customer_id: uuid.UUID, principal: PrincipalDep, db: DB) -> list[ContactOut]:
    get_active_customer(
        db,
        principal.org_id,
        customer_id,
        include_deleted=has_permission(principal.role, Permission.MANAGE_RETENTION),
    )
    rows = db.scalars(
        select(Contact)
        .where(Contact.organization_id == principal.org_id, Contact.customer_id == customer_id)
        .order_by(Contact.is_primary.desc(), Contact.created_at)
    ).all()
    return [ContactOut.model_validate(c) for c in rows]


@router.post("/customers/{customer_id}/contacts", response_model=ContactOut, status_code=201)
def create_contact(customer_id: uuid.UUID, payload: ContactIn, principal: PrincipalDep, db: DB) -> ContactOut:
    principal.require(Permission.EDIT_QUOTES)
    get_active_customer(db, principal.org_id, customer_id)
    if payload.email and not valid_email(payload.email):
        raise ValidationFailed("Invalid e-mail address", field="email")
    c = Contact(organization_id=principal.org_id, customer_id=customer_id, **payload.model_dump())
    db.add(c)
    db.flush()
    audit.record(
        db,
        organization_id=principal.org_id,
        actor_user_id=principal.user.id,
        action="contact.create",
        entity_type="contact",
        entity_id=c.id,
        ip=principal.ip,
        summary={"customer_id": str(customer_id)},
    )
    return ContactOut.model_validate(c)


def _contact(db: DB, principal: PrincipalDep, contact_id: uuid.UUID) -> Contact:
    c = db.scalar(select(Contact).where(Contact.id == contact_id, Contact.organization_id == principal.org_id))
    if c is None:
        raise NotFoundError("Contact not found")
    return c


@router.patch("/contacts/{contact_id}", response_model=ContactOut)
def update_contact(contact_id: uuid.UUID, payload: ContactIn, principal: PrincipalDep, db: DB) -> ContactOut:
    principal.require(Permission.EDIT_QUOTES)
    c = _contact(db, principal, contact_id)
    get_active_customer(db, principal.org_id, c.customer_id)  # no edits to the contacts of a customer being erased
    if payload.email and not valid_email(payload.email):
        raise ValidationFailed("Invalid e-mail address", field="email")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(c, k, v)
    db.flush()
    audit.record(
        db,
        organization_id=principal.org_id,
        actor_user_id=principal.user.id,
        action="contact.update",
        entity_type="contact",
        entity_id=c.id,
        ip=principal.ip,
        summary={"fields": sorted(payload.model_dump(exclude_unset=True).keys())},
    )
    return ContactOut.model_validate(c)


@router.post("/contacts/{contact_id}/opt-out", response_model=ContactOut)
def set_opt_out(contact_id: uuid.UUID, payload: OptOutIn, principal: PrincipalDep, db: DB) -> ContactOut:
    principal.require(Permission.EDIT_QUOTES)
    c = _contact(db, principal, contact_id)
    if payload.opted_out:
        c.opted_out_at = c.opted_out_at or utcnow()
        c.opt_out_source = payload.source
        for s in db.scalars(
            select(FollowUpSchedule).where(
                FollowUpSchedule.organization_id == principal.org_id,
                FollowUpSchedule.contact_id == c.id,
                FollowUpSchedule.status == "pending",
            )
        ):
            s.status = "suppressed"
            s.last_error = "contact opted out"
    else:
        if not has_permission(principal.role, Permission.MANAGE_ORG):
            raise ForbiddenError(
                "Only an owner/admin may re-enable messaging for an opted-out contact", action="opt_in"
            )
        c.opted_out_at = None
        c.opt_out_source = None
    db.flush()
    audit.record(
        db,
        organization_id=principal.org_id,
        actor_user_id=principal.user.id,
        action="contact.opt_out" if payload.opted_out else "contact.opt_in",
        entity_type="contact",
        entity_id=c.id,
        ip=principal.ip,
        summary={"source": payload.source},
    )
    return ContactOut.model_validate(c)


# ---------------------------------------------------------------- policy & templates
@router.get("/follow-up/policy", response_model=PolicyOut)
def get_policy(principal: PrincipalDep, db: DB) -> PolicyOut:
    return PolicyOut.model_validate(fu.ensure_policy(db, principal.org_id))


@router.patch("/follow-up/policy", response_model=PolicyOut)
def update_policy(payload: PolicyUpdate, principal: PrincipalDep, db: DB) -> PolicyOut:
    principal.require(Permission.MANAGE_ORG)
    pol = fu.ensure_policy(db, principal.org_id)
    data = payload.model_dump(exclude_unset=True)
    if "offsets_days" in data and data["offsets_days"] is not None:
        if any((not isinstance(d, int)) or d < 0 or d > 365 for d in data["offsets_days"]):
            raise ValidationFailed("offsets_days must be integers between 0 and 365", field="offsets_days")
        data["offsets_days"] = sorted(set(data["offsets_days"]))
    if "reply_to_email" in data and data["reply_to_email"] and not valid_email(data["reply_to_email"]):
        raise ValidationFailed("Invalid reply-to address", field="reply_to_email")
    auto = data.pop("automation_enabled", None)
    if auto is not None and auto != pol.automation_enabled:
        pol.automation_enabled = auto
        pol.automation_consented_by = principal.user.id if auto else None
        pol.automation_consented_at = utcnow() if auto else None
        data["automation_enabled"] = auto
    for k, v in data.items():
        if hasattr(pol, k):
            setattr(pol, k, v)
    db.flush()
    audit.record(
        db,
        organization_id=principal.org_id,
        actor_user_id=principal.user.id,
        action="followup.policy_update",
        entity_type="follow_up_policy",
        entity_id=pol.id,
        ip=principal.ip,
        summary={"fields": sorted(data.keys()), "automation_enabled": pol.automation_enabled},
    )
    return PolicyOut.model_validate(pol)


@router.get("/follow-up/templates", response_model=list[TemplateOut])
def list_templates(principal: PrincipalDep, db: DB) -> list[TemplateOut]:
    rows = fu.ensure_default_templates(db, principal.org_id)
    return [TemplateOut.model_validate(t) for t in sorted(rows, key=lambda t: t.key)]


@router.patch("/follow-up/templates/{template_id}", response_model=TemplateOut)
def update_template(template_id: uuid.UUID, payload: TemplateUpdate, principal: PrincipalDep, db: DB) -> TemplateOut:
    principal.require(Permission.MANAGE_ORG)
    t = db.scalar(
        select(MessageTemplate).where(
            MessageTemplate.id == template_id, MessageTemplate.organization_id == principal.org_id
        )
    )
    if t is None:
        raise NotFoundError("Template not found")
    data = payload.model_dump(exclude_unset=True)
    for key in ("subject", "body"):
        if data.get(key):
            tpl.validate_template(data[key])
    for k, v in data.items():
        if v is not None:
            setattr(t, k, v)
    db.flush()
    audit.record(
        db,
        organization_id=principal.org_id,
        actor_user_id=principal.user.id,
        action="followup.template_update",
        entity_type="message_template",
        entity_id=t.id,
        ip=principal.ip,
        summary={"key": t.key, "fields": sorted(data.keys())},
    )
    return TemplateOut.model_validate(t)


# ---------------------------------------------------------------- lifecycle
@router.post("/quotes/{quote_id}/transition", response_model=QuoteOut)
def transition_quote(quote_id: uuid.UUID, payload: TransitionIn, principal: PrincipalDep, db: DB) -> QuoteOut:
    principal.require(Permission.EDIT_QUOTES)
    from app.api.routers.quotes import quote_out

    quote = quote_service.get_quote(db, principal.org_id, quote_id)
    lifecycle_service.transition(
        db,
        quote=quote,
        to_status=payload.to_status,
        actor_user_id=principal.user.id,
        source="manual",
        evidence=payload.evidence,
        occurred_at=payload.occurred_at,
        ip=principal.ip,
        expected_lock_version=payload.expected_lock_version,
    )
    if payload.to_status == "sent":
        fu.plan_schedules(db, org=principal.organization, quote=quote, actor=principal.user, ip=principal.ip)
    return quote_out(db, principal, quote)


@router.get("/quotes/{quote_id}/lifecycle")
def quote_lifecycle(quote_id: uuid.UUID, principal: PrincipalDep, db: DB) -> dict[str, object]:
    quote = quote_service.get_quote(db, principal.org_id, quote_id)
    rec = fu.recommendation_for(db, quote)
    return {
        "status": lifecycle_service.effective_lifecycle_status(quote),
        "allowed_transitions": lifecycle_service.allowed_transitions(quote),
        "events": [
            StatusEventOut.model_validate(e).model_dump(mode="json") for e in lifecycle_service.status_events(db, quote)
        ],
        "recommendation": rec.as_dict(),
        "contacts": [ContactOut.model_validate(c).model_dump(mode="json") for c in fu.contacts_for_quote(db, quote)],
        "drafts": [
            DraftOut.model_validate(d).model_dump(mode="json")
            for d in db.scalars(
                select(CommunicationDraft)
                .where(CommunicationDraft.organization_id == principal.org_id, CommunicationDraft.quote_id == quote.id)
                .order_by(CommunicationDraft.created_at.desc())
            )
        ],
        "schedules": [ScheduleOut.model_validate(s).model_dump(mode="json") for s in fu.schedules_for_quote(db, quote)],
        "timeline": fu.timeline(db, org=principal.organization, quote=quote),
    }


# ---------------------------------------------------------------- drafts
@router.post("/quotes/{quote_id}/drafts", response_model=DraftOut, status_code=201)
def create_draft(quote_id: uuid.UUID, payload: DraftCreate, principal: PrincipalDep, db: DB) -> DraftOut:
    principal.require(Permission.EDIT_QUOTES)
    quote = quote_service.get_quote(db, principal.org_id, quote_id)
    rec = fu.recommendation_for(db, quote)
    draft = fu.create_draft(
        db,
        org=principal.organization,
        quote=quote,
        actor=principal.user,
        ip=principal.ip,
        contact_id=payload.contact_id,
        template_key=payload.template_key or rec.template_key,
        reasons=rec.reasons,
    )
    return DraftOut.model_validate(draft)


def _draft(db: DB, principal: PrincipalDep, draft_id: uuid.UUID) -> CommunicationDraft:
    d = db.scalar(
        select(CommunicationDraft).where(
            CommunicationDraft.id == draft_id, CommunicationDraft.organization_id == principal.org_id
        )
    )
    if d is None:
        raise NotFoundError("Draft not found")
    return d


@router.get("/drafts", response_model=list[DraftOut])
def list_drafts(
    principal: PrincipalDep, db: DB, status: str | None = Query(default=None, max_length=16)
) -> list[DraftOut]:
    stmt = select(CommunicationDraft).where(CommunicationDraft.organization_id == principal.org_id)
    status = clean_param(status)
    if status:
        stmt = stmt.where(CommunicationDraft.status == status)
    return [
        DraftOut.model_validate(d) for d in db.scalars(stmt.order_by(CommunicationDraft.created_at.desc()).limit(200))
    ]


@router.get("/drafts/{draft_id}", response_model=DraftOut)
def get_draft(draft_id: uuid.UUID, principal: PrincipalDep, db: DB) -> DraftOut:
    return DraftOut.model_validate(_draft(db, principal, draft_id))


@router.patch("/drafts/{draft_id}", response_model=DraftOut)
def edit_draft(draft_id: uuid.UUID, payload: DraftUpdate, principal: PrincipalDep, db: DB) -> DraftOut:
    principal.require(Permission.EDIT_QUOTES)
    d = _draft(db, principal, draft_id)
    fields = payload.model_fields_set
    fu.update_draft(
        db,
        draft=d,
        actor=principal.user,
        ip=principal.ip,
        subject=payload.subject,
        body=payload.body,
        to_email=payload.to_email if "to_email" in fields else None,
    )
    return DraftOut.model_validate(d)


@router.post("/drafts/{draft_id}/send", response_model=DraftOut)
def send_draft(draft_id: uuid.UUID, principal: PrincipalDep, db: DB) -> DraftOut:
    """Explicit, authorized, CSRF-protected manual send (S2-F05). Never automatic."""
    principal.require(Permission.EDIT_QUOTES)
    d = _draft(db, principal, draft_id)
    fu.send_draft(db, org=principal.organization, draft=d, actor=principal.user, ip=principal.ip, mode="manual")
    return DraftOut.model_validate(d)


@router.post("/drafts/{draft_id}/cancel", response_model=DraftOut)
def cancel_draft(draft_id: uuid.UUID, principal: PrincipalDep, db: DB) -> DraftOut:
    principal.require(Permission.EDIT_QUOTES)
    d = _draft(db, principal, draft_id)
    if d.status not in ("draft", "failed", "queued"):
        from app.core.errors import StateError

        raise StateError(f"Draft is '{d.status}'")
    d.status = "cancelled"
    db.flush()
    audit.record(
        db,
        organization_id=principal.org_id,
        actor_user_id=principal.user.id,
        action="draft.cancel",
        entity_type="communication_draft",
        entity_id=d.id,
        ip=principal.ip,
    )
    return DraftOut.model_validate(d)


# ---------------------------------------------------------------- schedules
@router.get("/follow-up/schedules", response_model=list[ScheduleOut])
def list_schedules(
    principal: PrincipalDep,
    db: DB,
    status: str | None = Query(default="pending", max_length=16),
    quote_id: uuid.UUID | None = None,
) -> list[ScheduleOut]:
    stmt = select(FollowUpSchedule).where(FollowUpSchedule.organization_id == principal.org_id)
    status = clean_param(status)
    if status and status != "all":
        stmt = stmt.where(FollowUpSchedule.status == status)
    if quote_id:
        stmt = stmt.where(FollowUpSchedule.quote_id == quote_id)
    return [ScheduleOut.model_validate(s) for s in db.scalars(stmt.order_by(FollowUpSchedule.due_at).limit(500))]


def _schedule(db: DB, principal: PrincipalDep, schedule_id: uuid.UUID) -> FollowUpSchedule:
    s = db.scalar(
        select(FollowUpSchedule).where(
            FollowUpSchedule.id == schedule_id, FollowUpSchedule.organization_id == principal.org_id
        )
    )
    if s is None:
        raise NotFoundError("Schedule not found")
    return s


@router.post("/follow-up/schedules/{schedule_id}/cancel", response_model=ScheduleOut)
def cancel_schedule(schedule_id: uuid.UUID, payload: CancelIn, principal: PrincipalDep, db: DB) -> ScheduleOut:
    principal.require(Permission.EDIT_QUOTES)
    s = _schedule(db, principal, schedule_id)
    return ScheduleOut.model_validate(
        fu.cancel_schedule(
            db, org=principal.organization, schedule=s, actor=principal.user, ip=principal.ip, reason=payload.reason
        )
    )


@router.post("/follow-up/schedules/{schedule_id}/reschedule", response_model=ScheduleOut)
def reschedule_schedule(schedule_id: uuid.UUID, payload: RescheduleIn, principal: PrincipalDep, db: DB) -> ScheduleOut:
    principal.require(Permission.EDIT_QUOTES)
    s = _schedule(db, principal, schedule_id)
    due = payload.due_at if payload.due_at.tzinfo else payload.due_at.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return ScheduleOut.model_validate(
        fu.reschedule(db, org=principal.organization, schedule=s, actor=principal.user, ip=principal.ip, due_at=due)
    )


@router.post("/quotes/{quote_id}/follow-up/plan", response_model=list[ScheduleOut], status_code=201)
def plan_quote_schedules(quote_id: uuid.UUID, principal: PrincipalDep, db: DB) -> list[ScheduleOut]:
    principal.require(Permission.EDIT_QUOTES)
    quote = quote_service.get_quote(db, principal.org_id, quote_id)
    return [
        ScheduleOut.model_validate(s)
        for s in fu.plan_schedules(db, org=principal.organization, quote=quote, actor=principal.user, ip=principal.ip)
    ]


# ---------------------------------------------------------------- events / jobs / integrations
@router.get("/communication-events", response_model=list[CommEventOut])
def list_events(principal: PrincipalDep, db: DB, quote_id: uuid.UUID | None = None) -> list[CommEventOut]:
    stmt = select(CommunicationEvent).where(CommunicationEvent.organization_id == principal.org_id)
    if quote_id:
        stmt = stmt.where(CommunicationEvent.quote_id == quote_id)
    return [
        CommEventOut.model_validate(e)
        for e in db.scalars(stmt.order_by(CommunicationEvent.occurred_at.desc()).limit(500))
    ]


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(principal: PrincipalDep, db: DB, status: str | None = Query(default=None, max_length=16)) -> list[JobOut]:
    principal.require(Permission.MANAGE_ORG)
    stmt = select(JobRun).where(JobRun.organization_id == principal.org_id)
    status = clean_param(status)
    if status:
        stmt = stmt.where(JobRun.status == status)
    return [JobOut.model_validate(j) for j in db.scalars(stmt.order_by(JobRun.created_at.desc()).limit(200))]


@router.post("/jobs/{job_id}/retry", response_model=JobOut)
def retry_job(job_id: uuid.UUID, principal: PrincipalDep, db: DB) -> JobOut:
    principal.require(Permission.MANAGE_ORG)
    j = db.scalar(select(JobRun).where(JobRun.id == job_id, JobRun.organization_id == principal.org_id))
    if j is None:
        raise NotFoundError("Job not found")
    if j.status not in ("dead", "failed"):
        from app.core.errors import StateError

        raise StateError(f"Job is '{j.status}'")
    j.status = "queued"
    j.next_run_at = utcnow()
    j.attempts = 0
    db.flush()
    audit.record(
        db,
        organization_id=principal.org_id,
        actor_user_id=principal.user.id,
        action="job.retry",
        entity_type="job_run",
        entity_id=j.id,
        ip=principal.ip,
    )
    return JobOut.model_validate(j)


@router.get("/integrations", response_model=list[IntegrationOut])
def list_integrations(principal: PrincipalDep, db: DB) -> list[IntegrationOut]:
    principal.require(Permission.MANAGE_ORG)
    return [
        IntegrationOut.model_validate(c)
        for c in db.scalars(
            select(IntegrationConnection).where(IntegrationConnection.organization_id == principal.org_id)
        )
    ]


@router.post("/integrations", response_model=IntegrationOut, status_code=201)
def create_integration(
    payload: IntegrationCreate, principal: PrincipalDep, db: DB, settings: SettingsDep
) -> IntegrationOut:
    """Connect a provider. Secrets are envelope-encrypted and never returned; gmail/msgraph are documented only."""
    principal.require(Permission.MANAGE_ORG)
    if payload.provider in ("gmail", "msgraph"):
        raise ValidationFailed(
            f"{payload.provider} is documented but not shipped in this release; use simulator or smtp", field="provider"
        )
    encrypted: str | None = None
    if payload.secret:
        from app.services.comms.secrets import EncryptionUnavailable, encrypt

        try:
            encrypted = encrypt(payload.secret, aad=f"{principal.org_id}:{payload.provider}")
        except EncryptionUnavailable as exc:
            raise ValidationFailed(str(exc), field="secret") from exc
    existing = db.scalar(
        select(IntegrationConnection).where(
            IntegrationConnection.organization_id == principal.org_id,
            IntegrationConnection.provider == payload.provider,
        )
    )
    for other in db.scalars(
        select(IntegrationConnection).where(
            IntegrationConnection.organization_id == principal.org_id,
            IntegrationConnection.provider != payload.provider,
        )
    ):
        other.status = "revoked"
    conn = existing or IntegrationConnection(organization_id=principal.org_id, provider=payload.provider)
    conn.status = "active"
    conn.scopes = ["send", "read_inbound"] if payload.provider == "simulator" else ["send"]
    conn.account_email = payload.account_email
    conn.encrypted_secret = encrypted  # type: ignore[assignment]
    conn.connected_by = principal.user.id
    conn.last_error = None
    db.add(conn)
    db.flush()
    audit.record(
        db,
        organization_id=principal.org_id,
        actor_user_id=principal.user.id,
        action="integration.connect",
        entity_type="integration_connection",
        entity_id=conn.id,
        ip=principal.ip,
        summary={"provider": payload.provider, "has_secret": bool(encrypted)},
    )
    return IntegrationOut.model_validate(conn)


@router.post("/integrations/{integration_id}/revoke", response_model=IntegrationOut)
def revoke_integration(integration_id: uuid.UUID, principal: PrincipalDep, db: DB) -> IntegrationOut:
    principal.require(Permission.MANAGE_ORG)
    conn = db.scalar(
        select(IntegrationConnection).where(
            IntegrationConnection.id == integration_id, IntegrationConnection.organization_id == principal.org_id
        )
    )
    if conn is None:
        raise NotFoundError("Integration not found")
    conn.status = "revoked"
    conn.encrypted_secret = None
    db.flush()
    audit.record(
        db,
        organization_id=principal.org_id,
        actor_user_id=principal.user.id,
        action="integration.revoke",
        entity_type="integration_connection",
        entity_id=conn.id,
        ip=principal.ip,
        summary={"provider": conn.provider},
    )
    return IntegrationOut.model_validate(conn)


@router.post("/integrations/simulator/inbound", status_code=201)
def simulate_inbound(
    payload: SimulatedInboundIn, principal: PrincipalDep, db: DB, settings: SettingsDep
) -> dict[str, object]:
    """Development/demo helper: inject a synthetic inbound e-mail and process it immediately (never in production)."""
    principal.require(Permission.MANAGE_ORG)
    if settings.is_production:
        raise ForbiddenError("Simulator inbound injection is disabled in production", action="simulator")
    msg = InboundMessage(
        provider="simulator",
        provider_message_id=f"sim-in-{uuid.uuid4()}",
        from_email=payload.from_email,
        subject=payload.subject,
        snippet=payload.snippet,
        received_at=utcnow(),
        in_reply_to=payload.in_reply_to,
    )
    event = fu.record_inbound(db, org=principal.organization, msg=msg)
    return {
        "event_id": str(event.id) if event else None,
        "kind": event.kind if event else None,
        "quote_id": str(event.quote_id) if event and event.quote_id else None,
    }


@router.post("/integrations/sync-inbound")
def sync_now(principal: PrincipalDep, db: DB) -> dict[str, int]:
    principal.require(Permission.MANAGE_ORG)
    n = fu.sync_inbound(db, org=principal.organization)
    return {"processed": n}


@router.post("/jobs/run-once")
def run_jobs_once(principal: PrincipalDep, db: DB, settings: SettingsDep) -> dict[str, int]:
    """Development helper: run the worker loop once for this process (disabled in production)."""
    principal.require(Permission.MANAGE_ORG)
    if settings.is_production:
        raise ForbiddenError("Use the worker process in production", action="worker")
    db.commit()
    queue.enqueue_due_schedules(db)
    return {"processed": queue.run_once(db)}
