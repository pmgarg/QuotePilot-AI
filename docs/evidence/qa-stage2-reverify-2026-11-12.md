# QA re-verification of Stage 2 after fix rounds 1 and 2 (2026-11-12)

Worktree `/home/user/qp-stage2`, branch `claude/stage2-followup-assistant`, HEAD `43ec762` (plus the one new untracked
test file below). Narrow checklist pass, not a new campaign. Verifier: independent QA. No product code, no existing test
and no commit was changed by this pass. Another untracked file, `apps/api/tests/security/test_stage2_confirm.py`, is not
mine (security re-verification) and was not run or touched.

## Environment
Python 3.13 venv (`apps/api/.venv`), PostgreSQL 16 on localhost:5432, database `quotepilot_qa2` only (app role
`quotepilot_app`, owner role `quotepilot_owner`; credentials redacted/not recorded). `APP_ENV=test`. Provider = built-in
simulator (no real mail). Browser: Playwright Chromium, API on 8101, web on 3101 (production build in the web-server
command), `--trace off`. The database was dropped and re-created empty before the browser run (fresh database).

## Runs
| # | Command (cwd) | Exit | Result |
|---|---|---|---|
| R1 | `.venv/bin/pytest -q -p no:cacheprovider tests/qa/test_qa_stage2*.py -rfEsx` (`apps/api`) | 0 | 55 passed, 0 failed, 0 skipped, 0 xfailed (85 s), before my new file existed |
| R2 | `.venv/bin/pytest -q -p no:cacheprovider tests/qa/test_qa_stage2_confirm.py -rfEs` (`apps/api`) | 0 | 4 passed (first attempt: 3 passed, 1 failed, a bug in my own test, it queried a table that does not exist; fixed in the test, no product impact) |
| R3 | same as R2, repeated 3 more times | 0 x3 | 4 passed each time (the barrier test is stable) |
| R4 | `.venv/bin/pytest -q -p no:cacheprovider tests/qa/test_qa_stage2_sending.py tests/qa/test_qa_stage2_worker.py -k "a03_concurrent or a02_opt_out or no_follow_ups or edited_draft or placeholders_in_an_edited or withdrawing" -v` | 0 | 6 passed (D-01, D-03, D-04, D-05, D-06, D-13 by id) |
| R5 | `.venv/bin/pytest -q -p no:cacheprovider tests/qa/test_qa_stage2_inbound_tenancy_roles.py -k unsubscribe_reply_opts_out_every -v` | 0 | 1 passed (D-02) |
| R6 | `.venv/bin/ruff check tests/qa/test_qa_stage2_confirm.py` and `ruff format --check` (`apps/api`) | 0 / 0 | clean |
| R7 | `npx playwright test specs/qa-stage2-followups-ui.spec.ts specs/stage2-followup.spec.ts --reporter=list --trace off` (`tests/e2e`; `DATABASE_URL`/`DATABASE_ADMIN_URL` = the qa2 URLs, ports 8101/3101) | 0 | 8 passed, 0 failed, 0 flaky, 0 skipped (32 s) on a freshly migrated and seeded database |

Not run, per the brief: the full API suite and the full browser suite (implementing side and CI record 1509 / 5 / 3 / 1
and 53 / 1; not independently reproduced here).

