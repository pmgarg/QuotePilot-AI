# Implementer evidence - Stage 1 fix round 11 (2026-11-12)
Branch `s1-round8`, base f8ef581 (round 10 + re-verification 7 as delivered). Nothing here is marked VERIFIED; independent
re-verification 8 and a release review are still required. Environment for every run: `APP_ENV=test`, database
`quotepilot_s1r8` (never the default test database).

## Findings: what changed and what covers it
| ID | Status | Change (file:line) | Tests |
|---|---|---|---|
| SEC7-1 / SEC6-2 (High) | mitigated (see "Not fixed") | `app/services/match_worker.py` (new, stdin JSON -> one flushed JSON line per query, rlimits, stdlib + rapidfuzz only); `app/services/matching.py` `fuzzy_batch` (:139, in-process up to `IN_PROCESS_MAX_COMPARISONS = 50,000`, else worker with deadline, `MAX_CONCURRENT_MATCHERS = 2` bounded semaphore, partial stdout used on timeout), `_fuzzy_in_worker` (:94), `_parse_worker_lines` (:73), `CatalogIndex.prefetch` (:195), shared `_exact` helper (:163), description cap `MAX_DESCRIPTION_CHARS = 200` (:38); `app/services/rfqs.py:306` calls `prefetch` before the item loop; `core/config.py` `match_timeout_seconds` (default 8) | `tests/unit/test_round11_fixes.py` (identical hits/outcomes worker vs in-process, small jobs stay in-process, deadline + answered prefix + no leftover process, busy slots, <= 2 processes, start failure/garbage/torn lines, parser of worker output, bad worker input, description cap, budget in line order with duplicates); `tests/integration/test_round11_fixes.py::test_sec7_1_*` (API level, 30,000 products x 300 chars, 300 lines, health probe thread) |
| SEC7-7 (Medium) | fixed | `app/services/ratelimit.py` (new `ActionRateLimiter`, per user and action, sliding window, refused call not counted); `api/routers/rfqs.py:144,215` (create, re-parse), `api/routers/catalogs.py:61` (import); `main.py:88` `Retry-After`; settings `rfq_parse_rate_limit_attempts` 30, `catalog_import_rate_limit_attempts` 10, `expensive_rate_limit_window_seconds` 300 | unit `test_action_limiter_sliding_window`; integration `test_rfq_creation_is_rate_limited_per_user`, `test_reparse_is_rate_limited_and_forbidden_or_cross_tenant_calls_are_not_counted` (403 for readonly, 404 for another tenant's RFQ, neither counted), `test_catalog_import_is_rate_limited_per_user` (403 for sales, other tenant unaffected, no audit row or catalog from a refused request) |
| SEC7-2 (Medium) | fixed | `money.py:~262` comma-only branch: thousands reading needs a 1-3 digit first group; `1234,567` -> 1234.567, `1234,567,890` -> None | auditor `test_sec7_2_*` (marker removed), unit `test_sec7_2_*` |
| SEC7-3 (Low) | fixed | `money.py:237` zero-led first group rejected with space/apostrophe grouping | auditor `test_sec7_3_*` (marker removed), unit |
| SEC7-5 (Medium) | fixed (orchestrator change verified) | `money.py` strip + 128-char cap before the per-character scans, cached `_not_a_plain_digit` | auditor `test_sec7_5_*` (marker removed), unit `test_sec7_5_*` (1 MB cell of U+FDFA refused in microseconds) |
| SEC7-6 (Info) | fixed | `money.py:227` head of only dots and blanks rejected | unit `test_sec7_6_*` (`.5`, `$.50`, `Rs. 500`, `-.5` still parse) |
| SEC7-4 (Low), QA J7-3 | fixed | `fixtures/seed.py:78` `_read_demo_password` (only `DEMO_PASSWORD`; undecodable/malformed/missing/dir/dangling = absent; quotes, `export`, inline comment), `:107` `_password` (non-empty env, cwd `.env`, root `.env`; empty value does not mask) | auditor `test_sec7_4_*` (marker removed), unit seed tests |
| QA J7-2, J7-4 | orchestrator changes, kept | `rfqs.py:451` clears the flag on product selection; web `rfqs/[id]/page.tsx` | e2e spec `round11-sku-select.spec.ts` (not run here) |

## Measurements (A)
Measured with `tests/integration/test_round11_fixes.py::_bulk_products` (rows bulk-inserted as the owner role, as the auditor
did) and 450 adversarial pasted lines of about 190 characters, sales user, `POST /rfqs`; a second thread polled
`GET /health` (own client) during the request.
| Case | Request time | Lines matched / flagged | Health probe during the request (count, max, mean) |
|---|---|---|---|
| worker, 30,000 products x 300 chars, 450 lines, deadline 8 s | 11.58 s (about 3.6 s is loading products and storing items) | 51 / 399 | 579, 0.341 s, 0.010 s |
| worker, 30,000 x 60 chars, 450 lines | 11.15 s | 15 / 435 | 591, 0.523 s, 0.008 s |
| contrast: matcher forced in-process (`IN_PROCESS_MAX_COMPARISONS = 10^12`), 20,000 x 60 chars, 100 lines | 39.92 s | 99 / 1 | 33, 11.161 s, 1.152 s |
Auditor's figure before the change for 30,000 x 300 chars: 389.9 s. Unit-level deadline check (4 s deadline, 20,000 x 200
chars, 500 queries): `prefetch` returned after 4.006 s with 14 queries answered. Descriptions are capped at 200 characters
before matching; a 300-character description previously cost 0.17-0.29 ms per comparison, at 200 characters 4-10 us.
Assertions written from these numbers: API test request < 40 s (measured 9 s with a 4 s deadline), health max < 3.0 s
(measured 0.3-0.5 s, 11 s in-process); unit deadline test < deadline + 3 s (measured +0.01 s), at least 1 and fewer than
all queries answered (measured 14 of 500 with 4 s).

