from decimal import Decimal as D

from app.services.matching import CatalogIndex
from app.services.units import normalize_sku, normalize_unit


class P:  # lightweight stand-in for the Product model
    def __init__(self, sku: str, description: str, unit: str = "EA") -> None:
        self.sku = sku
        self.sku_normalized = normalize_sku(sku)
        self.description = description
        self.unit = unit


def test_unit_aliases() -> None:
    assert normalize_unit("pcs") == "EA"
    assert normalize_unit("Pieces") == "EA"
    assert normalize_unit("box") == "BOX"
    assert normalize_unit("KGS") == "KG"
    assert normalize_unit("Stück") == "EA"
    assert normalize_unit("furlong") is None
    assert normalize_unit("") is None
    assert normalize_unit(None) is None


def test_sku_normalisation() -> None:
    assert normalize_sku(" sku-1001 ") == "SKU-1001"
    assert normalize_sku("sku  1001") == "SKU 1001"
    assert normalize_sku("(SKU-1001)") == "SKU-1001"


def _index() -> CatalogIndex:
    return CatalogIndex(
        [  # type: ignore[list-item]
            P("SKU-1001", "Hex bolt M8x40 zinc plated"),
            P("SKU-1002", "Hex bolt M8x60 zinc plated"),
            P("SKU-4001", "Deep groove ball bearing 6204-2RS"),
            P("SKU-4003", "Deep groove ball bearing 6204-ZZ"),
            P("SKU-9002", "Industrial degreaser 20L"),
        ]
    )


def test_exact_sku_match() -> None:
    out = _index().match(sku_hint="sku-1001", description="anything", raw_text="")
    assert out.status == "exact"
    assert out.candidates[0].product.sku == "SKU-1001"
    assert out.candidates[0].score == D("100")


def test_sku_token_in_text() -> None:
    out = _index().match(
        sku_hint=None, description="bolt per SKU-1002 please", raw_text="10 pcs bolt per SKU-1002 please"
    )
    assert out.status == "exact"
    assert out.candidates[0].matched_on == "sku_token"


def test_ambiguous_description() -> None:
    out = _index().match(sku_hint=None, description="ball bearing 6204", raw_text="ball bearing 6204")
    assert out.status == "ambiguous"
    assert len(out.candidates) >= 2
    assert all(c.category == "ambiguous" for c in out.candidates)


def test_likely_description() -> None:
    out = _index().match(sku_hint=None, description="industrial degreaser 20 l", raw_text="")
    assert out.status in ("likely", "ambiguous")
    assert out.candidates[0].product.sku == "SKU-9002"


def test_unmatched() -> None:
    out = _index().match(sku_hint="ZZZ-404", description="quantum flux capacitor", raw_text="quantum flux capacitor")
    assert out.status == "unmatched"
    assert out.candidates == ()


def test_wrong_sku_hint_falls_back_to_description() -> None:
    out = _index().match(sku_hint="NOPE-1", description="Hex bolt M8x40 zinc plated", raw_text="")
    assert out.status in ("likely", "ambiguous")
    assert out.candidates[0].product.sku == "SKU-1001"
