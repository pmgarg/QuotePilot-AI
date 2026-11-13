"""Stage 4 request/response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


def contains_nul(value: Any) -> bool:
    """True if any string, dict key or nested list/dict member contains the NUL control character."""
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(contains_nul(k) or contains_nul(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(contains_nul(v) for v in value)
    return False


class RequestModel(BaseModel):
    """Base of Stage 4 request bodies: rejects NUL (\\x00) anywhere in the payload (PostgreSQL cannot store it)."""

    @model_validator(mode="before")
    @classmethod
    def _reject_nul(cls, data: Any) -> Any:
        if contains_nul(data):
            raise ValueError("NUL characters are not allowed")
        return data


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class PlanOut(BaseModel):
    plan_code: str
    plan_name: str
    status: str
    limits: dict[str, Any]
    usage: dict[str, int]
    billing_reference: str | None
    billing_mode: str = "test"
    plans: dict[str, dict[str, Any]]


class PlanChangeIn(RequestModel):
    plan_code: str = Field(min_length=1, max_length=32)


class AssistantSessionIn(RequestModel):
    title: str | None = Field(default=None, max_length=200)


class AssistantMessageIn(RequestModel):
    text: str = Field(min_length=1, max_length=4000)


class AssistantMessageOut(ORM):
    id: uuid.UUID
    role: str
    content: str
    proposed_actions: list[dict[str, Any]]
    citations: list[dict[str, Any]]
    created_at: datetime


class AssistantSessionOut(ORM):
    id: uuid.UUID
    title: str | None
    status: str
    turns: int
    created_at: datetime
    messages: list[AssistantMessageOut] = []


class ToolOut(BaseModel):
    name: str
    scope: str
    description: str
    requires_approval: bool
    args_schema: dict[str, Any]


class InvocationProposeIn(RequestModel):
    tool: str = Field(min_length=1, max_length=64)
    args: dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = False


class InvocationOut(ORM):
    id: uuid.UUID
    tool: str
    scope: str
    args: dict[str, Any]
    status: str
    requires_approval: bool
    dry_run: bool
    origin: str
    session_id: uuid.UUID | None
    run_id: uuid.UUID | None
    requested_by: uuid.UUID | None
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    decision_reason: str | None
    executed_at: datetime | None
    result: dict[str, Any]
    error: str | None
    created_at: datetime


class DecisionIn(RequestModel):
    reason: str | None = Field(default=None, max_length=2000)


class WorkflowIn(RequestModel):
    key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    definition: dict[str, Any]


class WorkflowVersionOut(ORM):
    version_no: int
    status: str
    definition: dict[str, Any]
    validation: dict[str, Any]
    created_at: datetime


class WorkflowOut(ORM):
    id: uuid.UUID
    key: str
    name: str
    enabled: bool
    active_version_no: int | None
    versions: list[WorkflowVersionOut] = []


class RunIn(RequestModel):
    mode: str = Field(pattern="^(dry_run|live)$")
    context: dict[str, Any] = Field(default_factory=dict)
    version_no: int | None = Field(default=None, ge=1)


class StepOut(ORM):
    index: int
    step_id: str
    tool: str
    args: dict[str, Any]
    status: str
    invocation_id: uuid.UUID | None
    attempts: int
    output: dict[str, Any]
    error: str | None


class RunOut(ORM):
    id: uuid.UUID
    definition_id: uuid.UUID
    version_no: int
    mode: str
    trigger: str
    context: dict[str, Any]
    status: str
    current_step: int
    budget_used: int
    failure_explanation: str | None
    created_at: datetime
    finished_at: datetime | None
    steps: list[StepOut] = []


class KillSwitchIn(RequestModel):
    reason: str = Field(min_length=3, max_length=2000)


class KillSwitchOut(BaseModel):
    engaged: bool
    platform: bool
    reason: str | None = None
    engaged_at: datetime | None = None
    runs_terminated: int | None = None
