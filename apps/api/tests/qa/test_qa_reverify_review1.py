"""QA (independent) re-verification of release review 1 items on HEAD ff99762.

Derived from docs/evidence/release-review-1-stage1-2026-11-01.md ("Required retest"), docs/PRD.md (S1-A03, S1-A08,
S1-A12, S1-A13, S1-A15, S1-A18, S1-A19) and docs/API_CONTRACT.md (blocker `manual_price_unconfirmed`,
`POST /quotes/{id}/lines/{line_id}/price-override`). Each test probes a case the implementer's regression test in
tests/integration/test_review1_fixes.py does not cover. Integration evidence (API + PostgreSQL + RLS).
"""

from __future__ import annotations

import hashlib
import io
import re
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.db.session import get_sessionmaker, set_tenant
from app.models.audit import AuditEvent
from app.services.auth import account_limiter, login_limiter
from tests.conftest import API, PASSWORD, Tenant, login_as, make_tenant, upload
from tests.integration.test_quotes import make_quote

pytestmark = pytest.mark.integration

PCT_FIGURE = re.compile(r"-?\d+(\.\d+)?\s*%")


def _approved(app, tenant: Tenant, text_: str | None = None):  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant.users["sales"])
    approver = login_as(app, tenant.users["approver"])
    q = make_quote(sales, str(tenant.customer_id), text_)
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]})
    assert s.status_code == 200, s.text
    a = approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": s.json()["lock_version"]})
    assert a.status_code == 200, a.text
    return sales, approver, a.json()


def _pdf_text(data: bytes) -> str:
    """Text as a reader sees it. Raw byte-substring checks on reportlab output are not reliable evidence because
    page streams may be compressed/encoded, so every content assertion below goes through text extraction."""
    import pdfplumber

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return "\n".join((p.extract_text() or "") for p in pdf.pages)


def _freeze_snapshot_validity(org_id: uuid.UUID, quote_id: str, version_no: int, when: date) -> None:
    """Simulate time passing for an approved version: move its frozen `document_snapshot.valid_until`."""
    with get_sessionmaker()() as db:
        set_tenant(db, org_id, None)
        res = db.execute(
            text(
                "UPDATE quote_versions SET document_snapshot = jsonb_set(document_snapshot, '{valid_until}', "
                "to_jsonb(CAST(:d AS text))) WHERE quote_id = :q AND version_no = :v"
            ),
            {"d": when.isoformat(), "q": quote_id, "v": version_no},
        )
        assert res.rowcount == 1  # type: ignore[attr-defined]
        db.commit()


# ---------------------------------------------------------------------------------------------------------------
# H-1 — approved document immutable (S1-A15, S1-A18, S1-F13)
# ---------------------------------------------------------------------------------------------------------------


