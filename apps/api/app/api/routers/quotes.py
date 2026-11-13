from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import APIRouter, Query
from fastapi.responses import Response
from sqlalchemy import ColumnElement, or_, select

from app.api.deps import DB, Principal, PrincipalDep, clean_param
from app.models.audit import AuditEvent
from app.models.customer import Customer
from app.models.quote import Quote
from app.schemas.common import (
    AuditOut,
    HistoryOut,
    LockedAction,
    QuoteCreate,
    QuoteLineOut,
    QuoteLinePrivileged,
    QuoteOut,
    QuoteSummary,
    QuoteUpdate,
    QuoteVersionOut,
)
from app.services import quotes as quote_service
from app.services.authz import Permission, has_permission
from app.services.exports import quote_exports
from app.services.pricing import LineInput, compute_line

router = APIRouter(prefix="/quotes", tags=["quotes"])


def _version_out(principal: Principal, quote: Quote, version: object) -> QuoteVersionOut:
    out = QuoteVersionOut.model_validate(version)
    if has_permission(principal.role, Permission.VIEW_COSTS):
        lines = []
        for ln in version.lines:  # type: ignore[attr-defined]
            priv = QuoteLinePrivileged.model_validate(ln)
            r = compute_line(
                LineInput(ln.quantity, ln.unit_price, ln.discount_pct, ln.tax_rate_pct, ln.cost_price),
                quote.currency,
                principal.organization.rounding_mode,
            )
            priv.margin_pct = r.margin_pct
            lines.append(priv)
        out.lines = lines  # type: ignore[assignment]
    else:
        out.lines = [QuoteLineOut.model_validate(ln) for ln in version.lines]  # type: ignore[attr-defined]
        out.pricing_snapshot = {k: v for k, v in out.pricing_snapshot.items() if k != "min_margin_pct"}  # R-2
    return out


def quote_out(db: DB, principal: Principal, quote: Quote) -> QuoteOut:
    db.refresh(quote)
    current = quote_service.current_version(quote)
    data = QuoteOut.model_validate(
        {
            **{
                c: getattr(quote, c)
                for c in (
                    "id",
                    "number",
                    "customer_id",
                    "rfq_id",
                    "currency",
                    "status",
                    "current_version_no",
                    "lock_version",
                    "valid_until",
                    "withdrawn_reason",
                    "created_by",
                    "created_at",
                    "updated_at",
                )
            },
            "effective_status": quote_service.effective_status(quote),
            "current_version": _version_out(principal, quote, current),
            "blockers": [
                b.as_dict()
                for b in quote_service.compute_blockers(
                    quote,
                    current,
                    principal.organization,
                    reveal_margin=has_permission(principal.role, Permission.VIEW_COSTS),
                    db=db,
                )
            ],
            "versions": [_version_out(principal, quote, v) for v in quote.versions],
        }
    )
    return data


@router.get("", response_model=list[QuoteSummary])
def list_quotes(
    principal: PrincipalDep,
    db: DB,
    q: str | None = Query(default=None, max_length=100),
    status: str | None = Query(default=None, max_length=20),
    customer_id: uuid.UUID | None = None,
) -> list[QuoteSummary]:
    stmt = (
        select(Quote, Customer.name, Customer.deleted_at)
        .outerjoin(Customer, Customer.id == Quote.customer_id)
        .where(Quote.organization_id == principal.org_id)
    )
    see_erased = has_permission(principal.role, Permission.MANAGE_RETENTION)
    status = clean_param(status)
    if status:
        stmt = stmt.where(Quote.status == status)
    if customer_id:
        stmt = stmt.where(Quote.customer_id == customer_id)
    q = clean_param(q)
    if q:
        like = f"%{q.replace('%', r'\%').replace('_', r'\_')}%"
        name_match: ColumnElement[bool] = Customer.name.ilike(like, escape="\\")
        if not see_erased:  # SEC3-13: do not let a name search reveal a customer pending erasure
            name_match = name_match & Customer.deleted_at.is_(None)
        stmt = stmt.where(or_(Quote.number.ilike(like, escape="\\"), name_match))
    rows = db.execute(stmt.order_by(Quote.created_at.desc()).limit(200)).all()
    out: list[QuoteSummary] = []
    for quote, customer_name, customer_deleted_at in rows:
        if customer_deleted_at is not None and not see_erased:
            customer_name = None  # SEC3-13: name of a customer pending erasure is withheld
        current = quote_service.current_version(quote)
        out.append(
            QuoteSummary(
                id=quote.id,
                number=quote.number,
                customer_id=quote.customer_id,
                customer_name=customer_name,
                currency=quote.currency,
                status=quote.status,
                effective_status=quote_service.effective_status(quote),
                current_version_no=quote.current_version_no,
                grand_total=current.grand_total,
                valid_until=quote.valid_until,
                created_at=quote.created_at,
                updated_at=quote.updated_at,
            )
        )
    return out


