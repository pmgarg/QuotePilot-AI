# Security re-verification 5 - Stage 1, fix rounds 8 and 8b (verified at 6b3c452, 2026-11-12)

Auditor: independent security agent. I did not implement rounds 8 or 8b. Local application only, no live servers
(ports 8092/3092 never opened), no third-party targets, no paid services, no product code changed, nothing committed.
All pytest runs: `APP_ENV=test`, `DATABASE_URL_TEST=...quotepilot_app...@localhost:5432/quotepilot_sec`,
`DATABASE_ADMIN_URL_TEST=...quotepilot_owner...@localhost:5432/quotepilot_sec` (database `quotepilot_sec` for every run).

Commit labels: work started at 3586da2 (round 8). HEAD moved to 6b3c452 (round 8b) mid-task; results are labelled with
the commit they were measured on. Final verdicts are for 6b3c452.

## Findings (open at 6b3c452)
| ID | Severity | Finding | Test |
|---|---|---|---|
| SEC5-6 | Medium | Separator confusion: `0,500` -> 500, `0,001` -> 1, `0,125` -> 125, `00,500` -> 500. A leading zero cannot start a thousands group, so a European price of 0,500 is read 1000x too high. Pre-existing rule ("1,234 = thousands") extended to a leading zero; same inputs fail QA's `test_d5_1_*` (4). Fix: treat `0,ddd` and `00,ddd` as a decimal fraction (or reject). | `test_sec5_6_*` strict xfail |
| SEC5-5 | High (pre-existing, not in the round-8 diff) | The text-line parser regexes `QTY_LEAD` / `QTY_TRAIL` (`extract.py:67-77`) are quadratic on whitespace runs inside a line: 2,000 chars 0.09 s, 10,000 chars 2.3 s, 20,000 chars about 10 s (measured on 6b3c452, code unchanged by 8b). Reachable by any sales member through pasted RFQ text (schema allows 2,000,000 chars; the service limit is 100k) and through PDF page text (200k chars per page). It runs in the API process, with no timeout, holding the GIL. This is the same class CodeQL reported for `_HEAD`. Fix: bound line length (for example 2,000) before matching, or rewrite the patterns without overlapping `\s*` / `.+?` / `\s+`. | `test_sec5_5_*` (2) strict xfail |
| SEC5-2 | Low | NFKC glues superscript and circled digits into the number: `5²` -> 52, `10³` -> 103, `5①` -> 51, `2⁵` -> 25, `x²` -> 2, `¹²` -> 12. `12 m²` and `100m²` are rejected (good). Fix: reject if NFKC changed any digit-bearing character. | `test_sec5_2_*` |
| SEC5-3 | Low | Dashes other than U+2212 family still drop the sign: U+2010..U+2015, U+FE58, U+2796, U+2E3A (`–5` -> +5). Unchanged since bf05198 (SEC4-3 closed only for U+2212/FE63/FF0D). | `test_sec5_3_*` |
| SEC5-7 | Low | `..5`, `...5`, `..50` are accepted as 0.5 (QA D5-2). | `test_sec5_7_*` |
| SEC5-4 | Info | Digits of different scripts in one number are accepted (`1٢3` -> 123, `१२३` -> 123). Same digits, so the value matches what is written. Also `5%` -> 5 and `A- 2` / `AED - 5` -> negative (sign directly before digits after whitespace). | `test_sec5_4_*` (passing, documents behaviour) |

