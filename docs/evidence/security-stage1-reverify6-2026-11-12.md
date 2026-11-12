# Security re-verification 6 - Stage 1, fix round 9 (verified at 7151e64, 2026-11-12)

Auditor: independent security agent. I did not implement round 9. Local application only, no live servers (ports
8092/3092 never opened), no third-party targets, no paid services, no product code changed, nothing committed.
All pytest runs: `APP_ENV=test`, `DATABASE_URL_TEST=...quotepilot_app...@localhost:5432/quotepilot_sec`,
`DATABASE_ADMIN_URL_TEST=...quotepilot_owner...@localhost:5432/quotepilot_sec` (database `quotepilot_sec` for every run).
Scope `git diff 8f9416e..7151e64` (product files: `seed.py`, `matching.py`, `money.py`, `extract.py`; test harness:
`tests/e2e/global-setup.ts`, `playwright.config.ts`).

## Correction to re-verification 5 (CodeQL)
Re-verification 5 treated the CodeQL "1 new alert, 1 high" on 3586da2 as the round-8 parser pattern. That was wrong. The alert
was identified from the CI job log of af8c902 (the security workflow prints SARIF results): python SARIF 1 result
`py/clear-text-logging-sensitive-data` at `apps/api/app/fixtures/seed.py:171` (the demo seed printed the demo password);
javascript SARIF 0 results. My timing confirmation of the round-8 `_HEAD` slowdown (4.6 s for a 20,003-character cell at
3586da2, 0.000 s at 6b3c452) stands on its own as a real CPU denial of service, but it was not the CodeQL alert. On CI for
7151e64 the CodeQL code-scanning check is `success` and both SARIF files (python, javascript-typescript; push and
pull_request) report 0 results (coordinator-reported; I did not read CI). That check is separate from the `security`
workflow conclusion. A CI failure of the `ci` api job on 7151e64 (five QA tests reading `DATABASE_ADMIN_URL_TEST` directly)
is QA's file; my files only use `DATABASE_ADMIN_URL`/`DATABASE_URL` after `tests/conftest.py` has set them, as in
rounds 5 and 6.

## Findings (open at 7151e64)
| ID | Severity | Finding | Test (strict xfail) |
|---|---|---|---|
| SEC6-5 | Medium | The leading-zero rule (SEC5-6) misses the two-separator branch: `0,500.00` -> 500.00, `0.500,00` -> 500, `00,500.25` -> 500.25, `0,001.5` -> 1.5, `0.500,5` -> 500.5 (QA D6-1 fails the same five inputs). A price or quantity is read 1000x too high. Fix: apply the "group never starts with zero" test before the both-separators branch (reject or read as decimal). | `test_sec6_5_*` |
| SEC6-1 | Medium (admin only) | `units.normalize_sku` (`_SKU_STRIP = ^[\s\W_]+\|[\s\W_]+$`) is quadratic on a run of non-word characters: 2,000-char cell `a`+`-`x1998+`b` 20 ms, 1,000 chars 4.7 ms. `catalog_import.build_products` calls it on the raw cell (up to 2,000 chars after worker truncation) before the 100-character check. Measured: a 960 KB catalog CSV with 500 such rows took 10 s (422, every row rejected); a 10 MB file (about 5,000 cells) extrapolates to about 100 s of API-process CPU (extrapolation, not run). Fix: truncate/length-check before normalising, or replace the pattern with `str.strip` over a character set. | `test_sec6_1_*` (2) |
| SEC6-2 | Medium (pre-existing, design) | Fuzzy matching (`CatalogIndex.match` -> `rapidfuzz.process.extract`) is unbounded in query length and catalog size and holds the GIL (a second thread made 3 ticks during a 0.4 s call). Per item on a synthetic index: 5,000 products 3.9 ms (short query) / 15 ms (990-char query); 50,000 products 40 ms / 146 ms. An RFQ may carry up to 2,000 items, so 2,000 unmatched items against a 50,000-product catalog is about 81-293 s of single-process stall (computed from the per-item figures, not run end to end). Fix: cap the query length (for example 200 characters), budget items x catalog per request, and run matching in a worker process or with `workers`/timeouts. | `test_sec6_2_*` |
| SEC6-3 | Low | Negative stock is stored: stock cell `-5` and `– 5` import as -5 (price and cost negatives are rejected). `–`/`—` before a number is now negative (round 9), so more cells reach this. Fix: reject or leave unknown when stock < 0. | `test_sec6_3_*` |
| SEC6-4 | Low | The SEC5-2 guard tests `unicodedata.numeric`, but characters with no numeric value that NFKC-expand to digits still glue: `5㋀` -> 51, `5㏰`-style hour/day telegraph symbols -> 50..531, `㎡5` -> 25 (SQUARE M SQUARED expands to `m2`). `12 ㎡` and `12㎡` are rejected. Fix: reject when NFKC changes a non-decimal character into text that contains a digit. | `test_sec6_4_*` |
| SEC6-6 | Info | `raw_text` of a pasted line is now the whitespace-collapsed form (`5\tpcs\t\tSKU-1001   bolt` -> `5 pcs SKU-1001 bolt`), not the customer's exact text. Differential 60,000 lines: 16,505 text-only differences of this kind. | `test_info_raw_text_*` (passing) |
| QA D6-2/D6-3 | Low (docs, QA-owned) | repo-root `.env` is not read when the seed runs with cwd `apps/api`; docs still say "environment or `.env`" in places. I confirmed that `.env` in the current directory is read and a repo-root one is not. | QA tests |

