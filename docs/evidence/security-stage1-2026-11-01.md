# Security audit evidence — Stage 1 (2026-11-01)

Auditor: security-auditor subagent (independent of the implementer). Repository `QuotePilot-AI`, branch
`claude/multi-stage-project-impl-8hz2wj`, commit `70351b7` (HEAD moved to `7a29d68`, CI-only change, during the
audit). Product code was **not** modified by the auditor; only `apps/api/tests/security/test_audit_followups.py` and
this document were added.

**Concurrent changes:** while this audit ran, the implementer landed *uncommitted* working-tree changes to
`apps/api/app/api/routers/org.py`, `routers/auth.py`, `schemas/common.py`, `services/quotes.py`,
`services/parsing/extract.py` and `services/catalog_import.py` (and the QA agent added `apps/api/tests/qa/`). The first
audit pass (code review + tests) ran against `70351b7`; every finding was then re-verified against the working tree.
Findings marked **fixed-WT** below reproduce at `70351b7` but no longer reproduce in the working tree; they remain
open until the fix is committed and reviewed.

This document states what was tested, how, and what was found. It does not claim the absence of vulnerabilities.

## 1. Scope

| In scope | Method |
|---|---|
| API (`apps/api/app`): auth, sessions, CSRF, RBAC, tenant isolation/RLS, RFQ/quote/catalog/member/customer/file routes, pricing engine, exports, parser sandbox, storage, AI adapter, config | Code review of every file listed in the task + black-box tests against a local PostgreSQL 16 (`quotepilot_sec`) + dynamic probing of a local uvicorn (`:8041`) |
| Web (`apps/web`): runtime API proxy `src/app/api/[...path]/route.ts`, `src/lib/api.ts`, `next.config.ts`, login redirect, built bundle `.next` | Code review + dynamic probing through `next start` on `:3041` (proxy → `:8041`) + Chromium (Playwright) for the login redirect |
| Database: migration `20261101_0001_stage1_initial.py` (RLS policies, grants) | Catalog queries as the runtime role, raw-SQL tests |
| Dependency / static scanners | pip-audit, bandit, ruff (S rules), npm audit; gitleaks / semgrep / trufflehog **not available** (see § 3) |

Checklists used: OWASP ASVS 5.0 V2/V3/V4/V5/V12/V14, OWASP Top 10 for LLM Applications 2025, `docs/THREAT_MODEL.md`
abuse cases 1–10, PRD X-01..X-09 and S1-F01..F21.

Only the local application built from this repository was tested (localhost). No external host was touched.

## 2. Environment

| Item | Value |
|---|---|
| Host | Claude Code cloud container, Ubuntu 24.04, Python 3.13.18 (`apps/api/.venv`), Node 22, PostgreSQL 16 (local service) |
| Database | `postgresql://…/quotepilot_sec` (dedicated, empty before the run; migrated by `tests/conftest.py`). Runtime role `quotepilot_app` (`rolsuper=f`, `rolbypassrls=f`); migrations as `quotepilot_owner` |
| Env for tests | `APP_ENV=test DATABASE_URL=…quotepilot_app…/quotepilot_sec DATABASE_ADMIN_URL=…quotepilot_owner…/quotepilot_sec` |
| Dynamic servers | `uvicorn app.main:app --host 127.0.0.1 --port 8041` (APP_ENV=test, seeded with `python -m app.fixtures.seed`, `DEMO_PASSWORD` set, synthetic data only); `npx next start --port 3041` with `API_INTERNAL_URL=http://127.0.0.1:8041`. Both stopped after the probes. |
| Not available | Docker daemon (compose topology not executed), gitleaks, semgrep, trufflehog, outbound network targets |

## 3. Tool results (command, exit code, summary)

