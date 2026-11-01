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

## Human actions required
- Enable branch protection on `main` requiring checks `api`, `web`, `e2e`, `security` and one review (repository settings → Branches).
- Sign any risk acceptance in this file with name, date and rationale.
