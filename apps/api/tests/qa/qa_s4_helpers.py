"""Shared helpers for the independent Stage 4 QA tests (no product code is modified)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select, text, update

from app.db.session import get_sessionmaker, set_tenant
from app.models.audit import AuditEvent
from app.models.platform import Subscription, UsageCounter
from tests.conftest import AuthedClient, Tenant, login_as
from tests.integration.test_quotes import make_quote


def approved_quote(app, tenant: Tenant) -> tuple[AuthedClient, dict[str, Any]]:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant.users["sales"])
    approver = login_as(app, tenant.users["approver"])
    q = make_quote(sales, str(tenant.customer_id))
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).json()
    a = approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": s["lock_version"]})
    assert a.status_code == 200, a.text
    return sales, a.json()


def count_rows(org_id: uuid.UUID, model: Any, *where: Any) -> int:
    with get_sessionmaker()() as db:
        set_tenant(db, org_id, None)
        n = db.scalar(select(func.count()).select_from(model).where(model.organization_id == org_id, *where))
        db.rollback()
    return int(n or 0)


def audit_actions(org_id: uuid.UUID, prefix: str = "") -> list[str]:
    with get_sessionmaker()() as db:
        set_tenant(db, org_id, None)
        rows = db.scalars(
            select(AuditEvent.action).where(AuditEvent.organization_id == org_id, AuditEvent.action.like(f"{prefix}%"))
        ).all()
        db.rollback()
    return list(rows)


def set_plan(owner: AuthedClient, code: str) -> None:
    r = owner.post("/platform/plan", json={"plan_code": code})
    assert r.status_code == 200, r.text


def set_limit_override(org_id: uuid.UUID, key: str, value: Any) -> None:
    """Test-only: tweak a plan limit through the tenant session (as the app role, RLS applies)."""
    with get_sessionmaker()() as db:
        set_tenant(db, org_id, None)
        sub = db.scalar(select(Subscription).where(Subscription.organization_id == org_id))
        if sub is None:
            sub = Subscription(organization_id=org_id, plan_code="starter", status="active")
            db.add(sub)
            db.flush()
        sub.limit_overrides = {**(sub.limit_overrides or {}), key: value}
        db.commit()


def set_subscription_status(org_id: uuid.UUID, status: str) -> None:
    with get_sessionmaker()() as db:
        set_tenant(db, org_id, None)
        db.execute(update(Subscription).where(Subscription.organization_id == org_id).values(status=status))
        db.commit()


def set_counter(org_id: uuid.UUID, metric: str, period_key: str, n: int) -> None:
    with get_sessionmaker()() as db:
        set_tenant(db, org_id, None)
        db.execute(
            text(
                "INSERT INTO usage_counters (id, organization_id, metric, period_key, count, updated_at) "
                "VALUES (gen_random_uuid(), :o, :m, :k, :n, now()) "
                "ON CONFLICT (organization_id, metric, period_key) DO UPDATE SET count = :n"
            ),
            {"o": str(org_id), "m": metric, "k": period_key, "n": n},
        )
        db.commit()


def get_counter(org_id: uuid.UUID, metric: str) -> int:
    with get_sessionmaker()() as db:
        set_tenant(db, org_id, None)
        n = db.scalar(
            select(func.coalesce(func.sum(UsageCounter.count), 0)).where(
                UsageCounter.organization_id == org_id, UsageCounter.metric == metric
            )
        )
        db.rollback()
    return int(n or 0)


BOOKKEEPING = {
    "tool_invocations",
    "workflow_runs",
    "workflow_steps",
    "audit_events",
    "usage_counters",
    "ai_usage",
    "subscriptions",
    "workflow_definitions",
    "workflow_versions",
    "assistant_sessions",
    "assistant_messages",
}


def business_snapshot(org_id: uuid.UUID) -> dict[str, int]:
    """Row counts of every tenant table except Stage 4 bookkeeping (used to prove dry runs change nothing)."""
    out: dict[str, int] = {}
    with get_sessionmaker()() as db:
        set_tenant(db, org_id, None)
        tables = [
            t
            for (t,) in db.execute(
                text(
                    "SELECT table_name FROM information_schema.columns WHERE table_schema='public' "
                    "AND column_name='organization_id'"
                )
            )
        ]
        for t in sorted(tables):
            if t in BOOKKEEPING:
                continue
            sql = f"SELECT count(*) FROM {t} WHERE organization_id = :o"  # noqa: S608 - names from catalog
            out[t] = int(db.execute(text(sql), {"o": str(org_id)}).scalar() or 0)
        db.rollback()
    return out


WF_ORDER = {
    "name": "Quote to order",
    "trigger": "manual",
    "steps": [
        {"id": "read", "tool": "get_quote", "args": {"quote_number": "{{quote_number}}"}},
        {"id": "order", "tool": "propose_order", "args": {"quote_number": "{{quote_number}}"}},
    ],
}


def make_workflow(admin: AuthedClient, key: str, definition: dict[str, Any], activate: bool = True) -> dict[str, Any]:
    w = admin.post("/workflows", json={"key": key, "definition": definition})
    assert w.status_code == 201, w.text
    body = w.json()
    if activate:
        a = admin.post(f"/workflows/{body['id']}/activate?version_no={body['versions'][-1]['version_no']}", json={})
        assert a.status_code == 200, a.text
        body = a.json()
    return body


def run_wf(admin: AuthedClient, wid: str, mode: str, ctx: dict[str, Any] | None = None, **kw: Any):  # type: ignore[no-untyped-def]
    return admin.post(f"/workflows/{wid}/runs", json={"mode": mode, "context": ctx or {}, **kw})


def chat(ac: AuthedClient, text_: str, session_id: str | None = None):  # type: ignore[no-untyped-def]
    if session_id is None:
        session_id = ac.post("/assistant/sessions", json={"title": "qa"}).json()["id"]
    return ac.post(f"/assistant/sessions/{session_id}/messages", json={"text": text_})
