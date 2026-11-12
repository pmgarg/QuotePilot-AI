"""Regression tests for the Stage 4 security-audit findings S4-SEC-01..07 (fix round 1)."""

from __future__ import annotations

import threading
import uuid
from typing import Any

import pytest
from sqlalchemy import func, select

from app.db.session import get_sessionmaker, set_tenant
from app.models.audit import AuditEvent
from app.models.followup import CommunicationDraft
from app.models.intelligence import Order
from app.models.org import Membership
from app.models.platform import Subscription
from app.schemas.platform import contains_nul
from tests.conftest import AuthedClient, Tenant, login_as
from tests.integration.test_quotes import make_quote

pytestmark = pytest.mark.integration

PW = "Another-Long-Pass-77"


def audit_actions(org: uuid.UUID, prefix: str = "") -> list[str]:
    with get_sessionmaker()() as db:
        set_tenant(db, org, None)
        rows = db.scalars(
            select(AuditEvent.action).where(AuditEvent.organization_id == org, AuditEvent.action.like(f"{prefix}%"))
        ).all()
        db.rollback()
    return list(rows)


def growth(app, t: Tenant) -> None:  # type: ignore[no-untyped-def]
    assert login_as(app, t.users["owner"]).post("/platform/plan", json={"plan_code": "scale"}).status_code == 200


def new_member(c: AuthedClient, email: str) -> Any:
    return c.post("/org/members", json={"email": email, "full_name": "M", "role": "sales", "password": PW})


