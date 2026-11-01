export type Role = "owner" | "admin" | "sales" | "approver" | "readonly";

export interface User { id: string; email: string; full_name: string; is_active: boolean }
export interface Organization {
  id: string; name: string; legal_name: string | null; address: string | null; default_currency: string;
  default_tax_rate: string; rounding_mode: string; default_validity_days: number; brand_color: string;
  pdf_footer: string | null; allow_self_approval: boolean; min_margin_pct?: string;
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
