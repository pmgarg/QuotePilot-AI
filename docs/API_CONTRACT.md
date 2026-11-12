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
| GET | /catalogs/active-version | any | `{version_id}` of the catalog version new ad-hoc quote lines price from (null without an active catalog); used by the editor's "Use active catalog" re-link |
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
  Job outcomes: a refusal by our own rules (consent withdrawn, opt-out, wrong state) dead-letters at once with the
  schedule marked `failed`; transient provider errors and the hourly limit are retried with capped backoff.
- Stage 3: `/orders{,/{id}/events}`, `/kpis?from&to&currency`, `/imports`, `/exports` (role-aware), `/connectors`,
  `/sync/conflicts{,/{id}/resolve}`, `/stock/{product_id}`, `/deletion-requests`.
- Stage 4: `/assistant/sessions{,/{id}/messages}`, `/tools` (allowed list), `/workflows{,/{id}/versions,/{id}/dry-run,/{id}/runs}`,
  `/admin/kill-switch`, `/billing/plan`, `/admin/usage`.
