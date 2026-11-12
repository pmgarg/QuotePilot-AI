# QA confirmation, Stage 1 fix round 11 (2026-11-12)
Scope: `git diff f8ef581..23c1eb3`, HEAD 23c1eb3. Narrow check, not an exploratory campaign. Product code not modified.
Environment: `APP_ENV=test`, DB `quotepilot_qa` (app role `quotepilot_app`, owner role for migrations; URLs redacted),
PostgreSQL local service, Playwright with `E2E_API_PORT=8091 E2E_WEB_PORT=3091`, `--trace off`. Not run: full API or browser suites.

## Checklist
| # | Verdict | Evidence |
|---|---|---|
| 1 | CONFIRMED | `tests/qa/test_qa_reverify_review7.py` 85 passed with `integration/test_round11_fixes.py`, 0 skipped (git history available, so the 2 history-based differentials ran). Own test also compared in-process results to worker results (item 2) |
| 2 | CONFIRMED (one unreproduced flake, see Observations) | `test_large_request_matches_like_small_requests_and_flags_skipped_lines`: 5,000 bulk products + demo catalog, 60 pasted lines (> 50,000 comparisons) -> 201; status, SKU, score, rank and every candidate field equal to the same lines sent in 8-line requests (in-process); exact-SKU lines (`SKU-1001`, `BULK-77`) are `exact`; with `match_timeout_seconds=0.05` some lines carry `match_skipped_budget` (unmatched, no candidates) and exact SKUs still match; `pgrep -f match_worker` empty after each request and after the run |
| 3 | CONFIRMED (existing tests, run here) | `integration/test_round11_fixes.py` 6 passed: N+1th create/upload/re-parse -> 429, `Retry-After` int in [1, window], `error: rate_limited` + `request_id`; refused call writes no audit row/RFQ/catalog and is not counted; other user and other tenant unaffected; 403 (readonly, sales on import) and 404 (foreign RFQ) never counted. Not re-implemented by me |
| 4 | CONFIRMED | `test_parser_cells_direct`: `1234,567`=1234.567; `1234,567,890`, `0 500`, `05 050`, `. 5`, `..-5` rejected; `.5`, `$.50`, `Rs. 500`, `1,234`, `1,234.56`, `1.234,56`, `1 234,56`, `0,500`, `12,5` as expected. `test_parser_cells_through_catalog_import`: one CSV with all 15 cells -> import report 15 rows, 10 imported, 5 skipped with "list price is not a number" (rows 3-7 = the five rejected cells); the 10 stored list prices equal the expected values |
| 5 | CONFIRMED | 4 subprocess tests (`python -m app.fixtures.seed`, my DB only): empty env + cwd `.env` + root `.env` -> cwd password used, root password not; non-empty env beats both files; empty env + no cwd file -> root file used; undecodable root `.env` -> exit 2, no `Traceback`. No test password in any stdout/stderr. Root `.env` removed by the context manager (verified absent) |
| 6 | CONFIRMED | Playwright, 2 specs, 2 passed: `round11-sku-select` (no-suggestion line, wrong SKU refused with message, `sku-1001` resolves the line, Clear returns to unresolved) and `qa-reverify-review7` (skipped line shows flag and "Automatic matching was not run") |
| 7 | CONFIRMED | `git diff f8ef581..23c1eb3 -- apps/api/tests/qa tests/e2e/specs`: only (a) `_old_matching_module` skip when `git show` fails (assertions untouched), (b) j7_2 count 2 -> 3, (c) j7_3 `returncode == 2 and stdout == ""` -> `== 0 and PW1 not in output`, (d) e2e text `/No catalog candidate/` -> `/Automatic matching was not run/`, (e) new spec `round11-sku-select.spec.ts`. No test deleted or skipped otherwise. (The other two declared edits are in `tests/security/`, outside this diff filter and not re-checked) |
Items 7(b), 7(c) and 7(d) weaken nothing: each follows a behaviour change the round-11 notes declare (flag cleared on selection, empty env no longer masks files, new message).

## Runs
| Command | cwd | Exit | Result |
|---|---|---|---|
| `.venv/bin/pytest -q -p no:cacheprovider tests/qa/test_qa_reverify_review7.py tests/integration/test_round11_fixes.py -rs` | apps/api | 0 | 85 passed, 0 failed, 0 skipped, 71 s |
| `.venv/bin/pytest -q -p no:cacheprovider tests/qa/test_qa_confirm_round11.py` (final file, run once more after the last edit) | apps/api | 0 | 7 passed in 13.6 s |
| same file, `-k large`, repeated 18 times | apps/api | 0 each | 18 passed (about 6 s each) |
| same file, first complete run, before I fixed my own test bugs | apps/api | 1 | 1 passed + 3 failed: parser import test (my bytes/str bug and wrong products endpoint), seed test (my test assumed passwords of existing accounts change; they do not), and the large test (see Observations) |
| `npx playwright test specs/round11-sku-select.spec.ts specs/qa-reverify-review7.spec.ts --reporter=list --trace off` (`DATABASE_URL`, `DATABASE_ADMIN_URL` = the quotepilot_qa URLs) | tests/e2e | 0 | 2 passed, 0 failed, 20.9 s |
Evidence types: pytest runs are unit/integration (API in process, real PostgreSQL, real worker subprocesses, seed as subprocess); the last row is the only browser evidence.

## Defects
None confirmed in the round-11 diff.

## Observations
- O1 (unresolved, possibly a defect in the round-11 diff, Low): in my first run of the large-request test, one pasted line (`27 pcs hex bolt size 999 steel zinc`, status `ambiguous`) had a candidate list from the worker path that differed at index 1 from the in-process list (truncated output; I did not capture the differing field). 18 reruns with the same input (including full-dict comparison) did not reproduce it. Candidate causes, not established: tie order among equal scores (many synthetic products score alike) depending on product load order, or a worker/in-process difference. If it recurs it needs a precise capture; I could not give one.
- O2: the test `test_seed_*` tests rely on `app` fixture resetting accounts per test; the seed keeps passwords of already existing accounts (the seed prints this), so precedence is checked on a fresh database only.
- O3: the CI note in the implementer evidence (head not green before 23c1eb3) is not something I checked.

## Not checked
- The full API suite, full browser suite, ruff/mypy/tsc/next build (recorded by the orchestrator at 8cfcff2, not re-run).
- Concurrency of three parallel big parses, memory/rlimit behaviour of the worker, `Retry-After` value precision beyond the range check.
- The security-test edits in `tests/security/test_audit_followups8.py` (outside the requested diff filter).
- Whether the CI on 23c1eb3 is green.
- Rate-limit counters per API process (multi-process deployments), documented by the implementer as residual.
