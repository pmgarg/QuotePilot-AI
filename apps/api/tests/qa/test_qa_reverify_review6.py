"""QA (independent) re-verification 6 of Stage 1 at 7151e64 (fix round 9).

Derived from CLAUDE.md / PRD ("never invent prices, stock or customer facts"; parse failures are reported, not
guessed), docs/API_CONTRACT.md and the re-verification-5 list. Scope: git diff 8f9416e..7151e64.
Unit tests (parse_decimal, text-line parser differential against a verbatim copy of the 6b3c452 logic), integration tests
(RFQ API, seed as a subprocess against the QA database). Not browser evidence.
"""

from __future__ import annotations

import os
import random
import re
import subprocess
import sys
import time
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.services.money import parse_decimal
from app.services.parsing import extract as new
from app.services.parsing.extract import (
    NOISE,
    QTY_LEAD,
    QTY_TRAIL,
    SKU_TOKEN,
    ParsedItem,
    looks_like_sku,
    sku_tokens,
)
from app.services.units import normalize_unit
from tests.conftest import API_DIR, AuthedClient, Tenant, login_as, upload

integration = pytest.mark.integration
ROOT = API_DIR.parent.parent

# ------------------------------------------------------------------------------------------- 1. comma rule

COMMA_RIGHT = [
    ("0,500", D("0.5")),
    ("0,001", D("0.001")),
    ("0,5", D("0.5")),
    ("0,05", D("0.05")),
    ("0,125 kg", D("0.125")),
    ("AED 0,500", D("0.5")),
    ("-0,500", D("-0.5")),
    ("(0,500)", D("-0.5")),
    ("0,999", D("0.999")),
    ("012,345", D("12.345")),
    ("1,000", D(1000)),
    ("1,000,000", D(1000000)),
    ("10,000", D(10000)),
    ("100,000", D(100000)),
    ("1,234.56", D("1234.56")),
    ("1.234,56", D("1234.56")),
    ("12,5", D("12.5")),
    ("0.500", D("0.5")),
    ("0.5", D("0.5")),
    ("0", D(0)),
    ("1 000,50", D("1000.50")),
    ("0,50 EUR", D("0.5")),
    ("1.000,00 EUR", D(1000)),
    ("10,5 pcs", D("10.5")),
    ("2,500", D(2500)),
    ("999,999", D(999999)),
]


@pytest.mark.parametrize(("raw", "expected"), COMMA_RIGHT, ids=[r for r, _ in COMMA_RIGHT])
def test_round9_comma_decimals_and_thousands(raw: str, expected: D) -> None:
    got = parse_decimal(raw)
    assert got is not None and got == expected, (raw, got)


@pytest.mark.parametrize("raw", ["0,500,000", "0,5,0", "0,,5", "0,", ",500", "0,0,0", "00,5,5"])
def test_round9_malformed_comma_cells_are_rejected(raw: str) -> None:
    assert parse_decimal(raw) is None, (raw, parse_decimal(raw))


@pytest.mark.parametrize("raw", ["0,500.00", "0.500,00", "00,500.25", "0,001.5", "0.500,5"])
def test_d6_1_leading_zero_group_with_both_separators_is_not_read_as_a_large_number(raw: str) -> None:
    """Defect D6-1 (Low, same class as D5-1, left open by round 9): the new rule 'a thousands group never starts with
    a zero' is applied only to cells with a comma and no dot. '0,500.00' and '0.500,00' still become 500.00 and
    '0,001.5' becomes 1.5 (a leading-zero group read as a thousands group). Expected: None (malformed) or a value < 1.
    money.py: the `"," in core and "." in core` branch runs before the leading-zero check."""
    got = parse_decimal(raw)
    assert got is None or abs(got) < 1, (raw, got)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("\u2010 5", D(-5)),
        ("\u2013 5", D(-5)),
        ("\u20135", D(-5)),
        ("\u2014 5", D(-5)),
        ("\u2012 5", D(-5)),
        ("\u2015 5", D(-5)),
        ("\u2212 5", D(-5)),
        ("\u2796 5", D(-5)),
        ("\ufe58 5", D(-5)),
        ("\u2e3a 5", D(-5)),
        ("AED \u2013 5", D(-5)),
        ("AED\u20135", D(-5)),
        ("-\u0663", D(-3)),
        ("\u2013\uff15", D(-5)),
    ],
)
def test_round9_every_dash_before_a_number_is_a_minus(raw: str, expected: D) -> None:
    assert parse_decimal(raw) == expected, (raw, parse_decimal(raw))


