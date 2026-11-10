"""Quote lifecycle: creation from RFQ, editing, blockers, submission, approval, versions, concurrency (ADR-010)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import CursorResult, select, text, update
from sqlalchemy.orm import Session

from app.core.errors import (
    BlockedError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    StateError,
    ValidationFailed,
)
from app.db.base import utcnow
from app.models.catalog import Product
from app.models.customer import Customer
from app.models.org import Organization
from app.models.quote import ApprovalDecision, Quote, QuoteLine, QuoteVersion
from app.models.rfq import RFQ
from app.services import audit
from app.services.authz import Permission, has_permission
from app.services.money import is_supported_currency
from app.services.pricing import LineInput, compute_line, validate_line

MAX_TOTAL = Decimal("100000000000000")  # 1e14 < NUMERIC(18,3) capacity; keeps every aggregate representable


@dataclass(frozen=True)
class Blocker:
    code: str
    message: str
    line_no: int | None = None
    overridable_by: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "line_no": self.line_no,
            "overridable_by": list(self.overridable_by),
        }


def get_quote(db: Session, organization_id: uuid.UUID, quote_id: uuid.UUID) -> Quote:
    quote = db.scalar(select(Quote).where(Quote.id == quote_id, Quote.organization_id == organization_id))
    if quote is None:
        raise NotFoundError("Quote not found")
    return quote


def current_version(quote: Quote) -> QuoteVersion:
    for v in quote.versions:
        if v.version_no == quote.current_version_no:
            return v
    raise StateError("Quote has no current version")


def get_version(quote: Quote, version_no: int) -> QuoteVersion:
    for v in quote.versions:
        if v.version_no == version_no:
            return v
    raise NotFoundError("Quote version not found")


def effective_status(quote: Quote, today: date | None = None) -> str:
    today = today or date.today()  # noqa: DTZ011
    if quote.status == "approved" and quote.valid_until and quote.valid_until < today:
        return "expired"
    return quote.status


def allocate_number(db: Session, organization_id: uuid.UUID) -> str:
    seq = db.execute(
        text("UPDATE organizations SET quote_seq = quote_seq + 1 WHERE id = :org RETURNING quote_seq"),
        {"org": str(organization_id)},
    ).scalar_one()
    return f"Q-{utcnow().year}-{int(seq):05d}"


def _recompute(version: QuoteVersion, org: Organization, currency: str) -> None:
    subtotal = discount = tax = total = Decimal("0")
    for line in version.lines:
        r = compute_line(
            LineInput(
                line.quantity,
                line.unit_price,
                line.discount_pct,
                line.tax_rate_pct,
                line.cost_price,
            ),
            currency,
            org.rounding_mode,
        )
        line.line_subtotal, line.discount_amount, line.tax_amount, line.line_total = (
            r.line_subtotal,
            r.discount_amount,
            r.tax_amount,
            r.line_total,
        )
        subtotal += r.line_subtotal
        discount += r.discount_amount
        tax += r.tax_amount
        total += r.line_total
    version.subtotal, version.discount_total, version.tax_total, version.grand_total = (
        subtotal,
        discount,
        tax,
        total,
    )
    if max(abs(subtotal), abs(total)) >= MAX_TOTAL:
        raise ValidationFailed(f"Quote totals exceed the supported maximum of {MAX_TOTAL:f} {currency}")


def stale_catalog_lines(db: Session, org: Organization, version: QuoteVersion) -> set[int]:
    """Line numbers priced `catalog` from a product outside the quote's pricing version (NEW-13): happens to
    ad-hoc quotes when a new catalog version is activated after the line was linked."""
    pids = [ln.product_id for ln in version.lines if ln.product_id is not None and ln.price_source == "catalog"]
    if not pids:
        return set()
    allowed = _pricing_catalog_version_id(db, org, version)
    versions: dict[uuid.UUID, uuid.UUID] = {
        pid: cv
        for pid, cv in db.execute(
            select(Product.id, Product.catalog_version_id).where(
                Product.id.in_(pids), Product.organization_id == org.id
            )
        )
    }
    return {
        ln.line_no
        for ln in version.lines
        if ln.product_id is not None and ln.price_source == "catalog" and versions.get(ln.product_id) != allowed
    }


def compute_blockers(
    quote: Quote,
    version: QuoteVersion,
    org: Organization,
    *,
    reveal_margin: bool = True,
    db: Session | None = None,
) -> list[Blocker]:
    """Server-side submission blockers. `reveal_margin=False` produces messages without margin figures or the
    confidential floor (for roles without VIEW_COSTS). With `db`, catalog lines are also checked against the
    quote's pricing catalog version."""
    blockers: list[Blocker] = []
    if not version.lines:
        blockers.append(Blocker("no_lines", "Quote has no lines"))
    stale = stale_catalog_lines(db, org, version) if db is not None else set()
    for line in version.lines:
        if line.line_no in stale:
            blockers.append(
                Blocker(
                    "catalog_version_mismatch",
                    f"Line {line.line_no} is priced from a catalog version that is no longer this quote's price "
                    "list; re-link the product to the active catalog",
                    line.line_no,
                )
            )
        if line.product_id is None and not line.is_custom:
            blockers.append(
                Blocker(
                    "line_unresolved",
                    f"Line {line.line_no} is not linked to a product and not marked custom",
                    line.line_no,
                )
            )
        if line.unit_price is None:
            blockers.append(Blocker("missing_price", f"Line {line.line_no} has no unit price", line.line_no))
        if line.price_source == "none" and line.unit_price is not None:
            blockers.append(Blocker("missing_price", f"Line {line.line_no} price source unknown", line.line_no))
        if line.price_source == "manual" and not line.margin_override_reason:
            # S1-A03 / M-4: a manually entered price (custom line or typed over a catalog price) is not verified
            # against any cost; it needs a privileged, reasoned confirmation before submission.
            blockers.append(
                Blocker(
                    "manual_price_unconfirmed",
                    f"Line {line.line_no} has a manually entered price; an owner/admin must confirm it with a reason",
                    line.line_no,
                    ("owner", "admin"),
                )
            )
        r = compute_line(
            LineInput(
                line.quantity,
                line.unit_price,
                line.discount_pct,
                line.tax_rate_pct,
                line.cost_price,
            ),
            quote.currency,
            org.rounding_mode,
        )
        if r.margin_pct is not None and r.margin_pct < org.min_margin_pct and not line.margin_override_reason:
            if reveal_margin:
                msg = (
                    f"Line {line.line_no} margin {r.margin_pct}% is below the organisation floor "
                    f"of {org.min_margin_pct}%"
                )
            else:
                msg = f"Line {line.line_no} price is below the organisation's minimum margin policy"
            blockers.append(Blocker("below_min_margin", msg, line.line_no, ("owner", "admin")))
    if quote.valid_until and quote.valid_until < date.today():  # noqa: DTZ011
        blockers.append(Blocker("validity_expired", "Validity date is in the past"))
    return blockers