## Status of earlier findings
| ID | Status at 7151e64 | Evidence |
|---|---|---|
| SEC5-2 (numeric non-decimal characters) | CLOSED for characters with a numeric value (13 forms, plus a sweep of all 1,112,064 code points on each side of `5`); residual SEC6-4 for NFKC-expanding symbols without a numeric value | `test_sec5_2_*`, sweep test |
| SEC5-3 / SEC4-3 (dashes) | CLOSED: every code point of category Pd (26) plus U+2212 and U+2796 before a number is a minus, also with a no-break space; `12<dash>14` is rejected; a lone dash is None | `test_sec5_3_and_sec4_3_*` |
| SEC5-6 (leading-zero thousands) | PARTLY CLOSED: single-separator forms fixed (`0,500` = 0.500, `0,001`, `00,500`, `012,345` = 12.345, `0,500,000` and `0,5,5` rejected, `10,500` still 10500); two-separator forms open (SEC6-5) | `test_sec5_6_and_sec5_7_*` |
| SEC5-7 (doubled dot) | CLOSED: `..5`, `...5`, `..50`, `,.5`, `.,5`, `$..5`, `1..5`, `5..`, `0,,5` -> None; `.5` and `$.50` still parse | same |
| SEC5-5 (quadratic text-line regexes) | CLOSED for the line parser; the same class remains in `normalize_sku` (SEC6-1) and, as a cost, in matching (SEC6-2) | timing table below |
| SEC4-0..6, R4-1, R4-2 | unchanged, re-run inside the full suite, no regression | full suite |

## Parser attacks (task 2)
- 400,000 random strings, 8f9416e vs 7151e64: 0 value changes except one (`aa06,637` 6637 -> 6.637, the intended rule), 31,933
  newly rejected (non-decimal numeric characters, doubled dots, leading-zero forms), 3,183 sign flips (a dash directly before
  digits, for example `c–7`, `➖101`, `A–62`, now negative instead of positive), 46 newly accepted (exponent with a Unicode dash,
  for example `5E➖2` = 0.05, consistent with ASCII `5E-2`).
- Callers of `parse_decimal`: catalog list price and cost reject negatives (dash-prefixed values give row errors, tested);
  RFQ quantities flag `quantity_not_positive` and store nothing (tested, never flipped positive); stock accepts negatives
  (SEC6-3); `_guess_columns` only tests `is not None` (a negative still counts as numeric, no security effect).
- The two new per-character scans run after the 4x128 = 512-character pre-check, so they see at most 512 characters:
  20 calls each on 511 U+FDFA, 500 dashes, 512 circled digits, 512 digits, 100,000 superscripts and 2,000,000 hyphens averaged
  under 10 ms per call (test asserts it; measured far less).
- Legitimate characters with numeric values in a number cell (`½`, `5²`, Roman numerals, CJK numerals) are now refused (cell
  becomes a row error), which is the intended fail-closed behaviour.

