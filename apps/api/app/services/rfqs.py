"""RFQ intake: safe storage, sandboxed parsing, item extraction and candidate matching."""

from __future__ import annotations

import hashlib
import uuid
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from app.core.config import get_settings
from app.core.errors import NotFoundError, StateError, ValidationFailed
from app.models.catalog import CatalogVersion, Product
from app.models.files import UploadedFile
from app.models.rfq import RFQ, MatchCandidate, RFQItem
from app.services import audit
from app.services.catalog_import import active_version_for_org
from app.services.matching import CatalogIndex
from app.services.money import MAX_QUANTITY
from app.services.parsing.extract import (
    ParsedItem,
    items_from_pdf,
    items_from_spreadsheet,
    items_from_text_lines,
)
from app.services.parsing.limits import check_size, check_xlsx_zip, sniff_kind
from app.services.parsing.sandbox import ParseFailure, run_parser
from app.services.storage import get_storage, make_key
from app.services.units import normalize_sku, normalize_text, normalize_unit


def _stored_quantity(parsed_item: ParsedItem) -> Decimal | None:
    """A quantity read from a customer's file or text is stored only when it fits the range the API accepts for
    manual entry (0 < q <= MAX_QUANTITY, three decimals). Anything else is left empty and flagged, so the line is
    blocked for a human instead of crashing the upload or flowing into a quote (SEC4-6)."""
    quantity = parsed_item.quantity
    if quantity is None:
        return None
    if not quantity.is_finite() or quantity > MAX_QUANTITY:
        parsed_item.flags.append("quantity_out_of_range")
        return None
    quantity = quantity.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    if quantity <= 0:
        parsed_item.flags.append("quantity_not_positive")
        return None
    return quantity


def _store_upload(
    db: Session,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    filename: str,
    content: bytes,
    kind: str,
) -> UploadedFile:
    key = make_key(organization_id, "rfq", filename)
    get_storage().put(key, content)
    ct = {
        "pdf": "application/pdf",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "csv": "text/csv",
    }[kind]
    upload = UploadedFile(
        organization_id=organization_id,
        kind="rfq",
        original_name=filename[:255],
        content_type=ct,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        storage_key=key,
        uploaded_by=actor_user_id,
    )
    db.add(upload)
    db.flush()
    return upload


def _precheck_xlsx(content: bytes, settings: object) -> None:
    """Reject invalid/zip-bomb workbooks before anything is stored (413/415/422)."""
    import tempfile

    fd, name = tempfile.mkstemp(suffix=".xlsx")
    path = Path(name)
    try:
        with open(fd, "wb") as fh:
            fh.write(content)
        check_xlsx_zip(path, settings.xlsx_max_uncompressed_bytes, settings.xlsx_max_compression_ratio)  # type: ignore[attr-defined]
    finally:
        path.unlink(missing_ok=True)


def create_rfq_from_file(
    db: Session,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    ip: str | None,
    filename: str,
    content: bytes,
    customer_id: uuid.UUID | None,
    reference: str | None,
    currency: str,
) -> RFQ:
    settings = get_settings()
    check_size(len(content), settings.max_upload_bytes)
    kind = sniff_kind(filename, content[:4096])
    if kind == "xlsx":
        _precheck_xlsx(content, settings)
    upload = _store_upload(
        db,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        filename=filename,
        content=content,
        kind=kind,
    )
    rfq = RFQ(
        organization_id=organization_id,
        customer_id=customer_id,
        reference=(reference or "").strip()[:100] or None,
        source_kind=kind,
        source_file_id=upload.id,
        status="uploaded",
        currency=currency,
        created_by=actor_user_id,
    )
    db.add(rfq)
    db.flush()
    audit.record(
        db,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        action="rfq.create",
        entity_type="rfq",
        entity_id=rfq.id,
        ip=ip,
        summary={"source_kind": kind, "file": upload.original_name, "size": len(content)},
    )
    parse_rfq(db, rfq, actor_user_id=actor_user_id, ip=ip)
    return rfq


def create_rfq_from_text(
    db: Session,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    ip: str | None,
    text: str,
    customer_id: uuid.UUID | None,
    reference: str | None,
    currency: str,
) -> RFQ:
    settings = get_settings()
    text = text.replace("\x00", "")
    if not text.strip():
        raise ValidationFailed("Pasted text is empty.")
    if len(text) > settings.max_pasted_text_chars:
        from app.core.errors import PayloadTooLarge

        raise PayloadTooLarge(f"Pasted text exceeds {settings.max_pasted_text_chars} characters.")
    rfq = RFQ(
        organization_id=organization_id,
        customer_id=customer_id,
        reference=(reference or "").strip()[:100] or None,
        source_kind="text",
        pasted_text=text,
        status="uploaded",
        currency=currency,
        created_by=actor_user_id,
    )
    db.add(rfq)
    db.flush()
    audit.record(
        db,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        action="rfq.create",
        entity_type="rfq",
        entity_id=rfq.id,
        ip=ip,
        summary={"source_kind": "text", "chars": len(text)},
    )
    parse_rfq(db, rfq, actor_user_id=actor_user_id, ip=ip)
    return rfq


