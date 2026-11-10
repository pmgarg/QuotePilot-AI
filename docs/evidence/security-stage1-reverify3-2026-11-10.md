# Security re-verification evidence 3 — Stage 1 after fix rounds 5+6 (2026-11-10)

Auditor: security-auditor subagent (independent of the implementer; did not implement rounds 5 or 6). Repository
`QuotePilot-AI`, branch `claude/multi-stage-project-impl-8hz2wj`, **HEAD `f69ac25`**. Product code was **not** modified.
Added: `apps/api/tests/security/test_audit_followups4.py` and this file. The scan script wrote
`docs/evidence/scan-20261110-170807.md` (names commit `f69ac25`). Nothing was committed. Uncommitted files by others
seen in the working tree (QA re-verification, running in parallel): `apps/api/tests/qa/test_qa_reverify_review3.py`,
`tests/e2e/specs/qa-reverify-review3.spec.ts`; nothing below depends on them.

This document states what was tested and what was found. It does not claim the absence of vulnerabilities.

## 1. Scope and method

Inputs read: `CLAUDE.md`, `release-review-3-stage1-2026-11-02.md` ("Required retest — Security"),
`security-stage1-reverify2-2026-11-01.md` (NEW-13..NEW-17, I-1, I-3), `implementer-fixes5-…`, `implementer-fixes6-…`.
Environment: `APP_ENV=test`, PostgreSQL 16 database `quotepilot_sec` (schema dropped and rebuilt by `alembic upgrade head`
as the owner role, seed script run), API `127.0.0.1:8092`, web (`next start` from the existing `.next` build, which
contains the "Use active catalog" control) `127.0.0.1:3092`, all probes through the web proxy `/api/v1/*`. A second web
instance (3093) in front of a header-echo stub (8093) was used for I-1. All servers were stopped afterwards; ports
3092, 3093, 8092, 8093 verified closed (`/proc/net/tcp` listener lookup; `ss` is not installed). Localhost only, no paid
services, no third-party targets.

## 2. Findings

No Critical, High or Medium findings. All six items under retest behave as intended on the tested paths.

| ID | Severity | Title | Evidence | Remediation | Test |
|---|---|---|---|---|---|
| R4-1 | Info | `scripts/revoke_sessions.py` writes the **platform-wide** `total` into every tenant's audit row (tenant A's row carries B's session count). Not reachable through any API today (session audit rows have no `entity_id`, so the quote history never returns them) — same as R3-9 | § 4 NEW-15: org NW row `{"count": 9, "scope": "all", "total": 12}`, org CO row `{"count": 3, …, "total": 12}` | Keep `total` on stdout only | `test_r4_1_…` (strict xfail) |
| R4-2 | Info | `scripts/check_compose.py` passes vacuously: `if svc is None: continue`, so a renamed or removed `api`/`web` service prints `compose check: OK` and exits 0. It also only inspects `environment` (the Compose v5 renderer resolves `env_file` into `environment`, so a re-added `env_file` is still caught via its keys — verified) | `test_r4_2_…` (strict xfail): rendered file with only `backend` carrying `QP_OWNER_PASSWORD` → exit 0 | Fail if `api` or `web` is absent | `test_r4_2_…` (strict xfail) |
| R4-3 | Info | The README-documented render with `.env.example` copied to `.env` gives the `migrate` service `COOKIE_SECURE` and `ANTHROPIC_API_KEY` values that are the **inline comment text** (`'# empty = derived from APP_ENV …'`, `'# server-side only; …'`): Compose's `env_file` parser keeps the comment for an empty value, while `${…}` interpolation (used by `api`) strips it (`APP_ENV: development` rendered correctly). Only `migrate` is affected, which uses neither key, so there is no effect today; a non-empty bogus key/flag is a trap if `migrate` or an allow-list ever consumes them by `env_file` | § 5 `compose.yml` lines 46-47 | Move comments to their own lines in `.env.example` | not tested |
| R4-4 | Info | The proxy's forwarding header handling is a blocklist; `X-Forwarded-Prefix` (and by extension other `X-Forwarded-*`, `True-Client-IP`, `CF-Connecting-IP`) still pass upstream. The API has no consumer for any of them (`request.client.host` only; asserted by `test_i1_…`) | § 4 I-1 probe: only `x-forwarded-prefix` arrived upstream | Allow-list forwarded request headers, or drop the `x-forwarded-*` prefix class | `test_i1_…` (guards against a future consumer) |
| R4-5 | Info (process) | Provenance of `test_audit_followups3.py` cannot be verified byte-for-byte: it was first committed by the implementer (`597f4b0`); no pristine copy of the delivered file exists in git or on disk (searched working tree, other worktrees, dangling objects) | § 6 | Orchestrator: commit auditor files unmodified before the implementer touches them | — |