@pytest.mark.parametrize(
    "raw",
    [
        "\u00b2",
        "5\u00b2",
        "5\u2462",
        "12 \u4e07",
        "10\u2070",
        "12\u2085",
        "\u00bd",
        "1\u00bd",
        "\u2155",
        "\u2160",
        "\u2460",
        "5\u2030\u00b2",
        "20 m\u00b2",
        "5 m\u00b3",
        "5\u2013",
        "5 \u2013 pcs",
        "10\u201320",
        "10\u201412",
    ],
)
def test_round9_non_decimal_numeric_characters_and_trailing_dashes_make_the_cell_unreadable(raw: str) -> None:
    assert parse_decimal(raw) is None, (raw, parse_decimal(raw))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("\u0663\u0665", D(35)),
        ("\u0665 pcs", D(5)),
        ("\u06f5", D(5)),
        ("\u0966", D(0)),
        ("\uff15\uff10", D(50)),
        ("\u0663\u066b\u0665", D("3.5")),
        ("1\u066c234", D(1234)),
        ("12 \u00b0C", D(12)),
        ("\u00b050", D(50)),
        ("\u20ac 12,50", D("12.5")),
        ("12 \u2116", D(12)),
    ],
)
def test_round9_decimal_digits_of_other_scripts_and_harmless_symbols_still_parse(raw: str, expected: D) -> None:
    assert parse_decimal(raw) == expected, (raw, parse_decimal(raw))


def test_round9_leading_zero_padded_thousands_observation() -> None:
    """Observation O6-1 (documented, not asserted as a defect): '01,234' and '007,500' were read as 1234 and 7500
    before round 9 and are now 1.234 and 7.500 (a zero-padded export would be wrong either way; neither is flagged)."""
    assert parse_decimal("01,234") == D("1.234") and parse_decimal("007,500") == D("7.500")


def test_round9_fuzz_never_raises_and_never_invents_digits_with_new_tokens() -> None:
    import unicodedata

    toks = [
        "1",
        "2",
        "5",
        "0",
        "12",
        "345",
        ".",
        ",",
        " ",
        "\u00a0",
        "'",
        "-",
        "\u2013",
        "\u2212",
        "(",
        ")",
        "e",
        "x",
        "pcs",
        "AED",
        "$",
        "%",
        "\u00b2",
        "\u00bd",
        "\u0663",
        "\uff15",
        "k",
        "m",
    ]
    rnd = random.Random(20261113)
    bad: list[tuple[str, Any]] = []
    for _ in range(60000):
        s = "".join(rnd.choice(toks) for _ in range(rnd.randint(1, 8)))
        got = parse_decimal(s)
        if got is None:
            continue
        assert got.is_finite() and abs(got) < D(10) ** 31, (s, got)
        dashless = "".join(
            "-" if unicodedata.category(c) == "Pd" or c in "\u2212\u2796" else c
            for c in unicodedata.normalize("NFKC", s)
        )
        if re.search(r"\d\.?[eE][+-]?\d", dashless):
            continue
        digits = "".join(str(unicodedata.digit(c)) for c in unicodedata.normalize("NFKC", s) if c.isdecimal())
        mine = "".join(map(str, got.as_tuple().digits)).strip("0")
        if mine != digits.strip("0"):
            bad.append((s, got))
    assert not bad, f"digits invented or lost for {len(bad)} cells, e.g. {bad[:8]}"


# ---------------------------------------------------------------------------------- 2. RFQ text lines


