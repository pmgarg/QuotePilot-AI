# Release review 1 — Stage 2 (sales follow-up assistant), 2026-11-12

Release: branch `claude/stage2-followup-assistant`, worktree `/home/user/qp-stage2`, HEAD 632b56a (docs-only on top of 0d610fd, the gated head). I could not run git or tests. Everything below is from reading files. Whether the evidence files describe real runs is outside what I can verify.

## 1. Verdict

**PASS WITH CONDITIONS.** Stage 2 is not VERIFIED and must not be called done.

I found no open Critical or High defect. The send gate, address-level suppression, the exactly-once send, tenant scoping of the worker and RLS on all nine Stage 2 tables are implemented as claimed in the code I read. Recorded evidence backs them: the first full QA and security pass, the fix rounds, two narrow re-verifications, and CI on 0d610fd. Closing the gate depends on human decisions and on Stage 1, which no agent can supply. Some evidence is thinner than the claims, and the traceability matrix is stale.

Conditions:
- **C1 (human owner):** Stage 1's two open conditions (sign-off of documented residuals; human decisions) must be closed. Stage 2 is stacked on Stage 1 and `docs/ROADMAP.md` says it depends on Stage 1 being VERIFIED. Stage 2 cannot be VERIFIED before Stage 1.
- **C2 (named human owner):** A signed, recorded decision on each of these.
  - D-13: a withdrawn consent turns a due automated follow-up into a draft. The orchestrator decided this, not a human.
  - O-1: plus-tag and dot variants of an opted-out address are not suppressed.
  - S2-SEC-06: the per-tenant provider secret is stored but unused, so all tenants send through one process-wide SMTP account. The word "accepted" in the evidence is an agent's acceptance, not a human risk acceptance.
  - S2-SEC-12 (Low): partial log redaction of RFC-invalid addresses.
  - The scope gap on S2-F06/S2-A05: Gmail and Graph are not shipped, and "job paused, admin notified" is not demonstrated.
  - The loss of an opt-out when an unsubscribe comes from an address with no contact row (S2-RR1-3).
- **C3 (orchestrator, then reviewer):** Replace the one-row Stage 2 section of `docs/REQUIREMENTS_TRACEABILITY.md` with a per-requirement-group matrix that carries run evidence. Set statuses honestly. Fix the stale statements listed in S2-RR1-5.
- **C4 (QA, then reviewer):** Close S2-RR1-2 and S2-RR1-4 with tests or an explicit documented limitation. Reduce the narrow-verification gap (answer B): one independent full API and browser run on the final gated head, plus one hostile exploratory session on the send and consent paths.
- **C5 (orchestrator):** Record the CI result for the final head, with job-level counts, as a file under `docs/evidence/`, not only a paragraph in the gate log (S2-RR1-8).

## 2. Findings

