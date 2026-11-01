"""QA (independent): catalog browsing, import validation report, audit coverage and file access.

Derived from docs/PRD.md S1-F04, S1-F18, S1-F16 and acceptance IDs S1-A05, S1-A07, S1-A14, S1-A19.
Integration evidence (API + PostgreSQL + RLS).
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.session import get_sessionmaker, set_tenant
from app.models.audit import AuditEvent
from tests.conftest import API, FIXTURES, Tenant, login_as, make_tenant, upload
from tests.integration.test_quotes import make_quote

pytestmark = pytest.mark.integration

MAPPING = json.dumps(
    {"sku": "SKU", "description": "Description", "unit": "Unit", "currency": "Currency", "list_price": "List Price"}
)


# ---------------------------------------------------------------------------------------------------------------
# Products listing: pagination, search escaping, bounds
# ---------------------------------------------------------------------------------------------------------------


def test_products_pagination_and_search_escaping(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    base = f"/catalogs/versions/{tenant_a.catalog_version_id}/products"
    p1 = sales.get(f"{base}?page_size=2").json()
    assert (p1["total"], len(p1["items"]), p1["page"], p1["page_size"]) == (30, 2, 1, 2)
    p2 = sales.get(f"{base}?page_size=2&page=2").json()
    assert {i["id"] for i in p1["items"]}.isdisjoint({i["id"] for i in p2["items"]})
    assert [i["sku"] for i in p1["items"]] + [i["sku"] for i in p2["items"]] == [
        "SKU-1001",
        "SKU-1002",
        "SKU-1003",
        "SKU-1004",
    ]
    last = sales.get(f"{base}?page_size=2&page=15").json()
    assert len(last["items"]) == 2 and last["items"][-1]["sku"] == "SKU-9002"
    assert sales.get(f"{base}?page_size=2&page=16").json()["items"] == []
    # wildcard characters are literal, not LIKE operators
    assert sales.get(f"{base}?q=%25").json()["total"] == 0
    assert sales.get(f"{base}?q=%5F").json()["total"] == 0
    assert sales.get(f"{base}?q=SKU-100").json()["total"] == 5
    assert sales.get(f"{base}?q=hex%20bolt").json()["total"] == 3
    assert sales.get(f"{base}?q=%27%3B%20DROP%20TABLE%20products%3B--").json()["total"] == 0
    # bounds
    assert sales.get(f"{base}?page=0").status_code == 422
    assert sales.get(f"{base}?page_size=0").status_code == 422
    assert sales.get(f"{base}?page_size=201").status_code == 422
    assert sales.get(f"{base}?q=" + "a" * 101).status_code == 422
    assert sales.get(f"/catalogs/versions/{uuid.uuid4()}/products").status_code == 404
    assert sales.get("/catalogs/versions/not-a-uuid/products").status_code == 422


def test_products_cost_hidden_for_readonly_and_visible_for_owner(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    base = f"/catalogs/versions/{tenant_a.catalog_version_id}/products?q=SKU-1001"
    for role in ("sales", "readonly"):
        item = login_as(app, tenant_a.users[role]).get(base).json()["items"][0]
        assert "cost_price" not in item and item["list_price"] == "0.85", role
    for role in ("owner", "admin", "approver"):
        assert login_as(app, tenant_a.users[role]).get(base).json()["items"][0]["cost_price"] == "0.52", role


# ---------------------------------------------------------------------------------------------------------------
# S1-F04 — import validation report
# ---------------------------------------------------------------------------------------------------------------


def test_import_report_rejects_invalid_rows_with_row_numbers(app) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("Report Co", with_catalog=False)
    admin = login_as(app, t.users["admin"])
    csv = (
        b"SKU,Description,Unit,Currency,List Price\n"
        b"OK-1,Good row,EA,AED,1.50\n"
        b"NEG-1,Negative price,EA,AED,-2\n"
        b",Missing sku,EA,AED,3\n"
        b"BAD-1,Bad currency,EA,ZZZ,4\n"
        b"OK-2,Good row two,EA,AED,abc\n"
    )
    r = upload(admin, "/catalogs/import", "c.csv", csv, {"mapping": MAPPING})
    assert r.status_code == 201, r.text
    rep = r.json()["import_report"]
    assert rep["imported"] == 1 and rep["skipped"] == 4, rep
    assert len(rep["errors"]) == 4
    assert all(isinstance(e.get("row"), int) and e["row"] >= 2 for e in rep["errors"]), rep["errors"]
    prods = admin.get(f"/catalogs/versions/{r.json()['id']}/products").json()
    assert prods["total"] == 1 and prods["items"][0]["sku"] == "OK-1"


def test_import_with_no_valid_rows_is_rejected_and_nothing_stored(app) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("Empty Co", with_catalog=False)
    admin = login_as(app, t.users["admin"])
    r = upload(admin, "/catalogs/import", "c.csv", b"SKU,Description,Unit,Currency,List Price\nA,B,EA,AED,-1\n", {"mapping": MAPPING})
    assert r.status_code == 422
    assert admin.get("/catalogs").json() == [] or all(not c["versions"] for c in admin.get("/catalogs").json())


def test_activation_requires_admin_and_is_tenant_scoped(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    for role in ("sales", "approver", "readonly"):
        ac = login_as(app, tenant_a.users[role])
        assert ac.post(f"/catalogs/versions/{tenant_a.catalog_version_id}/activate").status_code == 403, role
    b_owner = login_as(app, tenant_b.users["owner"])
    assert b_owner.post(f"/catalogs/versions/{tenant_a.catalog_version_id}/activate").status_code == 404
    assert b_owner.get(f"/catalogs/versions/{tenant_a.catalog_version_id}/products").status_code == 404
    assert [c["id"] for c in b_owner.get("/catalogs").json()] != [c["id"] for c in login_as(app, tenant_a.users["owner"]).get("/catalogs").json()]


# ---------------------------------------------------------------------------------------------------------------
# S1-A05 / S1-A14 — original file access
# ---------------------------------------------------------------------------------------------------------------


def test_original_file_access_matrix(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    content = (FIXTURES / "rfq_clean.csv").read_bytes()
    rfq = upload(sales, "/rfqs", "rfq_clean.csv", content, content_type="text/csv").json()
    fid = rfq["source_file_id"]
    for role in ("readonly", "approver", "owner"):
        r = login_as(app, tenant_a.users[role]).get(f"/files/{fid}")
        assert r.status_code == 200 and r.content == content, role
        assert r.headers["content-disposition"].startswith("attachment;")
        assert r.headers["X-Content-Type-Options"] == "nosniff"
    for role in ("owner", "sales", "readonly"):
        assert login_as(app, tenant_b.users[role]).get(f"/files/{fid}").status_code == 404, role
    anon = TestClient(app, base_url="http://testserver")
    assert anon.get(f"{API}/files/{fid}").status_code == 401
    assert sales.get(f"/files/{uuid.uuid4()}").status_code == 404


def test_rejected_upload_stores_no_file_row(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    for name in ("not_supported.docx", "binary.exe", "macro.xlsm"):
        assert upload(sales, "/rfqs", name, (FIXTURES / name).read_bytes()).status_code == 415, name
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    r = upload(sales, "/rfqs", "scan.png", png, content_type="image/png")
    assert r.status_code == 415 and r.json()["error"] == "unsupported_media_type" and r.json()["detail"]
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        from app.models.files import UploadedFile

        assert db.scalar(select(UploadedFile).where(UploadedFile.kind == "rfq")) is None
    assert sales.get("/rfqs").json() == []


# ---------------------------------------------------------------------------------------------------------------
# S1-A19 / S1-F18 — audit row for every mutation
# ---------------------------------------------------------------------------------------------------------------


def _audit_rows(org_id: uuid.UUID) -> list[AuditEvent]:
    with get_sessionmaker()() as db:
        set_tenant(db, org_id, None)
        rows = db.scalars(select(AuditEvent).where(AuditEvent.organization_id == org_id).order_by(AuditEvent.id)).all()
        db.expunge_all()
        return list(rows)


def test_every_mutation_writes_an_audit_row(app) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("Audit Co", min_margin="12")
    owner = login_as(app, t.users["owner"])
    admin = login_as(app, t.users["admin"])
    sales = login_as(app, t.users["sales"])
    approver = login_as(app, t.users["approver"])
    expected: dict[str, str] = {}  # action → actor user id

    c = sales.post("/customers", json={"name": "Audit Buyer"}).json()
    expected["customer.create"] = sales.user_id
    assert sales.patch(f"/customers/{c['id']}", json={"name": "Audit Buyer Ltd"}).status_code == 200
    expected["customer.update"] = sales.user_id
    assert admin.patch("/org", json={"pdf_footer": "audited"}).status_code == 200
    expected["org.update"] = admin.user_id
    m = admin.post(
        "/org/members",
        json={"email": "new@auditco.example", "full_name": "New", "role": "sales", "password": "Another-Long-Pass-77"},
    ).json()
    expected["member.create"] = admin.user_id
    assert owner.patch(f"/org/members/{m['id']}", json={"role": "readonly"}).status_code == 200
    expected["member.update"] = owner.user_id
    v = upload(
        admin,
        "/catalogs/import",
        "c.csv",
        b"SKU,Description,Unit,Currency,List Price\nX-1,Extra,EA,AED,1\n",
        {"mapping": MAPPING, "catalog_name": "Main"},
    ).json()
    expected["catalog.import"] = admin.user_id
    assert v["version_no"] == 2
    assert admin.post(f"/catalogs/versions/{v['id']}/activate").status_code == 200
    expected["catalog.activate"] = admin.user_id
    # back to version 1 of the main catalog for pricing
    assert admin.post(f"/catalogs/versions/{t.catalog_version_id}/activate").status_code == 200

    rfq = sales.post("/rfqs", json={"text": "10 pcs SKU-1001 Hex bolt\n3 pcs SKU-9999 widget\n"}).json()
    expected["rfq.create"] = sales.user_id
    unknown = next(i for i in rfq["items"] if "SKU-9999" in i["raw_text"])
    assert sales.patch(f"/rfqs/{rfq['id']}/items/{rfq['items'][0]['id']}", json={"quantity": "11"}).status_code == 200
    expected["rfq.item_update"] = sales.user_id
    assert sales.post(f"/rfqs/{rfq['id']}/items", json={"description": "Nut", "quantity": "2", "sku_hint": "SKU-1003"}).status_code == 201
    expected["rfq.item_create"] = sales.user_id
    assert sales.delete(f"/rfqs/{rfq['id']}/items/{unknown['id']}").status_code == 204
    expected["rfq.item_delete"] = sales.user_id
    assert sales.post(f"/rfqs/{rfq['id']}/reparse").status_code == 200
    expected["rfq.reparse"] = sales.user_id
    assert sales.delete(f"/rfqs/{rfq['id']}/items/{next(i for i in sales.get(f'/rfqs/{rfq['id']}').json()['items'] if 'SKU-9999' in i['raw_text'])['id']}").status_code == 204
    q = sales.post(f"/rfqs/{rfq['id']}/generate-quote").json()
    expected["quote.create"] = sales.user_id
    ln = q["current_version"]["lines"][0]
    q = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [{"id": ln["id"], "unit_price": "0.55"}]},
    ).json()
    expected["quote.update"] = sales.user_id
    q = admin.post(
        f"/quotes/{q['id']}/lines/{ln['id']}/margin-override",
        json={"expected_lock_version": q["lock_version"], "reason": "strategic account"},
    ).json()
    expected["quote.margin_override"] = admin.user_id
    q = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).json()
    expected["quote.submit"] = sales.user_id
    q = approver.post(f"/quotes/{q['id']}/reject", json={"expected_lock_version": q["lock_version"], "reason": "no"}).json()
    expected["quote.reject"] = approver.user_id
    q = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).json()
    q = approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": q["lock_version"]}).json()
    expected["quote.approve"] = approver.user_id
    assert sales.get(f"/quotes/{q['id']}/versions/1/pdf").status_code == 200
    expected["quote.export_pdf"] = sales.user_id
    assert sales.get(f"/quotes/{q['id']}/versions/1/csv").status_code == 200
    expected["quote.export_csv"] = sales.user_id
    q = sales.post(f"/quotes/{q['id']}/versions", json={"expected_lock_version": q["lock_version"]}).json()
    expected["quote.new_version"] = sales.user_id
    q2 = make_quote(sales, str(t.customer_id), "1 pcs SKU-1003 Hex nut\n")
    q2 = sales.post(f"/quotes/{q2['id']}/submit", json={"expected_lock_version": q2["lock_version"]}).json()
    assert approver.post(f"/quotes/{q2['id']}/withdraw", json={"expected_lock_version": q2["lock_version"], "reason": "x"}).status_code == 200
    expected["quote.withdraw"] = approver.user_id
    f = upload(sales, "/rfqs", "rfq_clean.csv", (FIXTURES / "rfq_clean.csv").read_bytes()).json()
    assert sales.get(f"/files/{f['source_file_id']}").status_code == 200
    expected["file.download"] = sales.user_id
    assert sales.post("/auth/logout").status_code == 200
    expected["auth.logout"] = sales.user_id

    rows = _audit_rows(t.org_id)
    by_action: dict[str, list[AuditEvent]] = {}
    for row in rows:
        by_action.setdefault(row.action, []).append(row)
    missing = [a for a in expected if a not in by_action]
    assert not missing, f"no audit rows for: {missing}"
    for action, actor in expected.items():
        ev = by_action[action][-1]
        assert str(ev.actor_user_id) == actor, (action, ev.actor_user_id, actor)
        assert ev.organization_id == t.org_id
        assert ev.entity_type and ev.entity_id, action
        assert ev.request_id and ev.created_at, action
    assert "auth.login" in by_action and len(by_action["auth.login"]) >= 4
    # the history endpoint exposes the quote-related subset to the tenant
    hist = approver.get(f"/quotes/{q['id']}/history").json()["audit"]
    assert {e["action"] for e in hist} >= {
        "quote.create",
        "quote.update",
        "quote.margin_override",
        "quote.submit",
        "quote.reject",
        "quote.approve",
        "quote.export_pdf",
        "quote.export_csv",
        "quote.new_version",
    }


def test_failed_login_audit_has_no_tenant_and_no_actor(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    client = TestClient(app, base_url="http://testserver")
    pre = client.get(f"{API}/auth/csrf").json()["csrf_token"]
    client.post(f"{API}/auth/login", json={"email": tenant_a.users["sales"], "password": "nope-nope-nope"}, headers={"X-CSRF-Token": pre})
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        rows = db.scalars(select(AuditEvent).where(AuditEvent.action == "auth.login_failed")).all()
        # visible rows (if any) never carry a tenant/actor and never the password
        for r in rows:
            assert r.actor_user_id is None and r.organization_id is None
            assert "nope" not in json.dumps(r.summary)
