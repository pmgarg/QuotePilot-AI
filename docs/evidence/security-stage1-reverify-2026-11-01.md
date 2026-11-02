# Security re-verification evidence — Stage 1 after fix rounds 1+2 (2026-11-01)

Auditor: security-auditor subagent (independent of the implementer). Repository `QuotePilot-AI`, branch
`claude/multi-stage-project-impl-8hz2wj`, **HEAD `ff99762`** ("Stage 1 fix round 2"). Product code was **not**
modified by the auditor; only `apps/api/tests/security/test_audit_followups2.py` and this document were added.

This document re-verifies the predecessor audit (`security-stage1-2026-11-01.md`, SEC-01…SEC-22, written against
`70351b7`/`7a29d68`) and the independent release review (`release-review-1-stage1-2026-11-01.md`, H-1/H-2,
M-1…M-5, L-1…L-10) against the code actually at `ff99762`. It states what was tested, how, and what was found. It does
not claim the absence of vulnerabilities.

**Inputs that were not available:** the task named `docs/evidence/implementer-fixes2-stage1-2026-11-01.md`; that file
does not exist in the repository. The round-2 fix claims were taken from the commit message of `ff99762`, from
`apps/api/tests/integration/test_review1_fixes.py` and from the diff itself.

**Concurrent working-tree changes observed (not the auditor's):** `scripts/revoke_sessions.py` (uncommitted rewrite of
two SQL strings, see NEW-3/G-1), `apps/api/tests/qa/test_qa_reverify_review1.py`, `tests/e2e/specs/qa-reverify-review1.spec.ts`
and `docs/evidence/qa-stage1-reverify-2026-11-01.md` appeared while this audit ran (QA re-verification). Findings below
were checked against `ff99762` and re-checked against the working tree where the file differed.

## 1. Scope

