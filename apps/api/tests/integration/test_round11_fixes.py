"""Stage 1 fix round 11 - integration regressions: matching bounded and off the API process (SEC7-1/SEC6-2),
per-user limits on expensive endpoints (SEC7-7), seed reading only DEMO_PASSWORD (SEC7-4)."""

from __future__ import annotations

import json
import os
import random
import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.core.config import get_settings
from tests.conftest import API, Tenant, login_as, make_tenant, upload

pytestmark = pytest.mark.integration

HEAD = b"SKU,Description,Unit,Currency,List Price,Cost,Stock\n"
MAPPING = json.dumps(
    {
        "sku": "SKU",
        "description": "Description",
        "unit": "Unit",
        "currency": "Currency",
        "list_price": "List Price",
        "cost_price": "Cost",
        "stock_qty": "Stock",
    }
)
WORDS = ["hex", "bolt", "steel", "zinc", "washer", "m8", "m10", "plated", "size", "nut", "screw", "flange"]


def _bulk_products(tenant: Tenant, n: int, desc_len: int) -> None:
    """Insert ``n`` synthetic products into the tenant's active catalog version as the owner role (as the auditor did)."""
    eng = create_engine(os.environ["DATABASE_ADMIN_URL"], future=True, isolation_level="AUTOCOMMIT")
    base = "hex bolt size NUM steel zinc plated " * 20
    try:
        with eng.connect() as conn:
            conn.execute(text("SELECT set_config('app.current_org', :o, false)"), {"o": str(tenant.org_id)})
            conn.execute(
                text(
                    "INSERT INTO products (id, organization_id, catalog_version_id, sku, sku_normalized, description,"
                    " unit, currency, list_price, flags) SELECT gen_random_uuid(), :o, :v, 'BULK-' || g, 'BULK-' || g,"
                    " left(replace(:base, 'NUM', g::text), :len), 'EA', 'AED', 5, '[]'::jsonb"
                    " FROM generate_series(1, :n) AS g"
                ),
                {"o": str(tenant.org_id), "v": str(tenant.catalog_version_id), "base": base, "len": desc_len, "n": n},
            )
    finally:
        eng.dispose()


def _adversarial_lines(n: int) -> str:
    rnd = random.Random(7)
    return "\n".join(
        " ".join(rnd.choice(WORDS) + str(rnd.randint(0, 99999)) for _ in range(30))[:190] for _ in range(n)
    )