def test_h1_customer_rename_and_brand_change_do_not_alter_approved_pdf_or_csv(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """Reviewer repro extended: customer rename + org brand/footer/legal change + new version with edited lines.
    The approved v1 PDF (GET and POST) and CSV must be byte-identical before and after, and the old names kept."""
    sales, approver, q = _approved(app, tenant_a)
    v1 = next(v for v in q["versions"] if v["version_no"] == 1)
    pdf_before = sales.get(f"/quotes/{q['id']}/versions/1/pdf")
    csv_before = sales.get(f"/quotes/{q['id']}/versions/1/csv")
    assert pdf_before.status_code == 200 and csv_before.status_code == 200
    text_before = _pdf_text(pdf_before.content)
    assert "Alpha Trading Customer" in text_before and "Alpha Trading" in text_before and "APPROVED" in text_before
    assert hashlib.sha256(pdf_before.content).hexdigest() == v1["pdf_sha256"]

    # live data changes after approval
    owner = login_as(app, tenant_a.users["owner"])
    r = sales.patch(f"/customers/{tenant_a.customer_id}", json={"name": "Renamed Customer Ltd", "reference": "NEW-REF"})
    assert r.status_code == 200, r.text
    r = owner.patch(
        "/org",
        json={
            "name": "Rebranded Org",
            "legal_name": "Rebranded Org L.L.C.",
            "address": "99 New Avenue",
            "brand_color": "#ff0000",
            "pdf_footer": "NEW FOOTER TEXT",
        },
    )
    assert r.status_code == 200, r.text
    # a new draft version with different lines, validity and notes
    nv = sales.post(f"/quotes/{q['id']}/versions", json={"expected_lock_version": q["lock_version"]}).json()
    ln = nv["current_version"]["lines"][0]
    r = sales.patch(
        f"/quotes/{q['id']}",
        json={
            "expected_lock_version": nv["lock_version"],
            "valid_until": "2099-12-31",
            "notes": "NOTES ADDED IN V2",
            "lines": [{"id": ln["id"], "quantity": "999", "discount_pct": "3"}],
        },
    )
    assert r.status_code == 200, r.text

    for role in ("sales", "approver", "readonly", "owner"):
        c = login_as(app, tenant_a.users[role])
        again = c.get(f"/quotes/{q['id']}/versions/1/pdf")
        assert again.status_code == 200 and again.content == pdf_before.content, role
        posted = c.post(f"/quotes/{q['id']}/versions/1/pdf")
        assert posted.status_code == 200 and posted.json()["sha256"] == v1["pdf_sha256"], role
    text_after = _pdf_text(sales.get(f"/quotes/{q['id']}/versions/1/pdf").content)
    assert text_after == text_before
    for needle in ("Renamed Customer", "NEW-REF", "Rebranded", "99 New Avenue", "NEW FOOTER", "2099-12-31"):
        assert needle not in text_after, needle
    assert "Alpha Trading Customer" in text_after
    # CSV of v1: lines are the approved rows (quantity 500, no discount), identical to the pre-change export
    csv_after = sales.get(f"/quotes/{q['id']}/versions/1/csv")
    assert csv_after.status_code == 200 and csv_after.content == csv_before.content
    assert b"999" not in csv_after.content
    # the stored sha is untouched and the v2 draft renders the *new* data (so the snapshot is what froze v1)
    hist = sales.get(f"/quotes/{q['id']}").json()
    assert next(v for v in hist["versions"] if v["version_no"] == 1)["pdf_sha256"] == v1["pdf_sha256"]
    draft = _pdf_text(sales.get(f"/quotes/{q['id']}/versions/2/pdf").content)
    assert "Rebranded Org" in draft and "Renamed Customer Ltd" in draft and "NOT APPROVED" in draft
    assert "2099-12-31" in draft and "NOTES ADDED IN V2" in draft


def test_h1_stored_pdf_lost_is_rerendered_from_snapshot_with_identical_hash(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """Operational case: if the stored artifact disappears, the re-render must come from the frozen snapshot
    (same bytes), not from the live organisation/customer rows."""
    from app.services.storage import get_storage

    sales, _approver, q = _approved(app, tenant_a)
    v1 = next(v for v in q["versions"] if v["version_no"] == 1)
    first = sales.get(f"/quotes/{q['id']}/versions/1/pdf").content
    owner = login_as(app, tenant_a.users["owner"])
    assert owner.patch("/org", json={"name": "Changed After Approval", "pdf_footer": "changed"}).status_code == 200
    assert sales.patch(f"/customers/{tenant_a.customer_id}", json={"name": "Changed Customer"}).status_code == 200
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        key = db.execute(
            text("SELECT pdf_storage_key FROM quote_versions WHERE quote_id = :q AND version_no = 1"), {"q": q["id"]}
        ).scalar()
    assert key
    get_storage().delete(key)
    assert not get_storage().exists(key)
    again = sales.get(f"/quotes/{q['id']}/versions/1/pdf")
    assert again.status_code == 200
    assert hashlib.sha256(again.content).hexdigest() == v1["pdf_sha256"]
    assert again.content == first
    extracted = _pdf_text(again.content)
    assert "Changed After Approval" not in extracted and "Changed Customer" not in extracted
    assert "Alpha Trading Customer" in extracted


def test_h1_frozen_expiry_blocks_v1_while_a_later_approved_version_exports(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """Expiry is judged per version on the frozen validity: v1 expired (snapshot) → 409 for PDF (GET/POST) and CSV
    for every role; v2 approved later with a fresh validity exports fine; header edits never revive v1."""
    sales, approver, q = _approved(app, tenant_a)
    nv = sales.post(f"/quotes/{q['id']}/versions", json={"expected_lock_version": q["lock_version"]}).json()
    assert nv["current_version_no"] == 2
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": nv["lock_version"]}).json()
    a = approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": s["lock_version"]})
    assert a.status_code == 200, a.text
    _freeze_snapshot_validity(tenant_a.org_id, q["id"], 1, date.today() - timedelta(days=1))
    for role in ("sales", "approver", "readonly", "owner"):
        c = login_as(app, tenant_a.users[role])
        assert c.get(f"/quotes/{q['id']}/versions/1/pdf").status_code == 409, role
        assert c.post(f"/quotes/{q['id']}/versions/1/pdf").status_code == 409, role
        assert c.get(f"/quotes/{q['id']}/versions/1/csv").status_code == 409, role
        assert c.get(f"/quotes/{q['id']}/versions/2/pdf").status_code == 200, role
        assert c.get(f"/quotes/{q['id']}/versions/2/csv").status_code == 200, role
    # header is still valid, so the quote itself is not "expired" — only the old document is
    assert sales.get(f"/quotes/{q['id']}").json()["effective_status"] == "approved"
    # a draft v3 with a far-future header validity still does not revive v1
    nv3 = sales.post(f"/quotes/{q['id']}/versions", json={"expected_lock_version": a.json()["lock_version"]}).json()
    assert (
        sales.patch(
            f"/quotes/{q['id']}", json={"expected_lock_version": nv3["lock_version"], "valid_until": "2099-12-31"}
        ).status_code
        == 200
    )
    assert sales.get(f"/quotes/{q['id']}/versions/1/pdf").status_code == 409
    assert sales.get(f"/quotes/{q['id']}/versions/2/pdf").status_code == 200


def test_h1_approval_freezes_validity_so_a_later_header_expiry_cannot_be_undone_by_sales(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """Reviewer repro (D1 variant): approved v1, header validity passes (DB), Sales opens v2 and sets 2099 → v1 must
    stay 409 because its own frozen validity has passed; the quote header reads draft again."""
    from sqlalchemy import update

    from app.models.quote import Quote

    sales, _approver, q = _approved(app, tenant_a)
    yesterday = date.today() - timedelta(days=1)
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        db.execute(update(Quote).where(Quote.id == uuid.UUID(q["id"])).values(valid_until=yesterday))
        db.commit()
    _freeze_snapshot_validity(tenant_a.org_id, q["id"], 1, yesterday)  # time passed for the document as well
    assert sales.get(f"/quotes/{q['id']}").json()["effective_status"] == "expired"
    assert sales.get(f"/quotes/{q['id']}/versions/1/pdf").status_code == 409
    nv = sales.post(f"/quotes/{q['id']}/versions", json={"expected_lock_version": q["lock_version"]}).json()
    assert (
        sales.patch(
            f"/quotes/{q['id']}", json={"expected_lock_version": nv["lock_version"], "valid_until": "2099-12-31"}
        ).status_code
        == 200
    )
    assert sales.get(f"/quotes/{q['id']}").json()["effective_status"] == "draft"
    assert sales.get(f"/quotes/{q['id']}/versions/1/pdf").status_code == 409
    assert sales.get(f"/quotes/{q['id']}/versions/1/csv").status_code == 409


# ---------------------------------------------------------------------------------------------------------------
# M-4 — manual prices need owner/admin confirmation (S1-A03 "privileged", S1-F12)
# ---------------------------------------------------------------------------------------------------------------


def test_m4_typed_over_catalog_price_full_lifecycle(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """Catalog line, Sales types a *higher* price (no margin problem): blocker `manual_price_unconfirmed` only;
    sales/approver/readonly cannot clear it; a short reason is refused; owner clears it; a later price edit by Sales
    re-raises it (override invalidated); re-selecting the catalog product restores the catalog price and needs no
    confirmation."""
    sales = login_as(app, tenant_a.users["sales"])
    approver = login_as(app, tenant_a.users["approver"])
    readonly = login_as(app, tenant_a.users["readonly"])
    owner = login_as(app, tenant_a.users["owner"])
    q = make_quote(sales, str(tenant_a.customer_id), "10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n")
    ln = q["current_version"]["lines"][0]
    assert ln["price_source"] == "catalog" and q["blockers"] == []
    q = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [{"id": ln["id"], "unit_price": "5.00"}]},
    ).json()
    assert q["current_version"]["lines"][0]["price_source"] == "manual"
    assert [b["code"] for b in q["blockers"]] == ["manual_price_unconfirmed"]
    assert q["blockers"][0]["line_no"] == 1 and set(q["blockers"][0]["overridable_by"]) == {"owner", "admin"}
    blocked = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]})
    assert blocked.status_code == 409 and blocked.json()["error"] == "blocked"
    assert [b["code"] for b in blocked.json()["blockers"]] == ["manual_price_unconfirmed"]
    path = f"/quotes/{q['id']}/lines/{ln['id']}/price-override"
    body = {"expected_lock_version": q["lock_version"], "reason": "agreed with the customer by phone"}
    assert sales.post(path, json=body).status_code == 403
    assert approver.post(path, json=body).status_code == 403  # approvers alone cannot clear it (policy)
    assert readonly.post(path, json=body).status_code == 403
    assert owner.post(path, json={**body, "reason": "ok"}).status_code == 422  # reason ≥ 5 chars
    assert owner.post(path, json={"expected_lock_version": q["lock_version"]}).status_code == 422
    assert sales.get(f"/quotes/{q['id']}").json()["blockers"][0]["code"] == "manual_price_unconfirmed"
    r = owner.post(path, json=body)
    assert r.status_code == 200 and r.json()["blockers"] == []
    assert r.json()["current_version"]["lines"][0]["margin_override_reason"] == body["reason"]
    # Sales changes the price again → confirmation is void, blocker is back
    q = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": r.json()["lock_version"], "lines": [{"id": ln["id"], "unit_price": "5.50"}]},
    ).json()
    assert q["current_version"]["lines"][0]["margin_override_reason"] is None
    assert [b["code"] for b in q["blockers"]] == ["manual_price_unconfirmed"]
    assert sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).status_code == 409
    # a save that re-sends the same price (editor behaviour) must not count as a new manual entry after confirmation
    r = owner.post(path, json={"expected_lock_version": q["lock_version"], "reason": "confirmed second time"})
    assert r.status_code == 200
    q = sales.patch(
        f"/quotes/{q['id']}",
        json={
            "expected_lock_version": r.json()["lock_version"],
            "notes": "touch",
            "lines": [{"id": ln["id"], "unit_price": "5.50", "discount_pct": "0"}],
        },
    ).json()
    assert q["blockers"] == [] and q["current_version"]["lines"][0]["margin_override_reason"] == "confirmed second time"
    # re-selecting the catalog product restores the catalog price: no manual price, nothing to confirm
    q = sales.patch(
        f"/quotes/{q['id']}",
        json={
            "expected_lock_version": q["lock_version"],
            "lines": [{"id": ln["id"], "product_id": ln["product_id"], "unit_price": None}],
        },
    ).json()
    line = q["current_version"]["lines"][0]
    assert line["price_source"] == "catalog" and line["unit_price"] == "0.85" and q["blockers"] == []
    assert sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).status_code == 200


