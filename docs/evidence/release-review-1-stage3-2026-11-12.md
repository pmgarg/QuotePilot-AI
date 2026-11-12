# Release review 1, Stage 3 (customer and order intelligence), 2026-11-12

Release: Stage 3, branch `claude/stage3-customer-order-intelligence`, HEAD 9f63da8 (docs only on top of the gated code head e2e6c3f). First review of this stage. The reviewer is read-only and ran no tests. Everything below rests on recorded evidence and source reading.

## 1. Verdict

**PASS WITH CONDITIONS.** Stage 3 stays IMPLEMENTED, not VERIFIED. The spec coverage is real and independently exercised, and no Critical or High finding is open. The conditions that need a human, or a further verifier pass, are listed below. The Stage 1 and Stage 2 dependency alone prevents VERIFIED status.

Conditions:
- **C1, human owner:** make a named decision on the erasure scope that is not purged. That covers `rfq_items.raw_text`, uploaded RFQ files, audit summaries, order and quote notes, and backups. Either accept it in writing as a documented residual risk, or direct implementation. I do not accept risk.
- **C2, human owner:** ratify the two product decisions an agent took. A replayed idempotency key with a different payload returns 409. A customer pending erasure returns 404 to non-`manage_retention` roles.
- **C3, QA and security verifiers:** run a short targeted re-check of fix round 2 at the final head (see answer B). It needs a fresh full API and browser run and a scan recorded for that head.
- **C4, implementer:** correct the stale documents (S3-RR1-5 to S3-RR1-7).
- **C5, human owner:** the Stage 1 and Stage 2 conditions (release-review-5 and release-review-1 for those stages) must be closed. Stage 3 cannot be called VERIFIED until Stages 1 and 2 are.

## 2. Findings

No Critical and no High.

| ID | Sev | Finding | Evidence | Required action |
|---|---|---|---|---|
| S3-RR1-1 | Medium (privacy) | The purge leaves identifying data outside the customer row: `rfq_items.raw_text`, uploaded RFQ files, audit summaries (for example `customer.create` carries the name), order and quote notes and terms, backups. `purge()` nulls only `rfqs.pasted_text`. The purge is therefore not complete erasure. | `retention.py:281-302`; KNOWN_LIMITATIONS lines 200-208; PRIVACY_AND_RETENTION lines 36-40; security reverify section 3 and 8 (`rfq_items.raw_text` "NOT CHECKED") | C1. Owner decides. If implemented, add a purge test per field. If accepted, record it in RELEASE_GATES with a name and date. |
| S3-RR1-2 | Medium (process) | Fix round 2 is not re-verified by an independent agent. The verifier's reproduction tests are not neutral evidence once the xfail markers are removed by the fixing side. | RELEASE_GATES Stage 3 "Fix round 2" ("not been re-checked by a verifier agent") | C3. See answer B. |
| S3-RR1-3 | Medium (data) | No test covers purge redaction of connector `sync_runs` reports (`kind="connector"`) or the `external_ref` and `notes` of imported or connector orders. `_redact_copies` only walks `csv_import` runs. QA lists connector run reports under "Not checked". The code shows connector rejection reasons omit the reference, but nothing proves it for all paths. Orders keep their `customer_id` after erasure by design. | `retention.py:233`; `connectors.py:228-229, 433`; QA reverify "Not checked" | Add a test and extend or document. Fold into C1. |
| S3-RR1-4 | Low | CSV imports have the same concurrent-replay race that SEC3-16 fixed for connector syncs. `_start_run` flushes with no savepoint, so the loser of a same-key race gets an `IntegrityError` and a likely 500. Data is applied once. | `data_io.py:199-213`; connectors.py savepoint at lines 152-161 | Same handling as connectors, plus a race test. Backlog before wider release. |
| S3-RR1-5 | Low (docs) | Stale document: KNOWN_LIMITATIONS says a purge job left `running` is not reclaimed ("SEC3-14, test still xfail"). That is fixed (`queue.reclaim_abandoned` called from `enqueue_due_purges`, test passes). | KNOWN_LIMITATIONS 209-211 vs `retention.py:333` and the gate-log addendum | Correct the document. |
| S3-RR1-6 | Low (docs) | The traceability Stage 3 section still says re-verification and review "have not run yet". All reviewer_verdict cells say "pending", and the test_id cells cite only one implementer test per row. ROADMAP table row 3 says "NOT STARTED (designed)" while the paragraph below says implemented. The gate log's "Still required" items 1-4 are partly done, which is misleading. | REQUIREMENTS_TRACEABILITY 96-112; ROADMAP 10 and 35; RELEASE_GATES 503-510 | Rewrite the section from the verifier evidence (rows in section 3 below). Update ROADMAP. |
| S3-RR1-7 | Low (docs) | The LLD Stage 3 sketch contradicts the implemented design. It names `services/orders.py`, `services/connectors/{base,csv,erp_stub}.py` with `pull()` into `sync_staging`, and `strip_sensitive`. The code is under `services/intelligence/`, push-model, with no staging table. | LLD 160-165; KNOWN_LIMITATIONS 178-180 | Update the LLD. |
| S3-RR1-8 | Low | CI ran on the pull-request merge commit 9559b38, which is e2e6c3f merged into Stage 2 head 7d5cf4a. It did not run on e2e6c3f alone. The counts of pip-audit, npm audit, bandit and semgrep were "not read". The latest local scan evidence names commit 8258112, which is not the gated head. One earlier green head had a flaky accessibility spec (retried green, not investigated). | ci-stage3 file lines 8-12; scan-20261112-093157.md line 3; fixes1 file line 98 | C3: record a scan and counts at the final head. Investigate the flake. |
| S3-RR1-9 | Low | The race regressions (SEC3-16, SEC3-17, fixes2 confirm test) run a single round in the suite. The auditor's own probe used 8 rounds. A one-round race test can pass by luck, so it is weak as a regression guard. Bounded by the row lock (`orders.py:46-55`) and the savepoint. | `test_stage3_fixes2.py:81-106` | Loop the race 5 to 8 times in the test. |
| S3-RR1-10 | Low / observations | Only NUL is rejected among control characters, so bidi marks and bell are stored. The failed-row redaction matches by substring and can over-redact (safe direction). `purge()`, `decide()`, `resolve_conflict` and legal-hold set take no row lock (narrow race with a hold). There is no pagination on Stage 3 lists (200-row caps). There is no alerting on `purge_stalled`. The erased-customer 404 was not driven in the web UI. The real worker loop was driven in-process only. | security reverify section 8; KNOWN_LIMITATIONS 191, 209; QA O7, O13 | Backlog. Not blocking. |

