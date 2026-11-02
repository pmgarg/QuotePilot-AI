# Security re-verification evidence 2 — Stage 1 after fix rounds 3+4 (2026-11-01)

Auditor: security-auditor subagent (independent of the implementer and of QA). Repository `QuotePilot-AI`, branch
`claude/multi-stage-project-impl-8hz2wj`, **HEAD `58976f3`** ("evidence: security scan on round-4 commit", on top of
`80aaf7f` "Stage 1 fix round 4"). Product code was **not** modified; the auditor added only
`apps/api/tests/security/test_audit_followups3.py` and this document.

Inputs: `release-review-2-stage1-2026-11-01.md` (N2-1, N2-2, L2-1…L2-7 and its "Required retest"),
`security-stage1-reverify-2026-11-01.md` (NEW-1…NEW-12), `implementer-fixes3-…` and `implementer-fixes4-…`.
This document states what was tested, how, and what was found. It does not claim the absence of vulnerabilities.

**Concurrent working-tree changes observed (not the auditor's):** `apps/api/tests/qa/test_qa_reverify_review2.py`,
`tests/e2e/specs/qa-reverify-review2.spec.ts`, `docs/evidence/qa-stage1-reverify2-2026-11-01.md` (QA re-verification
running in parallel). Nothing below depends on them.

## 1. Scope

| In scope | Method |
|---|---|
| `app/services/quotes.py` (update_draft product re-link rules, `_pricing_catalog_version_id`, withdraw across versions, new_version copy, margin_override), `services/exports/quote_exports.py`, `services/rfqs.py` (item selection), `alembic/versions/*0004*` (column grants), `*0005*` (identity triggers + data fix), `api/routers/org.py`, `apps/web/src/app/login/page.tsx` (`safeNext`), `apps/web/src/app/api/[...path]/route.ts`, `infra/docker-compose.yml`, `apps/api/Dockerfile`, `scripts/revoke_sessions.py` | Code review of every file; black-box pytest (`TestClient`) against a dedicated PostgreSQL 16 database `quotepilot_sec` migrated `base → head` (0001→0005); live probes against `uvicorn :8041`, the Next proxy `:3041` (→ real API) and a second Next instance `:3042` (`WEB_TRUST_FORWARDED_HEADERS=false` → header-echo server `:8042`); Chromium (Playwright 1243) for the login redirect; raw SQL as `quotepilot_app` and as the owner role; `docker compose config` (CLI only, no daemon) for the compose topology |
| Scanners | pip-audit, bandit `-ll`, ruff (project config + `S*`), npm audit (web prod/full, e2e). **gitleaks / semgrep / trufflehog / hadolint are CI-only** (not installed locally); the CI `security` workflow for `58976f3` was read with `gh run list` (§ 5) |

Checklists: OWASP ASVS 5.0 V2/V3/V4/V5/V12/V14, `docs/THREAT_MODEL.md` abuse cases (pricing integrity, document
revival, tenant containment). Only the local application built from this repository was tested.

## 2. Environment

| Item | Value |
|---|---|
| Host | Claude Code cloud container, Ubuntu 24.04, Python 3.13 (`apps/api/.venv`), Node 22, PostgreSQL 16.15 (local), uvicorn 0.54, Next 16.4.3, Docker Compose CLI v5.6.1 (**no daemon**) |
| Database | `quotepilot_sec`: `alembic downgrade base && alembic upgrade head` → `0001 → 0002 → 0003 → 0004 → 0005` OK (exit 0). Roles `quotepilot_app` / `quotepilot_owner`: `rolsuper=f rolbypassrls=f`; owner owns the tables |
| Env | `APP_ENV=test DATABASE_URL=…quotepilot_app…/quotepilot_sec DATABASE_ADMIN_URL=…quotepilot_owner…/quotepilot_sec`, `SECRET_KEY` set, `DEMO_PASSWORD` 25 chars, `STORAGE_DIR` in the scratchpad; seeded with `python -m app.fixtures.seed` (exit 0) |
| Servers | `uvicorn app.main:app --host 127.0.0.1 --port 8041`; `npx next start --port 3041` (`API_INTERNAL_URL=http://127.0.0.1:8041`); `npx next start --port 3042` (`API_INTERNAL_URL=http://127.0.0.1:8042`, trust=false); `.next` build `SR-O6WjdGYwAfQyud7h77` is newer than every file under `apps/web/src`. All stopped after the probes (ports verified closed) |
| Not available | Docker daemon (image not built, compose not executed), gitleaks, semgrep, trufflehog, hadolint, external network |

## 3. Per-finding status

Status values: **FIXED** (verified at `58976f3`), **PARTIAL** (fixed for the reported case, adjacent gap found —
see § 4), **OPEN**, **ACCEPTED** (documented in KNOWN_LIMITATIONS; not a human-signed acceptance), **GATE** (process).
`T2` = `test_audit_followups2.py`, `T3` = `test_audit_followups3.py` (new), § 8 = run log.

| ID | Status | Evidence at `58976f3` |
|---|---|---|
| NEW-1 withdrawn document revived by new version | **FIXED** | `T2::test_new1_…` passes; `T3::test_n2_2_withdrawn_document_stays_withdrawn_after_a_later_version_is_approved` — v1 stays `withdrawn` after v2 is approved; PDF/CSV GET and PDF POST on v1 ≠ 200 for **owner, admin, sales, approver, readonly**; a second withdrawal covers v2 and approval of a withdrawn quote → 409 |
| NEW-2 Dockerfile CMD | **FIXED** (static) | `apps/api/Dockerfile:17` is valid JSON exec form; comment moved above. **Image not built** (no daemon) |
| NEW-3 `revoke_sessions.py --org` | **FIXED** (dynamic) | § 8: 4 live sessions (3 Northwind, 1 Contoso) → `--org <northwind> --yes` → `revoked 3 session(s)`, **exit 0**; Northwind cookies → `/auth/me` 401, Contoso 200; exactly one `ops.sessions_revoked` audit row with the org id. `T3::test_new3_org_kill_switch_…` repeats this with two tenants. Adjacent: NEW-15 |
| NEW-4 custom line keeps `catalog` source | **FIXED** | `T2::test_new4_…` passes; `T3::test_price_override_…`: `is_custom:true` clears product/cost and voids a prior confirmation |
| NEW-5 sessions/users full-row UPDATE | **FIXED** | § 8 `information_schema.column_privileges` for `quotepilot_app`: `sessions` UPDATE = `last_seen_at, revoked_at`; `users` UPDATE = `full_name, password_hash, updated_at`; table grants = `INSERT, SELECT` only (no DELETE). As the app role with a matching tenant/user context: `UPDATE sessions SET user_id/organization_id/token_hash/expires_at` and `UPDATE users SET email/is_active/id`, `DELETE` on both → **permission denied**; `ALTER TABLE … DISABLE TRIGGER`, `DROP TRIGGER`, `CREATE OR REPLACE FUNCTION qp_guard_identity_columns` → refused. `T3::test_new5_app_role_cannot_touch_identity_columns_…` |
| NEW-6 admin re-activates owner | **FIXED** | `T2::test_new6_…` passes; `routers/org.py:150-151` gates both directions on `principal.role == "owner"` |
| NEW-7 open redirect via dot segments | **FIXED** (Chromium) | § 8 `redirect3.cjs` through `:3041`: `/..//evil.com`, `/%2e%2e/%2e%2e//evil.com`, `/quotes/..//evil.com/x`, `/\evil.com`, `//evil.com` **plus 15 new variants** (`/\/evil.com`, `/%5c%5cevil.com`, `/..%2f%2fevil.com`, `/dashboard/%2e%2e//evil.com`, `/%2F%2Fevil.com`, `/dashboard#//evil.com`, `https://evil.com/`, `javascript:alert(1)`, `/dashboard?x=//evil.com`, `/dashboard?r=1&%2F%2Fevil.com`, U+2215 slashes, `/%252e%252e//evil.com`, `/dashboard%09//evil.com`, `/.//evil.com`, `/evil.com`) all land on `http://127.0.0.1:3041/dashboard`; **zero requests to any non-127.0.0.1 host** (route interception). Positive case `/quotes?page=2` is preserved. `safeNext` now whitelists `^\/[A-Za-z0-9_\-/?=&]*$` before URL parsing (`login/page.tsx:9-19`) |
| NEW-8 `X-Forwarded-Proto` synthesised | **FIXED** | § 8 via `:3042` (trust=false): `X-Forwarded-Proto/For/Host`, `X-Real-IP`, `Forwarded` (any case) are **absent** upstream; nothing is synthesised (`route.ts:29`). Info: `X-Forwarded-Port` is still client-controlled (§ 4, I-1) |
| NEW-9 runbook hops, NEW-11 weak-secret heuristic, NEW-12 limiter/parser trade-off | ACCEPTED (docs) | Not re-tested in this round (documentation changes only) |
| NEW-10 consent audit `before: {}` | **FIXED** (code) | `routers/org.py:48-61` records `before["ai_processing_consent"]` and `after` |
| N2-1 A other-currency re-link keeps price | **FIXED** | `test_review2_fixes::test_n2_1a_…`; `T3::test_n2_1a_cross_currency_product_never_yields_a_catalog_price_on_any_route`: RFQ generation → `unit_price=None, price_source=none`; editor re-link with a typed price in the same PATCH → `manual` + `manual_price_unconfirmed`; submit 409; Sales and approver `price-override` → 403 |
| N2-1 B inactive/archived products linkable | **PARTIAL** | Editor PATCH refuses non-pricing-version products with 422, atomically (`test_n2_1b_…`). Still open: the RFQ item route when the RFQ has no frozen catalog version, and submission of lines priced from a since-archived version → **NEW-13** |
| N2-2 withdrawal across versions / while draft | **FIXED** for revival | `test_review2_fixes::test_n2_2_*`; `T3` revival tests above. Adjacent data-integrity gap in the withdraw-while-draft path and in the 0005 data fix → **NEW-14** |
| L2-1 NEW-7 variants untested | **FIXED** | `review1-regressions.spec.ts` carries the three variants; 22 variants exercised here |
| L2-2 API container receives owner DSN | **PARTIAL** | `docker-compose.yml:43-46`: `DATABASE_ADMIN_URL: ""` for `api` (the empty string wins over `env_file`). But `env_file: ../.env` still injects **every** key of `.env`, including `QP_OWNER_PASSWORD` → **NEW-16**. `migrate` legitimately holds both DSNs; there is no worker service (redis is a Stage-2 profile); `web` receives only `API_INTERNAL_URL` |
| L2-3 blocker reveals cost ratio | ACCEPTED (docs) | KNOWN_LIMITATIONS note; not re-tested |
| L2-4 `sessions_update` WITH CHECK permissive | **FIXED** (defence in depth) | The policy text is unchanged (§ 8: `WITH CHECK` = `SESSIONS_SELECT`), but it is now moot: column grants (0004) deny identity columns to the app role and `trg_sessions_identity` / `trg_users_identity` (0005) refuse `user_id`, `organization_id`, `token_hash`, `csrf_secret` / `id`, `email` changes **even for the owner role** (§ 8). Legitimate flows verified: idle touch of `last_seen_at`, logout, member create/demote/deactivate/re-activate, operator `revoked_at` (`T3::test_l2_4_identity_guards_do_not_break_legitimate_auth_and_membership_flows`). No password-change route exists; the seed only inserts. Note: `users.email` is `citext`, so a case-only change is not "distinct" and passes the trigger (same identity — benign, § 8) |
| L2-5 no data migration for legacy withdrawn approvals | **PARTIAL** | 0005 withdraws approved versions of quotes whose `withdrawn_reason IS NOT NULL`. That misses the exact legacy pattern of N2-2 (withdraw → new version cleared the marker) and is not idempotent against round-4 data → **NEW-14** |
| L2-6 documentation drift, L2-7 human sign-off | not assessed / **GATE** | Out of the security scope; L2-7 remains a human action |

## 4. New findings

| ID | Severity | Title | Location | Reproduction | Impact | Remediation |
|---|---|---|---|---|---|---|
| NEW-13 | **Low** | N2-1 B residual: inactive/archived catalog prices still reach submitted quotes via the RFQ route and via stale/copied lines | `services/rfqs.py:412` (`if rfq.catalog_version_id and …` — guard skipped when the RFQ has no frozen version); `services/quotes.py:266-274` (`_pricing_catalog_version_id` falls back to the *current* active version for ad-hoc quotes); `compute_blockers` `:122-177` (never checks that a `catalog`-priced line's product belongs to the pricing version); `new_version` `:812-825` (copies `product_id`/`price_source`) | `T3::test_new13_rfq_without_catalog_version_refuses_products_of_inactive_versions` (xfail strict) + `…repro…`: tenant with no active catalog → RFQ parsed (`flags: ["no_active_catalog"]`, `catalog_version_id=NULL`) → admin imports a **never-activated** list (SKU-1001 @ 0.01) and activates another (@ 0.85) → Sales selects the 0.01 product on the RFQ item → **200** → generate-quote → line `0.01 / catalog`, `blockers == []`. `T3::test_new13_archived_catalog_lines_are_blocked_or_repriced_on_submit` (xfail strict): ad-hoc quote linked under C1 → C2 activated → re-saving the line is refused (422) but **submit succeeds** at the archived price labelled `catalog`. Copy route documented in `…repro_new_version_copies_archived_catalog_pricing` (RFQ quotes keep their frozen version by design) | "Inactive price lists are never a price authority" (round-4 comment) does not hold on two routes; the RFQ one needs an org without an active catalog at parse time, the second is the normal "catalog re-imported while drafts exist" situation. No privileged confirmation is required, cost/margin come from the stale list | Apply the same version check in `rfqs.update_item` when `rfq.catalog_version_id` is NULL (resolve to the active version at selection time, or refuse until a catalog is active); add a `catalog_version_mismatch` blocker (or re-price) in `compute_blockers` for `catalog` lines whose product is not in `_pricing_catalog_version_id`; decide and document whether RFQ-frozen versions remain an authority after archival |
| NEW-14 | **Low** (data integrity / operations) | Withdrawal marker and 0005 data fix are inconsistent with the round-4 state machine | `services/quotes.py:743-748` (withdraw while a draft is current sets `quote.withdrawn_reason`, status stays `draft`); `approve` `:601-660` never clears it; `alembic/versions/20261101_0005_…:43-47` (data fix keyed on `withdrawn_reason IS NOT NULL`); `new_version` `:828` (legacy path cleared the marker) | (a) `T3::test_new14_withdraw_while_draft_then_approve_clears_the_withdrawal_marker` (xfail strict): v1 approved → v2 draft → withdraw (200, draft) → submit/approve v2 → quote `approved` **with `withdrawn_reason = "v1 wrong"`** still set. (b) `…l2_5_data_fix_is_idempotent_against_round_4_data` (xfail strict): the verbatim 0005 UPDATE re-applied on that data (replayed deploy, `downgrade -1 && upgrade head`) **withdraws the legitimately approved v2** (PDF 409). (c) `…l2_5_data_fix_covers_the_legacy_withdraw_then_new_version_pattern` (xfail strict): pre-round-3 state (v1 `approved`, withdrawal only in `approval_decisions`, marker cleared by the new version) is **not** touched by the data fix — v1 stays exportable | A current approved document carries a stale withdrawal reason in the API payload (misleading banner/export decisions built on it later); the migration is not idempotent; the legacy data the fix was written for (N2-2's own scenario) is left as-is | Clear `withdrawn_reason` when a later version is approved (or move the reason to the version row, where the status already lives); rewrite the data fix on `approval_decisions.decision = 'withdrawn'` (withdraw every approved version with `version_no <=` the withdrawn decision's version) and make it idempotent; add a migration round-trip test with such data |
| NEW-15 | **Low** (audit evidence) | Platform-scope kill-switch audit rows are write-only | `scripts/revoke_sessions.py:60-66` (`organization_id = NULL` for `--user-email` / `--all`); `audit_events` policy `audit_scope` (`USING organization_id = app.current_org`, `WITH CHECK … OR organization_id IS NULL`, table `FORCE ROW LEVEL SECURITY`) | § 8 and `T3::test_new15_…` (xfail strict): `--user-email`/`--all` → exit 0, `pg_stat_user_tables.n_tup_ins` +1, but `SELECT … WHERE organization_id IS NULL` returns **0** for the owner role (with or without a tenant context; `SET row_security = off` is refused because of FORCE) and for the app role | The only record of a platform-wide forced logout (an incident-response action) cannot be read by any role the product uses; it is invisible to the tenant audit views and to the operator | Write one row per affected organisation (bind each org as `--org` does), or add a `SELECT` policy for `organization_id IS NULL` restricted to the owner role, and document where operators read it |
| NEW-16 | **Low** (secrets hygiene) | Compose `api` service still inherits the owner role's password through `env_file` | `infra/docker-compose.yml:40-46` (`env_file: ../.env` + `DATABASE_ADMIN_URL: ""`); `.env.example` keys `QP_OWNER_PASSWORD`, `DATABASE_ADMIN_URL` | § 8 `docker compose config` with `.env.example` as `.env`: `api` receives 21 variables incl. `QP_OWNER_PASSWORD=quotepilot_o…` (and `QP_APP_PASSWORD`, `ANTHROPIC_API_KEY`, `SECRET_KEY`); `DATABASE_ADMIN_URL` is `''`. `T3::test_new16_…` (xfail strict) | L2-2's intent ("the owner role is never given to the API container") is only met for the DSN; an RCE or env dump in the API container still yields the owner password (host and db name are in `DATABASE_URL`). Compose was not executed (no daemon) | Give `api` an explicit `environment:` list (or a separate `env.api` file without `QP_OWNER_PASSWORD`/`DATABASE_ADMIN_URL`); keep `../.env` for `migrate` only; add a compose-config check to CI |
| NEW-17 | **Medium** (control degradation of M-4) | Re-saving an unchanged catalog line in the editor relabels it `manual` and raises `manual_price_unconfirmed` | `services/quotes.py:466-469` (`if "unit_price" in payload and payload["unit_price"] is not None: … price_source = "manual"` — no comparison with the current price/source); `apps/web/src/app/(app)/quotes/[id]/page.tsx:88` (`save()` re-sends `unit_price` for every line) | `T3::test_editor_resave_of_an_unchanged_catalog_price_keeps_price_source_catalog` (xfail strict): generated quote line `catalog / 0.85` → PATCH with exactly the fields the editor sends, unchanged → observed `('manual', '0.85', ['manual_price_unconfirmed'])` | Every editor save of a generated quote turns **all** catalog lines into "manually priced" lines that an owner/admin must confirm one by one. The confirmation becomes routine, so a genuinely altered price on one line is confirmed in the same sweep — the behaviour M-4/NEW-4 were introduced to prevent; `quote.margin_override` audit events and `price_source` then misrepresent catalog prices as overrides. Also a functional defect QA should see (the CI e2e M-4 spec passes because any save produces the blocker) | Only switch to `manual` when the submitted price differs from the current `unit_price` (or when `price_source != "catalog"`); have the editor omit `unit_price` for untouched catalog lines; add this regression test to the integration suite |

Informational observations (no separate severity):

- **I-1** `X-Forwarded-Port` passes through the proxy from the client (`route.ts:12` lists proto/for/host/real-ip/forwarded only; Next sets the header from the inbound value) — § 8: `X-Forwarded-Port: 443` reaches upstream. No consumer in the API today; add it to `FORWARDING` for symmetry with NEW-8.
- **I-2** `users.email` is `citext`: `UPDATE users SET email = upper(email)` passes the identity trigger (not distinct under citext). Same identity, no impact; stated so the trigger's guarantee is read correctly.
- **I-3** CI on `58976f3`: the push-triggered `ci` run 41362169899 is green, the later `pull_request`-triggered `ci` run 41362914665 (23:36Z) **failed in e2e** (6 specs: SEC-09 redirect, H-1 ×2, M-4 ×1+retry, S1-A20 XSS, X-08 axe; `api` and `web` jobs green). Not root-caused here (looks like cross-spec interference rather than a product regression — all failing specs passed in the push run of the same commit); the gate should not read "CI green" from one of two runs.
- **I-4** `.env.example`'s `ANTHROPIC_API_KEY=` line carries an inline comment that `docker compose` treats as the value (`'# server-sid…'`); harmless for the placeholder, but operators copying the pattern will ship comments as secrets. Move the comment to its own line.
- **I-5** `_pricing_catalog_version_id` with no frozen version and no active catalog returns `None`, so an ad-hoc quote can only hold custom lines (every product link → 422). Intended, worth a user-facing message.

## 5. Tool results (command, exit code, summary)

| Tool / command (cwd `apps/api` unless stated) | Exit | Summary |
|---|---|---|
| `alembic downgrade base && alembic upgrade head` (`quotepilot_sec`) | 0 | 0001→0005 applied; `alembic current` = `0005 (head)` |
| `pip-audit 2.10.1 -r <(pip freeze)` (+ `-f json`) | 0 | 99 packages audited, **0 known vulnerabilities**, 1 skipped (`quotepilot-api` local project) |
| `bandit 1.9.4 -q -r app -ll` (+ `-f json`) | 0 | 6 041 LOC, **0 medium/high**; 7 low (below `-ll`) |
| `ruff 0.17.3 check app tests` / `ruff check --select S --ignore S101,S105,S106,S311,S603,S607,S608 app` | 0 / 0 | clean / no `S*` findings |
| `ruff check` + `ruff format --check tests/security/test_audit_followups3.py` | 0 | new file lint/format clean |
| `npm audit --omit=dev` (`apps/web`) | 0 | 0 vulnerabilities |
| `npm audit` full (`apps/web`) | 1 | 16 moderate, all dev-only lint tooling via `minimatch` (`eslint*`, `typescript-eslint*`) — unchanged from the implementer's scan |
| `npm audit` full (`tests/e2e`) | 0 | 0 vulnerabilities |
| gitleaks / semgrep / trufflehog / hadolint | n/a | **not installed locally — CI-only.** `gh run list`: `security` workflow on `58976f3` = success (runs 41362169890, 41362914592); `ci` = success (41362169899) **and** failure (41362914665, e2e — see I-3) |
| `docker compose config` (CLI v5.6.1, no daemon; `.env.example` copied as `.env` in the scratchpad) | 0 | rendered; see NEW-16. Image build / compose up **not executed** |
| `pytest tests/security tests/integration/test_review2_fixes.py` (`quotepilot_sec`) | 0 | **78 passed, 7 xfailed** (before the NEW-17 marker); final `T3` run: 8 passed, 8 xfailed (strict), 0 failed |
| `node redirect3.cjs` (Playwright Chromium via `:3041`) | 0 | 22 variants, 0 off-site requests |

## 6. Residual risks

| Risk | Status / owner |
|---|---|
| NEW-17 editor save relabels catalog lines `manual` (M-4 control becomes routine) — Medium | Open — implementer; strict-xfail test in `T3` |
| NEW-13 inactive/archived prices via RFQ-without-catalog and submit-time (N2-1 B residual) — Low | Open — implementer + product decision on frozen-version authority; `T3` strict-xfail tests |
| NEW-14 withdrawal marker persists / 0005 data fix not idempotent and incomplete (L2-5 residual) — Low | Open — implementer; three `T3` strict-xfail tests |
| NEW-15 platform-scope revocation audit rows unreadable — Low | Open — implementer/ops; `T3` strict-xfail test |
| NEW-16 owner password in the `api` container env (L2-2 residual) — Low | Open — implementer; `T3` strict-xfail test; compose still never executed |
| I-3 e2e failure on the PR-triggered run of the same HEAD | Open — QA/orchestrator to root-cause before reading CI as green |
| NEW-2 Dockerfile, compose topology, image health check | Static review only; still **never executed** (no daemon) — unchanged from the previous round |
| NEW-9/11/12, L2-3 (documented trade-offs) | ACCEPTED in docs; **not human-signed** (L2-7 / gate item) |
| Column grants + identity triggers depend on the DB roles being created as in `infra/postgres/init.sh` (non-superuser, no BYPASSRLS) | Verified for the local roles only; production role creation is outside this repository's tests |

## 7. Explicitly NOT tested

- Building or running the Docker images / compose stack (no daemon): NEW-2, NEW-16 and the health check are static.
- gitleaks, semgrep, trufflehog, hadolint locally (CI results read, not reproduced).
- Load/concurrency of the parser semaphore (NEW-12), the login limiter under distributed IPs, and production TLS/proxy header handling beyond the local Next proxy.
- The AI adapter (not wired in Stage 1) and any prompt-injection fixtures.
- Accessibility / screen-reader evidence (X-08) and the QA functional scope; the concurrent QA files were not reviewed.
- Root cause of the failed PR-triggered e2e run (I-3).

## 8. Run log (verbatim tail of the recorded commands; synthetic data only, credentials local-only)

```
$ .venv/bin/alembic downgrade base && .venv/bin/alembic upgrade head
… Running upgrade 0003 -> 0004, review 2 hardening: column-level write grants …
… Running upgrade 0004 -> 0005, review 2: session/user identity guard trigger (L2-4) and data fix …
exit=0   $ alembic current → 0005 (head)

== NEW-8: headers reaching upstream via :3042 (trust=false), request sent with
   X-Forwarded-Proto/For/Host, X-Real-IP, Forwarded, x-FORWARDED-proto, X-Forwarded-Proto-Evil
{"host": "127.0.0.1:8042", "connection": "keep-alive", "accept": "*/*", "user-agent": "curl/8.5.0",
 "x-forwarded-port": "3042", "x-forwarded-proto-evil": "https", …}      ← none of the five forwarding headers
$ curl -H "X-Forwarded-Port: 443" … → {'x-forwarded-port': '443'}       ← I-1

== NEW-5a column_privileges (grantee quotepilot_app)
sessions|UPDATE|last_seen_at,revoked_at
users|UPDATE|full_name,password_hash,updated_at
table grants: sessions|INSERT,SELECT   users|INSERT,SELECT
== NEW-5b app role (app.current_org/current_user/login_email bound)
UPDATE sessions SET user_id=<owner> …            -> ERROR:  permission denied for table sessions
UPDATE sessions SET organization_id=<org> …      -> ERROR:  permission denied for table sessions
UPDATE sessions SET token_hash='x'               -> ERROR:  permission denied for table sessions
UPDATE sessions SET expires_at=now()+'10 years'  -> ERROR:  permission denied for table sessions
UPDATE sessions SET last_seen_at=now()           -> ok
DELETE FROM sessions WHERE false                 -> ERROR:  permission denied for table sessions
UPDATE users SET email='x@y.example' …           -> ERROR:  permission denied for table users
UPDATE users SET is_active=false …               -> ERROR:  permission denied for table users
UPDATE users SET full_name=full_name …           -> ok
UPDATE users SET password_hash=…, updated_at=now() -> ok
DELETE FROM users WHERE false                    -> ERROR:  permission denied for table users
ALTER TABLE sessions DISABLE TRIGGER …           -> ERROR:  must be owner of table sessions
DROP TRIGGER trg_sessions_identity …             -> ERROR:  must be owner of relation sessions
CREATE OR REPLACE FUNCTION qp_guard_identity_columns() … -> ERROR:  permission denied for schema public
== NEW-5c / L2-4 OWNER role vs triggers
UPDATE users SET email='other@northwind.example' -> ERROR:  user identity columns are immutable
UPDATE users SET id=gen_random_uuid() …          -> ERROR:  user identity columns are immutable
UPDATE users SET email=upper(email) …            -> UPDATE 1   (citext: not distinct — I-2)
UPDATE users SET is_active=false, password_hash='x' -> UPDATE 1 (allowed, not identity)
UPDATE sessions SET user_id=<sales> …            -> ERROR:  session identity columns are immutable
UPDATE sessions SET organization_id=<other org>  -> ERROR:  session identity columns are immutable
UPDATE sessions SET token_hash=repeat('a',64)    -> ERROR:  session identity columns are immutable
UPDATE sessions SET csrf_secret=repeat('b',64)   -> ERROR:  session identity columns are immutable
UPDATE sessions SET revoked_at=now() / expires_at=now() -> ok (owner)
qp_guard_identity_columns|prosecdef=f|owner=quotepilot_owner ; trg_sessions_identity O sessions ; trg_users_identity O users
sessions_update policy: USING = WITH CHECK = SESSIONS_SELECT expression (unchanged; moot per L2-4 row above)

== NEW-3
logins: 200 200 200 200   (northwind owner, sales ×2, contoso owner)  northwind org=ce5a41a8-…
before: contoso active=1 | northwind active=3
$ python ../../scripts/revoke_sessions.py --org ce5a41a8-… --yes
revoked 3 session(s)      exit=0
after:  contoso active=1 | northwind active=0 (3 rows, revoked)
audit (ctx=northwind): ops.sessions_revoked|session|ce5a41a8-…||{"count": 3, "scope": "org"}   (exactly one row)
cookies → /auth/me: nw_owner 401, nw_sales 401, nw_sales2 401, co_owner 200
$ … --user-email owner@contoso.example --yes → revoked 1 session(s) exit=0 ; co_owner → 401
== NEW-15
$ … --all --yes → revoked 22 session(s) exit=0 ; audit_events n_tup_ins before=32 after=33
owner: SELECT count(*) FROM audit_events WHERE organization_id IS NULL → 0 (with and without ctx)
owner: SET row_security = off → HINT: To disable the policy for the table's owner, use ALTER TABLE NO FORCE …
audit_scope: USING (organization_id = current_org) WITH CHECK (organization_id IS NULL OR …); audit_events FORCE RLS

== NEW-7 (Chromium, PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers, login owner@contoso via :3041)
{"raw":"/..//evil.com","final":"http://127.0.0.1:3041/dashboard","offsite":[]}
{"raw":"/%2e%2e/%2e%2e//evil.com","final":"…/dashboard","offsite":[]}
{"raw":"/quotes/..//evil.com/x","final":"…/dashboard","offsite":[]}
{"raw":"/\\evil.com","final":"…/dashboard","offsite":[]}
{"raw":"//evil.com","final":"…/dashboard","offsite":[]}
… 15 further variants (§ 3 NEW-7 row) → "…/dashboard", "offsite":[]
{"raw":"/quotes?page=2","final":"http://127.0.0.1:3041/quotes?page=2","offsite":[]}

== NEW-16 (docker compose config, offline)
api -> 21 env vars: ANTHROPIC_API_KEY='# server-sid…', DATABASE_ADMIN_URL='', DATABASE_URL=postgresql+p…,
      DEMO_PASSWORD='', QP_APP_PASSWORD=quotepilot_a…, QP_OWNER_PASSWORD=quotepilot_o…, SECRET_KEY=change-me-to…
migrate -> 21 env vars (DATABASE_ADMIN_URL=postgresql+p…)      web -> 1 env var

== pytest (quotepilot_sec)
tests/security tests/integration/test_review2_fixes.py → 78 passed, 7 xfailed (122 s)
tests/security/test_audit_followups3.py --runxfail (intended assertions reached):
  NEW-13: PATCH rfq item (selected inactive product) → 200 … flags ["no_active_catalog"]
  NEW-13: "submission with a line priced from an archived catalog version must be blocked"
  NEW-14: AssertionError: v1 wrong            (withdrawn_reason on the approved quote)
  NEW-14: approved v2 was withdrawn by the data fix
  NEW-14: legacy withdrawn approval left as 'approved' by the data fix
  NEW-15: … neither the owner role nor the app role can ever SELECT it
  NEW-16: {'DATABASE_ADMIN_URL', 'QP_OWNER_PASSWORD'}
  NEW-17: AssertionError: ('manual', '0.85', ['manual_price_unconfirmed'])
final: 8 passed, 8 xfailed in 25.56s

== scanners
pip-audit: No known vulnerabilities found (99 audited, 1 skipped: quotepilot-api)   exit=0
bandit -ll: loc 6041 results 0 {HIGH 0, MEDIUM 0, LOW 7}                            exit=0
ruff check app tests: All checks passed!  ruff --select S …: no findings            exit=0
npm audit --omit=dev (web): total 0 | npm audit (web): 16 moderate (eslint tooling/minimatch) exit=1 | (e2e): total 0
gh run list --branch …: 58976f3 security success ×2; ci success 41362169899; ci failure 41362914665 (e2e)
== shutdown: all probe ports closed; next-server workers gone
```