def test_m4_margin_override_alias_and_price_override_are_the_same_privileged_action(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    admin = login_as(app, tenant_a.users["admin"])
    approver = login_as(app, tenant_a.users["approver"])
    q = make_quote(sales, str(tenant_a.customer_id), "10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n")
    ln = q["current_version"]["lines"][0]
    q = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [{"id": ln["id"], "unit_price": "0.01"}]},
    ).json()
    assert {b["code"] for b in q["blockers"]} == {"manual_price_unconfirmed", "below_min_margin"}
    alias = f"/quotes/{q['id']}/lines/{ln['id']}/margin-override"
    assert (
        approver.post(alias, json={"expected_lock_version": q["lock_version"], "reason": "fine by me"}).status_code
        == 403
    )
    r = admin.post(alias, json={"expected_lock_version": q["lock_version"], "reason": "clearance stock"})
    assert r.status_code == 200 and r.json()["blockers"] == []
    # exactly one audit row, naming the line, with the reason — visible in the quote history
    hist = sales.get(f"/quotes/{q['id']}/history").json()
    rows = [e for e in hist["audit"] if e["action"] == "quote.margin_override"]
    assert len(rows) == 1 and rows[0]["summary"]["reason"] == "clearance stock" and rows[0]["summary"]["line_no"] == 1


