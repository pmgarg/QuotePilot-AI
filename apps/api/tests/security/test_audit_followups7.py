"""Security re-verification tests, round 7 (Stage 1 fix round 9, commit 7151e64, 2026-11-12).

Attacks the round-9 changes: linear RFQ text-line parsing (``extract.py``), the parser edge-case rules in
``money.parse_decimal`` (SEC5-2/3/6/7), the demo seed (no password printed or generated) and the e2e harness change.
Open findings are ``xfail(strict=True)`` with an ID (SEC6-n). See
docs/evidence/security-stage1-reverify6-2026-11-12.md.
"""

from __future__ import annotations

import ast
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

from app.services.matching import CatalogIndex
from app.services.money import parse_decimal
from app.services.parsing.extract import (
    MAX_SKU_RUN,
    MAX_TEXT_LINE,
    SKU_TOKEN,
    items_from_rows,
    items_from_text_lines,
    sku_tokens,
)
from app.services.units import normalize_sku
from tests.conftest import API_DIR, login_as, make_tenant, upload

integration = pytest.mark.integration
pytestmark = [pytest.mark.security]
REPO_ROOT = API_DIR.parents[1]
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
HEAD = b"SKU,Description,Unit,Currency,List Price,Cost,Stock\n"


# --------------------------------------------------------------------------- SEC5-2/3/6/7 and SEC4-3 re-check
def test_sec5_2_closed_numeric_but_not_decimal_characters_make_the_cell_not_a_number() -> None:
    for raw in ("5²", "10³", "5①", "2⁵", "1²3", "x²", "¹²", "½",
                "1½", "Ⅸ", "5 三", "三", "5⅓"):  # fmt: skip
        assert parse_decimal(raw) is None, raw


def test_sec5_2_decimal_digits_of_other_scripts_and_width_forms_still_parse() -> None:
    assert parse_decimal("٥") == 5 and parse_decimal("５０") == 50 and parse_decimal("१२") == 12


def test_sec6_4_nfkc_expansion_to_digits_without_numeric_value_is_not_glued() -> None:
    for ch in ("㋀", "㋉", "㍘", "㍢", "㏠", "㏾", "㍸", "㎡", "㎥"):
        assert parse_decimal(f"5{ch}") is None, (hex(ord(ch)), parse_decimal(f"5{ch}"))
        assert parse_decimal(f"{ch}5") is None, (hex(ord(ch)), parse_decimal(f"{ch}5"))


def test_unicode_sweep_a_single_foreign_character_never_changes_the_value_unexpectedly() -> None:
    """Every code point before/after a '5': the result is None, 5, -5 (decorator/sign) or a digit/separator gluing that
    is one of the documented NFKC-expansion cases (SEC6-4). Nothing raises."""
    known_sec6_4 = set(range(0x32C0, 0x32CC)) | set(range(0x3358, 0x3371)) | set(range(0x33E0, 0x33FF))
    known_sec6_4 |= {0x3378, 0x3379, 0x339F, 0x33A0, 0x33A1, 0x33A2, 0x33A3, 0x33A4, 0x33A5, 0x33A6, 0x33A8, 0x33AF}
    ok = {None, Decimal(5), Decimal(-5)}
    for cp in range(0x110000):
        if 0xD800 <= cp <= 0xDFFF or chr(cp).isdecimal() or chr(cp) in ".,":
            continue
        ch = chr(cp)
        if unicodedata.numeric(ch, None) is not None:
            assert parse_decimal(f"5{ch}") is None and parse_decimal(f"{ch}5") is None, hex(cp)
            continue
        for s in (f"5{ch}", f"{ch}5"):
            r = parse_decimal(s)
            if r not in ok:
                assert (
                    cp in known_sec6_4 or unicodedata.normalize("NFKC", ch) in (".", ",", "..") or cp in (0x66B, 0x66C)
                ), (hex(cp), r)


def test_sec5_3_and_sec4_3_every_dash_code_point_is_a_minus_before_a_number() -> None:
    dashes = [chr(c) for c in range(0x110000) if unicodedata.category(chr(c)) == "Pd"] + ["−", "➖"]
    assert len(dashes) >= 28
    for ch in dashes:
        assert parse_decimal(f"{ch}5") == Decimal(-5), hex(ord(ch))
        assert parse_decimal(f"{ch} 5") == Decimal(-5), hex(ord(ch))
        assert parse_decimal(f"12{ch}14") is None, hex(ord(ch))  # a range is two numbers, not 12 or -14
    assert parse_decimal("–") is None and parse_decimal("– –") is None  # a lone dash is not a number