| In scope | Method |
|---|---|
| RLS migrations `0002`/`0003` (users/sessions SELECT/INSERT/UPDATE split, WITH CHECK, ENABLE vs FORCE), `app/db/session.py`, `app/services/auth.py`, `app/api/deps.py`, `routers/org.py`, `routers/quotes.py`, `services/quotes.py`, `services/exports/quote_exports.py`, `services/parsing/{sandbox,worker}.py`, `services/rfqs.py`, `core/config.py`, `main.py`, `app/fixtures/seed.py`, `apps/api/Dockerfile`, `infra/docker-compose.yml`, `apps/web/src/app/api/[...path]/route.ts`, `apps/web/src/app/login/page.tsx`, `scripts/revoke_sessions.py` | Code review of every file above; black-box pytest against a dedicated PostgreSQL 16 database (`quotepilot_sec`, migrated `base → head`); dynamic probing of `uvicorn :8041` (bound to `0.0.0.0` so that a **non-loopback peer** could be simulated via the container's own interface `192.0.2.2`), the Next proxy `:3041`, two extra Next instances (`:3042` trust=false, `:3043` trust=true) in front of a header-echo server on `:8042`; Chromium (Playwright) for the login redirect; raw SQL as the runtime role `quotepilot_app` and the owner role |
| Scanners | pip-audit, bandit, ruff (project + `S*`), npm audit; **gitleaks / semgrep / trufflehog are CI-only** (not installed locally) — the CI `security` workflow result for `ff99762` was read with `gh run view` (§ 4) |

Checklists: OWASP ASVS 5.0 V2/V3/V4/V5/V12/V14, OWASP LLM Top 10 2025 (consent gate only — no AI route exists),
`docs/THREAT_MODEL.md` abuse cases 5, 6, 8, 9. Only the local application built from this repository was tested.

## 2. Environment

| Item | Value |
|---|---|
| Host | Claude Code cloud container, Ubuntu 24.04, Python 3.13 (`apps/api/.venv`), Node 22.23, PostgreSQL 16.15 (local service), uvicorn 0.54.0 |
| Database | `quotepilot_sec`: `alembic downgrade base && alembic upgrade head` → `0001 → 0002 → 0003` OK. Roles: `quotepilot_app` and `quotepilot_owner` both `rolsuper=f rolbypassrls=f`; `quotepilot_owner` owns the tables |
| Env | `APP_ENV=test DATABASE_URL=…quotepilot_app…/quotepilot_sec DATABASE_ADMIN_URL=…quotepilot_owner…/quotepilot_sec`, `SECRET_KEY` set, `DEMO_PASSWORD` ≥ 12 chars, `STORAGE_DIR` in the scratchpad |
| Servers | `uvicorn app.main:app --host 0.0.0.0 --port 8041` (no `--forwarded-allow-ips`, i.e. uvicorn default = trust `127.0.0.1`); `npx next start --port 3041` with `API_INTERNAL_URL=http://127.0.0.1:8041` (`.next` build `xtLHn7vO…` is newer than every changed web source file); all stopped after the probes |
| Not available | Docker daemon (compose/Dockerfile not executed), gitleaks, semgrep, trufflehog, external network |

## 3. Per-finding status

Status values: **FIXED** (verified at `ff99762`), **PARTIAL** (fixed for the reported case, adjacent gap found),
**OPEN**, **ACCEPTED** (documented risk acceptance in KNOWN_LIMITATIONS; not a human-signed acceptance), **GATE**
(process item). "Evidence" names the test (`T1` = `test_audit_followups.py`, `T2` = `test_audit_followups2.py`,
`R1` = `tests/integration/test_review1_fixes.py`) or the probe in § 8.

| ID | Previous sev. | Status | Evidence |
|---|---|---|---|
| SEC-01 | High | **FIXED** | `POST /org/members` with another tenant's e-mail → 409 with the same body as for an own existing e-mail (§ 8 probe); `PATCH /org/members/{id}` on all 5 tenant-A ids from tenant B → 404; `memberships.is_active` is per tenant: a genuinely shared account (membership inserted in both tenants) keeps its tenant-A session and login after tenant B deactivates it; role change / deactivation in B revokes only the B session (`T2::test_sec01_shared_account_…`); `users.is_active` untouched. Residual: e-mail existence oracle (L-2, ACCEPTED) |
| SEC-02 | Medium | **FIXED** | `users`/`sessions` have RLS (`ENABLE`, not `FORCE`; runtime role is not the owner, so it is always subject to it). Raw SQL as `quotepilot_app`: no context → 0 users / 0 sessions; tenant-A context → only A's 6 users, sessions of exactly 1 org; `app.login_email` → exactly 1 row, `%` → 0 rows, case-insensitive (citext) (§ 8, `T2::test_l1_…`, `R1::test_l1_…`). Owner role without context sees all rows (12 users, 25 sessions) — by design (`ENABLE` only), documented in migration 0002 |
| SEC-03 | Medium (fixed-WT) | **FIXED** | committed; `T1::test_admin_cannot_deactivate_an_owner_even_when_two_owners_exist` passes. New asymmetry NEW-6 |
| SEC-04 | Medium (fixed-WT) | **FIXED** | committed; `T1::test_withdrawn_quote_cannot_be_approved` passes |
| SEC-05 | Medium | **FIXED** (with topology caveats) | Through `:3041` the proxy drops `X-Forwarded-For`, `X-Real-IP`, `Forwarded`, `X-Forwarded-Host` (echo server on `:8042`, § 8); audit `ip` of a request with spoofed XFF through the proxy = `127.0.0.1`; 12 failed logins rotating XFF through the proxy → 429 from the 11th. Direct to the API from a **non-loopback peer** (`192.0.2.2`) spoofed XFF is ignored (audit ip `192.0.2.2`, 429 from the 11th); from **loopback** uvicorn trusts XFF by default (audit ip `203.0.113.9`, no 429 in 12 attempts) — expected uvicorn behaviour, matters only when the proxy and API share a host (`make dev`). `T2::test_m2_…` pins both behaviours with `ProxyHeadersMiddleware(trusted_hosts="127.0.0.1")`. Caveats: NEW-2 (container CMD), NEW-9 (runbook `FORWARDED_ALLOW_IPS` guidance), NEW-8 (`X-Forwarded-Proto`) |
| SEC-06 | Low | **FIXED** | quantity `1e-400`, `0.0001`, `1e9`, price `1e-7` → 422 (schema); `1e7 × 1e7`, five lines `5e6 × 1e7`, two lines `9.9e6 × 9.9e6` → 422 "totals exceed the supported maximum" (L-4 cap `MAX_TOTAL = 1e14`); no 500 in the API log for the whole session (§ 8) |
| SEC-07 | Info | closed | unchanged |
| SEC-08 | Low | **FIXED** | `q=%00`, `status=%00`, `q=a%00b` on quotes/customers/rfqs/catalog products → 200 (`T2::test_sec08_…`, § 8) |
| SEC-09 | Medium | **PARTIAL → reopened as NEW-7** | `/\evil.com`, `//evil.com`, `/%5Cevil.com`, `/%2F%2Fevil.com`, `https://…`, `javascript:` and 20 more encodings are blocked (§ 8 `safenext.mjs`), **but** `next=/..//evil.com` (and `/%2e%2e/%2e%2e//evil.com`, `/quotes/..//evil.com/x`) redirects to `http://evil.com/` in Chromium (§ 8 `redirect2.cjs`) |
| SEC-10 | Low | **FIXED** | medians against `:8041` (n = 9 each, interleaved, under the limiter budget): unknown e-mail **70.3 ms**, known e-mail + wrong password **66.9 ms** (previously 9.6 vs 87.3 ms). Not measured under load |
| SEC-11 | Low | **FIXED** (code review) | `update_draft` sets `cost_price = None` when `product.currency != quote.currency`; `generate_from_rfq` unchanged. Not re-tested dynamically |
| SEC-12 | Low | **ACCEPTED** | KNOWN_LIMITATIONS: `GET …/pdf` still audits/stores; approved PDFs are now written once and served from storage (idempotent) |
| SEC-13 | Info | **FIXED** | logout without / with wrong `X-CSRF-Token` → 403, with token → 200 and `/auth/me` → 401 (`T2::test_sec13_…`, § 8) |
| SEC-14 | Low | **ACCEPTED** | `script-src 'unsafe-inline'` documented |
| SEC-15 | Info | **FIXED** (code review) | `openapi_url=None` in production; `--no-server-header` in the Dockerfile CMD — but see NEW-2 (the CMD as written is not exec form) |
| SEC-16 | Low | **FIXED** (code review) | unhandled handler reads `request.state.request_id` and sets `X-Request-ID`; no 500 could be provoked to re-test dynamically |
| SEC-17 | Low | **FIXED** (code review) | `quote.update` audit carries `line_changes` (price/discount/qty/product before→after, max 50) |
| SEC-18 | Info | FIXED (code review) | `formula_like` flag for pasted lines; not re-tested |
| SEC-19 | Info | **FIXED** | worker reports `limits_applied`, parent logs a warning; `BoundedSemaphore(4)`: 12 concurrent PDF uploads completed in 3 waves (0.3 s / 1.1 s / 2.6 s), all `parsed`, health unaffected (§ 8). Residual NEW-12 |
| SEC-20 / L-9 | Info | **FIXED** | `rfqs.py::_products_for` and `generate_from_rfq` filter on `organization_id` explicitly |
| SEC-21 | Info | **ACCEPTED** | documented |
| SEC-22 | Low | **FIXED** | approved version whose **frozen** `document_snapshot.valid_until` is in the past → PDF GET/POST and CSV 409 (§ 8, `R1::test_h1_expiry_judged_on_frozen_validity`) |
| H-1 | High | **FIXED** (with NEW-1 gap) | approve → v1 PDF stored (`pdf_sha256` set at approval) → Sales creates v2, sets `valid_until=2099-12-31` and notes, owner renames org, Sales renames customer → v1 PDF **byte-identical**, CSV identical, no "2099"/"Renamed"/"TAMPERED" inside; `POST …/1/pdf` returns the stored SHA; v2 draft PDF shows live data (§ 8). Withdrawn → v1 export 409. **But** a later new version revives the withdrawn v1 document (NEW-1) |
| H-2 | High (gate) | **GATE — still red** | `gh run list --commit ff99762`: `ci` **success** (41356139188), `security` **failure** (41356139143): semgrep blocking rule `python.sqlalchemy.security.audit.avoid-sqlalchemy-text` ×2 on `scripts/revoke_sessions.py`. See G-1 |
| M-1 | Medium | **FIXED** | key built from `email.strip().lower()`; `R1::test_m1_…` passes; per-account bucket (5× budget) added — see NEW-12 for the trade-off |
| M-2 | Medium | **FIXED** (see SEC-05 caveats) | Dockerfile no longer passes `--forwarded-allow-ips "*"`; compose no longer publishes the API port (`expose` only); web proxy trust is opt-in. NEW-2 and NEW-9 remain |
| M-3 | Medium | **FIXED** | Sales/read-only: detail and submit-409 messages are "Line 1 price is below the organisation's minimum margin policy" (no `%`, no floor); approver sees "margin -6017.65% is below the organisation floor of 12.00%" (§ 8, `R1::test_m3_…`); UI renders `b.message` only |
| M-4 | Medium | **PARTIAL → NEW-4** | typed-over catalog price and custom line both raise `manual_price_unconfirmed`; sales/approver/read-only → 403 on `price-override` **and** the hidden `margin-override` alias, tenant-B admin → 404; reason < 5 chars → 422; admin clears; re-pricing by Sales clears the override and re-blocks (`T2::test_m4_…`, § 8). Bypass: a line can carry `price_source=catalog` without a product (NEW-4) |
| M-5 | process | this document | — |
| L-1 | Low | **FIXED** (NEW-5 residual) | policies split per command; `users_insert` needs an org context, `users_update` only the acting user (UPDATE of other members / all hashes → 0 rows, DELETE → 0 rows: no policy); `sessions_insert` requires `user_id = acting user AND organization_id = current org` (cross-org insert → RLS error); tenant B cannot revoke A's sessions (0 rows); login scope cannot UPDATE (0 rows) (`T2::test_l1_…`). Nothing in the app updates `users` rows at all (grep: only inserts in `org.py` and `seed.py`); seed works with the app role because it binds `app.current_org`/`app.login_email` |
| L-2 | Low | **ACCEPTED** | documented (409 oracle) |
| L-3 | Low | **FIXED** | `SELECT … FOR UPDATE` on the organisation row; `R1::test_l3_…` passes |
| L-4 | Low | **FIXED** | see SEC-06 |
| L-5 | Low | **FIXED** | `R1::test_l5_…` passes (RFQ `too_large` failed state with no items; catalog 413) |
| L-6 | Low | **ACCEPTED** | documented (deployment isolation) |
| L-7 | Low | **PARTIAL** | browser regression for SEC-09 (5 variants) and H-1/M-3 exist; `T2` adds the uvicorn-side XFF test; **no automated test of the web proxy's header dropping** exists (only this audit's echo-server probe) and the new NEW-7 variant is missing from the e2e list |
| L-8 | Low | **PARTIAL** | `scripts/revoke_sessions.py` exists but `--org` fails (NEW-3); runbook's `FORWARDED_ALLOW_IPS=<web/ingress ip>` is incomplete for uvicorn 0.54 (NEW-9); `org.update` audit for `ai_processing_consent` records `before: {}` (NEW-10) |
| L-9 | Low | **FIXED** | see SEC-20 |
| L-10 | Low | **FIXED** (heuristic) | `staging`/`production` refuse marker strings and < 12 distinct characters (`R1::test_l10_…`); `abcdefghijkl`×3 and `0123456789ab`×3 are accepted (NEW-11, Info) |

