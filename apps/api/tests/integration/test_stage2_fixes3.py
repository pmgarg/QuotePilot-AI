"""Stage 2 fix round 3: terminal schedule failure (S2-RR1-2) and abandoned-job reclaim (S2-RR1-4 / SEC3-14)."""

from __future__ import annotations

import uuid

import pytest

from app.db.session import get_sessionmaker, set_tenant
from app.services.comms.providers import ProviderError, reset_simulators, simulator_for
from app.services.jobs import queue
from tests.conftest import Tenant, login_as
from tests.integration.test_stage2_fixes1 import _draft_for_sale, _due_auto_schedule, sql, tick

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _reset_sim() -> None:
    reset_simulators()


def _schedule(client, quote_id: str) -> dict:  # type: ignore[no-untyped-def,type-arg]
    return client.get(f"/follow-up/schedules?status=all&quote_id={quote_id}").json()[0]  # type: ignore[no-any-return]


def _abandon(org_id: uuid.UUID, age: str = "1 hour") -> None:
    """Simulate a worker that died while holding the job: running, attempt counted, lock older than the timeout."""
    sql(
        org_id,
        "UPDATE job_runs SET status = 'running', attempts = GREATEST(attempts, 1), locked_by = 'dead-worker', "
        "locked_at = now() - CAST(:age AS interval)",
        age=age,
    )


def _job(org_id: uuid.UUID) -> dict:  # type: ignore[type-arg]
    r = sql(org_id, "SELECT status, attempts, max_attempts, locked_at, locked_by FROM job_runs")[0]
    return dict(zip(("status", "attempts", "max_attempts", "locked_at", "locked_by"), r, strict=True))