| ID | Sev | Finding | Evidence | Required action |
|---|---|---|---|---|
| S2-RR1-1 | Medium | D-13 (consent withdrawn after planning gives a draft, not a refusal) was decided by the orchestrator and two security-audit assertions were edited to fit. The property "nothing leaves without consent" is still asserted and holds. The semantics are still an agent decision. | `implementer-fixes2-stage2-2026-11-12.md` row "QA D-13"; `service.py:836-856`; `test_stage2_audit.py:586-595,697-700` | The human owner ratifies or reverses it (C2). |
| S2-RR1-2 | Medium | Test gap created by the D-13 change. No test produces a terminally failed schedule job, so `_fail_schedule` (`queue.py:221-232`) and the schedule re-arm in `retry_job` (`followup.py:485-497`) are untested. A grep for `_fail_schedule`, `policy_refusal` and a schedule "failed" status in `apps/api/tests` finds only a comment. The S2-SEC-03 test now forces dead jobs by raw SQL. Its assertion that the attempt-1 schedule is not `pending` is true trivially, because that schedule is now `drafted`. The round-1 tests named in the fixes file (`test_policy_refusal_is_not_retried..`) no longer exist under that name. The re-verifier closed S2-SEC-03 without noticing this. | `test_stage2_audit.py:686-730`; grep results; `implementer-fixes1` row D-13 | Add a test where a schedule job fails terminally: the schedule becomes `failed` with a reason, an admin retry re-arms it, and it is processed once. |
| S2-RR1-3 | Medium (compliance, policy) | An unsubscribe from an address with no contact row is stored as an unlinked event only. A contact created later for that address is not opted out. Documented in KNOWN_LIMITATIONS but not tested. For a consent control this is a real hole. | `service.py:893-931` (opt-out only if a contact row exists); KNOWN_LIMITATIONS "Fix round 1" bullet 1 | A human decides whether to keep it. Preferably store a hashed per-tenant suppression entry, with a test. |
| S2-RR1-4 | Medium | No recovery for a job left `running` after a worker crash. `claim_next` sets `status='running'` and `locked_at`. A grep over `app/` shows nothing that reads `locked_at` or reclaims. `retry_job` accepts only `dead` and `failed`. A crash leaves the job invisible to retry and the schedule pending with a job, so it is never re-scanned. This is a liveness gap against "provider outages" and dead-letter views. It is not a duplicate-send risk. No test covers it. | `queue.py:147-174`; `followup.py:477`; `test_qa_stage2_worker.py:113` (only asserts a running claim) | Add stale-lock reclaim with a visibility timeout and a test, or document it as a limitation. |
| S2-RR1-5 | Low | Stale or contradictory documents. The traceability Stage 2 section says re-verification and the release review "have not run yet" and cites "test_stage2_followup.py (12)". `RELEASE_GATES.md` heading says "OPEN, not reviewed". PRD S2-F07 and LLD line 155 still say "Redis-backed / Redis list queue" while the code is a PostgreSQL queue (ADR-012 amendment). PRD S2-F06 (priority C) lists Gmail and Graph, which are not shipped. `KNOWN_LIMITATIONS` does disclose that. | `REQUIREMENTS_TRACEABILITY.md:66-73`; `RELEASE_GATES.md:345`; `PRD.md:78`; `LLD.md:155` | Correct them to the implemented behaviour (C3). |
| S2-RR1-6 | Medium | S2-A05 ("invalid/expired OAuth token: job paused, admin notified, no crash") is called PASS in the re-verification. The first QA called it PARTIAL because no OAuth provider exists. "Job paused" and "admin notified" are not demonstrated. Only a misconfigured SMTP provider is stored as a connection error (D-14), and an outage ends in dead-lettering. | `qa-stage2-reverify-2026-11-12.md` S2-A05 row; `qa-stage2-2026-11-02.md:68`; KNOWN_LIMITATIONS | Mark S2-A05 and S2-F06 PARTIAL in the matrix. The human decides whether the deferral is acceptable (C2). |
| S2-RR1-7 | Medium (honest status) | Every tenant sends through one shared SMTP identity (`provider_for`, `service.py:224-230`). The strict xfail S2-SEC-06 remains. No real provider has ever been exercised, so no SMTP transport behaviour (TLS, auth, bounces) is verified end to end. | `service.py:215-231`; `test_stage2_audit.py:859`; KNOWN_LIMITATIONS | Any real-tenant use is blocked until a human accepts this or per-tenant credentials ship. No paid service may be called without human approval. |
| S2-RR1-8 | Low | CI evidence exists only as paragraphs in `RELEASE_GATES.md` and `implementer-fixes2`: run ids 43295840234 and 43295851515, counts `1521 passed, 5 skipped, 4 xfailed, 1 xpassed` and Playwright `53 passed, 1 skipped`. There is no stored log, job table or CodeQL artefact in `docs/evidence/`. The "1 xpassed" is not explained for Stage 2. I did not query GitHub. HEAD 632b56a has no recorded run. | `RELEASE_GATES.md:393-399` | Store the job-level table and the xpassed test id. Record a run for the final head. |
| S2-RR1-9 | Low | Some QA confirmation tests are permissive. The D-01 route test accepts any of 400/403/409/422 and `audit(...) == [] or all(...)`. One path calls `pytest.skip`. The tests still check the outbox. | `test_qa_stage2_confirm.py:47-77,88` | Tighten to the expected status and remove the conditional skip. |
| S2-RR1-10 | Low | O-4: the `thread` link path (`in_reply_to` only) has no sender check (`service.py:885-892`). Separately, any inbound event on a quote suppresses all automated follow-ups for it (`service.py:782`). Inbound exists only as a simulator today, so a forged reply could silence follow-ups once a real adapter ships. | `service.py:782,885-892` | Backlog. It becomes a blocker for any real inbound adapter. |
| S2-RR1-11 | Low (process) | The orchestrator wrote fix round 2, decided the disputed defect, edited auditor tests, merged Stage 1 and chose the narrow verification. This weakens the independence of the loop. See A and B. | `implementer-fixes2` | Human visibility. Never let the same agent decide and certify. |

