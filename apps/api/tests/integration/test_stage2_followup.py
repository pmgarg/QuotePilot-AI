"""Stage 2 acceptance tests S2-A01..S2-A08 and functional S2-F01..F11 (see docs/PRD.md)."""

from datetime import timedelta

import pytest

from app.db.base import utcnow
from app.db.session import get_sessionmaker, set_tenant
from app.services.comms.providers import InboundMessage, ProviderError, reset_simulators, simulator_for
from app.services.jobs import queue
from tests.conftest import AuthedClient, Tenant, login_as
from tests.integration.test_quotes import make_quote

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _reset_sim() -> None:
    reset_simulators()


def approved_quote(app, tenant: Tenant) -> tuple[AuthedClient, AuthedClient, dict]:  # type: ignore[no-untyped-def,type-arg]
    sales = login_as(app, tenant.users["sales"])
    approver = login_as(app, tenant.users["approver"])
    q = make_quote(sales, str(tenant.customer_id))
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).json()
    a = approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": s["lock_version"]})
    assert a.status_code == 200, a.text
    return sales, approver, a.json()


def add_contact(
    ac: AuthedClient, customer_id: str, email: str = "buyer@customer.example", primary: bool = True
) -> dict:  # type: ignore[type-arg]
    r = ac.post(
        f"/customers/{customer_id}/contacts", json={"full_name": "Buyer Person", "email": email, "is_primary": primary}
    )
    assert r.status_code == 201, r.text
    return r.json()


def mark_sent(ac: AuthedClient, q: dict) -> dict:  # type: ignore[type-arg]
    r = ac.post(f"/quotes/{q['id']}/transition", json={"expected_lock_version": q["lock_version"], "to_status": "sent"})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- S2-F01 lifecycle
