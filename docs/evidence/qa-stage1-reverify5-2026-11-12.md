# QA (independent) re-verification 5 — Stage 1, fix round 8 / 8b

Date 2026-11-12 (UTC 03:55-04:50). Scope: `git diff bf05198..3586da2`, extended by the coordinator to the later HEAD
`6b3c452` (round 8b: parser rewritten without regular expressions) and `af8c902` (CI workflow + evidence only; no product
file differs from `6b3c452`, checked with `git diff 6b3c452..af8c902 --stat`). I did not implement rounds 8/8b. No product
code was changed and nothing was committed. Tests derive from CLAUDE.md ("never invent prices, stock or customer facts"),
the PRD, `docs/API_CONTRACT.md` and the review/security evidence, not from the implementation.

## Environment
| item | value |
|---|---|
| DB | local PostgreSQL 16 (service), database `quotepilot_qa`, schema reset and migrated by me; never the default database |
| `APP_ENV` | `test` |
| `DATABASE_URL`, `DATABASE_URL_TEST` | `postgresql+psycopg://quotepilot_app:***@localhost:5432/quotepilot_qa` (non-owner, RLS enforced) |
| `DATABASE_ADMIN_URL`, `DATABASE_ADMIN_URL_TEST` | `postgresql+psycopg://quotepilot_owner:***@localhost:5432/quotepilot_qa` |
| tools | `apps/api/.venv` (Python 3.13, FastAPI 0.162.0), Playwright Chromium (`--trace off`), ports 8091 / 3091 |
| tree | the worktree also held the security agent's untracked/modified verifier files (`tests/security/test_audit_followups5.py` modified, `test_audit_followups6.py` new); they ran as part of the full suite and are not mine |
| load | shared machine (stage 2 / stage 4 / security suites live) |

## Runs
| # | Command (cwd) | Commit | Exit | Result |
|---|---|---|---|---|
| 1 | `pytest -q -p no:cacheprovider` full suite, first attempt (apps/api) | tree moved during the run | stopped by me | **discarded**: HEAD moved from 3586da2 to 6b3c452 while it ran, so it mixed two versions of `money.py` |
| 2 | `.venv/bin/pytest -q -p no:cacheprovider tests/qa/test_qa_reverify_review5.py` (apps/api) | 6b3c452 | 1 | `7 failed, 263 passed in 76.19s`; the 7 failures are the two intended defect families D5-1 (4) and D5-2 (3); an earlier iteration of the file at 3586da2 had the same 7 plus test-side errors that I corrected |
| 3 | **`.venv/bin/pytest -q -p no:cacheprovider` full API suite, once, after my additions (apps/api, 04:23-04:39)** | **6b3c452** (HEAD moved to `af8c902` at 04:37; product code identical) | 1 | **`8 failed, 955 passed, 2 skipped, 4 xfailed in 922.09s (0:15:22)`**. Failures: my 7 defect tests (D5-1 x4, D5-2 x3) and 1 in the security agent's `test_audit_followups6.py::test_sec4_0_5_obvious_numbers_keep_their_meaning_and_sign[(−5)--5]` (see D5-3). Skips: the two compose-render tests needing a repo-root `.env` (RR4-5). |
| 4 | `npx playwright test --reporter=list --trace off` (tests/e2e; global setup migrated, seeded and rebuilt the web app) | af8c902 (= 6b3c452 product code) | 0 | **40 passed, 0 failed, 0 flaky, 1 skipped** (`screenshots.spec.ts` self-skip); includes my 2 new tests |
| 5 | `npx playwright test specs/qa-reverify-review5.spec.ts --trace off` (development) | 3586da2 code | 0 | 2 passed (RFQ UI is unaffected by 8b) |
| 6 | live read-your-writes loop `ryw.py` (real `uvicorn app.main:app` on 127.0.0.1:8091, httpx), run twice: on 3586da2 and again on af8c902 | both | 0 | **0 misses in each**: 400 x (POST /customers, immediate GET) = 0 misses; 400 x (POST /quotes, GET) = 0; 200 x (login, fresh client, GET /auth/me) = 0; 8 threads x 60 customers = 0 misses and 0 errors (480); all flow statuses 200/201 (patch, submit, approve, PDF GET/POST, CSV, RFQ upload), `/auth/me` 401 after logout. Total 1,480 immediate reads, 0 misses. |
| 7 | `ruff check` and `ruff format --check` on my two Python test files; `ruff check app tests -q` (apps/api); `tsc --noEmit -p .` (tests/e2e) | 6b3c452 | 0 | clean (the `ctx` F841 reported by the coordinator is fixed; the UP031 and format findings are fixed) |
| 8 | differential runs `diff.py` / `diff2.py` (load `bf05198`, `3586da2` and HEAD `money.py` as modules; 300+ cells) | - | 0 | see "Behaviour changes" |