def _check_lock(db: Session, quote: Quote, expected: int) -> None:
    """Conditional increment; raising ConflictError if another writer moved the version (race-safe)."""
    res = db.execute(
        update(Quote)
        .where(Quote.id == quote.id, Quote.lock_version == expected)
        .values(lock_version=Quote.lock_version + 1)
    )
    if cast("CursorResult[Any]", res).rowcount != 1:
        db.refresh(quote)
        raise ConflictError("Quote was modified by another user", current_lock_version=quote.lock_version)
    db.refresh(quote)


def create_quote(
    db: Session,
    *,
    org: Organization,
    actor_user_id: uuid.UUID,
    ip: str | None,
    customer_id: uuid.UUID | None,
    currency: str | None,
    rfq: RFQ | None = None,
) -> Quote:
    cur = (currency or (rfq.currency if rfq else None) or org.default_currency).upper()
    if not is_supported_currency(cur):
        raise ValidationFailed(f"Unsupported currency '{cur}'", field="currency")
    if (
        customer_id is not None
        and db.scalar(select(Customer.id).where(Customer.id == customer_id, Customer.organization_id == org.id)) is None
    ):
        raise NotFoundError("Customer not found")
    quote = Quote(
        organization_id=org.id,
        number=allocate_number(db, org.id),
        customer_id=customer_id,
        rfq_id=rfq.id if rfq else None,
        currency=cur,
        status="draft",
        current_version_no=1,
        lock_version=0,
        valid_until=date.today() + timedelta(days=org.default_validity_days),  # noqa: DTZ011
        created_by=actor_user_id,
    )
    db.add(quote)
    db.flush()
    version = QuoteVersion(
        organization_id=org.id,
        quote_id=quote.id,
        version_no=1,
        status="draft",
        pricing_snapshot=_snapshot(org, cur, rfq),
    )
    db.add(version)
    db.flush()
    db.refresh(quote)
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=actor_user_id,
        action="quote.create",
        entity_type="quote",
        entity_id=quote.id,
        ip=ip,
        summary={"number": quote.number, "currency": cur, "rfq_id": str(rfq.id) if rfq else None},
    )
    return quote


