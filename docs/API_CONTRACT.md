# API Contract (Stage 1) — base path `/api/v1`

The machine-readable schema is served at `/api/v1/openapi.json` (FastAPI) and exported to `docs/openapi.json` by
`make openapi`. Money fields are decimal strings (`"1234.50"`). Errors use `{"error": "<code>", "detail": ..., "request_id": "..."}`.

Auth: cookie `qp_session` (HttpOnly). Unsafe methods require header `X-CSRF-Token` matching cookie `qp_csrf`.
Tenant-miss → 404. Role denial → 403. State/concurrency → 409. Validation → 422. Too large → 413. Unsupported → 415.
Durability: the transaction of a request (data change and its audit row) is committed before the response is sent; a
2xx answer therefore means the change is visible to the next request, and a failed commit is answered with an error.

| Method | Path | Roles | Purpose |
|---|---|---|---|
| GET | /health | public | liveness `{status:"ok"}`; `/health/ready` checks DB |
| GET | /auth/csrf | public | issues `qp_csrf` cookie, returns token |
| POST | /auth/login | public (+csrf) | `{email,password}` → `{user, organization, role}`; sets session cookie; rate limited |
| POST | /auth/logout | any (+csrf) | revokes session |
| GET | /auth/me | any | current user, org, role, csrf token |
| GET | /org | any | organization settings (brand, currency, tax, rounding, margin floor hidden for sales/readonly) |
| PATCH | /org | owner, admin | update settings; `ai_processing_consent` and `allow_self_approval` are owner-only; audit stores before/after |
| GET | /org/members | owner, admin | list members |
| POST | /org/members | owner, admin | create a NEW local user + membership `{email, full_name, role, password}`; 409 (generic) if the address is already registered anywhere |
| PATCH | /org/members/{id} | owner, admin | change role / (de)activate the **membership** (tenant-scoped; revokes that tenant's sessions); last-active-owner guard; owner-only for owner rows |
| GET/POST | /customers | any / sales+ | list/create |
| GET/PATCH | /customers/{id} | any / sales+ | detail/update |
| GET | /catalogs | any | catalogs with versions summary |
| GET | /catalogs/active-version | any | `{version_id}` of the catalog version new ad-hoc quote lines price from (null without an active catalog); used by the editor's "Use active catalog" re-link |
| POST | /catalogs/import | owner, admin | multipart `file` + `mapping` (JSON string) + `catalog_name?` → version + report. A price cell must hold exactly one number (optional currency code/symbol); anything else, a negative price or a price ≥ 10^12 is a row error (`list price is not a number` / `is negative` / `is too large`), never a guessed value. Per-user limit: 10 imports per 5 minutes, else 429 with `Retry-After` |
| POST | /catalogs/versions/{id}/activate | owner, admin | activate version |
| GET | /catalogs/versions/{id}/products?q=&page= | any | products (cost hidden for sales/readonly) |
| POST | /rfqs | sales+ | multipart `file` (+`customer_id`, `reference`) or JSON `{text, customer_id, reference}` → RFQ (parsed or failed). Item quantities are stored only when 0 < q ≤ 10,000,000 (three decimals); otherwise `quantity` is null with flag `quantity_unreadable`, `quantity_not_positive` or `quantity_out_of_range`, which blocks quote generation until a person enters one. A line whose fuzzy matching was not run (comparison budget of 2,000,000 per parse, the 8 s matching deadline, two concurrent matcher processes per API process, or a matcher failure) has `match_status` `unmatched`, no candidates and the flag `match_skipped_budget`; exact-SKU matches are never skipped, and choosing a product clears the flag. Per-user limit: 30 RFQ creations/re-parses per 5 minutes, else 429 with `Retry-After` |
| GET | /rfqs?status=&q= | any | list |
| GET | /rfqs/{id} | any | detail with items + candidates |
| POST | /rfqs/{id}/reparse | sales+ | re-run parser (same per-user limit as RFQ creation: 429 + `Retry-After`) |
| POST | /rfqs/{id}/items | sales+ | add manual item |
| PATCH | /rfqs/{id}/items/{item_id} | sales+ | `{selected_product_id|null, quantity, unit, description, unit_confirmed, is_custom}` |
| DELETE | /rfqs/{id}/items/{item_id} | sales+ | remove item |
| POST | /rfqs/{id}/generate-quote | sales+ | creates quote v1 from resolved items → `{quote}` (409 with blockers if unresolved) |
| GET | /quotes?q=&status=&customer_id= | any | list/search |
| POST | /quotes | sales+ | create empty draft quote `{customer_id, currency?}` |
| GET | /quotes/{id} | any | header + current version + lines + blockers + history summary. `withdrawn_reason` is cleared when a later version is approved; withdrawn documents are visible as `versions[].status = withdrawn` |
| PATCH | /quotes/{id} | sales+ | edit current draft version `{expected_lock_version, valid_until?, notes?, terms?, lines:[...]}`. Line semantics: `unit_price` equal to a `catalog` line's current price keeps `price_source=catalog` (omit it for untouched lines); a different price makes the line `manual` (needs owner/admin confirmation); changing `product_id` is only accepted for products of the quote's pricing catalog version (422 otherwise); untouched lines are never re-validated on save |
| POST | /quotes/{id}/submit | sales+ | `{expected_lock_version}` → pending_approval or 409 blockers. Blocker codes: `no_lines`, `line_unresolved`, `missing_price`, `manual_price_unconfirmed`, `below_min_margin`, `catalog_version_mismatch` (catalog line priced from a list that is no longer this quote's price list — re-link it), `validity_expired` |
| POST | /quotes/{id}/approve | owner, admin, approver | `{expected_lock_version, reason?}`; SoD enforced |
| POST | /quotes/{id}/reject | owner, admin, approver | `{expected_lock_version, reason}` |
| POST | /quotes/{id}/withdraw | owner, admin, approver | `{expected_lock_version, reason}` — withdraws EVERY approved version (status `withdrawn`, never exportable); allowed while a newer draft is open (the draft stays editable) |
| POST | /quotes/{id}/versions | sales+ | new draft version from approved |
| POST | /quotes/{id}/lines/{line_id}/price-override | owner, admin | `{expected_lock_version, reason}` — confirms a manually entered price or accepts a below-floor margin (clears `manual_price_unconfirmed` / `below_min_margin`); `/margin-override` kept as alias |
| GET | /quotes/{id}/history | any | versions, decisions, audit events |
| POST | /quotes/{id}/versions/{n}/pdf | any | render (idempotent) → `{sha256, size}` |
| GET | /quotes/{id}/versions/{n}/pdf | any | stream PDF (approved; drafts watermarked) |
| GET | /quotes/{id}/versions/{n}/csv | any | formula-safe CSV |
| GET | /files/{id} | any | stream original upload (same org) |

## Samples
### Login
```http
POST /api/v1/auth/login
Content-Type: application/json
X-CSRF-Token: 3b1f…
Cookie: qp_csrf=3b1f…

{"email": "sales@northwind.example", "password": "********"}
```
```json
{"user": {"id": "…", "email": "sales@northwind.example", "full_name": "Sara Sales"},
 "organization": {"id": "…", "name": "Northwind Trading LLC", "default_currency": "AED"},
 "role": "sales", "csrf_token": "3b1f…"}
```
### RFQ detail (excerpt)
```json
{"id": "…", "status": "parsed", "source_kind": "pdf", "currency": "AED",
 "items": [{"id": "…", "line_no": 1, "raw_text": "10 pcs SKU-1001 Hex bolt M8x40 zinc", "quantity": "10", "unit_raw": "pcs",
   "unit_normalized": "EA", "match_status": "exact", "selected_product_id": "…", "unit_mismatch": false, "is_duplicate": false,
   "source_ref": {"page": 1, "line": 7},
   "candidates": [{"product_id": "…", "sku": "SKU-1001", "description": "Hex bolt M8x40 zinc plated", "category": "exact",
                   "score": "100", "reason": "exact SKU match", "unit": "EA", "currency": "AED", "list_price": "0.85"}]}]}
```
Blocker codes: `no_lines`, `line_unresolved`, `missing_price`, `manual_price_unconfirmed`, `below_min_margin`,
`validity_expired`, plus RFQ-level `duplicate_unconfirmed`, `quantity_missing`, `unit_mismatch_unconfirmed`. Messages for
roles without `view_costs` never contain margin figures or the organisation floor.

### Submit refused
```json
HTTP 409
{"error": "blocked", "blockers": [{"code": "missing_price", "line_no": 3, "message": "Line 3 has no unit price"},
                                 {"code": "below_min_margin", "line_no": 1, "message": "Margin 4.0% below floor 12%", "overridable_by": ["owner","admin"]}],
 "request_id": "…"}
```
### Conflict
```json
HTTP 409
{"error": "conflict", "detail": "Quote was modified by another user", "current_lock_version": 7}
```

## Stage 2–4 endpoints (designed)
- Stage 2 (implemented): `GET/POST /customers/{id}/contacts`, `PATCH /contacts/{id}`, `POST /contacts/{id}/opt-out`
  (opt-out and opt-in act on the **e-mail address**: every contact row of the tenant with that address (case-insensitive)
  changes together; a new or re-addressed contact with an opted-out address starts opted out; opt-in is owner/admin
  only); `GET/PATCH /follow-up/policy` (automation consent owner/admin, audited; `sender_name` rejects control
  characters with 422; the response carries `dev_endpoints_enabled` for the UI);
  `GET /follow-up/templates`, `PATCH /follow-up/templates/{id}` (whitelisted placeholders, plain text);
  `POST /quotes/{id}/transition` `{expected_lock_version,to_status,evidence?,occurred_at?}` (evidence mandatory for
  acknowledged/won/lost); `GET /quotes/{id}/lifecycle` (status, allowed transitions, events, recommendation with reasons,
  contacts, drafts, schedules, timeline); `POST /quotes/{id}/drafts`, `GET /drafts`, `GET/PATCH /drafts/{id}`
  (placeholders in an edit are resolved when it is saved, unknown ones give 422; `to_email` must be the draft's own
  contact, else 422), `POST /drafts/{id}/send` (explicit, CSRF, sales+; the opt-out instruction is always present in
  what is delivered; concurrent clicks deliver once; 403 `opt_out` or `no_follow_ups` (per address), 429 hourly limit,
  400/503 provider errors), `POST /drafts/{id}/cancel`; `GET /follow-up/schedules?status=` (statuses include `failed`
  with the reason in `last_error`), `POST …/{id}/cancel`, `POST …/{id}/reschedule`, `POST /quotes/{id}/follow-up/plan`
  (schedule keys include the quote version; a system send of an approved quote plans follow-ups too);
  `GET /communication-events`; `GET /jobs`, `POST /jobs/{id}/retry` (re-arms a `failed` schedule);
  `POST /jobs/run-once` (development/test only, i.e. `APP_ENV` is `development` or `test`; processes only the
  caller's tenant, at most 25 jobs per call, audited as `worker.run_once`; 403 `worker` otherwise);
  `GET/POST /integrations`, `POST /integrations/{id}/revoke`, `POST /integrations/sync-inbound` (audited;
  a provider error is stored on the connection and audited as `integration.sync_failed`),
  `POST /integrations/simulator/inbound` (development/test only; `from_email` is validated).
  Job outcomes: a due schedule whose automation consent was withdrawn becomes a draft (`drafted`, `draft_only`); a
  refusal by our own rules on the send path (no consent for a queued automated send, opt-out, wrong state)
  dead-letters at once with the schedule marked `failed`; transient provider errors and the hourly limit are retried
  with capped backoff.
- Stage 3 (implemented): **Orders** `GET /orders?status&customer_id`, `GET /orders/{id}` (events + lines; `cost_price`
  only with `view_costs`), `POST /quotes/{id}/orders` `{version_no?, notes?}` (sales+; approved version only; 409 if an
  open order exists) → `proposed`; `POST /orders/{id}/confirm` (approve_order: owner/admin/approver; proposer may not
  confirm own proposal unless `allow_self_approval`), `/fulfil` (sales+, confirmed only), `/cancel` `{reason}` (approve_order).
  **Repeat orders** `POST /customers/{id}/order-proposals` → proposal or `null` (nothing to propose), `GET /order-proposals?status`,
  `POST /order-proposals/{id}/approve|reject` `{reason?}` (approve_order; approval creates a *proposed* order priced from
  the active catalog or, labelled, the last paid price). **KPIs** `GET /kpis?date_from&date_to` → metrics with
  `definitions`, `reporting_currency`, `unconverted{currency: amount}`; `GET/POST /fx-rates` (POST manage_connectors).
  **Data** `GET /data/export/{customers|orders|products}` (export_data = owner/admin/approver; sales/readonly 403 **and**
  audited `export.denied`; formula-safe CSV; cost columns only with view_costs), `POST /data/import/{customers|orders}?idempotency_key=`
  multipart CSV (import_data = owner/admin; per-row validation, partial apply, `report.failed_rows`),
  `GET /data/runs`, `GET /data/runs/{id}/failed.csv` (fix-and-re-upload file). **Connectors** `GET/POST /connectors`
  `{kind: erp_stub|csv, name, scopes ⊆ read:products|read:stock|read:orders|read:customers}`, `POST /connectors/{id}/sync`
  `{entity, idempotency_key, records[]}` (push model, no outbound network; same key → same run; scope missing → 403),
  `POST /connectors/{id}/disable`, `GET /sync-conflicts?status`, `POST /sync-conflicts/{id}/resolve`
  `{resolution: keep_local|apply_remote|dismiss, note?}` (a `new` SKU cannot be applied — import a catalog version).
  **Stock** `GET /stock?q` → per active-catalog product `{status: fresh|stale|unknown, quantity, observed_at, source, age_hours}`.
  **Profiles** `GET /customers/{id}/profile` (contacts, quotes, orders, open proposals, stats; `avg_margin_pct` only with
  view_costs). **Retention** `POST /retention/requests` `{customer_id, reason}` (sales+), `GET /retention/requests`,
  `POST /retention/requests/{id}/approve|reject|legal-hold` (manage_retention = owner/admin; requester ≠ approver;
  approval soft-deletes immediately — hidden from lists, follow-ups cancelled, contacts opted out — and the worker
  purges after `RETENTION_GRACE_DAYS`).
  **Stage 3 hardening (fix round 1)**: `POST /connectors/{id}/enable` (manage_connectors; 409 when another active
  connector of the same kind exists — one *active* connector per kind). Idempotency keys for imports and syncs are
  namespaced per `(kind, connector, entity)` inside the tenant and stored with a hash of the request: same key + same
  payload returns the earlier run and applies nothing; same key + different payload → **409** (never a silent no-op).
  Import and sync records are validated per row/record (lengths, control characters, quantity/price bounds, currency,
  timestamps): an invalid record is rejected with a reason and never aborts the others or returns 500.
  Orders are created only from the *current* approved version of a quote and copy its totals verbatim.
  A soft-deleted customer (approved deletion) returns **404** on every route that would start new processing (RFQ,
  quote, contact creation, order proposal) for all roles, and on read routes for roles without `manage_retention`;
  owner/admin can still read the customer, its contacts and profile with `deleted_at` set.
  **Fix round 2**: `POST /quotes/{id}/orders` and `POST /order-proposals/{id}/approve` also return **404** for a
  customer pending erasure (rejecting a proposal stays possible). `GET /quotes` returns `customer_name: null` for
  quotes of such a customer, and a `q=` name search does not match it, unless the role has `manage_retention`.
  `POST /orders/{id}/confirm|fulfil|cancel` lock the order row: of two concurrent requests the loser gets **409**
  (one event, one audit row). A connector sync replayed concurrently under one key answers the loser like a
  sequential replay (same payload → the existing run, different payload → **409**), never 500. The purge summary
  and the `retention.purged` audit row carry `communication_events_redacted`.
  `GET /quotes/{id}` may report the blocker `catalog_price_changed` (a draft/pending catalog-priced line whose product
  price was changed in place by an applied connector conflict); it blocks submit/approve until the line is re-priced
  or explicitly overridden. `pending_value` counts approved quotes in lifecycle `approved`, `sent`, `acknowledged`
  and `awaiting_customer` (not won/lost/withdrawn/expired).
- Stage 4 (implemented): **Command center** `GET /command-center` (queues filtered by role, kill-switch state, plan).
  **Assistant** `GET /assistant/tools` (catalogue = role permission ∩ plan scopes), `GET/POST /assistant/sessions`,
  `GET /assistant/sessions/{id}` (own sessions only; others 404), `POST /assistant/sessions/{id}/messages` `{text ≤ 4000}`
  → assistant message with `proposed_actions` (each `executed | proposed (needs approval) | rejected`) and `citations`;
  429 on plan/turn limits, 503 `kill_switch_engaged`. **Actions** `GET /actions?status` (own; owner/admin see all),
  `POST /actions` `{tool, args, dry_run}`, `POST /actions/{id}/approve|reject` `{reason?}` (requester or owner/admin;
  re-checks policy and kill switch at execution; an approved export returns `download.content_b64` once and never
  stores it). **Workflows** (owner/admin) `GET/POST /workflows` `{key, definition}` (each POST = new draft version with
  a validation report), `POST /workflows/validate`, `POST /workflows/{id}/activate?version_no=`, `/disable`,
  `POST /workflows/{id}/runs` `{mode: dry_run|live, context, version_no?}` (live needs the active version, kill switch
  off, breaker closed, daily run allowance; a workflow `dry_run` stays possible while the kill switch is engaged, `POST
  /actions` with `dry_run` does not), `GET /workflow-runs`, `GET /workflow-runs/{id}`,
  `POST /workflow-runs/{id}/approve|reject` (continues or stops a run paused at a side-effect step). Triggers:
  `manual`, `quote.approved`, `order.confirmed` (dispatched after the business action; never undo it); any other trigger fails validation.
  Concurrent approvals of one action or workflow step: exactly one wins, the others get 409. A failed approved action
  is persisted as `failed` (with a `tool.failed` audit row) and cannot be approved again; a workflow step whose
  approved action fails ends the run as `failed` (HTTP 200 with the run). Request bodies containing a NUL character
  anywhere (strings, keys, nested args) return 422. `GET /workflows` is 403 for roles without manage_workflows.
  **Plan & ops** `GET /platform/plan`, `POST /platform/plan` (owner; test-mode adapter), `GET /platform/usage`,
  `GET /platform/support-export` (owner/admin; configuration and counts only, audited), `GET/POST/DELETE
  /platform/kill-switch` (owner/admin). Limits enforced server-side: seats on `POST /org/members`, RFQs per month on
  `POST /rfqs`, AI calls per day on assistant messages, workflow runs per day, active workflows on activation.