def test_m4_new_version_copies_manual_price_but_not_its_confirmation(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """A manual price confirmed on v1 is copied to v2 as a manual price; the confirmation (audited against the v1 line)
    is not carried over, so v2 needs a fresh owner/admin confirmation before submission."""
    sales = login_as(app, tenant_a.users["sales"])
    owner = login_as(app, tenant_a.users["owner"])
    approver = login_as(app, tenant_a.users["approver"])
    q = make_quote(sales, str(tenant_a.customer_id), "10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n")
    ln = q["current_version"]["lines"][0]
    q = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [{"id": ln["id"], "unit_price": "5.00"}]},
    ).json()
    r = owner.post(
        f"/quotes/{q['id']}/lines/{ln['id']}/price-override",
        json={"expected_lock_version": q["lock_version"], "reason": "negotiated frame price"},
    )
    assert r.status_code == 200
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": r.json()["lock_version"]}).json()
    a = approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": s["lock_version"]}).json()
    nv = sales.post(f"/quotes/{q['id']}/versions", json={"expected_lock_version": a["lock_version"]}).json()
    line2 = nv["current_version"]["lines"][0]
    assert line2["price_source"] == "manual" and line2["unit_price"] == "5"
    assert line2["margin_override_reason"] is None
    assert [b["code"] for b in nv["blockers"]] == ["manual_price_unconfirmed"]
    assert (
        sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": nv["lock_version"]}).status_code == 409
    )


def test_m4_generated_custom_line_needs_price_then_confirmation_in_browser_order(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """RFQ review path: unknown item marked custom → generated line has no price (`missing_price`), Sales enters one
    (`manual_price_unconfirmed`), approver cannot clear it, admin can; both blockers never coexist on that line."""
    sales = login_as(app, tenant_a.users["sales"])
    admin = login_as(app, tenant_a.users["admin"])
    approver = login_as(app, tenant_a.users["approver"])
    rfq = sales.post(
        "/rfqs", json={"text": "3 pcs SKU-9999 Unknown widget\n", "customer_id": str(tenant_a.customer_id)}
    ).json()
    item = rfq["items"][0]
    assert sales.patch(f"/rfqs/{rfq['id']}/items/{item['id']}", json={"is_custom": True}).status_code == 200
    q = sales.post(f"/rfqs/{rfq['id']}/generate-quote")
    assert q.status_code == 201, q.text
    q = q.json()
    assert [b["code"] for b in q["blockers"]] == ["missing_price"]
    ln = q["current_version"]["lines"][0]
    q = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [{"id": ln["id"], "unit_price": "42"}]},
    ).json()
    assert [b["code"] for b in q["blockers"]] == ["manual_price_unconfirmed"]
    body = {"expected_lock_version": q["lock_version"], "reason": "supplier quotation attached"}
    assert approver.post(f"/quotes/{q['id']}/lines/{ln['id']}/price-override", json=body).status_code == 403
    r = admin.post(f"/quotes/{q['id']}/lines/{ln['id']}/price-override", json=body)
    assert r.status_code == 200 and r.json()["blockers"] == []


# ---------------------------------------------------------------------------------------------------------------
# M-3 — blocker text is role-safe (S1-F16 cost confidentiality)
# ---------------------------------------------------------------------------------------------------------------


def _below_margin(app, tenant: Tenant):  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant.users["sales"])
    q = make_quote(sales, str(tenant.customer_id), "10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n")
    ln = q["current_version"]["lines"][0]
    q = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [{"id": ln["id"], "discount_pct": "40"}]},
    ).json()
    assert [b["code"] for b in q["blockers"]] == ["below_min_margin"]
    return sales, q


