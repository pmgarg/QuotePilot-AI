"""Security audit — Stage 3, part 2: untrusted input on CSV import and connector push, caps, idempotency
namespaces, FX table, stock timestamps, order version selection and conflict-resolution side effects.

Findings are strict ``xfail`` tests/params carrying their finding id (SEC3-xx, see
docs/evidence/security-stage3-2026-11-02.md); passing checks are plain tests.
"""

from __future__ import annotations

import time
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.db.base import utcnow
from app.db.session import get_sessionmaker, set_tenant
from app.models.catalog import Product
from app.models.customer import Customer
from app.models.intelligence import Order, OrderLine, SyncRun
from app.services.money import parse_decimal
from tests.conftest import AuthedClient, Tenant, login_as, upload
from tests.integration.test_quotes import make_quote
from tests.integration.test_stage3_intelligence import _connector, approved_quote

pytestmark = [pytest.mark.integration, pytest.mark.security]

ORDER_HDR = b"order_ref,customer_reference,sku,description,quantity,unit,unit_price,currency\r\n"
BIG = b"1" + b"0" * 16  # 10^16: overflows every NUMERIC(18, x) column
SEC3_04 = "SEC3-04: unvalidated length/NUL/overflow reaches PostgreSQL → unhandled 500, whole run rolled back"
SEC3_11 = "SEC3-11: parse_decimal() strips the exponent letter — scientific notation is silently misparsed"


def _run_keys(org_id: uuid.UUID) -> set[str]:
    with get_sessionmaker()() as db:
        set_tenant(db, org_id, None)
        keys = set(db.scalars(select(SyncRun.idempotency_key)).all())
        db.rollback()
    return keys


def _orders(org_id: uuid.UUID) -> int:
    with get_sessionmaker()() as db:
        set_tenant(db, org_id, None)
        n = db.scalar(select(func.count()).select_from(Order))
        db.rollback()
    return int(n or 0)


def _sync(admin: AuthedClient, conn: dict, entity: str, key: str, records: list) -> object:  # type: ignore[type-arg]
    return admin.post(
        f"/connectors/{conn['id']}/sync", json={"entity": entity, "idempotency_key": key, "records": records}
    )


def _xf(reason: str):  # type: ignore[no-untyped-def]
    """Fix round 1 closed every case that used this marker; kept as a no-op so the parameter lists stay readable."""
    return ()


