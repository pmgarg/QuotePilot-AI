"""Security re-verification tests, round 5 (Stage 1 fix round 7, commit bf05198, 2026-11-12).

Adversarial tests for ``money.parse_decimal`` (scientific-notation change) and every caller (catalog import, RFQ
parsing), the round-7 changes to ``scripts/check_compose.py`` and ``scripts/revoke_sessions.py``, and the
server-side provenance rules behind the quote-editor change. Open findings are ``xfail(strict=True)`` with an ID.
See docs/evidence/security-stage1-reverify4-2026-11-12.md.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import time
import zipfile
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from app.services.money import parse_decimal
from tests.conftest import API_DIR, login_as, make_tenant, upload

pytestmark = [pytest.mark.security]
integration = pytest.mark.integration
REPO_ROOT = API_DIR.parents[1]
MAPPING = json.dumps(
    {"sku": "SKU", "description": "Description", "unit": "Unit", "currency": "Currency", "list_price": "List Price"}
)
HEAD = b"SKU,Description,Unit,Currency,List Price\n"


# ---------------------------------------------------------------------------------------------- parse_decimal units
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1e3", Decimal(1000)),
        ("1E+3", Decimal(1000)),
        (" 8.5E2\n", Decimal(850)),
        ("1.e3", Decimal(1000)),
        (".5e1", Decimal(5)),
        ("1e0003", Decimal(1000)),
        ("１e３", Decimal(1000)),  # full-width digits: exact, not misread
        ("١٢e٢", Decimal(1200)),  # Arabic-Indic digits
        ("1e-30", Decimal("1e-30")),
        ("1e30", Decimal("1e30")),
        ("12.50 AED", Decimal("12.50")),
        ("1,234.56", Decimal("1234.56")),
        ("1.234,56", Decimal("1234.56")),
    ],
)
def test_parse_decimal_exact_values(raw: str, expected: Decimal) -> None:
    assert parse_decimal(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "1e31",
        "1e-31",
        "1e999999999",
        "0e1000",
        "Infinity",
        "-Infinity",
        "NaN",
        "sNaN",
        "1e3e3",
        "1,000e2",
        "USD 1e3",
        "1e3 USD",
        "1.5e3,0",
        "1_0e2",
        "1e1.5",
        "1e--3",
        "--5",
        "5-",
        "1.2.3",
        "1,2,3",
        "",
        "   ",
        "e",
        ".",
    ],
)
def test_parse_decimal_rejects_hostile_or_ambiguous_text(raw: str) -> None:
    assert parse_decimal(raw) is None


def test_parse_decimal_is_linear_time_on_hostile_long_input() -> None:
    for s in (
        "1" * 200_000 + "e5",
        "1" * 200_000,
        "1" + "e" * 200_000,
        "1e" * 100_000,
        "." * 200_000 + "e1",
        "0" * 200_000 + "e" + "9" * 200_000,
    ):
        t = time.perf_counter()
        parse_decimal(s)
        assert time.perf_counter() - t < 1.0, s[:20]


def test_parse_decimal_never_returns_non_finite_for_text() -> None:
    for s in ("nan", "inf", "Infinity", "1e9999", "-1e9999", "NaN1", "9e99999999999999999999"):
        d = parse_decimal(s)
        assert d is None or d.is_finite()


def test_parse_decimal_scientific_form_never_silently_reinterpreted() -> None:
    """Pre-round-7 bug: '1e3' -> 13. No exponent text may now yield its digits-with-the-e-removed reading."""
    assert parse_decimal("1e3") != Decimal(13)
    assert parse_decimal("2e5") != Decimal(25)
    assert parse_decimal("5 EUR") == Decimal(5)  # currency code with an E must still parse
    assert parse_decimal("5EUR") == Decimal(5)
    assert parse_decimal("3 E") == Decimal(3)


# ---------------------------------------------------------------------------------------------------------- findings
def test_sec4_0_spaced_or_malformed_exponent_is_rejected_not_misread() -> None:
    """Round-7 only guards `\\d[eE][+-]?\\d`; the letters are then stripped, so '1 e3' / '1e 3' / '1ee3' -> 13."""
    for raw in ("1 e3", "1e 3", "1 E 3", "1ee3", "1e  3", "1e+ 3"):
        assert parse_decimal(raw) in (None, Decimal(1000)), (raw, parse_decimal(raw))


def test_sec4_1_float_non_finite_and_unbounded_inputs_are_rejected() -> None:
    for f in (float("nan"), float("inf"), float("-inf"), 1e300):
        d = parse_decimal(f)
        assert d is None or (d.is_finite() and d.adjusted() <= 30), f


def test_sec4_3_unicode_minus_does_not_flip_sign() -> None:
    d = parse_decimal("−5")
    assert d is None or d == Decimal(-5)


@pytest.mark.xfail(strict=True, reason="SEC4-4 (Low): digits separated by words are concatenated ('12 per 6' -> 126)")
def test_sec4_4_digit_groups_separated_by_words_are_not_concatenated() -> None:
    for raw in ("12 per 6", "10 x 5", "3 boxes of 12", "0x10"):
        assert parse_decimal(raw) in (None, Decimal(raw.split()[0]) if raw[0].isdigit() else None), raw
    assert parse_decimal("12 per 6") != Decimal(126)


def test_sec4_5_negative_zero_is_never_returned() -> None:
    for raw in ("-0", "-0e5", "-0.00"):
        d = parse_decimal(raw)
        assert d is None or not d.is_signed(), raw


# ------------------------------------------------------------------------------------------------- catalog import
def _xlsx_with_raw_cell(raw_value: str) -> bytes:
    """A real XLSX whose list-price cell is a numeric <v> element carrying hostile text (openpyxl uses float())."""
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["SKU", "Description", "Unit", "Currency", "List Price"])
    ws.append(["X1", "Hostile numeric", "EA", "AED", 123456])
    buf = io.BytesIO()
    wb.save(buf)
    src = zipfile.ZipFile(io.BytesIO(buf.getvalue()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                assert b"<v>123456</v>" in data
                data = data.replace(b"<v>123456</v>", f"<v>{raw_value}</v>".encode())
            z.writestr(item, data)
    return out.getvalue()


def _import_csv(admin, rows: bytes):  # type: ignore[no-untyped-def]
    return upload(admin, "/catalogs/import", "c.csv", HEAD + rows, {"mapping": MAPPING, "catalog_name": "Main"})


def _products(admin, version_id: str) -> dict[str, str]:  # type: ignore[no-untyped-def]
    r = admin.get(f"/catalogs/versions/{version_id}/products")
    assert r.status_code == 200, r.text
    return {p["sku"]: p["list_price"] for p in r.json()["items"]}


@integration
def test_import_exponent_and_unicode_digit_prices_exact_or_rejected(app) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("Sec Sci", with_catalog=False)
    admin = login_as(app, t.users["admin"])
    r = _import_csv(
        admin,
        b"A1,a,EA,AED,1e3\nA2,a,EA,AED,\xef\xbc\x91e\xef\xbc\x93\nA3,a,EA,AED,1e999\nA4,a,EA,AED,NaN\n"
        b'A5,a,EA,AED,Infinity\nA6,a,EA,AED,1.5e3 AED\nA7,a,EA,AED,"1,000e2"\nA8,a,EA,AED,-1e3\n'
        b'A9,a,EA,AED,1e-31\nA10,a,EA,AED,"(1e3)"\nA11,a,EA,AED,1e1e1\n',
    )
    assert r.status_code == 201, r.text
    prods = _products(admin, r.json()["id"])
    assert prods.get("A1") == "1000" and prods.get("A2") == "1000"
    for bad in ("A3", "A4", "A5", "A6", "A7", "A8", "A9", "A10", "A11"):
        assert bad not in prods, (bad, prods.get(bad))  # rejected, never invented


@integration
def test_import_negative_and_scientific_negative_prices_are_rejected(app) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("Sec Neg", with_catalog=False)
    admin = login_as(app, t.users["admin"])
    r = _import_csv(admin, b"N1,a,EA,AED,-5\nN2,a,EA,AED,-5e0\nN3,a,EA,AED,(5)\nN4,a,EA,AED,7\n")
    assert r.status_code == 201, r.text
    assert _products(admin, r.json()["id"]) == {"N4": "7"}


_X2 = ()  # SEC4-2 fixed in fix round 8 (marker removed, declared)


@integration
@pytest.mark.parametrize(
    "price",
    [pytest.param(p, marks=_X2) for p in ("1e30", "1" + "0" * 30, "9" * 40, "1e12", "999999999999.9999999")],
)
def test_sec4_2_oversized_prices_never_crash_or_store_garbage(app, price: str) -> None:  # type: ignore[no-untyped-def]
    """Numeric(18,6) holds < 1e12. Larger must be a row error (201 with report), never a 500 or a stored value."""
    t = make_tenant("Sec Big", with_catalog=False)
    admin = login_as(app, t.users["admin"])
    r = _import_csv(admin, f"B1,a,EA,AED,{price}\nB2,a,EA,AED,5\n".encode())
    assert r.status_code in (201, 422), (price, r.status_code, r.text[:200])
    if r.status_code == 201:
        assert "B1" not in _products(admin, r.json()["id"])


# ------------------------------------------------------------------------------------------------------ RFQ parsing
_X6 = ()  # SEC4-6 fixed in fix round 8 (marker removed, declared)


@integration
@pytest.mark.parametrize(
    "qty", [pytest.param(q, marks=_X6) for q in ("1e30", "1" + "0" * 30, "99999999999999.5", "1e12")]
)
def test_sec4_6_oversized_rfq_quantities_never_crash(app, tenant_a, qty: str) -> None:  # type: ignore[no-untyped-def]
    """Member-facing path: any sales user's RFQ upload. rfq_items.quantity is Numeric(18,3)."""
    sales = login_as(app, tenant_a.users["sales"])
    csv = f"SKU,Description,Qty,Unit\nSKU-1001,Hex bolt,{qty},EA\nSKU-1002,Other,5,EA\n".encode()
    r = upload(sales, "/rfqs", "r.csv", csv, {"customer_id": str(tenant_a.customer_id)})
    assert r.status_code in (201, 422), (qty, r.status_code, r.text[:200])
    if r.status_code == 201:
        for it in r.json()["items"]:
            assert it["quantity"] is None or Decimal(it["quantity"]) < Decimal(10) ** 7, it


