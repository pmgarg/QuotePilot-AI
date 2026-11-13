# Low-Level Design (Stage 1 implemented; Stages 2–4 designed)

## API package layout (`apps/api/app`)
```
main.py                 FastAPI app factory, middleware (request id, security headers, CSRF), routers
core/config.py          Settings (pydantic-settings), limits, feature flags
core/security.py        Argon2id hashing, token generation/hash, CSRF token helpers
core/errors.py          AppError hierarchy → HTTP mapping (404 tenant-miss, 403 role, 409 state/conflict, 413/415/422)
core/logging.py         JSON logs with request_id, redaction filter (never logs bodies/documents)
db/base.py              DeclarativeBase, UUID/timestamps mixins, TenantMixin
db/session.py           engine, SessionLocal, tenant context (set_config app.current_org / app.current_user)
models/                 SQLAlchemy models (see DATA_MODEL.md)
schemas/                Pydantic request/response models (money as str Decimal)
api/deps.py             get_db, current_session, current_user, current_membership, require_roles, csrf_protect
api/routers/            auth, org, members, customers, catalogs, rfqs, quotes, exports, files, health
services/auth.py        login/logout/session lifecycle, rate limiter
services/audit.py       record(db, org, actor, action, entity, summary)
services/storage.py     Storage protocol, LocalStorage, S3Storage (optional)
services/catalog_import.py  column mapping, validation report, version creation, activation
services/units.py       unit alias normalisation; sku normalisation
services/parsing/       detect.py (type sniffing), limits.py, worker.py (subprocess entry), csv_xlsx.py, pdf.py, text.py, sandbox.py
services/matching.py    exact → fuzzy candidates with categories and reasons
services/pricing.py     Decimal engine: quantise, line compute, totals, margin, blockers
services/quotes.py      create from RFQ, edit version, submit/approve/reject/withdraw, new version, number allocation
services/exports/pdf.py ReportLab invariant renderer;  exports/csv.py formula-safe writer
services/ai/            provider.py (protocol, DisabledProvider), anthropic_provider.py (optional), schemas.py, redaction.py
fixtures/seed.py        synthetic demo data loader (CLI: python -m app.fixtures.seed)
```

## Web → API proxy
The Next.js app serves `/api/*` through `src/app/api/[...path]/route.ts`, a streaming reverse proxy that forwards
method, headers (minus hop-by-hop), body and `Set-Cookie` to `API_INTERNAL_URL`. Browser code never sees the API origin.

