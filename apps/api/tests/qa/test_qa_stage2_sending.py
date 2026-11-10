"""QA (independent) — Stage 2 drafts, explicit sending, consent, opt-out, templates, policy (S2-F02/F04/F05,
S2-A01/A02/A03). Derived from docs/PRD.md and the master prompt: "Outbound sending requires an explicit authorized
user action or separately enabled, consented workflow approved by an organization admin, with rate limits,
recipient validation and tenant-scoped audit logs. Default to draft-only." and "unsubscribed/opted-out contact never
receives an automated message". Integration evidence against PostgreSQL with the RLS role and the simulator provider.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from app.db.base import utcnow
from app.db.session import get_sessionmaker, set_tenant
from app.services.comms.providers import reset_simulators, simulator_for
from app.services.jobs import queue
from tests.conftest import API, Tenant, login_as
from tests.integration.test_stage2_followup import add_contact, approved_quote, mark_sent

pytestmark = [pytest.mark.integration]
OPT_OUT_MARKER = "UNSUBSCRIBE"


@pytest.fixture(autouse=True)
def _reset_sim() -> None:
    reset_simulators()


def force_due(org_id) -> None:  # type: ignore[no-untyped-def]
    with get_sessionmaker()() as db:
        set_tenant(db, org_id, None)
        db.execute(text("UPDATE follow_up_schedules SET due_at = now() - interval '1 minute' WHERE status = 'pending'"))
        db.commit()


def worker_tick() -> int:
    with get_sessionmaker()() as db:
        queue.enqueue_due_schedules(db)
        return queue.run_once(db)


def audit(org_id, action: str) -> list[dict]:  # type: ignore[no-untyped-def,type-arg]
    with get_sessionmaker()() as db:
        set_tenant(db, org_id, None)
        return [
            {"actor": r[0], "summary": r[1]}
            for r in db.execute(
                text("SELECT actor_user_id, summary FROM audit_events WHERE action = :a ORDER BY created_at"),
                {"a": action},
            ).all()
        ]


# ---------------------------------------------------------------- S2-A01 nothing leaves without the click
def test_qa_s2_a01_nothing_is_sent_before_the_explicit_authorized_click(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    c = add_contact(sales, str(tenant_a.customer_id))
    q = mark_sent(sales, q)
    sim = simulator_for(tenant_a.org_id)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    # reading, re-reading, planning and running the worker (automation off) must not send anything
    for _ in range(3):
        assert sales.get(f"/quotes/{q['id']}/lifecycle").status_code == 200
    assert sales.post(f"/quotes/{q['id']}/follow-up/plan").status_code == 201
    force_due(tenant_a.org_id)
    worker_tick()
    assert sim.outbox == [] and sales.get(f"/communication-events?quote_id={q['id']}").json() == []
    assert sales.get(f"/drafts/{d['id']}").json()["status"] == "draft"
    # unauthorized roles and CSRF-less requests cannot trigger the send
    readonly = login_as(app, tenant_a.users["readonly"])
    assert readonly.post(f"/drafts/{d['id']}/send").status_code == 403
    raw = sales.client.post(f"{API}/drafts/{d['id']}/send")  # authenticated cookie, no CSRF header
    assert raw.status_code == 403 and raw.json()["action"] == "csrf"
    assert sim.outbox == []
    # the explicit click: exactly one message, recorded as a manual send by that user
    r = sales.post(f"/drafts/{d['id']}/send")
    assert r.status_code == 200 and r.json()["status"] == "sent" and r.json()["sent_by"] == sales.user_id
    assert len(sim.outbox) == 1 and sim.outbox[0][0].to_email == c["email"]
    events = sales.get(f"/communication-events?quote_id={q['id']}").json()
    assert [e["kind"] for e in events] == ["sent"] and events[0]["direction"] == "out"
    rows = audit(tenant_a.org_id, "draft.sent")
    assert len(rows) == 1 and str(rows[0]["actor"]) == sales.user_id and rows[0]["summary"]["mode"] == "manual"
    # a sent draft is immutable and cannot be "discarded"
    assert sales.post(f"/drafts/{d['id']}/cancel").status_code == 409
    assert sales.patch(f"/drafts/{d['id']}", json={"body": "edited after the fact"}).status_code == 409


def test_qa_s2_f05_every_stage2_mutation_requires_csrf(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    c = add_contact(sales, str(tenant_a.customer_id))
    q = mark_sent(sales, q)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    s = sales.get(f"/follow-up/schedules?quote_id={q['id']}").json()[0]
    t = sales.get("/follow-up/templates").json()[0]
    attempts = [
        (
            sales,
            "POST",
            f"/quotes/{q['id']}/transition",
            {"expected_lock_version": q["lock_version"], "to_status": "won", "evidence": "x"},
        ),
        (sales, "POST", f"/quotes/{q['id']}/drafts", {}),
        (sales, "PATCH", f"/drafts/{d['id']}", {"subject": "pwn"}),
        (sales, "POST", f"/drafts/{d['id']}/send", None),
        (sales, "POST", f"/drafts/{d['id']}/cancel", None),
        (sales, "POST", f"/follow-up/schedules/{s['id']}/cancel", {}),
        (
            sales,
            "POST",
            f"/follow-up/schedules/{s['id']}/reschedule",
            {"due_at": (utcnow() + timedelta(days=3)).isoformat()},
        ),
        (sales, "POST", f"/contacts/{c['id']}/opt-out", {"opted_out": True}),
        (sales, "PATCH", f"/contacts/{c['id']}", {"full_name": "pwn"}),
        (sales, "POST", f"/customers/{tenant_a.customer_id}/contacts", {"full_name": "pwn"}),
        (admin, "PATCH", "/follow-up/policy", {"automation_enabled": True}),
        (admin, "PATCH", f"/follow-up/templates/{t['id']}", {"subject": "pwn"}),
        (admin, "POST", "/jobs/run-once", None),
        (admin, "POST", "/integrations", {"provider": "simulator"}),
        (admin, "POST", "/integrations/sync-inbound", None),
        (admin, "POST", "/integrations/simulator/inbound", {"from_email": c["email"], "subject": "x", "snippet": "y"}),
    ]
    for ac, method, path, body in attempts:
        r = ac.client.request(method, f"{API}{path}", json=body)  # cookies yes, X-CSRF-Token no
        assert r.status_code == 403 and r.json().get("action") == "csrf", (method, path, r.status_code, r.text)
        bad = ac.client.request(method, f"{API}{path}", json=body, headers={"X-CSRF-Token": "f" * 64})
        assert bad.status_code == 403, (method, path, bad.status_code)
    assert simulator_for(tenant_a.org_id).outbox == []
    assert sales.get(f"/drafts/{d['id']}").json()["status"] == "draft"
    assert admin.get("/follow-up/policy").json()["automation_enabled"] is False
    assert sales.get(f"/customers/{tenant_a.customer_id}/contacts").json()[0]["opted_out_at"] is None


# ---------------------------------------------------------------- S2-F04 draft accuracy and templates
def test_qa_s2_f04_draft_carries_accurate_quote_facts_and_opt_out_text(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    c = add_contact(sales, str(tenant_a.customer_id))
    q = mark_sent(sales, q)
    total = Decimal(q["current_version"]["grand_total"])
    for key, subject_hint in (
        ("follow_up_1", "any questions"),
        ("follow_up_2", "Following up"),
        ("expiry_reminder", "expires on"),
    ):
        d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"], "template_key": key}).json()
        assert d["status"] == "draft" and d["template_key"] == key and subject_hint in d["subject"], d
        assert q["number"] in d["subject"] and q["number"] in d["body"]
        assert "Buyer Person" in d["body"] and tenant_a.name in d["body"]
        assert q["valid_until"] in d["body"] or key == "follow_up_2" and q["valid_until"] in d["body"]
        if key != "follow_up_2":
            assert f"{total:,.2f}" in d["body"] and q["currency"] in d["body"]
        assert OPT_OUT_MARKER in d["body"] and d["to_email"] == c["email"]
        assert "{" not in d["body"] and "{" not in d["subject"], "unrendered placeholders leaked into the draft"
        assert d["generated_reasons"], "every generated draft explains why it was proposed (S2-F03)"
    assert sales.post(f"/quotes/{q['id']}/drafts", json={"template_key": "does_not_exist"}).status_code == 404
    # contact of another customer cannot be the recipient
    other = sales.post("/customers", json={"name": "Other Co"}).json()
    oc = add_contact(sales, other["id"], email="other@elsewhere.example")
    assert sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": oc["id"]}).status_code == 404


def test_qa_s2_f04_placeholders_in_an_edited_draft_are_rendered_before_delivery(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """The draft editor tells the user "placeholders in braces are allowed" and validates them against the
    whitelist; S2-F04 requires accurate quote references in what the customer receives."""
    sales, _, q = approved_quote(app, tenant_a)
    c = add_contact(sales, str(tenant_a.customer_id))
    q = mark_sent(sales, q)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    r = sales.patch(
        f"/drafts/{d['id']}", json={"body": "Hi {contact_name}, quick reminder about {quote_number}. {opt_out_text}"}
    )
    assert r.status_code == 200, r.text
    assert sales.post(f"/drafts/{d['id']}/send").status_code == 200
    ((message, _result),) = simulator_for(tenant_a.org_id).outbox
    assert "{" not in message.body_text, "customer received unrendered placeholders: " + message.body_text
    assert q["number"] in message.body_text and "Buyer Person" in message.body_text


def test_qa_s2_f04_edited_draft_is_sent_with_opt_out_text(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """PRD S2-F04: drafts are editable but carry opt-out text; the implementer's report claims it is "guaranteed"."""
    sales, _, q = approved_quote(app, tenant_a)
    c = add_contact(sales, str(tenant_a.customer_id))
    q = mark_sent(sales, q)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    r = sales.patch(f"/drafts/{d['id']}", json={"body": f"Hi there, quick reminder about quotation {q['number']}."})
    assert r.status_code == 200, r.text
    assert sales.post(f"/drafts/{d['id']}/send").status_code == 200
    ((message, _result),) = simulator_for(tenant_a.org_id).outbox
    assert q["number"] in message.body_text
    assert OPT_OUT_MARKER in message.body_text, (
        "an edited draft was delivered without any opt-out instruction: " + message.body_text
    )


