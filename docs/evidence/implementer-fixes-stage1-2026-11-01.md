# Implementer evidence — Stage 1 defect fix round (2026-11-01)

Inputs: `qa-stage1-2026-11-01.md` (defects D1–D8, observations O1–O5) and `security-stage1-2026-11-01.md`
(findings SEC-01…SEC-22). Product changes (all in this round's commit):

| Finding | Fix |
|---|---|
| D1/SEC-22, D3 | `exports/quote_exports.py::_check_exportable` — approved versions of expired or withdrawn quotes → 409 for PDF and CSV |
| D2/SEC-04 | `quotes.py` approve/reject refuse withdrawn quotes; `withdraw` supersedes a pending version |
| D4 | margin override only cleared when price/discount/product actually change |
| D5/SEC-03, O1 | last-owner guard counts only active owner memberships; only owners may deactivate/demote owners |
| D6/SEC-13 | logout requires the CSRF token |
| D7/SEC-06 | RFQ item and quote line quantities/prices bounded (≤ 1e7, 3/6 decimal places) → 422 instead of 500 |
| D8 | bullet prefix requires whitespace, so `-5` is a negative quantity (flagged) |
| O2 | most recently activated catalog is used for matching |
| O4 | pasted text above the configured limit → 413 (schema cap raised above the service limit) |
| SEC-01 | member creation only creates new accounts (generic 409 otherwise); activation is per membership (`memberships.is_active`, migration 0002); session revocation tenant-scoped |
| SEC-02 | RLS on `users` and `sessions` (migration 0002) with `app.login_email` / `app.session_token_hash` scopes |
| SEC-05 | web proxy drops client forwarding headers unless `WEB_TRUST_FORWARDED_HEADERS=true` |
| SEC-08 | NUL bytes stripped from query params |
| SEC-09 | post-login redirect restricted to same-origin paths |
| SEC-10 | dummy Argon2 verification for unknown accounts |
| SEC-11 | no cost copied across currencies on quote-line product changes |
| SEC-15 | uvicorn `--no-server-header`; OpenAPI disabled in production |
| SEC-16 | request id in body and header of unhandled 500s |
| SEC-17 | `quote.update` audit carries per-line pricing deltas |
| SEC-18/19 | formula-like flag for pasted lines; parser reports whether rlimits applied; max 4 concurrent parser processes |
| SEC-12/14/21 | accepted as Low, documented in KNOWN_LIMITATIONS.md |

Test adjustments (reported transparently): the QA tests asserted the old user-level activation flag and were
updated to the membership-level field (3 assertions); the security auditor's strict `xfail` markers for SEC-01/02/06/08/22
were removed because the tests now pass, and its `_admin_exec` helper gained an optional tenant context because the
owner role is subject to FORCED RLS on tenant tables.

## Runs (cwd apps/api unless stated; APP_ENV=test; DB quotepilot_test)
| Command | Result |
|---|---|
| `alembic downgrade base && alembic upgrade head && alembic downgrade -1 && alembic upgrade head` | OK (0001, 0002) |
| `ruff check app tests && ruff format --check app tests && mypy app` | clean / Success: no issues in 58 files |
| `.venv/bin/pytest -q -p no:cacheprovider --cov=app` | **240 passed**, 0 failed, 0 xfailed; coverage 89 % |
| `.venv/bin/pytest -q tests/security` | 52 passed |
| `npx tsc --noEmit && npx eslint . && npm run build` (apps/web) | clean, build OK |
| `E2E_SKIP_WEB_BUILD=1 npx playwright test` (tests/e2e) | **12 passed**, 1 skipped (screenshot spec) |
| `scripts/security_scan.sh` | `scan-20261101-213930.md`: pip-audit clean, bandit clean, ruff clean, npm prod audit clean, dev audit 16 moderate (tooling), gitleaks/semgrep not available locally (CI), secret greps clean |