## 3. Review template (one row per requirement group)

Abbreviations. Tests: QA = `apps/api/tests/qa/test_qa_stage3_*.py`; SEC = `tests/security/test_stage3_*.py`; INT = `tests/integration/test_stage3_*.py`; UI = `tests/e2e/specs/{stage3-intelligence,qa-stage3-ui}.spec.ts`. Evidence files: QA-RV = qa-stage3-reverify-2026-11-12; SEC-RV = security-stage3-reverify-2026-11-12; CI = ci-stage3-e2e6c3f-2026-11-12. Evidence is by runs recorded in those files; I verified code only by reading.

| requirement_id | design_reference | code_reference | test_id | test_result/evidence | security_check | reviewer_verdict | open_issue |
|---|---|---|---|---|---|---|---|
| S3-F01 profiles, role-aware cost | PRD 88 | `routers/intelligence.py::customer_profile` | QA orders/data_io cost tests; SEC costs-hidden test; UI 1-2 | QA-RV: S3-A02 PASS; API full run 1646 passed + D6 failure (D6 fixed afterwards); CI 1682 passed | cost and margin absent without `view_costs` in API and exports (SEC-RV item 8) | PASS (implemented, not VERIFIED) | none beyond C5 |
| S3-F02 quote to order with approval | PRD 89 | `orders.py:86-173, 238-272` (current approved version only, totals copied, row lock) | INT `test_quote_to_order_requires_separate_confirmation`, QA D1 tests (AED/JPY/KWD/BHD), SEC-12/17 tests | D1 confirmed fixed (QA-RV); SEC-12/17 reproductions pass after fix 2, not re-run by a verifier | proposer cannot confirm own order; 403/409 matrix; superseded version 409 | PASS WITH CONDITIONS | S3-RR1-2 (fix round 2 not verifier-checked) |
| S3-F03 / S3-A08 repeat-order assistant | PRD 90 | `repeat_orders.py` | QA repeat_retention; INT audit-trail test; UI 2 | QA-RV A08 PASS | proposals only, own history, approval, audit trail complete | PASS | none |
| S3-F04 / S3-A01 / S3-A03 KPIs, FX | PRD 91 | `kpis.py:31-177`, `fx.py` | QA kpis (fixture, boundaries, FX latest-on-or-before, inverse); INT kpis | QA-RV A01 and A03 PASS; D2 confirmed fixed | definitions in `DEFINITIONS`; unconverted bucket with no guessed rate (`kpis.py:174`); FX bounds 1e-8..1e6 | PASS | none; the written definitions are mine to read only, QA hand-computed the fixture |
| S3-F05 / S3-A06 / S3-A07 import/export | PRD 92 | `data_io.py`, `inputs.py`, `routers/intelligence.py:296-328` | QA data_io (403 plus audit, partial recovery, oversized); SEC audit_inputs (67 tests, 21 hostile params) | QA-RV and SEC-RV PASS; D3 and SEC3-04 closed | formula neutralisation tested; `export.denied` audit row (`:301-311`); per-row savepoint | PASS WITH CONDITIONS | S3-RR1-4 (CSV replay race), S3-RR1-10 |
| S3-F06 / S3-A04 connectors, conflicts | PRD 93 | `connectors.py` (conflict queue `:196-217`, products and customers never overwritten except by explicit resolution) | QA connectors_stock; INT sync-conflicts test; UI S3-F06; SEC-16 test | QA-RV A04 PASS; SEC3-06/07 closed; SEC3-16 passes after fix 2 | scope to 403; idempotency per (kind, connector, entity) plus request hash; push-model stubs only | PASS WITH CONDITIONS | no real adapter (limitation); S3-RR1-2 (savepoint replay not verifier-checked) |
| S3-F07 / S3-A05 stock labels | PRD 94 | `stock.py:33-45`; `connectors.py:292-339` | QA `test_stock_labels_follow_source_timestamps_not_sync_order`; UI S3-F07 | QA-RV A05 PASS | label from `observed_at` only; naive and future timestamps rejected; none means "unknown" | PASS | stock snapshot growth unbounded (limitation) |
| S3-F08 retention workflow | PRD 95 | `retention.py` (request, other-person approval, soft delete, purge, legal hold, re-queue cap, reclaim) | QA repeat_retention; SEC confirm 12-17; INT fixes2; UI S3-F08 | SEC-RV SEC3-01/02/03 MITIGATED; fix-2 tests pass; D4 and D6 closed | requester not approver; hold blocks; every step audited; `get_active_customer` 404 | PASS WITH CONDITIONS | S3-RR1-1, S3-RR1-3 (scope), S3-RR1-2 |
| S3-A02 cost/margin hidden | PRD 96 | see S3-F01 | see S3-F01 | PASS | see S3-F01 | PASS | none |
| S3-A09 cross-tenant regression | PRD 96, X-01 | migration `0007` (10 tables ENABLE + FORCE RLS + `tenant_isolation`, `:227-231`); `0007b` | QA tenancy (18 id routes to 404, raw RLS on all Stage 3 tables); SEC route matrix (19 routes plus enable/disable) | QA-RV and SEC-RV PASS | non-owner role; WITH CHECK insert refused; `set_worker_scope` only in `queue.py` | PASS (tested; not a penetration test) | none |

