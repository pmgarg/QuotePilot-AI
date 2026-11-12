# Independent Release Review 5 — QuotePilot AI Stage 1 (RFQ to verified quotation)

**Release:** Stage 1, branch `claude/multi-stage-project-impl-8hz2wj`, HEAD b474aef (docs-only per the request) on code head 96e28ab. Review date 2026-11-12. Reviewer: read-only release-reviewer (Read/Glob/Grep only). I ran no tests and no git commands. I could not query GitHub, so every CI figure below is the orchestrator's record, not something I checked.

## 1. Verdict

**PASS WITH CONDITIONS**

No Critical or High finding is open on the evidence I read. The former High (SEC7-1/SEC6-2, matching cost) is now a bounded, measured Medium residual, but I cannot accept risk myself, so a named human signature is Condition 1. CI on 96e28ab is recorded green with job-level counts on both events, which closes RR4-1. Stage 1 may only be called released once the conditions below are met.

Conditions:

| # | Condition | Who |
|---|---|---|
| C1 | Sign in `docs/RELEASE_GATES.md` (name, date, rationale) the acceptance of these residuals, or require a fix. (a) SEC7-1/SEC6-2: in-process matching up to 50,000 comparisons, an 8 s deadline, 2 matchers per process, a DB transaction held during matching, later lines of very large jobs left flagged. (b) SEC8-2. (c) SEC7-7 design residuals: per-process in-memory limits, quote generation and export unlimited. (d) Post-sandbox parsing without a wall-clock timeout. | Human owner |
| C2 | Bring `docs/REQUIREMENTS_TRACEABILITY.md` up to date with rounds 8–11 (RR5-1). No row may be set to VERIFIED before C1 is done. | Orchestrator |
| C3 | Record that `git diff 23c1eb3..96e28ab -- apps/ tests/e2e infra .github` touches no product code (RR5-6). The confirmation was run on 23c1eb3 and the gate is 96e28ab. | Orchestrator/QA |
| C4 | Make the human decisions already listed in RELEASE_GATES "Human actions required". Without them the rows stay PARTIAL/INCOMPLETE and the stage cannot be called fully released. They are: branch protection, S1-F21 (AI adapter present, unwired), X-08 (manual screen-reader audit), Compose never executed, and the documented Lows. | Human owner |

Strongly recommended, not a condition: fix SEC8-2 before release. It is a small change: apply `limit_rfq_parse`, or a separate limit, to `POST /rfqs/{id}/items`, and route its matching through `fuzzy_batch`.

## 2. Findings