def build_document_snapshot(db: Session, org: Organization, quote: Quote, version: QuoteVersion) -> dict[str, Any]:
    """Everything the approved document depends on, frozen at approval (H-1). Lines are immutable rows already."""
    customer = db.get(Customer, quote.customer_id) if quote.customer_id else None
    return {
        "valid_until": quote.valid_until.isoformat() if quote.valid_until else None,
        "customer": {"name": customer.name, "reference": customer.reference} if customer else None,
        "organization": {
            "name": org.name,
            "legal_name": org.legal_name,
            "address": org.address,
            "brand_color": org.brand_color,
            "pdf_footer": org.pdf_footer,
        },
        "notes": version.notes,
        "terms": version.terms,
        "currency": quote.currency,
    }


def _pricing_catalog_version_id(db: Session, org: Organization, version: QuoteVersion) -> uuid.UUID | None:
    """The catalog version a quote prices from: the one frozen in its pricing snapshot, else the active version."""
    from app.services.catalog_import import active_version_for_org

    raw = (version.pricing_snapshot or {}).get("catalog_version_id")
    if raw:
        return uuid.UUID(str(raw))
    active = active_version_for_org(db, org.id)
    return active.id if active else None


def _snapshot(org: Organization, currency: str, rfq: RFQ | None) -> dict[str, Any]:
    return {
        "currency": currency,
        "rounding_mode": org.rounding_mode,
        "default_tax_rate": str(org.default_tax_rate),
        "min_margin_pct": str(org.min_margin_pct),
        "catalog_version_id": str(rfq.catalog_version_id) if rfq and rfq.catalog_version_id else None,
    }


