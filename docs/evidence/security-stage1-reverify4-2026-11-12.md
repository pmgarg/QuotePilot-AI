# Security re-verification 4 - Stage 1, commit bf05198 (2026-11-12)

Auditor: independent security agent (did not implement round 7). Local application only; no live servers were started
(ports 8092/3092 never opened); no third-party targets; no paid services. No product code changed; nothing committed.

## Scope
Round-7 changes: `money.parse_decimal` and all callers (catalog import, RFQ file/text parsing, `_guess_columns`),
quote editor (`quotes/[id]/page.tsx`: pairing by id, `is_custom:false` on re-link, fieldset locking),
`scripts/check_compose.py`, `scripts/revoke_sessions.py`, and the two strict-xfail removals in
`test_audit_followups4.py`. Prior findings R4-1, R4-2 re-checked. Tests: `apps/api/tests/security/test_audit_followups5.py`.

Runs used database `quotepilot_sec` (schema reset with the owner role first) via `DATABASE_URL_TEST` /
`DATABASE_ADMIN_URL_TEST`.

## Findings
| ID | Severity | Finding | Test (strict xfail) |
|---|---|---|---|
| SEC4-0 | Medium | Round-7 fix is incomplete. Only `\d[eE][+-]?\d` is guarded; `_NUM_CLEAN` then strips the letters. `1 e3`, `1e 3`, `1 E 3`, `1ee3`, `1e  3` still parse to **13** (same silent misread as SEC3-11). Reaches catalog `list_price`/`cost_price`/`stock_qty` and RFQ quantities. Confirmed end to end by CSV import (price stored as 13). Also `e3`->3, `12e`->12, `1e+`->1. QA's untracked `test_qa_reverify_review4.py` independently fails on the same inputs (9 failures in the full run). Fix: reject any string whose letters other than a recognised currency/unit token are adjacent to digits and an `e`, or generally reject text containing `e`/`E` between digit groups after whitespace removal. | `test_sec4_0_*` (2) |
| SEC4-2 | Low/Medium | Catalog import: a price >= 1e12 (`1e30` is accepted by the new bound; also plain 31+ digit strings, or XLSX numeric cells `1e300`/`1E+400` that arrive as floats) raises an unhandled `DataError` (Numeric(18,6) overflow) or `decimal.InvalidOperation` in `quantize` => HTTP 500, whole import aborted. Admin-only, self-inflicted, nothing stored. Fix: bound-check magnitude in `build_products` and report a row error. | `test_sec4_2_*` (8 params) |
| SEC4-6 | Medium | RFQ intake (any sales member): quantity >= 1e15 gives HTTP 500; quantities 1e7..1e15 (e.g. `1e12`) are stored and `generate-quote` copies `1e12` into a quote line, bypassing the 10,000,000 cap enforced by the editor schema. Price stays the catalog price (not invented) but the line total can overflow Numeric(18,3). Fix: cap in `extract.py` (flag `quantity_unreadable`/`quantity_too_large`). | `test_sec4_6_*` (4), `test_sec4_6b_*` |
| SEC4-1 | Low | `parse_decimal(float)` returns `Decimal('NaN')`, `Infinity`, `1E+300` unchecked (float branch skips the finite/bounded check). openpyxl turned `nan`/`inf` cells into text in my probe, so NaN was not reachable via XLSX; `1e300`/`1E+400` were (SEC4-2). | `test_sec4_1_*` |
| SEC4-3 | Low | U+2212 minus is stripped: `−5` parses to +5 (sign flip; pre-existing, not from round 7). | `test_sec4_3_*` |
| SEC4-4 | Low | Digit groups separated by words are concatenated: `12 per 6` -> 126, `10 x 5` -> 105, `0x10` -> 10 (pre-existing; same root cause as SEC4-0). | `test_sec4_4_*` |
| SEC4-5 | Low/Info | `-0`, `-0e5` return negative zero (`(0)` returns +0, inconsistent); `-0` passes the `< 0` price check. Not observed to cause a wrong total. | `test_sec4_5_*` |

No new Critical or High finding. No finding lets a user obtain a price that was not in the catalog, supplied by a
privileged role, or marked manual.