| Tool | Command (cwd) | Exit | Result |
|---|---|---|---|
| pip-audit | `.venv/bin/pip-audit --skip-editable` (apps/api) | 0 | `No known vulnerabilities found` (editable `quotepilot-api` skipped) |
| bandit | `.venv/bin/bandit -r app -q -f txt -ll` (apps/api) | 0 | no medium/high findings |
| ruff (project rules incl. S*) | `.venv/bin/ruff check app --output-format concise` (apps/api) | 0 | `All checks passed!` |
| ruff S-only, app + tests | `.venv/bin/ruff check app tests --select S` (apps/api) | 1 | 789 hits, **all `S101` (assert in tests)**, which the project config excludes by policy; no other S rule fires |
| ruff on new test file | `.venv/bin/ruff check tests/security/test_audit_followups.py` + `ruff format --check` | 0 | clean after formatting |
| npm audit (prod) | `npm audit --omit=dev` (apps/web) | 0 | `found 0 vulnerabilities` |
| npm audit (all) | `npm audit` (apps/web) | 1 | 16 moderate, all transitive `minimatch` via `eslint`/`eslint-config-next` (dev tooling, not shipped) — unchanged from `scan-20261101-203701.md` |
| gitleaks | `gitleaks detect …` | — | **NOT AVAILABLE** locally (binary absent; CI job exists but was not observed by the auditor) |
| semgrep | `semgrep scan …` | — | **NOT AVAILABLE** locally (CI only, not observed) |
| trufflehog | — | — | **NOT AVAILABLE** |
| fallback secret grep | `grep -rInE "(sk-ant-…|AKIA…|BEGIN … PRIVATE KEY|ghp_…)"` excluding node_modules/.venv/.next/.git | 0 | only hit is the allow-listed sample string in `.gitleaks.toml:15` |
| built bundle grep | `grep -rIlE "sk-ant-\|AKIA…\|ANTHROPIC_API_KEY\|SECRET_KEY\|quotepilot_owner" .next/static .next/server`; `grep -rIoE "NEXT_PUBLIC_[A-Z_]+" .next/static` | 0 | no secret patterns; no `NEXT_PUBLIC_*` variables in the client bundle |
| `.env.example` | manual review | — | placeholders only; default local DB passwords are documented as local-only |

### Test runs (counts recorded verbatim)