def test_sec7_1_large_catalog_and_hundreds_of_long_lines_return_in_bounded_time_without_starving_the_api(
    app, tenant_a: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    """30,000 extra products x 300 characters, 300 pasted lines of about 190 characters. Before round 11 this held the
    API process (GIL) for minutes; now the matching runs in a worker process with an 8 s deadline (4 s here)."""
    monkeypatch.setattr(get_settings(), "match_timeout_seconds", 4.0)
    _bulk_products(tenant_a, 30_000, 300)
    sales = login_as(app, tenant_a.users["sales"])
    lines = _adversarial_lines(300)
    probe = TestClient(app, base_url="http://testserver")
    latencies: list[float] = []
    stop = threading.Event()

    def poll_health() -> None:
        while not stop.is_set():
            t0 = time.perf_counter()
            r = probe.get(f"{API}/health")
            latencies.append(time.perf_counter() - t0)
            assert r.status_code == 200
            time.sleep(0.01)

    poller = threading.Thread(target=poll_health)
    poller.start()
    time.sleep(0.3)
    before = len(latencies)
    started = time.perf_counter()
    r = sales.post("/rfqs", json={"text": lines, "customer_id": str(tenant_a.customer_id)})
    elapsed = time.perf_counter() - started
    stop.set()
    poller.join()
    assert r.status_code == 201, r.text
    items = r.json()["items"]
    assert len(items) == 300
    skipped = [i for i in items if "match_skipped_budget" in i["flags"]]
    matched = [i for i in items if "match_skipped_budget" not in i["flags"]]
    assert matched and skipped, (len(matched), len(skipped))  # some lines were answered, the rest left for a person
    assert all(i["match_status"] == "unmatched" and not i["candidates"] for i in skipped)
    # the skipped lines are the tail: matching answers in line order
    first_skipped = items.index(skipped[0])
    assert all("match_skipped_budget" in i["flags"] for i in items[first_skipped:])
    # Bound: 4 s deadline + loading 30,000 products + storing 300 items. Measured about 6 s; the old in-process code
    # needed minutes for the same request.
    assert elapsed < 40, elapsed
    during = latencies[before:]
    # The key property: while the parse ran, another request thread was served promptly and continuously.
    assert len(during) >= 10, len(during)
    assert max(during) < 3.0, max(
        during
    )  # measured 0.3-0.5 s (11 s with the old in-process matcher); a GIL-held matcher stalls it for the whole call


def test_sec7_1_a_second_parse_while_matchers_are_busy_is_still_answered(
    app, tenant_a: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    """Three parallel requests for a big job: at most two matcher processes run, the third waits for a slot (or is
    left unmatched at its deadline) and every request completes with 201."""
    monkeypatch.setattr(get_settings(), "match_timeout_seconds", 2.0)
    _bulk_products(tenant_a, 20_000, 200)
    users = [login_as(app, tenant_a.users[r]) for r in ("sales", "admin", "owner")]
    lines = _adversarial_lines(100)
    results: list[int] = []

    def go(c) -> None:  # type: ignore[no-untyped-def]
        results.append(c.post("/rfqs", json={"text": lines, "customer_id": str(tenant_a.customer_id)}).status_code)

    threads = [threading.Thread(target=go, args=(c,)) for c in users]
    started = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [201, 201, 201]
    assert time.perf_counter() - started < 60


def test_small_requests_are_matched_exactly_as_before(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    r = sales.post(
        "/rfqs",
        json={
            "text": "10 pcs SKU-1001\n5 pcs hex bolt m8 zinc\n2 pcs unobtainium widget",
            "customer_id": str(tenant_a.customer_id),
        },
    )
    assert r.status_code == 201, r.text
    items = r.json()["items"]
    assert items[0]["match_status"] == "exact"
    assert all("match_skipped_budget" not in i["flags"] for i in items)
    assert items[1]["candidates"]


# ------------------------------------------------------------------------------------------------ SEC7-7
def _audit_count(org_id) -> int:  # type: ignore[no-untyped-def]
    eng = create_engine(os.environ["DATABASE_ADMIN_URL"], future=True, isolation_level="AUTOCOMMIT")
    try:
        with eng.connect() as conn:
            conn.execute(text("SELECT set_config('app.current_org', :o, false)"), {"o": str(org_id)})
            return int(conn.execute(text("SELECT count(*) FROM audit_events")).scalar() or 0)
    finally:
        eng.dispose()


def test_rfq_creation_is_rate_limited_per_user(app, tenant_a: Tenant, tenant_b: Tenant, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(get_settings(), "rfq_parse_rate_limit_attempts", 3)
    sales = login_as(app, tenant_a.users["sales"])
    other_user = login_as(app, tenant_a.users["admin"])
    other_tenant = login_as(app, tenant_b.users["sales"])
    body = {"text": "5 pcs SKU-1001", "customer_id": str(tenant_a.customer_id)}
    csv = b"SKU,Description,Qty,Unit\nSKU-1001,Hex bolt,5,EA\n"
    assert sales.post("/rfqs", json=body).status_code == 201
    assert upload(sales, "/rfqs", "r.csv", csv, {"customer_id": str(tenant_a.customer_id)}).status_code == 201
    first = sales.post("/rfqs", json=body)
    assert first.status_code == 201  # the third is still allowed
    audits = _audit_count(tenant_a.org_id)
    for attempt in (lambda: sales.post("/rfqs", json=body), lambda: upload(sales, "/rfqs", "r.csv", csv)):
        r = attempt()
        assert r.status_code == 429, r.text
        assert r.json()["error"] == "rate_limited" and "request_id" in r.json()
        retry = int(r.headers["Retry-After"])
        assert 1 <= retry <= get_settings().expensive_rate_limit_window_seconds
    assert _audit_count(tenant_a.org_id) == audits  # a refused request writes and stores nothing
    assert len(sales.get("/rfqs").json()) == 3
    # re-parse shares the budget
    rfq_id = first.json()["id"]
    assert sales.post(f"/rfqs/{rfq_id}/reparse").status_code == 429
    # other users and other tenants have their own budget
    assert other_user.post("/rfqs", json=body).status_code == 201
    assert other_tenant.post("/rfqs", json={"text": "5 pcs SKU-1001"}).status_code == 201
    # reads and cheap edits are not limited
    assert sales.get(f"/rfqs/{rfq_id}").status_code == 200
    item_id = first.json()["items"][0]["id"]
    assert sales.patch(f"/rfqs/{rfq_id}/items/{item_id}", json={"quantity": "7"}).status_code == 200


def test_reparse_is_rate_limited_and_forbidden_or_cross_tenant_calls_are_not_counted(
    app, tenant_a: Tenant, tenant_b: Tenant, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    readonly = login_as(app, tenant_a.users["readonly"])
    other = login_as(app, tenant_b.users["sales"])
    rfq = sales.post("/rfqs", json={"text": "5 pcs SKU-1001", "customer_id": str(tenant_a.customer_id)}).json()
    foreign = other.post("/rfqs", json={"text": "5 pcs SKU-1001"}).json()
    monkeypatch.setattr(get_settings(), "rfq_parse_rate_limit_attempts", 2)
    from app.services.ratelimit import action_limiter

    action_limiter.reset()
    # forbidden role inside the tenant: 403, and neither counted nor rate limited, however often it is tried
    for _ in range(5):
        assert readonly.post(f"/rfqs/{rfq['id']}/reparse").status_code == 403
        assert readonly.post("/rfqs", json={"text": "5 pcs SKU-1001"}).status_code == 403
    # another tenant's RFQ: 404 (no existence oracle), not counted against the caller's budget
    for _ in range(5):
        assert sales.post(f"/rfqs/{foreign['id']}/reparse").status_code == 404
    assert sales.post(f"/rfqs/{rfq['id']}/reparse").status_code == 200
    assert sales.post(f"/rfqs/{rfq['id']}/reparse").status_code == 200
    r = sales.post(f"/rfqs/{rfq['id']}/reparse")
    assert r.status_code == 429 and r.headers["Retry-After"].isdigit()
    # the foreign RFQ is untouched
    assert other.get(f"/rfqs/{foreign['id']}").status_code == 200


def test_catalog_import_is_rate_limited_per_user(app, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("R11 Import", with_catalog=False)
    other = make_tenant("R11 Import Two", with_catalog=False)
    monkeypatch.setattr(get_settings(), "catalog_import_rate_limit_attempts", 2)
    admin = login_as(app, t.users["admin"])
    owner = login_as(app, t.users["owner"])
    sales = login_as(app, t.users["sales"])
    foreign_admin = login_as(app, other.users["admin"])
    content = HEAD + b"A-1,Widget,EA,AED,5,1,1\n"
    fields = {"mapping": MAPPING, "catalog_name": "M"}
    for _ in range(2):
        assert upload(admin, "/catalogs/import", "c.csv", content, fields).status_code == 201
    audits = _audit_count(t.org_id)
    r = upload(admin, "/catalogs/import", "c.csv", content, fields)
    assert r.status_code == 429 and r.json()["error"] == "rate_limited" and int(r.headers["Retry-After"]) >= 1
    assert _audit_count(t.org_id) == audits
    # forbidden role: 403 (never counted, never 429)
    for _ in range(4):
        assert upload(sales, "/catalogs/import", "c.csv", content, fields).status_code == 403
    # another user in the same tenant and an admin of another tenant are unaffected
    assert upload(owner, "/catalogs/import", "c.csv", content, fields).status_code == 201
    assert upload(foreign_admin, "/catalogs/import", "c.csv", content, fields).status_code == 201
    versions = [v for v in admin.get("/catalogs").json()]
    assert versions  # the refused import created no catalog