def _old_items_from_text_lines(text: str, source: dict[str, Any]) -> list[ParsedItem]:
    """Verbatim copy of items_from_text_lines at 6b3c452 (before round 9), using the unchanged module regexes."""
    items: list[ParsedItem] = []
    for lidx, line in enumerate(text.splitlines()):
        s = line.strip()
        if len(s) < 3 or NOISE.match(s):
            continue
        item: ParsedItem | None = None
        m = QTY_LEAD.match(s)
        if m and len(m.group("desc")) >= 3:
            item = ParsedItem(
                raw_text=s[:2000],
                description=m.group("desc").strip()[:1000],
                quantity=parse_decimal(m.group("qty")),
                unit_raw=m.group("unit"),
            )
        else:
            m = QTY_TRAIL.match(s)
            if (
                m
                and len(m.group("desc")) >= 3
                and (
                    m.group("unit")
                    or re.search(
                        r"(qty\.?|quantity|\sx|\u00d7|[-\u2013:,])\s*[:=]?\s*\d+(?:[.,]\d+)?\s*$", s, re.IGNORECASE
                    )
                )
            ):
                item = ParsedItem(
                    raw_text=s[:2000],
                    description=m.group("desc").strip(" -\u2013:,")[:1000],
                    quantity=parse_decimal(m.group("qty")),
                    unit_raw=m.group("unit"),
                )
        if item is None:
            if SKU_TOKEN.search(s) and not re.search(r"@|www\.|http", s):
                item = ParsedItem(raw_text=s[:2000], description=s[:1000], flags=["quantity_missing"])
            else:
                continue
        if item.quantity is not None and item.quantity <= 0:
            item.flags.append("quantity_not_positive")
            item.quantity = None
        if s[:1] in ("=", "+", "@") or s[:2] in ("\t=", "\r="):
            item.flags.append("formula_like")
        tokens = [t for t in SKU_TOKEN.findall(item.description) if looks_like_sku(t)]
        if tokens:
            item.sku_hint = tokens[0][:100]
        if item.unit_raw and normalize_unit(item.unit_raw) is None:
            item.flags.append("unit_unknown")
        item.source_ref = {**source, "line": lidx + 1}
        items.append(item)
    return items


def _ws(s: str | None) -> str | None:
    return " ".join(s.split()) if s is not None else None


def _core(i: ParsedItem) -> tuple[Any, ...]:
    return (
        _ws(i.raw_text),
        str(i.quantity),
        i.unit_raw,
        i.sku_hint,
        tuple(i.flags),
        tuple(sorted(i.source_ref.items())),
    )


def _desc(i: ParsedItem) -> str | None:
    return _ws(i.description).lstrip(" -\u2013:,").rstrip(" -\u2013:,") if i.description else i.description  # type: ignore[union-attr]


def _gen(seed: int, n: int) -> list[str]:
    r = random.Random(seed)
    unit = ["pcs", "ea", "kg", "m", "box", "pails", "nos", "pc", "doz", "", ""]
    sku = ["SKU-1001", "SKU-1003", "AB123", "4711-X", "M8x40", "12V", "ABC-12-xyz", "x-1", "SKU_77/b"]
    words = [
        "Hex",
        "bolt",
        "zinc",
        "plated",
        "grease",
        "pail",
        "18kg",
        "nut",
        "M8",
        "cable",
        "3x1.5",
        "for",
        "www.a.b",
        "a@b.c",
        "qty:",
        "x",
        "-",
        "\u2013",
        ":",
        ",",
    ]
    ws = [" ", "  ", "\t", " \t ", "   ", "\u00a0", " "]
    out = []
    for _ in range(n):
        lines = []
        for _ in range(r.randint(1, 4)):
            parts = []
            if r.random() < 0.5:
                lead = r.choice(["", "- ", "\u2022 ", "* ", "1. ", "item 2: ", "#3 ", "Pos. 4 - "])
                parts.append(
                    lead
                    + r.choice(["5", "1,000", "12.5", "0,5", "1.234,5", "10x", "0", "-3", "1e3", "99999999999", "2k"])
                )
            if r.random() < 0.5:
                parts.append(r.choice(unit))
            parts += [r.choice(words + sku) for _ in range(r.randint(0, 4))]
            if r.random() < 0.5:
                parts.append(
                    r.choice(["qty:", "x", "-", ",", ":", "qty"]) + " " + r.choice(["5", "12.5", "1,000", "7", "0"])
                )
            if r.random() < 0.3:
                parts.append(r.choice(unit))
            lines.append(r.choice(ws).join(p for p in parts if p != ""))
        out.append("\n".join(lines))
    return out