## Auditor's SEC7-1 / SEC6-2 tests with the markers removed
- `test_sec6_2_matching_a_long_description_against_a_large_catalog_is_bounded` (followups7): FAILS without the marker, so
  the xfail stays. It measures `index.match` on a 30,000-product index below 20 ms per call (3 calls, single line). A
  single-line job is 30,000 comparisons, below the 50,000 in-process limit, and costs about 144 ms; a process hop
  (about 0.1 s start-up plus shipping 30,000 descriptions) cannot meet 20 ms either. The property that matters
  (the API is not held) is covered by the integration test above.
- `test_sec7_1_one_request_at_the_matching_budget_is_bounded_in_seconds` (followups8): per-line latency of one `index.match`
  on 4,000 products x 300 chars is 18-28 ms, the test requires < 20 ms (`per_line x 500 < 10 s`). It passed on one run and
  failed on others, so a strict xfail would fail the suite on a fast machine. I kept the xfail and relaxed `strict=True`
  to `strict=False` (decorator only, declared below). Its premise (request time = budget x per-line latency) no longer
  holds: the request is bounded by the deadline, not by the budget.

## Verifier test edits (declared)
| File | Edit |
|---|---|
| `apps/api/tests/security/test_audit_followups8.py` | removed the strict-xfail decorators (decorator only, no assertion touched) of `test_sec7_2_*`, `test_sec7_3_*`, `test_sec7_5_*`, `test_sec7_4_*`; changed `strict=True` to `strict=False` (comment added) on `test_sec7_1_*` as explained above |
| `tests/e2e/specs/qa-reverify-review7.spec.ts` | one expectation: `/No catalog candidate/` -> `/Automatic matching was not run/` |
| `apps/api/tests/conftest.py` (shared infra, not a verifier assertion) | `RFQ_PARSE_RATE_LIMIT_ATTEMPTS` and `CATALOG_IMPORT_RATE_LIMIT_ATTEMPTS` default to 1,000,000 in the test environment; `action_limiter.reset()` per test |
| `tests/e2e/playwright.config.ts` (infra) | API env: both limits 100,000 and `MATCH_TIMEOUT_SECONDS=120` so the browser suite (many RFQs for one user; 5,000 products x 405 lines) hits the comparison budget and not the 8 s deadline. Not run here |
| `apps/api/tests/unit/test_round9_fixes.py` (my own earlier test) | `test_seed_never_prints_or_generates_a_password` no longer patches `seed_module.get_settings` (removed) and runs in a temporary working directory |

## Verifier tests that now fail because they pin behaviour I was told to change (NOT edited; for the lead)
1. `tests/security/test_audit_followups8.py::test_info_sec7_6_punctuation_only_head_is_accepted_documented`:
   `assert parse_decimal(". 5") == Decimal(5) and parse_decimal("..-5") == Decimal(-5)` (SEC7-6 fix rejects both).
2. `tests/security/test_audit_followups8.py::test_seed_root_env_precedence_and_robustness`, last block: for a root `.env`
   with invalid UTF-8, `assert r.returncode == 1 and pw not in r.stdout + r.stderr` (the file is now treated as absent: exit 2).