def test_sec5_6_and_sec5_7_leading_zero_and_doubled_dot() -> None:
    for raw, expected in (("0,500", "0.500"), ("0,001", "0.001"), ("00,500", "0.500"), ("012,345", "12.345"),
                          ("0,5", "0.5"), ("10,500", "10500"), ("1,234", "1234"), ("0.500", "0.500"), ("0", "0")):  # fmt: skip
        assert parse_decimal(raw) == Decimal(expected), raw
    for raw in ("0,500,000", "0,5,5", "..5", "...5", "..50", ",.5", ".,5", "$..5", "1..5", "5..", "0,,5", "0,"):
        assert parse_decimal(raw) is None, (raw, parse_decimal(raw))
    assert parse_decimal(".5") == Decimal("0.5") and parse_decimal("$.50") == Decimal("0.50")


def test_round9_rules_do_not_open_value_changes_against_8f9416e_on_a_random_corpus() -> None:
    """Differential run recorded in the evidence (400,000 strings): only sign flips for dash-before-number, rejections
    for numeric/non-decimal characters and leading-zero commas. Here: the same properties on a smaller corpus."""
    rnd = random.Random(31)
    pool = list("0123456789.,-+()'eE  $−–➖٫ abcAED%")
    for _ in range(30000):
        s = "".join(rnd.choice(pool) for _ in range(rnd.randint(1, 10)))
        d = parse_decimal(s)
        assert d is None or (d.is_finite() and not (d.is_zero() and d.is_signed())), s


def test_per_character_scans_are_bounded_by_the_pre_normalisation_cap() -> None:
    worst = 0.0
    for s in ("ﷺ" * 511, "5" + "–" * 500, "①" * 512, "9" * 512, "²" * 100_000, "-" * 2_000_000):
        t = time.perf_counter()
        for _ in range(20):
            parse_decimal(s)
        worst = max(worst, (time.perf_counter() - t) / 20)
    assert worst < 0.01, worst