def test_m3_blocker_text_hides_margin_and_floor_from_sales_and_readonly_on_every_surface(app) -> None:  # type: ignore[no-untyped-def]
    """A distinctive floor (37.5 %) so the assertion cannot pass by accident; checked on GET, on the submit-409
    payload and on the history endpoint for Sales and Read-only; privileged roles still see the figures."""
    t = make_tenant("Gamma Floor", min_margin="37.5")
    sales, q = _below_margin(app, t)
    readonly = login_as(app, t.users["readonly"])
    sales_get = sales.get(f"/quotes/{q['id']}").json()["blockers"]
    assert sales_get and not PCT_FIGURE.search(" ".join(b["message"] for b in sales_get))
    assert "37.5" not in " ".join(b["message"] for b in sales_get)
    assert "margin" in sales_get[0]["message"].lower()  # still actionable
    ro_get = readonly.get(f"/quotes/{q['id']}").json()["blockers"]
    assert ro_get and not PCT_FIGURE.search(" ".join(b["message"] for b in ro_get)) and "37.5" not in str(ro_get)
    blocked = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]})
    assert blocked.status_code == 409
    assert not PCT_FIGURE.search(" ".join(b["message"] for b in blocked.json()["blockers"]))
    assert "37.5" not in blocked.text
    assert (
        readonly.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).status_code == 403
    )
    # lines for these roles carry no cost or margin fields
    for payload in (sales.get(f"/quotes/{q['id']}").text, readonly.get(f"/quotes/{q['id']}").text):
        assert '"cost_price"' not in payload and '"margin_pct"' not in payload
    # privileged roles get the figures (actionable override)
    for role in ("owner", "admin", "approver"):
        priv = login_as(app, t.users[role]).get(f"/quotes/{q['id']}").json()["blockers"]
        msg = " ".join(b["message"] for b in priv)
        assert "37.5" in msg and PCT_FIGURE.search(msg), (role, msg)


def test_m3_confidential_floor_is_not_in_the_quote_payload_for_sales_and_readonly(app) -> None:  # type: ignore[no-untyped-def]
    """M-3 is about the floor being confidential (GET /org hides `min_margin_pct` from Sales/Read-only). The same
    value must not be readable from any other field of the quote representation handed to those roles
    (e.g. `versions[].pricing_snapshot`), otherwise sanitising the blocker message achieves nothing."""
    t = make_tenant("Delta Floor", min_margin="37.5")
    sales, q = _below_margin(app, t)
    readonly = login_as(app, t.users["readonly"])
    assert "min_margin_pct" not in sales.get("/org").json()  # the floor is confidential by design
    for role, c in (("sales", sales), ("readonly", readonly)):
        for path in (f"/quotes/{q['id']}", f"/quotes/{q['id']}/history"):
            body = c.get(path).text
            assert "37.5" not in body, (role, path, "confidential floor present in payload")
            assert "min_margin_pct" not in body, (role, path)


