# QA re-verification of Stage 4 fix rounds 1-2 (independent)

Head: `da27047` (branch `claude/stage4-governed-platform`, worktree `/home/user/qp-stage4`), working tree clean except my new
test file and untracked logs `.qa4b-*.log` (raw run output). Nothing committed. Product code untouched.

## Environment
Linux, 4 cores, Python 3.13 (uv), Node 22, PostgreSQL 16 local service, database `quotepilot_qa4b` (non-owner app role,
RLS enforced; owner role only for migrations). `APP_ENV=test`, `DATABASE_URL_TEST=postgresql+psycopg://quotepilot_app:***@localhost:5432/quotepilot_qa4b`.
Browser: Playwright Chromium, API 8094, web 3094 (web built by the harness, no `E2E_SKIP_WEB_BUILD`). No Docker, no hosted LLM, no real SMTP.
Evidence kind: all pytest results are integration (API + PostgreSQL); browser results come only from the Playwright run.

## Checklist
| # | Item | Verdict | Evidence |
|---|---|---|---|
| 1a | D-QA4-01 kill switch vs dry run | CONFIRMED fixed; exemption judged acceptable | QA test `test_kill_switch_terminates_runs_blocks_assistant_and_actions_but_allows_dry_run` passes (run 3). `invocations.py:65-69`: skip only when `dry_run and origin == "workflow"`. My `test_manual_dry_run_action_stays_blocked...` passes: manual `POST /actions {dry_run:true}` 503 while engaged, 201 before and after release. PRD S4-A07 only requires that the kill switch terminates workflows (live runs/approvals/assistant/manual actions stay 503; killed runs stay killed); it does not require dry runs to be blocked, and S4-A09 (verified by `test_dry_run_of_every_side_effect_tool_changes_no_business_data`) shows dry runs have no side effects. Residual: a dry run still writes run/invocation/audit rows during an incident (acceptable, not a side effect on business data). |
| 1b | D-QA4-02 `GET /workflows` | CONFIRMED fixed | `test_workflow_definitions_listing_is_owner_admin_only_per_api_contract` passes; my `test_get_workflows_role_matrix...`: owner/admin 200, approver/sales/readonly 403. |
| 1c | D-QA4-03 NUL byte | CONFIRMED fixed | QA `test_nul_character_in_message_is_a_client_error_not_a_500` passes; my 2 parametrized tests: NUL in message, action args and session title -> 422. |
| 1d | D-QA4-04 `quote.won` | CONFIRMED fixed; decision acceptable | QA test (unchanged, `ok is False or warning`) passes; validate reports `ok:false`. My test: `POST /workflows` with this trigger may save an inactive draft (201) but activation is refused and `active_version_no` stays null. Removing an undispatchable trigger is correct against the PRD (no S4 requirement names `quote.won`; API_CONTRACT lists manual/quote.approved/order.confirmed); documented in KNOWN_LIMITATIONS. Note: saving the invalid draft with 201 is consistent with versioned drafts but is not an error response. |
| 1e | D-QA4-05 support export leak | CONFIRMED fixed | QA `test_support_export_and_usage_contain_no_customer_or_message_data` passes; my `test_validation_error_text_never_contains_input_fragments` (canary in args) absent from 422 body, support export, `/actions`. |
| 2 | Declared edits to QA tests | CONFIRMED | `git log -- apps/api/tests/qa/test_qa_stage4_* tests/e2e/specs/qa-stage4-pages.spec.ts` shows only `514afe9` (mine, as delivered) and `99d7558`. `git diff 514afe9 99d7558 -w` on the four QA files: only line wrapping/quote style, an unused `import json` removed, `f"/actions?status=executed"` f-prefix dropped. No xfail marker existed in QA files (defect tests were left failing, not xfailed) and none was added; no assertion, status code or expectation changed (the D-QA4-04 and D-QA4-01 tests are byte-identical modulo wrapping). No e2e spec was touched after delivery. The security verifier's three xfail markers were replaced by comments (fix round 2, declared; assertions unchanged, security files not edited by me). |
| 3 | Stage 4 QA + integration files | CONFIRMED | run 3: 153 passed. |
| 4 | Acceptance/features | see table | |
| 4a | Fix round 2 behaviour | CONFIRMED | `test_run_explanation_keeps_rejection_reason_but_support_export_drops_it_and_kill_switch_reason` (reason present in run explanation via POST reject and GET run; reason and kill-switch reason absent from `/platform/support-export`, failed run id still listed); `test_new_session_and_plain_chat_turn_write_audit_rows_without_message_text` (rows `assistant.session_started` / `assistant.message` with `turn`, `chars`; message canary in no audit row). |
| 4b | Approval-required assistant tool | CONFIRMED | `test_assistant_proposed_order_executes_only_after_permitted_other_user_approves_exactly_once`: sales' chat proposes `propose_order` (status proposed, 0 orders); readonly approve 403/404, 0 orders; admin approve 200 -> 1 order; second approve by admin and by requester 409, still 1 order, one `tool.approve` audit row. |
| 4c | Plan limits | CONFIRMED | `test_seat_limit_gives_clear_error_and_no_side_effect` (429 `limit_exceeded`, message mentions seat, member list unchanged, no member audit row); `test_rfq_monthly_limit_gives_clear_error_and_creates_nothing` (429, RFQ count 1, usage counter 1, no extra rfq audit row). Limits set via the subscription override helper (test-only DB write, as in the QA suite). |
| 5 | Full API + browser runs | see runs table | |
| 6 | Lint | CONFIRMED | `uv run ruff check app tests` exit 0 "All checks passed!"; `ruff format --check app tests` exit 0 "175 files already formatted". (`mypy` on tests is not part of the gate; I did not run `mypy app`.) |