@integration
def test_rfq_text_exponent_quantities_are_exact_or_unset(app, tenant_a) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    txt = "1e3 pcs SKU-1001 hex bolt\n2E2 x SKU-1002 washer\n12 pcs SKU-1003 nut 1e3\n"
    r = sales.post("/rfqs", json={"text": txt, "customer_id": str(tenant_a.customer_id)})
    assert r.status_code == 201, r.text
    for it in r.json()["items"]:
        assert it["quantity"] in (None, "1000", "200", "12"), it  # never 13 / 22 / 1e3-misread values


@integration
def test_rfq_item_patch_rejects_nonfinite_negative_zero_and_huge_quantity(app, tenant_a) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    rfq = sales.post(
        "/rfqs", json={"text": "5 pcs SKU-1001 hex bolt\n", "customer_id": str(tenant_a.customer_id)}
    ).json()
    iid = rfq["items"][0]["id"]
    for bad in ("NaN", "Infinity", "-Infinity", "-0", "0", "1e999", "1e30", "-1", "1e8", "0.0001"):
        r = sales.patch(f"/rfqs/{rfq['id']}/items/{iid}", json={"quantity": bad})
        assert r.status_code == 422, (bad, r.status_code, r.text[:120])
    assert sales.patch(f"/rfqs/{rfq['id']}/items/{iid}", json={"quantity": "1e3"}).json()["quantity"] == "1000"


