"""Unit and SKU normalisation. Aliases only — no quantity conversion between units (documented limitation)."""

from __future__ import annotations

import re

UNIT_ALIASES: dict[str, str] = {
    # each / pieces
    "ea": "EA",
    "each": "EA",
    "pc": "EA",
    "pcs": "EA",
    "piece": "EA",
    "pieces": "EA",
    "unit": "EA",
    "units": "EA",
    "nos": "EA",
    "no": "EA",
    "no.": "EA",
    "nr": "EA",
    "qty": "EA",
    "stk": "EA",
    "stück": "EA",
    "stuck": "EA",
    # packaging
    "box": "BOX",
    "boxes": "BOX",
    "bx": "BOX",
    "ctn": "CTN",
    "carton": "CTN",
    "cartons": "CTN",
    "pk": "PK",
    "pack": "PK",
    "packs": "PK",
    "pkt": "PK",
    "pair": "PR",
    "pairs": "PR",
    "pr": "PR",
    "set": "SET",
    "sets": "SET",
    "roll": "ROLL",
    "rolls": "ROLL",
    "rl": "ROLL",
    "bag": "BAG",
    "bags": "BAG",
    "drum": "DRUM",
    "drums": "DRUM",
    "pallet": "PLT",
    "pallets": "PLT",
    "plt": "PLT",
    "dz": "DZ",
    "dozen": "DZ",
    # mass
    "kg": "KG",
    "kgs": "KG",
    "kilogram": "KG",
    "kilograms": "KG",
    "g": "G",
    "gram": "G",
    "grams": "G",
    "t": "T",
    "ton": "T",
    "tons": "T",
    "tonne": "T",
    "tonnes": "T",
    "mt": "T",
    "lb": "LB",
    "lbs": "LB",
    # length
    "m": "M",
    "mtr": "M",
    "mtrs": "M",
    "meter": "M",
    "meters": "M",
    "metre": "M",
    "metres": "M",
    "mm": "MM",
    "cm": "CM",
    "km": "KM",
    "ft": "FT",
    "feet": "FT",
    "foot": "FT",
    "in": "IN",
    "inch": "IN",
    "inches": "IN",
    # area / volume
    "m2": "M2",
    "sqm": "M2",
    "sq.m": "M2",
    "m3": "M3",
    "cbm": "M3",
    "l": "L",
    "ltr": "L",
    "liter": "L",
    "litre": "L",
    "liters": "L",
    "litres": "L",
    "ml": "ML",
    "gal": "GAL",
    "gallon": "GAL",
    "gallons": "GAL",
    # time / misc
    "hr": "HR",
    "hour": "HR",
    "hours": "HR",
    "day": "DAY",
    "days": "DAY",
    "lot": "LOT",
    "lots": "LOT",
}

KNOWN_UNITS = frozenset(UNIT_ALIASES.values())

_WS = re.compile(r"\s+")
_SKU_STRIP = re.compile(r"^[\s\W_]+|[\s\W_]+$")


def normalize_unit(raw: str | None) -> str | None:
    """Return canonical unit code, or None when unknown/empty. Unknown units are kept raw by callers."""
    if raw is None:
        return None
    key = raw.strip().lower().rstrip(".")
    if not key:
        return None
    if key in UNIT_ALIASES:
        return UNIT_ALIASES[key]
    upper = raw.strip().upper()
    if upper in KNOWN_UNITS:
        return upper
    return None


def normalize_sku(raw: str | None) -> str:
    if raw is None:
        return ""
    s = _WS.sub(" ", str(raw)).strip()
    s = _SKU_STRIP.sub("", s)
    return s.upper()


def normalize_text(raw: str | None) -> str:
    if raw is None:
        return ""
    return _WS.sub(" ", str(raw)).strip().lower()