3. `tests/qa/test_qa_reverify_review7.py::test_j7_2_flag_match_skipped_budget_is_stale_after_manual_resolution_observation`:
   `assert "match_skipped_budget" in src and src.count("match_skipped_budget") == 2` (rfqs.py now has the flag in a third place,
   the orchestrator's J7-2 change).
4. `tests/qa/test_qa_reverify_review7.py::test_j7_3_seed_empty_environment_value_masks_the_env_files_judgement` (line 588):
   `assert r.returncode == 2 and r.stdout == ""` for `DEMO_PASSWORD=` set but empty in the environment with a valid root file
   (an empty value no longer masks the files: exit 0).
Flaky QA test `test_m3_confidential_floor_is_not_in_the_quote_payload_for_sales_and_readonly`: see the runs table.

## Not fixed / residual
- Per-call latency assertions of the two auditor tests above (cannot be met by a process hop; see above).
- In-process jobs up to 50,000 comparisons still run in the API process (worst case measured about 15-17 us per comparison,
  so up to roughly 1 s of GIL-held CPU).
- A parse holds its database transaction while waiting for the worker (up to the 8 s deadline).
- Limits (rate, matcher slots) are per API process. Quote generation and exports are not rate limited.
- The seed still builds its database session from `Settings`, so an invalid value in the working-directory `.env` can still
  fail the database setup there (only the root file and the password lookup were in scope of SEC7-4).
- Behaviour change: `1234,567` is now read as 1234.567 (was 1,234,567); it is documented as a decimal reading, not a guess of thousands.

## Behaviour changes a consumer or user notices
- Large RFQ parses return within about the deadline plus loading time; lines past the deadline/budget/busy matcher come
  back `unmatched`, no candidates, flag `match_skipped_budget` (selection by SKU or custom line still possible).
- 429 with `Retry-After` and the usual error body (`error: rate_limited`) after 30 RFQ creations/re-parses or 10 catalog imports per user per 5 minutes.
- Parser: `1234,567` -> 1234.567; `1234,567,890`, `0 500`, `05 050`, `. 5`, `..-5` are now rejected (row error / unreadable quantity).
- Catalog descriptions longer than 200 characters are matched on their first 200.
- Seed: empty `DEMO_PASSWORD` in the environment no longer masks `.env` files; undecodable `.env` is ignored.

## Runs
All from `apps/api` with `.venv/bin/...`, `-p no:cacheprovider`, environment as above.
| Command | Exit | Result |
|---|---|---|
| `pytest -q tests/unit/test_round11_fixes.py` | 0 | 56 passed |
| `pytest -q tests/integration/test_round11_fixes.py` | 0 | 6 passed |
| `pytest -q tests/security/test_audit_followups*.py tests/qa/test_qa_reverify_review*.py -rfExs` (before the round-9 unit-test fix) | 0 as reported by the pipe (pytest itself failed) | 4 failed, 939 passed, 2 skipped, 1 xfailed, 1 xpassed in 549 s; the 4 failures are the pinned tests listed above; the xpass is `test_sec7_1_*` (non-strict) |
| `pytest -q tests/unit tests/integration/test_round11_fixes.py` | 1 | 1 failed (`test_round9_fixes.py::test_seed_never_prints_or_generates_a_password`, my own test patching a removed name; fixed), 215 passed |
| `pytest -q tests/unit/test_round9_fixes.py` (after the fix) | 0 | 61 passed |
| `pytest -q -rfExs` (FULL API suite, once, final tree) | 1 | 4 failed, 1346 passed, 2 skipped, 2 xfailed in 822 s. Failures: the four pinned verifier tests listed above (nothing else). xfailed: `test_sec6_2_*`, `test_sec7_1_*`. Skips: the two compose/.env checks (no `.env` file in the worktree). The flaky QA test `test_m3_confidential_floor...` passed in this run |
| `ruff check app tests` | 0 | All checks passed |
| `ruff format --check app tests` | 0 | 102 files already formatted |
| `mypy app` | 0 | Success: no issues found in 60 source files |
| `npx tsc --noEmit` (apps/web) | 0 | no output |
| `npx eslint .` (apps/web) | 0 | no output |
| `docs/openapi.json` regenerated as the Makefile target does | - | identical (no route or schema change) |
Not run here: `next build`, Playwright (the specs `round11-sku-select.spec.ts` and the edited `qa-reverify-review7.spec.ts` are unrun).

## Orchestrator addendum — four verifier expectations updated (declared)
Four verifier tests documented behaviour that round 11 changed on purpose (each one pinned a finding or judgement call
of re-verification 7 "as is"). The implementer stopped and reported them; the orchestrator changed exactly these
assertions, each marked with a comment in the test:
- `tests/security/test_audit_followups8.py::test_info_sec7_6_…`: `. 5` and `..-5` are now rejected (was: accepted, documented).
- `tests/security/test_audit_followups8.py::test_seed_root_env_precedence_and_robustness`: an undecodable root `.env`
  is treated as absent — exit 2 without a traceback (was: crash, exit 1); "no password in any output" is unchanged.
- `tests/qa/test_qa_reverify_review7.py::test_j7_2_…`: the module now mentions the flag three times because
  `update_item` clears it (was: two — the stale-flag observation).
- `tests/qa/test_qa_reverify_review7.py::test_j7_3_…`: an empty `DEMO_PASSWORD` in the environment no longer masks
  the `.env` files — exit 0, nothing secret printed (was: exit 2).
After the edits: `pytest tests/security/test_audit_followups8.py tests/qa/test_qa_reverify_review7.py` → 126 passed,
1 xpassed (the non-strict SEC7-1 marker). The full-suite and browser results on the final tree are in the gate log.