## Request lifecycle
1. `RequestIdMiddleware` assigns `X-Request-ID` (uuid4 unless a safe incoming one is present).
2. `SecurityHeadersMiddleware`: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`,
   `Cache-Control: no-store` on `/api`, `Content-Security-Policy: default-src 'none'` for API responses.
3. Dependency `current_session`: reads `qp_session` cookie → sets `app.session_token_hash` (RLS scope) → `sessions` by SHA-256 → expiry/idle check → touches
   `last_seen_at` (at most once per 60 s). `current_membership` loads membership for the session's org and calls
   `set_tenant(db, org_id, user_id)` which executes `SELECT set_config('app.current_org', :o, true), set_config('app.current_user', :u, true)`
   inside the request transaction. All later queries are RLS-scoped.
4. `csrf_protect` for POST/PATCH/PUT/DELETE: `X-CSRF-Token` header must equal the `qp_csrf` cookie value and the
   session's stored csrf secret (HMAC compare). Login/logout are exempt from session but login requires csrf cookie
   pair as well (issued by `GET /api/v1/auth/csrf`).
5. Router → service → `audit.record` → commit. Errors map via `core/errors.py`; unknown exceptions → 500 with
   request id only.

## Authorization
`require_roles(*roles)` dependency; `Permission` enum in `services/authz.py` maps actions to roles (single source for
the matrix in USER_JOURNEYS.md). Object lookups use `get_or_404(model, id)` which relies on RLS + explicit
`organization_id ==` filter. Role denials → 403 `{"error":"forbidden","action":...}`.

## Catalog import algorithm
1. Validate upload (size ≤ `MAX_UPLOAD_BYTES`, extension ∈ {csv, xlsx}, sniffed content type, XLSX zip pre-check).
2. Parse in sandbox to a row list (≤ `MAX_CATALOG_ROWS`). Header row detected; user mapping `{sku, description, unit,
   currency, list_price, cost_price?, stock_qty?, valid_from?, valid_to?}` applied (case-insensitive header names;
   numeric column indexes accepted).
3. Per row: trim; sku non-empty; `sku_normalized = upper(collapse ws, strip surrounding punct)`; price parsed with
   locale-tolerant Decimal parser (`1.234,56` / `1,234.56` / `1234.56`), must be ≥ 0; currency must be ISO-4217 code in
   supported table; unit normalised via alias table (unknown units kept raw with warning); formula-like cells
   (`=`, `+`, `-`, `@`, tab, CR prefixes) kept as text and flagged `formula_like`.
4. Duplicates (same `sku_normalized`) → row error "duplicate sku, first occurrence kept"; invalid rows skipped and reported.
5. New `catalog_versions` row (`status=imported`, `import_report` with counts/errors/warnings ≤ 500 entries) and products
   bulk-inserted. `POST .../activate` sets `catalogs.active_version_id` (audited). Previous versions remain immutable.

## RFQ parsing
- Accept: `text/csv`, `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`, `application/pdf`, or JSON
  `{"text": "..."}` (≤ 100 kB). Magic-byte sniffing: `%PDF-` / `PK\x03\x04` + `[Content_Types].xml` / text heuristics.
- Login sets `app.login_email` before the user lookup and performs a dummy Argon2 verification for unknown accounts (timing equalisation).
- Sandbox (`sandbox.run_parser(path, kind)`): `subprocess.run([sys.executable, "-I", "-m", "app.services.parsing.worker", kind, path])`,
  `timeout=PARSE_TIMEOUT_SECONDS` (20), child sets `RLIMIT_AS` (768 MB) and `RLIMIT_CPU`, output JSON on stdout
  (≤ 2 MB), stderr discarded from logs except first 500 chars of a generic error class.
- Extraction output: `ParsedItem{raw_text, source_ref{page|sheet,row|line}, quantity?, unit_raw?, sku_hint?, description}`.
  Spreadsheets: header detection by synonyms (qty/quantity/qnty, item/description/desc, sku/part/code/article, unit/uom);
  without header: first text column = description, first numeric = quantity. PDF: `extract_tables()` rows first;
  fallback to text lines matched by regexes (`qty unit desc`, `desc qty unit`, `sku ... qty`). Zero text → `scanned_or_image_pdf`.
- Status machine: `uploaded → parsing → parsed | failed(reason)`; `POST /reparse` allowed from `failed`/`parsed`
  (replaces items that are not yet linked to a generated quote). Items are inserted in one transaction only after a
  successful parse (S1-A17).

## Matching
```
for item in rfq.items:
  cands = []
  if item.sku_hint and sku_index.get(norm(item.sku_hint)): cands += [(p, 100, "exact SKU", "sku")]
  else: tokens = sku-like tokens in raw_text; exact hits appended with reason "SKU token in text"
  if not exact: fuzzy = rapidfuzz.process.extract(desc_norm, desc_index, scorer=WRatio, limit=5, score_cutoff=60)
  category: exact if sku exact; likely if top ≥ 90 and (top - second) ≥ 10; ambiguous if top ≥ 70; else unmatched
  unit_mismatch = item.unit_normalized and product.unit and item.unit_normalized != product.unit
  duplicates: same (sku_hint or desc_norm) within RFQ → is_duplicate on later lines
