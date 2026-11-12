"""QA (independent) re-verification 7 of Stage 1 at 8258112 (fix round 10).

Derived from CLAUDE.md / PRD ("never invent prices, stock or customer facts"; parse failures are reported, not guessed),
docs/API_CONTRACT.md and the re-verification-6 list. Scope: git diff 46c3bcb..8258112 (two-separator numbers,
non-ASCII symbols, negative stock, linear normalize_sku, matching budget, seed reading the repo-root .env).
Unit tests, integration tests (API + PostgreSQL + RLS through the ASGI TestClient, seed as a subprocess).
"""

from __future__ import annotations

import csv
import os
import random
import re
import subprocess
import sys
import time
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.services import matching as new_matching
from app.services.money import parse_decimal
from app.services.units import normalize_sku
from tests.conftest import API, API_DIR, login_as, make_tenant, upload
from tests.integration.test_catalog import MAPPING

integration = pytest.mark.integration
ROOT = API_DIR.parent.parent

# ------------------------------------------------------------------------------------------- 1. two separators
WELL_FORMED = [
    ("1,234.56", D("1234.56")),
    ("1.234,56", D("1234.56")),
    ("12,345.6789", D("12345.6789")),
    ("1,234.5", D("1234.5")),
    ("123,456.7", D("123456.7")),
    ("1,234,567.89", D("1234567.89")),
    ("1.234.567,89", D("1234567.89")),
    ("1,000,000.00 AED", D(1000000)),
    ("AED 1,000,000.00", D(1000000)),
    ("$ 1,234.56", D("1234.56")),
    ("-1,234.56", D("-1234.56")),
    ("(1,234.56)", D("-1234.56")),
    ("€1.234,56", D("1234.56")),
    ("1,234.56 pcs", D("1234.56")),
    ("1,00,000.50", D("100000.50")),
    ("10,00,000.00", D(1000000)),
    ("1,00,00,000.5", D("10000000.5")),
    ("9,999.999", D("9999.999")),
    ("9.999,999", D("9999.999")),
    ("123,456,789.01", D("123456789.01")),
    ("1,234,567,890.12", D("1234567890.12")),
    ("1.234.567.890,12", D("1234567890.12")),
    ("12.345,6", D("12345.6")),
    ("99,999.9", D("99999.9")),
    # comma-only and dot-only paths (unchanged)
    ("1,234,567", D(1234567)),
    ("1,234", D(1234)),
    ("12,5", D("12.5")),
    ("0,500", D("0.5")),
    ("1.234", D("1.234")),
]


@pytest.mark.parametrize(("raw", "expected"), WELL_FORMED, ids=[r for r, _ in WELL_FORMED])
def test_round10_well_formed_numbers_keep_their_value(raw: str, expected: D) -> None:
    got = parse_decimal(raw)
    assert got is not None and got == expected, (raw, got)


MALFORMED = [
    "1234.567,89",
    "1234,567.89",
    "12,34.56",
    "1,2345.67",
    "1,23.4",
    "123,45.6",
    "1234,567.8",
    "1,234,56.7",
    ".5,0",
    "5.,0",
    "5,.0",
    "1,.5",
    "1.,5",
    "1,5.5",
    "1.5,5",
    "0,5.5",
    "0.5,5",
    "1.2,3",
    "0,500.00",
    "0.500,00",
    "00,500.25",
    "0,001.5",
    "0.500,5",
    "1,234.56.78",
    "1.234,56,78",
    "1,234.567.890",
    "5.5,0",
]


@pytest.mark.parametrize("raw", MALFORMED, ids=MALFORMED)
def test_round10_malformed_two_separator_cells_are_rejected(raw: str) -> None:
    assert parse_decimal(raw) is None, (raw, parse_decimal(raw))


def test_round10_every_symbol_whose_compatibility_form_contains_a_digit_makes_the_cell_not_a_number() -> None:
    """Exhaustive over the whole Unicode range: any non-ASCII, non-decimal character whose NFKC form has a decimal digit
    must make '5<c>', '<c>5', '<c>' and 'AED 12<c>' unreadable (no digit is glued to the number)."""
    import unicodedata

    offenders = []
    checked = 0
    for cp in range(0x80, 0x110000):
        ch = chr(cp)
        if ch.isdecimal() or 0xD800 <= cp <= 0xDFFF:
            continue
        if not any(c.isdecimal() for c in unicodedata.normalize("NFKC", ch)):
            continue
        checked += 1
        for cell in (f"5{ch}", f"{ch}5", ch, f"AED 12{ch}"):
            got = parse_decimal(cell)
            if got is not None:
                offenders.append((hex(cp), cell, got))
    assert checked > 100 and not offenders, (checked, offenders[:10])


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("12 \u2103", D(12)), ("5\u2122", D(5)), ("12 \u2116", D(12)), ("\u00b050", D(50)), ("12 \u00b0C", D(12))],
)
def test_round10_harmless_symbols_without_digits_still_parse(raw: str, expected: D) -> None:
    assert parse_decimal(raw) == expected, (raw, parse_decimal(raw))