| ID | Sev | Finding | Evidence | Required action |
|---|---|---|---|---|
| RR5-1 | Medium (doc gate) | Traceability is stale after rounds 8–11. Rows still cite "re-verification 3 on f69ac25 (330 API + 17 QA, 32 browser tests)" and "review 4: PASS". None of these appear in the Stage 1 rows: the strict number parser, commit-before-response, the bounded matching worker, per-user rate limits, the seed change, the exact-SKU UI control, caps and 429. The NFR CI row stops at the round-7 head and never mentions 96e28ab. The I-3 open_issue says "not root-caused" while KNOWN_LIMITATIONS records a probable cause (D5, an inference). The S1-A rows say "see security evidence" without naming a file. | `REQUIREMENTS_TRACEABILITY.md` lines 10–61 | Update the rows for X-01/02/03/06, S1-F04, F06, F09, F10, F19, A07–A09, A11 and A20 and the NFR CI row. Cite the files `qa-stage1-reverify4..7`, `security-stage1-reverify4..7` and `*-confirm-round11` plus the 96e28ab CI run ids. Re-date or remove "review 4: PASS" where the code changed afterwards. |
| RR5-2 | Medium (High at origin) | SEC7-1/SEC6-2. The 389.9 s API stall is mitigated: the worst case is now 11.4–11.6 s, health answered at ≤0.42 s, 47–52 of 450 lines matched, about 400 flagged for a person. Residuals: jobs ≤50,000 comparisons run in-process holding the GIL (about 0.14 s per line at 30,000 products, up to about 1 s per job); two matchers per process; the DB transaction is held up to 8 s, plus up to 8 s waiting for a slot. A realistic 100-line RFQ against a 20,000-product catalog (2,000,000 comparisons) exceeds the 8 s deadline, so later lines lose fuzzy suggestions (the J7-5 arithmetic, my inference). The worker sets RLIMITs silently and skips them if the platform refuses. | `services/matching.py:37-41,94-146`; security confirm item 1; QA reverify7 J7-5 | C1. Document the 100-line/20k-catalog degradation explicitly in KNOWN_LIMITATIONS. |
| RR5-3 | Medium | SEC8-2. The add-item route is unlimited and matches in-process. `limit_rfq_parse` is wired only at `rfqs.py:144,215` and `catalogs.py:61`. `POST /rfqs/{id}/items` (`rfqs.py:232`) reloads the catalog and does a single-line fuzzy match (1.4–2.1 s per call at 30,000 products). A member can loop it. | grep of `limit_*`; security confirm "New findings" | Fix (recommended) or sign as part of C1. |
| RR5-4 | Medium (design) | Rate-limit and parse-time residuals. The limiter is in process memory and per replica. Quote generation and exports are unlimited and were not measured by the auditor. Text parsing and cell parsing after the sandbox have no wall-clock timeout. | `ratelimit.py`; KNOWN_LIMITATIONS "Rate limits"; security confirm items 4 and "Still open" | C1(c)(d). Shared limiter is the Stage 2 plan. |
| RR5-5 | Low | Expected-failure tests encode open limits. `test_sec7_1_…` was changed from `strict=True` to `strict=False` and now XPASSes on the CI runner, so it no longer detects regression. `test_sec6_2_matching_a_long_description…` is a strict xfail demanding under 20 ms per `index.match` call, which the design cannot meet and which is not the property that now matters. `test_info_sec7_6_…_is_accepted_documented` keeps its old name but asserts rejection. | `test_audit_followups8.py:214-217,327-341`; `test_audit_followups7.py:245-261` | Replace the two latency tests with assertions on the real properties (deadline honoured, API responsive), which the integration test already measures. Rename the SEC7-6 test. |
| RR5-6 | Low | Narrowed round-11 verification (see answer B). Not covered by any verifier: three parallel big parses and semaphore contention; behaviour when RLIMIT is refused; the unexplained `pgrep` PIDs 3021/3030 in the auditor's first run (not reproduced); QA O1 (one candidate list differing worker vs in-process at index 1, not reproduced in 18 reruns, tie order suspected but unproven); SEC8-1 (RecursionError in `_parse_worker_lines`, not attacker-reachable today). The `23c1eb3` to `96e28ab` delta is claimed test-only but I could not diff it. | Both confirm files "Not checked"; RELEASE_GATES lines 261–273 | C3. Backlog: fix SEC8-1; capture O1 precisely if it recurs. |
| RR5-7 | Low | The parser keeps locale heuristics on catalog prices: `1,234` is read as 1234 (not 1.234), `12,5` as 12.5, `1234,567` as 1234.567. They are documented and pinned by tests. An import with a European convention can therefore silently load a price 1000 times off in a pricing-relevant path. Rejected row cells are reported, but the ambiguous ones are not. | `money.py:251-267`; KNOWN_LIMITATIONS "Numbers read from files" | Backlog: per-import decimal-convention setting, or reject the ambiguous three-digit comma form. Keep the human catalog review step. |
| RR5-8 | Low | The gitleaks allowlist hides every path under `docs/evidence/` and `tests/fixtures/`. The added markers (`tiny-secret-visible`, `Root-Env-Pass-Marker-77`) are unanchored regexes. | `.gitleaks.toml` | Narrow the path allowlist to specific files, or anchor the regexes. |
| RR5-9 | Low | The seed still builds `Settings` from the working-directory `.env`, so an invalid value there can fail the DB setup and be echoed (declared residual). | `implementer-fixes11` "Not fixed" | Backlog. |
| RR5-10 | Info | Local scan evidence names 8258112 (round 10), before the round-11 worker subprocess existed. Bandit, semgrep, pip-audit and CodeQL for 96e28ab rest on CI job success; SARIF counts are recorded, but findings counts for bandit, semgrep, pip-audit and npm audit are not. | `scan-20261112-093157.md` line 3; RELEASE_GATES line 279 | Record job-log summaries, or re-run `make security-scan` at the release head. |
| RR5-11 | Info | Wording is stale: RELEASE_GATES line 230 and line 314 still say "re-verification 8 required"; what happened was the narrowed confirmation. | RELEASE_GATES lines 230, 314 | The signature text in C1 must name what was actually verified. |
| RR5-12 | Info | No "100% secure" or equivalent claim anywhere; RELEASE_GATES line 7 states the opposite. | grep of docs | None. |

