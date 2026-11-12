from decimal import Decimal as D

import pytest

from app.services.money import minor_units, parse_decimal, quantize
from app.services.pricing import LineInput, compute_line, compute_totals, validate_line


def test_line_rounding_half_up_two_dp() -> None:
    r = compute_line(LineInput(D("3"), D("10.005")), "USD")
    assert r.line_subtotal == D("30.02")  # 30.015 → half up
    assert r.line_total == D("30.02")


def test_line_rounding_half_even() -> None:
    r = compute_line(LineInput(D("1"), D("2.125")), "USD", "HALF_EVEN")
    assert r.line_subtotal == D("2.12")
    r2 = compute_line(LineInput(D("1"), D("2.125")), "USD", "HALF_UP")
    assert r2.line_subtotal == D("2.13")


def test_discount_and_tax_attribution() -> None:
    r = compute_line(LineInput(D("10"), D("100"), D("10"), D("5")), "USD")
    assert (r.line_subtotal, r.discount_amount, r.net_amount, r.tax_amount, r.line_total) == (
        D("1000.00"),
        D("100.00"),
        D("900.00"),
        D("45.00"),
        D("945.00"),
    )


@pytest.mark.parametrize(
    ("currency", "places", "expected"), [("JPY", 0, D("1235")), ("KWD", 3, D("1234.568")), ("EUR", 2, D("1234.57"))]
)
def test_currency_minor_units(currency: str, places: int, expected: D) -> None:
    assert minor_units(currency) == places
    assert quantize(D("1234.5678"), currency) == expected


def test_totals_are_sums_of_rounded_lines() -> None:
    lines = [LineInput(D("1"), D("0.005")), LineInput(D("1"), D("0.005")), LineInput(D("1"), D("0.005"))]
    t = compute_totals(lines, "USD")
    assert t.subtotal == D("0.03")  # each line rounds to 0.01 (half up), never 0.015*3 rounded once
    assert t.grand_total == D("0.03")


def test_margin_uses_effective_discounted_price() -> None:
    r = compute_line(LineInput(D("1"), D("100"), D("20"), D("0"), cost_price=D("70")), "USD")
    assert r.margin_pct == D("12.50")  # (80-70)/80


def test_margin_none_without_cost_or_price() -> None:
    assert compute_line(LineInput(D("1"), D("5")), "USD").margin_pct is None
    assert compute_line(LineInput(D("1"), None, cost_price=D("1")), "USD").margin_pct is None


@pytest.mark.parametrize("qty", [D("0"), D("-1")])
def test_validate_rejects_non_positive_quantity(qty: D) -> None:
    assert "quantity must be greater than zero" in validate_line(LineInput(qty, D("1")))


def test_validate_rejects_bad_discount_tax_price() -> None:
    errs = validate_line(LineInput(D("1"), D("-1"), D("101"), D("-5")))
    assert len(errs) == 3


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,234.56", D("1234.56")),
        ("1.234,56", D("1234.56")),
        ("12,5", D("12.5")),
        ("1234", D("1234")),
        ("(12.50)", D("-12.50")),
        ("AED 1,234", D("1234")),
        (12.5, D("12.5")),
        (7, D("7")),
        ("abc", None),
        ("", None),
        (None, None),
        (True, None),
    ],
)
def test_parse_decimal_locales(raw: object, expected: D | None) -> None:
    assert parse_decimal(raw) == expected


def test_no_float_in_decimal_pipeline() -> None:
    r = compute_line(LineInput(D("0.1"), D("0.2")), "USD")
    assert isinstance(r.line_total, D)
    assert r.line_total == D("0.02")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1e3", D("1000")),
        ("1E+3", D("1000")),
        ("8.5E2", D("850")),
        ("2.5e2", D("250")),
        ("1e-2", D("0.01")),
        ("-1.5e1", D("-15")),
    ],
)
def test_parse_decimal_scientific_notation_is_exact(raw: str, expected: D) -> None:
    """SEC3-11: an exponent must never be dropped ('1e3' used to parse as 13)."""
    assert parse_decimal(raw) == expected


@pytest.mark.parametrize("raw", ["1e3 AED", "USD 2.5e2", "1e9999999999", "1e-999", "NaN", "Infinity", "1e3e4"])
def test_parse_decimal_rejects_ambiguous_or_non_finite_exponent_forms(raw: str) -> None:
    assert parse_decimal(raw) is None


def test_parse_decimal_currency_codes_containing_e_still_parse() -> None:
    assert parse_decimal("12.50 AED") == D("12.50")
    assert parse_decimal("EUR 1.234,56") == D("1234.56")


# --- fix round 8 (SEC4-0..5): the parser accepts one number or nothing; it never assembles a value from fragments ---
@pytest.mark.parametrize(
    "raw",
    ["1 e3", "1e 3", "1 E 3", "1ee3", "1e  3", "1e+ 3", "12e", "e3", "1e+", "12 per 6", "10 x 5", "3 boxes of 12",
     "0x10", "12 6", "Rs.500", "0e1000", "5-", "12-5", "(5", "5)", "1e3 AED", "AED 1e3"],
)  # fmt: skip
def test_ambiguous_or_fragmented_numbers_are_rejected_not_assembled(raw: str) -> None:
    assert parse_decimal(raw) is None, (raw, parse_decimal(raw))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("−5", D(-5)), ("AED -5", D(-5)), ("- 5", D(-5)), ("(12.50)", D("-12.50")),
     ("Rs. 500", D(500)), ("$.50", D("0.50")), ("1 234,56", D("1234.56")),
     ("1'234.50", D("1234.50")), ("12 pcs.", D(12)), ("5EUR", D(5)), ("3 E", D(3)),
     ("5%", D(5)), ("١٢٣", D(123))],
)  # fmt: skip
def test_single_decorated_numbers_keep_their_sign_and_value(raw: str, expected: D) -> None:
    assert parse_decimal(raw) == expected


def test_parser_never_returns_negative_zero_or_non_finite_or_absurd_magnitudes() -> None:
    for raw in ("-0", "-0e5", "-0.00", "(0)", D("-0")):
        d = parse_decimal(raw)
        assert d is not None and d == 0 and not d.is_signed(), raw
    for raw in (float("nan"), float("inf"), float("-inf"), 1e300, D("NaN"), D("Infinity"), D("1e40")):
        assert parse_decimal(raw) is None, raw
    assert parse_decimal(12.5) == D("12.5") and parse_decimal(7) == D(7)
