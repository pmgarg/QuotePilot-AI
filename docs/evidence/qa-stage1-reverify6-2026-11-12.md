# QA (independent) re-verification 6 — Stage 1 @7151e64 (fix round 9)

Date 2026-11-12 (UTC 04:50-08:35; the container was restarted once in between, nothing in flight survived and the affected runs were repeated).
Scope: `git diff 8f9416e..7151e64` (parser edge cases, RFQ text-line parsing, demo seed, E2E harness). I did not implement
round 9. No product code was changed and nothing was committed; `git status` at the end shows only my files (and the
security agent's untracked files, which I did not touch). Expectations come from CLAUDE.md / PRD ("never invent prices";
parse failures are reported, not guessed) and from my re-verification-5 list.

## Environment
| item | value |
|---|---|
| commit | `7151e64` (`git rev-parse --short HEAD` before every run; HEAD did not move) |
| DB | local PostgreSQL 16 (service), database `quotepilot_qa`, schema reset and migrated by me (`0005b`); never the default database |
| `APP_ENV` | `test` |
| `DATABASE_URL`, `DATABASE_URL_TEST` | `postgresql+psycopg://quotepilot_app:***@localhost:5432/quotepilot_qa` (non-owner, RLS enforced) |
| `DATABASE_ADMIN_URL`, `DATABASE_ADMIN_URL_TEST` | `postgresql+psycopg://quotepilot_owner:***@localhost:5432/quotepilot_qa` |
| Playwright | Chromium, `E2E_API_PORT=8091`, `E2E_WEB_PORT=3091`, `--trace off`, `DEMO_PASSWORD` set (the seed now requires it) |
| load | shared machine (other agents' suites live) |

## Runs
| # | Command (cwd) | Exit | Result |
|---|---|---|---|
| 1 | `alembic upgrade head` on a freshly reset schema (apps/api) | 0 | `0005b (head)` |
| 2 | `pytest -q -p no:cacheprovider tests/qa/test_qa_reverify_review5.py tests/qa/test_qa_reverify_review4.py` (apps/api) | 0 | **341 passed in 89 s**: my 7 formerly failing D5-1/D5-2 tests and the 14 D4 tests pass **unedited** |
| 3 | **`.venv/bin/pytest -q -p no:cacheprovider -rs` full API suite, once, after the CI helper fix (apps/api, 08:20-08:31)** | 1 | **`7 failed, 1126 passed, 2 skipped, 5 xfailed in 646.79s (0:10:46)`**. All 7 failures are my defect tests: `test_d6_1_...` x5, `test_d6_2_...`, `test_d6_3_...`. Skips: the two compose-render tests needing a repo-root `.env` (RR4-5). The 5 xfails are the security agent's. |
| 4 | same command before the CI helper fix (05:18-05:32, DB state identical) | 1 | 7 failed, 1102 passed, 2 skipped (same 7 failures); superseded by run 3 (the security agent's `test_audit_followups7.py` was added meanwhile) |
| 5 | `npx playwright test --reporter=list --trace off` (tests/e2e; the web app is built by the server command) | 0 | **40 passed, 0 failed, 0 flaky, 1 skipped** (`screenshots.spec.ts`, self-skipped by design) in 2.4 min |
| 6 | `ruff check tests/qa` and `ruff format --check tests/qa` (apps/api); `tsc --noEmit -p .` (tests/e2e) | 0 | clean (11 files formatted); no new Playwright spec this round (none needed) |
| 7 | E2E harness proof, 3 throw-away Playwright invocations (section 4) | 0 / 0 / 0 | see below; the edit and the spec were reverted/removed |
| 8 | differential scripts: `parse_decimal` bf05198 vs 6b3c452 vs 7151e64 (about 330 cells), text-line parser 6b3c452 vs 7151e64 (9,000+ texts), `sku_tokens` vs `SKU_TOKEN.findall` (300,000 random strings) | 0 | results below |

Evidence levels: runs 2-4 and 8 are unit/integration (ASGI TestClient + PostgreSQL + RLS, subprocess seed); run 5 and 7 are
browser/harness evidence. Ports 8091 and 3091 are closed (curl: no connection); nothing of mine is running.

## Changes to earlier QA files (CI fix, declared)
`apps/api/tests/qa/test_qa_reverify_review5.py` (`_admin_url`, one line plus comment): CI failed on 7151e64 with
`KeyError: 'DATABASE_ADMIN_URL_TEST'` in 5 of its tests because the helper read that variable directly while
`tests/conftest.py` falls back to a default when it is unset. The helper now returns `os.environ["DATABASE_ADMIN_URL"]`,
which `tests/conftest.py` always sets at import time (to the `_TEST` value or its default). No assertion was changed.
My new review6 file uses `DATABASE_URL` / `DATABASE_ADMIN_URL` the same way. `grep -n 'environ\\[' apps/api/tests/qa`: the
only remaining direct reads are `DATABASE_ADMIN_URL` (reviews 2, 3, 5, 6) and `DATABASE_URL` (review 6), all set by conftest.
Proof used: **code reading plus a tiny throw-away check, not a live run**. conftest's default for both URLs resolves to
`quotepilot_test`, the shared database, so I did not run the five tests with the variable unset. With
`env -u DATABASE_ADMIN_URL_TEST -u DATABASE_URL_TEST -u DATABASE_ADMIN_URL -u DATABASE_URL` a script that imports
`tests.conftest` printed `DATABASE_ADMIN_URL = postgresql+psycopg://quotepilot_owner:***@localhost:5432/quotepilot_test`
and the helper `test_qa_reverify_review5._admin_url()` returned exactly that value (no connection was opened). With the
variables set (runs 3 and 4) the five tests pass against `quotepilot_qa`.

## Per-claim verdicts
| claim | verdict | evidence |
|---|---|---|
| D5-1 and D5-2 fixed; my 7 tests pass unedited | **VERIFIED** | `git diff 8f9416e..7151e64 -- apps/api/tests/qa tests/e2e/specs` is empty; run 2 |
| Comma rule ("a thousands group never starts with a zero") | **VERIFIED with D6-1 (Low) and O6-1** | `0,500`=0.5, `0,001`=0.001, `0,125 kg`, `AED 0,500`, `-0,500`, `(0,500)`, `012,345`=12.345; `0,500,000`, `0,5,0`, `0,,5`, `0,`, `,500` rejected; ordinary `1,000`, `10,000`, `1,000,000`, `1,234.56`, `1.234,56`, `12,5`, `0.500`, `1 000,50` unchanged. Gap: cells with both separators |
| New Unicode rules | **VERIFIED** | dashes U+2010..2015, U+2E3A/2E3B, U+FE58, U+2212, U+2796 before a number are a minus (`AED – 5`, `–٣`); `5²`, `5③`, `10⁰`, `12₅`, `½`, `1½`, `Ⅰ`, `①`, `12 万`, `20 m²`, `5 m³` are not numbers; Arabic-Indic, Extended, Devanagari, full-width digits, `°`, `№` still parse |
| Fuzz | **VERIFIED** | 60,000 seeded cells with the new tokens: never raises, always finite and < 1e31, digit sequence of a result equals that of the input (exponent forms excluded, typographic dashes in exponents read as minus) |
| RFQ text lines: same items as 6b3c452 | **VERIFIED** | differential over a hand-written corpus (clean list, bullets, tab/space aligned columns, qty forms, e-mail body, CRLF, NBSP, formula-like, locale numbers, SKU-only, noise) plus 6,000 generated texts (and 6,000 more in my exploratory run): **0 differences in quantity, unit, sku_hint, flags and source_ref**; descriptions differ only by whitespace collapsing and by leading/trailing separator characters on tab-separated junk lines (7 of 6,015 generated texts, all artificial) |
| Whitespace collapsing changes `raw_text`/`description` | **ACCEPTABLE** | only runs of blanks/tabs/NBSP become one space (`"Hex bolt ...   SKU-1001    500   pcs"` -> `"Hex bolt ... SKU-1001 500 pcs"`); no content is lost; the stored raw text is used only for display and for SKU lookup, which gives the same result |
| Matching unchanged | **VERIFIED** | `sku_tokens` equals `SKU_TOKEN.findall` for 100,000 random strings (all runs <= 64 characters; 0 mismatches); every SKU line of `rfq_clean.txt` is still an exact match with a quantity; pasted RFQ with aligned columns (spaces and tabs), `qty:` forms, SKU-only line, URL line and noise gives the same items and >= 3 exact matches; PDF text with wide column gaps reads 500, 250 and 2 |
| Long-line rule (> 1,000 characters) | **ACCEPTABLE, documented by tests** | after collapsing: a 1,001-character line with a SKU is kept without quantity (`quantity_missing`, sku_hint kept); without a SKU it is dropped; a 1,000-character line starting with a quantity is read; 5,000 blanks inside a line do not count; a spreadsheet row pasted as one tab-separated line (about 70 characters) is read like before. Real order lines and rows are far shorter; a whole e-mail pasted without line breaks becomes one flagged item for a person (reported, not guessed) |
| SEC5-5 (High) linear text lines | **VERIFIED** | nine hostile lines (200,000 blanks, 200,000 `a-`, `SKU-1001` + 100,000 dashes, 100,000 tabs, ...) in < 1.5 s total; pasted RFQ with three 8,000-blank lines answered in < 3 s and the ordinary line in the same text is still read |
| Demo seed: refuses without password | **VERIFIED** | subprocess, QA database: no variable -> exit 2, stdout empty, stderr "Set DEMO_PASSWORD (at least 12 characters) ... does not generate or print a password."; 11-character and empty value -> exit 2, nothing on stdout, the value never echoed |
| Demo seed: with password | **VERIFIED** | exit 0 twice (idempotent: exactly one `owner@northwind.example`); stdout lists the 10 accounts and "the value of DEMO_PASSWORD (not shown)"; the chosen password appears in neither stdout nor stderr; login with it succeeds (role `sales`, active catalog present); a different password gets 401; `.env` in the working directory is read |
| Seed documentation | **PARTLY: D6-2 (Low) and D6-3 (Info)** | README.md:25 still says `make demo # ... prints LOCAL-ONLY credentials`; the documented flow (`cp .env.example .env` at the repo root, then `make demo`) does not make the seed find the password (it runs with cwd `apps/api`, Settings reads `.env` from the working directory) |
| E2E harness: servers start before global setup | **VERIFIED (by earlier observation)** | in my re-verification-4 log the `[WebServer]` lines (uvicorn up, `next start`) appear before the first alembic line printed by `global-setup.ts` |
| E2E harness: no stale chunks after a source change | **VERIFIED** | throw-away edit of `apps/web/src/app/login/page.tsx:69` (visible marker text) + throw-away spec: the default harness rebuilt (`.next/BUILD_ID` mtime changed), the page showed the marker, no `/_next/` response >= 400. After `git checkout` of the page, a run with `E2E_SKIP_WEB_BUILD=1` served the old build (marker still visible, `BUILD_ID` unchanged), i.e. skipping works and does not rebuild; a default run rebuilt from the reverted source and the marker disappeared. The page edit was reverted and the spec deleted; `git status` shows no trace |
| Regression | **VERIFIED except my own defect tests** | runs 3 and 5 |

## Behaviour changes of round 9 (7151e64 vs 6b3c452; bf05198 value where it differs)
| cell | bf05198 / 6b3c452 | 7151e64 | judgement |
|---|---|---|---|
| `0,500`, `0,001`, `0,050`, `0,125 kg`, `0,999`, `AED 0,500`, `-0,500`, `(0,500)` | 500, 1, 50, 125, 999, 500, -500, -500 | 0.500, 0.001, 0.050, 0.125, 0.999, 0.500, -0.500, -0.500 | IMPROVED (the 1000x error D5-1) |
| `012,345`, `01,234`, `007,500` | 12345, 1234, 7500 | 12.345, 1.234, 7.500 | judgement call (O6-1): a zero-padded thousands export now reads 1000x lower instead of 1000x higher; neither is flagged; rejecting a multi-digit leading-zero group would be safer |
| `0,500,000` | 500000 | None | OK, reported |
| `0,500.00`, `0.500,00`, `00,500.25`, `0,001.5`, `0.500,5` | 500.00, 500.00, 500.25, 1.5, 500.5 | **unchanged** | **D6-1 (Low)**: the rule is applied only when a comma is the only separator |
| `..5`, `...5`, `..50` | 0.5 (round 8), None (bf05198) | None | IMPROVED (D5-2) |
| `–5`, `— 5`, `‐ 5`, `‒ 5`, `― 5`, `➖ 5`, `﹘ 5`, `⸺ 5`, `AED – 5`, `AED–5` | +5 | -5 | IMPROVED (a dropped sign turned a credit into a charge). Side effect: an en dash used as a bullet or separator in a quantity cell (`– 500`) is now -500, flagged `quantity_not_positive` instead of silently 500: safe |
| `5–`, `5 – pcs` | 5 | None | safe rejection; a trailing dash is not a number; small loss of convenience |
| `10–20`, `10—12` | None | None | unchanged |
| `5²`, `5③`, `10⁰`, `12₅` | 52, 53, 100, 125 | None | IMPROVED (invented digits, SEC5-2) |
| `²`, `①`, `½`, `1½`, `Ⅰ`, `⅕`, `12 万`, `20 m²`, `5 m³` | 2, 1, ..., 12 (some by luck) | None | safe rejection. `20 m²` / `5 m³` in a quantity cell: a plausible distributor entry (construction) is unreadable and flagged `quantity_unreadable`; the same at 6b3c452 (NFKC turned `²` into a digit), unchanged by round 9 in outcome |
| Arabic-Indic / Extended / Devanagari / full-width digits, `12 °C`, `°50`, `12 №` | parse | parse | unchanged |
| Everything else in my 330-cell list (currency before/after, thousands separators, accounting negatives, percent, units, trailing dots, NBSP, Unicode minus) | - | identical to 6b3c452 | unchanged |
| text lines (`items_from_text_lines`) | whitespace as typed | collapsed; lines > 1,000 characters not searched for a quantity | see verdicts; no quantity/unit/sku/flag change in 9,000+ texts |
| SKU runs | tokens found anywhere | tokens only in runs of <= 64 characters | O6-2: a slash/dash joined list of 9+ SKUs (> 64 characters in one run) now yields no SKU token (it did before); a 62-character run is unchanged. Acceptable (DoS bound), rare in real RFQs |

## Defects
**D6-1 (Low, same class as D5-1, left open by round 9).** `apps/api/app/services/money.py`, the `"," in core and "." in core`
branch runs before the new leading-zero check. Input (catalog price cell or RFQ quantity cell): `0,500.00`, `0.500,00`,
`00,500.25`, `0,001.5`, `0.500,5`. Expected: None (malformed) or a value below 1. Actual: 500.00, 500.00, 500.25, 1.5, 500.5.
Tests: `test_d6_1_leading_zero_group_with_both_separators_is_not_read_as_a_large_number` (5, failing).

**D6-2 (Low, usability/documentation).** `apps/api/app/core/config.py:13` (`env_file=".env"`, relative to the working
directory) with `Makefile` `demo` (runs the seed with cwd `apps/api`) and `README.md` Quick start (`cp .env.example .env`
at the repo root). Repro: repo-root `.env` containing `DEMO_PASSWORD=...` (valid, >= 12 characters), then
`cd apps/api && python -m app.fixtures.seed` (what `make demo` runs). Expected: exit 0 (or a README that says to export the
variable). Actual: exit 2 "Set DEMO_PASSWORD ... in the environment or in .env". Compose is not affected (`env_file: ../.env`).
Test: `test_d6_2_documented_quick_start_repo_root_dot_env_is_read_by_make_demo` (failing; it creates and removes a temporary
repo-root `.env`, and refuses to run if one exists).

**D6-3 (Info, documentation).** `README.md:25` still reads `make demo   # migrate + seed two synthetic organisations;
prints LOCAL-ONLY credentials`, contradicting lines 28-31 and the seed. Test:
`test_d6_3_no_document_still_says_a_password_is_printed` (failing). No other document still says a password is printed.

No Critical, High or Medium defect found. SEC5-2, SEC5-3, SEC5-5, SEC5-6 and SEC5-7 behave as claimed.

## Observations
- O6-1 and O6-2 are in the behaviour table (zero-padded thousands; SKU runs over 64 characters).
- O6-3 (Info). The implementer evidence shows 1008 tests; mine is 1140 collected (the extra tests are my 270+101 modules and
  the security agent's newer module); no expected failure other than my defect tests remains.
- O6-4 (Info). Unrelated to round 9, noted from earlier rounds and unchanged: `2k pcs` -> 2, text around exactly one number
  is read as that number, `AED - 5` -> -5.
- O6-5 (Info). The CI `api` job on 7151e64 failed only on my review5 helper (see "Changes to earlier QA files"); that is
  fixed here. I could not query GitHub, so the CI result of the fix is not verified by me.

## Not tested
- GitHub CI (neither the failure nor the fix), CodeQL, Docker/compose (no daemon) and the compose `migrate` service with `DEMO_PASSWORD`.
- Concurrent writers, real e-mail/Outlook pastes beyond my synthetic corpus, scanned PDFs, browsers other than Chromium, `mypy`.
- The security agent's new module (`test_audit_followups7.py`) beyond the fact that it passes/xfails in my full run.
- No statement here is a security certification.

## Files created or changed
- `apps/api/tests/qa/test_qa_reverify_review6.py` (101 tests; 7 failing for D6-1, D6-2, D6-3)
- `apps/api/tests/qa/test_qa_reverify_review5.py` (CI helper fix, one line)
- `docs/evidence/qa-stage1-reverify6-2026-11-12.md` (this report)