## Status of SEC4-0..6 and R4-1/R4-2
| ID | Status at 6b3c452 | Evidence |
|---|---|---|
| SEC4-0 (spaced/malformed exponent -> 13) | CLOSED | 34 rejected forms (`1 e3`, `1ee3`, `12e`, `0x10`, ...) all None; the end-to-end CSV import test passes |
| SEC4-1 (float NaN/Inf/1e300) | CLOSED | float, int, Decimal, sNaN, bool, bytes, list inputs: None or finite and bounded (`test_sec4_1_*`) |
| SEC4-2 (oversized price/cost/stock, 500) | CLOSED | boundaries 999999999999.999999 stored; 999999999999.9999995 and 1e30 rejected as row errors; oversized stock left unknown with a warning; 8 XLSX numeric-cell cases (1e300, inf, nan, 1E+400 in price, cost or stock) give 201 or 422, never 500 |
| SEC4-3 (U+2212 minus) | CLOSED for U+2212/FE63/FF0D; other dashes remain (SEC5-3) | `test_sec4_0_5_*` |
| SEC4-4 (digit gluing) | CLOSED | `12 per 6`, `10 x 5`, `3 boxes of 12`, `0x10`, `5 pcs 6`, `1/2`, `1 1/2` -> None. I repaired my own test (the expected value called `Decimal("0x10")`, which raises) and removed the strict-xfail marker |
| SEC4-5 (negative zero) | CLOSED | `-0`, `(0)`, `-0e5`, `-0.0` float, `Decimal("-0")` never signed |
| SEC4-6 (RFQ quantity cap) | CLOSED | CSV/XLSX/pasted text/PDF paths; boundaries below; manual POST/PATCH 422; rows stored directly above the cap (50,000,000, 10000000.001, 999999999999999 inserted as the owner role) give 409/422 from generate-quote with no quote created; AI schema cap aligned |
| R4-1 (platform total in tenant audit rows) | UNCHANGED, still closed | `scripts/revoke_sessions.py` untouched; re-run `test_r4_1_recheck_*` pass |
| R4-2 (compose check fails closed) | UNCHANGED, still closed | `check_compose.py` untouched; 14 `test_r4_2_*` pass |

Quantity boundary behaviour (observed): 10000000 stored; `10000000.0004` and `10000000.001` empty with flag
`quantity_out_of_range`; `9999999.9996` rounds to 10000000 (rounding up at the cap, Info); `0.0004` empty with
`quantity_not_positive`; `0.0005` -> 0.001 (half-up); `1e7` stored; `1e8`, `1e30`, 40-digit values, `0`, `-3`, `(3)` empty.
Cost and stock use the 1e12 price bound, so a stock of 1e12..1e15 is dropped to unknown although the column would hold it
(conservative, Info).

## Round 8 parser on 3586da2: was it exploitable? (CodeQL claim: confirmed)
Measured on 3586da2 code (module extracted with `git show`), single call, per cell:
| Input | length | 3586da2 | 6b3c452 |
|---|---|---|---|
| `x` + 1,998 spaces + `(5` | 2,001 | 0.045 s | 0.000 s |
| `x` + (space,tab) x 999 + `+-5` | 2,002 | 0.045 s (up to 0.077 s) | 0.000 s |
| 20,000 spaces + `x` + 20,000 spaces + `+ 5` | 40,004 | 4.65 s | 0.000 s |
| `x` + 20,000 spaces + `(5` | 20,003 | 4.61 s (later runs: 5.0 s, 18.6 s at 40k) | 0.000 s |
- How large a cell reaches `parse_decimal`: the parser worker truncates every cell to 2,000 characters
  (`worker.py:37`), so file-borne cells cost at most about 0.06-0.08 s each. The pasted-text path passes only the regex
  `qty` group (digits), so it is not a vector for `_HEAD`.
- Bound: the sandbox timeout (20 s) covers only the worker subprocess that reads the file. `parse_decimal` runs afterwards
  in the API process (`catalog_import.build_products`, `extract.items_from_rows`, `_guess_columns`) with no timeout; the
  regex engine holds the GIL. A 10 MB upload can hold about 5,000 such cells (3 columns per catalog row; every cell of
  the first 200 rows is parsed by `_guess_columns` for header-less files), so my estimate is several minutes of CPU per
  request from any sales member (RFQ) or admin (catalog). An in-test measurement: 100 hostile quantity cells in an RFQ CSV
  took over 2 s against the old code (xfail confirmed it at 3586da2 before the fix). I did not run a multi-minute proof.
- Verdict: the CodeQL "high" alert on 3586da2 is a real polynomial-time (CPU denial of service) defect, regression
  against bf05198 (that code had no such regex). Severity High for availability, authenticated only.
- Fixed in 6b3c452 as far as `parse_decimal` goes (see below). The same class remains in `extract.py` (SEC5-5).

## 6b3c452 parser attacks
- Linear-time battery: 15 hostile shapes at 1k, 100k and 2,000,000 characters (whitespace runs, tab runs, U+FDFA
  expansion, digit runs, space-grouped runs, `e` chains, dots, full-width digits, combining accents, sign/paren runs) all
  finish in under 0.05 s each (worst observed 0.0003 s; the 129-char cap case 0.0003 s).
