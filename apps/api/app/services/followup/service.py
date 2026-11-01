"""Follow-up service: drafts, consented sending, schedules, inbound linking, timeline (S2-F02..F10).

Hard rules enforced here (not in providers or UI):
- nothing is sent without an explicit user action OR an admin-consented automation policy;
- opted-out contacts never receive automated or manual follow-ups through this system;
- a draft is marked sent only after the provider acknowledged it; duplicate triggers cannot double-send
  (send idempotency key + status check inside a row lock).
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ConflictError, ForbiddenError, NotFoundError, RateLimited, StateError, ValidationFailed
from app.db.base import utcnow
from app.db.session import set_tenant
from app.models.customer import Customer
from app.models.followup import (
    CommunicationDraft,
    CommunicationEvent,
    Contact,
    FollowUpPolicy,
    FollowUpSchedule,
    IntegrationConnection,
    MessageTemplate,
)
from app.models.org import Organization, User
from app.models.quote import Quote
from app.services import audit
from app.services.comms.providers import (
    InboundMessage,
    MailProvider,
    NotImplementedProvider,
    OutboundMessage,
    ProviderError,
    SMTPProvider,
    simulator_for,
    valid_email,
)
from app.services.followup import templates as tpl
from app.services.followup.recommend import Recommendation, recommend
from app.services.lifecycle import effective_lifecycle_status, transition
from app.services.quotes import current_version

logger = logging.getLogger(__name__)
UNSUBSCRIBE_RE = re.compile(r"\b(unsubscribe|stop|opt[\s-]?out|remove me)\b", re.IGNORECASE)
QUOTE_NUMBER_RE = re.compile(r"\bQ-\d{4}-\d{5}\b")


# ---------------------------------------------------------------- policy / contacts helpers
def get_policy(db: Session, org_id: uuid.UUID) -> FollowUpPolicy | None:
    return db.scalar(select(FollowUpPolicy).where(FollowUpPolicy.organization_id == org_id))


def ensure_policy(db: Session, org_id: uuid.UUID) -> FollowUpPolicy:
    pol = get_policy(db, org_id)
    if pol is None:
        pol = FollowUpPolicy(organization_id=org_id)
        db.add(pol)
        db.flush()
    return pol


def ensure_default_templates(db: Session, org_id: uuid.UUID) -> list[MessageTemplate]:
    existing = {t.key: t for t in db.scalars(select(MessageTemplate).where(MessageTemplate.organization_id == org_id))}
    for d in tpl.DEFAULT_TEMPLATES:
        if d["key"] not in existing:
            t = MessageTemplate(
                organization_id=org_id, key=d["key"], name=d["name"], subject=d["subject"], body=d["body"]
            )
            db.add(t)
            existing[d["key"]] = t
    db.flush()
    return list(existing.values())


def contacts_for_quote(db: Session, quote: Quote) -> list[Contact]:
    if quote.customer_id is None:
        return []
    return list(
        db.scalars(
            select(Contact)
            .where(Contact.organization_id == quote.organization_id, Contact.customer_id == quote.customer_id)
            .order_by(Contact.is_primary.desc(), Contact.created_at)
        )
    )


def events_for_quote(db: Session, quote: Quote) -> list[CommunicationEvent]:
    return list(
        db.scalars(
            select(CommunicationEvent)
            .where(CommunicationEvent.organization_id == quote.organization_id, CommunicationEvent.quote_id == quote.id)
            .order_by(CommunicationEvent.occurred_at)
        )
    )


def schedules_for_quote(db: Session, quote: Quote) -> list[FollowUpSchedule]:
    return list(
        db.scalars(
            select(FollowUpSchedule)
            .where(FollowUpSchedule.organization_id == quote.organization_id, FollowUpSchedule.quote_id == quote.id)
            .order_by(FollowUpSchedule.due_at)
        )
    )


def recommendation_for(db: Session, quote: Quote) -> Recommendation:
    return recommend(
        quote,
        policy=get_policy(db, quote.organization_id),
        contacts=contacts_for_quote(db, quote),
        events=events_for_quote(db, quote),
        schedules=schedules_for_quote(db, quote),
    )


# ---------------------------------------------------------------- providers
def provider_for(db: Session, org: Organization) -> MailProvider:
    conn = db.scalar(
        select(IntegrationConnection).where(
            IntegrationConnection.organization_id == org.id, IntegrationConnection.status == "active"
        )
    )
    name = conn.provider if conn else get_settings().mail_provider_default
    if name == "simulator":
        return simulator_for(org.id)
    if name == "smtp":
        s = get_settings()
        if not (s.smtp_host and s.smtp_from_email):
            raise ProviderError("SMTP is selected but SMTP_HOST / SMTP_FROM_EMAIL are not configured", retryable=False)
        return SMTPProvider(
            s.smtp_host, s.smtp_port, s.smtp_username, s.smtp_password, s.smtp_use_tls, s.smtp_from_email
        )
    return NotImplementedProvider(name)


# ---------------------------------------------------------------- drafts
def _context(
    db: Session, org: Organization, quote: Quote, contact: Contact | None, attempt_no: int, sender: User | None
) -> tpl.TemplateContext:
    version = current_version(quote)
    customer = db.get(Customer, quote.customer_id) if quote.customer_id else None
    events = events_for_quote(db, quote)
    last_out = max((e.occurred_at for e in events if e.direction == "out" and e.kind == "sent"), default=None)
    sent_event = next((e for e in events if e.kind == "sent"), None)
    base = last_out or (sent_event.occurred_at if sent_event else quote.updated_at)
    policy = get_policy(db, org.id)
    return tpl.TemplateContext(
        contact_name=contact.full_name if contact else (customer.name if customer else "Customer"),
        customer_name=customer.name if customer else "",
        quote_number=quote.number,
        quote_version=version.version_no,
        grand_total=version.grand_total,
        currency=quote.currency,
        valid_until=quote.valid_until.isoformat() if quote.valid_until else "—",
        org_name=org.name,
        sender_name=(
            policy.sender_name if policy and policy.sender_name else (sender.full_name if sender else org.name)
        ),
        attempt_no=attempt_no,
        days_since_sent=max((utcnow() - base).days, 0),
    )


def create_draft(
    db: Session,
    *,
    org: Organization,
    quote: Quote,
    actor: User | None,
    ip: str | None,
    contact_id: uuid.UUID | None,
    template_key: str | None,
    reasons: list[str] | None = None,
    schedule_id: uuid.UUID | None = None,
) -> CommunicationDraft:
    status = effective_lifecycle_status(quote)
    if status not in ("sent", "acknowledged", "awaiting_customer", "approved"):
        raise StateError(f"Follow-up drafts need an approved/sent quote (status '{status}')")
    contacts = contacts_for_quote(db, quote)
    contact = (
        next((c for c in contacts if c.id == contact_id), None)
        if contact_id
        else next((c for c in contacts if c.email and c.opted_out_at is None), None)
    )
    if contact_id and contact is None:
        raise NotFoundError("Contact not found for this quote's customer")
    if contact is not None and contact.opted_out_at is not None:
        raise ForbiddenError("This contact opted out of follow-up messages", action="opt_out")
    key = template_key or "follow_up_1"
    template = db.scalar(
        select(MessageTemplate).where(
            MessageTemplate.organization_id == org.id, MessageTemplate.key == key, MessageTemplate.is_active.is_(True)
        )
    )
    if template is None:
        ensure_default_templates(db, org.id)
        template = db.scalar(
            select(MessageTemplate).where(MessageTemplate.organization_id == org.id, MessageTemplate.key == key)
        )
    if template is None:
        raise NotFoundError(f"Template '{key}' not found")
    attempt_no = len([e for e in events_for_quote(db, quote) if e.direction == "out" and e.kind == "sent"]) + 1
    ctx = _context(db, org, quote, contact, attempt_no, actor)
    draft = CommunicationDraft(
        organization_id=org.id,
        quote_id=quote.id,
        contact_id=contact.id if contact else None,
        schedule_id=schedule_id,
        channel="email",
        to_email=contact.email if contact else None,
        subject=tpl.render(template.subject, ctx)[:300],
        body=tpl.ensure_opt_out(tpl.render(template.body, ctx), ctx),
        template_key=key,
        status="draft",
        generated_reasons=reasons or [],
        created_by=actor.id if actor else None,
    )
    db.add(draft)
    db.flush()
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=actor.id if actor else None,
        action="draft.create",
        entity_type="communication_draft",
        entity_id=draft.id,
        ip=ip,
        summary={"quote_id": str(quote.id), "template": key, "contact_id": str(contact.id) if contact else None},
    )
    return draft


def update_draft(
    db: Session,
    *,
    draft: CommunicationDraft,
    actor: User,
    ip: str | None,
    subject: str | None,
    body: str | None,
    to_email: str | None | object,
) -> CommunicationDraft:
    if draft.status != "draft":
        raise StateError(f"Draft is '{draft.status}' and can no longer be edited")
    if subject is not None:
        draft.subject = subject.replace("\r", " ").replace("\n", " ").strip()[:300]
    if body is not None:
        tpl.validate_template(body)
        draft.body = body[:20000]
    if isinstance(to_email, str):
        if not valid_email(to_email):
            raise ValidationFailed("Invalid recipient address", field="to_email")
        # recipient must belong to this quote's customer contacts (no arbitrary recipients)
        allowed = {c.email.lower() for c in contacts_for_quote(db, db.get(Quote, draft.quote_id)) if c.email}  # type: ignore[arg-type]
        if to_email.lower() not in allowed:
            raise ValidationFailed("Recipient must be a contact of this quote's customer", field="to_email")
        draft.to_email = to_email
    db.flush()
    audit.record(
        db,
        organization_id=draft.organization_id,
        actor_user_id=actor.id,
        action="draft.update",
        entity_type="communication_draft",
        entity_id=draft.id,
        ip=ip,
        summary={
            "fields": [
                k
                for k, v in (
                    ("subject", subject),
                    ("body", body),
                    ("to_email", to_email if isinstance(to_email, str) else None),
                )
                if v is not None
            ]
        },
    )
    return draft


def _rate_limit_ok(db: Session, org_id: uuid.UUID, limit: int) -> bool:
    since = utcnow() - timedelta(hours=1)
    n = (
        db.scalar(
            select(func.count())
            .select_from(CommunicationEvent)
            .where(
                CommunicationEvent.organization_id == org_id,
                CommunicationEvent.kind == "sent",
                CommunicationEvent.occurred_at >= since,
            )
        )
        or 0
    )
    return n < limit


def send_draft(
    db: Session,
    *,
    org: Organization,
    draft: CommunicationDraft,
    actor: User | None,
    ip: str | None,
    mode: str,  # manual | automation
    idempotency_key: str | None = None,
) -> CommunicationDraft:
    """The single sending gate. Raises instead of silently skipping; callers decide retry semantics."""
    locked = db.scalar(
        select(CommunicationDraft)
        .where(CommunicationDraft.id == draft.id, CommunicationDraft.organization_id == org.id)
        .with_for_update()
    )
    if locked is None:
        raise NotFoundError("Draft not found")
    draft = locked
    if draft.status == "sent":
        return draft  # idempotent: already sent
    if draft.status not in ("draft", "queued", "failed"):
        raise StateError(f"Draft is '{draft.status}'")
    policy = ensure_policy(db, org.id)
    if mode == "automation":
        if not (policy.automation_enabled and policy.automation_consented_at):
            raise ForbiddenError("Automated sending is not enabled for this organisation", action="automation_consent")
    elif actor is None:
        raise ForbiddenError("A user must trigger a manual send", action="send")
    quote = db.get(Quote, draft.quote_id)
    if quote is None or effective_lifecycle_status(quote) in ("draft", "pending_approval", "withdrawn", "won", "lost"):
        raise StateError("Quote is not in a state that allows follow-up messages")
    contact = db.get(Contact, draft.contact_id) if draft.contact_id else None
    if contact is None or not contact.email:
        raise ValidationFailed("Draft has no eligible recipient contact", field="contact_id")
    if contact.opted_out_at is not None:
        draft.status = "cancelled"
        draft.last_error = "recipient opted out"
        db.flush()
        audit.record(
            db,
            organization_id=org.id,
            actor_user_id=actor.id if actor else None,
            action="draft.suppressed",
            entity_type="communication_draft",
            entity_id=draft.id,
            ip=ip,
            summary={"reason": "opted_out"},
        )
        db.commit()  # the suppression is a fact worth keeping even though the request fails
        set_tenant(db, org.id, actor.id if actor else None)
        raise ForbiddenError("Recipient opted out; message suppressed", action="opt_out")
    to_email = draft.to_email or contact.email
    if not valid_email(to_email) or to_email.lower() != (contact.email or "").lower():
        raise ValidationFailed("Recipient does not match the contact on record", field="to_email")
    if not _rate_limit_ok(db, org.id, policy.hourly_send_limit):
        raise RateLimited(f"Hourly send limit of {policy.hourly_send_limit} reached")
    key = idempotency_key or f"draft:{draft.id}"
    if draft.send_idempotency_key and draft.send_idempotency_key != key:
        raise ConflictError("Draft already has a different send in progress")
    draft.send_idempotency_key = key
    try:
        provider = provider_for(db, org)
    except ProviderError as exc:  # misconfigured provider: the draft fails safely, nothing is claimed as sent
        draft.status = "failed"
        draft.last_error = str(exc)[:500]
        db.flush()
        audit.record(
            db,
            organization_id=org.id,
            actor_user_id=actor.id if actor else None,
            action="draft.send_failed",
            entity_type="communication_draft",
            entity_id=draft.id,
            ip=ip,
            summary={"provider": "unconfigured", "retryable": False},
        )
        db.commit()
        set_tenant(db, org.id, actor.id if actor else None)
        raise
    message = OutboundMessage(
        to_email=to_email,
        subject=draft.subject,
        body_text=draft.body,
        from_name=policy.sender_name or org.name,
        reply_to=policy.reply_to_email,
        headers={"X-QuotePilot-Quote": quote.number, "X-QuotePilot-Draft": str(draft.id)},
    )
    try:
        result = provider.send(message)
    except ProviderError as exc:
        draft.status = "failed" if not exc.retryable else draft.status
        draft.last_error = str(exc)[:500]
        draft.provider = provider.name
        db.flush()
        audit.record(
            db,
            organization_id=org.id,
            actor_user_id=actor.id if actor else None,
            action="draft.send_failed",
            entity_type="communication_draft",
            entity_id=draft.id,
            ip=ip,
            summary={"provider": provider.name, "retryable": exc.retryable, "status": exc.status},
        )
        db.commit()  # keep the failure record; the caller still sees the error
        set_tenant(db, org.id, actor.id if actor else None)
        raise
    now = utcnow()
    draft.status = "sent"
    draft.provider = result.provider
    draft.provider_message_id = result.provider_message_id
    draft.sent_by = actor.id if actor else None
    draft.sent_at = result.accepted_at
    draft.last_error = None
    db.add(
        CommunicationEvent(
            organization_id=org.id,
            quote_id=quote.id,
            contact_id=contact.id,
            draft_id=draft.id,
            kind="sent",
            direction="out",
            provider=result.provider,
            provider_message_id=result.provider_message_id,
            subject=draft.subject[:300],
            snippet=draft.body[:200],
            payload={"mode": mode, "attempt_header": message.headers},
            occurred_at=result.accepted_at,
            created_at=now,
        )
    )
    if draft.schedule_id:
        sched = db.get(FollowUpSchedule, draft.schedule_id)
        if sched is not None:
            sched.status = "sent"
    if quote.status == "approved":
        transition(
            db,
            quote=quote,
            to_status="sent",
            actor_user_id=actor.id if actor else None,
            source="send",
            evidence=f"sent via {result.provider}",
            occurred_at=result.accepted_at,
            ip=ip,
        )
    elif quote.status in ("sent", "acknowledged"):
        pass
    db.flush()
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=actor.id if actor else None,
        action="draft.sent",
        entity_type="communication_draft",
        entity_id=draft.id,
        ip=ip,
        summary={"provider": result.provider, "mode": mode, "quote_id": str(quote.id)},
    )
    logger.info(
        "follow-up sent",
        extra={"extra": {"org_id": str(org.id), "draft_id": str(draft.id), "provider": result.provider}},
    )
    return draft


# ---------------------------------------------------------------- schedules
def _next_allowed(dt: datetime, policy: FollowUpPolicy) -> datetime:
    """Push a due time out of quiet hours / weekends in the policy time zone."""
    try:
        tz = ZoneInfo(policy.timezone)
    except Exception:  # noqa: BLE001 - invalid zone falls back to UTC
        tz = ZoneInfo("UTC")
    local = dt.astimezone(tz)
    for _ in range(14):
        qs, qe = policy.quiet_hours_start, policy.quiet_hours_end
        in_quiet = (local.hour >= qs or local.hour < qe) if qs > qe else (qs <= local.hour < qe)
        if in_quiet:
            next_day = local.date() + timedelta(days=1) if local.hour >= qs or qs <= qe else local.date()
            local = datetime.combine(next_day, time(hour=qe, minute=0), tzinfo=tz)
            continue
        if policy.skip_weekends and local.weekday() >= 5:
            local = datetime.combine(
                local.date() + timedelta(days=7 - local.weekday()), time(hour=max(policy.quiet_hours_end, 9)), tzinfo=tz
            )
            continue
        break
    return local.astimezone(UTC)


def plan_schedules(
    db: Session, *, org: Organization, quote: Quote, actor: User | None, ip: str | None
) -> list[FollowUpSchedule]:
    """Create pending follow-up schedules for a sent quote according to policy (idempotent per attempt)."""
    policy = ensure_policy(db, org.id)
    status = effective_lifecycle_status(quote)
    if status not in ("sent", "acknowledged", "awaiting_customer"):
        raise StateError(f"Schedules apply to sent quotes (status '{status}')")
    contacts = [c for c in contacts_for_quote(db, quote) if c.email and c.opted_out_at is None]
    contact = next((c for c in contacts if c.is_primary), contacts[0] if contacts else None)
    events = events_for_quote(db, quote)
    sent_event = next((e for e in events if e.kind == "sent"), None)
    anchor = sent_event.occurred_at if sent_event else utcnow()
    existing = {s.idempotency_key for s in schedules_for_quote(db, quote)}
    created: list[FollowUpSchedule] = []
    for i, days in enumerate(list(policy.offsets_days)[: policy.max_attempts], start=1):
        key = f"quote:{quote.id}:attempt:{i}"
        if key in existing:
            continue
        due = _next_allowed(anchor + timedelta(days=int(days)), policy)
        if quote.valid_until and due.date() > quote.valid_until:
            continue
        sched = FollowUpSchedule(
            organization_id=org.id,
            quote_id=quote.id,
            contact_id=contact.id if contact else None,
            attempt_no=i,
            due_at=due,
            status="pending" if contact else "suppressed",
            mode="auto_send" if (policy.automation_enabled and policy.automation_consented_at) else "draft_only",
            idempotency_key=key,
            reason=f"policy offset {days} day(s) after send" + ("" if contact else "; no eligible contact"),
        )
        db.add(sched)
        created.append(sched)
    db.flush()
    if created:
        audit.record(
            db,
            organization_id=org.id,
            actor_user_id=actor.id if actor else None,
            action="followup.schedule",
            entity_type="quote",
            entity_id=quote.id,
            ip=ip,
            summary={"count": len(created)},
        )
    return created


def cancel_schedule(
    db: Session, *, org: Organization, schedule: FollowUpSchedule, actor: User, ip: str | None, reason: str | None
) -> FollowUpSchedule:
    if schedule.status != "pending":
        raise StateError(f"Schedule is '{schedule.status}'")
    schedule.status = "cancelled"
    schedule.cancelled_by = actor.id
    schedule.cancelled_reason = (reason or "").strip()[:500] or None
    db.flush()
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=actor.id,
        action="followup.cancel",
        entity_type="follow_up_schedule",
        entity_id=schedule.id,
        ip=ip,
        summary={"reason": schedule.cancelled_reason},
    )
    return schedule


def reschedule(
    db: Session, *, org: Organization, schedule: FollowUpSchedule, actor: User, ip: str | None, due_at: datetime
) -> FollowUpSchedule:
    if schedule.status != "pending":
        raise StateError(f"Schedule is '{schedule.status}'")
    if due_at <= utcnow():
        raise ValidationFailed("New due time must be in the future", field="due_at")
    schedule.due_at = due_at
    db.flush()
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=actor.id,
        action="followup.reschedule",
        entity_type="follow_up_schedule",
        entity_id=schedule.id,
        ip=ip,
        summary={"due_at": due_at.isoformat()},
    )
    return schedule


def process_due_schedule(db: Session, *, org: Organization, schedule: FollowUpSchedule) -> CommunicationDraft | None:
    """Worker path: materialise the draft; send it only when automation is consented and the contact is eligible."""
    if schedule.status != "pending":
        return None
    quote = db.get(Quote, schedule.quote_id)
    if quote is None:
        schedule.status = "cancelled"
        schedule.last_error = "quote missing"
        return None
    status = effective_lifecycle_status(quote)
    if status not in ("sent", "acknowledged", "awaiting_customer"):
        schedule.status = "superseded"
        schedule.last_error = f"quote status is {status}"
        db.flush()
        return None
    if any(e.direction == "in" for e in events_for_quote(db, quote)):
        schedule.status = "superseded"
        schedule.last_error = "customer replied; automated follow-up suppressed"
        db.flush()
        return None
    contact = db.get(Contact, schedule.contact_id) if schedule.contact_id else None
    if contact is None or contact.opted_out_at is not None or not contact.email:
        schedule.status = "suppressed"
        schedule.last_error = "no eligible contact (opted out or missing e-mail)"
        db.flush()
        audit.record(
            db,
            organization_id=org.id,
            actor_user_id=None,
            action="followup.suppressed",
            entity_type="follow_up_schedule",
            entity_id=schedule.id,
            summary={"reason": schedule.last_error},
        )
        return None
    rec = recommendation_for(db, quote)
    key = rec.template_key or ("follow_up_1" if schedule.attempt_no == 1 else "follow_up_2")
    draft = db.scalar(
        select(CommunicationDraft).where(
            CommunicationDraft.schedule_id == schedule.id, CommunicationDraft.organization_id == org.id
        )
    )
    if draft is None:
        draft = create_draft(
            db,
            org=org,
            quote=quote,
            actor=None,
            ip=None,
            contact_id=contact.id,
            template_key=key,
            reasons=rec.reasons,
            schedule_id=schedule.id,
        )
        schedule.draft_id = draft.id
    if schedule.mode == "auto_send":
        send_draft(
            db, org=org, draft=draft, actor=None, ip=None, mode="automation", idempotency_key=f"schedule:{schedule.id}"
        )
    else:
        # draft-only mode (default): the schedule is fulfilled by preparing a draft for a human to review and send
        schedule.status = "drafted"
        schedule.last_error = None
    db.flush()
    return draft


# ---------------------------------------------------------------- inbound
def record_inbound(db: Session, *, org: Organization, msg: InboundMessage) -> CommunicationEvent | None:
    """Link an inbound message to a quote by quote number or header; record reply / opt-out facts."""
    existing = db.scalar(
        select(CommunicationEvent).where(
            CommunicationEvent.organization_id == org.id,
            CommunicationEvent.provider == msg.provider,
            CommunicationEvent.provider_message_id == msg.provider_message_id,
            CommunicationEvent.kind == "reply_received",
        )
    )
    if existing is not None:
        return existing  # idempotent
    text = f"{msg.subject}\n{msg.snippet}"
    number = next(iter(QUOTE_NUMBER_RE.findall(text)), None)
    quote = db.scalar(select(Quote).where(Quote.organization_id == org.id, Quote.number == number)) if number else None
    if quote is None and msg.in_reply_to:
        prior = db.scalar(
            select(CommunicationEvent).where(
                CommunicationEvent.organization_id == org.id, CommunicationEvent.provider_message_id == msg.in_reply_to
            )
        )
        quote = db.get(Quote, prior.quote_id) if prior and prior.quote_id else None
    contact = db.scalar(
        select(Contact).where(Contact.organization_id == org.id, func.lower(Contact.email) == msg.from_email.lower())
    )
    kind = "opt_out" if UNSUBSCRIBE_RE.search(text) else "reply_received"
    event = CommunicationEvent(
        organization_id=org.id,
        quote_id=quote.id if quote else None,
        contact_id=contact.id if contact else None,
        kind=kind,
        direction="in",
        provider=msg.provider,
        provider_message_id=msg.provider_message_id,
        subject=msg.subject[:300],
        snippet=msg.snippet[:500],
        payload={"linked_by": "quote_number" if number and quote else ("thread" if quote else "unlinked")},
        occurred_at=msg.received_at,
        created_at=utcnow(),
    )
    db.add(event)
    if kind == "opt_out" and contact is not None and contact.opted_out_at is None:
        contact.opted_out_at = msg.received_at
        contact.opt_out_source = "reply"
        for s in db.scalars(
            select(FollowUpSchedule).where(
                FollowUpSchedule.organization_id == org.id,
                FollowUpSchedule.contact_id == contact.id,
                FollowUpSchedule.status == "pending",
            )
        ):
            s.status = "suppressed"
            s.last_error = "contact opted out"
    if quote is not None and kind == "reply_received" and quote.status == "sent":
        transition(
            db,
            quote=quote,
            to_status="acknowledged",
            actor_user_id=None,
            source="inbound",
            evidence=f"reply received via {msg.provider} ({msg.provider_message_id})",
            occurred_at=msg.received_at,
            ip=None,
        )
    db.flush()
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=None,
        action=f"inbound.{kind}",
        entity_type="communication_event",
        entity_id=event.id,
        summary={"quote_id": str(quote.id) if quote else None, "linked": bool(quote)},
    )
    return event


def sync_inbound(db: Session, *, org: Organization) -> int:
    provider = provider_for(db, org)
    conn = db.scalar(
        select(IntegrationConnection).where(
            IntegrationConnection.organization_id == org.id, IntegrationConnection.provider == provider.name
        )
    )
    if conn is None:  # persist the inbound cursor even for the default simulator
        conn = IntegrationConnection(
            organization_id=org.id, provider=provider.name, status="active", scopes=["send", "read_inbound"]
        )
        db.add(conn)
        db.flush()
    cursor = conn.last_inbound_cursor
    try:
        messages, new_cursor = provider.fetch_inbound(cursor)
    except ProviderError as exc:
        if conn is not None:
            conn.status = "error" if not exc.retryable else conn.status
            conn.last_error = str(exc)[:500]
        raise
    for m in messages:
        record_inbound(db, org=org, msg=m)
    if conn is not None:
        conn.last_inbound_cursor = new_cursor
        conn.last_sync_at = utcnow()
        conn.last_error = None
    db.flush()
    return len(messages)


# ---------------------------------------------------------------- timeline
def timeline(db: Session, *, org: Organization, quote: Quote) -> list[dict[str, object]]:
    from app.models.audit import AuditEvent
    from app.services.lifecycle import status_events

    items: list[dict[str, object]] = []
    for e in status_events(db, quote):
        items.append(
            {
                "at": e.occurred_at,
                "kind": "status",
                "title": f"{e.from_status} → {e.to_status}",
                "detail": e.evidence,
                "source": e.source,
            }
        )
    for c in events_for_quote(db, quote):
        items.append(
            {
                "at": c.occurred_at,
                "kind": f"comm_{c.direction}",
                "title": c.kind.replace("_", " "),
                "detail": c.subject,
                "source": c.provider,
            }
        )
    for s in schedules_for_quote(db, quote):
        items.append(
            {
                "at": s.due_at,
                "kind": "schedule",
                "title": f"follow-up #{s.attempt_no} ({s.status})",
                "detail": s.reason,
                "source": s.mode,
            }
        )
    for a in db.scalars(
        select(AuditEvent)
        .where(
            AuditEvent.organization_id == org.id,
            AuditEvent.entity_id == quote.id,
            AuditEvent.action.in_(("quote.approve", "quote.create", "quote.withdraw")),
        )
        .order_by(AuditEvent.created_at)
    ):
        items.append({"at": a.created_at, "kind": "audit", "title": a.action, "detail": None, "source": "audit"})
    items.sort(key=lambda x: str(x["at"]))
    for it in items:
        at = it["at"]
        it["at"] = at.isoformat() if isinstance(at, datetime) else str(at)
    return items
