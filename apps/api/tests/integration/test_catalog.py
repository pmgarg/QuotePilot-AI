import json

import pytest

from tests.conftest import FIXTURES, Tenant, login_as, make_tenant, upload

pytestmark = pytest.mark.integration

MAPPING = json.dumps(
    {"sku": "SKU", "description": "Description", "unit": "Unit", "currency": "Currency", "list_price": "List Price"}
)


def test_import_requires_admin(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    r = upload(
        sales,
        "/catalogs/import",
        "c.csv",
        b"SKU,Description,Unit,Currency,List Price\nA1,Thing,EA,AED,1\n",
        {"mapping": MAPPING},
    )
    assert r.status_code == 403


def test_import_versioning_and_activation(app) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("Gamma Imports", with_catalog=False)
    admin = login_as(app, t.users["admin"])
    r = upload(
        admin,
        "/catalogs/import",
        "c.csv",
        b"SKU,Description,Unit,Currency,List Price\nA1,Thing one,EA,AED,1.50\nA2,Thing two,box,AED,2\n",
        {"mapping": MAPPING, "catalog_name": "Main"},
    )
    assert r.status_code == 201, r.text
    v1 = r.json()
    assert v1["version_no"] == 1 and v1["row_count"] == 2 and v1["status"] == "imported"
    r2 = upload(
        admin,
        "/catalogs/import",
        "c2.csv",
        b"SKU,Description,Unit,Currency,List Price\nA1,Thing one,EA,AED,1.75\n",
        {"mapping": MAPPING, "catalog_name": "Main"},
    )
    v2 = r2.json()
    assert v2["version_no"] == 2
    assert admin.post(f"/catalogs/versions/{v1['id']}/activate").status_code == 200
    assert admin.post(f"/catalogs/versions/{v1['id']}/activate").status_code == 409
    assert admin.post(f"/catalogs/versions/{v2['id']}/activate").status_code == 200
    cats = admin.get("/catalogs").json()
    assert cats[0]["active_version_id"] == v2["id"]
    assert [v["status"] for v in cats[0]["versions"]] == ["archived", "active"]
    prods = admin.get(f"/catalogs/versions/{v2['id']}/products").json()
    assert prods["total"] == 1 and prods["items"][0]["list_price"] == "1.75"


def test_import_report_duplicates_formula_and_bad_values(app) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("Delta Imports", with_catalog=False)
    admin = login_as(app, t.users["admin"])
    r = upload(
        admin,
        "/catalogs/import",
        "catalog_formula_injection.csv",
        (FIXTURES / "catalog_formula_injection.csv").read_bytes(),
        {"mapping": MAPPING},
    )
    assert r.status_code == 201, r.text
    rep = r.json()["import_report"]
    assert rep["imported"] == 2 and rep["skipped"] == 2
    msgs = " | ".join(e["message"] for e in rep["errors"])
    assert "not a number" in msgs and "duplicate sku" in msgs
    assert any("formula" in w["message"] for w in rep["warnings"])
    prods = admin.get(f"/catalogs/versions/{r.json()['id']}/products").json()["items"]
    skus = {p["sku"] for p in prods}
    assert "=cmd|' /C calc'!A0" in skus  # stored as inert text and flagged
    assert any("formula_like" in p["flags"] for p in prods)


def test_import_semicolon_and_european_decimals(app) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("Epsilon GmbH", currency="EUR", with_catalog=False)
    admin = login_as(app, t.users["admin"])
    mapping = json.dumps({"sku": 0, "description": 1, "unit": 2, "currency": 3, "list_price": 4, "cost_price": 5})
    r = upload(
        admin, "/catalogs/import", "eu.csv", (FIXTURES / "catalog_semicolon_eu.csv").read_bytes(), {"mapping": mapping}
    )
    assert r.status_code == 201, r.text
    prods = {p["sku"]: p for p in admin.get(f"/catalogs/versions/{r.json()['id']}/products").json()["items"]}
    assert prods["EU-1"]["list_price"] == "1234.56" and prods["EU-1"]["cost_price"] == "900.1"
    assert prods["EU-2"]["list_price"] == "0.25" and prods["EU-2"]["unit"] == "EA"


def test_import_xlsx_and_rejections(app) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("Zeta Imports", with_catalog=False)
    admin = login_as(app, t.users["admin"])
    mapping = json.dumps({"sku": "SKU", "description": "Description", "list_price": "Quantity"})
    r = upload(
        admin, "/catalogs/import", "rfq_clean.xlsx", (FIXTURES / "rfq_clean.xlsx").read_bytes(), {"mapping": mapping}
    )
    assert r.status_code == 422  # header row is on row 2; mapping names not found in row 1 → clear error
    assert "not found in header" in r.json()["detail"]
    assert (
        upload(
            admin, "/catalogs/import", "zip_bomb.xlsx", (FIXTURES / "zip_bomb.xlsx").read_bytes(), {"mapping": MAPPING}
        ).status_code
        == 413
    )
    assert (
        upload(
            admin,
            "/catalogs/import",
            "corrupted.xlsx",
            (FIXTURES / "corrupted.xlsx").read_bytes(),
            {"mapping": MAPPING},
        ).status_code
        == 422
    )
    assert (
        upload(
            admin, "/catalogs/import", "macro.xlsm", (FIXTURES / "macro.xlsm").read_bytes(), {"mapping": MAPPING}
        ).status_code
        == 415
    )
    assert (
        upload(
            admin, "/catalogs/import", "rfq.pdf", (FIXTURES / "rfq_clean.pdf").read_bytes(), {"mapping": MAPPING}
        ).status_code
        == 415
    )
    assert upload(admin, "/catalogs/import", "c.csv", b"", {"mapping": MAPPING}).status_code == 422
    assert (
        upload(admin, "/catalogs/import", "c.csv", b"SKU,Description\nA,B\n", {"mapping": "not json"}).status_code
        == 422
    )
    assert (
        upload(
            admin, "/catalogs/import", "c.csv", b"SKU,Description\nA,B\n", {"mapping": json.dumps({"sku": "SKU"})}
        ).status_code
        == 422
    )


def test_import_oversize_rejected(app, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("Eta Imports", with_catalog=False)
    admin = login_as(app, t.users["admin"])
    big = b"SKU,Description,Unit,Currency,List Price\n" + b"A1,Thing,EA,AED,1\n" * 800_000  # ~14 MB > 10 MiB
    r = upload(admin, "/catalogs/import", "big.csv", big, {"mapping": MAPPING})
    assert r.status_code == 413


def test_products_hide_cost_for_sales(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    items = sales.get(f"/catalogs/versions/{tenant_a.catalog_version_id}/products?q=SKU-1001").json()["items"]
    assert items and "cost_price" not in items[0]
    approver = login_as(app, tenant_a.users["approver"])
    items = approver.get(f"/catalogs/versions/{tenant_a.catalog_version_id}/products?q=SKU-1001").json()["items"]
    assert items[0]["cost_price"] == "0.52"
