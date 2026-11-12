# Security re-verification 7 - Stage 1, fix round 10 (verified at 8258112, 2026-11-12)

Auditor: independent security agent. I did not implement round 10. Local application only, no live servers (ports
8092/3092 never opened), no third-party targets, no paid services, no product code changed, nothing committed.
All pytest runs: `APP_ENV=test`, `DATABASE_URL_TEST=...quotepilot_app...@localhost:5432/quotepilot_sec`,
`DATABASE_ADMIN_URL_TEST=...quotepilot_owner...@localhost:5432/quotepilot_sec` (database `quotepilot_sec` for every run).
Scope `git diff 46c3bcb..8258112` (product files: `money.py`, `units.py`, `matching.py`, `rfqs.py`, `catalog_import.py`,
`seed.py`).

## Status of SEC6-1..6
| ID | Status at 8258112 | Evidence |
|---|---|---|
| SEC6-1 (`normalize_sku` quadratic) | CLOSED | regex-free scans; identical output to the old regex version on 360,000 random and 9 ordinary cells; 4,000,000-character hostile cells take at most 0.6 s (`lead/trail` shape), most under 0.05 s; catalog import of 1,500 hostile-SKU rows under 3 s (asserted; was 10 s for 500 rows). `normalize_text`, `normalize_unit`, header mapping (`re.sub(r"\s+")`), duplicate detection (set lookups) are linear (2,000,000-blank inputs under 0.2 s) |
| SEC6-2 (matching unbounded) | NOT CLOSED, residual is worse than declared: see SEC7-1 and the section below | measured end to end |
| SEC6-3 (negative stock) | CLOSED | `-1`, `(3)`, `−3`, `1e30` stock -> unknown with warning and row kept; `-0` -> 0; cost/price negatives and `999999999999.9999995` still row errors; `0` and `999999999999.999999` still stored |
| SEC6-4 (digit-expanding symbols) | CLOSED | all 1,112,064 code points checked on both sides of `5` (1,300+ characters with a numeric value or a digit-bearing NFKC form): all give None. A new cost problem: SEC7-5 |
| SEC6-5 (two-separator magnitude) | CLOSED for two-separator forms; a related single-separator form is open (SEC7-2) | 17 malformed forms rejected, 14 well-formed forms (including lakh grouping) keep their value; exhaustive run below |
| SEC6-6 (collapsed raw_text) | OPEN, Info, accepted by QA | unchanged |
Earlier: SEC5-2/3/5/6/7 and SEC4-0..6, R4-1, R4-2 re-run inside the full suite, no regression.

## New findings (open at 8258112)
| ID | Severity | Finding | Test (strict xfail) |
|---|---|---|---|
| SEC7-1 | High (availability) | The SEC6-2 mitigation bounds the number of comparisons, not the work. Cost per comparison grows with description length and with the query, so one request still pins the API process for 40-390 s. End to end through `POST /rfqs` (pasted text, ~190-character adversarial lines, 450 lines, sales user): 5,000 products x 60-char descriptions 40.6 s (397 lines fuzzy-matched, 53 skipped); 30,000 x 60 chars 41.7 s (66 matched); 50,000 x 60 chars 46.7 s (39 matched); 30,000 x 300 chars 389.9 s (66 matched); 30,000 x 1,500 chars, 300 lines 413.8 s. The implementer's estimate of 2.5-8 s is wrong by one to two orders of magnitude (per line 0.35 s at 20,000 x 60 chars, 4.2 s at 20,000 x 300 chars). The scorer holds the GIL (second thread got 3 ticks during a 0.4 s call), so every tenant on that process stalls. Fix: bound work, not count (a wall-clock deadline per index of a few seconds, counted in characters compared), cap description length used, and move matching out of the API process (worker process with timeout). | `test_sec7_1_*`, `test_sec6_2_*` (7) |
| SEC7-2 | Medium | `1234,567` -> 1234567, `12345,678` -> 12345678, `5550,000` -> 5550000, `1234,567,890` -> 1234567890: with one separator type, an all-three-digit tail after a first group of four or more digits is read as thousands (1000x; the reading a person would give is 1234.567). Found by the exhaustive comparison; this branch is unchanged by round 10. Fix: require `len(parts[0]) <= 3` in the all-groups-of-3 branch (else decimal or reject). | `test_sec7_2_*` |
| SEC7-5 | Medium | The new digit-expansion scan normalises every non-ASCII, non-decimal character one at a time: about 10 us per expanding character (`U+FDFA` expands to 18). A cell of 511 `U+FDFA` costs 4.9 ms. Measured end to end: a 6.7 MB header-less RFQ CSV (70 rows x 64 cells) held the API process for 19.6 s; a 6.8 MB catalog import 6.7 s (0.2 s for ASCII cells of the same size). Linear, but with a large constant, and reachable by any sales member. Fix: normalise the whole cell once and compare digit counts, or cache per character, or skip when the cell has no digit. | `test_sec7_5_*` |
| SEC7-7 | Medium (design) | There is no per-user rate limit on RFQ, catalog or quote endpoints (limiters exist only for login: `auth.py:51-53`). The concurrency cap of 4 applies only to sandbox parsers; pasted text bypasses it. Combined with SEC7-1 and SEC7-5, one account can hold the process continuously. Not exercised in parallel (see not tested). | none (design) |
| SEC7-3 | Low | The leading-zero rule is not applied to space or apostrophe grouping: `0 500` -> 500, `05 050` -> 5050, `0'500` -> 500. | `test_sec7_3_*` |
| SEC7-4 | Low | The seed validates the whole root `.env` as `Settings`: an invalid value (a short `SECRET_KEY`) crashes the seed and pydantic echoes `input_value='<the value>'` to stderr. Invalid UTF-8 also gives a traceback (no password in it). Fix: read only `DEMO_PASSWORD` from the file (`dotenv_values`). | `test_sec7_4_*` |
| SEC7-6 | Info | A head made only of dots and spaces is accepted as decoration (`. 5` -> 5, `..-5` -> -5, `-. 5,500` -> -5500). `trailing dot` (`12.`) is accepted by design. | passing doc test |

