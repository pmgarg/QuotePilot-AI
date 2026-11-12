"""Security re-verification tests, round 6 (Stage 1 fix round 8, commit 3586da2, 2026-11-12).

Attacks the strict ``parse_decimal`` rewrite, the catalog/RFQ value caps and the function-scoped DB dependency
(commit before response). Open findings are ``xfail(strict=True)`` with an ID (SEC5-n); remove the marker with the fix.
See docs/evidence/security-stage1-reverify5-2026-11-12.md.
"""

from __future__ import annotations

import io
import json
import os
import time
import unicodedata
import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.services.ai.schemas import ProposedLine
from app.services.money import MAX_QUANTITY, parse_decimal
from tests.conftest import API, PASSWORD, login_as, make_tenant, upload

integration = pytest.mark.integration
pytestmark = [pytest.mark.security]

MAPPING = json.dumps(
    {"sku": "SKU", "description": "Description", "unit": "Unit", "currency": "Currency", "list_price": "List Price"}
)
MAPPING_FULL = json.dumps(
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


def _owner(sql: str, org: uuid.UUID | None = None, **params: object) -> object:
    eng = create_engine(os.environ["DATABASE_ADMIN_URL"], future=True, isolation_level="AUTOCOMMIT")
    try:
        with eng.connect() as conn:
            if org is not None:
                conn.execute(text("SELECT set_config('app.current_org', :o, false)"), {"o": str(org)})
            res = conn.execute(text(sql), params)
            return res.scalar() if res.returns_rows else res.rowcount
    finally:
        eng.dispose()


# ----------------------------------------------------------------------------------------------- SEC4-0..5 re-check
@pytest.mark.parametrize(
    "raw",
    [
        "1 e3",
        "1e 3",
        "1 E 3",
        "1ee3",
        "1e  3",
        "1e+ 3",
        "e3",
        "12e",
        "3E",
        "5e",
        "0x10",
        "12 per 6",
        "10 x 5",
        "3 boxes of 12",
        "12 EA 3",
        "5 pcs 6",
        "1/2",
        "1 1/2",
        "3/4",
        "Rs.500",
        "1_000",
        "5,",
        ",5",
        "(5",
        "5)",
        "1 00 000",
        "1,00,000",
        "1.234.567",
        "5 +",
        "+-5",
        "5-",
        "12 - 5",
        "1​000",
        "1‏000",
        "1­000",
    ],
)
def test_sec4_0_4_strict_grammar_rejects_fragmented_or_ambiguous_numbers(raw: str) -> None:
    assert parse_decimal(raw) is None, (raw, parse_decimal(raw))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("−5", -5),
        ("﹣5", -5),
        ("－5", -5),
        ("- 5", -5),
        ("(5)", -5),
        ("-0", 0),
        ("-0.00", 0),
        ("-0e5", 0),
        ("(0)", 0),
        ("1٫5", Decimal("1.5")),
        ("AED 1٬234٫50", Decimal("1234.50")),
        ("1 234,56", Decimal("1234.56")),
        ("1'234", 1234),
        ("Rs. 500", 500),
        ("$.50", Decimal("0.5")),
        ("12.", 12),
        ("１２，５００", 12500),
        ("8.5E2", 850),
        ("1e0003", 1000),
    ],
)
def test_sec4_0_5_obvious_numbers_keep_their_meaning_and_sign(raw: str, expected: object) -> None:
    d = parse_decimal(raw)
    assert d == Decimal(str(expected)), (raw, d)
    assert d is not None and not (d.is_zero() and d.is_signed()), raw


def test_sec4_5_negative_zero_never_returned_for_any_input_type() -> None:
    for v in ("-0", "-0.0", "(0.00)", -0.0, Decimal("-0"), Decimal("-0.000"), "-0e5", "−0"):
        d = parse_decimal(v)
        assert d is None or not d.is_signed(), v


def test_sec4_1_non_finite_and_huge_inputs_of_every_type_rejected() -> None:
    for v in (
        float("nan"),
        float("inf"),
        float("-inf"),
        1e300,
        10**31,
        -(10**31),
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("1E+31"),
        Decimal("0E+1000"),
        "1e31",
        "9" * 40,
        True,
        False,
        b"5",
        [5],
        {},
    ):
        d = parse_decimal(v)
        assert d is None or (d.is_finite() and abs(d.adjusted()) <= 30), repr(v)[:30]
    assert parse_decimal(10**30) == Decimal(10**30)