@integration
def test_quote_editor_numeric_inputs_reject_nonfinite_and_oversized(app, tenant_a) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    prod = sales.get(f"/catalogs/versions/{tenant_a.catalog_version_id}/products?q=SKU-1001").json()["items"][0]
    q = sales.post("/quotes", json={"customer_id": str(tenant_a.customer_id)}).json()
    for field, bad in [
        ("quantity", "NaN"),
        ("quantity", "Infinity"),
        ("quantity", "-0"),
        ("quantity", "1e30"),
        ("quantity", "0"),
        ("unit_price", "NaN"),
        ("unit_price", "-0.01"),
        ("unit_price", "1e30"),
        ("unit_price", "Infinity"),
        ("discount_pct", "NaN"),
        ("discount_pct", "101"),
        ("tax_rate_pct", "NaN"),
    ]:
        body = {"product_id": prod["id"], "quantity": "1"}
        body[field] = bad
        r = sales.patch(f"/quotes/{q['id']}", json={"expected_lock_version": q["lock_version"], "lines": [body]})
        assert r.status_code in (403, 422), (field, bad, r.status_code, r.text[:150])


# ---------------------------------------------------------- XLSX numeric cells carrying hostile text (float path)
@integration
@pytest.mark.parametrize(
    "raw",
    ["nan", "inf", "-inf", pytest.param("1e300", marks=_X2), pytest.param("1E+400", marks=_X2)],
)
def test_sec4_2_catalog_xlsx_hostile_numeric_cell_is_a_row_error_not_a_500(app, raw: str) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("Sec Xlsx", with_catalog=False)
    admin = login_as(app, t.users["admin"])
    try:
        r = upload(
            admin, "/catalogs/import", "c.xlsx", _xlsx_with_raw_cell(raw), {"mapping": MAPPING, "catalog_name": "Main"}
        )
    except Exception as exc:  # TestClient re-raises unhandled server errors
        pytest.fail(f"unhandled server error for <v>{raw}</v>: {type(exc).__name__}: {str(exc)[:100]}")
    assert r.status_code in (201, 422), (raw, r.status_code)
    if r.status_code == 201:
        assert "X1" not in _products(admin, r.json()["id"])


