# QA (independent) re-verification 7 — Stage 1 @8258112 (fix round 10)

Date 2026-11-12 (UTC 08:40-09:45). Scope: `git diff 46c3bcb..8258112` (two-separator numbers and non-ASCII symbols in
`money.py`, negative stock, linear `normalize_sku`, matching budget, seed reading the repo-root `.env`, README).
I did not implement round 10. No product code was changed and nothing was committed. Expectations come from CLAUDE.md /
PRD ("never invent prices, stock or customer facts"; parse failures are reported, not guessed) and my re-verification-6 list.
HEAD was 8258112 before every run and did not move.

## Environment
| item | value |
|---|---|
| DB | local PostgreSQL 16 (service), `quotepilot_qa`, migrated (`0005b`); never the default database |
| `APP_ENV` | `test` |
| `DATABASE_URL`, `DATABASE_URL_TEST` | `postgresql+psycopg://quotepilot_app:***@localhost:5432/quotepilot_qa` (non-owner, RLS enforced) |
| `DATABASE_ADMIN_URL`, `DATABASE_ADMIN_URL_TEST` | `postgresql+psycopg://quotepilot_owner:***@localhost:5432/quotepilot_qa` |
| Playwright | Chromium, `E2E_API_PORT=8091`, `E2E_WEB_PORT=3091`, `--trace off`, `DEMO_PASSWORD` set |
| load | shared machine; the full API suite took 24 min instead of 11 (other agents' suites) |

## Runs
| # | Command (cwd) | Exit | Result |
|---|---|---|---|
| 1 | `.venv/bin/pytest -q -p no:cacheprovider tests/qa/test_qa_reverify_review7.py` (apps/api; final form) | 0 | **79 passed** (all new tests pass; no defect (i) found). Earlier iterations had 2-3 test-side failures only (symbol-count threshold, README regex, empty-env expectation), corrected |
| 2 | `.venv/bin/pytest -q -p no:cacheprovider -rs` full API suite, once, 09:12-09:36 (apps/api) | 0 | **`1245 passed, 2 skipped, 1 xfailed in 1449.71s (0:24:09)`**; skips: the two compose-render tests that need a repo-root `.env`; xfail: the security agent's SEC6-2 test. The tree also contained the security agent's untracked/modified verifier files (`test_audit_followups7.py` modified, `test_audit_followups8.py`, a temporary `test_tmp_budget.py`) |
| 3 | `npx playwright test --reporter=list --trace off` (tests/e2e; web built by the server command) 09:36-09:39 | 0 | **41 passed, 0 failed, 0 flaky, 1 skipped** (`screenshots.spec.ts`, self-skipped by design) in 2.8 min; includes my new spec; no browser failure, so nothing was re-run in isolation |
| 4 | `ruff check tests/qa`, `ruff format --check tests/qa` (apps/api), `tsc --noEmit -p .` (tests/e2e) | 0 | clean (12 files formatted) |
| 5 | differential scripts (`parse_decimal` 7151e64 vs 8258112 over about 360 cells and 200,000 fuzz strings; `normalize_sku` over 300,078 cells; `match` 46c3bcb vs 8258112 over 5,520 queries; timing of the fuzzy scan at 5k/20k/50k products) | 0 | results below |
| 6 | live server (real `uvicorn app.main:app`, 127.0.0.1:8091): 5,000-product catalog, 450-line RFQ, `/health` polled every 100 ms during the parse | 0 | parse 5.7 s, 50 lines flagged, `/health` max latency 5.63 s (median 2.8 ms): see J7-5 |

Evidence level: runs 1, 2, 5 are unit/integration (ASGI TestClient + PostgreSQL + RLS, subprocess seed, git-history differentials);
runs 3 (and the budget UI spec) are browser evidence; run 6 is a live server. Ports 8091 and 3091 are closed; no process of mine is
running; no repo-root `.env` was left behind (a test asserts it).

## Per-claim verdicts
| claim | verdict | evidence |
|---|---|---|
| 1. D6-1, D6-2, D6-3 pass unedited | **VERIFIED** | `git diff 46c3bcb..8258112 -- apps/api/tests/qa tests/e2e/specs` is empty; `test_qa_reverify_review6.py` is green in run 2 |
| 2. Both-separator numbers must be well formed | **VERIFIED, no regression of a realistic cell found** | accepted: `1,234.56`, `1.234,56`, `12,345.6789`, `1,234.5`, `123,456.7`, `1,234,567.89`, `1.234.567,89`, `1.234.567.890,12`, `AED 1,000,000.00`, `-1,234.56`, `(1,234.56)`, `€1.234,56`, `1,234.56 pcs`, lakh `1,00,000.50`, `10,00,000.00`, `1,00,00,000.5`. Rejected: `1234.567,89`, `1234,567.89`, `12,34.56`, `1,2345.67`, `1,23.4`, `123,45.6`, `1,234,56.7`, `.5,0`, `5.,0`, `1,.5`, `1,5.5`, `1.5,5`, `0,500.00`, `0.500,00`, `00,500.25`, `0,001.5`, `1,234.56.78` and 12 more. Comma-only path unchanged: `1,234,567`=1234567, `1,234`, `12,5`, `0,500`=0.5. 200,000 fuzz strings 7151e64 vs 8258112: 190,804 identical, 9,196 became None, **0 changed value, 0 newly accepted**; 80,000-cell digit-invention fuzz clean |
| Non-ASCII symbols whose compatibility form contains a digit | **VERIFIED exhaustively** | all 222 such code points in 0x80..0x10FFFF (including `㎡`, `㋀`, `㌀`-type squares, circled/parenthesised digits and the ligature range) make `5<c>`, `<c>5`, `<c>` and `AED 12<c>` not-a-number; `℃`, `™`, `№`, `°` (no digit in the expansion) still parse |
| 3. Negative stock unknown with a warning | **VERIFIED** | `-5`, `(5)`, `– 5`, `-0,5`, `abc`, 10^16 -> stock left unknown (6 warnings; the empty cell gets none); `0`, `-0`, `12,5`, `1,234`, `1e3` stored as 0, 0, 12.5, 1234, 1000 |
| `normalize_sku` equals the regex version | **VERIFIED** | differential over 300,078 cells (realistic SKUs, underscores, inner whitespace, NBSP, ideographic space, `ß`, `İ`, `ǅ`, Arabic-Indic and full-width digits, superscripts, combining marks, zero-width characters, ellipsis, dashes, `™`, random mixes): **0 mismatches**; `None` -> `""`, `123` -> `"123"`; linear (400,000-character hostile cells < 1 s in total) |
| 4a. Matching unchanged for descriptions <= 200 chars | **VERIFIED** | 5,520 queries (demo catalog 30 products and a 2,000-product synthetic catalog; description, with `qty`, with quantity prefix, uppercase, with and without SKU hint/token): status, candidate product ids, scores, categories and reasons identical to 46c3bcb in all of them. The unit test repeats this with 4,000+ comparisons |
| 4a'. Longer descriptions | **CHANGED, judgement J7-1** | of 480 queries over 200 normalised characters, 383 differ: the long text is matched on its first 200 characters. Example: "Hex bolt M8x40 zinc plated" plus 360 characters of delivery text was `unmatched` before and is now `ambiguous` with SKU-1001/1002/1003 (85.5 %) for a person |
| 4b. Budget through the API | **VERIFIED** | catalog of 5,000 products imported and activated; RFQ of 450 descriptive lines plus one exact-SKU line: lines 401-450 (exactly 50) carry `match_skipped_budget`, `match_status` `unmatched`, no candidates, no selection; lines 1-400 are matched/unmatched normally without the flag; the exact-SKU line 451 (after the budget) is `exact` with a selected product and no flag; generate-quote is refused (409/422) while they are unresolved; manual selection of a skipped line by `PATCH selected_product_id` works (status `resolved`); `is_custom` works. At the index level: exactly 400 x 5,000 = 2,000,000 comparisons are allowed, the 401st description line is skipped, exact SKU hints and SKU tokens in the raw text cost nothing; a new index has a new budget |
| 4c. What the user sees | **JUDGEMENT J7-4** | browser spec: row 401 shows the badge "match skipped budget" and the status "unmatched", **but also** the text "No catalog candidate. Mark as custom line or remove.", no product selector (the UI offers one only when candidates exist) and "Mark custom"; the page nowhere says that matching was skipped because of size or what to do. Lines inside the budget have no flag; the exact line after it is "exact" |
| 4d. Budget acceptable | **JUDGEMENT J7-5** | see below |
| 5. Seed reads the root `.env` | **VERIFIED** | subprocess runs against `quotepilot_qa`: repo-root `.env` only (cwd `apps/api`, i.e. `make demo`) -> exit 0, nothing secret printed, stdout lists accounts and "not shown", login works with that password and not with another; precedence **environment > working-directory `.env` > repo-root `.env`** each proven with distinct passwords (the losing passwords do not log in); root `.env` with empty value, 11 characters, other keys only or a commented line -> exit 2, empty stdout, the value never echoed; README quick-start line no longer says credentials are printed |
| 6. Regression | **VERIFIED** | runs 2 and 3 |

## Behaviour changes of round 10 (8258112 vs 7151e64)
| change | before -> after | judgement |
|---|---|---|
| numbers with both separators not well formed (`1234.567,89`, `1234,567.89`, `12,34.56`, `1,23.4`, `.5,0`, `1,5.5`, ...) | read as 1234567.89, 1234567.89, 1234.56, 123.4, 5.0, 15.5 -> None | IMPROVED: all of them were invented values from a malformed cell. A realistic cell is not affected: every realistic grouping (US, EU, Swiss apostrophe/space, lakh with decimals) keeps its value |
| leading-zero first group with both separators (`0,500.00`, `0.500,00`, `00,500.25`, `0,001.5`) | 500.00, 500.00, 500.25, 1.5 -> None | FIXED (D6-1) |
| `㎡`, `㋀`-type symbols | 2, 1 -> None | IMPROVED (digit expansions glued in); `5 m²`-like cells were already unreadable since round 8 |
| negative stock | stored (e.g. -5) -> left unknown + warning | IMPROVED (a negative stock is not a fact) |
| `normalize_sku` | regex -> two linear scans | identical results (0 mismatches in 300,078 cells) |
| fuzzy matching, descriptions > 200 characters | whole text -> first 200 characters | J7-1 |
| fuzzy matching, many lines | unbounded -> at most 2,000,000 comparisons per index, then `match_skipped_budget` | J7-4 / J7-5 |
| seed | env, cwd `.env` -> also repo-root `.env` | FIXED (D6-2); empty-but-set environment value still wins, J7-3 |
| README line | "prints LOCAL-ONLY credentials" -> "never prints a password" | FIXED (D6-3) |
Indian grouping **without** decimals (`1,00,000`, `₹1,00,000`) remains rejected (comma-only path) while `1,00,000.50` is accepted: unchanged from earlier rounds, listed as O7-1.

## (i) Defects — wrong value stored/shown, crash, lost data
**None found.** All new tests pass; no failing test was left in place. No Critical, High, Medium or Low defect of that kind in the delta.

## (ii) Judgement calls — two readings are defensible
- **J7-1 Long descriptions use the first 200 characters.** Reading A (implemented): bounded cost, and a long, diluted line now yields candidates for a person instead of "unmatched"; an exact SKU anywhere in the raw text is still found first. Reading B: information after character 200 (e.g. a size at the end) is ignored, so a candidate can be wrong; every non-exact result is reviewed by a person anyway. Acceptable; documented by `test_round10_long_descriptions_are_matched_on_their_first_200_characters_judgement`.
- **J7-2 `match_skipped_budget` is not cleared when a person resolves the line** (`update_item` does not remove it), so the badge stays next to a resolved line. Cosmetic; stated as an observation in a test.
- **J7-3 An empty-but-set `DEMO_PASSWORD` in the environment masks both `.env` files** (standard dotenv precedence). The seed then exits 2 with the generic message. Defensible; a stale `export DEMO_PASSWORD=` is a plausible trap. Test: `test_j7_3_...`.
- **J7-4 What the user sees for a skipped line.** The flag is shown ("match skipped budget"), the line is blocked like any unmatched line and can be made custom or removed, and the API accepts a manual product choice. But the screen also says "No catalog candidate" (not true: matching was not run), it offers **no product selector** (the selector exists only when candidates exist, the same for every unmatched line), and nothing explains the reason or the workaround (split the RFQ, add SKUs). Reading A: acceptable for an edge case, lines are reported and blocked, nothing is guessed. Reading B: the only UI way out is "Mark custom", which turns a catalog product into a custom line with a manually confirmed price, which is a bad outcome for exactly the lines that were skipped for size. I judge the wording "No catalog candidate" misleading and recommend a specific message and a selector (not a defect against the stated requirement; no wrong value is stored or shown).
- **J7-5 Is the budget acceptable for the PRD's target users?** Measured on this machine (fuzzy scan with `WRatio`): 5,000 products 34 ms per line, 20,000 products 144 ms, 50,000 products 328 ms. Lines that fuzzy-match before the budget bites: `2,000,000 / products` = **400 lines at 5,000 products, 100 lines at 20,000, 40 lines at 50,000** (the catalog cap is 50,000 rows). Exact SKUs and SKU tokens do not use budget. So it bites: a 100-line RFQ with no exact SKUs against a 20,000-product catalog is exactly at the limit; a 40-line RFQ against a 50,000-product catalog likewise; a 400+ line RFQ against 5,000. Typical small/medium distributors (a few thousand SKUs, RFQs of 5-100 lines) are not affected. The worst case remains expensive: the same measurements give about 13-14 s of API-process CPU per request at the budget (400 x 34 ms; 100 x 144 ms; 40 x 328 ms), longer than the implementer's computed 2.5-8 s. **Live measurement (run 6): a 450-line RFQ against 5,000 products parsed in 5.7 s and `/health` from a second connection stalled for up to 5.63 s**, i.e. the whole API process (all tenants) is unresponsive during an RFQ parse at the budget. This is the open SEC6-2 (declared "mitigated, not closed" by the implementer); the budget bounds, but does not remove, the stall. Acceptable as a documented limitation for a single-tenant/low-concurrency demo; not acceptable for a shared multi-tenant production without moving matching out of the request process.

## (iii) Observations
- O7-1 Indian grouping without decimals (`1,00,000`) is rejected while with decimals it is accepted (see above); unchanged since earlier rounds.
- O7-2 see J7-2.
- O7-3 Manual "add item" builds a fresh catalog index per added item (a full pass over the catalog descriptions); that path has its own fresh budget. Not new.
- O7-4 The security agent's tree additions (`test_audit_followups8.py`, a temporary budget test, a modified `test_audit_followups7.py`) passed or xfailed in my full run; I did not review them.
- O7-5 `Settings(_env_file=...)` in the seed also reads other keys of the root `.env`; only `demo_password` is used. Nothing is printed.

## Not tested
- GitHub CI and CodeQL; Docker/compose and the compose `migrate` service; matching against catalogs above 5,000 products through the API (the 20k/50k figures are index-level timings, not API runs); concurrent RFQ parses; the AI adapters; browsers other than Chromium; `mypy`.
- Timing figures depend on this shared machine and vary with load (the same scan measured 34-36 ms per line at 5,000 products in two runs).
- No statement here is a security certification.

## Files
- `apps/api/tests/qa/test_qa_reverify_review7.py` (79 tests, all passing)
- `tests/e2e/specs/qa-reverify-review7.spec.ts` (1 browser test, passing; imports a 5,000-product catalog and restores the demo catalog in `finally`)
- `docs/evidence/qa-stage1-reverify7-2026-11-12.md` (this report)
No earlier QA file was changed this round.
