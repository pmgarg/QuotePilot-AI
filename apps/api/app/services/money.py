"""Currency metadata and Decimal helpers. No floats, ever."""

from __future__ import annotations

import re
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation

# ISO 4217 minor units (subset covering the target markets; default 2)
MINOR_UNITS: dict[str, int] = {
    "JPY": 0,
    "KRW": 0,
    "VND": 0,
    "CLP": 0,
    "ISK": 0,
    "UGX": 0,
    "XOF": 0,
    "XAF": 0,
    "BHD": 3,
    "KWD": 3,
    "OMR": 3,
    "JOD": 3,
    "IQD": 3,
    "LYD": 3,
    "TND": 3,
}
SUPPORTED_CURRENCIES = frozenset(
    {
        "USD",
        "EUR",
        "GBP",
        "AED",
        "SAR",
        "QAR",
        "KWD",
        "BHD",
        "OMR",
        "JOD",
        "EGP",
        "INR",
        "PKR",
        "BDT",
        "LKR",
        "CNY",
        "HKD",
        "SGD",
        "MYR",
        "IDR",
        "THB",
        "VND",
        "PHP",
        "JPY",
        "KRW",
        "AUD",
        "NZD",
        "CAD",
        "MXN",
        "BRL",
        "ZAR",
        "NGN",
        "KES",
        "TRY",
        "CHF",
        "SEK",
        "NOK",
        "DKK",
        "PLN",
        "CZK",
        "HUF",
        "RON",
        "ILS",
        "MAD",
        "TND",
        "DZD",
        "IQD",
        "LYD",
        "ISK",
        "CLP",
        "COP",
        "PEN",
        "ARS",
        "UGX",
        "TZS",
        "XOF",
        "XAF",
    }
)
ROUNDING = {"HALF_UP": ROUND_HALF_UP, "HALF_EVEN": ROUND_HALF_EVEN}


def is_supported_currency(code: str | None) -> bool:
    return bool(code) and code.upper() in SUPPORTED_CURRENCIES  # type: ignore[union-attr]


def minor_units(currency: str) -> int:
    return MINOR_UNITS.get(currency.upper(), 2)


def quantize(value: Decimal, currency: str, rounding_mode: str = "HALF_UP") -> Decimal:
    exp = Decimal(1).scaleb(-minor_units(currency))
    return value.quantize(exp, rounding=ROUNDING.get(rounding_mode, ROUND_HALF_UP))


_NUM_CLEAN = re.compile(r"[^\d,.\-()]")


def parse_decimal(raw: object) -> Decimal | None:
    """Locale-tolerant Decimal parser: '1,234.56', '1.234,56', '1234,56', '(12.50)', '12.50 AED' → Decimal.
    Returns None when not parseable. Never uses float."""
    if raw is None:
        return None
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return Decimal(raw)
    if isinstance(raw, float):
        # spreadsheets deliver floats for numeric cells; repr() gives the shortest round-trip string
        return Decimal(repr(raw))
    s = str(raw).strip()
    if not s:
        return None
    negative = s.startswith("(") and s.endswith(")")
    s = _NUM_CLEAN.sub("", s).strip("()")
    if not s or s in ("-", ".", ","):
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")  # 1.234,56
        else:
            s = s.replace(",", "")  # 1,234.56
    elif "," in s:
        parts = s.split(",")
        if len(parts) == 2 and len(parts[1]) in (1, 2, 3) and len(parts[0]) <= 3 and len(parts[1]) != 3:
            s = s.replace(",", ".")  # 12,5 or 12,50
        elif len(parts) == 2 and len(parts[1]) == 3 and len(parts[0]) <= 3:
            s = s.replace(",", "")  # 1,234 → thousands (ambiguous; documented)
        elif all(len(p) == 3 for p in parts[1:]):
            s = s.replace(",", "")
        else:
            s = s.replace(",", ".")
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    if negative:
        d = -d
    return d


def money_str(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")
