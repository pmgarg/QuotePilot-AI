from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, SerializeAsAny, field_validator


def _dec(v: Decimal) -> str:
    """Decimal → plain string without exponent or trailing zeros ("425.000" → "425", "0.850000" → "0.85")."""
    return format(v.normalize(), "f")


Money = Annotated[Decimal, PlainSerializer(_dec, return_type=str, when_used="json")]
OptMoney = Annotated[
    Decimal | None, PlainSerializer(lambda v: None if v is None else _dec(v), return_type=str | None, when_used="json")
]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ErrorResponse(BaseModel):
    error: str
    detail: Any = None
    request_id: str | None = None


class UserOut(ORMModel):
    id: uuid.UUID
    email: str
    full_name: str
    is_active: bool


class OrganizationPublic(ORMModel):
    id: uuid.UUID
    name: str
    legal_name: str | None
    address: str | None
    default_currency: str
    default_tax_rate: Money
    rounding_mode: str
    default_validity_days: int
    brand_color: str
    pdf_footer: str | None
    allow_self_approval: bool


class OrganizationPrivileged(OrganizationPublic):
    min_margin_pct: Money
    ai_processing_consented_at: datetime | None = None
    updated_at: datetime


class OrganizationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    legal_name: str | None = Field(default=None, max_length=200)
    address: str | None = Field(default=None, max_length=1000)
    default_currency: str | None = Field(default=None, min_length=3, max_length=3)
    default_tax_rate: Decimal | None = Field(default=None, ge=0, le=100)
    rounding_mode: str | None = Field(default=None, pattern="^(HALF_UP|HALF_EVEN)$")
    min_margin_pct: Decimal | None = Field(default=None, ge=0, le=100)
    default_validity_days: int | None = Field(default=None, ge=1, le=365)
    allow_self_approval: bool | None = None
    brand_color: str | None = Field(default=None, pattern="^#[0-9a-fA-F]{6}$")
    pdf_footer: str | None = Field(default=None, max_length=500)
    ai_processing_consent: bool | None = None  # owner-only; records consent for hosted AI processing (X-05)


class MemberOut(ORMModel):
    id: uuid.UUID
    role: str
    is_active: bool
    user: UserOut


class MemberCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    full_name: str = Field(min_length=1, max_length=200)
    role: str = Field(pattern="^(owner|admin|sales|approver|readonly)$")
    password: str = Field(min_length=12, max_length=256)


class MemberUpdate(BaseModel):
    role: str | None = Field(default=None, pattern="^(owner|admin|sales|approver|readonly)$")
    is_active: bool | None = None


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)


class SessionInfo(BaseModel):
    user: UserOut
    organization: OrganizationPublic
    role: str
    csrf_token: str
    permissions: list[str]


class CustomerIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    reference: str | None = Field(default=None, max_length=100)
    email: str | None = Field(default=None, max_length=320)
    default_currency: str | None = Field(default=None, min_length=3, max_length=3)
    notes: str | None = Field(default=None, max_length=2000)


class CustomerOut(ORMModel):
    id: uuid.UUID
    name: str
    reference: str | None
    email: str | None
    default_currency: str | None
    notes: str | None
    created_at: datetime
    deleted_at: datetime | None = None


class CatalogVersionOut(ORMModel):
    id: uuid.UUID
    catalog_id: uuid.UUID
    version_no: int
    status: str
    row_count: int
    import_report: dict[str, Any]
    column_mapping: dict[str, Any]
    created_at: datetime


class CatalogOut(ORMModel):
    id: uuid.UUID
    name: str
    active_version_id: uuid.UUID | None
    versions: list[CatalogVersionOut]


class ProductOut(ORMModel):
    id: uuid.UUID
    sku: str
    description: str
    unit: str
    currency: str
    list_price: Money
    stock_qty: OptMoney
    valid_from: date | None
    valid_to: date | None
    flags: list[str]


class ProductPrivileged(ProductOut):
    cost_price: OptMoney


class Page(BaseModel):
    items: list[Any]
    total: int
    page: int
    page_size: int


