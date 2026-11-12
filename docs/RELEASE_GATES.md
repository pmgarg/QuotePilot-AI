# Release Gates

A stage passes only when: all critical (C) requirements are IMPLEMENTED with runnable test evidence; CI/local suites
genuinely green (commands, exit codes, counts recorded); zero unresolved Critical/High vulnerabilities (or a
human-signed risk acceptance — AI may not accept risk); tenant isolation verified; acceptance demo (E2E) passes; and the
**independent release-reviewer** was actually invoked with a fresh context and returned PASS. Medium/Low risks and
limitations are documented in KNOWN_LIMITATIONS.md. We never claim "100% secure".

## Review template
`requirement_id | design_reference | code_reference | test_id | test_result/evidence | security_check | reviewer_verdict | open_issue`

## Gate log
| Stage | Date | Commit | QA evidence | Security evidence | Reviewer invocation | Verdict | Notes |
|---|---|---|---|---|---|---|---|
| 1 | 2026-11-01 | 3169ad6 | qa-stage1-2026-11-01.md (97 API + 6 browser tests; run @7a29d68) | security-stage1-2026-11-01.md (SEC-01..22; run @70351b7/7a29d68), scan-20261101-213930.md | **Invoked** (fresh-context reviewer with the read-only role from `.claude/agents/release-reviewer.md`; project agent names were not registered in this session, so the role ran as a general agent instructed to read-only tools) | **FAIL — BLOCKED** | Findings recorded verbatim below (review 1) |

## Review 1 — Stage 1 @3169ad6 (2026-11-01) — verdict FAIL / BLOCKED
Reviewer decision and findings (recorded by the orchestrator without alteration of substance):
- **H-1 (High)** Approved quote document mutable after `new_version` + `valid_until` change: `build_pdf_model` reads quote-level `valid_until` and live org/customer fields; `render_and_store_pdf` overwrites the stored approved PDF when the hash differs; defeats D1/SEC-22 (S1-F13/F15/A15/A18, X-03).
- **H-2 (Gate)** CI red on the candidate: `ruff format --check` failed on 4 QA test files; pytest/migrations/OpenAPI drift/E2E did not run in CI on 3169ad6. The implementer's "format clean" claim was false.
- **M-1** Login limiter key built before e-mail normalisation → whitespace variants get fresh budgets (S1-F02).
- **M-2** API trusts `X-Forwarded-For` from any client (`--forwarded-allow-ips *`, API port published) — SEC-05 not fully closed; no automated test for the proxy header fix.
- **M-3** Blocker text leaks margin % and the confidential margin floor to Sales/Read-only (S1-F20).
- **M-4** Sales can bypass the margin floor by marking a line custom with a manual price (S1-F12/S1-A03); needs a product decision or a privileged gate.
- **M-5 (process)** No independent QA/security re-verification of the fix round on 3169ad6.
- **Low/Info** L-1 users/sessions RLS write policies loose + untested; L-2 member-create e-mail enumeration (201 vs 409); L-3 last-owner race (no row lock); L-4 totals overflow possible (NUMERIC(18,3)); L-5 caps truncate instead of rejecting (S1-A08); L-6 parser sandbox lacks FS/network isolation; L-7 no regression tests for SEC-05/SEC-09 web fixes; L-8 doc drift (threat model test names, runbook kill switch script missing, traceability rows X-05/07/08/09 and S1-A*); L-9 product lookups rely on RLS only; L-10 weak SECRET_KEY guard.
- Confirmed fixed in code: D2/SEC-04, D4, D5/SEC-03/O1, D6/SEC-13, D7/SEC-06 (per field), D8, O2, O4, SEC-01, SEC-02 (policies exist), SEC-08/09/10/11/15/16/17/18/19.
- Required retest: format + green CI; fix H-1, M-1..M-4 with regression tests; independent QA and security re-runs on the new HEAD; docs fixes; fresh review.

## Re-verification after fix round 2 (independent, on ff99762)
- QA re-verification (`qa-stage1-reverify-2026-11-01.md`): 252/252 pre-existing API tests and 16/16 browser tests green;
  28 new API + 5 browser tests; defects **R-3 High** (line removal 500), R-4, R-1, R-2 Medium, R-5 Low; H-1, M-1, M-4, L-3,
  L-4, L-5, L-10, X-05 and D1–D8 confirmed fixed.
- Security re-verification (`security-stage1-reverify-2026-11-01.md`): SEC-01..22 fixed/partial table; 12 new findings
  NEW-1..NEW-12 (5 Medium incl. a Dockerfile exec-form bug), G-1 CI red (semgrep on the ops script).
- Fix round 3 (`implementer-fixes3-stage1-2026-11-01.md`): all R-*/NEW-* items fixed or documented; 293 API tests and
  21 browser tests green locally. Gate remains **INCOMPLETE** until CI is green on the new HEAD and release review 2 passes.

## Review 2 — Stage 1 @60adb00 (2026-11-01) — verdict FAIL / BLOCKED
Full report: `docs/evidence/release-review-2-stage1-2026-11-01.md`. All review-1 items verified fixed; CI green;
blocked on N2-1 (editor product re-link pricing bypass) and N2-2 (withdrawal of older approved versions) — both fixed
in round 4 (`implementer-fixes4-stage1-2026-11-01.md`) pending release review 3.

## Re-verification after fix round 4 (independent, on 58976f3) and fix round 5
- QA re-verification 2 (`qa-stage1-reverify2-2026-11-01.md`): N2-1 A/B, N2-2 (API), R-1..R-5, L2-1 confirmed fixed;
  defects **D2-1 High** (editor save relabels catalog lines manual), D2-2/D2-3/D2-4 Medium, D2-5 Low; 16 API + 5
  browser tests added; recommendation "release review 3 must not proceed as PASS on 58976f3".
- Security re-verification 2 (`security-stage1-reverify2-2026-11-01.md`): NEW-3/5/7/8, N2-1 A, N2-2 fixed; new
  **NEW-17 Medium** (= D2-1), NEW-13/14/15/16 Low, I-1, I-3 (one PR-triggered CI e2e run failed on the same HEAD).
- Fix round 5 (`implementer-fixes5-stage1-2026-11-02.md`): D2-1..D2-5, NEW-13..NEW-17, I-1 fixed with the agents'
  own tests as regression gates; migration `0005b` added; I-3 mitigated and left open (trace artifact unreachable
  from the container). Local: 329 API tests + 26 browser tests green. Gate stays **INCOMPLETE** until both CI runs
  are green on the new HEAD and release review 3 records its verdict.

## Review 3 — Stage 1 @d0aac67 (2026-11-02, resumed 2026-11-10) — verdict INCOMPLETE / BLOCKED
Full report: `docs/evidence/release-review-3-stage1-2026-11-02.md` (fresh-context reviewer, read-only tools).
No Critical, High or Medium defect found in the round-5 code; the reviewer checked `update_draft` (no manual price can
be relabelled `catalog`, no archived/inactive product can be newly linked), migration 0005b (correct, idempotent,
transactional), `approve()`, `rfqs.update_item` and the web withdraw/banner logic. Blocked on process gates:
- **R3-1 (Medium)** round-5 code had no independent QA/security re-verification and no scan file naming the commit.
- **R3-2 (Medium)** CI for d0aac67 was not recorded in evidence; I-3 not root-caused.
- **R3-3..R3-6 (Low)** compose start-up with the README command (fails closed), an undeclared test edit, stale
  traceability, no UI path to resolve `catalog_version_mismatch`. **R3-7..R3-10 (Info)** explicit org filter, 0005b
  error masking, revoke-script audit detail, missing tests.

### CI evidence recorded for d0aac67 (GitHub Actions, queried 2026-11-02)
| workflow | event | run id | conclusion |
|---|---|---|---|
| ci | push | 41369753932 | success |
| ci | pull_request | 41369758251 | success (api, web, e2e jobs all success) |
| security | push | 41369753972 | success |
| security | pull_request | 41369758393 | success |
Earlier pull_request `ci` runs failed in the browser job on 58976f3 (41362914665), 597f4b0 (41366647320) and e090089
(41368062998) while the push runs of the same commits passed. The e090089 run (with page snapshots) showed logins
landing back on `/login` and submit clicks leaving the version in `draft`, i.e. requests not reaching the API state
the test expected; no product rule failed. The d0aac67 runs (with the API request log enabled) passed on both events,
so I-3 remains **not root-caused**; the diagnostics stay enabled and every gated HEAD needs both events green.

