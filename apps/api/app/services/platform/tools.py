"""Tool registry (ADR-013 / S4-F02): the only actions the assistant or a workflow can take. Every tool declares a
scope, the permission the acting user needs, a strict argument schema and whether a human approval checkpoint is
required before execution (S4-F03). Handlers call the ordinary service layer, so tenant scoping, RLS, audit and the
existing approval gates (quote approval, order confirmation, send gate) all still apply underneath. No tool runs
SQL, shell or arbitrary HTTP; no tool is ever added from a prompt."""

from __future__ import annotations

import base64
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError, ValidationFailed
from app.db.base import utcnow
from app.models.customer import Customer
from app.models.followup import CommunicationDraft
from app.models.org import Organization, User
from app.models.quote import Quote
from app.services import quotes as quote_service
from app.services.authz import Permission, has_permission
from app.services.followup import service as followup_service
from app.services.intelligence import data_io, kpis, retention
from app.services.intelligence import orders as order_service
from app.services.lifecycle import effective_lifecycle_status


@dataclass
class Actor:
    user: User
    role: str
    ip: str | None


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", str_max_length=500)


class SearchQuotesArgs(_Args):
    query: str = Field(default="", max_length=100)
    status: str | None = Field(default=None, max_length=20)
    limit: int = Field(default=10, ge=1, le=50)


class QuoteRefArgs(_Args):
    quote_number: str | None = Field(default=None, max_length=32)
    quote_id: uuid.UUID | None = None


class CustomerArgs(_Args):
    customer_name: str | None = Field(default=None, max_length=200)
    customer_id: uuid.UUID | None = None


class KpiArgs(_Args):
    days: int = Field(default=90, ge=1, le=1095)


class DraftArgs(QuoteRefArgs):
    template_key: str | None = Field(default=None, max_length=64)


class SendDraftArgs(_Args):
    draft_id: uuid.UUID


class DeletionArgs(CustomerArgs):
    reason: str = Field(min_length=3, max_length=2000)


class NoArgs(_Args):
    pass


def _resolve_quote(db: Session, org: Organization, args: QuoteRefArgs) -> Quote:
    if args.quote_id is not None:
        return quote_service.get_quote(db, org.id, args.quote_id)
    if args.quote_number:
        q = db.scalar(
            select(Quote).where(Quote.organization_id == org.id, Quote.number == args.quote_number.strip().upper())
        )
        if q is not None:
            return q
    raise NotFoundError("Quote not found")


def _resolve_customer(db: Session, org: Organization, args: CustomerArgs) -> Customer:
    stmt = select(Customer).where(Customer.organization_id == org.id, Customer.deleted_at.is_(None))
    if args.customer_id is not None:
        c = db.scalar(stmt.where(Customer.id == args.customer_id))
    elif args.customer_name:
        like = f"%{args.customer_name.replace('%', r'\%').replace('_', r'\_')}%"
        c = db.scalar(
            stmt.where(or_(Customer.name.ilike(like, escape="\\"), Customer.reference.ilike(like, escape="\\"))).limit(
                1
            )
        )
    else:
        c = None
    if c is None:
        raise NotFoundError("Customer not found")
    return c


def _quote_summary(q: Quote) -> dict[str, Any]:
    v = quote_service.current_version(q)
    return {
        "quote_id": str(q.id),
        "number": q.number,
        "status": q.status,
        "lifecycle": effective_lifecycle_status(q),
        "currency": q.currency,
        "grand_total": str(v.grand_total),
        "version_no": q.current_version_no,
        "valid_until": q.valid_until.isoformat() if q.valid_until else None,
    }


