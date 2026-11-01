"""Build export payloads from quote versions; store PDFs privately with SHA-256 (idempotent)."""

from __future__ import annotations

import hashlib
import uuid
from datetime import date

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
    """Approved versions render exclusively from their frozen `document_snapshot` (H-1); drafts from live data."""
    snap = version.document_snapshot if version.status == "approved" and version.document_snapshot else None
    if snap is not None:
        org_s = snap.get("organization") or {}
        cust_s = snap.get("customer") or {}
        org_name, legal, address, brand, footer = (
            org_s.get("name", org.name),
            org_s.get("legal_name"),
            org_s.get("address"),
            org_s.get("brand_color", org.brand_color),
            org_s.get("pdf_footer"),
        )
        valid_until = snap.get("valid_until")
        cust_name, cust_ref = cust_s.get("name"), cust_s.get("reference")
        notes, terms = snap.get("notes"), snap.get("terms")
        label = "APPROVED"
    else:
        customer = db.get(Customer, quote.customer_id) if quote.customer_id else None
        org_name, legal, address, brand, footer = org.name, org.legal_name, org.address, org.brand_color, org.pdf_footer
        valid_until = quote.valid_until.isoformat() if quote.valid_until else None
        cust_name, cust_ref = (customer.name, customer.reference) if customer else (None, None)
        notes, terms = version.notes, version.terms
        label = "APPROVED" if version.status == "approved" else "DRAFT – NOT APPROVED"
    if quote.status == "withdrawn" and version.status != "approved":
        label = "WITHDRAWN"
    return PdfQuote(
        org_name=org_name,
        org_legal_name=legal,
        org_address=address,
        brand_color=brand,
        footer=footer,
        number=quote.number,
        version_no=version.version_no,
        status_label=label,
        approved_on=version.approved_at.date().isoformat() if version.approved_at else None,
        valid_until=valid_until,
        customer_name=cust_name,
        customer_reference=cust_ref,
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
        notes=notes,
        terms=terms,
    )


def _version_valid_until(quote: Quote, version: QuoteVersion) -> date | None:
    """Validity frozen at approval (immutable); falls back to the quote header for drafts."""
    snap = version.document_snapshot or {}
    raw = snap.get("valid_until")
    if raw:
        return date.fromisoformat(str(raw))
    return quote.valid_until


def _check_exportable(quote: Quote, version: QuoteVersion) -> None:
    """Approved versions of withdrawn or expired quotes must not be exported as current documents (S1-A15).
    Expiry is judged on the version's own frozen validity, so later header edits cannot revive it (H-1)."""
    if version.status == "approved":
        if quote.status == "withdrawn":
            raise StateError("Quote was withdrawn; the approved document is no longer exportable as current")
        today = date.today()  # noqa: DTZ011
        vu = _version_valid_until(quote, version)
        header_expired = (
            version.version_no == quote.current_version_no
            and quote.valid_until is not None
            and quote.valid_until < today
        )
        if (vu is not None and vu < today) or header_expired:
            raise StateError("This approved version's validity has expired; create a new version before exporting")


def store_approved_pdf(db: Session, *, org: Organization, quote: Quote, version: QuoteVersion) -> tuple[bytes, str]:
    """Render the approved document exactly once at approval time and store it; the bytes are never replaced."""
    if version.pdf_storage_key and get_storage().exists(version.pdf_storage_key):
        with get_storage().open(version.pdf_storage_key) as fh:
            data = fh.read()
        return data, version.pdf_sha256 or hashlib.sha256(data).hexdigest()
    data = render_quote_pdf(build_pdf_model(db, org, quote, version))
    sha = hashlib.sha256(data).hexdigest()
    key = f"org/{org.id}/quotes/{quote.id}/v{version.version_no}.pdf"
    get_storage().put(key, data)
    version.pdf_storage_key = key
    version.pdf_sha256 = sha
    db.flush()
    return data, sha


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
    if version.status == "approved":
        data, sha = store_approved_pdf(db, org=org, quote=quote, version=version)  # immutable stored artifact
    else:
        data = render_quote_pdf(build_pdf_model(db, org, quote, version))
        sha = hashlib.sha256(data).hexdigest()
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
