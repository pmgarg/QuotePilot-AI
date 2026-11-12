"""Currency metadata and Decimal helpers. No floats, ever."""

from __future__ import annotations

import math
import re
import unicodedata
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


_SCI = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)[eE][+-]?\d+$")
# U+2212 MINUS SIGN and its small/full-width forms are real minus signs (a dropped sign turns a credit into a charge);
# U+066B/U+066C are the Arabic decimal and thousands separators.
_TRANSLATE = str.maketrans({"\u2212": "-", "\ufe63": "-", "\uff0d": "-", "\u066b": ".", "\u066c": ","})
_GROUP_SEPARATORS = " \u00a0\u202f'"
_SPACE_GROUPED = re.compile(r"^\d{1,3}(?:[ \u00a0\u202f']\d{3})+(?:[.,]\d+)?$")
_CORE = re.compile(r"^[\d.,]+$")
_HEAD = re.compile(r"^(?P<sign1>[+-]?)\s*(?P<text>[^\d+\-(),']*?)\s*(?P<sign2>[+-]?)$")
_TAIL = re.compile(r"^[^\d+\-(),']*$")
_LONE_E = re.compile(r"(?<![^\W\d_])[eE](?![^\W\d_])")

# Largest magnitude any number read from a file, a cell or free text may have (10^30). Callers apply their own,
# much tighter, business limits (MAX_QUANTITY, MAX_STORED_PRICE); this bound only keeps absurd values out of arithmetic.
_MAX_ADJUSTED = 30
MAX_QUANTITY = Decimal("10000000")  # the same cap the API schemas enforce for RFQ items and quote lines
MAX_STORED_PRICE = Decimal("1000000000000")  # exclusive: NUMERIC(18,6) holds values below 10^12


def _bounded(d: Decimal) -> Decimal | None:
    if not d.is_finite():
        return None
    if d.is_zero():
        exponent = d.as_tuple().exponent
        if not isinstance(exponent, int) or abs(exponent) > _MAX_ADJUSTED:
            return None  # "0e1000": a zero with an absurd exponent still poisons later arithmetic
        return d.copy_abs()  # never return negative zero
    return d if -_MAX_ADJUSTED <= d.adjusted() <= _MAX_ADJUSTED else None


def parse_decimal(raw: object) -> Decimal | None:
    """Locale-tolerant but *strict* Decimal parser. Never uses float arithmetic.

    Accepted: one number, optionally wrapped in a currency code/symbol or a unit ("AED 12.50", "12.50 EUR",
    "$1,299.99", "12 pcs"), with "," / "." as decimal or thousands separators ("1,234.56", "1.234,56", "12,5"),
    space/apostrophe thousands groups ("1 234,56"), a leading sign, accounting parentheses ("(12.50)" = -12.50), or
    pure scientific notation ("8.5E2").

    Rejected (None): anything that contains more than one number or letters *inside* the number ("12 per 6",
    "1 e3", "1ee3", "0x10"), exponent notation mixed with other text, a stray "e", non-finite values and magnitudes
    beyond 10^30. A value is never assembled from fragments: when in doubt the caller gets None and reports the cell.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, Decimal):
        return _bounded(raw)
    if isinstance(raw, int):
        return _bounded(Decimal(raw))
    if isinstance(raw, float):
        # spreadsheets deliver floats for numeric cells; repr() gives the shortest round-trip string
        return _bounded(Decimal(repr(raw))) if math.isfinite(raw) else None
    s = unicodedata.normalize("NFKC", str(raw)).translate(_TRANSLATE).strip()
    if not s:
        return None
    if _SCI.match(s):
        # scientific notation ("1e3", "8.5E2") is parsed exactly; it must never lose its exponent (SEC3-11)
        try:
            return _bounded(Decimal(s))
        except InvalidOperation:
            return None
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()
    digits = [i for i, ch in enumerate(s) if ch.isdecimal()]
    if not digits:
        return None
    first, last = digits[0], digits[-1]
    head, core, tail = s[:first], s[first : last + 1], s[last + 1 :]
    # a decimal point directly before the first digit belongs to the number (".5", "$.50") unless it ends a word
    # ("Rs.500" is ambiguous: 500 or 0.500?) — then the cell is rejected instead of guessed
    if head.endswith("."):
        if head[-2:-1].isalpha():
            return None
        head, core = head[:-1], "." + core
    if tail.startswith(".") and (len(tail) == 1 or tail[1].isspace()):
        tail = tail[1:]  # "12." / "12. pcs"
    hm = _HEAD.match(head)
    if hm is None or (hm.group("sign1") and hm.group("sign2")) or not _TAIL.match(tail):
        return None
    if tail.lstrip().startswith(".") or _LONE_E.match(tail) or _LONE_E.search(head[-1:]) and _LONE_E.search(head):
        return None  # "12e", "e3": a stray exponent marker glued to the digits, not a unit ("3 E" is a unit)
    if any(ch in _GROUP_SEPARATORS for ch in core):
        if not _SPACE_GROUPED.match(core):
            return None  # "12 6", "1 e3" (letters), "10 5": two numbers, never glued together
        for ch in _GROUP_SEPARATORS:
            core = core.replace(ch, "")
    if not _CORE.match(core):
        return None  # letters or other characters inside the number
    if "," in core and "." in core:
        if core.rfind(",") > core.rfind("."):
            core = core.replace(".", "").replace(",", ".")  # 1.234,56
        else:
            core = core.replace(",", "")  # 1,234.56
    elif "," in core:
        parts = core.split(",")
        if len(parts) == 2 and len(parts[1]) in (1, 2, 3) and len(parts[0]) <= 3 and len(parts[1]) != 3:
            core = core.replace(",", ".")  # 12,5 or 12,50
        elif len(parts) == 2 and len(parts[1]) == 3 and len(parts[0]) <= 3:
            core = core.replace(",", "")  # 1,234 → thousands (ambiguous; documented)
        elif all(len(p) == 3 for p in parts[1:]):
            core = core.replace(",", "")
        else:
            core = core.replace(",", ".")
    try:
        d = Decimal(core)
    except InvalidOperation:
        return None
    if negative or "-" in (hm.group("sign1"), hm.group("sign2")):
        d = -d
    return _bounded(d)


def money_str(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")