Observations (no finding):
- `GET /catalogs/active-version` is available to every authenticated role including `readonly` and returns only
  `{"version_id": <uuid|null>}` of the caller's own organisation; 401 anonymous, 405 for POST/PATCH/DELETE.
- RFQ-pinned quotes still price from the version frozen at parse time even after it is archived (O2-1, documented in
  `KNOWN_LIMITATIONS.md`); the `catalog_version_mismatch` blocker deliberately applies to unpinned (ad-hoc) quotes only.
- A `manual` line whose price equals the list price stays `manual` (stricter than needed; resolved by sending
  `unit_price: null`).
- Under `.env.example` defaults the Compose quick start runs the `api` with `APP_ENV=development` and the placeholder
  `SECRET_KEY` (accepted in development only; production refuses it). Compose was never executed.

## 3. Per-control table

| Control / item | Result | How verified |
|---|---|---|
| NEW-13 part 1 (RFQ without frozen version accepts inactive-version products) | **FIXED** | in-process: inactive product on RFQ item → 422 (`followups3::test_new13_rfq_without_…`, `followups4::test_new13_rfq_item_selected_before_activation_is_flagged_after_it`: after a new activation the archived product is refused on the RFQ too, generated quote is not submittable) |
| NEW-13 part 2 (archived-version prices reach submission) | **FIXED** | live: v2 imported but not activated → link 422; activated → `GET /quotes/{id}` blockers `[catalog_version_mismatch line 1]`, submit **409**; re-save of untouched line 200 (draft stays editable); `unit_price: null`, quantity, discount, `is_custom:false` edits cannot shed the blocker (`test_new13_stale_blocker_survives_…`); new line with archived product 422; relink (editor request shape) re-prices to the new list price and clears the blocker; pending ad-hoc quote after a later activation: approve **409** `catalog_version_mismatch`, reject 200 |
| NEW-14 (a) `withdrawn_reason` after later approval | **FIXED** | live: v1 approved → v2 draft → withdraw (status `draft`, reason set) → submit/approve v2 → `withdrawn_reason: null`, versions `[(1 withdrawn),(2 approved)]`; v1 PDF **409**, v2 PDF 200 (`test_new14_withdrawn_version_stays_unexportable_…`) |
| NEW-14 (b, c) data fix keyed on the decision trail, FORCE RLS | **FIXED** | `alembic upgrade head` on empty DB, `downgrade 0005` then `upgrade head`: `relforcerowsecurity = true` for `quotes`, `quote_versions`, `approval_decisions` (and `audit_events`) after each; statement runs twice on a clean DB with 0 rows affected; `NO FORCE` restored in a `finally`; legacy shapes via `followups3::test_new14_*` (pass). The relaxation affects only the owner role used for migrations; the runtime role is always subject to RLS |
| NEW-15 platform-scope revocation rows | **FIXED** | live (`quotepilot_owner`, tenant scope bound): `--user-email` → NW row `count 3/scope user`; `--all` (12 sessions) → NW `count 9`, CO `count 3`; `--org NW` → `count 2/scope org`; `audit_events WHERE organization_id IS NULL` = **0**; `--org "x' OR '1'='1"` → non-zero exit (UUID cast error, parameterised), nothing revoked (`test_new15_malicious_org_…`). Residual: R4-1 |
| NEW-16 / R3-3 compose | **FIXED statically; not executed** | `.env.example` → temp `.env`; `docker compose --env-file .env -f infra/docker-compose.yml config` exit **0**; `api` environment = `AI_PROVIDER, APP_ENV, CORS_ORIGINS, DATABASE_ADMIN_URL="", DATABASE_URL (app role), FORWARDED_ALLOW_IPS, SECRET_KEY, STORAGE_DIR`; `web` = `API_INTERNAL_URL`; no `env_file` on either; `QP_OWNER_PASSWORD`/`POSTGRES_PASSWORD`/owner DSN only on `migrate` (and `POSTGRES_*` on `postgres`). Without `--env-file`: exit **1**, "required variable SECRET_KEY is missing" (fails closed). `scripts/check_compose.py` → `compose check: OK`, exit 0; mutation tests (env_file, owner password, owner DSN in `web`, non-empty admin URL) all exit 1. Residual: R4-2, R4-3 |
| NEW-17 / D2-1 editor re-save keeps `catalog` | **FIXED** | live: link catalog line 0.85 → re-send same price, `0.850000` → `catalog`, no blockers; `NaN`, `sNaN`, `Infinity`, `1e999`, `-1`, 7-decimal → **422** (never 500); lowered to 0.01 → `manual` + `manual_price_unconfirmed`, `below_min_margin`; re-sending list price does not launder it back to `catalog`; client-supplied `price_source` ignored; `unit_price: null` reprices to catalog; relink + old price re-sent → `manual` (no stale price kept as catalog) |
| I-1 `X-Forwarded-Port` | **FIXED** | live, web 3093 → echo stub: sent `X-Forwarded-Port/For/Proto/Host`, `X-Real-IP`, `Forwarded`, `X-Forwarded-Prefix` → only `x-forwarded-prefix` arrived (R4-4) |
| I-3 e2e flake on PR-triggered run | **Not tested** | no CI access; implementer reports it unresolved (R3-2) |
| `GET /catalogs/active-version` | OK | live + `test_round6_active_version_endpoint_…`: tenant-scoped (NW and Contoso get different ids), no cross-tenant listing (404 on the other tenant's version), 401 anonymous, 405 on non-GET, `null` when no active catalog |
| Cross-tenant on round-5/6 object paths | OK | live: Contoso product id linked from a Northwind quote → 404, Contoso GET/PATCH of a Northwind quote → 404, Contoso version listing as Northwind → 404 (`test_round6_quote_line_cannot_link_another_tenants_product`) |
| CSRF / role on changed routes | OK | live: PATCH `/quotes/{id}` without CSRF header → 403; `readonly` PATCH → 403; `sales` withdraw → 403 |
| `stale_catalog_lines` org filter (R3-7) | OK | read: `Product.organization_id == org.id` present at `quotes.py:129-131`; RLS is the second layer |
| Editor re-link (R3-6) | OK | read: product id comes from the server (`/catalogs/active-version`, then the version's product listing, exact SKU match); the browser sends no computed price (`price_touched: false` → `unit_price` omitted → server prices); no secret handling; static test `test_round6_editor_relink_sends_no_browser_computed_price` |
| Session cookie flags (spot check, unchanged code) | OK for `APP_ENV=test` | `qp_session` HttpOnly, SameSite=Lax, Path=/; `Secure` is derived from `APP_ENV` (absent in test, by design); `no-store`, CSP, `X-Frame-Options: DENY`, `nosniff` present on proxied responses |
| Web bundle secrets | OK | `scripts/security_scan.sh` bundle grep: no `sk-ant-`, `AKIA…`, `ANTHROPIC_API_KEY=` in `.next/static` |

## 4. Live probe transcripts (abridged)

Files (scratchpad, not committed): `…/scratchpad/sec4/live-out.txt`, `live2-out.txt`, `live3-out.txt`. Key lines:

```
bad price NaN|sNaN|Infinity|1e999|-1|0.0000001 -> 422
lowered manual 0.01 ['manual_price_unconfirmed', 'below_min_margin']
laundering attempt manual 0.85 ['manual_price_unconfirmed']
explicit null reprices catalog 0.85 []
link inactive-version product 422   ->  activate v2 -> blockers [('catalog_version_mismatch', 1)]  -> submit stale 409
relinked (editor style) 200 catalog 0.01 []        same product + old price -> manual 0.85 ['manual_price_unconfirmed']
contoso product as northwind link 404 | contoso reads northwind quote 404 | patch no csrf 403 | readonly patch 403
NEW-14: withdraw 200 status draft 'wrong terms' -> approve v2 -> status approved None [(1,'withdrawn'),(2,'approved')]; pdf v1 409, v2 200
R3-10: approve stale pending 409 ['catalog_version_mismatch']
revoke: --user-email revoked 3; --all revoked 12; --org revoked 2; audit rows per org; NULL-org rows 0
I-1 echo: {'x-forwarded-prefix': '/x'}
```

## 5. Tool results

| Tool | Command | Exit | Result |
|---|---|---|---|
| `scripts/security_scan.sh` | `./scripts/security_scan.sh` (writes `docs/evidence/scan-20261110-170807.md`, commit `f69ac25`) | 0 | see rows below |
| pip-audit | `.venv/bin/pip-audit --skip-editable` | 0 | No known vulnerabilities found |
| bandit | `.venv/bin/bandit -r app -q -f txt -ll` | 0 | no medium/high issues |
| ruff (S*) | `.venv/bin/ruff check app` | 0 | All checks passed |
| npm audit (prod) | `npm audit --omit=dev` | 0 | 0 vulnerabilities |
| npm audit (all) | `npm audit` | **1** | 8 moderate, all in the dev-only ESLint chain (`minimatch` via `@typescript-eslint/*`, `eslint-config-next`); fix needs `--force` (breaking); not in the production dependency tree |
| gitleaks | `gitleaks detect …` | n/a | **not installed** (not assumed clean; CI-only) |
| fallback secret grep | in script | 0 | only hit is the documented test pattern in `.gitleaks.toml` (allow-list entry), printed as "POSSIBLE SECRET FOUND" by the script's wording |
| semgrep / trufflehog / hadolint | — | n/a | **not installed** (CI-only) |
| bundle grep | in script | 0 | no patterns in `.next/static` |
| `ruff check app tests/security` + `ruff format --check` (new file) + `mypy app` | apps/api | 0 / 0 / 0 | clean; mypy "no issues found in 58 source files" |
| `docker compose --env-file .env -f infra/docker-compose.yml config` (CLI v5, no daemon) | repo root with temp `.env` (deleted afterwards) | 0 | rendered; no images built, nothing started |
| same without `--env-file` | — | 1 | `required variable SECRET_KEY is missing a value` |
| `scripts/check_compose.py <rendered>` | apps/api venv | 0 | `compose check: OK` |
| `pytest -q tests/security` (before my file) | apps/api, `quotepilot_sec` | 0 | **80 passed** |
| `pytest -q tests/security` (with `test_audit_followups4.py`, `.env` present) | apps/api | 0 | **97 passed, 2 xfailed** (2:01) |
| `pytest -q tests/security/test_audit_followups4.py` (no `.env`) | apps/api | 0 | 16 passed, **1 skipped** (docker-render test needs `../.env`), 2 xfailed |
| `alembic downgrade 0005` / `upgrade head` | apps/api | 0 | 0005b reapplied; FORCE RLS true on all three tables |

## 6. Test-file provenance (task 5)

`apps/api/tests/security/test_audit_followups3.py` is committed in `597f4b0` (added by the implementer, 495 lines) and
changed once more in `f69ac25`. A pristine copy of the delivered file is not available, so a byte comparison is **not
possible** (R4-5). By content, the file matches the declared edits and nothing further: no `xfail` markers remain (8
removed, round 5); `L2_5_DATA_FIX` loads `DATA_FIX` from migration 0005b (stale comment above it still says "0005",
cosmetic); `test_new13_archived_catalog_lines_…` asserts re-save 200 + blocker and submit 409 and still asserts the
security property; `test_new15_…` reads tenant-scoped rows (round 6) instead of `n_tup_ins`; the always-skipping
`test_new13_repro_inactive_product_…` is removed (round 6, as its docstring instructed). `git diff 597f4b0 f69ac25` for
the file is exactly the `test_new13_repro…` removal and the `test_new15` rewrite. The file passes (80 passed in the
security directory above). Note: `test_new15_…` now asserts a count of one in tenant scope, which is a weaker check
than the original `n_tup_ins` plus NULL-org check; the NULL-org property is covered again in
`test_new15_all_scope_writes_one_readable_row_per_org_…`.

## 7. Residual-risk table

| Risk | Status |
|---|---|
| R4-1..R4-4 (Info) | open; see § 2 |
| Compose topology never executed (no daemon); health check and the credential boundary are static/rendered only | open (X-09 human decision) |
| RFQ-pinned quotes may carry archived-version prices (O2-1) | documented, awaiting human acknowledgement |
| Lows awaiting human sign-off (L2-7) | not an auditor decision |
| I-3 flaky PR-triggered e2e | unexplained here; CI not accessed |
| gitleaks, semgrep, trufflehog, hadolint | not run locally; CI only |
| Dev-only `npm audit` moderates (8) | open, not in production dependencies |
| Cookies `Secure` flag, HSTS, TLS | not verifiable with `APP_ENV=test` over plain HTTP; unchanged code |

## 8. Not tested

- Docker image builds, `docker compose up`, container health checks.
- GitHub Actions results (CI, `security` workflow); branch protection.
- The browser UI of the relink control and the withdrawn banner (read statically; QA owns the Playwright suite).
- Migration 0005b on a database that genuinely predates round 4 (only the replay and legacy-shape simulations).
- Full re-run of the zip-bomb/PDF/CSV/SQLi/upload manual checks of earlier rounds (code unchanged in rounds 5/6; the
  security directory's earlier tests still pass).
- AI adapter (unwired, disabled by default), load/timing behaviour, TLS/HSTS, concurrency races between editor saves and
  catalog activation.
- No penetration test; this is not a certification that the system is secure.
