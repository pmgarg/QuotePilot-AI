# Implementer — Stage 4 (governed AI sales operations) — self-reported, not independently verified

Branch `claude/stage4-governed-platform`, based on the Stage 3 branch (b4256fb). Scope: S4-F01..F07, S4-A01..A09.
Nothing here is VERIFIED until independent QA, a security red-team pass and the release reviewer record their own
evidence.

## What was built
- Migration `0008_stage4_platform`: eleven tenant tables (subscriptions, usage counters, assistant sessions/messages,
  tool invocations, workflow definitions/versions/runs/steps, AI usage, kill switches), ENABLE + FORCE RLS; the worker
  scope is added only on invocations/runs/steps. Up/down/up verified on an empty database.
- Services `app/services/platform/`: `entitlements` (plans, metered counters with atomic upsert, capacity checks,
  test-mode billing adapter), `killswitch`, `tools` (registry of nine tools with scopes, permissions, strict argument
  models and approval flags), `invocations` (propose → approve → execute with policy re-checks, dry run, redacted
  result summaries), `assistant` (rule planner by default, optional consent-gated hosted planner, step/turn/daily
  budgets, per-step kill-switch checks, redaction before storage, usage traces without content), `workflows`
  (validated versioned definitions, dry-run executor, live runner with approval pauses, retries, depth budget,
  circuit breaker, compensation, event dispatch on `quote.approved` / `order.confirmed`).
- Plan limits wired into existing routes: seats (`POST /org/members`), RFQs per month (`POST /rfqs`).
- Router `routers/platform.py`; permissions `use_assistant`, `manage_workflows`, `kill_switch`, `manage_billing`, `view_ops`.
- Web: `/command-center`, `/assistant`, `/workflows`, `/platform`.
- Two existing auditor inventory tests updated (declared): `test_rls_enabled_and_forced_on_every_tenant_table` lists the
  Stage 4 tables; `test_ai_adapter_disabled_by_default_unwired_and_output_validated` now allows exactly one importer of
  the AI provider — `services/platform/assistant.py` — instead of none (Stage 1 had no AI-driven action; Stage 4 adds
  the consent-gated planner by design).

## Runs (local PostgreSQL 16, DB `quotepilot_s4`, worktree `/home/user/qp-stage4`)
| # | Command | cwd | Exit | Result |
|---|---|---|---|---|
| 1 | `ruff check app tests && mypy app` | apps/api | 0 | clean; "no issues found in 93 source files" |
| 2 | `pytest -q -p no:cacheprovider tests/integration/test_stage4_platform.py` | apps/api | 0 | **16 passed** (after fixing a relationship mapping, a savepoint conflict with the self-committing send gate, and four test assumptions) |
| 3 | `pytest -q -p no:cacheprovider` (all stages) | apps/api | 1 | 368 passed, 2 failed — the two auditor inventory tests above; after the declared updates both pass (re-run 2 passed) |
| 4 | `tsc --noEmit && eslint src && npm run build` | apps/web | 0 | clean |
| 5 | `npx playwright test` (DB quotepilot_s4, ports 8064/3064) | tests/e2e | 0 | **35 passed, 1 skipped** (incl. `stage4-platform.spec.ts`, 2 tests) |
| 6 | `alembic upgrade head` / `downgrade 0007` / `upgrade head` | apps/api | 0 | reversible |

## Not done / honest gaps
- No independent QA, security red-team or release review yet (gate INCOMPLETE).
- The hosted planner was never exercised against a real model (no paid calls); only the policy gate around it is tested.
- Onboarding wizard, in-app help pages and demo-workspace reset (S4-F06) are not built.
- This branch does not yet contain Stage 1 fix round 6 or the Stage 2/3 fix rounds; it must be merged forward.