def test_qa_s2_f04_template_placeholder_whitelist_plain_text_and_role_gate(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    readonly = login_as(app, tenant_a.users["readonly"])
    c = add_contact(sales, str(tenant_a.customer_id))
    q = mark_sent(sales, q)
    tpls = readonly.get("/follow-up/templates").json()
    assert {t["key"] for t in tpls} == {"follow_up_1", "follow_up_2", "expiry_reminder"}
    t1 = next(t for t in tpls if t["key"] == "follow_up_1")
    assert sales.patch(f"/follow-up/templates/{t1['id']}", json={"subject": "x"}).status_code == 403
    assert readonly.patch(f"/follow-up/templates/{t1['id']}", json={"subject": "x"}).status_code == 403
    for bad in (
        {"subject": "Hello {evil}"},
        {"body": "Dear {contact_name} {__class__}"},
        {"body": "Dear {contact_name} {customer_email}"},
        {"body": "<script>alert(1)</script> {quote_number}"},
        {"body": "click javascript:alert(1) {quote_number}"},
        {"body": ""},
    ):
        r = admin.patch(f"/follow-up/templates/{t1['id']}", json=bad)
        assert r.status_code == 422, (bad, r.status_code, r.text)
    assert admin.patch(f"/follow-up/templates/{uuid.uuid4()}", json={"subject": "x"}).status_code == 404
    # a valid edit (dropping the opt-out placeholder on purpose) is persisted, audited and used for new drafts
    r = admin.patch(
        f"/follow-up/templates/{t1['id']}",
        json={
            "subject": "QA {quote_number} v{quote_version}",
            "body": "Hello {contact_name}, total {grand_total} {currency}.",
        },
    )
    assert r.status_code == 200 and r.json()["subject"].startswith("QA ")
    assert audit(tenant_a.org_id, "followup.template_update")
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"], "template_key": "follow_up_1"}).json()
    assert d["subject"] == f"QA {q['number']} v1"
    assert d["body"].startswith("Hello Buyer Person, total ")
    assert OPT_OUT_MARKER in d["body"], "opt-out text must be appended even when the template omitted it"
    # the draft editor applies the same whitelist and never accepts scripts
    assert sales.patch(f"/drafts/{d['id']}", json={"body": "{quote_number} {evil}"}).status_code == 422
    assert sales.patch(f"/drafts/{d['id']}", json={"body": "<script>x</script>"}).status_code == 422
    assert (
        sales.patch(f"/drafts/{d['id']}", json={"subject": "line1\r\nBcc: x@y.example"}).json()["subject"]
        == "line1  Bcc: x@y.example"
    )


def test_qa_s2_f05_recipient_validation_arbitrary_and_cross_contact(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    a = add_contact(sales, str(tenant_a.customer_id), email="a.buyer@customer.example")
    b = add_contact(sales, str(tenant_a.customer_id), email="b.buyer@customer.example", primary=False)
    opted = add_contact(sales, str(tenant_a.customer_id), email="gone@customer.example", primary=False)
    assert sales.post(f"/contacts/{opted['id']}/opt-out", json={"opted_out": True}).status_code == 200
    q = mark_sent(sales, q)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": a["id"]}).json()
    sim = simulator_for(tenant_a.org_id)
    # arbitrary / malformed recipients are refused
    for bad in ("attacker@evil.example", "not-an-email", "a.buyer@customer.example\r\nBcc: x@evil.example", ""):
        r = sales.patch(f"/drafts/{d['id']}", json={"to_email": bad})
        assert r.status_code == 422, (bad, r.status_code, r.text)
    # redirecting the draft to an opted-out contact of the same customer must never deliver
    r = sales.patch(f"/drafts/{d['id']}", json={"to_email": opted["email"]})
    if r.status_code == 200:
        assert sales.post(f"/drafts/{d['id']}/send").status_code != 200
    assert sim.outbox == [], "message delivered to an opted-out address via to_email redirect"
    # redirecting to another eligible contact of the same customer must be either refused or honoured, not both
    r = sales.patch(f"/drafts/{d['id']}", json={"to_email": b["email"]})
    if r.status_code == 200:
        s = sales.post(f"/drafts/{d['id']}/send")
        assert s.status_code == 200, (
            "PATCH accepted a recipient the send gate then rejects: the user cannot send the edited draft "
            f"({s.status_code} {s.text})"
        )
        assert sim.outbox[-1][0].to_email == b["email"]
    else:
        assert r.status_code == 422


# ---------------------------------------------------------------- S2-A02 opt-out and preferences
def test_qa_s2_a02_opt_out_is_enforced_per_address_not_per_contact_row(app, tenant_a: Tenant) -> None:  # type: ignore[no-undef,no-untyped-def]
    """A sales user must not be able to bypass an opt-out by creating a second contact with the same e-mail."""
    sales, _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    a = add_contact(sales, str(tenant_a.customer_id), email="buyer@customer.example")
    assert sales.post(f"/contacts/{a['id']}/opt-out", json={"opted_out": True, "source": "manual"}).status_code == 200
    dup = add_contact(sales, str(tenant_a.customer_id), email="Buyer@Customer.example", primary=False)
    sim = simulator_for(tenant_a.org_id)
    admin.patch("/follow-up/policy", json={"automation_enabled": True})
    q = mark_sent(sales, q)  # plans schedules; the only eligible-looking contact is the duplicate
    created = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": dup["id"]})
    if created.status_code == 201:
        sales.post(f"/drafts/{created.json()['id']}/send")
    force_due(tenant_a.org_id)
    worker_tick()
    delivered = [m.to_email for m, _ in sim.outbox]
    assert delivered == [], f"opted-out address received mail via a duplicate contact row: {delivered}"
    assert created.status_code == 403, created.text
    sched = sales.get(f"/follow-up/schedules?status=all&quote_id={q['id']}").json()
    assert all(s["status"] in ("suppressed", "cancelled") for s in sched), sched


def test_qa_s2_a02_contact_preferring_no_follow_ups_is_never_auto_messaged(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """S2-F02 per-contact preferences: the UI offers "no follow-ups" (preferred_channel=none)."""
    sales, _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    r = sales.post(
        f"/customers/{tenant_a.customer_id}/contacts",
        json={
            "full_name": "Quiet Buyer",
            "email": "quiet@customer.example",
            "preferred_channel": "none",
            "is_primary": True,
        },
    )
    assert r.status_code == 201
    admin.patch("/follow-up/policy", json={"automation_enabled": True})
    q = mark_sent(sales, q)
    lc = sales.get(f"/quotes/{q['id']}/lifecycle").json()
    assert lc["recommendation"]["suggested_contact_id"] is None, "the recommendation must not propose this contact"
    force_due(tenant_a.org_id)
    worker_tick()
    sim = simulator_for(tenant_a.org_id)
    assert sim.outbox == [], (
        f"automated message delivered to a contact who prefers no follow-ups: {sim.outbox[0][0].to_email}"
    )
    sched = sales.get(f"/follow-up/schedules?status=all&quote_id={q['id']}").json()
    assert all(s["status"] != "sent" for s in sched)


def test_qa_s2_a02_manual_opt_out_cancels_pending_and_only_admin_can_opt_in(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, approver, q = approved_quote(app, tenant_a)
    owner = login_as(app, tenant_a.users["owner"])
    readonly = login_as(app, tenant_a.users["readonly"])
    c = add_contact(sales, str(tenant_a.customer_id))
    q = mark_sent(sales, q)
    assert readonly.post(f"/contacts/{c['id']}/opt-out", json={"opted_out": True}).status_code == 403
    r = approver.post(f"/contacts/{c['id']}/opt-out", json={"opted_out": True, "source": "manual"})
    assert r.status_code == 200 and r.json()["opted_out_at"] and r.json()["opt_out_source"] == "manual"
    first_ts = r.json()["opted_out_at"]
    # repeating keeps the original timestamp (facts are not rewritten)
    assert sales.post(f"/contacts/{c['id']}/opt-out", json={"opted_out": True}).json()["opted_out_at"] == first_ts
    assert all(
        s["status"] == "suppressed" for s in sales.get(f"/follow-up/schedules?status=all&quote_id={q['id']}").json()
    )
    assert sales.get("/follow-up/schedules").json() == []  # nothing pending remains
    assert sales.post(f"/contacts/{c['id']}/opt-out", json={"opted_out": False}).status_code == 403
    assert approver.post(f"/contacts/{c['id']}/opt-out", json={"opted_out": False}).status_code == 403
    r = owner.post(f"/contacts/{c['id']}/opt-out", json={"opted_out": False})
    assert r.status_code == 200 and r.json()["opted_out_at"] is None
    assert [a["summary"] for a in audit(tenant_a.org_id, "contact.opt_in")]
    assert len(audit(tenant_a.org_id, "contact.opt_out")) == 2


# ---------------------------------------------------------------- S2-A03 duplicate triggers
def test_qa_s2_a03_concurrent_duplicate_sends_deliver_exactly_once(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, approver, q = approved_quote(app, tenant_a)
    c = add_contact(sales, str(tenant_a.customer_id))
    q = mark_sent(sales, q)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    sim = simulator_for(tenant_a.org_id)
    clients = [sales, approver, login_as(app, tenant_a.users["admin"]), login_as(app, tenant_a.users["owner"])]
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(lambda ac: ac.post(f"/drafts/{d['id']}/send").status_code, clients))
    assert all(code == 200 for code in results), results
    assert len(sim.outbox) == 1, f"{len(sim.outbox)} messages for one draft under concurrent triggers"
    events = sales.get(f"/communication-events?quote_id={q['id']}").json()
    assert len([e for e in events if e["kind"] == "sent"]) == 1
    assert len(audit(tenant_a.org_id, "draft.sent")) == 1


def test_qa_s2_a03_worker_and_manual_trigger_on_the_same_schedule_send_once(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    add_contact(sales, str(tenant_a.customer_id))
    admin.patch("/follow-up/policy", json={"automation_enabled": True, "offsets_days": [1]})
    q = mark_sent(sales, q)
    sim = simulator_for(tenant_a.org_id)
    force_due(tenant_a.org_id)
    with get_sessionmaker()() as db:
        assert queue.enqueue_due_schedules(db) == 1
        assert queue.enqueue_due_schedules(db) == 0  # same due schedule → same idempotency key → no second job
        assert queue.run_once(db) == 1
    assert len(sim.outbox) == 1
    s = sales.get(f"/follow-up/schedules?status=all&quote_id={q['id']}").json()[0]
    assert s["status"] == "sent" and s["draft_id"]
    # the human clicking "send" on the already-sent automated draft does not resend
    assert sales.post(f"/drafts/{s['draft_id']}/send").status_code == 200
    # a stale retry of the schedule job (e.g. operator retry) does not resend either
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        db.execute(
            text("UPDATE job_runs SET status = 'queued', next_run_at = now() - interval '1 second', attempts = 0")
        )
        db.commit()
    with get_sessionmaker()() as db:
        queue.run_once(db)
    assert len(sim.outbox) == 1
    assert admin.get("/jobs").json()[0]["status"] == "succeeded"


# ---------------------------------------------------------------- S2-F05 consent, rate limits, policy
def test_qa_s2_f05_automation_consent_is_owner_admin_only_and_recorded(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    approver = login_as(app, tenant_a.users["approver"])
    readonly = login_as(app, tenant_a.users["readonly"])
    admin = login_as(app, tenant_a.users["admin"])
    owner = login_as(app, tenant_a.users["owner"])
    pol = readonly.get("/follow-up/policy").json()
    assert pol["automation_enabled"] is False and pol["automation_consented_at"] is None, "default is draft-only"
    for ac in (sales, approver, readonly):
        assert ac.patch("/follow-up/policy", json={"automation_enabled": True}).status_code == 403
        assert ac.patch("/follow-up/policy", json={"sender_name": "Mallory"}).status_code == 403
    assert readonly.get("/follow-up/policy").json()["automation_enabled"] is False
    r = admin.patch("/follow-up/policy", json={"automation_enabled": True})
    assert r.status_code == 200 and r.json()["automation_enabled"] and r.json()["automation_consented_at"]
    rows = audit(tenant_a.org_id, "followup.policy_update")
    assert str(rows[-1]["actor"]) == admin.user_id and rows[-1]["summary"]["automation_enabled"] is True
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        by = db.execute(text("SELECT automation_consented_by FROM follow_up_policies")).scalar()
        assert str(by) == admin.user_id, "consent must name the admin who gave it"
    r = owner.patch("/follow-up/policy", json={"automation_enabled": False})
    assert r.status_code == 200 and r.json()["automation_consented_at"] is None
    assert audit(tenant_a.org_id, "followup.policy_update")[-1]["summary"]["automation_enabled"] is False


def test_qa_s2_f02_policy_validation_bounds(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    for bad in (
        {"offsets_days": [-1]},
        {"offsets_days": [366]},
        {"offsets_days": list(range(11))},
        {"max_attempts": 11},
        {"max_attempts": -1},
        {"quiet_hours_start": 24},
        {"quiet_hours_end": -1},
        {"hourly_send_limit": 0},
        {"hourly_send_limit": 1001},
        {"reply_to_email": "not-an-address"},
        {"timezone": "x" * 65},
    ):
        r = admin.patch("/follow-up/policy", json=bad)
        assert r.status_code == 422, (bad, r.status_code, r.text)
    r = admin.patch("/follow-up/policy", json={"offsets_days": [7, 3, 3], "max_attempts": 2, "timezone": "Asia/Dubai"})
    assert r.status_code == 200 and r.json()["offsets_days"] == [3, 7] and r.json()["timezone"] == "Asia/Dubai"


def test_qa_s2_f02_quiet_hours_and_weekends_shift_due_times_in_policy_timezone(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    add_contact(sales, str(tenant_a.customer_id))
    admin.patch(
        "/follow-up/policy",
        json={
            "offsets_days": [1, 2, 3, 4, 5],
            "max_attempts": 5,
            "timezone": "Asia/Dubai",
            "quiet_hours_start": 19,
            "quiet_hours_end": 8,
            "skip_weekends": True,
        },
    )
    q = mark_sent(sales, q)
    sched = sales.get(f"/follow-up/schedules?quote_id={q['id']}").json()
    assert len(sched) >= 3
    tz = ZoneInfo("Asia/Dubai")
    for s in sched:
        local = datetime.fromisoformat(s["due_at"]).astimezone(tz)
        assert 8 <= local.hour < 19, (s["due_at"], local)
        assert local.weekday() < 5, (s["due_at"], local)
    assert [s["attempt_no"] for s in sched] == sorted(s["attempt_no"] for s in sched)
    # a user-chosen reschedule is kept as chosen (explicit human decision), but never in the past
    first = sched[0]
    chosen = datetime(2027, 1, 9, 21, 30, tzinfo=UTC)  # a Saturday evening on purpose
    r = sales.post(f"/follow-up/schedules/{first['id']}/reschedule", json={"due_at": chosen.isoformat()})
    assert r.status_code == 200 and datetime.fromisoformat(r.json()["due_at"]) == chosen
    assert (
        sales.post(
            f"/follow-up/schedules/{first['id']}/reschedule", json={"due_at": "2020-01-01T00:00:00Z"}
        ).status_code
        == 422
    )


def test_qa_s2_f05_hourly_rate_limit_applies_to_manual_and_automated_sends(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    c = add_contact(sales, str(tenant_a.customer_id))
    admin.patch("/follow-up/policy", json={"hourly_send_limit": 1, "automation_enabled": True, "offsets_days": [1]})
    q = mark_sent(sales, q)
    sim = simulator_for(tenant_a.org_id)
    d1 = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    d2 = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    assert sales.post(f"/drafts/{d1['id']}/send").status_code == 200
    r = sales.post(f"/drafts/{d2['id']}/send")
    assert r.status_code == 429 and r.json()["error"] == "rate_limited"
    assert len(sim.outbox) == 1 and sales.get(f"/drafts/{d2['id']}").json()["status"] == "draft"
    # the automated path is subject to the same limit and is retried later rather than dropped or delivered
    force_due(tenant_a.org_id)
    worker_tick()
    assert len(sim.outbox) == 1
    jobs = admin.get("/jobs").json()
    assert jobs and jobs[0]["status"] in ("queued", "dead", "failed"), jobs
    assert jobs[0]["status"] == "queued", "a rate-limited automated send should be retried after the window"


def test_qa_s2_f05_automation_respects_max_attempts_counting_manual_followups(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """Policy: max 1 follow-up per quote. A human already sent one; the consented automation must not send a second."""
    sales, _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    c = add_contact(sales, str(tenant_a.customer_id))
    admin.patch("/follow-up/policy", json={"offsets_days": [2], "max_attempts": 1, "automation_enabled": True})
    q = mark_sent(sales, q)
    sim = simulator_for(tenant_a.org_id)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    assert sales.post(f"/drafts/{d['id']}/send").status_code == 200
    lc = sales.get(f"/quotes/{q['id']}/lifecycle").json()
    assert lc["recommendation"]["action"] == "record_outcome", lc["recommendation"]
    force_due(tenant_a.org_id)
    worker_tick()
    assert len(sim.outbox) == 1, "automation exceeded the policy's max_attempts after a manual follow-up"


def test_qa_s2_f02_quote_sent_through_the_system_gets_follow_ups_planned(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """Sending the first message from an approved quote moves it to 'sent'; policy timing should apply from there."""
    sales, _, q = approved_quote(app, tenant_a)
    c = add_contact(sales, str(tenant_a.customer_id))
    admin = login_as(app, tenant_a.users["admin"])
    admin.patch("/follow-up/policy", json={"offsets_days": [1, 2]})
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    assert sales.post(f"/drafts/{d['id']}/send").status_code == 200
    lc = sales.get(f"/quotes/{q['id']}/lifecycle").json()
    assert lc["status"] == "sent" and lc["events"][0]["source"] == "send"
    assert lc["schedules"], "no follow-up schedule was planned for a quote the system itself sent"


def test_qa_s2_f02_contact_validation_and_roles(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    approver = login_as(app, tenant_a.users["approver"])
    readonly = login_as(app, tenant_a.users["readonly"])
    cid = str(tenant_a.customer_id)
    for bad in (
        {"full_name": "x", "email": "nope"},
        {"full_name": "x", "email": "a@b"},
        {"full_name": "x", "email": "a@b.example\r\nBcc: c@d.example"},
        {"full_name": "", "email": "ok@b.example"},
        {"full_name": "x", "preferred_channel": "pigeon"},
    ):
        r = sales.post(f"/customers/{cid}/contacts", json=bad)
        assert r.status_code == 422, (bad, r.status_code, r.text)
    assert readonly.post(f"/customers/{cid}/contacts", json={"full_name": "x"}).status_code == 403
    assert (
        approver.post(
            f"/customers/{cid}/contacts", json={"full_name": "Phone Only", "preferred_channel": "phone"}
        ).status_code
        == 201
    )
    c = add_contact(sales, cid)
    assert readonly.patch(f"/contacts/{c['id']}", json={"full_name": "x"}).status_code == 403
    assert sales.patch(f"/contacts/{c['id']}", json={"full_name": "Renamed", "email": "bad"}).status_code == 422
    assert sales.patch(f"/contacts/{c['id']}", json={"full_name": "Renamed"}).json()["full_name"] == "Renamed"
    assert sales.post(f"/customers/{uuid.uuid4()}/contacts", json={"full_name": "x"}).status_code == 404
    assert readonly.get(f"/customers/{cid}/contacts").status_code == 200
    assert {a["summary"].get("customer_id") for a in audit(tenant_a.org_id, "contact.create")} == {cid}