_HAND = [
    """Dear Sir,
Please quote for the following:
1. 500 pcs SKU-1001 Hex bolt M8x40 zinc plated
2. 500 pcs SKU-1003 Hex nut M8 zinc plated
3. 2 ea SKU-3002 Lithium grease EP2 pail 18kg
Regards, Ahmed""",
    """- 10 x SKU-1001 Hex bolt M8x40
- 20 x SKU-1002 Washer M8 flat
* 5 box SKU-3002 grease
• 7 pcs SKU-1003 nut""",
    "SKU-1001\t500\tpcs\nSKU-1003\t250\tpcs\nSKU-3002\t2\tea\n",
    "Hex bolt M8x40 zinc plated    SKU-1001       500      pcs\nHex nut M8 zinc plated       SKU-1003        250   pcs\n",
    "Hex bolt M8x40 qty: 500\nHex nut M8, qty 250\nWasher M8 - 1,000 pcs\nGrease pail x 3\nGrease pail - 3\nLithium grease: 4 pails\n",
    "item 1: 500 pcs SKU-1001 hex bolt\nitem 2) 250 SKU-1003 nut\nPos. 3 - 10 m cable 3x1.5 SKU-9001\n#4: 12 SKU-1002 washer\n",
    "SKU-1001\nSKU-1003 qty missing here\nplain prose line without any sku\nThank you\nTotal: 5\n",
    "Subject: RFQ 2026-11\nFrom: buyer@example.com\nTo: sales@example.com\n\nHi team,\nneed 1,000 pcs SKU-1001 hex bolt and 2,500 pcs SKU-1003 nut by Friday.\nAlso: 12.5 kg SKU-3002 grease\nwww.example.com\nsee http://x.example/SKU-1001\n",
    "1,000 pcs SKU-1001 Hex bolt\n1.000,50 kg SKU-1002 Washer\n0,5 kg SKU-3002 grease\n0,500 kg SKU-3002 grease\n10000000 pcs SKU-1001 x\n10000001 pcs SKU-1001 x\n",
    "500 pcs SKU-1001 Hex bolt\r\n250 pcs SKU-1003 nut\r\n\r\n=1+1 pcs SKU-1001\n+5 pcs SKU-1001\n@cmd 5 pcs SKU-1001\n\t=5 pcs SKU-1001\n",
    "   500 pcs SKU-1001 Hex bolt  \n　250 pcs SKU-1003 nut\n",
    "500 pcs 5mm-bolt-123 thing\n12V relay AB123 x 4\nPart 4711-X, 6\nM8x60 bolt SKU-1001 6\nBolt M8x60 SKU-1001\n",
    "10 x SKU-1001/SKU-1002/SKU-1003/SKU-1004/SKU-1005/SKU-1006/SKU-1007 mixed set\n",
    "QTY: 5 SKU-1001 hex\nQuantity 7 SKU-1003 nut\n5 SKU-1001 hex\n5SKU-1001 hex\n5 pcs.SKU-1001\n",
    "10 pcs\n10\nx 5\nabc\nSKU-1001 x\n",
]


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_round9_text_lines_same_items_as_6b3c452_except_whitespace(seed: int) -> None:
    """Differential over a hand-written corpus (clean list, bullets, tab/space-aligned columns, qty: forms, e-mail body,
    CRLF, NBSP, formula-like lines, locale numbers, SKU-only and noise lines) plus 2,000 generated texts per seed.
    Quantity, unit, sku_hint, flags and source_ref must be identical; raw_text/description may differ only by
    whitespace collapsing (and leading/trailing separator characters of the description, which only ever
    differed on tab-separated junk lines)."""
    texts = _HAND + _gen(seed, 2000)
    for t in texts:
        a = _old_items_from_text_lines(t, {})
        b = new.items_from_text_lines(t, {})
        assert [_core(i) for i in a] == [_core(i) for i in b], t[:300]
        assert [_desc(i) for i in a] == [_desc(i) for i in b], t[:300]


def test_round9_collapsing_is_acceptable_for_raw_text_and_description() -> None:
    t = "Hex bolt M8x40 zinc plated    SKU-1001       500      pcs\n"
    (it,) = new.items_from_text_lines(t, {})
    assert it.raw_text == "Hex bolt M8x40 zinc plated SKU-1001 500 pcs"
    assert it.description == "Hex bolt M8x40 zinc plated SKU-1001" and it.quantity == D(500) and it.unit_raw == "pcs"
    assert it.sku_hint == "SKU-1001"
    (tab,) = new.items_from_text_lines("SKU-1001\t500\tpcs\n", {})
    assert tab.quantity == D(500) and tab.unit_raw == "pcs" and tab.sku_hint == "SKU-1001"