## Worst-case timing table (task 1; measured on 7151e64 unless stated; shared machine)
| Path / shape | Where it runs | Measured |
|---|---|---|
| Pasted text, 100 lines x 1,000 chars of 30 adversarial separator/digit/keyword/unit shapes | API process | worst shape 0.19 s per 100 KB |
| Random search, 4,000 lines of 1,000 chars | API process | worst 7.6 ms per line; 100 lines of the worst shape 0.37 s |
| 33,000 tiny lines (132 KB) | API process | 0.07 s; 7,000 ordinary lines 0.09 s |
| Whitespace-run lines (200,000 blanks) | API process | under 0.01 s |
| `POST /rfqs` pasted, 99 x 1,000-char worst shape (full request incl. DB) | API process | 0.87 s; SKU-run lines 0.61 s; long descriptions 0.43 s |
| PDF 3 pages x 100 lines (90,000 chars) | worker in the 20 s sandbox: 2.2 s; `items_from_pdf` in the API process: 0.33 s | extrapolation to the sandbox ceiling about 6 s API CPU (not run) |
| `sku_tokens`: 3,000 runs of 63 chars; one 300,000-char run; `SKU_TOKEN.fullmatch` on 2,100-char cells x200 | API process | each under 1 s (asserted) |
| `items_from_rows`: 1,500 rows with 2,000-char hostile SKU/description/qty cells; headerless 150 rows x 64 hostile cells | API process | each under 2 s (asserted) |
| `POST /rfqs` CSV 5.9 MB, 2,000 rows, 990-char SKU/description cells (tenant catalog of a few products) | worker (sandbox) + API process | 9.2 s and 11.2 s |
| `POST /rfqs` XLSX 2,000 rows, 54 KB | same | baseline short cells 10.1 s, 990-char descriptions 12.3 s, 990-char SKU runs 19.4 s (baseline is DB insert and candidate work; the machine was shared with QA) |
| Matching alone, 2,000 items, 200-product catalog | API process | 0.29 s short; 1.7 s with 990-char descriptions |
| Matching, 50,000-product synthetic catalog, per item | API process, holds GIL | 40 ms (short) / 146 ms (990-char) -> SEC6-2 |
| `normalize_sku` on a 2,002-char hostile cell | API process | 20 ms (SEC6-1); catalog import of 500 such rows 10 s |
Sandbox boundary: the 20 s timeout and 10 MB/2,000-character-cell limits apply to the worker subprocess that reads the file;
everything above marked "API process" (`items_from_*`, matching, `normalize_sku`, `parse_decimal`, DB writes) runs afterwards
with no wall-clock limit. Pasted text (limit 100,000 characters) never enters the sandbox.

## Parsing confusion from whitespace collapse
Differential of 60,000 random short lines (tabs, CR, NBSP, U+2003, U+001C, U+0085, formula prefixes) between 8f9416e and
7151e64: 0 flag differences, 16,505 text-only differences (collapsed whitespace in description/raw_text, SEC6-6), 6 quantity
differences, all of the form `item 1:<odd whitespace>5` where the old code failed to match and the new code reads
description `item 1`, quantity 5, as it already did for plain spaces. No case read a different quantity than the plain-space
version. Formula-like detection: `=`, `+`, `@` prefixes behind 8 kinds of leading whitespace are either dropped or flagged
`formula_like`, never imported clean (the `\t=`/`\r=` branches were dead code even before, because `strip()` removed them).
Tab-separated forms (`SKU-1001<TAB>qty:<TAB>5`, `5<TAB>pcs<TAB>SKU-1001`, NBSP variants) give the same item as the space form.

## Demo seed (task 3)
Read and executed (subprocess, database `quotepilot_sec`):
- No variable: exit 2, stdout empty, stderr one instruction line, no traceback. Empty and 11-character values: exit 2, value not echoed.
- Valid value: exit 0; stdout lists the 10 accounts and "the value of DEMO_PASSWORD (not shown)"; neither stdout nor stderr
  (0 bytes) contains the value; the stored hash (`$argon2id$...`) is not printed; no `audit_events` row contains the value;
  idempotent re-run prints nothing secret. A failing database connection (exit 1) does not echo the password.
- `grep`/AST over `apps/api/app` and `scripts`: no `print`/logger call receives a name or attribute containing password,
  secret, token, key, credential, hash or csrf (`test_no_print_or_log_call_*`); the three remaining seed prints are constants.
- Compose: `migrate` keeps `env_file: ../.env` and runs the seed; `api` has no `env_file` and no `DEMO_PASSWORD`; CI sets
  `DEMO_PASSWORD` at job level. With the default `.env.example` (empty value) the seed exits 2, so `migrate` fails and `api`
  does not start until the operator sets it (Info; by design, and it also means Compose always seeds the demo accounts).
- CodeQL's alert condition (a password flowing into `print`): by reading the code, the variable `password` no longer reaches
  any `print` argument; the earlier f-string at old line 171 is replaced by a constant. The CI check result is the
  coordinator's to record (reported `success`, 0 SARIF results).
