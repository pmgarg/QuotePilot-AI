"""QA (independent) Stage 4: S4-F04 budgets / circuit breaker / kill switch, S4-F06 plan limits and support export,
S4-A05/A06/A07/A08 and a Stage 1-3 regression through the new limits. Integration evidence."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.db.base import utcnow
from app.db.session import get_sessionmaker, set_tenant
from app.models.intelligence import Order
from app.models.platform import ToolInvocation
from app.models.rfq import RFQ
from app.services.platform import assistant
from app.services.platform.assistant import Plan, PlannedAction
from tests.conftest import Tenant, login_as
from tests.qa.qa_s4_helpers import (
    WF_ORDER,
    approved_quote,
    audit_actions,
    chat,
    count_rows,
    get_counter,
    make_workflow,
    run_wf,
    set_counter,
    set_limit_override,
    set_plan,
    set_subscription_status,
)

pytestmark = pytest.mark.integration


class FakePlanner:
    name = "fake"

    def __init__(self, actions: list[PlannedAction]) -> None:
        self.actions = actions

    def plan(self, text: str, tools: list[Any]) -> Plan:
        return Plan(actions=list(self.actions), reply="", provider="fake")


FAIL_WF = {"name": "fails", "steps": [{"id": "q", "tool": "get_quote", "args": {"quote_number": "Q-1999-99999"}}]}
OK_WF = {"name": "ok", "steps": [{"id": "k", "tool": "kpis_summary", "args": {}}]}


# ------------------------------------------------------------------ assistant budgets (S4-A05)
@pytest.mark.parametrize(("n", "budget_msg"), [(8, False), (9, True), (40, True)])
def test_assistant_step_budget_boundary(app, tenant_a: Tenant, monkeypatch: pytest.MonkeyPatch, n: int, budget_msg: bool) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    monkeypatch.setattr(assistant, "planner_for", lambda org: FakePlanner([PlannedAction("kpis_summary", {}, "loop")] * n))
    m = chat(sales, "go").json()
    assert len(m["proposed_actions"]) == min(n, 8) and ("budget" in m["content"]) is budget_msg
    assert count_rows(tenant_a.org_id, ToolInvocation) == min(n, 8)


def test_looping_side_effect_proposals_are_capped_and_never_execute(app, tenant_a: Tenant, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    sales, q = approved_quote(app, tenant_a)
    monkeypatch.setattr(
        assistant, "planner_for", lambda org: FakePlanner([PlannedAction("propose_order", {"quote_number": q["number"]}, "loop")] * 30)
    )
    m = chat(sales, "go").json()
    assert len(m["proposed_actions"]) == 8 and all(a["status"] == "proposed" for a in m["proposed_actions"])
    assert count_rows(tenant_a.org_id, Order) == 0


def test_session_turn_limit(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    set_plan(login_as(app, tenant_a.users["owner"]), "growth")
    sales = login_as(app, tenant_a.users["sales"])
    sid = sales.post("/assistant/sessions", json={}).json()["id"]
    codes = [chat(sales, "hello", sid).status_code for _ in range(51)]
    assert codes[:50] == [201] * 50 and codes[50] == 429
    assert chat(sales, "hello").status_code == 201  # a new session works


def test_daily_ai_call_budget_is_per_org_exact_and_not_consumed_by_rejections(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    sid = sales.post("/assistant/sessions", json={}).json()["id"]
    codes = [chat(sales, "hi", sid).status_code for _ in range(25)]
    assert codes.count(201) == 20 and codes.count(429) == 5
    assert get_counter(tenant_a.org_id, "ai_calls_per_day") == 20  # blocked attempts did not inflate usage
    assert chat(login_as(app, tenant_b.users["sales"]), "hi").status_code == 201  # other tenant unaffected
    body = chat(sales, "hi", sid).json()
    assert body["error"] == "limit_exceeded"
    # next day: a counter for yesterday does not block today
    set_counter(tenant_a.org_id, "ai_calls_per_day", (utcnow() - timedelta(days=1)).strftime("%Y-%m-%d"), 20)
    set_plan(login_as(app, tenant_a.users["owner"]), "growth")
    assert chat(sales, "hi", sid).status_code == 201


def test_oversized_request_bodies_rejected_not_500(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    sid = sales.post("/assistant/sessions", json={}).json()["id"]
    r = sales.post(f"/assistant/sessions/{sid}/messages", json={"text": "A" * 2_000_000})
    assert r.status_code in (413, 422)
    admin = login_as(app, tenant_a.users["admin"])
    big = {"name": "big", "steps": [{"id": f"s{i}", "tool": "kpis_summary"} for i in range(5000)]}
    r = admin.post("/workflows/validate", json={"key": "big", "definition": big})
    assert r.status_code < 500 and (r.status_code != 200 or r.json()["ok"] is False)


# ------------------------------------------------------------------ workflow limits and breaker
def test_workflow_depth_limit(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    ok = {"name": "d", "steps": [{"id": f"s{i}", "tool": "kpis_summary"} for i in range(8)]}
    deep = {"name": "d", "steps": [{"id": f"s{i}", "tool": "kpis_summary"} for i in range(9)]}
    assert admin.post("/workflows/validate", json={"key": "d", "definition": ok}).json()["ok"] is True
    rep = admin.post("/workflows/validate", json={"key": "d", "definition": deep}).json()
    assert rep["ok"] is False and any("too many steps" in e for e in rep["errors"])
    w = make_workflow(admin, "d8", ok)
    run = run_wf(admin, w["id"], "live").json()
    assert run["status"] == "succeeded" and run["budget_used"] == 8


def test_circuit_breaker_opens_per_tenant_resets_after_cooldown_and_spares_dry_runs(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    set_plan(login_as(app, tenant_a.users["owner"]), "growth")
    admin = login_as(app, tenant_a.users["admin"])
    w = make_workflow(admin, "fails", FAIL_WF)
    assert [run_wf(admin, w["id"], "live").json()["status"] for _ in range(5)] == ["failed"] * 5
    r = run_wf(admin, w["id"], "live")
    assert r.status_code == 429 and "ircuit" in r.text
    assert run_wf(admin, w["id"], "dry_run").status_code == 201  # dry run is not blocked by the breaker
    wb = make_workflow(login_as(app, tenant_b.users["admin"]), "okb", OK_WF)
    assert run_wf(login_as(app, tenant_b.users["admin"]), wb["id"], "live").json()["status"] == "succeeded"
    # cooldown: age the failed runs past the window; the breaker closes
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        db.execute(text("UPDATE workflow_runs SET created_at = now() - interval '1 day' WHERE organization_id = :o"), {"o": str(tenant_a.org_id)})
        db.commit()
    assert run_wf(admin, w["id"], "live").status_code == 201


def test_one_success_among_failures_keeps_breaker_closed(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    set_plan(login_as(app, tenant_a.users["owner"]), "growth")
    admin = login_as(app, tenant_a.users["admin"])
    bad = make_workflow(admin, "bad", FAIL_WF)
    good = make_workflow(admin, "good", OK_WF)
    for _ in range(4):
        run_wf(admin, bad["id"], "live")
    assert run_wf(admin, good["id"], "live").json()["status"] == "succeeded"
    assert run_wf(admin, bad["id"], "live").status_code == 201  # 5th run is not a streak of 5 failures


def test_daily_workflow_run_allowance_and_active_workflow_limit(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    first = make_workflow(admin, "one", OK_WF)
    second = admin.post("/workflows", json={"key": "two", "definition": OK_WF}).json()
    r = admin.post(f"/workflows/{second['id']}/activate?version_no=1", json={})
    assert r.status_code == 429  # starter: 1 active workflow
    assert admin.post(f"/workflows/{first['id']}/activate?version_no=1", json={}).status_code == 200  # same workflow ok
    admin.post(f"/workflows/{first['id']}/disable", json={})
    assert admin.post(f"/workflows/{second['id']}/activate?version_no=1", json={}).status_code == 200
    codes = [run_wf(admin, second["id"], "live").status_code for _ in range(21)]
    assert codes[:20] == [201] * 20 and codes[20] == 429


# ------------------------------------------------------------------ kill switch (S4-A07)
def test_kill_switch_permissions_validation_and_idempotency(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    for role in ("sales", "approver", "readonly"):
        c = login_as(app, tenant_a.users[role])
        assert c.post("/platform/kill-switch", json={"reason": "nope"}).status_code == 403, role
        assert c.delete("/platform/kill-switch").status_code == 403, role
    owner = login_as(app, tenant_a.users["owner"])
    assert owner.post("/platform/kill-switch", json={"reason": ""}).status_code == 422
    assert owner.post("/platform/kill-switch", json={"reason": "ab"}).status_code == 422
    assert owner.post("/platform/kill-switch", json={}).status_code == 422
    assert owner.delete("/platform/kill-switch").status_code == 409  # nothing to release
    assert owner.post("/platform/kill-switch", json={"reason": "incident"}).json()["engaged"] is True
    assert owner.post("/platform/kill-switch", json={"reason": "again"}).status_code == 409
    # scoped to the tenant
    b = login_as(app, tenant_b.users["sales"])
    assert b.get("/platform/kill-switch").json()["engaged"] is False
    assert chat(b, "hi").status_code == 201
    assert owner.delete("/platform/kill-switch").json()["engaged"] is False
    assert {"kill_switch.engage", "kill_switch.release"} <= set(audit_actions(tenant_a.org_id, "kill_switch"))


def test_kill_switch_terminates_runs_blocks_assistant_and_actions_but_allows_dry_run(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    w = make_workflow(admin, "ks", WF_ORDER)
    runs = [run_wf(admin, w["id"], "live", {"quote_number": q["number"]}).json() for _ in range(2)]
    assert [r["status"] for r in runs] == ["awaiting_approval"] * 2
    pending = sales.post("/actions", json={"tool": "propose_order", "args": {"quote_number": q["number"]}}).json()
    ks = admin.post("/platform/kill-switch", json={"reason": "qa incident"}).json()
    assert ks["runs_terminated"] == 2
    for r in runs:
        got = admin.get(f"/workflow-runs/{r['id']}").json()
        assert got["status"] == "killed" and "kill switch" in got["failure_explanation"]
        assert admin.post(f"/workflow-runs/{r['id']}/approve", json={}).status_code == 409
    for text_ in ("hello", f"show quote {q['number']}"):
        r = chat(sales, text_)
        assert r.status_code == 503 and r.json()["error"] == "kill_switch_engaged"
    assert sales.post(f"/actions/{pending['id']}/approve", json={}).status_code == 503
    assert sales.post("/actions", json={"tool": "kpis_summary", "args": {}}).status_code == 503
    assert run_wf(admin, w["id"], "live", {"quote_number": q["number"]}).status_code == 503
    assert count_rows(tenant_a.org_id, Order) == 0
    # dry run stays available so an operator can inspect a workflow during an incident
    d = run_wf(admin, w["id"], "dry_run", {"quote_number": q["number"]})
    assert d.status_code == 201
    assert d.json()["status"] == "succeeded", d.json().get("failure_explanation")
    # release restores service, killed runs stay killed
    admin.delete("/platform/kill-switch")
    assert chat(sales, "hello").status_code == 201
    assert admin.get(f"/workflow-runs/{runs[0]['id']}").json()["status"] == "killed"
    assert sales.post(f"/actions/{pending['id']}/approve", json={}).status_code == 200


def test_manual_business_operations_continue_during_kill_switch(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    admin.post("/platform/kill-switch", json={"reason": "automation incident"})
    sales, q = approved_quote(app, tenant_a)  # create, submit, approve by hand
    assert sales.post(f"/quotes/{q['id']}/orders", json={}).status_code == 201
    assert sales.get("/kpis").status_code == 200


def test_platform_wide_kill_switch_flag(app, tenant_a: Tenant, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    admin = login_as(app, tenant_a.users["admin"])
    w = make_workflow(admin, "pf", OK_WF)
    monkeypatch.setenv("PLATFORM_KILL_SWITCH", "true")
    get_settings.cache_clear()
    assert chat(sales, "hi").status_code == 503
    assert run_wf(admin, w["id"], "live").status_code == 503
    st = admin.get("/platform/kill-switch").json()
    assert st["engaged"] is True and st["platform"] is True
    assert sales.get("/command-center").json()["kill_switch"]["engaged"] is True


# ------------------------------------------------------------------ plan limits (S4-A06)
def test_plan_change_is_owner_only_audited_and_test_mode(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    for role in ("admin", "sales", "approver", "readonly"):
        assert login_as(app, tenant_a.users[role]).post("/platform/plan", json={"plan_code": "scale"}).status_code == 403
    owner = login_as(app, tenant_a.users["owner"])
    for bad in ("", "GROWTH", "enterprise", "../x", "growth; drop table"):
        assert owner.post("/platform/plan", json={"plan_code": bad}).status_code == 422, bad
    assert owner.post("/platform/plan", json={}).status_code == 422
    assert owner.post("/platform/plan", json={"plan_code": 5}).status_code == 422
    p = owner.post("/platform/plan", json={"plan_code": "growth"}).json()
    assert p["plan_code"] == "growth" and p["billing_mode"] == "test" and p["billing_reference"].startswith("test_")
    assert login_as(app, tenant_a.users["admin"]).get("/platform/plan").json()["billing_reference"] is None
    assert login_as(app, tenant_a.users["sales"]).get("/platform/plan").json()["plan_code"] == "growth"
    assert "billing.plan_changed" in audit_actions(tenant_a.org_id, "billing.")
    assert login_as(app, tenant_a.users["readonly"]).get("/platform/plan").json()["billing_reference"] is None


def test_seat_limit_blocks_member_creation_only_and_respects_upgrade(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    set_limit_override(tenant_a.org_id, "seats", 6)  # 5 seeded users
    admin = login_as(app, tenant_a.users["admin"])

    def add(i: int):  # type: ignore[no-untyped-def]
        return admin.post(
            "/org/members",
            json={"email": f"qa{i}@alphatrading.example", "full_name": f"Q {i}", "role": "sales", "password": "Another-Long-Pass-77"},
        )

    assert add(1).status_code == 201
    r = add(2)
    assert r.status_code == 429 and r.json()["error"] == "limit_exceeded"
    # existing members still work at the limit; seats unchanged
    assert login_as(app, tenant_a.users["sales"]).get("/command-center").status_code == 200
    assert len(admin.get("/org/members").json()) == 6
    # upgrading removes the override? (override stays) -> raise it explicitly like an upgrade would
    set_limit_override(tenant_a.org_id, "seats", 25)
    assert add(2).status_code == 201


def test_rfq_monthly_allowance_counts_only_successes(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    set_limit_override(tenant_a.org_id, "rfqs_per_month", 2)
    sales = login_as(app, tenant_a.users["sales"])
    payload = {"text": "10 pcs SKU-1001", "customer_id": str(tenant_a.customer_id)}
    for bad in ({"text": "", "customer_id": str(tenant_a.customer_id)}, {"customer_id": "nope"}):
        assert sales.post("/rfqs", json=bad).status_code in (400, 422)
    assert get_counter(tenant_a.org_id, "rfqs_per_month") <= 2
    first, second = sales.post("/rfqs", json=payload), sales.post("/rfqs", json=payload)
    third = sales.post("/rfqs", json=payload)
    assert (first.status_code, second.status_code) == (201, 201), (first.text, second.text)
    assert third.status_code == 429
    assert count_rows(tenant_a.org_id, RFQ) == 2
    assert login_as(app, tenant_b.users["sales"]).post(
        "/rfqs", json={"text": "10 pcs SKU-1001", "customer_id": str(tenant_b.customer_id)}
    ).status_code == 201
    assert sales.get("/platform/plan").json()["usage"]["rfqs_per_month"] == 2


def test_inactive_subscription_pauses_metered_features(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    owner = login_as(app, tenant_a.users["owner"])
    set_plan(owner, "growth")
    set_subscription_status(tenant_a.org_id, "canceled")
    sales = login_as(app, tenant_a.users["sales"])
    r = chat(sales, "hi")
    assert r.status_code == 429 and "paused" in r.text
    assert sales.get("/quotes").status_code == 200  # core read paths remain
    set_plan(owner, "growth")  # test-mode plan change re-activates
    assert chat(sales, "hi").status_code == 201


# ------------------------------------------------------------------ support export / usage
def test_support_export_and_usage_contain_no_customer_or_message_data(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    admin.post("/customers", json={"name": "ZZQA Secret Customer Ltd", "reference": "ZZQA-REF"})
    chat(sales, "please remember zzqa-confidential-phrase and show quote " + q["number"])
    w = make_workflow(
        admin, "leaky", {"name": "leaky", "steps": [{"id": "c", "tool": "get_customer_profile", "args": {"customer_name": "{{customer_name}}"}}]}
    )
    run = run_wf(admin, w["id"], "live", {"customer_name": "ZZQA Secret Customer Ltd " + "x" * 600}).json()
    assert run["status"] == "failed"
    bundle = admin.get("/platform/support-export")
    usage = admin.get("/platform/usage")
    blob = (bundle.text + usage.text).lower()
    for token in ("zzqa", "customer ltd", "confidential-phrase", q["number"].lower(), "@alphatrading", "buyer"):
        assert token not in blob, f"support export / usage leaks: {token}"
    assert bundle.json()["contains_customer_data"] is False
    assert "support.export" in audit_actions(tenant_a.org_id, "support.")


def test_ops_endpoints_permissions_and_validation(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    for role in ("sales", "approver", "readonly"):
        c = login_as(app, tenant_a.users[role])
        assert c.get("/platform/usage").status_code == 403 and c.get("/platform/support-export").status_code == 403
    owner = login_as(app, tenant_a.users["owner"])
    for q in ("days=0", "days=366", "days=abc", "days=-5"):
        assert owner.get(f"/platform/usage?{q}").status_code == 422, q
    chat(login_as(app, tenant_a.users["sales"]), "hello")
    assert login_as(app, tenant_b.users["owner"]).get("/platform/usage").json()["ai_calls"] == []
    assert owner.get("/platform/support-export").json()["organization_id"] == str(tenant_a.org_id)


# ------------------------------------------------------------------ Stage 1-3 regression through the new limits
def test_stage1_to_3_flows_unaffected_by_starter_limits_and_usage_counts_rfqs(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, q = approved_quote(app, tenant_a)  # RFQ -> quote -> submit -> approve (Stages 1)
    assert sales.get("/platform/plan").json()["usage"]["rfqs_per_month"] == 1
    approver = login_as(app, tenant_a.users["approver"])
    order = sales.post(f"/quotes/{q['id']}/orders", json={})
    assert order.status_code == 201
    assert approver.post(f"/orders/{order.json()['id']}/confirm", json={}).status_code == 200  # Stage 3
    assert sales.get("/kpis").status_code == 200
    csv = login_as(app, tenant_a.users["admin"]).get("/data/export/customers")
    assert csv.status_code == 200  # REST export is a Stage 3 feature, not metered by the assistant plan scope
    assert sales.get("/drafts").status_code == 200  # Stage 2


def test_every_stage4_route_requires_login_and_csrf(client, app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    gets = ["/command-center", "/platform/plan", "/platform/usage", "/platform/support-export", "/platform/kill-switch",
            "/assistant/tools", "/assistant/sessions", "/actions", "/workflows", "/workflow-runs"]
    for g in gets:
        assert client.get(f"/api/v1{g}").status_code == 401, g
    owner = login_as(app, tenant_a.users["owner"])
    for path, body in (("/platform/plan", {"plan_code": "growth"}), ("/platform/kill-switch", {"reason": "csrf test"}),
                       ("/actions", {"tool": "kpis_summary", "args": {}}), ("/workflows", {"key": "x", "definition": OK_WF})):
        assert owner.client.post(f"/api/v1{path}", json=body).status_code == 403, path  # missing CSRF header
    assert owner.get("/platform/kill-switch").json()["engaged"] is False