## 4. New findings

Severity per SECURITY_POLICY (CVSS v3.1 bands, impact × exploitability in the documented topologies). All
reproductions used synthetic accounts on localhost.

| ID | Sev | Title | Location | Reproduction | Impact | Remediation |
|---|---|---|---|---|---|---|
| NEW-1 | **Medium** | Withdrawn approved document is revived by creating a new version | `apps/api/app/services/quotes.py:777-778` (`new_version` sets `quote.status="draft"`, `withdrawn_reason=None`), `services/exports/quote_exports.py:93-97` (`_check_exportable` only consults `quote.status == "withdrawn"`) | `T2::test_new1_…` (xfail strict): approve v1 → owner `POST /withdraw` (200; v1 PDF → **409**) → Sales `POST /versions` (201, allowed for `EDIT_QUOTES`) → `GET /versions/1/pdf`, `/csv`, `POST /versions/1/pdf` → **200**, label `APPROVED` (from the snapshot), `withdrawn_reason` gone from the quote | A withdrawal by owner/admin/approver (e.g. "pricing error — do not send") is silently undone for export purposes by any Sales user; S1-F15 "withdrawn quotes cannot be exported as current" and the H-1 intent are violated. Audit trail (`approval_decisions`) still records the withdrawal | Record the withdrawal on the version (`withdrawn_at`/status `withdrawn` on the approved version, as is already done for pending versions via `superseded`) and have `_check_exportable` refuse it regardless of the quote header; keep `withdrawn_reason` on the version |
| NEW-2 | **Medium** (availability of the documented Docker topology) | Dockerfile `CMD` is no longer valid exec form: trailing `# proxy headers …` comment is part of the instruction | `apps/api/Dockerfile:15` (introduced in `ff99762`) | `T2::test_new2_dockerfile_cmd_is_valid_exec_form` (xfail strict): `json.loads` of the CMD remainder fails. Per the Dockerfile reference a `#` outside column 0 is an argument, and a non-JSON CMD is run as `/bin/sh -c '["uvicorn", …]  # …'`, which cannot execute. **Not executed** (no Docker daemon) | The `api`/`migrate` images built from this Dockerfile are expected not to start (`sh: ["uvicorn",: not found`), which also voids `--no-server-header`, the worker count and the proxy-header defaults the fix round relied on. Compose "written and linted but not executed" (KNOWN_LIMITATIONS) hid this | Move the comment to its own line above `CMD`; add a CI step that builds the image and runs the health check (or at least a Dockerfile lint such as hadolint) |
| NEW-3 | **Medium** (incident response) | Operator kill switch `scripts/revoke_sessions.py --org <id>` fails and rolls back | `scripts/revoke_sessions.py:52-58` (audit `INSERT … organization_id = :o` as the owner role; `audit_events` is `FORCE ROW LEVEL SECURITY`, policy `WITH CHECK (organization_id IS NULL OR organization_id = app.current_org)`, no context is bound) | `T2::test_new3_…` (xfail strict) and § 8: `--org … --yes` → `InsufficientPrivilege: new row violates row-level security policy for table "audit_events"`, exit 1, **0 sessions revoked** (whole transaction rolled back); `--user-email` and `--all` work (`T2::test_revoke_sessions_script_user_and_all_scopes_work`) | The runbook's documented per-tenant containment action does not work; an operator under incident pressure would have to fall back to `--all` (platform-wide logout) or hand-written SQL | `SELECT set_config('app.current_org', :o, false)` before the audit insert (or insert with `organization_id = NULL` and the org id in `summary`); add the subprocess test to CI |
| NEW-4 | **Medium** | M-4 bypass: a "custom" line can carry `price_source = catalog` (no confirmation) after relabelling | `apps/api/app/services/quotes.py:451-454` (`is_custom=True` clears `product_id` but keeps `price_source`, `unit_price`, `cost_price`), `:438` (SKU may be rewritten once `product_id` is `None`) | `T2::test_new4_…` (xfail strict): Sales PATCH 1 `{product_id: <cheapest SKU>, is_custom: true, description: "Compressor 90 kW"}` → line `product_id=None, is_custom=true, price_source=catalog, unit_price=0.85`; PATCH 2 `{sku: "COMP-900"}` → `blockers == []`, submit → **200** | A Sales user prices an arbitrary custom item at any catalog product's list price with no `manual_price_unconfirmed` blocker and a margin computed against the wrong product's cost; the approver sees a custom line labelled "catalog". This is the "relabel a catalog item as custom" residual from the first audit, now contradicting the adopted M-4 policy | When `is_custom` becomes true or `product_id` is cleared, set `price_source="manual"` (and `cost_price=None`); treat `price_source="catalog" and product_id is None` as a blocker |
| NEW-5 | Low (defence in depth) | `sessions_update` policy allows re-pointing a session to another user/org; app role has full-row UPDATE on `users`/`sessions` | `alembic/versions/20261101_0003_review1_fixes.py` (`sessions_update … WITH CHECK (SESSIONS_SELECT)`), `0001` grants (`GRANT … UPDATE, DELETE ON ALL TABLES`) | `T2::test_new5_…` (xfail strict) and § 8: with `app.current_org=A, app.current_user=sales`, `UPDATE sessions SET user_id = <owner> WHERE token_hash = <mine>` → **UPDATE 1** (the token now authenticates as the owner); `UPDATE sessions SET organization_id = B WHERE token_hash = <mine>` → UPDATE 1 (then 401, no membership) | Not exploitable through the API (parameterised queries only; no SQL injection found). If an injection or ORM bug ever appears, in-tenant privilege escalation via the session table is one statement away | Column-level privileges: `REVOKE UPDATE ON sessions FROM quotepilot_app; GRANT UPDATE (revoked_at, last_seen_at) ON sessions TO quotepilot_app;` and `REVOKE UPDATE, DELETE ON users FROM quotepilot_app` (the app never updates `users`); optionally a `BEFORE UPDATE` trigger rejecting identity-column changes |
| NEW-6 | Low | Admin may re-activate an owner that an owner deactivated | `apps/api/app/api/routers/org.py:149-157` (only `is_active=false` on an owner is owner-gated) | `T2::test_new6_…` (xfail strict) and § 8: owner creates owner2 → owner deactivates (200) → **admin** `PATCH {is_active: true}` → **200** | Hierarchy asymmetry (ASVS V4.2.2): an admin can restore owner-level access removed by an owner (e.g. after an offboarding) | Gate every mutation of an owner membership on `principal.role == "owner"` |
| NEW-7 | **Medium** | Open redirect after login survives the SEC-09 fix via dot-segment normalisation | `apps/web/src/app/login/page.tsx:20` (`u.pathname + u.search` after WHATWG normalisation of `/..//evil.com` is `//evil.com`, which `router.replace` treats as protocol-relative) | § 8 `redirect2.cjs` (Chromium): `/login?next=%2F..%2F%2Fevil.com`, sign in → request to **`http://evil.com/`**; same for `/%2e%2e/%2e%2e//evil.com` and `/quotes/..//evil.com/x`; `/\evil.com`, `//evil.com`, `/dashboard` behave correctly. `safenext.mjs` (port of the function) shows the returned value is `//evil.com` | Phishing after authentication on the real domain (same impact as SEC-09) | Validate the **normalised** result: reject when `u.pathname.startsWith("//")` or contains `\`; simplest: `const target = new URL(u.pathname + u.search, origin); if (target.origin !== origin) return "/dashboard"`; add the three variants to `review1-regressions.spec.ts` |
| NEW-8 | Info | `X-Forwarded-Proto` is client-controlled through the proxy even with `WEB_TRUST_FORWARDED_HEADERS=false` | `route.ts:34` sets it from `req.nextUrl.protocol`, which Next derives from the inbound `X-Forwarded-Proto` | § 8: `:3042` (trust=false) with `X-Forwarded-Proto: https` → upstream receives `https`; without the header → `http` | No consumer today (`secure_cookies` is env-derived; uvicorn only honours it from a trusted peer). Would matter if scheme-based logic (HSTS, redirects) is added | Set it from the listener (`https` only when the deployment terminates TLS in front), not from `nextUrl` |
| NEW-9 | Info (docs) | Runbook guidance for `FORWARDED_ALLOW_IPS` is incomplete for uvicorn ≥ 0.30 semantics | `docs/OPERATIONS_RUNBOOK.md:22`, `apps/api/Dockerfile:15` comment | uvicorn 0.54 `get_trusted_client_address` walks `X-Forwarded-For` **right-to-left and returns the first untrusted hop**. With `FORWARDED_ALLOW_IPS=<web ip>` only and a TLS proxy that appends its own entry, every request is attributed to the TLS proxy's IP (one shared limiter bucket); with trust=false in the web proxy the API never sees the client IP at all (XFF is dropped, not rewritten) | Mis-set deployments silently degrade the login limiter to one bucket for all users (the state KNOWN_LIMITATIONS already describes for compose) | Document: list **every** hop (TLS proxy and web container); have the web proxy, when trusted, overwrite XFF with the socket address rather than forward it; add a deployment smoke check that the audit `ip` differs between two client addresses |
| NEW-10 | Info | `org.update` audit for AI consent has `before: {}` | `routers/org.py:48-61` (`ai_processing_consent` is not an attribute, so `before` skips it) | § 8: audit summary `{"after": {"ai_processing_consent": true}, "before": {}, "fields": […]}` | Consent withdrawal/grant history cannot be reconstructed from the event alone (X-05/X-07) | Record `ai_processing_consented_at/by` before and after |
| NEW-11 | Info | Weak-secret heuristic accepts low-entropy repeated keys | `core/config.py:88-90` | `APP_ENV=staging SECRET_KEY=abcdefghijklabcdefghijklabcdefghijkl` → accepted; `aaaa…`, `password-…`, `dev-only-…` → refused (§ 8) | Heuristic, not a guarantee; operators can still deploy a guessable key | Document as a guard rail; consider a Shannon-entropy threshold |
| NEW-12 | Info (trade-off) | Per-account limiter (50 failures / 5 min across all IPs) is an unauthenticated lockout primitive; parser slots can hold request threads | `services/auth.py:52-54`, `parsing/sandbox.py:49` (`acquire(timeout=parse_timeout_seconds)`) | design review; semaphore behaviour observed (§ 8) | Anyone can lock any known e-mail for 5 min with 50 requests from distributed IPs (M-1 intentionally added this bucket); parse requests beyond 4 wait up to 20 s each on an anyio worker thread (default pool 40) before 20 s of parsing — sustained slow-PDF uploads from one account could starve other tenants' requests (not reproduced under load) | Document the lockout trade-off; return `429 busy` immediately (or after a short wait) instead of blocking for the full timeout; per-user upload rate limit |

### Gate items (process)

| ID | Item |
|---|---|
| G-1 | CI `security` workflow is **red** on `ff99762` (semgrep `avoid-sqlalchemy-text` ×2 in `scripts/revoke_sessions.py`); the uncommitted working-tree rewrite of that script removes the string concatenation but has not been pushed/re-run. Per SECURITY_POLICY every PR needs the security scans green |
| G-2 | `docs/evidence/implementer-fixes2-stage1-2026-11-01.md` referenced by the orchestration does not exist; fix-round-2 claims are only in the commit message |
| G-3 | NEW-1, NEW-4 and NEW-7 re-open the substance of H-1, M-4 and SEC-09 respectively; NEW-2 and NEW-3 break controls the fix round introduced. High/Medium findings need a fix or a human-signed acceptance before the Stage 1 gate |

## 5. Tool results (command, exit code, summary)

| Tool | Command (cwd) | Exit | Result |
|---|---|---|---|
| pip-audit | `.venv/bin/pip-audit --skip-editable` (apps/api) | 0 | `No known vulnerabilities found` (editable `quotepilot-api` skipped) |
| bandit | `.venv/bin/bandit -r app -q -f txt -ll` (apps/api) | 0 | no medium/high findings |
| ruff (project rules) | `.venv/bin/ruff check app --output-format concise` | 0 | `All checks passed!` |
| ruff S-only | `.venv/bin/ruff check app --select S` | 1 | 3 × `S101` (asserts in `catalog_import.py:358`, `rfqs.py:179`, …) — excluded by project config; no other S rule |
| ruff on scripts | `.venv/bin/ruff check ../../scripts` / `--select S` | 1 / 0 | 7 style hits (`E501` in `make_fixtures.py`, not security); S rules clean |
| ruff on new test | `ruff format` + `ruff check tests/security/test_audit_followups2.py` | 0 | clean |
| npm audit (prod) | `npm audit --omit=dev` (apps/web) | 0 | `found 0 vulnerabilities` |
| npm audit (all) | `npm audit` (apps/web) | 1 | 16 moderate, all transitive `minimatch` (GHSA-g3hv-75wg-m249) via eslint tooling — unchanged |
| gitleaks / semgrep / trufflehog | — | — | **NOT AVAILABLE locally.** CI (`gh run view 41356139143`): gitleaks ✓, CodeQL python ✓, CodeQL js/ts ✓, pip-audit ✓, bandit ✓, ruff ✓, npm audit ✓, **semgrep ✗** (2 blocking findings, see G-1) |
| Docker | — | — | **NOT AVAILABLE** (binary present, no daemon); Dockerfile/compose reviewed only |

## 6. Residual risks

| Risk | Status / owner |
|---|---|
| NEW-1 (withdrawn document revived), NEW-4 (M-4 bypass), NEW-7 (open redirect variant) — Medium | Open — implementer; strict-xfail tests in `T2` for NEW-1/NEW-4; browser repro script for NEW-7 (§ 8) to be added to `review1-regressions.spec.ts` |
| NEW-2 (Dockerfile CMD), NEW-3 (`--org` kill switch) — Medium, operational | Open — implementer; `T2` tests; add an image build + health check to CI |
| NEW-5 (sessions/users column privileges), NEW-6 (owner re-activation) — Low | Open — schedule; `T2` tests |
| Login limiter still in-process; per-account bucket is a lockout primitive; shared bucket in the compose topology | Known limitation / NEW-12; Redis limiter planned (Stage 2) |
| Forwarded-header configuration is deployment-sensitive (NEW-8/NEW-9) | Runbook update; smoke check at deployment |
| Secrets scanning, semgrep, CodeQL run only in CI; semgrep currently blocks | G-1 |
| Dev-tooling npm advisories (`minimatch`) | not shipped; upgrade with `eslint-config-next` |
| Human verification remains the control against relabelled lines until NEW-4 is fixed | implementer / product |

## 7. Explicitly NOT tested

- Docker build/compose topology end-to-end (no daemon): NEW-2 is derived from the Dockerfile reference, not observed.
- S3 storage backend; hosted LLM provider (no key); only the consent flag/gate function was exercised.
- Sustained load / thread-pool starvation via slow parses (NEW-12 is a design observation; the 12-upload probe used a fast PDF).
- Timing under load or across the network; SEC-10 medians are loopback, n = 9.
- SEC-11 (cross-currency cost), SEC-16 (500 request id), SEC-17 (audit deltas), SEC-18 (formula flag): code review only this round.
- Playwright e2e suite as a whole (only two ad-hoc Chromium scripts); the QA agent's concurrent `qa-reverify` files were not reviewed.
- HTTP request smuggling / HTTP/2 / WebSocket paths; browser XSS beyond existing fixtures; multi-worker races.
- git history secret scan (gitleaks CI job was green per `gh`, not re-run locally).
- mypy on the new test file (project runs mypy on `app` only).

## 8. Run log (verbatim tail of the recorded commands; synthetic data only)

```
$ alembic downgrade base && alembic upgrade head   (quotepilot_sec)        → 0001, 0002, 0003 applied; pg_policies: users_{select,insert,update}, sessions_{select,insert,update}; users/sessions relforcerowsecurity=f, memberships/audit_events=t
$ .venv/bin/pytest -q -p no:cacheprovider tests/security/test_audit_followups2.py            → 7 passed, 6 xfailed in 21.97s (exit 0)  [xfail strict = NEW-1, NEW-2, NEW-3, NEW-4, NEW-5, NEW-6]
$ .venv/bin/pytest -q -p no:cacheprovider tests/security tests/integration/test_review1_fixes.py → 71 passed, 6 xfailed in 124.53s (exit 0)
$ .venv/bin/pytest -q -p no:cacheprovider -m security                                         → 71 passed, 216 deselected, 6 xfailed in 109.86s (exit 0)
$ pip-audit → No known vulnerabilities found (0) | bandit -ll → (none) (0) | ruff check app → All checks passed! (0) | npm audit --omit=dev → 0 vulnerabilities (0) | npm audit → 16 moderate (1)
$ gh run list --commit ff99762 → ci: success (41356139188); security: FAILURE (41356139143) — semgrep avoid-sqlalchemy-text ×2, scripts/revoke_sessions.py
# SEC-05 / M-2
$ proxy :3041   POST /customers + X-Forwarded-For: 203.0.113.9 → audit ip 127.0.0.1 ; 12 bad logins rotating XFF → 401×10, 429, 429
$ direct 127.0.0.1:8041 (loopback peer)  → audit ip 203.0.113.9 ; 12 bad logins rotating XFF → 401×12 (uvicorn default trusts loopback)
$ direct 192.0.2.2:8041 (non-loopback peer) → audit ip 192.0.2.2 ; 12 bad logins rotating XFF → 401×10, 429, 429
$ echo upstream :8042 via next :3042 (trust=false): x-forwarded-for=None x-real-ip=None forwarded=None x-forwarded-host=None x-forwarded-proto=https(spoofed)/http(default) cookie kept
$ echo upstream :8042 via next :3043 (trust=true):  all five forwarding headers passed through verbatim ("203.0.113.9, 10.0.0.1")
# SEC-10
$ timing n=9 interleaved: unknown median 70.3 ms (63.6–81.3) ; known+wrong median 66.9 ms (62.1–83.1)
# SEC-13 / SEC-08
$ logout: no token 403, wrong token 403, token 200 → /auth/me 401 ; GET /quotes?q=%00 200, /customers?q=%00abc 200, /rfqs?q=a%00b 200, /quotes?status=%00 200
# SEC-06 / L-4
$ 1e7×1e7 → 422 totals exceed ; 1e-400 / 0.0001 / 1e9 / 1e-7 → 422 schema ; 5×(5e6×1e7) → 422 ; 2×(9.9e6×9.9e6) → 422 ; api.log: 0 × status 500
# H-1 / SEC-22 / NEW-1
$ approve 200, v1 pdf_sha256 bb2b88f4… ; v1 pdf 200 (3001 B) ; new version 201 ; valid_until=2099-12-31 200 ; org rename 200 ; customer rename 200
$ v1 pdf again: bytes identical True, csv identical True, contains 2099/Renamed/TAMPERED: False/False/False ; POST v1 pdf sha == stored True ; v2 draft pdf contains renamed org True
$ snapshot valid_until frozen to 2020-01-01 (owner SQL with org ctx) → v1 pdf 409, csv 409, POST pdf 409
$ quote 2: approve 200 → owner withdraw 200 → v1 pdf 409 → sales new version 201 (status draft, withdrawn_reason None) → v1 pdf 200, csv 200, v1.status approved   [NEW-1]
# M-3 / M-4 / NEW-4
$ typed-over catalog price → {manual_price_unconfirmed}; submit 409 ; price-override & margin-override: sales 403/403, approver 403/403, readonly 403/403, tenant-B admin 404/404 ; admin reason "abc" 422 ; admin 200 blockers [] ; sales re-price 0.01 → [manual_price_unconfirmed, below_min_margin], override cleared
$ catalog + 99 % discount: sales/readonly "Line 1 price is below the organisation's minimum margin policy" ; approver "Line 1 margin -6017.65% is below the organisation floor of 12.00%" ; submit-409 text identical to sales text
$ product_id + is_custom in one payload → product_id=None price_source=catalog unit_price=0.85 is_custom=True blockers=[]   [NEW-4]
# SEC-01 / consent / NEW-6
$ B admin POST /org/members {A's email} → 409 "This email address cannot be used for a new member" ; own existing email → 409 identical body ; fresh email → 201 ; B admin PATCH on 5 A membership ids → 404
$ admin consent 422 ; owner consent 200 ; audit {"after":{"ai_processing_consent":true},"before":{},"fields":[…]}   [NEW-10]
$ owner creates owner2 201 → owner deactivates 200 → ADMIN re-activates 200 [NEW-6] ; admin demotes active owner2 422 ; inactive owner2 422
$ owner A deactivates sales A 200 → sales A /me 401, login 401 ; re-activate 200 → login 200 ; users.is_active still t
# RLS raw SQL as quotepilot_app
$ no ctx: users 0, sessions 0 ; ctx A: users 6 (contoso 0), sessions distinct orgs 1 ; UPDATE users(B) 0 ; UPDATE users(A member) 0 ; UPDATE password_hash all 0 ; DELETE users 0 ; DELETE sessions 0 ; UPDATE sessions(B) 0 ; UPDATE sessions(A) 23 ; INSERT users (org ctx) 1 ; INSERT membership(B) → RLS error
$ ctx A + user sales: UPDATE own row 1 ; INSERT session(B) → RLS error ; INSERT session(A) 1 ; UPDATE own session organization_id→B 1 ; UPDATE own session user_id→owner 1 (token now owner's)   [NEW-5]
$ ctx B: UPDATE sessions(A) 0 ; login_email ctx: users 1, UPDATE 0, memberships 0, sessions 0 ; login_email='%' → 0 ; uppercase e-mail → 1 (citext)
$ column privileges quotepilot_app: users/sessions SELECT, INSERT, UPDATE, DELETE on all columns (RLS is the only write guard)
# revoke_sessions.py (owner role)  [NEW-3]
$ --org <A> --yes → psycopg.errors.InsufficientPrivilege: new row violates row-level security policy for table "audit_events" (exit 1, rollback) ; --user-email … → revoked 2 (exit 0) ; --all → revoked 12 (exit 0)
# L-10
$ APP_ENV=staging: 'abcdefghijkl'×3 ACCEPTED ; 'password-…' REFUSED ; '0123456789ab'×3 ACCEPTED ; 'a'×40 REFUSED ; 'dev-only-…' REFUSED ; random 40-char ACCEPTED   [NEW-11]
# parser semaphore
$ 12 concurrent PDF uploads (one account): all 201 parsed, latencies 0.26 / 1.1×4 / 2.6×7 s (waves of 4), wall 2.6 s ; health 200 in 14 ms
# SEC-09 / NEW-7
$ safenext.mjs (port of safeNext): /\evil.com, //evil.com, /%5Cevil.com, /%2F%2Fevil.com, https://…, javascript:, /@evil.com, /%68ttp:/… → /dashboard ; "/..//evil.com" → "//evil.com" ; "/%2e%2e/%2e%2e//evil.com" → "//evil.com"
$ redirect2.cjs (Chromium, :3041): next=/..//evil.com → external request http://evil.com/ ; /%2e%2e/%2e%2e//evil.com → http://evil.com/ ; /quotes/..//evil.com/x → http://evil.com/x ; /\evil.com → /dashboard ; /dashboard → /dashboard
```

Probe scripts used (kept outside the repository, in the session scratchpad): `probe1.py` (API/proxy probes),
`echo_server.py`, `safenext.mjs`, `redirect2.cjs`. The Chromium repro for NEW-7, for inclusion in
`tests/e2e/specs/review1-regressions.spec.ts`:

```ts
for (const next of ["/..//evil.com", "/%2e%2e/%2e%2e//evil.com", "/quotes/..//evil.com/x"]) {
  await page.goto(`/login?next=${encodeURIComponent(next)}`);
  // … sign in …
  await expect(page).toHaveURL(/\/dashboard$/);   // currently navigates to http://evil.com/
}
```
