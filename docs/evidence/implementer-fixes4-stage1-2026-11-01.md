# Implementer evidence — Stage 1 fix round 4 (after independent release review 2 of 60adb00)

| Finding | Fix | Regression test |
|---|---|---|
| N2-1 A | re-linking a product priced in another currency clears price and cost and sets `price_source="none"` (manual price → confirmation) | `test_review2_fixes::test_n2_1a_…` |
| N2-1 B | editor links accept only products of the quote's pricing catalog version (snapshot) or the active version; 422 otherwise, atomic | `test_n2_1b_…` |
| N2-2 | withdrawal marks EVERY approved version `withdrawn` (decision per version) and is allowed while a newer draft is open (draft stays editable) | `test_n2_2_withdrawal_covers_every_approved_version…`, `test_n2_2_withdraw_while_a_draft_is_open…` |
| L2-1 | three NEW-7 redirect variants added to the browser spec | `review1-regressions.spec.ts` |
| L2-2 | compose `api` service no longer receives the owner DSN | `infra/docker-compose.yml` |
| L2-4 | migration 0005: BEFORE UPDATE triggers make session/user identity columns immutable (even for the owner role) | `test_l2_4_session_and_user_identity_columns_are_immutable…`, existing NEW-5 tests |
| L2-5 | migration 0005 data fix: approved versions of previously withdrawn quotes become `withdrawn` | migration |
| L2-3 / L2-6 | KNOWN_LIMITATIONS note; DATA_MODEL/API_CONTRACT version statuses; compose comment; traceability counts; reconstructed `implementer-fixes2` record; test docstring | docs |
| L2-7 | left for the human owner (RELEASE_GATES "Human actions required") | — |

## Runs (cwd apps/api unless stated; APP_ENV=test; DB quotepilot_test)
| Command | Result |
|---|---|
| `alembic upgrade head && downgrade -1 && upgrade head` (0005) | OK |
| `ruff check app tests && ruff format --check app tests && mypy app` | clean / Success: no issues in 58 files |
| `.venv/bin/pytest -q -p no:cacheprovider --cov=app` | **298 passed**, 0 failed; coverage 89 % |
| `E2E_SKIP_WEB_BUILD=1 npx playwright test` (tests/e2e, DB quotepilot_sec, ports 8071/3071) | **21 passed**, 1 skipped |
| `scripts/security_scan.sh` | run after commit (see the scan file with this commit's hash) |