# ------------------------------------------------------------------ handlers
def _search_quotes(db: Session, org: Organization, actor: Actor, a: SearchQuotesArgs, dry_run: bool) -> dict[str, Any]:
    stmt = select(Quote).where(Quote.organization_id == org.id)
    if a.status:
        stmt = stmt.where(Quote.status == a.status)
    if a.query:
        like = f"%{a.query.replace('%', r'\%').replace('_', r'\_')}%"
        stmt = stmt.where(Quote.number.ilike(like, escape="\\"))
    rows = db.scalars(stmt.order_by(Quote.created_at.desc()).limit(a.limit)).all()
    return {"quotes": [_quote_summary(q) for q in rows], "count": len(rows)}


def _get_quote(db: Session, org: Organization, actor: Actor, a: QuoteRefArgs, dry_run: bool) -> dict[str, Any]:
    q = _resolve_quote(db, org, a)
    v = quote_service.current_version(q)
    lines = [
        {"line_no": ln.line_no, "sku": ln.sku, "description": ln.description[:120], "quantity": str(ln.quantity)}
        for ln in v.lines
    ]
    out = _quote_summary(q)
    out["lines"] = lines
    out["blockers"] = [b.as_dict() for b in quote_service.compute_blockers(q, v, org, reveal_margin=False, db=db)]
    return out


def _customer_profile(db: Session, org: Organization, actor: Actor, a: CustomerArgs, dry_run: bool) -> dict[str, Any]:
    c = _resolve_customer(db, org, a)
    quotes = db.scalars(
        select(Quote).where(Quote.customer_id == c.id).order_by(Quote.created_at.desc()).limit(10)
    ).all()
    return {
        "customer_id": str(c.id),
        "name": c.name,
        "reference": c.reference,
        "default_currency": c.default_currency,
        "recent_quotes": [_quote_summary(q) for q in quotes],
    }


def _kpis(db: Session, org: Organization, actor: Actor, a: KpiArgs, dry_run: bool) -> dict[str, Any]:
    today = utcnow().date()
    data = kpis.compute(db, org, kpis.Range(today - timedelta(days=a.days), today), today=today)
    return {k: v for k, v in data.items() if k != "definitions"}


def _draft_follow_up(db: Session, org: Organization, actor: Actor, a: DraftArgs, dry_run: bool) -> dict[str, Any]:
    q = _resolve_quote(db, org, a)
    if dry_run:
        return {"would": "create a follow-up draft", "quote": q.number, "template_key": a.template_key or "follow_up_1"}
    draft = followup_service.create_draft(
        db,
        org=org,
        quote=q,
        actor=actor.user,
        ip=actor.ip,
        contact_id=None,
        template_key=a.template_key,
        reasons=["created by the assistant on request; nothing is sent until a human sends it"],
    )
    return {"draft_id": str(draft.id), "status": draft.status, "subject": draft.subject[:120], "quote": q.number}


def _send_follow_up(db: Session, org: Organization, actor: Actor, a: SendDraftArgs, dry_run: bool) -> dict[str, Any]:
    draft = db.scalar(
        select(CommunicationDraft).where(
            CommunicationDraft.id == a.draft_id, CommunicationDraft.organization_id == org.id
        )
    )
    if draft is None:
        raise NotFoundError("Draft not found")
    if dry_run:
        return {"would": "send draft through the single send gate", "draft_id": str(draft.id), "to": "[EMAIL]"}
    sent = followup_service.send_draft(db, org=org, draft=draft, actor=actor.user, ip=actor.ip, mode="manual")
    return {"draft_id": str(sent.id), "status": sent.status, "provider": sent.provider}


def _propose_order(db: Session, org: Organization, actor: Actor, a: QuoteRefArgs, dry_run: bool) -> dict[str, Any]:
    q = _resolve_quote(db, org, a)
    if dry_run:
        return {"would": "create a *proposed* order (still needs confirmation)", "quote": q.number}
    order = order_service.propose_from_quote(db, org=org, quote=q, actor_user_id=actor.user.id, ip=actor.ip)
    return {"order_id": str(order.id), "number": order.number, "status": order.status}


def _request_deletion(db: Session, org: Organization, actor: Actor, a: DeletionArgs, dry_run: bool) -> dict[str, Any]:
    c = _resolve_customer(db, org, a)
    if dry_run:
        return {"would": "open a deletion request (owner/admin approval required)", "customer_id": str(c.id)}
    req = retention.request_deletion(
        db, org=org, actor_user_id=actor.user.id, ip=actor.ip, customer_id=c.id, reason=a.reason
    )
    return {"request_id": str(req.id), "status": req.status}


def _export_customers(db: Session, org: Organization, actor: Actor, a: NoArgs, dry_run: bool) -> dict[str, Any]:
    if dry_run:
        return {"would": "produce the customers CSV (formula-safe)"}
    data = data_io.export_customers(db, org.id)
    return {
        "filename": f"customers-{utcnow().date().isoformat()}.csv",
        "bytes": len(data),
        "content_b64": base64.b64encode(data).decode(),
    }


Handler = Callable[[Session, Organization, Actor, Any, bool], dict[str, Any]]


@dataclass(frozen=True)
class Tool:
    name: str
    scope: str  # read | write:drafts | write:orders | send:email | export | write:retention
    description: str
    args_model: type[_Args]
    permission: Permission
    requires_approval: bool
    handler: Handler
    side_effect: bool
    self_commits: bool = False  # the underlying service commits its own failure records (send gate)


REGISTRY: dict[str, Tool] = {
    t.name: t
    for t in (
        Tool(
            "search_quotes",
            "read",
            "Find quotes by number/status",
            SearchQuotesArgs,
            Permission.VIEW,
            False,
            _search_quotes,
            False,
        ),
        Tool(
            "get_quote",
            "read",
            "Read one quote with lines and blockers",
            QuoteRefArgs,
            Permission.VIEW,
            False,
            _get_quote,
            False,
        ),
        Tool(
            "get_customer_profile",
            "read",
            "Customer profile and recent quotes",
            CustomerArgs,
            Permission.VIEW,
            False,
            _customer_profile,
            False,
        ),
        Tool("kpis_summary", "read", "KPI summary for the last N days", KpiArgs, Permission.VIEW, False, _kpis, False),
        Tool(
            "draft_follow_up",
            "write:drafts",
            "Create a follow-up draft (never sent by itself)",
            DraftArgs,
            Permission.EDIT_QUOTES,
            False,
            _draft_follow_up,
            True,
        ),
        Tool(
            "send_follow_up",
            "send:email",
            "Send an existing draft through the send gate",
            SendDraftArgs,
            Permission.EDIT_QUOTES,
            True,
            _send_follow_up,
            True,
            True,
        ),
        Tool(
            "propose_order",
            "write:orders",
            "Convert an approved quote into a proposed order",
            QuoteRefArgs,
            Permission.EDIT_QUOTES,
            True,
            _propose_order,
            True,
        ),
        Tool(
            "request_customer_deletion",
            "write:retention",
            "Open a retention/deletion request",
            DeletionArgs,
            Permission.EDIT_QUOTES,
            True,
            _request_deletion,
            True,
        ),
        Tool(
            "export_customers",
            "export",
            "Bulk CSV export of customers",
            NoArgs,
            Permission.EXPORT_DATA,
            True,
            _export_customers,
            True,
        ),
    )
}


def allowed_tools(role: str, plan_scopes: list[str]) -> list[Tool]:
    """Backend policy: the acting user's permission AND the organisation's plan scopes, never the prompt."""
    out = []
    for t in REGISTRY.values():
        if not has_permission(role, t.permission):
            continue
        if t.scope != "read" and t.scope not in plan_scopes:
            continue
        if t.scope == "read" and "read" not in plan_scopes:
            continue
        out.append(t)
    return out


def validate_args(tool: Tool, raw: Any) -> _Args:
    if not isinstance(raw, dict):
        raise ValidationFailed("Tool arguments must be an object", field="args")
    try:
        return tool.args_model.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError → 422 with no internals
        raise ValidationFailed(f"Invalid arguments for {tool.name}: {str(exc)[:300]}", field="args") from exc
