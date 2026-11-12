# Security audit and red-team — Stage 4 (governed AI sales operations)

Auditor: independent security-auditor agent (did not implement Stage 4). Worktree `/home/user/qp-stage4`, branch
`claude/stage4-governed-platform`, commit `0ac3404`. Product code was not modified. Executed 2026-11-10 to 2026-11-12.
No paid or third-party service was called; the hosted planner stayed unconfigured (`AI_PROVIDER=disabled`); all
adversarial model output was simulated with monkeypatched fake planners. No live servers were started (all checks ran
through the in-process ASGI TestClient against real PostgreSQL 16), so there are no ports to close.

Database used by every pytest run below: `quotepilot_sec4`
(`DATABASE_URL_TEST=postgresql+psycopg://quotepilot_app:...@localhost:5432/quotepilot_sec4`, admin URL likewise
`quotepilot_owner`, connection as the non-owner role so RLS is enforced).
**Process deviation, disclosed:** one early run (a backgrounded pytest of `test_stage4_audit.py`, started 2026-11-10 in a
shell where the variables were not exported) used the default database `quotepilot_test` (Stage 1 branch). The test
fixtures TRUNCATE all tables on that database, so any other agent's data there was wiped for a moment; the run was killed
within minutes and all results reported here come from reruns with the correct variables. The Stage 1 owner should re-run
anything that depended on `quotepilot_test` contents.

## Scope and methodology
Static review of `services/platform/{entitlements,killswitch,tools,invocations,assistant,workflows}.py`,
`api/routers/platform.py`, `services/authz.py`, migration `0008`, plan hooks in `routers/org.py` / `routers/rfqs.py`,
event dispatch, and the four web pages; then adversarial tests (`apps/api/tests/security/test_stage4_audit.py`) written
against the running service layer and HTTP routes. Findings are strict xfail tests carrying their id (they fail the suite
the day the defect is fixed, so remove the marker with the fix); controls that hold are plain passing tests.
Race tests use threads + barriers against real PostgreSQL; S4-SEC-03 and S4-SEC-07 are timing dependent (`strict=False`).

## Findings
| ID | Severity | Title | Location | Reproduction | Impact | Remediation |
|---|---|---|---|---|---|---|
| S4-SEC-01 | Medium | Approval of a tool invocation is read-then-write with no row lock or compare-and-set; a stale/concurrent approval executes the action twice | `services/platform/invocations.py:26-33` (no `FOR UPDATE`), `:168-171` (status check on in-memory copy), routes `platform.py` approve | `test_s4_sec_01...`: two sessions load the same `proposed` export invocation, both call `decide(approve=True)`; both pass the state check and both execute (two `tool.execute` audit rows). Tools without a downstream duplicate guard (export, draft creation via workflows, deletion requests) are exposed; `propose_order` is saved only by Order's own duplicate guard. | A double-click or two approvers can run an approved side effect twice; single-approval guarantee (S4-A03 "cannot be raced") is not met. | `SELECT ... FOR UPDATE` in `get_invocation` for decide paths, or `UPDATE ... SET status='approved' WHERE id=:id AND status='proposed' RETURNING` and abort on 0 rows. Same for `decide_step` on `WorkflowRun`. |
| S4-SEC-02 | Medium | Seat limit bypass by member reactivation | `api/routers/org.py:157-166` (no `require_capacity` on `is_active` false→true) | `test_s4_sec_02...`: fill 10 seats, deactivate one, create a new member, reactivate the old one: 11 active seats, 200 OK. | Plan seat entitlement (S4-A06) bypassed by any admin. | Call `entitlements.require_capacity(seats, active+1)` on reactivation, inside the same org-row lock. |
| S4-SEC-03 | Low | Seat check races with concurrent member creation (count-then-insert, no lock; `update_member` locks the org row but `create_member` does not) | `api/routers/org.py:94-99` | `test_s4_sec_03...`: 9/10 seats, 8 parallel creates; more than one succeeds (reproduced). | Seat cap exceeded by a few seats under concurrency. | Take the same `SELECT ... FOR UPDATE` on the organization row before counting. |
| S4-SEC-04 | Low | A failed execution of an approved action leaves no failure record or `tool.failed` audit row for non-self-committing tools: the handler's exception propagates after the `failed` status is flushed, and `get_db` rolls the whole transaction back | `services/platform/invocations.py:119-148`, `api/deps`/`db/session.py get_db` | `test_s4_sec_04...`: approve `propose_order` on a draft quote: 4xx returned; invocation stays `proposed`, no `tool.failed` row. | Audit gap for failed high-impact actions; invocation can be retried indefinitely without trace. | Commit the failure record in a separate transaction (as done for `self_commits`), or convert the error into a response without re-raising. |
| S4-SEC-05 | Low | NUL byte (`\x00`) in assistant message, session title, tool argument or workflow name reaches PostgreSQL and returns HTTP 500 (`psycopg.DataError`) | `services/platform/assistant.py:221` and other free-text paths; schemas lack a control-char check | `test_s4_sec_05...` (4 params). | Unhandled 500 per request (log noise, no data exposure observed; stack traces were not returned). Availability/robustness only. | Reject `\x00` in the Pydantic schemas (validator) or strip it before persistence. |
| S4-SEC-06 | Low | Workflow compensation of a draft writes `draft.status = "cancelled"` directly with no audit row (docstring says compensations go through audited services) | `services/platform/workflows.py:334` | `test_s4_sec_06...`: run `draft_follow_up` then a failing step with `on_failure=compensate`; draft is `cancelled`, no draft/cancel audit event. | Undo of a side effect is not traceable. | Route through the Stage 2 cancel service (audited) or call `audit.record`. |
| S4-SEC-07 | Low | `subscription_for` get-or-create is not conflict-safe: concurrent first requests of a new organisation raise `uq_subscription_org` IntegrityError -> 500 | `services/platform/entitlements.py:83-89` | `test_s4_sec_07...` (8 barrier threads); also seen as unhandled thread errors during the budget race test. | Transient 500s on first use of the platform by a tenant. | `INSERT ... ON CONFLICT DO NOTHING` then select; or create the subscription at org creation. |