# ---------------------------------------------------------------- S2-RR1-2: terminal schedule failure + retry re-arm
def test_permanent_provider_error_fails_the_job_and_schedule_and_admin_retry_rearms_it(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, admin, q = _due_auto_schedule(app, tenant_a, "buyer@customer.example")
    sim = simulator_for(tenant_a.org_id)
    sim.fail_next.append(ProviderError("recipient refused by server", retryable=False, status=550))
    assert tick() == 1
    job = admin.get("/jobs").json()[0]
    assert job["kind"] == "schedule_followup" and job["status"] == "failed" and job["attempts"] == 1
    s = _schedule(sales, q["id"])
    assert s["status"] == "failed" and s["last_error"] and "ProviderError" in s["last_error"]
    assert sim.outbox == []
    assert tick() == 0  # a failed schedule is not scanned again, the job is not claimed again
    # roles / tenant for the retry endpoint
    assert login_as(app, tenant_a.users["sales"]).post(f"/jobs/{job['id']}/retry").status_code == 403
    # admin retry re-arms the schedule; it is processed exactly once
    assert admin.post(f"/jobs/{job['id']}/retry").status_code == 200
    assert _schedule(sales, q["id"])["status"] == "pending"
    assert tick() == 1
    assert len(sim.outbox) == 1
    assert admin.get("/jobs").json()[0]["status"] == "succeeded"
    assert _schedule(sales, q["id"])["status"] == "sent"
    assert tick() == 0 and len(sim.outbox) == 1


def test_exhausted_attempts_dead_letter_the_job_and_fail_the_schedule(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, admin, q = _due_auto_schedule(app, tenant_a, "buyer@customer.example")
    sim = simulator_for(tenant_a.org_id)
    with get_sessionmaker()() as db:
        queue.enqueue_due_schedules(db)
    sql(tenant_a.org_id, "UPDATE job_runs SET max_attempts = 1")
    sim.fail_next.append(ProviderError("SMTP unavailable", retryable=True))
    with get_sessionmaker()() as db:
        assert queue.run_once(db) == 1
    job = admin.get("/jobs").json()[0]
    assert job["status"] == "dead" and job["attempts"] == 1
    assert _schedule(sales, q["id"])["status"] == "failed"
    assert sim.outbox == []
    # cross-tenant: another tenant's admin cannot retry it
    assert admin.post(f"/jobs/{uuid.uuid4()}/retry").status_code == 404


def test_retry_of_another_tenants_dead_job_is_404(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    _due_auto_schedule(app, tenant_a, "buyer@customer.example")
    simulator_for(tenant_a.org_id).fail_next.append(ProviderError("nope", retryable=False, status=550))
    tick()
    job_id = login_as(app, tenant_a.users["admin"]).get("/jobs").json()[0]["id"]
    assert login_as(app, tenant_b.users["admin"]).post(f"/jobs/{job_id}/retry").status_code == 404


# ---------------------------------------------------------------- S2-RR1-4: visibility timeout
def test_abandoned_schedule_job_is_requeued_with_attempts_preserved_and_processed_once(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, admin, q = _due_auto_schedule(app, tenant_a, "buyer@customer.example")
    with get_sessionmaker()() as db:
        queue.enqueue_due_schedules(db)
    _abandon(tenant_a.org_id)
    with get_sessionmaker()() as db:
        assert queue.reclaim_abandoned(db) == 1
    j = _job(tenant_a.org_id)
    assert j["status"] == "queued" and j["attempts"] == 1 and j["locked_at"] is None and j["locked_by"] is None
    assert tick() == 1
    j = _job(tenant_a.org_id)
    assert j["status"] == "succeeded" and j["attempts"] == 2
    assert len(simulator_for(tenant_a.org_id).outbox) == 1
    assert tick() == 0 and len(simulator_for(tenant_a.org_id).outbox) == 1
    assert _schedule(sales, q["id"])["status"] == "sent"


def test_worker_tick_reclaims_without_a_separate_call(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    _due_auto_schedule(app, tenant_a, "buyer@customer.example")
    with get_sessionmaker()() as db:
        queue.enqueue_due_schedules(db)
    _abandon(tenant_a.org_id)
    assert tick() == 1
    assert _job(tenant_a.org_id)["status"] == "succeeded"
    assert len(simulator_for(tenant_a.org_id).outbox) == 1


def test_abandoned_job_with_exhausted_attempts_is_dead_and_fails_its_schedule(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, admin, q = _due_auto_schedule(app, tenant_a, "buyer@customer.example")
    with get_sessionmaker()() as db:
        queue.enqueue_due_schedules(db)
    sql(tenant_a.org_id, "UPDATE job_runs SET max_attempts = 2, attempts = 2")
    _abandon(tenant_a.org_id)
    with get_sessionmaker()() as db:
        assert queue.reclaim_abandoned(db) == 1
    j = _job(tenant_a.org_id)
    assert j["status"] == "dead" and j["attempts"] == 2
    s = _schedule(sales, q["id"])
    assert s["status"] == "failed" and "abandoned" in s["last_error"]
    assert tick() == 0 and simulator_for(tenant_a.org_id).outbox == []
    # the existing dead-letter handling applies: admin retry re-arms the schedule and it is processed once
    job_id = admin.get("/jobs").json()[0]["id"]
    assert admin.post(f"/jobs/{job_id}/retry").status_code == 200
    assert tick() == 1 and len(simulator_for(tenant_a.org_id).outbox) == 1


def test_fresh_running_job_is_not_touched(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    _due_auto_schedule(app, tenant_a, "buyer@customer.example")
    with get_sessionmaker()() as db:
        queue.enqueue_due_schedules(db)
    _abandon(tenant_a.org_id, age="10 seconds")  # far below the 900 s default (90x headroom)
    with get_sessionmaker()() as db:
        assert queue.reclaim_abandoned(db) == 0
        assert queue.run_once(db) == 0
    j = _job(tenant_a.org_id)
    assert j["status"] == "running" and j["locked_by"] == "dead-worker"
    assert simulator_for(tenant_a.org_id).outbox == []


def test_timeout_setting_is_honoured(app, tenant_a: Tenant, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    from app.core.config import get_settings

    _due_auto_schedule(app, tenant_a, "buyer@customer.example")
    with get_sessionmaker()() as db:
        queue.enqueue_due_schedules(db)
    _abandon(tenant_a.org_id, age="1 hour")
    monkeypatch.setattr(get_settings(), "job_visibility_timeout_seconds", 2 * 3600)
    with get_sessionmaker()() as db:
        assert queue.reclaim_abandoned(db) == 0
    monkeypatch.setattr(get_settings(), "job_visibility_timeout_seconds", 60)
    with get_sessionmaker()() as db:
        assert queue.reclaim_abandoned(db) == 1


def test_tenant_scoped_reclaim_and_run_once_leave_another_tenants_abandoned_job_alone(
    app, tenant_a: Tenant, tenant_b: Tenant
) -> None:  # type: ignore[no-untyped-def]
    _due_auto_schedule(app, tenant_b, "buyer@beta.example")
    with get_sessionmaker()() as db:
        queue.enqueue_due_schedules(db, org_id=tenant_b.org_id)
    _abandon(tenant_b.org_id)
    with get_sessionmaker()() as db:
        assert queue.reclaim_abandoned(db, tenant_a.org_id) == 0
        assert queue.run_once(db, org_id=tenant_a.org_id) == 0
    assert _job(tenant_b.org_id)["status"] == "running"
    # the API run-once path is tenant scoped too
    assert login_as(app, tenant_a.users["admin"]).post("/jobs/run-once").json() == {"processed": 0}
    assert _job(tenant_b.org_id)["status"] == "running" and simulator_for(tenant_b.org_id).outbox == []
    # B's own run-once reclaims and processes it
    assert login_as(app, tenant_b.users["admin"]).post("/jobs/run-once").json() == {"processed": 1}
    assert _job(tenant_b.org_id)["status"] == "succeeded" and len(simulator_for(tenant_b.org_id).outbox) == 1


def test_reclaimed_send_job_does_not_deliver_twice(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """The worker died after the send committed (draft 'sent') but before the job was marked done: the reclaimed job
    re-enters send_draft, which returns early on a 'sent' draft (row lock + status check), so nothing is redelivered."""
    sales, _admin, _q, _c, d = _draft_for_sale(app, tenant_a)
    sim = simulator_for(tenant_a.org_id)
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        assert queue.enqueue(
            db,
            org_id=tenant_a.org_id,
            kind="send_draft",
            idempotency_key=f"draft:{d['id']}",
            payload={"draft_id": d["id"], "idempotency_key": f"draft:{d['id']}"},
        )
        db.commit()
    assert sales.post(f"/drafts/{d['id']}/send").status_code == 200
    assert len(sim.outbox) == 1
    _abandon(tenant_a.org_id)
    assert tick() == 1
    assert _job(tenant_a.org_id)["status"] == "succeeded"
    assert len(sim.outbox) == 1
    assert sql(tenant_a.org_id, "SELECT count(*) FROM audit_events WHERE action = 'draft.sent'")[0][0] == 1
