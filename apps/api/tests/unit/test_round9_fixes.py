"""Stage 1 fix round 9 — implementer regression tests (SEC5-2/3/5/6/7, QA D5-1/D5-2, CodeQL seed alert)."""

from __future__ import annotations

import time
from decimal import Decimal as D

import pytest

from app.fixtures import seed as seed_module
from app.services.money import parse_decimal
from app.services.parsing.extract import MAX_SKU_RUN, MAX_TEXT_LINE, SKU_TOKEN, items_from_text_lines, sku_tokens


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("0,500", D("0.500")), ("0,001", D("0.001")), ("0,125", D("0.125")), ("00,500", D("0.500")), ("0,5", D("0.5")),
     ("00,5", D("0.5")), ("012,345", D("12.345")), ("1,234", D(1234)), ("12,5", D("12.5")), ("1.234,56", D("1234.56")), ("0", D(0))],
)  # fmt: skip
def test_sec5_6_a_leading_zero_never_starts_a_thousands_group(raw: str, expected: D) -> None:
    assert parse_decimal(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "0,500,000",
        "..5",
        "...5",
        "..50",
        ",.5",
        "5²",
        "10³",
        "5①",
        "x²",
        "½",
        "5 ⅔",
        "12 m²",
    ],
)
def test_sec5_2_and_5_7_typos_and_non_decimal_digits_are_rejected(raw: str) -> None:
    assert parse_decimal(raw) is None, (raw, parse_decimal(raw))


@pytest.mark.parametrize("dash", ["‐", "‑", "‒", "–", "—", "―", "⸺", "﹘", "﹣", "－", "−", "➖"])  # fmt: skip
def test_sec5_3_every_dash_before_a_number_is_a_minus_sign(dash: str) -> None:
    assert parse_decimal(f"{dash}5") == D(-5)
    assert parse_decimal(f"12{dash}14") is None  # a range is two numbers


def test_sec5_5_text_lines_are_parsed_in_linear_time() -> None:
    hostile = [
        "abc" + " " * 200_000 + "x",
        "1" + " " * 200_000 + "x",
        "abc " + " " * 200_000 + "5 xx",
        "a-" * 100_000,
        "1." * 100_000,
        "5 pcs " * 50_000,
        "\n".join("abc" + " " * 20_000 + "x" for _ in range(20)),
    ]
    for text in hostile:
        started = time.perf_counter()
        items_from_text_lines(text, {})
        assert time.perf_counter() - started < 0.5, text[:10]


