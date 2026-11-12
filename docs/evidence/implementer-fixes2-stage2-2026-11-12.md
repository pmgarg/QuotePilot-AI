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
