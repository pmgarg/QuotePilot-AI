# QA (independent) re-verification 4 — Stage 1 @bf05198 (fix round 7)

Date 2026-11-12 (UTC 03:05-03:50). Scope: the QA items of "Required retest" in `release-review-4-stage1-2026-11-10.md`
(one full pytest invocation; browser remove-then-re-link) plus the round-7 changes (`money.parse_decimal` scientific
notation; quote editor paired by id, `is_custom` reset, fieldset locked while saving). I did not implement round 7.
No product code was changed; nothing was committed. Tests are derived from the review text, PRD S1-F01/S1-F11/S1-F12/X-01
and CLAUDE.md ("never invent prices"), not from the implementation.

## Environment
| item | value |
|---|---|
| commit / branch | `bf05198` / `claude/multi-stage-project-impl-8hz2wj`; untracked: my 2 test files, the security agent's `tests/security/test_audit_followups5.py` and `docs/evidence/scan-20261112-031237.md` (not mine) |
| OS / DB | Linux 6.18; local PostgreSQL 16 (service, no Docker); database `quotepilot_qa`, schema dropped and recreated before the run |
| `DATABASE_URL`, `DATABASE_URL_TEST` | `postgresql+psycopg://quotepilot_app:***@localhost:5432/quotepilot_qa` (non-owner, RLS enforced) |
| `DATABASE_ADMIN_URL`, `DATABASE_ADMIN_URL_TEST` | `postgresql+psycopg://quotepilot_owner:***@localhost:5432/quotepilot_qa` |
| API tests | `apps/api/.venv` (Python 3.13), `-p no:cacheprovider` |
| Browser | Playwright Chromium, `APP_ENV=test`, `E2E_API_PORT=8091`, `E2E_WEB_PORT=3091`; web app built by me (`npm run build`, exit 0). Machine shared with at least 3 other suites (stage 2 / stage 4 / security runs were live). |

## Runs
| # | Command (cwd) | Exit | Result |
|---|---|---|---|
| 1 | `alembic upgrade head`; `alembic downgrade 0005`; `alembic upgrade head` (apps/api, fresh schema) | 0 / 0 / 0 | `0005b (head)` -> `0005` -> `0005b (head)` |
| 2 | `npm run build` (apps/web) | 0 | succeeded |
| 3 | `.venv/bin/pytest -q -p no:cacheprovider` (apps/api; FIRST full run, 03:10-03:26) | 1 | 21 failed, 484 passed, 1 skipped, 4 xfailed (942 s). **Contaminated, superseded by run 8**: the security agent created `tests/security/test_audit_followups5.py` during the run (mtime 03:12:21, after collection of other files started), 12 of its tests failed in that run and all pass/xfail when re-run alone (run 4); the 9 remaining failures were my defect tests. |
| 4 | `pytest -q -p no:cacheprovider tests/security/test_audit_followups5.py` | 0 | 68 passed, 1 skipped, 16 xfailed |
| 5 | `npx playwright test specs/qa-reverify-review4.spec.ts` (tests/e2e; development) | 1 | 4 passed, 2 failed — both test-side: `toBeDisabled` does not treat a `<fieldset disabled>` as disabled in Playwright (changed to an attribute check); the "custom line" test assumed a custom line can be stale (it cannot, see O4-2). Rewritten. |
| 6 | `npx playwright test` full suite (03:30-03:34) | 1 | 37 passed, 1 failed, 1 skipped: my in-flight-save test read the API before its own last save finished (test race, fixed by waiting for the PATCH response). Not a product failure. |
| 7 | `npx playwright test --reporter=list` full suite, final (03:33:49, tests/e2e) | **0** | **38 passed, 0 failed, 0 flaky, 1 skipped** (`screenshots.spec.ts`, self-skipped by design) (3.5 min) |
| 8 | **`.venv/bin/pytest -q -p no:cacheprovider` (apps/api; ONE invocation, whole suite, final; 03:47-03:57)** | 1 | **`14 failed, 505 passed, 2 skipped, 18 xfailed in 566.29s (0:09:26)`**. All 14 failures are in my new module and are the two defects below (D4-1: 5, D4-2: 9). No other module failed. |
| 9 | `pytest -q -rs tests/security tests/unit` (skip reasons) | 0 | 222 passed, 2 skipped, 18 xfailed. Skips: `test_audit_followups4.py:402` and `test_audit_followups5.py:415`, both "docker compose config unavailable / needs repo-root `.env`" (RR4-5, environment). |
| 10 | `ruff check` and `ruff format` on `tests/qa/test_qa_reverify_review4.py` | 0 | clean |