## Exhaustive comparison with an independent reference (task 1)
I wrote a reference reader (`_readings` in `test_audit_followups8.py`) from the human rules for sign, parentheses, thousands
groups (3 digits, lakh 2+3), decimal marks and zero-led groups, without reading the parser's branches. 1,000,000+ strings
were enumerated and compared (alphabet `{5 , .}` up to length 11, `{5 0 , . space}` up to 7, `{5 , . space - ( )}` up to 6; a
second pass added `{5 0 , . space -}` up to 8). Disagreement classes:
- **value not among the reference's readings**: only the SEC7-2 form (`\d{4,},\d{3}(,\d{3})*`). No other magnitude or sign error
  in any accepted two-separator or lakh form.
- **accepted although the reference calls it malformed**: only trailing dot (`5,5.`), punctuation-only heads (SEC7-6), and
  zero-led space groups (SEC7-3), all with the right magnitude except SEC7-3.
- **parser stricter than the reference**: `5,`, `,55`, `55,55,555` (lakh without decimals), `(5,)`; harmless (row error).
- **documented ambiguity** (parser picks one reading): `5,555` -> 5555, `5.555` -> 5.555 and the like.
The test enforces these classes (it fails on any new one).
Backtracking: the three new patterns are anchored, deterministic and applied only after the 128-character cap; adversarial
122-130 character chains cost 23-51 us. Per-character scans: the pre-check cap is 512 raw characters (checked before the
scan); the cost per cell is linear, 1-220 us for ordinary non-ASCII text, 4.9 ms for expanding characters (SEC7-5).

## SEC6-2 residual: measured numbers and recommendation (task 4)
- Every path that builds an index: `parse_rfq` (new RFQ, re-parse/retry: one fresh index each) and `add_item` (one fresh index);
  nothing else constructs `CatalogIndex` (grep). `update_item` (PATCH) does not re-match. So the bound is per request.
- Budget does not leak: `fuzzy_comparisons` lives on the index; a second request, another tenant, `add_item` and re-parse all
  start with a full budget (tested with the budget patched to three fuzzy lines). Inside one request an early run of unmatched
  lines can use the budget and later legitimate lines are flagged `match_skipped_budget` (exact SKU lines still match); that is
  limited to the attacker's own request.
- Query cap: a 9.5 MB description is cut to 200 characters (time equal to a short one); `raw_text` is at most 2,000 characters.
- Real worst case: see the SEC7-1 table (41.7 s at 30,000 x 60 chars, 389.9 s at 30,000 x 300 chars). Index build alone,
  one-line request or `add_item`: 0.2 s (5,000 products), 0.9 s (30,000), 1.35 s (50,000), 2.8 s (30,000 x 1,500 chars).
- Multiplication: no per-user limiter (SEC7-7); several requests in parallel serialise on the GIL during the scorer, so N
  parallel requests give an N x stall (inferred from the GIL measurement, not run in parallel).
- **Recommendation: not acceptable as a documented Medium for a first release that serves more than one tenant or untrusted
  users.** A single sales account can make the process unresponsive for 40 s to 6 minutes per request, and the figures the
  limitation text relies on are off by 10x-100x. It is acceptable only for a single-organisation, trusted-user deployment, or
  once (a) matching is bounded by wall-clock/characters rather than comparison count, and (b) a limiter caps concurrent
  expensive requests per user. The clean fix is to run matching in the sandbox/worker process with a timeout. Evidence that
  would change my mind: an end-to-end run (30,000 products x 300 chars, 450 adversarial lines) finishing in a few seconds with
  the remaining lines flagged `match_skipped_budget`, and a parallel run showing health checks answered within a second.
  The `test_sec6_2_*` xfail stays, with its reason updated to the residual (edited in `test_audit_followups7.py`).

