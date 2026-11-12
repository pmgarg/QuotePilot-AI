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
