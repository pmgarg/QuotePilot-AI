export type Role = "owner" | "admin" | "sales" | "approver" | "readonly";

export interface User { id: string; email: string; full_name: string; is_active: boolean }
export interface Organization {
  id: string; name: string; legal_name: string | null; address: string | null; default_currency: string;
  default_tax_rate: string; rounding_mode: string; default_validity_days: number; brand_color: string;
  pdf_footer: string | null; allow_self_approval: boolean; min_margin_pct?: string; ai_processing_consented_at?: string | null;
}
export interface SessionInfo { user: User; organization: Organization; role: Role; csrf_token: string; permissions: string[] }
export interface Member { id: string; role: Role; is_active: boolean; user: User }
export interface Customer { id: string; name: string; reference: string | null; email: string | null; default_currency: string | null; notes: string | null; created_at: string }
export interface CatalogVersion {
  id: string; catalog_id: string; version_no: number; status: string; row_count: number; created_at: string;
  import_report: { total_rows: number; imported: number; skipped: number; errors: { row: number; message: string }[]; warnings: { row: number; message: string }[]; truncated: boolean };
  column_mapping: Record<string, number>;
}
export interface Catalog { id: string; name: string; active_version_id: string | null; versions: CatalogVersion[] }
export interface Product { id: string; sku: string; description: string; unit: string; currency: string; list_price: string; cost_price?: string | null; stock_qty: string | null; flags: string[] }
export interface Candidate { product_id: string; sku: string; description: string; unit: string; currency: string; list_price: string; category: string; score: string; reason: string; matched_on: string; rank: number }
export interface RFQItem {
  id: string; line_no: number; raw_text: string; source_ref: Record<string, unknown>; sku_hint: string | null; description: string;
  quantity: string | null; unit_raw: string | null; unit_normalized: string | null; match_status: string; selected_product_id: string | null;
  unit_mismatch: boolean; unit_confirmed: boolean; is_duplicate: boolean; duplicate_confirmed: boolean; is_custom: boolean; flags: string[]; notes: string | null;
  candidates: Candidate[];
}
export interface RFQ {
  id: string; customer_id: string | null; reference: string | null; source_kind: string; source_file_id: string | null; status: string;
  failure_reason: string | null; currency: string; catalog_version_id: string | null; parse_meta: Record<string, unknown>; created_at: string; updated_at: string; items: RFQItem[];
}
export interface RFQSummary { id: string; customer_id: string | null; reference: string | null; source_kind: string; status: string; currency: string; created_at: string }
export interface QuoteLine {
  id: string; line_no: number; product_id: string | null; rfq_item_id: string | null; sku: string | null; description: string; quantity: string; unit: string;
  unit_price: string | null; price_source: string; discount_pct: string; tax_rate_pct: string; line_subtotal: string; discount_amount: string; tax_amount: string; line_total: string;
  is_custom: boolean; unit_mismatch_confirmed: boolean; margin_override_reason: string | null; cost_price?: string | null; margin_pct?: string | null;
}
export interface Decision { id: string; decision: string; reason: string | null; decided_by: string | null; decided_at: string }
export interface QuoteVersion {
  id: string; version_no: number; status: string; notes: string | null; terms: string | null; subtotal: string; discount_total: string; tax_total: string; grand_total: string;
  pricing_snapshot: Record<string, unknown>; submitted_by: string | null; submitted_at: string | null; approved_by: string | null; approved_at: string | null; pdf_sha256: string | null; created_at: string;
  lines: QuoteLine[]; decisions: Decision[];
}
export interface Blocker { code: string; message: string; line_no: number | null; overridable_by: string[] }
export interface Quote {
  id: string; number: string; customer_id: string | null; rfq_id: string | null; currency: string; status: string; effective_status: string; current_version_no: number;
  lock_version: number; valid_until: string | null; withdrawn_reason: string | null; created_by: string | null; created_at: string; updated_at: string;
  current_version: QuoteVersion; blockers: Blocker[]; versions: QuoteVersion[];
}
export interface QuoteSummary { id: string; number: string; customer_id: string | null; customer_name: string | null; currency: string; status: string; effective_status: string; current_version_no: number; grand_total: string; valid_until: string | null; created_at: string; updated_at: string }
export interface AuditEvent { id: number; actor_user_id: string | null; action: string; entity_type: string | null; entity_id: string | null; request_id: string | null; summary: Record<string, unknown>; created_at: string }
export interface History { versions: QuoteVersion[]; audit: AuditEvent[] }