def generate_from_rfq(db: Session, *, org: Organization, actor_user_id: uuid.UUID, ip: str | None, rfq: RFQ) -> Quote:
    if rfq.status != "parsed":
        raise StateError(f"RFQ must be in 'parsed' state (currently '{rfq.status}')")
    problems: list[Blocker] = []
    for item in rfq.items:
        if item.is_duplicate and not item.duplicate_confirmed:
            problems.append(
                Blocker(
                    "duplicate_unconfirmed",
                    f"Line {item.line_no} looks like a duplicate; confirm or remove it",
                    item.line_no,
                )
            )
        if item.quantity is None:
            problems.append(Blocker("quantity_missing", f"Line {item.line_no} has no valid quantity", item.line_no))
        if item.selected_product_id is None and not item.is_custom:
            problems.append(
                Blocker(
                    "line_unresolved",
                    f"Line {item.line_no} is {item.match_status}; select a product, mark custom or remove it",
                    item.line_no,
                )
            )
        if item.unit_mismatch and not item.unit_confirmed:
            problems.append(
                Blocker(
                    "unit_mismatch_unconfirmed",
                    f"Line {item.line_no} unit '{item.unit_normalized or item.unit_raw}' differs from the "
                    "catalog unit; confirm",
                    item.line_no,
                )
            )
    if problems:
        raise BlockedError("RFQ has unresolved lines", blockers=[b.as_dict() for b in problems])
    quote = create_quote(
        db,
        org=org,
        actor_user_id=actor_user_id,
        ip=ip,
        customer_id=rfq.customer_id,
        currency=rfq.currency,
        rfq=rfq,
    )
    version = current_version(quote)
    n = 0
    for item in rfq.items:
        if item.is_duplicate and not item.duplicate_confirmed:
            continue
        n += 1
        product = (
            db.scalar(select(Product).where(Product.id == item.selected_product_id, Product.organization_id == org.id))
            if item.selected_product_id
            else None
        )
        line = QuoteLine(
            organization_id=org.id,
            quote_version_id=version.id,
            line_no=n,
            product_id=product.id if product else None,
            rfq_item_id=item.id,
            sku=product.sku if product else item.sku_hint,
            description=product.description if product else item.description,
            quantity=item.quantity,
            unit=(product.unit if product else (item.unit_normalized or item.unit_raw or "EA")),
            tax_rate_pct=org.default_tax_rate,
            is_custom=item.is_custom,
            unit_mismatch_confirmed=item.unit_confirmed,
        )
        if product is not None and product.currency == quote.currency:
            line.unit_price = product.list_price
            line.price_source = "catalog"
            line.cost_price = product.cost_price
        elif product is not None:
            # catalog currency differs from the quote currency: no auto price and no cross-currency margin
            line.unit_price = None
            line.price_source = "none"
            line.cost_price = None
        db.add(line)
    rfq.status = "quoted"
    db.flush()
    db.refresh(version)
    _recompute(version, org, quote.currency)
    db.flush()
    return quote


