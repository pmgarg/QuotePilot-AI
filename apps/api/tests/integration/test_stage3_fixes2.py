"""Regression tests for fix round 2 (SEC3-12..17): edges not covered by tests/security/test_stage3_confirm.py."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select, text

from app.db.session import get_sessionmaker, set_tenant
from app.models.followup import CommunicationEvent
from app.models.intelligence import DeletionRequest
from app.services.comms.providers import reset_simulators
from app.services.intelligence import retention
from app.services.jobs import queue
from tests.conftest import Tenant, login_as
from tests.integration.test_stage2_followup import add_contact, mark_sent
from tests.integration.test_stage3_intelligence import approved_quote, seed_confirmed_orders
from tests.security.test_stage3_confirm import _approve_erasure, _make_due, _race

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _reset_sim() -> None:
    reset_simulators()


def test_erased_customer_name_stays_visible_to_retention_roles_and_hidden_in_search(app: Any, tenant_a: Tenant) -> None:
    sales, _, _q = approved_quote(app, tenant_a)
    owner = login_as(app, tenant_a.users["owner"])
    _approve_erasure(app, tenant_a)
    assert owner.get("/quotes").json()[0]["customer_name"], "owner/admin keep the name (manage_retention)"
    assert owner.get("/quotes", params={"q": "Alpha"}).json() != []
    assert sales.get("/quotes", params={"q": "Alpha"}).json() == []  # a name search must not reveal the quote
    assert sales.get("/quotes").json()[0]["customer_name"] is None  # the quote itself stays listed


def test_open_proposal_of_an_erased_customer_can_still_be_rejected(app: Any, tenant_a: Tenant) -> None:
    sales = login_as(app, tenant_a.users["sales"])
    approver = login_as(app, tenant_a.users["approver"])
    seed_confirmed_orders(tenant_a, [40, 20])
    proposal = sales.post(f"/customers/{tenant_a.customer_id}/order-proposals").json()
    _approve_erasure(app, tenant_a)
    r = approver.post(f"/order-proposals/{proposal['id']}/approve", json={})
    assert r.status_code in (404, 409)
    assert [o for o in approver.get("/orders").json() if o["status"] == "proposed"] == []
    rej = approver.post(f"/order-proposals/{proposal['id']}/reject", json={"reason": "customer erased"})
    assert rej.status_code == 200 and rej.json()["status"] == "rejected"


def test_purge_redacts_event_text_keeps_rows_and_counts_them(app: Any, tenant_a: Tenant) -> None:
    sales, _, q = approved_quote(app, tenant_a)
    contact = add_contact(sales, str(tenant_a.customer_id))
    q = mark_sent(sales, q)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": contact["id"]}).json()
    assert sales.post(f"/drafts/{d['id']}/send").status_code == 200
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        before = len(db.scalars(select(CommunicationEvent.id)).all())
        db.rollback()
    assert before >= 1
    rid = _approve_erasure(app, tenant_a)
    _make_due(tenant_a, rid)
    with get_sessionmaker()() as db:
        assert retention.enqueue_due_purges(db) == 1 and queue.run_once(db, "w") == 1
        set_tenant(db, tenant_a.org_id, None)
        events = db.scalars(select(CommunicationEvent)).all()
        assert len(events) == before  # timeline rows are kept
        assert all(e.subject is None and e.snippet is None for e in events)
        req = db.get(DeletionRequest, uuid.UUID(rid))
        assert req is not None and req.summary["communication_events_redacted"] == before
        audited = db.scalar(
            text("SELECT summary->>'communication_events_redacted' FROM audit_events WHERE action = 'retention.purged'")
        )
        assert audited == str(before)
        db.rollback()


def test_concurrent_confirm_loser_gets_409_with_one_event_and_one_audit_row(app: Any, tenant_a: Tenant) -> None:
    from tests.conftest import upload

    admin = login_as(app, tenant_a.users["admin"])
    approver = login_as(app, tenant_a.users["approver"])
    owner = login_as(app, tenant_a.users["owner"])
    r = upload(
        admin, "/data/import/orders?idempotency_key=fx2", "o.csv", b"order_ref,sku,quantity,unit_price\r\nR,A,1,1\r\n"
    )
    assert r.status_code == 201
    oid = next(o["id"] for o in admin.get("/orders").json() if o["status"] == "proposed")
    res = _race(
        lambda: approver.post(f"/orders/{oid}/confirm", json={}).status_code,
        lambda: owner.post(f"/orders/{oid}/confirm", json={}).status_code,
    )
    assert sorted(res) == [200, 409], res
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        n_events = db.scalar(
            text("SELECT count(*) FROM order_events WHERE order_id = :o AND kind = 'confirmed'"), {"o": oid}
        )
        n_audit = db.scalar(
            text("SELECT count(*) FROM audit_events WHERE action = 'order.confirmed' AND entity_id = :o"), {"o": oid}
        )
        db.rollback()
    assert (n_events, n_audit) == (1, 1)