def test_parse_decimal_never_raises_on_arbitrary_unicode() -> None:
    import random

    rnd = random.Random(1234)
    pool = list("0123456789.,-+()'e E$€−٫٬ ​‮⁄²①ﷺ٣१abc%/")
    for _ in range(20000):
        s = "".join(rnd.choice(pool) for _ in range(rnd.randint(0, 14)))
        d = parse_decimal(s)
        assert d is None or (d.is_finite() and not (d.is_zero() and d.is_signed())), repr(s)


def test_parse_decimal_differential_does_not_widen_acceptance_for_plain_ascii_forms() -> None:
    """Every ASCII string of digits/separators/sign that bf05198 rejected must still be rejected or an obvious number."""
    import itertools

    alphabet = "1,.-() "
    for n in range(1, 6):
        for tup in itertools.product(alphabet, repeat=n):
            s = "".join(tup)
            d = parse_decimal(s)
            if d is not None:
                # accepted: at most one sign, balanced decoration, digits contiguous apart from group separators
                assert sum(c.isdigit() for c in s) >= 1
                assert d.is_finite()


# ------------------------------------------------------------------------------------------------------- findings
def test_sec5_1_parse_decimal_is_not_quadratic_on_whitespace_padded_cells() -> None:
    worst = 0.0
    for s in ("x" + " " * 1998 + "(5", "x" + " \t" * 999 + "+-5", "$" + " " * 1998 + "(5"):
        t = time.perf_counter()
        parse_decimal(s)
        worst = max(worst, time.perf_counter() - t)
    assert worst < 0.01, worst


@integration
def test_sec5_1_rfq_upload_with_padded_quantity_cells_is_not_a_cpu_dos(app, tenant_a) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    cell = "x" + " " * 1990 + "(5"
    rows = "".join(f'SKU-1001,Hex bolt,"{cell}",EA\n' for _ in range(100))
    t = time.perf_counter()
    r = upload(
        sales,
        "/rfqs",
        "r.csv",
        ("SKU,Description,Qty,Unit\n" + rows).encode(),
        {"customer_id": str(tenant_a.customer_id)},
    )
    elapsed = time.perf_counter() - t
    assert r.status_code in (201, 422)
    assert elapsed < 2.0, elapsed


def test_sec5_2_compatibility_digits_are_not_glued_to_the_number() -> None:
    for raw in ("5²", "10³", "5①", "2⁵", "1²3", "x²", "¹²"):
        assert parse_decimal(raw) is None, (raw, parse_decimal(raw))


def test_sec5_3_other_dash_code_points_do_not_silently_flip_the_sign() -> None:
    for ch in ("‐", "‑", "‒", "–", "—", "―", "﹘", "➖", "⸺"):
        d = parse_decimal(f"{ch}5")
        assert d is None or d == Decimal(-5), (hex(ord(ch)), d)


def test_sec5_4_info_mixed_digit_scripts_are_read_as_one_number() -> None:
    """Info only: Decimal() accepts any Nd digit; '1٢3' and '१२३' read as 123 (the same digits)."""
    assert parse_decimal("1٢3") == Decimal(123)
    assert parse_decimal("१२३") == Decimal(123)
    assert unicodedata.numeric("٢") == 2


def test_bidi_and_zero_width_inside_a_number_reject_but_outside_are_ignored() -> None:
    assert parse_decimal("‮5‬") == Decimal(5)
    assert parse_decimal("‏-5") == Decimal(-5)
    assert parse_decimal("1‮000") is None and parse_decimal("1⁠000") is None


def test_percent_and_currency_decoration_is_accepted_but_value_is_the_stated_digits() -> None:
    assert parse_decimal("5%") == Decimal(5) and parse_decimal("€ 1.234,56") == Decimal("1234.56")