Observations (not scored, no failing test):
- S4-OBS-01: `propose_order` / `send_follow_up` proposals are created without resolving their target (quote/draft); a foreign or non-existent id yields a `proposed` row and a 404 only at approval. No cross-tenant effect (verified: approval returns 404, no order in either tenant); responses for foreign and non-existent ids are identical.
- S4-OBS-02: `tool_invocations.args` stores the user-supplied arguments unredacted (e.g. a deletion `reason`); results, messages and `ai_usage` are redacted/content-free. Treat args as business data.
- S4-OBS-03: `draft_follow_up` (scope `write:drafts`) executes immediately without approval by design; under a hostile planner it can create drafts (never sent) for any quote the user can edit. Within the user's own permissions.
- S4-OBS-04: dry runs, `/actions` proposals and session creation have no per-request budget (only live runs and assistant messages are metered); each writes invocation/audit rows. Storage-growth DoS by an authenticated user is possible. Not tested to exhaustion.
- S4-OBS-05: `search_quotes {"limit": true}` is coerced to 1 by Pydantic lax mode (harmless).
- S4-OBS-06: `run.context[...] = ...` in `workflows.advance` mutates a plain JSONB dict in place (no MutableDict), so that one context update may not persist; the later `run.context = {...}` assignments do. Correctness only.
- Separation of duties: a requester may approve their own assistant-proposed action (documented in KNOWN_LIMITATIONS); downstream gates (order confirmation, deletion approval, send gate) remain.

## Per-control results
| Control | Result | Evidence (tests in `test_stage4_audit.py` unless noted) |
|---|---|---|
| S4-A01 injection in user text, business data (customer name, contact notes, RFQ text, quote lines), fake-model output (unknown/odd-case/whitespace tool names, extra args such as price/role/org id, foreign org ids) | Held: no unapproved side effect, only registered+permitted tools run | `test_a01_*` (5), plus implementer test |
| S4-A02 tenant isolation: sessions, invocations, runs, workflows, usage, support export; same-tenant cross-user | Held (404 cross-tenant, 404/403 cross-user) | `test_a02_*` (3) |
| RLS on all 11 new tables incl. worker scope (insert/select cross-tenant denied; no-context sees 0; worker scope reaches only runs/steps/invocations) | Held | `test_a02_rls_blocks...` |
| S4-A03 no side effect before approval; replay, reject-then-approve, workflow step via `/actions` (403), approver authorization, plan downgrade and kill switch re-check at execution, role change between propose and approve | Held except concurrent/stale approval | `test_a03_*` (7); **S4-SEC-01** |
| S4-A04 malformed args (17 parametrised cases), non-object args, odd tool names, SQL/LIKE strings, template injection, context key filtering, unicode, definition limits | Held (422/404, no 500) except NUL byte | `test_a04_*`; **S4-SEC-05** |
| S4-A05 step budget (8), message size, daily budget across many sessions and 12 concurrent clients, atomic `consume()` under 40 threads (exactly 20 allowed) | Held | `test_a05_*` (4) |
| S4-A06 plan change owner-only, CSRF, no cross-tenant/override smuggling; RFQ monthly limit; scope limits; active-workflow limit; seats | Held except seat reactivation and seat race | `test_a06_*` (4); **S4-SEC-02/03** |
| S4-A07 kill switch: assistant, `/actions` incl. dry run, approval of pending actions, workflow approve, new live runs, event-triggered runs, mid-run between steps, platform-wide env flag; non-admin cannot release; killed runs stay killed | Held | `test_a07_*` (3) |
| S4-A08 secrets/raw content: API-key pattern, JWT, e-mail redacted in messages; none in `tool_invocations.result`, `assistant_messages`, `proposed_actions`, `workflow_steps.output`, `ai_usage` (no content columns), audit summaries, usage, support export, logs; web source and `.next/static` free of provider keys | Held | `test_a08_*` (3), `test_web_pages_do_not_embed_provider_keys`, bundle grep in scan |
| S4-A09 dry run side-effect free for all five side-effect handlers (row counts, audit rows, drafts untouched, provider outbox empty), dry-run invocation cannot be approved; workflow dry run; export bytes never persisted (also via workflow approve); export CSV formula-safe; order compensation cancels the proposed order | Held except draft-compensation audit | `test_a09_*` (6); **S4-SEC-06** |
| CSRF on all 15 Stage 4 mutating routes (missing/forged token -> 4xx; anonymous -> 401/403) | Held | `test_csrf_required_on_every_stage4_mutation[*]` |
| RBAC matrix for ops/workflow/kill-switch/plan routes; readonly cannot propose side effects | Held | `test_rbac_*` |
| Stage 1-3 regression (existing `tests/security` suite + Stage 4 integration suite) | Held | run 3 below |