- Remaining patterns `_SCI`, `_SPACE_GROUPED`, `_CORE`, the digit scan and `Decimal(core)` only ever see at most 128
  characters (cap checked after NFKC and strip), so they are bounded. NFKC expansion: the pre-check refuses raw strings
  over 512 characters (expansion factor up to 18 gives at most 9,216 characters, then the 128 cap rejects).
- Length cap regressions: a number with more than 128 characters after normalisation, or padded with more than 512 raw
  characters, is now `None` (reported as a row error). 120 leading zeros + `5` still parses; `5` + 400 spaces parses;
  `5` + 600 spaces is rejected (Info: fixed-width exports padded beyond 512 columns would be refused).
- Sign rule: `(−5)`, `(+5)`, `(-5)`, `-(5)`, `5-`, `+-5`, `--5` -> None (fail closed); `(5)` -> -5; `AED - 5` and `A- 2` are
  now accepted (negative). This was my differential: 300,000 random strings against 3586da2 gave 0 value changes, 130 newly
  accepted (all "sign, whitespace, digits" shapes), 2 newly rejected (`(-1 )`, `(-ZWSP5)`).
- Exceptions: 20,000 random Unicode strings (digits, signs, separators, bidi, ZWSP, U+2044, superscripts, U+FDFA)
  produced no exception and no non-finite or negative-zero result.

## Declared test edits to `test_audit_followups5.py` (judged)
Diff `14299e5..3586da2`: LEGITIMATE. (1) Removal of strict-xfail markers for SEC4-0 (2 tests), SEC4-1, SEC4-3, SEC4-5, SEC4-6b and
the empty shared markers `_X2`/`_X6`: each corresponding behaviour passes on the fixed code and the assertions are
unchanged, so nothing was weakened (strict markers guaranteed they would otherwise have XPASS-failed). (2) The added valid
row `Z3` in the end-to-end SEC4-0 test: with both hostile rows rejected the import has no valid rows and answers 422, so
the original `201` precondition could not hold; the assertion on `Z1` is unchanged. My own assertion there is weak
(`in (None, "1000")`) but is covered by the strict-grammar test that requires None. (3) `ruff format`: whitespace and
quoting only. I additionally repaired `test_sec4_4_*` (see above) in the working tree (uncommitted, my file).

## Task 2: other numeric columns fed by `parse_decimal`
`catalog_import.py` caps price, cost and stock (`_too_large`, which tests `abs(v) >= 1e12` before `quantize`, so no
InvalidOperation); `extract.py` quantity goes through `_stored_quantity` (cap before quantize). Nothing else consumes
`parse_decimal`. Quote editor, RFQ item POST/PATCH and quote lines use Pydantic bounds, not `parse_decimal`: NaN,
Infinity, `-0`, `1e30`, 1e8 all give 422. No 500 and no stored value above the caps in any case I tried. Not
established: `_guess_columns` (headerless files) accepting superscript or separator-confusion values (SEC5-2, SEC5-6 apply).

## Task 3: function-scoped DB dependency (`deps.py`)
- (a) Every router uses the single `DB` alias; no other `get_db`/sessionmaker use exists in routers (grep). The middleware
  (`main.py:47`) and exception handlers do no SQL. Serialisation after the session closes cannot lazy-load (detached
  instances raise, they do not query). The file download commits explicitly before returning and then streams from storage
  with no SQL. Fail-closed check: a session that commits and then queries without `set_config` sees 0 rows on 13 tenant and
  auth tables, `app.current_org` is empty, and a tenant-table INSERT is refused (`test_d5_sql_after_commit_...`).
- (b) Authentication dependencies and the endpoint share one session: exactly one session per request (counted by wrapping
  `get_sessionmaker`) for a GET and a POST.
- (c) Audit rows are in the same transaction: with `Session.commit` forced to fail, `POST /customers` returns a generic 500
  (no SQL text, no exception text) and neither the customer nor an audit row exists.
- (d) Rollback paths: an unhandled exception raised after `audit.record` wrote its row leaves no customer and no audit row;
  422/404/403 paths add no audit rows.
