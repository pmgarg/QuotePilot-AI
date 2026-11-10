# Data Model

All tenant tables: `id UUID PK`, `organization_id UUID NOT NULL FK → organizations`, `created_at`, `updated_at`,
RLS policy `organization_id = current_setting('app.current_org', true)::uuid` (SELECT/INSERT/UPDATE/DELETE, FORCED),
composite index on `(organization_id, <lookup column>)`. The global auth tables `users` and `sessions` also carry RLS
(migration 0002): a user row is visible only as the acting user, the account being logged in (`app.login_email`) or a
member of the current organisation; a session row only by its presented token hash (`app.session_token_hash`), its own
user or its organisation. Membership activation (`memberships.is_active`) is per tenant; `users.is_active` is a global
platform flag never toggled by tenant admins. `quote_versions.document_snapshot` freezes validity, customer and
organisation header fields at approval; the approved PDF is rendered once at approval and never rewritten (migration 0003).

## ERD — Stage 1 (implemented)
```mermaid
erDiagram
  organizations ||--o{ memberships : has
  users ||--o{ memberships : has
  users ||--o{ sessions : has
  organizations ||--o{ customers : has
  organizations ||--o{ catalogs : has
  catalogs ||--o{ catalog_versions : has
  catalog_versions ||--o{ products : contains
  organizations ||--o{ uploaded_files : has
  organizations ||--o{ rfqs : has
  rfqs }o--|| customers : for
  rfqs ||--o{ rfq_items : has
  rfq_items ||--o{ match_candidates : has
  match_candidates }o--|| products : refers
  rfqs ||--o{ quotes : generates
  quotes ||--o{ quote_versions : has
  quote_versions ||--o{ quote_lines : has
  quote_versions ||--o{ approval_decisions : has
  organizations ||--o{ audit_events : has

  organizations { uuid id PK; text name; text legal_name; text address; char3 default_currency; numeric default_tax_rate; text rounding_mode; numeric min_margin_pct; int default_validity_days; bool allow_self_approval; text brand_color; text pdf_footer; int quote_seq; timestamptz ai_processing_consented_at; uuid ai_processing_consented_by; timestamptz created_at }
  users { uuid id PK; citext email UK; text password_hash; text full_name; bool is_active; timestamptz created_at }
  memberships { uuid id PK; uuid organization_id FK; uuid user_id FK; text role; bool is_active; UK organization_id_user_id }
  sessions { uuid id PK; uuid user_id FK; uuid organization_id; text token_hash UK; timestamptz expires_at; timestamptz last_seen_at; text ip; text user_agent }
  customers { uuid id PK; uuid organization_id FK; text name; text reference; text email; char3 default_currency; text notes }
  catalogs { uuid id PK; uuid organization_id FK; text name; uuid active_version_id }
  catalog_versions { uuid id PK; uuid organization_id FK; uuid catalog_id FK; int version_no; text status; uuid source_file_id; jsonb column_mapping; jsonb import_report; int row_count; uuid created_by }
  products { uuid id PK; uuid organization_id FK; uuid catalog_version_id FK; text sku; text sku_normalized; text description; text unit; char3 currency; numeric list_price; numeric cost_price; numeric stock_qty; date valid_from; date valid_to; UK version_sku }
  uploaded_files { uuid id PK; uuid organization_id FK; text kind; text original_name; text content_type; bigint size_bytes; text sha256; text storage_key; uuid uploaded_by }
  rfqs { uuid id PK; uuid organization_id FK; uuid customer_id FK; text reference; text source_kind; uuid source_file_id FK; text pasted_text; text status; text failure_reason; char3 currency; uuid created_by }
  rfq_items { uuid id PK; uuid organization_id FK; uuid rfq_id FK; int line_no; text raw_text; jsonb source_ref; text sku_hint; text description; numeric quantity; text unit_raw; text unit_normalized; text match_status; uuid selected_product_id; bool unit_mismatch; bool unit_confirmed; bool is_duplicate; bool is_custom; text notes }
  match_candidates { uuid id PK; uuid organization_id FK; uuid rfq_item_id FK; uuid product_id FK; int rank; text category; numeric score; text reason; text matched_on }
  quotes { uuid id PK; uuid organization_id FK; text number UK_org; uuid customer_id FK; uuid rfq_id FK; char3 currency; text status; int current_version_no; int lock_version; date valid_until; text withdrawn_reason; uuid created_by }
  quote_versions { uuid id PK; uuid organization_id FK; uuid quote_id FK; int version_no; text status; text notes; text terms; numeric subtotal; numeric discount_total; numeric tax_total; numeric grand_total; jsonb pricing_snapshot; uuid submitted_by; timestamptz submitted_at; uuid approved_by; timestamptz approved_at; text pdf_storage_key; text pdf_sha256; jsonb document_snapshot }
  quote_lines { uuid id PK; uuid organization_id FK; uuid quote_version_id FK; int line_no; uuid product_id; uuid rfq_item_id; text sku; text description; numeric quantity; text unit; numeric unit_price; text price_source; numeric cost_price; numeric discount_pct; numeric tax_rate_pct; numeric line_subtotal; numeric discount_amount; numeric tax_amount; numeric line_total; bool is_custom; text margin_override_reason; uuid margin_override_by }
  approval_decisions { uuid id PK; uuid organization_id FK; uuid quote_version_id FK; text decision; text reason; uuid decided_by; timestamptz decided_at }
  audit_events { bigint id PK; uuid organization_id; uuid actor_user_id; text action; text entity_type; uuid entity_id; text request_id; text ip; jsonb summary; timestamptz created_at }
```

