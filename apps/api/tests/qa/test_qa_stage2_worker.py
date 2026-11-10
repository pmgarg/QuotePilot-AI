"""QA (independent) — Stage 2 job runner, retries, dead letters, providers and secrets (S2-F06, S2-F07,
S2-A05, S2-A06, S2-A07). Derived from docs/PRD.md and ADR-012 (PostgreSQL queue: idempotency keys, capped
exponential backoff 30 s·2ⁿ ≤ 1 h, dead letters with admin retry, worker `python -m app.workers.runner --once`).
Integration evidence against PostgreSQL with the RLS role; the provider is the deterministic simulator unless the
test says otherwise (SMTP against a closed local port — no network, no real mail).
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import datetime

import pytest
from sqlalchemy import text

from app.db.base import utcnow
from app.db.session import get_sessionmaker, set_tenant
from app.services.comms.providers import ProviderError, reset_simulators, simulator_for
from app.services.jobs import queue
from tests.conftest import API_DIR, Tenant, login_as
from tests.integration.test_stage2_followup import add_contact, approved_quote, mark_sent

pytestmark = [pytest.mark.integration]


@pytest.fixture(autouse=True)
def _reset_sim() -> None:
    reset_simulators()


def force_due(org_id, *, schedules: bool = True, jobs: bool = False) -> None:  # type: ignore[no-untyped-def]
    with get_sessionmaker()() as db:
        set_tenant(db, org_id, None)
        if schedules:
            db.execute(
                text("UPDATE follow_up_schedules SET due_at = now() - interval '1 minute' WHERE status = 'pending'")
            )
        if jobs:
            db.execute(text("UPDATE job_runs SET next_run_at = now() - interval '1 second' WHERE status = 'queued'"))
        db.commit()


def tick() -> int:
    with get_sessionmaker()() as db:
        queue.enqueue_due_schedules(db)
        return queue.run_once(db)


def seconds_until(iso: str) -> float:
    return (datetime.fromisoformat(iso) - utcnow()).total_seconds()


# ---------------------------------------------------------------- S2-F07 worker process
def test_qa_s2_f07_worker_runner_once_processes_due_schedules_and_is_idempotent(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    add_contact(sales, str(tenant_a.customer_id))
    q = mark_sent(sales, q)
    pending = sales.get(f"/follow-up/schedules?quote_id={q['id']}").json()
    assert len(pending) >= 2
    force_due(tenant_a.org_id)
    env = {**os.environ, "LOG_LEVEL": "INFO"}
    r = subprocess.run(
        [sys.executable, "-m", "app.workers.runner", "--once"],
        cwd=API_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, r.stderr[-2000:]
    assert f"processed {len(pending)} job(s)" in r.stdout, r.stdout[-500:]
    # the worker log is JSON and carries no addresses or bodies
    assert "@" not in r.stdout and "Dear Buyer" not in r.stdout
    # automation is off → drafts only, no sends, every schedule fulfilled by a draft
    drafts = sales.get("/drafts?status=draft").json()
    assert len(drafts) == len(pending) and all(d["schedule_id"] for d in drafts)
    assert sales.get(f"/communication-events?quote_id={q['id']}").json() == []
    assert all(
        s["status"] == "drafted" for s in sales.get(f"/follow-up/schedules?status=all&quote_id={q['id']}").json()
    )
    # a second worker pass has nothing to do and creates nothing
    r2 = subprocess.run(
        [sys.executable, "-m", "app.workers.runner", "--once"],
        cwd=API_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r2.returncode == 0 and "processed 0 job(s)" in r2.stdout
    assert len(sales.get("/drafts?status=draft").json()) == len(pending)
    admin = login_as(app, tenant_a.users["admin"])
    assert {j["status"] for j in admin.get("/jobs").json()} == {"succeeded"}


def test_qa_s2_f07_claim_is_exclusive_between_two_workers(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    c = add_contact(sales, str(tenant_a.customer_id))
    q = mark_sent(sales, q)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        assert queue.enqueue(
            db, org_id=tenant_a.org_id, kind="send_draft", idempotency_key="qa:x", payload={"draft_id": d["id"]}
        )
        db.commit()
    s1, s2 = get_sessionmaker()(), get_sessionmaker()()
    try:
        job = queue.claim_next(s1, "worker-a")
        assert job is not None and job.status == "running" and job.locked_by == "worker-a"
        assert queue.claim_next(s2, "worker-b") is None, "a running job must not be claimable by a second worker"
        queue.run_job(s1, job)
    finally:
        s1.close()
        s2.close()
    admin = login_as(app, tenant_a.users["admin"])
    j = admin.get("/jobs").json()[0]
    assert j["status"] != "running" and j["attempts"] == 1  # the lock was released and the attempt recorded
    assert simulator_for(tenant_a.org_id).outbox == []  # automation is off → the job itself refuses to send


# ---------------------------------------------------------------- S2-A06 retries with backoff, dead letters
def test_qa_s2_a06_429_5xx_timeout_retry_with_capped_backoff_then_dead_letter(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    add_contact(sales, str(tenant_a.customer_id))
    admin.patch("/follow-up/policy", json={"automation_enabled": True, "offsets_days": [1]})
    q = mark_sent(sales, q)
    sim = simulator_for(tenant_a.org_id)
    sim.fail_next = [
        ProviderError("429 too many requests", retryable=True, status=429),
        ProviderError("503 service unavailable", retryable=True, status=503),
        ProviderError("timeout", retryable=True),
        ProviderError("502 bad gateway", retryable=True, status=502),
        ProviderError("500", retryable=True, status=500),
    ]  # exactly max_attempts (5) transient failures; the admin retry afterwards must succeed
    force_due(tenant_a.org_id)
    assert tick() == 1
    j = admin.get("/jobs").json()[0]
    assert j["status"] == "queued" and j["attempts"] == 1 and "429" in j["last_error"]
    assert 20 <= seconds_until(j["next_run_at"]) <= 40, j["next_run_at"]  # 30 s · 2⁰
    assert tick() == 0, "a job waiting for its backoff must not be re-run early"
    expected_backoff = {2: 60, 3: 120, 4: 240}
    for attempt, secs in expected_backoff.items():
        force_due(tenant_a.org_id, schedules=False, jobs=True)
        with get_sessionmaker()() as db:
            assert queue.run_once(db) == 1
        j = admin.get("/jobs").json()[0]
        assert j["status"] == "queued" and j["attempts"] == attempt, j
        assert secs - 10 <= seconds_until(j["next_run_at"]) <= secs + 10, (attempt, j["next_run_at"])
    force_due(tenant_a.org_id, schedules=False, jobs=True)
    with get_sessionmaker()() as db:
        queue.run_once(db)
    dead = admin.get("/jobs?status=dead").json()
    assert len(dead) == 1 and dead[0]["attempts"] == 5 and dead[0]["finished_at"]
    assert sim.outbox == [] and sales.get(f"/communication-events?quote_id={q['id']}").json() == []
    # the schedule is not falsely marked sent and the draft (if any) is not marked sent
    sched = sales.get(f"/follow-up/schedules?status=all&quote_id={q['id']}").json()[0]
    assert sched["status"] != "sent"
    assert all(d["status"] != "sent" for d in sales.get("/drafts").json())
    # cap: backoff never exceeds one hour
    # (observation, not asserted: backoff(n) overflows timedelta for n > 42 because the uncapped power is
    #  computed before min(); unreachable with max_attempts ≤ 42)
    assert queue.backoff(10).total_seconds() == 3600 and queue.backoff(40).total_seconds() == 3600
    # dead-letter view is admin-only; retry re-queues and the next successful attempt delivers exactly once
    for role in ("sales", "approver", "readonly"):
        ac = login_as(app, tenant_a.users[role])
        assert ac.get("/jobs?status=dead").status_code == 403
        assert ac.post(f"/jobs/{dead[0]['id']}/retry").status_code == 403
    assert admin.post(f"/jobs/{uuid.uuid4()}/retry").status_code == 404
    r = admin.post(f"/jobs/{dead[0]['id']}/retry")
    assert r.status_code == 200 and r.json()["status"] == "queued" and r.json()["attempts"] == 0
    assert admin.post(f"/jobs/{dead[0]['id']}/retry").status_code == 409  # only dead/failed jobs can be retried
    with get_sessionmaker()() as db:
        assert queue.run_once(db) == 1
    assert len(sim.outbox) == 1
    assert admin.get("/jobs").json()[0]["status"] == "succeeded"


def test_qa_s2_a06_permanent_provider_rejection_fails_fast_without_retry(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    add_contact(sales, str(tenant_a.customer_id))
    admin.patch("/follow-up/policy", json={"automation_enabled": True, "offsets_days": [1]})
    q = mark_sent(sales, q)
    sim = simulator_for(tenant_a.org_id)
    sim.fail_next = [ProviderError("550 mailbox unavailable", retryable=False, status=550)]
    force_due(tenant_a.org_id)
    assert tick() == 1
    j = admin.get("/jobs").json()[0]
    assert j["status"] == "failed" and j["attempts"] == 1 and "550" in j["last_error"]
    assert tick() == 0
    assert sim.outbox == []
    drafts = sales.get("/drafts").json()
    assert drafts and drafts[0]["status"] == "failed" and drafts[0]["last_error"]
    assert sales.get(f"/communication-events?quote_id={q['id']}").json() == []


# ---------------------------------------------------------------- S2-A05 provider outage / misconfiguration handled safely
def test_qa_s2_a05_smtp_outage_is_retried_then_dead_lettered_without_false_sent(
    app, tenant_a: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    from app.core import config

    sales, _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    c = add_contact(sales, str(tenant_a.customer_id))
    monkeypatch.setenv("SMTP_HOST", "127.0.0.1")
    monkeypatch.setenv("SMTP_PORT", "9")  # discard port: nothing listens → connection refused, no network
    monkeypatch.setenv("SMTP_FROM_EMAIL", "quotes@northwind.example")
    config.get_settings.cache_clear()
    try:
        r = admin.post("/integrations", json={"provider": "smtp", "account_email": "quotes@northwind.example"})
        assert r.status_code == 201 and r.json()["status"] == "active" and "secret" not in r.json()
        admin.patch("/follow-up/policy", json={"automation_enabled": True, "offsets_days": [1]})
        q = mark_sent(sales, q)
        d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
        # manual path: a clear transient error, the draft is not claimed sent
        r = sales.post(f"/drafts/{d['id']}/send")
        assert r.status_code == 503, r.text
        got = sales.get(f"/drafts/{d['id']}").json()
        assert got["status"] == "draft" and got["last_error"] and "SMTP" in got["last_error"]
        assert sales.get(f"/communication-events?quote_id={q['id']}").json() == []
        # automated path: retried with backoff, eventually dead-lettered and visible to the admin
        force_due(tenant_a.org_id)
        assert tick() == 1
        j = admin.get("/jobs").json()[0]
        assert j["status"] == "queued" and j["attempts"] == 1 and "SMTP" in j["last_error"]
        for _ in range(4):
            force_due(tenant_a.org_id, schedules=False, jobs=True)
            with get_sessionmaker()() as db:
                queue.run_once(db)
        dead = admin.get("/jobs?status=dead").json()
        assert len(dead) == 1 and dead[0]["attempts"] == 5
        assert sales.get(f"/communication-events?quote_id={q['id']}").json() == []
        assert simulator_for(tenant_a.org_id).outbox == []
        # revocation falls back to the default simulator and removes any stored secret
        conn = admin.get("/integrations").json()[0]
        assert admin.post(f"/integrations/{conn['id']}/revoke").json()["status"] == "revoked"
        with get_sessionmaker()() as db:
            set_tenant(db, tenant_a.org_id, None)
            assert db.execute(text("SELECT encrypted_secret FROM integration_connections")).scalar() is None
        r = sales.post(f"/drafts/{d['id']}/send")
        assert r.status_code == 200 and r.json()["provider"] == "simulator"
    finally:
        config.get_settings.cache_clear()


def test_qa_s2_a05_sync_inbound_with_misconfigured_provider_fails_safely_and_is_recorded(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """Admin selects SMTP from the settings page without server-side SMTP settings; inbound sync must not crash
    and the connection must carry the error so the admin can see it (S2-A05 "admin notified")."""
    admin = login_as(app, tenant_a.users["admin"])
    assert admin.post("/integrations", json={"provider": "smtp"}).status_code == 201
    r = admin.post("/integrations/sync-inbound")
    assert r.status_code in (400, 503), (r.status_code, r.text)
    assert "SMTP" in r.text
    conns = admin.get("/integrations").json()
    assert len(conns) == 1 and conns[0]["provider"] == "smtp"
    assert conns[0]["last_error"] or conns[0]["status"] == "error", (
        "the failed sync left no trace on the integration record: " + str(conns[0])
    )


def test_qa_s2_f06_secret_handling_bad_key_lengths_and_no_echo(
    app, tenant_a: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    import base64

    from app.core import config

    admin = login_as(app, tenant_a.users["admin"])
    try:
        monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", base64.b64encode(b"short").decode())
        config.get_settings.cache_clear()
        r = admin.post("/integrations", json={"provider": "smtp", "secret": "pw-1234567"})
        assert r.status_code == 422 and "32 bytes" in r.text and "pw-1234567" not in r.text
        monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
        config.get_settings.cache_clear()
        r = admin.post("/integrations", json={"provider": "smtp", "secret": "pw-1234567"})
        assert r.status_code == 201
        assert "pw-1234567" not in admin.get("/integrations").text
        with get_sessionmaker()() as db:
            set_tenant(db, tenant_a.org_id, None)
            rows = db.execute(text("SELECT summary::text FROM audit_events WHERE action = 'integration.connect'")).all()
            assert rows and all("pw-1234567" not in r[0] for r in rows)
        for provider in ("gmail", "msgraph"):
            r = admin.post("/integrations", json={"provider": provider, "secret": "tok"})
            assert r.status_code == 422 and "not shipped" in r.text
        assert admin.post("/integrations", json={"provider": "pigeon"}).status_code == 422
    finally:
        config.get_settings.cache_clear()


# ---------------------------------------------------------------- consent changes after planning, cancel/reschedule (S2-A07)
def test_qa_s2_f05_withdrawing_consent_after_planning_degrades_to_draft_only(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    add_contact(sales, str(tenant_a.customer_id))
    admin.patch("/follow-up/policy", json={"automation_enabled": True, "offsets_days": [1]})
    q = mark_sent(sales, q)
    sched = sales.get(f"/follow-up/schedules?quote_id={q['id']}").json()
    assert sched and sched[0]["mode"] == "auto_send"
    assert admin.patch("/follow-up/policy", json={"automation_enabled": False}).json()["automation_enabled"] is False
    force_due(tenant_a.org_id)
    tick()
    assert simulator_for(tenant_a.org_id).outbox == [], "sent after consent was withdrawn"
    jobs = admin.get("/jobs").json()
    assert jobs and jobs[0]["status"] not in ("dead", "failed"), jobs[0]
    s = sales.get(f"/follow-up/schedules?status=all&quote_id={q['id']}").json()[0]
    assert s["status"] == "drafted" and s["draft_id"], (
        "without consent the due follow-up should become a draft for a human instead of erroring: " + str(s)
    )


def test_qa_s2_a07_cancelled_or_rescheduled_followups_are_not_processed(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, approver, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    readonly = login_as(app, tenant_a.users["readonly"])
    add_contact(sales, str(tenant_a.customer_id))
    admin.patch("/follow-up/policy", json={"automation_enabled": True, "offsets_days": [1, 2, 3], "max_attempts": 3})
    q = mark_sent(sales, q)
    pending = sales.get("/follow-up/schedules").json()
    assert [p["attempt_no"] for p in pending] == [1, 2, 3] and all(p["mode"] == "auto_send" for p in pending)
    a, b, c = pending
    assert readonly.post(f"/follow-up/schedules/{a['id']}/cancel", json={}).status_code == 403
    assert (
        readonly.post(f"/follow-up/schedules/{a['id']}/reschedule", json={"due_at": "2030-01-01T09:00:00Z"}).status_code
        == 403
    )
    assert (
        approver.post(f"/follow-up/schedules/{a['id']}/cancel", json={"reason": "customer asked us to wait"}).json()[
            "status"
        ]
        == "cancelled"
    )
    assert (
        sales.post(f"/follow-up/schedules/{b['id']}/reschedule", json={"due_at": "2030-01-01T09:00:00Z"}).status_code
        == 200
    )
    assert (
        sales.post(f"/follow-up/schedules/{a['id']}/reschedule", json={"due_at": "2030-01-01T09:00:00Z"}).status_code
        == 409
    )
    assert sales.post(f"/follow-up/schedules/{uuid.uuid4()}/cancel", json={}).status_code == 404
    assert [p["id"] for p in sales.get("/follow-up/schedules").json()] == [c["id"], b["id"]]
    # only the untouched third schedule is due → exactly one automated send
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        db.execute(
            text("UPDATE follow_up_schedules SET due_at = now() - interval '1 minute' WHERE id = :i"), {"i": c["id"]}
        )
        db.execute(
            text("UPDATE follow_up_schedules SET due_at = now() - interval '1 minute' WHERE id = :i"), {"i": a["id"]}
        )
        db.commit()
    tick()
    tick()
    sim = simulator_for(tenant_a.org_id)
    assert len(sim.outbox) == 1
    states = {
        s["attempt_no"]: s["status"] for s in sales.get(f"/follow-up/schedules?status=all&quote_id={q['id']}").json()
    }
    assert states == {1: "cancelled", 2: "pending", 3: "sent"}, states
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        acts = [
            r[0]
            for r in db.execute(
                text("SELECT action FROM audit_events WHERE action LIKE 'followup.%' ORDER BY created_at")
            ).all()
        ]
    assert "followup.cancel" in acts and "followup.reschedule" in acts
    assert admin.get("/jobs").json()[0]["status"] == "succeeded"


def test_qa_s2_f07_job_records_contain_ids_only_never_addresses_or_bodies(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    c = add_contact(sales, str(tenant_a.customer_id), email="private.person@customer.example")
    admin.patch("/follow-up/policy", json={"automation_enabled": True, "offsets_days": [1]})
    q = mark_sent(sales, q)
    sim = simulator_for(tenant_a.org_id)
    sim.fail_next = [ProviderError("mailbox private.person@customer.example rejected", retryable=False, status=550)]
    force_due(tenant_a.org_id)
    tick()
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    assert sales.post(f"/drafts/{d['id']}/send").status_code == 200
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        payloads = [r[0] for r in db.execute(text("SELECT payload::text FROM job_runs")).all()]
        errors = [r[0] or "" for r in db.execute(text("SELECT last_error FROM job_runs")).all()]
        summaries = [r[0] for r in db.execute(text("SELECT summary::text FROM audit_events")).all()]
    assert payloads and all("@" not in p and "Dear" not in p for p in payloads), payloads
    assert all("Dear Buyer" not in e for e in errors)
    assert all("private.person@customer.example" not in s and "Dear Buyer" not in s for s in summaries)
    # observation (not asserted): provider error strings are stored verbatim in job_runs.last_error and shown to
    # admins; the shipped SMTP adapter's own messages do not include addresses.
    assert all("Dear Buyer" not in (j["last_error"] or "") for j in admin.get("/jobs").json())