```
Selection on `exact` is pre-filled but editable; `likely`/`ambiguous` require explicit user choice.

## Pricing engine (`services/pricing.py`)
```
q = quantize(value, minor_units(currency), rounding)  # ROUND_HALF_UP | ROUND_HALF_EVEN
line_subtotal   = q(quantity * unit_price)
discount_amount = q(line_subtotal * discount_pct / 100)
net             = line_subtotal - discount_amount
tax_amount      = q(net * tax_rate_pct / 100)
line_total      = net + tax_amount
totals          = Σ per field over lines (already quantised)
margin_pct      = (unit_price - cost_price) / unit_price * 100  (only if cost known and unit_price > 0)
```
Blockers (computed server-side, returned with every version): `no_lines`, `line_unresolved`, `missing_price`,
`currency_mismatch`, `below_min_margin` (override-able by Owner/Admin with reason), `unit_mismatch_unconfirmed`
(cleared by explicit line confirmation), `validity_expired`. `submit` is refused with 409 and the blocker list while any
remains. Pricing snapshot (currency, rounding, tax default, min margin, catalog version id) is stored on the version.

## Quote lifecycle & concurrency
- `quotes.lock_version` increments with every mutation; requests carry `expected_lock_version`; the UPDATE is
  conditional (`WHERE lock_version = :expected`); 0 rows → 409 `{"error":"conflict","current":{...}}`.
- `submit`: version `draft → pending_approval`, records submitter. `approve`: requires role ∈ {owner, admin, approver},
  actor ≠ submitted_by unless `allow_self_approval`; version `approved`, `approved_by/at`, quote status `approved`,
  `valid_until = today + default_validity_days` if unset. `reject`: back to `draft` with reason. `withdraw`: approved →
  withdrawn (privileged, reason). `expired` is derived at read time from `valid_until < today` and enforced on approve/export.
- New version from approved: copies lines, status `draft`, `current_version_no++`; previous version immutable (`approved`
  remains downloadable as history; the quote status shows `draft` with "previous approved v1").
- Quote number allocation: `UPDATE organizations SET quote_seq = quote_seq + 1 ... RETURNING quote_seq` → `Q-YYYY-NNNNN`.

## Exports
- PDF: ReportLab `SimpleDocTemplate(invariant=1)`, metadata title/author fixed, no timestamps other than the approval
  date string; fonts: built-in Helvetica (deterministic). Content: org brand block, customer, quote number/version,
  validity, lines table (sku, description, qty, unit, unit price, disc %, tax %, line total), totals block, notes/terms,
  footer. Stored at `org/<org>/quotes/<quote>/v<n>.pdf` with SHA-256; regeneration returns the stored file if hash
  matches (idempotent). Only versions in `approved` may be rendered as "current"; draft versions can preview with a
  "DRAFT – not approved" watermark line.
- CSV: `csv.writer` with `QUOTE_ALL`; cells starting with `= + - @ \t \r` prefixed with `'`.

## Audit events (Stage 1 actions)
`auth.login`, `auth.logout`, `auth.login_failed` (no org), `org.update`, `member.create`, `member.role_change`,
`customer.create/update`, `catalog.import`, `catalog.activate`, `rfq.create`, `rfq.parse_failed`, `rfq.reparse`,
`rfq.item_update/create/delete`, `quote.create`, `quote.update`, `quote.submit`, `quote.approve`, `quote.reject`,
`quote.withdraw`, `quote.new_version`, `quote.margin_override`, `quote.export_pdf`, `quote.export_csv`, `file.download`.

## Timeouts & limits (defaults, env-configurable)
| Setting | Default |
|---|---|
| MAX_UPLOAD_BYTES | 10 MiB |
| MAX_PASTED_TEXT_CHARS | 100 000 |
| MAX_CATALOG_ROWS | 50 000 |
| MAX_RFQ_ROWS / MAX_PDF_PAGES | 2 000 / 50 |
| PARSE_TIMEOUT_SECONDS | 20 |
| MAX_CONCURRENT_PARSERS (per API process) | 4 (503-style `busy` failure beyond) |
| XLSX_MAX_UNCOMPRESSED_BYTES | 60 MiB |
| LOGIN_RATE_LIMIT | 10 / 5 min per (ip, email) |
| SESSION_IDLE / ABSOLUTE | 12 h / 7 d |
| DB statement_timeout | 15 s (app role) |

## Stage 2 LLD sketch
- `services/followup/policy.py` computes due schedules from approved/sent events + policy + contact preferences
  (opt-out → `suppressed`). `services/followup/recommend.py` rule set emits `{action, reason_facts[]}`.
