# Security re-verification of Stage 4 fix round 1 (2026-11-12)

Auditor: independent security-auditor agent. Worktree `/home/user/qp-stage4`, branch `claude/stage4-governed-platform`,
HEAD `6511c69`. Product code not modified. New test file: `apps/api/tests/security/test_stage4_reverify.py` (12 probes).
Local, in-process ASGI TestClient against PostgreSQL 16 (non-owner role, RLS enforced); no live servers, no paid or
external service, hosted planner never configured (a fake client object exercised its response parser).
Environment: `APP_ENV=test`, database `quotepilot_sec4b`, `uv run pytest -q -p no:cacheprovider <paths>`.

## Checklist
| # | Item | Verdict | Evidence |
|---|---|---|---|
| 1 | S4-SEC-01 approval lock | MITIGATED | `invocations.py:79-92` `lock_invocation` (FOR UPDATE + populate_existing) called first in `decide`; `workflows.py::decide_step` locks the run. Audit test `test_s4_sec_01...` + HTTP race tests pass 3 of 3 rounds |
| 1 | S4-SEC-02 seat bypass by reactivation | MITIGATED | `org.py` update_member re-checks capacity on false->true under org-row lock; `test_s4_sec_02` passes |
| 1 | S4-SEC-03 seat race | MITIGATED (timing-dependent test, 3 of 3 green) | `org.py` create_member `SELECT ... FOR UPDATE` on organization row; `test_s4_sec_03`, fixes1 8-thread test |
| 1 | S4-SEC-04 durable failure record | MITIGATED for approved actions; see gap in item 5 | `invocations.py::execute` commits `failed` + `tool.failed` row, re-raises; `decide_step` ends the run `failed` |
| 1 | S4-SEC-05 NUL byte | MITIGATED | `schemas/platform.py` `RequestModel._reject_nul` (recursive); 4 audit params pass |
| 1 | S4-SEC-06 compensation audit | MITIGATED | `workflows.py:_compensate` writes `draft.cancelled_by_workflow_compensation`; order cancel goes through `order_service.transition` |
| 1 | S4-SEC-07 subscription race | MITIGATED (timing-dependent, 3 of 3 green) | `entitlements.py` `ON CONFLICT DO NOTHING` then select |
| 2 | Declared edits to auditor tests | VERIFIED | `git show 99d7558` on `test_stage4_audit.py`: AST comparison of old (`f744584`) and new, per function, ignoring decorators: only `test_s4_sec_01` body differs (import moved to function top, `# noqa: N806`, `a.close(); b.close()` split); the seven decorator diffs are exactly the seven removed `xfail` markers; no assertion changed |
| 3a | Kill switch: only workflow dry runs exempt; no side-effect path | MITIGATED | `invocations.py:73` `skip_kill_switch=dry_run and origin == "workflow"` (origin set only by server code). P06: workflow dry run of all five side-effect tools succeeds with `would_execute`, zero business rows/outbox/`tool.execute` audit; live run, assistant turn, manual `/actions` (also `dry_run`), approval of a pending action all 503; paused run is `killed` and 409 on approve; `job_runs` has no workflow/tool job kind and no worker code references workflows (grep of `workers/`, `services/jobs/`) |
| 3b | `quote.won` removed | MITIGATED | P07: validate `ok:false`, save yields an unactivatable version (422), definition stays disabled |
| 4 | Adversarial pass | see probes below | 12 probes; 9 pass, 3 reproduce weaknesses (S4-SEC-08..10) |
| 5 | Known gaps rated | see section | both Low, acceptable as documented limitations |
| 6 | Scan | RAN | see scan table |
| 7 | Lint | PASS | `ruff check app tests` exit 0; `ruff format --check app tests` exit 0 (174 files) |

## Probes (`test_stage4_reverify.py`)
| Probe | Result |
|---|---|
| P01 tool catalogue and `/actions` per role (5 roles, 9 tools, case/whitespace variants, planner naming denied tools) | held: 403/rejected, zero invocation rows |
| P02 client-supplied `requires_approval/status/scope/origin/run_id/requested_by/organization_id` | held: ignored; other approver 403; replay 409; one order |
| P03 other-tenant ids on 10 id routes (approve, reject, sessions, messages, activate, disable, runs, run get/approve/reject) | held: 404, body identical to a random uuid (ignoring request_id); lists empty |
| P04 hosted-planner parser, well-formed hostile output (38 actions, unknown/array tool names, string args, foreign org/customer/draft ids, extra keys) | held: step cap 8, extra keys rejected, nothing executed, foreign draft approval 404 |
| P05 hosted-planner malformed shapes | **S4-SEC-08** (xfail) |
| P06 kill switch | held (3a) |
| P07 `quote.won` | held (3b) |
| P08 breaker/step cap: after 5 failures, another workflow, disable+re-enable, kill-switch engage/release, plan change, lower roles, `context.budget_used`, 9-step definition with `max_depth` 50 | held: 429 / 403 / 422 |
| P09 `rfqs_per_month` with 8 concurrent creates at 197/200; billing | held: 1..3 succeed, counter == 197 + successes; billing reference `test_*`, no payment module imported |
| P10 support export content | **S4-SEC-09** (xfail); other-tenant id/name absent |
| P11 audit row per mutating endpoint (13 mutations) | **S4-SEC-10** (xfail) |
| P12 CSRF on every mutating route of `platform.router`, enumerated from the router (missing and forged token) | held (>= 15 routes) |
Also checked by reading: `0008` migration enables and FORCEs RLS and adds a `tenant_isolation` policy with USING and WITH CHECK on all 11
tables (lines 215-222); worker scope only on `workflow_runs/steps`, `tool_invocations`. Live RLS test is in the first audit (passes).