@router.post("", response_model=QuoteOut, status_code=201)
def create_quote(payload: QuoteCreate, principal: PrincipalDep, db: DB) -> QuoteOut:
    principal.require(Permission.EDIT_QUOTES)
    quote = quote_service.create_quote(
        db,
        org=principal.organization,
        actor_user_id=principal.user.id,
        ip=principal.ip,
        customer_id=payload.customer_id,
        currency=payload.currency,
    )
    return quote_out(db, principal, quote)


@router.get("/{quote_id}", response_model=QuoteOut)
def get_quote(quote_id: uuid.UUID, principal: PrincipalDep, db: DB) -> QuoteOut:
    return quote_out(db, principal, quote_service.get_quote(db, principal.org_id, quote_id))


@router.patch("/{quote_id}", response_model=QuoteOut)
def update_quote(quote_id: uuid.UUID, payload: QuoteUpdate, principal: PrincipalDep, db: DB) -> QuoteOut:
    principal.require(Permission.EDIT_QUOTES)
    quote = quote_service.get_quote(db, principal.org_id, quote_id)
    fields = payload.model_fields_set
    unset = object
    lines = None
    if payload.lines is not None:
        lines = [ln.model_dump(exclude_unset=True) for ln in payload.lines]
        for ln in lines:
            for k in ("quantity", "unit_price", "discount_pct", "tax_rate_pct"):
                if k in ln and ln[k] is not None:
                    ln[k] = Decimal(str(ln[k]))
    quote = quote_service.update_draft(
        db,
        org=principal.organization,
        actor_user_id=principal.user.id,
        role=principal.role,
        ip=principal.ip,
        quote=quote,
        expected_lock_version=payload.expected_lock_version,
        valid_until=payload.valid_until if "valid_until" in fields else unset,
        notes=payload.notes if "notes" in fields else unset,
        terms=payload.terms if "terms" in fields else unset,
        lines=lines,
    )
    return quote_out(db, principal, quote)


@router.post("/{quote_id}/submit", response_model=QuoteOut)
def submit(quote_id: uuid.UUID, payload: LockedAction, principal: PrincipalDep, db: DB) -> QuoteOut:
    principal.require(Permission.SUBMIT_QUOTE)
    quote = quote_service.get_quote(db, principal.org_id, quote_id)
    quote = quote_service.submit(
        db,
        org=principal.organization,
        actor_user_id=principal.user.id,
        ip=principal.ip,
        quote=quote,
        expected_lock_version=payload.expected_lock_version,
        reveal_margin=has_permission(principal.role, Permission.VIEW_COSTS),
    )
    return quote_out(db, principal, quote)


@router.post("/{quote_id}/approve", response_model=QuoteOut)
def approve(quote_id: uuid.UUID, payload: LockedAction, principal: PrincipalDep, db: DB) -> QuoteOut:
    quote = quote_service.get_quote(db, principal.org_id, quote_id)
    quote = quote_service.approve(
        db,
        org=principal.organization,
        actor_user_id=principal.user.id,
        role=principal.role,
        ip=principal.ip,
        quote=quote,
        expected_lock_version=payload.expected_lock_version,
        reason=payload.reason,
    )
    return quote_out(db, principal, quote)


@router.post("/{quote_id}/reject", response_model=QuoteOut)
def reject(quote_id: uuid.UUID, payload: LockedAction, principal: PrincipalDep, db: DB) -> QuoteOut:
    quote = quote_service.get_quote(db, principal.org_id, quote_id)
    quote = quote_service.reject(
        db,
        org=principal.organization,
        actor_user_id=principal.user.id,
        role=principal.role,
        ip=principal.ip,
        quote=quote,
        expected_lock_version=payload.expected_lock_version,
        reason=payload.reason or "",
    )
    return quote_out(db, principal, quote)


@router.post("/{quote_id}/withdraw", response_model=QuoteOut)
def withdraw(quote_id: uuid.UUID, payload: LockedAction, principal: PrincipalDep, db: DB) -> QuoteOut:
    quote = quote_service.get_quote(db, principal.org_id, quote_id)
    quote = quote_service.withdraw(
        db,
        org=principal.organization,
        actor_user_id=principal.user.id,
        role=principal.role,
        ip=principal.ip,
        quote=quote,
        expected_lock_version=payload.expected_lock_version,
        reason=payload.reason or "",
    )
    return quote_out(db, principal, quote)