### Key constraints and indexes (Stage 1)
- `products`: UNIQUE `(catalog_version_id, sku_normalized)`; INDEX `(organization_id, catalog_version_id, sku_normalized)`; GIN trigram on `description` optional (pg_trgm) for search.
- `quotes`: UNIQUE `(organization_id, number)`; INDEX `(organization_id, status)`, `(organization_id, customer_id)`.
- `quote_versions`: UNIQUE `(quote_id, version_no)`; `status ∈ draft | pending_approval | approved | rejected | superseded | withdrawn`
  (`superseded`: a pending version made obsolete by withdrawal; `withdrawn`: an approved document withdrawn for good — never exportable).
- `rfq_items`: UNIQUE `(rfq_id, line_no)`; `quantity > 0` CHECK when not null.
- `quote_lines`: CHECK `quantity > 0`, `discount_pct BETWEEN 0 AND 100`, `tax_rate_pct BETWEEN 0 AND 100`.
- `memberships`: UNIQUE `(organization_id, user_id)`; role CHECK in (owner, admin, sales, approver, readonly).
- `sessions`: INDEX `(token_hash)`, `(expires_at)`.
- `audit_events`: INDEX `(organization_id, created_at DESC)`, `(organization_id, entity_type, entity_id)`.

## Stage 2 tables (migration 0006, all RLS-forced; `follow_up_schedules`/`job_runs` also readable under the worker scope)
`contacts`, `follow_up_policies` (one per org; automation consent fields), `message_templates`, `quote_status_events`
(recorded lifecycle facts with source/evidence), `follow_up_schedules` (idempotency key per quote+attempt; statuses
pending/drafted/sent/cancelled/suppressed/superseded), `communication_drafts` (send idempotency key, provider message id),
`communication_events` (sent/reply_received/opt_out; bounded snippets, never full bodies), `integration_connections`
(AES-GCM envelope-encrypted secret, non-secret config, cursor), `job_runs` (queue: idempotency key, attempts, backoff,
dead letters). Column-level grants from migration 0004 are preserved (0005 grants only on the new tables).

