"""Stage 2 security re-verification (2026-11-12): confirmation tests for the fix rounds.

Fixed findings are covered by plain tests; an open finding is a strict xfail carrying its id (S2-SEC-12 onwards,
see docs/evidence/security-stage2-reverify-2026-11-12.md). No product code is changed by these tests.
"""

from __future__ import annotations

import re
import time

import pytest

from app.api.routers import followup as followup_router
from app.core.logging import redact
from app.db.session import get_sessionmaker, set_tenant
from app.services.comms.providers import _ADDRESS_IN_TEXT, EMAIL_RE, reset_simulators, simulator_for, valid_email
from app.services.followup import service as fu
from app.services.followup import templates as tpl
from app.services.jobs import queue
from tests.conftest import Tenant, login_as
from tests.security.test_stage2_audit import audit_actions, make_due, sent_quote_with_contact, sql

pytestmark = [pytest.mark.integration, pytest.mark.security]
OPT_TAIL = "reply with the word UNSUBSCRIBE and we will stop."


@pytest.fixture(autouse=True)
def _reset_sim() -> None:
    reset_simulators()


def _unsubscribe(admin, address: str) -> None:  # type: ignore[no-untyped-def]
    r = admin.post(
        "/integrations/simulator/inbound", json={"from_email": address, "subject": "re", "snippet": "UNSUBSCRIBE"}
    )
    assert r.status_code == 201 and r.json()["kind"] == "opt_out", r.text