def update_draft(
    db: Session,
    *,
    org: Organization,
    actor_user_id: uuid.UUID,
    role: str,
    ip: str | None,
    quote: Quote,
    expected_lock_version: int,
    valid_until: date | None | object,
    notes: str | None | object,
    terms: str | None | object,
    lines: list[dict[str, Any]] | None,
) -> Quote:
    version = current_version(quote)
    if version.status not in ("draft",):
        raise StateError(
            f"Only draft versions can be edited (version is '{version.status}'). Create a new version first."
        )
    _check_lock(db, quote, expected_lock_version)
    unset = object
    if valid_until is not unset:
        quote.valid_until = valid_until  # type: ignore[assignment]
    if notes is not unset:
        version.notes = notes  # type: ignore[assignment]
    if terms is not unset:
        version.terms = terms  # type: ignore[assignment]
    line_changes: list[dict[str, Any]] = []
    if lines is not None:
        existing = {str(line.id): line for line in version.lines}
        new_lines: list[QuoteLine] = []
        for n, payload in enumerate(lines, start=1):
            line = existing.get(str(payload.get("id"))) if payload.get("id") else None
            if line is None:
                line = QuoteLine(
                    organization_id=org.id,
                    quote_version_id=version.id,
                    line_no=-(1000 + n),  # parked; renumbered after removals are flushed
                    description="",
                    quantity=Decimal("1"),
                    unit="EA",
                    price_source="none",
                    discount_pct=Decimal("0"),
                    tax_rate_pct=org.default_tax_rate,
                    is_custom=False,
                    unit_mismatch_confirmed=False,
                )
                db.add(line)
            before_price, before_discount, before_product = line.unit_price, line.discount_pct, line.product_id
            before_qty = line.quantity
            if "product_id" in payload:
                pid = payload["product_id"]
                if pid is None:
                    line.product_id = None
                    line.cost_price = None
                    if line.price_source == "catalog":
                        line.price_source = "none"
                        line.unit_price = None
                else:
                    product = db.scalar(select(Product).where(Product.id == pid, Product.organization_id == org.id))
                    if product is None:
                        raise NotFoundError("Product not found")
                    link_changed = product.id != before_product
                    # N2-1 variant B: only products of the pricing catalog version may be *linked* (no archived or
                    # merely-imported versions; inactive price lists are never a price authority). Untouched lines
                    # re-sent by the editor are not re-validated here (D2-2); a line whose product has since left
                    # the pricing version is reported by the `catalog_version_mismatch` blocker instead (NEW-13).
                    if link_changed:
                        allowed_version = _pricing_catalog_version_id(db, org, version)
                        if allowed_version is None or product.catalog_version_id != allowed_version:
                            raise ValidationFailed(
                                "Product is not in the catalog version this quote prices from "
                                "(the RFQ's version, or the active version for ad-hoc quotes)",
                                line_no=n,
                            )
                    line.product_id = product.id
                    line.sku = product.sku
                    if not payload.get("description"):
                        line.description = product.description
                    line.unit = product.unit
                    line.is_custom = False
                    if product.currency == quote.currency:
                        line.cost_price = product.cost_price
                        reset_price = "unit_price" in payload and payload["unit_price"] is None
                        if link_changed or line.price_source == "catalog" or reset_price:
                            # (re)price from the catalog; an identical price re-sent by the editor keeps the
                            # `catalog` provenance (D2-1 / NEW-17), a different price becomes manual below, and an
                            # explicit `unit_price: null` on a linked line restores the catalog price
                            line.unit_price = product.list_price
                            line.price_source = "catalog"
                    else:
                        # N2-1 variant A: a product priced in another currency carries no price or cost into the
                        # quote; the user must enter (and an owner/admin confirm) a manual price
                        line.cost_price = None
                        line.unit_price = None
                        line.price_source = "none"
            if "description" in payload and payload["description"] is not None:
                line.description = str(payload["description"]).strip()[:2000]
            if "sku" in payload and line.product_id is None:
                line.sku = (payload["sku"] or None) and str(payload["sku"])[:100]
            if "quantity" in payload and payload["quantity"] is not None:
                line.quantity = Decimal(str(payload["quantity"]))
            if "unit" in payload and payload["unit"]:
                line.unit = str(payload["unit"])[:16]
            if "unit_price" in payload and payload["unit_price"] is not None:
                new_price = Decimal(str(payload["unit_price"]))
                if line.price_source == "catalog" and line.unit_price is not None and new_price == line.unit_price:
                    pass  # unchanged catalog price re-sent by the editor: provenance stays `catalog` (D2-1)
                else:
                    line.unit_price = new_price
                    line.price_source = "manual"
            if "discount_pct" in payload and payload["discount_pct"] is not None:
                line.discount_pct = Decimal(str(payload["discount_pct"]))
            if "tax_rate_pct" in payload and payload["tax_rate_pct"] is not None:
                line.tax_rate_pct = Decimal(str(payload["tax_rate_pct"]))
            if "is_custom" in payload and payload["is_custom"] is not None:
                line.is_custom = bool(payload["is_custom"])
                if line.is_custom:
                    # a custom line never carries catalog pricing or cost (NEW-4): the typed price is manual
                    line.product_id = None
                    line.cost_price = None
                    if line.unit_price is not None:
                        line.price_source = "manual"
            if line.product_id is None and not line.is_custom and line.price_source == "manual":
                line.is_custom = True
            errors = validate_line(
                LineInput(
                    line.quantity,
                    line.unit_price,
                    line.discount_pct,
                    line.tax_rate_pct,
                    line.cost_price,
                )
            )
            if not line.description:
                errors.append("description is required")
            if errors:
                raise ValidationFailed(f"Line {n}: " + "; ".join(errors), line_no=n)
            # a real change to price, discount or product invalidates a previous margin override (re-sending the
            # same values, as the editor does on every save, must not silently drop an audited override)
            if line.margin_override_reason and (
                line.unit_price != before_price
                or line.discount_pct != before_discount
                or line.product_id != before_product
            ):
                line.margin_override_reason = None
                line.margin_override_by = None
            if (line.unit_price, line.discount_pct, line.product_id, line.quantity) != (
                before_price,
                before_discount,
                before_product,
                before_qty,
            ):
                line_changes.append(
                    {
                        "line_no": n,
                        "unit_price": [
                            str(before_price) if before_price is not None else None,
                            str(line.unit_price) if line.unit_price is not None else None,
                        ],
                        "discount_pct": [str(before_discount), str(line.discount_pct)],
                        "quantity": [str(before_qty), str(line.quantity)],
                        "product_id": [
                            str(before_product) if before_product else None,
                            str(line.product_id) if line.product_id else None,
                        ],
                    }
                )
            new_lines.append(line)
        for line in version.lines:
            if line not in new_lines:
                line_changes.append({"line_no": line.line_no, "removed": True})
                db.delete(line)
        db.flush()
        # unique (version, line_no): park every surviving line on a negative number before the final renumbering so
        # reordering/removal never collides with a row that has not moved yet (R-3/R-4)
        for idx, line in enumerate(new_lines, start=1):
            line.line_no = -idx
        db.flush()
        for idx, line in enumerate(new_lines, start=1):
            line.line_no = idx
        db.flush()
        db.expire(version, ["lines"])
    _recompute(version, org, quote.currency)
    db.flush()
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=actor_user_id,
        action="quote.update",
        entity_type="quote",
        entity_id=quote.id,
        ip=ip,
        summary={
            "version_no": version.version_no,
            "line_count": len(version.lines),
            "line_changes": line_changes[:50],
            "header_changes": [
                k for k, v in (("valid_until", valid_until), ("notes", notes), ("terms", terms)) if v is not unset
            ],
        },
    )
    return quote