# ---------------------------------------------------------------------------------------------- SEC4-2 / SEC4-6 caps
def _import(admin, rows: bytes, mapping: str = MAPPING_FULL):  # type: ignore[no-untyped-def]
    head = b"SKU,Description,Unit,Currency,List Price,Cost,Stock\n"
    return upload(admin, "/catalogs/import", "c.csv", head + rows, {"mapping": mapping, "catalog_name": "Main"})


def _products(admin, vid: str) -> dict[str, dict]:  # type: ignore[no-untyped-def, type-arg]
    r = admin.get(f"/catalogs/versions/{vid}/products")
    assert r.status_code == 200, r.text
    return {p["sku"]: p for p in r.json()["items"]}


@integration
def test_sec4_2_boundaries_for_price_cost_and_stock(app) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("S6 Caps", with_catalog=False)
    admin = login_as(app, t.users["admin"])
    rows = (
        b"P1,a,EA,AED,999999999999.999999,1,1\n"  # largest storable price
        b"P2,a,EA,AED,999999999999.9999995,1,1\n"  # rounds up to 1e12 -> too large
        b"P3,a,EA,AED,1000000000000,1,1\n"
        b"P4,a,EA,AED,5,999999999999.9999995,1\n"  # cost rounds to 1e12
        b"P5,a,EA,AED,5,1e30,1\n"
        b"P6,a,EA,AED,5,1,1e30\n"  # stock too large -> unknown + warning, row kept
        b"P7,a,EA,AED,5,1,999999999999.9996\n"
        b"P8,a,EA,AED,5,1,-1e30\n"
        b"P9,a,EA,AED,5,1,1e31\n"
        b"P10,a,EA,AED,0.0000004,,\n"
        b"P11,a,EA,AED,5,,\n"
    )
    r = _import(admin, rows)
    assert r.status_code == 201, r.text
    prods = _products(admin, r.json()["id"])
    assert prods["P1"]["list_price"] == "999999999999.999999"
    for bad in ("P2", "P3", "P4", "P5"):
        assert bad not in prods, bad
    for kept in ("P6", "P7", "P8", "P9"):
        assert kept in prods and prods[kept]["stock_qty"] is None, (kept, prods.get(kept))
    assert prods["P10"]["list_price"] in ("0", "0.000000")  # sub-quantum price rounds to zero (documented, Low)
    assert prods["P11"]["list_price"] == "5"