## Tool and test runs (all pytest runs: DB `quotepilot_sec4`, `-p no:cacheprovider`, cwd `apps/api`)
| # | Command | Exit | Summary |
|---|---|---|---|
| 1 | `pytest tests/security/test_stage4_audit.py` (first complete run) | 1 | 8 failed, 75 passed, 4 xfailed: failures were test bugs (3), plus new findings S4-SEC-05/06/07 and a validation-before-authz test artifact; tests corrected |
| 2 | same file after corrections | 1 | 80 passed, 1 failed (test artifact, NUL byte in an input meant to be benign; moved to S4-SEC-05), 10 xfailed |
| 3 | `pytest tests/security tests/integration/test_stage4_platform.py -m "security or integration"` | 0 | 177 passed, 1 skipped, 10 xfailed (the 10 xfails are the open findings S4-SEC-01..07, all reproduced); existing Stage 1-3 security tests and the implementer Stage 4 suite pass |
| scan | `bash scripts/security_scan.sh` -> `docs/evidence/scan-20261110-172956.md` | 0 (script) | pip-audit exit 0 ("No known vulnerabilities found"); bandit -ll exit 0; ruff `app` (S rules) exit 0; `npm audit --omit=dev` exit 0 (0 vulnerabilities); full `npm audit` exit 1 (8 moderate, dev-only: `minimatch` via `typescript-eslint`/`eslint-config-next`, GHSA-g3hv-75wg-m249; not in production deps); gitleaks NOT AVAILABLE; semgrep NOT AVAILABLE (CI-only); fallback pattern grep reported one hit = the placeholder `sk-ant-api03-abcdefghijklmnop` in `.gitleaks.toml` (allowlist entry, not a secret); web bundle grep: no secret patterns in `.next/static` |

Not run: gitleaks and semgrep (unavailable locally; reported as not run, not as clean). pip-audit queries the public vulnerability service by design of the mandated scan.

## Residual risk
| Risk | Level | Note |
|---|---|---|
| S4-SEC-01 double execution under concurrent approval | Medium until fixed | Most downstream gates dedupe, not all |
| Seat entitlement bypass (S4-SEC-02/03) | Medium/Low | Commercial, not confidentiality |
| Audit gaps on failed/compensated actions (S4-SEC-04/06) | Low | Traceability |
| Robustness: NUL byte 500, subscription race 500 (S4-SEC-05/07) | Low | No data exposure observed |
| Hosted planner path | Unknown | `AnthropicPlanner` never exercised (no paid calls). Only the policy gate around it is tested via fakes; response parsing of a real model, and `redact_for_model`, were not red-teamed |
| Prompt injection with a real model | Unknown | Tools results are not fed back into planning (single pass), which limits the surface; not validated with a live model |
| Unbounded dry-run/proposal/session creation | Low | S4-OBS-04 |
| Self-approval of assistant actions | Accepted by design | Documented limitation |
| Billing is test-mode only | n/a | Real adapter not reviewed |

## Not tested
Live HTTP servers/browser (Playwright, CSP in a real browser, XSS in the four web pages beyond a source grep for
`dangerouslySetInnerHTML`/provider keys); real hosted model; real email/SMS/S3 adapters; worker process scheduling of
workflows (Stage 4 has no worker job beyond RLS scope); load/exhaustion DoS; migration up/down safety (implementer
verified, not repeated); gitleaks/semgrep; TLS/deployment configuration; session-cookie flags and Stage 1 upload
parsers (zip-bomb XLSX, PDF JS, CSV import) were re-covered only through the existing `tests/security` suite in run 3,
not newly tested. This report does not claim the system is free of vulnerabilities; it lists what was exercised.
