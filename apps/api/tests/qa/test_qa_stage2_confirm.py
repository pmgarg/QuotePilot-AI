"""QA re-verification of Stage 2 fix rounds 1-2: the two Critical defects exercised directly through the API
(D-01 address-level opt-out on every route, D-03 exactly-once delivery under a barrier-synchronised burst) and the
D-13 decision (consent withdrawn: due automated follow-up becomes a draft; a queued automated send is refused).
Integration evidence against PostgreSQL with the RLS role and the simulator provider.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.db.session import get_sessionmaker, set_tenant
from app.services.comms.providers import reset_simulators, simulator_for
from app.services.jobs import queue
from tests.conftest import Tenant, login_as
from tests.integration.test_stage2_followup import add_contact, approved_quote, mark_sent
from tests.qa.test_qa_stage2_sending import audit, force_due, worker_tick

pytestmark = [pytest.mark.integration]
ADDR = "buyer@customer.example"


@pytest.fixture(autouse=True)
def _reset_sim() -> None:
    reset_simulators()


def test_confirm_d01_opted_out_address_is_unreachable_by_every_route(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    cust = str(tenant_a.customer_id)
    a = add_contact(sales, cust, email=ADDR)
    other = add_contact(sales, cust, email="other@customer.example", primary=False)
    admin.patch("/follow-up/policy", json={"automation_enabled": True})
    q = mark_sent(sales, q)  # schedules planned (auto_send) while the address is still subscribed
    pre = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": a["id"]})
    assert pre.status_code == 201, pre.text  # a draft created before the opt-out
    assert sales.post(f"/contacts/{a['id']}/opt-out", json={"opted_out": True, "source": "manual"}).status_code == 200
    sim = simulator_for(tenant_a.org_id)

    # (1) same address, other letter case, surrounding blanks, and a plain duplicate: creation refused or never mailable
    created = []
    for variant in ("BUYER@CUSTOMER.EXAMPLE", f"  {ADDR}  ", ADDR):
        r = sales.post(f"/customers/{cust}/contacts", json={"full_name": "Dup", "email": variant, "is_primary": False})
        assert r.status_code in (201, 400, 403, 409, 422), (variant, r.status_code, r.text)
        if r.status_code == 201:
            created.append(r.json())
    for c in created:
        d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]})
        assert d.status_code in (400, 403, 409, 422), (c["email"], d.status_code, d.text)

    # (2) a subscribed contact edited to the opted-out address must not become mailable
    patched = sales.patch(f"/contacts/{other['id']}", json={"email": ADDR.upper()})
    if patched.status_code == 200:
        d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": other["id"]})
        assert d.status_code in (400, 403, 409, 422), d.text

    # (3) the draft created before the opt-out can no longer be sent; neither can it be redirected
    assert sales.post(f"/drafts/{pre.json()['id']}/send").status_code == 403
    assert sales.patch(f"/drafts/{pre.json()['id']}", json={"to_email": ADDR.upper()}).status_code in (
        200,
        400,
        403,
        409,
        422,
    )
    assert sales.post(f"/drafts/{pre.json()['id']}/send").status_code in (400, 403, 409, 422)

    # (4) the automation path: every schedule due, worker runs twice
    force_due(tenant_a.org_id)
    worker_tick()
    worker_tick()
    mailed = [m.to_email for m, _ in sim.outbox]
    assert all(m.strip().lower() != ADDR for m in mailed), mailed
    assert audit(tenant_a.org_id, "draft.sent") == [] or all(
        "buyer@" not in str(r["summary"]).lower() for r in audit(tenant_a.org_id, "draft.sent")
    )


def test_confirm_d01_opt_out_recorded_on_a_case_variant_row_covers_the_original_row(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    cust = str(tenant_a.customer_id)
    a = add_contact(sales, cust, email=ADDR)
    b = sales.post(f"/customers/{cust}/contacts", json={"full_name": "Dup", "email": "BUYER@Customer.example"})
    if b.status_code != 201:
        pytest.skip("duplicate address refused at creation (stronger than required)")
    q = mark_sent(sales, q)
    assert sales.post(f"/contacts/{b.json()['id']}/opt-out", json={"opted_out": True}).status_code == 200
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": a["id"]})
    assert d.status_code in (400, 403, 409, 422), d.text
    assert simulator_for(tenant_a.org_id).outbox == []


def test_confirm_d03_burst_of_barrier_synchronised_sends_delivers_once(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    c = add_contact(sales, str(tenant_a.customer_id))
    q = mark_sent(sales, q)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    sim = simulator_for(tenant_a.org_id)
    n = 8
    names = ["sales", "approver", "admin", "owner"]
    clients = [login_as(app, tenant_a.users[names[i % 4]]) for i in range(n)]
    barrier = threading.Barrier(n)

    def fire(ac):  # type: ignore[no-untyped-def]
        barrier.wait(timeout=30)
        return ac.post(f"/drafts/{d['id']}/send").status_code

    with ThreadPoolExecutor(max_workers=n) as ex:
        codes = list(ex.map(fire, clients))
    assert all(code in (200, 409) for code in codes), codes
    assert codes.count(200) >= 1
    assert len(sim.outbox) == 1, f"{len(sim.outbox)} deliveries for one draft: {codes}"
    assert len(audit(tenant_a.org_id, "draft.sent")) == 1
    events = sales.get(f"/communication-events?quote_id={q['id']}").json()
    assert len([e for e in events if e["kind"] == "sent"]) == 1


def test_confirm_d13_withdrawn_consent_drafts_and_sends_nothing(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    add_contact(sales, str(tenant_a.customer_id))
    admin.patch("/follow-up/policy", json={"automation_enabled": True, "offsets_days": [1]})
    q = mark_sent(sales, q)
    sched = sales.get(f"/follow-up/schedules?quote_id={q['id']}").json()
    assert sched and sched[0]["mode"] == "auto_send"
    admin.patch("/follow-up/policy", json={"automation_enabled": False})
    force_due(tenant_a.org_id)
    worker_tick()
    assert simulator_for(tenant_a.org_id).outbox == []
    s = sales.get(f"/follow-up/schedules?status=all&quote_id={q['id']}").json()[0]
    assert s["status"] == "drafted" and s["mode"] == "draft_only" and s["draft_id"], s
    assert len(audit(tenant_a.org_id, "followup.degraded_to_draft")) == 1
    assert audit(tenant_a.org_id, "draft.sent") == []
    draft = sales.get(f"/drafts/{s['draft_id']}").json()
    assert draft["status"] == "draft"
    assert [j["status"] for j in admin.get("/jobs").json()] == ["succeeded"]
    # an automated send job queued for that draft is refused terminally (one attempt, dead-lettered, nothing sent)
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        assert queue.enqueue(
            db,
            org_id=tenant_a.org_id,
            kind="send_draft",
            idempotency_key="qa-late-automated-send",
            payload={"draft_id": s["draft_id"]},
        )
        db.commit()
    worker_tick()
    send_job = next(j for j in admin.get("/jobs").json() if j["kind"] == "send_draft")
    assert send_job["status"] == "dead" and send_job["attempts"] == 1, send_job
    worker_tick()
    assert simulator_for(tenant_a.org_id).outbox == []
    assert audit(tenant_a.org_id, "draft.sent") == []