def submit(
    db: Session,
    *,
    org: Organization,
    actor_user_id: uuid.UUID,
    ip: str | None,
    quote: Quote,
    expected_lock_version: int,
    reveal_margin: bool = False,
) -> Quote:
    version = current_version(quote)
    if version.status != "draft":
        raise StateError(f"Version is '{version.status}', not draft")
    if effective_status(quote) in ("withdrawn",):
        raise StateError("Withdrawn quotes cannot be submitted; create a new version")
    blockers = compute_blockers(quote, version, org, reveal_margin=reveal_margin, db=db)
    if blockers:
        raise BlockedError("Quote cannot be submitted", blockers=[b.as_dict() for b in blockers])
    _check_lock(db, quote, expected_lock_version)
    version.status = "pending_approval"
    version.submitted_by = actor_user_id
    version.submitted_at = utcnow()
    quote.status = "pending_approval"
    db.flush()
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=actor_user_id,
        action="quote.submit",
        entity_type="quote",
        entity_id=quote.id,
        ip=ip,
        summary={"version_no": version.version_no, "grand_total": str(version.grand_total)},
    )
    return quote


def approve(
    db: Session,
    *,
    org: Organization,
    actor_user_id: uuid.UUID,
    role: str,
    ip: str | None,
    quote: Quote,
    expected_lock_version: int,
    reason: str | None,
) -> Quote:
    if not has_permission(role, Permission.APPROVE_QUOTE):
        raise ForbiddenError("Role may not approve quotes", action="approve_quote")
    if quote.status == "withdrawn":
        raise StateError("Quote was withdrawn; create a new version instead")
    version = current_version(quote)
    if version.status != "pending_approval":
        raise StateError(f"Version is '{version.status}', not pending approval")
    if version.submitted_by == actor_user_id and not org.allow_self_approval:
        raise ForbiddenError(
            "Separation of duties: the submitter cannot approve their own quote",
            action="self_approval",
        )
    if quote.valid_until and quote.valid_until < date.today():  # noqa: DTZ011
        raise StateError("Quote validity date has passed; update the validity before approving")
    blockers = [b for b in compute_blockers(quote, version, org, db=db) if b.code != "validity_expired"]
    if blockers:
        raise BlockedError("Quote has blockers", blockers=[b.as_dict() for b in blockers])
    _check_lock(db, quote, expected_lock_version)
    now = utcnow()
    version.status = "approved"
    version.approved_by = actor_user_id
    version.approved_at = now
    quote.status = "approved"
    quote.withdrawn_reason = None  # D2-5: the withdrawal concerned earlier documents; this one is live
    version.document_snapshot = build_document_snapshot(db, org, quote, version)
    db.add(
        ApprovalDecision(
            organization_id=org.id,
            quote_version_id=version.id,
            decision="approved",
            reason=(reason or None),
            decided_by=actor_user_id,
            decided_at=now,
        )
    )
    db.flush()
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=actor_user_id,
        action="quote.approve",
        entity_type="quote",
        entity_id=quote.id,
        ip=ip,
        summary={"version_no": version.version_no, "grand_total": str(version.grand_total)},
    )
    from app.services.exports.quote_exports import store_approved_pdf

    store_approved_pdf(db, org=org, quote=quote, version=version)
    return quote