## Stage 3 tables (migration 0007, all RLS-forced; `deletion_requests` also readable under the worker scope)
`orders` (number `SO-YYYY-NNNNN` from `organizations.order_seq`; customer, source quote/version, currency, status
`proposed|confirmed|fulfilled|cancelled`, source `quote|repeat|import|connector`, `external_ref` idempotency for
imports/connectors, totals, confirmed_by/at, created_by), `order_lines` (copied prices — never recomputed —, cost
copied for margin reporting), `order_events` (append-only trail: proposed/confirmed/fulfilled/cancelled, actor,
source), `order_proposals` (repeat-order proposals: JSON lines + human-readable evidence, decision, resulting order),
`stock_snapshots` (product, source connector kind, quantity, `observed_at` from the source, `recorded_at`),
`fx_rates` (per-tenant reporting rates `base/quote/as_of`, UK per pair+date), `connector_connections` (kind
`erp_stub|csv`, declared scopes, status), `sync_runs` (connector syncs **and** CSV imports: idempotency key UK,
counters, report with rejected rows/indices), `sync_conflicts` (entity, key, local vs remote values, differing
fields, status `open|resolved_local|resolved_remote|dismissed`), `deletion_requests` (subject, reason, status
`requested|approved|rejected|executed`, `purge_after`, `legal_hold`, summary). `customers.deleted_at` marks the
soft-deleted state. Grants only on the new tables (column-level grants from 0004 preserved).

## Stage 4 tables (migration 0008, all RLS-forced; `tool_invocations`, `workflow_runs`, `workflow_steps` also readable under the worker scope)
`subscriptions` (one per org: plan code, status, limit overrides, test-mode billing reference), `usage_counters`
(metered limits per period: `rfqs_per_month`, `ai_calls_per_day`, `workflow_runs_per_day`; atomic upsert),
`assistant_sessions` / `assistant_messages` (private to the user; content redacted before storage; proposed actions
and citations as JSON), `tool_invocations` (tool, scope, validated args + hash, status
`proposed|approved|executed|rejected|failed|would_execute`, requires_approval, dry_run, origin
`assistant|workflow|manual`, requester/decider, redacted result summary — never bulk content),
`workflow_definitions` / `workflow_versions` (versioned JSON definition + validation report; one active version),
`workflow_runs` / `workflow_steps` (mode `dry_run|live`, trigger, context, status
`running|awaiting_approval|succeeded|failed|compensated|killed|budget_exceeded`, budget used, failure explanation),
`ai_usage` (provider, model, purpose, token counts, trace id, outcome — no prompt or completion columns),
`kill_switches` (tenant switch with reason, actor, runs terminated; the platform switch is an environment flag).

## Designed for later stages (not created yet)
| Stage | Table | Purpose / key columns |
|---|---|---|
| 2 | contacts | per customer: name, email, phone, preferences (channel, language, time zone), opted_out_at, opt_out_source |
| 2 | follow_up_policies | org-level timing rules: offsets (days), max_attempts, quiet_hours, timezone, automation_enabled, consented_by/at |
| 2 | follow_up_schedules | quote_id, contact_id, due_at, attempt_no, status (pending, sent, cancelled, suppressed, failed), idempotency_key UK |
| 2 | communication_drafts | quote_id, contact_id, channel, subject, body, status (draft, approved, sent, failed), provider_message_id, sent_by, sent_at |
| 2 | communication_events | quote_id, kind (sent, delivered, bounced, reply_received, opened?*), recorded_at, source, payload (redacted); *opened only when provider reports |
| 2 | integration_connections | provider (simulator, smtp, gmail, msgraph), scopes, encrypted_tokens (envelope encryption), status (active, revoked, error), last_error |
| 2 | job_runs / job_dead_letters | job type, idempotency_key UK, attempt, next_run_at, last_error, payload (no bodies) |
| 2 | templates | org email templates with placeholders (whitelisted variables) |

## Data minimisation and retention hooks
- Original uploaded files and parsed text are retained per org retention setting (default 24 months) then purged by job (Stage 3 deletion workflow); audit retained 7 years by default (configurable, documented in PRIVACY_AND_RETENTION.md).
- `pasted_text` and `raw_text` are the only free-text customer content in Stage 1; they are never sent to logs.
- Cost price is stored once per product version and copied into `quote_lines.cost_price` for margin audit; serializers strip it for Sales/Read-only.
