# Implementer — Stage 2 fix round 2 (orchestrator, after fix round 1)

Base: `abf1c03` (fix round 1 by the implementer agent). Self-reported; independent QA and security re-verification
of rounds 1 and 2 follow.

| Item | Change | Test |
|---|---|---|
| Migration id collision | Fix round 1 added migration `0007`, which is also the Stage 3 migration id. Renumbered to `0006b` (file and revision); Stage 3's `0007` will follow `0006b`. | suite migrates from scratch |
| QA D-13 (disputed in round 1) | Decision: follow QA. A due schedule planned as `auto_send` whose consent was withdrawn **degrades to draft-only** — a draft is prepared, nothing is sent, the schedule becomes `drafted`/`draft_only`, `followup.degraded_to_draft` is audited. This matches the product default ("draft-only unless consented") and keeps the security property (nothing leaves without consent). A queued automated *send* without consent is still refused terminally. | QA `test_qa_s2_f05_withdrawing_consent_after_planning_degrades_to_draft_only` (now passes, unedited); new `test_consent_withdrawal_degrades_to_draft_and_a_queued_automated_send_is_refused_terminally` (replaces the round-1 test that asserted a `failed` schedule) |
| Merge | Stage 1 fix rounds 6 and 7 merged (editor lock during saves, stale-catalog re-link, decimal parser, compose check). | full suites below |

## Edits to the security auditor's tests (declared; to be judged by the re-verification)
`apps/api/tests/security/test_stage2_audit.py`:
- `test_automation_consent_is_rechecked_at_send_time`: the line `all(j["status"] != "succeeded" …)` is replaced by
  "the schedule job succeeded by drafting; the schedule is `draft_only` and not sent". The assertions that prove the
  security property (empty outbox, no `draft.sent` audit row, no sent draft) are unchanged.
- `test_dead_lettered_schedules_are_resolved_and_do_not_starve_other_tenants`: the line expecting a dead job after
  consent withdrawal now expects none; the starvation part (200 forced dead jobs, second tenant still served, first
  schedule no longer `pending`) is unchanged.
Round 1 had already changed `test_worker_scope_has_no_caller_outside_the_queue_module` and removed ten strict xfails
(listed in `implementer-fixes1-stage2-2026-11-10.md`).

## Runs (2026-11-12, tree = fix round 2 merged with Stage 1 fix round 7; DB `quotepilot_s2`, ports 8073/3073)
The machine was running three other suites at the same time (load average 8–12 on 4 cores).

| Command (cwd) | Exit | Result |
|---|---|---|
| `.venv/bin/ruff check app tests -q` (`apps/api`) | 0 | clean |
| `.venv/bin/ruff format --check app tests` | 0 | 108 files already formatted |
| `.venv/bin/mypy app` | 0 | no issues in 72 source files |
| `.venv/bin/pytest -q -p no:cacheprovider` (full API suite) | 0 | **528 passed, 1 skipped, 1 xfailed** in 1314 s (the xfail is S2-SEC-06, accepted and documented) |
| `npm run build` (`apps/web`) | 0 | compiled |
| `npx playwright test --reporter=list` (`tests/e2e`) | 1 | **41 passed, 2 failed, 1 skipped** |

The two browser failures (`qa-stage1-negative.spec.ts:111` margin-override visibility; `qa-stage2-followups-ui.spec.ts:217`
opt-out in the UI) are **not explained yet**. Both specs passed in earlier runs of the same code paths, and the Stage 3 QA
run has since shown that the API answered 2xx before its transaction was committed (defect D5, fixed in Stage 1 fix
round 8), which produces exactly this kind of stale-read failure. That is a hypothesis, not a result: the browser suite
has to be re-run after the round-8 merge, and a green run is required before the Stage 2 gate.

