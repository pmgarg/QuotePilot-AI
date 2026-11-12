# Implementer evidence: Stage 4 fix round 1 (security S4-SEC-01..07 and QA D-QA4-01..05)

Worktree `/home/user/qp-stage4-fix`, branch `stage4-fix1`. All pytest runs: `APP_ENV=test`, database `quotepilot_s4fix`
(non-owner app role, RLS enforced), cwd `apps/api`, `.venv/bin/pytest -q -p no:cacheprovider <file>`.
Status words are the implementer's; nothing here is a verification verdict (no VERIFIED claims).

## Security findings
| Finding | Change | Regression tests (`tests/integration/test_stage4_fixes1.py`) |
|---|---|---|
| S4-SEC-01 | `services/platform/invocations.py` `lock_invocation()` (SELECT FOR UPDATE + populate_existing) at the start of `decide()`; `workflows.py::decide_step` locks and refreshes the `WorkflowRun` (and expires `steps`) before checking status. Loser gets StateError -> 409, no second `tool.execute` | `test_sec01_concurrent_http_approvals_execute_once` (6 threads), `..._approve_then_reject_race_has_single_outcome`, `..._concurrent_workflow_step_approval_executes_once`, `..._approve_authz_cross_tenant_and_forbidden_role` |
| S4-SEC-02 | `api/routers/org.py::update_member`: seat capacity check on is_active false->true under the existing org-row lock | `test_sec02_reactivation_allowed_when_seat_free_and_blocked_when_full`, `..._reactivation_races_creation_without_exceeding_seats` |
| S4-SEC-03 | `org.py::create_member`: `SELECT ... FOR UPDATE` on the organization row before counting | `test_sec03_concurrent_member_creation_never_exceeds_seats` (8 threads: exactly 1x201, 7x429) |
| S4-SEC-04 | `invocations.py::execute(durable_failure)`: handler work is already in a savepoint; on failure status `failed`, error summary (field/type only for app errors, exception class name otherwise), `tool.failed` audit row, then `commit` and re-raise (same HTTP status as before). `decide` defaults to durable for manual actions; for workflow steps `decide_step` catches the recorded failure, ends the run `failed`, writes `workflow.run_finished` | `test_sec04_failed_approved_action_is_persisted_and_not_retryable`, `test_sec04_failed_workflow_step_ends_run_and_records_failure` |
| S4-SEC-05 | `schemas/platform.py`: `contains_nul` (recursive over strings, dict keys, lists) and `RequestModel` base with a before-validator, used by all Stage 4 request bodies -> 422 | `test_sec05_contains_nul_is_recursive`, `test_sec05_nul_rejected_with_422` (4 params) |
| S4-SEC-06 | `workflows.py::_compensate`: `audit.record(action="draft.cancelled_by_workflow_compensation", actor None, summary {reason "workflow compensation", run_id})` in the same transaction | `test_sec06_compensated_draft_has_audit_row_with_run_id` |
| S4-SEC-07 | `entitlements.py::subscription_for`: `INSERT ... ON CONFLICT DO NOTHING` (pg_insert) then select | `test_sec07_subscription_get_or_create_is_idempotent_and_race_safe` (12 threads, RLS role), `..._is_tenant_scoped` |

## QA defects
| Defect | Change | Tests |
|---|---|---|
| D-QA4-01 | `invocations.py::propose`: kill-switch check skipped only when `dry_run and origin == "workflow"`; handlers run with `dry_run=True` (no provider/LLM call; only run/invocation/audit rows). Live runs, approvals, assistant and manual `POST /actions` (incl. `dry_run`) stay 503. Reason for the narrower scope: auditor test `test_a07_kill_switch_stops_everything` asserts `POST /actions {dry_run:true}` -> 503, so exempting that path would conflict with it; QA's test only needs the workflow dry run | `test_qa01_...` + QA `test_kill_switch_terminates_runs_blocks_assistant_and_actions_but_allows_dry_run` |
| D-QA4-02 | `routers/platform.py::list_workflows` requires MANAGE_WORKFLOWS. All other workflow routes already did (there is no definition-detail or version GET route). Web `/workflows` page is already gated by its `allowed` check (renders Forbidden) and no other page calls these routes | `test_qa02_list_workflows_requires_manage_workflows` |
| D-QA4-03 | same fix as S4-SEC-05 | `test_qa03_nul_in_assistant_message_is_422` + QA test |
| D-QA4-04 | Removed `quote.won` from `workflows.TRIGGERS` (validation reports it as an error). Dispatching was not chosen: QA's test requires `ok is False` or a warning, and no code path moves a quote to `won` through a router hook that is a small change. Documented in API_CONTRACT and KNOWN_LIMITATIONS | `test_qa04_quote_won_trigger_is_not_accepted` + QA test |
| D-QA4-05 | `tools.py::validate_args`: message is built from `exc.errors(include_input=False)` as `loc: type` only (pydantic `msg` can echo input fragments); applies to `step.error`, `failure_explanation`, support export, audit summary, invocation `error`, 422 body | `test_qa05_validation_error_does_not_echo_input_into_step_error_audit_or_export` (greps export, steps, actions, all audit summaries) + QA test |