// ---------------------------------------------------------------- Stage 2
export interface Contact { id: string; customer_id: string; full_name: string; email: string | null; phone: string | null; preferred_channel: string; language: string; timezone: string; is_primary: boolean; opted_out_at: string | null; opt_out_source: string | null; notes: string | null }
export interface FollowUpPolicy { offsets_days: number[]; max_attempts: number; quiet_hours_start: number; quiet_hours_end: number; timezone: string; skip_weekends: boolean; automation_enabled: boolean; automation_consented_at: string | null; hourly_send_limit: number; sender_name: string | null; reply_to_email: string | null; dev_endpoints_enabled?: boolean }
export interface MessageTemplate { id: string; key: string; name: string; language: string; subject: string; body: string; is_active: boolean }
export interface Draft { id: string; quote_id: string; contact_id: string | null; schedule_id: string | null; channel: string; to_email: string | null; subject: string; body: string; template_key: string | null; status: string; generated_reasons: string[]; provider: string | null; provider_message_id: string | null; sent_by: string | null; sent_at: string | null; last_error: string | null; created_at: string }
export interface Schedule { id: string; quote_id: string; contact_id: string | null; attempt_no: number; due_at: string; status: string; mode: string; reason: string | null; draft_id: string | null; last_error: string | null }
export interface Recommendation { action: string; template_key: string | null; priority: string; reasons: string[]; blocked_by: string[]; suggested_contact_id: string | null }
export interface StatusEvent { id: string; from_status: string; to_status: string; source: string; evidence: string | null; actor_user_id: string | null; occurred_at: string }
export interface TimelineItem { at: string; kind: string; title: string; detail: string | null; source: string | null }
export interface Lifecycle { status: string; allowed_transitions: Record<string, boolean>; events: StatusEvent[]; recommendation: Recommendation; contacts: Contact[]; drafts: Draft[]; schedules: Schedule[]; timeline: TimelineItem[] }
export interface Job { id: string; kind: string; status: string; attempts: number; max_attempts: number; next_run_at: string; last_error: string | null; finished_at: string | null; created_at: string }
export interface Integration { id: string; provider: string; status: string; scopes: string[]; account_email: string | null; last_error: string | null; last_sync_at: string | null; created_at: string }

// ---- Stage 3
export interface OrderLine { id: string; line_no: number; product_id: string | null; sku: string | null; description: string; quantity: string; unit: string; unit_price: string; cost_price?: string | null; tax_rate_pct: string; line_total: string }
export interface OrderEvent { id: string; kind: string; detail: string | null; actor_user_id: string | null; source: string; occurred_at: string }
export interface Order { id: string; number: string; customer_id: string | null; quote_id: string | null; quote_version_no: number | null; currency: string; status: string; source: string; external_ref: string | null; subtotal: string; tax_total: string; grand_total: string; confirmed_at: string | null; confirmed_by: string | null; created_by: string | null; notes: string | null; created_at: string; lines: OrderLine[]; events: OrderEvent[] }
export interface OrderSummary { id: string; number: string; customer_id: string | null; quote_id: string | null; currency: string; status: string; source: string; grand_total: string; created_at: string; confirmed_at: string | null }
export interface Proposal { id: string; customer_id: string; kind: string; status: string; lines: Record<string, string | number>[]; evidence: string[]; currency: string; decided_by: string | null; decided_at: string | null; decision_reason: string | null; order_id: string | null; created_at: string }
export interface KpiMetric { value: string | number | null; [k: string]: unknown }
export interface Kpis { date_from: string; date_to: string; reporting_currency: string; metrics: Record<string, KpiMetric>; unconverted: Record<string, string>; unconverted_count: number; definitions: Record<string, string> }
export interface FxRate { id: string; base_currency: string; quote_currency: string; rate: string; as_of: string; source: string; created_at: string }
export interface Connector { id: string; kind: string; name: string; scopes: string[]; status: string; last_sync_at: string | null; last_error: string | null; created_at: string }
export interface SyncRun { id: string; connection_id: string | null; kind: string; idempotency_key: string; entity: string; status: string; received: number; applied: number; conflicts: number; rejected: number; report: { failed_rows?: { row: number; errors: string[] }[]; failed_total?: number; rejected?: { index: number; reason: string }[] }; created_at: string; finished_at: string | null }
export interface SyncConflict { id: string; sync_run_id: string; entity: string; entity_key: string; local_value: Record<string, unknown>; remote_value: Record<string, unknown>; fields: string[]; status: string; resolved_at: string | null; resolution_note: string | null; created_at: string }
export interface StockLevel { product_id: string; sku: string; status: "fresh" | "stale" | "unknown"; quantity: string | null; observed_at: string | null; source: string | null; age_hours: number | null }
export interface DeletionRequest { id: string; subject_type: string; subject_id: string; reason: string; status: string; requested_by: string | null; decided_by: string | null; decided_at: string | null; purge_after: string | null; executed_at: string | null; summary: Record<string, unknown>; legal_hold: boolean; created_at: string }
export interface CustomerProfile { customer: Customer & { deleted_at?: string | null }; contacts: Contact[]; quotes: QuoteSummary[]; orders: OrderSummary[]; open_proposals: number; stats: Record<string, string | number | null> }
