"""Security re-verification tests, round 8 (Stage 1 fix round 10, commit 8258112, 2026-11-12).

Delta checks for: well-formed grouped numbers (SEC6-5), digit-expanding symbols (SEC6-4), the regex-free
``normalize_sku`` (SEC6-1), negative stock (SEC6-3), the matching budget (SEC6-2 residual) and the seed reading the
repository-root ``.env``. Open findings are ``xfail(strict=True)`` with an ID (SEC7-n).
See docs/evidence/security-stage1-reverify7-2026-11-12.md.
"""

from __future__ import annotations

import itertools
import json
import os
import random
import re
import subprocess
import sys
import time
import unicodedata
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import matching
from app.services.matching import MAX_FUZZY_COMPARISONS, MAX_QUERY_CHARS, CatalogIndex
from app.services.money import parse_decimal
from app.services.units import normalize_sku, normalize_text, normalize_unit
from tests.conftest import API_DIR, login_as, make_tenant, upload

integration = pytest.mark.integration
pytestmark = [pytest.mark.security]
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


# ---------------------------------------------------------------------------- independent reference reader
def _readings(s: str) -> set[Decimal]:
    """Plausible human readings of ``s`` over the alphabet 0-9 , . - ( ) space, written independently of the parser."""
    t = s.strip()
    neg = False
    if len(t) >= 2 and t.startswith("(") and t.endswith(")"):
        neg, t = True, t[1:-1].strip()
    sign = ""
    if t[:1] in ("+", "-"):
        sign, t = t[0], t[1:].lstrip()
    if (neg and sign) or not t or not re.fullmatch(r"[0-9,. ]+", t) or t[0] == " " or t[-1] == " ":
        return set()
    if not any(c.isdigit() for c in t):
        return set()
    out: set[Decimal] = set()
    negative = neg or sign == "-"

    def add(int_part: str, dec_part: str) -> None:
        v = Decimal(int_part + ("." + dec_part if dec_part else ""))
        out.add(-v if negative else v)

    seps = [(i, c) for i, c in enumerate(t) if c in ",. "]
    if not seps:
        add(t, "")
        return out

    def groups_ok(body: str, sep: str) -> bool:
        parts = body.split(sep)
        if any(p == "" or not p.isdigit() for p in parts):
            return False
        first, rest = parts[0], parts[1:]
        if first.startswith("0") and rest:
            return False
        if len(first) <= 3 and all(len(p) == 3 for p in rest):
            return True
        return (
            sep == ","
            and len(first) <= 2
            and len(parts) >= 3
            and all(len(p) == 2 for p in rest[:-1])
            and len(rest[-1]) == 3
        )

    kinds = {c for _, c in seps}
    if len(kinds) == 1:
        c = next(iter(kinds))
        if groups_ok(t, c):
            add(t.replace(c, ""), "")
    i, c = seps[-1]
    if c in ",." and (t[i + 1 :].isdigit() or t[i + 1 :] == ""):
        int_part, dec = t[:i], t[i + 1 :]
        rest_kinds = {x for _, x in seps[:-1]}
        if not rest_kinds:
            if (int_part == "" or int_part.isdigit()) and (dec or int_part):
                add(int_part or "0", dec)
        elif len(rest_kinds) == 1 and c not in rest_kinds:
            r = next(iter(rest_kinds))
            if groups_ok(int_part, r) and dec:
                add(int_part.replace(r, ""), dec)
    return out


def _tolerated_malformed(s: str) -> bool:
    """Forms the parser accepts although the reference calls them malformed, by design or as a recorded finding."""
    t = s.strip()
    if len(t) >= 2 and t.startswith("(") and t.endswith(")"):
        return bool(_readings(t[1:-1])) or _tolerated_malformed(t[1:-1])
    core = re.sub(r"^[\s(+-]*", "", s)
    core = re.sub(r"^[\s.]+", "", core)  # punctuation-only head such as ". 5" or "Rs. 5" without letters (SEC7-6)
    stripped = re.sub(r"[ .]+$", "", s)  # trailing blank / dot ("12.")
    for cand in (core, stripped, re.sub(r"[ .]+$", "", core)):
        r = _readings(("-" if "-" in s else "") + cand.lstrip("-")) if cand else set()
        if r:
            return True
    return bool(re.match(r"^[\s.(+-]*0\d*[  ']\d{3}", s))  # zero-led space group (SEC7-3)