| Command (cwd `apps/api`, env as in § 2) | Result |
|---|---|
| `.venv/bin/pytest -q tests/security/test_audit_followups.py` @ `70351b7` (first pass) | **26 passed, 10 xfailed** (xfail = SEC-01, SEC-02, SEC-03, SEC-04, SEC-06 ×5, SEC-08) |
| same file @ working tree (final) | **32 passed, 7 xfailed** in 57.6 s — xfail(strict) = SEC-01, SEC-02, SEC-06 ×3 (quote-line cases), SEC-08, SEC-22; SEC-03/SEC-04 and the RFQ-item half of SEC-06 are now positive tests. A strict xfail turns into a hard failure the moment the defect is fixed, so the marker must be removed with the fix |
| `.venv/bin/pytest -q -m security` (working tree) | see § 3a |
| `.venv/bin/pytest -q tests/unit tests/integration tests/security` (working tree; `tests/qa` excluded because it was being written concurrently) | see § 3a |
| `.venv/bin/pytest -q` incl. `tests/qa` (working tree, intermediate) | 13 failed, 214 passed, 10 xfailed — **all 13 failures are in `tests/qa/`** (QA's own open-defect tests; verified by running `tests/qa` alone: 13 failed, 84 passed); 0 failures outside `tests/qa` |

### 3a. Suite counts (final run, working tree)

| Command (cwd `apps/api`) | Result | Exit |
|---|---|---|
| `.venv/bin/pytest -q -m security` | **45 passed, 7 xfailed**, 188 deselected in 68.4 s | 0 |
| `.venv/bin/pytest -q tests/unit tests/integration tests/security` | **136 passed, 7 xfailed** in 132.7 s (= the 104 pre-existing tests + 32 new passing + 7 strict xfails documenting open findings) | 0 |

## 4. Findings

Severity follows the project policy (CVSS v3.1 bands, judged on impact × exploitability in the documented
topologies). "Repro" refers to the test or the exact command that demonstrated the behaviour. All reproductions
used synthetic accounts only.

| ID | Sev | Title | Location | Reproduction | Impact | Remediation |
|---|---|---|---|---|---|---|
| SEC-01 | **High** | Cross-tenant account takeover of availability: an admin of tenant B can attach any existing user (by email) to B and then deactivate them globally | `apps/api/app/api/routers/org.py:329-343` (`create_member` reuses the existing `User` row when the email exists anywhere, ignores the supplied password), `org.py:382-392` (`is_active` is a global `users` column; `revoke_user_sessions` revokes **all** of the user's sessions) | `test_audit_followups.py::test_member_create_cannot_attach_or_disable_another_tenants_user` (xfail): B-admin `POST /org/members {email: sales@alphatrading.example}` → **201**, response contains the victim's real `full_name`/`is_active`; `PATCH /org/members/{id} {is_active:false}` → victim's tenant-A session returns **401** and login to tenant A fails | Any tenant admin can lock any user of any other tenant out of the whole platform (DoS on availability), learns whether an email is registered and its display name (information disclosure / oracle), and silently gains a membership for the victim in B. Violates X-01 and S1-F01 | Make membership state (`is_active`) per membership, not per user; never attach an existing account to another organisation without an explicit invitation accepted by the account holder; scope session revocation to `(user, organization)`; return an opaque result that does not reveal whether the email already exists |
| SEC-02 | Medium | `users` and `sessions` tables have no row-level security; the runtime role can read every tenant's users (email, Argon2 hash, name) and session token hashes from any tenant context | `apps/api/alembic/versions/20261101_0001_stage1_initial.py:18` (`TENANT_TABLES` omits `users`, `sessions`), `:409-416` (blanket `GRANT SELECT … ON ALL TABLES`) | `test_users_table_is_tenant_scoped` (xfail): with `app.current_org = A`, `SELECT email FROM users` returns tenant B's accounts | Defence-in-depth gap: any SQL injection or future service bug in the app role exposes all credentials hashes and session hashes platform-wide (THREAT_MODEL B3/B7). Root cause of SEC-01's disclosure | Add policies: `users` visible when `id = app.current_user` or a membership exists for `app.current_org`; `sessions` visible when `user_id = app.current_user`; or move login/session lookup behind `SECURITY DEFINER` functions and revoke direct `SELECT` on `password_hash`/`token_hash` from the app role |
| SEC-03 | Medium (**fixed-WT**) | Admin can deactivate an owner (when ≥ 2 owners exist) although admins may not change an owner's role | `org.py:382-392` (`is_active` branch lacks the "only an owner may modify an owner" guard that the role branch has at `:377`) | `test_admin_cannot_deactivate_an_owner_even_when_two_owners_exist`: at `70351b7` admin `PATCH /org/members/{owner} {is_active:false}` → **200**, owner's session → 401. Working tree: → 422 and the last-**active**-owner guard holds (test now passes) | Vertical privilege escalation: an admin can remove owner access (except the last owner) and thus block owner-only actions (enabling self-approval, granting owner) — ASVS V4.2.2, X-02 | Apply the hierarchy guard to every mutation of an owner's membership; audit with `before/after` |
| SEC-04 | Medium (**fixed-WT**) | A withdrawn quote can be approved: `approve()` checks only `version.status`, not `quote.status`/effective status | `apps/api/app/services/quotes.py` `approve()` (state checks `version.status != "pending_approval"`, no `quote.status` check; `withdraw()` keeps the version in `pending_approval`) | `test_withdrawn_quote_cannot_be_approved`: at `70351b7` submit → owner `withdraw` (200) → approver `approve` → **200**, quote `status=approved`. Working tree: approve and reject → 409, withdrawn pending version is marked `superseded` (test now passes) | Workflow integrity: S1-F15 ("withdrawn quotes cannot be approved") is violated; a withdrawal by a privileged user can be undone by any approver without a new version/audit reason | In `approve`/`reject` require `quote.status == "pending_approval"` and `effective_status(quote) not in {withdrawn, expired}`; include `status` in the conditional `UPDATE` (as the LLD already claims) |
| SEC-05 | Medium | `X-Forwarded-For` is forwarded verbatim by the web proxy and trusted by uvicorn → login rate limiter bypass and audit-IP spoofing (local/e2e topology); per-email lockout DoS (Docker topology) | `apps/web/src/app/api/[...path]/route.ts:24` (`headers.set("x-forwarded-for", req.headers.get("x-forwarded-for") ?? "")` — client value, real socket address never appended); `apps/api/app/api/deps.py:45` (`request.client.host`, which uvicorn rewrites from XFF for trusted proxies — default `forwarded_allow_ips=127.0.0.1`); `apps/api/Dockerfile:15` (no explicit `--forwarded-allow-ips`) | Direct to `:8041` from loopback: 14 failed logins for one email with rotating `X-Forwarded-For: 198.51.100.N` → **14× 401, no 429**; fixed XFF → 429 from the 11th. Through the Next proxy: `POST /customers` with `X-Forwarded-For: 203.0.113.9` → `audit_events.ip = 203.0.113.9` | Credential stuffing/brute force is not throttled whenever the proxy is on a trusted address (the `make dev`/e2e setup, and any reverse proxy added to `forwarded_allow_ips`); audit IPs are attacker-controlled. Where the proxy is *not* trusted, every user shares one IP and 10 wrong passwords lock any chosen email for 5 min (abuse by any anonymous party) | Proxy: set `X-Forwarded-For` to the connecting socket address (append, never pass through the inbound value); API: configure `--forwarded-allow-ips` to the ingress only, rate-limit on `(ip)` **and** `(email)` with different budgets, and add the Redis-backed limiter already planned |
| SEC-06 | Low (partially fixed-WT) | Finite-but-extreme Decimals pass Pydantic validation and crash in PostgreSQL (HTTP 500) | `apps/api/app/schemas/common.py:235` (`RFQItemUpdate.quantity: Decimal` unbounded), `:246` (`RFQItemCreate.quantity` only `gt=0`), `:256-258` (`QuoteLineIn` `le=1e9` but product `quantity*unit_price` up to 1e18 > `NUMERIC(18,3)`), no quantisation check before `CHECK (quantity > 0)` | `test_extreme_decimals_are_422_not_500`: quote-line `quantity=1e9, unit_price=1e9` → 500 (numeric overflow); `quantity="1e-400"` / `"0.0001"` → 500 (`ck_quote_line_qty_positive` after rounding to `0.000`) — **still open** (3 xfail cases). RFQ item `quantity="1e308"`/`"1e300"`/`"0.0001"`/`"1e-400"` → 500 at `70351b7`, 422 in the working tree (`le=1e9, decimal_places=3` added; 4 positive cases) | Unhandled exceptions, log noise, cheap error amplification by any editor; no data leak (generic body verified) | Add `max_digits`/`decimal_places` constraints matching the columns, quantise and re-validate `> 0` in the service, map `DataError`/`IntegrityError` to 422 |
| SEC-07 | Info (closed) | LLD states "approve checks state in the same UPDATE"; the implementation's conditional `UPDATE` only checks `lock_version` | `quotes.py::_check_lock` | `test_approve_race_with_guessed_next_lock_version` and `test_double_approve_same_lock_version_race` **pass**: a concurrent approve with lock N+1 (or the same N) gets 409 and exactly one `approval_decisions` row exists | Not exploitable today because every mutation increments `lock_version` before any state write; documented so the LLD claim is corrected and the invariant is kept in mind for future code paths | Either add the status predicate to the UPDATE or correct the LLD |
| SEC-08 | Low | NUL byte in a search parameter reaches PostgreSQL → HTTP 500 on four list routes | `apps/api/app/api/routers/quotes.py:102-104`, `customers.py:427-429`, `rfqs.py:193-195`, `catalogs.py:597-599` | `test_nul_byte_in_search_is_rejected_not_500` (xfail); through the proxy `GET /api/v1/quotes?q=%00` → `{"error":"internal_error",…}` 500 | Unauthenticated-after-login error amplification, log noise; body is generic (no leak) | Strip or reject `\x00` in string query params (422) via a shared validator |
| SEC-09 | Medium | Open redirect after login: `next=/\evil.com` passes the `startsWith("/") && !startsWith("//")` guard; WHATWG URL parsing turns `/\` into `//` | `apps/web/src/app/login/page.tsx:23` | Playwright (Chromium) against `:3041`: `GET /login?next=%2F%5Cevil.com`, sign in → browser navigates to `http://evil.com/` (external request observed); `//evil.com` is blocked; `/dashboard` works | Phishing: a crafted login link on the real domain forwards the victim to an attacker page right after authentication (ASVS V5.1 / V13 redirect validation) | Resolve `new URL(next, window.location.origin)` and require `url.origin === window.location.origin` and no backslash; or allow-list `^/[A-Za-z0-9/_-]*$` |
| SEC-10 | Low | Timing-based user enumeration at login (Argon2 verification skipped when the email is unknown) | `apps/api/app/services/auth.py:179-180` | `scratchpad/timing.py` against `:8041`: unknown email median **9.6 ms** (n=12) vs known email + wrong password median **87.3 ms** (n=15) | Confirms whether an email is a registered user (ASVS V2.2.2 generic responses; complements SEC-01's oracle) | Always run `verify_password` against a constant dummy hash when the user is missing |
| SEC-11 | Low | Cross-currency margin: editing a line with a product whose catalog currency differs from the quote copies `cost_price` and computes `margin_pct` against a manual price in another currency | `quotes.py::update_draft` (product branch sets `line.cost_price = product.cost_price` unconditionally; `generate_from_rfq` nulls it in the same situation) | Dynamic: EUR quote, AED product (cost 0.52 AED), `unit_price=1` → privileged view shows `margin_pct=48`, no blocker | The minimum-margin floor (S1-F12) is evaluated with mixed currencies, so it can be satisfied or triggered incorrectly; inconsistent with the RFQ path | Null `cost_price` (or require an explicit FX rate) when `product.currency != quote.currency`; add a `cross_currency_cost` flag/blocker |
| SEC-12 | Low | `GET /quotes/{id}/versions/{n}/pdf` has side effects (renders, stores, writes `quote.export_pdf` audit) and is reachable by top-level cross-site GET navigations (SameSite=Lax) | `apps/api/app/api/routers/quotes.py:309-331` | Dynamic: readonly `GET …/pdf` → 200 and the `quote.export_pdf` audit count increased 0 → 1 | Audit-trail pollution, storage/CPU churn triggered by a third-party page or image tag chain; export of sensitive data counted as "audited" even when unintended | Serve stored bytes on GET without new audit when the hash is unchanged; perform render/store via the existing POST only |
| SEC-13 | Info (**fixed-WT**) | `POST /auth/logout` did not require CSRF | `apps/api/app/api/routers/auth.py:226-238` | Dynamic @ `70351b7`: logout with cookies and no header → 200. Working tree adds `Depends(csrf_protect)` | Logout CSRF (nuisance) | Commit the fix |
| SEC-14 | Low | Web CSP allows `script-src 'unsafe-inline'` | `apps/web/next.config.ts:149` | Header observed on `/login` | Reduces XSS mitigation value of the CSP (React escaping remains the primary control; no `dangerouslySetInnerHTML` found by grep) | Nonce-based CSP via Next middleware |
| SEC-15 | Info | Server banner and schema exposure: `server: uvicorn` passes through the proxy; `/api/v1/openapi.json` stays enabled in production (`docs` are disabled) | `apps/api/app/main.py:32-35`; uvicorn default header | Dynamic headers; code | Fingerprinting / surface disclosure | Run uvicorn with `--no-server-header`; gate `openapi_url` on non-production |
| SEC-16 | Low | Unhandled 500 responses carry `request_id: "-"` and no `X-Request-ID` header, so incidents cannot be correlated with logs | `apps/api/app/main.py:108-118` (handler runs in Starlette's outer `ServerErrorMiddleware`, after the `request_context` middleware reset the contextvar) | Dynamic: `GET /api/v1/quotes?q=%00` → body `request_id: "-"`, no `x-request-id` header | Operability of the security logging requirement (SECURITY_POLICY "errors return request id only") | Generate the id in a pure-ASGI middleware added last (outermost) or store it on `request.state` and read it in the handler |
| SEC-17 | Low | Audit event for quote edits records only `line_count`; manual price/discount changes by Sales are not reconstructable (X-07 before/after summary) | `quotes.py::update_draft` audit call | Code review; `GET /quotes/{id}/history` after a price edit shows `{"version_no":1,"line_count":n}` | Repudiation / forensic gap for "price change", which PRD X-03 lists as high impact | Record a per-line diff of `unit_price`, `discount_pct`, `product_id`, `is_custom` |
| SEC-18 | Info | RFQ text parser extracts the SKU digits as quantity when a formula/instruction prefix precedes the line (`=HYPERLINK(...) 3 pcs SKU-1003` → qty `1003`), and RFQ items get no `formula_like` flag (catalog import does) | `apps/api/app/services/parsing/extract.py` text regexes | `test_pasted_text_nul_and_length_limits` prints items; value observed during the audit | Data-integrity footgun; mitigated by mandatory human verification and CSV neutralisation (verified) | Flag formula-like RFQ cells; prefer the quantity nearest a unit token |
| SEC-19 | Info | Parser worker swallows `setrlimit` failures silently; parent buffers the whole child stdout before enforcing the 8 MiB cap | `apps/api/app/services/parsing/worker.py:85-93`, `sandbox.py:46-57`; no cap on concurrent parser subprocesses | Code review (`test_parser_memory_limit_applies` only proves the happy path) | If limits cannot be applied in a container, nothing warns; N concurrent uploads × 768 MB RLIMIT_AS | Log when limits are not applied; bound concurrent parses with a semaphore; read stdout incrementally |
| SEC-20 | Info | Defence-in-depth deviation from CLAUDE.md ("org filter AND RLS"): two product lookups rely on RLS only | `apps/api/app/api/routers/rfqs.py:80-84` (`_products_for`), `quotes.py::generate_from_rfq` (`db.get(Product, item.selected_product_id)`) | Code review; not exploitable (RLS verified, ids come from validated rows) | Would become a tenant leak if RLS were ever disabled on `products` | Add the explicit `organization_id` filter |
| SEC-22 | Low | Expired approved quote is still exported as current: PDF (GET and POST) and CSV return 200 and the PDF label stays `APPROVED` after `valid_until` has passed | `apps/api/app/services/exports/quote_exports.py:153-160,198-209` (`build_pdf_model`/`render_and_store_pdf` consult `version.status` and `quote.status == "withdrawn"` only; `effective_status` is never consulted) | `test_expired_quote_is_not_exportable_as_current` (xfail): approve, set `valid_until = yesterday`, `effective_status == "expired"`, then `GET/POST …/pdf` and `GET …/csv` → **200** (expected 409). Independently reported by QA (`tests/qa/test_qa_quote_lifecycle.py`) | S1-F15 ("expired … cannot be exported as current") violated; a customer can receive a document labelled APPROVED for a quote the system considers expired | Refuse (409) or watermark `EXPIRED` when `effective_status == "expired"`, keep the stored historical PDF downloadable only through an explicit history flag |
| SEC-21 | Info | Docker image runs `uvicorn --workers 2`: the in-memory login limiter is per worker (budget effectively ×2 on one host) | `apps/api/Dockerfile:15`; KNOWN_LIMITATIONS mentions multi-replica only | Code review | Slightly weaker throttling than documented | Document, or move to the shared limiter |

No Critical finding. Per SECURITY_POLICY, High/Medium findings must be fixed or carry a human-signed risk acceptance
before the Stage 1 gate: **SEC-01 (High), SEC-02, SEC-05, SEC-09 (Medium) are open**; SEC-03 and SEC-04 (Medium) are
fixed in the uncommitted working tree and need commit + review + re-run of `test_audit_followups.py`. Low findings
SEC-06 (quote-line half), SEC-08, SEC-10, SEC-11, SEC-12, SEC-14, SEC-16, SEC-17, SEC-22 should be scheduled; Info
items are recorded for the backlog/LLD.

Overlap with the QA agent's independent run (`docs/evidence/qa-stage1-2026-11-01.md`, written concurrently): QA also
reached SEC-04, SEC-06 (RFQ item bounds), SEC-13 and SEC-22 through functional tests; the two reports were produced
without coordination, which is why the conclusions corroborate each other.

## 5. Checks that passed (evidence)

| Area (ASVS / abuse case) | Evidence |
|---|---|
| Cross-tenant read/write/download/approve (V4.2.1, abuse 5) | `test_tenant_isolation.py::test_cross_tenant_objects_are_404` (18 routes + files + ids in payloads) and `test_audit_followups.py::test_cross_tenant_remaining_routes_are_404` (submit, reject, margin-override, RFQ item create, member PATCH ×2, customer id in JSON and multipart RFQ, quote filter, foreign product id via admin, foreign line id re-use, members list) → all 404 / no effect; dynamic cross-tenant customer GET through the proxy → 404 |
| RLS (B3/B7) | 15 tables `relrowsecurity && relforcerowsecurity` with policies; raw SQL without filters sees only the current tenant; INSERT for another org refused; `rolbypassrls=f`; audit table: no UPDATE/DELETE/TRUNCATE privilege, cross-org INSERT refused (`test_rls_enabled_and_forced_on_every_tenant_table`, `test_audit_table_is_append_only_and_tenant_bound`, existing `test_rls_blocks_raw_sql_without_service_filters`) — except `users`/`sessions` (SEC-02) |
| Vertical RBAC (V4.1.3, abuse 6) | existing matrix test + `test_member_role_escalation_guards` (admin cannot create/promote owner, cannot demote/deactivate last owner, sales/approver cannot touch memberships incl. their own, only owner enables self-approval), `test_sales_cannot_override_margin_through_any_route` (sales/approver 403, admin needs ≥5-char reason, later price edit resets the override) |
| Sessions (V3) | `test_session_rotation_expiry_and_deactivation`: new token per login, attacker-supplied cookie not adopted, idle (12 h) and absolute (7 d) expiry enforced, deactivation revokes sessions and blocks login; cookie flags `HttpOnly; SameSite=lax; Path=/` observed through the proxy (`Secure` correctly derived off in `APP_ENV=test`) ; logout revocation (existing tests) |
| CSRF (abuse 9) | missing/wrong header → 403; token of user A with cookies of user B → 403; pre-login token after login → 403; cross-site-style POST with `Origin: evil` and cookies but no header → 403 through the proxy; `Connection: X-CSRF-Token` / `Connection: Cookie` tricks do not make the proxy drop the headers |
| Proxy path handling / SSRF | `/api/v1/../health`, `..%2F`, `%2e%2e`, `quotes%2F..%2F`, `..;/`, embedded absolute URL, `%00` → all 404 from the API; target origin is fixed (`API_INTERNAL_URL`), segments are `encodeURIComponent`-ed; `X-Request-ID` with invalid characters replaced by a UUID, valid ids echoed |
| Files (V12, abuse 1, 2, 4) | polyglot/mismatched uploads (PDF-in-.csv, zip-in-.pdf, text-in-.xlsx, no extension, .html, .svg, .csv.exe) → 415 on RFQ and catalog import; `%PDF-`+garbage → clean `failed` status without internals; 10 MiB+1 → 413; empty → 422; zip bomb, corrupted, JS-PDF, macro .xlsm, scanned PDF (existing tests); `keep_links=False`/`data_only=True` for XLSX; hostile original filename sanitised in `Content-Disposition`, `application/octet-stream` + `nosniff` + `no-store`; `LocalStorage._path` rejects traversal |
| Injection (V5.3, abuse 3, 7) | SQL/LIKE/Unicode/template payloads in `q`, `status`, `customer_id`, path ids → 200/422, no error text; CSV export neutralisation and catalog `formula_like` flags (existing tests); request-id sanitised; PDF strings `escape()`d before ReportLab markup |
| Pricing (X-06) | client cannot set `line_total`/`cost_price`/`margin_override_reason`/`price_source` (ignored); NaN/Infinity/sNaN/negative/hex/comma decimals → 422 on quote lines and RFQ items; lock-version replay → 409; currency mismatch never auto-prices and blocks submission (generate and PATCH paths) |
| Concurrency (abuse: double approve) | same-lock double approve (HTTP, threads, 3 rounds) → exactly one 200/409 pair and one decision row; guessed N+1 lock at service level → `ConflictError` |
| Config (V14) | production refuses the dev `SECRET_KEY`; `min_length=32`; `secure_cookies` derivation; CORS: unknown origin gets no `Access-Control-Allow-Origin`, allow-listed origin does; HSTS only when secure cookies; API CSP `default-src 'none'`; no secrets / `NEXT_PUBLIC_` in the bundle |
| LLM (OWASP LLM01/02/05/06, abuse 2) | provider `disabled` by default and **not imported by any router or non-AI service** (no AI-driven action exists in Stage 1); `parse_proposal` drops non-JSON, negative/NaN/oversized quantities, >500 lines, and never exposes unknown keys such as `unit_price`; `redact_for_model` removes credentials/card numbers and bounds input; hidden-prompt PDF and pasted injection text are stored as data and change no price/status (existing + new tests) |

## 6. Residual risks

| Risk | Status / owner |
|---|---|
| SEC-01/02 (membership & global `users` model, no RLS on `users`/`sessions`) | Open — implementer; blocks the Stage 1 gate until fixed or risk-accepted by a human. SEC-03 fixed-WT, pending commit |
| SEC-09 open redirect, SEC-05 XFF handling, SEC-22 expired export | Open — implementer. SEC-04 fixed-WT, pending commit |
| In-process login limiter (per worker, per replica); no limiter on other endpoints (uploads/parses are CPU/memory heavy; no concurrency cap on parser subprocesses) | Known limitation + SEC-19/21; Redis limiter planned for Stage 2 |
| Secrets scanning (gitleaks), semgrep, CodeQL run only in CI; CI results were not observed in this audit | Verify in CI before the gate |
| Dev-tooling npm advisories (16 moderate, `minimatch`) | Not shipped; upgrade when `eslint-config-next` allows |
| Encryption at rest, TLS/HSTS at the reverse proxy, backups, WAF | Deployment responsibility (runbook) |
| Human verification is the control for custom lines priced manually (a sales user can relabel a catalog item as "custom" and price it below the floor; `price_source=manual`, `is_custom=true` are visible to the approver) | Design decision — recommend a blocker when a custom line's SKU matches the active catalog |

## 7. Explicitly NOT tested

- Docker Compose topology (`infra/docker-compose.yml`) end-to-end — no daemon; conclusions about Docker behaviour (SEC-05, SEC-21) come from reading the Dockerfile/compose and uvicorn defaults.
- S3 storage backend (`S3Storage`) — not exercised (local backend only).
- Live hosted LLM provider (`AnthropicProvider`) — no key, no spend; only parser/redaction logic tested.
- HTTP request smuggling / desync between Next's proxy and uvicorn, HTTP/2, WebSocket paths.
- Sustained load / DoS beyond single large uploads and the 20 s parser timeout; memory behaviour under many concurrent parses.
- Browser-side XSS beyond the existing E2E fixture (grep shows no `dangerouslySetInnerHTML`, `eval`, or dynamic `href` from user data; React escaping is relied upon).
- gitleaks / semgrep / trufflehog / CodeQL (not available locally); git history secret scan therefore not performed by the auditor.
- Playwright E2E suite (`tests/e2e`) was not re-run by the auditor (one ad-hoc Chromium script only).
- Multi-worker / multi-replica races across processes (single uvicorn worker used).
- Password breach checks, MFA, SSO — out of Stage 1 scope.
- mypy on the new test file (project runs mypy on `app` only).

## 8. Run log (verbatim tail of the recorded commands)

```
$ .venv/bin/pytest -q tests/security/test_audit_followups.py   (@70351b7)   → 26 passed, 10 xfailed in 56.97s
$ .venv/bin/pytest -q tests/security/test_audit_followups.py   (working tree) → 32 passed, 7 xfailed in 57.64s
$ .venv/bin/pytest -q -m security                               (working tree) → 45 passed, 188 deselected, 7 xfailed in 68.44s (exit 0)
$ .venv/bin/pytest -q tests/unit tests/integration tests/security (working tree) → 136 passed, 7 xfailed in 132.67s (exit 0)
$ .venv/bin/pytest -q                                           (working tree, incl. tests/qa) → 13 failed, 214 passed, 10 xfailed; all 13 in tests/qa
$ .venv/bin/pip-audit --skip-editable            → No known vulnerabilities found (exit 0)
$ .venv/bin/bandit -r app -q -f txt -ll          → (no output) exit 0
$ .venv/bin/ruff check app --output-format concise → All checks passed! (exit 0)
$ npm audit --omit=dev                           → found 0 vulnerabilities (exit 0)
$ npm audit                                      → 16 moderate severity vulnerabilities (exit 1)
gitleaks: NOT AVAILABLE  semgrep: NOT AVAILABLE  trufflehog: NOT AVAILABLE
$ (direct :8041) 14 failed logins rotating X-Forwarded-For → 401 ×14 (no 429); fixed XFF → 429 from the 11th
$ (proxy :3041) POST /customers with X-Forwarded-For: 203.0.113.9 → audit_events.ip = 203.0.113.9
$ (proxy :3041) traversal probes (/api/v1/../health, ..%2F, %2e%2e, ..;/, embedded URL, %00) → all 404 from the API
$ (proxy :3041) POST /customers, cookies + Origin: http://evil.example, no X-CSRF-Token → 403 ; with token → 201
$ (proxy :3041) readonly GET /quotes/{id}/versions/1/pdf → 200, quote.export_pdf audit rows 0 → 1
$ (proxy :3041) GET /quotes?q=%00 → 500 {"error":"internal_error","detail":"Unexpected error","request_id":"-"}, no X-Request-ID header
$ (debug test) approved quote with valid_until = yesterday: effective_status=expired, pdf GET 200, csv 200, pdf POST 200
$ timing.py → unknown email median 9.6 ms (n=12); known email median 87.3 ms (n=15)
$ redirect.cjs → "/\evil.com" ⇒ external request http://evil.com/ ; "//evil.com" ⇒ /dashboard ; "/dashboard" ⇒ /dashboard
```
