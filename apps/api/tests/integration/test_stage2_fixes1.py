"""Stage 2 fix round 1 regression tests (QA D-01..D-15, security S2-SEC-01..11). Each test names the finding it pins.

Integration tests run against PostgreSQL with the non-owner role (RLS enforced) and the simulator provider.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from app.db.base import utcnow
from app.db.session import get_sessionmaker, set_tenant
from app.models.followup import CommunicationDraft, FollowUpPolicy
from app.models.org import Organization
from app.services.comms import providers
from app.services.comms.providers import (
    InboundMessage,
    OutboundMessage,
    ProviderError,
    SMTPProvider,
    reset_simulators,
    scrub_error,
    simulator_for,
    valid_email,
)
from app.services.followup import service as fu
from app.services.jobs import queue
from tests.conftest import Tenant, login_as
from tests.integration.test_stage2_followup import add_contact, approved_quote, mark_sent

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _reset_sim() -> None:
    reset_simulators()


def sql(org_id: uuid.UUID | None, stmt: str, **params: Any) -> Any:
    with get_sessionmaker()() as db:
        set_tenant(db, org_id, None)
        res = db.execute(text(stmt), params)
        out = res.fetchall() if res.returns_rows else res.rowcount
        db.commit()
        return out


def force_due(org_id: uuid.UUID) -> None:
    sql(org_id, "UPDATE follow_up_schedules SET due_at = now() - interval '1 minute' WHERE status = 'pending'")


def tick() -> int:
    with get_sessionmaker()() as db:
        queue.enqueue_due_schedules(db)
        return queue.run_once(db)


def audit_count(org_id: uuid.UUID, action: str) -> int:
    return int(sql(org_id, "SELECT count(*) FROM audit_events WHERE action = :a", a=action)[0][0])


# ---------------------------------------------------------------- address-level suppression (D-01, D-04, S2-SEC-02)
def test_opt_out_blocks_every_row_with_the_address_on_every_path(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    cid = str(tenant_a.customer_id)
    a = add_contact(sales, cid, email="buyer@customer.example")
    # a draft exists for the first row before the customer opts out
    q = mark_sent(sales, q)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": a["id"]}).json()
    assert sales.post(f"/contacts/{a['id']}/opt-out", json={"opted_out": True, "source": "manual"}).status_code == 200
    # a duplicate row typed in afterwards inherits the opt-out (any case)
    dup = add_contact(sales, cid, email="BUYER@Customer.example", primary=False)
    assert dup["opted_out_at"] and dup["opt_out_source"] == "manual"
    r = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": dup["id"]})
    assert r.status_code == 403 and r.json()["action"] == "opt_out"
    # the stale draft of the first row is suppressed at the gate; automation cannot deliver either
    r = sales.post(f"/drafts/{d['id']}/send")
    assert r.status_code == 403
    admin.patch("/follow-up/policy", json={"automation_enabled": True})
    sales.post(f"/quotes/{q['id']}/follow-up/plan")
    force_due(tenant_a.org_id)
    tick()
    assert simulator_for(tenant_a.org_id).outbox == []


def test_opt_out_reaches_rows_that_exist_when_it_happens_and_opt_in_clears_the_address(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, _ = approved_quote(app, tenant_a)
    owner = login_as(app, tenant_a.users["owner"])
    cid = str(tenant_a.customer_id)
    a = add_contact(sales, cid, email="buyer@customer.example")
    b = add_contact(sales, cid, email="Buyer@customer.example", primary=False)
    other = add_contact(sales, cid, email="other@customer.example", primary=False)
    out = sales.post(f"/contacts/{a['id']}/opt-out", json={"opted_out": True, "source": "manual"})
    assert out.status_code == 200
    rows = {c["id"]: c for c in sales.get(f"/customers/{cid}/contacts").json()}
    assert rows[a["id"]]["opted_out_at"] and rows[b["id"]]["opted_out_at"]
    assert rows[other["id"]]["opted_out_at"] is None  # other addresses are untouched
    # sales cannot undo it through the sibling row either; an owner can, for the whole address
    assert sales.post(f"/contacts/{b['id']}/opt-out", json={"opted_out": False}).status_code == 403
    assert owner.post(f"/contacts/{b['id']}/opt-out", json={"opted_out": False}).status_code == 200
    rows = {c["id"]: c for c in sales.get(f"/customers/{cid}/contacts").json()}
    assert rows[a["id"]]["opted_out_at"] is None and rows[b["id"]]["opted_out_at"] is None


def test_editing_a_contact_to_an_opted_out_address_inherits_the_opt_out(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, _ = approved_quote(app, tenant_a)
    cid = str(tenant_a.customer_id)
    gone = add_contact(sales, cid, email="gone@customer.example")
    sales.post(f"/contacts/{gone['id']}/opt-out", json={"opted_out": True, "source": "reply"})
    fresh = add_contact(sales, cid, email="fresh@customer.example", primary=False)
    assert fresh["opted_out_at"] is None
    r = sales.patch(f"/contacts/{fresh['id']}", json={"full_name": "Fresh", "email": "gone@customer.example"})
    assert r.status_code == 200 and r.json()["opted_out_at"] and r.json()["opt_out_source"] == "reply"


def test_no_follow_ups_preference_blocks_manual_schedule_and_automation_for_the_address(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    cid = str(tenant_a.customer_id)
    quiet = sales.post(
        f"/customers/{cid}/contacts",
        json={"full_name": "Quiet", "email": "quiet@customer.example", "preferred_channel": "none", "is_primary": True},
    ).json()
    # a second row for the same address that says "email" must not re-enable it
    twin = add_contact(sales, cid, email="Quiet@customer.example", primary=False)
    admin.patch("/follow-up/policy", json={"automation_enabled": True})
    q = mark_sent(sales, q)
    for contact in (quiet, twin):
        r = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": contact["id"]})
        assert r.status_code == 403 and r.json()["action"] == "no_follow_ups", r.text
    # with every contact blocked there is no default recipient: the draft has none and cannot be sent
    nobody = sales.post(f"/quotes/{q['id']}/drafts", json={})
    assert nobody.status_code == 201 and nobody.json()["to_email"] is None
    assert sales.post(f"/drafts/{nobody.json()['id']}/send").status_code == 422
    force_due(tenant_a.org_id)
    tick()
    assert simulator_for(tenant_a.org_id).outbox == []
    scheds = sales.get(f"/follow-up/schedules?status=all&quote_id={q['id']}").json()
    assert scheds and all(s["status"] == "suppressed" for s in scheds), scheds


def test_no_follow_ups_preference_set_after_the_draft_exists_suppresses_the_send(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    c = add_contact(sales, str(tenant_a.customer_id))
    q = mark_sent(sales, q)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    assert sales.patch(f"/contacts/{c['id']}", json={"full_name": "x", "preferred_channel": "none"}).status_code == 200
    r = sales.post(f"/drafts/{d['id']}/send")
    assert r.status_code == 403 and r.json()["action"] == "no_follow_ups"
    assert sales.get(f"/drafts/{d['id']}").json()["status"] == "cancelled"
    assert simulator_for(tenant_a.org_id).outbox == []
    assert audit_count(tenant_a.org_id, "draft.suppressed") == 1


def test_unsubscribe_reply_opts_out_all_rows_and_suppresses_their_schedules(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    cid = str(tenant_a.customer_id)
    add_contact(sales, cid, email="buyer@customer.example")
    add_contact(sales, cid, email="BUYER@customer.example", primary=False)
    q = mark_sent(sales, q)
    r = admin.post(
        "/integrations/simulator/inbound",
        json={"from_email": "Buyer@customer.example", "subject": f"Re: {q['number']}", "snippet": "unsubscribe"},
    )
    assert r.status_code == 201 and r.json()["kind"] == "opt_out"
    assert all(c["opted_out_at"] for c in sales.get(f"/customers/{cid}/contacts").json())
    assert sales.get("/follow-up/schedules").json() == []


# ---------------------------------------------------------------- send gate under concurrency (D-03)
def _draft_for_sale(app, tenant: Tenant):  # type: ignore[no-untyped-def]
    sales, approver, q = approved_quote(app, tenant)
    c = add_contact(sales, str(tenant.customer_id))
    q = mark_sent(sales, q)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    return sales, approver, q, c, d


def test_stale_draft_object_cannot_send_twice(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """Deterministic form of QA D-03: a session already holds the draft as 'draft' while another session sends it.
    The lock must re-read the row (populate_existing) so the stale in-memory status is not trusted."""
    sales, _, _q, _c, d = _draft_for_sale(app, tenant_a)
    did = uuid.UUID(d["id"])
    sim = simulator_for(tenant_a.org_id)
    stale = get_sessionmaker()()
    try:
        set_tenant(stale, tenant_a.org_id, None)
        stale_draft = stale.get(CommunicationDraft, did)
        assert stale_draft is not None and stale_draft.status == "draft"
        assert sales.post(f"/drafts/{d['id']}/send").status_code == 200
        org = stale.get(Organization, tenant_a.org_id)
        assert org is not None
        result = fu.send_draft(stale, org=org, draft=stale_draft, actor=None, ip=None, mode="automation")
        status = result.status
        stale.rollback()
        assert status == "sent"
    except Exception:
        stale.rollback()
        raise
    finally:
        stale.close()
    assert len(sim.outbox) == 1


def test_many_concurrent_clicks_deliver_one_message_one_event_one_audit_row(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, approver, _q, _c, d = _draft_for_sale(app, tenant_a)
    clients = [sales, approver, login_as(app, tenant_a.users["admin"]), login_as(app, tenant_a.users["owner"])] * 2
    with ThreadPoolExecutor(max_workers=8) as ex:
        codes = list(ex.map(lambda ac: ac.post(f"/drafts/{d['id']}/send").status_code, clients))
    assert codes == [200] * 8
    assert len(simulator_for(tenant_a.org_id).outbox) == 1
    assert audit_count(tenant_a.org_id, "draft.sent") == 1
    assert sql(tenant_a.org_id, "SELECT count(*) FROM communication_events WHERE kind = 'sent'")[0][0] == 1


# ---------------------------------------------------------------- edited drafts (D-05, D-06, S2-SEC-04)
def test_edited_draft_always_ends_with_the_opt_out_and_has_placeholders_resolved(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q, _c, d = _draft_for_sale(app, tenant_a)
    r = sales.patch(
        f"/drafts/{d['id']}",
        json={
            "subject": "About {quote_number}",
            "body": "Hi {contact_name}, {quote_number} totals {grand_total} {currency}.",
        },
    )
    assert r.status_code == 200
    body = r.json()["body"]
    assert "{" not in body and q["number"] in body and "Buyer Person" in body
    assert r.json()["subject"] == f"About {q['number']}"
    # unknown placeholders are refused in the subject as well as the body
    assert sales.patch(f"/drafts/{d['id']}", json={"subject": "Hi {evil}"}).status_code == 422
    assert sales.post(f"/drafts/{d['id']}/send").status_code == 200
    ((msg, _),) = simulator_for(tenant_a.org_id).outbox
    assert "UNSUBSCRIBE" in msg.body_text and "{" not in msg.body_text and "{" not in msg.subject
    # no duplicate footer when it is already there (idempotent)
    assert msg.body_text.count("UNSUBSCRIBE") == 1


def test_user_supplied_opt_out_placeholder_is_not_duplicated(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, _q, _c, d = _draft_for_sale(app, tenant_a)
    assert sales.patch(f"/drafts/{d['id']}", json={"body": "Short note.\n\n{opt_out_text}"}).status_code == 200
    assert sales.post(f"/drafts/{d['id']}/send").status_code == 200
    ((msg, _),) = simulator_for(tenant_a.org_id).outbox
    assert msg.body_text.count("UNSUBSCRIBE") == 1


def test_draft_recipient_must_be_the_drafts_own_contact(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """QA D-15: the editor refuses what the send gate would refuse, instead of creating an unsendable draft."""
    sales, _, _q, c, d = _draft_for_sale(app, tenant_a)
    add_contact(sales, str(tenant_a.customer_id), email="second@customer.example", primary=False)
    r = sales.patch(f"/drafts/{d['id']}", json={"to_email": "second@customer.example"})
    assert r.status_code == 422
    ok = sales.patch(f"/drafts/{d['id']}", json={"to_email": c["email"].upper()})
    assert ok.status_code == 200 and ok.json()["to_email"] == c["email"]
    assert sales.post(f"/drafts/{d['id']}/send").status_code == 200


# ---------------------------------------------------------------- planning (D-07, D-08, D-09, D-10)
def test_system_send_of_an_approved_quote_plans_follow_ups_from_that_send(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    c = add_contact(sales, str(tenant_a.customer_id))
    admin = login_as(app, tenant_a.users["admin"])
    admin.patch("/follow-up/policy", json={"offsets_days": [3, 9], "max_attempts": 2, "skip_weekends": False})
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    assert sales.post(f"/drafts/{d['id']}/send").status_code == 200
    lc = sales.get(f"/quotes/{q['id']}/lifecycle").json()
    assert lc["status"] == "sent" and [s["attempt_no"] for s in lc["schedules"]] == [1, 2]
    assert all(s["contact_id"] == c["id"] and s["status"] == "pending" for s in lc["schedules"])
    # the message that delivered the quote is not a follow-up: the cap of 2 still allows both attempts
    force_due(tenant_a.org_id)
    admin.patch("/follow-up/policy", json={"automation_enabled": True})
    sql(tenant_a.org_id, "UPDATE follow_up_schedules SET mode = 'auto_send'")
    tick()
    assert len(simulator_for(tenant_a.org_id).outbox) == 3  # the quote itself + 2 follow-ups


def test_follow_up_schedule_times_are_ordered_without_collisions() -> None:
    pol = FollowUpPolicy(
        organization_id=uuid.uuid4(),
        offsets_days=[1, 2, 3, 4, 5],
        max_attempts=5,
        quiet_hours_start=19,
        quiet_hours_end=8,
        timezone="Asia/Dubai",
        skip_weekends=True,
    )
    tz = ZoneInfo("Asia/Dubai")
    # the failing day of QA D-10: Tuesday 2026-11-10 at about 20:50 local
    anchor = datetime(2026, 11, 10, 20, 50, tzinfo=tz).astimezone(UTC)
    previous = None
    for days in pol.offsets_days:
        want = anchor + timedelta(days=days)
        if previous is not None and want < previous + timedelta(days=1):
            want = previous + timedelta(days=1)
        due = fu._next_allowed(want, pol)
        local = due.astimezone(tz)
        assert 8 <= local.hour < 19 and local.weekday() < 5
        assert previous is None or due > previous
        previous = due


@pytest.mark.parametrize(
    ("start", "end", "when", "expected"),
    [
        (19, 8, datetime(2026, 11, 3, 21, 0), datetime(2026, 11, 4, 8, 0)),  # evening → next morning
        (19, 8, datetime(2026, 11, 3, 5, 0), datetime(2026, 11, 3, 8, 0)),  # early morning → same morning
        (1, 6, datetime(2026, 11, 3, 3, 0), datetime(2026, 11, 3, 6, 0)),  # same-day window stays on the same day
        (19, 8, datetime(2026, 11, 7, 10, 0), datetime(2026, 11, 9, 8, 0)),  # Saturday → Monday at quiet end
    ],
)
def test_next_allowed_quiet_hours_and_weekends(start: int, end: int, when: datetime, expected: datetime) -> None:
    pol = FollowUpPolicy(
        organization_id=uuid.uuid4(),
        offsets_days=[1],
        max_attempts=1,
        quiet_hours_start=start,
        quiet_hours_end=end,
        timezone="UTC",
        skip_weekends=True,
    )
    assert fu._next_allowed(when.replace(tzinfo=UTC), pol) == expected.replace(tzinfo=UTC)


def test_new_version_plans_fresh_schedules_with_the_version_in_the_key(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, approver, q = approved_quote(app, tenant_a)
    add_contact(sales, str(tenant_a.customer_id))
    q = mark_sent(sales, q)
    keys1 = {r[0] for r in sql(tenant_a.org_id, "SELECT idempotency_key FROM follow_up_schedules")}
    assert keys1 and all(":v1:" in k for k in keys1)
    nv = sales.post(f"/quotes/{q['id']}/versions", json={"expected_lock_version": q["lock_version"]}).json()
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": nv["lock_version"]}).json()
    a = approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": s["lock_version"]})
    assert a.status_code == 200, a.text
    mark_sent(sales, a.json())
    keys = {r[0] for r in sql(tenant_a.org_id, "SELECT idempotency_key FROM follow_up_schedules")}
    assert keys - keys1 and all(":v2:" in k for k in keys - keys1)


def test_automation_never_exceeds_max_attempts_counting_manual_follow_ups(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    c = add_contact(sales, str(tenant_a.customer_id))
    admin.patch("/follow-up/policy", json={"offsets_days": [2], "max_attempts": 1, "automation_enabled": True})
    q = mark_sent(sales, q)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    assert sales.post(f"/drafts/{d['id']}/send").status_code == 200
    force_due(tenant_a.org_id)
    tick()
    assert len(simulator_for(tenant_a.org_id).outbox) == 1
    s = sales.get(f"/follow-up/schedules?status=all&quote_id={q['id']}").json()[0]
    assert s["status"] == "superseded" and "maximum" in s["last_error"]


# ---------------------------------------------------------------- queue: tenant scope, terminal refusals (S2-SEC-01/03, D-13)
def _due_auto_schedule(app, tenant: Tenant, email: str):  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant)
    admin = login_as(app, tenant.users["admin"])
    add_contact(sales, str(tenant.customer_id), email=email)
    admin.patch("/follow-up/policy", json={"automation_enabled": True, "offsets_days": [1]})
    q = mark_sent(sales, q)
    sql(tenant.org_id, "UPDATE follow_up_schedules SET due_at = now() - interval '1 minute'")
    return sales, admin, q


def test_org_scoped_tick_and_claim_never_touch_another_tenant(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    _due_auto_schedule(app, tenant_b, "buyer@beta.example")
    with get_sessionmaker()() as db:
        assert queue.enqueue_due_schedules(db, org_id=tenant_a.org_id) == 0
        assert queue.run_once(db, org_id=tenant_a.org_id) == 0
    assert sql(tenant_b.org_id, "SELECT count(*) FROM job_runs")[0][0] == 0
    with get_sessionmaker()() as db:
        assert queue.enqueue_due_schedules(db, org_id=tenant_b.org_id) == 1
        assert queue.run_once(db, org_id=tenant_a.org_id) == 0  # B's queued job is invisible to A's claim
    assert simulator_for(tenant_b.org_id).outbox == []
    with get_sessionmaker()() as db:
        assert queue.run_once(db, org_id=tenant_b.org_id, max_jobs=1) == 1
    assert len(simulator_for(tenant_b.org_id).outbox) == 1


def test_run_once_endpoint_processes_only_the_callers_tenant_and_is_audited(
    app, tenant_a: Tenant, tenant_b: Tenant
) -> None:  # type: ignore[no-untyped-def]
    _due_auto_schedule(app, tenant_b, "buyer@beta.example")
    _due_auto_schedule(app, tenant_a, "buyer@customer.example")
    a_admin = login_as(app, tenant_a.users["admin"])
    assert a_admin.post("/jobs/run-once").json() == {"processed": 1}
    assert len(simulator_for(tenant_a.org_id).outbox) == 1 and simulator_for(tenant_b.org_id).outbox == []
    assert sql(tenant_b.org_id, "SELECT count(*) FROM job_runs")[0][0] == 0
    assert audit_count(tenant_a.org_id, "worker.run_once") == 1
    # roles
    assert login_as(app, tenant_a.users["sales"]).post("/jobs/run-once").status_code == 403


def test_consent_withdrawal_degrades_to_draft_and_a_queued_automated_send_is_refused_terminally(
    app, tenant_a: Tenant
) -> None:  # type: ignore[no-untyped-def]
    """Fix round 2 (QA D-13): withdrawing consent after planning turns the due schedule into a draft for a human
    (nothing sent, nothing lost). A policy refusal on the send path itself is still terminal, never retried."""
    sales, admin, q = _due_auto_schedule(app, tenant_a, "buyer@customer.example")
    admin.patch("/follow-up/policy", json={"automation_enabled": False})
    assert tick() == 1
    job = admin.get("/jobs").json()[0]
    assert job["status"] == "succeeded" and job["attempts"] == 1
    s = sales.get(f"/follow-up/schedules?status=all&quote_id={q['id']}").json()[0]
    assert s["status"] == "drafted" and s["mode"] == "draft_only" and s["draft_id"]
    assert simulator_for(tenant_a.org_id).outbox == []
    assert (
        sql(tenant_a.org_id, "SELECT count(*) FROM audit_events WHERE action = 'followup.degraded_to_draft'")[0][0] == 1
    )
    # an automated send job for that draft (e.g. queued before the withdrawal) is refused once and dead-lettered
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        assert queue.enqueue(
            db,
            org_id=tenant_a.org_id,
            kind="send_draft",
            idempotency_key="late-automated-send",
            payload={"draft_id": s["draft_id"]},
        )
        db.commit()
    assert tick() == 1
    send_job = next(j for j in admin.get("/jobs").json() if j["kind"] == "send_draft")
    assert send_job["status"] == "dead" and send_job["attempts"] == 1 and "@" not in send_job["last_error"]
    assert tick() == 0 and simulator_for(tenant_a.org_id).outbox == []
    # the human path still works: the prepared draft can be sent by a person
    assert sales.post(f"/drafts/{s['draft_id']}/send").status_code == 200
    assert len(simulator_for(tenant_a.org_id).outbox) == 1


def test_rate_limit_is_still_retried_with_backoff(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, admin, q = _due_auto_schedule(app, tenant_a, "buyer@customer.example")
    admin.patch("/follow-up/policy", json={"hourly_send_limit": 1})
    c = sales.get(f"/customers/{tenant_a.customer_id}/contacts").json()[0]
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    assert sales.post(f"/drafts/{d['id']}/send").status_code == 200
    tick()
    job = admin.get("/jobs").json()[0]
    assert job["status"] == "queued" and "RateLimited" in job["last_error"]


def test_stuck_pending_schedules_with_jobs_do_not_starve_the_tick(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    _due_auto_schedule(app, tenant_a, "buyer@customer.example")
    _due_auto_schedule(app, tenant_b, "buyer@beta.example")
    # make tenant A's schedule look like a legacy stuck row: pending, job already dead
    sql(tenant_a.org_id, "UPDATE follow_up_schedules SET due_at = now() - interval '10 days'")
    with get_sessionmaker()() as db:
        assert queue.enqueue_due_schedules(db, limit=1) == 1  # limit applies to enqueued jobs
    sql(tenant_a.org_id, "UPDATE job_runs SET status = 'dead'")
    with get_sessionmaker()() as db:
        assert queue.enqueue_due_schedules(db) == 1  # B's schedule, reached by paging past A's stuck row
    assert sql(tenant_b.org_id, "SELECT count(*) FROM job_runs")[0][0] == 1


def test_backoff_is_capped_for_any_attempt_number() -> None:
    assert queue.backoff(1) == timedelta(seconds=30)
    assert queue.backoff(5) == timedelta(seconds=480)
    assert queue.backoff(10_000) == queue.MAX_BACKOFF


# ---------------------------------------------------------------- dev-only endpoints (S2-SEC-11)
def test_dev_endpoints_flag_follows_app_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import Settings

    for env, expected in (("development", True), ("test", True), ("staging", False), ("production", False)):
        monkeypatch.setenv("APP_ENV", env)
        assert Settings().dev_endpoints_enabled is expected, env


def test_policy_exposes_the_dev_flag_to_the_ui(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    assert login_as(app, tenant_a.users["admin"]).get("/follow-up/policy").json()["dev_endpoints_enabled"] is True


# ---------------------------------------------------------------- inbound (D-11, D-12, S2-SEC-05)
@pytest.mark.parametrize(
    ("subject", "snippet", "expected"),
    [
        ("Re: Q-2026-00001", "STOP", True),
        ("Re: Q-2026-00001", "stop.", True),
        ("Stop", "", True),
        ("x", "Please unsubscribe me", True),
        ("x", "I want to opt out", True),
        ("x", "remove me from your list", True),
        ("x", "Could your driver stop by our gate on Monday?", False),
        ("x", "We cannot stop the order now", False),
        ("Bus stop", "Thanks", False),
    ],
)
def test_unsubscribe_detection_is_strict_about_the_word_stop(subject: str, snippet: str, expected: bool) -> None:
    msg = InboundMessage("simulator", "m1", "a@b.example", subject, snippet, utcnow())
    assert fu._is_unsubscribe(msg) is expected


def test_replayed_inbound_message_is_idempotent_for_both_kinds(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    c = add_contact(sales, str(tenant_a.customer_id))
    q = mark_sent(sales, q)
    first: list[uuid.UUID] = []
    for pmid, snippet in (("r-1", "Please unsubscribe me"), ("r-2", "Thanks, reviewing")):
        for _ in range(3):
            with get_sessionmaker()() as db:
                set_tenant(db, tenant_a.org_id, None)
                org = db.get(Organization, tenant_a.org_id)
                assert org is not None
                ev = fu.record_inbound(
                    db,
                    org=org,
                    msg=InboundMessage("simulator", pmid, c["email"], f"Re: {q['number']}", snippet, utcnow()),
                )
                db.commit()
                assert ev is not None
                first.append(ev.id)
    assert len(set(first)) == 2
    assert sql(tenant_a.org_id, "SELECT count(*) FROM communication_events WHERE direction = 'in'")[0][0] == 2


def test_quote_number_from_an_unknown_sender_neither_acknowledges_nor_suppresses(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    c = add_contact(sales, str(tenant_a.customer_id))
    q = mark_sent(sales, q)
    r = admin.post(
        "/integrations/simulator/inbound",
        json={"from_email": "stranger@elsewhere.example", "subject": f"FW: {q['number']}", "snippet": "fyi"},
    )
    assert r.status_code == 201 and r.json()["quote_id"] is None
    assert sales.get(f"/quotes/{q['id']}/lifecycle").json()["status"] == "sent"
    assert sales.get(f"/follow-up/schedules?quote_id={q['id']}").json()  # still pending
    # the customer's own contact still acknowledges
    r = admin.post(
        "/integrations/simulator/inbound",
        json={"from_email": c["email"], "subject": f"Re: {q['number']}", "snippet": "thanks"},
    )
    assert r.json()["quote_id"] == q["id"]
    assert sales.get(f"/quotes/{q['id']}/lifecycle").json()["status"] == "acknowledged"


def test_simulator_inbound_validates_the_sender_address(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    r = admin.post(
        "/integrations/simulator/inbound", json={"from_email": "bad-address", "subject": "s", "snippet": "x"}
    )
    assert r.status_code == 422


# ---------------------------------------------------------------- integrations (D-14, S2-SEC-07)
def test_misconfigured_provider_sync_leaves_a_visible_error_and_an_audit_row(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    assert admin.post("/integrations", json={"provider": "smtp"}).status_code == 201
    r = admin.post("/integrations/sync-inbound")
    assert r.status_code in (400, 503) and "SMTP" in r.text
    conn = admin.get("/integrations").json()[0]
    assert conn["last_error"] and "SMTP" in conn["last_error"]
    assert conn["status"] == "active"  # still the selected provider: no silent fall-back to the simulator
    assert audit_count(tenant_a.org_id, "integration.sync_failed") == 1


def test_sync_inbound_success_is_audited_with_the_processed_count(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    assert admin.post("/integrations/sync-inbound").json() == {"processed": 0}
    assert audit_count(tenant_a.org_id, "integration.sync") == 1


def test_integration_account_email_is_validated(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    r = admin.post("/integrations", json={"provider": "simulator", "account_email": "nope"})
    assert r.status_code == 422


# ---------------------------------------------------------------- input hygiene (S2-SEC-09/10) and error text (O-2)
@pytest.mark.parametrize("address", ["a@b.example\n", "a@b.example\r\n", "a@b.example ", "\na@b.example", "a@b"])
def test_valid_email_rejects_trailing_newline_and_whitespace(address: str) -> None:
    assert valid_email(address) is False
    assert valid_email("a@b.example") is True


def test_sender_name_with_control_characters_is_a_422(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    for bad in ("Sara\r\nBcc: x@evil.example", "Sara\nX", "Sara\x00", "Sara\x7f"):
        assert admin.patch("/follow-up/policy", json={"sender_name": bad}).status_code == 422, repr(bad)
    assert admin.patch("/follow-up/policy", json={"sender_name": "Sara Ahmed"}).json()["sender_name"] == "Sara Ahmed"


def test_smtp_header_refusal_is_a_non_retryable_provider_error_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    class Boom:
        def __init__(self, *a: object, **k: object) -> None:
            raise AssertionError("SMTP must not be reached when headers are refused")

    monkeypatch.setattr(providers.smtplib, "SMTP", Boom)
    p = SMTPProvider("localhost", 25, None, None, False, "from@example.org")
    # a From name that survives cleaning but a Subject/header that the policy refuses is surfaced as ProviderError
    monkeypatch.setattr(providers.email.utils, "formataddr", lambda pair: (_ for _ in ()).throw(ValueError("bad")))
    with pytest.raises(ProviderError) as exc:
        p.send(OutboundMessage("a@b.example", "s", "b", "Sara", None))
    assert exc.value.retryable is False


def test_header_text_cleaning_strips_control_characters() -> None:
    assert providers.clean_header_text("Sara\r\nBcc: x") == "Sara  Bcc: x"


def test_stored_error_text_has_no_addresses_and_is_bounded() -> None:
    out = scrub_error("550 5.1.1 <jane.doe@customer.example>: user unknown " + "x" * 600)
    assert "@" not in out and "[EMAIL]" in out and len(out) <= 400