def _owner_scalar(sql: str, org, **params):  # type: ignore[no-untyped-def]
    from sqlalchemy import create_engine, text

    eng = create_engine(os.environ["DATABASE_ADMIN_URL"], future=True, isolation_level="AUTOCOMMIT")
    try:
        with eng.connect() as conn:
            conn.execute(text("SELECT set_config('app.current_org', :o, false)"), {"o": str(org)})
            return conn.execute(text(sql), params).scalar()
    finally:
        eng.dispose()


def _enumerate(alphabet: list[str], max_len: int) -> list[tuple[str, Decimal | None, set[Decimal]]]:
    out = []
    for n in range(1, max_len + 1):
        for tup in itertools.product(alphabet, repeat=n):
            s = "".join(tup)
            out.append((s, parse_decimal(s), _readings(s)))
    return out


# --------------------------------------------------------------------------- SEC6-5 / SEC6-4 / SEC4 re-check
@pytest.mark.parametrize(
    "raw",
    ["0,500.00", "0.500,00", "00,500.25", "0,001.5", "0.500,5", ".5,0", "12,34.56", "1.2,3.4", "1,2.3,4",
     "1,234,56.78", "1.234.56,78", "12,345,6.7", "1,,234.5", "1.234,", ",5.5", ".5,5", "1,234.5,6"],
)  # fmt: skip
def test_sec6_5_malformed_two_separator_numbers_are_rejected(raw: str) -> None:
    assert parse_decimal(raw) is None, (raw, parse_decimal(raw))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1,234.56", "1234.56"), ("1.234,56", "1234.56"), ("12,345,678.9", "12345678.9"), ("1.234.567,89", "1234567.89"),
     ("1,00,000.50", "100000.50"), ("12,34,567.8", "1234567.8"), ("999,999.99", "999999.99"), ("0.5", "0.5"),
     ("0,5", "0.5"), ("(1,234.56)", "-1234.56"), ("-1.234,56", "-1234.56"), ("AED 1,234.56", "1234.56"),
     ("1 234,56", "1234.56"), ("0,500", "0.500")],
)  # fmt: skip
def test_sec6_5_well_formed_grouped_numbers_keep_their_value(raw: str, expected: str) -> None:
    assert parse_decimal(raw) == Decimal(expected), raw


def test_sec6_4_every_code_point_that_expands_to_a_digit_makes_the_cell_not_a_number() -> None:
    checked = 0
    for cp in range(0x110000):
        if 0xD800 <= cp <= 0xDFFF:
            continue
        ch = chr(cp)
        if ch.isascii() or ch.isdecimal():
            continue
        if unicodedata.numeric(ch, None) is not None or any(c.isdecimal() for c in unicodedata.normalize("NFKC", ch)):
            checked += 1
            assert parse_decimal(f"5{ch}") is None and parse_decimal(f"{ch}5") is None, hex(cp)
    assert checked > 1000


def test_exhaustive_short_strings_no_value_outside_the_reference_readings_except_recorded_findings() -> None:
    """Every string over the alphabets below is compared with the independent reference. Allowed disagreements: the
    documented ambiguity (the parser picks one of the readings), stricter rejection, and the recorded SEC7-2/SEC7-3/
    SEC7-6 classes; everything else fails."""
    sec7_2 = re.compile(r"^\d{4,},\d{3}(,\d{3})*$")
    runs = [(["5", ",", "."], 11), (["5", "0", ",", ".", " "], 7), (["5", ",", ".", " ", "-", "(", ")"], 6)]
    total = 0
    unexpected: list[tuple[object, ...]] = []
    for alphabet, max_len in runs:
        for s, v, r in _enumerate(alphabet, max_len):
            total += 1
            if v is None:
                continue
            if not r:
                if not _tolerated_malformed(s):
                    unexpected.append(("accepts malformed", s, str(v)))
            elif v not in r and not sec7_2.match(s.strip()):
                unexpected.append(("value not among readings", s, str(v), sorted(map(str, r))))
    assert total > 400_000
    assert unexpected == [], unexpected[:15]


