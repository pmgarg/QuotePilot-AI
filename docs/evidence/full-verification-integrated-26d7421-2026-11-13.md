# Full local verification of the integrated tree — 26d7421 (2026-11-13)

Requested by the repository owner ("run and verify everything"). Run by the orchestrator (not an independent
verifier, not a release review) on branch `claude/stage4-governed-platform` at 26d7421, which contains the heads of
Stage 1 (`79c5571`), Stage 2 (`0ce6524`) and Stage 3 (`84b6671`) by merge — checked with `git merge-base --is-ancestor`.
Fresh database `quotepilot_verify` (PostgreSQL 16, app connects as the non-owner role so RLS is enforced).
Machine: 4 cores, local container. No paid service, hosted model, e-mail, SMS, S3 or payment provider was called.

| # | Command | Exit | Result (quoted) |
|---|---|---|---|
| 1 | `make api-lint` (ruff check, ruff format --check, mypy) | 0 | `All checks passed!` · `178 files already formatted` · `Success: no issues found in 97 source files` |
| 2 | `cd apps/api && uv run pytest -q -p no:cacheprovider -rfEsxX` (whole API suite, all four stages) | 0 | `1985 passed, 2 skipped, 6 xfailed in 1892.24s (0:31:32)` |
| 3 | `make web-check` (tsc --noEmit, eslint, next build) | 0 | build lists all routes incl. `/command-center`, `/workflows`, `/platform`, `/insights`, `/orders`, `/follow-ups` |
| 4 | `cd tests/e2e && npx playwright test --trace off --reporter=list` (API + built web app, seeded synthetic demo data, ports 8097/3097) | 0 | `76 passed (3.6m)` · `1 skipped`; no flaky or failed line |
| 5 | `make security-scan` → `docs/evidence/scan-20261113-143524.md` | 0 | pip-audit: `No known vulnerabilities found`; bandit (medium and above): exit 0; ruff S rules: exit 0; `npm audit --omit=dev`: `found 0 vulnerabilities`; full `npm audit`: `8 moderate severity vulnerabilities` (dev-only eslint tool chain, exit 1 inside the script); gitleaks and semgrep **not available locally** (they run in CI) |
| 6 | temporary smoke walk (not in the repository): owner logs in to the running app and opens dashboard, quotes, follow-ups, customers, orders, insights, command center, workflows, platform; asserts no page error and no 5xx API response | 0 | `1 passed (16.6s)`; nine screenshots handed to the owner |

## Detail for run 2
Skipped (2): `tests/security/test_audit_followups4.py:402` and `test_audit_followups5.py:474` — both need a repo-root
`.env` (compose checks). The three git-history checks that skip in CI run here, which settles the 5-versus-2 skip
difference noted in `ci-stage4-7ee64c4-2026-11-13.md`.

Expected failures (6), each an open, documented finding — none is a Stage 4 test:
- SEC6-2 (Stage 1, matching cost residual) · SEC7-1 (Stage 1, time bound at the matching budget; passes on the faster
  CI runner, where it shows as `xpassed`) · SEC8-1 (Stage 1, Low)
- S2-SEC-06 (Stage 2, one shared SMTP credential) · S2-SEC-12 (Stage 2, Low, log redaction of over-long addresses)
- SEC3-C3-1 (Stage 3, Low, quote PDF prints the name of a customer pending erasure until the purge)

Total collected 1993, the same as CI on 7ee64c4 (`1982 passed, 5 skipped, 5 xfailed, 1 xpassed`).

## What this run does not show
- It is the implementing side's own run. Gate status is unchanged: every stage is PASS WITH CONDITIONS, nothing is
  VERIFIED (see `docs/RELEASE_GATES.md`).
- Not exercised: `docker compose` deployment, a real hosted model, real e-mail/SMS/S3/payment providers, backups and
  restore, load/performance, a manual accessibility audit, the real worker process loop (driven in-process in tests).
- Open human decisions and deferred checks are listed in the gate log; this run does not close any of them.
