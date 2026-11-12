"""Stage 2 — sales follow-up assistant: contacts, policies, schedules, drafts, events, integrations, jobs."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin

LIFECYCLE_STATUSES = (
    "draft",
    "pending_approval",
    "approved",
    "withdrawn",
    "sent",
    "acknowledged",
    "awaiting_customer",
    "won",
    "lost",
    "expired",
)
SCHEDULE_STATUSES = ("pending", "drafted", "sent", "cancelled", "suppressed", "failed", "superseded")
DRAFT_STATUSES = ("draft", "queued", "sent", "failed", "cancelled")
JOB_STATUSES = ("queued", "running", "succeeded", "failed", "dead")


class Contact(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "contacts"
    __table_args__ = (
        Index("ix_contacts_org_customer", "organization_id", "customer_id"),
        Index("ix_contacts_org_lower_email", "organization_id", text("lower(email)")),
    )

    customer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(50))
    preferred_channel: Mapped[str] = mapped_column(String(16), nullable=False, default="email")  # email|phone|none
    language: Mapped[str] = mapped_column(String(8), nullable=False, default="en")
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    opted_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    opt_out_source: Mapped[str | None] = mapped_column(String(64))  # link|reply|manual|import
    notes: Mapped[str | None] = mapped_column(Text)


class FollowUpPolicy(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """One row per organisation. Automation is OFF until an admin/owner explicitly consents."""

    __tablename__ = "follow_up_policies"
    __table_args__ = (
        UniqueConstraint("organization_id", name="uq_follow_up_policy_org"),
        CheckConstraint("max_attempts >= 0 AND max_attempts <= 10", name="ck_policy_attempts"),
        CheckConstraint("quiet_hours_start >= 0 AND quiet_hours_start <= 23", name="ck_policy_qh_start"),
        CheckConstraint("quiet_hours_end >= 0 AND quiet_hours_end <= 23", name="ck_policy_qh_end"),
        CheckConstraint("hourly_send_limit >= 1 AND hourly_send_limit <= 1000", name="ck_policy_rate"),
    )

    offsets_days: Mapped[list[int]] = mapped_column(JSONB, nullable=False, default=lambda: [3, 7, 14])
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    quiet_hours_start: Mapped[int] = mapped_column(Integer, nullable=False, default=19)
    quiet_hours_end: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    skip_weekends: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    automation_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    automation_consented_by: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    automation_consented_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hourly_send_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    sender_name: Mapped[str | None] = mapped_column(String(200))
    reply_to_email: Mapped[str | None] = mapped_column(String(320))


class MessageTemplate(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "message_templates"
    __table_args__ = (UniqueConstraint("organization_id", "key", name="uq_template_org_key"),)

    key: Mapped[str] = mapped_column(String(64), nullable=False)  # follow_up_1, follow_up_2, expiry_reminder…
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    language: Mapped[str] = mapped_column(String(8), nullable=False, default="en")
    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class QuoteStatusEvent(UUIDPrimaryKeyMixin, TenantMixin, Base):
    """Recorded lifecycle transitions (S2-F01). Only facts that actually happened are stored."""

    __tablename__ = "quote_status_events"
    __table_args__ = (Index("ix_qse_org_quote", "organization_id", "quote_id"),)

    quote_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("quotes.id", ondelete="CASCADE"), nullable=False
    )
    from_status: Mapped[str] = mapped_column(String(20), nullable=False)
    to_status: Mapped[str] = mapped_column(String(20), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # manual|send|inbound|system
    evidence: Mapped[str | None] = mapped_column(Text)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FollowUpSchedule(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "follow_up_schedules"
    __table_args__ = (
        UniqueConstraint("organization_id", "idempotency_key", name="uq_schedule_idem"),
        Index("ix_schedules_org_status_due", "organization_id", "status", "due_at"),
    )

    quote_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("quotes.id", ondelete="CASCADE"), nullable=False
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL")
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="draft_only")  # draft_only|auto_send
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    draft_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    cancelled_by: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    cancelled_reason: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(String(500))


class CommunicationDraft(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "communication_drafts"
    __table_args__ = (
        Index("ix_drafts_org_quote", "organization_id", "quote_id"),
        UniqueConstraint("organization_id", "send_idempotency_key", name="uq_draft_send_idem"),
    )

    quote_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("quotes.id", ondelete="CASCADE"), nullable=False
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL")
    )
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    channel: Mapped[str] = mapped_column(String(16), nullable=False, default="email")
    to_email: Mapped[str | None] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    template_key: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    generated_reasons: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    send_idempotency_key: Mapped[str | None] = mapped_column(String(120))
    provider: Mapped[str | None] = mapped_column(String(32))
    provider_message_id: Mapped[str | None] = mapped_column(String(300))
    sent_by: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(500))
    created_by: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))


class CommunicationEvent(UUIDPrimaryKeyMixin, TenantMixin, Base):
    """Recorded communication facts (sent, bounced, reply_received…). Never inferred."""

    __tablename__ = "communication_events"
    __table_args__ = (
        Index("ix_comm_events_org_quote", "organization_id", "quote_id"),
        UniqueConstraint(
            "organization_id", "provider", "provider_message_id", "kind", name="uq_comm_event_provider_msg"
        ),
    )

    quote_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("quotes.id", ondelete="SET NULL")
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL")
    )
    draft_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # sent|delivered|bounced|reply_received|opt_out
    direction: Mapped[str] = mapped_column(String(8), nullable=False)  # out|in
    provider: Mapped[str | None] = mapped_column(String(32))
    provider_message_id: Mapped[str | None] = mapped_column(String(300))
    subject: Mapped[str | None] = mapped_column(String(300))
    snippet: Mapped[str | None] = mapped_column(String(500))  # bounded, never full bodies in events
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IntegrationConnection(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "integration_connections"
    __table_args__ = (UniqueConstraint("organization_id", "provider", name="uq_integration_org_provider"),)

    provider: Mapped[str] = mapped_column(String(32), nullable=False)  # simulator|smtp|gmail|msgraph
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")  # active|revoked|error
    scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)  # non-secret settings only
    encrypted_secret: Mapped[bytes | None] = mapped_column(Text().with_variant(Text, "postgresql"))  # base64 envelope
    account_email: Mapped[str | None] = mapped_column(String(320))
    connected_by: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    last_error: Mapped[str | None] = mapped_column(String(500))
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_inbound_cursor: Mapped[str | None] = mapped_column(String(200))


class JobRun(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """PostgreSQL-backed job queue (SKIP LOCKED) with idempotency keys, capped backoff and dead letters."""

    __tablename__ = "job_runs"
    __table_args__ = (
        UniqueConstraint("organization_id", "idempotency_key", name="uq_job_idem"),
        Index("ix_jobs_status_next", "status", "next_run_at"),
    )

    kind: Mapped[str] = mapped_column(String(48), nullable=False)  # send_draft|sync_inbound|schedule_followups
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)  # ids only, no bodies
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(100))
    last_error: Mapped[str | None] = mapped_column(String(500))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