- (e) Login: with the commit forced to fail, the response is 500 with no `Set-Cookie`, `GET /auth/me` is 401 afterwards, and
  the `sessions` table is empty. After a successful login, a fresh client replaying the cookie is authenticated at once (the
  row is committed before the response), and two logins give different tokens. Logout: failed commit leaves the session valid
  (replay returns 200); a successful logout is visible as 401 to a replaying client immediately.
Not tested: response-time behaviour under concurrent load, a database failure between commit and socket write, streaming of
very large files, `Depends(scope="function")` behaviour on FastAPI versions other than 0.162.0.

## CodeQL / scan results
The GitHub CodeQL code-scanning check is separate from the `security` workflow conclusion. I cannot read it from this
container. Its result was not recorded for earlier commits (the gate log only has `ci` and `security` workflow conclusions),
so there is no baseline for "1 new alert"; the alert text itself is unread by me and my confirmation above rests on my
own timings, not on CodeQL's report.

| Tool (`scripts/security_scan.sh`, file `docs/evidence/scan-20261112-042305.md`, commit 6b3c452) | Exit | Result |
|---|---|---|
| pip-audit | 0 | No known vulnerabilities |
| bandit -r app -ll | 0 | clean |
| ruff (S* rules) | 0 | clean |
| npm audit --omit=dev | 0 | 0 vulnerabilities |
| npm audit (full) | 1 | 8 moderate (dev toolchain, unchanged) |
| gitleaks | n/a | TOOL NOT AVAILABLE locally (CI-only) |
| semgrep | n/a | TOOL NOT AVAILABLE locally (CI-only) |
| fallback secret grep | 0 | no patterns |
| web bundle grep | 0 | no patterns (bundle not rebuilt; web unchanged in round 8/8b) |
A first scan at 3586da2 was run and discarded because HEAD moved; results were identical.

## Runs (database `quotepilot_sec`)
| Command (cwd `apps/api`) | Commit | Exit | Result |
|---|---|---|---|
| `pytest -q -p no:cacheprovider tests/security/test_audit_followups6.py` | 6b3c452 | 0 | 108 passed, 6 xfailed (SEC5-2, 3, 5 x2, 6, 7) |
| `pytest -q -p no:cacheprovider tests/security/test_audit_followups5.py` | 6b3c452 | 0 | 86 passed, 1 skipped (compose render needs repo-root `.env`), 0 xfailed |
| `pytest -q -p no:cacheprovider -rfx` (full suite) | 6b3c452 | 1 | 955 passed, 7 failed, 2 skipped, 4 xfailed in 951 s. All 7 failures are in QA's untracked `tests/qa/test_qa_reverify_review5.py` (`0,001/0,500/0,125/0,999`, `..5`, `...5`, `..50`), i.e. SEC5-6 and SEC5-7. This run preceded my last two tests (SEC5-6/7), which I then ran with the file alone (the counts above include them). The 4 xfails were my SEC5-2/3/5 tests. |
| `ruff check` and `ruff format --check` on my two files | 6b3c452 | 0 | clean |
| earlier run on 3586da2 (file alone, before 8b) | 3586da2 | 0 | 87 passed, 4 xfailed (SEC5-1 x2, SEC5-2, SEC5-3) |
Timing and differential scripts were ad hoc Python against the importable modules (3586da2 module extracted with `git show`);
their numbers are reproduced in the tables above.

## Residual risks
| Risk | Level |
|---|---|
| Text-line regex CPU denial of service (SEC5-5) | High |
| `0,500` read as 500 (SEC5-6) | Medium |
| NFKC digit gluing, other dash sign loss, doubled dot (SEC5-2/3/7) | Low |
| Parsing still guesses locale (`1,234` = 1234, `1.234` = 1.234) | Low (documented) |
| Sub-quantum price rounds to 0; zero price accepted | Low |
| Parser CPU runs in the API process without a timeout (design) | Medium until SEC5-5 is fixed |
| gitleaks/semgrep/CodeQL alert text not read locally | Info |

## Not tested
Live servers and browser; real Compose start-up; CI results and the CodeQL alert text; full cross-tenant, CSRF, cookie and
file-limit matrices beyond the existing suite (unchanged areas); multi-minute CPU DoS reproduction against a live API;
concurrent-load behaviour of the commit-before-response change; the hosted-LLM adapter (disabled).