Evidence levels: runs 2-3 and 6 are unit/integration (ASGI TestClient + PostgreSQL + RLS, and a real server over HTTP);
run 4 and 5 are browser evidence. Unit tests are not used as evidence for browser claims.
Ports 8091 and 3091 are closed (curl returns no connection); no process of mine is running.

## Per-claim verdicts
| claim | verdict | evidence |
|---|---|---|
| (a) my 14 formerly failing tests pass unedited | **VERIFIED** | `git diff 14299e5..HEAD -- apps/api/tests/qa` is empty; all of `test_qa_reverify_review4.py` is green in run 3 (it has 0 failures) |
| Parser: one-number grammar, no fragments glued | **VERIFIED** with D5-1/D5-2 | 90 realistic cells keep their obvious meaning; ~90 ambiguous/multi-number cells are None (list below); 60,000-string seeded fuzz: parser never raises, result always finite and < 1e31, and the digit sequence of a result always equals the digit sequence of the input (no invented or lost digits) |
| Parser: round 8b is linear and bounded | **VERIFIED** | 40,000 spaces before/after the number, `"a" * 1e6`, `"1" + " 1" * 20000`, 50,000 parentheses, 40,000 NBSP: nine hostile cells in total < 1 s (about 0.5 ms for the megabyte cell); 128-character cap and sign rule behave as stated |
| Catalog import CSV and XLSX | **VERIFIED** | CSV: valid cells (`1.234,56`, `12.50 AED`, `1 234,56`, `EUR 12,50`, `Rs. 500`, `1'234.50`, Arabic decimals, `8.5E2`, `AED 1,299.99`) exact; `(12.50)`, `Rs.500`, `12 per 6`, `1.234.567`, `1 e3`, `10 to 20`, `N/A`, `1e12`, `999999999999.9999999`, `1e30`, `-5`, `TBD`, empty and a 20-digit price are row errors, report says 9 imported / 14 skipped with "too large" messages. XLSX: float/int/text cells exact (0.1+0.2 stored as 0.300000); `1e300`, 10^25, `1e12`, `nan`, `Rs.500`, `12 per 6`, -3, `True` skipped; no 500 |
| RFQ upload CSV / XLSX / pasted text / PDF | **VERIFIED** | CSV: `12 per 6`, `1 e3`, `Rs.500`, `10 to 20`, `N/A` -> empty + `quantity_unreadable`; `1,000`, `12 pcs`, `1.234,5` read exactly; `(5)`, `-5`, `0` -> `quantity_not_positive`. XLSX: 12.5, 1000, `1 234` exact; `1e30`, 10^20 -> `quantity_out_of_range`; inf -> empty. Text: `1,000 pcs`, `12.5 kg`, `2 x` read; PDF (built with reportlab) reads `1,000 pcs` and `7 pcs` and never a quantity from `99999999999999999999 pcs` |
| SEC4-2 catalog price too large | **VERIFIED** | `1e12`, `1e15`, `1e20`, `1E+30`, `9.99e29` and plain-digit forms are row errors (D4-1 tests green); a catalog with one valid row still imports |
| SEC4-6 quantity cap boundaries | **VERIFIED** | CSV and pasted text: `10000000` and `9999999.999` stored; `10000000.001`, `10000000.0004`, `10000001`, `1e8` -> empty + `quantity_out_of_range`; `1e7` -> 10000000.000 (CSV); `0.0005` -> 0.001, `1.2345` -> 1.235, `1.2344` -> 1.234 (HALF_UP, 3 decimals); `0.0004`, `0`, `-1`, `0.00`, `(3)` -> `quantity_not_positive` |
| SEC4-6 generate-quote gate and UI | **VERIFIED** | API: generate on an RFQ with an out-of-range line is 409/422 mentioning quantity; after `PATCH quantity 250` the flag is gone and the quote has exactly [5, 250]; manual 10000001 is 422, 10000000 is 200; a legacy row of 50,000,000 written by the owner role is still refused by generate-quote. Browser: the badge "quantity out of range" and "Quantity required" are shown, generate keeps the user on the RFQ page and no quote exists, an over-cap manual value leaves the flag, entering 250 clears both, the quote carries [5, 250]; 10,000,000 is accepted unflagged; 0 and -4 show "quantity not positive" with an empty field |
| D5 (a) commit before response, 7 endpoints | **VERIFIED** | second owner-role connection queried at `http.response.start`: login (session row count +1 and `auth.login` audit), customer create (row + audit), submit (`pending_approval` row + audit), approve (row + audit), PDF POST and GET (`quote.export_pdf` +1 each), file download as StreamingResponse (`file.download` +1; the audit row is committed by the endpoint itself), logout (`auth.logout` +1; `/auth/me` is 401 afterwards) |
| D5 (b) rollback on failure | **VERIFIED** | (1) a deferred constraint trigger raising only at COMMIT: `POST /customers` returns 500 (not 2xx), no customer row and no audit row remain, the next request on a new session succeeds; (2) an immediate trigger raising inside the endpoint (audit insert): 500, nothing persisted; 422 and 404 inside endpoints persist nothing; (3) a failed login still stores its `auth.login_failed` row (witnessed by a trigger-filled side table, since NULL-org rows are invisible under RLS) and returns 401 |
| D5 (c) nothing touches the session after it closed | **VERIFIED** | full API suite (955 passed) plus a pass through every response kind with `raise_server_exceptions=True`: QuoteOut list/get/history, submit/approve/new version, PDF/CSV bodies, file download streaming, 404 handlers, audit, RFQs, customers, active-version; live server run 6 over HTTP exercises the same |
| D5 (d) read-your-writes | **VERIFIED** | run 6: 0 misses of 1,480 |
| Earlier items D2-1..D2-5, R3-6, R3-10, D4 editor tests | **STILL CLOSED** | review-2/3/4 QA modules and specs green in runs 3 and 4 |
| Verifier-test edits declared by the implementer | **JUDGEMENT** | `git diff 14299e5..HEAD -- apps/api/tests/qa` is empty (my files untouched). The removal of strict xfails for fixed findings is legitimate, since the tests pass; I did not review the security file line by line |