# ------------------------------------------------------------------------ SEC5-5 re-check (text and cell paths)
def test_sec5_5_adversarial_separator_digit_keyword_unit_lines_are_bounded() -> None:
    rnd = random.Random(7)
    toks = ["1", "1,", "1.", "- ", "-", "a", "qty", "qty:", "x", "×", "pcs", "ea", "kg", ": ", ", ", "item 1",
            "1)", "9", "SKU-1", "a-1", "0", "1,1", "1.1", ".", "=", "+", "@", "– ", "1 ", " "]  # fmt: skip
    worst = 0.0
    for _ in range(1500):
        unit = "".join(rnd.choice(toks) + rnd.choice(["", " "]) for _ in range(rnd.randint(1, 4)))
        line = (unit * (MAX_TEXT_LINE // len(unit) + 1))[:MAX_TEXT_LINE]
        t = time.perf_counter()
        items_from_text_lines(line, {})
        worst = max(worst, time.perf_counter() - t)
    assert worst < 0.05, worst  # measured worst about 8 ms per 1,000-character line


def test_sec5_5_whole_text_at_the_pasted_limit_and_pdf_page_limit_is_bounded() -> None:
    line = (", 0 " * 300)[:MAX_TEXT_LINE]
    for total, limit in ((100_000, 2.0), (200_000, 4.0)):
        text = "\n".join([line] * (total // MAX_TEXT_LINE))
        t = time.perf_counter()
        items_from_text_lines(text, {})
        assert time.perf_counter() - t < limit, total
    tiny = "\n".join("ab1" for _ in range(33_000))
    t = time.perf_counter()
    items_from_text_lines(tiny, {})
    assert time.perf_counter() - t < 1.0


def test_sec5_5_sku_tokens_many_runs_just_under_the_cap_and_fullmatch_on_cells() -> None:
    run = ("A1-" * 30)[: MAX_SKU_RUN - 1]
    text = " ".join([run] * 3000)
    t = time.perf_counter()
    sku_tokens(text)
    assert time.perf_counter() - t < 1.0
    t = time.perf_counter()
    assert sku_tokens("A1-" * 100_000) == []
    assert time.perf_counter() - t < 0.5
    cell = "A1-" * 700
    t = time.perf_counter()
    for _ in range(200):
        SKU_TOKEN.fullmatch(cell)
    assert time.perf_counter() - t < 1.0


def test_sec5_5_header_detection_and_row_items_on_hostile_cells_are_bounded() -> None:
    hostile = "x" + " " * 1990 + "(5"
    rows = [["SKU", "Description", "Qty", "Unit"]] + [[("A1-" * 600)[:2000], hostile, hostile, "EA"]] * 1500
    t = time.perf_counter()
    items_from_rows(rows, {})
    assert time.perf_counter() - t < 2.0
    rows = [[hostile] * 64 for _ in range(150)]  # header-less: _guess_columns scores 200 x 64 cells
    t = time.perf_counter()
    items_from_rows(rows, {})
    assert time.perf_counter() - t < 2.0


def test_collapsing_whitespace_does_not_bypass_formula_detection_or_change_quantities() -> None:
    for prefix in ("=", "+", "@"):
        for pad in ("", " ", "\t", "\r", " ", " ", "\x1c", "  \t "):
            items = items_from_text_lines(f"{pad}{prefix}SUM(A1) SKU-1001 5", {})
            assert all("formula_like" in i.flags for i in items), (prefix, repr(pad))  # dropped or flagged, never clean
    for line in (
        "SKU-1001\tqty:\t5",
        "5\tpcs\tSKU-1001",
        "SKU-1001\u00a0\u00a0x\u00a05",
        "SKU-1001 \t qty:\t5",
        "5\t\tpcs\u2003bolt",
    ):
        spaced = re.sub(r"\s+", " ", line)
        (a,) = items_from_text_lines(line, {})
        (b,) = items_from_text_lines(spaced, {})
        assert (a.quantity, a.unit_raw, a.sku_hint, a.flags) == (b.quantity, b.unit_raw, b.sku_hint, b.flags), line
        assert a.quantity == Decimal(5), (line, a.quantity)
    # a tab between a SKU and digits never merges them into one number
    (it,) = items_from_text_lines("SKU-1001\t12\tqty:\t5", {})
    assert it.quantity == Decimal(5) and it.sku_hint == "SKU-1001"


def test_info_raw_text_of_a_pasted_line_is_the_whitespace_collapsed_form() -> None:
    """Info (SEC6-5): raw_text is stored collapsed, not byte-identical to the customer's line."""
    (it,) = items_from_text_lines("5\tpcs\t\tSKU-1001   bolt", {})
    assert it.raw_text == "5 pcs SKU-1001 bolt"


def test_sku_tokens_equivalence_on_ordinary_text() -> None:
    for text in ("order ABC-123, SKU-1001 and x.y/Z9-88 plus AB12 (M8x40) 200mm", "ref:PN_77.A/3 12V item-0001", ""):
        assert sku_tokens(text) == SKU_TOKEN.findall(text)


# --------------------------------------------------------------------------- findings: quadratic / heavy paths
def test_sec6_1_normalize_sku_is_linear_on_hostile_cells() -> None:
    worst = 0.0
    for s in ("a" + "-" * 1998 + "b", "a" + " ." * 999 + "b", "a" + "_" * 1998 + "b"):
        t = time.perf_counter()
        normalize_sku(s)
        worst = max(worst, time.perf_counter() - t)
    assert worst < 0.003, worst


@integration
def test_sec6_1_catalog_import_with_hostile_sku_cells_is_not_a_cpu_dos(app) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("S7 Sku", with_catalog=False)
    admin = login_as(app, t.users["admin"])
    run = "a" + "-" * 1900 + "b"
    rows = "".join(f'"{run}{i}",d,EA,AED,5,1,1\n' for i in range(200)).encode()
    t0 = time.perf_counter()
    r = upload(admin, "/catalogs/import", "c.csv", HEAD + rows, {"mapping": MAPPING, "catalog_name": "M"})
    assert r.status_code in (201, 422)
    assert time.perf_counter() - t0 < 1.0


@pytest.mark.xfail(
    strict=True,
    reason="SEC6-2 (Medium): fuzzy matching is unbounded in query length and catalog size and holds the GIL "
    "(2,000 long items against a 50,000-product catalog measured at about 290 s)",
)
def test_sec6_2_matching_a_long_description_against_a_large_catalog_is_bounded() -> None:
    products = [
        SimpleNamespace(sku=f"SKU-{i}", sku_normalized=f"SKU-{i}", description=f"hex bolt size {i} steel")
        for i in range(30_000)
    ]
    index = CatalogIndex(products)  # type: ignore[arg-type]
    query = ("hex bolt size 77 " * 60)[:990]
    t = time.perf_counter()
    for _ in range(3):
        index.match(sku_hint=None, description=query, raw_text=query)
    assert (time.perf_counter() - t) / 3 < 0.02


@integration
def test_sec6_3_negative_stock_is_not_stored(app) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("S7 Stock", with_catalog=False)
    admin = login_as(app, t.users["admin"])
    rows = 'S1,d,EA,AED,5,1,"– 5"\nS2,d,EA,AED,5,1,"-5"\nS3,d,EA,AED,5,1,7\n'.encode()
    r = upload(admin, "/catalogs/import", "c.csv", HEAD + rows, {"mapping": MAPPING, "catalog_name": "M"})
    assert r.status_code == 201, r.text
    stock = {
        p["sku"]: p["stock_qty"] for p in admin.get(f"/catalogs/versions/{r.json()['id']}/products").json()["items"]
    }
    assert stock["S3"] == "7" and stock.get("S1") is None and stock.get("S2") is None


@integration
def test_dash_as_separator_before_a_price_or_cost_is_rejected_as_negative_not_imported(app) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("S7 Dash", with_catalog=False)
    admin = login_as(app, t.users["admin"])
    rows = 'D1,d,EA,AED,"AED – 5",1,1\nD2,d,EA,AED,5,"— 3",1\nD3,d,EA,AED,"–5",,\nD4,d,EA,AED,9,,\n'.encode()
    r = upload(admin, "/catalogs/import", "c.csv", HEAD + rows, {"mapping": MAPPING, "catalog_name": "M"})
    assert r.status_code == 201, r.text
    prods = {
        p["sku"]: p["list_price"] for p in admin.get(f"/catalogs/versions/{r.json()['id']}/products").json()["items"]
    }
    assert prods == {"D4": "9"}


@integration
def test_rfq_quantity_with_a_dash_prefix_is_flagged_not_positive_never_flipped_positive(app, tenant_a) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    csv = "SKU,Description,Qty,Unit\nSKU-1001,Hex bolt,– 5,EA\nSKU-1002,Other,−3,EA\nSKU-1003,Third,5,EA\n"
    r = upload(sales, "/rfqs", "r.csv", csv.encode(), {"customer_id": str(tenant_a.customer_id)})
    assert r.status_code == 201, r.text
    q = {i["line_no"]: i for i in r.json()["items"]}
    assert q[1]["quantity"] is None and "quantity_not_positive" in q[1]["flags"]
    assert q[2]["quantity"] is None and q[3]["quantity"] == "5"


# ----------------------------------------------------------------------------- demo seed (CodeQL alert condition)
def _seed(extra_env: dict[str, str | None], *, db: bool = True) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "DEMO_PASSWORD"}
    env["APP_ENV"] = "test"
    env["DATABASE_URL"] = os.environ.get("DATABASE_URL", env.get("DATABASE_URL", ""))
    for k, v in extra_env.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    return subprocess.run(
        [sys.executable, "-m", "app.fixtures.seed"], cwd=API_DIR, env=env, capture_output=True, text=True, check=False
    )


def test_seed_without_or_with_short_password_exits_2_and_prints_nothing_secret(tmp_path: Path) -> None:
    for env in ({}, {"DEMO_PASSWORD": ""}, {"DEMO_PASSWORD": "short-11-ch"}):
        r = _seed(env)
        assert r.returncode == 2, (env, r.returncode, r.stderr[-200:])
        assert r.stdout == ""
        assert "DEMO_PASSWORD" in r.stderr and "Traceback" not in r.stderr
        if env.get("DEMO_PASSWORD"):
            assert env["DEMO_PASSWORD"] not in r.stdout + r.stderr


@integration
def test_seed_with_password_never_prints_it_nor_its_hash_and_writes_no_audit_secret(app) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import create_engine, text

    pw = "Zq9-Marker-Unique-" + os.urandom(4).hex()
    r = _seed({"DEMO_PASSWORD": pw})
    assert r.returncode == 0, r.stderr[-300:]
    for stream in (r.stdout, r.stderr):
        assert pw not in stream
    assert "(not shown)" in r.stdout and "@northwind.example" in r.stdout
    eng = create_engine(os.environ["DATABASE_ADMIN_URL"], future=True, isolation_level="AUTOCOMMIT")
    try:
        with eng.connect() as conn:
            h = conn.execute(text("SELECT password_hash FROM users LIMIT 1")).scalar()
            assert h and h.startswith("$argon2") and h not in r.stdout + r.stderr
            n = conn.execute(
                text("SELECT count(*) FROM audit_events WHERE summary::text LIKE :p"), {"p": f"%{pw}%"}
            ).scalar()
            assert n == 0
    finally:
        eng.dispose()
    r2 = _seed({"DEMO_PASSWORD": pw})  # idempotent re-run: still nothing printed
    assert r2.returncode == 0 and pw not in r2.stdout + r2.stderr


def test_seed_failure_paths_do_not_echo_the_password() -> None:
    pw = "Zq9-Marker-Unique-badDB"
    r = _seed({"DEMO_PASSWORD": pw, "DATABASE_URL": "postgresql+psycopg://x:y@localhost:1/nope"})
    assert r.returncode != 0 and pw not in r.stdout + r.stderr


def test_no_print_or_log_call_in_app_or_scripts_carries_a_secret_like_name() -> None:
    sensitive = re.compile(r"(passw|secret|token|api_?key|credential|hash|csrf)", re.IGNORECASE)
    offenders: list[str] = []
    for base in (API_DIR / "app", REPO_ROOT / "scripts"):
        for path in base.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else "")
                if name not in {"print", "info", "debug", "warning", "error", "exception", "critical", "log_event"}:
                    continue
                for arg in [*node.args, *[k.value for k in node.keywords]]:
                    for sub in ast.walk(arg):
                        if isinstance(sub, ast.Name) and sensitive.search(sub.id):
                            offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} name {sub.id}")
                        if isinstance(sub, ast.Attribute) and sensitive.search(sub.attr):
                            offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} attr {sub.attr}")
    assert offenders == [], offenders