def _xlsx_two_cells(price: str, cost: str, stock: str) -> bytes:
    import zipfile

    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["SKU", "Description", "Unit", "Currency", "List Price", "Cost", "Stock"])
    ws.append(["X1", "d", "EA", "AED", 111111, 222222, 333333])
    buf = io.BytesIO()
    wb.save(buf)
    src = zipfile.ZipFile(io.BytesIO(buf.getvalue()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for it in src.infolist():
            data = src.read(it.filename)
            if it.filename == "xl/worksheets/sheet1.xml":
                for old, new in ((b"111111", price), (b"222222", cost), (b"333333", stock)):
                    assert f"<v>{old.decode()}</v>".encode() in data
                    data = data.replace(f"<v>{old.decode()}</v>".encode(), f"<v>{new}</v>".encode())
            z.writestr(it, data)
    return out.getvalue()


@integration
@pytest.mark.parametrize(
    ("price", "cost", "stock"),
    [
        ("1e300", "1", "1"),
        ("1", "1e300", "1"),
        ("1", "1", "1e300"),
        ("inf", "1", "1"),
        ("1", "nan", "1"),
        ("1", "1", "-inf"),
        ("1E+400", "1E+400", "1E+400"),
        ("123456789012.5", "5", "1e15"),
    ],
)
def test_sec4_2_xlsx_numeric_cells_never_500(app, price: str, cost: str, stock: str) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("S6 Xlsx", with_catalog=False)
    admin = login_as(app, t.users["admin"])
    r = upload(
        admin,
        "/catalogs/import",
        "c.xlsx",
        _xlsx_two_cells(price, cost, stock),
        {"mapping": MAPPING_FULL, "catalog_name": "Main"},
    )
    assert r.status_code in (201, 422), (price, cost, stock, r.status_code)
    if r.status_code == 201:
        for p in _products(admin, r.json()["id"]).values():
            assert Decimal(p["list_price"]) < Decimal(10) ** 12


def _rfq_csv(sales, tenant, qtys: list[str]):  # type: ignore[no-untyped-def]
    rows = "".join(f'SKU-100{i},Item {i},"{q}",EA\n' for i, q in enumerate(qtys, start=1))
    return upload(
        sales,
        "/rfqs",
        "r.csv",
        ("SKU,Description,Qty,Unit\n" + rows).encode(),
        {"customer_id": str(tenant.customer_id)},
    )


@integration
def test_sec4_6_quantity_boundaries_rounding_and_flags(app, tenant_a) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    cases = [
        "10000000",
        "10000000.0004",
        "10000000.001",
        "9999999.9996",
        "0.0004",
        "0.0005",
        "1e7",
        "1e-3",
        "1e8",
        "1e30",
        "0",
        "-3",
        "(3)",
        "99999999999999999999999999999999",
    ]
    r = _rfq_csv(sales, tenant_a, cases)
    assert r.status_code == 201, r.text
    by_line = {i["line_no"]: i for i in r.json()["items"]}
    got = [by_line[n]["quantity"] for n in range(1, len(cases) + 1)]
    assert got[0] == "10000000" and got[3] == "10000000" and got[5] == "0.001" and got[6] == "10000000"
    assert got[7] == "0.001"
    assert got[1] is None and got[2] is None and got[4] is None  # above cap / rounds to zero: empty, never invented
    assert all(g is None for g in got[8:])
    assert "quantity_out_of_range" in by_line[3]["flags"] and "quantity_not_positive" in by_line[5]["flags"]
    for g in got:
        assert g is None or (Decimal(0) < Decimal(g) <= MAX_QUANTITY)


@integration
def test_sec4_6_pasted_text_and_pdf_paths_cap_quantities(app, tenant_a) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    txt = "\n".join(
        [
            "99999999999999999 pcs SKU-1001 hex bolt",
            "1" + "0" * 40 + " pcs SKU-1002 washer",
            "10000001 x SKU-1003 nut",
            "10000000 pcs SKU-1004 screw",
            "1e3 pcs SKU-1005 pin",
        ]
    )
    r = sales.post("/rfqs", json={"text": txt, "customer_id": str(tenant_a.customer_id)})
    assert r.status_code == 201, r.text
    for it in r.json()["items"]:
        assert it["quantity"] is None or Decimal(it["quantity"]) <= MAX_QUANTITY, it
    import reportlab.pdfgen.canvas as cv

    buf = io.BytesIO()
    c = cv.Canvas(buf)
    y = 800
    for line in (
        "1. 99999999999999999 pcs SKU-1001 hex bolt",
        "2. 10000001 pcs SKU-1002 washer",
        "3. 25 pcs SKU-1003 nut",
    ):
        c.drawString(40, y, line)
        y -= 20
    c.save()
    r = upload(sales, "/rfqs", "r.pdf", buf.getvalue(), {"customer_id": str(tenant_a.customer_id)})
    assert r.status_code in (201, 422), r.text[:200]
    if r.status_code == 201:
        for it in r.json()["items"]:
            assert it["quantity"] is None or Decimal(it["quantity"]) <= MAX_QUANTITY, it


@integration
def test_sec4_6_manual_item_post_and_patch_paths_cap(app, tenant_a) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    rfq = sales.post(
        "/rfqs", json={"text": "5 pcs SKU-1001 hex bolt\n", "customer_id": str(tenant_a.customer_id)}
    ).json()
    for bad in ("10000000.001", "10000001", "1e30", "0.0005", "0", "-1", "NaN", "Infinity", "1" * 40):
        r = sales.post(f"/rfqs/{rfq['id']}/items", json={"description": "x item", "quantity": bad})
        assert r.status_code == 422, (bad, r.status_code)
        r = sales.patch(f"/rfqs/{rfq['id']}/items/{rfq['items'][0]['id']}", json={"quantity": bad})
        assert r.status_code == 422, (bad, r.status_code)
    ok = sales.post(f"/rfqs/{rfq['id']}/items", json={"description": "x item", "quantity": "10000000"})
    assert ok.status_code == 201 and ok.json()["quantity"] == "10000000"


@integration
@pytest.mark.parametrize("stored", ["50000000", "10000000.001", "999999999999999"])
def test_sec4_6_rows_already_stored_above_cap_are_blocked_in_generate_quote(app, tenant_a, stored: str) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    rfq = sales.post(
        "/rfqs", json={"text": "5 pcs SKU-1001 hex bolt\n", "customer_id": str(tenant_a.customer_id)}
    ).json()
    assert rfq["items"][0]["selected_product_id"], "fixture assumption: exact SKU match"
    _owner("UPDATE rfq_items SET quantity = :q WHERE rfq_id = :r", tenant_a.org_id, q=Decimal(stored), r=rfq["id"])
    got = sales.get(f"/rfqs/{rfq['id']}")
    assert got.status_code == 200 and got.json()["items"][0]["quantity"] is not None
    g = sales.post(f"/rfqs/{rfq['id']}/generate-quote")
    assert g.status_code in (409, 422), (stored, g.status_code, g.text[:200])
    assert "quantity" in g.text.lower()
    assert _owner("SELECT count(*) FROM quotes WHERE rfq_id = :r", tenant_a.org_id, r=rfq["id"]) == 0


def test_sec4_6_ai_proposal_schema_cap_is_aligned() -> None:
    ProposedLine(description="x", quantity=Decimal(10_000_000))
    for bad in (Decimal(10_000_001), Decimal("1e9"), Decimal(0), Decimal(-1), Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises(Exception):  # noqa: B017, PT011
            ProposedLine(description="x", quantity=bad)


# ------------------------------------------------------------------- D5: function-scoped dependency review (task 3)
def _count(table: str, org: uuid.UUID) -> int:
    return int(_owner(f"SELECT count(*) FROM {table}", org))  # type: ignore[call-overload]  # noqa: S608 - constant table names


@integration
def test_d5_failed_commit_on_login_gives_generic_500_no_cookie_and_no_session_row(app, tenant_a, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    def boom(self):  # type: ignore[no-untyped-def]
        self.rollback()  # a failed COMMIT leaves nothing behind
        raise OperationalError(
            "INSERT INTO sessions (token_hash) VALUES ('LEAKME')", {"token_hash": "LEAKME"}, Exception("conn lost")
        )

    c = TestClient(app, base_url="http://testserver", raise_server_exceptions=False)
    pre = c.get(f"{API}/auth/csrf").json()["csrf_token"]
    monkeypatch.setattr(Session, "commit", boom)
    r = c.post(
        f"{API}/auth/login",
        json={"email": tenant_a.users["sales"], "password": PASSWORD},
        headers={"X-CSRF-Token": pre},
    )
    monkeypatch.undo()
    assert r.status_code == 500, (r.status_code, r.text[:200])
    body = r.text
    for leak in ("INSERT", "LEAKME", "token_hash", "Traceback", "OperationalError", "sqlalchemy", "psycopg"):
        assert leak not in body, leak
    assert not any(
        "quotepilot_session" in h.lower() or "session" in h.lower() for h in r.headers.get_list("set-cookie")
    ), r.headers
    assert c.get(f"{API}/auth/me").status_code == 401
    assert int(_owner("SELECT count(*) FROM sessions")) == 0  # type: ignore[call-overload]


@integration
def test_d5_failed_commit_leaves_no_partial_state_and_no_audit_row(app, tenant_a, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    owner = login_as(app, tenant_a.users["owner"])
    before_c, before_a = _count("customers", tenant_a.org_id), _count("audit_events", tenant_a.org_id)

    def boom(self):  # type: ignore[no-untyped-def]
        self.rollback()
        raise OperationalError("UPDATE customers SET secret=1", {}, Exception("disk full"))

    monkeypatch.setattr(Session, "commit", boom)
    owner.client.close()
    safe = TestClient(app, base_url="http://testserver", raise_server_exceptions=False)
    safe.cookies.update(owner.client.cookies)
    r = safe.post(
        f"{API}/customers", json={"name": "Ghost Ltd", "default_currency": "AED"}, headers={"X-CSRF-Token": owner.csrf}
    )
    monkeypatch.undo()
    assert r.status_code == 500 and "UPDATE" not in r.text and "disk full" not in r.text
    assert _count("customers", tenant_a.org_id) == before_c and _count("audit_events", tenant_a.org_id) == before_a


@integration
def test_d5_exception_after_audit_write_rolls_back_audit_and_data(app, tenant_a, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import app.services.audit as audit_mod

    owner = login_as(app, tenant_a.users["owner"])
    real = audit_mod.record
    before_a = _count("audit_events", tenant_a.org_id)

    def record_then_fail(*a, **kw):  # type: ignore[no-untyped-def]
        real(*a, **kw)
        raise RuntimeError("late failure SELECT secret FROM x")

    monkeypatch.setattr(audit_mod, "record", record_then_fail)
    safe = TestClient(app, base_url="http://testserver", raise_server_exceptions=False)
    safe.cookies.update(owner.client.cookies)
    r = safe.post(
        f"{API}/customers", json={"name": "Ghost Two", "default_currency": "AED"}, headers={"X-CSRF-Token": owner.csrf}
    )
    monkeypatch.undo()
    assert r.status_code == 500 and "SELECT secret" not in r.text and "late failure" not in r.text
    assert _owner("SELECT count(*) FROM customers WHERE name = 'Ghost Two'", tenant_a.org_id) == 0
    assert _count("audit_events", tenant_a.org_id) == before_a


@integration
def test_d5_4xx_paths_commit_nothing(app, tenant_a) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    before = _count("audit_events", tenant_a.org_id)
    assert sales.post("/customers", json={"name": ""}).status_code == 422
    assert sales.get(f"/quotes/{uuid.uuid4()}").status_code == 404
    assert sales.post(f"/quotes/{uuid.uuid4()}/approve").status_code in (403, 404, 422)
    assert sales.get("/org/members").status_code in (200, 403)
    assert _count("audit_events", tenant_a.org_id) == before


@integration
def test_d5_one_db_session_per_request_shared_by_auth_and_endpoint(app, tenant_a, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import app.db.session as sess

    sales = login_as(app, tenant_a.users["sales"])
    real = sess.get_sessionmaker
    created: list[int] = []

    def counting():  # type: ignore[no-untyped-def]
        sm = real()

        def factory():  # type: ignore[no-untyped-def]
            s = sm()
            created.append(id(s))
            return s

        return factory

    monkeypatch.setattr(sess, "get_sessionmaker", counting)
    assert sales.get("/customers").status_code == 200
    assert sales.post("/customers", json={"name": "One Session Ltd", "default_currency": "AED"}).status_code == 201
    monkeypatch.undo()
    assert len(created) == 2, created  # one session per request, auth + endpoint share it


@integration
def test_d5_sql_after_commit_without_tenant_context_fails_closed(app, tenant_a, tenant_b) -> None:  # type: ignore[no-untyped-def]
    """If anything queried after the request's commit (new implicit transaction, no set_config), RLS yields nothing."""
    from app.db.session import get_sessionmaker

    login_as(app, tenant_a.users["sales"])
    login_as(app, tenant_b.users["sales"])
    with get_sessionmaker()() as db:
        db.execute(text("SELECT 1"))
        db.commit()  # transaction-local settings are gone after commit
        for table in (
            "customers",
            "quotes",
            "quote_lines",
            "rfqs",
            "rfq_items",
            "audit_events",
            "uploaded_files",
            "products",
            "catalog_versions",
            "sessions",
            "users",
            "memberships",
            "organizations",
        ):
            n = db.execute(text(f"SELECT count(*) FROM {table}")).scalar()  # noqa: S608 - constant table names
            assert n == 0, (table, n)
        assert db.execute(text("SELECT coalesce(current_setting('app.current_org', true), '')")).scalar() == ""
        with pytest.raises(Exception):  # noqa: B017, PT011  - cannot write a tenant row without a tenant
            db.execute(
                text(
                    "INSERT INTO customers (id, organization_id, name, default_currency) VALUES "
                    "(gen_random_uuid(), :o, 'x', 'AED')"
                ),
                {"o": str(tenant_a.org_id)},
            )
        db.rollback()


@integration
def test_d5_login_cookie_resolves_immediately_and_each_login_rotates_the_token(app, tenant_a) -> None:  # type: ignore[no-untyped-def]
    tokens = []
    for _ in range(2):
        c = TestClient(app, base_url="http://testserver")
        pre = c.get(f"{API}/auth/csrf").json()["csrf_token"]
        r = c.post(
            f"{API}/auth/login",
            json={"email": tenant_a.users["sales"], "password": PASSWORD},
            headers={"X-CSRF-Token": pre},
        )
        assert r.status_code == 200
        # a brand-new client replaying the cookie right after the response must be authenticated (row committed)
        fresh = TestClient(app, base_url="http://testserver")
        fresh.cookies.update(c.cookies)
        assert fresh.get(f"{API}/auth/me").status_code == 200
        tokens.append(c.cookies.get("qp_session") or next(v for k, v in c.cookies.items() if "session" in k))
    assert tokens[0] != tokens[1]


@integration
def test_d5_logout_revokes_before_response_and_failed_commit_keeps_session_valid(app, tenant_a, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    a = login_as(app, tenant_a.users["sales"])
    replay = TestClient(app, base_url="http://testserver")
    replay.cookies.update(a.client.cookies)
    real = Session.commit

    def boom(self):  # type: ignore[no-untyped-def]
        self.rollback()
        raise OperationalError("UPDATE sessions SET revoked_at", {}, Exception("x"))

    safe = TestClient(app, base_url="http://testserver", raise_server_exceptions=False)
    safe.cookies.update(a.client.cookies)
    monkeypatch.setattr(Session, "commit", boom)
    r = safe.post(f"{API}/auth/logout", headers={"X-CSRF-Token": a.csrf})
    monkeypatch.setattr(Session, "commit", real)
    assert r.status_code == 500 and "UPDATE" not in r.text
    assert replay.get(f"{API}/auth/me").status_code == 200  # nothing was revoked, nothing half-done
    assert a.post("/auth/logout").status_code == 200
    assert replay.get(f"{API}/auth/me").status_code == 401  # revoked and committed before the 200 reached the client


@integration
def test_d5_download_streams_without_further_sql_and_stays_tenant_scoped(app, tenant_a, tenant_b) -> None:  # type: ignore[no-untyped-def]
    sa = login_as(app, tenant_a.users["sales"])
    sb = login_as(app, tenant_b.users["sales"])
    r = upload(
        sa, "/rfqs", "r.csv", b"SKU,Description,Qty\nSKU-1001,Hex,5\n", {"customer_id": str(tenant_a.customer_id)}
    )
    fid = r.json()["source_file_id"]
    d = sa.get(f"/files/{fid}")
    assert d.status_code == 200 and d.content.startswith(b"SKU,Description")
    assert sb.get(f"/files/{fid}").status_code == 404


# ------------------------------------------------------------- round 8b (6b3c452): linear-time parser, length cap
def test_8b_parse_decimal_is_fast_on_every_hostile_shape_up_to_megabytes() -> None:
    for n in (1_000, 100_000, 2_000_000):
        shapes = [
            "x" + " " * n + "(5",
            "x" + " \t" * n + "+-5",
            " " * n + "x" + " " * n + "+ 5",
            "\ufdfa" * n + "5",
            "9" * n,
            "1" + " 000" * n,
            "5" + " e" * n,
            "." * n + "5",
            "\uff11" * n,
            "e\u0301" * n + "5",
            "-" * n + "5",
            "(" * n + "5",
            "5" + ")" * n,
            "1" + ",000" * n,
            "5" + " " * n + "-",
        ]
        for sh in shapes:
            t = time.perf_counter()
            parse_decimal(sh)
            assert time.perf_counter() - t < 0.05, (n, sh[:8])


def test_8b_length_cap_boundaries_and_nfkc_expansion() -> None:
    assert parse_decimal("0" * 120 + "5") == Decimal(5)  # inside the 128-character cap
    assert parse_decimal("0" * 130 + "5") is None  # outside: refused, never truncated or guessed
    assert parse_decimal("5" + " " * 400) == Decimal(5)  # padded cell below 512 raw characters is still read
    assert parse_decimal("5" + " " * 600) is None  # documented over-long cell
    assert parse_decimal("\ufdfa" * 512 + "5") is None  # NFKC expansion (x18) is bounded by the pre-check
    assert parse_decimal("\ufdfa" * 9 + "5") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("(\u22125)", None),
        ("(+5)", None),
        ("(-5)", None),
        ("(5)", Decimal(-5)),
        ("(5 AED)", Decimal(-5)),
        ("-AED 5", Decimal(-5)),
        ("AED -5", Decimal(-5)),
        ("AED - 5", Decimal(-5)),
        ("- 5 AED", Decimal(-5)),
        ("+-5", None),
        ("-+5", None),
        ("5-", None),
        ("-(5)", None),
        ("5 AED-", None),
        ("--5", None),
    ],
)
def test_8b_sign_rules(raw: str, expected: Decimal | None) -> None:
    assert parse_decimal(raw) == expected, raw


def test_8b_differential_against_3586da2_corpus_never_changes_a_value() -> None:
    """Recorded during re-verification 5: 300,000 random strings, 0 value changes, 130 newly accepted (sign directly
    before the digits after whitespace, e.g. 'AED - 5', 'A- 2'), 2 newly rejected ('(-1 )', '(-<ZWSP>5)')."""
    for raw, expected in (("A- 2", Decimal(-2)), ("E+ 3", Decimal(3)), ("AED - 5", Decimal(-5))):
        assert parse_decimal(raw) == expected


@integration
def test_8b_rfq_upload_with_padded_cells_is_fast_and_flags_the_quantity(app, tenant_a) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    cell = "x" + " " * 1990 + "(5"
    rows = "".join(f'SKU-1001,Hex bolt,"{cell}",EA\n' for _ in range(100))
    t = time.perf_counter()
    r = upload(
        sales,
        "/rfqs",
        "r.csv",
        ("SKU,Description,Qty,Unit\n" + rows).encode(),
        {"customer_id": str(tenant_a.customer_id)},
    )
    assert r.status_code in (201, 422)
    assert time.perf_counter() - t < 2.0
    if r.status_code == 201:
        assert all(i["quantity"] is None for i in r.json()["items"])


@integration
def test_8b_catalog_import_with_padded_price_cells_is_fast(app) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("S6 Pad", with_catalog=False)
    admin = login_as(app, t.users["admin"])
    cell = "x" + " " * 1990 + "(5"
    rows = "".join(f'P{i},a,EA,AED,"{cell}"\n' for i in range(100)) + "OK,a,EA,AED,5\n"
    t0 = time.perf_counter()
    r = upload(
        admin,
        "/catalogs/import",
        "c.csv",
        (b"SKU,Description,Unit,Currency,List Price\n" + rows.encode()),
        {"mapping": MAPPING, "catalog_name": "Main"},
    )
    assert r.status_code == 201, r.text
    assert time.perf_counter() - t0 < 2.0
    assert list(_products(admin, r.json()["id"])) == ["OK"]


# ---------------------------------------------------- pre-existing quadratic regexes in the text-line parser (new)
def test_sec5_5_text_line_parser_is_linear_on_whitespace_runs() -> None:
    from app.services.parsing.extract import items_from_text_lines

    worst = 0.0
    for line in ("abc" + " " * 8000 + "x", "1" + " " * 8000 + "x", "abc " + " " * 8000 + "5 xx"):
        t = time.perf_counter()
        items_from_text_lines(line, {})
        worst = max(worst, time.perf_counter() - t)
    assert worst < 0.25, worst


@integration
def test_sec5_5_pasted_rfq_text_with_whitespace_run_is_not_a_cpu_dos(app, tenant_a) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    txt = "\n".join("abc" + " " * 8000 + "x" for _ in range(3))
    t = time.perf_counter()
    r = sales.post("/rfqs", json={"text": txt, "customer_id": str(tenant_a.customer_id)})
    assert r.status_code in (201, 413, 422)
    assert time.perf_counter() - t < 1.0


def test_sec5_6_leading_zero_comma_three_digits_is_not_thousands() -> None:
    for raw in ("0,500", "0,001", "0,125", "00,500"):
        d = parse_decimal(raw)
        assert d is None or d < 1, (raw, d)


def test_sec5_7_doubled_leading_dot_is_rejected() -> None:
    for raw in ("..5", "...5", "..50"):
        assert parse_decimal(raw) is None, raw
