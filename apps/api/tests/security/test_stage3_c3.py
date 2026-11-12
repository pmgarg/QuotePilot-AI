"""Security re-check of Stage 3 fix rounds 2 and 3 (release review 1, condition C3).

Evidence: docs/evidence/security-stage3-c3-2026-11-12.md. Probes: surfaces that show a customer pending erasure,
purge-job reclaim (SEC3-14 side effects), tenant scope of the reclaim, concurrent CSV replay (S3-RR1-4) and what a
purge leaves behind (observations for the owner decision C1). Weaknesses are strict ``xfail`` tests.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.base import utcnow
from app.db.session import get_sessionmaker, set_tenant
from app.models.followup import FollowUpSchedule, JobRun
from app.services.comms.providers import reset_simulators
from app.services.intelligence import data_io, retention
from app.services.jobs import queue
from tests.conftest import Tenant, login_as, upload
from tests.integration.test_stage3_intelligence import _connector, approved_quote, seed_confirmed_orders
from tests.security.test_stage3_confirm import _approve_erasure, _make_due, _race

pytestmark = [pytest.mark.integration, pytest.mark.security]

SUBJECT_EMAIL = "pending.subject@erasure.example"


@pytest.fixture(autouse=True)
def _reset_sim() -> None:
    reset_simulators()


def _sql(org_id: uuid.UUID, statement: str, **params: Any) -> Any:
    with get_sessionmaker()() as db:
        set_tenant(db, org_id, None)
        rows = db.execute(text(statement), params)
        out = rows.all() if rows.returns_rows else None
        db.commit()
    return out


def _scalar(org_id: uuid.UUID, statement: str, **params: Any) -> Any:
    out = _sql(org_id, statement, **params)
    return out[0][0] if out else None


def _add_job(tenant: Tenant, kind: str, payload: dict[str, Any], key: str, **fields: Any) -> uuid.UUID:
    with get_sessionmaker()() as db:
        set_tenant(db, tenant.org_id, None)
        job = JobRun(
            organization_id=tenant.org_id,
            kind=kind,
            idempotency_key=key,
            payload=payload,
            next_run_at=utcnow(),
            **fields,
        )
        db.add(job)
        db.commit()
        return job.id


def _stale_running(attempts: int = 1, max_attempts: int = 5) -> dict[str, Any]:
    return {
        "status": "running",
        "attempts": attempts,
        "max_attempts": max_attempts,
        "locked_by": "crashed-worker",
        "locked_at": utcnow() - timedelta(days=1),
    }


# ======================================================================= 3(a) surfaces for a customer pending erasure
def test_c3_a_no_read_surface_shows_name_or_email_of_a_customer_pending_erasure(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    name = f"{tenant_a.name} Customer"
    _sql(tenant_a.org_id, "UPDATE customers SET email = :e", e=SUBJECT_EMAIL)
    sales, _, q = approved_quote(app, tenant_a)
    seed_confirmed_orders(tenant_a, [40, 20])
    order = sales.post(f"/quotes/{q['id']}/orders", json={}).json()
    proposal = sales.post(f"/customers/{tenant_a.customer_id}/order-proposals").json()
    _approve_erasure(app, tenant_a)
    cid = tenant_a.customer_id
    paths = [
        "/quotes",
        f"/quotes?q={tenant_a.name}",
        f"/quotes?customer_id={cid}",
        f"/quotes/{q['id']}",
        f"/quotes/{q['id']}/history",
        "/orders",
        f"/orders?customer_id={cid}",
        f"/orders/{order['id']}",
        "/order-proposals",
        "/kpis",
        "/customers",
        f"/customers?q={tenant_a.name}",
        f"/customers/{cid}",
        f"/customers/{cid}/profile",
        f"/customers/{cid}/contacts",
        "/data/runs",
        "/sync-conflicts",
    ]
    del proposal
    for role in ("sales", "approver", "readonly"):
        client = login_as(app, tenant_a.users[role])
        for path in paths:
            r = client.get(path)
            assert r.status_code in (200, 404, 403), (role, path, r.status_code)
            assert name not in r.text and SUBJECT_EMAIL not in r.text, (role, path)
    approver = login_as(app, tenant_a.users["approver"])  # export_data without manage_retention
    for entity in ("customers", "orders"):
        r = approver.get(f"/data/export/{entity}")
        assert r.status_code == 200
        assert name not in r.text and SUBJECT_EMAIL not in r.text, entity


@pytest.mark.xfail(
    strict=True,
    reason="SEC3-C3-1: the quote CSV/PDF export of an approved quote of a customer pending erasure still prints the "
    "customer name (from the approval snapshot) for every role (export permission = all roles)",
)
def test_c3_a_quote_exports_do_not_print_the_name_of_a_customer_pending_erasure(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    _approve_erasure(app, tenant_a)
    readonly = login_as(app, tenant_a.users["readonly"])
    name = f"{tenant_a.name} Customer".encode()
    before = sales.get(f"/quotes/{q['id']}/versions/1/pdf")
    assert before.status_code == 200 and name in before.content, "precondition: the PDF prints the customer name"
    for suffix in ("pdf", "csv"):
        r = readonly.get(f"/quotes/{q['id']}/versions/1/{suffix}")
        assert r.status_code in (403, 404) or name not in r.content, suffix


# ======================================================================= 3(b) purge-job reclaim
def _due_request(app: Any, tenant: Tenant) -> str:
    rid = _approve_erasure(app, tenant)
    _make_due(tenant, rid)
    return rid


def test_c3_b_stale_running_purge_job_is_requeued_and_executes_once(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    rid = _due_request(app, tenant_a)
    job_id = _add_job(tenant_a, "purge_customer", {"request_id": rid}, f"purge:{rid}", **_stale_running())
    with get_sessionmaker()() as db:
        assert retention.enqueue_due_purges(db) == 1  # requeued, and no second job is created next to it
        assert _scalar(tenant_a.org_id, "SELECT count(*) FROM job_runs WHERE kind = 'purge_customer'") == 1
        assert _scalar(tenant_a.org_id, "SELECT status FROM job_runs WHERE id = :i", i=job_id) == "queued"
        assert queue.run_once(db, "w") == 1
    assert _scalar(tenant_a.org_id, "SELECT status FROM deletion_requests WHERE id = :i", i=rid) == "executed"
    assert _scalar(tenant_a.org_id, "SELECT status FROM job_runs WHERE id = :i", i=job_id) == "succeeded"
    assert _scalar(tenant_a.org_id, "SELECT attempts FROM job_runs WHERE id = :i", i=job_id) >= 1


def test_c3_b_rerun_on_an_executed_request_is_quiet_and_redacts_nothing_twice(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    rid = _due_request(app, tenant_a)
    with get_sessionmaker()() as db:
        assert retention.enqueue_due_purges(db) == 1 and queue.run_once(db, "w") == 1
    summary = _scalar(tenant_a.org_id, "SELECT summary::text FROM deletion_requests WHERE id = :i", i=rid)
    audit_rows = _scalar(tenant_a.org_id, "SELECT count(*) FROM audit_events WHERE action = 'retention.purged'")
    assert audit_rows == 1
    # a duplicate job for the executed request (e.g. a reclaimed job that had in fact finished)
    dup = _add_job(tenant_a, "purge_customer", {"request_id": rid}, f"purge:{rid}:dup", **_stale_running())
    with get_sessionmaker()() as db:
        assert retention.enqueue_due_purges(db) == 1  # only the stale duplicate is reclaimed; nothing new is created
        assert queue.run_once(db, "w") == 1  # the reclaimed duplicate is processed
    row = _sql(tenant_a.org_id, "SELECT status, last_error FROM job_runs WHERE id = :i", i=dup)[0]
    assert row[0] == "succeeded" and row[1] is None, row
    assert _scalar(tenant_a.org_id, "SELECT summary::text FROM deletion_requests WHERE id = :i", i=rid) == summary
    assert _scalar(tenant_a.org_id, "SELECT count(*) FROM audit_events WHERE action = 'retention.purged'") == 1


def test_c3_b_dead_purge_job_does_not_touch_stage2_schedules_or_other_kinds(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    _, _, q = approved_quote(app, tenant_a)
    rid = _due_request(app, tenant_a)
    with get_sessionmaker()() as db:  # created after the approval: the soft delete cancels earlier schedules
        set_tenant(db, tenant_a.org_id, None)
        sched = FollowUpSchedule(
            organization_id=tenant_a.org_id,
            quote_id=uuid.UUID(q["id"]),
            attempt_no=1,
            due_at=utcnow() + timedelta(days=2),
            status="pending",
            mode="draft_only",
            idempotency_key="c3:sched",
            reason="c3 fixture",
        )
        db.add(sched)
        db.commit()
        sched_id = sched.id
    purge = _add_job(tenant_a, "purge_customer", {"request_id": rid}, f"purge:{rid}", **_stale_running(5, 5))
    other = _add_job(tenant_a, "schedule_followup", {"schedule_id": str(sched_id)}, "c3:other", **_stale_running(5, 5))
    with get_sessionmaker()() as db:
        n = retention.enqueue_due_purges(db)
    assert n == 1, n  # dead purge job -> a new job (count_dead=False does not count the dead one)
    assert _scalar(tenant_a.org_id, "SELECT status FROM job_runs WHERE id = :i", i=purge) == "dead"
    # the unrelated kind keeps its state: not reclaimed, not failed, its schedule untouched
    assert _scalar(tenant_a.org_id, "SELECT status FROM job_runs WHERE id = :i", i=other) == "running"
    assert _scalar(tenant_a.org_id, "SELECT status FROM follow_up_schedules WHERE id = :i", i=sched_id) == "pending"


def test_c3_b_legal_hold_blocks_after_reclaim_and_releasing_it_resumes(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    rid = _due_request(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    _add_job(tenant_a, "purge_customer", {"request_id": rid}, f"purge:{rid}", **_stale_running())
    # the hold arrives while the (crashed) job is still `running`; the scheduler no longer sees the request
    assert admin.post(f"/retention/requests/{rid}/legal-hold", json={"hold": True}).status_code == 200
    with get_sessionmaker()() as db:
        retention.enqueue_due_purges(db)  # reclaims the job (kind scoped), does not create another one
        queue.run_once(db, "w")
        assert retention.enqueue_due_purges(db) == 0
    assert _scalar(tenant_a.org_id, "SELECT status FROM deletion_requests WHERE id = :i", i=rid) == "approved"
    assert _scalar(tenant_a.org_id, "SELECT count(*) FROM customers WHERE name LIKE 'Deleted customer%'") == 0
    assert _scalar(tenant_a.org_id, "SELECT count(*) FROM job_runs WHERE status = 'running'") == 0
    assert admin.post(f"/retention/requests/{rid}/legal-hold", json={"hold": False}).status_code == 200
    with get_sessionmaker()() as db:
        assert retention.enqueue_due_purges(db) == 1 and queue.run_once(db, "w") == 1
    assert _scalar(tenant_a.org_id, "SELECT status FROM deletion_requests WHERE id = :i", i=rid) == "executed"


# ======================================================================= 3(c) cross-tenant
def test_c3_c_tenant_b_cannot_reclaim_or_process_tenant_a_purge_jobs(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    rid = _due_request(app, tenant_a)
    job_id = _add_job(tenant_a, "purge_customer", {"request_id": rid}, f"purge:{rid}", **_stale_running())
    queued_id = _add_job(tenant_a, "purge_customer", {"request_id": rid}, f"purge:{rid}:q", status="queued")
    owner_b = login_as(app, tenant_b.users["owner"])
    r = owner_b.post("/jobs/run-once")  # dev helper: tenant-scoped reclaim + run for the caller's tenant only
    assert r.status_code == 200, r.text
    with get_sessionmaker()() as db:
        queue.reclaim_abandoned(db, tenant_b.org_id)
        queue.run_once(db, "w", org_id=tenant_b.org_id)
    assert _scalar(tenant_a.org_id, "SELECT status FROM job_runs WHERE id = :i", i=job_id) == "running"
    assert _scalar(tenant_a.org_id, "SELECT status FROM job_runs WHERE id = :i", i=queued_id) == "queued"
    assert _scalar(tenant_a.org_id, "SELECT status FROM deletion_requests WHERE id = :i", i=rid) == "approved"
    # tenant B cannot see A's jobs or retention requests either
    assert all(j["id"] not in (str(job_id), str(queued_id)) for j in owner_b.get("/jobs").json())
    assert owner_b.get("/retention/requests").json() == []
    assert owner_b.post(f"/retention/requests/{rid}/approve", json={}).status_code == 404
    # the worker (all tenants) runs A's purge under A's own scope
    with get_sessionmaker()() as db:
        retention.enqueue_due_purges(db)
        queue.run_once(db, "w")
    assert _scalar(tenant_a.org_id, "SELECT status FROM deletion_requests WHERE id = :i", i=rid) == "executed"
    assert _scalar(tenant_b.org_id, "SELECT count(*) FROM customers WHERE name LIKE 'Deleted customer%'") == 0


# ======================================================================= 4(a) CSV same-key replay
def _csv_upload(client: Any, key: str, body: bytes) -> Callable[[], int]:
    url = f"/data/import/customers?idempotency_key={key}"
    return lambda: upload(client, url, "c.csv", body).status_code


def test_c3_csv_replay_three_concurrent_uploads_eight_rounds_apply_once(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    clients = [login_as(app, tenant_a.users[r]) for r in ("admin", "owner", "admin")]
    for attempt in range(8):
        body = f"name,reference\r\nRace Co {attempt},RC-{attempt}\r\n".encode()
        key = f"c3race-{attempt}"
        res = _race(*[_csv_upload(c, key, body) for c in clients])
        assert res == [201, 201, 201], (attempt, res)
        assert _scalar(tenant_a.org_id, "SELECT count(*) FROM customers WHERE reference = :r", r=f"RC-{attempt}") == 1
        assert _scalar(tenant_a.org_id, "SELECT count(*) FROM sync_runs WHERE idempotency_key = :k", k=key) == 1
        assert _scalar(tenant_a.org_id, "SELECT applied FROM sync_runs WHERE idempotency_key = :k", k=key) == 1
    other = upload(
        clients[0], "/data/import/customers?idempotency_key=c3race-0", "c.csv", b"name,reference\r\nOther,OT-1\r\n"
    )
    assert other.status_code == 409
    assert _scalar(tenant_a.org_id, "SELECT count(*) FROM customers WHERE reference = 'OT-1'") == 0


def test_c3_csv_replay_different_payload_in_a_race_never_applies_the_loser(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    a, b = login_as(app, tenant_a.users["admin"]), login_as(app, tenant_a.users["owner"])
    for attempt in range(8):
        key = f"c3diff-{attempt}"
        res = _race(
            _csv_upload(a, key, f"name,reference\r\nA {attempt},DA-{attempt}\r\n".encode()),
            _csv_upload(b, key, f"name,reference\r\nB {attempt},DB-{attempt}\r\n".encode()),
        )
        assert sorted(res) == [201, 409], (attempt, res)
        applied = _scalar(
            tenant_a.org_id,
            "SELECT count(*) FROM customers WHERE reference IN (:x, :y)",
            x=f"DA-{attempt}",
            y=f"DB-{attempt}",
        )
        assert applied == 1, (attempt, applied)


def test_c3_start_run_does_not_swallow_an_integrity_error_that_is_not_a_replay(  # type: ignore[no-untyped-def]
    app, tenant_a: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        data_io._start_run(db, tenant_a.org_id, "orders", "c3-dup", 1, "0" * 64)
        db.commit()
        set_tenant(db, tenant_a.org_id, None)  # the tenant setting is transaction-local
        # a unique violation for which no winning run can be found is not a replay: it must surface
        monkeypatch.setattr(data_io, "find_run", lambda *a, **k: None)
        with pytest.raises(IntegrityError):
            data_io._start_run(db, tenant_a.org_id, "orders", "c3-dup", 1, "0" * 64)
        db.rollback()
    assert _scalar(tenant_a.org_id, "SELECT count(*) FROM sync_runs WHERE idempotency_key = 'c3-dup'") == 1


# ======================================================================= 4(b) what a purge leaves behind
def test_c3_purge_redacts_import_and_connector_run_reports_that_quote_the_subject(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    name = f"{tenant_a.name} Customer"
    _sql(tenant_a.org_id, "UPDATE customers SET email = :e", e=SUBJECT_EMAIL)
    admin = login_as(app, tenant_a.users["admin"])
    # CSV customers import: duplicate reference -> failed row quoting the reference, e-mail and name in the values
    dup = f"name,reference,email\r\n{name},CUST-1,{SUBJECT_EMAIL}\r\n".encode()
    assert upload(admin, "/data/import/customers?idempotency_key=c3-dup", "c.csv", dup).status_code == 201
    # CSV orders import naming the subject's reference
    orders = b"order_ref,sku,quantity,unit_price,customer_reference\r\nPO-CUST-1-77,A,1,1,CUST-1\r\n"
    assert upload(admin, "/data/import/orders?idempotency_key=c3-ord", "o.csv", orders).status_code == 201
    # connector: records that are rejected with the subject's values in them
    conn = _connector(admin, ["read:customers", "read:orders"])
    recs = [
        {"reference": "CUST-1", "name": name, "email": f"not an address {SUBJECT_EMAIL}"},
        {"reference": "CUST-1", "name": name + "x" * 300, "email": SUBJECT_EMAIL},
    ]
    r = admin.post(
        f"/connectors/{conn['id']}/sync", json={"entity": "customers", "idempotency_key": "c3-c", "records": recs}
    )
    assert r.status_code == 201, r.text
    before = " ".join(row[0] for row in _sql(tenant_a.org_id, "SELECT report::text FROM sync_runs") if row[0]).lower()
    assert "cust-1" in before and SUBJECT_EMAIL in before, "precondition: the run reports quote the subject"
    rid = _due_request(app, tenant_a)
    with get_sessionmaker()() as db:
        assert retention.enqueue_due_purges(db) == 1 and queue.run_once(db, "w") == 1
    reports = [row[0] for row in _sql(tenant_a.org_id, "SELECT report::text FROM sync_runs") if row[0]]
    assert reports, "precondition: run reports exist"
    for rep in reports:
        low = rep.lower()
        assert name.lower() not in low and SUBJECT_EMAIL not in low and "cust-1" not in low, rep[:200]
    assert rid


def test_c3_obs_c1_what_a_purge_still_leaves_behind(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """Observation for the owner decision C1 (pins the CURRENT, documented behaviour; it flips if the scope grows)."""
    name = f"{tenant_a.name} Customer"
    sales, _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    orders = b"order_ref,sku,quantity,unit_price,customer_reference\r\nPO-CUST-1-77,A,1,1,CUST-1\r\n"
    assert upload(admin, "/data/import/orders?idempotency_key=c3-obs", "o.csv", orders).status_code == 201
    rfq = sales.post("/rfqs", json={"text": f"From {name}\n10 pcs SKU-1001", "customer_id": str(tenant_a.customer_id)})
    assert rfq.status_code in (200, 201), rfq.text
    _sql(tenant_a.org_id, "UPDATE quote_versions SET notes = :n", n=f"for {name}")
    rid = _due_request(app, tenant_a)
    with get_sessionmaker()() as db:
        assert retention.enqueue_due_purges(db) == 1 and queue.run_once(db, "w") == 1
    left = {
        "orders.external_ref": _scalar(
            tenant_a.org_id, "SELECT count(*) FROM orders WHERE external_ref LIKE '%CUST-1%'"
        ),
        "orders.customer_id_kept": _scalar(
            tenant_a.org_id, "SELECT count(*) FROM orders WHERE customer_id = :c", c=tenant_a.customer_id
        ),
        "rfq_items.raw_text": _scalar(
            tenant_a.org_id, "SELECT count(*) FROM rfq_items WHERE raw_text ILIKE '%10 pcs%'"
        ),
        "quote_versions.notes": _scalar(
            tenant_a.org_id, "SELECT count(*) FROM quote_versions WHERE notes ILIKE :n", n=f"%{name}%"
        ),
        "audit.customer.create_name": _scalar(
            tenant_a.org_id, "SELECT count(*) FROM audit_events WHERE summary::text ILIKE :n", n=f"%{name}%"
        ),
    }
    assert rid
    assert left["orders.external_ref"] == 1  # external_ref kept (C1)
    assert left["orders.customer_id_kept"] >= 1  # by design: orders keep the pseudonymised customer id
    assert left["quote_versions.notes"] >= 1  # free text kept (C1, documented)
    assert left["rfq_items.raw_text"] >= 0  # recorded in the evidence file, not asserted either way