class CandidateOut(BaseModel):
    product_id: uuid.UUID
    sku: str
    description: str
    unit: str
    currency: str
    list_price: Money
    category: str
    score: Money
    reason: str
    matched_on: str
    rank: int


class RFQItemOut(ORMModel):
    id: uuid.UUID
    line_no: int
    raw_text: str
    source_ref: dict[str, Any]
    sku_hint: str | None
    description: str
    quantity: OptMoney
    unit_raw: str | None
    unit_normalized: str | None
    match_status: str
    selected_product_id: uuid.UUID | None
    unit_mismatch: bool
    unit_confirmed: bool
    is_duplicate: bool
    duplicate_confirmed: bool
    is_custom: bool
    flags: list[str]
    notes: str | None
    candidates: list[CandidateOut] = []


class RFQOut(ORMModel):
    id: uuid.UUID
    customer_id: uuid.UUID | None
    reference: str | None
    source_kind: str
    source_file_id: uuid.UUID | None
    status: str
    failure_reason: str | None
    currency: str
    catalog_version_id: uuid.UUID | None
    parse_meta: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    items: list[RFQItemOut] = []


class RFQSummary(ORMModel):
    id: uuid.UUID
    customer_id: uuid.UUID | None
    reference: str | None
    source_kind: str
    status: str
    currency: str
    created_at: datetime


class RFQTextIn(BaseModel):
    text: str = Field(min_length=1, max_length=2_000_000)  # service enforces the configurable 100k limit → 413
    customer_id: uuid.UUID | None = None
    reference: str | None = Field(default=None, max_length=100)
    currency: str | None = Field(default=None, min_length=3, max_length=3)


class RFQItemUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selected_product_id: uuid.UUID | None = None
    quantity: Decimal | None = Field(default=None, le=Decimal("10000000"), decimal_places=3)
    unit: str | None = Field(default=None, max_length=32)
    description: str | None = Field(default=None, max_length=1000)
    unit_confirmed: bool | None = None
    duplicate_confirmed: bool | None = None
    is_custom: bool | None = None
    notes: str | None = Field(default=None, max_length=2000)


class RFQItemCreate(BaseModel):
    description: str = Field(min_length=1, max_length=1000)
    quantity: Decimal = Field(gt=0, le=Decimal("10000000"), decimal_places=3)
    unit: str | None = Field(default=None, max_length=32)
    sku_hint: str | None = Field(default=None, max_length=100)


class QuoteLineIn(BaseModel):
    id: uuid.UUID | None = None
    product_id: uuid.UUID | None = None
    sku: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    quantity: Decimal | None = Field(default=None, gt=0, le=Decimal("10000000"), decimal_places=3)
    unit: str | None = Field(default=None, max_length=16)
    unit_price: Decimal | None = Field(default=None, ge=0, le=Decimal("10000000"), decimal_places=6)
    discount_pct: Decimal | None = Field(default=None, ge=0, le=100)
    tax_rate_pct: Decimal | None = Field(default=None, ge=0, le=100)
    is_custom: bool | None = None


class QuoteCreate(BaseModel):
    customer_id: uuid.UUID | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)


class QuoteUpdate(BaseModel):
    expected_lock_version: int = Field(ge=0)
    valid_until: date | None = None
    notes: str | None = Field(default=None, max_length=4000)
    terms: str | None = Field(default=None, max_length=4000)
    lines: list[QuoteLineIn] | None = Field(default=None, max_length=500)


class LockedAction(BaseModel):
    expected_lock_version: int = Field(ge=0)
    reason: str | None = Field(default=None, max_length=2000)


class QuoteLineOut(ORMModel):
    id: uuid.UUID
    line_no: int
    product_id: uuid.UUID | None
    rfq_item_id: uuid.UUID | None
    sku: str | None
    description: str
    quantity: Money
    unit: str
    unit_price: OptMoney
    price_source: str
    discount_pct: Money
    tax_rate_pct: Money
    line_subtotal: Money
    discount_amount: Money
    tax_amount: Money
    line_total: Money
    is_custom: bool
    unit_mismatch_confirmed: bool
    margin_override_reason: str | None


class QuoteLinePrivileged(QuoteLineOut):
    cost_price: OptMoney
    margin_pct: OptMoney = None


