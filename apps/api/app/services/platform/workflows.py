"""Org-configurable workflows (S4-F01/F05): versioned JSON definitions validated against a strict schema, a dry-run
executor that records intended effects without side effects (S4-A09), and a live runner with approval checkpoints
for side-effect tools (S4-A03), bounded depth/retries/timeouts, a per-organisation circuit breaker, kill-switch
checks per step (S4-A07) and compensation hooks on failure (S4-F04)."""

from __future__ import annotations

import re
import time
import uuid
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError, KillSwitchEngaged, LimitExceeded, NotFoundError, StateError, ValidationFailed
from app.db.base import utcnow
from app.models.org import Organization
from app.models.platform import ToolInvocation, WorkflowDefinition, WorkflowRun, WorkflowStep, WorkflowVersion
from app.services import audit
from app.services.platform import entitlements, invocations, killswitch
from app.services.platform.tools import REGISTRY, Actor, allowed_tools, validate_args

TRIGGERS = {"manual", "quote.approved", "quote.won", "order.confirmed"}
CONTEXT_KEYS = {"quote_id", "quote_number", "customer_id", "customer_name", "order_id", "draft_id"}
TEMPLATE_RE = re.compile(r"^\{\{\s*([a-z_]+)\s*\}\}$")


class StepDef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    tool: str = Field(min_length=1, max_length=64)
    args: dict[str, Any] = Field(default_factory=dict)
    on_failure: str = Field(default="stop", pattern="^(stop|compensate|continue)$")


class WorkflowDef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    trigger: str = Field(default="manual")
    steps: list[StepDef] = Field(min_length=1, max_length=50)
    max_depth: int = Field(default=8, ge=1, le=50)


COMPENSATIONS: dict[str, str] = {
    # a compensation is itself a registered, audited action; nothing here is irreversible without one
    "draft_follow_up": "cancel the draft (no message was sent)",
    "propose_order": "cancel the proposed order (it was never confirmed)",
    "request_customer_deletion": "reject the pending deletion request",
}