- `services/comms/providers/{simulator,smtp,gmail,msgraph}.py` behind `MailProvider` protocol: `send(draft) ->
  ProviderResult`, `fetch_inbound(since)`; tokens encrypted with envelope key from env (`TOKEN_ENCRYPTION_KEY`).
- `workers/runner.py`: PostgreSQL job queue (`job_runs`, claimed with `SKIP LOCKED`; ADR-012 as amended — not Redis); job row with `idempotency_key` UNIQUE; backoff `min(2^n * 30s, 1h)` ≤ 5 attempts;
  dead-letter table + `/api/v1/jobs/dead-letters`.
- Sending gate: `policy.automation_enabled && consented_by admin && contact.not_opted_out && rate_limit.ok` or explicit
  `POST /drafts/{id}/send` by Sales+ with CSRF; both audited; provider message id stored; no "sent" without provider ack.

## Stage 3 LLD (as implemented)
All Stage 3 services live in `app/services/intelligence/`; the router is `app/api/routers/intelligence.py`.
- `orders.py`: converts the *current approved* quote version into a `proposed` order (totals copied, never recomputed);
  confirmation is a separate decision by a different user; transitions take a row lock (`get_order(for_update=True)`)
  and write `order_events`. A customer pending erasure cannot get a new order.
- `kpis.py` / `fx.py`: pure functions over quotes/orders with written `DEFINITIONS`; reporting currency through the
  tenant's manually entered `fx_rates` (latest rate on or before the quote date), otherwise the `unconverted` bucket.
- `data_io.py` / `inputs.py`: CSV import of customers and orders (per-row validation and savepoint, idempotency key +
  request hash, failed-row report) and role-gated, audited, formula-neutralised export.
- `connectors.py`: **push model** — an authenticated caller posts records to `/connectors/{id}/sync`; there is no
  `pull()` and no staging table. Differences from master data go to `sync_conflicts` and are applied only by an
  explicit, audited resolution. Shipped connector kinds are stubs; no real ERP/CRM adapter exists.
- `stock.py`: availability labels only from timestamped `stock_snapshots`; otherwise `unknown`.
- `repeat_orders.py`: proposals from the customer's own confirmed orders; a human decides.
- `retention.py`: deletion request → approval by another user → soft delete → worker purge (`purge_customer` job,
  re-queue cap, reclaim of abandoned jobs), legal hold, audit at each step.
- Cost/margin visibility: there is no generic `strip_sensitive` serializer; each response and export adds cost
  columns only when the principal has `view_costs`.

## Stage 4 LLD (as implemented)
All Stage 4 services live in `app/services/platform/`; the router is `app/api/routers/platform.py`.
- `tools.py`: registry of 9 tools with scope, required permission, `requires_approval` and a strict Pydantic argument
  model (`extra="forbid"`); `allowed_tools(role, plan_scopes)` is the backend policy. No SQL, shell or HTTP tool.
- `invocations.py`: propose → (approval) → execute. Policy is checked at proposal and again at execution; approval
  takes a row lock; a failed approved action is persisted. Approval checks the approver's permission; it does **not**
  require approver ≠ proposer (release review 1, S4-RR1-1 — human decision open).
- `assistant.py`: one planning pass per message. Default `RulePlanner` (deterministic, no network); optional hosted
  planner only with `AI_PROVIDER`, a server-side key and owner consent. Planner output is untrusted: unknown tools,
  wrong argument types and extra keys are rejected by the gate; a provider failure becomes a reply, not an error.
  Limits: 8 tool steps per message, 50 messages per session, 4000 characters per message, AI calls per day by plan.
  Messages are redacted before storage; `ai_usage` holds counts only.
- `workflows.py`: versioned JSON definitions, validation at save and again at activation, dry run (handlers receive
  `dry_run=True`), live run with approval pause, bounded retry, step budget, compensation for `propose_order` and
  `draft_follow_up`, circuit breaker per tenant.
- `killswitch.py`: tenant switch (owner/admin, audited) plus a platform-wide environment flag; checked before every
  step. Only workflow dry runs proceed while it is engaged.
- `entitlements.py`: plans, limits and counters enforced in the service layer; billing is a test-mode adapter (no
  payment provider). There is no `services/billing/` package and no agent runner loop.