### Fix round 6 (Stage 1)
R3-3 compose command (`--env-file .env`) + `scripts/check_compose.py` in CI and `make compose-check`; R3-4 evidence
corrected; R3-5 traceability refreshed with review-3 verdicts; R3-6 editor "Use active catalog" control re-links a stale
line (server re-prices); R3-7 explicit `organization_id` filter in `stale_catalog_lines`; R3-10 tests (stale pending
quote cannot be approved; withdrawn-documents banner and re-link browser tests); NEW-15 test de-flaked; the auditor's
self-declared dead repro test removed. Independent QA and security re-verification on the round-6 HEAD and a fresh
review (4) are required before PASS.

## Independent re-verification 3 — Stage 1 @f69ac25 (2026-11-10)
- QA (`qa-stage1-reverify3-2026-11-10.md`): migrations base → head → `downgrade 0005` → head exit 0; full API suite
  330 passed; new QA module 17 passed; Playwright 32 passed, 1 skipped (incl. 4 new QA browser tests). D2-1..D2-5, R3-6
  and R3-10 confirmed closed in API and browser. **No defects**; Info O3-1..O3-4.
- Security (`security-stage1-reverify3-2026-11-10.md`, scan `scan-20261110-170807.md` naming f69ac25): NEW-13..NEW-17 and
  I-1 re-tested against a running system and closed; compose rendered as documented, `check_compose.py` OK, fails
  closed without `--env-file`; `tests/security` 97 passed, 2 strict xfail (Info). **No Critical/High/Medium/Low**;
  Info R4-1 (revoke script writes the platform total into each tenant's row), R4-2 (`check_compose.py` passes vacuously
  if a service is renamed), R4-3 (inline comments in `.env.example` become values for `migrate`), R4-4 (forwarding
  filter is a blocklist; `X-Forwarded-Prefix` passes, no consumer), R4-5 (provenance of an auditor file not provable
  byte-for-byte; diff matches the declared edits).

### CI evidence for f69ac25 (GitHub Actions, queried 2026-11-10)
| workflow | event | run id | conclusion |
|---|---|---|---|
| ci | push | 42861093431 | success |
| ci | pull_request | 42861108149 | success |
| security | push | 42861093454 | success |
| security | pull_request | 42861108774 | success |

## Review 4 — Stage 1 @287d09e (2026-11-10, resumed 2026-11-12) — verdict INCOMPLETE / BLOCKED
Full report: `docs/evidence/release-review-4-stage1-2026-11-10.md` (project `release-reviewer` agent, Read/Glob/Grep only).
No Critical, High or Medium defect in the round-6 code; R3-1, R3-3, R3-4, R3-6, R3-7, R3-10 closed. Blocked on:
- **RR4-1 (Medium, gate)** no recorded CI run with counts on the gated HEAD; I-3 not root-caused.
- **RR4-2 (Low)** traceability, API contract and limitation wording behind the evidence.
- RR4-3 (Low) editor paired rows by array index; RR4-4..RR4-6 Info.
Human decisions listed by the reviewer are under "Human actions required" below.

### Fix round 7 (Stage 1)
`docs/evidence/implementer-fixes7-stage1-2026-11-12.md`: RR4-2 docs; RR4-3 (pair by id, reset `is_custom`); editor
locked during saves; R4-2 compose check no longer vacuous; SEC3-11 (scientific notation in the shared decimal parser,
reported by the Stage 3 security audit). Product code changed, so independent QA and security re-verification 4 and a
review 5 are required before PASS.

### CI evidence for bf05198 (fix round 7; GitHub Actions, queried 2026-11-12) — closes RR4-1 for that commit
| workflow | event | run id | job results | counts read from the job logs |
|---|---|---|---|---|
| ci | push | 43145985640 | api success, web success, e2e success | pytest `380 passed, 1 skipped in 368.94s`, coverage TOTAL 90 %; Playwright `Running 33 tests using 1 worker` → `32 passed (1.7m)`, `1 skipped`, no flaky line, no `retry #` |
| ci | pull_request | 43145992231 | api success, web success, e2e success | pytest `380 passed, 1 skipped in 376.54s`, coverage TOTAL 90 %; Playwright `32 passed (1.8m)`, `1 skipped`, no flaky line, no `retry #` |
| security | push | 43145985620 | success | — |
| security | pull_request | 43145992151 | success | — |

