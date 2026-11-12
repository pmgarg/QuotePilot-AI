# Implementer evidence: Stage 2 fix round 1 (2026-11-10)

Scope: QA defects D-01..D-15 (`qa-stage2-2026-11-02.md`) and security findings S2-SEC-01..11
(`security-stage2-2026-11-02.md`). Not a release verdict. DB `quotepilot_s2` (non-owner app role, RLS enforced).

## Finding -> change -> test
| Finding | Change | Test |
|---|---|---|
| D-01, S2-SEC-02 opt-out per row | Suppression keyed on lower(e-mail) in the tenant (`address_block_reason`, `set_address_opt_out`, `inherit_address_opt_out`); checked in draft creation, send gate, planning, worker; create/update contact inherits; opt-out/opt-in act on all rows with the address; migration 0006b (renumbered from 0007 to avoid colliding with the Stage 3 migration) adds `lower(email)` index | QA `..per_address_not_per_contact_row`; security `opt_out_follows_the_address..`; new `test_opt_out_blocks_every_row..`, `..opt_in_clears_the_address`, `..editing_a_contact_to_an_opted_out_address..` |
| D-02 unsubscribe reply opts out one row | `record_inbound` opts out every row of the address, suppresses their schedules | QA `..opts_out_every_contact_row..`; security `test_unsubscribe_reply_opts_out_every_contact_row..`; new `test_unsubscribe_reply_opts_out_all_rows..` |
| D-03 concurrent sends | Lock re-reads with `populate_existing`; policy row locked too (serialises hourly limit, audit I-7) | QA `concurrent_duplicate_sends..`; new `test_stale_draft_object_cannot_send_twice` (verified to fail without the fix), `test_many_concurrent_clicks..` |
| D-04 `preferred_channel=none` | Same address-level block (reason `no_follow_ups`, 403) on manual, schedule, automation | QA `contact_preferring_no_follow_ups..`; new `test_no_follow_ups_preference_*` |
| D-05, S2-SEC-04 footer removable | `ensure_opt_out_for` applied at send (idempotent) | QA `edited_draft_is_sent_with_opt_out_text`; security `test_every_sent_message_carries..`; new `test_edited_draft_always_ends_with_the_opt_out..`, `test_user_supplied_opt_out_placeholder..` |
| D-06 placeholders | Resolved once when an edit is saved (subject and body), unknown ones 422; stored text is final (no second expansion of customer data) | QA `placeholders_in_an_edited_draft..`; security `templates_whitelist..` (kept green) |
| D-07 max_attempts | `process_due_schedule` counts manual follow-ups since the send anchor; supersedes with reason | QA `automation_respects_max_attempts..`; new `test_automation_never_exceeds_max_attempts..` |
| D-08 | `send_draft` plans schedules after approved->sent | QA `quote_sent_through_the_system..`; new `test_system_send_of_an_approved_quote..` |
| D-09 | Schedule key includes version; anchor = latest `sent` status event | QA `new_version_from_sent_quote..`; new `test_new_version_plans_fresh_schedules..` |
| D-10 | `_next_allowed` rewritten (always quiet-end, same-day windows); attempts forced >= 1 day apart | QA `quiet_hours_and_weekends..`; new `test_follow_up_schedule_times_are_ordered..`, `test_next_allowed_*` |
| D-11 | Replay lookup covers `opt_out` and `reply_received` | QA `unsubscribe_processing_is_idempotent..`; new `test_replayed_inbound_message_is_idempotent..` |
| D-12, I-1 | "stop" only when it is the whole subject/snippet | QA `ordinary_reply_containing_the_word_stop..`; new parametrized `test_unsubscribe_detection_is_strict..` |
| D-13 partly | Policy refusals (AppError except RateLimited) are terminal: job `dead` at once, schedule `failed` with reason, admin retry re-arms. NOT fixed: QA wants a draft instead (see below) | new `test_policy_refusal_is_not_retried..`, `test_rate_limit_is_still_retried..` |
| D-14 | Provider-config failure stored on the connection, audited, committed before re-raise; errored connection still selects its provider | QA `sync_inbound_with_misconfigured_provider..`; new `test_misconfigured_provider_sync..` |
| D-15 | `to_email` must be the draft's own contact (422) | QA `recipient_validation..`; new `test_draft_recipient_must_be_the_drafts_own_contact` |
| S2-SEC-01 | `run-once` uses tenant scope only (`org_id=`), max 25 jobs; no `set_worker_scope` on request sessions | security `test_run_once_request..`; new `test_org_scoped_tick_and_claim..`, `test_run_once_endpoint_processes_only..` |
| S2-SEC-03 | Terminal job failure sets schedule `failed`; tick pages (keyset) past schedules that already have a job | security `test_dead_lettered_schedules..`; new `test_stuck_pending_schedules_with_jobs..` |
| S2-SEC-05 | Quote-number link needs a contact of the quote's customer or an answer to our message, else unlinked | security `test_inbound_from_an_unknown_address..`; new `test_quote_number_from_an_unknown_sender..` |
| S2-SEC-07 | Audit rows `integration.sync`, `integration.sync_failed`, `worker.run_once` | security `test_sync_inbound_and_run_once_write_audit_rows` |
| S2-SEC-09 | `fullmatch` in `valid_email` | security `test_contact_addresses_with_a_trailing_newline..`; new `test_valid_email_rejects..` |
| S2-SEC-10 | `sender_name` control chars -> 422; SMTP header ValueError -> non-retryable ProviderError | security `test_crlf_in_admin_sender_name..`; new `test_sender_name_with_control_characters..`, `test_smtp_header_refusal..` |
| S2-SEC-11 | `dev_endpoints_enabled` = development/test only; UI button hidden via policy flag | security `test_dev_only_endpoints_are_disabled_in_staging`; new `test_dev_endpoints_flag_follows_app_env` |
| O-1, O-2, O-3, I-7, I-8 | backoff exponent cap; stored errors scrubbed of addresses; simulator `from_email` and integration `account_email` validated; policy lock | new `test_backoff_is_capped..`, `test_stored_error_text..`, `test_simulator_inbound_validates..`, `test_integration_account_email_is_validated` |
| Flawed tests | `or True` replaced by an audit-row assertion; e2e opt-out click targets the row by e-mail | `test_stage2_followup.py` (cancel test), `stage2-followup.spec.ts` |

