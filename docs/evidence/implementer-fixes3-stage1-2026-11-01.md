# Implementer evidence — Stage 1 fix round 3 (after independent QA + security re-verification of ff99762)

Inputs: `qa-stage1-reverify-2026-11-01.md` (R-1..R-5, O-R1..O-R5) and `security-stage1-reverify-2026-11-01.md`
(NEW-1..NEW-12, G-1). Both agents confirmed H-1, M-1, M-3 (payload), M-4, L-1, L-3, L-4, L-5, L-10, X-05 fixed.

| Finding | Fix | Regression test |
|---|---|---|
| R-3 High / R-4 | quote line edits delete removed lines first, park survivors on negative numbers, then renumber (no `uq_quote_line_no` collision) | qa `test_editor_remove_of_a_non_last_line…`, `test_replacing_a_line…`; browser "removing a line" |
| R-1 / NEW-1 Medium | withdrawing an approved quote marks the version `withdrawn` (new version status); exports refuse withdrawn versions for good; new versions may start from them | qa `test_withdrawn_approved_version_blocks_every_export…`, security NEW-1 test |
| R-2 Medium | `pricing_snapshot.min_margin_pct` stripped for roles without `view_costs` (detail + history) | qa `test_m3_confidential_floor_is_not_in_the_quote_payload…` |
| R-5 Low | underlined "Manage catalog" link (axe link-in-text-block) | browser axe spec incl. RFQ review page |
| NEW-2 Medium | Dockerfile CMD exec-form comment removed (comment moved above) | security NEW-2 test parses the CMD JSON |
| NEW-3 Medium | `scripts/revoke_sessions.py --org` binds the tenant for the audit insert | security NEW-3 test |
| NEW-4 Medium | marking a line custom clears product, cost and sets manual price source | security NEW-4 test |
| NEW-5 Low | migration 0004: column-level UPDATE grants on `sessions` (last_seen_at, revoked_at) and `users` (full_name, password_hash, updated_at); DELETE revoked | security NEW-5 / L-1 tests (accept permission-denied) |
| NEW-6 Low | only owners may activate or deactivate owner memberships | security NEW-6 test |
| NEW-7 Medium | `safeNext` rejects dot segments, double slashes and encoded variants after decoding | browser SEC-09 spec |
| NEW-8 Info | proxy no longer synthesises `X-Forwarded-Proto` | code |
| NEW-9 / NEW-10 / NEW-11 / NEW-12 | runbook lists every proxy hop; consent audit carries before/after; stronger weak-secret heuristic; limiter/parser limits documented | docs, `test_l10_weak_secret…` |
| O-R4 | owner-only denials now 403 (contract) — 11 test expectations updated accordingly | integration/qa/security suites |

Test adjustments (transparent): tests encoding 422 for owner-only denials → 403 (contract alignment); one QA test now
expects `withdrawn` for a withdrawn approved version (intended, NEW-1/R-1); one QA test asserts the floor is hidden
from Sales and visible to Admin (R-2); two security tests accept "permission denied" from the new column grants; the
six strict xfail markers for NEW-1..NEW-6 were removed because the findings are fixed.

## Runs (cwd apps/api unless stated; APP_ENV=test; DB quotepilot_test)
| Command | Result |
|---|---|
| `alembic upgrade head && downgrade -1 && upgrade head` (0004 hardening) | OK |
| `ruff check app tests && ruff format --check app tests && mypy app` | clean / Success: no issues in 58 files |
| `.venv/bin/pytest -q -p no:cacheprovider --cov=app` | **293 passed**, 0 failed, 0 xfailed (incl. 28 QA re-verify + 13 security re-verify tests); coverage 89 % |
| `npx tsc --noEmit && npx eslint . && npm run build` (apps/web) | clean |
| `E2E_SKIP_WEB_BUILD=1 npx playwright test` (tests/e2e, DB quotepilot_sec, ports 8071/3071) | **21 passed**, 1 skipped (incl. 5 QA re-verify browser tests) |
| `scripts/security_scan.sh` | `scan-20261101-230340.md`: pip-audit clean, bandit clean, ruff clean, npm prod audit clean, dev audit 16 moderate (tooling), secret greps clean, gitleaks/semgrep CI-only |