class DecisionOut(ORMModel):
    id: uuid.UUID
    decision: str
    reason: str | None
    decided_by: uuid.UUID | None
    decided_at: datetime


class QuoteVersionOut(ORMModel):
    id: uuid.UUID
    version_no: int
    status: str
    notes: str | None
    terms: str | None
    subtotal: Money
    discount_total: Money
    tax_total: Money
    grand_total: Money
    pricing_snapshot: dict[str, Any]
    submitted_by: uuid.UUID | None
    submitted_at: datetime | None
    approved_by: uuid.UUID | None
    approved_at: datetime | None
    pdf_sha256: str | None
    created_at: datetime
    lines: list[SerializeAsAny[QuoteLineOut]] = []
    decisions: list[DecisionOut] = []


class QuoteOut(ORMModel):
    id: uuid.UUID
    number: str
    customer_id: uuid.UUID | None
    rfq_id: uuid.UUID | None
    currency: str
    status: str
    effective_status: str
    current_version_no: int
    lock_version: int
    valid_until: date | None
    withdrawn_reason: str | None
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    current_version: QuoteVersionOut
    blockers: list[dict[str, Any]] = []
    versions: list[QuoteVersionOut] = []


class QuoteSummary(ORMModel):
    id: uuid.UUID
    number: str
    customer_id: uuid.UUID | None
    customer_name: str | None = None
    currency: str
    status: str
    effective_status: str
    current_version_no: int
    grand_total: Money
    valid_until: date | None
    created_at: datetime
    updated_at: datetime


class AuditOut(ORMModel):
    id: int
    actor_user_id: uuid.UUID | None
    action: str
    entity_type: str | None
    entity_id: uuid.UUID | None
    request_id: str | None
    summary: dict[str, Any]
    created_at: datetime


class HistoryOut(BaseModel):
    versions: list[QuoteVersionOut]
    audit: list[AuditOut]


# ---------------------------------------------------------------- Stage 2 — follow-up assistant
class ContactIn(BaseModel):
    full_name: str = Field(min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=50)
    preferred_channel: str = Field(default="email", pattern="^(email|phone|none)$")
    language: str = Field(default="en", min_length=2, max_length=8)
    timezone: str = Field(default="UTC", max_length=64)
    is_primary: bool = False
    notes: str | None = Field(default=None, max_length=2000)


class ContactOut(ORMModel):
    id: uuid.UUID
    customer_id: uuid.UUID
    full_name: str
    email: str | None
    phone: str | None
    preferred_channel: str
    language: str
    timezone: str
    is_primary: bool
    opted_out_at: datetime | None
    opt_out_source: str | None
    notes: str | None


class OptOutIn(BaseModel):
    opted_out: bool
    source: str = Field(default="manual", max_length=64)


class PolicyOut(ORMModel):
    offsets_days: list[int]
    max_attempts: int
    quiet_hours_start: int
    quiet_hours_end: int
    timezone: str
    skip_weekends: bool
    automation_enabled: bool
    automation_consented_at: datetime | None
    hourly_send_limit: int
    sender_name: str | None
    reply_to_email: str | None
    dev_endpoints_enabled: bool = False


class PolicyUpdate(BaseModel):
    offsets_days: list[int] | None = Field(default=None, max_length=10)
    max_attempts: int | None = Field(default=None, ge=0, le=10)
    quiet_hours_start: int | None = Field(default=None, ge=0, le=23)
    quiet_hours_end: int | None = Field(default=None, ge=0, le=23)
    timezone: str | None = Field(default=None, max_length=64)
    skip_weekends: bool | None = None
    automation_enabled: bool | None = None  # admin/owner only; records consent
    hourly_send_limit: int | None = Field(default=None, ge=1, le=1000)
    sender_name: str | None = Field(default=None, max_length=200)
    reply_to_email: str | None = Field(default=None, max_length=320)

    @field_validator("sender_name")
    @classmethod
    def _no_control_chars(cls, v: str | None) -> str | None:
        if v is not None and any(ord(ch) < 32 or ord(ch) == 127 for ch in v):
            raise ValueError("sender_name must not contain control characters (CR/LF)")
        return v