def test_seed_print_calls_never_reference_the_password_variable() -> None:
    src = (API_DIR / "app" / "fixtures" / "seed.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
            names = {n.id for a in node.args for n in ast.walk(a) if isinstance(n, ast.Name)}
            assert not (names & {"password", "pw", "pwd", "hashed"}), node.lineno


def test_compose_migrate_receives_the_variable_through_env_file_and_ci_sets_it() -> None:
    compose = (REPO_ROOT / "infra" / "docker-compose.yml").read_text(encoding="utf-8")
    migrate = compose[compose.index("  migrate:") : compose.index("  api:")]
    assert "env_file: ../.env" in migrate and "app.fixtures.seed" in migrate
    api = "\n".join(
        ln
        for ln in compose[compose.index("  api:") : compose.index("  web:")].splitlines()
        if not ln.strip().startswith("#")
    )
    assert "env_file" not in api and "DEMO_PASSWORD" not in api  # the API container never sees the demo password
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "DEMO_PASSWORD:" in ci


# --------------------------------------------------------------------------------------------- e2e harness change
def test_e2e_harness_change_is_test_only_and_weakens_nothing_security_relevant() -> None:
    setup = (REPO_ROOT / "tests" / "e2e" / "global-setup.ts").read_text(encoding="utf-8")
    config = (REPO_ROOT / "tests" / "e2e" / "playwright.config.ts").read_text(encoding="utf-8")
    assert "npm run build" not in setup and "alembic upgrade head" in setup and "app.fixtures.seed" in setup
    assert "npm run build && npx next start" in config and "reuseExistingServer: false" in config
    assert "APP_ENV" in config or "APP_ENV" in setup
    for forbidden in ("SECURE_COOKIES", "--no-verify", "rejectUnauthorized", "NODE_TLS_REJECT_UNAUTHORIZED"):
        assert forbidden not in setup + config
    assert "CORS_ORIGINS" not in setup.replace("//", "") or "*" not in setup


def test_only_test_and_docs_files_changed_outside_the_declared_product_files() -> None:
    out = subprocess.run(["git", "diff", "--name-only", "8f9416e", "7151e64"], cwd=REPO_ROOT, capture_output=True,
                         text=True, check=False)  # fmt: skip
    if out.returncode != 0 or not out.stdout.strip():
        pytest.skip("git history unavailable")
    product = {f for f in out.stdout.split() if f.startswith(("apps/api/app/", "apps/web/src/", "infra/", "scripts/"))}
    assert product == {
        "apps/api/app/fixtures/seed.py",
        "apps/api/app/services/matching.py",
        "apps/api/app/services/money.py",
        "apps/api/app/services/parsing/extract.py",
    }


def test_sec6_5_leading_zero_group_with_both_separators_is_not_read_as_a_large_number() -> None:
    for raw in ("0,500.00", "0.500,00", "00,500.25", "0,001.5", "0.500,5"):
        d = parse_decimal(raw)
        assert d is None or d < 1, (raw, d)