## Checklist
| # | Item | Verdict | Evidence |
|---|---|---|---|
| 1 | D-01..D-15 tests pass and were not weakened | CONFIRMED | R1: 55 passed, no skips or xfails. `git diff 3cacb04..HEAD` over `apps/api/tests/qa/test_qa_stage2*.py` and `tests/e2e/specs/qa-stage2*.spec.ts` is empty; `git log` shows only 3cacb04 touching them. Declared edits to `security/test_stage2_audit.py` (judged below) are acceptable. |
| 2a | D-01 holds on every route | CONFIRMED | `test_confirm_d01_opted_out_address_is_unreachable_by_every_route` and `..._case_variant_row_covers_the_original_row` pass (R2, R3). |
| 2b | D-03 exactly-once under a burst | CONFIRMED | `test_confirm_d03_burst_of_barrier_synchronised_sends_delivers_once`: 8 threads, `threading.Barrier`, real PostgreSQL; 1 simulator delivery, 1 `sent` event, 1 `draft.sent` audit row; passed 4 of 4 runs. |
| 3 | D-02, D-04, D-05, D-06 fixed | CONFIRMED | See below; all four existing tests pass (R4, R5) and each exercises the fix path. |
| 4 | D-13 as decided | CONFIRMED | `test_confirm_d13_withdrawn_consent_drafts_and_sends_nothing` (R2) plus QA's own D-13 test (R4). |
| 5 | Acceptance S2-A01..A08 | see table below | A01..A08 each have passing evidence; A05 and A06 depend on the simulator / fake provider only. |
| 6 | Browser specs on a fresh database | CONFIRMED | R7: 8 passed, 0 failed, 0 flaky. No re-run needed. |
| 7 | Customer directory layout change | CONFIRMED by code read only (no browser check run) | Diff `f777817..HEAD` on `apps/web/src/app/(app)/customers/page.tsx`: the table is wrapped in `div.overflow-x-auto` and the card has `min-w-0`; the Edit and Contacts buttons are in the last `<td>` of the same table, nothing is hidden or removed, and `ContactsPanel` stays outside the scroll wrapper. At 1024 px (the `lg` grid breakpoint) the table can scroll horizontally inside the card and Playwright scrolls the buttons into view. The three previously failing tests pass locally (R7: `qa-stage2-followups-ui:217`, `stage2-followup:23`, `:66`). |

### Item 1: judgement of the declared verifier edits (`apps/api/tests/security/test_stage2_audit.py`, diff 3cacb04..HEAD)
- Ten strict `xfail` markers removed for S2-SEC-01/02/03/04/05/07/09/10/11 findings; the tests now assert the fixed behaviour. Each test still runs
  and passes in the implementer's and CI's full runs (not re-run by me). Acceptable: they were strict, so they had to go once the defect was fixed.
- `test_automation_consent_is_rechecked_at_send_time`: the old line `no job succeeded` became `the schedule job succeeded by drafting; schedule is draft_only and not sent`. The security assertions (empty outbox, no `draft.sent` row, no sent draft) are untouched. Acceptable: a direct consequence of the D-13 decision QA asked for.
- `test_dead_lettered_schedules_...`: the line expecting a dead job after consent withdrawal now expects none; the 200-dead-job starvation part is unchanged. Acceptable for the same reason. One comment changed text only.
- `test_worker_scope_has_no_caller_outside_the_queue_module`: now requires the scoped forms (`org_id=org_id`) and forbids the unscoped ones. This is stronger than before, not weaker.
The edit to one integration test (`test_stage2_fixes1.py`, replaced by the draft-and-refuse test) is covered by my D-13 test (item 4).

### Item 3: High fixes
| Defect | One-line description | Test id (all passed) |
|---|---|---|
| D-02 | An unsubscribe reply opted out only one of the contact rows for that address. | `test_qa_s2_f08_unsubscribe_reply_opts_out_every_contact_row_with_that_address` |
| D-04 | A contact with `preferred_channel=none` still received automated follow-ups. | `test_qa_s2_a02_contact_preferring_no_follow_ups_is_never_auto_messaged` (asserts empty outbox after the worker runs, and that no schedule was sent) |
| D-05 | An edited draft was sent without the opt-out text. | `test_qa_s2_f04_edited_draft_is_sent_with_opt_out_text` (asserts the delivered body) |
| D-06 | Placeholders in an edited draft were delivered unrendered. | `test_qa_s2_f04_placeholders_in_an_edited_draft_are_rendered_before_delivery` (asserts the delivered body) |