def test_m3_generate_quote_409_from_rfq_does_not_reveal_margin_or_cost(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    rfq = sales.post("/rfqs", json={"text": "10 pcs SKU-9999 nothing\n"}).json()
    r = sales.post(f"/rfqs/{rfq['id']}/generate-quote")
    assert r.status_code == 409 and "cost" not in r.text.lower() and "margin" not in r.text.lower()


# ---------------------------------------------------------------------------------------------------------------
# M-1 — login limiter: normalised key and per-account bucket
# ---------------------------------------------------------------------------------------------------------------


def test_m1_per_account_bucket_catches_distributed_guessing_with_whitespace_and_case_variants(
    client, tenant_a: Tenant
) -> None:  # type: ignore[no-untyped-def]
    """The TestClient always presents the same source address, so distinct IPs are simulated by resetting the per-IP
    limiter after every attempt: only the per-account bucket accumulates. Variants of the same address (whitespace,
    tabs, case) must share one bucket; the bucket is 5× the per-IP budget; another account is unaffected."""
    from app.core.config import get_settings

    per_ip = get_settings().login_rate_limit_attempts
    budget = account_limiter.attempts
    assert budget == per_ip * 5
    email = tenant_a.users["sales"]
    variants = [email, f"  {email}", f"{email}\t", email.upper(), f" {email.title()} ", f"\t{email.upper()}\t"]

    def attempt(addr: str, password: str) -> int:
        pre = client.get(f"{API}/auth/csrf").json()["csrf_token"]
        code = client.post(
            f"{API}/auth/login", json={"email": addr, "password": password}, headers={"X-CSRF-Token": pre}
        ).status_code
        login_limiter.reset()  # next attempt "comes from another IP"
        return code

    for i in range(budget):
        assert attempt(variants[i % len(variants)], "wrong-password-xyz") == 401, i
    assert attempt(variants[1], "wrong-password-xyz") == 429
    assert attempt(email, PASSWORD) == 429  # even the right password is held back while the account is throttled
    # a different account on the same tenant is not affected
    assert attempt(tenant_a.users["approver"], "wrong-password-xyz") == 401
    assert attempt(tenant_a.users["approver"], PASSWORD) == 200
    # and the per-account budget is counted once per attempt, not once per variant
    hits = sum(len(q) for k, q in account_limiter._hits.items() if k.startswith("account|"))  # noqa: SLF001
    assert hits == budget + 1  # the 429 attempt itself is not recorded as a hit


def test_m1_per_ip_bucket_is_shared_by_whitespace_and_case_variants(client, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    from app.core.config import get_settings

    per_ip = get_settings().login_rate_limit_attempts
    email = tenant_a.users["readonly"]
    codes = []
    for i in range(per_ip + 1):
        addr = [f" {email}", email.upper(), f"{email}\t"][i % 3]
        pre = client.get(f"{API}/auth/csrf").json()["csrf_token"]
        codes.append(
            client.post(
                f"{API}/auth/login", json={"email": addr, "password": "bad-bad-bad"}, headers={"X-CSRF-Token": pre}
            ).status_code
        )
    assert codes == [401] * per_ip + [429], codes


# ---------------------------------------------------------------------------------------------------------------
# L-4 — totals cap boundary and atomicity
# ---------------------------------------------------------------------------------------------------------------


def test_l4_total_cap_boundary_is_exact_and_a_refused_save_changes_nothing(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    q = sales.post("/quotes", json={"customer_id": str(tenant_a.customer_id)}).json()
    big = {"description": "big", "quantity": "9999999", "unit_price": "9999999", "is_custom": True, "tax_rate_pct": "0"}
    r = sales.patch(f"/quotes/{q['id']}", json={"expected_lock_version": q["lock_version"], "lines": [big]})
    assert r.status_code == 200, r.text  # 99 999 980 000 001 < 1e14 is representable
    assert Decimal(r.json()["current_version"]["grand_total"]) == Decimal("99999980000001")
    q = r.json()
    ln = q["current_version"]["lines"][0]
    r = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [{"id": ln["id"]}, {**big, "description": "two"}]},
    )
    assert r.status_code == 422 and "maximum" in r.json()["detail"]
    after = sales.get(f"/quotes/{q['id']}").json()
    assert after["lock_version"] == q["lock_version"]
    assert len(after["current_version"]["lines"]) == 1
    assert Decimal(after["current_version"]["grand_total"]) == Decimal("99999980000001")
    # exactly at the cap is refused too (>=)
    r = sales.patch(
        f"/quotes/{q['id']}",
        json={
            "expected_lock_version": q["lock_version"],
            "lines": [{"id": ln["id"], "quantity": "10000000", "unit_price": "10000000"}],
        },
    )
    assert r.status_code == 422, r.text


def _two_line_quote(app, tenant: Tenant):  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    from tests.conftest import AuthedClient

    # do not re-raise server exceptions: an unhandled error must surface as the HTTP status the user would see
    sales = AuthedClient(
        TestClient(app, base_url="http://testserver", raise_server_exceptions=False), tenant.users["sales"]
    )
    q = make_quote(
        sales,
        str(tenant.customer_id),
        "10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n5 pcs SKU-1003 Hex nut M8 zinc plated\n",
    )
    assert len(q["current_version"]["lines"]) == 2
    return sales, q


def test_editor_remove_of_a_non_last_line_saves_without_server_error(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """The editor's "Remove" button re-sends the remaining lines (S1-F11): removing line 1 of 2 renumbers line 2 to
    line_no 1 while line 1 is deleted in the same save. Must be 200 with one line, never a 500."""
    sales, q = _two_line_quote(app, tenant_a)
    keep = q["current_version"]["lines"][1]
    r = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [{"id": keep["id"]}]},
    )
    assert r.status_code == 200, (r.status_code, r.text[:300])
    lines = r.json()["current_version"]["lines"]
    assert [ln["sku"] for ln in lines] == ["SKU-1003"] and lines[0]["line_no"] == 1


