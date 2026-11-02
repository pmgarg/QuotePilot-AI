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
