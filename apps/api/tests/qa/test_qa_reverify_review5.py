"""QA (independent) re-verification 5 of Stage 1 at 3586da2 (fix round 8).

Derived from CLAUDE.md / PRD ("never invent prices, stock or customer facts"; missing data blocks finalization),
docs/API_CONTRACT.md and docs/evidence/{release-review-4,security-stage1-reverify4}. Scope: git diff bf05198..3586da2.
Unit tests (parse_decimal), integration tests (API + PostgreSQL + RLS through the ASGI TestClient, second DB
connections for commit probes). Not browser evidence.
"""

from __future__ import annotations

import io
import random
import re
import unicodedata
from decimal import Decimal as D
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.services.money import parse_decimal
from tests.conftest import AuthedClient, Tenant, login_as, make_tenant, upload
from tests.integration.test_catalog import MAPPING
from tests.integration.test_quotes import make_quote

integration = pytest.mark.integration

# ---------------------------------------------------------------------------------------------------------------
# 1b. realistic cells: right answer
# ---------------------------------------------------------------------------------------------------------------
RIGHT = [
    ("500", D(500)),
    ("500.00", D(500)),
    ("500,00", D(500)),
    ("1,234", D(1234)),
    ("1,234.56", D("1234.56")),
    ("1.234,56", D("1234.56")),
    ("1,234,567", D(1234567)),
    ("1,234,567.89", D("1234567.89")),
    ("1.234.567,89", D("1234567.89")),
    ("12,5", D("12.5")),
    ("0,5", D("0.5")),
    (".5", D("0.5")),
    ("$.50", D("0.5")),
    ("12.", D(12)),
    ("12. pcs", D(12)),
    ("1 234,56", D("1234.56")),
    ("1 234 567,89", D("1234567.89")),
    ("1 234,56", D("1234.56")),
    ("1 234,56", D("1234.56")),
    ("1'234.56", D("1234.56")),
    ("1'234'567.50", D("1234567.50")),
    ("AED 12.50", D("12.50")),
    ("12.50 AED", D("12.50")),
    ("AED12.50", D("12.50")),
    ("12.50AED", D("12.50")),
    ("$1,299.99", D("1299.99")),
    ("€ 12,50", D("12.5")),
    ("12,50 €", D("12.5")),
    ("£1,000", D(1000)),
    ("¥1000", D(1000)),
    ("Rs. 500", D(500)),
    ("Rs 500", D(500)),
    ("INR 500", D(500)),
    ("US$ 5", D(5)),
    ("USD1,000.00", D(1000)),
    ("1.000,00 EUR", D(1000)),
    ("CHF 1'234.50", D("1234.50")),
    ("1'234.50 CHF", D("1234.50")),
    ("SEK 1 234,56", D("1234.56")),
    ("Fr. 5", D(5)),
    ("(12.50)", D("-12.50")),
    ("-12.50", D("-12.50")),
    ("+12.50", D("12.50")),
    ("- 12.50", D("-12.50")),
    ("−12.50", D("-12.50")),
    ("− 12", D(-12)),
    ("−" + "5", D(-5)),
    ("（" + "5" + "）", D(-5)),
    ("５００", D(500)),
    ("٣٥٠", D(350)),
    ("٣٫٥", D("3.5")),
    ("1٬234٫5", D("1234.5")),
    ("10%", D(10)),
    ("10 %", D(10)),
    ("12.5%", D("12.5")),
    ("12 pcs", D(12)),
    ("12 PCS", D(12)),
    ("12pcs", D(12)),
    ("12 ea", D(12)),
    ("12 EA.", D(12)),
    ("12 m", D(12)),
    ("12 Nos", D(12)),
    ("12 nos.", D(12)),
    ("12 ft", D(12)),
    ("1.5 mtr", D("1.5")),
    ("1.5 m.", D("1.5")),
    ("5 pcs.", D(5)),
    ("3.5 kg", D("3.5")),
    ("0,5 kg", D("0.5")),
    ("kg 3.5", D("3.5")),
    ("Qty: 12", D(12)),
    ("Qty 12", D(12)),
    ("qty:12", D(12)),
    ("No. 12", D(12)),
    ("1,000 pcs", D(1000)),
    ("5 USD each", D(5)),
    ("00500", D(500)),
    ("1 000", D(1000)),
    ("12 345", D(12345)),
    ("123 456", D(123456)),
    ("1 000,5", D("1000.5")),
    ("0", D(0)),
    ("0.00", D(0)),
    ("1e3", D(1000)),
    ("8.5E2", D(850)),
    ("AED 1,234.567", D("1234.567")),
    ("12,345.678", D("12345.678")),
    ("12.345,678", D("12345.678")),
    ("1,00,000.50", D("100000.50")),
    (12, D(12)),
    (12.5, D("12.5")),
    (D("3.10"), D("3.10")),
    ("\t5\t", D(5)),
    ("5\r\n", D(5)),
    ("﻿5", D(5)),
]


@pytest.mark.parametrize(("raw", "expected"), RIGHT, ids=[repr(r) for r, _ in RIGHT])
def test_realistic_cells_keep_their_obvious_meaning(raw: object, expected: D) -> None:
    got = parse_decimal(raw)
    assert got is not None and got == expected, (raw, got)