def validate_definition(db: Session, org: Organization, role: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Returns a validation report; `ok` is False when the definition must not be activated."""
    settings = get_settings()
    errors: list[str] = []
    warnings: list[str] = []
    try:
        wf = WorkflowDef.model_validate(raw)
    except ValidationError as exc:
        return {
            "ok": False,
            "errors": [str(e["msg"])[:200] + " at " + "/".join(str(x) for x in e["loc"]) for e in exc.errors()][:20],
            "warnings": [],
        }
    if wf.trigger not in TRIGGERS:
        errors.append(f"unknown trigger '{wf.trigger}' (allowed: {', '.join(sorted(TRIGGERS))})")
    if len(wf.steps) > min(wf.max_depth, settings.workflow_max_steps):
        errors.append(f"too many steps: {len(wf.steps)} > max depth {min(wf.max_depth, settings.workflow_max_steps)}")
    plan_scopes = entitlements.entitlements_for(db, org.id).limits.get("tool_scopes", [])
    allowed = {t.name for t in allowed_tools(role, plan_scopes)}
    seen: set[str] = set()
    for s in wf.steps:
        if s.id in seen:
            errors.append(f"duplicate step id '{s.id}'")
        seen.add(s.id)
        tool = REGISTRY.get(s.tool)
        if tool is None:
            errors.append(f"step '{s.id}': unknown tool '{s.tool}'")
            continue
        if s.tool not in allowed:
            errors.append(f"step '{s.id}': tool '{s.tool}' is not allowed for this user/plan")
        for v in s.args.values():
            if isinstance(v, str) and v.startswith("{{"):
                m = TEMPLATE_RE.match(v)
                if not m or m.group(1) not in CONTEXT_KEYS:
                    errors.append(f"step '{s.id}': template '{v}' is not a known context key")
        if tool.requires_approval:
            warnings.append(f"step '{s.id}' ({s.tool}) pauses the run for a human approval")
        if s.on_failure == "compensate" and s.tool not in COMPENSATIONS and tool.side_effect:
            warnings.append(f"step '{s.id}': no compensation hook for '{s.tool}'; failure will stop the run")
        # static schema check with templates replaced by placeholders
        try:
            probe = {
                k: ("00000000-0000-0000-0000-000000000000" if k.endswith("_id") else "X")
                if isinstance(v, str) and v.startswith("{{")
                else v
                for k, v in s.args.items()
            }
            validate_args(tool, probe)
        except AppError as exc:
            errors.append(f"step '{s.id}': {exc.detail}")
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def get_definition(db: Session, org_id: uuid.UUID, definition_id: uuid.UUID) -> WorkflowDefinition:
    d = db.scalar(
        select(WorkflowDefinition).where(
            WorkflowDefinition.id == definition_id, WorkflowDefinition.organization_id == org_id
        )
    )
    if d is None:
        raise NotFoundError("Workflow not found")
    return d


def create_or_update(
    db: Session, *, org: Organization, actor: Actor, key: str, raw: dict[str, Any]
) -> tuple[WorkflowDefinition, WorkflowVersion]:
    report = validate_definition(db, org, actor.role, raw)
    d = db.scalar(
        select(WorkflowDefinition).where(WorkflowDefinition.organization_id == org.id, WorkflowDefinition.key == key)
    )
    name = str(raw.get("name") or key)[:200]
    if d is None:
        d = WorkflowDefinition(organization_id=org.id, key=key, name=name, created_by=actor.user.id)
        db.add(d)
        db.flush()
    else:
        d.name = name
    version_no = (max((v.version_no for v in d.versions), default=0)) + 1
    v = WorkflowVersion(
        organization_id=org.id,
        definition_id=d.id,
        version_no=version_no,
        status="draft",
        definition=raw,
        validation=report,
        created_by=actor.user.id,
    )
    db.add(v)
    db.flush()
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=actor.user.id,
        action="workflow.version_created",
        entity_type="workflow",
        entity_id=d.id,
        ip=actor.ip,
        summary={"key": key, "version_no": version_no, "valid": report["ok"]},
    )
    return d, v


def activate(
    db: Session, *, org: Organization, actor: Actor, d: WorkflowDefinition, version_no: int
) -> WorkflowDefinition:
    v = next((x for x in d.versions if x.version_no == version_no), None)
    if v is None:
        raise NotFoundError("Version not found")
    report = validate_definition(db, org, actor.role, v.definition)  # re-validated against *current* policy
    v.validation = report
    if not report["ok"]:
        raise ValidationFailed("Version does not validate: " + "; ".join(report["errors"])[:500], field="definition")
    if not d.enabled:
        active_count = db.scalar(
            select(func.count()).where(
                WorkflowDefinition.organization_id == org.id, WorkflowDefinition.enabled.is_(True)
            )
        )
        entitlements.require_capacity(db, org.id, "active_workflows", int(active_count or 0) + 1)
    for other in d.versions:
        if other.status == "active":
            other.status = "archived"
    v.status = "active"
    d.active_version_no = version_no
    d.enabled = True
    db.flush()
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=actor.user.id,
        action="workflow.activated",
        entity_type="workflow",
        entity_id=d.id,
        ip=actor.ip,
        summary={"version_no": version_no},
    )
    return d


def disable(db: Session, *, org: Organization, actor: Actor, d: WorkflowDefinition) -> WorkflowDefinition:
    d.enabled = False
    db.flush()
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=actor.user.id,
        action="workflow.disabled",
        entity_type="workflow",
        entity_id=d.id,
        ip=actor.ip,
    )
    return d


# ------------------------------------------------------------------ runner
def _breaker_open(db: Session, org_id: uuid.UUID) -> bool:
    """Circuit breaker: N consecutive failed live runs within the cooldown window stop new runs for the tenant."""
    s = get_settings()
    recent = db.scalars(
        select(WorkflowRun.status)
        .where(
            WorkflowRun.organization_id == org_id,
            WorkflowRun.mode == "live",
            WorkflowRun.created_at >= utcnow() - timedelta(seconds=s.circuit_breaker_cooldown_seconds),
        )
        .order_by(WorkflowRun.created_at.desc())
        .limit(s.circuit_breaker_failures)
    ).all()
    return len(recent) >= s.circuit_breaker_failures and all(st in ("failed", "compensated") for st in recent)


def _render(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in args.items():
        if isinstance(v, str):
            m = TEMPLATE_RE.match(v)
            if m:
                if m.group(1) not in context:
                    raise ValidationFailed(f"context key '{m.group(1)}' missing for argument '{k}'", field=k)
                out[k] = context[m.group(1)]
                continue
        out[k] = v
    return out


def start_run(
    db: Session,
    *,
    org: Organization,
    actor: Actor,
    d: WorkflowDefinition,
    mode: str,
    context: dict[str, Any],
    trigger: str = "manual",
    version_no: int | None = None,
) -> WorkflowRun:
    if mode not in ("dry_run", "live"):
        raise ValidationFailed("mode must be dry_run or live", field="mode")
    vno = version_no or d.active_version_no or max((v.version_no for v in d.versions), default=0)
    v = next((x for x in d.versions if x.version_no == vno), None)
    if v is None:
        raise StateError("Workflow has no version to run")
    if mode == "live":
        if not d.enabled or v.status != "active":
            raise StateError("Only an enabled workflow's active version can run live; use dry_run for drafts")
        killswitch.check(db, org.id)
        if _breaker_open(db, org.id):
            raise LimitExceeded(
                "Circuit breaker open: recent workflow runs failed; retry after the cooldown", metric="circuit_breaker"
            )
        entitlements.consume(db, org.id, "workflow_runs_per_day")
    ctx = {k: str(val) for k, val in (context or {}).items() if k in CONTEXT_KEYS}
    wf = WorkflowDef.model_validate(v.definition)
    run = WorkflowRun(
        organization_id=org.id,
        definition_id=d.id,
        version_no=vno,
        mode=mode,
        trigger=trigger,
        context=ctx,
        status="running",
        started_by=actor.user.id,
    )
    db.add(run)
    db.flush()
    for i, s in enumerate(wf.steps):
        run.steps.append(
            WorkflowStep(organization_id=org.id, run_id=run.id, index=i, step_id=s.id, tool=s.tool, args=s.args)
        )
    db.flush()
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=actor.user.id,
        action="workflow.run_started",
        entity_type="workflow_run",
        entity_id=run.id,
        ip=actor.ip,
        summary={"mode": mode, "trigger": trigger, "version_no": vno},
    )
    return advance(db, org=org, actor=actor, run=run)


def _finish(run: WorkflowRun, status: str, explanation: str | None = None) -> None:
    run.status = status
    run.failure_explanation = explanation
    run.finished_at = utcnow()


def _compensate(db: Session, org: Organization, actor: Actor, run: WorkflowRun, upto: int) -> list[str]:
    """Undo side effects of executed steps (newest first) where a compensation exists. Each compensation goes through
    the ordinary audited services, so it is visible like any other action."""
    from app.models.followup import CommunicationDraft
    from app.models.intelligence import Order
    from app.services.intelligence import orders as order_service

    notes: list[str] = []
    for step in sorted(run.steps, key=lambda s: -s.index):
        if step.index >= upto or step.status != "executed":
            continue
        out = step.output or {}
        try:
            if step.tool == "propose_order" and out.get("order_id"):
                order = db.get(Order, uuid.UUID(out["order_id"]))
                if order is not None and order.status == "proposed":
                    order_service.transition(
                        db,
                        org=org,
                        order=order,
                        to_status="cancelled",
                        actor_user_id=actor.user.id,
                        ip=actor.ip,
                        reason=f"workflow run {run.id} compensation",
                    )
            elif step.tool == "draft_follow_up" and out.get("draft_id"):
                draft = db.get(CommunicationDraft, uuid.UUID(out["draft_id"]))
                if draft is not None and draft.status in ("draft", "approved"):
                    draft.status = "cancelled"
            else:
                continue
            step.status = "compensated"
            notes.append(f"{step.step_id}: {COMPENSATIONS.get(step.tool, 'compensated')}")
        except AppError as exc:
            notes.append(f"{step.step_id}: compensation failed ({exc.code})")
    return notes


def advance(db: Session, *, org: Organization, actor: Actor, run: WorkflowRun) -> WorkflowRun:
    """Execute steps from `current_step` until the run finishes or pauses for an approval."""
    settings = get_settings()
    wf = WorkflowDef.model_validate(
        next(
            v.definition
            for v in get_definition(db, org.id, run.definition_id).versions
            if v.version_no == run.version_no
        )
    )
    steps_by_index = {s.index: s for s in run.steps}
    while run.current_step < len(wf.steps):
        if run.status not in ("running",):
            break
        sdef = wf.steps[run.current_step]
        step = steps_by_index[run.current_step]
        if run.mode == "live":
            try:
                killswitch.check(db, org.id)
            except KillSwitchEngaged:
                _finish(run, "killed", "kill switch engaged during the run")
                break
        if run.budget_used >= min(wf.max_depth, settings.workflow_max_steps):
            _finish(run, "budget_exceeded", f"step budget {settings.workflow_max_steps} exhausted")
            break
        step.started_at = utcnow()
        started = time.monotonic()
        try:
            args = _render(sdef.args, run.context)
            inv = invocations.propose(
                db,
                org=org,
                actor=actor,
                tool_name=sdef.tool,
                raw_args=args,
                origin="workflow",
                dry_run=run.mode == "dry_run",
                run_id=run.id,
            )
            step.invocation_id = inv.id
            step.attempts += 1
            run.budget_used += 1
            if time.monotonic() - started > settings.workflow_step_timeout_seconds:
                raise LimitExceeded(f"step exceeded {settings.workflow_step_timeout_seconds}s", metric="step_timeout")
            if inv.status == "would_execute":
                step.status = "would_execute"
                step.output = inv.result
            elif inv.status == "proposed":
                step.status = "awaiting_approval"
                run.status = "awaiting_approval"
                step.finished_at = utcnow()
                break  # a human continues the run via approve_step
            else:
                step.status = "executed"
                full = getattr(inv, "_last_result", None) or inv.result
                step.output = {k: v for k, v in full.items() if k not in ("content_b64", "_full")}
                if "quote_id" in full and "quote_id" not in run.context:
                    run.context["quote_id"] = str(full["quote_id"])
                if "draft_id" in full:
                    run.context = {**run.context, "draft_id": str(full["draft_id"])}
                if "order_id" in full:
                    run.context = {**run.context, "order_id": str(full["order_id"])}
            step.finished_at = utcnow()
            run.current_step += 1
        except AppError as exc:
            step.attempts += 1
            step.error = f"{exc.code}: {exc.detail}"[:500]
            retryable = exc.code in ("rate_limited", "limit_exceeded") and exc.code != "kill_switch_engaged"
            if retryable and step.attempts <= settings.workflow_step_retries and exc.code == "rate_limited":
                continue  # bounded retry
            step.status = "failed"
            step.finished_at = utcnow()
            if sdef.on_failure == "continue":
                run.current_step += 1
                continue
            if sdef.on_failure == "compensate" and run.mode == "live":
                notes = _compensate(db, org, actor, run, run.current_step)
                _finish(
                    run,
                    "compensated",
                    f"step '{sdef.id}' failed ({step.error}); compensated: {'; '.join(notes) or 'nothing to undo'}",
                )
            else:
                _finish(run, "failed", f"step '{sdef.id}' failed: {step.error}")
            break
    else:
        if run.status == "running":
            _finish(run, "succeeded")
    db.flush()
    if run.status not in ("running", "awaiting_approval"):
        audit.record(
            db,
            organization_id=org.id,
            actor_user_id=actor.user.id,
            action="workflow.run_finished",
            entity_type="workflow_run",
            entity_id=run.id,
            ip=actor.ip,
            summary={"status": run.status, "steps": run.budget_used, "explanation": run.failure_explanation},
        )
    return run


def get_run(db: Session, org_id: uuid.UUID, run_id: uuid.UUID) -> WorkflowRun:
    r = db.scalar(select(WorkflowRun).where(WorkflowRun.id == run_id, WorkflowRun.organization_id == org_id))
    if r is None:
        raise NotFoundError("Run not found")
    return r


def decide_step(
    db: Session, *, org: Organization, actor: Actor, run: WorkflowRun, approve: bool, reason: str | None
) -> WorkflowRun:
    if run.status != "awaiting_approval":
        raise StateError("Run is not waiting for an approval")
    step = next(s for s in run.steps if s.index == run.current_step)
    inv = db.get(ToolInvocation, step.invocation_id) if step.invocation_id else None
    if inv is None:
        raise StateError("Pending invocation missing")
    if not approve:
        invocations.decide(db, org=org, actor=actor, inv=inv, approve=False, reason=reason)
        step.status = "failed"
        step.error = "rejected by approver"
        _finish(run, "failed", f"step '{step.step_id}' rejected: {reason or 'no reason given'}")
        db.flush()
        return run
    invocations.decide(db, org=org, actor=actor, inv=inv, approve=True, reason=reason)
    step.status = "executed"
    full = getattr(inv, "_last_result", None) or inv.result
    step.output = {k: v for k, v in full.items() if k not in ("content_b64", "_full")}
    for key in ("draft_id", "order_id", "quote_id"):
        if key in full:
            run.context = {**run.context, key: str(full[key])}
    step.finished_at = utcnow()
    run.current_step += 1
    run.status = "running"
    db.flush()
    return advance(db, org=org, actor=actor, run=run)


def dispatch_event(
    db: Session, *, org: Organization, actor: Actor, event: str, context: dict[str, Any]
) -> list[WorkflowRun]:
    """Start a live run of every enabled workflow bound to `event`. Never raises into the caller's transaction."""
    runs: list[WorkflowRun] = []
    defs = db.scalars(
        select(WorkflowDefinition).where(
            WorkflowDefinition.organization_id == org.id, WorkflowDefinition.enabled.is_(True)
        )
    ).all()
    for d in defs:
        v = next((x for x in d.versions if x.version_no == d.active_version_no), None)
        if v is None or v.definition.get("trigger") != event:
            continue
        try:
            with db.begin_nested():
                runs.append(start_run(db, org=org, actor=actor, d=d, mode="live", context=context, trigger=event))
        except AppError:
            continue
    return runs