## Auditor test edits (declared)
- `tests/security/test_stage4_audit.py`: removed the seven `@pytest.mark.xfail(...)` markers of S4-SEC-01, 02, 03, 04, 05, 06, 07 reproductions (nothing else in their bodies changed).
- Same file, mechanical lint/format only (no assertion changed), because `make api-lint` runs `ruff check/format --check tests`: unused imports removed (`create_engine`, `AIUsage`, `make_tenant`), import sort, `ruff format`, and `# noqa: N806/S608/B017` on 7 lines.
- QA files (committed unmodified in commit "Stage 4 independent QA: tests and evidence (as delivered)"), then in the fix commit: `ruff check --fix` (unused `json` import, empty f-string prefix) and `ruff format` on `apps/api/tests/qa/*` only; no assertion changed.

## Behaviour changes visible to API consumers
- Concurrent/duplicate approval or reject of the same action: loser gets 409 (previously possible double execution).
- A failed approved manual action now stays `failed` (visible in `GET /actions`, `tool.failed` audit row), is not approvable again (409); HTTP status of the failing approval unchanged. Failed approved workflow step: approve returns 200 with run `failed` (previously an error response and a stuck `awaiting_approval` run).
- 422 for NUL characters in Stage 4 request bodies; reactivating a member can now return 429 (seat limit).
- `GET /workflows` 403 for sales/approver/readonly. Workflow `quote.won` trigger rejected by validation. Workflow dry runs work under the kill switch. Tool-argument validation messages no longer contain input values or pydantic messages (`field.path: error_type`).
- Session lifetime: no new code relies on the DB session staying open after an endpoint returns (no streaming/background tasks). `durable_failure` commits inside the request before re-raising; `app/api/deps.py` and `app/db/session.py` untouched.

## Runs (final code state)
| Command | Exit | Result |
|---|---|---|
| `pytest tests/integration/test_stage4_fixes1.py` (first run) | 1 | 1 failed, 21 passed (my test bug: asserted on the run response that legitimately echoes caller context; fixed) |
| `pytest tests/integration/test_stage4_fixes1.py` (second run) | 0 | 22 passed in 46.5 s |
| `pytest tests/integration/test_stage4_platform.py` | 0 | 16 passed in 45.1 s |
| `pytest tests/security/test_stage4_audit.py` | 0 | 91 passed in 223.4 s (0 xfailed) |
| `pytest tests/qa/test_qa_stage4_*.py` | 0 | 115 passed in 249.3 s |
| `ruff check app tests` | 0 | All checks passed |
| `ruff format --check app tests` | 0 | 130 files already formatted |
| `mypy app` | 0 | Success: no issues found in 93 source files |
| openapi regeneration (CI command) vs `docs/openapi.json` | 0 | identical, no update needed |
Note: the first-run test file fixes1 had one failure only in that first run (listed above); the race tests passed in both runs. The full API suite, `make web-check` and Playwright were not run (instructed not to; no web code changed).

## Not fixed / caveats
- Workflow compensation does not run when an approved step fails at approval time (run ends `failed`); documented.
- A workflow-origin invocation that fails on the propose path (non-approval tools) is still rolled back with the request transaction, as before; only approved actions are made durable.
- Timing-dependent race tests could not be proven flake-free; they passed on every run after the fix.

## Orchestrator addendum — full suites on the merged tree (2026-11-12)
Tree: `d80a024` = Stage 4 fix round 1 merged with Stage 3 (fix round 1 and the worker-scope change), Stage 2 (fix rounds
1–2) and Stage 1 (fix rounds 7–9, e2e harness fix). Migration chain 0001 … 0006 → 0006b → 0007 → 0007b → 0008 applied
to an empty database (`quotepilot_s4`, recreated). Ports 8064/3064. Two other suites were running.

| Command (cwd) | Exit | Result |
|---|---|---|
| `ruff check app tests` / `ruff format --check app tests` / `mypy app` (`apps/api`) | 0 / 0 / 0 | clean; 157 files formatted; 95 source files |
| `.venv/bin/pytest -q -p no:cacheprovider -rfEx` (full API suite) | 0 | **1526 passed, 2 skipped, 1 xfailed** in 1615 s (xfail: S2-SEC-06, accepted in Stage 2) |
| `npx tsc --noEmit`, `npx eslint .` (`apps/web`) | 0 / 0 | clean |
| `npx playwright test --reporter=list --trace off` (`tests/e2e`) | 0 | **73 passed, 1 skipped**, 0 failed, 0 flaky |

This is the first full API and browser run of Stage 4 with every earlier stage's fixes; it is the implementing side's
run, not a verification. CI on this branch is expected to show the Stage 1 QA-test environment issue (five tests in
`tests/qa/test_qa_reverify_review5.py`) until the corrected file is merged from Stage 1.