class TemplateOut(ORMModel):
    id: uuid.UUID
    key: str
    name: str
    language: str
    subject: str
    body: str
    is_active: bool


class TemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    subject: str | None = Field(default=None, min_length=1, max_length=300)
    body: str | None = Field(default=None, min_length=1, max_length=20000)
    is_active: bool | None = None


class TransitionIn(BaseModel):
    expected_lock_version: int = Field(ge=0)
    to_status: str = Field(pattern="^(sent|acknowledged|awaiting_customer|won|lost)$")
    evidence: str | None = Field(default=None, max_length=2000)
    occurred_at: datetime | None = None


class StatusEventOut(ORMModel):
    id: uuid.UUID
    from_status: str
    to_status: str
    source: str
    evidence: str | None
    actor_user_id: uuid.UUID | None
    occurred_at: datetime


class DraftCreate(BaseModel):
    contact_id: uuid.UUID | None = None
    template_key: str | None = Field(default=None, max_length=64)


class DraftUpdate(BaseModel):
    subject: str | None = Field(default=None, min_length=1, max_length=300)
    body: str | None = Field(default=None, min_length=1, max_length=20000)
    to_email: str | None = Field(default=None, max_length=320)


class DraftOut(ORMModel):
    id: uuid.UUID
    quote_id: uuid.UUID
    contact_id: uuid.UUID | None
    schedule_id: uuid.UUID | None
    channel: str
    to_email: str | None
    subject: str
    body: str
    template_key: str | None
    status: str
    generated_reasons: list[str]
    provider: str | None
    provider_message_id: str | None
    sent_by: uuid.UUID | None
    sent_at: datetime | None
    last_error: str | None
    created_at: datetime


class ScheduleOut(ORMModel):
    id: uuid.UUID
    quote_id: uuid.UUID
    contact_id: uuid.UUID | None
    attempt_no: int
    due_at: datetime
    status: str
    mode: str
    reason: str | None
    draft_id: uuid.UUID | None
    last_error: str | None


class RescheduleIn(BaseModel):
    due_at: datetime