The pytest skip is `tests/security/test_audit_followups4.py:402` (compose render needs a repo-root `.env`, RR4-5); the
Playwright skip is `screenshots.spec.ts` (self-skipped by design). Local counts for the same commit were 379 passed,
1 skipped, 1 xfailed (the xfail was R4-1, fixed in the same round and re-run) and 32 passed / 1 skipped — consistent.

## Independent re-verification 4 — Stage 1 @bf05198 (2026-11-12) — defects found, gate stays open
- QA (`qa-stage1-reverify4-2026-11-12.md`; 71 API tests, 6 browser tests): round-7 editor fixes verified in the
  browser (row pairing, save lock, re-link), ordinary number formats unchanged, D2-1..D2-5 / R3-6 / R3-10 still closed;
  full browser suite 38 passed, 0 failed, 0 flaky, 1 skipped. **Defects: D4-1 (Medium)** catalog price ≥ 1e12 in
  scientific notation → unhandled numeric overflow (HTTP 500); **D4-2 (Low)** `1 e3`, `1e 3`, `1ee3`, `e3`, `12e`
  still misread. Full API suite at bf05198 with the new tests: exit 1, 14 failed (all D4-1/D4-2), 505 passed,
  2 skipped, 18 xfailed.
- Security (`security-stage1-reverify4-2026-11-12.md`, scan `scan-20261112-031237.md` naming bf05198): R4-1 and R4-2
  confirmed fixed; provenance rules hold. **Findings: SEC4-0 (Medium)** = D4-2 with end-to-end proof (a price of 13
  stored for `1 e3`); **SEC4-6 (Medium)** RFQ quantity ≥ 1e15 → HTTP 500 and quantities above the API cap copied into
  quote lines; SEC4-2 (Low/Medium) oversized prices → 500; SEC4-1, SEC4-3, SEC4-4, SEC4-5 (Low). No Critical or High.
- Found outside Stage 1's own verification, by the Stage 3 QA run: **D5 (Medium)** — responses were sent before the
  transaction committed (`app/db/session.py` / `api/deps.py`, unchanged since the first Stage 1 commit).
The round-7 parser fix was therefore **incomplete**, and review 4's "no Medium defect in the code" no longer holds
for the gated tree. No review was requested for bf05198.

### Fix round 8 (Stage 1)
`docs/evidence/implementer-fixes8-stage1-2026-11-12.md`: strict one-number parser (SEC4-0/1/3/4/5, D4-2), column and
API caps for imported prices and RFQ quantities (SEC4-2, SEC4-6, D4-1), commit before response (D5; probable cause of
I-3, recorded as an inference). Declared verifier-test edits are listed there. Product code changed again, so
independent QA and security **re-verification 5** and a **review 5** are required before PASS.

### CI on the round-8 commits (GitHub Actions, queried 2026-11-12)
| commit | `ci` (api / web / e2e) | `security` workflow | CodeQL code-scanning **check** |
|---|---|---|---|
| 3586da2 (round 8) | runs 43152110441 and 43152116127 (push + pull_request): api success, web success; e2e still running when queried, superseded by the next push | 43152110485, 43152116146: success | **failure** — "1 new alert including 1 high severity" |
| 6b3c452 (round 8b) | runs 43154055740 and 43154063337: all three jobs success on both | 43154055508, 43154063401: success | **failure** — same summary |
| af8c902 (CI change only) | not queried (docs/workflow commit, superseded) | 43156058810 (push), 43156063859 (pull_request): success; job logs print the SARIF results: python **1** (`py/clear-text-logging-sensitive-data`, `apps/api/app/fixtures/seed.py:171`), javascript **0** | failure (same alert) |

Test counts were not extracted for these three commits because each was superseded within the hour; counts are
recorded for the gated HEAD only. **The code-scanning check is a separate status from the `security` workflow
conclusion. Earlier gate tables recorded only workflow conclusions, so whether this check was red on earlier commits
is unknown;** the flagged line (the seed printing the demo password) exists since the first Stage 1 commit, so it very
probably was. From now on the check and the printed SARIF counts are part of every CI record.