## 4. Answers A to G

**A. Process integrity.** I checked the current files against the declared edits. The QA edits are in the code as declared:
- `test_qa_stage3_connectors_stock.py:58-65` now asserts 409 for a different payload. It still asserts nothing from the second payload is applied (stock stays 10, one audit row), and it adds an identical-replay check.
- The retention tests were moved to admin reads plus a 404 for sales.
- The wording check for `reference is required` is now coupled to message text.
- Comments say "Orchestrator edit" where the implementer file says implementer edit. The attribution is inconsistent, though the edit is declared.
- The xfail removals leave no `xfail` in the Stage 3 test files except the stale docstrings.

I accept QA-RV's judgement that these edits are not weakenings. SEC-RV's judgement that the security-side edits are legitimate (decorators only) is credible, but it covers only fix round 1. The round-2 reordering of `test_every_stage3_mutation_writes_an_audit_row...` and the decorator removals in `test_stage3_confirm.py` were judged by no verifier. They are declared, with the assertions reportedly unchanged, but that rests on the implementing side's statement.

Decisions made by an agent, not a human:
- 409 for a replayed key with a different payload.
- 404 (not 403) for sales on a customer pending erasure.
- The re-queue cap of 10 for purge jobs.
- Keeping name and notes truncation on customer import.
- That SEC3-14, 15, 17 and 13 are not blocking.
- That all QA-edit judgements are acceptable.

I list these for human ratification (C2). The 404 decision has a UI consequence that nobody tested (QA O13).

