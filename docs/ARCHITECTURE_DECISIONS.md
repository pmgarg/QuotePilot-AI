# Architecture Decision Records

Format: Context → Options → Decision → Consequences → Security impact → Revisit when.

## ADR-001 Modular monolith: FastAPI API + Next.js web + (later) one worker
- Context: team of 1–3; four stages; must run locally with Docker Compose at no cost.
- Options: (a) microservices per stage; (b) single Django app; (c) FastAPI modular monolith + Next.js.
- Decision: (c). Modules under `apps/api/app/{api,services,models}`; a worker process shares the codebase from Stage 2.
- Consequences: one deployable, simple migrations, shared auth; scaling by replicas; clear module boundaries enforced by review.
- Security: smaller attack surface, one auth path. Revisit when: parsing or job load requires independent scaling.

## ADR-002 PostgreSQL with organization scoping + row-level security (defense in depth)
- Options: schema-per-tenant; DB-per-tenant; shared tables with `organization_id` + RLS.
- Decision: shared tables, `organization_id NOT NULL` on every tenant table, service-layer filtering, and RLS policies
  using `current_setting('app.current_org')` set per transaction with `set_config(..., true)`. Runtime connects as
  `quotepilot_app` (no table ownership, `NOBYPASSRLS`); migrations run as `quotepilot_owner`.
- Consequences: a forgotten filter cannot leak rows; integration tests assert RLS directly. Cross-tenant IDs return 404.
- Security: mitigates IDOR/BOLA (ASVS V4). Revisit when: a customer requires physical isolation (then DB-per-tenant tier).

## ADR-003 Authentication: Argon2id password hashing + server-side sessions; OIDC optional later
- Options: JWT access tokens; third-party auth SaaS (paid); OIDC-only; library-hashed passwords with DB sessions.
- Decision: `argon2-cffi` (PasswordHasher defaults, Argon2id) for local accounts; opaque 256-bit session token stored
  as SHA-256 hash with idle (12 h) and absolute (7 d) expiry; cookie `qp_session` HttpOnly, SameSite=Lax, Secure
  outside development; CSRF double-submit (`qp_csrf` cookie + `X-CSRF-Token` header) for unsafe methods; login
  rate limiting. OIDC (Authlib) is a documented Stage 4 option for SSO on the Business plan.
- Consequences: immediate revocation, no token-in-JS. Security: ASVS V2/V3. Revisit when: SSO demand or multi-region.

## ADR-004 Deterministic money with `Decimal`
- Decision: all monetary values are `Decimal` with per-currency minor units (ISO 4217 table subset) and configurable
  rounding (`ROUND_HALF_UP` default, `ROUND_HALF_EVEN` option) applied at line level; totals are sums of rounded lines.
  DB columns `NUMERIC(18,6)` for prices, `NUMERIC(18,2/3)` quantised on write. JSON transports money as strings.
- Consequences: reproducible totals; no float anywhere. Revisit: never.

## ADR-005 Document parsing in an isolated subprocess with hard limits
- Options: parse in-request thread; Celery worker; subprocess with timeout and rlimits.
- Decision: `subprocess` running `python -m app.services.parsing.worker` with wall-clock timeout, `RLIMIT_AS`
  memory cap, size/page/row caps, XLSX zip-member size pre-check (zip bomb), `openpyxl` read-only + `data_only`,
  `pdfplumber` text/table extraction only. No macros, formulas, scripts or external references are evaluated.
- Consequences: a crash or hang cannot take down the API; RFQ transitions to `failed` with reason and can be retried.
- Security: LLM/doc injection remains data; parser DoS bounded. Revisit when: volume justifies async worker (Stage 2 runner).

## ADR-006 Matching: deterministic normalisation + rapidfuzz, LLM optional and non-authoritative
- Decision: exact normalised SKU → fuzzy description (`rapidfuzz` WRatio/token_set) with thresholds mapped to
  categorical confidence; reasons recorded per candidate. LLM adapter (ADR-009) may only propose extracted lines
  from pasted text; its output is schema-validated and marked `proposed`.
- Consequences: works offline, explainable. Revisit when: calibrated confidence data exists from real usage.

## ADR-007 PDF rendering with ReportLab in invariant mode
- Options: WeasyPrint (needs Pango/Cairo), wkhtmltopdf (unmaintained), ReportLab.
- Decision: ReportLab Platypus, `invariant=1`, fixed metadata and creation date = approval timestamp → identical
  bytes for identical input (S1-A18). Stored privately with SHA-256.
- Revisit when: rich HTML branding is required (then WeasyPrint in a dedicated container).

## ADR-008 Storage abstraction: local private directory now, S3-compatible adapter for production
- Decision: `Storage` interface with `LocalStorage` (root outside web roots, server-generated keys
  `org/<org_id>/<kind>/<uuid>`) and `S3Storage` (boto3, optional extra, private bucket, SSE). Downloads only via
  authorized API streaming; no public URLs; signed URLs (short expiry) are a documented option.

## ADR-009 AI provider adapter: disabled by default, server-side only, structured output
- Decision: `AIProvider` protocol with `DisabledProvider` (default) and `AnthropicProvider` (optional extra, key from
  env only). Inputs are redacted (no cost/margin), bounded in size, wrapped with injection-resistant instructions;
  outputs parsed into Pydantic models; failures fall back to deterministic parsing. Usage recorded in `ai_usage`
  (Stage 4 entitlements). Keys never reach the browser.

## ADR-010 Optimistic concurrency with `lock_version`
- Decision: quotes carry `lock_version`; mutating calls pass `expected_lock_version`; update is `WHERE lock_version =
  :expected` and failure returns 409 with the current representation. Approved versions are immutable rows.

## ADR-011 Frontend: Next.js 16 App Router, client components over same-origin `/api` rewrite
- Decision: UI logic in client components calling `/api/*`, which a runtime route handler
  (`apps/web/src/app/api/[...path]/route.ts`) proxies to `API_INTERNAL_URL` per request (a `next.config` rewrite was
  rejected because its target is baked at build time and breaks Docker/E2E topologies); auth is cookie-based and
  first-party; no secrets in the bundle; Cache Components disabled (all data is per-tenant dynamic).
- Consequences: simple mental model; SSR of tenant data avoided in Stage 1. Revisit when: SEO or SSR needed (never for app pages).

## ADR-012 Background jobs (Stage 2): Redis + lightweight runner with DB-backed idempotency
- Decision: RQ-style runner (plain Redis lists + DB `workflow_runs/steps` tables for state) rather than Celery, with
  idempotency keys stored in PostgreSQL unique index; capped exponential backoff; dead-letter table and UI.

## ADR-013 Stage 4 policy engine: tool access from backend entitlements, never from prompts
- Decision: tool registry with declared scopes; `allowed_tools(user, org, plan)` computed server-side; every tool call
  validated against JSON schema, tenant scope and approval requirements; budgets/circuit breaker per org; kill switch flag.

## ADR-014 CI and merge policy
- Decision: GitHub Actions (`.github/workflows/ci.yml`, `security.yml`) run lint/type/unit/integration/E2E/scans.
  Branch protection must be enabled by a human in repository settings (required checks `api`, `web`, `e2e`,
  `security`; require review). The independent release-reviewer verdict recorded in `RELEASE_GATES.md` is a required
  merge condition by policy.