def test_round10_two_separator_rule_changes_only_rejections_fuzz() -> None:
    """Fuzz: whatever parses is still finite, bounded, and keeps the digit sequence (no new invention)."""
    import unicodedata

    toks = ["1", "2", "5", "0", "12", "345", "000", ".", ",", " ", "-", "(", ")", "AED", "$", "pcs", "²", "㎡", "㋀"]
    rnd = random.Random(20261114)
    bad: list[tuple[str, Any]] = []
    for _ in range(80000):
        s = "".join(rnd.choice(toks) for _ in range(rnd.randint(1, 8)))
        got = parse_decimal(s)
        if got is None:
            continue
        assert got.is_finite() and abs(got) < D(10) ** 31
        digits = "".join(str(unicodedata.digit(c)) for c in unicodedata.normalize("NFKC", s) if c.isdecimal())
        if "".join(map(str, got.as_tuple().digits)).strip("0") != digits.strip("0"):
            bad.append((s, got))
    assert not bad, bad[:8]


@integration
def test_round10_catalog_import_two_separator_prices(app) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("R7 Price", with_catalog=False)
    admin = login_as(app, t.users["admin"])
    cells = [
        ("G1", "1,234.56", "1234.56"),
        ("G2", "1.234,56", "1234.56"),
        ("G3", "AED 12,345.6789", "12345.6789"),
        ("G4", "1,00,000.50", "100000.50"),
        ("G5", "1,234,567", "1234567"),
        ("B1", "1234.567,89", None),
        ("B2", "12,34.56", None),
        ("B3", "0,500.00", None),
        ("B4", "㎡", None),
        ("B5", "1234,567.89", None),
    ]
    body = "SKU,Description,Unit,Currency,List Price\n" + "".join(f'{s},Item {s},EA,AED,"{c}"\n' for s, c, _ in cells)
    r = upload(admin, "/catalogs/import", "c.csv", body.encode(), {"mapping": MAPPING, "catalog_name": "Main"})
    assert r.status_code == 201, r.text
    prods = {
        p["sku"]: p["list_price"] for p in admin.get(f"/catalogs/versions/{r.json()['id']}/products").json()["items"]
    }
    for sku, _, exp in cells:
        if exp is None:
            assert sku not in prods, (sku, prods.get(sku))
        else:
            assert D(prods[sku]) == D(exp), (sku, prods.get(sku))
    assert r.json()["import_report"]["skipped"] == 5


# ------------------------------------------------------------------------------------------- 3. stock + normalize_sku
@integration
def test_round10_negative_stock_unknown_zero_and_positive_kept(app) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("R7 Stock", with_catalog=False)
    admin = login_as(app, t.users["admin"])
    mapping = (
        '{"sku":"SKU","description":"Description","unit":"Unit","currency":"Currency","list_price":"List Price",'
        '"stock_qty":"Stock"}'
    )
    rows = [
        ("S1", "-5"),
        ("S2", "(5)"),
        ("S3", "– 5"),
        ("S4", "0"),
        ("S5", "-0"),
        ("S6", "12,5"),
        ("S7", "1,234"),
        ("S8", "1e3"),
        ("S9", "abc"),
        ("S10", "10000000000000000"),
        ("S11", "-0,5"),
        ("S12", ""),
    ]
    body = "SKU,Description,Unit,Currency,List Price,Stock\n" + "".join(
        f'{s},Item {s},EA,AED,5,"{c}"\n' for s, c in rows
    )
    r = upload(admin, "/catalogs/import", "c.csv", body.encode(), {"mapping": mapping, "catalog_name": "Main"})
    assert r.status_code == 201, r.text
    items = {p["sku"]: p for p in admin.get(f"/catalogs/versions/{r.json()['id']}/products").json()["items"]}
    assert r.json()["import_report"]["imported"] == 12
    for sku in ("S1", "S2", "S3", "S9", "S10", "S11", "S12"):
        assert items[sku]["stock_qty"] is None, (sku, items[sku]["stock_qty"])
    for sku, exp in (("S4", 0), ("S5", 0), ("S6", D("12.5")), ("S7", 1234), ("S8", 1000)):
        assert items[sku]["stock_qty"] is not None and D(str(items[sku]["stock_qty"])) == D(exp), (
            sku,
            items[sku]["stock_qty"],
        )
    # every unreadable cell (not the empty one) produced a warning
    assert len(r.json()["import_report"]["warnings"]) == 6, r.json()["import_report"]["warnings"]