**B. Fix round 2 without a verifier re-check.** This is not sufficient for a clean PASS, but it is enough for PASS WITH CONDITIONS because:
- The six changes are small and local. I read `orders.py:106-108`, the row lock at `orders.py:46-55`, the savepoint at `connectors.py:152-161`, `_redact_copies` and `reclaim_abandoned`. They are plausible as described.
- The tests were written before the fixes by independent verifiers, as strict xfails, and proven failing for the stated reason (`--runxfail`, 7 failed). They now pass in 151-test and CI runs.
- CI on the gated head is green.

What is missing:
1. A verifier must judge that the decorator removals changed no assertion, and must re-run the reproductions.
2. Multi-round race runs for SEC3-16 and SEC3-17. The auditor used 8 rounds, the suite uses one.
3. Negative tests that the fixes did not widen access. Quote detail is not tested for the erased name. Rejecting an open proposal of an erased customer is tested once (fixes2). The 404 by role for the new order refusal is covered only by the sec3_12 tests.
4. A check of side effects. `reclaim_abandoned` with `count_dead=False` calls `_fail_schedule` for dead purge jobs. I believe that is harmless but it was not examined. A purge job legitimately running past the visibility timeout is rerun and then refused by the `approved` status check, but no test shows that.
5. Redaction coverage beyond the named fields (S3-RR1-3).

**C. Sufficiency of the round-1 re-verification.** Adequate for what it covers, and better than Stage 2.
- QA re-ran all QA tests and added a new independent file.
- QA made one full API run (1646 passed, 1 failed) and one full browser run (62 passed, 1 skipped, 0 flaky).
- QA ran a written 12-item exploratory session.
- Security checked 9 areas and ran 12 probes. That found real defects (SEC3-12 to 17 and D6), which supports that it was not a rubber stamp.

Gaps:
- Mostly API-level. UI behaviour of the erased customer's 404 was not tested.
- Skip counts differ between QA (2) and CI (5), and QA's explanation is only arithmetic.
- One env slip, disclosed. The abort happened before any test body ran.
- The scan was not run by the security verifier.
- KPI performance, 5 MB CSV load and the real worker loop are untested.

It is a checklist-plus-probes pass, not an adversarial campaign. That does not block, but it limits the confidence in "closed" to the tested set.

**D. Open items.**
- *Blocks a clean PASS or needs a named human decision:* the purge scope (S3-RR1-1). S3-F08 says "retention/deletion workflows for customer data ... with audit", and a customer cannot rely on the purge as full erasure. It is honestly documented in three places (KNOWN_LIMITATIONS, PRIVACY_AND_RETENTION, the gate log), which is why this is a condition and not a FAIL. The statement that the customer name is pseudonymised and contacts and drafts are gone is supported by tests.
- Needs a named human: the C2 product decisions, and the C5 dependency on the Stage 1 and 2 conditions.
- Low, fix soon: the CSV replay race (S3-RR1-4), the one-round race tests (S3-RR1-9), the missing connector-report redaction test (S3-RR1-3).
- Backlog: the missing pagination, push-model-only connectors, control characters, row locks on purge/decide/resolve, alerting on `purge_stalled`, the worker-crash redelivery window (Stage 2 queue, documented), snapshot compaction.
- Not tested, said honestly: the erased-customer UI path and the worker runner loop. Neither blocks, but both should be listed under unverified claims.

**E. CI evidence.** CI says all 15 checks succeeded on both events (pytest `1682 passed, 5 skipped, 4 xfailed, 1 xpassed`; Playwright `62 passed`, `1 skipped`, no flaky line; CodeQL success). That is good evidence for the merged tree.
- It is the merge commit (9559b38), not e2e6c3f itself.
- Only one of two api logs and one of two e2e logs were read for counts. The scan counts were not read.
- The 4 xfails are Stage 1 and 2 items, none in Stage 3. The 1 xpassed is a Stage 1 non-strict timing test.
- Earlier red runs: an inherited Stage 1 timing test, browser click failures on the customers page, `py/polynomial-redos` alerts, and a harness artifact (web build replaced under a running server). The causes are stated, with the fixes merged in from the Stage 1 and 2 branches.
- The flaky accessibility spec (`qa-reverify-review1.spec.ts:230`) was not investigated and did not reproduce in QA's run or in the final CI. I note it, and it does not block.