## 3. Review template (Stage 1 requirement groups)

Unless a row says otherwise, "CI" means the 96e28ab runs 43212316572 and 43212324076 (pytest `1361 passed, 5 skipped, 2 xfailed, 1 xpassed`; Playwright `42 passed, 1 skipped`) and the verifier files are those in `docs/evidence/`.

| requirement_id | design_reference | code_reference | test_id | test_result/evidence | security_check | reviewer_verdict | open_issue |
|---|---|---|---|---|---|---|---|
| X-01, S1-A14 tenant isolation | ADR-002, RLS | `db/session.py::set_tenant`, `deps.py:83-106` | `security/test_tenant_isolation`, `followups4`, `test_confirm_round11::worker_input_contains_only_callers_catalog` | CI plus security reverify4–7 and confirm | The worker payload holds only the caller's catalog version; no module-level cache in `matching.py` (I read it) | PASS | none |
| X-02, X-03, S1-F14, A13, A19 RBAC, approval, audit | HLD state machine | `authz.py`, `Principal.require`, `quotes.py` | `test_rbac_csrf_headers`, `test_quotes` SoD tests | CI | Server-side 403; self-approval blocked | PASS | RR5-1 (docs) |
| Commit before response (D5) | LLD | `deps.py:24` (`scope="function"`), `session.py:68-77` | QA reverify5 second-connection checks, 1,480 reads with 0 misses | QA local plus CI | A failed commit gives a 500 with nothing persisted; no tenant leak on late queries | PASS | the I-3 cause is an inference |
| S1-F04, A07, A08 catalog import and number parsing | LLD catalog | `catalog_import.py`, `money.py:180-276` | `followups7/8`, `qa_reverify5-7`, `test_round11_fixes` | CI; QA fuzz of 60,000 cells; parser SEC7-2/3/5/6 closed | Formula cells stored as text, caps 413, no float money | PASS | RR5-7 (heuristic, documented) |
| S1-F06, A05, A06, A08, A17 RFQ intake and parsing | ADR-005 | `parsing/*`, `extract.py` (`MAX_TEXT_LINE = 1000`, 64-char token cap) | `test_rfq_flow`, QA differential over 9,000 texts | CI; SEC5-5 closed in reverify6 | Subprocess plus rlimits; no formula evaluation | PASS | RR5-4 (post-sandbox parse timeout) |
| S1-F07, F08, F09, F10, A01, A02, A04, A09, A10 items, matching, human verification | ADR-006 | `matching.py`, `match_worker.py`, `rfqs.py`, `rfqs/[id]/page.tsx:22-40,114` | `test_round11_fixes` (unit 56, integration 6), `round11-sku-select.spec.ts` | CI; QA confirm item 2 (worker equals in-process) | Exact-SKU control validated server-side via the `PATCH`; categorical confidence; no AI authority | PASS WITH CONDITIONS | RR5-2, RR5-3, RR5-6 (O1) |
| SEC7-7 rate limits | SECURITY_POLICY | `ratelimit.py`, `rfqs.py:144,215`, `catalogs.py:61`, `main.py:88` | `test_round11_fixes` (refused calls not counted, 403/404 not counted) | CI | 429 plus `Retry-After`; no audit row for refused calls | PASS WITH CONDITIONS | RR5-3, RR5-4 |
| S1-F11, F12, A03, A11, A12, X-06 pricing and overrides | ADR-004 | `pricing.py`, `quotes.py::compute_blockers`, `money.py` | `test_pricing`, `test_quotes` | CI | Decimal only, privileged audited override, no invented price or stock | PASS | RR5-1 (row text) |
| S1-F13, F15, F16, F17, A15, A16, A18 versions, withdrawal, export | ADR-007, ADR-010 | `quotes.py`, `exports/*` | `test_quotes`, e2e identical bytes and conflict banner | CI | CSV formula neutralisation; cost hidden for sales/readonly | PASS | none |
| S1-F19 seed | PRD | `fixtures/seed.py:78-119` (only `DEMO_PASSWORD`; no printing or generation) | `test_round9_fixes`, `followups8::test_sec7_4_*`, QA seed subprocess tests | CI; CodeQL alert gone | No password in any output (I read the seed up to the print block; it prints accounts only) | PASS | RR5-9 |
| S1-F20, A20 UI and browser E2E | USER_JOURNEYS | `apps/web/src/app/(app)/**`, `tests/e2e/*` | Playwright 42 passed, 1 skipped (`screenshots.spec` self-skip) | CI no retry and no flaky line; QA local runs | XSS test; the e2e config raises the limits (100,000) and `MATCH_TIMEOUT_SECONDS=120`, so production defaults are not exercised in the browser | PASS | worker-deadline path is API-test only |
| X-08 accessibility | USER_JOURNEYS | web | axe scans in the browser suite | pass | none | INCOMPLETE | no manual AT audit (C4) |
| X-04, X-05, S1-F21 AI boundary | ADR-009 | `services/ai/*` (disabled, not wired) | unit consent and redaction tests | pass | AI unwired, consent gate | PARTIAL (by design) | human decision (C4) |
| X-09, NFR CI | ADR-014 | `.github/workflows`, `infra/` | CI both events | green on 96e28ab (orchestrator record) | CodeQL, gitleaks, pip-audit, npm audit, bandit, semgrep success | PASS for CI; Compose INCOMPLETE | RR5-8, RR5-10, C4 |