The 18 xfails are the security agent's strict xfails (SEC4-x, documented defects), not tests I weakened; I did not touch them.
Evidence levels: runs 1, 3-4, 8-9 are integration/unit (API + PostgreSQL + RLS via ASGI TestClient, alembic); runs 5-7 are
browser evidence (Chromium, real uvicorn + Next.js). The unit tests of `parse_decimal` are not evidence for the browser.

Environment incident: one Playwright invocation (my development run after run 5) was started without exporting the DB
variables and so pointed at the default `quotepilot_test` database; `alembic upgrade head` aborted at once
(`Can't locate revision identified by '0008'`, another agent's database is newer than this checkout) before `seed`, so
nothing was written there; I re-ran with the variables set. Reported for transparency.

## Per-item verdicts
| item | verdict | evidence |
|---|---|---|
| Migrations base -> head -> `downgrade 0005` -> head | **PASS** | run 1 |
| Full Playwright suite | **PASS** | run 7: 38 passed / 0 failed / 0 flaky / 1 skipped |
| Full API suite, one invocation | **FAIL (defects D4-1, D4-2 from my tests)** | run 8; everything else green |
| Scientific notation, unit | **PASS for the valid forms, FAIL for two hostile families** | exact: `1e3`=1000, `8.5E2`=850, `1E+3`, `-1e3`, `1e-3`=0.001, `+2e1`, `.5e1`, `5.e1`, padded ` 1e3 `. Rejected (None): `1e31`, `1e-31`, `1e999999`, `1e3 AED`, `AED 1e3`, `1e3e`, `1e3.5`, `1,5e3`, `1e1,5`, `NaN`, `Infinity`, `-Infinity`, `sNaN`, `1_0e3`, `0x1e3`, `1e3 EUR`. Not rejected: D4-1, D4-2. |
| Ordinary formats unchanged | **PASS** | `1.234,56`, `12.50 AED`, `(12.50)` (= -12.50), `1,234.56`, `12,5`, `1234,56`, `AED 12.50`, `12.50 EUR`, `5 DKK`, `1,234`, `USD 1,299.99`, `1.234.567,89`, ints, floats, Decimals; currency codes containing `e` (`EUR`, `SEK`, `GBP`, `CHF`, `JPY`) still parse |
| Scientific notation through catalog import | **PASS for valid, FAIL for D4-1/D4-2** | `8.5E2`->850, `1e3`->1000, `1.5e-1`->0.15, ordinary formats exact; `1e3 AED`, `1e999999`, `NaN`, `Infinity`, `1e31`, `1,5e3` become row errors (skipped). Cost-price column: `2.5E1`->25 exact. |
| Editor: remove a row without saving, badges stay on the right rows | **PASS (browser)** | manual-price badge follows its row (SKU-1003 moved to position 1) before and after saving; unit price 9.99 kept |
| Editor: re-link button stays on the stale row after removing a non-stale row; re-link then save | **PASS (browser)** | custom row 1 removed unsaved -> `relink-1` on the SKU-1001 row, `relink-2` gone; re-link + save re-prices to 0.91, API shows 1 line, `catalog`, 0.91, blockers gone, submit enabled |
| Editor: add then remove unsaved rows do not shift buttons | **PASS (browser)** | no button on a new custom row; dropped SKU still refuses re-link with the clear message and changes nothing |
| Editor: custom line re-linked becomes catalog line | **PASS at API level; UI path unreachable** (O4-2) | the server drops `product_id` on a custom line, so a custom line is never `catalog`, never stale, never offered "Use active catalog"; the payload the editor would send after re-link (`product_id`, `is_custom:false`, price omitted) turns it into a `catalog` line at 0.91 |
| Editor: save in flight | **PASS (browser)** | fieldset carries `disabled`; price/quantity inputs, Remove, Add custom line and Save are disabled; focused typing (`777`) and a forced click change nothing; exactly 1 PATCH; after release the values are the typed ones (0.86 / 501), the badge is right, and a following edit (0.87) persists with no conflict. A forced 422 unlocks the editor and keeps the typed value, and a retry succeeds. |
| D2-1..D2-5, R3-6, R3-10 still closed | **CLOSED (browser + API)** | all of `test_qa_reverify_review2.py` and `…review3.py` and the review-2/3 specs (`qa-reverify-review2/3.spec.ts`, `review3-regressions.spec.ts`) are green in runs 7 and 8 |

Limit: I could not run my new editor tests against pre-round-7 code (no product edits allowed), so I cannot show that
they fail on the old index pairing; they are written so the old pairing would put the badge/button on the wrong row.

## Defects
**D4-1 (Medium, availability; widened by round 7) — large scientific prices crash the catalog import with a 500.**
`apps/api/app/services/money.py:132` accepts any magnitude up to 1e30; the price column is `numeric(18,6)` (< 1e12).
Request: `POST /catalogs/import` with CSV row `BIG,Big,EA,AED,1e12` (also `1e15`, `1e20`, `1E+30`, `9.99e29`).
Expected: a row error ("not a number"/too large), HTTP 201 with the row skipped and other rows imported.
Actual: unhandled `sqlalchemy.exc.DataError: NumericValueOutOfRange: numeric field overflow` (HTTP 500 in production).
Before round 7 `1e12` was misparsed as 112 (wrong but harmless); the plain-digit form `1000000000000` has the same crash
(SEC4-2, pre-existing, `test_audit_followups5.py` xfail), so the root fix is a cap in `parse_decimal`/the importer
(e.g. reject `>= 1e12` for prices) and covers both. Tests: `test_import_large_scientific_price_is_a_row_error_not_a_500` (5, failing).
Also 1e-30..1e-7 are accepted and quantize to 0 (observation O4-3).

**D4-2 (Low) — whitespace- or repeat-`e` variants still invent a number.** `money.py:136` (`_NUM_CLEAN` strips all letters)
runs after the guards, which only look at a digit directly followed by `e`. Results: `"1 e3"` -> 13, `"1e 3"` -> 13,
`"1 E 3"` -> 13, `"1e  3"` -> 13, `"1ee3"` -> 13, `"e3"` -> 3, `"12e"` -> 12, `"1e+"` -> 1. Through the catalog import `1 e3` and
`1ee3` are stored with list price 13 (exactly the SEC3-11 outcome for different spellings). Expected: None / row error.
Tests: `test_letter_e_adjacent_junk_must_not_silently_become_a_different_number` (8) and the `B7`/`B8` rows of
`test_import_scientific_and_ordinary_formats_and_hostile_rows`, failing. Pre-existing letter stripping (`12 pcs`, `AED 12.50`)
is by design; a stricter rule (letters only as a leading/trailing currency token) would close this.

No Critical or High defect found. No regression found in D2-1..D2-5, R3-6, R3-10.

## Observations
- O4-1 (Info). A pre-existing security-agent module (`test_audit_followups5.py`) was written during my first full run; I
  re-ran the whole suite after it was stable (run 8) so the final count is clean of that artefact.
- O4-2 (Info). The `is_custom: false` reset added to the re-link is defensive: the server stores custom lines without
  `product_id` and `stale_catalog_lines` needs `price_source == catalog`, so no UI line can be both custom and stale.
  There is also no UI route to turn a custom line into a catalog line.
- O4-3 (Info). `1e-30` parses to `1E-30` (bound is -30); a price that quantizes to 0.00 is not the same as "missing" and
  would need the zero-price rules; not tested end to end.
- O4-4 (Info). Playwright `toBeDisabled` does not report a disabled `<fieldset>` as disabled, but its controls are
  (asserted individually), so browser users and screen readers see them as disabled.
- O4-5 (Info). RR4-5 still holds: two compose-render tests skip without a repo-root `.env`.

## Not tested
- `scripts/check_compose.py` / `scripts/revoke_sessions.py` behaviour beyond what the existing suite asserts (green in run 8); no Docker daemon, compose not executed.
- CI on GitHub (RR4-1), migration 0005b on a database that predates round 4, concurrent editor saves racing a catalog activation.
- Browsers other than Chromium, responsive layout, manual assistive-technology audit, `make api-lint` over `app` (only my test file was linted), scanners.
- No statement here is a security certification.

## Files created
- `apps/api/tests/qa/test_qa_reverify_review4.py` (71 tests collected: unit and import tests for `parse_decimal`; 14 currently failing for D4-1/D4-2)
- `tests/e2e/specs/qa-reverify-review4.spec.ts` (6 browser tests, all passing)
- this report

## Cleanup
Playwright stopped its servers; `curl` to ports 8091 and 3091 returns no connection, and no process references them.