**F. Traceability and honesty.**
- The traceability section is honest but out of date. It correctly marks everything IMPLEMENTED, none VERIFIED, with verdict "pending". It says re-verification has not run. Its test_id cells cite single implementer tests. It does not map the verifier evidence, so it is inadequate as the record (S3-RR1-6).
- No claim of VERIFIED appears in the Stage 3 documents, and I found no "100% secure"-type claim.
- Run records give command, exit code and counts in the implementer, QA, security and CI files. One implementer entry (the first merged-tree API run, "1 failed" with the test unnamed) is incomplete, but it is superseded.
- Stale statements: KNOWN_LIMITATIONS SEC3-14 (S3-RR1-5), the LLD sketch (S3-RR1-7), the ROADMAP table (S3-RR1-6), and the RELEASE_GATES "Still required" list, which says QA and security re-verification and CI are still needed although they exist. API_CONTRACT lines 144-158 and PRIVACY_AND_RETENTION match the implemented behaviour for the round-2 changes. PRD S3 rows are requirement text only.

**G. Specification coverage.**
- *S3-F01 to F08:* implementation, tests and verifier evidence exist for each. F08 and F06 are conditional as above.
- *S3-A01 to A09:* each has an independent QA test and a PASS in QA-RV. A02, A04, A06, A09 also have security tests.
- *KPI definitions and currency rule:* written definitions in `kpis.DEFINITIONS`, date range validated and capped at three years, reporting-currency conversion by tenant FX table with the latest rate on or before the quote date, and an "unconverted" bucket. Nothing is guessed. I read the code and QA hand-computed a fixture. The pending-value rule uses today's rate and ignores the date range, and the definition says so.
- *No invented stock:* the label comes only from a timestamped snapshot (`stock.py`), and the `unknown` state exists. There is no catalog-derived quantity.
- *Conflicts never overwrite master data:* the connector paths write conflicts only. The only overwrites are explicit audited `resolve_conflict(apply_remote)` by a permitted role, with before and after in the audit summary, and a new SKU can only be staged, not applied.
- *Role-gated, audited export:* the `export_data` gate with the `export.denied` audit row is at `routers/intelligence.py:296-328`. Cost columns appear only for `view_costs`, and formula neutralisation is tested.
- *Deletion workflow with audit:* request, approval by a different person, soft delete, purge with re-queue and reclaim, legal hold. Each step is audited, and the scope limit is S3-RR1-1.
- *Cross-stage dependencies:* the D5 (commit before response) fix lives in Stage 1 code and arrives by merge. The dependency on Stages 1 and 2 is C5.

## 5. What I did not review
- I ran nothing: not the tests, scans, browser or CI. I did not independently reproduce any recorded result.
- I did not read `fx.py`, the schemas, `repeat_orders.py`, the customers/RFQ/follow-up routers, the web pages, or `quotes.py::repriced_catalog_lines` and `list_quotes`. I checked their behaviour only through the evidence tables.
- I read only part of the migration (RLS lines of `0007`) and none of `0007b`.
- I did not read the original QA and security 2026-11-02 reports beyond headings and a table fragment, and I did not read the LLM or provider paths (they were out of Stage 3 scope).
- I did not review Stages 1 and 2 beyond noting their open status, and I did not review the browser bundle for secrets or the logs for Stage 3 data.
- This is not penetration testing or a security certification. Absence of findings here does not make the stage "secure".

## 6. Decision and required retest

**Decision:** release recommended only as PASS WITH CONDITIONS, with the status staying IMPLEMENTED. It is BLOCKED from "VERIFIED" until C1 to C5 are satisfied. Reason: the spec coverage is evidenced and no Critical or High is open, but the erasure scope needs a human decision, fix round 2 has had no verifier re-check, and Stages 1 and 2 are not VERIFIED.

**Required retest (exact requests):**
1. QA reverify of fix round 2 at the final head. Re-run the 151-test Stage 3 set, plus an order from a quote of a customer pending erasure, a name search and a list for each role, a multi-round concurrent confirm, and the UI path for a sales user opening an erased customer's quote. Make one full API and browser run on that head. Judge the declared decorator removals and the reordered audit test.
2. Security reverify of SEC3-12 to 17 with 8-round race runs (SEC3-16 and SEC3-17). Add tests for purge redaction of connector run reports, order `external_ref` and notes, and the CSV same-key race. Run `make security-scan` at the final head and record its counts.
3. Code review of the doc corrections (S3-RR1-5 to S3-RR1-7) and the traceability rewrite.
4. Re-invoke this reviewer after the human decisions C1 and C2.