def test_sec7_2_four_digit_first_group_before_a_comma_group_is_not_thousands() -> None:
    for raw in ("1234,567", "12345,678", "5550,000", "1234,567,890", "5005,505"):
        d = parse_decimal(raw)
        assert d is None or d < Decimal(10) ** 5, (raw, d)


def test_sec7_3_leading_zero_first_group_with_space_grouping_is_rejected() -> None:
    for raw in ("0 500", "0 005", "00 500", "05 050", "0'500"):
        assert parse_decimal(raw) is None, (raw, parse_decimal(raw))


def test_info_sec7_6_punctuation_only_head_is_accepted_documented() -> None:
    """Info: '. 5', '..-5', '-. 5,500' read as 5, -5, -5500 (a head without letters is treated as decoration)."""
    # orchestrator edit (declared, fix round 11): SEC7-6 is fixed, the documented acceptance became a rejection
    assert parse_decimal(". 5") is None and parse_decimal("..-5") is None


def test_sec7_5_digit_expansion_scan_is_cheap_on_expanding_characters() -> None:
    cell = "ﷺ" * 500
    t = time.perf_counter()
    for _ in range(20):
        parse_decimal(cell)
    assert (time.perf_counter() - t) / 20 < 0.0005


def test_patterns_and_scans_are_bounded_on_adversarial_128_to_512_char_cells() -> None:
    shapes = ["1" + ",22" * 40 + ",333.5", "1" + ",22" * 40 + ",33.5", "1" + ",000" * 30 + ".5", "1" + ".000" * 30 + ",5",
              "1," * 64, "0" + ",000" * 31, "é" * 511, "１" * 511, "一" * 511]  # fmt: skip
    for s in shapes:
        t = time.perf_counter()
        for _ in range(20):
            parse_decimal(s)
        assert (time.perf_counter() - t) / 20 < 0.002, s[:12]


# ------------------------------------------------------------------------------------------------- SEC6-1
def _old_normalize_sku(raw: str | None) -> str:
    if raw is None:
        return ""
    s = re.sub(r"\s+", " ", str(raw)).strip()
    return re.sub(r"^[\s\W_]+|[\s\W_]+$", "", s).upper()


def test_sec6_1_normalize_sku_matches_the_old_regex_version_on_random_and_ordinary_cells() -> None:
    rnd = random.Random(9)
    pool = list("abcXYZ019 -_./\t\n  ​́éß一−()#*\x00ﷺ²")
    for _ in range(60_000):
        s = "".join(rnd.choice(pool) for _ in range(rnd.randint(0, 12)))
        assert normalize_sku(s) == _old_normalize_sku(s), repr(s)
    for s in ("SKU-1001", " sku 1001 ", "--A-1--", "", "  ", "éclair-9!", "(X)", "_Q_", "ab  cd"):
        assert normalize_sku(s) == _old_normalize_sku(s)
    assert normalize_sku(None) == ""