## Behaviour changes of `parse_decimal` (differential: bf05198 vs 3586da2 vs 6b3c452)
Judgement key: OK = right result or safe rejection; IMPROVED = was wrong before; LOSS = realistic cell now unreadable (safe, but a user must type it); REGRESSION = new wrong number.
| cell | bf05198 | round 8 | round 8b | judgement |
|---|---|---|---|---|
| `Rs. 500`, `Ksh. 500`, `Dhs. 500`, `Fr. 5`, `No. 12`, `approx. 5`, `c. 5` | 0.500 / 0.5 / 0.12 | 500 / 5 / 12 | same | IMPROVED (a currency/abbreviation dot no longer makes a fraction) |
| `Rs.500`, `Rs.5`, `Ksh.500`, `Dhs.500`, `AED.500`, `Rs.1,234`, `Rs.500.50` | 0.500 (wrong) | None | None | OK safe rejection (a distributor may type `Rs.500`; the cell is reported, not guessed). LOSS of convenience |
| `1.234.567` | None | None | None | unchanged. NOT a new rejection (the fix-note wording "now reported" is inaccurate); a European thousands value is not readable and is reported |
| `1.234` / `1,234` / `1.500` | 1.234 / 1234 / 1.5 | same | same | unchanged ambiguity (documented) |
| `−12.50`, `− 12`, `−5`, `（5）` (Unicode minus, full-width parentheses) | +12.50, +12, +5, +5 | negative | same | IMPROVED (a credit was read as a charge) |
| `٣٫٥`, `1٬234٫5` (Arabic separators) | 35, 12345 | 3.5, 1234.5 | same | IMPROVED |
| `(12.50) AED`, `(5) AED`, `AED (5)` | +12.50, +5, +5 (wrong sign) | None | None | IMPROVED/OK safe |
| `(-5)` | +5 | -5 | None | OK safe (8b: parentheses plus sign refused); contradicts a security test, D5-3 |
| `(+5)` | -5 | -5 | None | OK safe |
| `AED - 5` | -5 | None | -5 | OK, but see O5-4 (separator dash read as a minus; a price becomes a row error, a quantity becomes `quantity_not_positive`) |
| `AED +-5` | -5 | None | None | OK |
| `12 per 6`, `12/6`, `12 x 6`, `12x6`, `1 x 5`, `2 x 3.5`, `10 to 20`, `5 or 10`, `5 pcs @ 3`, `10+5`, `10 + 1 free`, `1/2`, `1 1/2`, `12 12`, `12 3456`, `250 0`, `1 234 5` | 126, 126, 126, 126, 15, 23.5, 1020, 510, 53, 105, 101, 12, 112, 1212, 123456, 2500, 12345 | None | None | IMPROVED (two numbers were glued into one; this is the main fix) |
| `1 e3`, `1e 3`, `1ee3`, `12e`, `e3`, `3E`, `1e+` | 13, 13, 13, 12, 3, 3, 1 | None | None | IMPROVED (D4-2) |
| `12 m2`, `12 m²`, `12 (approx)`, `12,`, `12'`, `5+`, `1½`, `=1+1`, `'5`, `0x10`, `1_000` | 122, 12, 12, 12, 12, 5, 1, 11, 5, 10, 1000 | None | None | mixed: `12 m2` was 122 (wrong, IMPROVED); `12 m²`, `12 (approx)`, `12,`, `12'`, `5+`, `1_000`, `'5` were right by luck and are now reported = LOSS of convenience, safe. `12 m²`: NFKC turns the superscript into a digit, so it is a "unit with a digit" |
| `..5` | None | 0.5 | 0.5 | **new acceptance, D5-2 (Low)**; `..50` -> 0.50, `...5` -> 0.5 |
| `1.5 m.`, `1.5 mt.` | None | 1.5 | 1.5 | OK, now accepted (unit abbreviation with a trailing dot) |
| `-0` | -0 | 0 | 0 | OK (no negative zero) |
| `0,001` / `0,500` / `0,125` / `0,999` | 1 / 500 / 125 / 999 | same | same | **pre-existing, D5-1 (Low)**: a 1000x error for a comma decimal with a leading zero |
| `5K`, `2M`, `1.5k`, `SAR 1.5k` | 5, 2, 1.5, 1.5 | same | same | unchanged (O5-2): suffix letters are a unit, so `2k pcs` is 2, not 2000 |
| `1,00,000` (Indian grouping), `500/-`, `5,-`, `12.50-` | None | None | None | unchanged safe rejections |
| `Packed in cartons delivery within 2 weeks` | 2 | 2 | 2 | unchanged (O5-3): text around ONE number is read as that number; with a comma the text is rejected |
| cells longer than 128 characters (`5 xxx...` 140 chars, a 150-character description ending in 12) | 5 / 12 | 5 / 12 | None | OK: no price or quantity needs more; see column detection below |
| 129-character padded cells (`5` + 120 spaces is accepted after `strip`, `"1 " * 70` rejected) | - | - | OK | verified |
| `-AED 5`, `AED -5`, `- AED 5`, `$-5.50`, `-$5.50`, `−AED 5`, `AED −5` | mostly -5 (Unicode minus: +5) | -5 | -5 | OK (sign rule works; Unicode minus now negative) |
| `-AED -5`, `--5`, `5 AED-`, `AED5-`, `12.50- AED` | None / -5 | None | None | OK |
Of the ~300 cells I compared, 3586da2 -> 6b3c452 differs only in `(-5)`, `(+5)`, `AED - 5` and cells longer than 128 characters.

