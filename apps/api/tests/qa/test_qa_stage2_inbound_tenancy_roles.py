"""QA (independent) — Stage 2 inbound linking (S2-F08), tenant isolation (S2-A04), role gates, and log
redaction (S2-F11 / S2-A08). Derived from docs/PRD.md, docs/USER_JOURNEYS.md role matrix and the master prompt
("never falsely claim to have sent an email or received a reply", "no cross-tenant contact access",
"logs redact sensitive content"). Integration evidence against PostgreSQL with the RLS role.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.logging import JsonFormatter
from app.db.base import utcnow
from app.db.session import get_sessionmaker, set_tenant
from app.services.comms.providers import InboundMessage, ProviderError, reset_simulators, simulator_for
from app.services.followup import service as fu
from app.services.jobs import queue
from tests.conftest import AuthedClient, Tenant, login_as
from tests.integration.test_stage2_followup import add_contact, approved_quote, mark_sent

pytestmark = [pytest.mark.integration, pytest.mark.security]


@pytest.fixture(autouse=True)
def _reset_sim() -> None:
    reset_simulators()


def inbound(admin: AuthedClient, from_email: str, subject: str, snippet: str, in_reply_to: str | None = None):  # type: ignore[no-untyped-def]
    return admin.post(
        "/integrations/simulator/inbound",
        json={"from_email": from_email, "subject": subject, "snippet": snippet, "in_reply_to": in_reply_to},
    )


def status_of(ac: AuthedClient, quote_id: str) -> str:
    return ac.get(f"/quotes/{quote_id}/lifecycle").json()["status"]


# ---------------------------------------------------------------- S2-F08 inbound linking
def test_qa_s2_f08_reply_links_by_thread_header_when_no_quote_number(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    c = add_contact(sales, str(tenant_a.customer_id))
    q = mark_sent(sales, q)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    sent = sales.post(f"/drafts/{d['id']}/send").json()
    assert status_of(sales, q["id"]) == "sent"
    r = inbound(
        admin, c["email"], "Re: your offer", "Looks good, checking internally.", in_reply_to=sent["provider_message_id"]
    )
    assert r.status_code == 201 and r.json()["kind"] == "reply_received" and r.json()["quote_id"] == q["id"]
    assert status_of(sales, q["id"]) == "acknowledged"
    ev = sales.get(f"/quotes/{q['id']}/lifecycle").json()["events"][-1]
    assert ev["source"] == "inbound" and ev["actor_user_id"] is None and "reply received" in ev["evidence"]
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        linked_by = db.execute(
            text("SELECT payload->>'linked_by' FROM communication_events WHERE direction = 'in'")
        ).scalar()
    assert linked_by == "thread"
    # a second reply does not create a second acknowledgement event (facts, not noise)
    inbound(admin, c["email"], "Re: your offer", "one more question", in_reply_to=sent["provider_message_id"])
    events = sales.get(f"/quotes/{q['id']}/lifecycle").json()["events"]
    assert [e["to_status"] for e in events] == ["sent", "acknowledged"]


def test_qa_s2_f08_unmatched_inbound_is_recorded_unlinked_and_changes_no_quote(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    add_contact(sales, str(tenant_a.customer_id))
    q = mark_sent(sales, q)
    r = inbound(admin, "stranger@elsewhere.example", "Hello", "Is this the sales department?")
    assert r.status_code == 201 and r.json()["quote_id"] is None and r.json()["kind"] == "reply_received"
    assert status_of(sales, q["id"]) == "sent", "an unlinked message must never acknowledge a quote"
    assert sales.get(f"/communication-events?quote_id={q['id']}").json() == []
    unlinked = [e for e in admin.get("/communication-events").json() if e["quote_id"] is None]
    assert len(unlinked) == 1 and unlinked[0]["direction"] == "in"
    # a message mentioning a number that does not exist in this tenant stays unlinked
    r = inbound(admin, "stranger@elsewhere.example", "Re: Quotation Q-2026-99999", "ok")
    assert r.json()["quote_id"] is None
    # an inbound message never fabricates an outbound "sent" fact
    assert all(e["direction"] == "in" for e in admin.get("/communication-events").json())


def test_qa_s2_f08_quote_numbers_do_not_link_across_tenants(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    a_sales, _, qa1 = approved_quote(app, tenant_a)
    _, _, qa2 = approved_quote(app, tenant_a)  # A has two quotes; B only one
    b_sales, _, qb1 = approved_quote(app, tenant_b)
    a_admin = login_as(app, tenant_a.users["admin"])
    b_admin = login_as(app, tenant_b.users["admin"])
    add_contact(a_sales, str(tenant_a.customer_id), email="buyer@customer.example")
    add_contact(b_sales, str(tenant_b.customer_id), email="buyer@customer.example")  # same address, other tenant
    qa1, qa2, qb1 = mark_sent(a_sales, qa1), mark_sent(a_sales, qa2), mark_sent(b_sales, qb1)
    assert qa2["number"] != qb1["number"]
    r = inbound(b_admin, "buyer@customer.example", f"Re: {qa2['number']}", "we accept")
    assert r.status_code == 201 and r.json()["quote_id"] is None
    assert status_of(a_sales, qa2["id"]) == "sent" and status_of(b_sales, qb1["id"]) == "sent"
    assert b_sales.get("/communication-events").json()[0]["quote_id"] is None
    assert a_sales.get("/communication-events").json() == []
    # B's own number links B's own quote only
    r = inbound(b_admin, "buyer@customer.example", f"Re: {qb1['number']}", "we accept")
    assert r.json()["quote_id"] == qb1["id"]
    assert status_of(b_sales, qb1["id"]) == "acknowledged" and status_of(a_sales, qa1["id"]) == "sent"
    # an unsubscribe in B opts out B's contact only
    inbound(b_admin, "buyer@customer.example", "unsubscribe", "please unsubscribe")
    assert a_sales.get(f"/customers/{tenant_a.customer_id}/contacts").json()[0]["opted_out_at"] is None
    assert b_sales.get(f"/customers/{tenant_b.customer_id}/contacts").json()[0]["opted_out_at"] is not None
    assert a_admin.get("/communication-events").json() == []


def test_qa_s2_f08_unsubscribe_processing_is_idempotent_per_provider_message(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """Re-delivery of the same provider message (cursor reset, provider replay) must not crash the sync."""
    sales, _, q = approved_quote(app, tenant_a)
    c = add_contact(sales, str(tenant_a.customer_id))
    q = mark_sent(sales, q)
    msg = InboundMessage("simulator", "replayed-1", c["email"], f"Re: {q['number']}", "Please unsubscribe me", utcnow())
    from app.models.org import Organization

    for _ in range(2):
        with get_sessionmaker()() as db:
            set_tenant(db, tenant_a.org_id, None)
            org = db.get(Organization, tenant_a.org_id)
            try:
                ev = fu.record_inbound(db, org=org, msg=msg)
                db.commit()
            except IntegrityError as exc:  # pragma: no cover - the defect path
                db.rollback()
                pytest.fail(f"replayed unsubscribe message crashed the inbound sync: {type(exc).__name__}")
            assert ev is not None and ev.kind == "opt_out"
    events = sales.get("/communication-events").json()
    assert len([e for e in events if e["direction"] == "in"]) == 1
    assert sales.get(f"/customers/{tenant_a.customer_id}/contacts").json()[0]["opt_out_source"] == "reply"


def test_qa_s2_f08_unsubscribe_reply_opts_out_every_contact_row_with_that_address(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    add_contact(sales, str(tenant_a.customer_id), email="buyer@customer.example")
    add_contact(sales, str(tenant_a.customer_id), email="BUYER@customer.example", primary=False)
    q = mark_sent(sales, q)
    r = inbound(admin, "buyer@customer.example", f"Re: {q['number']}", "STOP - remove me from these mails")
    assert r.status_code == 201 and r.json()["kind"] == "opt_out"
    contacts = sales.get(f"/customers/{tenant_a.customer_id}/contacts").json()
    still_subscribed = [c["email"] for c in contacts if c["opted_out_at"] is None]
    assert still_subscribed == [], f"address unsubscribed but these contact rows remain subscribed: {still_subscribed}"
    assert sales.get("/follow-up/schedules").json() == []


def test_qa_s2_f08_ordinary_reply_containing_the_word_stop_is_a_reply_not_an_opt_out(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    c = add_contact(sales, str(tenant_a.customer_id))
    q = mark_sent(sales, q)
    r = inbound(
        admin,
        c["email"],
        f"Re: Quotation {q['number']}",
        "Thanks! Could your driver stop by our Jebel Ali gate on Monday to discuss delivery?",
    )
    assert r.status_code == 201
    contact = sales.get(f"/customers/{tenant_a.customer_id}/contacts").json()[0]
    assert contact["opted_out_at"] is None, "a genuine reply silently opted the customer out of all follow-ups"
    assert r.json()["kind"] == "reply_received" and status_of(sales, q["id"]) == "acknowledged"


def test_qa_s2_f08_inbound_endpoints_are_admin_only(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    for role in ("sales", "approver", "readonly"):
        ac = login_as(app, tenant_a.users[role])
        assert ac.post("/integrations/sync-inbound").status_code == 403, role
        assert inbound(ac, "x@y.example", "s", "unsubscribe").status_code == 403, role
        assert ac.post("/integrations", json={"provider": "simulator"}).status_code == 403, role
        assert ac.get("/integrations").status_code == 403, role
    owner = login_as(app, tenant_a.users["owner"])
    assert owner.post("/integrations/sync-inbound").json() == {"processed": 0}
    # observation: the dev-only simulator endpoint does not validate from_email (records an unlinked event)
    assert inbound(owner, "bad-address", "s", "x").status_code in (201, 422)


# ---------------------------------------------------------------- S2-A04 tenant isolation for every Stage 2 resource
def test_qa_s2_a04_every_stage2_resource_is_404_for_another_tenant(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    c = add_contact(sales, str(tenant_a.customer_id))
    q = mark_sent(sales, q)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    s = sales.get(f"/follow-up/schedules?quote_id={q['id']}").json()[0]
    t = sales.get("/follow-up/templates").json()[0]
    conn = admin.post("/integrations", json={"provider": "simulator"}).json()
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        job = queue.enqueue(
            db, org_id=tenant_a.org_id, kind="send_draft", idempotency_key="qa:iso", payload={"draft_id": d["id"]}
        )
        job.status = "dead"
        db.commit()
        job_id = str(job.id)
    b_owner = login_as(app, tenant_b.users["owner"])
    future = (utcnow() + timedelta(days=3)).isoformat()
    attempts = [
        ("GET", f"/customers/{tenant_a.customer_id}/contacts", None),
        ("POST", f"/customers/{tenant_a.customer_id}/contacts", {"full_name": "x"}),
        ("PATCH", f"/contacts/{c['id']}", {"full_name": "pwn"}),
        ("POST", f"/contacts/{c['id']}/opt-out", {"opted_out": False}),
        ("GET", f"/quotes/{q['id']}/lifecycle", None),
        (
            "POST",
            f"/quotes/{q['id']}/transition",
            {"expected_lock_version": q["lock_version"], "to_status": "won", "evidence": "x"},
        ),
        ("POST", f"/quotes/{q['id']}/drafts", {"contact_id": c["id"]}),
        ("POST", f"/quotes/{q['id']}/follow-up/plan", None),
        ("GET", f"/drafts/{d['id']}", None),
        ("PATCH", f"/drafts/{d['id']}", {"subject": "pwn"}),
        ("POST", f"/drafts/{d['id']}/send", None),
        ("POST", f"/drafts/{d['id']}/cancel", None),
        ("POST", f"/follow-up/schedules/{s['id']}/cancel", {}),
        ("POST", f"/follow-up/schedules/{s['id']}/reschedule", {"due_at": future}),
        ("PATCH", f"/follow-up/templates/{t['id']}", {"subject": "pwn {quote_number}"}),
        ("POST", f"/jobs/{job_id}/retry", None),
        ("POST", f"/integrations/{conn['id']}/revoke", None),
    ]
    for method, path, body in attempts:
        r = b_owner.client.request(method, f"/api/v1{path}", json=body, headers={"X-CSRF-Token": b_owner.csrf})
        assert r.status_code == 404, (method, path, r.status_code, r.text)
    # list/filter endpoints never leak the other tenant's rows
    assert b_owner.get(f"/communication-events?quote_id={q['id']}").json() == []
    assert b_owner.get(f"/follow-up/schedules?status=all&quote_id={q['id']}").json() == []
    assert (
        b_owner.get("/drafts").json() == []
        and b_owner.get("/jobs").json() == []
        and b_owner.get("/integrations").json() == []
    )
    assert all(x["id"] != t["id"] for x in b_owner.get("/follow-up/templates").json())
    # and nothing in A changed
    assert sales.get(f"/drafts/{d['id']}").json()["status"] == "draft"
    assert sales.get(f"/follow-up/schedules?quote_id={q['id']}").json()[0]["status"] == "pending"
    assert simulator_for(tenant_a.org_id).outbox == [] and simulator_for(tenant_b.org_id).outbox == []
    assert sales.get(f"/customers/{tenant_a.customer_id}/contacts").json()[0]["full_name"] == "Buyer Person"


def test_qa_s2_a04_rls_blocks_stage2_tables_without_the_service_filter(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    c = add_contact(sales, str(tenant_a.customer_id))
    q = mark_sent(sales, q)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    sales.post(f"/drafts/{d['id']}/send")
    admin.post("/integrations", json={"provider": "simulator"})
    admin.post("/integrations/sync-inbound")
    tables = [  # fixed identifiers (test-owned list) interpolated into the count queries below
        "contacts",
        "follow_up_policies",
        "message_templates",
        "quote_status_events",
        "follow_up_schedules",
        "communication_drafts",
        "communication_events",
        "integration_connections",
        "job_runs",
    ]
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        own = {t: db.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in tables}  # noqa: S608
    assert all(own[t] >= 1 for t in tables if t != "job_runs"), own
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_b.org_id, None)
        other = {t: db.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in tables}  # noqa: S608
        assert all(v == 0 for v in other.values()), other
        # writes against the other tenant's rows are invisible no-ops, even by primary key
        upd = db.execute(
            text("UPDATE contacts SET opted_out_at = NULL, email = 'pwn@evil.example' WHERE id = :i"), {"i": c["id"]}
        )
        assert upd.rowcount == 0
        upd = db.execute(
            text("UPDATE communication_drafts SET to_email = 'pwn@evil.example' WHERE id = :i"), {"i": d["id"]}
        )
        assert upd.rowcount == 0
        # inserting a row that claims the other tenant is refused by WITH CHECK
        with pytest.raises(Exception):  # noqa: B017 - any RLS violation
            db.execute(
                text(
                    "INSERT INTO contacts (id, organization_id, customer_id, full_name, preferred_channel, language, timezone, is_primary, created_at, updated_at) VALUES (:i, :o, :c, 'x', 'email', 'en', 'UTC', false, now(), now())"
                ),
                {"i": str(uuid.uuid4()), "o": str(tenant_a.org_id), "c": str(tenant_a.customer_id)},
            )
        db.rollback()
    with get_sessionmaker()() as db:  # no tenant context at all → nothing visible
        none = {t: db.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in tables}  # noqa: S608
        assert all(v == 0 for v in none.values()), none
    assert sales.get(f"/customers/{tenant_a.customer_id}/contacts").json()[0]["email"] == c["email"]


# ---------------------------------------------------------------- role matrix
def test_qa_s2_role_matrix_for_stage2_endpoints(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, approver, q = approved_quote(app, tenant_a)
    owner = login_as(app, tenant_a.users["owner"])
    admin = login_as(app, tenant_a.users["admin"])
    readonly = login_as(app, tenant_a.users["readonly"])
    c = add_contact(sales, str(tenant_a.customer_id))
    q = mark_sent(sales, q)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    t = sales.get("/follow-up/templates").json()[0]
    s = sales.get(f"/follow-up/schedules?quote_id={q['id']}").json()
    reads = [
        "/follow-up/policy",
        "/follow-up/templates",
        "/drafts",
        f"/drafts/{d['id']}",
        "/follow-up/schedules",
        "/communication-events",
        f"/quotes/{q['id']}/lifecycle",
        f"/customers/{tenant_a.customer_id}/contacts",
    ]
    for ac in (readonly, sales, approver, admin, owner):
        for path in reads:
            assert ac.get(path).status_code == 200, (ac.role, path)
    manage_only = [
        ("GET", "/jobs", None),
        ("GET", "/integrations", None),
        ("PATCH", "/follow-up/policy", {"sender_name": "x"}),
        ("PATCH", f"/follow-up/templates/{t['id']}", {"name": "x"}),
        ("POST", "/jobs/run-once", None),
        ("POST", "/integrations", {"provider": "simulator"}),
        ("POST", "/integrations/sync-inbound", None),
        ("POST", "/integrations/simulator/inbound", {"from_email": "a@b.example", "subject": "x", "snippet": "y"}),
    ]
    for ac in (readonly, sales, approver):
        for method, path, body in manage_only:
            r = ac.client.request(method, f"/api/v1{path}", json=body, headers={"X-CSRF-Token": ac.csrf})
            assert r.status_code == 403, (ac.role, method, path, r.status_code)
    edit_only = [
        ("POST", f"/quotes/{q['id']}/drafts", {"contact_id": c["id"]}),
        ("PATCH", f"/drafts/{d['id']}", {"subject": "x"}),
        ("POST", f"/drafts/{d['id']}/send", None),
        ("POST", f"/drafts/{d['id']}/cancel", None),
        ("POST", f"/follow-up/schedules/{s[0]['id']}/cancel", {}),
        ("POST", f"/contacts/{c['id']}/opt-out", {"opted_out": True}),
        (
            "POST",
            f"/quotes/{q['id']}/transition",
            {"expected_lock_version": q["lock_version"], "to_status": "won", "evidence": "x"},
        ),
        ("POST", f"/quotes/{q['id']}/follow-up/plan", None),
    ]
    for method, path, body in edit_only:
        r = readonly.client.request(method, f"/api/v1{path}", json=body, headers={"X-CSRF-Token": readonly.csrf})
        assert r.status_code == 403, ("readonly", method, path, r.status_code)
    assert simulator_for(tenant_a.org_id).outbox == []
    # approver and sales may act on follow-ups; admin and owner may manage
    assert approver.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).status_code == 201
    assert approver.post(f"/follow-up/schedules/{s[0]['id']}/cancel", json={}).status_code == 200
    assert sales.post(f"/follow-up/schedules/{s[1]['id']}/cancel", json={}).status_code == 200
    for ac in (admin, owner):
        assert ac.get("/jobs").status_code == 200 and ac.get("/integrations").status_code == 200
        assert ac.patch("/follow-up/policy", json={"sender_name": ac.role}).status_code == 200
        assert ac.patch(f"/follow-up/templates/{t['id']}", json={"name": ac.role}).status_code == 200
        assert ac.post("/jobs/run-once").status_code == 200


# ---------------------------------------------------------------- S2-F11 / S2-A08 logs
def test_qa_s2_a08_logs_on_success_failure_worker_and_inbound_paths_contain_no_addresses_or_bodies(
    app, tenant_a: Tenant, caplog: pytest.LogCaptureFixture
) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    c = add_contact(sales, str(tenant_a.customer_id), email="secret.person@customer.example")
    admin.patch("/follow-up/policy", json={"automation_enabled": True, "offsets_days": [1]})
    q = mark_sent(sales, q)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    sim = simulator_for(tenant_a.org_id)
    with caplog.at_level(logging.DEBUG):
        sim.fail_next = [
            ProviderError("mailbox secret.person@customer.example unavailable", retryable=True, status=451)
        ]
        assert sales.post(f"/drafts/{d['id']}/send").status_code == 503
        assert sales.post(f"/drafts/{d['id']}/send").status_code == 200
        sim.fail_next = [ProviderError("550 rejected secret.person@customer.example", retryable=False, status=550)]
        with get_sessionmaker()() as db:
            set_tenant(db, tenant_a.org_id, None)
            db.execute(text("UPDATE follow_up_schedules SET due_at = now() - interval '1 minute'"))
            db.commit()
        with get_sessionmaker()() as db:
            queue.enqueue_due_schedules(db)
            queue.run_once(db)
        admin.post(
            "/integrations/simulator/inbound",
            json={
                "from_email": c["email"],
                "subject": f"Re: {q['number']}",
                "snippet": "Confidential: our budget is 1.2M and please unsubscribe",
            },
        )
        assert sales.get(f"/quotes/{q['id']}/lifecycle").status_code == 200
    fmt = JsonFormatter()
    rendered = [fmt.format(r) for r in caplog.records]
    assert rendered, "expected application log records during send/worker/inbound processing"
    joined = "\n".join(rendered)
    assert "secret.person@customer.example" not in joined
    assert d["body"][:60] not in joined and "Dear Buyer Person" not in joined
    assert "our budget is 1.2M" not in joined
    for line in rendered:
        payload = json.loads(line)
        assert "@" not in json.dumps(payload), line
    # raw (pre-formatter) records never carry message bodies either: redaction is not the only line of defence
    raw = "\n".join(r.getMessage() for r in caplog.records)
    assert "Dear Buyer Person" not in raw and "our budget is 1.2M" not in raw
    # the API never returns full bodies in event feeds: snippets are bounded
    for e in admin.get("/communication-events").json():
        assert e["snippet"] is None or len(e["snippet"]) <= 500
