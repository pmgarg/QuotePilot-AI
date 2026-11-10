"""Plan entitlements enforced server-side (S4-F06 / S4-A06). The billing adapter is test-mode only: no money moves,
no third-party account exists; plan changes are recorded with a synthetic reference and audited."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import LimitExceeded, ValidationFailed
from app.db.base import utcnow
from app.models.org import Organization
from app.models.platform import Subscription, UsageCounter
from app.services import audit

PLANS: dict[str, dict[str, Any]] = {
    "starter": {
        "name": "Starter",
        "price_per_month": "0",
        "limits": {
            "seats": 10,
            "rfqs_per_month": 200,
            "ai_calls_per_day": 20,
            "connectors": 1,
            "active_workflows": 1,
            "workflow_runs_per_day": 20,
            "tool_scopes": ["read", "write:drafts", "write:orders"],
        },
    },
    "growth": {
        "name": "Growth",
        "price_per_month": "149",
        "limits": {
            "seats": 25,
            "rfqs_per_month": 1000,
            "ai_calls_per_day": 300,
            "connectors": 3,
            "active_workflows": 10,
            "workflow_runs_per_day": 500,
            "tool_scopes": ["read", "write:drafts", "write:orders", "send:email", "export"],
        },
    },
    "scale": {
        "name": "Scale",
        "price_per_month": "499",
        "limits": {
            "seats": 200,
            "rfqs_per_month": 20000,
            "ai_calls_per_day": 3000,
            "connectors": 10,
            "active_workflows": 50,
            "workflow_runs_per_day": 5000,
            "tool_scopes": ["read", "write:drafts", "write:orders", "send:email", "export", "write:retention"],
        },
    },
}

PERIODS = {
    "rfqs_per_month": "month",
    "ai_calls_per_day": "day",
    "workflow_runs_per_day": "day",
}


@dataclass
class Entitlements:
    plan_code: str
    plan_name: str
    status: str
    limits: dict[str, Any]
    billing_reference: str | None


def _period_key(metric: str, now: datetime) -> str:
    return now.strftime("%Y-%m") if PERIODS.get(metric) == "month" else now.strftime("%Y-%m-%d")


def subscription_for(db: Session, org_id: uuid.UUID) -> Subscription:
    sub = db.scalar(select(Subscription).where(Subscription.organization_id == org_id))
    if sub is None:
        sub = Subscription(organization_id=org_id, plan_code=get_settings().default_plan_code, status="active")
        db.add(sub)
        db.flush()
    return sub


def entitlements_for(db: Session, org_id: uuid.UUID) -> Entitlements:
    sub = subscription_for(db, org_id)
    plan = PLANS.get(sub.plan_code, PLANS["starter"])
    limits = {**plan["limits"], **(sub.limit_overrides or {})}
    return Entitlements(sub.plan_code, str(plan["name"]), sub.status, limits, sub.billing_reference)


def usage(db: Session, org_id: uuid.UUID, metric: str, now: datetime | None = None) -> int:
    now = now or utcnow()
    row = db.scalar(
        select(UsageCounter.count).where(
            UsageCounter.organization_id == org_id,
            UsageCounter.metric == metric,
            UsageCounter.period_key == _period_key(metric, now),
        )
    )
    return int(row or 0)


def consume(db: Session, org_id: uuid.UUID, metric: str, amount: int = 1, now: datetime | None = None) -> int:
    """Atomically increment a metered counter after checking the plan limit; raises LimitExceeded (429)."""
    now = now or utcnow()
    ent = entitlements_for(db, org_id)
    if ent.status != "active":
        raise LimitExceeded(f"Subscription is {ent.status}; metered features are paused", metric=metric)
    limit = int(ent.limits.get(metric, 0))
    key = _period_key(metric, now)
    row = db.execute(
        text(
            "INSERT INTO usage_counters (id, organization_id, metric, period_key, count, updated_at) "
            "VALUES (gen_random_uuid(), :org, :metric, :key, :n, now()) "
            "ON CONFLICT (organization_id, metric, period_key) DO UPDATE SET count = usage_counters.count + :n, "
            "updated_at = now() RETURNING count"
        ),
        {"org": str(org_id), "metric": metric, "key": key, "n": amount},
    ).scalar_one()
    if int(row) > limit:
        db.execute(
            text(
                "UPDATE usage_counters SET count = count - :n WHERE organization_id = :org AND metric = :metric "
                "AND period_key = :key"
            ),
            {"org": str(org_id), "metric": metric, "key": key, "n": amount},
        )
        raise LimitExceeded(
            f"Plan limit reached for {metric} ({limit} per {PERIODS.get(metric, 'period')})", metric=metric, limit=limit
        )
    return int(row)


def require_capacity(db: Session, org_id: uuid.UUID, metric: str, current: int) -> None:
    """For non-metered limits (seats, connectors, active workflows): `current` is the count that would exist."""
    ent = entitlements_for(db, org_id)
    limit = int(ent.limits.get(metric, 0))
    if current > limit:
        raise LimitExceeded(f"Plan limit reached for {metric} ({limit})", metric=metric, limit=limit)


def scope_allowed(db: Session, org_id: uuid.UUID, scope: str) -> bool:
    allowed = entitlements_for(db, org_id).limits.get("tool_scopes", [])
    return scope in allowed or (scope.startswith("read") and "read" in allowed)


class TestModeBillingAdapter:
    """No network, no money: records a plan change with a synthetic reference. A real adapter must implement the
    same two methods; it is never selected unless explicitly configured by a human."""

    name = "test_mode"

    def change_plan(self, org: Organization, plan_code: str) -> str:
        return f"test_{plan_code}_{uuid.uuid4().hex[:12]}"

    def invoice_preview(self, plan_code: str) -> dict[str, Any]:
        plan = PLANS[plan_code]
        return {"plan": plan_code, "amount": plan["price_per_month"], "currency": "USD", "mode": "test"}


def change_plan(
    db: Session, *, org: Organization, actor_user_id: uuid.UUID, ip: str | None, plan_code: str
) -> Entitlements:
    if plan_code not in PLANS:
        raise ValidationFailed("Unknown plan", field="plan_code")
    sub = subscription_for(db, org.id)
    previous = sub.plan_code
    sub.plan_code = plan_code
    sub.status = "active"
    sub.billing_reference = TestModeBillingAdapter().change_plan(org, plan_code)
    sub.changed_by = actor_user_id
    db.flush()
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=actor_user_id,
        action="billing.plan_changed",
        entity_type="subscription",
        entity_id=sub.id,
        ip=ip,
        summary={"from": previous, "to": plan_code, "adapter": "test_mode", "reference": sub.billing_reference},
    )
    return entitlements_for(db, org.id)