## Per-control results
| Control | Result | Evidence |
|---|---|---|
| Exponent forms exact (`1e3`, `8.5E2`, `1.e3`, `.5e1`, `1e0003`, full-width and Arabic-Indic digits) | PASS | `test_parse_decimal_exact_values` (13) |
| Bounded exponent: `1e31`, `1e-31`, `1e999999999`, `0e1000` rejected | PASS | `test_parse_decimal_rejects_hostile_or_ambiguous_text` (24) |
| NaN/Infinity/sNaN text rejected | PASS | same, `..._never_returns_non_finite_for_text`; CSV import test (A3-A11 all rejected) |
| Mixed forms (`1,000e2`, `USD 1e3`, `1e3 USD`, `1e3e3`, `1_0e2`) rejected | PASS | same |
| Mixed form with whitespace between digit and `e` | FAIL | SEC4-0 |
| Currency codes with E (`5 EUR`, `5EUR`, `3 E`) still parse | PASS | `..._never_silently_reinterpreted` |
| ReDoS / DoS (200k-char digit, `e`, dot, huge-exponent strings) | PASS (each < 1 s; 3M digits 0.18 s) | `..._linear_time_...` |
| Huge exponent / digit strings through import and RFQ | FAIL (500) | SEC4-2, SEC4-6 |
| Negative prices rejected on import (`-5`, `-5e0`, `(5)`, `-1e3`, `(1e3)`) | PASS | `test_import_negative_...`, import test |
| Quote editor/RFQ item PATCH numeric inputs (NaN, Infinity, -0, 0, 1e30, 1e999, negatives, caps) | PASS (422; these use Pydantic, not `parse_decimal`) | `test_rfq_item_patch_...`, `test_quote_editor_numeric_...` |
| Prices never invented from exponent text | PASS for pure forms; FAIL for SEC4-0 variants (13 invented from `1 e3`) | tests above |
| Manual price cannot be labelled catalog (server-side) | PASS: sales sends `unit_price 0.01` with any combination of `is_custom` true/false and `price_source:"catalog"` => 200 with `price_source=manual` (never `catalog`); re-link of a custom line to a product with a client price of 1.00 and `is_custom:false` stays `manual` | `test_server_rejects_or_downgrades_...`, `test_relink_...` |
| Editor row pairing by id / fieldset lock | Code read only (diff reviewed); lock is client-side UX; no server impact; no browser run by me | not tested in browser |
| R4-1: tenant audit rows hold no platform total | PASS: each tenant row is exactly `{"scope","count"}` with its own count (A=1, B=3); stdout has `revoked 4`; `--org` leaves other tenant untouched | `test_r4_1_recheck_*` (2), source check |
| R4-1/R4-2 xfail removals in followups4 | LEGITIMATE: diff shows only the two decorators, one stale comment and the "currently prints OK" comment removed; test bodies and assertions unchanged. The R4-1 assertion is weak (only `total` absent); my extra tests cover it | `git diff 287d09e bf05198` |
| R4-2: compose check fails closed | PASS: empty/None/missing/renamed/case-renamed api or web => non-zero; clean topology => 0; list-form environment crashes (non-zero) | `test_r4_2_*` (14) |
| Compose check still detects owner credentials | PASS: `QP_OWNER_PASSWORD`, `POSTGRES_PASSWORD`, owner DSN in any value, non-empty `DATABASE_ADMIN_URL`, `env_file` | `test_r4_2_..._detects_owner_credentials` (6) |
| Real compose render | Manual: `.env` copied from `.env.example`, `docker compose --env-file .env -f infra/docker-compose.yml config` exit 0, `check_compose.py` exit 0 (services: api, migrate, postgres, web); `.env` deleted. The pytest version skips because compose needs a repo-root `.env` (RR4-5, unchanged) | manual |

## Tool results (`scripts/security_scan.sh`, commit bf05198, file `docs/evidence/scan-20261112-031237.md`)
| Tool | Exit | Summary |
|---|---|---|
| pip-audit | 0 | No known vulnerabilities |
| bandit -r app -ll | 0 | no medium/high issues |
| ruff (S* rules) | 0 | clean |
| npm audit --omit=dev | 0 | 0 vulnerabilities |
| npm audit (full) | 1 | 8 moderate (dev toolchain: minimatch via typescript-eslint chain); not in the production tree |
| gitleaks | n/a | TOOL NOT AVAILABLE locally (CI-only) |
| semgrep | n/a | TOOL NOT AVAILABLE locally (CI-only) |
| fallback secret grep | 0 | no patterns found (weaker than gitleaks) |
| web bundle grep (`.next/static`) | 0 | no patterns; `.next/BUILD_ID` (03:09:50) is newer than the changed `page.tsx` (02:54) |
Scan output is not a proof of absence of vulnerabilities.

## Test runs (database `quotepilot_sec`)
| Command | Exit | Result |
|---|---|---|
| `pytest -q -p no:cacheprovider tests/security/test_audit_followups5.py` | 0 | 68 passed, 1 skipped (compose render needs repo-root `.env`), 18 xfailed (strict, = SEC4-0/1/2/3/4/5/6) |
| `pytest -q -p no:cacheprovider` (full API suite, includes the QA agent's untracked `tests/qa/test_qa_reverify_review4.py`) | 1 | 505 passed, 2 skipped, 16 xfailed (before my last two tests), 9 failed - all 9 in the QA file, all the SEC4-0 inputs and its import test; no failure outside it |
| `ruff check tests/security/test_audit_followups5.py` | 0 | clean |

## Residual risks
| Risk | Level | Note |
|---|---|---|
| Locale-tolerant parser strips letters and guesses separators | Medium | SEC4-0/3/4; every imported/parsed number needs a rule-based reject or a review flag |
| Magnitude not bounded on plain digits, floats, RFQ quantities | Medium/Low | SEC4-2/6 |
| Catalog `1,234` read as thousands (documented ambiguity) | Low | known |
| Zero or sub-cent list price accepted (`1e-30` quantizes to 0) | Low | import checks only `< 0` |
| gitleaks/semgrep not run locally | Info | CI only |
| Editor concurrency/lock is UX only | Info | server enforces lock_version |
| Compose never executed | Info | human decision X-09 |

## Not tested
Live browser behaviour of the editor changes; real compose start-up; gitleaks/semgrep; CI results for bf05198; the
QA-authored e2e spec; PDF/XLSX zip-bomb, cross-tenant, CSRF, cookie, SQLi matrices were not re-run beyond the full
existing suite (unchanged areas, covered by earlier files); hosted-LLM adapter (disabled by default); load testing.