Observations (non-blocking):
- Duplicate contact rows are accepted and rely on use-time suppression.
- `{Foo}`-style braces are sent literally.
- A dead `send_draft` job leaves its draft un-failed.
- The customers page has stale copy.
- A scan cap of 5000 bounds the starvation fix.
- Two local browser failures on 2026-11-12 were explained only by a hypothesis (the D5 commit-before-response fix) and then passed in later runs.

## 3. Mandatory review template (one row per requirement group)

Evidence file abbreviations:
- QA1 = `qa-stage2-2026-11-02.md`
- QA2 = `qa-stage2-reverify-2026-11-12.md`
- SEC1 = `security-stage2-2026-11-02.md`
- SEC2 = `security-stage2-reverify-2026-11-12.md`
- FIX1 and FIX2 = `implementer-fixes1` and `implementer-fixes2`
- CI = `RELEASE_GATES.md:393-399`

| requirement_id | design_reference | code_reference | test_id | test_result/evidence | security_check | reviewer_verdict | open_issue |
|---|---|---|---|---|---|---|---|
| S2-F01 lifecycle, manual transitions audited | PRD S2-F01; API_CONTRACT | `services/lifecycle.py`; `followup.py:247-290` | QA `test_qa_stage2_lifecycle.py` (15) | QA1 PASS; QA2 ran the 55 QA Stage 2 tests: 55 passed; CI green | 404 on cross-tenant, readonly 403, stale lock 409 (QA1) | PASS (pending C1) | none new |
| S2-F02 policy, per-contact and per-address opt-out | PRD S2-F02 | `service.py:102-181`; `followup.py:137-207`; migration 0006b index | QA D-01/D-04, SEC `test_suppression_follows_the_address..`, `test_plus_tag..pinned` | QA2 R1-R5 passed; D-01 was Critical, now 4 independent tests | address-level lower() and strip() on every route; opt-in admin-only | PASS WITH CONDITIONS | O-1 (human, C2); S2-RR1-3 |
| S2-F03 rule-based recommendation | PRD S2-F03 | `followup/recommend.py` | QA lifecycle and sending tests | QA1 PASS; no LLM in the path | no model call in the send path (SEC2) | PASS | none |
| S2-F04 drafts, opt-out text, templates | PRD S2-F04 | `templates.py`; `service.py:268-402,527` | QA D-05/D-06; SEC `opt_out_instruction_survives_hostile_edits` | fixed by FIX1; QA2 R4 passed | whitelisted placeholders; instruction re-appended at send | PASS | O-3, `{Foo}` observation |
| S2-F05 explicit send or consented automation, rate limit, recipient validation, audit | PRD S2-F05 | `service.py:450-631`; `followup.py:359-366,175-206` | QA A01/A03; D-03 barrier test; D-13 test | 8 barrier-synchronised sends gave 1 delivery (QA2); CI green | server-side role gate (EDIT_QUOTES / MANAGE_ORG); FOR UPDATE on draft and policy; consent rechecked at send time; CSRF on 16 endpoints (QA1) | PASS WITH CONDITIONS | S2-RR1-1 (human decision) |
| S2-F06 providers (simulator, SMTP, Gmail, Graph) | PRD S2-F06; master prompt (OAuth "when configured") | `comms/providers.py`; `secrets.py`; `service.py:215-231` | QA worker tests; `smtp_outage..`; secrets tests; S2-SEC-06 xfail | simulator and SMTP against a closed port only; Gmail and Graph return 422 "not shipped" | AES-GCM per-tenant secret, never echoed; shared SMTP identity | INCOMPLETE (partial) | S2-RR1-5, S2-RR1-6, S2-RR1-7 |
| S2-F07 job runner, idempotency, backoff, dead letters, no duplicate sends | PRD S2-F07; ADR-012 amended (PostgreSQL queue) | `jobs/queue.py`; `followup.py:461-509,637-662` | QA worker (10); SEC01/SEC03 tests; `test_stage2_fixes1.py` | claims are SKIP LOCKED; backoff 30 s doubling, capped at 1 h; dead letters after 5; run-once limited to the caller's tenant, 25 jobs, dev/test only (SEC2) | worker scope only in `queue.py`; RLS on `job_runs` | PASS WITH CONDITIONS | S2-RR1-2, S2-RR1-4 |
| S2-F08 inbound sync and linking | PRD S2-F08 | `service.py:862-1030` | QA inbound (11); SEC05 | simulator only; fixed D-02/D-11/D-12; CI green | cross-tenant number linking refused; quote-number link needs a sender check | PASS WITH CONDITIONS | O-4 (S2-RR1-10) |
| S2-F09 timeline of recorded events only | PRD S2-F09 | `service.py:1031` | QA `timeline_shows_only_recorded_events` | PASS (QA1, browser) | readonly cannot act | PASS | none |
| S2-F10 pending follow-ups, cancel and reschedule | PRD S2-F10 | `followup.py:391-448`; `follow-ups/page.tsx` | QA `cancelled_or_rescheduled..`; browser S2-F10/A07 | API and browser pass (QA2 R7: 8 browser passed on a fresh DB) | 404 cross-tenant, role gates | PASS | none |
| S2-F11 log redaction | PRD S2-F11 | `core/logging.py:15-30` | QA A08 tests; SEC `bounded_address_patterns_are_linear` | pass; a linear timing check on a 200,000-character input (`test_stage2_confirm.py:136-143`) | bounded regex; CodeQL ReDoS alerts fixed | PASS WITH CONDITIONS | S2-SEC-12 (Low, strict xfail) |
| S2-A01 no send without approval | PRD S2 acceptance | `followup.py:359`; `service.py:488-489` | QA A01; browser | PASS (QA2) | CSRF and role | PASS | none |
| S2-A02 opted-out never auto-messaged | PRD S2 acceptance | `service.py:115-131,500,795` | QA D-01/D-02/D-04 and confirm tests | PASS (QA2) | every route covered, including automation | PASS WITH CONDITIONS | O-1, S2-RR1-3 |
| S2-A03 duplicate trigger gives one send | PRD S2 acceptance | `service.py:463-483` | QA A03 (2 tests) and the barrier test | PASS (QA2, 4 runs) | row locks | PASS | none |
| S2-A04 no cross-tenant contact access | PRD S2 acceptance | org filters; migration 0006 (FORCE RLS, 9 tables) | QA A04 (17 endpoints; raw RLS on 9 tables) | PASS | non-owner role; INSERT claiming another org refused | PASS | RLS for the Stage 2 tables was last tested in SEC1, not re-audited (SEC2 "Not checked") |
| S2-A05 invalid or expired OAuth token handled | PRD S2 acceptance | `service.py` sync/provider errors | QA D-14 test; SMTP outage test | proxy tests only; no OAuth provider | n/a | INCOMPLETE | S2-RR1-6 |
| S2-A06 429/5xx/timeout retry, then dead letter | PRD S2 acceptance | `queue.py:28-30,177-219` | QA A06 (2 tests) | PASS against fake providers only | no false "sent" | PASS (simulated) | S2-RR1-7 |
| S2-A07 user sees and cancels a pending follow-up | PRD S2 acceptance | `followup.py:391-435` | QA A07; browser | PASS | none | PASS | none |
| S2-A08 logs hold no bodies or addresses | PRD S2 acceptance | `logging.py`; `scrub_error` | QA A08 (2 tests) | PASS; partial for over-long addresses | redaction is pattern-based | PASS WITH CONDITIONS | S2-SEC-12 |

