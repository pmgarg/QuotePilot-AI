# API Contract (Stage 1) — base path `/api/v1`

The machine-readable schema is served at `/api/v1/openapi.json` (FastAPI) and exported to `docs/openapi.json` by
`make openapi`. Money fields are decimal strings (`"1234.50"`). Errors use `{"error": "<code>", "detail": ..., "request_id": "..."}`.

Auth: cookie `qp_session` (HttpOnly). Unsafe methods require header `X-CSRF-Token` matching cookie `qp_csrf`.
Tenant-miss → 404. Role denial → 403. State/concurrency → 409. Validation → 422. Too large → 413. Unsupported → 415.

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
| POST | /catalogs/import | owner, admin | multipart `file` + `mapping` (JSON string) + `catalog_name?` → version + report |
| POST | /catalogs/versions/{id}/activate | owner, admin | activate version |
| GET | /catalogs/versions/{id}/products?q=&page= | any | products (cost hidden for sales/readonly) |
| POST | /rfqs | sales+ | multipart `file` (+`customer_id`, `reference`) or JSON `{text, customer_id, reference}` → RFQ (parsed or failed) |
| GET | /rfqs?status=&q= | any | list |
| GET | /rfqs/{id} | any | detail with items + candidates |
| POST | /rfqs/{id}/reparse | sales+ | re-run parser |
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
  (opt-in is owner/admin only); `GET/PATCH /follow-up/policy` (automation consent owner/admin, audited);
  `GET /follow-up/templates`, `PATCH /follow-up/templates/{id}` (whitelisted placeholders, plain text);
  `POST /quotes/{id}/transition` `{expected_lock_version,to_status,evidence?,occurred_at?}` (evidence mandatory for
  acknowledged/won/lost); `GET /quotes/{id}/lifecycle` (status, allowed transitions, events, recommendation with reasons,
  contacts, drafts, schedules, timeline); `POST /quotes/{id}/drafts`, `GET /drafts`, `GET/PATCH /drafts/{id}`,
  `POST /drafts/{id}/send` (explicit, CSRF, sales+; 403 `opt_out`, 429 hourly limit, 400/503 provider errors),
  `POST /drafts/{id}/cancel`; `GET /follow-up/schedules?status=`, `POST …/{id}/cancel`, `POST …/{id}/reschedule`,
  `POST /quotes/{id}/follow-up/plan`; `GET /communication-events`; `GET /jobs`, `POST /jobs/{id}/retry`,
  `POST /jobs/run-once` (non-production); `GET/POST /integrations`, `POST /integrations/{id}/revoke`,
  `POST /integrations/sync-inbound`, `POST /integrations/simulator/inbound` (non-production).
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