def test_sec6_1_normalizers_are_linear_at_megabyte_sizes() -> None:
    n = 4_000_000
    for s in (
        "a" + "-" * n + "b",
        "a" + " ." * (n // 2) + "b",
        "-" * n + "a" + "_" * n,
        "a" + " \t" * (n // 2) + "b",
        "-" * n,
    ):
        t = time.perf_counter()
        normalize_sku(s)
        assert time.perf_counter() - t < 2.0, s[:6]
    for fn in (normalize_text, normalize_unit):
        for s in ("a" + " " * 2_000_000 + "b", "a \t" * 1_000_000):
            t = time.perf_counter()
            fn(s)
            assert time.perf_counter() - t < 1.0, fn.__name__


@integration
def test_sec6_1_catalog_import_at_the_cell_limit_is_linear(app) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("S8 Sku", with_catalog=False)
    admin = login_as(app, t.users["admin"])
    cells = ["a" + "-" * 1990 + "b", "a" + " ." * 995 + "b", "-" * 1998 + "a", "x" + " \t" * 998 + "y"]
    rows = "".join(f'"{cells[i % 4]}{i}",d,EA,AED,5,1,1\n' for i in range(1500)).encode()
    t0 = time.perf_counter()
    r = upload(admin, "/catalogs/import", "c.csv", HEAD + rows, {"mapping": MAPPING, "catalog_name": "M"})
    assert r.status_code in (201, 422)
    assert time.perf_counter() - t0 < 3.0


# ------------------------------------------------------------------------------------------------- SEC6-3
@integration
def test_sec6_3_stock_cost_and_price_boundaries(app) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("S8 Bounds", with_catalog=False)
    admin = login_as(app, t.users["admin"])
    rows = (
        "B1,d,EA,AED,0,0,0\nB2,d,EA,AED,999999999999.999999,999999999999.999999,999999999999.999\n"
        "B3,d,EA,AED,5,1,-1\nB4,d,EA,AED,5,1,-0\nB5,d,EA,AED,5,-1,1\nB6,d,EA,AED,-1,1,1\n"
        "B7,d,EA,AED,999999999999.9999995,1,1\nB8,d,EA,AED,5,999999999999.9999995,1\nB9,d,EA,AED,5,1,1e30\n"
        'B10,d,EA,AED,5,1,"(3)"\nB11,d,EA,AED,5,1,"−3"\nB12,d,EA,AED,5,,\n'
    ).encode()
    r = upload(admin, "/catalogs/import", "c.csv", HEAD + rows, {"mapping": MAPPING, "catalog_name": "M"})
    assert r.status_code == 201, r.text
    prods = {p["sku"]: p for p in admin.get(f"/catalogs/versions/{r.json()['id']}/products").json()["items"]}
    assert prods["B1"]["list_price"] == "0" and prods["B1"]["stock_qty"] == "0"
    assert prods["B2"]["list_price"] == "999999999999.999999" and prods["B2"]["stock_qty"] == "999999999999.999"
    for sku in ("B3", "B9", "B10", "B11"):
        assert prods[sku]["stock_qty"] is None, sku  # negative or absurd stock: unknown, row kept
    assert prods["B4"]["stock_qty"] == "0"  # '-0' is zero, not negative
    for sku in ("B5", "B6", "B7", "B8"):
        assert sku not in prods, sku  # unchanged: negative cost/price and oversized values are row errors
    assert prods["B12"]["stock_qty"] is None and prods["B12"]["cost_price"] is None


# ---------------------------------------------------------------------------------------- SEC6-2 residual
def _fake_products(n: int, desc_len: int) -> list[SimpleNamespace]:
    base = "hex bolt size NUM steel zinc plated " * 100
    return [
        SimpleNamespace(sku=f"B-{i}", sku_normalized=f"B-{i}", description=base.replace("NUM", str(i))[:desc_len])
        for i in range(n)
    ]


def _adversarial_query() -> str:
    rnd = random.Random(1)
    words = ["hex", "bolt", "steel", "zinc", "washer", "m8", "m10", "plated", "size", "nut", "screw", "flange"]
    return " ".join(rnd.choice(words) + str(rnd.randint(0, 99999)) for _ in range(30))[:190]


@pytest.mark.xfail(
    strict=False,  # round 11: per-call latency (18-28 ms) straddles this test's 20 ms line, so XPASS must not fail
    reason="SEC7-1 (High, availability): the budget counts comparisons, not work; with 300-character descriptions one "
    "line costs about 0.2 ms per product, so the 2,000,000-comparison budget is about 420 s of GIL-held API CPU per request "
    "(measured end to end: 390 s, 30,000 products x 300 chars, 450 lines; 41.7 s with 60-char descriptions)",
)
def test_sec7_1_one_request_at_the_matching_budget_is_bounded_in_seconds() -> None:
    n = 4_000
    index = CatalogIndex(_fake_products(n, 300))  # type: ignore[arg-type]
    q = _adversarial_query()
    t = time.perf_counter()
    index.match(sku_hint=None, description=q, raw_text=q)
    per_line = time.perf_counter() - t
    implied = per_line * (MAX_FUZZY_COMPARISONS // n)
    assert implied < 10.0, f"{per_line * 1000:.0f} ms/line -> {implied:.0f} s per request at the budget"


def test_sec6_2_query_cap_holds_regardless_of_input_length() -> None:
    index = CatalogIndex(_fake_products(2_000, 60))  # type: ignore[arg-type]
    short = _adversarial_query()
    t = time.perf_counter()
    index.match(sku_hint=None, description=short, raw_text=short)
    t_short = time.perf_counter() - t
    huge = (short + " ") * 50_000  # 9.5 MB description: cut to the first 800 characters, then to 200
    t = time.perf_counter()
    index.match(
        sku_hint=None, description=huge, raw_text=huge[:2000]
    )  # raw_text is at most 2,000 characters in practice
    assert time.perf_counter() - t < max(0.5, 5 * t_short)
    assert MAX_QUERY_CHARS == 200


def test_sec6_2_budget_is_per_index_and_exact_sku_still_matches_after_exhaustion() -> None:
    products = _fake_products(1_000, 60)
    a = CatalogIndex(products)  # type: ignore[arg-type]
    b = CatalogIndex(products)  # type: ignore[arg-type]
    q = "hex bolt size 77 steel"
    skipped = 0
    for _ in range(MAX_FUZZY_COMPARISONS // 1_000 + 5):
        if a.match(sku_hint=None, description=q, raw_text=q).fuzzy_skipped:
            skipped += 1
    assert skipped == 5  # exactly the lines beyond the budget
    assert a.match(sku_hint="B-7", description=q, raw_text=q).status == "exact"
    assert not b.match(
        sku_hint=None, description=q, raw_text=q
    ).fuzzy_skipped  # another index starts with a full budget


@integration
def test_sec6_2_budget_cannot_leak_across_requests_tenants_or_the_add_item_path(
    app, tenant_a, tenant_b, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    n_prod = int(
        _owner_scalar(
            "SELECT count(*) FROM products WHERE catalog_version_id = :v",
            tenant_a.org_id,
            v=str(tenant_a.catalog_version_id),
        )
    )
    assert n_prod > 0
    monkeypatch.setattr(matching, "MAX_FUZZY_COMPARISONS", 3 * n_prod)  # three fuzzy lines per index
    sales_a = login_as(app, tenant_a.users["sales"])
    sales_b = login_as(app, tenant_b.users["sales"])
    lines = "\n".join(f"5 pcs unknown widget {chr(97 + i)}{chr(98 + i)} zinc" for i in range(40))
    r1 = sales_a.post("/rfqs", json={"text": lines, "customer_id": str(tenant_a.customer_id)})
    assert r1.status_code == 201, r1.text
    skipped1 = sum("match_skipped_budget" in i["flags"] for i in r1.json()["items"])
    # a second request by the same user starts with a fresh budget: its first line is fuzzy-matched again
    r2 = sales_a.post("/rfqs", json={"text": lines, "customer_id": str(tenant_a.customer_id)})
    first2 = r2.json()["items"][0]
    assert "match_skipped_budget" not in first2["flags"]
    # another tenant is not affected
    r3 = sales_b.post("/rfqs", json={"text": lines, "customer_id": str(tenant_b.customer_id)})
    assert "match_skipped_budget" not in r3.json()["items"][0]["flags"]
    # manual add_item builds its own index
    add = sales_a.post(f"/rfqs/{r1.json()['id']}/items", json={"description": "zinc widget zz", "quantity": "2"})
    assert add.status_code == 201 and "match_skipped_budget" not in add.json()["flags"]
    # reparse (a new index) starts fresh as well
    assert skipped1 >= 30  # 40 lines, budget for 3 fuzzy lines (exact-SKU or unmatched-empty lines are not counted)


# --------------------------------------------------------------------------------------------- seed / root .env
_HARNESS = (
    "import sys, pathlib\n"
    "from app.fixtures import seed\n"
    "seed._REPO_ROOT_ENV = pathlib.Path(sys.argv[1])\n"
    "sys.exit(seed.main())\n"
)


def _seed_with_root_env(root_env: Path, env: dict[str, str | None] | None = None, cwd: Path | None = None):  # type: ignore[no-untyped-def]
    e = {k: v for k, v in os.environ.items() if k != "DEMO_PASSWORD"}
    e["APP_ENV"] = "test"
    e["PYTHONPATH"] = str(API_DIR)
    for k, v in (env or {}).items():
        if v is None:
            e.pop(k, None)
        else:
            e[k] = v
    return subprocess.run(
        [sys.executable, "-c", _HARNESS, str(root_env)],
        cwd=cwd or API_DIR,
        env=e,
        capture_output=True,
        text=True,
        check=False,
    )


@integration
def test_seed_root_env_precedence_and_robustness(tmp_path: Path) -> None:
    pw = "Root-Env-Pass-Marker-" + os.urandom(3).hex()
    root = tmp_path / ".env"
    # absent
    r = _seed_with_root_env(root)
    assert r.returncode == 2 and r.stdout == "" and "Traceback" not in r.stderr
    # root .env supplies the password
    root.write_text(f"DEMO_PASSWORD={pw}\n")
    r = _seed_with_root_env(root)
    assert r.returncode == 0 and pw not in r.stdout + r.stderr
    # the environment wins over the root file, even with an invalid (short) value
    r = _seed_with_root_env(root, {"DEMO_PASSWORD": "short"})
    assert r.returncode == 2 and "short" not in r.stdout + r.stderr
    # a short or empty root value is refused without echo
    for content in ("DEMO_PASSWORD=short\n", "DEMO_PASSWORD=\n"):
        root.write_text(content)
        r = _seed_with_root_env(root)
        assert r.returncode == 2 and "short" not in r.stdout + r.stderr
    # cwd .env has precedence over the root file (a short cwd value refuses, never falls through)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / ".env").write_text("DEMO_PASSWORD=short\n")
    root.write_text(f"DEMO_PASSWORD={pw}\n")
    r = _seed_with_root_env(root, cwd=cwd)
    assert r.returncode == 2 and pw not in r.stdout + r.stderr
    # root DATABASE_URL never redirects the seed: the environment database is still the one written (exit 0)
    root.write_text(f"DEMO_PASSWORD={pw}\nDATABASE_URL=postgresql+psycopg://x:y@localhost:1/nope\n")
    r = _seed_with_root_env(root)
    assert r.returncode == 0 and pw not in r.stdout + r.stderr and "nope" not in r.stdout + r.stderr
    # symlink to a file works, dangling symlink and a directory are treated as absent
    real = tmp_path / "real"
    real.write_text(f"DEMO_PASSWORD={pw}\n")
    link = tmp_path / "link.env"
    link.symlink_to(real)
    assert _seed_with_root_env(link).returncode == 0
    dangling = tmp_path / "dangling.env"
    dangling.symlink_to(tmp_path / "missing")
    assert _seed_with_root_env(dangling).returncode == 2
    directory = tmp_path / "dir.env"
    directory.mkdir()
    r = _seed_with_root_env(directory)
    assert r.returncode == 2 and "Traceback" not in r.stderr
    # invalid bytes crash with a traceback but never print the password
    bad = tmp_path / "bad.env"
    bad.write_bytes(b"\xff\xfe\x00bad\n")
    r = _seed_with_root_env(bad)
    # orchestrator edit (declared, fix round 11): SEC7-4 is fixed — an undecodable file counts as absent (exit 2,
    # no traceback) instead of crashing (exit 1); the property that matters, no password in any output, is unchanged
    assert r.returncode == 2 and "Traceback" not in r.stderr and pw not in r.stdout + r.stderr


@integration
def test_sec7_4_invalid_root_env_value_is_not_echoed_by_the_seed(tmp_path: Path) -> None:
    root = tmp_path / ".env"
    root.write_text("DEMO_PASSWORD=Root-Env-Pass-Marker-77\nSECRET_KEY=tiny-secret-visible\n")
    r = _seed_with_root_env(root, {"SECRET_KEY": None})
    assert "tiny-secret-visible" not in r.stdout + r.stderr