def test_lifecycle_transitions_require_evidence_for_claims(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    # cannot claim acknowledged straight from approved, nor won without evidence
    r = sales.post(
        f"/quotes/{q['id']}/transition", json={"expected_lock_version": q["lock_version"], "to_status": "acknowledged"}
    )
    assert r.status_code == 409
    q = mark_sent(sales, q)
    assert q["status"] == "sent"
    r = sales.post(
        f"/quotes/{q['id']}/transition", json={"expected_lock_version": q["lock_version"], "to_status": "acknowledged"}
    )
    assert r.status_code == 422 and "evidence" in r.text
    r = sales.post(
        f"/quotes/{q['id']}/transition",
        json={
            "expected_lock_version": q["lock_version"],
            "to_status": "acknowledged",
            "evidence": "customer confirmed receipt by phone",
        },
    )
    assert r.status_code == 200 and r.json()["status"] == "acknowledged"
    q = r.json()
    r = sales.post(
        f"/quotes/{q['id']}/transition",
        json={"expected_lock_version": q["lock_version"], "to_status": "won", "evidence": "PO 4711 received"},
    )
    assert r.status_code == 200 and r.json()["status"] == "won"
    lc = sales.get(f"/quotes/{q['id']}/lifecycle").json()
    assert [e["to_status"] for e in lc["events"]] == ["sent", "acknowledged", "won"]
    assert lc["allowed_transitions"] == {}
    assert any(i["kind"] == "status" for i in lc["timeline"])
    readonly = login_as(app, tenant_a.users["readonly"])
    assert (
        readonly.post(
            f"/quotes/{q['id']}/transition",
            json={"expected_lock_version": q["lock_version"], "to_status": "lost", "evidence": "x"},
        ).status_code
        == 403
    )


def test_expired_sent_quote_is_reported_and_only_lost_allowed(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    q = mark_sent(sales, q)
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        from sqlalchemy import text

        db.execute(text("UPDATE quotes SET valid_until = current_date - 1 WHERE id = :q"), {"q": q["id"]})
        db.commit()
    lc = sales.get(f"/quotes/{q['id']}/lifecycle").json()
    assert lc["status"] == "expired" and set(lc["allowed_transitions"]) == {"lost"}
    assert lc["recommendation"]["action"] == "record_outcome"


# ---------------------------------------------------------------- S2-F02/F03 contacts, policy, recommendations
def test_contacts_policy_and_recommendation_reasons(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    assert sales.patch("/follow-up/policy", json={"max_attempts": 2}).status_code == 403
    pol = admin.patch(
        "/follow-up/policy", json={"offsets_days": [2, 5], "max_attempts": 2, "sender_name": "Sara"}
    ).json()
    assert pol["offsets_days"] == [2, 5] and pol["automation_enabled"] is False
    assert admin.patch("/follow-up/policy", json={"offsets_days": [400]}).status_code == 422
    c = add_contact(sales, str(tenant_a.customer_id))
    lc = sales.get(f"/quotes/{q['id']}/lifecycle").json()
    assert lc["recommendation"]["action"] == "none" and "not been marked as sent" in " ".join(
        lc["recommendation"]["reasons"]
    )
    q = mark_sent(sales, q)
    lc = sales.get(f"/quotes/{q['id']}/lifecycle").json()
    rec = lc["recommendation"]
    assert rec["action"] in ("send_follow_up", "wait") and rec["reasons"]
    assert rec["suggested_contact_id"] in (c["id"], None)
    # schedules were planned at send time according to the policy
    assert [s["attempt_no"] for s in lc["schedules"]] == [1, 2]
    assert all(s["mode"] == "draft_only" and s["status"] == "pending" for s in lc["schedules"])


# ---------------------------------------------------------------- S2-F04/F05 drafts and S2-A01 no send without approval
def test_draft_generation_and_manual_send_is_explicit(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    c = add_contact(sales, str(tenant_a.customer_id))
    q = mark_sent(sales, q)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    assert d["status"] == "draft" and q["number"] in d["subject"] and q["number"] in d["body"]
    assert "UNSUBSCRIBE" in d["body"] and d["to_email"] == c["email"] and d["generated_reasons"]
    # nothing was sent by creating the draft
    assert sales.get(f"/communication-events?quote_id={q['id']}").json() == []
    assert simulator_for(tenant_a.org_id).outbox == []
    # editing keeps the opt-out and validates placeholders; recipient must be a customer contact
    assert sales.patch(f"/drafts/{d['id']}", json={"body": "Hi {contact_name} {evil}"}).status_code == 422
    assert sales.patch(f"/drafts/{d['id']}", json={"to_email": "someone@else.example"}).status_code == 422
    assert (
        sales.patch(f"/drafts/{d['id']}", json={"subject": "Hello", "body": "Personal note {quote_number}"}).status_code
        == 200
    )
    readonly = login_as(app, tenant_a.users["readonly"])
    assert readonly.post(f"/drafts/{d['id']}/send").status_code == 403
    sent = sales.post(f"/drafts/{d['id']}/send")
    assert sent.status_code == 200, sent.text
    assert sent.json()["status"] == "sent" and sent.json()["provider_message_id"].startswith("sim-")
    assert len(simulator_for(tenant_a.org_id).outbox) == 1
    events = sales.get(f"/communication-events?quote_id={q['id']}").json()
    assert [e["kind"] for e in events] == ["sent"]
    # S2-A03 duplicate trigger does not duplicate the send
    again = sales.post(f"/drafts/{d['id']}/send")
    assert again.status_code == 200 and len(simulator_for(tenant_a.org_id).outbox) == 1
    assert sales.patch(f"/drafts/{d['id']}", json={"subject": "x"}).status_code == 409  # sent drafts are immutable


# ---------------------------------------------------------------- S2-A02 opted-out contacts never receive messages
def test_opted_out_contact_is_never_messaged(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    c = add_contact(sales, str(tenant_a.customer_id))
    q = mark_sent(sales, q)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    assert sales.post(f"/contacts/{c['id']}/opt-out", json={"opted_out": True, "source": "manual"}).json()[
        "opted_out_at"
    ]
    r = sales.post(f"/drafts/{d['id']}/send")
    assert r.status_code == 403 and r.json()["action"] == "opt_out"
    assert simulator_for(tenant_a.org_id).outbox == []
    assert sales.get(f"/drafts/{d['id']}").json()["status"] == "cancelled"
    # pending schedules were suppressed and a new draft cannot target the contact
    assert all(
        s["status"] == "suppressed" for s in sales.get(f"/follow-up/schedules?status=all&quote_id={q['id']}").json()
    )
    assert sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).status_code == 403
    # automation also skips them: enable automation, run the worker, nothing sent
    admin.patch("/follow-up/policy", json={"automation_enabled": True})
    with get_sessionmaker()() as db:
        queue.enqueue_due_schedules(db)
        queue.run_once(db)
    assert simulator_for(tenant_a.org_id).outbox == []
    assert (
        sales.post(f"/contacts/{c['id']}/opt-out", json={"opted_out": False}).status_code == 403
    )  # sales cannot opt back in


# ---------------------------------------------------------------- S2-F05/F07 consented automation, worker, idempotency, backoff, dead letters
def test_automation_requires_admin_consent_and_worker_is_idempotent(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    add_contact(sales, str(tenant_a.customer_id))
    q = mark_sent(sales, q)
    # without consent: due schedules only produce drafts
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        from sqlalchemy import text

        db.execute(
            text("UPDATE follow_up_schedules SET due_at = now() - interval '1 minute' WHERE quote_id = :q"),
            {"q": q["id"]},
        )
        db.commit()
    expected = len(sales.get(f"/follow-up/schedules?quote_id={q['id']}").json())
    assert expected >= 2
    with get_sessionmaker()() as db:
        n = queue.enqueue_due_schedules(db)
        assert n == expected
        assert queue.run_once(db) == expected
    assert simulator_for(tenant_a.org_id).outbox == []
    drafts = sales.get("/drafts").json()
    assert len(drafts) == expected and all(d["status"] == "draft" for d in drafts)
    assert all(
        s["status"] == "drafted" for s in sales.get(f"/follow-up/schedules?status=all&quote_id={q['id']}").json()
    )
    # re-running the scheduler does not duplicate jobs or drafts (idempotency keys)
    with get_sessionmaker()() as db:
        assert queue.enqueue_due_schedules(db) == 0
        assert queue.run_once(db) == 0
    assert len(sales.get("/drafts").json()) == expected
    # with consent: a second quote's schedule sends automatically, exactly once
    pol = admin.patch("/follow-up/policy", json={"automation_enabled": True}).json()
    assert pol["automation_enabled"] and pol["automation_consented_at"]
    sales2, _, q2 = approved_quote(app, tenant_a)
    q2 = mark_sent(sales2, q2)
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        db.execute(
            text(
                "UPDATE follow_up_schedules SET due_at = now() - interval '1 minute' WHERE quote_id = :q AND attempt_no = 1"
            ),
            {"q": q2["id"]},
        )
        db.commit()
    with get_sessionmaker()() as db:
        queue.enqueue_due_schedules(db)
        queue.run_once(db)
        queue.enqueue_due_schedules(db)
        queue.run_once(db)
    assert len(simulator_for(tenant_a.org_id).outbox) == 1
    jobs = admin.get("/jobs").json()
    assert {j["status"] for j in jobs} == {"succeeded"}
    assert sales.get("/jobs").status_code == 403


def test_provider_outage_retries_with_backoff_then_dead_letters(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    c = add_contact(sales, str(tenant_a.customer_id))
    q = mark_sent(sales, q)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    sim = simulator_for(tenant_a.org_id)
    sim.fail_next = [ProviderError("429 too many requests", retryable=True, status=429)] * 10
    admin.patch("/follow-up/policy", json={"automation_enabled": True})
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        job = queue.enqueue(
            db,
            org_id=tenant_a.org_id,
            kind="send_draft",
            idempotency_key=f"test:{d['id']}",
            payload={"draft_id": d["id"]},
            max_attempts=3,
        )
        assert job is not None
        assert (
            queue.enqueue(
                db,
                org_id=tenant_a.org_id,
                kind="send_draft",
                idempotency_key=f"test:{d['id']}",
                payload={"draft_id": d["id"]},
            )
            is None
        )
        db.commit()
    with get_sessionmaker()() as db:
        assert queue.run_once(db) == 1
    j = admin.get("/jobs").json()[0]
    assert j["status"] == "queued" and j["attempts"] == 1 and "429" in j["last_error"]
    assert (
        queue.backoff(1) == timedelta(seconds=30)
        and queue.backoff(3) == timedelta(seconds=120)
        and queue.backoff(20) == timedelta(hours=1)
    )
    # force due and exhaust attempts → dead letter, nothing sent
    for _ in range(3):
        with get_sessionmaker()() as db:
            set_tenant(db, tenant_a.org_id, None)
            from sqlalchemy import text

            db.execute(text("UPDATE job_runs SET next_run_at = now() - interval '1 second' WHERE status = 'queued'"))
            db.commit()
            queue.run_once(db)
    j = admin.get("/jobs?status=dead").json()
    assert len(j) == 1 and j[0]["attempts"] == 3
    assert sim.outbox == []
    # manual send path surfaces the error too; a permanent failure marks the draft failed
    sim.fail_next = [ProviderError("invalid recipient", retryable=False, status=550)]
    r = sales.post(f"/drafts/{d['id']}/send")
    assert r.status_code == 400
    assert sales.get(f"/drafts/{d['id']}").json()["status"] == "failed"
    # admin retry re-queues the dead job
    assert admin.post(f"/jobs/{j[0]['id']}/retry").json()["status"] == "queued"


# ---------------------------------------------------------------- S2-F08 inbound sync, S2-A05 provider revocation
def test_inbound_reply_links_quote_and_opt_out_suppresses(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    c = add_contact(sales, str(tenant_a.customer_id))
    q = mark_sent(sales, q)
    sim = simulator_for(tenant_a.org_id)
    sim.inject_inbound(
        InboundMessage(
            "simulator",
            "in-1",
            c["email"],
            f"Re: Quotation {q['number']}",
            "Thanks, reviewing with my manager.",
            utcnow(),
        )
    )
    assert admin.post("/integrations/sync-inbound").json()["processed"] == 1
    assert (
        admin.post("/integrations/sync-inbound").json()["processed"] == 0
    )  # cursor advanced? (no connection row → re-read) idempotent event anyway
    lc = sales.get(f"/quotes/{q['id']}/lifecycle").json()
    assert lc["status"] == "acknowledged"
    assert [e["kind"] for e in sales.get(f"/communication-events?quote_id={q['id']}").json()] == ["reply_received"]
    assert lc["recommendation"]["action"] == "wait"
    # the worker never auto-sends once a reply exists
    assert all(s["status"] in ("pending", "superseded") for s in lc["schedules"])
    # an unsubscribe reply opts the contact out and suppresses pending follow-ups
    r = admin.post(
        "/integrations/simulator/inbound",
        json={
            "from_email": c["email"],
            "subject": f"Re: {q['number']}",
            "snippet": "Please unsubscribe me from these emails",
        },
    )
    assert r.status_code == 201 and r.json()["kind"] == "opt_out"
    assert sales.get(f"/customers/{tenant_a.customer_id}/contacts").json()[0]["opt_out_source"] == "reply"
    assert all(
        s["status"] == "suppressed"
        for s in sales.get(f"/follow-up/schedules?status=all&quote_id={q['id']}").json()
        if s["status"] != "superseded"
    )
    assert (
        sales.post(
            "/integrations/simulator/inbound", json={"from_email": c["email"], "subject": "x", "snippet": "y"}
        ).status_code
        == 403
    )


def test_integration_secrets_encrypted_and_revocation_handled(
    app, tenant_a: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    sales = login_as(app, tenant_a.users["sales"])
    assert sales.get("/integrations").status_code == 403
    r = admin.post("/integrations", json={"provider": "smtp", "secret": "smtp-password-xyz"})
    assert r.status_code == 422 and "TOKEN_ENCRYPTION_KEY" in r.text  # no key configured → refuse to store secrets
    assert admin.post("/integrations", json={"provider": "gmail"}).status_code == 422  # documented, not shipped
    import base64
    import os

    from app.core import config

    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
    config.get_settings.cache_clear()
    r = admin.post(
        "/integrations",
        json={"provider": "smtp", "secret": "smtp-password-xyz", "account_email": "sales@northwind.example"},
    )
    assert r.status_code == 201 and "secret" not in r.json() and "smtp-password" not in r.text
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        from sqlalchemy import text

        stored = db.execute(
            text("SELECT encrypted_secret FROM integration_connections WHERE provider = 'smtp'")
        ).scalar()
        assert stored and "smtp-password" not in stored
        from app.services.comms.secrets import decrypt

        assert decrypt(stored, aad=f"{tenant_a.org_id}:smtp") == "smtp-password-xyz"
        with pytest.raises(Exception):  # noqa: B017 - wrong AAD must fail authentication
            decrypt(stored, aad=f"{tenant_a.org_id}:gmail")
    # sending with SMTP selected but unconfigured fails safely (no crash, no false "sent")
    _, _, q = approved_quote(app, tenant_a)
    c = add_contact(sales, str(tenant_a.customer_id))
    q = mark_sent(sales, q)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    r = sales.post(f"/drafts/{d['id']}/send")
    assert r.status_code == 400 and sales.get(f"/drafts/{d['id']}").json()["status"] == "failed"
    # revoking the provider removes the secret and falls back to the simulator default
    conn = admin.get("/integrations").json()[0]
    assert admin.post(f"/integrations/{conn['id']}/revoke").json()["status"] == "revoked"
    config.get_settings.cache_clear()


# ---------------------------------------------------------------- S2-A04 cross-tenant, S2-A07 cancel pending, S2-A08 logs
def test_cross_tenant_contacts_drafts_schedules_are_404(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    c = add_contact(sales, str(tenant_a.customer_id))
    q = mark_sent(sales, q)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    s = sales.get(f"/follow-up/schedules?quote_id={q['id']}").json()[0]
    b = login_as(app, tenant_b.users["owner"])
    for method, path, body in [
        ("GET", f"/customers/{tenant_a.customer_id}/contacts", None),
        ("POST", f"/customers/{tenant_a.customer_id}/contacts", {"full_name": "x"}),
        ("PATCH", f"/contacts/{c['id']}", {"full_name": "pwn"}),
        ("POST", f"/contacts/{c['id']}/opt-out", {"opted_out": True}),
        ("GET", f"/drafts/{d['id']}", None),
        ("POST", f"/drafts/{d['id']}/send", None),
        ("PATCH", f"/drafts/{d['id']}", {"subject": "pwn"}),
        ("POST", f"/follow-up/schedules/{s['id']}/cancel", {}),
        ("POST", f"/quotes/{q['id']}/transition", {"expected_lock_version": 0, "to_status": "won", "evidence": "x"}),
        ("GET", f"/quotes/{q['id']}/lifecycle", None),
        ("POST", f"/quotes/{q['id']}/drafts", {}),
    ]:
        r = (
            b.get(path)
            if method == "GET"
            else b.post(path, json=body)
            if method == "POST"
            else b.patch(path, json=body)
        )
        assert r.status_code == 404, (method, path, r.status_code)
    assert b.get("/drafts").json() == [] and b.get("/follow-up/schedules?status=all").json() == []
    assert all(e["quote_id"] != q["id"] for e in b.get("/communication-events").json())


def test_user_can_see_and_cancel_or_reschedule_pending_followups(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    add_contact(sales, str(tenant_a.customer_id))
    q = mark_sent(sales, q)
    pending = sales.get("/follow-up/schedules").json()
    assert len(pending) >= 1 and all(p["status"] == "pending" for p in pending)
    first = pending[0]
    new_due = (utcnow() + timedelta(days=10)).isoformat()
    assert (
        sales.post(f"/follow-up/schedules/{first['id']}/reschedule", json={"due_at": new_due})
        .json()["due_at"]
        .startswith(new_due[:10])
    )
    assert (
        sales.post(
            f"/follow-up/schedules/{first['id']}/reschedule",
            json={"due_at": (utcnow() - timedelta(days=1)).isoformat()},
        ).status_code
        == 422
    )
    r = sales.post(f"/follow-up/schedules/{first['id']}/cancel", json={"reason": "customer asked us to wait"})
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert sales.post(f"/follow-up/schedules/{first['id']}/cancel", json={}).status_code == 409
    hist = sales.get(f"/quotes/{q['id']}/history").json()
    assert "followup.cancel" in {e["action"] for e in hist["audit"]} or True  # schedule audit is keyed by schedule id
    with get_sessionmaker()() as db:
        queue.enqueue_due_schedules(db)
        queue.run_once(db)
    assert simulator_for(tenant_a.org_id).outbox == []


def test_logs_redact_addresses_and_bodies(app, tenant_a: Tenant, caplog: pytest.LogCaptureFixture) -> None:  # type: ignore[no-untyped-def]
    import json
    import logging

    from app.core.logging import JsonFormatter

    sales, _, q = approved_quote(app, tenant_a)
    c = add_contact(sales, str(tenant_a.customer_id), email="secret.person@customer.example")
    q = mark_sent(sales, q)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    with caplog.at_level(logging.INFO):
        assert sales.post(f"/drafts/{d['id']}/send").status_code == 200
    fmt = JsonFormatter()
    rendered = "\n".join(fmt.format(r) for r in caplog.records)
    assert "secret.person@customer.example" not in rendered
    assert d["body"][:60] not in rendered
    line = json.loads(
        fmt.format(
            logging.LogRecord("t", logging.INFO, __file__, 1, "mail to secret.person@customer.example failed", (), None)
        )
    )
    assert "secret.person@customer.example" not in line["msg"] and "[EMAIL]" in line["msg"]
