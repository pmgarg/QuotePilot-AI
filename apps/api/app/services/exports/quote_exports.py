"""Build export payloads from quote versions; store PDFs privately with SHA-256 (idempotent)."""

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy.orm import Session

from app.core.errors import StateError
from app.models.customer import Customer
from app.models.org import Organization
from app.models.quote import Quote, QuoteVersion
from app.services import audit
from app.services.exports.csv_export import write_csv
from app.services.exports.pdf import PdfLine, PdfQuote, render_quote_pdf
from app.services.storage import get_storage


def build_pdf_model(db: Session, org: Organization, quote: Quote, version: QuoteVersion) -> PdfQuote:
    customer = db.get(Customer, quote.customer_id) if quote.customer_id else None
    if version.status == "approved":
        label = "APPROVED"
    else:
        label = "DRAFT – NOT APPROVED"
    if quote.status == "withdrawn":
        label = "WITHDRAWN"
    return PdfQuote(
        org_name=org.name,
        org_legal_name=org.legal_name,
        org_address=org.address,
        brand_color=org.brand_color,
        footer=org.pdf_footer,
        number=quote.number,
        version_no=version.version_no,
        status_label=label,
        approved_on=version.approved_at.date().isoformat() if version.approved_at else None,
        valid_until=quote.valid_until.isoformat() if quote.valid_until else None,
        customer_name=customer.name if customer else None,
        customer_reference=customer.reference if customer else None,
        currency=quote.currency,
        lines=tuple(
            PdfLine(
                line_no=ln.line_no,
                sku=ln.sku,
                description=ln.description,
                quantity=ln.quantity,
                unit=ln.unit,
                unit_price=ln.unit_price,
                discount_pct=ln.discount_pct,
                tax_rate_pct=ln.tax_rate_pct,
                line_total=ln.line_total,
            )
            for ln in version.lines
        ),
        subtotal=version.subtotal,
        discount_total=version.discount_total,
        tax_total=version.tax_total,
        grand_total=version.grand_total,
        notes=version.notes,
        terms=version.terms,
    )


def _check_exportable(quote: Quote, version: QuoteVersion) -> None:
    """Approved versions of withdrawn or expired quotes must not be exported as current documents (S1-A15)."""
    from app.services.quotes import effective_status

    if version.status == "approved":
        if quote.status == "withdrawn":
            raise StateError("Quote was withdrawn; the approved document is no longer exportable as current")
        if effective_status(quote) == "expired":
            raise StateError("Quote validity has expired; create a new version before exporting")


def render_and_store_pdf(
    db: Session,
    *,
    org: Organization,
    actor_user_id: uuid.UUID,
    ip: str | None,
    quote: Quote,
    version: QuoteVersion,
) -> tuple[bytes, str]:
    _check_exportable(quote, version)
    model = build_pdf_model(db, org, quote, version)
    data = render_quote_pdf(model)
    sha = hashlib.sha256(data).hexdigest()
    storage = get_storage()
    if version.status == "approved":
        key = version.pdf_storage_key or f"org/{org.id}/quotes/{quote.id}/v{version.version_no}.pdf"
        if version.pdf_sha256 != sha or not storage.exists(key):
            storage.put(key, data)
            version.pdf_storage_key = key
            version.pdf_sha256 = sha
            db.flush()
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=actor_user_id,
        action="quote.export_pdf",
        entity_type="quote_version",
        entity_id=version.id,
        ip=ip,
        summary={"version_no": version.version_no, "sha256": sha, "status": version.status},
    )
    return data, sha


def render_csv(
    db: Session,
    *,
    org: Organization,
    actor_user_id: uuid.UUID,
    ip: str | None,
    quote: Quote,
    version: QuoteVersion,
    include_costs: bool,
) -> bytes:
    _check_exportable(quote, version)
    header = [
        "line_no",
        "sku",
        "description",
        "quantity",
        "unit",
        "unit_price",
        "discount_pct",
        "tax_rate_pct",
        "line_subtotal",
        "discount_amount",
        "tax_amount",
        "line_total",
    ]
    if include_costs:
        header += ["cost_price"]
    rows = []
    for ln in version.lines:
        row: list[object] = [
            ln.line_no,
            ln.sku,
            ln.description,
            ln.quantity,
            ln.unit,
            ln.unit_price,
            ln.discount_pct,
            ln.tax_rate_pct,
            ln.line_subtotal,
            ln.discount_amount,
            ln.tax_amount,
            ln.line_total,
        ]
        if include_costs:
            row.append(ln.cost_price)
        rows.append(row)
    rows.append(
        [
            "",
            "",
            f"TOTAL {quote.currency}",
            "",
            "",
            "",
            "",
            "",
            version.subtotal,
            version.discount_total,
            version.tax_total,
            version.grand_total,
        ]
        + ([""] if include_costs else [])
    )
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=actor_user_id,
        action="quote.export_csv",
        entity_type="quote_version",
        entity_id=version.id,
        ip=ip,
        summary={"version_no": version.version_no, "include_costs": include_costs},
    )
    return write_csv(header, rows)
