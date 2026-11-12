"""QA (independent) — Stage 2 lifecycle (S2-F01, S2-F09) and Stage 1 regressions through the new states.

Derived from docs/PRD.md S2-F01 ("approved → sent → acknowledged (only when recorded) → awaiting_customer →
won / lost / expired; manual transitions audited"), S2-F09 (timeline shows recorded events only) and the Stage 1
acceptance rows S1-A12/A13/A15/A16/A18 re-run with Stage 2 states present. Integration evidence (real PostgreSQL,
RLS role). Nothing here reads the implementation for expected values; expectations come from the PRD/API contract.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from app.db.base import utcnow
from app.db.session import get_sessionmaker, set_tenant
from app.services.comms.providers import reset_simulators, simulator_for
from app.services.jobs import queue
from tests.conftest import AuthedClient, Tenant, login_as
from tests.integration.test_quotes import make_quote
from tests.integration.test_stage2_followup import add_contact, approved_quote, mark_sent

pytestmark = [pytest.mark.integration]


@pytest.fixture(autouse=True)
def _reset_sim() -> None:
    reset_simulators()


def transition(ac: AuthedClient, q: dict, to: str, evidence: str | None = None, **extra):  # type: ignore[no-untyped-def,type-arg]
    body = {"expected_lock_version": q["lock_version"], "to_status": to, "evidence": evidence, **extra}
    return ac.post(f"/quotes/{q['id']}/transition", json=body)


def audit_rows(org_id, action: str) -> list[dict]:  # type: ignore[no-untyped-def,type-arg]
    with get_sessionmaker()() as db:
        set_tenant(db, org_id, None)
        rows = db.execute(
            text("SELECT actor_user_id, summary FROM audit_events WHERE action = :a ORDER BY created_at"),
            {"a": action},
        ).all()
        return [{"actor": r[0], "summary": r[1]} for r in rows]


# ---------------------------------------------------------------- S2-F01 happy path and audit
def test_qa_s2_f01_full_path_sent_awaiting_acknowledged_won_is_recorded_and_audited(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    lv0 = q["lock_version"]
    assert sales.get(f"/quotes/{q['id']}/lifecycle").json()["events"] == []  # nothing recorded yet

    q = mark_sent(sales, q)
    assert q["status"] == "sent" and q["lock_version"] == lv0 + 1  # a transition is a write → lock bump
    r = transition(sales, q, "awaiting_customer")  # no evidence needed (no claim about the customer)
    assert r.status_code == 200, r.text
    q = r.json()
    r = transition(sales, q, "acknowledged", "customer confirmed receipt by phone, 2 Nov")
    assert r.status_code == 200, r.text
    q = r.json()
    r = transition(sales, q, "won", "PO 9001 received by e-mail")
    assert r.status_code == 200, r.text
    q = r.json()
    assert q["status"] == "won" and q["effective_status"] == "won"

    lc = sales.get(f"/quotes/{q['id']}/lifecycle").json()
    assert lc["status"] == "won" and lc["allowed_transitions"] == {}
    assert [(e["from_status"], e["to_status"]) for e in lc["events"]] == [
        ("approved", "sent"),
        ("sent", "awaiting_customer"),
        ("awaiting_customer", "acknowledged"),
        ("acknowledged", "won"),
    ]
    assert all(e["source"] == "manual" and e["actor_user_id"] == sales.user_id for e in lc["events"])
    assert lc["events"][2]["evidence"].startswith("customer confirmed")
    # every manual transition produced an audit row with actor, from/to and the evidence flag
    rows = audit_rows(tenant_a.org_id, "quote.transition")
    assert [(r["summary"]["from"], r["summary"]["to"], r["summary"]["has_evidence"]) for r in rows] == [
        ("approved", "sent", False),
        ("sent", "awaiting_customer", False),
        ("awaiting_customer", "acknowledged", True),
        ("acknowledged", "won", True),
    ]
    assert all(str(r["actor"]) == sales.user_id for r in rows)
    # terminal: nothing else is accepted
    assert transition(sales, q, "lost", "x").status_code == 409
    assert transition(sales, q, "sent").status_code == 409


def test_qa_s2_f01_lost_from_approved_needs_evidence_and_is_terminal(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    r = transition(sales, q, "lost")
    assert r.status_code == 422 and r.json().get("field") == "evidence"
    r = transition(sales, q, "lost", "   ")  # whitespace is not evidence
    assert r.status_code == 422
    r = transition(sales, q, "lost", "customer chose a competitor (e-mail 1 Nov)")
    assert r.status_code == 200 and r.json()["status"] == "lost"
    lc = sales.get(f"/quotes/{q['id']}/lifecycle").json()
    assert lc["allowed_transitions"] == {} and lc["recommendation"]["action"] == "none"
    assert transition(sales, r.json(), "won", "oops").status_code == 409


@pytest.mark.parametrize(
    ("to", "evidence"),
    [("won", "x"), ("acknowledged", "x"), ("awaiting_customer", None), ("sent", None)],
)
def test_qa_s2_f01_transitions_not_allowed_from_draft_or_pending(
    app, tenant_a: Tenant, to: str, evidence: str | None
) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    q = make_quote(sales, str(tenant_a.customer_id))
    assert transition(sales, q, to, evidence).status_code == 409, "draft quotes have no sales lifecycle"
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).json()
    assert transition(sales, s, to, evidence).status_code == 409, "pending approval is not approved"


def test_qa_s2_f01_cannot_skip_to_won_or_acknowledged_from_approved(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    assert transition(sales, q, "won", "PO").status_code == 409
    assert transition(sales, q, "acknowledged", "they said so").status_code == 409
    assert transition(sales, q, "awaiting_customer").status_code == 409
    assert transition(sales, q, "draft").status_code == 422  # not a lifecycle target at all
    assert transition(sales, q, "expired").status_code == 422  # expiry is derived, never claimed
    assert sales.get(f"/quotes/{q['id']}").json()["status"] == "approved"  # nothing moved


def test_qa_s2_f01_occurred_at_cannot_be_in_the_future_but_backdating_is_recorded(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    future = (utcnow() + timedelta(days=1)).isoformat()
    r = transition(sales, q, "sent", occurred_at=future)
    assert r.status_code == 422 and r.json().get("field") == "occurred_at"
    past = utcnow() - timedelta(days=2)
    r = transition(sales, q, "sent", occurred_at=past.isoformat())
    assert r.status_code == 200, r.text
    ev = sales.get(f"/quotes/{q['id']}/lifecycle").json()["events"][0]
    assert ev["occurred_at"][:16] == past.isoformat()[:16]


def test_qa_s2_f01_stale_lock_version_is_a_conflict_and_changes_nothing(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    r = sales.post(
        f"/quotes/{q['id']}/transition", json={"expected_lock_version": q["lock_version"] + 5, "to_status": "sent"}
    )
    assert r.status_code == 409 and r.json()["error"] == "conflict"
    assert r.json()["current_lock_version"] == q["lock_version"]
    assert sales.get(f"/quotes/{q['id']}").json()["status"] == "approved"
    assert sales.get(f"/quotes/{q['id']}/lifecycle").json()["events"] == []
    assert audit_rows(tenant_a.org_id, "quote.transition") == []


def test_qa_s2_f01_expired_sent_quote_only_allows_lost_with_evidence(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    q = mark_sent(sales, q)
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        db.execute(text("UPDATE quotes SET valid_until = current_date - 1 WHERE id = :q"), {"q": q["id"]})
        db.commit()
    lc = sales.get(f"/quotes/{q['id']}/lifecycle").json()
    assert lc["status"] == "expired" and lc["allowed_transitions"] == {"lost": True}
    assert transition(sales, q, "won", "PO").status_code == 409
    assert transition(sales, q, "acknowledged", "x").status_code == 409
    assert transition(sales, q, "lost").status_code == 422
    r = transition(sales, q, "lost", "no answer before validity ended")
    assert r.status_code == 200 and r.json()["status"] == "lost"
    assert sales.get(f"/quotes/{q['id']}/lifecycle").json()["events"][-1]["from_status"] == "expired"


# ---------------------------------------------------------------- S2-F09 timeline: recorded facts only
def test_qa_s2_f09_timeline_shows_only_recorded_events_and_is_readable_by_readonly(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    add_contact(sales, str(tenant_a.customer_id))
    readonly = login_as(app, tenant_a.users["readonly"])
    before = readonly.get(f"/quotes/{q['id']}/lifecycle")
    assert before.status_code == 200
    kinds = {t["kind"] for t in before.json()["timeline"]}
    assert kinds <= {"audit"}, "an approved-but-never-sent quote must show no communication or status facts"
    q = mark_sent(sales, q)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={}).json()
    after = readonly.get(f"/quotes/{q['id']}/lifecycle").json()
    titles = [t["title"] for t in after["timeline"]]
    assert "approved → sent" in titles
    assert not any(t["kind"] == "comm_out" for t in after["timeline"]), "a draft is not a sent message"
    assert not any("acknowledg" in t["title"] for t in after["timeline"])
    assert sales.post(f"/drafts/{d['id']}/send").status_code == 200
    after = readonly.get(f"/quotes/{q['id']}/lifecycle").json()
    outs = [t for t in after["timeline"] if t["kind"] == "comm_out"]
    assert len(outs) == 1 and outs[0]["source"] == "simulator" and outs[0]["title"] == "sent"
    # the timeline is ordered by time and every item names a source
    ats = [t["at"] for t in after["timeline"]]
    assert ats == sorted(ats)
    # read-only may look but not act
    assert readonly.post(f"/quotes/{q['id']}/drafts", json={}).status_code == 403
    assert transition(readonly, q, "won", "x").status_code == 403


# ---------------------------------------------------------------- Stage 1 regressions with Stage 2 states present
def test_qa_s1_regression_withdraw_of_sent_quote_blocks_followups_and_sends(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, approver, q = approved_quote(app, tenant_a)
    c = add_contact(sales, str(tenant_a.customer_id))
    q = mark_sent(sales, q)
    d = sales.post(f"/quotes/{q['id']}/drafts", json={"contact_id": c["id"]}).json()
    assert len(sales.get(f"/follow-up/schedules?quote_id={q['id']}").json()) >= 1
    # sales cannot withdraw (S1 role matrix); approver can, with a reason
    assert (
        sales.post(
            f"/quotes/{q['id']}/withdraw", json={"expected_lock_version": q["lock_version"], "reason": "x"}
        ).status_code
        == 403
    )
    r = approver.post(
        f"/quotes/{q['id']}/withdraw", json={"expected_lock_version": q["lock_version"], "reason": "price error"}
    )
    assert r.status_code == 200 and r.json()["status"] == "withdrawn"
    lc = sales.get(f"/quotes/{q['id']}/lifecycle").json()
    assert lc["status"] == "withdrawn" and lc["allowed_transitions"] == {}
    assert lc["recommendation"]["action"] == "none"
    # the prepared draft can no longer be sent and the worker never sends for a withdrawn quote
    assert sales.post(f"/drafts/{d['id']}/send").status_code == 409
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        db.execute(text("UPDATE follow_up_schedules SET due_at = now() - interval '1 minute'"))
        db.commit()
    with get_sessionmaker()() as db:
        queue.enqueue_due_schedules(db)
        queue.run_once(db)
    assert simulator_for(tenant_a.org_id).outbox == []
    sched = sales.get(f"/follow-up/schedules?status=all&quote_id={q['id']}").json()
    assert sched and all(s["status"] in ("superseded", "cancelled", "suppressed") for s in sched), sched
    assert sales.get(f"/quotes/{q['id']}/versions/1/pdf").status_code == 409  # S1-A15 still holds


def test_qa_s1_regression_new_version_from_sent_quote_reopens_editor_and_approval(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, approver, q = approved_quote(app, tenant_a)
    add_contact(sales, str(tenant_a.customer_id))
    q = mark_sent(sales, q)
    r = sales.post(f"/quotes/{q['id']}/versions", json={"expected_lock_version": q["lock_version"]})
    assert r.status_code == 201, r.text
    q = r.json()
    assert q["status"] == "draft" and q["current_version"]["version_no"] == 2
    assert sales.get(f"/quotes/{q['id']}/lifecycle").json()["allowed_transitions"] == {}
    # editor still works on v2 (quantity edit keeps catalog price)
    lines = q["current_version"]["lines"]
    r = sales.patch(
        f"/quotes/{q['id']}",
        json={
            "expected_lock_version": q["lock_version"],
            "lines": [
                {"id": ln["id"], "quantity": "600"} if i == 0 else {"id": ln["id"]} for i, ln in enumerate(lines)
            ],
        },
    )
    assert r.status_code == 200, r.text
    q = r.json()
    assert q["current_version"]["lines"][0]["quantity"].startswith("600")
    assert q["current_version"]["lines"][0]["price_source"] == "catalog"
    # approval path unchanged: submit, self-approval refused, approver approves
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]})
    assert s.status_code == 200, s.text
    assert (
        sales.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": s.json()["lock_version"]}).status_code
        == 403
    )
    a = approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": s.json()["lock_version"]})
    assert a.status_code == 200 and a.json()["status"] == "approved"
    lc = sales.get(f"/quotes/{q['id']}/lifecycle").json()
    assert lc["status"] == "approved" and set(lc["allowed_transitions"]) == {"sent", "lost"}
    # the v1 'sent' event stays in history; the old schedules are superseded by the worker, not silently kept pending
    assert [e["to_status"] for e in lc["events"]] == ["sent"]
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        db.execute(text("UPDATE follow_up_schedules SET due_at = now() - interval '1 minute'"))
        db.commit()
    with get_sessionmaker()() as db:
        queue.enqueue_due_schedules(db)
        queue.run_once(db)
    assert simulator_for(tenant_a.org_id).outbox == []
    # the v2 approval re-arms the lifecycle: mark sent works again and plans fresh schedules
    q = a.json()
    q = mark_sent(sales, q)
    pend = sales.get(f"/follow-up/schedules?quote_id={q['id']}").json()
    assert pend and all(p["status"] == "pending" for p in pend)
    # PDF of the approved v2 is deterministic (S1-A18)
    p1 = sales.get(f"/quotes/{q['id']}/versions/2/pdf")
    p2 = sales.get(f"/quotes/{q['id']}/versions/2/pdf")
    assert p1.status_code == 200 and p1.content == p2.content


def test_qa_s1_regression_stage1_editor_and_approval_smoke(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    approver = login_as(app, tenant_a.users["approver"])
    readonly = login_as(app, tenant_a.users["readonly"])
    q = make_quote(sales, str(tenant_a.customer_id))
    assert q["status"] == "draft" and q["blockers"] == []
    r = sales.patch(
        f"/quotes/{q['id']}", json={"expected_lock_version": q["lock_version"], "notes": "Deliver to gate 3"}
    )
    assert r.status_code == 200 and r.json()["current_version"]["notes"] == "Deliver to gate 3"
    q = r.json()
    # S1-A16 concurrent edit
    assert (
        sales.patch(
            f"/quotes/{q['id']}", json={"expected_lock_version": q["lock_version"] - 1, "notes": "x"}
        ).status_code
        == 409
    )
    assert (
        readonly.patch(
            f"/quotes/{q['id']}", json={"expected_lock_version": q["lock_version"], "notes": "x"}
        ).status_code
        == 403
    )
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).json()
    assert s["status"] == "pending_approval"
    assert (
        readonly.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": s["lock_version"]}).status_code
        == 403
    )
    a = approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": s["lock_version"]}).json()
    assert a["status"] == "approved"
    assert sales.get(f"/quotes/{q['id']}/versions/1/csv").status_code == 200
    assert "cost_price" not in sales.get(f"/quotes/{q['id']}/versions/1/csv").text
    hist = sales.get(f"/quotes/{q['id']}/history").json()
    assert {"quote.create", "quote.update", "quote.submit", "quote.approve"} <= {e["action"] for e in hist["audit"]}
    # the quote list still filters by the new states without error
    assert sales.get("/quotes?status=sent").status_code == 200
    assert sales.get("/quotes?status=approved").json()[0]["id"] == q["id"]


def test_qa_s1_regression_expired_sent_quote_is_not_exportable_as_current(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    q = mark_sent(sales, q)
    assert sales.get(f"/quotes/{q['id']}/versions/1/pdf").status_code == 200
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        db.execute(text("UPDATE quotes SET valid_until = current_date - 1 WHERE id = :q"), {"q": q["id"]})
        db.commit()
    assert sales.get(f"/quotes/{q['id']}/versions/1/pdf").status_code == 409
    assert sales.get(f"/quotes/{q['id']}/versions/1/csv").status_code == 409