def test_round9_fixture_texts_same_items_as_6b3c452() -> None:
    for name in ("rfq_clean.txt", "rfq_ambiguous.txt", "rfq_injection.txt"):
        t = (ROOT / "tests" / "fixtures" / name).read_text(errors="ignore")
        a = _old_items_from_text_lines(t, {})
        b = new.items_from_text_lines(t, {})
        assert [_core(i) for i in a] == [_core(i) for i in b], name
        assert len(b) >= 1, name


def test_round9_sku_tokens_equal_the_regex_for_runs_up_to_64_characters() -> None:
    rnd = random.Random(5)
    alpha = list("ab1-_./ 9Z") + ["SKU-1001", "x", "--", ".5", " "]
    checked = 0
    for _ in range(100000):
        s = "".join(rnd.choice(alpha) for _ in range(rnd.randint(1, 60)))
        if any(len(m.group(0)) > 64 for m in re.finditer(r"[A-Za-z0-9\-_./]+", s)):
            continue
        checked += 1
        assert sku_tokens(s) == SKU_TOKEN.findall(s), s
    assert checked > 50000


def test_round9_sku_runs_longer_than_64_are_not_skus_observation() -> None:
    """Observation O6-2: a slash- or dash-joined run of more than 64 characters yields no SKU token (it did before)."""
    s = "/".join(f"SKU-{n}" for n in range(1001, 1013))
    assert len(s) > 64 and sku_tokens(s) == [] and SKU_TOKEN.findall(s) != []
    assert sku_tokens("SKU-1001/SKU-1002/SKU-1003/SKU-1004/SKU-1005/SKU-1006/SKU-1007") == SKU_TOKEN.findall(
        "SKU-1001/SKU-1002/SKU-1003/SKU-1004/SKU-1005/SKU-1006/SKU-1007"
    )


def test_round9_long_line_rule() -> None:
    sku_line = "SKU-1001 " + "word " * 250  # > 1000 characters, names a SKU
    assert len(" ".join(sku_line.split())) > 1000
    (kept,) = new.items_from_text_lines(sku_line, {})
    assert kept.quantity is None and "quantity_missing" in kept.flags and kept.sku_hint == "SKU-1001"
    assert new.items_from_text_lines("lorem " * 200 + "\n", {}) == []  # no SKU: dropped
    # a 1,001-character line starting with a clear quantity is NOT searched: quantity stays empty (flagged missing)
    qline = "500 pcs SKU-1001 " + "x" * 984
    assert len(qline) == 1001
    (long_item,) = new.items_from_text_lines(qline, {})
    assert long_item.quantity is None and "quantity_missing" in long_item.flags
    # exactly 1,000 is still searched
    (edge,) = new.items_from_text_lines(qline[:-1], {})
    assert edge.quantity == D(500)
    # whitespace padding does not count towards the cap (collapsed first)
    (padded,) = new.items_from_text_lines("500 pcs SKU-1001 bolt" + " " * 5000 + "end", {})
    assert padded.quantity == D(500)
    # one spreadsheet row pasted as a single tab-separated line (realistic length) is read
    row = "\t".join(["SKU-1001", "Hex bolt M8x40 zinc plated", "500", "EA"])
    assert len(row) < 200
    (r,) = new.items_from_text_lines(row, {})
    (r_old,) = _old_items_from_text_lines(row, {})
    assert _core(r) == _core(r_old)
    assert r.quantity == D(500) and r.sku_hint == "SKU-1001"


def test_round9_hostile_lines_are_linear() -> None:
    lines = [
        "abc" + " " * 200000 + "x",
        "1" + " " * 200000,
        "a-" * 100000,
        "SKU-1001" + "-" * 100000,
        "5" + " 5" * 50000,
        ("\t" * 100000) + "7 pcs bolt",
        "x" * 200000,
        "1 " * 100000 + "pcs",
        "a" * 5000 + " " + "b-" * 5000,
    ]
    t0 = time.perf_counter()
    for ln in lines:
        new.items_from_text_lines(ln, {})
    assert time.perf_counter() - t0 < 1.5


