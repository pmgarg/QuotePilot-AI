"""QA (independent) Stage 4: S4-F05 workflow validation/versioning/dry run/history/failure explanations, S4-A03,
S4-A09, event triggers and compensation (S4-F04). Integration evidence."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from app.models.followup import CommunicationDraft
from app.models.intelligence import DeletionRequest, Order
from app.services.comms.providers import reset_simulators, simulator_for
from tests.conftest import Tenant, login_as
from tests.qa.qa_s4_helpers import (
    WF_ORDER,
    approved_quote,
    audit_actions,
    business_snapshot,
    count_rows,
    make_workflow,
    run_wf,
    set_plan,
)

pytestmark = pytest.mark.integration


def _wf(steps: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    return {"name": "qa", "steps": steps, **kw}


READ_STEP = {"id": "read", "tool": "get_quote", "args": {"quote_number": "{{quote_number}}"}}


# ---------------------------------------------------------------- permissions
def test_workflow_endpoints_are_owner_admin_only(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    w = make_workflow(admin, "perm", WF_ORDER)
    run = run_wf(admin, w["id"], "dry_run", {"quote_number": "Q-0000-00000"}).json()
    for role in ("sales", "approver", "readonly"):
        c = login_as(app, tenant_a.users[role])
        calls = [
            c.post("/workflows", json={"key": "x", "definition": WF_ORDER}),
            c.post("/workflows/validate", json={"key": "x", "definition": WF_ORDER}),
            c.post(f"/workflows/{w['id']}/activate?version_no=1", json={}),
            c.post(f"/workflows/{w['id']}/disable", json={}),
            c.post(f"/workflows/{w['id']}/runs", json={"mode": "dry_run", "context": {}}),
            c.get("/workflow-runs"),
            c.get(f"/workflow-runs/{run['id']}"),
            c.post(f"/workflow-runs/{run['id']}/approve", json={}),
        ]
        assert [x.status_code for x in calls] == [403] * len(calls), role
    assert login_as(app, tenant_a.users["owner"]).get("/workflows").status_code == 200


def test_workflow_definitions_listing_is_owner_admin_only_per_api_contract(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """docs/API_CONTRACT.md: 'Workflows (owner/admin) GET/POST /workflows'. A viewer must not read definitions."""
    make_workflow(login_as(app, tenant_a.users["admin"]), "secretish", WF_ORDER)
    for role in ("sales", "approver", "readonly"):
        assert login_as(app, tenant_a.users[role]).get("/workflows").status_code == 403, role


# ---------------------------------------------------------------- validation
INVALID = {
    "unknown_trigger": _wf([READ_STEP], trigger="webhook.evil"),
    "empty_steps": _wf([]),
    "unknown_tool": _wf([{"id": "a", "tool": "run_sql", "args": {}}]),
    "duplicate_ids": _wf([READ_STEP, READ_STEP]),
    "bad_id": _wf([{"id": "Bad-ID", "tool": "kpis_summary"}]),
    "unknown_template": _wf([{"id": "a", "tool": "get_quote", "args": {"quote_number": "{{secret}}"}}]),
    "extra_step_field": _wf([{"id": "a", "tool": "kpis_summary", "evil": True}]),
    "extra_def_field": {**_wf([READ_STEP]), "evil": 1},
    "bad_on_failure": _wf([{"id": "a", "tool": "kpis_summary", "on_failure": "ignore"}]),
    "bad_arg_type": _wf([{"id": "a", "tool": "kpis_summary", "args": {"days": "many"}}]),
    "bad_arg_range": _wf([{"id": "a", "tool": "search_quotes", "args": {"limit": 9999}}]),
    "extra_arg": _wf([{"id": "a", "tool": "kpis_summary", "args": {"sql": "drop table x"}}]),
    "plan_blocked_tool": _wf([{"id": "a", "tool": "send_follow_up", "args": {"draft_id": "{{draft_id}}"}}]),
    "role_blocked_tool_depth": _wf([{"id": f"s{i}", "tool": "kpis_summary"} for i in range(9)]),
    "max_depth_lower_than_steps": _wf([{"id": "a", "tool": "kpis_summary"}, {"id": "b", "tool": "kpis_summary"}], max_depth=1),
    "max_depth_zero": _wf([{"id": "a", "tool": "kpis_summary"}], max_depth=0),
    "name_missing": {"steps": [{"id": "a", "tool": "kpis_summary"}]},
    "huge_arg": _wf([{"id": "a", "tool": "search_quotes", "args": {"query": "z" * 100000}}]),
}


@pytest.mark.parametrize("name", sorted(INVALID))
def test_invalid_definitions_report_errors_and_cannot_activate(app, tenant_a: Tenant, name: str) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    rep = admin.post("/workflows/validate", json={"key": "bad", "definition": INVALID[name]})
    assert rep.status_code == 200, rep.text
    assert rep.json()["ok"] is False and rep.json()["errors"], name
    saved = admin.post("/workflows", json={"key": "bad", "definition": INVALID[name]})
    assert saved.status_code == 201 and saved.json()["versions"][0]["validation"]["ok"] is False
    act = admin.post(f"/workflows/{saved.json()['id']}/activate?version_no=1", json={})
    assert act.status_code == 422
    assert admin.get("/workflows").json()[0]["enabled"] is False


def test_request_shape_errors_are_422(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    for body in (
        {"key": "Bad Key", "definition": WF_ORDER},
        {"key": "", "definition": WF_ORDER},
        {"key": "ok", "definition": [1, 2]},
        {"key": "ok"},
        {"definition": WF_ORDER},
    ):
        assert admin.post("/workflows", json=body).status_code == 422, body
        assert admin.post("/workflows/validate", json=body).status_code == 422, body


def test_valid_definition_has_approval_warning_and_no_errors(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    rep = login_as(app, tenant_a.users["admin"]).post("/workflows/validate", json={"key": "w", "definition": WF_ORDER}).json()
    assert rep["ok"] is True and rep["errors"] == [] and any("approval" in w for w in rep["warnings"])


# ---------------------------------------------------------------- versions and activation
def test_versions_history_and_activation_semantics(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    _, q = approved_quote(app, tenant_a)
    v1 = admin.post("/workflows", json={"key": "ver", "definition": _wf([READ_STEP], name="v1")}).json()
    admin.post("/workflows", json={"key": "ver", "definition": INVALID["unknown_tool"]})  # v2 invalid draft
    v3 = admin.post("/workflows", json={"key": "ver", "definition": _wf([READ_STEP], name="v3")}).json()
    assert [v["version_no"] for v in v3["versions"]] == [1, 2, 3] and v3["id"] == v1["id"]
    assert [v["validation"]["ok"] for v in v3["versions"]] == [True, False, True]
    wid = v1["id"]
    assert admin.post(f"/workflows/{wid}/activate?version_no=2", json={}).status_code == 422
    assert admin.post(f"/workflows/{wid}/activate?version_no=99", json={}).status_code == 404
    assert admin.post(f"/workflows/{wid}/activate?version_no=0", json={}).status_code == 422
    assert admin.post(f"/workflows/{wid}/activate", json={}).status_code == 422
    a1 = admin.post(f"/workflows/{wid}/activate?version_no=1", json={}).json()
    assert a1["enabled"] and a1["active_version_no"] == 1
    a3 = admin.post(f"/workflows/{wid}/activate?version_no=3", json={}).json()
    assert [v["status"] for v in a3["versions"]] == ["archived", "draft", "active"]
    # history: runs keep the version they ran
    r_old = run_wf(admin, wid, "dry_run", {"quote_number": q["number"]}, version_no=1).json()
    r_new = run_wf(admin, wid, "live", {"quote_number": q["number"]}).json()
    assert (r_old["version_no"], r_new["version_no"]) == (1, 3)
    # an archived or draft version cannot run live even when requested explicitly
    assert run_wf(admin, wid, "live", {"quote_number": q["number"]}, version_no=1).status_code == 409
    assert run_wf(admin, wid, "live", {"quote_number": q["number"]}, version_no=2).status_code == 409
    assert run_wf(admin, wid, "live", {"quote_number": q["number"]}, version_no=42).status_code in (404, 409)
    assert run_wf(admin, wid, "turbo", {}).status_code == 422
    hist = admin.get("/workflow-runs").json()
    assert {r["version_no"] for r in hist} == {1, 3}
    assert {"workflow.version_created", "workflow.activated", "workflow.run_started"} <= set(
        audit_actions(tenant_a.org_id, "workflow.")
    )


def test_activation_revalidates_against_current_plan(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    owner = login_as(app, tenant_a.users["owner"])
    admin = login_as(app, tenant_a.users["admin"])
    set_plan(owner, "growth")
    wf = _wf([{"id": "send", "tool": "send_follow_up", "args": {"draft_id": "{{draft_id}}"}}])
    w = admin.post("/workflows", json={"key": "sender", "definition": wf}).json()
    assert w["versions"][0]["validation"]["ok"] is True
    set_plan(owner, "starter")
    r = admin.post(f"/workflows/{w['id']}/activate?version_no=1", json={})
    assert r.status_code == 422 and "not allowed" in r.text


def test_workflow_active_before_downgrade_cannot_use_removed_scope(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    reset_simulators()
    owner = login_as(app, tenant_a.users["owner"])
    admin = login_as(app, tenant_a.users["admin"])
    set_plan(owner, "growth")
    w = make_workflow(admin, "sender", _wf([{"id": "send", "tool": "send_follow_up", "args": {"draft_id": "{{draft_id}}"}}]))
    set_plan(owner, "starter")
    run = run_wf(admin, w["id"], "live", {"draft_id": "00000000-0000-0000-0000-000000000001"}).json()
    assert run["status"] == "failed" and "send" in (run["failure_explanation"] or "")
    assert simulator_for(tenant_a.org_id).outbox == []


# ---------------------------------------------------------------- S4-A09 dry run
def test_dry_run_of_every_side_effect_tool_changes_no_business_data(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    reset_simulators()
    owner = login_as(app, tenant_a.users["owner"])
    set_plan(owner, "scale")
    sales, q = approved_quote(app, tenant_a)
    sales.post(f"/customers/{tenant_a.customer_id}/contacts", json={"full_name": "B", "email": "b@c.example", "is_primary": True})
    d = sales.post("/actions", json={"tool": "draft_follow_up", "args": {"quote_number": q["number"]}}).json()
    steps = [
        {"id": "draft", "tool": "draft_follow_up", "args": {"quote_number": "{{quote_number}}"}},
        {"id": "order", "tool": "propose_order", "args": {"quote_number": "{{quote_number}}"}},
        {"id": "send", "tool": "send_follow_up", "args": {"draft_id": d["result"]["draft_id"]}},
        {"id": "del", "tool": "request_customer_deletion", "args": {"customer_id": str(tenant_a.customer_id), "reason": "qa dry"}},
        {"id": "exp", "tool": "export_customers", "args": {}},
    ]
    admin = login_as(app, tenant_a.users["admin"])
    w = admin.post("/workflows", json={"key": "alleffects", "definition": _wf(steps)}).json()
    assert w["versions"][0]["validation"]["ok"] is True, w["versions"][0]["validation"]
    before = business_snapshot(tenant_a.org_id)
    run = run_wf(admin, w["id"], "dry_run", {"quote_number": q["number"]}).json()
    assert run["status"] == "succeeded" and [s["status"] for s in run["steps"]] == ["would_execute"] * 5
    assert business_snapshot(tenant_a.org_id) == before
    assert simulator_for(tenant_a.org_id).outbox == []
    assert count_rows(tenant_a.org_id, DeletionRequest) == 0 and count_rows(tenant_a.org_id, Order) == 0
    assert "content_b64" not in str(run) and "b@c.example" not in str(run)
    assert count_rows(tenant_a.org_id, CommunicationDraft, CommunicationDraft.status == "sent") == 0
    # dry run is not metered as a live run
    assert admin.get("/platform/plan").json()["usage"]["workflow_runs_per_day"] == 0


def test_dry_run_with_missing_data_fails_with_explanation_and_still_no_side_effects(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    w = admin.post("/workflows", json={"key": "miss", "definition": WF_ORDER}).json()
    before = business_snapshot(tenant_a.org_id)
    run = run_wf(admin, w["id"], "dry_run", {"quote_number": "Q-1999-99999"}).json()
    assert run["status"] == "failed" and "read" in run["failure_explanation"]
    assert business_snapshot(tenant_a.org_id) == before
    missing_ctx = run_wf(admin, w["id"], "dry_run", {}).json()
    assert missing_ctx["status"] == "failed" and "quote_number" in missing_ctx["failure_explanation"]


# ---------------------------------------------------------------- live runs, pause, continue, stop
def test_live_run_pauses_before_side_effect_and_only_continues_on_approval(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    wf = _wf([READ_STEP, {"id": "order", "tool": "propose_order", "args": {"quote_number": "{{quote_number}}"}}, {"id": "after", "tool": "kpis_summary", "args": {}}])
    w = make_workflow(admin, "pause", wf)
    run = run_wf(admin, w["id"], "live", {"quote_number": q["number"]}).json()
    assert run["status"] == "awaiting_approval" and run["current_step"] == 1
    assert [s["status"] for s in run["steps"]] == ["executed", "awaiting_approval", "pending"]
    assert count_rows(tenant_a.org_id, Order) == 0
    # only owner/admin decide; other tenants/roles cannot
    assert login_as(app, tenant_a.users["approver"]).post(f"/workflow-runs/{run['id']}/approve", json={}).status_code == 403
    assert sales.post(f"/workflow-runs/{run['id']}/approve", json={}).status_code == 403
    assert count_rows(tenant_a.org_id, Order) == 0
    done = admin.post(f"/workflow-runs/{run['id']}/approve", json={}).json()
    assert done["status"] == "succeeded" and [s["status"] for s in done["steps"]] == ["executed"] * 3
    assert count_rows(tenant_a.org_id, Order) == 1
    for ep in ("approve", "reject"):
        assert admin.post(f"/workflow-runs/{run['id']}/{ep}", json={}).status_code == 409  # not waiting any more
    assert count_rows(tenant_a.org_id, Order) == 1


def test_rejected_step_stops_run_and_later_steps_never_run(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    wf = _wf([{"id": "order", "tool": "propose_order", "args": {"quote_number": "{{quote_number}}"}}, {"id": "after", "tool": "kpis_summary", "args": {}}])
    w = make_workflow(admin, "stop", wf)
    run = run_wf(admin, w["id"], "live", {"quote_number": q["number"]}).json()
    rej = admin.post(f"/workflow-runs/{run['id']}/reject", json={"reason": "not today"}).json()
    assert rej["status"] == "failed" and "not today" in rej["failure_explanation"] and "order" in rej["failure_explanation"]
    assert rej["steps"][1]["status"] == "pending" and count_rows(tenant_a.org_id, Order) == 0


def test_step_invocations_of_runs_cannot_be_approved_through_actions_endpoint(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    w = make_workflow(admin, "bypass", WF_ORDER)
    run = run_wf(admin, w["id"], "live", {"quote_number": q["number"]}).json()
    inv_id = run["steps"][1]["invocation_id"]
    assert admin.post(f"/actions/{inv_id}/approve", json={}).status_code == 403
    assert admin.post(f"/actions/{inv_id}/reject", json={}).status_code == 403
    assert count_rows(tenant_a.org_id, Order) == 0


def test_failure_policies_stop_continue_and_compensate(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    set_plan(login_as(app, tenant_a.users["owner"]), "growth")  # starter allows only one active workflow
    sales, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    sales.post(f"/customers/{tenant_a.customer_id}/contacts", json={"full_name": "B", "email": "b@c.example", "is_primary": True})
    bad = {"id": "bad", "tool": "get_quote", "args": {"quote_number": "Q-1999-99999"}}
    # stop (default)
    w = make_workflow(admin, "f-stop", _wf([bad, READ_STEP]))
    r = run_wf(admin, w["id"], "live", {"quote_number": q["number"]}).json()
    assert r["status"] == "failed" and "bad" in r["failure_explanation"]
    assert [s["status"] for s in r["steps"]] == ["failed", "pending"]
    for leak in ("Traceback", "sqlalchemy", "/home/", "psycopg"):
        assert leak not in r["failure_explanation"] + str(r["steps"])
    # continue
    w = make_workflow(admin, "f-cont", _wf([{**bad, "on_failure": "continue"}, READ_STEP]))
    r = run_wf(admin, w["id"], "live", {"quote_number": q["number"]}).json()
    assert r["status"] == "succeeded" and [s["status"] for s in r["steps"]] == ["failed", "executed"]
    # compensate a draft created earlier in the run
    steps = [{"id": "draft", "tool": "draft_follow_up", "args": {"quote_number": "{{quote_number}}"}}, {**bad, "on_failure": "compensate"}]
    w = make_workflow(admin, "f-comp", _wf(steps))
    r = run_wf(admin, w["id"], "live", {"quote_number": q["number"]}).json()
    assert r["status"] == "compensated" and "draft" in r["failure_explanation"]
    assert r["steps"][0]["status"] == "compensated"
    drafts = admin.get("/drafts").json()
    assert drafts and all(d["status"] == "cancelled" for d in drafts)


def test_compensation_cancels_order_created_after_approval(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    wf = _wf(
        [
            {"id": "order", "tool": "propose_order", "args": {"quote_number": "{{quote_number}}"}},
            {"id": "bad", "tool": "get_quote", "args": {"quote_number": "Q-1999-99999"}, "on_failure": "compensate"},
        ]
    )
    w = make_workflow(admin, "comp-order", wf)
    run = run_wf(admin, w["id"], "live", {"quote_number": q["number"]}).json()
    done = admin.post(f"/workflow-runs/{run['id']}/approve", json={}).json()
    assert done["status"] == "compensated"
    orders = admin.get("/orders").json()
    assert len(orders) == 1 and orders[0]["status"] == "cancelled"


# ---------------------------------------------------------------- event triggers
def test_quote_approved_event_fires_only_matching_enabled_workflows(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    set_plan(login_as(app, tenant_a.users["owner"]), "growth")
    on = make_workflow(admin, "on-approve", {**WF_ORDER, "trigger": "quote.approved"})
    make_workflow(admin, "manual-only", {**WF_ORDER, "trigger": "manual"})
    make_workflow(admin, "on-confirm", {**WF_ORDER, "trigger": "order.confirmed"})
    drafty = admin.post("/workflows", json={"key": "draft-only", "definition": {**WF_ORDER, "trigger": "quote.approved"}}).json()  # never activated
    wb = make_workflow(login_as(app, tenant_b.users["admin"]), "b-on-approve", {**WF_ORDER, "trigger": "quote.approved"})
    sales, q = approved_quote(app, tenant_a)
    runs = admin.get("/workflow-runs").json()
    assert [(r["definition_id"], r["trigger"], r["mode"]) for r in runs] == [(on["id"], "quote.approved", "live")]
    assert runs[0]["context"]["quote_number"] == q["number"] and runs[0]["status"] == "awaiting_approval"
    assert login_as(app, tenant_b.users["admin"]).get("/workflow-runs").json() == []
    assert drafty["enabled"] is False and wb["enabled"] is True
    # order.confirmed fires its own workflow, with the order id in context
    admin.post(f"/workflow-runs/{runs[0]['id']}/approve", json={})
    order = admin.get("/orders").json()[0]
    assert login_as(app, tenant_a.users["approver"]).post(f"/orders/{order['id']}/confirm", json={}).status_code == 200
    triggers = sorted(r["trigger"] for r in admin.get("/workflow-runs").json())
    assert triggers == ["order.confirmed", "quote.approved"]
    # disabling stops the trigger
    admin.post(f"/workflows/{on['id']}/disable", json={})
    approved_quote(app, tenant_a)
    assert sum(1 for r in admin.get("/workflow-runs").json() if r["trigger"] == "quote.approved") == 1


def test_event_runs_never_undo_or_block_the_business_action(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    make_workflow(admin, "on-approve", {**WF_ORDER, "trigger": "quote.approved"})
    admin.post("/platform/kill-switch", json={"reason": "drill"})
    sales = login_as(app, tenant_a.users["sales"])
    _, q = approved_quote(app, tenant_a)  # manual approval still works while automation is stopped
    assert admin.get("/workflow-runs").json() == []
    assert sales.get(f"/quotes/{q['id']}").json()["status"] == "approved"


def test_quote_won_trigger_is_accepted_but_documented_triggers_only_fire(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """Observation (low): validator accepts 'quote.won' but no dispatch exists; assert what is documented only."""
    admin = login_as(app, tenant_a.users["admin"])
    rep = admin.post("/workflows/validate", json={"key": "w", "definition": {**WF_ORDER, "trigger": "quote.won"}}).json()
    assert rep["ok"] is False or any("won" in w for w in rep["warnings"]), (
        "workflow accepts trigger 'quote.won' silently although nothing ever dispatches it"
    )


def test_cross_tenant_workflow_ids_are_404(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    wa = make_workflow(login_as(app, tenant_a.users["admin"]), "a-only", WF_ORDER)
    b = login_as(app, tenant_b.users["admin"])
    for r in (
        b.post(f"/workflows/{wa['id']}/disable", json={}),
        b.post(f"/workflows/{wa['id']}/activate?version_no=1", json={}),
        run_wf(b, wa["id"], "dry_run"),
    ):
        assert r.status_code == 404
    assert b.get("/workflows").json() == []
    assert copy.deepcopy(WF_ORDER)["name"]  # fixture unchanged