def test_sec5_5_ordinary_lines_are_read_as_before_and_long_lines_carry_no_quantity() -> None:
    text = (
        "10 pcs  Hex bolt   M8x40 SKU-1001\nSKU-1002   widget   qty: 5\n  2)   3 x   Gasket AB-77\nJust SKU-9000 here\n"
    )
    got = [(i.quantity, i.unit_raw, i.description, i.sku_hint, i.flags) for i in items_from_text_lines(text, {})]
    assert got == [
        (D(10), "pcs", "Hex bolt M8x40 SKU-1001", "SKU-1001", []),
        (D(5), None, "SKU-1002 widget", "SKU-1002", []),
        (D(3), None, "Gasket AB-77", "AB-77", []),
        (None, None, "Just SKU-9000 here", "SKU-9000", ["quantity_missing"]),
    ]
    long_line = "SKU-1001 " + "word " * (MAX_TEXT_LINE // 5 + 5) + "qty 7"
    (item,) = items_from_text_lines(long_line, {})
    assert item.quantity is None and item.flags == ["quantity_missing"] and item.sku_hint == "SKU-1001"


def test_sku_tokens_equals_the_whole_text_search_on_ordinary_text_and_skips_overlong_runs() -> None:
    for text in (
        "order ABC-123, SKU-1001 and x.y/Z9-88 plus AB12 (M8x40) 200mm é7A-1x",
        "http://e.example/p/ABC-999/q ref:PN_77.A/3 12V item-0001",
        "",
        "no tokens here",
    ):
        assert sku_tokens(text) == SKU_TOKEN.findall(text), text
    assert sku_tokens("A1-" * MAX_SKU_RUN) == []


def test_seed_never_prints_or_generates_a_password(monkeypatch, capsys, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """CodeQL py/clear-text-logging-sensitive-data on fixtures/seed.py: the demo password was printed."""
    monkeypatch.delenv("DEMO_PASSWORD", raising=False)
    monkeypatch.chdir(tmp_path)  # round 11: the seed reads only DEMO_PASSWORD from `.env` in the working directory
    monkeypatch.setattr(seed_module, "_REPO_ROOT_ENV", seed_module.Path("/nonexistent/.env"))
    assert seed_module.main() == 2  # refuses to seed without a chosen password; touches no database
    out = capsys.readouterr()
    assert "DEMO_PASSWORD" in out.err and out.out == ""
    monkeypatch.setenv("DEMO_PASSWORD", "short")
    assert seed_module.main() == 2
    assert "short" not in capsys.readouterr().err


# --- fix round 10 (SEC6-1/3/4/5, QA D6-1) ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw",
    ["0,500.00", "0.500,00", "00,500.25", "0,001.5", "0.500,5", ".5,0", ",5.0", "12,34.56", "1.2,3.4", "5㋀", "㎡5"],
)  # fmt: skip
def test_round10_malformed_two_separator_numbers_and_digit_expanding_symbols_are_rejected(raw: str) -> None:
    assert parse_decimal(raw) is None, (raw, parse_decimal(raw))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1,234.56", D("1234.56")), ("1.234,56", D("1234.56")), ("1.234.567,89", D("1234567.89")),
     ("1,234,567.89", D("1234567.89")), ("10,500.5", D("10500.5")), ("1,00,000.50", D("100000.50")),
     ("12,34,567.89", D("1234567.89")), ("USD 1,299.99", D("1299.99")), ("(1,234.56)", D("-1234.56"))],
)  # fmt: skip
def test_round10_well_formed_grouped_numbers_keep_their_value(raw: str, expected: D) -> None:
    assert parse_decimal(raw) == expected


def test_round10_normalize_sku_is_linear_and_unchanged_for_ordinary_cells() -> None:
    from app.services.units import normalize_sku

    assert [normalize_sku(x) for x in ("  --SKU-1001..  ", "_ab_", "a  b\tc", "###", None, "ÄB-1")] == [
        "SKU-1001", "AB", "A B C", "", "", "ÄB-1",
    ]  # fmt: skip
    started = time.perf_counter()
    normalize_sku("a" + "-" * 300_000 + "b" + "." * 300_000)
    assert time.perf_counter() - started < 0.5


def test_round10_fuzzy_matching_has_a_query_cap_and_a_comparison_budget() -> None:
    from types import SimpleNamespace

    from app.services import matching

    products = [
        SimpleNamespace(sku=f"SKU-{i}", sku_normalized=f"SKU-{i}", description=f"hex bolt size {i} steel")
        for i in range(2_000)
    ]
    index = matching.CatalogIndex(products)  # type: ignore[arg-type]
    allowed = matching.MAX_FUZZY_COMPARISONS // len(products)
    outcomes = [
        index.match(sku_hint=None, description="hex bolt size 77 steel" + " x" * 400, raw_text="")
        for _ in range(allowed + 3)
    ]
    assert not any(o.fuzzy_skipped for o in outcomes[:allowed]) and outcomes[0].candidates
    assert all(o.fuzzy_skipped and o.status == "unmatched" and not o.candidates for o in outcomes[allowed:])
    # an exact SKU is still found after the budget is spent: no comparison is needed for it
    exact = index.match(sku_hint="SKU-77", description="whatever", raw_text="")
    assert exact.status == "exact" and not exact.fuzzy_skipped
