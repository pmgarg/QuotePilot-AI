"""Independent QA re-verification of Stage 4 fix rounds 1-2 (integration: API + PostgreSQL, app role, RLS on)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.session import get_sessionmaker, set_tenant
from app.models.audit import AuditEvent
from app.models.intelligence import Order
from app.models.rfq import RFQ
from tests.conftest import Tenant, login_as
from tests.qa.qa_s4_helpers import (
    WF_ORDER,
    approved_quote,
    audit_actions,
    chat,
    count_rows,
    make_workflow,
    run_wf,
    set_limit_override,
)

pytestmark = pytest.mark.integration

REASON = "zzqa-reject-reason-canary"
KS_REASON = "zzqa-killswitch-reason-canary"


def _audit_rows(org_id, action: str) -> list[dict]:  # type: ignore[no-untyped-def,type-arg]
    with get_sessionmaker()() as db:
        set_tenant(db, org_id, None)
        rows = [dict(e.summary or {}) for e in db.scalars(select(AuditEvent).where(AuditEvent.action == action)).all()]
        db.rollback()
    return rows


def test_run_explanation_keeps_rejection_reason_but_support_export_drops_it_and_kill_switch_reason(
    app, tenant_a: Tenant
) -> None:  # type: ignore[no-untyped-def]
    _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    w = make_workflow(admin, "reasons", WF_ORDER)
    r1 = run_wf(admin, w["id"], "live", {"quote_number": q["number"]}).json()
    rej = admin.post(f"/workflow-runs/{r1['id']}/reject", json={"reason": REASON}).json()
    assert REASON in rej["failure_explanation"]
    assert REASON in admin.get(f"/workflow-runs/{r1['id']}").json()["failure_explanation"]
    r2 = run_wf(admin, w["id"], "live", {"quote_number": q["number"]}).json()
    assert admin.post("/platform/kill-switch", json={"reason": KS_REASON}).status_code == 200
    killed = admin.get(f"/workflow-runs/{r2['id']}").json()
    assert killed["status"] == "killed" and "kill switch" in killed["failure_explanation"]
    export = admin.get("/platform/support-export")
    assert export.status_code == 200
    assert REASON not in export.text and KS_REASON not in export.text
    assert r1["id"] in export.text  # the failed run is still listed, only the free text is gone


def test_new_session_and_plain_chat_turn_write_audit_rows_without_message_text(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    before_s = len(_audit_rows(tenant_a.org_id, "assistant.session_started"))
    before_m = len(_audit_rows(tenant_a.org_id, "assistant.message"))
    text_ = "zzqa-plain-chat-canary hello there"
    sid = sales.post("/assistant/sessions", json={"title": "qa"}).json()["id"]
    assert len(_audit_rows(tenant_a.org_id, "assistant.session_started")) == before_s + 1
    assert chat(sales, text_, sid).status_code == 201
    msgs = _audit_rows(tenant_a.org_id, "assistant.message")
    assert len(msgs) == before_m + 1
    assert msgs[-1]["turn"] == 1 and msgs[-1]["chars"] == len(text_)
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        dumped = " ".join(f"{e.action} {e.summary}" for e in db.scalars(select(AuditEvent)).all())
        db.rollback()
    assert "zzqa-plain-chat-canary" not in dumped


def test_assistant_proposed_order_executes_only_after_permitted_other_user_approves_exactly_once(
    app, tenant_a: Tenant
) -> None:  # type: ignore[no-untyped-def]
    sales, q = approved_quote(app, tenant_a)
    r = chat(sales, f"please convert quote {q['number']} into an order")
    assert r.status_code == 201
    acts = [a for a in r.json()["proposed_actions"] if a["tool"] == "propose_order"]
    assert len(acts) == 1 and acts[0]["status"] == "proposed", r.json()
    inv = acts[0]["invocation_id"]
    assert count_rows(tenant_a.org_id, Order) == 0  # proposal alone has no effect
    readonly = login_as(app, tenant_a.users["readonly"])
    assert readonly.post(f"/actions/{inv}/approve", json={}).status_code in (403, 404)
    assert count_rows(tenant_a.org_id, Order) == 0
    admin = login_as(app, tenant_a.users["admin"])
    assert admin.post(f"/actions/{inv}/approve", json={"reason": "ok"}).status_code == 200
    assert count_rows(tenant_a.org_id, Order) == 1
    assert admin.post(f"/actions/{inv}/approve", json={}).status_code == 409
    assert sales.post(f"/actions/{inv}/approve", json={}).status_code == 409
    assert count_rows(tenant_a.org_id, Order) == 1
    assert audit_actions(tenant_a.org_id, "tool.approve").count("tool.approve") == 1


def test_seat_limit_gives_clear_error_and_no_side_effect(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    members = len(admin.get("/org/members").json())
    set_limit_override(tenant_a.org_id, "seats", members)
    audits = len(audit_actions(tenant_a.org_id, "member"))
    r = admin.post(
        "/org/members",
        json={
            "email": "zzqa-extra@alphatrading.example",
            "full_name": "Extra",
            "role": "sales",
            "password": "Another-Long-Pass-77",
        },
    )
    assert r.status_code == 429 and r.json()["error"] == "limit_exceeded"
    assert "seat" in r.text.lower()
    assert len(admin.get("/org/members").json()) == members
    assert "zzqa-extra" not in str(admin.get("/org/members").json())
    assert len(audit_actions(tenant_a.org_id, "member")) == audits


def test_rfq_monthly_limit_gives_clear_error_and_creates_nothing(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    set_limit_override(tenant_a.org_id, "rfqs_per_month", 1)
    sales = login_as(app, tenant_a.users["sales"])
    body = {"text": "10 pcs SKU-1001", "customer_id": str(tenant_a.customer_id)}
    assert sales.post("/rfqs", json=body).status_code == 201
    audits = len(audit_actions(tenant_a.org_id, "rfq"))
    r = sales.post("/rfqs", json=body)
    assert r.status_code == 429 and r.json()["error"] == "limit_exceeded"
    assert "rfqs_per_month" in r.text or "RFQ" in r.text
    assert count_rows(tenant_a.org_id, RFQ) == 1
    assert len(audit_actions(tenant_a.org_id, "rfq")) == audits
    assert sales.get("/platform/plan").json()["usage"]["rfqs_per_month"] == 1


def test_manual_dry_run_action_stays_blocked_while_kill_switch_engaged_and_release_restores_it(
    app, tenant_a: Tenant
) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    admin = login_as(app, tenant_a.users["admin"])
    body = {"tool": "kpis_summary", "args": {}, "dry_run": True}
    assert sales.post("/actions", json=body).status_code == 201
    assert admin.post("/platform/kill-switch", json={"reason": "qa drill reason"}).status_code == 200
    assert sales.post("/actions", json=body).status_code == 503
    assert admin.delete("/platform/kill-switch").status_code == 200
    assert sales.post("/actions", json=body).status_code == 201


def test_get_workflows_role_matrix_and_quote_won_rejected(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    for role, status in (("owner", 200), ("admin", 200), ("approver", 403), ("sales", 403), ("readonly", 403)):
        assert login_as(app, tenant_a.users[role]).get("/workflows").status_code == status, role
    admin = login_as(app, tenant_a.users["admin"])
    wf = {"name": "won", "trigger": "quote.won", "steps": [{"id": "k", "tool": "kpis_summary", "args": {}}]}
    assert admin.post("/workflows/validate", json={"key": "won", "definition": wf}).json()["ok"] is False
    created = admin.post("/workflows", json={"key": "won", "definition": wf})
    if created.status_code == 201:  # saved as an inactive draft is acceptable; it must never become active
        wid = created.json()["id"]
        assert admin.post(f"/workflows/{wid}/activate?version_no=1", json={}).status_code in (400, 409, 422)
        assert all(w["active_version_no"] is None for w in admin.get("/workflows").json() if w["id"] == wid)
    else:
        assert created.status_code in (400, 422)


@pytest.mark.parametrize("payload", ["a\x00b", "\x00"])
def test_nul_in_message_and_workflow_args_is_422_not_500(app, tenant_a: Tenant, payload: str) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    sid = sales.post("/assistant/sessions", json={}).json()["id"]
    assert sales.post(f"/assistant/sessions/{sid}/messages", json={"text": payload}).status_code == 422
    r = sales.post("/actions", json={"tool": "get_quote", "args": {"quote_number": payload}})
    assert r.status_code == 422
    assert sales.post("/assistant/sessions", json={"title": payload}).status_code == 422


def test_validation_error_text_never_contains_input_fragments(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    canary = "ZZQA-Customer-Canary-Ltd"
    r = sales.post("/actions", json={"tool": "get_quote", "args": {"quote_number": 12345, "x": canary}})
    assert r.status_code in (403, 422)
    assert canary not in r.text
    admin = login_as(app, tenant_a.users["admin"])
    assert canary not in admin.get("/platform/support-export").text
    assert canary not in str(admin.get("/actions").json())
