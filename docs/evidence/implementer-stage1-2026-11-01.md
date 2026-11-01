# Implementer evidence — Stage 1 (2026-11-01)

Environment: Claude Code cloud container, Ubuntu 24.04, Python 3.13.18, PostgreSQL 16.15 (local service,
roles `quotepilot_owner` / `quotepilot_app`), Node 22.23.4, Playwright 1.63.0 with bundled Chromium.
No Docker daemon available (compose not executed). Hosted AI provider disabled (`AI_PROVIDER=disabled`).

## Commands and results (verbatim exit status / counts)
| # | Command (cwd) | Result |
|---|---|---|
| 1 | `scripts/db_setup.sh` | roles + databases created, exit 0 |
| 2 | `uv run alembic upgrade head && alembic downgrade base && alembic upgrade head` (apps/api, DATABASE_ADMIN_URL=…/quotepilot_test) | 3 runs OK; RLS enabled+forced on tenant tables (verified via `pg_class.relforcerowsecurity`) |
| 3 | `.venv/bin/ruff check app tests && ruff format --check app tests` (apps/api) | All checks passed |
| 4 | `.venv/bin/mypy app` (apps/api, strict) | Success: no issues found in 58 source files |
| 5 | `.venv/bin/pytest -q --cov=app --cov-report=term` (apps/api) | **104 passed**, 0 failed, 0 skipped in 71.6 s; coverage TOTAL 86 % (3454 stmts, 480 miss) |
| 6 | `.venv/bin/pytest -q tests/unit` | 42 passed (no DB) |
| 7 | `.venv/bin/pytest -q -m security` | included in #5 (security-marked tests: tenant isolation incl. raw-SQL RLS, RBAC matrix, CSRF, headers/cookies, parser abuse, logging redaction) |
| 8 | `npx tsc --noEmit && npx eslint . && npm run build` (apps/web) | tsc clean; ESLint clean; build OK (11 routes + runtime API proxy route) |
| 9 | `E2E_SKIP_WEB_BUILD=1 npx playwright test` (tests/e2e; boots uvicorn :8011 + `next start` :3011 against seeded `quotepilot_test`) | **6 passed** in 16.6 s: full flow S1-A20 (sales → approver, PDF bytes identical on repeat), concurrent-edit 409 banner, read-only permission states, invalid login, UI catalog import + unsupported file, XSS fixture inert |
| 10 | `scripts/security_scan.sh` | see `scan-20261101-203701.md`: pip-audit 0 known vulns; bandit 0 medium/high; ruff S* clean; `npm audit --omit=dev` 0; `npm audit` (dev tooling) 16 moderate (transitive `minimatch` via eslint-config-next, not shipped); gitleaks + semgrep **not available locally** (run in CI); fallback secret grep clean; built bundle contains no key patterns |
| 11 | OpenAPI export → `docs/openapi.json` | 4319 lines; CI diffs against the live schema |

## Not done / limitations observed by the implementer
- Docker Compose topology not executed (no daemon).
- gitleaks/semgrep not run locally; CI workflows include them but CI has not been observed yet at the time of writing.
- The in-process login rate limiter is single-replica.
- Hosted AI adapter code path (`AnthropicProvider`) is exercised only through unit tests of its parser/redaction;
  no live model call was made (no key, no spend).
