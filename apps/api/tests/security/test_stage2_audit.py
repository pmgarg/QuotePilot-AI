"""Independent Stage 2 security audit (2026-11-02, auditor: security-auditor subagent).

Scope: tenant isolation / RLS of every Stage 2 table and the `app.worker` scope, consent and suppression on every
send path (manual, scheduled, queued retry), secrets handling, template / SMTP header injection, inbound processing,
the PostgreSQL job queue, RBAC / CSRF / audit coverage and the development-only endpoints.

Findings were reproduced as strict xfails carrying the finding id (S2-SEC-nn; see docs/evidence/security-stage2-2026-11-02.md);
after fix round 1 only S2-SEC-06 (accepted, documented) is still an xfail.
Passing checks are plain tests. The auditor changed no product code.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import re
import secrets as pysecrets
import uuid
from datetime import timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core import config
from app.core.logging import JsonFormatter, redact
from app.db.base import utcnow
from app.db.session import get_sessionmaker, set_tenant, set_worker_scope
from app.models.followup import FollowUpSchedule
from app.models.org import Organization
from app.services.comms import providers
from app.services.comms.providers import SMTPProvider, reset_simulators, simulator_for
from app.services.followup import service as fu
from app.services.jobs import queue
from tests.conftest import API, AuthedClient, Tenant, login_as
from tests.integration.test_stage2_followup import add_contact, approved_quote, mark_sent

pytestmark = [pytest.mark.integration, pytest.mark.security]

STAGE2_TABLES = (
    "contacts",
    "follow_up_policies",
    "message_templates",
    "quote_status_events",
    "follow_up_schedules",
    "communication_drafts",
    "communication_events",
    "integration_connections",
    "job_runs",
)
WORKER_TABLES = {"follow_up_schedules", "job_runs"}
APP_DIR = Path(__file__).resolve().parents[2] / "app"


@pytest.fixture(autouse=True)
def _reset_sim() -> None:
    reset_simulators()


# ---------------------------------------------------------------- helpers
def sql(org_id: uuid.UUID | None, stmt: str, **params: Any) -> Any:
    """Run one statement as the app role (RLS enforced) inside the given tenant context (None → no tenant)."""
    with get_sessionmaker()() as db:
        set_tenant(db, org_id, None)
        res = db.execute(text(stmt), params)
        out = res.fetchall() if res.returns_rows else res.rowcount
        db.commit()
        return out


def count(org_id: uuid.UUID | None, table: str) -> int:
    return int(sql(org_id, f"SELECT count(*) FROM {table}")[0][0])  # noqa: S608 - table names are a fixed tuple


def audit_actions(org_id: uuid.UUID) -> list[str]:
    return [r[0] for r in sql(org_id, "SELECT action FROM audit_events ORDER BY created_at, id")]


def make_due(org_id: uuid.UUID, quote_id: str, attempt_no: int | None = None) -> None:
    stmt = "UPDATE follow_up_schedules SET due_at = now() - interval '1 minute' WHERE quote_id = :q"
    if attempt_no is not None:
        stmt += " AND attempt_no = :a"
    sql(org_id, stmt, q=quote_id, a=attempt_no)


def force_jobs_due(org_id: uuid.UUID) -> None:
    sql(org_id, "UPDATE job_runs SET next_run_at = now() - interval '1 second' WHERE status = 'queued'")


def worker_tick() -> tuple[int, int]:
    with get_sessionmaker()() as db:
        enq = queue.enqueue_due_schedules(db)
        ran = queue.run_once(db)
    return enq, ran


def sent_quote_with_contact(
    app: Any,
    tenant: Tenant,
    *,
    automation: bool = False,
    email: str = "buyer@customer.example",
    name: str = "Buyer Person",
) -> tuple[AuthedClient, AuthedClient, dict[str, Any], dict[str, Any]]:
    """Approved quote → (optional admin consent) → contact → marked sent (schedules planned by policy)."""
    sales, _, q = approved_quote(app, tenant)
    admin = login_as(app, tenant.users["admin"])
    if automation:
        pol = admin.patch("/follow-up/policy", json={"automation_enabled": True}).json()
        assert pol["automation_enabled"] and pol["automation_consented_at"]
    r = sales.post(
        f"/customers/{tenant.customer_id}/contacts", json={"full_name": name, "email": email, "is_primary": True}
    )
    assert r.status_code == 201, r.text
    c = r.json()
    q = mark_sent(sales, q)
    return sales, admin, q, c


def _app_for_env(monkeypatch: pytest.MonkeyPatch, env: str) -> Any:
    monkeypatch.setenv("APP_ENV", env)
    monkeypatch.setenv("SECRET_KEY", pysecrets.token_urlsafe(48))
    monkeypatch.setenv("COOKIE_SECURE", "false")  # keep the http TestClient usable; cookie flags are a Stage 1 control
    config.get_settings.cache_clear()
    from app.main import create_app

    return create_app()


class FakeSMTP:
    """Captures what SMTPProvider would hand to smtplib; nothing leaves the process."""

    sent: list[EmailMessage] = []

    def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
        self.host, self.port = host, port

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def starttls(self) -> None:
        return None

    def login(self, username: str, password: str) -> None:
        return None

    def send_message(self, msg: EmailMessage) -> dict[str, Any]:
        FakeSMTP.sent.append(msg)
        return {}


def _smtp_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(providers.smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sent.clear()
    monkeypatch.setenv("MAIL_PROVIDER_DEFAULT", "smtp")
    monkeypatch.setenv("SMTP_HOST", "smtp.invalid")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "quotes@alphatrading.example")
    config.get_settings.cache_clear()


# ================================================================ 1. tenant isolation and the worker scope
def test_rls_isolates_every_stage2_table_for_the_app_role(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, admin, q, c = sent_quote_with_contact(app, tenant_a)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    assert sales.post(f"/drafts/{d['id']}/send").status_code == 200
    assert admin.post("/integrations", json={"provider": "simulator"}).status_code == 201
    assert admin.get("/follow-up/templates").status_code == 200
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        assert queue.enqueue(db, org_id=tenant_a.org_id, kind="sync_inbound", idempotency_key="audit", payload={})
        db.commit()
    flags = {
        r[0]: (r[1], r[2])
        for r in sql(
            None,
            "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = ANY(:t)",
            t=list(STAGE2_TABLES),
        )
    }
    for table in STAGE2_TABLES:
        assert flags[table] == (True, True), f"{table} must have RLS enabled and forced"
        assert count(tenant_a.org_id, table) >= 1, table
        assert count(tenant_b.org_id, table) == 0, table
        assert count(None, table) == 0, table
        assert sql(tenant_b.org_id, f"UPDATE {table} SET organization_id = organization_id") == 0, table  # noqa: S608
        assert sql(tenant_b.org_id, f"DELETE FROM {table}") == 0, table  # noqa: S608
        assert count(tenant_a.org_id, table) >= 1, table
    # WITH CHECK: tenant B's context cannot insert a row that belongs to tenant A
    with pytest.raises(Exception, match="row-level security"):
        sql(
            tenant_b.org_id,
            "INSERT INTO contacts (id, organization_id, customer_id, full_name, preferred_channel, language, timezone, "
            "is_primary, created_at, updated_at) VALUES (gen_random_uuid(), :o, :cust, 'pwn', 'email', 'en', 'UTC', "
            "false, now(), now())",
            o=tenant_a.org_id,
            cust=tenant_a.customer_id,
        )
    # the worker scope widens exactly the two scheduler tables and nothing else
    with get_sessionmaker()() as db:
        set_worker_scope(db)
        for table in STAGE2_TABLES:
            n = db.execute(text(f"SELECT count(*) FROM {table}")).scalar()  # noqa: S608
            assert (n >= 1) if table in WORKER_TABLES else (n == 0), (table, n)
        db.rollback()


def test_worker_scope_is_transaction_local_and_absent_inside_job_bodies(
    app, tenant_a: Tenant, tenant_b: Tenant
) -> None:  # type: ignore[no-untyped-def]
    add_contact(login_as(app, tenant_b.users["sales"]), str(tenant_b.customer_id), email="other@beta.example")
    seen: dict[str, Any] = {}

    def probe(db: Any, org: Organization, payload: dict[str, Any]) -> None:
        seen["worker"] = db.execute(text("SELECT current_setting('app.worker', true)")).scalar()
        seen["org"] = db.execute(text("SELECT current_setting('app.current_org', true)")).scalar()
        seen["foreign_contacts"] = db.execute(text("SELECT count(*) FROM contacts")).scalar()
        seen["foreign_jobs"] = db.execute(
            text("SELECT count(*) FROM job_runs WHERE organization_id <> :o"), {"o": org.id}
        ).scalar()

    queue.HANDLERS["audit_probe"] = probe
    try:
        with get_sessionmaker()() as db:
            set_tenant(db, tenant_a.org_id, None)
            queue.enqueue(db, org_id=tenant_a.org_id, kind="audit_probe", idempotency_key="probe", payload={})
            db.commit()
        with get_sessionmaker()() as db:
            job = queue.claim_next(db, "audit-worker")
            assert job is not None and job.status == "running" and job.locked_by == "audit-worker"
            # the claim committed; the worker scope did not survive the transaction boundary
            assert (db.execute(text("SELECT current_setting('app.worker', true)")).scalar() or "") == ""
            assert db.execute(text("SELECT count(*) FROM job_runs")).scalar() == 0  # no tenant bound either
            queue.run_job(db, job)
        assert (seen["worker"] or "") == ""
        assert seen["org"] == str(tenant_a.org_id)
        assert seen["foreign_contacts"] == 0 and seen["foreign_jobs"] == 0
        assert sql(tenant_a.org_id, "SELECT status FROM job_runs WHERE kind = 'audit_probe'")[0][0] == "succeeded"
    finally:
        queue.HANDLERS.pop("audit_probe", None)


def test_worker_scope_has_no_caller_outside_the_queue_module() -> None:
    callers = sorted(
        str(p.relative_to(APP_DIR))
        for p in APP_DIR.rglob("*.py")
        if re.search(r"set_worker_scope\(", p.read_text()) and p.name != "session.py"
    )
    assert callers == ["services/jobs/queue.py"]
    # S2-SEC-01 fixed: the request handler may only call the queue's tenant-scoped entry points (org_id=...), never
    # the unscoped cross-tenant forms this test documented before the fix round
    router = (APP_DIR / "api" / "routers" / "followup.py").read_text()
    assert "queue.enqueue_due_schedules(db)" not in router and "queue.run_once(db)" not in router
    assert "queue.enqueue_due_schedules(db, org_id=org_id" in router
    assert "queue.run_once(db, org_id=org_id" in router


def test_run_once_request_from_one_tenant_touches_only_that_tenants_queue(
    app, tenant_a: Tenant, tenant_b: Tenant
) -> None:  # type: ignore[no-untyped-def]
    _, _, qb, _ = sent_quote_with_contact(app, tenant_b, automation=True, email="buyer@beta.example")
    make_due(tenant_b.org_id, qb["id"], 1)
    a_admin = login_as(app, tenant_a.users["admin"])
    r = a_admin.post("/jobs/run-once")
    assert r.status_code == 200, r.text
    assert sql(tenant_a.org_id, "SELECT count(*) FROM job_runs")[0][0] == 0  # A has no work of its own
    # secure behaviour: A's request must neither run B's jobs nor report their number
    assert r.json()["processed"] == 0
    assert simulator_for(tenant_b.org_id).outbox == []
    assert sql(tenant_b.org_id, "SELECT count(*) FROM job_runs WHERE status = 'succeeded'")[0][0] == 0


def test_cross_tenant_remaining_stage2_routes_are_404_and_filters_are_empty(
    app, tenant_a: Tenant, tenant_b: Tenant
) -> None:  # type: ignore[no-untyped-def]
    sales, admin, q, c = sent_quote_with_contact(app, tenant_a)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    s = sales.get(f"/follow-up/schedules?quote_id={q['id']}").json()[0]
    tmpl = admin.get("/follow-up/templates").json()[0]
    conn = admin.post("/integrations", json={"provider": "simulator"}).json()
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        job = queue.enqueue(db, org_id=tenant_a.org_id, kind="sync_inbound", idempotency_key="x", payload={})
        assert job is not None
        job.status = "dead"
        job_id = str(job.id)
        db.commit()
    b = login_as(app, tenant_b.users["owner"])
    future = (utcnow() + timedelta(days=3)).isoformat()
    attempts = [
        ("POST", f"/follow-up/schedules/{s['id']}/reschedule", {"due_at": future}),
        ("POST", f"/drafts/{d['id']}/cancel", None),
        ("POST", f"/quotes/{q['id']}/follow-up/plan", None),
        ("PATCH", f"/follow-up/templates/{tmpl['id']}", {"name": "pwn"}),
        ("POST", f"/jobs/{job_id}/retry", None),
        ("POST", f"/integrations/{conn['id']}/revoke", None),
        ("GET", f"/customers/{tenant_a.customer_id}/contacts", None),
        ("POST", f"/customers/{tenant_a.customer_id}/contacts", {"full_name": "pwn", "email": "p@w.example"}),
        ("POST", f"/contacts/{c['id']}/opt-out", {"opted_out": False}),
        ("POST", f"/quotes/{q['id']}/transition", {"expected_lock_version": 0, "to_status": "lost", "evidence": "x"}),
    ]
    for method, path, body in attempts:
        r = b.get(path) if method == "GET" else (b.post if method == "POST" else b.patch)(path, json=body)
        assert r.status_code == 404, (method, path, r.status_code, r.text)
    assert b.get(f"/follow-up/schedules?status=all&quote_id={q['id']}").json() == []
    assert b.get(f"/communication-events?quote_id={q['id']}").json() == []
    assert b.get("/jobs").json() == [] and b.get("/integrations").json() == []
    # B cannot aim a draft or a recipient at A's contact
    b_sales, _, qb = approved_quote(app, tenant_b)
    cb = add_contact(b_sales, str(tenant_b.customer_id), email="buyer@beta.example")
    qb = mark_sent(b_sales, qb)
    assert b_sales.post(f"/quotes/{qb['id']}/drafts", json={"contact_id": c["id"]}).status_code == 404
    db_ = b_sales.post(f"/quotes/{qb['id']}/drafts", json={"contact_id": cb["id"]}).json()
    assert b_sales.patch(f"/drafts/{db_['id']}", json={"to_email": c["email"]}).status_code == 422
    # nothing in A changed
    assert sales.get(f"/drafts/{d['id']}").json()["status"] == "draft"
    assert sales.get(f"/follow-up/schedules?quote_id={q['id']}").json()[0]["due_at"] == s["due_at"]
    assert admin.get("/integrations").json()[0]["status"] == "active"
    assert sales.get(f"/customers/{tenant_a.customer_id}/contacts").json()[0]["opted_out_at"] is None


# ================================================================ 2. RBAC, CSRF, audit coverage
def test_rbac_matrix_for_stage2_routes(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, admin, q, c = sent_quote_with_contact(app, tenant_a)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    s = sales.get(f"/follow-up/schedules?quote_id={q['id']}").json()[0]
    tmpl = admin.get("/follow-up/templates").json()[0]
    conn = admin.post("/integrations", json={"provider": "simulator"}).json()
    readonly = login_as(app, tenant_a.users["readonly"])
    mutations = [
        ("POST", f"/customers/{tenant_a.customer_id}/contacts", {"full_name": "x"}),
        ("PATCH", f"/contacts/{c['id']}", {"full_name": "x"}),
        ("POST", f"/contacts/{c['id']}/opt-out", {"opted_out": True}),
        ("PATCH", "/follow-up/policy", {"max_attempts": 1}),
        ("PATCH", f"/follow-up/templates/{tmpl['id']}", {"name": "x"}),
        (
            "POST",
            f"/quotes/{q['id']}/transition",
            {"expected_lock_version": q["lock_version"], "to_status": "lost", "evidence": "x"},
        ),
        ("POST", f"/quotes/{q['id']}/drafts", {}),
        ("PATCH", f"/drafts/{d['id']}", {"subject": "x"}),
        ("POST", f"/drafts/{d['id']}/send", None),
        ("POST", f"/drafts/{d['id']}/cancel", None),
        ("POST", f"/follow-up/schedules/{s['id']}/cancel", {}),
        ("POST", f"/follow-up/schedules/{s['id']}/reschedule", {"due_at": (utcnow() + timedelta(days=2)).isoformat()}),
        ("POST", f"/quotes/{q['id']}/follow-up/plan", None),
        ("POST", "/jobs/run-once", None),
        ("POST", "/integrations", {"provider": "simulator"}),
        ("POST", f"/integrations/{conn['id']}/revoke", None),
        ("POST", "/integrations/sync-inbound", None),
        (
            "POST",
            "/integrations/simulator/inbound",
            {"from_email": c["email"], "subject": "x", "snippet": "unsubscribe"},
        ),
    ]
    for method, path, body in mutations:
        r = (readonly.post if method == "POST" else readonly.patch)(path, json=body)
        assert r.status_code == 403 and r.json()["error"] == "forbidden", (method, path, r.status_code, r.text)
    assert readonly.get("/jobs").status_code == 403 and readonly.get("/integrations").status_code == 403
    admin_only = [
        m for m in mutations if m[1].startswith(("/follow-up/policy", "/follow-up/templates", "/jobs", "/integrations"))
    ]
    for method, path, body in admin_only:
        r = (sales.post if method == "POST" else sales.patch)(path, json=body)
        assert r.status_code == 403, (method, path, r.status_code, r.text)
    assert sales.get("/jobs").status_code == 403 and sales.get("/integrations").status_code == 403
    assert sales.get("/follow-up/policy").status_code == 200 and sales.get("/follow-up/templates").status_code == 200
    # opt-in is owner/admin only, opt-out is available to every editor
    assert sales.post(f"/contacts/{c['id']}/opt-out", json={"opted_out": True}).status_code == 200
    assert sales.post(f"/contacts/{c['id']}/opt-out", json={"opted_out": False}).status_code == 403
    assert (
        login_as(app, tenant_a.users["approver"])
        .post(f"/contacts/{c['id']}/opt-out", json={"opted_out": False})
        .status_code
        == 403
    )
    assert admin.post(f"/contacts/{c['id']}/opt-out", json={"opted_out": False}).status_code == 200
    # none of the denied calls had a side effect
    assert simulator_for(tenant_a.org_id).outbox == []
    assert sales.get(f"/drafts/{d['id']}").json()["status"] == "draft"
    # the one effective call above was the sales opt-out: it suppressed the pending schedules and an admin opt-in
    # does not silently revive them (conservative by design)
    assert {s["status"] for s in sales.get(f"/follow-up/schedules?status=all&quote_id={q['id']}").json()} == {
        "suppressed"
    }
    assert sales.get(f"/quotes/{q['id']}/lifecycle").json()["status"] == "sent"
    assert "contact.opt_out" in audit_actions(tenant_a.org_id)


def test_csrf_is_enforced_on_stage2_mutations(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, admin, q, c = sent_quote_with_contact(app, tenant_a)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    s = sales.get(f"/follow-up/schedules?quote_id={q['id']}").json()[0]
    cases = [
        (sales, "POST", f"/drafts/{d['id']}/send", None),
        (sales, "PATCH", f"/drafts/{d['id']}", {"subject": "pwn"}),
        (sales, "POST", f"/contacts/{c['id']}/opt-out", {"opted_out": True}),
        (
            sales,
            "POST",
            f"/quotes/{q['id']}/transition",
            {"expected_lock_version": q["lock_version"], "to_status": "won", "evidence": "x"},
        ),
        (sales, "POST", f"/follow-up/schedules/{s['id']}/cancel", {}),
        (admin, "PATCH", "/follow-up/policy", {"automation_enabled": True}),
        (admin, "POST", "/integrations", {"provider": "simulator"}),
        (admin, "POST", "/jobs/run-once", None),
        (
            admin,
            "POST",
            "/integrations/simulator/inbound",
            {"from_email": c["email"], "subject": "x", "snippet": "unsubscribe"},
        ),
    ]
    for ac, method, path, body in cases:
        for headers in ({}, {"X-CSRF-Token": "not-the-token"}):
            r = ac.client.request(method, f"{API}{path}", json=body, headers=headers)
            assert r.status_code == 403 and r.json().get("action") == "csrf", (method, path, headers, r.text)
    assert simulator_for(tenant_a.org_id).outbox == []
    assert sales.get(f"/drafts/{d['id']}").json()["status"] == "draft"
    assert sales.get(f"/quotes/{q['id']}/lifecycle").json()["status"] == "sent"
    assert admin.get("/follow-up/policy").json()["automation_enabled"] is False
    assert sales.get(f"/customers/{tenant_a.customer_id}/contacts").json()[0]["opted_out_at"] is None


def test_every_stage2_mutation_writes_an_audit_row_in_the_same_transaction(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, admin, q, c = sent_quote_with_contact(app, tenant_a)
    c2 = add_contact(sales, str(tenant_a.customer_id), email="second@customer.example", primary=False)
    assert sales.patch(f"/contacts/{c2['id']}", json={"full_name": "Second Person"}).status_code == 200
    assert admin.patch("/follow-up/policy", json={"sender_name": "Sara"}).status_code == 200
    tmpl = admin.get("/follow-up/templates").json()[0]
    assert admin.patch(f"/follow-up/templates/{tmpl['id']}", json={"name": "Renamed"}).status_code == 200
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    assert sales.patch(f"/drafts/{d['id']}", json={"subject": "Edited"}).status_code == 200
    assert sales.post(f"/drafts/{d['id']}/send").status_code == 200
    d2 = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c2["id"]}).json()
    assert sales.post(f"/drafts/{d2['id']}/cancel").status_code == 200
    d3 = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c2["id"]}).json()
    assert sales.post(f"/contacts/{c2['id']}/opt-out", json={"opted_out": True}).status_code == 200
    assert sales.post(f"/drafts/{d3['id']}/send").status_code == 403
    assert admin.post(f"/contacts/{c2['id']}/opt-out", json={"opted_out": False}).status_code == 200
    scheds = sales.get(f"/follow-up/schedules?quote_id={q['id']}").json()
    assert sales.post(f"/follow-up/schedules/{scheds[0]['id']}/cancel", json={"reason": "audit"}).status_code == 200
    assert (
        sales.post(
            f"/follow-up/schedules/{scheds[1]['id']}/reschedule",
            json={"due_at": (utcnow() + timedelta(days=9)).isoformat()},
        ).status_code
        == 200
    )
    conn = admin.post("/integrations", json={"provider": "simulator"}).json()
    assert admin.post(f"/integrations/{conn['id']}/revoke").status_code == 200
    assert (
        admin.post(
            "/integrations/simulator/inbound",
            json={"from_email": c["email"], "subject": f"Re: {q['number']}", "snippet": "thanks"},
        ).json()["kind"]
        == "reply_received"
    )
    assert (
        admin.post(
            "/integrations/simulator/inbound", json={"from_email": c["email"], "subject": "x", "snippet": "unsubscribe"}
        ).json()["kind"]
        == "opt_out"
    )
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        job = queue.enqueue(
            db, org_id=tenant_a.org_id, kind="no_such_kind", idempotency_key="dead", payload={}, max_attempts=1
        )
        assert job is not None
        job_id = str(job.id)
        db.commit()
    worker_tick()
    assert admin.get("/jobs?status=dead").json()[0]["id"] == job_id
    assert admin.post(f"/jobs/{job_id}/retry").json()["status"] == "queued"
    actions = set(audit_actions(tenant_a.org_id))
    expected = {
        "contact.create",
        "contact.update",
        "contact.opt_out",
        "contact.opt_in",
        "followup.policy_update",
        "followup.template_update",
        "quote.transition",
        "followup.schedule",
        "draft.create",
        "draft.update",
        "draft.sent",
        "draft.cancel",
        "draft.suppressed",
        "followup.cancel",
        "followup.reschedule",
        "job.retry",
        "integration.connect",
        "integration.revoke",
        "inbound.reply_received",
        "inbound.opt_out",
    }
    assert expected <= actions, sorted(expected - actions)
    # audit rows carry the actor and never the message body or the recipient address
    rows = sql(tenant_a.org_id, "SELECT action, actor_user_id, summary::text FROM audit_events")
    assert all(
        r[1] is not None
        for r in rows
        if r[0].startswith(("contact.", "draft.update", "draft.sent", "job.", "integration."))
    )
    assert not any("@customer.example" in r[2] or "Dear " in r[2] for r in rows)


def test_sync_inbound_and_run_once_write_audit_rows(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    before = len(audit_actions(tenant_a.org_id))
    assert admin.post("/integrations/sync-inbound").status_code == 200
    assert admin.get("/integrations").json()[0]["provider"] == "simulator"  # a row was created by the call
    assert admin.post("/jobs/run-once").status_code == 200
    assert len(audit_actions(tenant_a.org_id)) >= before + 2


# ================================================================ 3. consent and suppression on every send path
def test_opt_out_follows_the_address_when_a_contact_is_recreated(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, admin, q, c = sent_quote_with_contact(app, tenant_a)
    r = admin.post(
        "/integrations/simulator/inbound",
        json={"from_email": c["email"], "subject": f"Re: {q['number']}", "snippet": "unsubscribe"},
    )
    assert r.status_code == 201 and r.json()["kind"] == "opt_out"
    assert sales.get(f"/customers/{tenant_a.customer_id}/contacts").json()[0]["opted_out_at"]
    c2 = add_contact(sales, str(tenant_a.customer_id), email=c["email"], primary=False)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c2["id"]})
    r = sales.post(f"/drafts/{d.json()['id']}/send") if d.status_code == 201 else d
    assert r.status_code == 403, r.text
    assert simulator_for(tenant_a.org_id).outbox == []


def test_unsubscribe_reply_opts_out_every_contact_row_with_that_address(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, admin, q, c = sent_quote_with_contact(app, tenant_a)
    add_contact(
        sales, str(tenant_a.customer_id), email=c["email"].upper(), primary=False
    )  # same mailbox, as imports do
    r = admin.post(
        "/integrations/simulator/inbound",
        json={"from_email": c["email"], "subject": "x", "snippet": "please unsubscribe"},
    )
    assert r.json()["kind"] == "opt_out"
    rows = sales.get(f"/customers/{tenant_a.customer_id}/contacts").json()
    assert len(rows) == 2 and all(row["opted_out_at"] for row in rows), [
        (row["email"], row["opted_out_at"]) for row in rows
    ]


def test_every_sent_message_carries_the_opt_out_instruction(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, admin, q, c = sent_quote_with_contact(app, tenant_a)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    assert "UNSUBSCRIBE" in d["body"]
    r = sales.patch(f"/drafts/{d['id']}", json={"body": "Hi, just checking in on {quote_number}."})
    assert r.status_code == 200 and "UNSUBSCRIBE" not in r.json()["body"]
    assert sales.post(f"/drafts/{d['id']}/send").status_code == 200
    msg, _ = simulator_for(tenant_a.org_id).outbox[0]
    assert "UNSUBSCRIBE" in msg.body_text


def test_automation_consent_is_rechecked_at_send_time(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, admin, q, c = sent_quote_with_contact(app, tenant_a, automation=True)
    assert all(s["mode"] == "auto_send" for s in sales.get(f"/follow-up/schedules?quote_id={q['id']}").json())
    pol = admin.patch("/follow-up/policy", json={"automation_enabled": False}).json()
    assert pol["automation_enabled"] is False and pol["automation_consented_at"] is None
    make_due(tenant_a.org_id, q["id"], 1)
    worker_tick()
    for _ in range(6):
        force_jobs_due(tenant_a.org_id)
        worker_tick()
    assert simulator_for(tenant_a.org_id).outbox == []
    assert "draft.sent" not in audit_actions(tenant_a.org_id)
    assert all(d["status"] != "sent" for d in sales.get("/drafts").json())
    jobs = admin.get("/jobs").json()
    # Fix round 2 (QA D-13): a schedule planned as auto_send whose consent was withdrawn degrades to draft-only. The
    # job therefore succeeds by preparing a draft for a human; the security property — nothing leaves without
    # consent — is asserted above (empty outbox, no draft.sent audit, no sent draft). Edited by the implementer;
    # the original line required that no job succeeds.
    assert jobs and all(j["status"] == "succeeded" for j in jobs if j["kind"] == "schedule_followup")
    assert all(
        s["mode"] == "draft_only" and s["status"] != "sent"
        for s in sales.get(f"/follow-up/schedules?status=all&quote_id={q['id']}").json()
        if s["attempt_no"] == 1
    )
    # the consent record itself is audited with the actor
    rows = sql(
        tenant_a.org_id, "SELECT actor_user_id, summary::text FROM audit_events WHERE action = 'followup.policy_update'"
    )
    assert len(rows) == 2 and all(r[0] is not None for r in rows) and '"automation_enabled": true' in rows[0][1]


def test_opt_out_is_enforced_on_the_queued_retry_path(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, admin, q, c = sent_quote_with_contact(app, tenant_a, automation=True)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        assert queue.enqueue(
            db,
            org_id=tenant_a.org_id,
            kind="send_draft",
            idempotency_key=f"retry:{d['id']}",
            payload={"draft_id": d["id"]},
        )
        db.commit()
    # the customer opts out while the job is already queued
    assert sales.post(f"/contacts/{c['id']}/opt-out", json={"opted_out": True}).status_code == 200
    for _ in range(3):
        force_jobs_due(tenant_a.org_id)
        worker_tick()
    assert simulator_for(tenant_a.org_id).outbox == []
    assert sales.get(f"/drafts/{d['id']}").json()["status"] == "cancelled"
    assert "draft.suppressed" in audit_actions(tenant_a.org_id) and "draft.sent" not in audit_actions(tenant_a.org_id)
    # admin re-queue of the dead/failed job does not resurrect the send either
    for j in admin.get("/jobs").json():
        if j["status"] in ("dead", "failed"):
            assert admin.post(f"/jobs/{j['id']}/retry").status_code == 200
    force_jobs_due(tenant_a.org_id)
    worker_tick()
    assert simulator_for(tenant_a.org_id).outbox == []


def test_hourly_limit_recipient_binding_and_provider_acknowledgement(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, admin, q, c = sent_quote_with_contact(app, tenant_a)
    assert admin.patch("/follow-up/policy", json={"hourly_send_limit": 1}).status_code == 200
    d1 = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    d2 = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    assert sales.post(f"/drafts/{d1['id']}/send").status_code == 200
    r = sales.post(f"/drafts/{d2['id']}/send")
    assert r.status_code == 429 and r.json()["error"] == "rate_limited"
    assert len(simulator_for(tenant_a.org_id).outbox) == 1
    assert admin.patch("/follow-up/policy", json={"hourly_send_limit": 50}).status_code == 200
    # a provider failure after the gate never produces a 'sent' fact
    sim = simulator_for(tenant_a.org_id)
    sim.fail_next = [providers.ProviderError("timeout", retryable=True)]
    r = sales.post(f"/drafts/{d2['id']}/send")
    assert r.status_code == 503 and sales.get(f"/drafts/{d2['id']}").json()["status"] == "draft"
    assert [e["kind"] for e in sales.get(f"/communication-events?quote_id={q['id']}").json()] == ["sent"]
    assert sql(tenant_a.org_id, "SELECT count(*) FROM communication_events WHERE kind = 'sent'")[0][0] == 1
    # recipient must be the draft's contact on record; a contact e-mail change after drafting is caught
    assert (
        sales.patch(
            f"/contacts/{c['id']}", json={"full_name": c["full_name"], "email": "moved@customer.example"}
        ).status_code
        == 200
    )
    r = sales.post(f"/drafts/{d2['id']}/send")
    assert r.status_code == 422 and len(sim.outbox) == 1


def test_skip_locked_claim_hands_a_job_to_exactly_one_worker(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        job = queue.enqueue(db, org_id=tenant_a.org_id, kind="sync_inbound", idempotency_key="lock", payload={})
        assert job is not None
        job_id = job.id
        db.commit()
    holder = get_sessionmaker()()
    try:
        set_worker_scope(holder)
        locked = holder.execute(
            text("SELECT id FROM job_runs WHERE id = :j FOR UPDATE SKIP LOCKED"), {"j": job_id}
        ).scalar()
        assert locked == job_id  # worker 1 holds the row lock inside an open transaction
        with get_sessionmaker()() as other:
            assert queue.claim_next(other, "worker-2") is None  # worker 2 skips it instead of blocking
        holder.rollback()
        with get_sessionmaker()() as other:
            claimed = queue.claim_next(other, "worker-2")
            assert claimed is not None and claimed.id == job_id and claimed.attempts == 1
            assert queue.claim_next(other, "worker-3") is None  # already running
    finally:
        holder.close()


def test_dead_lettered_schedules_are_resolved_and_do_not_starve_other_tenants(
    app, tenant_a: Tenant, tenant_b: Tenant
) -> None:  # type: ignore[no-untyped-def]
    sales, admin, q, c = sent_quote_with_contact(app, tenant_a, automation=True)
    admin.patch("/follow-up/policy", json={"automation_enabled": False})
    make_due(tenant_a.org_id, q["id"], 1)
    worker_tick()
    for _ in range(6):
        force_jobs_due(tenant_a.org_id)
        worker_tick()
    assert simulator_for(tenant_a.org_id).outbox == []
    # Fix round 2 (QA D-13): consent withdrawal no longer dead-letters the job (the schedule becomes a draft); the
    # starvation scenario below still forces 200 dead jobs. Edited by the implementer; the original line expected a
    # dead job here.
    assert all(j["status"] != "dead" for j in admin.get("/jobs").json())
    with get_sessionmaker()() as db:
        assert queue.enqueue_due_schedules(db) == 0  # the schedule is no longer pending → nothing to enqueue
    # the same state, 200 times, blocks the tick for a second tenant with a legitimately due follow-up
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        for i in range(200):
            db.add(
                FollowUpSchedule(
                    organization_id=tenant_a.org_id,
                    quote_id=uuid.UUID(q["id"]),
                    attempt_no=10 + i,
                    due_at=utcnow() - timedelta(days=2),
                    status="pending",
                    mode="draft_only",
                    idempotency_key=f"stale:{i}",
                    reason="audit fixture: dead-lettered schedule",
                )
            )
        db.commit()
        assert queue.enqueue_due_schedules(db) == 200
    sql(tenant_a.org_id, "UPDATE job_runs SET status = 'dead' WHERE kind = 'schedule_followup' AND status = 'queued'")
    _, _, qb, _ = sent_quote_with_contact(app, tenant_b, email="buyer@beta.example")
    make_due(tenant_b.org_id, qb["id"], 1)
    with get_sessionmaker()() as db:
        enqueued_for_b = queue.enqueue_due_schedules(db)
    first = next(
        s for s in sales.get(f"/follow-up/schedules?status=all&quote_id={q['id']}").json() if s["attempt_no"] == 1
    )
    assert first["status"] != "pending", first  # the dead-lettered schedule should be suppressed/failed, not pending
    assert enqueued_for_b >= 1


# ================================================================ 4. inbound processing
def test_inbound_processing_cannot_reach_other_tenants_contacts_or_quotes(
    app, tenant_a: Tenant, tenant_b: Tenant
) -> None:  # type: ignore[no-untyped-def]
    a_sales, a_admin, qa, ca = sent_quote_with_contact(app, tenant_a)
    b_sales, b_admin, qb, cb = sent_quote_with_contact(app, tenant_b, email="buyer@beta.example")
    assert qa["number"] == qb["number"]  # per-tenant sequences collide by design: linking must stay tenant-scoped
    r = a_admin.post(
        "/integrations/simulator/inbound",
        json={"from_email": cb["email"], "subject": f"Re: {qb['number']}", "snippet": "unsubscribe"},
    )
    assert r.status_code == 201 and r.json()["kind"] == "opt_out"
    assert r.json()["quote_id"] in (None, qa["id"])
    assert b_sales.get(f"/customers/{tenant_b.customer_id}/contacts").json()[0]["opted_out_at"] is None
    assert b_sales.get(f"/quotes/{qb['id']}/lifecycle").json()["status"] == "sent"
    assert all(s["status"] == "pending" for s in b_sales.get(f"/follow-up/schedules?quote_id={qb['id']}").json())
    assert b_sales.get("/communication-events").json() == []
    assert sql(tenant_a.org_id, "SELECT contact_id FROM communication_events WHERE kind = 'opt_out'")[0][0] is None
    # B's admin cannot replay A's thread ids either
    sent = a_sales.post(f"/quotes/{qa['id']}/drafts", json={"contact_id": ca["id"]}).json()
    assert a_sales.post(f"/drafts/{sent['id']}/send").status_code == 200
    pmid = a_sales.get(f"/drafts/{sent['id']}").json()["provider_message_id"]
    r = b_admin.post(
        "/integrations/simulator/inbound",
        json={"from_email": ca["email"], "subject": "Re:", "snippet": "ok", "in_reply_to": pmid},
    )
    assert r.status_code == 201 and r.json()["quote_id"] is None
    assert a_sales.get(f"/quotes/{qa['id']}/lifecycle").json()["status"] == "sent"


def test_inbound_from_an_unknown_address_does_not_change_quote_status(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, admin, q, c = sent_quote_with_contact(app, tenant_a)
    r = admin.post(
        "/integrations/simulator/inbound",
        json={
            "from_email": "stranger@elsewhere.example",
            "subject": f"FW: {q['number']} looks interesting",
            "snippet": "forwarded for information",
        },
    )
    assert r.status_code == 201
    lc = sales.get(f"/quotes/{q['id']}/lifecycle").json()
    assert lc["status"] == "sent", [e["evidence"] for e in lc["events"]]


def test_simulator_inbound_is_admin_only_and_bounded(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, admin, q, c = sent_quote_with_contact(app, tenant_a)
    assert (
        sales.post(
            "/integrations/simulator/inbound", json={"from_email": c["email"], "subject": "x", "snippet": "unsubscribe"}
        ).status_code
        == 403
    )
    assert (
        admin.post(
            "/integrations/simulator/inbound", json={"from_email": c["email"], "subject": "x" * 301, "snippet": "y"}
        ).status_code
        == 422
    )
    assert (
        admin.post(
            "/integrations/simulator/inbound", json={"from_email": c["email"], "subject": "x", "snippet": "y" * 501}
        ).status_code
        == 422
    )
    assert sales.get(f"/customers/{tenant_a.customer_id}/contacts").json()[0]["opted_out_at"] is None


# ================================================================ 5. secrets and logging
def test_integration_secret_is_never_exposed_and_revocation_wipes_it(
    app, tenant_a: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    secret = "hunter2-smtp-app-password"
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", base64.b64encode(os.urandom(16)).decode())  # wrong length
    config.get_settings.cache_clear()
    r = admin.post("/integrations", json={"provider": "smtp", "secret": secret})
    assert r.status_code == 422 and "32 bytes" in r.text and secret not in r.text
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
    config.get_settings.cache_clear()
    r = admin.post(
        "/integrations", json={"provider": "smtp", "secret": secret, "account_email": "sales@alphatrading.example"}
    )
    assert (
        r.status_code == 201
        and secret not in r.text
        and "encrypted_secret" not in r.json()
        and "secret" not in r.json()
    )
    assert secret not in admin.get("/integrations").text
    stored = sql(tenant_a.org_id, "SELECT encrypted_secret FROM integration_connections WHERE provider = 'smtp'")[0][0]
    blob = base64.b64decode(stored)
    assert secret.encode() not in blob and blob[:1] == b"\x01" and len(blob) > 1 + 12 + 16
    assert (
        sql(tenant_a.org_id, "SELECT count(*) FROM audit_events WHERE summary::text ILIKE :s", s=f"%{secret}%")[0][0]
        == 0
    )
    assert (
        sql(tenant_a.org_id, "SELECT summary::text FROM audit_events WHERE action = 'integration.connect'")[0][0].count(
            "has_secret"
        )
        == 1
    )
    from app.services.comms.secrets import decrypt

    assert decrypt(stored, aad=f"{tenant_a.org_id}:smtp") == secret
    with pytest.raises(Exception):  # noqa: B017 - tenant/provider binding (AAD) must be authenticated
        decrypt(stored, aad=f"{uuid.uuid4()}:smtp")
    conn = admin.get("/integrations").json()[0]
    assert admin.post(f"/integrations/{conn['id']}/revoke").json()["status"] == "revoked"
    assert (
        sql(tenant_a.org_id, "SELECT encrypted_secret FROM integration_connections WHERE provider = 'smtp'")[0][0]
        is None
    )
    # reconnecting another provider revokes the rest (one active provider per tenant)
    assert admin.post("/integrations", json={"provider": "simulator"}).status_code == 201
    assert {c["provider"]: c["status"] for c in admin.get("/integrations").json()} == {
        "smtp": "revoked",
        "simulator": "active",
    }
    assert redact({"secret": "x", "nested": {"api_key": "y", "ok": "a@b.example"}}) == {
        "secret": "[REDACTED]",
        "nested": {"api_key": "[REDACTED]", "ok": "[EMAIL]"},
    }


@pytest.mark.xfail(
    strict=True,
    reason="S2-SEC-06: the envelope-encrypted per-tenant secret is write-only — provider_for() builds the SMTP provider "
    "from the process-wide SMTP_* settings and no product code ever calls decrypt(); every tenant shares one credential",
)
def test_smtp_provider_uses_the_tenants_stored_credential(
    app, tenant_a: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    _smtp_env(monkeypatch)
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
    monkeypatch.setenv("SMTP_USERNAME", "shared-env-user")
    monkeypatch.setenv("SMTP_PASSWORD", "shared-env-password")
    config.get_settings.cache_clear()
    admin = login_as(app, tenant_a.users["admin"])
    assert (
        admin.post(
            "/integrations",
            json={"provider": "smtp", "secret": "tenant-a-only-password", "account_email": "a@alphatrading.example"},
        ).status_code
        == 201
    )
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        org = db.get(Organization, tenant_a.org_id)
        assert org is not None
        provider = fu.provider_for(db, org)
        assert isinstance(provider, SMTPProvider)
        assert provider.password == "tenant-a-only-password"


def test_send_path_logs_contain_no_addresses_bodies_or_secrets(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        sales, admin, q, c = sent_quote_with_contact(app, tenant_a)
        d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
        assert sales.post(f"/drafts/{d['id']}/send").status_code == 200
        assert (
            admin.post(
                "/integrations/simulator/inbound",
                json={
                    "from_email": c["email"],
                    "subject": f"Re: {q['number']}",
                    "snippet": "Thanks, reviewing with my manager",
                },
            ).status_code
            == 201
        )
        logging.getLogger("audit").info(
            "password=%s token=%s",
            "hunter2",
            "eyJabcdefghijklmnopqrstuvwxyz0123456789.sig",
            extra={"extra": {"api_key": "sk-ant-api03-abcdefghijklmnop", "note": f"ask {c['email']}"}},
        )
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
    out = buf.getvalue()
    assert '"msg": "follow-up sent"' in out and '"msg": "request"' in out
    assert "@" not in out.replace("[EMAIL]", ""), out[:2000]
    assert "Dear Buyer" not in out and "UNSUBSCRIBE" not in out and "reviewing with my manager" not in out
    assert (
        "sk-ant-api03" not in out
        and "eyJabc" not in out
        and '"api_key": "[REDACTED]"' in out
        and '"note": "ask [EMAIL]"' in out
    )
    # observation (not a finding): redaction is key- and pattern-based; a caller that logged a `body`/`snippet` key
    # would not be masked — the product never does (grep: no logger call passes message content)
    assert redact({"body": "Dear X"}) == {"body": "Dear X"}
    assert not re.search(
        r"logger\.\w+\([^)]*(body|snippet|subject|to_email)",
        (APP_DIR / "services" / "followup" / "service.py").read_text(),
    )
    # the sent draft body/addresses are in the DB only; the log line about the send carries ids
    line = next(ln for ln in out.splitlines() if "follow-up sent" in ln)
    assert d["id"] in line and str(tenant_a.org_id) in line and c["email"] not in line


# ================================================================ 6. templates and SMTP headers
def test_templates_whitelist_placeholders_and_values_are_not_re_expanded(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    hostile = "{grand_total} <script>alert(1)</script> {org_name}"
    sales, admin, q, c = sent_quote_with_contact(app, tenant_a, name=hostile)
    tmpl = next(t for t in admin.get("/follow-up/templates").json() if t["key"] == "follow_up_1")
    assert (
        admin.patch(f"/follow-up/templates/{tmpl['id']}", json={"body": "Hi {contact_name} {evil}"}).status_code == 422
    )
    assert admin.patch(f"/follow-up/templates/{tmpl['id']}", json={"body": "Hi <script>x</script>"}).status_code == 422
    assert admin.patch(f"/follow-up/templates/{tmpl['id']}", json={"subject": "javascript:alert(1)"}).status_code == 422
    assert (
        admin.patch(
            f"/follow-up/templates/{tmpl['id']}", json={"subject": "Hello {contact_name} about {quote_number}"}
        ).status_code
        == 200
    )
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"], "template_key": "follow_up_1"}).json()
    # single-pass substitution: placeholders inside customer data stay literal and script text is inert plain text
    assert hostile in d["body"] and hostile in d["subject"]
    assert sales.patch(f"/drafts/{d['id']}", json={"body": "x" * 20001}).status_code == 422
    assert sales.patch(f"/drafts/{d['id']}", json={"body": "<script>alert(1)</script>"}).status_code == 422
    assert (
        sales.patch(f"/drafts/{d['id']}", json={"subject": "A\r\nBcc: x@y.example"}).json()["subject"]
        == "A  Bcc: x@y.example"
    )
    # unknown template keys are tenant-scoped lookups, not file paths
    assert (
        sales.post(
            f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"], "template_key": "../../etc/passwd"}
        ).status_code
        == 404
    )
    assert sales.post(f"/drafts/{d['id']}/send").status_code == 200
    msg, _ = simulator_for(tenant_a.org_id).outbox[0]
    assert hostile in msg.body_text and "UNSUBSCRIBE" in msg.body_text


def test_smtp_headers_resist_crlf_injection_from_customer_data(
    app, tenant_a: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    _smtp_env(monkeypatch)
    sales, admin, q, c = sent_quote_with_contact(
        app, tenant_a, name="Buyer\r\nBcc: victim@evil.example\r\nX-Injected: 1"
    )
    tmpl = next(t for t in admin.get("/follow-up/templates").json() if t["key"] == "follow_up_1")
    assert (
        admin.patch(
            f"/follow-up/templates/{tmpl['id']}", json={"subject": "Hello {contact_name} about {quote_number}"}
        ).status_code
        == 200
    )
    assert (
        admin.patch(
            "/follow-up/policy", json={"reply_to_email": "sales@alphatrading.example\r\nBcc: x@evil.example"}
        ).status_code
        == 422
    )
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"], "template_key": "follow_up_1"}).json()
    assert "\r\n" in d["subject"]  # the generated subject carries the hostile name verbatim
    r = sales.post(f"/drafts/{d['id']}/send")
    assert r.status_code == 200, r.text
    assert len(FakeSMTP.sent) == 1
    msg = FakeSMTP.sent[0]
    headers = msg.as_bytes().split(b"\n\n", 1)[0]  # header block only; the body legitimately echoes the name
    assert msg.get_all("Bcc") is None and msg.get_all("X-Injected") is None
    assert b"\nBcc:" not in headers and b"\nX-Injected:" not in headers
    assert all(line.startswith((b" ", b"\t")) or b":" in line for line in headers.split(b"\n"))
    assert "\r" not in str(msg["Subject"]) and "\n" not in str(msg["Subject"])
    assert msg["To"] == c["email"] and msg["X-QuotePilot-Quote"] == q["number"] and msg["X-QuotePilot-Draft"] == d["id"]
    assert msg.get_content_type() == "text/plain" and not msg.is_multipart()
    assert "UNSUBSCRIBE" in msg.get_content()
    assert r.json()["provider"] == "smtp" and r.json()["provider_message_id"] == str(msg["Message-ID"])


def test_crlf_in_admin_sender_name_cannot_inject_headers(
    app, tenant_a: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    _smtp_env(monkeypatch)
    sales, admin, q, c = sent_quote_with_contact(app, tenant_a)
    assert admin.patch("/follow-up/policy", json={"sender_name": "Sara\r\nBcc: x@evil.example"}).status_code in (
        200,
        422,
    )
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    r = sales.post(f"/drafts/{d['id']}/send")
    for msg in FakeSMTP.sent:
        assert msg.get_all("Bcc") is None and b"\nBcc:" not in msg.as_bytes()
    status = sales.get(f"/drafts/{d['id']}").json()["status"]
    assert (r.status_code == 200 and status == "sent") or (
        r.status_code != 200 and status != "sent" and not FakeSMTP.sent
    )
    assert r.status_code != 500, "S2-SEC-10: CRLF in the policy sender name must not crash the send path"


def test_contact_addresses_with_a_trailing_newline_are_rejected(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    r = sales.post(
        f"/customers/{tenant_a.customer_id}/contacts", json={"full_name": "x", "email": "buyer@customer.example\n"}
    )
    assert r.status_code == 422, r.text


# ================================================================ 7. development-only endpoints and queue hygiene
def test_dev_only_endpoints_are_disabled_in_production(tenant_a: Tenant, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app_for_env(monkeypatch, "production")
    try:
        admin = login_as(app, tenant_a.users["admin"])
        r = admin.post("/jobs/run-once")
        assert r.status_code == 403 and r.json()["action"] == "worker"
        r = admin.post(
            "/integrations/simulator/inbound",
            json={"from_email": "a@b.example", "subject": "x", "snippet": "unsubscribe"},
        )
        assert r.status_code == 403 and r.json()["action"] == "simulator"
        assert (
            TestClient(app).get(f"{API}/docs").status_code == 404
            and TestClient(app).get(f"{API}/openapi.json").status_code == 404
        )
    finally:
        config.get_settings.cache_clear()


def test_dev_only_endpoints_are_disabled_in_staging(tenant_a: Tenant, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app_for_env(monkeypatch, "staging")
    try:
        admin = login_as(app, tenant_a.users["admin"])
        assert admin.post("/jobs/run-once").status_code == 403
    finally:
        config.get_settings.cache_clear()


def test_job_payloads_hold_ids_only_and_unknown_kinds_dead_letter_boundedly(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, admin, q, c = sent_quote_with_contact(app, tenant_a)
    make_due(tenant_a.org_id, q["id"])
    worker_tick()
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        job = queue.enqueue(
            db,
            org_id=tenant_a.org_id,
            kind="exfil",
            idempotency_key="exfil",
            payload={"draft_id": str(uuid.uuid4())},
            max_attempts=2,
        )
        assert job is not None
        db.commit()
    for _ in range(4):
        force_jobs_due(tenant_a.org_id)
        worker_tick()
    rows = sql(tenant_a.org_id, "SELECT kind, status, attempts, max_attempts, payload::text, last_error FROM job_runs")
    assert rows
    for kind, status, attempts, max_attempts, payload, last_error in rows:
        assert set(re.findall(r'"(\w+)":', payload)) <= {"schedule_id", "draft_id", "idempotency_key"}, payload
        assert "@" not in payload and "Dear" not in payload
        assert attempts <= max_attempts
        if kind == "exfil":
            assert status == "dead" and attempts == 2 and last_error and "Traceback" not in last_error
    assert all(j["status"] in ("succeeded", "dead") for j in admin.get("/jobs").json())
    # JobOut exposes no payload and no lock internals to the UI
    assert set(admin.get("/jobs").json()[0]) == {
        "id",
        "kind",
        "status",
        "attempts",
        "max_attempts",
        "next_run_at",
        "last_error",
        "finished_at",
        "created_at",
    }
    # retry resets the attempt budget but keeps the tenant binding: a retried foreign kind dies again, nothing else runs
    dead = admin.get("/jobs?status=dead").json()[0]
    assert admin.post(f"/jobs/{dead['id']}/retry").json()["attempts"] == 0
    assert admin.post(f"/jobs/{dead['id']}/retry").status_code == 409  # only dead/failed jobs can be retried


def test_security_headers_and_no_store_on_stage2_routes(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    for path in ("/follow-up/policy", "/drafts", "/follow-up/schedules", "/communication-events"):
        r = sales.get(path)
        assert r.status_code == 200, path
        assert r.headers["Cache-Control"] == "no-store" and r.headers["X-Content-Type-Options"] == "nosniff"
        assert (
            r.headers["X-Frame-Options"] == "DENY"
            and r.headers["Content-Security-Policy"] == "default-src 'none'; frame-ancestors 'none'"
        )
        assert re.fullmatch(r"[A-Za-z0-9\-_.]{8,64}", r.headers["X-Request-ID"])
    # error bodies for Stage 2 provider failures carry no stack traces or hostnames
    r = sales.post(f"/drafts/{uuid.uuid4()}/send")
    assert r.status_code == 404 and set(r.json()) == {"error", "detail", "request_id"}