## 4. Answers A–F

**A. Process integrity.** I checked a sample of the declared edits against the test files, but I could not run `git diff`, so this is a read of current file state only.
- The four removed strict-xfail markers (SEC7-2, 3, 4, 5) are gone, and the tests assert the fixed behaviour (`test_audit_followups8.py:203-225`, :489).
- The `strict=False` change on `test_sec7_1` is present at :327 with an explanatory comment.
- The assertion change in `test_info_sec7_6` is present at :217 (`. 5` and `..-5` now None) and matches the declaration.
- The security auditor confirmed the security-side edits against the declaration. The fresh QA confirmer confirmed the QA-side edits and the e2e expectation change, and judged each a consequence of a declared behaviour change.

None of what I sampled lowers the intent of a test: each changed assertion follows a deliberate behaviour change. Two process weaknesses remain. The same side that changed the behaviour edited the tests that pinned it, which is mitigated by independent confirmation but not eliminated. The `strict=False` change is a real loss of regression signal and should be replaced by the property tests in RR5-5.

**B. The narrowed round-11 verification.** It is sufficient for what it covered and not sufficient to call the worker path exhaustively verified.
- Covered: a checklist limited to the round-11 diff; measured worst case (3 runs, 11.4–11.6 s, health probes alongside); worker launch and environment; tenant isolation of the worker input; limiter semantics; parser and seed behaviour; the browser control. The full suites were run by the implementing side and by CI on the real gated head, including the verifiers' own test files.
- Missing: any independent full-suite run on the final tree; concurrency and slot contention; refused-RLIMIT behaviour; independent re-measurement of the rate-limit counting (the auditor read it, did not re-measure); quote and export cost; real PDF/XLSX files; secrets, bundle and log scans on the round-11 diff; the 23c1eb3 to 96e28ab delta.
- The remaining uncertainty is availability and determinism, not tenant isolation or authority, so I accept it with C3 and the C1 signature rather than demanding a ninth round.
- Rounds 7–10 had full independent re-verification, which is the stronger standard.

**C. Open findings.**
- *Gate rule:* a High needs a human signature. SEC7-1 was High and is now Medium-class by measurement. I concur on the evidence but the downgrade is not mine to approve, hence C1(a).
- *SEC8-2 (Medium):* does not block by the stated rules, but it is an authenticated availability gap sitting next to the mitigation. I recommend fixing it; otherwise it needs a named signature.
- *Rate-limit design residual and post-sandbox parse timeout:* may be carried as documented limitations with named acceptance, each described in KNOWN_LIMITATIONS.
- *SEC8-1, O1, RR5-7, RR5-8, RR5-9, SEC6-6, SEC5-4 and the earlier documented Lows:* carry as backlog or documented limitations. None is Critical or High, and none touches tenant isolation, approval gates or pricing authority.
- *Needs a human decision, not carry-over:* S1-F21, X-08, the Compose topology and branch protection (C4).

