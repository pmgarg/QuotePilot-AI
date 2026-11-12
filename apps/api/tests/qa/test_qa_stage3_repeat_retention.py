"""QA (independent) — S3-F03 repeat-order assistant (S3-A08 audit trail, proposals never auto-execute),
S3-F08 retention/deletion workflow (requester ≠ approver, legal hold, worker purge), S3-F01 profile visibility,
plus a Stage 1/2 regression check that the deletion workflow interacts correctly with follow-ups."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.session import get_sessionmaker, set_tenant
from app.models.customer import Customer
from app.models.followup import Contact, FollowUpSchedule
from app.models.intelligence import DeletionRequest
from app.services.intelligence import retention
from app.services.jobs import queue
from tests.conftest import Tenant, login_as
from tests.qa.test_qa_stage3_helpers import approved_quote, audit_rows, run_sql, seed_confirmed_order, transition

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------- S3-F03 / S3-A08
def test_proposal_is_never_executed_and_leaves_a_complete_audit_trail(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    approver = login_as(app, tenant_a.users["approver"])
    owner = login_as(app, tenant_a.users["owner"])
    readonly = login_as(app, tenant_a.users["readonly"])
    cid = tenant_a.customer_id
    # history: 400 and 600 pieces, 60 and 30 days ago → median qty 500, interval 30 d, predicted today
    seed_confirmed_order(tenant_a, customer_id=cid, days_ago=60, qty="400")
    seed_confirmed_order(tenant_a, customer_id=cid, days_ago=30, qty="600")
    assert readonly.post(f"/customers/{cid}/order-proposals").status_code == 403
    before = len(owner.get("/orders").json())
    p = sales.post(f"/customers/{cid}/order-proposals")
    assert p.status_code == 201, p.text
    p = p.json()
    assert p["status"] == "proposed" and p["order_id"] is None and p["kind"] == "repeat"
    (line,) = p["lines"]
    assert (line["sku"], Decimal(line["quantity"]), line["times_ordered"], line["median_interval_days"]) == (
        "SKU-1001",
        Decimal("500"),
        2,
        30,
    )
    assert any("this organisation" in e for e in p["evidence"]) and "cost" not in str(p).lower()
    assert len(owner.get("/orders").json()) == before  # nothing was ordered
    assert sales.get(f"/customers/{cid}/profile").json()["open_proposals"] == 1
    assert [x["id"] for x in readonly.get("/order-proposals?status=proposed").json()] == [p["id"]]
    # decisions: approve_order only; rejection needs a reason
    assert sales.post(f"/order-proposals/{p['id']}/approve", json={}).status_code == 403
    assert readonly.post(f"/order-proposals/{p['id']}/reject", json={"reason": "no"}).status_code == 403
    assert approver.post(f"/order-proposals/{p['id']}/reject", json={"reason": " "}).status_code == 422
    a = approver.post(f"/order-proposals/{p['id']}/approve", json={"reason": "customer confirmed by phone"})
    assert a.status_code == 200 and a.json()["status"] == "converted" and a.json()["decided_by"] == approver.user_id
    order = approver.get(f"/orders/{a.json()['order_id']}").json()
    assert order["status"] == "proposed" and order["source"] == "repeat" and p["id"] in order["notes"]
    assert order["lines"][0]["unit_price"] == "0.85" and Decimal(order["lines"][0]["quantity"]) == 500
    assert order["created_by"] == approver.user_id
    # the approving approver proposed the order and therefore cannot confirm it; the owner can
    assert approver.post(f"/orders/{order['id']}/confirm", json={}).status_code == 403
    assert owner.post(f"/orders/{order['id']}/confirm", json={}).json()["status"] == "confirmed"
    assert approver.post(f"/order-proposals/{p['id']}/reject", json={"reason": "late"}).status_code == 409
    trail = [(r["action"], r["actor"]) for r in audit_rows(tenant_a.org_id, "order")]
    assert trail[0] == ("order_proposal.generate", sales.user_id) and trail[-1] == ("order.confirmed", owner.user_id)
    # approval and the resulting proposed order are written in the same transaction (either order is acceptable)
    assert sorted(trail[1:3]) == sorted(
        [("order_proposal.approve", approver.user_id), ("order.propose", approver.user_id)]
    )
    assert len(trail) == 4
    approve_row = audit_rows(tenant_a.org_id, "order_proposal.approve")[0]
    assert approve_row["summary"] == {"reason": "customer confirmed by phone", "order_id": order["id"]}
    assert approve_row["entity_id"] == p["id"]


def test_proposals_only_when_own_history_predicts_a_reorder_soon(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    approver = login_as(app, tenant_a.users["approver"])
    cid = tenant_a.customer_id
    # a single order is no pattern; two orders 30 days apart whose next date is 20 days out are outside the horizon
    seed_confirmed_order(tenant_a, customer_id=cid, days_ago=40)
    assert sales.post(f"/customers/{cid}/order-proposals").json() is None
    seed_confirmed_order(tenant_a, customer_id=cid, days_ago=10)
    assert sales.post(f"/customers/{cid}/order-proposals").json() is None
    # a proposed (unconfirmed) order is not history
    _, _, q = approved_quote(app, tenant_a, sales=sales, approver=approver)
    sales.post(f"/quotes/{q['id']}/orders", json={})
    assert sales.post(f"/customers/{cid}/order-proposals").json() is None
    assert audit_rows(tenant_a.org_id, "order_proposal") == []
    # third order makes the median gap 15 days → predicted in 5 days → proposal; reject, then regenerate works
    seed_confirmed_order(tenant_a, customer_id=cid, days_ago=25)
    p = sales.post(f"/customers/{cid}/order-proposals").json()
    assert p and p["lines"][0]["times_ordered"] == 3
    assert sales.post(f"/customers/{cid}/order-proposals").status_code == 409
    rj = approver.post(f"/order-proposals/{p['id']}/reject", json={"reason": "customer paused purchasing"})
    assert rj.status_code == 200 and rj.json()["status"] == "rejected" and rj.json()["order_id"] is None
    assert sales.post(f"/customers/{cid}/order-proposals").status_code == 201
    assert [r["summary"]["reason"] for r in audit_rows(tenant_a.org_id, "order_proposal.reject")] == [
        "customer paused purchasing"
    ]
    assert sales.get("/orders?status=proposed").json()[0]["source"] == "quote"  # still only the quote order


def test_proposal_prices_are_labelled_when_no_catalog_price_exists(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    approver = login_as(app, tenant_a.users["approver"])
    usd = sales.post("/customers", json={"name": "Dollar Buyer", "default_currency": "USD"}).json()
    cid = uuid.UUID(usd["id"])
    for d in (50, 25):
        seed_confirmed_order(tenant_a, customer_id=cid, days_ago=d, sku="SKU-1001", unit_price="0.80", currency="USD")
    p = sales.post(f"/customers/{cid}/order-proposals").json()
    assert p["currency"] == "USD" and p["lines"][0]["last_unit_price"] == "0.800000"
    a = approver.post(f"/order-proposals/{p['id']}/approve", json={}).json()
    order = approver.get(f"/orders/{a['order_id']}").json()
    ln = order["lines"][0]
    assert order["currency"] == "USD" and ln["product_id"] is None and ln["unit_price"] == "0.8"
    assert "priced from last order" in ln["description"] and "verify" in ln["description"]
    assert "cost_price" in ln and ln["cost_price"] is None  # nothing invented for margin either


# ---------------------------------------------------------------- S3-F08 retention
def test_requester_cannot_approve_and_legal_hold_blocks_approval_and_purge(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    owner = login_as(app, tenant_a.users["owner"])
    admin = login_as(app, tenant_a.users["admin"])
    sales = login_as(app, tenant_a.users["sales"])
    readonly = login_as(app, tenant_a.users["readonly"])
    cid = str(tenant_a.customer_id)
    assert readonly.post("/retention/requests", json={"customer_id": cid, "reason": "erase"}).status_code == 403
    assert owner.post("/retention/requests", json={"customer_id": cid, "reason": "no"}).status_code == 422  # ≥ 3 chars
    req = owner.post("/retention/requests", json={"customer_id": cid, "reason": "GDPR art. 17 request"}).json()
    assert req["status"] == "requested" and req["requested_by"] == owner.user_id and req["legal_hold"] is False
    assert owner.post(f"/retention/requests/{req['id']}/approve", json={}).status_code == 403  # own request
    for ac in (sales, readonly):
        assert ac.get("/retention/requests").status_code == 403
        assert ac.post(f"/retention/requests/{req['id']}/legal-hold", json={"hold": True}).status_code == 403
    assert admin.post(f"/retention/requests/{req['id']}/legal-hold", json={"hold": True}).json()["legal_hold"] is True
    assert admin.post(f"/retention/requests/{req['id']}/approve", json={}).status_code == 409
    assert admin.post(f"/retention/requests/{req['id']}/legal-hold", json={"hold": False}).json()["legal_hold"] is False
    ap = admin.post(f"/retention/requests/{req['id']}/approve", json={})
    assert ap.status_code == 200 and ap.json()["status"] == "approved" and ap.json()["decided_by"] == admin.user_id
    assert admin.post(f"/retention/requests/{req['id']}/approve", json={}).status_code == 409
    assert owner.post("/retention/requests", json={"customer_id": cid, "reason": "again please"}).status_code == 409
    # hold placed *after* approval keeps the worker away even when the grace period is over
    admin.post(f"/retention/requests/{req['id']}/legal-hold", json={"hold": True})
    run_sql(
        tenant_a.org_id,
        "UPDATE deletion_requests SET purge_after = now() - interval '1 hour' WHERE id = :id",
        {"id": req["id"]},
    )
    with get_sessionmaker()() as db:
        assert retention.enqueue_due_purges(db) == 0
        assert queue.run_once(db, "qa-worker") == 0
    assert sales.get(f"/customers/{cid}").json()["name"] == "Alpha Trading Customer"  # not purged
    admin.post(f"/retention/requests/{req['id']}/legal-hold", json={"hold": False})
    with get_sessionmaker()() as db:
        assert retention.enqueue_due_purges(db) == 1
        assert queue.run_once(db, "qa-worker") == 1
    gone = sales.get(f"/customers/{cid}").json()
    assert gone["name"] == f"Deleted customer {cid[:8]}" and gone["email"] is None and gone["reference"] is None
    final = next(r for r in admin.get("/retention/requests").json() if r["id"] == req["id"])
    assert final["status"] == "executed" and final["executed_at"]
    assert admin.post(f"/retention/requests/{req['id']}/legal-hold", json={"hold": True}).status_code == 409
    purged = audit_rows(tenant_a.org_id, "retention.purged")
    assert len(purged) == 1 and purged[0]["actor"] is None and purged[0]["summary"]["customer_id"] == cid
    holds = [r["summary"]["hold"] for r in audit_rows(tenant_a.org_id, "retention.legal_hold")]
    assert holds == [True, False, True, False]


def test_self_approval_switch_also_governs_deletion_requests(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    owner = login_as(app, tenant_a.users["owner"])
    req = owner.post("/retention/requests", json={"customer_id": str(tenant_a.customer_id), "reason": "test"}).json()
    assert owner.post(f"/retention/requests/{req['id']}/approve", json={}).status_code == 403
    assert owner.patch("/org", json={"allow_self_approval": True}).status_code == 200
    assert owner.post(f"/retention/requests/{req['id']}/approve", json={}).status_code == 200


def test_rejection_reopens_the_subject_and_is_audited(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    owner = login_as(app, tenant_a.users["owner"])
    cid = str(tenant_a.customer_id)
    req = sales.post("/retention/requests", json={"customer_id": cid, "reason": "customer asked"}).json()
    assert owner.post(f"/retention/requests/{req['id']}/reject", json={}).status_code == 422
    rj = owner.post(f"/retention/requests/{req['id']}/reject", json={"reason": "open invoices"})
    assert (
        rj.status_code == 200
        and rj.json()["status"] == "rejected"
        and rj.json()["summary"] == {"rejection_reason": "open invoices"}
    )
    assert owner.post(f"/retention/requests/{req['id']}/approve", json={}).status_code == 409
    assert sales.get(f"/customers/{cid}").json()["deleted_at"] is None
    assert cid in {c["id"] for c in sales.get("/customers").json()}
    assert (
        sales.post("/retention/requests", json={"customer_id": cid, "reason": "customer asked again"}).status_code
        == 201
    )
    assert (
        sales.post("/retention/requests", json={"customer_id": str(uuid.uuid4()), "reason": "ghost"}).status_code == 404
    )
    assert [r["action"] for r in audit_rows(tenant_a.org_id, "retention.")] == [
        "retention.request",
        "retention.reject",
        "retention.request",
    ]


def test_approval_stops_processing_immediately_and_keeps_commercial_records(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    admin = login_as(app, tenant_a.users["admin"])
    approver = login_as(app, tenant_a.users["approver"])
    cid = str(tenant_a.customer_id)
    c = sales.post(
        f"/customers/{cid}/contacts", json={"full_name": "Buyer", "email": "buyer@alpha.example", "is_primary": True}
    )
    assert c.status_code == 201, c.text
    _, _, q = approved_quote(app, tenant_a, sales=sales, approver=approver)
    q = transition(sales, q, "sent")
    plan = sales.post(f"/quotes/{q['id']}/follow-up/plan")
    assert plan.status_code == 201, plan.text  # idempotent: the "sent" transition may already have planned them
    pending_before = [s for s in sales.get("/follow-up/schedules").json() if s["status"] == "pending"]
    assert pending_before
    order = sales.post(f"/quotes/{q['id']}/orders", json={}).json()
    req = sales.post("/retention/requests", json={"customer_id": cid, "reason": "erasure requested"}).json()
    ap = admin.post(f"/retention/requests/{req['id']}/approve", json={}).json()
    soft = ap["summary"]["soft_deleted"]
    assert soft["contacts_opted_out"] == 1 and soft["schedules_cancelled"] == len(pending_before)
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        assert db.scalar(select(Customer.deleted_at).where(Customer.id == tenant_a.customer_id)) is not None
        statuses = {
            s.status
            for s in db.scalars(select(FollowUpSchedule).where(FollowUpSchedule.quote_id == uuid.UUID(q["id"])))
        }
        assert statuses <= {"cancelled"}
        assert all(
            ct.opted_out_at for ct in db.scalars(select(Contact).where(Contact.customer_id == tenant_a.customer_id))
        )
        db.rollback()
    # hidden from lists and exports; profile and records remain reachable for record-keeping
    assert cid not in {x["id"] for x in sales.get("/customers").json()}
    assert cid not in admin.get("/data/export/customers").content.decode("utf-8-sig")
    assert sales.get(f"/customers/{cid}/profile").json()["customer"]["deleted_at"]
    assert sales.get(f"/quotes/{q['id']}").status_code == 200 and sales.get(f"/orders/{order['id']}").status_code == 200
    assert sales.post(f"/customers/{cid}/order-proposals").status_code == 404
    # no new processing may start for a subject whose erasure was approved
    rfq = sales.post("/rfqs", json={"text": "1 pcs SKU-1001", "customer_id": cid})
    contact = sales.post(f"/customers/{cid}/contacts", json={"full_name": "New Buyer", "email": "new@alpha.example"})
    outcomes = {"new RFQ": rfq.status_code, "new contact": contact.status_code}
    assert all(code in (404, 409) for code in outcomes.values()), (
        f"deletion-approved customer still accepts new processing: {outcomes} (expected 404/409)"
    )


def test_worker_purge_is_idempotent_and_scoped_to_the_request(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    admin = login_as(app, tenant_a.users["admin"])
    keep = sales.post("/customers", json={"name": "Keep Me", "email": "keep@alpha.example"}).json()
    sales.post(f"/customers/{keep['id']}/contacts", json={"full_name": "Keeper", "email": "keeper@alpha.example"})
    sales.post(f"/customers/{tenant_a.customer_id}/contacts", json={"full_name": "Gone", "email": "gone@alpha.example"})
    _, _, q = approved_quote(app, tenant_a, sales=sales)
    req = sales.post("/retention/requests", json={"customer_id": str(tenant_a.customer_id), "reason": "erase"}).json()
    admin.post(f"/retention/requests/{req['id']}/approve", json={})
    run_sql(
        tenant_a.org_id,
        "UPDATE deletion_requests SET purge_after = now() - interval '1 day' WHERE id = :id",
        {"id": req["id"]},
    )
    with get_sessionmaker()() as db:
        assert retention.enqueue_due_purges(db) == 1
        assert retention.enqueue_due_purges(db) == 0
        assert queue.run_once(db, "qa-worker") == 1
        assert queue.run_once(db, "qa-worker") == 0
        set_tenant(db, tenant_a.org_id, None)
        assert db.scalar(select(DeletionRequest.status).where(DeletionRequest.id == uuid.UUID(req["id"]))) == "executed"
        assert db.scalar(select(Contact).where(Contact.customer_id == tenant_a.customer_id)) is None
        assert db.scalar(select(Contact).where(Contact.customer_id == uuid.UUID(keep["id"]))) is not None
        pasted = db.execute(select(Customer.name).where(Customer.id == uuid.UUID(keep["id"]))).scalar_one()
        assert pasted == "Keep Me"
        db.rollback()
    assert (
        run_sql(
            tenant_a.org_id,
            "SELECT count(*) FROM rfqs WHERE customer_id = :c AND pasted_text IS NOT NULL",
            {"c": str(tenant_a.customer_id)},
        )[0][0]
        == 0
    )
    assert sales.get(f"/quotes/{q['id']}").status_code == 200
    assert len(audit_rows(tenant_a.org_id, "retention.purged")) == 1


# ---------------------------------------------------------------- S3-F01 profile
def test_customer_profile_stats_and_role_aware_margin(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, approver, q = approved_quote(app, tenant_a)
    owner = login_as(app, tenant_a.users["owner"])
    readonly = login_as(app, tenant_a.users["readonly"])
    q = transition(sales, q, "sent")
    transition(sales, q, "won", "PO received")
    order = sales.post(f"/quotes/{q['id']}/orders", json={}).json()
    approver.post(f"/orders/{order['id']}/confirm", json={})
    cid = str(tenant_a.customer_id)
    for ac in (sales, readonly):
        prof = ac.get(f"/customers/{cid}/profile").json()
        assert prof["stats"] == {
            "quotes": 1,
            "won_quotes": 1,
            "orders": 1,
            "confirmed_orders": 1,
            "last_order_at": prof["stats"]["last_order_at"],
        }
        assert prof["stats"]["last_order_at"] and prof["open_proposals"] == 0
        assert prof["quotes"][0]["effective_status"] == "won" and prof["orders"][0]["status"] == "confirmed"
        assert "cost" not in str(prof).lower() and "margin" not in str(prof).lower()
    # (0.85 − 0.52) / 0.85 × 100 = 38.82… → 38.8 (1 dp)
    for ac in (approver, owner):
        assert ac.get(f"/customers/{cid}/profile").json()["stats"]["avg_margin_pct"] == "38.8"
    assert sales.get(f"/customers/{uuid.uuid4()}/profile").status_code == 404