## Independent re-verification 5 — Stage 1 round 8 / 8b (2026-11-12) — defects found, gate stays open
- QA (`qa-stage1-reverify5-2026-11-12.md`; 270 API tests, 2 browser tests; final runs at 6b3c452 / af8c902): D5
  (commit before response) verified from a second connection for login, customer create, submit, approve, PDF, file
  download and logout; failing commit → 500 with nothing persisted; live read-your-writes loop **0 misses of 1,480**;
  parser judged on ~180 realistic/ambiguous cells plus a 60,000-cell fuzz (never raises, never changes the digit
  sequence); caps verified at the boundaries; browser suite 40 passed, 0 failed, 0 flaky, 1 skipped. **Defects:
  D5-1 (Low)** `0,500` read as 500; **D5-2 (Low/Info)** `..5` accepted. Full API suite: exit 1, 8 failed (7 = D5-1/D5-2,
  1 = a contradiction inside the security agent's then-unfinished file), 955 passed, 2 skipped, 4 xfailed.
- Security (`security-stage1-reverify5-2026-11-12.md`, scan `scan-20261112-042305.md` naming 6b3c452): SEC4-0..6
  closed (SEC4-3 only for the U+2212 family); declared edits to its round-4 tests judged legitimate; the D5 change
  reviewed with no defect (late queries fail closed under RLS, one session per request, failed commit → generic 500, no
  cookie). The round-8 parser pattern was confirmed as a CPU denial of service and a regression (18.6 s for one
  40,000-character cell at 3586da2), fixed by 8b. **Open: SEC5-5 (High, pre-existing)** quadratic RFQ text-line
  patterns in the API process; **SEC5-6 (Medium)** = D5-1; SEC5-2, SEC5-3, SEC5-7 (Low); SEC5-4 (Info).
- Orchestrator note: the auditor confirmed the parser slowdown by timing, not the CodeQL alert itself (it could not
  read the alert). The alert turned out to be the seed line, not the parser — see the table above.
No review was requested for round 8.

### Fix round 9 (Stage 1)
`docs/evidence/implementer-fixes9-stage1-2026-11-12.md`: linear-time RFQ line parsing (SEC5-5), leading-zero comma
decimals (SEC5-6/D5-1), doubled dots (SEC5-7/D5-2), all dashes as minus (SEC5-3), non-decimal digit characters rejected
(SEC5-2), demo seed no longer prints or generates a password (CodeQL alert). Declared verifier-test edits are listed
there. Independent **re-verification 6** and a release review are required before PASS.

### CI on 7151e64 (round 9 + harness fix; GitHub Actions, queried 2026-11-12)
| workflow | event | run id | job results | counts / notes from the job logs |
|---|---|---|---|---|
| ci | push | 43159503457 | web success, **api failure**, e2e skipped | pytest `5 failed, 1003 passed, 2 skipped in 538.73s`; coverage TOTAL 93 % |
| ci | pull_request | 43159510659 | web success, **api failure**, e2e skipped | pytest `5 failed, 1003 passed, 2 skipped in 502.66s` |
| security | push | 43159503427 | all four jobs success | SARIF: python 0 results, javascript 0 results |
| security | pull_request | 43159510666 | all four jobs success | SARIF: python 0 results, javascript 0 results |
| CodeQL code-scanning check | — | check 130987393590 | **success** | the seed alert is gone |

The five failures are all in QA's `tests/qa/test_qa_reverify_review5.py` (`KeyError: 'DATABASE_ADMIN_URL_TEST'`): the
tests read a variable that only the local runs export. They are test-harness failures, not product failures, but the
head was **not green in CI** and has no CI browser evidence. QA corrected its helper in the re-verification 6 delivery.

## Independent re-verification 6 — Stage 1 round 9 @7151e64 (2026-11-12) — defects found, gate stays open
- QA (`qa-stage1-reverify6-2026-11-12.md`; 101 new API tests): D5-1/D5-2 closed; line-parser differential over
  9,000+ texts with no difference in quantity, unit, SKU hint or flags; `sku_tokens` equals the old search on 100,000
  strings; seed verified as a subprocess (exit codes, no password in any output, idempotent, login works); e2e harness
  claim verified with a throw-away page edit; full browser suite 40 passed, 0 failed, 0 flaky, 1 skipped. **Defects:
  D6-1 (Low)** leading zero with both separators; **D6-2 (Low)** repo-root `.env` not read by `make demo`;
  **D6-3 (Info)** stale README line. Full API suite: exit 1, 7 failed (all three defects), 1126 passed, 2 skipped,
  5 xfailed.
- Security (`security-stage1-reverify6-2026-11-12.md`, scan `scan-20261112-052108.md` naming 7151e64): SEC5-5,
  SEC5-7, SEC5-3/SEC4-3 closed; SEC5-2 closed for numeric characters; SEC5-6 partly closed. Declared test edits judged
  legitimate. **Open: SEC6-5 (Medium)** = D6-1; **SEC6-1 (Medium, admin only)** quadratic `normalize_sku`;
  **SEC6-2 (Medium, pre-existing, design)** unbounded fuzzy-matching cost in the API process; SEC6-3, SEC6-4 (Low);
  SEC6-6 (Info). No Critical or High.

### Fix round 10 (Stage 1)
`docs/evidence/implementer-fixes10-stage1-2026-11-12.md`: SEC6-5/D6-1, SEC6-4, SEC6-1, SEC6-3, D6-2, D6-3 fixed;
**SEC6-2 mitigated only** (query cap and per-request comparison budget) and listed under human actions below.
Independent re-verification 7 and a release review are required before PASS.

### Fix round 11 (Stage 1)
`docs/evidence/implementer-fixes11-stage1-2026-11-12.md`: fuzzy matching of large jobs now runs in a resource-limited
worker process with an 8 s deadline (at most two per API process), so the API stays responsive; lines that were not
matched are flagged `match_skipped_budget` (SEC7-1 / SEC6-2 mitigated, not closed: the auditor's per-call latency
assertions for `index.match` cannot be met by design and stay xfail); per-user rate limits on RFQ create/re-parse and
catalog import (SEC7-7); parser SEC7-2, SEC7-3, SEC7-5, SEC7-6; seed reads only `DEMO_PASSWORD` (SEC7-4, QA J7-3); QA J7-2
and J7-4. Nothing is marked VERIFIED. Independent re-verification 8 and a release review are still required before PASS.

### CI on the round-10 and round-11 commits (GitHub Actions, queried 2026-11-12)
| commit | `ci` | `security` workflow | CodeQL check |
|---|---|---|---|
| 8258112 (round 10) | web success; **api failure** on both events: `1 failed, 1160 passed, 3 skipped, 1 xfailed` — the security verifier's timing test `test_per_character_scans_are_bounded_by_the_pre_normalisation_cap` (12–16 ms on the runner vs a 10 ms threshold; real per-cell cost, reduced in round 11); e2e skipped | success; SARIF python 0, javascript 0 | success |
| 8cfcff2 (round 11) | web success; **api failure** on both events: `2 failed, 1347 passed, 3 skipped, 1 xfailed, 1 xpassed` — two QA tests that run `git show` on history CI does not check out; e2e skipped | success except **gitleaks on the pull_request run** (a dummy `SECRET_KEY=tiny-secret-visible` string in a security test) | success |

Both round-11 failures are verifier-test portability problems, fixed in 23c1eb3 (history-based QA tests skip without
history; the two synthetic markers are allowlisted). CI for the head that contains these confirmations is recorded in
the next entry of this file.

## Independent confirmation of fix round 11 — Stage 1 @23c1eb3 (2026-11-12)
Process change (requested by the project owner: the fix → re-verify loop was consuming disproportionate effort):
this round was confirmed by two **fresh** verifier agents against a **fixed checklist limited to the round-11 diff**,
without re-running the full suites and without exploratory campaigns. New findings below High are logged as backlog
instead of triggering another fix round. This is a narrower verification than rounds 4–7 and is stated as such.
- QA (`qa-stage1-confirm-round11-2026-11-12.md`): 7 of 7 checklist items CONFIRMED (ordinary matching unchanged;
  worker path gives the same results as the in-process path; rate limits; parser cells; seed precedence; the exact-SKU
  control and the new message in the browser; declared test edits). No defect. Observation O1 (Low, not reproduced in
  18 reruns): once, one candidate list differed between worker and in-process path at position 2 — possibly tie order.
- Security (`security-stage1-confirm-round11-2026-11-12.md`): parser findings SEC7-2/3/5/6 and seed SEC7-4 CLOSED;
  tenant isolation of the worker path CLOSED; declared edits legitimate. **SEC7-1 / SEC6-2 MITIGATED, not closed**:
  the 389.9 s worst case (30,000 products × 300 characters, 450 adversarial lines) now takes 11.4–11.6 s, with health
  checks answered throughout (max 0.42 s); 47–52 lines matched, about 400 flagged for a person. Residual: jobs up to
  50,000 comparisons still run in the API process (≈ 0.14 s per line at 30,000 products), the database transaction is
  held while waiting, two matcher processes per API process. **New, backlog:** SEC8-2 (Medium) `POST /rfqs/{id}/items`
  is neither rate-limited nor routed through the bounded matcher (1.4–2.1 s per call on a 30,000-product catalog);
  SEC8-1 (Low) the reader of the worker's output does not catch a `RecursionError`. The auditor's statement: no
  Critical or High finding it considers release-blocking is open, **subject to accepting the matching residual**.

CI on 5195ee0 (the commit that added the two confirmation test files; runs 43206479891 push, 43206491750
pull_request): `web` success, `security` workflow success on both events (gitleaks green again; SARIF python 0,
javascript 0), CodeQL check success, **`api` failure at the lint step on both events** — `ruff check` found four
errors in the confirmation agents' new test files (two unused imports, two unsorted import blocks), so pytest did not
run and e2e was skipped. The orchestrator had committed those files without linting them. Fixed mechanically
(`ruff check --fix`, `ruff format`; no assertion changed; both files re-run locally: 14 passed, 1 xfailed).

CI on 58b1023 (lint fix; runs 43209371016 and 43209377707): `web` success; `security` workflow success on both
events (SARIF python 0, javascript 0; gitleaks success); CodeQL check success; **`api` failure**:
`1 failed, 1360 passed, 5 skipped, 2 xfailed, 1 xpassed` (coverage TOTAL 92 %) on both events — the single failure is
the security confirmation test `test_item1_worst_case_end_to_end_and_add_item_cost`, which wrote its measurements to a
path that exists only on the development machine (`FileNotFoundError`); its assertions had passed. e2e skipped.
Fixed by writing the dump to the system temp directory (declared edit, no assertion changed).

### Open for Stage 1 after round 11 (single list for the reviewer)
| ID | Severity | State |
|---|---|---|
| SEC7-1 / SEC6-2 matching cost | High → Medium residual | mitigated (deadline, worker process, budget); needs a human decision |
| SEC8-2 add-item path unlimited / in-process | Medium | open, backlog |
| SEC7-7 rate limits | Medium (design) | RFQ parse and catalog import limited per user and process; quote generation and exports not limited |
| post-sandbox parsing has no wall-clock timeout | Medium (design) | open; matching part now has a deadline |
| SEC8-1 worker-output reader | Low | open, backlog |
| QA O1 candidate tie order | Low | unreproduced observation |
| SEC6-6, SEC5-4 | Info | accepted / documented |
| documented Lows from earlier rounds | Low | listed under "Human actions required" |

## Human actions required
- Enable branch protection on `main` requiring checks `api`, `web`, `e2e`, `security` and one review (repository settings → Branches).
- Sign any risk acceptance in this file with name, date and rationale. Documented Lows awaiting acknowledgement:
  L-2 (member-create e-mail existence), L-6 (parser has no OS-level isolation), NEW-11/12 (secret heuristic, limiter
  lockout primitive, parser concurrency wait), SEC-12 (GET renders/audits PDF), SEC-14 (CSP `unsafe-inline`), SEC-21
  (per-process limiter), L2-3 (blocker reveals cost ratio). None is Critical or High.
- Acknowledge the catalog pinning rules (O2-1) and R3-6 behaviour together with the other documented Lows: a pending
  ad-hoc quote whose catalog moved on must be rejected (which returns the same version to draft), re-linked with "Use
  active catalog" and re-submitted; a SKU dropped from the new catalog can only be removed or replaced by a custom line
  that an owner/admin confirms.
- **Decide on SEC6-2 / SEC7-1** (High/Medium, mitigated in round 11, not independently re-verified): fuzzy matching of
  large jobs runs in a worker process with a deadline, at most two per API process, and per-user rate limits apply
  (see KNOWN_LIMITATIONS "Matching cost" and "Rate limits"). Remaining: limits are per process, a parse can hold a
  database transaction for the 8 s deadline, and long RFQs against very large catalogs lose suggestions for later
  lines. Either accept that for Stage 1 with a signature here after re-verification 8, or require more.
- Decide whether Stage 1 may ship with S1-F21 (AI extraction adapter present, disabled, not wired) partial.
- Decide whether a manual screen-reader audit (X-08) is required for Stage 1.
- Decide whether running the Docker Compose topology (never executed in the development container) is required before
  calling Stage 1 released.
