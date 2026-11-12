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
- Decide whether Stage 1 may ship with S1-F21 (AI extraction adapter present, disabled, not wired) partial.
- Decide whether a manual screen-reader audit (X-08) is required for Stage 1.
- Decide whether running the Docker Compose topology (never executed in the development container) is required before
  calling Stage 1 released.