@integration
def test_sec4_6b_rfq_quantity_above_api_cap_cannot_reach_a_quote(app, tenant_a) -> None:  # type: ignore[no-untyped-def]
    """Whatever the RFQ stores, generate-quote must never carry a quantity above the 10,000,000 cap into a quote."""
    sales = login_as(app, tenant_a.users["sales"])
    csv = b"SKU,Description,Qty,Unit\nSKU-1001,Hex bolt,1e12,EA\n"
    r = upload(sales, "/rfqs", "r.csv", csv, {"customer_id": str(tenant_a.customer_id)})
    if r.status_code != 201:
        return
    g = sales.post(f"/rfqs/{r.json()['id']}/generate-quote")
    if g.status_code == 201:
        for ln in g.json()["current_version"]["lines"]:
            assert ln["quantity"] is None or Decimal(ln["quantity"]) <= Decimal(10_000_000), ln
    else:
        assert g.status_code in (409, 422), g.status_code


# ------------------------------------------- price provenance: the editor cannot mislabel a manual price as catalog
@integration
def test_server_rejects_or_downgrades_manual_price_labelled_as_catalog(app, tenant_a) -> None:  # type: ignore[no-untyped-def]
    """Editor round-7 change sends is_custom:false on re-link. The server, not the UI, owns price_source."""
    sales = login_as(app, tenant_a.users["sales"])
    prod = sales.get(f"/catalogs/versions/{tenant_a.catalog_version_id}/products?q=SKU-1001").json()["items"][0]
    q = sales.post("/quotes", json={"customer_id": str(tenant_a.customer_id)}).json()
    base = {"product_id": prod["id"], "quantity": "10"}
    r = sales.patch(f"/quotes/{q['id']}", json={"expected_lock_version": q["lock_version"], "lines": [base]})
    assert r.status_code == 200, r.text
    q = r.json()
    ln = q["current_version"]["lines"][0]
    list_price = ln["unit_price"]
    for extra in (
        {"unit_price": "0.01"},
        {"unit_price": "0.01", "is_custom": False},
        {"unit_price": "0.01", "is_custom": False, "price_source": "catalog"},
        {"unit_price": "0.01", "is_custom": True},
        {"unit_price": "0.01", "is_custom": True, "price_source": "catalog"},
    ):
        body = {"id": ln["id"], "product_id": ln["product_id"], "quantity": "10", **extra}
        r = sales.patch(f"/quotes/{q['id']}", json={"expected_lock_version": q["lock_version"], "lines": [body]})
        if r.status_code == 200:
            got = r.json()["current_version"]["lines"][0]
            # a sub-list price from a sales user is never accepted as a clean catalog price
            assert not (got["price_source"] == "catalog" and got["unit_price"] != list_price), (extra, got)
            assert got["unit_price"] == list_price or got["price_source"] != "catalog", (extra, got)
            q = r.json()
        else:
            assert r.status_code in (403, 409, 422), (extra, r.status_code, r.text[:160])