# safe rejections: ambiguous or multi-number cells must be None (never a guessed number)
REJECT = [
    "Rs.500",
    "AED 1.5e3",
    "Rs.5",
    "Rs.1,234",
    "Fr.5.50",
    "Ksh.500",
    "Dhs.500",
    "AED.500",
    "12 per 6",
    "12/6",
    "12 / 6",
    "12 x 6",
    "12x6",
    "1 x 5",
    "2 x 3.5",
    "10 to 20",
    "10-20",
    "5 or 10",
    "5 pcs @ 3",
    "5 pcs at 3",
    "10+5",
    "10 + 5",
    "10 +5",
    "10 + 1 free",
    "1 e3",
    "1e 3",
    "1ee3",
    "12e",
    "e3",
    "3E",
    "12 3456",
    "1234 567",
    "1 2",
    "1 2 3",
    "250 0",
    "25 00",
    "1 234 5",
    "12 345 6",
    "1234 567 890",
    "0x10",
    "0b1",
    "1_000",
    "=1+1",
    "'5",
    "5+",
    "5++",
    "+-5",
    "--5",
    "((5))",
    "(5",
    "5)",
    "(12.50) AED",
    "1/2",
    "1 1/2",
    "12 12",
    "12 12",
    "12 34 AED",
    "N/A",
    "n/a",
    "-",
    "TBD",
    "POA",
    "",
    "   ",
    "abc",
    "NaN",
    "Infinity",
    "-Infinity",
    "inf",
    "1e31",
    "1e-31",
    "1e999999",
    "1,5e3",
    "1e3 AED",
    "1.234.5",
    "1.2.3",
    "12..5",
    "1,,234",
    "1,23,4",
    "99.9.9",
    "5,-",
    "5.-",
    "500/-",
    "12.50-",
    "1,00,000",
    "₹ 1,00,000",
    "12 (approx)",
    "12,",
    "12'",
    "1½",
    "12 m²",
    "12 m2",
    "1.234.567",
    "Rs.500.50",
]


@pytest.mark.parametrize("raw", REJECT, ids=[repr(r) for r in REJECT])
def test_ambiguous_or_multi_number_cells_are_rejected_not_guessed(raw: str) -> None:
    assert parse_decimal(raw) is None, (raw, parse_decimal(raw))


def test_negative_zero_and_non_finite_and_bounds() -> None:
    assert str(parse_decimal("-0")) == "0" and str(parse_decimal("(0)")) == "0"
    assert parse_decimal(float("nan")) is None and parse_decimal(float("inf")) is None
    assert parse_decimal(D("NaN")) is None and parse_decimal(D("1e40")) is None
    assert parse_decimal(10**31) is None and parse_decimal(True) is None
    assert parse_decimal("0e1000") is None


# --- defect probes (failing tests = defects) -------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["0,001", "0,500", "0,125", "0,999"])
def test_d5_1_leading_zero_comma_with_three_decimals_is_a_fraction_not_thousands(raw: str) -> None:
    """Defect D5-1 (Low, pre-existing since bf05198 and earlier): '0,500' -> 500 and '0,001' -> 1. Nobody writes a
    thousands group after a leading zero; a unit price of '0,500 AED' becomes 1000x too high. Expected: 0.5/0.001
    (or None). Actual: 500 / 1. money.py `len(parts[1]) == 3 and len(parts[0]) <= 3` branch."""
    got = parse_decimal(raw)
    assert got is None or got < 1, (raw, got)


@pytest.mark.parametrize("raw", ["..5", "...5", "..50"])
def test_d5_2_doubled_leading_dot_is_a_typo_and_must_not_be_accepted(raw: str) -> None:
    """Defect D5-2 (Info/Low, new in round 8): '..5' was None at bf05198 and is now 0.5 (head '.' is accepted as a
    'currency text'). A typo should be reported."""
    assert parse_decimal(raw) is None, (raw, parse_decimal(raw))


# --- property: no number is ever invented from fragments -------------------------------------------------------

_TOKENS = [
    "1",
    "2",
    "5",
    "0",
    "12",
    "345",
    "6",
    ".",
    ",",
    " ",
    " ",
    "'",
    "-",
    "+",
    "(",
    ")",
    "e",
    "E",
    "x",
    "pcs",
    "AED",
    "Rs",
    "$",
    "%",
    "/",
    "@",
    "a",
    "−",
    "٣",
    "５",
    "k",
    "m",
]


