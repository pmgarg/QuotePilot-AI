"""QA (independent) Stage 4: S4-F03 action scopes and approval checkpoints, S4-A03 (unapproved never executes),
S4-A04 (malformed tool input rejected). Integration evidence."""

from __future__ import annotations

import base64
import threading
from datetime import date, timedelta

import pytest
from sqlalchemy import update

from app.db.session import get_sessionmaker, set_tenant
from app.models.followup import CommunicationDraft
from app.models.intelligence import DeletionRequest, Order
from app.models.quote import Quote
from app.services.comms.providers import reset_simulators, simulator_for
from tests.conftest import Tenant, login_as
from tests.integration.test_quotes import make_quote
from tests.qa.qa_s4_helpers import approved_quote, audit_actions, chat, count_rows, set_plan

pytestmark = pytest.mark.integration


def _propose_order(sales, q):  # type: ignore[no-untyped-def]
    r = sales.post("/actions", json={"tool": "propose_order", "args": {"quote_number": q["number"]}})
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------ nothing runs before approval
def test_no_side_effect_tool_executes_on_proposal(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    reset_simulators()
    set_plan(login_as(app, tenant_a.users["owner"]), "scale")
    sales, q = approved_quote(app, tenant_a)
    approver = login_as(app, tenant_a.users["approver"])
    sales.post(
        f"/customers/{tenant_a.customer_id}/contacts",
        json={"full_name": "B", "email": "b@c.example", "is_primary": True},
    )
    d = sales.post("/actions", json={"tool": "draft_follow_up", "args": {"quote_number": q["number"]}}).json()
    draft_id = d["result"]["draft_id"]
    assert d["status"] == "executed" and d["requires_approval"] is False  # a draft is not a send
    proposals = [
        sales.post("/actions", json={"tool": "propose_order", "args": {"quote_number": q["number"]}}),
        sales.post("/actions", json={"tool": "send_follow_up", "args": {"draft_id": draft_id}}),
        sales.post(
            "/actions",
            json={
                "tool": "request_customer_deletion",
                "args": {"customer_id": str(tenant_a.customer_id), "reason": "qa test"},
            },
        ),
        approver.post("/actions", json={"tool": "export_customers", "args": {}}),
    ]
    for p in proposals:
        assert p.status_code == 201, p.text
        body = p.json()
        assert body["status"] == "proposed" and body["requires_approval"] is True
        assert "download" not in body and "content_b64" not in str(body)
    assert count_rows(tenant_a.org_id, Order) == 0
    assert count_rows(tenant_a.org_id, DeletionRequest) == 0
    assert simulator_for(tenant_a.org_id).outbox == []
    assert count_rows(tenant_a.org_id, CommunicationDraft, CommunicationDraft.status == "sent") == 0
    # an approval-requiring tool cannot be forced through the dry-run flag to side-effect
    dry = sales.post("/actions", json={"tool": "propose_order", "args": {"quote_number": q["number"]}, "dry_run": True})
    assert dry.json()["status"] == "would_execute" and count_rows(tenant_a.org_id, Order) == 0


def test_deletion_tool_only_opens_request_and_downstream_gate_still_applies(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    set_plan(login_as(app, tenant_a.users["owner"]), "scale")
    sales = login_as(app, tenant_a.users["sales"])
    inv = sales.post(
        "/actions",
        json={
            "tool": "request_customer_deletion",
            "args": {"customer_id": str(tenant_a.customer_id), "reason": "gdpr"},
        },
    ).json()
    ok = sales.post(f"/actions/{inv['id']}/approve", json={})
    assert ok.status_code == 200 and ok.json()["status"] == "executed"
    reqs = login_as(app, tenant_a.users["admin"]).get("/retention/requests").json()
    assert len(reqs) == 1 and reqs[0]["status"] == "requested"
    assert sales.post(f"/retention/requests/{reqs[0]['id']}/approve", json={}).status_code == 403
    assert sales.get(f"/customers/{tenant_a.customer_id}").status_code == 200  # customer still exists


def test_order_proposal_for_unapproved_withdrawn_or_expired_quote_is_refused_downstream(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    draft_q = make_quote(sales, str(tenant_a.customer_id))
    inv = _propose_order(sales, draft_q)
    r = sales.post(f"/actions/{inv['id']}/approve", json={})
    assert r.status_code == 409 and count_rows(tenant_a.org_id, Order) == 0
    # expired approved quote
    _, q2 = approved_quote(app, tenant_a)
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        db.execute(
            update(Quote).where(Quote.number == q2["number"]).values(valid_until=date.today() - timedelta(days=2))
        )
        db.commit()
    inv2 = _propose_order(sales, q2)
    assert sales.post(f"/actions/{inv2['id']}/approve", json={}).status_code == 409
    assert count_rows(tenant_a.org_id, Order) == 0
    # after a failed approval the action must not appear as executed
    states = {a["id"]: a["status"] for a in sales.get("/actions").json()}
    assert states[inv["id"]] != "executed" and states[inv2["id"]] != "executed"


def test_duplicate_open_order_blocked_on_second_approval(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, q = approved_quote(app, tenant_a)
    a, b = _propose_order(sales, q), _propose_order(sales, q)
    assert sales.post(f"/actions/{a['id']}/approve", json={}).status_code == 200
    assert sales.post(f"/actions/{b['id']}/approve", json={}).status_code == 409
    assert count_rows(tenant_a.org_id, Order) == 1


# ------------------------------------------------------------ requester / owner / admin rules
def test_approval_authority_matrix(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, q = approved_quote(app, tenant_a)
    inv = _propose_order(sales, q)
    path = f"/actions/{inv['id']}"
    approver = login_as(app, tenant_a.users["approver"])
    readonly = login_as(app, tenant_a.users["readonly"])
    for c in (approver, readonly):
        assert c.post(f"{path}/approve", json={}).status_code == 403
        assert c.post(f"{path}/reject", json={}).status_code == 403
    assert login_as(app, tenant_b.users["owner"]).post(f"{path}/approve", json={}).status_code == 404
    assert count_rows(tenant_a.org_id, Order) == 0
    admin = login_as(app, tenant_a.users["admin"])
    r = admin.post(f"{path}/approve", json={"reason": "admin ok"})
    assert r.status_code == 200 and r.json()["decided_by"] != r.json()["requested_by"]
    assert r.json()["decision_reason"] == "admin ok"
    assert {"tool.propose", "tool.approve", "tool.execute"} <= set(audit_actions(tenant_a.org_id, "tool."))


def test_reject_then_approve_and_approve_then_reject_are_conflicts(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, q = approved_quote(app, tenant_a)
    inv = _propose_order(sales, q)
    assert sales.post(f"/actions/{inv['id']}/reject", json={"reason": "wrong"}).json()["status"] == "rejected"
    assert sales.post(f"/actions/{inv['id']}/approve", json={}).status_code == 409
    assert count_rows(tenant_a.org_id, Order) == 0
    inv2 = _propose_order(sales, q)
    assert sales.post(f"/actions/{inv2['id']}/approve", json={}).status_code == 200
    assert sales.post(f"/actions/{inv2['id']}/reject", json={}).status_code == 409
    assert sales.post(f"/actions/{inv2['id']}/approve", json={}).status_code == 409  # idempotent: no second order
    assert count_rows(tenant_a.org_id, Order) == 1


def test_concurrent_double_approval_executes_exactly_once(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, q = approved_quote(app, tenant_a)
    inv = _propose_order(sales, q)
    clients = [login_as(app, tenant_a.users["sales"]) for _ in range(6)]
    codes: list[int] = []
    barrier = threading.Barrier(len(clients))

    def go(c) -> None:  # type: ignore[no-untyped-def]
        barrier.wait()
        codes.append(c.post(f"/actions/{inv['id']}/approve", json={}).status_code)

    ts = [threading.Thread(target=go, args=(c,)) for c in clients]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert codes.count(200) == 1, codes
    assert count_rows(tenant_a.org_id, Order) == 1


# ------------------------------------------------------------ policy re-check at execution time
def test_plan_downgrade_or_kill_switch_between_proposal_and_approval_blocks_execution(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    owner = login_as(app, tenant_a.users["owner"])
    set_plan(owner, "growth")
    admin = login_as(app, tenant_a.users["admin"])
    inv = admin.post("/actions", json={"tool": "export_customers", "args": {}}).json()
    set_plan(owner, "starter")
    r = admin.post(f"/actions/{inv['id']}/approve", json={})
    assert r.status_code == 403 and "content_b64" not in r.text
    set_plan(owner, "growth")
    admin.post("/platform/kill-switch", json={"reason": "drill"})
    r = admin.post(f"/actions/{inv['id']}/approve", json={})
    assert r.status_code == 503 and "content_b64" not in r.text
    admin.delete("/platform/kill-switch")
    ok = admin.post(f"/actions/{inv['id']}/approve", json={})
    assert ok.status_code == 200 and "download" in ok.json()


def test_export_via_assistant_is_formula_safe_and_not_stored(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    set_plan(login_as(app, tenant_a.users["owner"]), "growth")
    admin = login_as(app, tenant_a.users["admin"])
    admin.post("/customers", json={"name": '=HYPERLINK("http://evil.example","x")', "reference": "CUST-INJ"})
    inv = admin.post("/actions", json={"tool": "export_customers", "args": {}}).json()
    ok = admin.post(f"/actions/{inv['id']}/approve", json={}).json()
    csv = base64.b64decode(ok["download"]["content_b64"]).decode("utf-8-sig")
    for line in csv.splitlines():
        assert not any(cell.startswith(("=", "@")) for cell in line.split(",")), line
    assert "content_b64" not in str(admin.get("/actions").json())
    assert "content_b64" not in str(admin.get("/actions?status=executed").json())
    # the same bulk export is audited like the REST export (high-impact action)
    acts = audit_actions(tenant_a.org_id)
    assert "export.customers" in acts or "tool.execute" in acts


# ------------------------------------------------------------ S4-A04 malformed arguments
@pytest.mark.parametrize(
    "body",
    [
        {"tool": "get_quote", "args": {"quote_number": "Q-1", "evil": 1}},
        {"tool": "get_quote", "args": {"quote_id": "not-a-uuid"}},
        {"tool": "search_quotes", "args": {"limit": 0}},
        {"tool": "search_quotes", "args": {"limit": 51}},
        {"tool": "search_quotes", "args": {"query": "x" * 101}},
        {"tool": "search_quotes", "args": {"status": "x" * 501}},
        {"tool": "kpis_summary", "args": {"days": -1}},
        {"tool": "kpis_summary", "args": {"days": "abc"}},
        {"tool": "send_follow_up", "args": {"draft_id": "1' OR '1'='1"}},
        {"tool": "get_quote", "args": ["list"]},
        {"tool": "get_quote", "args": "string"},
        {"tool": "get_quote", "args": None},
    ],
)
def test_malformed_tool_arguments_rejected_with_4xx(app, tenant_a: Tenant, body: dict) -> None:  # type: ignore[no-untyped-def,type-arg]
    r = login_as(app, tenant_a.users["sales"]).post("/actions", json=body)
    assert 400 <= r.status_code < 500, (body, r.status_code, r.text)
    assert "Traceback" not in r.text


@pytest.mark.parametrize("tool", ["run_sql", "shell", "http_get", "../etc/passwd", "GET_QUOTE", "get_quote ", "x" * 65])
def test_unknown_tool_names_rejected(app, tenant_a: Tenant, tool: str) -> None:  # type: ignore[no-untyped-def]
    r = login_as(app, tenant_a.users["owner"]).post("/actions", json={"tool": tool, "args": {}})
    assert 400 <= r.status_code < 500


def test_like_wildcards_in_search_are_literal(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, q = approved_quote(app, tenant_a)
    r = sales.post("/actions", json={"tool": "search_quotes", "args": {"query": "%"}}).json()
    assert r["result"]["count"] == 0
    r = sales.post("/actions", json={"tool": "search_quotes", "args": {"query": "_"}}).json()
    assert r["result"]["count"] == 0


def test_actions_listing_privacy_and_filters(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, q = approved_quote(app, tenant_a)
    inv = _propose_order(sales, q)
    approver = login_as(app, tenant_a.users["approver"])
    assert inv["id"] not in {a["id"] for a in approver.get("/actions").json()}
    admin = login_as(app, tenant_a.users["admin"])
    assert inv["id"] in {a["id"] for a in admin.get("/actions?status=proposed").json()}
    assert admin.get("/actions?status=executed").json() == []
    assert admin.get("/actions?status=" + "x" * 17).status_code == 422
    chat(sales, "hello")  # no crash on listing after assistant use
    assert sales.get("/actions").status_code == 200