@integration
def test_relink_of_custom_line_to_product_reprices_from_catalog_not_from_client(app, tenant_a) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    sales = login_as(app, tenant_a.users["sales"])
    prod = sales.get(f"/catalogs/versions/{tenant_a.catalog_version_id}/products?q=SKU-1001").json()["items"][0]
    q = admin.post("/quotes", json={"customer_id": str(tenant_a.customer_id)}).json()
    r = admin.patch(
        f"/quotes/{q['id']}",
        json={
            "expected_lock_version": q["lock_version"],
            "lines": [{"description": "Manual widget", "quantity": "2", "unit_price": "1.00", "is_custom": True}],
        },
    )
    assert r.status_code == 200, r.text
    q = r.json()
    ln = q["current_version"]["lines"][0]
    assert ln["is_custom"] is True and ln["price_source"] != "catalog"
    # re-link to a catalog product, as the editor now does, while still carrying the manual 1.00
    body = {"id": ln["id"], "product_id": prod["id"], "quantity": "2", "unit_price": "1.00", "is_custom": False}
    r = sales.patch(f"/quotes/{q['id']}", json={"expected_lock_version": q["lock_version"], "lines": [body]})
    if r.status_code == 200:
        got = r.json()["current_version"]["lines"][0]
        assert not (got["price_source"] == "catalog" and got["unit_price"] == "1" and prod["list_price"] != "1"), got
        assert got["price_source"] == "manual", got  # observed: the client price stays labelled manual (not catalog)
    else:
        assert r.status_code in (403, 409, 422)


# ---------------------------------------------------------------------------------- scripts (R4-1 / R4-2 re-check)
def _run_check(tmp: Path, doc: dict) -> subprocess.CompletedProcess[str]:  # type: ignore[type-arg]
    f = tmp / "c.yml"
    f.write_text(yaml.safe_dump(doc))
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_compose.py"), str(f)],
        capture_output=True,
        text=True,
        check=False,
    )


_OK_ENV = {"DATABASE_URL": "postgresql+psycopg://quotepilot_app:x@db/q", "DATABASE_ADMIN_URL": ""}


def test_r4_2_compose_check_passes_clean_topology(tmp_path: Path) -> None:
    doc = {"services": {"api": {"environment": dict(_OK_ENV)}, "web": {"environment": {"A": "b"}}}}
    assert _run_check(tmp_path, doc).returncode == 0


@pytest.mark.parametrize(
    "doc",
    [
        {"services": {}},
        {},
        {"services": None},
        {"services": {"backend": {"environment": dict(_OK_ENV)}, "frontend": {"environment": {}}}},
        {"services": {"api": {"environment": dict(_OK_ENV)}}},  # web missing
        {"services": {"web": {"environment": {}}}},  # api missing
        {"services": {"API": {"environment": dict(_OK_ENV)}, "WEB": {"environment": {}}}},  # case rename
    ],
)
def test_r4_2_compose_check_fails_closed_when_api_or_web_missing_or_renamed(tmp_path: Path, doc: dict) -> None:  # type: ignore[type-arg]
    assert _run_check(tmp_path, doc).returncode != 0


@pytest.mark.parametrize(
    "svc_api",
    [
        {"environment": {"QP_OWNER_PASSWORD": "x"}},
        {"environment": {"POSTGRES_PASSWORD": "x"}},
        {"environment": {"DATABASE_URL": "postgresql+psycopg://quotepilot_owner:pw@db/q"}},
        {"environment": {"X": "postgresql://quotepilot_owner@db"}},
        {"environment": {"DATABASE_ADMIN_URL": "postgresql://o:p@db/q"}},
        {"env_file": [".env"], "environment": dict(_OK_ENV)},
    ],
)
def test_r4_2_compose_check_still_detects_owner_credentials(tmp_path: Path, svc_api: dict) -> None:  # type: ignore[type-arg]
    doc = {"services": {"api": svc_api, "web": {"environment": {}}}}
    assert _run_check(tmp_path, doc).returncode == 1