## Acceptance and features
| Req | Status | Test (integration unless noted) |
|---|---|---|
| S4-F01 command center | tested, passing | `test_command_center_counts_match_domain_state_and_roles`; B: `qa-stage4-pages.spec.ts`, `stage4-platform.spec.ts` |
| S4-F02 bounded assistant | tested, passing | `test_tool_catalogue_matrix_role_and_plan`, `test_assistant_cannot_read_other_tenants_data` |
| S4-F03 action scopes/approvals | tested, passing | `test_no_side_effect_tool_executes_on_proposal`, `test_p01_...` (security), my 4b test |
| S4-F04 budgets/breaker/kill switch | tested, passing; partial gap | step budget, turn limit, daily AI, breaker, kill switch tests. Gap (predecessor): runtime `budget_exceeded`, retry cap and per-step timeout not reachable via API; compensation after a failed approved step not run (documented) |
| S4-F05 workflows | tested, passing | validation matrix (18), versions/activation, dry run, `test_rejected_step_stops_run...` |
| S4-F06 packaging | partial (as declared) | Exists: plans/entitlements (starter/growth/scale), owner-only test-mode plan change, seats, limits, support export, usage/ops metrics, web `/platform`, `/settings`. Not built (no route, page or code found; grep for onboarding/wizard hits only the catalog import page): onboarding wizard, in-app help pages, demo-workspace reset (`make demo` only seeds). |
| S4-F07 no secrets/raw content | tested, passing | `test_secrets_and_pii_in_messages_are_not_stored_or_logged`, D-QA4-05 test, my 4a/D-05 tests |
| S4-A01 injection | tested, passing | `test_injection_in_rfq_text_and_customer_name_cannot_trigger_tools` |
| S4-A02 tenant isolation | tested, passing | `test_assistant_cannot_read_other_tenants_data` |
| S4-A03 unapproved never executes | tested, passing | approvals tests; my 4b; B: `stage4-platform.spec.ts` |
| S4-A04 malformed tool output | tested, passing (fake planner only) | 12 malformed-argument cases; hosted planner never called |
| S4-A05 loops/oversized | tested, passing | step budget boundary, 2 MB message, NUL tests |
| S4-A06 limits server-side | tested, passing | daily AI, seats, rfqs/month, active workflows; my 4c |
| S4-A07 kill switch | tested, passing | `test_kill_switch_terminates_runs_...`, `test_a07_kill_switch_stops_everything`; B spec |
| S4-A08 logs/traces | tested, passing | as S4-F07 |
| S4-A09 dry run no side effects | tested, passing | `test_dry_run_of_every_side_effect_tool_changes_no_business_data`; B spec |

## Runs
| # | Command (cwd) | Exit | Result |
|---|---|---|---|
| 1 | `uv run pytest -q -p no:cacheprovider tests/qa/test_qa_stage4*.py tests/integration/test_stage4*.py` (apps/api) | 0 (detached run; the summary shows no failure, the exit code itself was not captured) | `153 passed in 217.81s` |
| 2 | `uv run pytest -q -p no:cacheprovider tests/qa/test_qa_stage4_reverify.py` | first 2 runs 1 (my own test errors: kill-switch reason too short, wrong expectation on `POST /workflows` for `quote.won`, detached ORM rows); final 0 | final `10 passed in 13.58s` |
| 3 | `uv run pytest -q -p no:cacheprovider -rfEx` (apps/api, full, incl. my file) | 0 (`EXIT=0` in log) | `1953 passed, 2 skipped, 5 xfailed in 1566.18s (0:26:06)`; 0 failed. xfails: SEC6-2, SEC7-1, SEC8-1 (Stage 1), S2-SEC-06, S2-SEC-12 (Stage 2); none is Stage 4 |
| 4 | `npx playwright test --trace off` (tests/e2e; env as instructed, ports 8094/3094) | 0 (`EXIT=0`) | `1 skipped`, `75 passed (3.2m)`, no flaky, no failed line. Skipped: `screenshots.spec.ts:9` (capture Stage 1 walkthrough) |
| 5 | `uv run ruff check app tests` / `ruff format --check app tests` | 0 / 0 | all checks passed / 175 files formatted |
Runs 3 and 4 were sequential (browser after API) and each attempted once. No failure needed an isolated re-run.
Skips (2) in run 3 were not itemised by `-rfEx` (it lists only f/E/x); I did not identify them.

## Defects
None new (no D-QA4-06). The five earlier defects are fixed. Minor observations, not defects:
- `POST /workflows` stores a definition with an unsupported trigger as an inactive draft (201); only validate/activate reject it.
- Dry runs during a kill switch still write run/invocation/audit rows and are counted against the workflow-runs-per-day allowance (not asserted either way).

## Not checked
- Hosted (Anthropic) planner and any real model output; real email/SMS/S3 adapters; Docker.
- Runtime `budget_exceeded`, `rate_limited` retry cap, per-step timeout; compensation after failed approved step (documented unsupported).
- Onboarding wizard, help pages, demo reset (not built); mobile/axe checks of the Stage 4 pages.
- Concurrency/race tests were not re-derived by me (security verifier's and implementer's tests passed within run 3 but are not proven flake-free).
- The two pytest skips in run 3; `make web-check` (tsc/eslint/next build separately), `make security-scan`, `mypy app`.
