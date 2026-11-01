"""Deterministic branded quote PDF (ADR-007). Same input → identical bytes."""

from __future__ import annotations

import io
from dataclasses import dataclass
from decimal import Decimal
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Flowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.services.money import minor_units


@dataclass(frozen=True)
class PdfLine:
    line_no: int
    sku: str | None
    description: str
    quantity: Decimal
    unit: str
    unit_price: Decimal | None
    discount_pct: Decimal
    tax_rate_pct: Decimal
    line_total: Decimal


@dataclass(frozen=True)
class PdfQuote:
    org_name: str
    org_legal_name: str | None
    org_address: str | None
    brand_color: str
    footer: str | None
    number: str
    version_no: int
    status_label: str  # "APPROVED" or "DRAFT – NOT APPROVED"
    approved_on: str | None
    valid_until: str | None
    customer_name: str | None
    customer_reference: str | None
    currency: str
    lines: tuple[PdfLine, ...]
    subtotal: Decimal
    discount_total: Decimal
    tax_total: Decimal
    grand_total: Decimal
    notes: str | None
    terms: str | None


def _fmt(value: Decimal | None, currency: str) -> str:
    if value is None:
        return "—"
    places = minor_units(currency)
    q = value.quantize(Decimal(1).scaleb(-places))
    return f"{q:,.{places}f}"


def _pct(v: Decimal) -> str:
    return f"{v.normalize():f}%" if v != 0 else "0%"


def _hex(color: str) -> colors.Color:
    try:
        return colors.HexColor(color)
    except (ValueError, TypeError):
        return colors.HexColor("#1f4e79")


def render_quote_pdf(q: PdfQuote) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=f"Quotation {q.number} v{q.version_no}",
        author=q.org_name,
        subject="Quotation",
        creator="QuotePilot AI",
        invariant=1,  # removes timestamps / random IDs → reproducible bytes
    )
    styles = getSampleStyleSheet()
    brand = _hex(q.brand_color)
    h1 = ParagraphStyle("h1", parent=styles["Title"], textColor=brand, alignment=0, fontSize=18, spaceAfter=4)
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8.5, leading=11)
    right = ParagraphStyle("right", parent=styles["Normal"], alignment=TA_RIGHT)
    cell = ParagraphStyle("cell", parent=styles["Normal"], fontSize=8.5, leading=10.5)
    story: list[Flowable] = []
    story.append(Paragraph(escape(q.org_name), h1))
    org_lines = [escape(x) for x in filter(None, [q.org_legal_name, *(q.org_address or "").splitlines()])]
    if org_lines:
        story.append(Paragraph("<br/>".join(org_lines), small))
    story.append(Spacer(1, 6 * mm))
    meta = [
        ["Quotation", q.number],
        ["Version", str(q.version_no)],
        ["Status", q.status_label],
        ["Approved on", q.approved_on or "—"],
        ["Valid until", q.valid_until or "—"],
        ["Currency", q.currency],
        ["Customer", q.customer_name or "—"],
        ["Customer reference", q.customer_reference or "—"],
    ]
    mt = Table(
        [[Paragraph(escape(a), small), Paragraph(escape(b), small)] for a, b in meta],
        colWidths=[40 * mm, 80 * mm],
        hAlign="LEFT",
    )
    mt.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("TEXTCOLOR", (0, 0), (0, -1), colors.grey)]))
    story.append(mt)
    story.append(Spacer(1, 6 * mm))
    header = ["#", "SKU", "Description", "Qty", "Unit", "Unit price", "Disc.", "Tax", "Line total"]
    data: list[list[object]] = [[Paragraph(f"<b>{escape(h)}</b>", cell) for h in header]]
    for ln in q.lines:
        data.append(
            [
                str(ln.line_no),
                Paragraph(escape(ln.sku or ""), cell),
                Paragraph(escape(ln.description), cell),
                f"{ln.quantity.normalize():f}",
                ln.unit,
                _fmt(ln.unit_price, q.currency),
                _pct(ln.discount_pct),
                _pct(ln.tax_rate_pct),
                _fmt(ln.line_total, q.currency),
            ]
        )
    t = Table(
        data,
        colWidths=[8 * mm, 24 * mm, 60 * mm, 14 * mm, 12 * mm, 20 * mm, 12 * mm, 12 * mm, 22 * mm],
        repeatRows=1,
    )
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), brand),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                ("ALIGN", (3, 1), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f7fa")]),
            ]
        )
    )
    story.append(t)
    story.append(Spacer(1, 4 * mm))
    totals = [
        ["Subtotal", _fmt(q.subtotal, q.currency)],
        ["Discount", "-" + _fmt(q.discount_total, q.currency)],
        ["Tax", _fmt(q.tax_total, q.currency)],
        [f"Total ({q.currency})", _fmt(q.grand_total, q.currency)],
    ]
    tt = Table(totals, colWidths=[40 * mm, 30 * mm], hAlign="RIGHT")
    tt.setStyle(
        TableStyle(
            [
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                ("LINEABOVE", (0, -1), (-1, -1), 0.75, brand),
            ]
        )
    )
    story.append(tt)
    if q.notes:
        story.append(Spacer(1, 5 * mm))
        story.append(Paragraph("<b>Notes</b><br/>" + escape(q.notes).replace("\n", "<br/>"), small))
    if q.terms:
        story.append(Spacer(1, 3 * mm))
        story.append(Paragraph("<b>Terms</b><br/>" + escape(q.terms).replace("\n", "<br/>"), small))
    if q.footer:
        story.append(Spacer(1, 6 * mm))
        story.append(Paragraph(escape(q.footer), ParagraphStyle("footer", parent=small, textColor=colors.grey)))
    story.append(Spacer(1, 2 * mm))
    story.append(Paragraph(escape(q.status_label), right))
    doc.build(story)
    return buf.getvalue()