def test_compose_check_list_form_environment_fails_closed_by_crashing(tmp_path: Path) -> None:
    doc = {"services": {"api": {"environment": ["QP_OWNER_PASSWORD=x"]}, "web": {"environment": {}}}}
    assert _run_check(tmp_path, doc).returncode != 0


def test_compose_render_if_available_matches_check(tmp_path: Path) -> None:
    """Render the real compose file (needs docker CLI + a temporary .env copy); skipped otherwise."""
    import shutil

    if shutil.which("docker") is None:
        pytest.skip("docker CLI unavailable")
    env_dst = tmp_path / ".env"
    env_dst.write_text((REPO_ROOT / ".env.example").read_text())
    r = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(env_dst),
            "-f",
            str(REPO_ROOT / "infra" / "docker-compose.yml"),
            "config",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    if r.returncode != 0:
        pytest.skip(f"docker compose config unavailable: {r.stderr[:100]}")
    (tmp_path / "r.yml").write_text(r.stdout)
    rendered = yaml.safe_load(r.stdout)
    assert {"api", "web"} <= set(rendered["services"])
    assert _run_check(tmp_path, rendered).returncode == 0


def test_revoke_sessions_script_source_has_no_platform_total_in_audit_insert() -> None:
    src = (REPO_ROOT / "scripts" / "revoke_sessions.py").read_text()
    insert = src[src.index("INSERT INTO audit_events") :]
    insert = insert[: insert.index("\n    print(")]
    assert '"total"' not in insert and "res.rowcount" not in insert
    assert re.search(r'"count": n', insert)


@integration
def test_r4_1_recheck_each_tenant_row_holds_only_its_own_count_and_stdout_has_the_total(
    app, tenant_a, tenant_b
) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import create_engine, text

    login_as(app, tenant_a.users["sales"])  # 1 session in A
    for u in ("sales", "owner", "admin"):  # 3 sessions in B
        login_as(app, tenant_b.users[u])

    env = {**os.environ, "DATABASE_ADMIN_URL": os.environ["DATABASE_ADMIN_URL"]}
    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "revoke_sessions.py"), "--all", "--yes"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=API_DIR,
    )
    assert r.returncode == 0, r.stderr[-400:]
    assert "revoked 4 session(s)" in r.stdout  # platform total only on the operator's stdout
    eng = create_engine(os.environ["DATABASE_ADMIN_URL"], future=True)
    try:
        for org, expected in ((tenant_a.org_id, 1), (tenant_b.org_id, 3)):
            with eng.begin() as conn:
                conn.execute(text("SELECT set_config('app.current_org', :o, true)"), {"o": str(org)})
                rows = (
                    conn.execute(text("SELECT summary FROM audit_events WHERE action='ops.sessions_revoked'"))
                    .scalars()
                    .all()
                )
            assert len(rows) == 1, rows
            assert rows[0] == {"scope": "all", "count": expected}, rows[0]
            blob = json.dumps(rows[0])
            assert str(tenant_a.org_id if org == tenant_b.org_id else tenant_b.org_id) not in blob
    finally:
        eng.dispose()


@integration
def test_r4_1_recheck_org_scope_leaves_other_tenant_sessions_and_audit_untouched(app, tenant_a, tenant_b) -> None:  # type: ignore[no-untyped-def]
    a = login_as(app, tenant_a.users["sales"])
    b = login_as(app, tenant_b.users["sales"])
    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "revoke_sessions.py"), "--org", str(tenant_a.org_id), "--yes"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ},
        cwd=API_DIR,
    )
    assert r.returncode == 0, r.stderr[-300:]
    assert a.get("/auth/me").status_code == 401 and b.get("/auth/me").status_code == 200


@integration
def test_sec4_0_catalog_import_spaced_exponent_price_is_not_stored_as_13(app) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("Sec Sp", with_catalog=False)
    admin = login_as(app, t.users["admin"])
    r = _import_csv(admin, b'Z1,a,EA,AED,"1 e3"\nZ2,a,EA,AED,"1ee3"\nZ3,a,EA,AED,5\n')
    assert r.status_code == 201, r.text
    assert _products(admin, r.json()["id"]).get("Z1") in (None, "1000")