# ================================================== H. CSV import hardening
def test_csv_import_caps_and_encoding(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    big = b"name\r\n" + b"x" * (5 * 1024 * 1024 + 10)
    assert upload(admin, "/data/import/customers?idempotency_key=big", "c.csv", big).status_code == 413
    many = b"name\r\n" + b"n\r\n" * 10_001
    assert upload(admin, "/data/import/customers?idempotency_key=many", "c.csv", many).status_code == 413
    assert (
        upload(admin, "/data/import/customers?idempotency_key=enc", "c.csv", b"name\r\n\xff\xfe\r\n").status_code == 422
    )
    assert upload(admin, "/data/import/customers?idempotency_key=", "c.csv", b"name\r\nx\r\n").status_code == 422
    assert (
        upload(admin, "/data/import/customers?idempotency_key=" + "k" * 161, "c.csv", b"name\r\nx\r\n").status_code
        == 422
    )
    assert upload(admin, "/data/import/customers", "c.csv", b"name\r\nx\r\n").status_code == 422  # key required
    # BOM + mixed-case header accepted; empty file → missing header 422
    assert (
        upload(
            admin, "/data/import/customers?idempotency_key=bom", "c.csv", "﻿Name,Reference\r\nBom Co,B-1\r\n".encode()
        ).json()["applied"]
        == 1
    )
    assert upload(admin, "/data/import/customers?idempotency_key=empty", "c.csv", b"").status_code == 422
    # rejected uploads leave no run behind
    assert _run_keys(tenant_a.org_id) == {"bom"}


def test_csv_order_import_with_many_groups_stays_bounded(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    rows = b"".join(f"PO-{i},,SKU-1001,Bolt,1,EA,0.85,AED\r\n".encode() for i in range(1500))
    t0 = time.monotonic()
    r = upload(admin, "/data/import/orders?idempotency_key=groups", "o.csv", ORDER_HDR + rows)
    elapsed = time.monotonic() - t0
    assert r.status_code == 201 and r.json()["applied"] == 1500, r.text
    assert elapsed < 60, elapsed  # one organisation row lock + 1500 inserts; recorded in the evidence
    assert _orders(tenant_a.org_id) == 1500
    assert len(admin.get("/orders").json()) == 200  # list is capped


@pytest.mark.parametrize(
    ("label", "row"),
    [
        pytest.param(
            "sku longer than 100 chars", b"PO-1,," + b"S" * 101 + b",Bolt,1,EA,0.85,AED\r\n", marks=_xf(SEC3_04)
        ),
        pytest.param(
            "unit longer than 16 chars", b"PO-2,,SKU-1001,Bolt,1," + b"U" * 17 + b",0.85,AED\r\n", marks=_xf(SEC3_04)
        ),
        pytest.param(
            "order_ref longer than 100 chars", b"P" * 101 + b",,SKU-1001,Bolt,1,EA,0.85,AED\r\n", marks=_xf(SEC3_04)
        ),
        pytest.param("NUL byte in description", b"PO-4,,SKU-1001,Bo\x00lt,1,EA,0.85,AED\r\n", marks=_xf(SEC3_04)),
        pytest.param(
            "unit_price 10^16 (numeric overflow)", b"PO-6,,SKU-1001,Bolt,1,EA," + BIG + b",AED\r\n", marks=_xf(SEC3_04)
        ),
        pytest.param(
            "quantity 10^16 (numeric overflow)", b"PO-8,,SKU-1001,Bolt," + BIG + b",EA,1,AED\r\n", marks=_xf(SEC3_04)
        ),
        ("quantity NaN", b"PO-5,,SKU-1001,Bolt,NaN,EA,0.85,AED\r\n"),
        ("quantity Infinity", b"PO-7,,SKU-1001,Bolt,Infinity,EA,0.85,AED\r\n"),
        ("negative tax", b"PO-9,,SKU-1001,Bolt,1,EA,1,AED\r\n"),
    ],
)
def test_sec3_04_csv_order_import_rejects_bad_rows_without_losing_the_run(
    app, tenant_a: Tenant, label: str, row: bytes
) -> None:  # type: ignore[no-untyped-def]
    """S3-A07: a bad row must land in ``failed_rows``; the valid rows and the run itself must survive."""
    admin = login_as(app, tenant_a.users["admin"])
    good = b"PO-GOOD,,SKU-1003,Nut,2,EA,0.25,AED\r\n"
    hdr = ORDER_HDR
    if label == "negative tax":
        hdr = ORDER_HDR[:-2] + b",tax_rate_pct\r\n"
        good = good[:-2] + b",5\r\n"
        row = row[:-2] + b",-5\r\n"
    r = upload(admin, "/data/import/orders?idempotency_key=bad", "o.csv", hdr + good + row)
    assert r.status_code == 201, (label, r.status_code, r.text[:300])
    run = r.json()
    assert run["applied"] == 1 and run["rejected"] == 1, (label, run)
    assert _orders(tenant_a.org_id) == 1


@pytest.mark.parametrize(
    ("label", "row"),
    [
        pytest.param(
            "bare CR inside an unquoted field (csv module error)", b"CR Co,N-0,,AED,x\ry\r\n", marks=_xf(SEC3_04)
        ),
        pytest.param("NUL byte in name", b"Bad\x00Co,N-1,,AED\r\n", marks=_xf(SEC3_04)),
        pytest.param("notes with NUL", b"Co,N-3,,AED,\x00\r\n", marks=_xf(SEC3_04)),
        ("unbalanced quote", b'"Quote Co,N-00,,AED,\r\n'),
        ("name longer than 200 chars is truncated, not fatal", b"N" * 300 + b",N-2,,AED\r\n"),
    ],
)
def test_sec3_04_csv_customer_import_rejects_bad_rows_without_losing_the_run(
    app, tenant_a: Tenant, label: str, row: bytes
) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    r = upload(
        admin,
        "/data/import/customers?idempotency_key=badc",
        "c.csv",
        b"name,reference,email,default_currency,notes\r\nGood Co,G-1,,AED,\r\n" + row,
    )
    assert r.status_code == 201, (label, r.status_code, r.text[:300])
    assert r.json()["applied"] >= 1, (label, r.json())


@pytest.mark.xfail(strict=True, reason=SEC3_11)
def test_sec3_11_scientific_notation_is_not_silently_misparsed() -> None:
    """Shared by Stage 1 catalog import, Stage 3 CSV import and connector push. ``1e3`` must be 1000 or rejected,
    never 13."""
    bad = {s: parse_decimal(s) for s in ("1e3", "1E+3", "2.5e2", "1e-2", "1e30")}
    assert all(v is None or v == Decimal(s) for s, v in bad.items()), bad


@pytest.mark.xfail(strict=True, reason=SEC3_11)
def test_sec3_11_scientific_notation_price_on_import_is_not_invented(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    r = upload(
        admin,
        "/data/import/orders?idempotency_key=sci",
        "o.csv",
        ORDER_HDR + b"PO-S,,SKU-1001,Bolt,1e3,EA,8.5E2,AED\r\n",
    ).json()
    if r["applied"] == 0:
        return  # rejected is acceptable
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        ln = db.scalar(select(OrderLine))
        assert ln is not None
        assert (ln.quantity, ln.unit_price) == (Decimal("1000"), Decimal("850")), (ln.quantity, ln.unit_price)
        db.rollback()


# ============================================== I. connector push hardening
def test_connector_caps_scopes_and_types(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    conn = _connector(admin, ["read:products", "read:stock", "read:customers"])
    assert _sync(admin, conn, "products", "cap", [{"sku": "x"}] * 5001).status_code == 422
    assert _sync(admin, conn, "orders", "scope", []).status_code == 403  # scope not declared
    assert _sync(admin, conn, "secrets", "ent", []).status_code == 422
    assert (
        admin.post(
            f"/connectors/{conn['id']}/sync", json={"entity": "products", "idempotency_key": "t", "records": "nope"}
        ).status_code
        == 422
    )
    assert (
        admin.post(
            f"/connectors/{conn['id']}/sync", json={"entity": "products", "idempotency_key": "t", "records": [1, "two"]}
        ).status_code
        == 422
    )
    assert admin.post("/connectors", json={"kind": "erp_stub", "name": "x", "scopes": []}).status_code == 422
    assert (
        admin.post("/connectors", json={"kind": "erp_stub", "name": "x", "scopes": ["read:stock"] * 9}).status_code
        == 422
    )
    assert (
        admin.post("/connectors", json={"kind": "../../etc", "name": "x", "scopes": ["read:stock"]}).status_code == 422
    )
    assert admin.post("/connectors", json={"kind": "csv", "name": "x", "scopes": ["write:orders"]}).status_code == 422
    # records with wrong-typed but harmless fields are rejected per record, never fatal
    run = _sync(
        admin, conn, "stock", "types", [{"sku": ["SKU-1001"], "quantity": {"a": 1}, "observed_at": 1700000000}]
    ).json()
    assert run["rejected"] == 1 and run["status"] == "failed"
    run = _sync(
        admin,
        conn,
        "products",
        "neg",
        [{"sku": "SKU-1001", "list_price": "-1"}, {"sku": "SKU-1001", "cost_price": "(5)"}],
    ).json()
    assert run["rejected"] == 2 and run["conflicts"] == 0  # "(5)" is accounting notation for -5 → rejected
    assert all("Traceback" not in str(x) for x in run["report"]["rejected"])
    assert run["status"] == "failed" and admin.get("/connectors").json()[0]["last_error"] == "all records rejected"


@pytest.mark.parametrize(
    ("entity", "record", "label"),
    [
        ("products", {"sku": "SKU-1001", "list_price": "NaN"}, "NaN price"),
        ("products", {"sku": "SKU-1001", "list_price": "sNaN"}, "signalling NaN price"),
        ("products", {"sku": "SKU-1001", "list_price": "Infinity"}, "Infinity price"),
        pytest.param("products", {"sku": "S\x00KU", "list_price": "1"}, "NUL in sku", marks=_xf(SEC3_04)),
        ("stock", {"sku": "SKU-1001", "quantity": "NaN", "observed_at": "2026-01-01T00:00:00+00:00"}, "NaN quantity"),
        pytest.param(
            "stock",
            {"sku": "SKU-1001", "quantity": BIG.decode(), "observed_at": "2026-01-01T00:00:00+00:00"},
            "quantity overflow",
            marks=_xf(SEC3_04),
        ),
        pytest.param(
            "customers",
            {"reference": "C-9", "name": "X", "email": {"$ne": 1}},
            "email is an object",
            marks=_xf(SEC3_04),
        ),
        pytest.param(
            "customers",
            {"reference": "C-9", "name": "X", "email": "e" * 400},
            "email longer than column",
            marks=_xf(SEC3_04),
        ),
        pytest.param("customers", {"reference": "C\x009", "name": "X"}, "NUL in reference", marks=_xf(SEC3_04)),
        pytest.param(
            "orders",
            {"external_ref": "E-1", "lines": [{"sku": "S" * 101, "quantity": 1, "unit_price": 1}]},
            "sku longer than column",
            marks=_xf(SEC3_04),
        ),
        (
            "orders",
            {"external_ref": "E-2", "lines": [{"sku": "A", "quantity": "NaN", "unit_price": 1}]},
            "NaN quantity",
        ),
        pytest.param(
            "orders",
            {"external_ref": "E-3", "lines": [{"sku": "A", "quantity": 1, "unit_price": BIG.decode()}]},
            "price overflow",
            marks=_xf(SEC3_04),
        ),
        pytest.param(
            "orders",
            {"external_ref": "E-4", "lines": [{"sku": "A", "quantity": 1, "unit_price": 1, "unit": "U" * 20}]},
            "unit longer than column",
            marks=_xf(SEC3_04),
        ),
        pytest.param(
            "orders",
            {"external_ref": "E-5", "lines": [{"sku": "A", "quantity": 1, "unit_price": 1, "tax_rate_pct": "1000000"}]},
            "tax overflow",
            marks=_xf(SEC3_04),
        ),
        pytest.param(
            "orders",
            {"external_ref": "E-6", "lines": [{"sku": "A", "quantity": 1, "unit_price": 1, "tax_rate_pct": "-5"}]},
            "negative tax accepted",
            marks=_xf("SEC3-04: connector orders accept a negative tax_rate_pct (CSV import rejects it)"),
        ),
        pytest.param(
            "orders",
            {"external_ref": "E-7", "lines": [{"sku": "A", "quantity": 1, "unit_price": 1, "tax_rate_pct": "150"}]},
            "tax above 100 accepted",
            marks=_xf("SEC3-04: connector orders accept tax_rate_pct > 100 (CSV import rejects it)"),
        ),
    ],
)
def test_sec3_04_connector_rejects_hostile_records_per_record(
    app, tenant_a: Tenant, entity: str, record: dict, label: str
) -> None:  # type: ignore[no-untyped-def, type-arg]
    admin = login_as(app, tenant_a.users["admin"])
    conn = _connector(admin, ["read:products", "read:stock", "read:orders", "read:customers"])
    r = _sync(admin, conn, entity, "h", [record])
    assert r.status_code == 201, (label, r.status_code, r.text[:300])
    run = r.json()
    assert run["rejected"] == 1 and run["applied"] == 0 and run["conflicts"] == 0, (label, run)
    assert run["status"] == "failed"


def test_sec3_04_apply_remote_with_oversized_unit_fails_closed(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    conn = _connector(admin, ["read:products"])
    run = _sync(admin, conn, "products", "unit", [{"sku": "SKU-1002", "unit": "U" * 40}]).json()
    if run["rejected"] == 1:
        return  # rejected at the door would be fine
    c = admin.get("/sync-conflicts").json()[0]
    r = admin.post(f"/sync-conflicts/{c['id']}/resolve", json={"resolution": "apply_remote"})
    assert r.status_code in (409, 422), (r.status_code, r.text[:200])


def test_connector_conflict_resolution_is_authorised_audited_and_scoped(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    appr = login_as(app, tenant_a.users["approver"])
    conn = _connector(admin, ["read:products", "read:customers"])
    _sync(admin, conn, "products", "c1", [{"sku": "SKU-1001", "list_price": "0.99", "cost_price": "0.10"}])
    bolt = admin.get("/sync-conflicts").json()[0]
    assert appr.post(f"/sync-conflicts/{bolt['id']}/resolve", json={"resolution": "apply_remote"}).status_code == 403
    assert admin.post(f"/sync-conflicts/{bolt['id']}/resolve", json={"resolution": "overwrite"}).status_code == 422
    # only the listed fields of the conflict may change — nothing else on the product
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        before = db.scalar(select(Product).where(Product.sku == "SKU-1001", Product.organization_id == tenant_a.org_id))
        assert before is not None
        snap = (before.description, before.unit, before.currency, before.sku, before.catalog_version_id)
        db.rollback()
    assert admin.post(f"/sync-conflicts/{bolt['id']}/resolve", json={"resolution": "apply_remote"}).status_code == 200
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        after = db.scalar(select(Product).where(Product.sku == "SKU-1001", Product.organization_id == tenant_a.org_id))
        assert after is not None
        assert (after.description, after.unit, after.currency, after.sku, after.catalog_version_id) == snap
        assert after.list_price == Decimal("0.99") and after.cost_price == Decimal("0.10")
        db.rollback()
    # a customer conflict can only patch master fields, never the reference / tenant / deletion marker
    admin.post("/customers", json={"name": "Keep Co", "reference": "KC-1", "email": "a@kc.example"})
    _sync(
        admin,
        conn,
        "customers",
        "c2",
        [
            {
                "reference": "KC-1",
                "name": "Keep Co",
                "email": "b@kc.example",
                "organization_id": "x",
                "deleted_at": "2020-01-01",
            }
        ],
    )
    c = next(x for x in admin.get("/sync-conflicts").json() if x["entity"] == "customers")
    assert c["fields"] == ["email"]
    assert admin.post(f"/sync-conflicts/{c['id']}/resolve", json={"resolution": "apply_remote"}).status_code == 200
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        kc = db.scalar(select(Customer).where(Customer.reference == "KC-1"))
        assert (
            kc is not None
            and kc.email == "b@kc.example"
            and kc.deleted_at is None
            and kc.organization_id == tenant_a.org_id
        )
        db.rollback()


def test_sec3_06_idempotency_key_namespace_is_bound_to_entity_and_connector(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    conn = _connector(admin, ["read:products", "read:stock"])
    first = _sync(admin, conn, "products", "same", [{"sku": "SKU-1001", "list_price": "0.85"}]).json()
    other = _sync(
        admin, conn, "stock", "same", [{"sku": "SKU-1001", "quantity": "1", "observed_at": utcnow().isoformat()}]
    ).json()
    assert other["id"] != first["id"] and other["entity"] == "stock", other
    imp = upload(admin, "/data/import/customers?idempotency_key=same", "c.csv", b"name\r\nX\r\n").json()
    assert imp["kind"] == "csv_import" and imp["entity"] == "customers", imp
    csv_conn = admin.post("/connectors", json={"kind": "csv", "name": "drop", "scopes": ["read:products"]}).json()
    third = _sync(admin, csv_conn, "products", "same", []).json()
    assert third["connection_id"] == csv_conn["id"], third


def test_sec3_07_disabled_connector_can_be_replaced_or_re_enabled(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    conn = _connector(admin, ["read:stock"])
    assert admin.post(f"/connectors/{conn['id']}/disable").status_code == 200
    assert _sync(admin, conn, "stock", "d", []).status_code == 409  # disabled stays disabled
    assert admin.post(f"/connectors/{conn['id']}/disable").status_code == 200  # idempotent, audited again
    r = admin.post("/connectors", json={"kind": "erp_stub", "name": "Demo ERP v2", "scopes": ["read:stock"]})
    assert r.status_code == 201, r.text


# ========================================================= J. FX table
def test_fx_rate_validation(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    base = {"base_currency": "USD", "quote_currency": "AED", "as_of": "2026-01-01"}
    for bad in ("NaN", "Infinity", "-1", "0", "1e7", "abc"):
        assert admin.post("/fx-rates", json={**base, "rate": bad}).status_code == 422, bad
    assert admin.post("/fx-rates", json={**base, "rate": "3.67", "as_of": "2999-01-01"}).status_code == 422
    assert admin.post("/fx-rates", json={**base, "rate": "3.67", "quote_currency": "USD"}).status_code == 422
    assert admin.post("/fx-rates", json={**base, "rate": "3.67", "quote_currency": "XXX"}).status_code == 422
    assert admin.post("/fx-rates", json={**base, "rate": "3.67", "source": "s" * 65}).status_code == 422
    ok = admin.post("/fx-rates", json={**base, "rate": "3.6725"})
    assert ok.status_code == 201 and ok.json()["rate"] == "3.6725"


def test_sec3_08_fx_rate_below_column_precision_is_refused_not_500(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    r = admin.post(
        "/fx-rates", json={"base_currency": "USD", "quote_currency": "AED", "as_of": "2026-01-01", "rate": "1e-9"}
    )
    assert r.status_code == 422, (r.status_code, r.text[:200])


# ========================================================= stock
def test_stock_timestamps_and_sku_matching(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    conn = _connector(admin, ["read:stock"])
    now = utcnow()
    recs = [
        {
            "sku": "sku-1001",
            "quantity": "7",
            "observed_at": (now - timedelta(minutes=1)).isoformat(),
        },  # case-insensitive match
        {
            "sku": "SKU-1001",
            "quantity": "8",
            "observed_at": (now - timedelta(days=400)).isoformat(),
        },  # older → not latest
        {"sku": "SKU-1002", "quantity": "1", "observed_at": "2026-01-01T00:00:00"},  # naive
        {"sku": "SKU-1002", "quantity": "1", "observed_at": (now + timedelta(seconds=120)).isoformat()},  # future
        {"sku": "SKU-1002", "quantity": "1", "observed_at": "not-a-date"},
        {"sku": "SKU-1002", "quantity": "1", "observed_at": 1700000000},
        {"sku": "SKU-1002", "quantity": "-1", "observed_at": now.isoformat()},
        {"sku": "' OR 1=1 --", "quantity": "1", "observed_at": now.isoformat()},
    ]
    run = _sync(admin, conn, "stock", "s", recs).json()
    assert (run["applied"], run["rejected"]) == (2, 6), run
    levels = {s["sku"]: s for s in admin.get("/stock").json()}
    assert levels["SKU-1001"]["quantity"] == "7" and levels["SKU-1001"]["status"] == "fresh"
    assert levels["SKU-1002"]["status"] == "unknown"
    assert admin.get("/stock?q=%27%20OR%201%3D1%20--").json() == []
    assert admin.get("/stock?q=%25").json() == []  # escaped LIKE wildcard


# ========================================================= orders
def test_sec3_09_superseded_approved_version_is_not_convertible(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, approver, q = approved_quote(app, tenant_a)
    v1_total = Decimal(q["current_version"]["grand_total"])
    nv = sales.post(f"/quotes/{q['id']}/versions", json={"expected_lock_version": q["lock_version"]})
    assert nv.status_code == 201, nv.text
    q2 = nv.json()
    bolt = next(ln for ln in q2["current_version"]["lines"] if ln["sku"] == "SKU-1001")
    e = sales.patch(
        f"/quotes/{q['id']}",
        json={
            "expected_lock_version": q2["lock_version"],
            "lines": [{"id": bolt["id"], "quantity": str(Decimal(bolt["quantity"]) * 2)}],
        },
    )
    assert e.status_code == 200, e.text
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": e.json()["lock_version"]}).json()
    a = approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": s["lock_version"]})
    assert a.status_code == 200 and a.json()["current_version_no"] == 2
    r = sales.post(f"/quotes/{q['id']}/orders", json={"version_no": 1})
    assert r.status_code in (404, 409), (
        r.status_code,
        r.json().get("quote_version_no"),
        r.json().get("grand_total"),
        str(v1_total),
    )


def test_sec3_10_in_place_catalog_price_change_is_surfaced_on_open_quotes(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    sales = login_as(app, tenant_a.users["sales"])
    approver = login_as(app, tenant_a.users["approver"])
    q = make_quote(sales, str(tenant_a.customer_id))
    bolt = next(ln for ln in q["current_version"]["lines"] if ln["sku"] == "SKU-1001")
    assert bolt["price_source"] == "catalog" and Decimal(bolt["unit_price"]) == Decimal("0.85")
    conn = _connector(admin, ["read:products"])
    _sync(admin, conn, "products", "px", [{"sku": "SKU-1001", "list_price": "1.70"}])
    c = admin.get("/sync-conflicts").json()[0]
    assert admin.post(f"/sync-conflicts/{c['id']}/resolve", json={"resolution": "apply_remote"}).status_code == 200
    fresh = sales.get(f"/quotes/{q['id']}").json()
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": fresh["lock_version"]})
    a = (
        approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": s.json()["lock_version"]})
        if s.status_code == 200
        else s
    )
    line = next(
        ln for ln in sales.get(f"/quotes/{q['id']}").json()["current_version"]["lines"] if ln["sku"] == "SKU-1001"
    )
    stale_and_approved = (
        a.status_code == 200 and line["price_source"] == "catalog" and Decimal(line["unit_price"]) == Decimal("0.85")
    )
    assert not stale_and_approved, (a.status_code, line["price_source"], line["unit_price"])


def test_order_notes_and_reasons_are_bounded_and_inert(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, approver, q = approved_quote(app, tenant_a)
    assert sales.post(f"/quotes/{q['id']}/orders", json={"notes": "n" * 2001}).status_code == 422
    assert sales.post(f"/quotes/{q['id']}/orders", json={"version_no": 0}).status_code == 422
    assert sales.post(f"/quotes/{q['id']}/orders", json={"version_no": 99}).status_code == 404
    o = sales.post(f"/quotes/{q['id']}/orders", json={"notes": "<script>alert(1)</script>"}).json()
    assert o["notes"] == "<script>alert(1)</script>"  # stored verbatim, rendered by React (escaped)
    assert approver.post(f"/orders/{o['id']}/cancel", json={"reason": "r" * 2001}).status_code == 422
    assert approver.post(f"/orders/{o['id']}/cancel", json={"reason": "   "}).status_code == 422
    c = approver.post(f"/orders/{o['id']}/cancel", json={"reason": "=cmd"}).json()
    assert c["status"] == "cancelled" and c["events"][-1]["detail"] == "=cmd"


# ================================================= K. regression of Stage 1/2 controls
def test_csrf_is_enforced_on_every_stage3_mutation(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    owner = login_as(app, tenant_a.users["owner"])
    z = str(uuid.uuid4())
    paths = [
        f"/quotes/{z}/orders",
        f"/orders/{z}/confirm",
        f"/orders/{z}/fulfil",
        f"/orders/{z}/cancel",
        f"/customers/{tenant_a.customer_id}/order-proposals",
        f"/order-proposals/{z}/approve",
        f"/order-proposals/{z}/reject",
        "/fx-rates",
        "/data/import/customers?idempotency_key=x",
        "/connectors",
        f"/connectors/{z}/sync",
        f"/connectors/{z}/disable",
        f"/sync-conflicts/{z}/resolve",
        "/retention/requests",
        f"/retention/requests/{z}/approve",
        f"/retention/requests/{z}/reject",
        f"/retention/requests/{z}/legal-hold",
    ]
    for p in paths:
        for hdr in ({}, {"X-CSRF-Token": "0" * 64}):
            r = owner.client.post(f"/api/v1{p}", json={}, headers=hdr)
            assert r.status_code == 403 and r.json().get("action") == "csrf", (p, hdr, r.status_code, r.text[:200])
    # CSV exports are GET downloads: no CSRF needed, but they are no-store attachments
    r = owner.get("/data/export/customers")
    assert r.headers["cache-control"] == "no-store" and r.headers["content-disposition"].startswith("attachment;")
    assert r.headers["x-content-type-options"] == "nosniff"


def test_sec3_04_crash_path_does_not_leak_internals(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """While SEC3-04 is open the hostile row yields a 500; the body must still be generic (Stage 1 control)."""
    from fastapi.testclient import TestClient

    admin = login_as(app, tenant_a.users["admin"])
    admin.client = TestClient(
        app, base_url="http://testserver", raise_server_exceptions=False, cookies=admin.client.cookies
    )
    r = upload(
        admin,
        "/data/import/orders?idempotency_key=leak",
        "o.csv",
        ORDER_HDR + b"PO-1,,SKU-1001,Bo\x00lt,1,EA,1,AED\r\n",
    )
    if r.status_code == 500:
        body = r.text
        assert "psycopg" not in body and "Traceback" not in body and "NUL" not in body and "request_id" in body, body
    assert _run_keys(tenant_a.org_id) == set() or r.status_code == 201


def test_sec3_04_connector_zero_tax_is_kept(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    conn = _connector(admin, ["read:orders"])
    run = _sync(
        admin,
        conn,
        "orders",
        "z",
        [{"external_ref": "Z-1", "lines": [{"sku": "A", "quantity": 1, "unit_price": 100, "tax_rate_pct": "0"}]}],
    ).json()
    assert run["applied"] == 1
    o = admin.get("/orders").json()[0]
    assert Decimal(o["grand_total"]) == Decimal("100"), o["grand_total"]
