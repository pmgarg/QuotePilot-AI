# Implementer evidence — Stage 2 (sales follow-up assistant), branch `claude/stage2-followup-assistant`

Scope delivered (PRD S2-F01..F11): lifecycle transitions with mandatory evidence, contacts with opt-out and
preferences, organisation follow-up policy with explicit automation consent, rule-based recommendations with reasons,
template-driven editable drafts with guaranteed opt-out text, explicit manual send and consented automated send
through one gate (opt-out, recipient validation, hourly rate limit, send idempotency), simulator + SMTP providers,
AES-GCM envelope encryption for integration secrets, PostgreSQL-backed job queue with capped backoff and dead letters,
inbound linking (reply → acknowledged; unsubscribe → opt-out + schedule suppression), timeline, follow-ups page,
settings UI, contacts UI. Gmail/Graph adapters documented, not shipped.

## Runs (cwd apps/api unless stated; APP_ENV=test; DB quotepilot; worktree /home/user/qp-stage2)
| Command | Result |
|---|---|
| `alembic` chain from empty schema 0001→0005, `downgrade -1`, `upgrade head` | OK |
| `ruff check app tests && ruff format --check app tests && mypy app` | clean / Success: no issues in 72 files |
| `.venv/bin/pytest -q -p no:cacheprovider` (all Stage 1 suites incl. QA/security + 12 Stage 2 tests) | **305 passed** (304 + the grant test after re-applying 0004/0005 in order) |
| `npx tsc --noEmit && npx eslint . && npm run build` (apps/web) | clean, build OK |
| `npx playwright test` (tests/e2e; Stage 1 specs + `stage2-followup.spec.ts`) | see final run below |

Acceptance coverage: S2-A01 (`test_draft_generation_and_manual_send_is_explicit`), S2-A02
(`test_opted_out_contact_is_never_messaged`, browser), S2-A03 (duplicate send → single provider message),
S2-A04 (`test_cross_tenant_contacts_drafts_schedules_are_404`), S2-A05 (`test_integration_secrets_encrypted_and_revocation_handled`),
S2-A06 (`test_provider_outage_retries_with_backoff_then_dead_letters`), S2-A07 (`test_user_can_see_and_cancel_or_reschedule_pending_followups`, browser),
S2-A08 (`test_logs_redact_addresses_and_bodies`). Not yet independently QA'd or security-audited; no release review.