def _old_normalize_sku(raw: str | None) -> str:
    """Verbatim logic of units.py at 46c3bcb (regex version)."""
    if raw is None:
        return ""
    s = re.sub(r"\s+", " ", str(raw)).strip()
    s = re.sub(r"^[\s\W_]+|[\s\W_]+$", "", s)
    return s.upper()


def test_round10_normalize_sku_equals_the_old_regex_version() -> None:
    cells = [
        "SKU-1001",
        "sku-1001",
        " SKU-1001 ",
        "-SKU-1001-",
        "__SKU_1001__",
        "SKU 1001",
        "SKU  1001",
        "SKU\t1001",
        "SKU 1001",
        "　SKU-1001　",
        "(SKU-1001)",
        "#SKU-1001",
        "'SKU-1001'",
        "ÄB-123",
        "straße-1",
        "ß",
        "ǅ-1",
        "ﬁ-5",
        "ŉ" + "1",
        "İ-1",
        "i̇-1",
        "٣-A",
        "Ａ１",
        "SKU_1001",
        "_",
        "-",
        "",
        "   ",
        "\n",
        "…SKU…",
        "SKU–1001",
        "—SKU—",
        "№" + "5",
        "a b  c",
        "a b",
        "a\x85b",
        "\x1fa\x1f",
        "²",
        "SKU²",
        "½",
        "Ⅳ-1",
        "日本-1",
        "áb",
        "́a",
        "á",
        "​SKU​",
        "﻿SKU-1",
        "1/2",
        "1.5mm",
        "4711-X",
        ".5.",
        "~SKU~",
        "SKU@",
    ]
    rnd = random.Random(3)
    alpha = list("abcAB12-_. \t 　()#'/ßİǅ٣Ａ²½Ⅳ日́​…—™")
    cells += ["".join(rnd.choice(alpha) for _ in range(rnd.randint(0, 14))) for _ in range(100000)]
    bad = [(c, _old_normalize_sku(c), normalize_sku(c)) for c in cells if _old_normalize_sku(c) != normalize_sku(c)]
    assert not bad, bad[:10]
    assert normalize_sku(None) == "" and normalize_sku(123) == "123"  # type: ignore[arg-type]


def test_round10_normalize_sku_is_linear() -> None:
    t0 = time.perf_counter()
    for c in ("-" * 400000 + "A", "A" + "-" * 400000, " " * 400000, "_-" * 200000 + "x" + "_-" * 200000, "́" * 300000):
        normalize_sku(c)
    assert time.perf_counter() - t0 < 1.0


# ------------------------------------------------------------------------------------------- 4. matching
def _prod(i: int, sku: str, desc: str) -> Any:
    return SimpleNamespace(id=i, sku=sku, sku_normalized=normalize_sku(sku), description=desc, unit="EA")