## Runs after merging Stage 1 fix round 8 (commit f09a842; same DB and ports; two other suites running)
| Command (cwd) | Exit | Result |
|---|---|---|
| `ruff check` / `ruff format --check` / `mypy app` (`apps/api`) | 0 / 0 / 0 | clean; 111 files formatted; 72 source files |
| `.venv/bin/pytest -q -p no:cacheprovider -rxs` (full API suite) | 0 | **730 passed, 2 skipped, 2 xfailed** in 1003 s (xfails: S2-SEC-06 accepted; the Stage 1 auditor's `test_sec4_4` whose own expression raises) |
| `npx tsc --noEmit`, `npx eslint .` (`apps/web`) | 0 / 0 | clean |
| `npx playwright test --reporter=list --trace off` (`tests/e2e`, web rebuilt) | 0 | **49 passed, 1 skipped**, 0 failed, 0 flaky |

The two browser failures of the previous run did not recur on the tree that contains the commit-before-response fix.
That is one green run under load, consistent with the D5 explanation but not proof of it.

## Runs after merging Stage 1 fix round 8b (commit 27b252f; three other suites running)
| Command (cwd) | Exit | Result |
|---|---|---|
| `ruff check` / `ruff format --check` / `mypy app` (`apps/api`) | 0 / 0 / 0 | clean; 111 files formatted; 72 source files |
| `.venv/bin/pytest -q -p no:cacheprovider` (full API suite) | 0 | **731 passed, 2 skipped, 2 xfailed** in 1142 s |
| `npx playwright test --reporter=list --trace off` (`tests/e2e`, web rebuilt) | 0 | **49 passed, 1 skipped**, 0 failed, 0 flaky |

## Runs after merging Stage 1 fix round 9 and the e2e harness fix (commit be754fd; two other suites running)
| Command (cwd) | Exit | Result |
|---|---|---|
| `ruff check` / `ruff format --check` / `mypy app` (`apps/api`) | 0 / 0 / 0 | clean; 115 files formatted; 72 source files |
| `.venv/bin/pytest -q -p no:cacheprovider -rfEx` (full API suite) | 0 | **1156 passed, 2 skipped, 1 xfailed** in 1093 s (xfail: S2-SEC-06, accepted) |
| `npx tsc --noEmit`, `npx eslint .` (`apps/web`) | 0 / 0 | clean |
| `npx playwright test --reporter=list --trace off` (`tests/e2e`; build now inside the web-server command) | 0 | **51 passed, 1 skipped**, 0 failed, 0 flaky |

Known CI issue inherited from Stage 1 at this merge: five tests in the Stage 1 QA file
`tests/qa/test_qa_reverify_review5.py` read `DATABASE_ADMIN_URL_TEST` directly and fail on GitHub Actions, where only
the conftest default is used (they pass locally, where the variable is exported). The QA verifier is correcting its
file on the Stage 1 branch; until that is merged the `api` job of this branch is expected to be red for that reason.

## Runs after merging Stage 1 fix round 10 (commit 8635153; other suites running)
| Command (cwd) | Exit | Result |
|---|---|---|
| `ruff check` / `ruff format --check` / `mypy app` (`apps/api`) | 0 / 0 / 0 | clean; 117 files formatted; 72 source files |
| `.venv/bin/pytest -q -p no:cacheprovider -rfEx` (full API suite) | 0 | **1310 passed, 2 skipped, 2 xfailed** in 1183 s (xfails: S2-SEC-06 accepted; Stage 1 SEC6-2 open, mitigated) |
| `npx tsc --noEmit`, `npx eslint .` (`apps/web`) | 0 / 0 | clean |
| `npx playwright test --reporter=list --trace off` (`tests/e2e`) | 0 | **51 passed, 1 skipped**, 0 failed, 0 flaky |

CI note: on the Stage 1 branch the same tree's `api` job failed on GitHub Actions on one timing assertion of the
Stage 1 security verifier (`test_per_character_scans_are_bounded_by_the_pre_normalisation_cap`: 12–16 ms measured on
the runner against a 10 ms threshold; it passes locally). The cause is real work per hostile cell in the number parser
and is being reduced on the Stage 1 branch; until that is merged this branch's `api` job can fail for the same reason.

## After merging Stage 1 fix round 11 (commit f777817) — CI findings and fixes
CI on f777817 (PR #2): `api` and `web` success on both events, `security` jobs success; **e2e failure** on both events
(`50 passed, 3 failed, 1 skipped`: `qa-stage2-followups-ui.spec.ts:217`, `stage2-followup.spec.ts:23` and `:66`,
each also failing its retry) and **CodeQL code-scanning check failure** (python SARIF: 2 results).
- Browser failures: in all three tests the click on a customer's "Contacts" button timed out because a neighbouring
  card intercepted the pointer — the directory table overflowed its card at the CI runner's font metrics and slid
  under the "New customer" panel. Not reproducible locally (53 passed on the accumulated database and on a fresh one).
  Fix: the table scrolls inside its card (`overflow-x-auto`, `min-w-0` on the grid item) in
  `apps/web/src/app/(app)/customers/page.tsx`. Whether CI is green with it is recorded from the next run.
- CodeQL `py/polynomial-redos` (2): `app/core/logging.py:28` (log redaction of e-mail addresses) and
  `app/services/comms/providers.py:88` (`valid_email`). Both patterns had an unbounded local part and a domain class
  that overlapped its own separator. They now use bounded repetitions (64-character local part, labels of at most 63
  characters) and `valid_email` checks the length before matching. Measured on 200,000-character hostile strings:
  ≤ 0.06 s for redaction, address search and validation together. Stricter than before: `a@b..com` is no longer a valid
  address. Stage 2 tests that touch addresses: 358 passed, 1 xfailed (S2-SEC-06).
Local browser suite on this tree, web app rebuilt, fresh database: **53 passed, 1 skipped**, exit 0. (A second run on
the same database had 1 failure, `stage2-followup.spec.ts:66`, the spec QA recorded as order-dependent on accumulated
data — it failed in 2 s on its data assumption, not on the click.)

## CI on the head with the fixes above (PR #2, queried 2026-11-12): all 15 checks green on both events
`api` success: pytest `1509 passed, 5 skipped, 3 xfailed, 1 xpassed`; `e2e` success: Playwright `53 passed`, `1 skipped`,
no flaky or failed line; `web` success; `security` jobs success; CodeQL code-scanning check success (the two
`py/polynomial-redos` alerts are gone). The three browser tests that timed out on the Contacts button pass.
This is CI evidence for the implementing side's fixes; independent re-verification and the release review are still owed.

## Fix round 3 (review 1 findings S2-RR1-2, S2-RR1-4; same root cause as SEC3-14)
What changed:
- `apps/api/app/services/jobs/queue.py` `reclaim_abandoned()` (new) is called first by `run_once()`, which is used by the
  worker tick (`app/workers/runner.py`, all tenants, worker scope bound inside `queue.py` only) and by the tenant-scoped
  run-once (`org_id`, normal tenant scope). Two conditional `UPDATE ... RETURNING` statements on `status='running' AND
  locked_at < now() - make_interval(secs => timeout)`: attempts < max_attempts goes back to `queued` (attempt count
  kept, lock cleared, `last_error` says abandoned); attempts exhausted goes to `dead` and `_fail_schedule()` marks the
  schedule `failed`, so the admin retry (`POST /jobs/{id}/retry`) re-arms it. A row can be won by one reclaimer only.
- `apps/api/app/core/config.py` `job_visibility_timeout_seconds` (default 900, >= 1); `.env.example`
  `JOB_VISIBILITY_TIMEOUT_SECONDS`. No migration, no API contract change, no new dependency.
- Recording: structured log line `job reclaimed` (job id, kind, status), the module's existing convention for job state
  changes (`run_job` logs, it does not audit; there is no actor).
- Send safety: a reclaimed `send_draft` job re-enters `send_draft`, which locks the draft row and returns early for a
  `sent` draft. Not covered: crash between provider acceptance and the commit (at-least-once), see KNOWN_LIMITATIONS.
- `docs/KNOWN_LIMITATIONS.md`: the two S2-RR1 entries rewritten.
Tests (`apps/api/tests/integration/test_stage2_fixes3.py`, 10): `test_permanent_provider_error_fails_the_job_and_schedule_and_admin_retry_rearms_it`,
`test_exhausted_attempts_dead_letter_the_job_and_fail_the_schedule`, `test_retry_of_another_tenants_dead_job_is_404`,
`test_abandoned_schedule_job_is_requeued_with_attempts_preserved_and_processed_once`,
`test_worker_tick_reclaims_without_a_separate_call`,
`test_abandoned_job_with_exhausted_attempts_is_dead_and_fails_its_schedule`, `test_fresh_running_job_is_not_touched`,
`test_timeout_setting_is_honoured`, `test_tenant_scoped_reclaim_and_run_once_leave_another_tenants_abandoned_job_alone`,
`test_reclaimed_send_job_does_not_deliver_twice`. Not tested: two truly concurrent reclaimers (relies on the single
conditional UPDATE). The permanent-provider-error case ends `failed` (not `dead`) by the existing status rule.

| Command (APP_ENV=test, DB quotepilot_s2, from apps/api) | Exit | Result |
|---|---|---|
| `.venv/bin/pytest -q -p no:cacheprovider tests/integration/test_stage2_fixes3.py` | 0 | 10 passed in 20.5 s |
| `.venv/bin/pytest -q -p no:cacheprovider tests/security/test_stage2_audit.py tests/security/test_stage2_confirm.py tests/qa/test_qa_stage2*.py tests/integration/test_stage2_*.py` | 0 | 170 passed, 2 xfailed (S2-SEC-06, S2-SEC-12) in 268 s |
| `.venv/bin/ruff check app tests` | 0 | All checks passed |
| `.venv/bin/ruff format --check app tests` | 0 | 128 files already formatted |
| `.venv/bin/mypy app` | 0 | no issues in 74 source files |
Full API suite and Playwright were not run (out of scope).