def _digits(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    return "".join(str(unicodedata.digit(c)) for c in s if c.isdecimal())


def test_property_fuzz_never_raises_and_never_changes_the_digit_sequence() -> None:
    rnd = random.Random(20261112)
    bad: list[tuple[str, Any]] = []
    for _ in range(60000):
        s = "".join(rnd.choice(_TOKENS) for _ in range(rnd.randint(1, 8)))
        got = parse_decimal(s)  # must never raise
        if got is None:
            continue
        assert got.is_finite() and abs(got) < D(10) ** 31, (s, got)
        if re.search(r"\d\.?[eE][+-]?\d", unicodedata.normalize("NFKC", s)):
            continue  # exponent notation legitimately changes the digits
        sign, dg, exp = got.as_tuple()
        mine = "".join(map(str, dg)).strip("0")
        want = _digits(s).strip("0")
        if mine != want:
            bad.append((s, got))
    assert not bad, f"digits invented or lost for {len(bad)} cells, e.g. {bad[:8]}"


# ---------------------------------------------------------------------------------------------------------------
# 1c. through the API
# ---------------------------------------------------------------------------------------------------------------
CATALOG_CELLS = [
    ("C01", "1.234,56", "1234.56"),
    ("C02", "12.50 AED", "12.5"),
    ("C03", "(12.50)", None),
    ("C04", "1 234,56", "1234.56"),
    ("C05", "€ 12,50", "12.5"),
    ("C06", "Rs. 500", "500"),
    ("C07", "1'234.50", "1234.5"),
    ("C08", "٣٫٥", "3.5"),
    ("C09", "8.5E2", "850"),
    ("C10", "AED 1,299.99", "1299.99"),
    ("R01", "Rs.500", None),
    ("R02", "12 per 6", None),
    ("R03", "1.234.567", None),
    ("R04", "1 e3", None),
    ("R05", "10 to 20", None),
    ("R06", "N/A", None),
    ("R07", "1e12", None),
    ("R08", "999999999999.9999999", None),
    ("R09", "1e30", None),
    ("R10", "-5", None),
    ("R11", "TBD", None),
    ("R12", "", None),
    ("R13", "99999999999999999999", None),
]


def _csv_catalog(admin: AuthedClient, cells: list[tuple[str, str, Any]]) -> Any:
    body = "SKU,Description,Unit,Currency,List Price\n" + "".join(
        f'{sku},Item {sku},EA,AED,"{cell}"\n' for sku, cell, _ in cells
    )
    return upload(admin, "/catalogs/import", "c.csv", body.encode(), {"mapping": MAPPING, "catalog_name": "Main"})


def _products(admin: AuthedClient, version_id: str) -> dict[str, str]:
    items = admin.get(f"/catalogs/versions/{version_id}/products?limit=200").json()["items"]
    return {p["sku"]: p["list_price"] for p in items}


@integration
def test_catalog_csv_import_realistic_and_hostile_cells(app) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("R5 Csv", with_catalog=False)
    admin = login_as(app, t.users["admin"])
    r = _csv_catalog(admin, CATALOG_CELLS)
    assert r.status_code == 201, r.text
    prods = _products(admin, r.json()["id"])
    for sku, cell, expected in CATALOG_CELLS:
        if sku == "C03":  # an accounting negative price is a row error, not a positive price
            assert sku not in prods, (cell, prods.get(sku))
        elif expected is None:
            assert sku not in prods, (sku, cell, prods.get(sku))
        else:
            assert D(prods[sku]) == D(expected), (sku, cell, prods.get(sku))
    rep = r.json()["import_report"]
    assert rep["imported"] == 9 and rep["skipped"] == len(CATALOG_CELLS) - 9, rep
    assert any("too large" in e["message"] for e in rep["errors"])


def _xlsx(rows: list[list[Any]]) -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@integration
def test_catalog_xlsx_import_numeric_and_text_cells(app) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("R5 Xlsx", with_catalog=False)
    admin = login_as(app, t.users["admin"])
    rows: list[list[Any]] = [["SKU", "Description", "Unit", "Currency", "List Price"]]
    rows += [
        ["X01", "num float", "EA", "AED", 12.5],
        ["X02", "num int", "EA", "AED", 1000],
        ["X03", "text eu", "EA", "AED", "1.234,56"],
        ["X04", "text ccy", "EA", "AED", "12.50 AED"],
        ["X05", "text sci", "EA", "AED", "8.5E2"],
        ["X06", "float 0.1+0.2", "EA", "AED", 0.1 + 0.2],
        ["X07", "Decimal-ish", "EA", "AED", 19.99],
        ["Y01", "huge float", "EA", "AED", 1e300],
        ["Y02", "huge int", "EA", "AED", 10**25],
        ["Y03", "text rs", "EA", "AED", "Rs.500"],
        ["Y04", "text per", "EA", "AED", "12 per 6"],
        ["Y05", "nan text", "EA", "AED", "nan"],
        ["Y06", "neg", "EA", "AED", -3],
        ["Y07", "bool", "EA", "AED", True],
        ["Y08", "1e12", "EA", "AED", 1e12],
        ["Y09", "tiny", "EA", "AED", 1e-9],
    ]
    try:
        r = upload(admin, "/catalogs/import", "c.xlsx", _xlsx(rows), {"mapping": MAPPING, "catalog_name": "Main"})
    except Exception as exc:  # the ASGI client re-raises unhandled server errors
        pytest.fail(f"unhandled server error on xlsx import: {type(exc).__name__}: {str(exc)[:120]}")
    assert r.status_code == 201, r.text
    prods = _products(admin, r.json()["id"])
    assert D(prods["X01"]) == D("12.5") and D(prods["X02"]) == D(1000) and D(prods["X03"]) == D("1234.56")
    assert D(prods["X04"]) == D("12.5") and D(prods["X05"]) == D(850)
    assert D(prods["X07"]) == D("19.99")
    assert D(prods["X06"]) == D("0.3"), prods["X06"]  # repr(0.30000000000000004) rounds to 6 places: 0.300000
    for sku in ("Y01", "Y02", "Y03", "Y04", "Y05", "Y06", "Y07", "Y08"):
        assert sku not in prods, (sku, prods.get(sku))


def _rfq_items(r: Any) -> list[dict[str, Any]]:
    assert r.status_code == 201, r.text
    return r.json()["items"]  # type: ignore[no-any-return]


@integration
def test_rfq_text_realistic_quantities(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    txt = (
        "1,000 pcs SKU-1001 Hex bolt M8x40 zinc plated\n"
        "12.5 kg SKU-1002 Washer M8 flat zinc\n"
        "2 x SKU-1003 Hex nut M8 zinc plated\n"
        "5 pcs SKU-3002 Lithium grease EP2 pail 18kg\n"
    )
    r = sales.post("/rfqs", json={"text": txt, "customer_id": str(tenant_a.customer_id)})
    items = _rfq_items(r)
    qty = {i["line_no"]: i["quantity"] for i in items}
    assert D(qty[1]) == D(1000) and D(qty[2]) == D("12.5") and D(qty[3]) == D(2) and D(qty[4]) == D(5), qty
    for i in items:  # never a quantity assembled from two numbers
        assert i["quantity"] is None or D(i["quantity"]) in (D(1000), D("12.5"), D(2), D(5)), i


@integration
def test_rfq_csv_and_xlsx_hostile_quantity_cells_are_flagged_not_guessed(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    cells = ["12 per 6", "1 e3", "Rs.500", "10 to 20", "1,000", "12 pcs", "N/A", "1.234,5", "(5)", "-5", "0", "2k"]
    csv = "SKU,Description,Qty,Unit\n" + "".join(f'SKU-1001,Hex bolt {i},"{c}",EA\n' for i, c in enumerate(cells))
    items = _rfq_items(upload(sales, "/rfqs", "r.csv", csv.encode(), {"customer_id": str(tenant_a.customer_id)}))
    got = {c: (i["quantity"], i["flags"]) for c, i in zip(cells, items, strict=True)}
    for c in ("12 per 6", "1 e3", "Rs.500", "10 to 20", "N/A"):
        assert got[c][0] is None and "quantity_unreadable" in got[c][1], (c, got[c])
    assert D(got["1,000"][0]) == 1000 and D(got["12 pcs"][0]) == 12 and D(got["1.234,5"][0]) == D("1234.5")
    for c in ("(5)", "-5", "0"):
        assert got[c][0] is None and "quantity_not_positive" in got[c][1], (c, got[c])
    # xlsx: float, int and text cells
    rows: list[list[Any]] = [["SKU", "Description", "Qty", "Unit"]]
    rows += [
        ["SKU-1001", "a", 12.5, "EA"],
        ["SKU-1001", "b", 1000, "EA"],
        ["SKU-1001", "c", "1 234", "EA"],
        ["SKU-1001", "d", 1e30, "EA"],
        ["SKU-1001", "e", 10**20, "EA"],
        ["SKU-1001", "f", float("inf"), "EA"],
    ]
    try:
        r = upload(sales, "/rfqs", "r.xlsx", _xlsx(rows), {"customer_id": str(tenant_a.customer_id)})
    except Exception as exc:
        pytest.fail(f"unhandled server error on xlsx RFQ: {type(exc).__name__}: {str(exc)[:120]}")
    xi = _rfq_items(r)
    q = [i["quantity"] for i in xi]
    assert D(q[0]) == D("12.5") and D(q[1]) == 1000 and D(q[2]) == 1234
    assert (
        q[3] is None
        and q[4] is None
        and "quantity_out_of_range" in xi[3]["flags"]
        and "quantity_out_of_range" in xi[4]["flags"]
    )
    assert q[5] is None


@integration
def test_rfq_pdf_text_quantities_if_pdf_can_be_built(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 780
    for line in [
        "RFQ",
        "1,000 pcs SKU-1001 Hex bolt M8x40 zinc plated",
        "99999999999999999999 pcs SKU-1002 Washer M8 flat zinc",
        "7 pcs SKU-1003 Hex nut M8 zinc plated",
    ]:
        c.drawString(50, y, line)
        y -= 20
    c.save()
    sales = login_as(app, tenant_a.users["sales"])
    r = upload(sales, "/rfqs", "r.pdf", buf.getvalue(), {"customer_id": str(tenant_a.customer_id)}, "application/pdf")
    assert r.status_code in (201, 422), r.text[:200]
    if r.status_code == 201:
        for it in r.json()["items"]:
            assert it["quantity"] is None or D(it["quantity"]) in (D(1000), D(7)), it


# ---------------------------------------------------------------------------------------------------------------
# 2. quantity cap boundaries, flags, generate-quote gate
# ---------------------------------------------------------------------------------------------------------------
BOUNDARY = [
    ("10000000", "10000000.000", None),
    ("9999999.999", "9999999.999", None),
    ("10000000.001", None, "quantity_out_of_range"),
    ("10000000.0004", None, "quantity_out_of_range"),
    ("10000001", None, "quantity_out_of_range"),
    ("1e7", "10000000.000", None),
    ("1e8", None, "quantity_out_of_range"),
    ("0.0005", "0.001", None),
    ("0.0004", None, "quantity_not_positive"),
    ("0.001", "0.001", None),
    ("1.2345", "1.235", None),
    ("1.2344", "1.234", None),
    ("0", None, "quantity_not_positive"),
    ("-1", None, "quantity_not_positive"),
    ("0.00", None, "quantity_not_positive"),
    ("(3)", None, "quantity_not_positive"),
]


@integration
@pytest.mark.parametrize(("cell", "stored", "flag"), BOUNDARY, ids=[b[0] for b in BOUNDARY])
def test_rfq_quantity_boundaries_csv(app, tenant_a: Tenant, cell: str, stored: str | None, flag: str | None) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    csv = f'SKU,Description,Qty,Unit\nSKU-1001,Hex bolt M8x40 zinc plated,"{cell}",EA\n'.encode()
    it = _rfq_items(upload(sales, "/rfqs", "r.csv", csv, {"customer_id": str(tenant_a.customer_id)}))[0]
    if stored is None:
        assert it["quantity"] is None, (cell, it["quantity"])
    else:
        assert D(it["quantity"]) == D(stored), (cell, it["quantity"])
    if flag:
        assert flag in it["flags"], (cell, it["flags"])
    else:
        assert not any(f.startswith("quantity_") for f in it["flags"]), (cell, it["flags"])


@integration
@pytest.mark.parametrize(
    ("cell", "stored", "flag"), [b for b in BOUNDARY if b[0] not in ("(3)", "-1", "0.00", "1e7", "1e8")]
)
def test_rfq_quantity_boundaries_text(app, tenant_a: Tenant, cell: str, stored: str | None, flag: str | None) -> None:  # type: ignore[no-untyped-def]
    """Pasted text path (QTY_LEAD); same boundary rules."""
    sales = login_as(app, tenant_a.users["sales"])
    r = sales.post(
        "/rfqs",
        json={"text": f"{cell} pcs SKU-1001 Hex bolt M8x40 zinc plated\n", "customer_id": str(tenant_a.customer_id)},
    )
    items = _rfq_items(r)
    assert items, r.text
    it = items[0]
    if stored is not None:
        assert it["quantity"] is not None and D(it["quantity"]) == D(stored), (cell, it["quantity"], it["flags"])
    else:
        assert it["quantity"] is None, (cell, it["quantity"])
        if flag:
            assert flag in it["flags"] or "quantity_missing" in it["flags"], (cell, it["flags"])


@integration
def test_out_of_range_quantity_blocks_generate_quote_until_a_person_enters_one(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    csv = (
        b"SKU,Description,Qty,Unit\nSKU-1001,Hex bolt M8x40 zinc plated,1e12,EA\nSKU-1003,Hex nut M8 zinc plated,5,EA\n"
    )
    rfq = upload(sales, "/rfqs", "r.csv", csv, {"customer_id": str(tenant_a.customer_id)}).json()
    it = rfq["items"][0]
    assert it["quantity"] is None and "quantity_out_of_range" in it["flags"]
    for item in rfq["items"]:  # accept the best catalog candidate so only the quantity can block
        if item["candidates"] and not item["selected_product_id"]:
            sales.patch(
                f"/rfqs/{rfq['id']}/items/{item['id']}",
                json={"selected_product_id": item["candidates"][0]["product_id"]},
            )
    g = sales.post(f"/rfqs/{rfq['id']}/generate-quote")
    assert g.status_code in (409, 422), (g.status_code, g.text[:300])
    assert "quantity" in g.text.lower()
    # a person enters a quantity: flag clears, generation succeeds, line carries exactly that quantity
    p = sales.patch(f"/rfqs/{rfq['id']}/items/{it['id']}", json={"quantity": "250"})
    assert p.status_code == 200, p.text
    assert D(p.json()["quantity"]) == 250 and not any(f.startswith("quantity_") for f in p.json()["flags"])
    g2 = sales.post(f"/rfqs/{rfq['id']}/generate-quote")
    assert g2.status_code == 201, g2.text
    qtys = sorted(D(ln["quantity"]) for ln in g2.json()["current_version"]["lines"])
    assert qtys == [D(5), D(250)], qtys
    # manual entry above the cap stays rejected
    r2 = upload(sales, "/rfqs", "r.csv", csv, {"customer_id": str(tenant_a.customer_id)}).json()
    assert (
        sales.patch(f"/rfqs/{r2['id']}/items/{r2['items'][0]['id']}", json={"quantity": "10000001"}).status_code == 422
    )
    assert (
        sales.patch(f"/rfqs/{r2['id']}/items/{r2['items'][0]['id']}", json={"quantity": "10000000"}).status_code == 200
    )


@integration
def test_stored_out_of_range_quantity_in_db_is_treated_as_missing_by_generate_quote(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """Defence in depth (quotes.py:338/387): a legacy rfq_item row above the cap (written by the owner role, as
    rows from before round 8 could be) must still block generation and never be copied into a quote line."""
    sales = login_as(app, tenant_a.users["sales"])
    rfq = sales.post(
        "/rfqs", json={"text": "5 pcs SKU-1001 Hex bolt M8x40 zinc plated\n", "customer_id": str(tenant_a.customer_id)}
    ).json()
    admin_url = _admin_url()
    eng = create_engine(admin_url)
    with eng.begin() as c:
        c.execute(text("SELECT set_config('app.current_org', :o, true)"), {"o": str(tenant_a.org_id)})
        c.execute(text("UPDATE rfq_items SET quantity = 50000000 WHERE rfq_id = :r"), {"r": rfq["id"]})
    eng.dispose()
    g = sales.post(f"/rfqs/{rfq['id']}/generate-quote")
    assert g.status_code in (409, 422), (g.status_code, g.text[:300])


def _admin_url() -> str:
    import os

    # tests/conftest.py resolves DATABASE_ADMIN_URL_TEST (or its own default) into DATABASE_ADMIN_URL at import time,
    # so this is the owner URL exactly as the suite uses it, whether or not the _TEST variable is exported (CI).
    return os.environ["DATABASE_ADMIN_URL"]


# ---------------------------------------------------------------------------------------------------------------
# 3. D5: commit before the response is sent
# ---------------------------------------------------------------------------------------------------------------


class Probe:
    """ASGI wrapper: calls ``fn(scope, status)`` immediately before the first response message goes out."""

    def __init__(self, inner: Any, fn: Any) -> None:
        self.inner, self.fn = inner, fn

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)

        async def s(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                self.fn(scope, message["status"])
            await send(message)

        return await self.inner(scope, receive, s)


def _count(org: Any, sql: str, **params: Any) -> int:
    """Committed rows only, through a separate connection (owner role is also subject to FORCE RLS: set tenant)."""
    eng = create_engine(_admin_url())
    try:
        with eng.connect() as c:
            c.execute(text("SELECT set_config('app.current_org', :o, false)"), {"o": str(org or "")})
            return int(c.execute(text(sql), params).scalar() or 0)
    finally:
        eng.dispose()


def _audit(org: Any, action: str) -> int:
    return _count(org, "SELECT count(*) FROM audit_events WHERE action = :a", a=action)


def _probed(app, email: str, fn: Any) -> AuthedClient:  # type: ignore[no-untyped-def]
    return AuthedClient(TestClient(Probe(app, fn), base_url="http://testserver"), email)


@integration
def test_d5_probe_from_second_connection_at_response_start_for_many_endpoints(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    org = tenant_a.org_id
    snap: dict[str, tuple[int, int, int]] = {}  # key -> (status, data_rows, audit_rows) at response start

    def fn(scope: dict[str, Any], status: int) -> None:
        m, p = scope["method"], scope["path"]
        if m == "POST" and p.endswith("/customers"):
            snap["customer"] = (
                status,
                _count(org, "SELECT count(*) FROM customers WHERE name='D5 Probe Co'"),
                _audit(org, "customer.create"),
            )
        elif m == "POST" and p.endswith("/submit"):
            snap["submit"] = (
                status,
                _count(org, "SELECT count(*) FROM quote_versions WHERE status='pending_approval'"),
                _audit(org, "quote.submit"),
            )
        elif m == "POST" and p.endswith("/approve"):
            snap["approve"] = (
                status,
                _count(org, "SELECT count(*) FROM quote_versions WHERE status='approved'"),
                _audit(org, "quote.approve"),
            )
        elif m == "POST" and p.endswith("/pdf"):
            snap["pdf_post"] = (
                status,
                _count(org, "SELECT count(*) FROM uploaded_files WHERE kind IS NOT NULL OR true"),
                _audit(org, "quote.export_pdf"),
            )
        elif m == "GET" and p.endswith("/pdf"):
            snap["pdf_get"] = (status, 0, _audit(org, "quote.export_pdf"))
        elif m == "GET" and "/files/" in p:
            snap["file"] = (status, 0, _audit(org, "file.download"))
        elif m == "POST" and p.endswith("/auth/logout"):
            snap["logout"] = (status, 0, _audit(org, "auth.logout"))
        elif m == "POST" and p.endswith("/auth/login"):
            snap["login"] = (
                status,
                _count(None, "SELECT count(*) FROM sessions"),
                _count(org, "SELECT count(*) FROM audit_events WHERE action='auth.login'"),
            )

    base_sessions = _count(None, "SELECT count(*) FROM sessions")
    sales = _probed(app, tenant_a.users["sales"], fn)
    assert snap["login"][0] == 200 and snap["login"][1] == base_sessions + 1, (snap["login"], base_sessions)
    assert snap["login"][2] >= 1
    r = sales.post("/customers", json={"name": "D5 Probe Co"})
    assert r.status_code == 201 and snap["customer"][:2] == (201, 1) and snap["customer"][2] >= 1, snap["customer"]
    q = make_quote(
        login_as(app, tenant_a.users["sales"]),
        str(tenant_a.customer_id),
        "10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n",
    )
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]})
    assert s.status_code == 200, s.text
    assert snap["submit"][1] >= 1 and snap["submit"][2] >= 1, snap["submit"]
    approver = _probed(app, tenant_a.users["approver"], fn)
    lv = approver.get(f"/quotes/{q['id']}").json()["lock_version"]
    a = approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": lv, "reason": None})
    assert a.status_code == 200, a.text
    assert snap["approve"][1] >= 1 and snap["approve"][2] >= 1, snap["approve"]
    before_pdf = _audit(org, "quote.export_pdf")
    p1 = approver.post(f"/quotes/{q['id']}/versions/1/pdf")
    assert p1.status_code == 200, p1.text
    assert snap["pdf_post"][2] == before_pdf + 1, (snap["pdf_post"], before_pdf)
    p2 = approver.get(f"/quotes/{q['id']}/versions/1/pdf")
    assert p2.status_code == 200 and p2.content[:4] == b"%PDF"
    assert snap["pdf_get"][2] == before_pdf + 2, (snap["pdf_get"], before_pdf)
    # file download (StreamingResponse): use an uploaded RFQ file
    rfq = upload(
        sales,
        "/rfqs",
        "r.csv",
        b"SKU,Description,Qty,Unit\nSKU-1001,Hex bolt,5,EA\n",
        {"customer_id": str(tenant_a.customer_id)},
    ).json()
    fid = rfq.get("source_file_id") or rfq.get("file_id") or (rfq.get("file") or {}).get("id")
    if fid is None:
        pytest.skip("RFQ payload exposes no file id to download")
    before_dl = _audit(org, "file.download")
    d = sales.get(f"/files/{fid}")
    assert d.status_code == 200 and d.content.startswith(b"SKU,Description")
    assert snap["file"][2] == before_dl + 1, (snap["file"], before_dl)
    before_lo = _audit(org, "auth.logout")
    lo = sales.post("/auth/logout")
    assert lo.status_code == 200
    assert snap["logout"][2] == before_lo + 1, (snap["logout"], before_lo)
    assert sales.get("/auth/me").status_code == 401  # the session deletion was committed too


@integration
def test_d5_failing_commit_is_an_error_not_a_2xx_and_leaves_nothing_behind(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """A deferred constraint trigger raises only at COMMIT (inside get_db). The client must not get a 2xx."""
    eng = create_engine(_admin_url())
    with eng.begin() as c:
        c.execute(
            text(
                "CREATE OR REPLACE FUNCTION qa5_boom() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                "IF NEW.name = 'Boom At Commit' THEN RAISE EXCEPTION 'qa5 commit failure'; END IF; RETURN NEW; END $$"
            )
        )
        c.execute(text("DROP TRIGGER IF EXISTS qa5_boom_t ON customers"))
        c.execute(
            text(
                "CREATE CONSTRAINT TRIGGER qa5_boom_t AFTER INSERT ON customers DEFERRABLE INITIALLY DEFERRED "
                "FOR EACH ROW EXECUTE FUNCTION qa5_boom()"
            )
        )
    try:
        ac = AuthedClient(
            TestClient(app, base_url="http://testserver", raise_server_exceptions=False), tenant_a.users["sales"]
        )
        n_before = _count(tenant_a.org_id, "SELECT count(*) FROM customers")
        a_before = _audit(tenant_a.org_id, "customer.create")
        r = ac.post("/customers", json={"name": "Boom At Commit"})
        assert r.status_code >= 500, (r.status_code, r.text[:200])  # NOT 2xx
        assert _count(tenant_a.org_id, "SELECT count(*) FROM customers") == n_before
        assert _audit(tenant_a.org_id, "customer.create") == a_before  # audit row rolled back with the data
        # the same session still works afterwards (connection not poisoned)
        ok = ac.post("/customers", json={"name": "After Boom"})
        assert ok.status_code == 201, ok.text
        assert _count(tenant_a.org_id, "SELECT count(*) FROM customers WHERE name='After Boom'") == 1
    finally:
        with eng.begin() as c:
            c.execute(text("DROP TRIGGER IF EXISTS qa5_boom_t ON customers"))
            c.execute(text("DROP FUNCTION IF EXISTS qa5_boom()"))
        eng.dispose()


@integration
def test_d5_error_raised_inside_endpoint_rolls_back_data_and_audit(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """An immediate trigger raises in the middle of the endpoint (after the customer row was flushed)."""
    eng = create_engine(_admin_url())
    with eng.begin() as c:
        c.execute(
            text(
                "CREATE OR REPLACE FUNCTION qa5_boom2() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                "IF NEW.action = 'customer.create' AND NEW.summary::text LIKE '%Boom Audit%' THEN RAISE EXCEPTION 'qa5 audit failure'; END IF; RETURN NEW; END $$"
            )
        )
        c.execute(text("DROP TRIGGER IF EXISTS qa5_boom2_t ON audit_events"))
        c.execute(
            text("CREATE TRIGGER qa5_boom2_t BEFORE INSERT ON audit_events FOR EACH ROW EXECUTE FUNCTION qa5_boom2()")
        )
    try:
        ac = AuthedClient(
            TestClient(app, base_url="http://testserver", raise_server_exceptions=False), tenant_a.users["sales"]
        )
        r = ac.post("/customers", json={"name": "Boom Audit Co"})
        assert r.status_code >= 500, r.status_code
        assert _count(tenant_a.org_id, "SELECT count(*) FROM customers WHERE name='Boom Audit Co'") == 0
        # 4xx inside the endpoint: nothing persisted
        before = _count(tenant_a.org_id, "SELECT count(*) FROM customers")
        assert ac.post("/customers", json={"name": ""}).status_code == 422
        assert ac.patch("/customers/00000000-0000-0000-0000-000000000000", json={"name": "x"}).status_code == 404
        assert _count(tenant_a.org_id, "SELECT count(*) FROM customers") == before
    finally:
        with eng.begin() as c:
            c.execute(text("DROP TRIGGER IF EXISTS qa5_boom2_t ON audit_events"))
            c.execute(text("DROP FUNCTION IF EXISTS qa5_boom2()"))
        eng.dispose()


@integration
def test_d5_failed_login_still_records_its_audit_row_and_lockout_counts(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """services/auth.py commits the failure audit row itself before raising 401 (organization_id NULL, invisible
    to tenants under RLS), so a side table filled by a trigger is the witness. The function-scoped dependency must
    neither undo that row nor turn the 401 into another status."""
    eng = create_engine(_admin_url())
    with eng.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS qa5_seen"))
        c.execute(text("CREATE TABLE qa5_seen (action text)"))
        c.execute(text("GRANT INSERT, SELECT ON qa5_seen TO quotepilot_app"))
        c.execute(
            text(
                "CREATE OR REPLACE FUNCTION qa5_seen_f() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN INSERT INTO qa5_seen VALUES (NEW.action); RETURN NEW; END $$"
            )
        )
        c.execute(text("DROP TRIGGER IF EXISTS qa5_seen_t ON audit_events"))
        c.execute(
            text("CREATE TRIGGER qa5_seen_t AFTER INSERT ON audit_events FOR EACH ROW EXECUTE FUNCTION qa5_seen_f()")
        )
    try:
        c = TestClient(app, base_url="http://testserver")
        tok = c.get("/api/v1/auth/csrf").json()["csrf_token"]
        r = c.post(
            "/api/v1/auth/login",
            json={"email": tenant_a.users["sales"], "password": "wrong-password-xyz"},
            headers={"X-CSRF-Token": tok},
        )
        assert r.status_code == 401, r.text
        with eng.connect() as k:
            n = k.execute(text("SELECT count(*) FROM qa5_seen WHERE action='auth.login_failed'")).scalar()
        assert n == 1, n
        r2 = c.post(
            "/api/v1/auth/login",
            json={"email": "nobody@nowhere.example", "password": "x" * 12},
            headers={"X-CSRF-Token": tok},
        )
        assert r2.status_code == 401
        with eng.connect() as k:
            assert k.execute(text("SELECT count(*) FROM qa5_seen WHERE action='auth.login_failed'")).scalar() == 2
    finally:
        with eng.begin() as k:
            k.execute(text("DROP TRIGGER IF EXISTS qa5_seen_t ON audit_events"))
            k.execute(text("DROP FUNCTION IF EXISTS qa5_seen_f()"))
            k.execute(text("DROP TABLE IF EXISTS qa5_seen"))
        eng.dispose()


@integration
def test_no_endpoint_touches_the_session_after_it_closed_streaming_and_response_models(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """Response models that lazy-load, streaming bodies and exception handlers: exercise every kind once with
    raise_server_exceptions=True so a DetachedInstanceError / closed-session error would surface as a test error."""
    sales = login_as(app, tenant_a.users["sales"])
    approver = login_as(app, tenant_a.users["approver"])
    q = make_quote(
        sales,
        str(tenant_a.customer_id),
        "10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n20 pcs SKU-1003 Hex nut M8 zinc plated\n",
    )
    assert sales.get(f"/quotes/{q['id']}").status_code == 200
    assert sales.get(f"/quotes/{q['id']}/history").status_code == 200
    assert sales.get("/quotes").status_code == 200
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]})
    assert s.status_code == 200
    lv = approver.get(f"/quotes/{q['id']}").json()["lock_version"]
    assert approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": lv}).status_code == 200
    for path in ("/pdf", "/csv"):
        assert approver.get(f"/quotes/{q['id']}/versions/1{path}").status_code == 200
    lv = approver.get(f"/quotes/{q['id']}").json()["lock_version"]
    nv = approver.post(f"/quotes/{q['id']}/versions", json={"expected_lock_version": lv})
    assert nv.status_code == 201, nv.text
    assert approver.get(f"/quotes/{q['id']}").json()["current_version"]["version_no"] == 2
    assert sales.get("/quotes/00000000-0000-0000-0000-000000000000").status_code == 404
    assert sales.get("/files/00000000-0000-0000-0000-000000000000").status_code == 404
    assert sales.get("/audit").status_code in (200, 403, 404)
    assert sales.get("/rfqs").status_code == 200
    assert sales.get("/customers").status_code == 200
    assert sales.get("/catalogs/active-version").status_code == 200


# ---------------------------------------------------------------------------------------------------------------
# fix round 8b (6b3c452): linear-time parser, 128-character cap, sign rule
# ---------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("-AED 5", D(-5)),
        ("AED -5", D(-5)),
        ("- AED 5", D(-5)),
        ("-$5.50", D("-5.50")),
        ("$-5.50", D("-5.50")),
        ("$ -5.50", D("-5.50")),
        ("\u2212AED 5", D(-5)),
        ("AED \u22125", D(-5)),
        ("-5 AED", D(-5)),
        ("+5 AED", D(5)),
        ("AED - 5", D(-5)),
        ("5" + " " * 120, D(5)),
        (" " * 100 + "5", D(5)),
    ],
)
def test_r8b_sign_placement_and_padding(raw: str, expected: D) -> None:
    assert parse_decimal(raw) == expected, (raw, parse_decimal(raw))


@pytest.mark.parametrize(
    "raw",
    [
        "(-5)",
        "(+5)",
        "-AED -5",
        "AED +-5",
        "--5",
        "(5) AED",
        "AED (5)",
        "5 AED-",
        "AED5-",
        "12.50- AED",
        "A" * 129,
        "1" * 129,
        "x" * 127 + " 5",
        "x" * 140 + " 5",
        "5" + "x" * 140,
        "1 " * 70,
        "1" + " 1" * 64,
    ],
)
def test_r8b_ambiguous_signs_and_over_long_cells_are_rejected(raw: str) -> None:
    assert parse_decimal(raw) is None, (raw[:40], parse_decimal(raw))


def test_r8b_hostile_long_cells_cost_next_to_nothing() -> None:
    import time

    cells = [
        " " * 40000 + "5",
        "5" + " " * 40000,
        "a" * 10**6,
        "1" + " 1" * 20000,
        "(" * 50000 + "5",
        "5" + "." * 50000,
        "\u00a0" * 40000 + "5",
        "e" * 40000,
        ("1 " * 100) + "x" * 40000,
    ]
    t0 = time.perf_counter()
    for c in cells:
        assert parse_decimal(c) is None
    assert time.perf_counter() - t0 < 1.0


def test_r8b_numbers_at_the_edge_of_the_cap() -> None:
    assert parse_decimal("AED " + "1" * 20) == D("1" * 20)  # 24 chars, magnitude < 1e30
    assert parse_decimal("x" * 100 + " 5") == D(5)  # <=128 chars: still read (design: text around ONE number)
    cell = "Packed in cartons delivery within 2 weeks"
    assert parse_decimal(cell) == D(2)  # observation O5-3: free text around one number is read as that number


@integration
def test_rfq_headerless_spreadsheet_still_finds_quantity_column_with_long_description_cells(
    app, tenant_a: Tenant
) -> None:  # type: ignore[no-untyped-def]
    """Column detection (_guess_columns) runs parse_decimal on every cell: a >128 character description ending in a
    number, thousands-grouped quantities and unit columns must not move the quantity column."""
    sales = login_as(app, tenant_a.users["sales"])
    long_desc = (
        "Hex bolt M8x40 zinc plated, grade 8.8, DIN 933 fully threaded, packed in cartons of 100 pieces, "
        "supplied with mill certificate and certificate of conformity, batch " + "7"
    )
    assert len(long_desc) > 128
    rows: list[list[Any]] = [
        [long_desc, "SKU-1001", 1000, "EA"],
        ["Hex nut M8 zinc plated", "SKU-1003", "1,500", "EA"],
        ["Lithium grease EP2 pail 18kg", "SKU-3002", "2 pcs", "EA"],
        ["Washer M8 flat zinc", "SKU-1002", 12.5, "EA"],
    ]
    xl = upload(sales, "/rfqs", "r.xlsx", _xlsx(rows), {"customer_id": str(tenant_a.customer_id)})
    items = _rfq_items(xl)
    got = {i["sku_hint"]: i["quantity"] for i in items}
    assert (
        D(got["SKU-1001"]) == 1000
        and D(got["SKU-1003"]) == 1500
        and D(got["SKU-3002"]) == 2
        and D(got["SKU-1002"]) == D("12.5")
    ), got
    csv = "\n".join(",".join(f'"{c}"' for c in r) for r in rows).encode()
    ci = _rfq_items(upload(sales, "/rfqs", "r.csv", csv, {"customer_id": str(tenant_a.customer_id)}))
    got2 = {i["sku_hint"]: i["quantity"] for i in ci}
    assert D(got2["SKU-1001"]) == 1000 and D(got2["SKU-1003"]) == 1500, got2
    # with a header the same rows map by name
    hdr = [["Description", "SKU", "Qty", "Unit"], *rows]
    hi = _rfq_items(upload(sales, "/rfqs", "r.xlsx", _xlsx(hdr), {"customer_id": str(tenant_a.customer_id)}))
    assert {i["sku_hint"]: D(i["quantity"]) for i in hi}["SKU-1001"] == 1000
    # a quantity cell with a >128 character note is unreadable and flagged, never guessed
    note = "5 pcs " + "delivery to the main warehouse on the second floor, please call before arrival " * 3
    ni = _rfq_items(
        upload(
            sales,
            "/rfqs",
            "r.csv",
            f'SKU,Description,Qty,Unit\nSKU-1001,Hex bolt,"{note}",EA\n'.encode(),
            {"customer_id": str(tenant_a.customer_id)},
        )
    )
    assert ni[0]["quantity"] is None and "quantity_unreadable" in ni[0]["flags"]