## Strict xfail markers removed (now passing), all in `tests/security/test_stage2_audit.py`
S2-SEC-01 `test_run_once_request_from_one_tenant_touches_only_that_tenants_queue`; S2-SEC-07 `test_sync_inbound_and_run_once_write_audit_rows`;
S2-SEC-02 `test_opt_out_follows_the_address_when_a_contact_is_recreated` and `test_unsubscribe_reply_opts_out_every_contact_row_with_that_address`;
S2-SEC-04 `test_every_sent_message_carries_the_opt_out_instruction`; S2-SEC-03 `test_dead_lettered_schedules_are_resolved_and_do_not_starve_other_tenants`;
S2-SEC-05 `test_inbound_from_an_unknown_address_does_not_change_quote_status`; S2-SEC-10 `test_crlf_in_admin_sender_name_cannot_inject_headers`;
S2-SEC-09 `test_contact_addresses_with_a_trailing_newline_are_rejected`; S2-SEC-11 `test_dev_only_endpoints_are_disabled_in_staging`.

## Not fixed / disputed
- **S2-SEC-06** (stored tenant SMTP secret unused): accepted; needs per-tenant host/user config. Strict xfail kept; documented in KNOWN_LIMITATIONS.
- **QA D-13 test** `test_qa_s2_f05_withdrawing_consent_after_planning_degrades_to_draft_only` still FAILS (not edited). It requires the job not to be dead/failed and the schedule `drafted`. In the same scenario the passing security test `test_automation_consent_is_rechecked_at_send_time` requires that no job succeeds, and the (formerly xfail) `test_dead_lettered_schedules_are_resolved..` requires a `dead` job. They cannot all hold; I followed the security tests and the brief (refusals are terminal, schedule leaves `pending` with a visible error). The "nothing is sent" half of D-13 holds. Coordinator decision needed.
- One existing security test encoded the vulnerable state (`test_worker_scope_has_no_caller_outside_the_queue_module` asserted the unscoped `queue.run_once(db)` call exists in the router); updated to assert the tenant-scoped calls instead.
- Unsubscribe from an address with no contact row is not retained as suppression (events keep no raw addresses); documented.
- O-4..O-8, I-2..I-6, I-9..I-11 not changed (info/low, no cheap safe fix).

## Runs (cwd `apps/api` unless noted; DB `quotepilot_s2`)
| Command | Exit | Result |
|---|---|---|
| `pytest -q -p no:cacheprovider tests/integration/test_stage2_fixes1.py` | 0 | 51 passed (30.35 s) |
| `pytest -q -p no:cacheprovider` (full) | 1 | 476 passed, 1 failed (QA D-13, above), 1 skipped (NEW-13), 1 xfailed (S2-SEC-06); 384.99 s |
| `ruff check app tests` / `ruff format --check app tests` / `mypy app` | 0 / 0 / 0 | all passed; 106 files formatted; 72 source files clean |
| `apps/web`: `npx tsc --noEmit`, `npx eslint .`, `npm run build` | 0 / 0 / 0 | clean; build succeeded |
| `tests/e2e`: `npx playwright test` (DB truncated first, ports 8073/3073) | 0 | 37 passed, 1 skipped (screenshots), 2.2 min |
`docs/openapi.json` regenerated (`dev_endpoints_enabled` on the policy). No servers left running.