def _extract(rfq: RFQ) -> tuple[list[ParsedItem], list[str]]:
    if rfq.source_kind == "text":
        return items_from_text_lines(rfq.pasted_text or "", {"source": "pasted_text"}), []
    settings = get_settings()
    db = object_session(rfq)
    assert db is not None
    upload = db.get(UploadedFile, rfq.source_file_id) if rfq.source_file_id else None
    if upload is None:
        raise ParseFailure("missing_file", "Original file is no longer available.")
    storage = get_storage()
    path = storage.local_path(upload.storage_key)
    tmp: Path | None = None
    if path is None:
        import tempfile

        fd, name = tempfile.mkstemp(suffix=f".{rfq.source_kind}")
        with open(fd, "wb") as fh, storage.open(upload.storage_key) as src:
            fh.write(src.read())
        tmp = path = Path(name)
    try:
        if rfq.source_kind == "xlsx":
            check_xlsx_zip(path, settings.xlsx_max_uncompressed_bytes, settings.xlsx_max_compression_ratio)
        parsed = run_parser(rfq.source_kind, path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    if rfq.source_kind == "pdf":
        return items_from_pdf(parsed)
    return items_from_spreadsheet(parsed, rfq.source_kind)


def parse_rfq(db: Session, rfq: RFQ, *, actor_user_id: uuid.UUID, ip: str | None) -> RFQ:
    """Parse + match. Items are only persisted after a fully successful extraction (S1-A17)."""
    if rfq.status == "quoted":
        raise StateError("RFQ already has a quote; items are locked.")
    rfq.status = "parsing"
    db.flush()
    try:
        items, flags = _extract(rfq)
    except (ParseFailure, Exception) as exc:  # noqa: BLE001 — any failure must leave a clean failed state
        code = getattr(exc, "code", "parser_error")
        message = getattr(exc, "message", None) or "Document could not be parsed."
        if isinstance(exc, (ValidationFailed,)) or exc.__class__.__name__ in (
            "PayloadTooLarge",
            "UnsupportedMedia",
        ):
            message = getattr(exc, "detail", message)
        rfq.status = "failed"
        rfq.failure_reason = f"{code}: {message}"[:500]
        for old in list(rfq.items):
            db.delete(old)
        db.flush()
        audit.record(
            db,
            organization_id=rfq.organization_id,
            actor_user_id=actor_user_id,
            action="rfq.parse_failed",
            entity_type="rfq",
            entity_id=rfq.id,
            ip=ip,
            summary={"reason": rfq.failure_reason},
        )
        return rfq
    if "scanned_or_image_pdf" in flags:
        rfq.status = "failed"
        rfq.failure_reason = (
            "unsupported_scanned: The PDF has no extractable text (scanned or image-only). OCR is not enabled."
        )
        rfq.parse_meta = {"flags": flags}
        for old in list(rfq.items):
            db.delete(old)
        db.flush()
        audit.record(
            db,
            organization_id=rfq.organization_id,
            actor_user_id=actor_user_id,
            action="rfq.parse_failed",
            entity_type="rfq",
            entity_id=rfq.id,
            ip=ip,
            summary={"reason": "scanned_or_image_pdf"},
        )
        return rfq
    settings = get_settings()
    if "rows_truncated" in flags or "pages_truncated" in flags or len(items) > settings.max_rfq_rows:
        rfq.status = "failed"
        rfq.failure_reason = (
            "too_large: the document exceeds the limits "
            f"({settings.max_rfq_rows} rows / {settings.max_pdf_pages} pages)."
        )
        for old in list(rfq.items):
            db.delete(old)
        db.flush()
        audit.record(
            db,
            organization_id=rfq.organization_id,
            actor_user_id=actor_user_id,
            action="rfq.parse_failed",
            entity_type="rfq",
            entity_id=rfq.id,
            ip=ip,
            summary={"reason": "too_large"},
        )
        return rfq
    for old in list(rfq.items):
        db.delete(old)
    db.flush()
    version = active_version_for_org(db, rfq.organization_id)
    rfq.catalog_version_id = version.id if version else None
    index = (
        CatalogIndex(list(db.scalars(select(Product).where(Product.catalog_version_id == version.id))))
        if version
        else None
    )
    seen_keys: set[str] = set()
    for n, parsed_item in enumerate(items, start=1):
        unit_norm = normalize_unit(parsed_item.unit_raw)
        item = RFQItem(
            organization_id=rfq.organization_id,
            rfq_id=rfq.id,
            line_no=n,
            raw_text=parsed_item.raw_text,
            source_ref=parsed_item.source_ref,
            sku_hint=parsed_item.sku_hint,
            description=parsed_item.description,
            quantity=_stored_quantity(parsed_item),
            unit_raw=parsed_item.unit_raw,
            unit_normalized=unit_norm,
            flags=list(parsed_item.flags),
        )
        if parsed_item.unit_raw and unit_norm is None and "unit_unknown" not in item.flags:
            item.flags.append("unit_unknown")
        dup_key = (
            normalize_sku(parsed_item.sku_hint) if parsed_item.sku_hint else normalize_text(parsed_item.description)
        )
        if dup_key and dup_key in seen_keys:
            item.is_duplicate = True
        elif dup_key:
            seen_keys.add(dup_key)
        db.add(item)
        db.flush()
        apply_matching(db, item, index)
    rfq.status = "parsed"
    rfq.failure_reason = None
    rfq.parse_meta = {
        "flags": flags,
        "item_count": len(items),
        "catalog_version_id": str(version.id) if version else None,
    }
    db.flush()
    return rfq


def apply_matching(db: Session, item: RFQItem, index: CatalogIndex | None) -> None:
    for c in list(item.candidates):
        db.delete(c)
    item.selected_product_id = None
    item.unit_mismatch = False
    if index is None:
        item.match_status = "unmatched"
        if "no_active_catalog" not in item.flags:
            item.flags = [*item.flags, "no_active_catalog"]
        return
    outcome = index.match(sku_hint=item.sku_hint, description=item.description, raw_text=item.raw_text)
    item.match_status = outcome.status
    for rank, cand in enumerate(outcome.candidates, start=1):
        db.add(
            MatchCandidate(
                organization_id=item.organization_id,
                rfq_item_id=item.id,
                product_id=cand.product.id,
                rank=rank,
                category=cand.category,
                score=cand.score,
                reason=cand.reason[:200],
                matched_on=cand.matched_on,
            )
        )
    if outcome.status == "exact":
        product = outcome.candidates[0].product
        item.selected_product_id = product.id
        item.unit_mismatch = bool(item.unit_normalized and product.unit and item.unit_normalized != product.unit)
    db.flush()


def get_rfq(db: Session, organization_id: uuid.UUID, rfq_id: uuid.UUID) -> RFQ:
    rfq = db.scalar(select(RFQ).where(RFQ.id == rfq_id, RFQ.organization_id == organization_id))
    if rfq is None:
        raise NotFoundError("RFQ not found")
    return rfq


def get_item(db: Session, rfq: RFQ, item_id: uuid.UUID) -> RFQItem:
    item = db.scalar(
        select(RFQItem).where(
            RFQItem.id == item_id,
            RFQItem.rfq_id == rfq.id,
            RFQItem.organization_id == rfq.organization_id,
        )
    )
    if item is None:
        raise NotFoundError("RFQ item not found")
    return item


def update_item(
    db: Session,
    rfq: RFQ,
    item: RFQItem,
    *,
    actor_user_id: uuid.UUID,
    ip: str | None,
    selected_product_id: uuid.UUID | None | object,
    quantity: Decimal | None | object,
    unit: str | None | object,
    description: str | None | object,
    unit_confirmed: bool | None,
    duplicate_confirmed: bool | None,
    is_custom: bool | None,
    notes: str | None | object,
) -> RFQItem:
    if rfq.status == "quoted":
        raise StateError("RFQ already has a quote; items are locked.")
    unset = object
    changes: dict[str, object] = {}
    if selected_product_id is not unset:
        if selected_product_id is None:
            item.selected_product_id = None
            item.match_status = "unmatched" if not item.is_custom else "custom"
        else:
            product = db.scalar(
                select(Product).where(
                    Product.id == selected_product_id,
                    Product.organization_id == rfq.organization_id,
                )
            )
            if product is None:
                raise NotFoundError("Product not found")
            if rfq.catalog_version_id:
                if product.catalog_version_id != rfq.catalog_version_id:
                    raise ValidationFailed(
                        "Product belongs to a different catalog version than this RFQ was matched against."
                    )
            else:
                # NEW-13: an RFQ parsed with no active catalog has no frozen version; only the *active* version
                # may be selected (never an imported-but-inactive or archived list)
                from app.services.catalog_import import active_version_for_org

                active = active_version_for_org(db, rfq.organization_id)
                if active is None or product.catalog_version_id != active.id:
                    raise ValidationFailed("Product is not in the active catalog version.")
            item.selected_product_id = product.id
            item.match_status = "resolved"
            item.is_custom = False
            item.unit_mismatch = bool(item.unit_normalized and product.unit and item.unit_normalized != product.unit)
            if not item.unit_mismatch:
                item.unit_confirmed = False
        changes["selected_product_id"] = str(selected_product_id) if selected_product_id else None
    if quantity is not unset:
        if quantity is None or not isinstance(quantity, Decimal) or quantity <= 0:
            raise ValidationFailed("Quantity must be greater than zero.", field="quantity")
        item.quantity = quantity
        item.flags = [f for f in item.flags if not f.startswith("quantity_")]
        changes["quantity"] = str(quantity)
    if unit is not unset:
        u = (unit or "").strip() if isinstance(unit, str) else ""
        item.unit_raw = u or None
        item.unit_normalized = normalize_unit(u) if u else None
        item.flags = [f for f in item.flags if f != "unit_unknown"] + (
            ["unit_unknown"] if u and item.unit_normalized is None else []
        )
        if item.selected_product_id:
            product = db.get(Product, item.selected_product_id)
            item.unit_mismatch = bool(product and item.unit_normalized and product.unit != item.unit_normalized)
        changes["unit"] = u
    if description is not unset and isinstance(description, str):
        item.description = description.strip()[:1000]
        changes["description"] = True
    if unit_confirmed is not None:
        item.unit_confirmed = unit_confirmed
        changes["unit_confirmed"] = unit_confirmed
    if duplicate_confirmed is not None:
        item.duplicate_confirmed = duplicate_confirmed
        changes["duplicate_confirmed"] = duplicate_confirmed
    if is_custom is not None:
        item.is_custom = is_custom
        if is_custom:
            item.selected_product_id = None
            item.match_status = "custom"
        elif item.match_status == "custom":
            item.match_status = "unmatched"
        changes["is_custom"] = is_custom
    if notes is not unset and (notes is None or isinstance(notes, str)):
        item.notes = (notes or "")[:2000] or None
    db.flush()
    audit.record(
        db,
        organization_id=rfq.organization_id,
        actor_user_id=actor_user_id,
        action="rfq.item_update",
        entity_type="rfq_item",
        entity_id=item.id,
        ip=ip,
        summary=changes,
    )
    return item


def add_item(
    db: Session,
    rfq: RFQ,
    *,
    actor_user_id: uuid.UUID,
    ip: str | None,
    description: str,
    quantity: Decimal,
    unit: str | None,
    sku_hint: str | None,
) -> RFQItem:
    if rfq.status == "quoted":
        raise StateError("RFQ already has a quote; items are locked.")
    if quantity <= 0:
        raise ValidationFailed("Quantity must be greater than zero.", field="quantity")
    next_no = max((i.line_no for i in rfq.items), default=0) + 1
    item = RFQItem(
        organization_id=rfq.organization_id,
        rfq_id=rfq.id,
        line_no=next_no,
        raw_text=f"{quantity} {unit or ''} {sku_hint or ''} {description}".strip(),
        source_ref={"source": "manual"},
        sku_hint=(sku_hint or "").strip()[:100] or None,
        description=description.strip()[:1000],
        quantity=quantity,
        unit_raw=(unit or "").strip()[:32] or None,
        unit_normalized=normalize_unit(unit),
        flags=["manual"],
    )
    db.add(item)
    db.flush()
    version = db.get(CatalogVersion, rfq.catalog_version_id) if rfq.catalog_version_id else None
    index = (
        CatalogIndex(list(db.scalars(select(Product).where(Product.catalog_version_id == version.id))))
        if version
        else None
    )
    apply_matching(db, item, index)
    if rfq.status == "failed":
        rfq.status = "parsed"
    audit.record(
        db,
        organization_id=rfq.organization_id,
        actor_user_id=actor_user_id,
        action="rfq.item_create",
        entity_type="rfq_item",
        entity_id=item.id,
        ip=ip,
        summary={"line_no": next_no},
    )
    return item


def delete_item(db: Session, rfq: RFQ, item: RFQItem, *, actor_user_id: uuid.UUID, ip: str | None) -> None:
    if rfq.status == "quoted":
        raise StateError("RFQ already has a quote; items are locked.")
    db.delete(item)
    db.flush()
    audit.record(
        db,
        organization_id=rfq.organization_id,
        actor_user_id=actor_user_id,
        action="rfq.item_delete",
        entity_type="rfq_item",
        entity_id=item.id,
        ip=ip,
        summary={"line_no": item.line_no},
    )