## Seed reading the root `.env` (task 5)
Tested by running the seed in subprocesses with `_REPO_ROOT_ENV` pointed at temporary files (no repo-root file was created):
- Precedence: environment variable, then the working-directory `.env`, then the root file. An invalid environment value
  (`short`) refuses and does not fall through; a short cwd value refuses even if the root file is valid.
- Other variables in the root file do not change the seed's behaviour: `DATABASE_URL=...nope` in the root `.env` is ignored
  (the seed still wrote to the environment's database, exit 0; the root file's values are only read for `demo_password`).
  Environment variables override the root file for every key.
- Absent file, a directory named `.env` and a dangling symlink behave as absent (exit 2 with the instruction line, no
  traceback). A symlink to a real file works. A quoted value with spaces is kept with its spaces (Info).
- Failure modes: invalid UTF-8 gives a traceback (exit 1) without the password; an invalid `Settings` value in the root file
  gives a traceback that echoes that value (SEC7-4).
- Nothing secret on any path: the password never appeared in stdout or stderr, the hash was not printed, a failing DB
  connection did not echo it (re-checked from round 9).
An unreadable file (mode 000) could not be tested: the container runs as root.

## Declared test edits to `test_audit_followups7.py` (judged)
`git diff 46c3bcb..8258112`: five strict-xfail decorators removed (SEC6-1 x2, SEC6-3, SEC6-4, SEC6-5; 16 lines), no assertion,
parameter or helper changed, the SEC6-2 decorator untouched. LEGITIMATE: each behaviour now passes. Caveat: my SEC6-5 test only
covered two-separator inputs, my SEC6-4 test used nine symbols; both gaps are now covered by the exhaustive and code-point sweeps.
After this round I edited only the SEC6-2 xfail reason text in the same file (residual wording), uncommitted.

## Scan (`scripts/security_scan.sh`, file `docs/evidence/scan-20261112-093157.md`, commit 8258112)
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
| web bundle grep | 0 | no patterns |

## Runs (database `quotepilot_sec`)
| Command (cwd `apps/api`) | Commit | Exit | Result |
|---|---|---|---|
| `pytest -q -p no:cacheprovider tests/security/test_audit_followups8.py` | 8258112 | 0 | 43 passed, 5 xfailed (SEC7-1, 2, 3, 4, 5) |
| `pytest -q -p no:cacheprovider -rfx` (full suite, once) | 8258112 | 1 | 1283 passed, 1 failed, 2 skipped, 6 xfailed in 906 s. The failure is QA's `tests/qa/test_qa_reverify_review1.py::test_m3_confidential_floor_is_not_in_the_quote_payload_for_sales_and_readonly`; it passed 3 of 3 times when re-run alone. Its assertion `"37.5" not in body` can match a timestamp (`...:37.5xx` seconds) in the payload, so it is probably a flaky QA test (hypothesis, not proven). The 6 xfails are my SEC6-2 residual plus SEC7-1, 2, 3, 4, 5. |
| `ruff check` on `test_audit_followups7.py`, `test_audit_followups8.py` | 8258112 | 0 | clean |
Ad hoc measurements (budget end to end, U+FDFA cells, enumeration, seed variants, normalizers) were Python scripts and
temporary pytest files, since removed; their numbers are above.

## All findings still open across rounds (one place for the release reviewer)
| ID | Severity | Summary |
|---|---|---|
| SEC7-1 / SEC6-2 | High (availability) | matching holds the GIL for 40-420 s per request despite the comparison budget |
| SEC7-2 | Medium | `1234,567` read as 1,234,567 |
| SEC7-5 | Medium | digit-expansion scan: 19.6 s per 6.7 MB RFQ |
| SEC7-7 | Medium (design) | no per-user rate limit on expensive endpoints |
| All post-sandbox parsing and matching runs in the API process without a timeout | Medium (design) | root cause of SEC7-1/5/7 |
| SEC7-3 | Low | zero-led space groups (`0 500` -> 500) |
| SEC7-4 | Low | invalid root `.env` value echoed by pydantic in the seed traceback |
| Locale guessing (`1,234` = 1234, `1.234` = 1.234); sub-quantum price rounds to 0; zero price accepted | Low (documented) | |
| Compose always seeds demo accounts with the chosen demo password | Low/Info | |
| SEC7-6, SEC6-6, SEC5-4 | Info | dots-only head, collapsed raw_text, mixed digit scripts and `5%` accepted |
| Earlier Infos from release review 4 (RR4-4 substring lookup cap, RR4-5 compose test skip, R3-8, R4-3, R4-4) | Info | unchanged by this delta; not re-examined |
| gitleaks/semgrep not run locally; CodeQL result recorded by the coordinator only | Info | |
No Critical finding is open. I did not re-audit areas outside this delta (tenant isolation, RBAC, CSRF, sessions) beyond the
existing suite, which passed.

## Not tested
Parallel requests against a live API (the multiplication in SEC7-7 is inferred); matching with a real browser-created
50,000-row catalog (products were bulk-inserted as the owner role); mode-000 `.env` files (container runs as root); the
e2e suite; real Compose start-up; CI results; PDF files at the sandbox ceiling; the cause of the flaky QA test.