def reject(
    db: Session,
    *,
    org: Organization,
    actor_user_id: uuid.UUID,
    role: str,
    ip: str | None,
    quote: Quote,
    expected_lock_version: int,
    reason: str,
) -> Quote:
    if not has_permission(role, Permission.APPROVE_QUOTE):
        raise ForbiddenError("Role may not reject quotes", action="approve_quote")
    if quote.status == "withdrawn":
        raise StateError("Quote was withdrawn; create a new version instead")
    version = current_version(quote)
    if version.status != "pending_approval":
        raise StateError(f"Version is '{version.status}', not pending approval")
    if not reason or not reason.strip():
        raise ValidationFailed("A rejection reason is required", field="reason")
    _check_lock(db, quote, expected_lock_version)
    now = utcnow()
    version.status = "draft"
    quote.status = "draft"
    db.add(
        ApprovalDecision(
            organization_id=org.id,
            quote_version_id=version.id,
            decision="rejected",
            reason=reason.strip()[:2000],
            decided_by=actor_user_id,
            decided_at=now,
        )
    )
    db.flush()
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=actor_user_id,
        action="quote.reject",
        entity_type="quote",
        entity_id=quote.id,
        ip=ip,
        summary={"version_no": version.version_no},
    )
    return quote


def withdraw(
    db: Session,
    *,
    org: Organization,
    actor_user_id: uuid.UUID,
    role: str,
    ip: str | None,
    quote: Quote,
    expected_lock_version: int,
    reason: str,
) -> Quote:
    if not has_permission(role, Permission.WITHDRAW_QUOTE):
        raise ForbiddenError("Role may not withdraw quotes", action="withdraw_quote")
    approved_versions = [v for v in quote.versions if v.status == "approved"]
    if quote.status not in ("approved", "pending_approval") and not approved_versions:
        raise StateError(
            "Only approved or pending quotes (or quotes with an approved version) can be withdrawn "
            f"(status '{quote.status}')"
        )
    if not reason or not reason.strip():
        raise ValidationFailed("A withdrawal reason is required", field="reason")
    _check_lock(db, quote, expected_lock_version)
    version = current_version(quote)
    now = utcnow()
    reason_text = reason.strip()[:2000]
    affected: list[QuoteVersion] = []
    if version.status == "pending_approval":
        version.status = "superseded"  # a withdrawn pending version can never be approved/rejected
        affected.append(version)
    for v in approved_versions:  # N2-2: every approved document is withdrawn, not only the current one
        v.status = "withdrawn"
        affected.append(v)
    if version.status == "draft" and approved_versions:
        # withdrawing the approved documents while a new draft is being prepared: the draft stays editable
        quote.withdrawn_reason = reason_text
    else:
        quote.status = "withdrawn"
        quote.withdrawn_reason = reason_text
    for v in affected or [version]:
        db.add(
            ApprovalDecision(
                organization_id=org.id,
                quote_version_id=v.id,
                decision="withdrawn",
                reason=reason_text,
                decided_by=actor_user_id,
                decided_at=now,
            )
        )
    db.flush()
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=actor_user_id,
        action="quote.withdraw",
        entity_type="quote",
        entity_id=quote.id,
        ip=ip,
        summary={"version_no": version.version_no, "withdrawn_versions": [v.version_no for v in affected]},
    )
    return quote