def _old_matching_module() -> Any:
    """matching.py at 46c3bcb, loaded from git history (the only source of the old function)."""
    import importlib.util

    src = subprocess.run(
        ["git", "show", "46c3bcb:apps/api/app/services/matching.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    target = Path(os.environ.get("TMPDIR", "/tmp")) / "qa7_matching_r9.py"
    target.write_text(src)
    spec = importlib.util.spec_from_file_location("qa7_matching_r9", target)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["qa7_matching_r9"] = mod
    spec.loader.exec_module(mod)
    return mod


def _catalog(n: int, seed: int) -> list[Any]:
    r = random.Random(seed)
    nouns = [
        "bolt",
        "nut",
        "washer",
        "screw",
        "cable",
        "grease",
        "pail",
        "gasket",
        "valve",
        "relay",
        "fuse",
        "hose",
        "clamp",
    ]
    adj = [
        "hex",
        "flat",
        "zinc plated",
        "stainless",
        "brass",
        "heavy duty",
        "nylon",
        "M8",
        "M10",
        "DN25",
        "18kg",
        "12V",
        "IP67",
    ]
    return [
        _prod(i, f"{r.choice(['AB', 'CD', 'XY'])}-{1000 + i}", f"{r.choice(nouns)} {r.choice(adj)} {r.choice(adj)}")
        for i in range(n)
    ]


def _key(o: Any) -> Any:
    return (o.status, [(c.product.id, str(c.score), c.category, c.reason, c.matched_on) for c in o.candidates])


def test_round10_matching_unchanged_for_descriptions_up_to_200_characters() -> None:
    old = _old_matching_module()
    demo_rows = list(csv.DictReader(open(API_DIR / "app" / "fixtures" / "data" / "demo_catalog.csv")))
    demo = [_prod(n, r["SKU"], r["Description"]) for n, r in enumerate(demo_rows)]
    rnd = random.Random(11)
    compared = 0
    for cat in (demo, _catalog(1500, 1)):
        oi, ni = old.CatalogIndex(cat), new_matching.CatalogIndex(cat)
        for _ in range(1500):
            p = rnd.choice(cat)
            kind = rnd.randint(0, 3)
            if kind == 0:
                q = p.description
            elif kind == 1:
                q = p.description + " qty"
            elif kind == 2:
                q = f"{rnd.choice(['500 pcs', '10 x', '2 ea'])} {p.description.split()[0]} {rnd.choice(['M8', 'zinc plated', 'brass'])}"
            else:
                q = p.description.upper() + "  (urgent)"
            if len(new_matching.normalize_text(q)) > 200:
                continue
            ni.fuzzy_comparisons = 0  # isolate the cap from the budget
            for sku_hint, raw in ((None, q), (p.sku, q), (None, f"{q} {p.sku}")):
                assert _key(oi.match(sku_hint=sku_hint, description=q, raw_text=raw)) == _key(
                    ni.match(sku_hint=sku_hint, description=q, raw_text=raw)
                ), (q, sku_hint)
                compared += 1
    assert compared > 4000


def test_round10_long_descriptions_are_matched_on_their_first_200_characters_judgement() -> None:
    """Judgement call J7-1: a description longer than 200 normalised characters is matched on its first 200. This often
    turns 'unmatched' (diluted long text) into candidates for a person; information after character 200 is ignored,
    but an exact SKU anywhere in the raw text is still found before the fuzzy step."""
    old = _old_matching_module()
    demo_rows = list(csv.DictReader(open(API_DIR / "app" / "fixtures" / "data" / "demo_catalog.csv")))
    demo = [_prod(n, r["SKU"], r["Description"]) for n, r in enumerate(demo_rows)]
    q = "Hex bolt M8x40 zinc plated " + "please confirm availability and delivery lead time to our warehouse " * 6
    o = old.CatalogIndex(demo).match(sku_hint=None, description=q, raw_text=q)
    n = new_matching.CatalogIndex(demo).match(sku_hint=None, description=q, raw_text=q)
    assert o.status == "unmatched" and n.status in ("ambiguous", "likely")
    tail = "x " * 150 + " SKU-1003"
    assert new_matching.CatalogIndex(demo).match(sku_hint=None, description=tail, raw_text=tail).status == "exact"


def test_round10_budget_is_per_index_exact_skus_are_free_and_skipped_lines_are_flagged() -> None:
    cat = _catalog(5000, 2)
    idx = new_matching.CatalogIndex(cat)
    skipped = 0
    started = time.perf_counter()
    for i in range(404):
        o = idx.match(sku_hint=None, description=f"hex bolt zinc plated variant {i}", raw_text="x")
        skipped += int(o.fuzzy_skipped)
        if o.fuzzy_skipped:
            assert o.status == "unmatched" and o.candidates == ()
    assert skipped == 4, skipped  # 400 x 5,000 = 2,000,000 comparisons are allowed, the 401st line is skipped
    assert time.perf_counter() - started < 60
    exact = idx.match(sku_hint=cat[10].sku, description="whatever", raw_text="x")
    assert exact.status == "exact" and not exact.fuzzy_skipped
    token = idx.match(sku_hint=None, description="zzz", raw_text=f"need {cat[11].sku} please")
    assert token.status == "exact"
    assert (
        new_matching.CatalogIndex(cat).match(sku_hint=None, description="hex bolt", raw_text="x").fuzzy_skipped is False
    )


@integration
def test_round10_budget_through_the_api_flags_blocks_and_allows_manual_selection(app) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("R7 Budget", with_catalog=False)
    admin = login_as(app, t.users["admin"])
    cat = _catalog(5000, 3)
    body = "SKU,Description,Unit,Currency,List Price\n" + "".join(
        f"{p.sku}x{p.id},{p.description} v{p.id},EA,AED,{1 + p.id % 9}.50\n" for p in cat
    )
    r = upload(admin, "/catalogs/import", "c.csv", body.encode(), {"mapping": MAPPING, "catalog_name": "Main"})
    assert r.status_code == 201, r.text
    assert r.json()["import_report"]["imported"] == 5000
    assert admin.post(f"/catalogs/versions/{r.json()['id']}/activate").status_code == 200
    sales = login_as(app, t.users["sales"])
    sku_line = f"{cat[7].sku}x7"
    lines = [f"{i % 90 + 1} pcs hex bolt zinc plated variant {i}" for i in range(450)]
    lines.append(f"10 pcs {sku_line} some bolt")
    t0 = time.perf_counter()
    resp = sales.post("/rfqs", json={"text": "\n".join(lines), "customer_id": str(t.customer_id)})
    elapsed = time.perf_counter() - t0
    assert resp.status_code == 201, resp.text[:300]
    items = resp.json()["items"]
    assert len(items) == 451
    flagged = [i for i in items if "match_skipped_budget" in i["flags"]]
    assert [i["line_no"] for i in flagged] == list(range(401, 451)), [i["line_no"] for i in flagged][:5]
    for it in flagged:
        assert it["match_status"] == "unmatched" and it["candidates"] == [] and it["selected_product_id"] is None
    assert not any("match_skipped_budget" in i["flags"] for i in items[:400])
    last = items[450]
    assert (
        last["match_status"] == "exact"
        and last["selected_product_id"] is not None
        and "match_skipped_budget" not in last["flags"]
    )
    assert elapsed < 120, elapsed
    # like any unmatched line the skipped item blocks quote generation
    g = sales.post(f"/rfqs/{resp.json()['id']}/generate-quote")
    assert g.status_code in (409, 422), g.status_code
    # manual product selection of a skipped line works (API); it becomes resolved
    target = flagged[0]
    pid = admin.get(f"/catalogs/versions/{r.json()['id']}/products?q={cat[3].sku}x3").json()["items"][0]["id"]
    p = sales.patch(f"/rfqs/{resp.json()['id']}/items/{target['id']}", json={"selected_product_id": pid})
    assert p.status_code == 200, p.text
    assert p.json()["match_status"] == "resolved" and p.json()["selected_product_id"] == pid
    # custom marking works too
    c = sales.patch(f"/rfqs/{resp.json()['id']}/items/{flagged[1]['id']}", json={"is_custom": True})
    assert c.status_code == 200 and c.json()["is_custom"] is True


def test_j7_2_flag_match_skipped_budget_is_stale_after_manual_resolution_observation() -> None:
    """Observation O7-2 (documented in the evidence file): after a person resolves a skipped line, update_item does
    not remove the flag; the badge stays next to a resolved line. Cosmetic; asserted only as documentation."""
    src = (API_DIR / "app" / "services" / "rfqs.py").read_text()
    # orchestrator edit (declared, fix round 11): the observation is resolved — update_item now clears the flag when a
    # person selects a product, which adds a third mention of the flag in the module
    assert "match_skipped_budget" in src and src.count("match_skipped_budget") == 3


# ------------------------------------------------------------------------------------------- 5. seed and root .env
PW1, PW2, PW3 = "Qa7-Root-Passw0rd!", "Qa7-Cwd--Passw0rd!", "Qa7-Env--Passw0rd!"


def _seed(
    env_extra: dict[str, str | None], cwd: Path | None = None, pythonpath: bool = False
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "DEMO_PASSWORD"}
    for k, v in env_extra.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    env["DATABASE_URL"] = os.environ["DATABASE_URL"]
    env["DATABASE_ADMIN_URL"] = os.environ["DATABASE_ADMIN_URL"]
    if pythonpath:
        env["PYTHONPATH"] = str(API_DIR)
    return subprocess.run(
        [sys.executable, "-m", "app.fixtures.seed"],
        cwd=str(cwd or API_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _login_ok(app: Any, password: str) -> bool:
    c = TestClient(app, base_url="http://testserver")
    tok = c.get(f"{API}/auth/csrf").json()["csrf_token"]
    r = c.post(
        f"{API}/auth/login",
        json={"email": "sales@northwind.example", "password": password},
        headers={"X-CSRF-Token": tok},
    )
    return r.status_code == 200


class _RootEnv:
    def __init__(self, content: str) -> None:
        self.path = ROOT / ".env"
        self.content = content

    def __enter__(self) -> _RootEnv:
        assert not self.path.exists(), "refusing to overwrite an existing repo-root .env"
        self.path.write_text(self.content)
        return self

    def __exit__(self, *exc: object) -> None:
        self.path.unlink(missing_ok=True)


@integration
def test_round10_seed_reads_the_repo_root_env_and_prints_nothing_secret(app) -> None:  # type: ignore[no-untyped-def]
    with _RootEnv(f"DEMO_PASSWORD={PW1}\n"):
        r = _seed({}, cwd=API_DIR)  # what `make demo` does
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert PW1 not in r.stdout + r.stderr and "owner@northwind.example" in r.stdout and "not shown" in r.stdout
    assert _login_ok(app, PW1) and not _login_ok(app, PW2)


@integration
def test_round10_seed_precedence_environment_then_cwd_env_then_root_env(app, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / ".env").write_text(f"DEMO_PASSWORD={PW2}\n")
    with _RootEnv(f"DEMO_PASSWORD={PW1}\n"):
        r = _seed({"DEMO_PASSWORD": PW3}, cwd=tmp_path, pythonpath=True)
    assert r.returncode == 0 and PW3 not in r.stdout + r.stderr
    assert _login_ok(app, PW3) and not _login_ok(app, PW2) and not _login_ok(app, PW1)


@integration
def test_round10_seed_precedence_cwd_env_beats_root_env(app, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / ".env").write_text(f"DEMO_PASSWORD={PW2}\n")
    with _RootEnv(f"DEMO_PASSWORD={PW1}\n"):
        r = _seed({}, cwd=tmp_path, pythonpath=True)
    assert r.returncode == 0 and PW2 not in r.stdout + r.stderr and PW1 not in r.stdout + r.stderr
    assert _login_ok(app, PW2) and not _login_ok(app, PW1)


@integration
@integration
def test_j7_3_seed_empty_environment_value_masks_the_env_files_judgement(app) -> None:  # type: ignore[no-untyped-def]
    """Judgement call J7-3: a *set but empty* DEMO_PASSWORD in the environment wins over every .env file (standard
    dotenv precedence; pydantic reads the empty value for the root file too), so the seed refuses (exit 2). Defensible,
    but `export DEMO_PASSWORD=` left over from a shell profile is a plausible trap; the message does not mention it."""
    with _RootEnv(f"DEMO_PASSWORD={PW1}\n"):
        r = _seed({"DEMO_PASSWORD": ""}, cwd=API_DIR)
        unset = _seed({}, cwd=API_DIR)
    # orchestrator edit (declared, fix round 11): J7-3 is resolved — an empty environment value no longer masks the
    # .env files, so the seed runs with the password from the root file and still prints no secret
    assert r.returncode == 0 and PW1 not in r.stdout + r.stderr
    assert unset.returncode == 0 and _login_ok(app, PW1)


@integration
def test_round10_seed_bad_root_env_values_refuse_and_never_echo_them() -> None:
    for content in (
        "DEMO_PASSWORD=\n",
        "DEMO_PASSWORD=short-11-ch\n",
        "OTHER=1\n",
        "# DEMO_PASSWORD=Qa7-Commented-Pw0rd!\n",
    ):
        with _RootEnv(content):
            bad = _seed({}, cwd=API_DIR)
        assert bad.returncode == 2 and bad.stdout == "", (content, bad.returncode, bad.stdout, bad.stderr[:120])
        assert "Qa7-Commented" not in bad.stderr and "short-11-ch" not in bad.stderr


def test_round10_readme_quick_start_no_longer_says_credentials_are_printed() -> None:
    readme = (ROOT / "README.md").read_text()
    assert not re.search(r"make demo[^\n]*prints (local|credentials|the password)", readme, re.IGNORECASE), (
        "README still says make demo prints credentials"
    )
    assert "never prints a password" in readme


def test_round10_no_env_file_is_left_behind() -> None:
    assert not (ROOT / ".env").exists()