Column detection (`_guess_columns`, headerless files): the heuristic counts a cell as numeric only if `parse_decimal` succeeds AND the cell contains no letters, so a 150-character description ending in a number was never counted and the cap does not change it. Test `test_rfq_headerless_spreadsheet_still_finds_quantity_column_with_long_description_cells` (XLSX and CSV; mixed int, `1,500`, `2 pcs` and 12.5 quantities next to a >128 character description ending in `7`, a SKU column and a unit column) finds the quantity column and the right values; with a header the same rows map by name; a quantity cell with a >128 character delivery note is empty and flagged `quantity_unreadable`. Passed on 6b3c452.

## Defects
**D5-1 (Low, pre-existing, not a regression).** `apps/api/app/services/money.py` (the `len(parts[1]) == 3 and len(parts[0]) <= 3` thousands branch of the comma handling). Input: catalog/RFQ cell `0,500` (also `0,001`, `0,125`, `0,999`). Expected: 0.5 (or None; a thousands group never follows a leading zero). Actual: 500 (1, 125, 999): a 1000x unit-price or quantity error without any flag. Tests: `test_d5_1_leading_zero_comma_with_three_decimals_is_a_fraction_not_thousands` (4 failing).

**D5-2 (Low/Info, new in round 8).** `money.py` (the `head.endswith(".")` handling): `..5`, `...5`, `..50` are read as 0.5, 0.5, 0.50; they were None at bf05198. A doubled dot is a typo and should be reported. Tests: `test_d5_2_doubled_leading_dot_is_a_typo_and_must_not_be_accepted` (3 failing).

