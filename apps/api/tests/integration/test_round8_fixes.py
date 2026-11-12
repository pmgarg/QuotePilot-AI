"""Stage 1 fix round 8 — implementer regression tests.

D5 (found by the Stage 3 QA run, lives in Stage 1 code): a 2xx answer to a mutation must mean the transaction is
committed. SEC4-2 / SEC4-6: numbers read from files stay inside the range the columns and the API accept.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db.session import get_sessionmaker, set_tenant
from tests.conftest import AuthedClient, Tenant, login_as, make_tenant, upload
from tests.integration.test_catalog import MAPPING

pytestmark = pytest.mark.integration


class ProbeAtResponseStart:
    """ASGI wrapper: runs ``probe(scope)`` just before the first response message reaches the client."""

    def __init__(self, inner: Any, probe: Any) -> None:
        self.inner = inner
        self.probe = probe

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.inner(scope, receive, send)
            return

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                self.probe(scope, message["status"])
            await send(message)

        await self.inner(scope, receive, send_wrapper)


def _committed(org_id: Any, statement: str, params: dict[str, Any]) -> int:
    with get_sessionmaker()() as db:  # a second connection sees committed rows only
        set_tenant(db, org_id, None)
        n = db.execute(text(statement), params).scalar()
        db.rollback()
    return int(n or 0)


def test_d5_mutation_and_its_audit_row_are_committed_before_the_response_starts(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    seen: list[tuple[int, int, int]] = []

    def probe(scope: dict[str, Any], status: int) -> None:
        if scope["method"] == "POST" and scope["path"].endswith("/customers"):
            rows = _committed(tenant_a.org_id, "SELECT count(*) FROM customers WHERE name = :n", {"n": "Durable Co"})
            audits = _committed(
                tenant_a.org_id,
                "SELECT count(*) FROM audit_events WHERE action = 'customer.create' AND summary::text LIKE :n",
                {"n": "%Durable Co%"},
            )
            seen.append((status, rows, audits))

    sales = AuthedClient(
        TestClient(ProbeAtResponseStart(app, probe), base_url="http://testserver"), tenant_a.users["sales"]
    )
    r = sales.post("/customers", json={"name": "Durable Co"})
    assert r.status_code == 201, r.text
    assert len(seen) == 1 and seen[0][0] == 201 and seen[0][1] == 1, seen
    total_audits = _committed(tenant_a.org_id, "SELECT count(*) FROM audit_events WHERE action = 'customer.create'", {})
    assert total_audits >= 1


def test_d5_failed_request_commits_nothing(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    before = _committed(tenant_a.org_id, "SELECT count(*) FROM customers", {})
    r = sales.post("/customers", json={"name": ""})
    assert r.status_code == 422
    assert _committed(tenant_a.org_id, "SELECT count(*) FROM customers", {}) == before


@pytest.mark.parametrize("price", ["1e12", "999999999999.9999999", "1e30", "9" * 40])
def test_sec4_2_price_the_column_cannot_hold_is_a_row_error(app, price: str) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("R8 Price", with_catalog=False)
    admin = login_as(app, t.users["admin"])
    body = f"SKU,Description,Unit,Currency,List Price\nBIG,Big,EA,AED,{price}\nOK,Fine,EA,AED,999999999999.999999\n"
    r = upload(admin, "/catalogs/import", "c.csv", body.encode(), {"mapping": MAPPING, "catalog_name": "Main"})
    assert r.status_code == 201, r.text
    report = r.json()["import_report"]
    assert report["imported"] == 1 and report["skipped"] == 1
    # beyond 10^30 the parser itself refuses the cell; below that the column bound applies
    expected = "list price is not a number" if len(price) > 31 else "list price is too large"
    assert [e["message"] for e in report["errors"]] == [expected]
    items = {
        p["sku"]: p["list_price"] for p in admin.get(f"/catalogs/versions/{r.json()['id']}/products").json()["items"]
    }
    assert set(items) == {"OK"} and Decimal(items["OK"]) == Decimal("999999999999.999999")


@pytest.mark.parametrize("qty", ["10000000.001", "1e12", "1e30", "99999999999999.5"])
def test_sec4_6_rfq_quantity_above_the_api_cap_is_left_empty_and_blocks_the_quote(
    app, tenant_a: Tenant, qty: str
) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    csv = f"SKU,Description,Qty,Unit\nSKU-1001,Hex bolt,{qty},EA\nSKU-1002,Other,10000000,EA\n".encode()
    r = upload(sales, "/rfqs", "r.csv", csv, {"customer_id": str(tenant_a.customer_id)})
    assert r.status_code == 201, r.text
    items = {it["line_no"]: it for it in r.json()["items"]}
    assert items[1]["quantity"] is None and "quantity_out_of_range" in items[1]["flags"]
    assert Decimal(items[2]["quantity"]) == Decimal(10_000_000) and "quantity_out_of_range" not in items[2]["flags"]
    g = sales.post(f"/rfqs/{r.json()['id']}/generate-quote")
    if g.status_code == 201:
        for ln in g.json()["current_version"]["lines"]:
            assert ln["quantity"] is None or Decimal(ln["quantity"]) <= Decimal(10_000_000)
    else:
        assert g.status_code in (409, 422), g.text