- Not secrets, but secret-like: the CI file and e2e defaults contain the synthetic test password `E2E-Demo-Password-2026!`
  (placeholder-class, CI-only). `.env.example` holds placeholders only.

## E2E harness (task 4)
`global-setup.ts` now only migrates and seeds (the web build moved into the `webServer` command
`npm run build && npx next start`), `reuseExistingServer` stays false, the timeout grows from 180 s to 420 s. Nothing in
either file touches cookies, CSRF, CORS, auth, TLS or role setup; the diff of the commit range contains no product file
outside the four declared ones (asserted by `test_only_test_and_docs_files_changed_*`). I judge it test-only and not
weakening. The effect of skipping a rebuild is documented (`E2E_SKIP_WEB_BUILD`); a stale build would fail loudly, not pass.

## Declared test edits to `test_audit_followups6.py` (judged)
`git diff 8f9416e..7151e64`: exactly six `@pytest.mark.xfail(strict=True, ...)` decorators were removed (SEC5-2, SEC5-3,
SEC5-5 x2, SEC5-6, SEC5-7); no assertion, parameter or helper changed. LEGITIMATE: each corresponding behaviour now passes
(strict markers would otherwise XPASS-fail). Caveat: my SEC5-6 test only checked four single-separator inputs, so it could
not detect the open two-separator form (SEC6-5); that is my test's coverage gap, now covered.

## Scan (`scripts/security_scan.sh`, file `docs/evidence/scan-20261112-052108.md`, commit 7151e64, complete)
| Tool | Exit | Result |
|---|---|---|
| pip-audit | 0 | No known vulnerabilities |
| bandit -r app -ll | 0 | clean |
| ruff (S* rules) | 0 | clean |
| npm audit --omit=dev | 0 | 0 vulnerabilities |
| npm audit (full) | 1 | 8 moderate (dev toolchain, unchanged) |
| gitleaks | n/a | TOOL NOT AVAILABLE locally (CI-only) |
| semgrep | n/a | TOOL NOT AVAILABLE locally (CI-only) |
| fallback secret grep | 0 | no patterns |
| web bundle grep (`.next/static`) | 0 | no patterns |

## Runs (database `quotepilot_sec`)
| Command (cwd `apps/api`) | Commit | Exit | Result |
|---|---|---|---|
| `pytest -q -p no:cacheprovider tests/security/test_audit_followups7.py` | 7151e64 | 0 | 24 passed, 6 xfailed (SEC6-1 x2, SEC6-2, SEC6-3, SEC6-4, SEC6-5) |
| `pytest -q -p no:cacheprovider -rfx` (full suite, once) | 7151e64 | 1 | 1126 passed, 7 failed, 2 skipped, 5 xfailed in 830 s. All 7 failures are in QA's untracked `tests/qa/test_qa_reverify_review6.py` (five D6-1 inputs = my SEC6-5, plus D6-2 and D6-3 documentation tests). The run preceded my sixth xfail (SEC6-5), which I ran with the file alone (counts above); the 5 xfails in the full run were my SEC6-1 x2, 2, 3, 4. |
| `ruff check` on `test_audit_followups7.py` | 7151e64 | 0 | clean |
| skips in the full run | | | the two compose-render tests needing a repo-root `.env` |
Ad hoc measurements (timing, differential, sweep, seed subprocesses) were run as Python scripts against the importable
modules and are reproduced in the tables above; the 8f9416e module versions were extracted with `git show`.

## Residual risks
| Risk | Level |
|---|---|
| Two-separator leading-zero forms read 1000x too high (SEC6-5) | Medium |
| Matching cost unbounded and GIL-holding (SEC6-2) | Medium |
| `normalize_sku` quadratic in catalog import (SEC6-1) | Medium (admin only) |
| Negative stock stored (SEC6-3); NFKC-expanding symbols glue digits (SEC6-4) | Low |
| All parsing after the sandbox runs in the API process without a timeout (design) | Medium |
| Locale guessing (`1,234` = 1234) remains documented | Low |
| Compose always seeds demo accounts with the chosen demo password | Low/Info |
| gitleaks and semgrep not run locally; CodeQL result not read by me | Info |

## Not tested
Live servers and browser; the e2e suite itself; real Compose start-up; CI runs and the CodeQL result (coordinator-reported
only); a multi-minute end-to-end proof of SEC6-1/SEC6-2 against a live API and a real 50,000-row catalog (both are
extrapolated from per-item measurements); PDF files at the sandbox ceiling; concurrent-load behaviour; the unchanged tenant,
CSRF, cookie and file-limit matrices beyond the existing suite.