@router.post("/{quote_id}/versions", response_model=QuoteOut, status_code=201)
def new_version(quote_id: uuid.UUID, payload: LockedAction, principal: PrincipalDep, db: DB) -> QuoteOut:
    principal.require(Permission.EDIT_QUOTES)
    quote = quote_service.get_quote(db, principal.org_id, quote_id)
    quote = quote_service.new_version(
        db,
        org=principal.organization,
        actor_user_id=principal.user.id,
        ip=principal.ip,
        quote=quote,
        expected_lock_version=payload.expected_lock_version,
    )
    return quote_out(db, principal, quote)


@router.post("/{quote_id}/lines/{line_id}/price-override", response_model=QuoteOut)
@router.post("/{quote_id}/lines/{line_id}/margin-override", response_model=QuoteOut, include_in_schema=False)
def margin_override(
    quote_id: uuid.UUID, line_id: uuid.UUID, payload: LockedAction, principal: PrincipalDep, db: DB
) -> QuoteOut:
    quote = quote_service.get_quote(db, principal.org_id, quote_id)
    quote = quote_service.margin_override(
        db,
        org=principal.organization,
        actor_user_id=principal.user.id,
        role=principal.role,
        ip=principal.ip,
        quote=quote,
        line_id=line_id,
        expected_lock_version=payload.expected_lock_version,
        reason=payload.reason or "",
    )
    return quote_out(db, principal, quote)


@router.get("/{quote_id}/history", response_model=HistoryOut)
def history(quote_id: uuid.UUID, principal: PrincipalDep, db: DB) -> HistoryOut:
    quote = quote_service.get_quote(db, principal.org_id, quote_id)
    version_ids = [v.id for v in quote.versions]
    line_ids = [ln.id for v in quote.versions for ln in v.lines]
    events = db.scalars(
        select(AuditEvent)
        .where(
            AuditEvent.organization_id == principal.org_id,
            AuditEvent.entity_id.in_([quote.id, *version_ids, *line_ids]),
        )
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(500)
    ).all()
    return HistoryOut(
        versions=[_version_out(principal, quote, v) for v in quote.versions],
        audit=[AuditOut.model_validate(e) for e in events],
    )


@router.post("/{quote_id}/versions/{version_no}/pdf")
def render_pdf(quote_id: uuid.UUID, version_no: int, principal: PrincipalDep, db: DB) -> dict[str, object]:
    principal.require(Permission.EXPORT)
    quote = quote_service.get_quote(db, principal.org_id, quote_id)
    version = quote_service.get_version(quote, version_no)
    data, sha = quote_exports.render_and_store_pdf(
        db,
        org=principal.organization,
        actor_user_id=principal.user.id,
        ip=principal.ip,
        quote=quote,
        version=version,
    )
    return {"sha256": sha, "size": len(data), "status": version.status}


@router.get("/{quote_id}/versions/{version_no}/pdf")
def download_pdf(quote_id: uuid.UUID, version_no: int, principal: PrincipalDep, db: DB) -> Response:
    principal.require(Permission.EXPORT)
    quote = quote_service.get_quote(db, principal.org_id, quote_id)
    version = quote_service.get_version(quote, version_no)
    data, sha = quote_exports.render_and_store_pdf(
        db,
        org=principal.organization,
        actor_user_id=principal.user.id,
        ip=principal.ip,
        quote=quote,
        version=version,
    )
    filename = f"{quote.number}-v{version.version_no}.pdf"
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-SHA256": sha,
            "Cache-Control": "no-store",
        },
    )


@router.get("/{quote_id}/versions/{version_no}/csv")
def download_csv(quote_id: uuid.UUID, version_no: int, principal: PrincipalDep, db: DB) -> Response:
    principal.require(Permission.EXPORT)
    quote = quote_service.get_quote(db, principal.org_id, quote_id)
    version = quote_service.get_version(quote, version_no)
    data = quote_exports.render_csv(
        db,
        org=principal.organization,
        actor_user_id=principal.user.id,
        ip=principal.ip,
        quote=quote,
        version=version,
        include_costs=has_permission(principal.role, Permission.VIEW_COSTS),
    )
    filename = f"{quote.number}-v{version.version_no}.csv"
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )
