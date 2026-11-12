"""Stage 1 fix round 9 — integration regressions (seed output, pasted RFQ text timing)."""

from __future__ import annotations

import time

import pytest

from app.db.session import get_sessionmaker
from app.fixtures import seed as seed_module
from tests.conftest import Tenant, login_as

pytestmark = pytest.mark.integration


def test_seed_output_lists_accounts_but_never_the_password(app, capsys) -> None:  # type: ignore[no-untyped-def]
    password = "Local-Only-Seed-Secret-2026!"
    with get_sessionmaker()() as db:
        created = seed_module.seed(db, password, verbose=True)
    out = capsys.readouterr()
    assert created and "owner@northwind.example" in out.out
    assert password not in out.out and password not in out.err


def test_sec5_5_pasted_text_with_long_whitespace_runs_is_answered_quickly(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    text = "\n".join("abc" + " " * 20_000 + "x" for _ in range(5)) + "\n10 pcs Hex bolt SKU-1001\n"
    started = time.perf_counter()
    r = sales.post("/rfqs", json={"text": text, "customer_id": str(tenant_a.customer_id)})
    elapsed = time.perf_counter() - started
    assert r.status_code in (201, 413, 422), r.text[:200]
    assert elapsed < 2.0, elapsed
    if r.status_code == 201:
        quantities = [it["quantity"] for it in r.json()["items"]]
        assert "10" in quantities or "10.000" in quantities, quantities


def test_round10_negative_stock_is_left_unknown_with_a_warning(app) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import make_tenant, upload

    t = make_tenant("R10 Stock", with_catalog=False)
    admin = login_as(app, t.users["admin"])
    mapping = (
        '{"sku":"SKU","description":"Description","unit":"Unit","currency":"Currency",'
        '"list_price":"List Price","stock_qty":"Stock"}'
    )
    body = "SKU,Description,Unit,Currency,List Price,Stock\nN1,a,EA,AED,5,-5\nN2,b,EA,AED,5,– 5\nN3,c,EA,AED,5,7\n"
    r = upload(admin, "/catalogs/import", "c.csv", body.encode(), {"mapping": mapping, "catalog_name": "Main"})
    assert r.status_code == 201, r.text
    report = r.json()["import_report"]
    assert report["imported"] == 3 and len(report["warnings"]) == 2
    items = {p["sku"]: p for p in admin.get(f"/catalogs/versions/{r.json()['id']}/products").json()["items"]}
    assert items["N1"]["stock_qty"] is None and items["N2"]["stock_qty"] is None
    assert items["N3"]["stock_qty"] is not None and float(items["N3"]["stock_qty"]) == 7.0
