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
# U+066B/U+066C are the Arabic decimal and thousands separators.
_TRANSLATE = str.maketrans({"\u066b": ".", "\u066c": ","})
# U+2212 MINUS SIGN and U+2796 HEAVY MINUS SIGN are not in the dash category but are minus signs.
_MINUS_LIKE = frozenset("\u2212\u2796")
_GROUP_SEPARATORS = " \u00a0\u202f'"
_SPACE_GROUPED = re.compile(r"^\d{1,3}(?:[ \u00a0\u202f']\d{3})+(?:[.,]\d+)?$")
_CORE = re.compile(r"^[\d.,]+$")
_NOT_DECORATION = frozenset("+-(),'")
# No real price or quantity needs more characters than this; longer cells are text, not numbers. The bound also keeps
# every step below linear in a small constant (the first version of this parser used a backtracking pattern for the
# text before the number and was quadratic on long runs of whitespace — found by CodeQL on the round-8 push).
MAX_NUMBER_TEXT = 128

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
    s = str(raw)
    if len(s) > 4 * MAX_NUMBER_TEXT:
        return None  # checked before normalisation so that hostile megabyte cells cost nothing
    # "5²", "10³", "5①", "½": NFKC would turn these into ordinary digits glued to the number (52, 103, 51, 1/2).
    # A character that carries a numeric value without being a decimal digit makes the cell not-a-plain-number.
    if any(not ch.isdecimal() and unicodedata.numeric(ch, None) is not None for ch in s):
        return None
    s = unicodedata.normalize("NFKC", s).translate(_TRANSLATE)
    # every dash and hyphen (category Pd: U+2010..U+2015, U+2E3A, U+FE58, U+FE63, U+FF0D …) and the minus signs are
    # read as "-": a dropped sign turns a credit into a charge, a kept one at worst makes the cell a rejected negative
    s = "".join("-" if ch in _MINUS_LIKE or unicodedata.category(ch) == "Pd" else ch for ch in s).strip()
    if not s or len(s) > MAX_NUMBER_TEXT:
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
        if head[-2:-1].isalpha() or head[-2:-1] in (".", ","):
            return None  # "Rs.500" is ambiguous, "..5" is a typo
        head, core = head[:-1], "." + core
    if tail.startswith(".") and (len(tail) == 1 or tail[1].isspace()):
        tail = tail[1:]  # "12." / "12. pcs"
    # "12e", "e3": a stray exponent marker glued to the digits is not a unit ("3 E" and "5EUR" are)
    if tail[:1] in ("e", "E") and not tail[1:2].isalpha():
        return None
    if head[-1:] in ("e", "E") and not head[-2:-1].isalpha():
        return None
    # the text before the number may carry one sign, at its start or directly before the digits ("-AED 5", "AED -5")
    head = head.strip()
    sign = ""
    if head[:1] in ("+", "-"):
        sign, head = head[0], head[1:].lstrip()
    if head[-1:] in ("+", "-"):
        if sign:
            return None
        sign, head = head[-1], head[:-1].rstrip()
    if any(ch in _NOT_DECORATION for ch in head) or any(ch in _NOT_DECORATION for ch in tail):
        return None  # a second sign, parentheses, separators or an apostrophe outside the number
    if tail.lstrip().startswith("."):
        return None
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
        if parts[0].startswith("0"):
            # a thousands group never starts with a zero: "0,500" is half a unit, not five hundred
            if len(parts) != 2:
                return None
            core = core.replace(",", ".")
        elif len(parts) == 2 and len(parts[1]) in (1, 2, 3) and len(parts[0]) <= 3 and len(parts[1]) != 3:
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
    if negative and sign:
        return None  # "(-5)": parentheses and a sign together are ambiguous
    if negative or sign == "-":
        d = -d
    return _bounded(d)


def money_str(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")
