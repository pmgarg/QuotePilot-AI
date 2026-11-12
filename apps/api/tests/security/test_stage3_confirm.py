"""Security re-verification — Stage 3 after fix round 1 (see docs/evidence/security-stage3-reverify-2026-11-12.md).

Covers what the first audit's tests do not: processing of records that already exist for a customer pending
erasure, the stalled-purge flag, purge completeness for contacts/drafts/PDFs/events, and concurrency probes.
Open findings (SEC3-12 onwards) are strict ``xfail`` tests; everything else is a plain test.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, text

from app.db.base import utcnow
from app.db.session import get_sessionmaker, set_tenant
from app.models.followup import CommunicationEvent, JobRun
from app.models.intelligence import DeletionRequest, Order
from app.models.quote import QuoteVersion
from app.services.comms.providers import reset_simulators
from app.services.intelligence import retention
from app.services.jobs import queue
from app.services.storage import get_storage
from tests.conftest import Tenant, login_as, upload
from tests.integration.test_stage2_followup import add_contact, mark_sent
from tests.integration.test_stage3_intelligence import _connector, approved_quote, seed_confirmed_orders

pytestmark = [pytest.mark.integration, pytest.mark.security]


@pytest.fixture(autouse=True)
def _reset_sim() -> None:
    reset_simulators()


def _approve_erasure(app: Any, tenant: Tenant) -> str:
    sales = login_as(app, tenant.users["sales"])
    owner = login_as(app, tenant.users["owner"])
    dr = sales.post("/retention/requests", json={"customer_id": str(tenant.customer_id), "reason": "erasure"}).json()
    assert owner.post(f"/retention/requests/{dr['id']}/approve", json={}).status_code == 200
    return str(dr["id"])


def _make_due(tenant: Tenant, request_id: str) -> None:
    with get_sessionmaker()() as db:
        set_tenant(db, tenant.org_id, None)
        db.execute(
            text("UPDATE deletion_requests SET purge_after = now() - interval '1 day' WHERE id = :id"),
            {"id": request_id},
        )
        db.commit()


def _race(*calls: Callable[[], Any]) -> list[Any]:
    barrier = threading.Barrier(len(calls))
    results: list[Any] = [None] * len(calls)

    def run(i: int, fn: Callable[[], Any]) -> None:
        barrier.wait()
        try:
            results[i] = fn()
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion
            results[i] = exc

    threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    return results


# ------------------------------------------------------------ SEC3-02: records that already exist
def test_sec3_12_existing_quote_of_an_erased_customer_cannot_start_a_new_order(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    _approve_erasure(app, tenant_a)
    r = sales.post(f"/quotes/{q['id']}/orders", json={})
    assert r.status_code in (404, 409), (r.status_code, r.text[:120])


def test_sec3_12_open_proposal_of_an_erased_customer_cannot_become_an_order(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    approver = login_as(app, tenant_a.users["approver"])
    seed_confirmed_orders(tenant_a, [40, 20])
    proposal = sales.post(f"/customers/{tenant_a.customer_id}/order-proposals").json()
    _approve_erasure(app, tenant_a)
    r = approver.post(f"/order-proposals/{proposal['id']}/approve", json={})
    assert r.status_code in (404, 409), (r.status_code, r.text[:120])


def test_sec3_13_quote_list_does_not_show_the_name_of_a_customer_pending_erasure(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    assert sales.get("/quotes").json()[0]["customer_name"], "precondition: the name is shown before the approval"
    _approve_erasure(app, tenant_a)
    names = {row["customer_name"] for row in sales.get("/quotes").json()}
    assert names <= {None, ""}, names


# ---------------------------------------------------------------- SEC3-01: stalled purge
def test_sec3_01_stalled_purge_is_requeued_then_flagged_and_a_hold_still_blocks(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    rid = _approve_erasure(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    _make_due(tenant_a, rid)
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        for i in range(retention.MAX_PURGE_JOBS):
            db.add(
                JobRun(
                    organization_id=tenant_a.org_id,
                    kind="purge_customer",
                    idempotency_key=f"purge:{rid}:x{i}",
                    payload={"request_id": rid},
                    status="dead",
                    max_attempts=5,
                    next_run_at=utcnow(),
                )
            )
        db.commit()
        assert retention.enqueue_due_purges(db) == 0  # bounded: no eleventh job
        set_tenant(db, tenant_a.org_id, None)
        req = db.get(DeletionRequest, uuid.UUID(rid))
        assert req is not None and req.summary.get("purge_stalled") is True and req.status == "approved"
        n_jobs = db.scalar(text("SELECT count(*) FROM job_runs WHERE kind = 'purge_customer'"))
        assert n_jobs == retention.MAX_PURGE_JOBS
        db.rollback()
    # a legal hold keeps the request out of the scan entirely
    assert admin.post(f"/retention/requests/{rid}/legal-hold", json={"hold": True}).status_code == 200
    with get_sessionmaker()() as db:
        assert retention.enqueue_due_purges(db) == 0
        db.rollback()


def test_sec3_14_a_crashed_worker_cannot_stall_an_approved_erasure_silently(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    rid = _approve_erasure(app, tenant_a)
    _make_due(tenant_a, rid)
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        db.add(
            JobRun(
                organization_id=tenant_a.org_id,
                kind="purge_customer",
                idempotency_key=f"purge:{rid}",
                payload={"request_id": rid},
                status="running",
                max_attempts=5,
                next_run_at=utcnow(),
                locked_by="crashed-worker",
                locked_at=utcnow() - timedelta(days=3),
            )
        )
        db.commit()
        queued = retention.enqueue_due_purges(db)
        set_tenant(db, tenant_a.org_id, None)
        req = db.get(DeletionRequest, uuid.UUID(rid))
        flagged = req is not None and bool(req.summary.get("purge_stalled"))
        db.rollback()
    assert queued == 1 or flagged, (queued, flagged)


# ------------------------------------------------------------------- SEC3-03: purge completeness
def test_sec3_03_purge_removes_contacts_drafts_rfq_text_pdf_and_snapshot_identifiers(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    contact = add_contact(sales, str(tenant_a.customer_id), email="subject.person@erasure.example")
    q = mark_sent(sales, q)
    sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": contact["id"]})
    pdf = sales.get(f"/quotes/{q['id']}/versions/1/pdf")
    assert pdf.status_code == 200
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        key = db.scalar(select(QuoteVersion.pdf_storage_key))
        db.rollback()
    assert key and get_storage().exists(key), "precondition: a stored PDF exists"
    sales.post(
        "/rfqs",
        json={
            "text": "From Subject Person subject.person@erasure.example\n10 pcs SKU-1001",
            "customer_id": str(tenant_a.customer_id),
        },
    )
    rid = _approve_erasure(app, tenant_a)
    _make_due(tenant_a, rid)
    with get_sessionmaker()() as db:
        assert retention.enqueue_due_purges(db) == 1 and queue.run_once(db, "w") == 1
        set_tenant(db, tenant_a.org_id, None)
        assert db.scalar(text("SELECT count(*) FROM contacts")) == 0
        assert db.scalar(text("SELECT count(*) FROM communication_drafts")) == 0
        assert db.scalar(text("SELECT count(*) FROM rfqs WHERE pasted_text IS NOT NULL")) == 0
        snap = str(db.scalars(select(QuoteVersion.document_snapshot)).all())
        assert "Alpha Trading Customer" not in snap and "Deleted customer" in snap
        assert db.scalar(select(QuoteVersion.pdf_storage_key)) is None
        db.rollback()
    assert not get_storage().exists(key)
    # the PDF is re-rendered from the pseudonymised snapshot, not from the live customer
    again = sales.get(f"/quotes/{q['id']}/versions/1/pdf")
    assert again.status_code == 200 and b"Alpha Trading Customer" not in again.content


def test_sec3_15_purge_clears_the_sent_message_snippet_that_names_the_contact(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    contact = add_contact(sales, str(tenant_a.customer_id))
    q = mark_sent(sales, q)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": contact["id"]}).json()
    assert sales.post(f"/drafts/{d['id']}/send").status_code == 200
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        assert "Buyer Person" in str(db.scalars(select(CommunicationEvent.snippet)).all()), "precondition"
        db.rollback()
    rid = _approve_erasure(app, tenant_a)
    _make_due(tenant_a, rid)
    with get_sessionmaker()() as db:
        assert retention.enqueue_due_purges(db) == 1 and queue.run_once(db, "w") == 1
        set_tenant(db, tenant_a.org_id, None)
        left = [s for s in db.scalars(select(CommunicationEvent.snippet)).all() if s and "Buyer Person" in s]
        db.rollback()
    assert left == [], left


# ------------------------------------------------------------------ concurrency / cross-tenant probes
def test_sec3_17_two_concurrent_confirmations_of_one_order_are_recorded_once(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    approver = login_as(app, tenant_a.users["approver"])
    owner = login_as(app, tenant_a.users["owner"])
    admin = login_as(app, tenant_a.users["admin"])
    duplicates = 0
    for attempt in range(8):  # the race window is narrow: a clean round proves nothing, a duplicate does
        r = upload(
            admin,
            f"/data/import/orders?idempotency_key=conf-{attempt}",
            "o.csv",
            f"order_ref,sku,quantity,unit_price\r\nR-{attempt},A,1,1\r\n".encode(),
        )
        assert r.status_code == 201 and r.json()["applied"] == 1, r.text[:200]
        oid = next(o["id"] for o in admin.get("/orders").json() if o["status"] == "proposed")
        res = _race(
            lambda oid=oid: approver.post(f"/orders/{oid}/confirm", json={}).status_code,
            lambda oid=oid: owner.post(f"/orders/{oid}/confirm", json={}).status_code,
        )
        assert all(isinstance(x, int) and x < 500 for x in res), res
        with get_sessionmaker()() as db:
            set_tenant(db, tenant_a.org_id, None)
            o = db.get(Order, uuid.UUID(oid))
            assert o is not None
            duplicates += max(0, len([e for e in o.events if e.kind == "confirmed"]) - 1)
            db.rollback()
    assert duplicates == 0, f"{duplicates} duplicate 'confirmed' events in 8 rounds"


def test_sec3_16_connector_sync_replayed_concurrently_is_applied_once_without_a_500(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    admin2 = login_as(app, tenant_a.users["owner"])
    conn = _connector(admin, ["read:orders"])
    failures: list[object] = []
    for attempt in range(8):
        body = {
            "entity": "orders",
            "idempotency_key": f"race-{attempt}",
            "records": [{"external_ref": f"RACE-{attempt}", "lines": [{"sku": "A", "quantity": 1, "unit_price": 1}]}],
        }
        res = _race(
            lambda body=body: admin.post(f"/connectors/{conn['id']}/sync", json=body).status_code,
            lambda body=body: admin2.post(f"/connectors/{conn['id']}/sync", json=body).status_code,
        )
        with get_sessionmaker()() as db:
            set_tenant(db, tenant_a.org_id, None)
            n = db.scalar(text("SELECT count(*) FROM orders WHERE external_ref = :r"), {"r": f"RACE-{attempt}"})
            db.rollback()
        assert n == 1, (res, n)  # never applied twice
        failures += [r for r in res if r not in (200, 201, 409)]
    assert failures == [], failures


def test_one_active_connector_per_kind_is_enforced_by_the_database(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    first = admin.post("/connectors", json={"kind": "erp_stub", "name": "one", "scopes": ["read:stock"]})
    assert first.status_code == 201
    with get_sessionmaker()() as db:  # bypass the service-level check: the partial unique index must still refuse
        set_tenant(db, tenant_a.org_id, None)
        with pytest.raises(Exception, match="uq_connector_org_kind_active"):
            db.execute(
                text(
                    "INSERT INTO connector_connections (id, organization_id, kind, name, scopes, status, config) "
                    "VALUES (gen_random_uuid(), :o, 'erp_stub', 'dup', '[]', 'active', '{}')"
                ),
                {"o": str(tenant_a.org_id)},
            )
        db.rollback()
    # disable / enable round trip: the second active one is refused until the first is disabled, and both are audited
    cid = first.json()["id"]
    other = login_as(app, tenant_b.users["admin"])
    assert other.post(f"/connectors/{cid}/enable").status_code == 404  # new route: 404 matrix extension
    assert other.post(f"/connectors/{cid}/disable").status_code == 404
    assert admin.post(f"/connectors/{cid}/disable").status_code == 200
    second = admin.post("/connectors", json={"kind": "erp_stub", "name": "two", "scopes": ["read:stock"]})
    assert second.status_code == 201
    assert admin.post(f"/connectors/{cid}/enable").status_code == 409
    assert admin.post(f"/connectors/{second.json()['id']}/disable").status_code == 200
    assert admin.post(f"/connectors/{cid}/enable").status_code == 200
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        actions = set(db.scalars(text("SELECT action FROM audit_events WHERE action LIKE 'connector.%'")).all())
        db.rollback()
    assert {"connector.create", "connector.disable", "connector.enable"} <= actions


def test_probe_csv_import_cannot_reference_another_tenants_customer(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin_a = login_as(app, tenant_a.users["admin"])
    admin_b = login_as(app, tenant_b.users["admin"])
    ref = "SHARED-REF-1"
    admin_b.post("/customers", json={"name": "Beta Secret Buyer", "reference": ref})
    r = upload(
        admin_a,
        "/data/import/orders?idempotency_key=x-tenant",
        "o.csv",
        f"order_ref,customer_reference,sku,quantity,unit_price\r\nPO-X,{ref},SKU-1001,1,1\r\n".encode(),
    )
    assert r.status_code == 201 and r.json()["applied"] == 0 and r.json()["rejected"] == 1
    failed = admin_a.get(f"/data/runs/{r.json()['id']}/failed.csv").content.decode()
    assert "Beta Secret Buyer" not in failed and "unknown customer_reference" in failed


def test_same_idempotency_key_with_a_different_payload_is_refused_without_side_effects(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    conn = _connector(admin, ["read:orders"])

    def push(ref: str) -> Any:
        return admin.post(
            f"/connectors/{conn['id']}/sync",
            json={
                "entity": "orders",
                "idempotency_key": "k1",
                "records": [{"external_ref": ref, "lines": [{"sku": "A", "quantity": 1, "unit_price": 1}]}],
            },
        )

    assert push("K-1").status_code == 201
    assert push("K-1").status_code == 201  # identical payload: replay of the first outcome
    assert push("K-2").status_code == 409  # different payload under the same key
    csv_a = b"order_ref,sku,quantity,unit_price\r\nC-1,A,1,1\r\n"
    assert upload(admin, "/data/import/orders?idempotency_key=k2", "o.csv", csv_a).status_code == 201
    other = csv_a.replace(b"C-1", b"C-2")
    assert upload(admin, "/data/import/orders?idempotency_key=k2", "o.csv", other).status_code == 409
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        refs = set(db.scalars(text("SELECT external_ref FROM orders")).all())
        runs = db.scalar(text("SELECT count(*) FROM sync_runs"))
        db.rollback()
    assert refs == {"K-1", "C-1"} and runs == 2, (refs, runs)
