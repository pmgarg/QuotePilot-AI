"""Stage 4 — governed AI sales-operations platform: entitlements, bounded assistant, tool invocations with approval
checkpoints, versioned workflows with dry-run, AI usage traces and kill switches."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Subscription(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """One row per organisation: plan code + optional limit overrides. Billing adapter is test-mode only."""

    __tablename__ = "subscriptions"
    __table_args__ = (UniqueConstraint("organization_id", name="uq_subscription_org"),)

    plan_code: Mapped[str] = mapped_column(String(32), nullable=False, default="starter")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")  # active|past_due|cancelled
    limit_overrides: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    billing_reference: Mapped[str | None] = mapped_column(String(100))  # test-mode invoice/customer id
    changed_by: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))


class UsageCounter(UUIDPrimaryKeyMixin, TenantMixin, Base):
    __tablename__ = "usage_counters"
    __table_args__ = (UniqueConstraint("organization_id", "metric", "period_key", name="uq_usage_org_metric_period"),)

    metric: Mapped[str] = mapped_column(String(48), nullable=False)  # rfqs_per_month, ai_calls_per_day, ...
    period_key: Mapped[str] = mapped_column(String(16), nullable=False)  # 2026-11 or 2026-11-02
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AssistantSession(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "assistant_sessions"
    __table_args__ = (Index("ix_assistant_sessions_org_user", "organization_id", "user_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    title: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")  # open|closed
    turns: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    messages: Mapped[list[AssistantMessage]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="AssistantMessage.created_at"
    )


class AssistantMessage(UUIDPrimaryKeyMixin, TenantMixin, Base):
    __tablename__ = "assistant_messages"

    session_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("assistant_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user|assistant|tool
    content: Mapped[str] = mapped_column(Text, nullable=False)  # redacted before storage
    proposed_actions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    session: Mapped[AssistantSession] = relationship(back_populates="messages")


class ToolInvocation(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """Every tool call the platform makes on a user's behalf: proposed → (approved) → executed, or rejected/failed.
    Side-effect tools are never executed before an explicit human approval (S4-F03 / S4-A03)."""

    __tablename__ = "tool_invocations"
    __table_args__ = (Index("ix_tool_invocations_org_status", "organization_id", "status"),)

    tool: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    args: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    args_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="proposed")
    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)  # assistant|workflow|manual
    session_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    run_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    requested_by: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)  # summary only, redacted
    error: Mapped[str | None] = mapped_column(String(500))


class WorkflowDefinition(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "workflow_definitions"
    __table_args__ = (UniqueConstraint("organization_id", "key", name="uq_workflow_org_key"),)

    key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    active_version_no: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))

    versions: Mapped[list[WorkflowVersion]] = relationship(
        back_populates="definition_ref", cascade="all, delete-orphan", order_by="WorkflowVersion.version_no"
    )


class WorkflowVersion(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "workflow_versions"
    __table_args__ = (UniqueConstraint("definition_id", "version_no", name="uq_workflow_version_no"),)

    definition_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("workflow_definitions.id", ondelete="CASCADE"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")  # draft|active|archived
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    validation: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_by: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))

    definition_ref: Mapped[WorkflowDefinition] = relationship(back_populates="versions")


class WorkflowRun(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "workflow_runs"
    __table_args__ = (Index("ix_workflow_runs_org_status", "organization_id", "status"),)

    definition_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    mode: Mapped[str] = mapped_column(String(8), nullable=False)  # dry_run|live
    trigger: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    # running|awaiting_approval|succeeded|failed|compensated|killed|budget_exceeded
    current_step: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    budget_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_explanation: Mapped[str | None] = mapped_column(Text)
    started_by: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    steps: Mapped[list[WorkflowStep]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="WorkflowStep.index"
    )


class WorkflowStep(UUIDPrimaryKeyMixin, TenantMixin, Base):
    __tablename__ = "workflow_steps"
    __table_args__ = (UniqueConstraint("run_id", "index", name="uq_workflow_step_index"),)

    run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    index: Mapped[int] = mapped_column(Integer, nullable=False)
    step_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tool: Mapped[str] = mapped_column(String(64), nullable=False)
    args: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # pending|awaiting_approval|executed|skipped|failed|compensated|would_execute
    invocation_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(String(500))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    run: Mapped[WorkflowRun] = relationship(back_populates="steps")


class AIUsage(UUIDPrimaryKeyMixin, TenantMixin, Base):
    """Model-call trace: counts and purpose only — never prompts, completions or customer content (S4-F07)."""

    __tablename__ = "ai_usage"
    __table_args__ = (Index("ix_ai_usage_org_created", "organization_id", "created_at"),)

    user_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)  # extraction|assistant|workflow
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_estimate: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, default=Decimal("0"))
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False, default="ok")  # ok|rejected|error|timeout
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class KillSwitch(UUIDPrimaryKeyMixin, TenantMixin, Base):
    """Tenant-scoped kill switch (the platform-wide switch is the `PLATFORM_KILL_SWITCH` environment flag)."""

    __tablename__ = "kill_switches"
    __table_args__ = (Index("ix_kill_switches_org_active", "organization_id", "released_at"),)

    scope: Mapped[str] = mapped_column(String(16), nullable=False, default="org")  # org
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    engaged_by: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    engaged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_by: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    runs_terminated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
