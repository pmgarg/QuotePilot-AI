"""Approval-gated tool invocations (S4-F03 / S4-A03): propose → (human approves) → execute, with policy checks at
*both* propose and execute time, dry-run that never touches state (S4-A09), kill-switch and plan checks."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ForbiddenError, NotFoundError, StateError
from app.core.logging import redact
from app.db.base import utcnow
from app.db.session import set_tenant
from app.models.org import Organization
from app.models.platform import ToolInvocation
from app.services import audit
from app.services.authz import has_permission
from app.services.platform import entitlements, killswitch
from app.services.platform.tools import REGISTRY, Actor, Tool, allowed_tools, validate_args


def get_invocation(db: Session, org_id: uuid.UUID, invocation_id: uuid.UUID) -> ToolInvocation:
    inv = db.scalar(
        select(ToolInvocation).where(ToolInvocation.id == invocation_id, ToolInvocation.organization_id == org_id)
    )
    if inv is None:
        raise NotFoundError("Invocation not found")
    return inv


def _policy_check(db: Session, org: Organization, actor: Actor, tool: Tool) -> None:
    killswitch.check(db, org.id)
    plan_scopes = entitlements.entitlements_for(db, org.id).limits.get("tool_scopes", [])
    if tool.name not in {t.name for t in allowed_tools(actor.role, plan_scopes)}:
        raise ForbiddenError(f"Tool '{tool.name}' is not available to this user/plan", action=tool.scope)


def _summary(result: dict[str, Any]) -> dict[str, Any]:
    """Store a redacted, bounded summary — never bulk content (export bytes stay in the HTTP response only)."""
    out = {k: v for k, v in result.items() if k != "content_b64"}
    encoded = json.dumps(out, default=str)
    summary: dict[str, Any] = json.loads(encoded) if len(encoded) <= 4000 else {"truncated": True}
    return dict(redact(summary))


def propose(
    db: Session,
    *,
    org: Organization,
    actor: Actor,
    tool_name: str,
    raw_args: Any,
    origin: str,
    dry_run: bool = False,
    session_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
) -> ToolInvocation:
    tool = REGISTRY.get(tool_name)
    if tool is None:
        raise ForbiddenError(f"Unknown tool '{tool_name[:40]}'", action="tool")
    args = validate_args(tool, raw_args)
    _policy_check(db, org, actor, tool)
    payload = args.model_dump(mode="json")
    inv = ToolInvocation(
        organization_id=org.id,
        tool=tool.name,
        scope=tool.scope,
        args=payload,
        args_hash=hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
        status="proposed",
        requires_approval=tool.requires_approval and not dry_run,
        dry_run=dry_run,
        origin=origin,
        session_id=session_id,
        run_id=run_id,
        requested_by=actor.user.id,
    )
    db.add(inv)
    db.flush()
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=actor.user.id,
        action="tool.propose",
        entity_type="tool_invocation",
        entity_id=inv.id,
        ip=actor.ip,
        summary={"tool": tool.name, "scope": tool.scope, "origin": origin, "dry_run": dry_run},
    )
    if dry_run:
        result = tool.handler(db, org, actor, args, True)
        inv.status = "would_execute"
        inv.result = _summary(result)
        db.flush()
        return inv
    if tool.requires_approval:
        return inv  # waits for a human
    return execute(db, org=org, actor=actor, inv=inv)


def execute(db: Session, *, org: Organization, actor: Actor, inv: ToolInvocation) -> ToolInvocation:
    tool = REGISTRY[inv.tool]
    if inv.status not in ("proposed", "approved"):
        raise StateError(f"Invocation is {inv.status}")
    if tool.requires_approval and inv.status != "approved":
        raise ForbiddenError("This action needs an approval before it can run", action=tool.scope)
    _policy_check(db, org, actor, tool)  # re-checked at execution time: plan/kill switch may have changed
    args = validate_args(tool, inv.args)
    try:
        if tool.self_commits:
            result = tool.handler(db, org, actor, args, False)
        else:
            with db.begin_nested():
                result = tool.handler(db, org, actor, args, False)
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:300]}"
        if tool.self_commits:
            # the service may have committed its own failure record and reset the transaction: start clean, reload
            inv_id, org_id, user_id = inv.id, org.id, actor.user.id  # read before rollback expires the instances
            db.rollback()
            set_tenant(db, org_id, user_id)
            reloaded = db.get(ToolInvocation, inv_id)
            if reloaded is None:
                raise
            inv = reloaded
        inv.status = "failed"
        inv.error = error
        inv.executed_at = utcnow()
        db.flush()
        audit.record(
            db,
            organization_id=org.id,
            actor_user_id=actor.user.id,
            action="tool.failed",
            entity_type="tool_invocation",
            entity_id=inv.id,
            ip=actor.ip,
            summary={"tool": tool.name, "error": inv.error},
        )
        if tool.self_commits:
            org_id, user_id = org.id, actor.user.id
            db.commit()  # keep the failure record; the caller still sees the error
            set_tenant(db, org_id, user_id)
        raise
    inv.status = "executed"
    inv.executed_at = utcnow()
    inv.result = _summary(result)
    inv.result["_full"] = None
    db.flush()
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=actor.user.id,
        action="tool.execute",
        entity_type="tool_invocation",
        entity_id=inv.id,
        ip=actor.ip,
        summary={"tool": tool.name, "scope": tool.scope},
    )
    inv._last_result = result  # type: ignore[attr-defined]  # transient, for the HTTP response only
    return inv


def decide(
    db: Session, *, org: Organization, actor: Actor, inv: ToolInvocation, approve: bool, reason: str | None
) -> ToolInvocation:
    if inv.status != "proposed" or not inv.requires_approval:
        raise StateError("Nothing to decide for this invocation")
    tool = REGISTRY[inv.tool]
    if not has_permission(actor.role, tool.permission):
        raise ForbiddenError("You may not approve this action", action=tool.scope)
    inv.decided_by = actor.user.id
    inv.decided_at = utcnow()
    inv.decision_reason = reason
    if not approve:
        inv.status = "rejected"
        db.flush()
        audit.record(
            db,
            organization_id=org.id,
            actor_user_id=actor.user.id,
            action="tool.reject",
            entity_type="tool_invocation",
            entity_id=inv.id,
            ip=actor.ip,
            summary={"tool": tool.name, "reason": reason},
        )
        return inv
    inv.status = "approved"
    db.flush()
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=actor.user.id,
        action="tool.approve",
        entity_type="tool_invocation",
        entity_id=inv.id,
        ip=actor.ip,
        summary={"tool": tool.name, "reason": reason},
    )
    return execute(db, org=org, actor=actor, inv=inv)