@integration
def test_round9_pasted_rfq_through_the_api_items_matches_and_flags(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    txt = (
        "Dear Sir,\nPlease quote:\n"
        "1.   500   pcs    SKU-1001      Hex bolt M8x40 zinc plated\n"
        "2.\t500\tpcs\tSKU-1003\tHex nut M8 zinc plated\n"
        "Lithium grease EP2 pail 18kg      qty:     2\n"
        "SKU-1002                    1,000 pcs\n"
        "- 7 x SKU-3002 grease\n"
        "Washer M8 flat zinc - 12.5 kg\n"
        "SKU-1001/SKU-1003 are alternatives\n"
        "see www.example.com/SKU-1001\n"
        "Regards\n"
    )
    r = sales.post("/rfqs", json={"text": txt, "customer_id": str(tenant_a.customer_id)})
    assert r.status_code == 201, r.text
    items = r.json()["items"]
    by_sku = {i["sku_hint"]: i for i in items if i["sku_hint"]}
    assert D(by_sku["SKU-1001"]["quantity"]) == 500 and by_sku["SKU-1001"]["match_status"] in (
        "exact",
        "likely",
        "matched",
    )
    assert D(items[0]["quantity"]) == 500 and D(items[1]["quantity"]) == 500
    assert any(i["quantity"] is not None and D(i["quantity"]) == D("12.5") for i in items)
    assert any(i["quantity"] is not None and D(i["quantity"]) == D(1000) for i in items)
    exact = [i for i in items if i["match_status"] == "exact"]
    assert len(exact) >= 3, [(i["sku_hint"], i["match_status"]) for i in items]


@integration
def test_round9_pasted_rfq_exact_match_is_unchanged_for_each_fixture_line(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """Matching uses sku_tokens on raw text: an exact SKU in the text must still produce an exact candidate."""
    sales = login_as(app, tenant_a.users["sales"])
    t = (ROOT / "tests" / "fixtures" / "rfq_clean.txt").read_text()
    r = sales.post("/rfqs", json={"text": t, "customer_id": str(tenant_a.customer_id)})
    assert r.status_code == 201, r.text
    for it in r.json()["items"]:
        if it["sku_hint"]:
            assert it["match_status"] == "exact", (it["sku_hint"], it["match_status"])
            assert it["quantity"] is not None


@integration
def test_round9_pasted_text_with_hostile_whitespace_is_answered_quickly_and_ordinary_lines_survive(
    app, tenant_a: Tenant
) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    txt = "\n".join(
        ["abc" + " " * 8000 + "x"] * 3 + ["10 pcs SKU-1001 Hex bolt M8x40 zinc plated", "SKU-1003" + "-" * 9000]
    )
    t0 = time.perf_counter()
    r = sales.post("/rfqs", json={"text": txt, "customer_id": str(tenant_a.customer_id)})
    assert time.perf_counter() - t0 < 3.0
    assert r.status_code == 201, r.text
    assert any(i["quantity"] is not None and D(i["quantity"]) == 10 for i in r.json()["items"])


@integration
def test_round9_pdf_text_lines_with_wide_column_spacing(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    import io

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 780
    for line in [
        "Request for quotation",
        "500 pcs      SKU-1001      Hex bolt M8x40 zinc plated",
        "Hex nut M8 zinc plated                      SKU-1003                          250 pcs",
        "Lithium grease EP2 pail 18kg - qty: 2",
    ]:
        c.drawString(40, y, line)
        y -= 20
    c.save()
    sales = login_as(app, tenant_a.users["sales"])
    r = upload(sales, "/rfqs", "r.pdf", buf.getvalue(), {"customer_id": str(tenant_a.customer_id)}, "application/pdf")
    assert r.status_code == 201, r.text
    q = sorted(D(i["quantity"]) for i in r.json()["items"] if i["quantity"] is not None)
    assert q == [D(2), D(250), D(500)], q


# ----------------------------------------------------------------------------------------- 3. demo seed
PW = "Qa6-Seed-Passw0rd!"


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


@integration
def test_seed_without_password_refuses_prints_no_password_and_writes_nothing() -> None:
    r = _seed({})
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert r.stdout == ""
    assert "DEMO_PASSWORD" in r.stderr and "does not generate or print" in r.stderr
    for bad in ("Demo-", "password for all demo accounts"):
        assert bad not in r.stdout + r.stderr
    short = _seed({"DEMO_PASSWORD": "short-11-ch"})
    assert short.returncode == 2 and short.stdout == "" and "short-11-ch" not in short.stderr
    empty = _seed({"DEMO_PASSWORD": ""})
    assert empty.returncode == 2 and empty.stdout == ""


@integration
def test_seed_with_password_lists_accounts_never_the_password_is_idempotent_and_login_works(app) -> None:  # type: ignore[no-untyped-def]
    r1 = _seed({"DEMO_PASSWORD": PW})
    assert r1.returncode == 0, (r1.stdout, r1.stderr)
    for text_ in (r1.stdout, r1.stderr):
        assert PW not in text_ and "Qa6-Seed" not in text_
    assert "owner@northwind.example" in r1.stdout and "readonly@contoso.example" in r1.stdout
    assert "not shown" in r1.stdout
    r2 = _seed({"DEMO_PASSWORD": PW})
    assert r2.returncode == 0 and PW not in r2.stdout + r2.stderr
    # exactly one set of accounts after two runs
    from sqlalchemy import create_engine, text

    eng = create_engine(os.environ["DATABASE_ADMIN_URL"])
    with eng.connect() as c:
        c.execute(text("SELECT set_config('app.login_email', 'owner@northwind.example', false)"))
        n = c.execute(text("SELECT count(*) FROM users WHERE email = 'owner@northwind.example'")).scalar()
    eng.dispose()
    assert n == 1
    ac = AuthedClient(TestClient(app, base_url="http://testserver"), "sales@northwind.example", PW)
    assert ac.role == "sales"
    assert ac.get("/catalogs/active-version").json()["version_id"] is not None
    # a password the seed did not use must not work
    from tests.conftest import API

    c2 = TestClient(app, base_url="http://testserver")
    tok = c2.get(f"{API}/auth/csrf").json()["csrf_token"]
    bad = c2.post(
        f"{API}/auth/login",
        json={"email": "sales@northwind.example", "password": "Demo-" + "x" * 16},
        headers={"X-CSRF-Token": tok},
    )
    assert bad.status_code == 401


@integration
def test_seed_reads_demo_password_from_dot_env_in_the_working_directory(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(f"DEMO_PASSWORD={PW}\n")
    r = _seed({}, cwd=tmp_path, pythonpath=True)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert PW not in r.stdout + r.stderr


@integration
def test_d6_2_documented_quick_start_repo_root_dot_env_is_read_by_make_demo() -> None:
    """Defect D6-2 (Low): README 'Quick start' says `cp .env.example .env` (repo root) and then `make demo`, and that
    DEMO_PASSWORD comes from 'environment or .env'. `make demo` runs the seed with cwd apps/api, and Settings reads
    `.env` relative to the working directory, so a repo-root .env with DEMO_PASSWORD is NOT read: the seed exits 2.
    Expected: exit 0 (or a doc that tells the user to export the variable / put the file in apps/api).
    config.py:13 `env_file=".env"`; seed.py::_password. Temporary file removed in `finally`."""
    root_env = ROOT / ".env"
    assert not root_env.exists(), "refusing to overwrite an existing repo-root .env"
    root_env.write_text(f"DEMO_PASSWORD={PW}\n")
    try:
        r = _seed({}, cwd=API_DIR)  # what `make demo` does: cd apps/api && python -m app.fixtures.seed
    finally:
        root_env.unlink()
    assert r.returncode == 0, (r.returncode, r.stderr[:200])


def test_d6_3_no_document_still_says_a_password_is_printed() -> None:
    """Defect D6-3 (Info, documentation): README.md:25 still reads 'make demo  # migrate + seed two synthetic
    organisations; prints LOCAL-ONLY credentials' although the seed no longer prints a password."""
    stale = re.compile(
        r"print\w*[^\n]{0,60}(credential|password)|(credential|password)[^\n]{0,40}printed", re.IGNORECASE
    )
    allowed = re.compile(
        r"never prints|not print|never writes|does not generate or print|no longer prints|never generates",
        re.IGNORECASE,
    )
    offenders = []
    for rel in (
        "README.md",
        "CLAUDE.md",
        "Makefile",
        ".env.example",
        "docs/OPERATIONS_RUNBOOK.md",
        "docs/KNOWN_LIMITATIONS.md",
        "docs/API_CONTRACT.md",
        "docs/HLD.md",
        "docs/LLD.md",
        "docs/SECURITY_POLICY.md",
        "docs/TEST_STRATEGY.md",
    ):
        p = ROOT / rel
        if not p.exists():
            continue
        lines = p.read_text().splitlines()
        for n, line in enumerate(lines, 1):
            prev = lines[n - 2] if n > 1 else ""
            if stale.search(line) and not allowed.search(line) and not allowed.search(prev + " " + line):
                offenders.append(f"{rel}:{n}: {line.strip()[:120]}")
    assert not offenders, offenders