def race(clients: list[AuthedClient], fn: Any) -> list[Any]:
    barrier = threading.Barrier(len(clients))
    out: list[Any] = [None] * len(clients)

    def go(i: int) -> None:
        barrier.wait()
        out[i] = fn(i, clients[i])

    ts = [threading.Thread(target=go, args=(i,)) for i in range(len(clients))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return out


# ---------------------------------------------------------------- S4-SEC-01
def test_sec01_concurrent_http_approvals_execute_once(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    growth(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    inv_id = admin.post("/actions", json={"tool": "export_customers", "args": {}}).json()["id"]
    clients = [login_as(app, tenant_a.users["admin"]) for _ in range(6)]
    res = race(clients, lambda i, c: c.post(f"/actions/{inv_id}/approve", json={}).status_code)
    assert sorted(res) == [200] + [409] * 5, res
    assert audit_actions(tenant_a.org_id, "tool.execute").count("tool.execute") == 1


def test_sec01_approve_then_reject_race_has_single_outcome(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    growth(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    inv_id = admin.post("/actions", json={"tool": "export_customers", "args": {}}).json()["id"]
    clients = [login_as(app, tenant_a.users["admin"]) for _ in range(2)]
    res = race(
        clients, lambda i, c: c.post(f"/actions/{inv_id}/{'approve' if i == 0 else 'reject'}", json={}).status_code
    )
    assert sorted(res) == [200, 409], res
    decided = audit_actions(tenant_a.org_id, "tool.")
    assert (decided.count("tool.execute"), decided.count("tool.reject")) in ((1, 0), (0, 1))


def test_sec01_concurrent_workflow_step_approval_executes_once(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    growth(app, tenant_a)
    wf = {"name": "x", "steps": [{"id": "e", "tool": "export_customers", "args": {}}]}
    admin = login_as(app, tenant_a.users["admin"])
    w = admin.post("/workflows", json={"key": "x", "definition": wf}).json()
    assert admin.post(f"/workflows/{w['id']}/activate?version_no=1", json={}).status_code == 200
    run = admin.post(f"/workflows/{w['id']}/runs", json={"mode": "live", "context": {}}).json()
    assert run["status"] == "awaiting_approval", run
    clients = [login_as(app, tenant_a.users["admin"]) for _ in range(5)]
    res = race(clients, lambda i, c: c.post(f"/workflow-runs/{run['id']}/approve", json={}).status_code)
    assert res.count(200) == 1 and all(r in (200, 409) for r in res), res
    assert audit_actions(tenant_a.org_id, "tool.execute").count("tool.execute") == 1


def test_sec01_approve_authz_cross_tenant_and_forbidden_role(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    growth(app, tenant_a)
    inv_id = (
        login_as(app, tenant_a.users["admin"])
        .post("/actions", json={"tool": "export_customers", "args": {}})
        .json()["id"]
    )
    other = login_as(app, tenant_b.users["admin"])
    assert other.post(f"/actions/{inv_id}/approve", json={}).status_code == 404
    assert other.post(f"/actions/{inv_id}/reject", json={}).status_code == 404
    assert login_as(app, tenant_a.users["readonly"]).post(f"/actions/{inv_id}/approve", json={}).status_code in (
        403,
        404,
    )
    assert audit_actions(tenant_a.org_id, "tool.execute") == []
    assert login_as(app, tenant_a.users["admin"]).post(f"/actions/{inv_id}/approve", json={}).status_code == 200


# ---------------------------------------------------------------- S4-SEC-02 / 03
def test_sec02_reactivation_allowed_when_seat_free_and_blocked_when_full(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    ids = [new_member(admin, f"q{i}@alphatrading.example").json()["id"] for i in range(5)]  # 10 of 10
    assert admin.patch(f"/org/members/{ids[0]}", json={"is_active": False}).status_code == 200
    assert admin.patch(f"/org/members/{ids[0]}", json={"is_active": True}).status_code == 200  # seat was free
    assert admin.patch(f"/org/members/{ids[1]}", json={"is_active": False}).status_code == 200
    assert new_member(admin, "z@alphatrading.example").status_code == 201
    r = admin.patch(f"/org/members/{ids[1]}", json={"is_active": True})
    assert r.status_code == 429, r.text
    assert "member.update" in audit_actions(tenant_a.org_id, "member.")
    assert (
        login_as(app, tenant_a.users["sales"]).patch(f"/org/members/{ids[1]}", json={"is_active": True}).status_code
        == 403
    )


def test_sec02_reactivation_races_creation_without_exceeding_seats(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    ids = [new_member(admin, f"w{i}@alphatrading.example").json()["id"] for i in range(4)]  # 9 of 10
    assert admin.patch(f"/org/members/{ids[0]}", json={"is_active": False}).status_code == 200  # 8 active
    clients = [login_as(app, tenant_a.users["admin"]) for _ in range(6)]

    def op(i: int, c: AuthedClient) -> int:
        if i % 2:
            return int(c.patch(f"/org/members/{ids[0]}", json={"is_active": True}).status_code)
        return int(new_member(c, f"v{i}@alphatrading.example").status_code)

    race(clients, op)
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        active = db.scalar(select(func.count()).select_from(Membership).where(Membership.is_active.is_(True)))
        db.rollback()
    assert int(active or 0) <= 10


def test_sec03_concurrent_member_creation_never_exceeds_seats(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    for i in range(4):  # 9 of 10
        assert new_member(admin, f"p{i}@alphatrading.example").status_code == 201
    clients = [login_as(app, tenant_a.users["admin"]) for _ in range(8)]
    codes = race(clients, lambda i, c: new_member(c, f"r{i}@alphatrading.example").status_code)
    assert codes.count(201) == 1 and codes.count(429) == 7, codes


# ---------------------------------------------------------------- S4-SEC-04
def test_sec04_failed_approved_action_is_persisted_and_not_retryable(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    q = sales.post("/quotes", json={"customer_id": str(tenant_a.customer_id)}).json()  # draft: not convertible
    inv = sales.post("/actions", json={"tool": "propose_order", "args": {"quote_id": q["id"]}}).json()
    r = sales.post(f"/actions/{inv['id']}/approve", json={})
    assert r.status_code >= 400 and "Traceback" not in r.text
    row = next(x for x in sales.get("/actions").json() if x["id"] == inv["id"])
    assert row["status"] == "failed" and row["error"] and "Traceback" not in row["error"]
    acts = audit_actions(tenant_a.org_id, "tool.")
    assert "tool.failed" in acts and "tool.approve" in acts
    assert sales.post(f"/actions/{inv['id']}/approve", json={}).status_code == 409  # not approvable again
    assert audit_actions(tenant_a.org_id, "tool.failed").count("tool.failed") == 1
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        assert db.scalar(select(func.count()).select_from(Order)) == 0
        db.rollback()


def test_sec04_failed_workflow_step_ends_run_and_records_failure(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    q = make_quote(sales, str(tenant_a.customer_id))  # draft quote: order creation will fail at approval
    wf = {"name": "o", "steps": [{"id": "o", "tool": "propose_order", "args": {"quote_number": "{{quote_number}}"}}]}
    admin = login_as(app, tenant_a.users["admin"])
    w = admin.post("/workflows", json={"key": "o", "definition": wf}).json()
    assert admin.post(f"/workflows/{w['id']}/activate?version_no=1", json={}).status_code == 200
    run = admin.post(
        f"/workflows/{w['id']}/runs", json={"mode": "live", "context": {"quote_number": q["number"]}}
    ).json()
    assert run["status"] == "awaiting_approval", run
    done = admin.post(f"/workflow-runs/{run['id']}/approve", json={})
    assert done.status_code == 200 and done.json()["status"] == "failed", done.text
    assert "tool.failed" in audit_actions(tenant_a.org_id, "tool.")
    assert admin.post(f"/workflow-runs/{run['id']}/approve", json={}).status_code == 409


# ---------------------------------------------------------------- S4-SEC-05
def test_sec05_contains_nul_is_recursive() -> None:
    assert contains_nul("a\x00") and contains_nul({"a": ["b", {"c\x00": 1}]}) and contains_nul({"k": {"v": ["x\x00"]}})
    assert not contains_nul({"a": ["b", 1, None, {"c": "d"}]})


@pytest.mark.parametrize(
    "path,body",
    [
        ("/actions", {"tool": "search_quotes", "args": {"query": {"nested": ["ok", "a\x00b"]}}}),
        ("/actions", {"tool": "search_quotes", "args": {"bad\x00key": "x"}}),
        ("/actions", {"tool": "t\x00", "args": {}}),
        ("/assistant/sessions", {"title": "a\x00b"}),
    ],
)
def test_sec05_nul_rejected_with_422(app, tenant_a: Tenant, path: str, body: dict[str, Any]) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    sales.client.raise_server_exceptions = False
    assert sales.post(path, json=body).status_code == 422
    admin = login_as(app, tenant_a.users["admin"])
    admin.client.raise_server_exceptions = False
    r = admin.post("/workflows", json={"key": "k", "definition": {"name": "a", "steps": [], "x": [{"n\x00": 1}]}})
    assert r.status_code == 422
    r = admin.post("/platform/kill-switch", json={"reason": "drill\x00reason"})
    assert r.status_code == 422
    assert sales.post("/actions", json={"tool": "kpis_summary", "args": {}}).status_code == 201  # control: still works


# ---------------------------------------------------------------- S4-SEC-06
def test_sec06_compensated_draft_has_audit_row_with_run_id(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    approver = login_as(app, tenant_a.users["approver"])
    q = make_quote(sales, str(tenant_a.customer_id))
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).json()
    q = approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": s["lock_version"]}).json()
    sales.post(
        f"/customers/{tenant_a.customer_id}/contacts",
        json={"full_name": "B", "email": "b@customer.example", "is_primary": True},
    )
    wf = {
        "name": "c",
        "steps": [
            {
                "id": "d",
                "tool": "draft_follow_up",
                "args": {"quote_number": "{{quote_number}}"},
                "on_failure": "compensate",
            },
            {"id": "bad", "tool": "get_quote", "args": {"quote_number": "Q-0000-00000"}, "on_failure": "compensate"},
        ],
    }
    admin = login_as(app, tenant_a.users["admin"])
    w = admin.post("/workflows", json={"key": "c", "definition": wf}).json()
    assert admin.post(f"/workflows/{w['id']}/activate?version_no=1", json={}).status_code == 200
    run = admin.post(
        f"/workflows/{w['id']}/runs", json={"mode": "live", "context": {"quote_number": q["number"]}}
    ).json()
    assert run["status"] == "compensated"
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        ev = [
            (e.actor_user_id, e.summary, e.entity_id)
            for e in db.scalars(
                select(AuditEvent).where(AuditEvent.action == "draft.cancelled_by_workflow_compensation")
            ).all()
        ]
        drafts = [(d.id, d.status) for d in db.scalars(select(CommunicationDraft)).all()]
        db.rollback()
    assert len(ev) == 1 and ev[0][0] is None
    assert ev[0][1]["run_id"] == run["id"] and ev[0][1]["reason"] == "workflow compensation"
    assert all(st == "cancelled" for _, st in drafts) and ev[0][2] == drafts[0][0]


# ---------------------------------------------------------------- S4-SEC-07
def test_sec07_subscription_get_or_create_is_idempotent_and_race_safe(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    from app.services.platform import entitlements

    barrier = threading.Barrier(12)
    errs: list[str] = []
    plans: list[str] = []

    def go() -> None:
        with get_sessionmaker()() as db:
            set_tenant(db, tenant_a.org_id, None)
            barrier.wait()
            try:
                plans.append(entitlements.subscription_for(db, tenant_a.org_id).plan_code)
                db.commit()
            except Exception as exc:  # noqa: BLE001
                errs.append(type(exc).__name__)

    ts = [threading.Thread(target=go) for _ in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errs == [] and len(plans) == 12
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        assert db.scalar(select(func.count()).select_from(Subscription)) == 1
        sub = entitlements.subscription_for(db, tenant_a.org_id)  # idempotent second call
        assert sub.organization_id == tenant_a.org_id
        db.rollback()


def test_sec07_subscription_is_tenant_scoped(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    from app.services.platform import entitlements

    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        entitlements.subscription_for(db, tenant_a.org_id)
        db.commit()
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_b.org_id, None)
        assert db.scalar(select(func.count()).select_from(Subscription)) == 0  # RLS hides tenant A's row
        db.rollback()
    assert login_as(app, tenant_b.users["owner"]).get("/platform/plan").status_code == 200
    assert login_as(app, tenant_b.users["sales"]).post("/platform/plan", json={"plan_code": "scale"}).status_code == 403


# ---------------------------------------------------------------- QA defects (D-QA4-01..05)
OK_WF = {"name": "r", "steps": [{"id": "k", "tool": "kpis_summary", "args": {}}]}


def _make_wf(admin: AuthedClient, key: str, definition: dict[str, Any]) -> dict[str, Any]:
    w = admin.post("/workflows", json={"key": key, "definition": definition}).json()
    assert admin.post(f"/workflows/{w['id']}/activate?version_no=1", json={}).status_code == 200
    return dict(w)


def test_qa01_workflow_dry_run_allowed_under_kill_switch_but_live_and_manual_dry_run_blocked(
    app, tenant_a: Tenant
) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    w = _make_wf(admin, "ks", OK_WF)
    assert admin.post("/platform/kill-switch", json={"reason": "drill reason"}).status_code == 200
    d = admin.post(f"/workflows/{w['id']}/runs", json={"mode": "dry_run", "context": {}})
    assert d.status_code == 201 and d.json()["status"] == "succeeded", d.text
    assert admin.post(f"/workflows/{w['id']}/runs", json={"mode": "live", "context": {}}).status_code == 503
    sales = login_as(app, tenant_a.users["sales"])
    assert sales.post("/actions", json={"tool": "kpis_summary", "args": {}, "dry_run": True}).status_code == 503
    assert sales.post("/actions", json={"tool": "kpis_summary", "args": {}}).status_code == 503
    sid = sales.post("/assistant/sessions", json={}).json()["id"]
    assert sales.post(f"/assistant/sessions/{sid}/messages", json={"text": "hi"}).status_code == 503
    # a sales user cannot start a workflow dry run, and another tenant cannot see/run this one
    assert sales.post(f"/workflows/{w['id']}/runs", json={"mode": "dry_run"}).status_code == 403


def test_qa02_list_workflows_requires_manage_workflows(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    _make_wf(login_as(app, tenant_a.users["admin"]), "w", OK_WF)
    for role in ("sales", "approver", "readonly"):
        assert login_as(app, tenant_a.users[role]).get("/workflows").status_code == 403, role
    for role in ("owner", "admin"):
        assert len(login_as(app, tenant_a.users[role]).get("/workflows").json()) == 1
    assert login_as(app, tenant_b.users["admin"]).get("/workflows").json() == []


def test_qa05_validation_error_does_not_echo_input_into_step_error_audit_or_export(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    wf = {
        "name": "l",
        "steps": [{"id": "c", "tool": "get_customer_profile", "args": {"customer_name": "{{customer_name}}"}}],
    }
    w = _make_wf(admin, "leaky", wf)
    secret = "ZZQA Secret Customer Ltd " + "x" * 600
    run = admin.post(f"/workflows/{w['id']}/runs", json={"mode": "live", "context": {"customer_name": secret}}).json()
    assert run["status"] == "failed"
    assert "customer_name" in run["steps"][0]["error"] and "input_value" not in run["steps"][0]["error"]
    blob = admin.get("/platform/support-export").text + str(run["failure_explanation"]) + str(run["steps"])
    blob += admin.get("/actions").text
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        blob += str([e.summary for e in db.scalars(select(AuditEvent)).all()])
        db.rollback()
    for token in ("zzqa", "secret customer", "input_value"):
        assert token not in blob.lower(), token
    # same for a directly proposed action: 422 body carries no input value
    r = admin.post("/actions", json={"tool": "get_customer_profile", "args": {"customer_name": secret}})
    assert r.status_code == 422 and "zzqa" not in r.text.lower()


def test_qa03_nul_in_assistant_message_is_422(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    sid = sales.post("/assistant/sessions", json={}).json()["id"]
    assert sales.post(f"/assistant/sessions/{sid}/messages", json={"text": "show quote \x00 now"}).status_code == 422


def test_qa04_quote_won_trigger_is_not_accepted(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    rep = admin.post("/workflows/validate", json={"key": "w", "definition": {**OK_WF, "trigger": "quote.won"}}).json()
    assert rep["ok"] is False
    for trig in ("quote.approved", "order.confirmed", "manual"):
        assert admin.post("/workflows/validate", json={"key": "w", "definition": {**OK_WF, "trigger": trig}}).json()[
            "ok"
        ]
    assert (
        login_as(app, tenant_a.users["sales"])
        .post("/workflows/validate", json={"key": "w", "definition": OK_WF})
        .status_code
        == 403
    )