## Findings
| ID | Sev | Title | Reproduction / impact | Remediation |
|---|---|---|---|---|
| S4-SEC-08 | Low | Hosted planner output not fully validated | `assistant.py` `AnthropicPlanner.plan` catches only `ValueError/AttributeError`; model JSON `{"actions": null}` or `{"actions": {...}}` raises `TypeError` -> HTTP 500 (`test_p05`, `--runxfail` shows 500). No data exposure, no action; reachable only with a hosted provider configured | Parse with a Pydantic model; catch `Exception`, discard answer |
| S4-SEC-09 | Low | Free text in support export | Approver's rejection `reason` is copied into `failure_explanation` (`workflows.py:decide_step`) and into `/platform/support-export` (`recent_failed_runs`, 300 chars), which declares `contains_customer_data: false` (`test_p10`; canary customer name typed in the reason appears). Admin/owner only; the text is typed by a user, not extracted from data | Omit reason from the explanation (keep it in `decision_reason`), or export step ids and status only |
| S4-SEC-10 | Low | No audit row for `POST /assistant/sessions` and for a chat turn without tool actions | `test_p11`: only 'session' and 'message' step leave audit count unchanged; rows persisted (redacted message, `ai_usage`) | Audit `assistant.session_created` / `assistant.message` (no content) |

## Known gaps stated by the implementer
1. Compensation not run when an approved step fails at approval time: Low. Earlier effects are drafts (never sent) or a proposed order (unconfirmed); run is `failed` with explanation and `workflow.run_finished` audit. Acceptable as documented (KNOWN_LIMITATIONS).
2. Failed propose-path of non-approval tools rolled back when the exception reaches the HTTP layer (manual `/actions`): Low, no consequential side effect existed and the user gets the error; by code reading, assistant and workflow callers catch `AppError` and commit, so their record stays (not separately tested). Acceptable as documented; the attempt itself leaves no `tool.propose` audit row.

## Runs
| Command (cwd `apps/api`) | Exit | Result |
|---|---|---|
| `pytest tests/security/test_stage4_audit.py` | 0 | 91 passed, 0 xfailed (160 s) |
| same file, `-k "sec_01 or sec_02 or sec_03 or sec_07 or concurrent or atomic"` x3 | 0,0,0 | 7 passed each |
| `pytest tests/integration/test_stage4_fixes1.py -k "race or concurrent or sec01..03 or sec07"` x3 | 0,0,0 | 9 passed each |
| `pytest tests/integration/test_stage4_fixes1.py` | 0 | 22 passed |
| `pytest tests/security/test_stage4_reverify.py` (final) | 0 | 9 passed, 3 xfailed (strict) |
| `pytest --runxfail ... -k "p05 or p10 or p11"` | 1 | 3 failed for the documented reasons (500; canary in export; session/message unaudited) |
| `ruff check app tests`; `ruff format --check app tests` | 0; 0 | clean; 174 files formatted |
Not run by instruction: full API suite, browser suite, mypy.

## Scan (`make security-scan`, exit 0, `docs/evidence/scan-20261112-221612.md`, commit 6511c69)
pip-audit: ran, exit 0, no known vulnerabilities. bandit `-ll`: ran, exit 0, no medium/high issues printed. ruff S rules on `app`: ran, exit 0.
`npm audit --omit=dev`: ran, exit 0, 0 vulnerabilities. `npm audit` full: ran, exit 1, 8 moderate (dev-only, `minimatch` chain, as before).
gitleaks: NOT AVAILABLE. semgrep: NOT AVAILABLE (CI-only). Fallback secret-pattern grep: exit 0, no patterns. Web bundle grep (`.next/static`,
a build from an earlier run): exit 0, no patterns. Fallback grep is not a substitute for gitleaks.

## Not checked
Live servers/browser (CSP, XSS in the four web pages), real hosted model and its prompt-injection behaviour, real email/SMS/S3 adapters,
load/exhaustion of dry runs and sessions (S4-OBS-04 unchanged), kill-switch race with an in-flight approval (check is at execute start),
migration up/down, Stage 1 parser checks (zip-bomb, PDF JS, CSV) beyond existing suites, cookie flags, gitleaks/semgrep.

## Residual risk
| Risk | Level |
|---|---|
| S4-SEC-08..10 open (xfail) | Low |
| Self-approval of an assistant/manual proposal by the requester (documented; downstream gates remain) | Accepted, Low |
| Owner can switch plan freely in test-mode billing (no payment verification by design) | Accepted until a real adapter exists |
| Timing-dependent race tests cannot prove absence of races | Low |
| Hosted planner path unexercised with a live model | Unknown |
| Unmetered dry runs/sessions/proposals (storage growth) | Low |
No claim is made that the system is free of vulnerabilities; this lists what was exercised.