### Item 2: what the new tests exercise (file `apps/api/tests/qa/test_qa_stage2_confirm.py`, 4 tests)
- D-01 test: opt out contact A (`buyer@customer.example`) after a draft for A already existed and after automation planned `auto_send` schedules. Then: a second contact with `BUYER@CUSTOMER.EXAMPLE`, one with blanks around the address, and an exact duplicate (creation may be accepted, but a draft for any of them must be refused with 400/403/409/422); a subscribed contact edited to the opted-out address in upper case (same rule); the pre-existing draft `POST .../send` returns 403, `PATCH to_email` to the opted-out address cannot make it sendable; every schedule forced due and the worker run twice (automation path): no outbox message to the address, no `draft.sent` row. The second test opts out via the case-variant row and checks the original row's address is also refused.
- D-03 test: 8 clients across 4 roles, barrier-released, statuses limited to 200/409.
- D-13 test: after withdrawal the due `auto_send` schedule becomes `drafted` with `mode=draft_only` and a `draft_id`, outbox empty, exactly one `followup.degraded_to_draft` audit row, the draft stays `draft`, the schedule job `succeeded`; then a `send_draft` job enqueued for that draft is `dead` after exactly one attempt, nothing is sent and no `draft.sent` row exists.

## Acceptance criteria (PRD Stage 2)
| Id | Verdict | Covering tests |
|---|---|---|
| S2-A01 no send without approval | PASS (API + browser) | `test_qa_s2_a01_nothing_is_sent_before_the_explicit_authorized_click`, `test_qa_s2_f05_every_stage2_mutation_requires_csrf`; browser `S2-F04/A01` and `stage2-followup.spec.ts:23` |
| S2-A02 opted-out never auto-messaged | PASS | D-01/D-02/D-04 tests, `..._manual_opt_out_cancels_pending_and_only_admin_can_opt_in`, my D-01 tests; browser `S2-A02` (`qa-stage2-followups-ui:217`, `stage2-followup:66`) |
| S2-A03 duplicate trigger, single send | PASS | `..._a03_concurrent_duplicate_sends_deliver_exactly_once`, `..._a03_worker_and_manual_trigger_on_the_same_schedule_send_once`, my barrier test |
| S2-A04 no cross-tenant contact access | PASS | `..._a04_every_stage2_resource_is_404_for_another_tenant`, `..._a04_rls_blocks_stage2_tables_without_the_service_filter`, `..._f08_quote_numbers_do_not_link_across_tenants` |
| S2-A05 invalid/expired token handled | PASS with a scope limit | `..._a05_sync_inbound_with_misconfigured_provider_fails_safely_and_is_recorded` (D-14), `..._a05_smtp_outage_is_retried_then_dead_lettered_without_false_sent`; no real OAuth provider exists, so "expired OAuth token" is exercised only as a misconfigured/failing provider |
| S2-A06 429/5xx/timeout retry then dead-letter | PASS | `..._a06_429_5xx_timeout_retry_with_capped_backoff_then_dead_letter`, `..._a06_permanent_provider_rejection_fails_fast_without_retry` (fake provider) |
| S2-A07 user sees and cancels pending follow-up | PASS (API + browser) | `..._a07_cancelled_or_rescheduled_followups_are_not_processed`; browser `S2-F10/A07`, `stage2-followup:66` |
| S2-A08 logs have no bodies/addresses | PASS | `..._a08_logs_on_success_failure_worker_and_inbound_paths_contain_no_addresses_or_bodies`, `..._f07_job_records_contain_ids_only_never_addresses_or_bodies` |

## Defects
None found in this pass (no failing test). No defect outside the fix rounds.

## Observations
- My D-01 test accepts creation of a duplicate contact row (201) and relies on the draft refusal; address-level suppression is therefore enforced at use, not at creation.
- `customers/page.tsx` still says "Contacts and preferences arrive with Stage 2" although they now exist (stale copy, cosmetic).
- `overflow-x-auto` also makes the wrapper clip vertically (computed `overflow-y: auto`); no visible effect seen in the passing specs.
- Playwright prints "`next start` does not work with `output: standalone`"; pages were served and the specs passed. Pre-existing config warning.

## Not checked
- Full API suite and full browser suite (relied on, not reproduced: 1509 passed / 5 skipped / 3 xfailed / 1 xpassed; 53 passed / 1 skipped).
- Customer table at 1024 px in a real browser (item 7 is a code read plus the three passing specs at Playwright's default 1280 px viewport).
- Real SMTP, real OAuth, or a hosted provider; the simulator and fake providers only.
- Security findings S2-SEC-01..11 (separate security re-verification); CI on this HEAD (read from the implementer's notes only).
- Stage 1 behaviour beyond the QA regression tests contained in the Stage 2 files (all 55 passed).