class CancelIn(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class CommEventOut(ORMModel):
    id: uuid.UUID
    quote_id: uuid.UUID | None
    contact_id: uuid.UUID | None
    kind: str
    direction: str
    provider: str | None
    subject: str | None
    snippet: str | None
    occurred_at: datetime


class JobOut(ORMModel):
    id: uuid.UUID
    kind: str
    status: str
    attempts: int
    max_attempts: int
    next_run_at: datetime
    last_error: str | None
    finished_at: datetime | None
    created_at: datetime


class SimulatedInboundIn(BaseModel):
    from_email: str = Field(max_length=320)
    subject: str = Field(max_length=300)
    snippet: str = Field(max_length=500)
    in_reply_to: str | None = Field(default=None, max_length=300)


class IntegrationOut(ORMModel):
    id: uuid.UUID
    provider: str
    status: str
    scopes: list[str]
    account_email: str | None
    last_error: str | None
    last_sync_at: datetime | None
    created_at: datetime


class IntegrationCreate(BaseModel):
    provider: str = Field(pattern="^(simulator|smtp|gmail|msgraph)$")
    account_email: str | None = Field(default=None, max_length=320)
    secret: str | None = Field(default=None, max_length=4000)  # encrypted at rest; never returned


# ---------------------------------------------------------------- Stage 3
class OrderLineOut(ORMModel):
    id: uuid.UUID
    line_no: int
    product_id: uuid.UUID | None
    sku: str | None
    description: str
    quantity: Money
    unit: str
    unit_price: Money
    tax_rate_pct: Money
    line_total: Money


class OrderLinePrivileged(OrderLineOut):
    cost_price: OptMoney


class OrderEventOut(ORMModel):
    id: uuid.UUID
    kind: str
    detail: str | None
    actor_user_id: uuid.UUID | None
    source: str
    occurred_at: datetime


class OrderOut(ORMModel):
    id: uuid.UUID
    number: str
    customer_id: uuid.UUID | None
    quote_id: uuid.UUID | None
    quote_version_no: int | None
    currency: str
    status: str
    source: str
    external_ref: str | None
    subtotal: Money
    tax_total: Money
    grand_total: Money
    confirmed_at: datetime | None
    confirmed_by: uuid.UUID | None
    created_by: uuid.UUID | None
    notes: str | None
    created_at: datetime
    lines: SerializeAsAny[list[OrderLineOut]] = []
    events: list[OrderEventOut] = []


class OrderSummary(ORMModel):
    id: uuid.UUID
    number: str
    customer_id: uuid.UUID | None
    quote_id: uuid.UUID | None
    currency: str
    status: str
    source: str
    grand_total: Money
    created_at: datetime
    confirmed_at: datetime | None


class OrderFromQuoteIn(BaseModel):
    version_no: int | None = Field(default=None, ge=1)
    notes: str | None = Field(default=None, max_length=2000)


class OrderTransitionIn(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


class ProposalOut(ORMModel):
    id: uuid.UUID
    customer_id: uuid.UUID
    kind: str
    status: str
    lines: list[dict[str, Any]]
    evidence: list[str]
    currency: str
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    decision_reason: str | None
    order_id: uuid.UUID | None
    created_at: datetime


class DecisionIn(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


class FxRateIn(BaseModel):
    base_currency: str = Field(min_length=3, max_length=3)
    quote_currency: str = Field(min_length=3, max_length=3)
    rate: Decimal = Field(ge=Decimal("0.00000001"), le=Decimal("1000000"), decimal_places=8)
    as_of: date
    source: str = Field(default="manual", max_length=64)


class FxRateOut(ORMModel):
    id: uuid.UUID
    base_currency: str
    quote_currency: str
    rate: Money
    as_of: date
    source: str
    created_at: datetime


class ConnectorIn(BaseModel):
    kind: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=200)
    scopes: list[str] = Field(min_length=1, max_length=8)


class ConnectorOut(ORMModel):
    id: uuid.UUID
    kind: str
    name: str
    scopes: list[str]
    status: str
    last_sync_at: datetime | None
    last_error: str | None
    created_at: datetime


class SyncIn(BaseModel):
    entity: str = Field(min_length=1, max_length=32)
    idempotency_key: str = Field(min_length=1, max_length=160)
    records: list[dict[str, Any]] = Field(max_length=5000)


class SyncRunOut(ORMModel):
    id: uuid.UUID
    connection_id: uuid.UUID | None
    kind: str
    idempotency_key: str
    entity: str
    status: str
    received: int
    applied: int
    conflicts: int
    rejected: int
    report: dict[str, Any]
    created_at: datetime
    finished_at: datetime | None


class ConflictOut(ORMModel):
    id: uuid.UUID
    sync_run_id: uuid.UUID
    entity: str
    entity_key: str
    local_value: dict[str, Any]
    remote_value: dict[str, Any]
    fields: list[str]
    status: str
    resolved_by: uuid.UUID | None
    resolved_at: datetime | None
    resolution_note: str | None
    created_at: datetime


class ConflictResolveIn(BaseModel):
    resolution: str = Field(pattern="^(keep_local|apply_remote|dismiss)$")
    note: str | None = Field(default=None, max_length=2000)


class StockOut(BaseModel):
    product_id: uuid.UUID
    sku: str
    status: str
    quantity: str | None
    observed_at: datetime | None
    source: str | None
    age_hours: float | None


class DeletionRequestIn(BaseModel):
    customer_id: uuid.UUID
    reason: str = Field(min_length=3, max_length=2000)


class DeletionRequestOut(ORMModel):
    id: uuid.UUID
    subject_type: str
    subject_id: uuid.UUID
    reason: str
    status: str
    requested_by: uuid.UUID | None
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    purge_after: datetime | None
    executed_at: datetime | None
    summary: dict[str, Any]
    legal_hold: bool
    created_at: datetime


class LegalHoldIn(BaseModel):
    hold: bool


class CustomerProfileOut(BaseModel):
    customer: CustomerOut
    contacts: list[ContactOut]
    quotes: list[QuoteSummary]
    orders: list[OrderSummary]
    open_proposals: int
    stats: dict[str, Any]