**D. CI evidence for 96e28ab.** Adequate, with the caveat that I could not query GitHub.
- RELEASE_GATES lines 275–286 give run ids for both events, job results for api, web and e2e, pytest counts (`1361 passed, 5 skipped, 2 xfailed, 1 xpassed`), coverage 92 %, Playwright `42 passed` with `1 skipped`, no flaky line, no `retry #`, and the security jobs with SARIF python 0 and javascript 0. This meets what RR4-1 asked.
- The 5 skips (3 history-based tests, 2 compose-render tests needing a repo-root `.env`), the 2 expected failures and the 1 XPASS are explained.
- The red runs between bf05198 and 96e28ab are each recorded with a cause: harness variable, timing threshold, `git show` on missing history, gitleaks marker, ruff on new test files, a dev-machine path. All are test-portability or lint failures, not product failures. The cost is that every head since round 9 went red once before going green, and the final green came from edits to verifier tests (RR5-5).
- Three of the cited head commits carry no browser evidence of their own. That is acceptable because only the gated head counts.

**E. Traceability and honesty.**
- No row is marked VERIFIED, which is correct. I found nothing reported as passing without a recorded command, exit code and counts in the implementer and verifier files I read.
- The matrix is not consistent with the evidence after rounds 8–11 (RR5-1), so C2 applies. The limitations and human-actions lists are largely complete and candid: SEC8-2, SEC8-1, O1, zero-padded groups and value ranges are all listed. Gaps: the 100-line/20k-catalog degradation, the RLIMIT silent skip and the worker's cost per large job (a 0.1 s start-up plus shipping descriptions) are only partly stated.
- No "100% secure" claim exists.

**F. RR4 status.**
- RR4-1 (CI on the gated head, counts, I-3): CLOSED for 96e28ab on the orchestrator's record. I-3 has a probable cause (D5), recorded as an inference, and is not independently proven.
- RR4-2 (docs behind the evidence): PARTIALLY CLOSED in round 7, but reopened by the staleness in RR5-1.
- RR4-3 (editor paired rows by index): CLOSED (round 7; QA reverify4 verified it in the browser).
- RR4-4 (substring lookup capped at 50): unchanged Info, listed in the gate log. Not re-verified by me.
- RR4-5 (a compose test skips in CI): still skips, and is explained in the CI table.
- RR4-6: Info, unchanged; not re-assessed by me beyond reading the gate log.

## 5. What I did not review

- GitHub Actions run pages, logs and the CodeQL alert state (orchestrator-recorded only).
- Any git diff, including f8ef581..23c1eb3 and 23c1eb3..96e28ab; I read the current files.
- The full text of `qa-stage1-reverify4..6` and `security-stage1-reverify4..6` (only gate-log summaries); I read the reverify7 defect tables and both confirm reports in full.
- Alembic migrations and RLS policy SQL; `quotes.py`, `pricing.py`, the exports, `catalog_import.py` and the parsing sandbox, beyond what the traceability and verifier evidence states.
- The actual `deps.py` FastAPI `scope="function"` semantics beyond reading the code. I relied on QA's second-connection evidence.
- The rest of the spec (`QuotePilot_Claude_Code_Master_Prompt.md`), PRD/HLD/LLD/THREAT_MODEL and `API_CONTRACT.md` beyond its preamble and the lines I grepped.
- Frontend bundle and log secret scans; Docker Compose execution; assistive-technology testing; browsers other than Chromium; load, concurrency and penetration testing.
- No statement here is a security certification, and nothing in this review shows the system is free of vulnerabilities.

## Decision

Release recommended once C1–C4 are satisfied. Until then Stage 1 stays "implemented, reviewed with conditions", not VERIFIED.

**Required retest:** no new QA or security run is needed for the conditions as written. If SEC8-2 is fixed, require a targeted QA/security check of the add-item path (429 behaviour, results identical to the bounded path, no cross-tenant effect) and a CI rerun on the new head. If any product code differs between 23c1eb3 and 96e28ab (C3), a targeted re-confirmation of that delta is required.

**Key files:** `/home/user/QuotePilot-AI/docs/RELEASE_GATES.md`, `/home/user/QuotePilot-AI/docs/REQUIREMENTS_TRACEABILITY.md`, `/home/user/QuotePilot-AI/docs/KNOWN_LIMITATIONS.md`, `/home/user/QuotePilot-AI/apps/api/app/services/matching.py`, `/home/user/QuotePilot-AI/apps/api/app/services/ratelimit.py`, `/home/user/QuotePilot-AI/apps/api/app/api/routers/rfqs.py`, `/home/user/QuotePilot-AI/apps/api/tests/security/test_audit_followups8.py`, `/home/user/QuotePilot-AI/.gitleaks.toml`.