def test_suppression_follows_the_address_across_case_and_recreated_contacts(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, admin, q, c = sent_quote_with_contact(app, tenant_a)
    _unsubscribe(admin, "Buyer@Customer.EXAMPLE")  # the sender's case differs from the stored address
    cust = str(tenant_a.customer_id)
    r = sales.post(f"/customers/{cust}/contacts", json={"full_name": "Again", "email": "BUYER@customer.example"})
    assert r.status_code == 201, r.text
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": r.json()["id"]})
    assert d.status_code == 403, d.text  # refused already at draft creation; the send gate is covered below
    assert simulator_for(tenant_a.org_id).outbox == []
    # surrounding blanks are refused at input, so no stored address can carry them
    for bad in (" buyer@customer.example", "buyer@customer.example ", "buyer@customer.example\n"):
        assert sales.post(f"/customers/{cust}/contacts", json={"full_name": "x", "email": bad}).status_code == 422


def test_plus_tag_and_dot_variants_are_different_addresses_pinned(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """Documents the normalisation: lower() + strip() only. `buyer+x@` and `b.uyer@` are NOT the opted-out address."""
    sales, admin, q, c = sent_quote_with_contact(app, tenant_a)
    _unsubscribe(admin, "buyer@customer.example")
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        assert fu.address_block_reason(db, tenant_a.org_id, "  BUYER@customer.example ") == "opted_out"
        assert fu.address_block_reason(db, tenant_a.org_id, "buyer+promo@customer.example") is None
        assert fu.address_block_reason(db, tenant_a.org_id, "b.uyer@customer.example") is None


def test_opt_out_recorded_after_the_draft_exists_blocks_the_send(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, admin, q, c = sent_quote_with_contact(app, tenant_a)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    _unsubscribe(admin, c["email"])
    assert sales.post(f"/drafts/{d['id']}/send").status_code == 403
    assert simulator_for(tenant_a.org_id).outbox == []
    assert sales.get(f"/drafts/{d['id']}").json()["status"] == "cancelled"
    assert "draft.suppressed" in audit_actions(tenant_a.org_id)


def test_opt_out_instruction_survives_hostile_edits(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    org = "Acme Trading"
    exact = tpl.OPT_OUT_TEXT.format(org_name=org)
    variants = [
        "Hi.",
        "Hi.\n\n" + exact.replace("UNSUBSCRIBE", "UNSUB​SCRIBE"),  # zero-width split
        "Hi.\n\n" + exact.replace(" ", " "),  # non-breaking blanks
        "Hi.\n\n" + exact.upper(),
        "Hi.\n\n<!-- hidden -->" + exact[:40] + "<br>" + exact[40:],
        "Hi.\n\n" + exact.replace("{org_name}", "Other Org"),
    ]
    for body in variants:
        assert exact in tpl.ensure_opt_out_for(body, org)
    # end to end: a zero-width-split edit is sent with the real instruction appended; unknown placeholders are refused
    sales, admin, q, c = sent_quote_with_contact(app, tenant_a)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    assert sales.patch(f"/drafts/{d['id']}", json={"body": "Hi {nope}"}).status_code == 422
    ok = sales.patch(f"/drafts/{d['id']}", json={"body": "Hi.\n\nreply with the word UNSUB​SCRIBE and we will stop."})
    assert ok.status_code == 200, ok.text
    assert sales.post(f"/drafts/{d['id']}/send").status_code == 200
    msg, _ = simulator_for(tenant_a.org_id).outbox[0]
    assert OPT_TAIL in msg.body_text and "{" not in msg.body_text.split("Hi.")[1].split("reply")[0]


def test_queued_automated_send_without_consent_is_refused_terminally(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, admin, q, c = sent_quote_with_contact(app, tenant_a, automation=True)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    assert admin.patch("/follow-up/policy", json={"automation_enabled": False}).status_code == 200
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        assert queue.enqueue(
            db, org_id=tenant_a.org_id, kind="send_draft", idempotency_key="confirm-1", payload={"draft_id": d["id"]}
        )
        db.commit()
        assert queue.run_once(db, org_id=tenant_a.org_id, max_jobs=5) == 1
    st = sql(tenant_a.org_id, "SELECT status, attempts FROM job_runs WHERE idempotency_key = 'confirm-1'")[0]
    assert tuple(st) == ("dead", 1)  # one attempt, no retry loop
    assert simulator_for(tenant_a.org_id).outbox == []
    assert sales.get(f"/drafts/{d['id']}").json()["status"] != "sent"


def test_run_once_is_capped_scoped_and_audited(app, tenant_a: Tenant, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    sent_quote_with_contact(app, tenant_a)
    q_id = sql(tenant_a.org_id, "SELECT id FROM quotes LIMIT 1")[0][0]
    make_due(tenant_a.org_id, str(q_id))
    monkeypatch.setattr(followup_router, "RUN_ONCE_MAX_JOBS", 1)
    admin = login_as(app, tenant_a.users["admin"])
    first = admin.post("/jobs/run-once")
    assert first.status_code == 200 and first.json() == {"processed": 1}
    assert admin.post("/jobs/run-once").json() == {"processed": 1}  # the second due schedule needs a second call
    assert audit_actions(tenant_a.org_id).count("worker.run_once") == 2
    assert login_as(app, tenant_a.users["sales"]).post("/jobs/run-once").status_code == 403


def test_valid_email_rejects_control_characters_and_empty_labels() -> None:
    for bad in ("a@b.example\n", "a@b.example\r", "a@b.example\x00", "a\n@b.example", "a@b..example", "a@b.example\t"):
        assert not valid_email(bad), repr(bad)
    for good in ("john.o'neil+tag@mail.example.co.uk", "a@b.io", "x@sub-domain.example.com", "A_B%c@x.example"):
        assert valid_email(good), good


def test_bounded_address_patterns_are_linear_on_hostile_input() -> None:
    size = 200_000
    shapes = ["a" * size, "a." * (size // 2), "a@" * (size // 2), "x@" + "a." * (size // 2), ("u" * 70 + "@") * 2800]
    for s in shapes:
        for fn in (lambda t: redact(t), lambda t: _ADDRESS_IN_TEXT.sub("", t), lambda t: EMAIL_RE.fullmatch(t)):
            t0 = time.perf_counter()
            fn(s)
            assert time.perf_counter() - t0 < 2.0


@pytest.mark.xfail(
    strict=True,
    reason="S2-SEC-12 (Low): log redaction and scrub_error use RFC-sized bounds, so an address with a local part over "
    "64 characters is only partly redacted and one with a domain label over 63 characters is not redacted at all",
)
def test_log_redaction_covers_over_long_address_parts() -> None:
    for addr in ("u" * 100 + "@customer.example", "bob@" + "l" * 64 + ".example"):
        out = redact(f"provider rejected {addr}")
        assert not re.search(r"u{20}|l{20}|@customer\.example", out), out