## 4. Answers A–F

**A. Process integrity.** I read the current `tests/security/test_stage2_audit.py`. The ten strict-xfail markers are gone. SEC2 states the test bodies are byte-identical apart from the markers. I could not check that against git history. The two D-13-driven edits are legible, and the file has inline "edited by the implementer" comments. In `test_automation_consent_is_rechecked_at_send_time` the security assertions (empty outbox, no `draft.sent` row, no sent draft) are untouched, so the intent is kept. The weakening is in `test_dead_lettered_schedules_..`: its first half no longer produces a dead job and its attempt-1 assertion is now trivially true (S2-RR1-2). The inverted static scope test (`test_worker_scope_has_no_caller..`) is stronger than before. I have no evidence that QA's files were edited. Both `qa-stage2` grep results show no xfail and only one conditional skip. The remaining concerns are that the orchestrator made the D-13 decision, edited the tests and merged Stage 1 in the same stretch, and that no human decided D-13 (S2-RR1-1, S2-RR1-11).

**B. Narrow re-verification.** It is not enough on its own for a stage whose first QA found 2 Critical and 4 High defects. Both re-verifiers disclaim the full suites. QA2: "Not run, per the brief: the full API suite and the full browser suite". SEC2 did not run the cross-tenant matrix, parser limits, file handling, scans or the migration. The fixes were checked against fixed checklists only, which cannot discover what the checklists omit. That is exactly how the D-13 test hole (S2-RR1-2) and the stale-lock gap (S2-RR1-4) were missed. The evidence for the gated head rests on implementer-run suites and CI. CI is real, but I did not independently confirm it. Missing:
- an independent full API and browser run on the final head;
- a security adversarial pass over the send gate and consent paths under concurrency, including opt-out racing an in-flight send;
- an RLS and cross-tenant re-run for the Stage 2 tables, including the 0006b migration and the downgrade path;
- a scan evidence file for the gated head: `make security-scan` outputs, gitleaks, pip-audit, npm audit, bandit and semgrep.

