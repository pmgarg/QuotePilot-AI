"""Stage 4 routes: command center, bounded assistant, approval-gated actions, workflows, plan/usage, kill switch and
support export. Every mutation is CSRF-protected (global dependency), server-authorized and audited; cross-tenant
ids return 404 and role denials 403."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import DB, Principal, PrincipalDep, clean_param
from app.core.config import get_settings
from app.core.errors import ForbiddenError
from app.db.base import utcnow
from app.models.audit import AuditEvent
from app.models.followup import CommunicationDraft, FollowUpSchedule
from app.models.intelligence import DeletionRequest, Order, SyncConflict
from app.models.platform import (
    AIUsage,
    AssistantSession,
    ToolInvocation,
    WorkflowDefinition,
    WorkflowRun,
)
from app.models.quote import Quote, QuoteVersion
from app.models.rfq import RFQ
from app.schemas.platform import (
    AssistantMessageIn,
    AssistantMessageOut,
    AssistantSessionIn,
    AssistantSessionOut,
    DecisionIn,
    InvocationOut,
    InvocationProposeIn,
    KillSwitchIn,
    KillSwitchOut,
    PlanChangeIn,
    PlanOut,
    RunIn,
    RunOut,
    ToolOut,
    WorkflowIn,
    WorkflowOut,
)
from app.services import audit
from app.services.authz import Permission, has_permission
from app.services.platform import assistant, entitlements, invocations, killswitch, workflows
from app.services.platform.tools import Actor, allowed_tools

router = APIRouter(tags=["platform"])


def _actor(p: Principal) -> Actor:
    return Actor(user=p.user, role=p.role, ip=p.ip)


def _count(db: Session, stmt: Any) -> int:
    return int(db.scalar(stmt) or 0)


# ------------------------------------------------------------ command center
@router.get("/command-center")
def command_center(principal: PrincipalDep, db: DB) -> dict[str, Any]:
    """S4-F01: one view of everything waiting for a human, filtered by what this role may act on."""
    org = principal.org_id
    role = principal.role
    q: dict[str, Any] = {
        "rfqs_to_review": _count(
            db, select(func.count()).where(RFQ.organization_id == org, RFQ.status.in_(("parsed", "needs_review")))
        ),
        "quotes_draft": _count(db, select(func.count()).where(Quote.organization_id == org, Quote.status == "draft")),
        "quotes_pending_approval": _count(
            db,
            select(func.count())
            .select_from(QuoteVersion)
            .where(QuoteVersion.organization_id == org, QuoteVersion.status == "pending_approval"),
        ),
        "drafts_awaiting_send": _count(
            db,
            select(func.count()).where(CommunicationDraft.organization_id == org, CommunicationDraft.status == "draft"),
        ),
        "follow_ups_due_7d": _count(
            db,
            select(func.count()).where(
                FollowUpSchedule.organization_id == org,
                FollowUpSchedule.status == "pending",
                FollowUpSchedule.due_at <= utcnow() + timedelta(days=7),
            ),
        ),
        "orders_proposed": _count(
            db, select(func.count()).where(Order.organization_id == org, Order.status == "proposed")
        ),
        "actions_awaiting_approval": _count(
            db,
            select(func.count()).where(
                ToolInvocation.organization_id == org,
                ToolInvocation.status == "proposed",
                ToolInvocation.requires_approval.is_(True),
                *(
                    ()
                    if has_permission(role, Permission.MANAGE_WORKFLOWS)
                    else (ToolInvocation.requested_by == principal.user.id,)
                ),
            ),
        ),
        "workflow_runs_awaiting_approval": _count(
            db,
            select(func.count()).where(WorkflowRun.organization_id == org, WorkflowRun.status == "awaiting_approval"),
        ),
    }
    if has_permission(role, Permission.MANAGE_CONNECTORS):
        q["sync_conflicts_open"] = _count(
            db, select(func.count()).where(SyncConflict.organization_id == org, SyncConflict.status == "open")
        )
    if has_permission(role, Permission.MANAGE_RETENTION):
        q["deletion_requests_open"] = _count(
            db,
            select(func.count()).where(DeletionRequest.organization_id == org, DeletionRequest.status == "requested"),
        )
    ks = killswitch.active_switch(db, org)
    return {
        "queues": q,
        "kill_switch": {"engaged": killswitch.is_engaged(db, org), "reason": ks.reason if ks else None},
        "plan": entitlements.entitlements_for(db, org).plan_code,
    }


# ------------------------------------------------------------------- plan/ops
@router.get("/platform/plan", response_model=PlanOut)
def get_plan(principal: PrincipalDep, db: DB) -> PlanOut:
    ent = entitlements.entitlements_for(db, principal.org_id)
    use = {m: entitlements.usage(db, principal.org_id, m) for m in entitlements.PERIODS}
    return PlanOut(
        plan_code=ent.plan_code,
        plan_name=ent.plan_name,
        status=ent.status,
        limits=ent.limits,
        usage=use,
        billing_reference=ent.billing_reference if has_permission(principal.role, Permission.MANAGE_BILLING) else None,
        plans={
            k: {"name": v["name"], "price_per_month": v["price_per_month"], "limits": v["limits"]}
            for k, v in entitlements.PLANS.items()
        },
    )


@router.post("/platform/plan", response_model=PlanOut)
def change_plan(payload: PlanChangeIn, principal: PrincipalDep, db: DB) -> PlanOut:
    principal.require(Permission.MANAGE_BILLING)
    entitlements.change_plan(
        db, org=principal.organization, actor_user_id=principal.user.id, ip=principal.ip, plan_code=payload.plan_code
    )
    return get_plan(principal, db)


@router.get("/platform/usage")
def usage(principal: PrincipalDep, db: DB, days: int = Query(default=30, ge=1, le=365)) -> dict[str, Any]:
    principal.require(Permission.VIEW_OPS)
    since = utcnow() - timedelta(days=days)
    rows = db.execute(
        select(
            AIUsage.provider,
            AIUsage.purpose,
            AIUsage.outcome,
            func.count(),
            func.sum(AIUsage.input_tokens),
            func.sum(AIUsage.output_tokens),
        )
        .where(AIUsage.organization_id == principal.org_id, AIUsage.created_at >= since)
        .group_by(AIUsage.provider, AIUsage.purpose, AIUsage.outcome)
    ).all()
    inv = db.execute(
        select(ToolInvocation.tool, ToolInvocation.status, func.count())
        .where(ToolInvocation.organization_id == principal.org_id, ToolInvocation.created_at >= since)
        .group_by(ToolInvocation.tool, ToolInvocation.status)
    ).all()
    runs = db.execute(
        select(WorkflowRun.status, func.count())
        .where(WorkflowRun.organization_id == principal.org_id, WorkflowRun.created_at >= since)
        .group_by(WorkflowRun.status)
    ).all()
    return {
        "days": days,
        "ai_calls": [
            {
                "provider": p,
                "purpose": pu,
                "outcome": o,
                "calls": c,
                "input_tokens": int(i or 0),
                "output_tokens": int(out or 0),
            }
            for p, pu, o, c, i, out in rows
        ],
        "tool_invocations": [{"tool": t, "status": s, "count": c} for t, s, c in inv],
        "workflow_runs": {s: c for s, c in runs},
    }


@router.get("/platform/support-export")
def support_export(principal: PrincipalDep, db: DB) -> dict[str, Any]:
    """Diagnostic bundle for support: configuration and counts only — no customer data, prices or message bodies."""
    principal.require(Permission.VIEW_OPS)
    org = principal.organization
    ent = entitlements.entitlements_for(db, org.id)
    recent = db.execute(
        select(AuditEvent.action, func.count())
        .where(AuditEvent.organization_id == org.id, AuditEvent.created_at >= utcnow() - timedelta(days=7))
        .group_by(AuditEvent.action)
    ).all()
    failed_runs = db.scalars(
        select(WorkflowRun)
        .where(
            WorkflowRun.organization_id == org.id,
            WorkflowRun.status.in_(("failed", "compensated", "killed", "budget_exceeded")),
        )
        .order_by(WorkflowRun.created_at.desc())
        .limit(20)
    ).all()
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=principal.user.id,
        action="support.export",
        entity_type="organization",
        entity_id=org.id,
        ip=principal.ip,
    )
    s = get_settings()
    return {
        "generated_at": utcnow().isoformat(),
        "organization_id": str(org.id),
        "app_env": s.app_env,
        "plan": {"code": ent.plan_code, "status": ent.status, "limits": ent.limits},
        "settings": {
            "default_currency": org.default_currency,
            "rounding_mode": org.rounding_mode,
            "ai_provider": s.ai_provider,
            "ai_consented": org.ai_processing_consented_at is not None,
            "kill_switch": killswitch.is_engaged(db, org.id),
        },
        "audit_actions_7d": {a: c for a, c in recent},
        "recent_failed_runs": [
            {"run_id": str(r.id), "status": r.status, "explanation": (r.failure_explanation or "")[:300]}
            for r in failed_runs
        ],
        "contains_customer_data": False,
    }


@router.get("/platform/kill-switch", response_model=KillSwitchOut)
def kill_switch_state(principal: PrincipalDep, db: DB) -> KillSwitchOut:
    sw = killswitch.active_switch(db, principal.org_id)
    return KillSwitchOut(
        engaged=killswitch.is_engaged(db, principal.org_id),
        platform=get_settings().platform_kill_switch,
        reason=sw.reason if sw else None,
        engaged_at=sw.engaged_at if sw else None,
        runs_terminated=sw.runs_terminated if sw else None,
    )


@router.post("/platform/kill-switch", response_model=KillSwitchOut)
def engage_kill_switch(payload: KillSwitchIn, principal: PrincipalDep, db: DB) -> KillSwitchOut:
    principal.require(Permission.KILL_SWITCH)
    killswitch.engage(
        db, org_id=principal.org_id, actor_user_id=principal.user.id, ip=principal.ip, reason=payload.reason
    )
    return kill_switch_state(principal, db)


@router.delete("/platform/kill-switch", response_model=KillSwitchOut)
def release_kill_switch(principal: PrincipalDep, db: DB) -> KillSwitchOut:
    principal.require(Permission.KILL_SWITCH)
    killswitch.release(db, org_id=principal.org_id, actor_user_id=principal.user.id, ip=principal.ip)
    return kill_switch_state(principal, db)


# ------------------------------------------------------------------ assistant
@router.get("/assistant/tools", response_model=list[ToolOut])
def list_tools(principal: PrincipalDep, db: DB) -> list[ToolOut]:
    scopes = entitlements.entitlements_for(db, principal.org_id).limits.get("tool_scopes", [])
    return [
        ToolOut(
            name=t.name,
            scope=t.scope,
            description=t.description,
            requires_approval=t.requires_approval,
            args_schema=t.args_model.model_json_schema(),
        )
        for t in allowed_tools(principal.role, scopes)
    ]


@router.get("/assistant/sessions", response_model=list[AssistantSessionOut])
def list_sessions(principal: PrincipalDep, db: DB) -> list[AssistantSessionOut]:
    rows = db.scalars(
        select(AssistantSession)
        .where(AssistantSession.organization_id == principal.org_id, AssistantSession.user_id == principal.user.id)
        .order_by(AssistantSession.created_at.desc())
        .limit(50)
    ).all()
    return [
        AssistantSessionOut(id=s.id, title=s.title, status=s.status, turns=s.turns, created_at=s.created_at)
        for s in rows
    ]


@router.post("/assistant/sessions", response_model=AssistantSessionOut, status_code=201)
def create_session(payload: AssistantSessionIn, principal: PrincipalDep, db: DB) -> AssistantSessionOut:
    principal.require(Permission.USE_ASSISTANT)
    s = assistant.start_session(db, principal.organization, _actor(principal), payload.title)
    return AssistantSessionOut(id=s.id, title=s.title, status=s.status, turns=s.turns, created_at=s.created_at)


@router.get("/assistant/sessions/{session_id}", response_model=AssistantSessionOut)
def get_session(session_id: uuid.UUID, principal: PrincipalDep, db: DB) -> AssistantSessionOut:
    s = assistant.get_session(db, principal.org_id, principal.user.id, session_id)
    return AssistantSessionOut.model_validate(s)


@router.post("/assistant/sessions/{session_id}/messages", response_model=AssistantMessageOut, status_code=201)
def post_message(
    session_id: uuid.UUID, payload: AssistantMessageIn, principal: PrincipalDep, db: DB
) -> AssistantMessageOut:
    principal.require(Permission.USE_ASSISTANT)
    s = assistant.get_session(db, principal.org_id, principal.user.id, session_id)
    msg = assistant.handle_message(
        db, org=principal.organization, actor=_actor(principal), session=s, text=payload.text
    )
    return AssistantMessageOut.model_validate(msg)


# -------------------------------------------------------------------- actions
def _visible_invocation(principal: Principal, inv: ToolInvocation) -> None:
    if inv.requested_by != principal.user.id and not has_permission(principal.role, Permission.MANAGE_WORKFLOWS):
        # another user's proposal: owners/admins only (404 inside the tenant would hide it; 403 is honest here)
        raise ForbiddenError("Only the requester or an owner/admin may act on this action", action="tool")


def _invocation_response(inv: ToolInvocation) -> dict[str, Any]:
    out = InvocationOut.model_validate(inv).model_dump(mode="json")
    full = getattr(inv, "_last_result", None)
    if isinstance(full, dict) and "content_b64" in full:
        out["download"] = {"filename": full.get("filename"), "content_b64": full["content_b64"]}
    return out


@router.get("/actions", response_model=list[InvocationOut])
def list_actions(
    principal: PrincipalDep, db: DB, status: str | None = Query(default=None, max_length=16)
) -> list[InvocationOut]:
    stmt = select(ToolInvocation).where(ToolInvocation.organization_id == principal.org_id)
    if not has_permission(principal.role, Permission.MANAGE_WORKFLOWS):
        stmt = stmt.where(ToolInvocation.requested_by == principal.user.id)
    status = clean_param(status)
    if status:
        stmt = stmt.where(ToolInvocation.status == status)
    rows = db.scalars(stmt.order_by(ToolInvocation.created_at.desc()).limit(200)).all()
    return [InvocationOut.model_validate(r) for r in rows]


@router.post("/actions", status_code=201)
def propose_action(payload: InvocationProposeIn, principal: PrincipalDep, db: DB) -> dict[str, Any]:
    principal.require(Permission.USE_ASSISTANT)
    inv = invocations.propose(
        db,
        org=principal.organization,
        actor=_actor(principal),
        tool_name=payload.tool,
        raw_args=payload.args,
        origin="manual",
        dry_run=payload.dry_run,
    )
    return _invocation_response(inv)


@router.post("/actions/{invocation_id}/approve")
def approve_action(invocation_id: uuid.UUID, payload: DecisionIn, principal: PrincipalDep, db: DB) -> dict[str, Any]:
    inv = invocations.get_invocation(db, principal.org_id, invocation_id)
    _visible_invocation(principal, inv)
    if inv.run_id is not None:
        raise ForbiddenError("Workflow steps are approved on the workflow run", action="tool")
    inv = invocations.decide(
        db, org=principal.organization, actor=_actor(principal), inv=inv, approve=True, reason=payload.reason
    )
    return _invocation_response(inv)


@router.post("/actions/{invocation_id}/reject", response_model=InvocationOut)
def reject_action(invocation_id: uuid.UUID, payload: DecisionIn, principal: PrincipalDep, db: DB) -> InvocationOut:
    inv = invocations.get_invocation(db, principal.org_id, invocation_id)
    _visible_invocation(principal, inv)
    if inv.run_id is not None:
        raise ForbiddenError("Workflow steps are decided on the workflow run", action="tool")
    inv = invocations.decide(
        db, org=principal.organization, actor=_actor(principal), inv=inv, approve=False, reason=payload.reason
    )
    return InvocationOut.model_validate(inv)


# ------------------------------------------------------------------ workflows
@router.get("/workflows", response_model=list[WorkflowOut])
def list_workflows(principal: PrincipalDep, db: DB) -> list[WorkflowOut]:
    principal.require(Permission.MANAGE_WORKFLOWS)
    rows = db.scalars(
        select(WorkflowDefinition)
        .where(WorkflowDefinition.organization_id == principal.org_id)
        .order_by(WorkflowDefinition.key)
    ).all()
    return [WorkflowOut.model_validate(d) for d in rows]


@router.post("/workflows/validate")
def validate_workflow(payload: WorkflowIn, principal: PrincipalDep, db: DB) -> dict[str, Any]:
    principal.require(Permission.MANAGE_WORKFLOWS)
    return workflows.validate_definition(db, principal.organization, principal.role, payload.definition)


@router.post("/workflows", response_model=WorkflowOut, status_code=201)
def save_workflow(payload: WorkflowIn, principal: PrincipalDep, db: DB) -> WorkflowOut:
    principal.require(Permission.MANAGE_WORKFLOWS)
    d, _v = workflows.create_or_update(
        db, org=principal.organization, actor=_actor(principal), key=payload.key, raw=payload.definition
    )
    db.refresh(d)
    return WorkflowOut.model_validate(d)


@router.post("/workflows/{definition_id}/activate", response_model=WorkflowOut)
def activate_workflow(
    definition_id: uuid.UUID, principal: PrincipalDep, db: DB, version_no: int = Query(ge=1)
) -> WorkflowOut:
    principal.require(Permission.MANAGE_WORKFLOWS)
    d = workflows.get_definition(db, principal.org_id, definition_id)
    return WorkflowOut.model_validate(
        workflows.activate(db, org=principal.organization, actor=_actor(principal), d=d, version_no=version_no)
    )


@router.post("/workflows/{definition_id}/disable", response_model=WorkflowOut)
def disable_workflow(definition_id: uuid.UUID, principal: PrincipalDep, db: DB) -> WorkflowOut:
    principal.require(Permission.MANAGE_WORKFLOWS)
    d = workflows.get_definition(db, principal.org_id, definition_id)
    return WorkflowOut.model_validate(workflows.disable(db, org=principal.organization, actor=_actor(principal), d=d))


@router.post("/workflows/{definition_id}/runs", response_model=RunOut, status_code=201)
def run_workflow(definition_id: uuid.UUID, payload: RunIn, principal: PrincipalDep, db: DB) -> RunOut:
    principal.require(Permission.MANAGE_WORKFLOWS)
    d = workflows.get_definition(db, principal.org_id, definition_id)
    run = workflows.start_run(
        db,
        org=principal.organization,
        actor=_actor(principal),
        d=d,
        mode=payload.mode,
        context=payload.context,
        version_no=payload.version_no,
    )
    return RunOut.model_validate(run)


@router.get("/workflow-runs", response_model=list[RunOut])
def list_runs(principal: PrincipalDep, db: DB, status: str | None = Query(default=None, max_length=20)) -> list[RunOut]:
    principal.require(Permission.MANAGE_WORKFLOWS)
    stmt = select(WorkflowRun).where(WorkflowRun.organization_id == principal.org_id)
    status = clean_param(status)
    if status:
        stmt = stmt.where(WorkflowRun.status == status)
    return [RunOut.model_validate(r) for r in db.scalars(stmt.order_by(WorkflowRun.created_at.desc()).limit(100)).all()]


@router.get("/workflow-runs/{run_id}", response_model=RunOut)
def get_run(run_id: uuid.UUID, principal: PrincipalDep, db: DB) -> RunOut:
    principal.require(Permission.MANAGE_WORKFLOWS)
    return RunOut.model_validate(workflows.get_run(db, principal.org_id, run_id))


@router.post("/workflow-runs/{run_id}/approve", response_model=RunOut)
def approve_run_step(run_id: uuid.UUID, payload: DecisionIn, principal: PrincipalDep, db: DB) -> RunOut:
    principal.require(Permission.MANAGE_WORKFLOWS)
    run = workflows.get_run(db, principal.org_id, run_id)
    return RunOut.model_validate(
        workflows.decide_step(
            db, org=principal.organization, actor=_actor(principal), run=run, approve=True, reason=payload.reason
        )
    )


@router.post("/workflow-runs/{run_id}/reject", response_model=RunOut)
def reject_run_step(run_id: uuid.UUID, payload: DecisionIn, principal: PrincipalDep, db: DB) -> RunOut:
    principal.require(Permission.MANAGE_WORKFLOWS)
    run = workflows.get_run(db, principal.org_id, run_id)
    return RunOut.model_validate(
        workflows.decide_step(
            db, org=principal.organization, actor=_actor(principal), run=run, approve=False, reason=payload.reason
        )
    )