def new_version(
    db: Session,
    *,
    org: Organization,
    actor_user_id: uuid.UUID,
    ip: str | None,
    quote: Quote,
    expected_lock_version: int,
) -> Quote:
    version = current_version(quote)
    if version.status not in ("approved",) and quote.status not in (
        "withdrawn",
        "approved",
        "expired",
    ):
        raise StateError("A new version can only be created from an approved, expired or withdrawn quote")
    _check_lock(db, quote, expected_lock_version)
    if version.status == "approved":
        pass  # the approved version stays immutable and downloadable
    nv = QuoteVersion(
        organization_id=org.id,
        quote_id=quote.id,
        version_no=quote.current_version_no + 1,
        status="draft",
        notes=version.notes,
        terms=version.terms,
        pricing_snapshot=_snapshot(org, quote.currency, None)
        | {"catalog_version_id": version.pricing_snapshot.get("catalog_version_id")},
    )
    db.add(nv)
    db.flush()
    for line in version.lines:
        db.add(
            QuoteLine(
                organization_id=org.id,
                quote_version_id=nv.id,
                line_no=line.line_no,
                product_id=line.product_id,
                rfq_item_id=line.rfq_item_id,
                sku=line.sku,
                description=line.description,
                quantity=line.quantity,
                unit=line.unit,
                unit_price=line.unit_price,
                price_source=line.price_source,
                cost_price=line.cost_price,
                discount_pct=line.discount_pct,
                tax_rate_pct=line.tax_rate_pct,
                is_custom=line.is_custom,
                unit_mismatch_confirmed=line.unit_mismatch_confirmed,
            )
        )
    quote.current_version_no = nv.version_no
    quote.status = "draft"
    quote.withdrawn_reason = None
    quote.valid_until = date.today() + timedelta(days=org.default_validity_days)  # noqa: DTZ011
    db.flush()
    db.expire(quote, ["versions"])
    _recompute(current_version(quote), org, quote.currency)
    db.flush()
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=actor_user_id,
        action="quote.new_version",
        entity_type="quote",
        entity_id=quote.id,
        ip=ip,
        summary={"version_no": nv.version_no, "from_version": version.version_no},
    )
    return quote


def margin_override(
    db: Session,
    *,
    org: Organization,
    actor_user_id: uuid.UUID,
    role: str,
    ip: str | None,
    quote: Quote,
    line_id: uuid.UUID,
    expected_lock_version: int,
    reason: str,
) -> Quote:
    """Privileged confirmation of a line price: clears `below_min_margin` and `manual_price_unconfirmed` blockers."""
    if not has_permission(role, Permission.OVERRIDE_PRICING):
        raise ForbiddenError("Role may not override pricing rules", action="override_pricing")
    if not reason or len(reason.strip()) < 5:
        raise ValidationFailed("An override reason (≥ 5 characters) is required", field="reason")
    version = current_version(quote)
    if version.status != "draft":
        raise StateError("Overrides apply to draft versions only")
    line = next((ln for ln in version.lines if ln.id == line_id), None)
    if line is None:
        raise NotFoundError("Quote line not found")
    _check_lock(db, quote, expected_lock_version)
    line.margin_override_reason = reason.strip()[:2000]
    line.margin_override_by = actor_user_id
    db.flush()
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=actor_user_id,
        action="quote.margin_override",
        entity_type="quote_line",
        entity_id=line.id,
        ip=ip,
        summary={
            "quote_id": str(quote.id),
            "line_no": line.line_no,
            "reason": line.margin_override_reason,
        },
    )
    return quote