**C. Open findings.**
- **Block a PASS (human decision needed):** D-13 (S2-RR1-1), the S2-SEC-06 acceptance (S2-RR1-7), S2-F06/S2-A05 scope (S2-RR1-6), and O-1 together with the no-contact-row opt-out gap (S2-RR1-3). A named human owner must decide each. None of them is a technical defect.
- **Needs a human decision but is not a blocker:** S2-SEC-12 (Low, acceptable if recorded). The limitation that no real provider was ever exercised: it blocks any real use, not the simulator-only release.
- **Backlog:** O-4 (becomes a blocker the moment a real inbound adapter ships), the observations, and O-2/O-3.
- **Must be fixed or declared before the gate closes:** S2-RR1-2 and S2-RR1-4.

**D. CI evidence.** The gate log records `ci` runs 43295840234 and 43295851515 (the second on the PR merge commit) for 0d610fd:
- api, web and e2e green on both events;
- pytest `1521 passed, 5 skipped, 4 xfailed, 1 xpassed in 1030.61s`;
- Playwright `53 passed`, `1 skipped`, no flaky or failed line;
- `security` workflow jobs (CodeQL python and javascript-typescript, gitleaks, pip-audit/npm audit/bandit/semgrep) green;
- the CodeQL code-scanning check green.

