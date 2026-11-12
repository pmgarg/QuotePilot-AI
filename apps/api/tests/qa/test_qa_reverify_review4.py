"""QA (independent) re-verification 4 of Stage 1 at bf05198 (fix round 7).

Derived from docs/evidence/release-review-4-stage1-2026-11-10.md (SEC3-11 follow-up) and PRD S1-F01/S1-F03/X-01
(never invent prices). Unit level (parse_decimal) and integration level (catalog CSV import through the API with
PostgreSQL + RLS). Not browser evidence.
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from app.services.money import parse_decimal
from tests.conftest import login_as, make_tenant, upload
from tests.integration.test_catalog import MAPPING

# --- unit: scientific notation --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1e3", D(1000)),
        ("8.5E2", D(850)),
        ("1E+3", D(1000)),
        ("-1e3", D(-1000)),
        ("1e-3", D("0.001")),
        ("+2e1", D(20)),
        ("1E-2", D("0.01")),
        ("1e0", D(1)),
        (" 1e3 ", D(1000)),
        (".5e1", D(5)),
        ("5.e1", D(50)),
    ],
)
def test_scientific_exact(raw: str, expected: D) -> None:
    got = parse_decimal(raw)
    assert got is not None and got == expected, (raw, got)


@pytest.mark.parametrize(
    "raw",
    [
        "1e31",
        "1e-31",
        "1e999999",
        "1e-999999",
        "1e3 AED",
        "AED 1e3",
        "1e3e",
        "1e3.5",
        "1,5e3",
        "1e1,5",
        "NaN",
        "Infinity",
        "-Infinity",
        "sNaN",
        "1_0e3",
        "0x1e3",
        "1e3 EUR",
        "1e3,5",
        "1.5e3.5",
    ],
)
def test_hostile_or_ambiguous_forms_are_rejected(raw: str) -> None:
    assert parse_decimal(raw) is None, (raw, parse_decimal(raw))


@pytest.mark.parametrize(
    "raw",
    ["1 e3", "1e 3", "1ee3", "1e+", "12e", "e3", "1 E 3", "1e  3"],
)
def test_letter_e_adjacent_junk_must_not_silently_become_a_different_number(raw: str) -> None:
    """Defect candidate (Low): the pure-exponent form is guarded, but whitespace/duplicated-e variants fall through to
    the letter-stripping path and become 13, 1 or 3. A price must never be invented (CLAUDE.md)."""
    assert parse_decimal(raw) is None, (raw, parse_decimal(raw))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.234,56", D("1234.56")),
        ("12.50 AED", D("12.50")),
        ("(12.50)", D("-12.50")),
        ("1,234.56", D("1234.56")),
        ("12,5", D("12.5")),
        ("1234,56", D("1234.56")),
        ("AED 12.50", D("12.50")),
        ("12.50 EUR", D("12.50")),
        ("5 DKK", D(5)),
        ("1,234", D(1234)),
        ("0", D(0)),
        ("-3.5", D("-3.5")),
        ("USD 1,299.99", D("1299.99")),
        ("1.234.567,89", D("1234567.89")),
        (12, D(12)),
        (12.5, D("12.5")),
        (D("3.10"), D("3.10")),
    ],
)
def test_ordinary_formats_unchanged(raw: object, expected: D) -> None:
    assert parse_decimal(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", True, "abc", "-", ".", ","])
def test_non_numbers_none(raw: object) -> None:
    assert parse_decimal(raw) is None


def test_currency_codes_with_e_still_parse() -> None:
    for raw, exp in [
        ("12.50 EUR", D("12.50")),
        ("EUR 12.50", D("12.50")),
        ("1,5 SEK", D("1.5")),
        ("12 GBP", D(12)),
        ("CHF 7", D(7)),
        ("3 JPY", D(3)),
        ("2 BDE", D(2)),
    ]:
        assert parse_decimal(raw) == exp, raw


# --- integration: through catalog import ----------------------------------------------------------------------


def test_import_scientific_and_ordinary_formats_and_hostile_rows(app) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("Sci QA", with_catalog=False)
    admin = login_as(app, t.users["admin"])
    rows = [
        ("A1", "8.5E2"),
        ("A2", "1e3"),
        ("A3", "1.5e-1"),
        ("A4", "1.234,56"),
        ("A5", "12.50 AED"),
        ("A6", "1,234.56"),
        ("A7", "12,5"),
        ("B1", "1e3 AED"),
        ("B2", "1e999999"),
        ("B3", "NaN"),
        ("B4", "Infinity"),
        ("B5", "1e31"),
        ("B6", "1,5e3"),
        ("B7", "1 e3"),
        ("B8", "1ee3"),
    ]
    body = "SKU,Description,Unit,Currency,List Price\n" + "".join(f'{s},Item {s},EA,AED,"{p}"\n' for s, p in rows)
    r = upload(admin, "/catalogs/import", "c.csv", body.encode(), {"mapping": MAPPING, "catalog_name": "Main"})
    assert r.status_code == 201, r.text
    prod = {
        p["sku"]: p["list_price"]
        for p in admin.get(f"/catalogs/versions/{r.json()['id']}/products?limit=200").json()["items"]
    }
    assert prod["A1"] == "850" and prod["A2"] == "1000"
    assert D(prod["A3"]) == D("0.15")
    assert D(prod["A4"]) == D("1234.56") and D(prod["A5"]) == D("12.50")
    assert D(prod["A6"]) == D("1234.56") and D(prod["A7"]) == D("12.5")
    leaked = {k: v for k, v in prod.items() if k.startswith("B")}
    assert leaked == {}, f"hostile/ambiguous prices were imported as numbers: {leaked}"
    rep = r.json()["import_report"]
    assert rep["imported"] == 7 and rep["skipped"] == 8


def test_import_cost_price_scientific_exact(app) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("Sci Cost QA", with_catalog=False)
    admin = login_as(app, t.users["admin"])
    mp = '{"sku":"SKU","description":"Description","unit":"Unit","currency":"Currency","list_price":"List Price","cost_price":"Cost"}'
    body = b"SKU,Description,Unit,Currency,List Price,Cost\nC1,x,EA,AED,10,2.5E1\nC2,y,EA,AED,10,1e3 AED\n"
    r = upload(admin, "/catalogs/import", "c.csv", body, {"mapping": mp, "catalog_name": "Main"})
    assert r.status_code == 201, r.text
    items = {p["sku"]: p for p in admin.get(f"/catalogs/versions/{r.json()['id']}/products").json()["items"]}
    assert D(items["C1"]["cost_price"]) == D(25)
    if "C2" in items:  # if kept, the cost must not be a misparse (13)
        assert items["C2"].get("cost_price") in (None,), items["C2"].get("cost_price")


@pytest.mark.parametrize("price", ["1e12", "1e15", "1e20", "1E+30", "9.99e29"])
def test_import_large_scientific_price_is_a_row_error_not_a_500(app, price: str) -> None:  # type: ignore[no-untyped-def]
    """Round 7 widened what parse_decimal accepts (bound 1e30). A price that the DB column cannot hold must be a row
    error. Defect candidate (Medium for availability): exponents 12..30 reach the DB (SEC4-2 pre-existing path)."""
    t = make_tenant("Sci Big QA", with_catalog=False)
    admin = login_as(app, t.users["admin"])
    body = f"SKU,Description,Unit,Currency,List Price\nBIG,Big,EA,AED,{price}\nOK,Fine,EA,AED,1e3\n".encode()
    try:
        r = upload(admin, "/catalogs/import", "c.csv", body, {"mapping": MAPPING, "catalog_name": "Main"})
    except Exception as exc:  # noqa: BLE001 - the ASGI client re-raises unhandled server errors
        pytest.fail(f"unhandled server error for price {price}: {type(exc).__name__}")
    assert r.status_code == 201, (price, r.status_code, r.text[:200])
    items = {
        p["sku"]: p["list_price"] for p in admin.get(f"/catalogs/versions/{r.json()['id']}/products").json()["items"]
    }
    assert items.get("OK") == "1000" and "BIG" not in items