def test_replacing_a_line_with_a_new_one_in_a_single_save_is_not_a_server_error(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """API contract: `lines` is the full desired list; an entry without `id` is a new line. Dropping line 2 and adding
    a new custom line at the same position in one PATCH must not raise a unique-constraint error (500)."""
    sales, q = _two_line_quote(app, tenant_a)
    first = q["current_version"]["lines"][0]
    r = sales.patch(
        f"/quotes/{q['id']}",
        json={
            "expected_lock_version": q["lock_version"],
            "lines": [
                {"id": first["id"]},
                {"description": "Replacement custom item", "quantity": "1", "unit_price": "3", "is_custom": True},
            ],
        },
    )
    assert r.status_code == 200, (r.status_code, r.text[:300])
    assert [ln["description"] for ln in r.json()["current_version"]["lines"]][1] == "Replacement custom item"


# ---------------------------------------------------------------------------------------------------------------
# L-5 — oversize documents are rejected, never truncated (S1-A08)
# ---------------------------------------------------------------------------------------------------------------


def _pdf_with_pages(n: int) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for p in range(n):
        c.setFont("Helvetica", 11)
        c.drawString(72, 760, f"{p + 1} pcs SKU-1001 Hex bolt M8x40 zinc plated")
        c.showPage()
    c.save()
    return buf.getvalue()


def _xlsx_with_rows(n: int) -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Item", "Qty"])
    for i in range(n):
        ws.append([f"SKU-1001 Hex bolt M8x40 zinc plated {i}", 1])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _patch_limits(monkeypatch: pytest.MonkeyPatch, **overrides: int) -> None:
    """Same technique as the implementer's L-5 test: the parser subprocess receives the caps as arguments, so
    overriding the settings seen by the sandbox, the RFQ service and the catalog importer is enough."""
    from app.core import config

    inst = config.Settings(**overrides)  # init kwargs take precedence over environment values
    monkeypatch.setattr("app.services.parsing.sandbox.get_settings", lambda: inst)
    monkeypatch.setattr("app.services.rfqs.get_settings", lambda: inst)
    monkeypatch.setattr("app.services.catalog_import.get_settings", lambda: inst)


def test_l5_pdf_with_more_pages_than_the_cap_fails_cleanly_without_partial_lines(
    app, tenant_a: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    _patch_limits(monkeypatch, max_pdf_pages=2)
    sales = login_as(app, tenant_a.users["sales"])
    r = upload(sales, "/rfqs", "long.pdf", _pdf_with_pages(3), content_type="application/pdf")
    assert r.status_code == 201, r.text
    rfq = r.json()
    assert rfq["status"] == "failed"
    assert rfq["failure_reason"].startswith("too_large") and "2 pages" in rfq["failure_reason"]
    assert rfq["items"] == []  # S1-A17: no partial lines persisted
    assert sales.post(f"/rfqs/{rfq['id']}/generate-quote").status_code == 409
    # a retry of the same document is still refused and still leaves no lines
    again = sales.post(f"/rfqs/{rfq['id']}/reparse")
    assert again.status_code == 200 and again.json()["status"] == "failed" and again.json()["items"] == []
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        rows = db.scalars(select(AuditEvent).where(AuditEvent.action == "rfq.parse_failed")).all()
        assert len(rows) == 2 and all(r.summary.get("reason") == "too_large" for r in rows)
    # boundary: exactly the cap is accepted and yields a line per page
    ok = upload(sales, "/rfqs", "ok.pdf", _pdf_with_pages(2), content_type="application/pdf").json()
    assert ok["status"] == "parsed", ok.get("failure_reason")
    assert len(ok["items"]) == 2 and {i["source_ref"]["page"] for i in ok["items"]} == {1, 2}


def test_l5_pdf_page_cap_default_is_enforced_without_monkeypatching(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """Real default (50 pages): a 51-page PDF is refused with the limits stated; a 50-page one parses."""
    sales = login_as(app, tenant_a.users["sales"])
    r = upload(sales, "/rfqs", "fifty_one.pdf", _pdf_with_pages(51), content_type="application/pdf").json()
    assert r["status"] == "failed" and "too_large" in r["failure_reason"] and "50 pages" in r["failure_reason"]
    assert r["items"] == []
    ok = upload(sales, "/rfqs", "fifty.pdf", _pdf_with_pages(50), content_type="application/pdf").json()
    assert ok["status"] == "parsed" and len(ok["items"]) == 50


def test_l5_xlsx_with_more_rows_than_the_cap_is_rejected_not_truncated(
    app, tenant_a: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    _patch_limits(monkeypatch, max_rfq_rows=5)
    sales = login_as(app, tenant_a.users["sales"])
    r = upload(sales, "/rfqs", "rows.xlsx", _xlsx_with_rows(20))
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "failed" and "too_large" in r.json()["failure_reason"] and r.json()["items"] == []
    # RFQ spreadsheets are capped on the number of extracted items (rfqs.parse_rfq); the sandbox itself receives the
    # catalog row cap for csv/xlsx (sandbox.run_parser), so the boundary is 5 items ok / 6 items refused
    ok = upload(sales, "/rfqs", "rows_ok.xlsx", _xlsx_with_rows(5)).json()
    assert ok["status"] == "parsed", ok.get("failure_reason")
    assert len(ok["items"]) == 5
    edge = upload(sales, "/rfqs", "rows_edge.xlsx", _xlsx_with_rows(6)).json()
    assert edge["status"] == "failed" and "too_large" in edge["failure_reason"] and edge["items"] == []


# ---------------------------------------------------------------------------------------------------------------
# L-10 — weak SECRET_KEY guard
# ---------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "key", "ok"),
    [
        ("production", "a" * 64, False),  # low entropy
        ("production", "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghijklmnopqrstuvwxyz+/", True),
        (
            "production",
            "QuotePilot-CHANGE-ME-" + "x7Kp2mQ9vL4nR8sT1wY5zB3cD6fG0hJ" * 2,
            False,
        ),  # marker inside strong key
        ("staging", "e2e-only-secret-key-e2e-only-secret-key-e2e", False),
        ("development", "dev-only-change-me-dev-only-change-me", True),  # dev may keep the placeholder
        ("test", "test-secret-key-test-secret-key-test-secret-key", True),
    ],
)
def test_l10_secret_key_guard_matrix(monkeypatch: pytest.MonkeyPatch, env: str, key: str, ok: bool) -> None:
    from app.core import config

    monkeypatch.setenv("APP_ENV", env)
    monkeypatch.setenv("SECRET_KEY", key)
    config.get_settings.cache_clear()
    try:
        if ok:
            assert config.get_settings().secret_key == key
        else:
            with pytest.raises(RuntimeError, match="SECRET_KEY"):
                config.get_settings()
    finally:
        config.get_settings.cache_clear()


# ---------------------------------------------------------------------------------------------------------------
# L-3 / D5 / O1 — owner guards
# ---------------------------------------------------------------------------------------------------------------


def test_l3_admin_may_not_deactivate_or_demote_any_owner_even_when_another_owner_exists(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    owner = login_as(app, tenant_a.users["owner"])
    admin = login_as(app, tenant_a.users["admin"])
    r = owner.post(
        "/org/members",
        json={
            "email": "owner2@alphatrading.example",
            "full_name": "Second Owner",
            "role": "owner",
            "password": PASSWORD,
        },
    )
    assert r.status_code == 201
    owner2_mid = r.json()["id"]
    owner1_mid = next(
        m["id"] for m in owner.get("/org/members").json() if m["user"]["email"] == tenant_a.users["owner"]
    )
    for mid in (owner1_mid, owner2_mid):
        # NOTE: the product answers 422 validation_error ("Only an owner can …"), the contract says role denial → 403;
        # recorded as observation, the security property (no change) is what is asserted here
        r1 = admin.patch(f"/org/members/{mid}", json={"is_active": False})
        r2 = admin.patch(f"/org/members/{mid}", json={"role": "sales"})
        assert r1.status_code in (403, 422) and r2.status_code in (403, 422), (mid, r1.status_code, r2.status_code)
    members = owner.get("/org/members").json()
    assert sum(1 for m in members if m["role"] == "owner" and m["is_active"]) == 2
    assert all(m["role"] == "owner" for m in members if m["id"] in (owner1_mid, owner2_mid))
    # the owner can retire the second owner, but then nobody can retire the last one
    assert owner.patch(f"/org/members/{owner2_mid}", json={"is_active": False}).status_code == 200
    assert owner.patch(f"/org/members/{owner1_mid}", json={"role": "admin"}).status_code == 409
    assert owner.patch(f"/org/members/{owner1_mid}", json={"is_active": False}).status_code == 409


# ---------------------------------------------------------------------------------------------------------------
# X-05 — AI processing consent is owner-only, audited and not leaked to unprivileged roles
# ---------------------------------------------------------------------------------------------------------------


def test_x05_consent_role_matrix_audit_and_visibility(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    owner = login_as(app, tenant_a.users["owner"])
    for role, code in (("sales", 403), ("readonly", 403), ("approver", 403), ("admin", 403)):
        assert (
            login_as(app, tenant_a.users[role]).patch("/org", json={"ai_processing_consent": True}).status_code == code
        ), role
    assert owner.get("/org").json()["ai_processing_consented_at"] is None
    r = owner.patch("/org", json={"ai_processing_consent": True})
    assert r.status_code == 200 and r.json()["ai_processing_consented_at"]
    # sales (public view) does not see the consent field; admin (privileged) does
    sales_org = login_as(app, tenant_a.users["sales"]).get("/org").json()
    assert "ai_processing_consented_at" not in sales_org and "min_margin_pct" not in sales_org
    assert login_as(app, tenant_a.users["admin"]).get("/org").json()["ai_processing_consented_at"]
    # audited with before/after, actor = owner
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        rows = db.scalars(
            select(AuditEvent).where(AuditEvent.action == "org.update").order_by(AuditEvent.created_at.desc())
        ).all()
    assert rows and "ai_processing_consent" in rows[0].summary["fields"]
    assert rows[0].summary["after"]["ai_processing_consent"] is True and str(rows[0].actor_user_id) == owner.user_id
    # withdrawing consent clears the timestamp
    assert owner.patch("/org", json={"ai_processing_consent": False}).json()["ai_processing_consented_at"] is None


# ---------------------------------------------------------------------------------------------------------------
# D2 / D3 (re-check with the new snapshot model): withdrawn after approval, decisions on withdrawn
# ---------------------------------------------------------------------------------------------------------------


def test_withdrawn_approved_version_blocks_every_export_and_revives_only_via_new_version(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, approver, q = _approved(app, tenant_a)
    w = approver.post(
        f"/quotes/{q['id']}/withdraw", json={"expected_lock_version": q["lock_version"], "reason": "lost"}
    )
    assert w.status_code == 200 and w.json()["status"] == "withdrawn"
    for role in ("sales", "approver", "readonly", "owner"):
        c = login_as(app, tenant_a.users[role])
        assert c.get(f"/quotes/{q['id']}/versions/1/pdf").status_code == 409
        assert c.post(f"/quotes/{q['id']}/versions/1/pdf").status_code == 409
        assert c.get(f"/quotes/{q['id']}/versions/1/csv").status_code == 409
    lv = w.json()["lock_version"]
    assert approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": lv}).status_code == 409
    assert (
        approver.post(f"/quotes/{q['id']}/reject", json={"expected_lock_version": lv, "reason": "x"}).status_code == 409
    )
    assert sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": lv}).status_code == 409
    nv = sales.post(f"/quotes/{q['id']}/versions", json={"expected_lock_version": lv})
    assert nv.status_code == 201 and nv.json()["status"] == "draft" and nv.json()["current_version_no"] == 2
    # S1-F15: a withdrawn approved document must not become exportable as an APPROVED document again merely because
    # a replacement draft was opened (expiry is frozen per version by H-1; withdrawal must stick the same way)
    assert [d["decision"] for d in nv.json()["versions"][0]["decisions"]] == ["approved", "withdrawn"]
    assert sales.get(f"/quotes/{q['id']}/versions/1/pdf").status_code == 409
    assert sales.get(f"/quotes/{q['id']}/versions/1/csv").status_code == 409