**D5-3 (Low, verifier-test conflict, not a product defect).** `tests/security/test_audit_followups6.py::test_sec4_0_5_obvious_numbers_keep_their_meaning_and_sign[(−5)--5]` (security agent's untracked file) expects `(−5)` = -5, while round 8b deliberately refuses parentheses plus a sign (`(-5)` = None) and the same file asserts `("(−5)", None)` at line 718. The two tests contradict each other; one of them needs to change. Failing in run 3 (not mine; the implementer/security agent decides).

No Critical, High or Medium defect found. D4-1/D4-2 are fixed.

## Observations
- O5-1 (Info). Round 8b was measured on `3586da2` for the live loop (run 6, first pass) and re-measured on `af8c902`: identical result. All other runs are labelled above.
- O5-2 (Info, pre-existing, unchanged). `k`/`M` suffixes are units, not multipliers: `2k pcs` -> 2, `1.5k` -> 1.5. In an RFQ that is a 1000x under-reading with no flag. A distributor plausibly types `2k`.
- O5-3 (Info, design). A cell of up to 128 characters with letters around exactly one number is read as that number (`delivery within 2 weeks` -> 2). Commas, signs and parentheses in the text make it a rejection.
- O5-4 (Info). `AED - 5` / `Qty - 12` read the dash as a minus (the same at bf05198). For a price this is a row error and for a quantity it is `quantity_not_positive`: both safe, both surprising for a separator dash.
- O5-5 (Info). `10000000.0004` is flagged `quantity_out_of_range` although it would round to the cap; the check happens before rounding (conservative).
- O5-6 (Info). The security agent's `test_audit_followups6.py` xfails open findings SEC5-2, SEC5-3 and SEC5-5 (a High, pre-existing: the RFQ text-line regexes `QTY_LEAD`/`QTY_TRAIL` are quadratic on whitespace runs). I did not verify those findings; they are the security agent's. My own hostile-cell timing test covers `parse_decimal` only (and that is linear).
- O5-7 (Info). The Playwright `toBeDisabled` limitation noted in re-verification 4 is unchanged.

## Not tested
- GitHub CI / CodeQL (the alert the implementer mentions); `scripts/*`, Docker/compose (no daemon).
- Concurrent writers racing the commit (only sequential and 8-thread read-your-writes), a commit failure on endpoints other than customers, and a failing commit on the streaming endpoint.
- XLSX/PDF parser sandbox limits (unchanged scope), scanned PDFs, other locales' RFQ layouts, browsers other than Chromium, `make api-lint` `mypy` (only `ruff` on `app` and `tests`).
- The AI extraction schema cap (`ai/schemas.py` 10,000,000): schema-level, no AI adapter is configured here.
- No statement here is a security certification.

## Files created
- `apps/api/tests/qa/test_qa_reverify_review5.py` (270 tests; 7 failing for D5-1 and D5-2)
- `tests/e2e/specs/qa-reverify-review5.spec.ts` (2 browser tests, passing)
- this report