Earlier red runs, each with a recorded cause:
- e2e red: three tests timed out on the Contacts button because the directory table slid under a neighbouring panel on the runner. The table now scrolls inside its card.
- CodeQL `py/polynomial-redos` in log redaction and e-mail validation. Both patterns are now bounded.
- Stage 1 timing and portability failures inherited through the merge.

I did not query GitHub, so these figures are the repository's record, not something I confirmed. Job-level tables are not stored as artefacts (S2-RR1-8). The "1 xpassed" is unexplained for Stage 2. The final HEAD 632b56a has no recorded run.

**E. Traceability and honesty.** The Stage 2 section is a single summary row plus a paragraph. It is not adequate for a gate. It covers S2-F01..F11 and S2-A01..A08 together, lists the wrong test count and carries stale text (S2-RR1-5). Nothing in Stage 2 is marked VERIFIED, which is correct. Status is "IMPLEMENTED (not VERIFIED)". Where evidence claims a pass it usually records a command, exit code and counts (FIX2, QA2, SEC2). The exceptions are the CI figures (S2-RR1-8) and the "re-run later" browser hypothesis. I found no "100% secure" claim in the Stage 2 evidence. `KNOWN_LIMITATIONS` for Stage 2 is truthful and fairly complete: Gmail/Graph not shipped, simulator-only inbound, plain text only, single worker, shared SMTP, S2-SEC-12, O-1, O-4, the lost no-row unsubscribe and the duplicate window. It omits the stale-running-job gap (S2-RR1-4) and does not record that S2-A05 is only proxy-tested.

**F. Specification coverage.**
- **Implementation and test evidence exist:** S2-F01, F02, F03, F04, F05, F08 (simulator only), F09, F10, F11, and S2-A01 to A04, A06 to A08.
- **Partial:** S2-F06 (SMTP and simulator only), S2-F07 (the dead-letter and retry path is tested, but schedule failure and stuck running jobs are not), S2-A05.
- **Missing:** none outright. The master prompt says Gmail/Graph are "when configured" and the simulator is acceptable for demos, so the deferral is allowed by the spec. The PRD's priority-C S2-F06 text has not been updated to say so.

## 5. What I did not review
- The web UI source (`apps/web`) and the Playwright specs beyond what the evidence describes.
- The full SMTP provider code, `secrets.py`, the workers runner, templates and the lifecycle module in detail.
- The 0006 migration column definitions and downgrade path; I read only the RLS statements and grants.
- Secrets exposure in the web bundle, and the output of the security scans (they were not run in the re-verifications).
- Stage 1 behaviour, beyond its stated dependency on this stage.
- Whether git history matches the claims about edited and unedited test files. I could not run git.
- That CI actually passed. I did not query GitHub, and the log records I read are second-hand.
- No penetration testing or security certification is claimed or implied.

**Decision:** Release is not recommended as VERIFIED. A simulator-only demo is acceptable after C3, C5 and the human decisions in C2. Real-provider or real-tenant use is BLOCKED until S2-RR1-7 and C1 are resolved.

**Required retest:**
- QA: one independent full API and browser run on the final head. Add the schedule-failure and stale-lock tests (S2-RR1-2, S2-RR1-4). Tighten S2-RR1-9.
- Security: an RLS and cross-tenant re-run for the Stage 2 tables including 0006b, a concurrency race between opt-out and an in-flight send, and a scan on the gated head.
- Release reviewer: re-invoke after C1 to C5.
