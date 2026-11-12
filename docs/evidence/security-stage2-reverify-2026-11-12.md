# Stage 2 security re-verification — 2026-11-12

Auditor: security-auditor subagent. Tree: `/home/user/qp-stage2`, branch `claude/stage2-followup-assistant`, HEAD `43ec762`.
Scope: narrow, checklist-driven re-check of S2-SEC-01..11 after fix rounds 1 and 2 and of the two regexes changed for
CodeQL `py/polynomial-redos`. Local only, database `quotepilot_sec2`. No product code changed; nothing committed.
This is not a statement that Stage 2 has no vulnerabilities; section "Not checked" lists what was not tested.

## Checklist

| # | Item | Verdict | Evidence |
|---|---|---|---|
| 1 | S2-SEC-01 (+07, +11) run-once / worker scope | **CLOSED** | `set_worker_scope` callers: only `services/jobs/queue.py:111,152` (and its definition in `db/session.py`); both are reached only when `org_id is None`. `run_jobs_once` (`routers/followup.py:~620`) passes `org_id=` to `enqueue_due_schedules` and `run_once`, uses `set_tenant`, caps at `RUN_ONCE_MAX_JOBS=25`, requires MANAGE_ORG, is 403 unless `app_env in (development, test)` (`config.dev_endpoints_enabled`, so staging is off) and writes `worker.run_once` (07). Tests: `T::test_run_once_request_from_one_tenant_touches_only_that_tenants_queue`, `T::test_dev_only_endpoints_are_disabled_in_staging` / `_in_production`, new `C::test_run_once_is_capped_scoped_and_audited` (cap 1 -> two calls needed; two audit rows; sales 403). `sync-inbound` audit: `T::test_sync_inbound_and_run_once_write_audit_rows` passes. |
| 2 | S2-SEC-02 address-level suppression | **CLOSED** (normalisation gap noted) | Normalisation = `lower()` + `strip()` of the whole address, compared with `lower(contacts.email)` tenant-wide (`contacts_with_address`). Case: equal (`C::test_suppression_follows_the_address...`: opt-out from `Buyer@Customer.EXAMPLE`, re-created `BUYER@customer.example` is refused at draft creation, 403). Surrounding blanks / trailing newline: refused at input (422), so never stored. Re-created contact and second contact with the same address: opted out together (`set_address_opt_out`, `inherit_address_opt_out`, `T::test_unsubscribe_reply_opts_out_every_contact_row_with_that_address`). Plus-tags and dots are NOT equal (`C::test_plus_tag_and_dot_variants_are_different_addresses_pinned`): `buyer+x@` and `b.uyer@` are not suppressed. That is a deliberate/standard choice (they are distinct mailboxes for most providers) but it is a bypass for a user who wants to receive again only if a sales user adds a tag-variant contact; backlog, see O-1. Send-time re-check: `send_draft` takes `FOR UPDATE` on the draft and on the policy row, then calls `block_reason` per address on every path; `C::test_opt_out_recorded_after_the_draft_exists_blocks_the_send` (403, draft `cancelled`, `draft.suppressed` audited, outbox empty). |
| 3 | S2-SEC-03 starvation / failed schedules | **CLOSED** | `enqueue_due_schedules` pages by keyset `(due_at, id)` past schedules that already have a job (scan_cap 5000, limit 200 per call), per-tenant with `org_id`; `run_job` -> `_fail_schedule` sets the schedule to `failed` with a reason on terminal failure. `T::test_dead_lettered_schedules_are_resolved_and_do_not_starve_other_tenants` passes (200 stuck rows in A, B's follow-up still enqueued). Residual: more than 5000 simultaneously pending-with-queued-job rows older than another tenant's due row would still delay it for one tick (O-2). |
| 4 | S2-SEC-04 opt-out instruction | **CLOSED** | `send_draft` calls `tpl.ensure_opt_out_for(draft.body, org.name)` after all checks and before the provider call; it appends the exact instruction unless that exact string is present. `C::test_opt_out_instruction_survives_hostile_edits`: six hostile bodies (none, zero-width split of UNSUBSCRIBE, no-break spaces, upper-cased, HTML comment / `<br>` splitting, other org name) all end up with the exact text; end to end the sent message contains it; `{nope}` -> 422. Exact text hidden inside an HTML comment would pass the idempotency check but the provider sends `text/plain`, so it stays visible (O-3). |
| 5 | S2-SEC-05 | **MITIGATED** | `record_inbound`: a quote-number link needs sender = contact of the quote's customer or `in_reply_to` = one of the quote's own outbound message ids, else the event stays `unlinked` and nothing changes (`T::test_inbound_from_an_unknown_address_does_not_change_quote_status` passes). Residual by code reading (not exercised): the `thread` path (`in_reply_to` only, no quote number) has no sender check, and an outbound Message-ID is visible in any forwarded mail (O-4). Inbound is simulator-only today. |
| | S2-SEC-08 | n/a | No finding with this id exists in the first audit's table (numbering skips 08). |
| | S2-SEC-09 | **CLOSED** | `valid_email` uses `fullmatch`; `T::test_contact_addresses_with_a_trailing_newline_are_rejected`; `C::test_valid_email_rejects_control_characters_and_empty_labels` (`\n \r \x00 \t`, newline in local part, `a@b..example`). |
| | S2-SEC-10 | **CLOSED** | `PolicyUpdate.sender_name` rejects control characters (`schemas/common.py:451`); `clean_header_text` exists for the provider; `T::test_crlf_in_admin_sender_name_cannot_inject_headers`. |
| 6 | S2-SEC-06 | **ACCEPTED** (still open as documented) | `docs/KNOWN_LIMITATIONS.md:150` documents it; `provider_for` still builds `SMTPProvider` from process `SMTP_*`; `T::test_smtp_provider_uses_the_tenants_stored_credential` is the only remaining strict xfail in the audit file and still XFAILs for the original reason. |
| 7 | Consent re-check, D-13 | **CLOSED** | `process_due_schedule`: `auto_send` + no consent -> `schedule.mode=draft_only`, `status=drafted`, audit `followup.degraded_to_draft`, no send (`T::test_automation_consent_is_rechecked_at_send_time`: empty outbox, no `draft.sent`). `send_draft(mode="automation")` re-checks `automation_enabled and automation_consented_at` under the policy row lock. `C::test_queued_automated_send_without_consent_is_refused_terminally`: job -> `dead` after exactly 1 attempt, outbox empty, draft not sent. Paths that send: manual (`actor` required, user-triggered), schedule (consent re-checked), `send_draft` job (consent re-checked); no other `provider.send` call site in `service.py`. Manual sends intentionally do not need automation consent. |
| 8 | Declared test edits | **ACCEPTED** (see below) | |
| 9a | Regex linearity | **CLOSED** | Measured below; `C::test_bounded_address_patterns_are_linear_on_hostile_input` (200 000 chars, 5 shapes, bound 2 s). |
| 9b | Redaction regression | **OPEN, Low** | S2-SEC-12 below. |
| 9c | `valid_email` regressions | **CLOSED** | Nothing realistic newly rejected, see below. |

## Item 8 — edits to the predecessor's tests
`git log -- apps/api/tests/security/test_stage2_audit.py`: `3cacb04` (added), `abf1c03` (round 1), `a17210e` (round 2).
Diff `3cacb04..HEAD`, judged:
- Ten strict xfail markers removed (S2-SEC-01, 02a, 02b, 03, 04, 05, 07, 09, 10, 11): each test body is byte-identical to the
  auditor's, only the marker is gone, and each now passes. Accepted: the original assertions still encode the property.
- `test_worker_scope_has_no_caller_outside_the_queue_module`: the static assertion that the router calls the unscoped
  `queue.run_once(db)` was inverted to require `org_id=`-scoped calls and forbid the unscoped forms. Still proves the property
  (the string form is brittle, but run-once behaviour is also covered behaviourally). Accepted.
- `test_automation_consent_is_rechecked_at_send_time`: "no job succeeds" became "every `schedule_followup` job succeeds and
  schedules are `draft_only`, not `sent`". The security property (nothing sent: outbox, audit, drafts) is asserted by the
  untouched lines. Accepted. Weakness: the `all(...)` over jobs could be vacuous; the schedule assertion and outbox lines cover it.
- `test_dead_lettered_schedules_...`: "a job is `dead`" became "no job is `dead`" and the comment on the second
  `enqueue_due_schedules` changed. The 200-row starvation scenario, which carries the property, is unchanged. Accepted.

## Item 9 — measurements (Python 3.13, `.venv`, seconds)
| Input shape | N=1k | N=10k | N=100k |
|---|---|---|---|
| `a`*N (log `_EMAIL`) | 0.0002 | 0.0013 | 0.0104 |
| `a.`*N/2 | 0.0001 | 0.0014 | 0.0104 |
| `x@` + `a.`*N/2 | 0.0002 | 0.0014 | 0.0107 |
| `x@` + `-`*N + `!` | 0.0001 | 0.0013 | 0.0107 |
| (70 chars + `@`) repeated (worst, `scrub_error`) | 0.0012 | 0.0103 | 0.137 |
Old `_EMAIL` on the first shapes: 0.0008 / 0.080 s at 1k / 10k (quadratic, ~100x at 10k). New: linear (10x size -> ~10x time).
`valid_email`/`fullmatch` is ~0 (length cap 320 first).

Redaction, new vs old (`failed for <addr> now`):
- Realistic (`john.o'neil+tag@mail.example.co.uk`): both `[EMAIL]`.
- Local part 65 chars: new leaves the first character (`a[EMAIL]`); 100 chars: leaves 36 characters of the local part. Label of 64 chars
  (`bob@<64 x l>.example`): **not redacted at all**; 70-char label in the middle: 7 characters of it remain. More than 8 domain labels
  after the first: `.example` tail remains. The old pattern removed all of these.
- New is better for digit TLDs (`x@a.c0m` fully redacted; the old one left `.c0m`).
How bad: all of these are invalid under RFC 5321 (local <= 64, label <= 63) so ordinary contact data cannot trigger it
(`valid_email` rejects them); it needs hostile or corrupt text reaching a log line or `scrub_error`, e.g. a provider echoing an
attacker-supplied inbound `From`. Impact: a PII fragment or one whole malformed address in a log/`last_error`. Low, backlog.
`valid_email` newly rejects: `a@b..com` (intended), local part > 64, label > 63, > 9 domain labels, TLD > 24 letters; none
realistic. Unchanged from before: TLDs with digits/hyphen (punycode, e.g. `xn--p1ai`) and one-letter TLDs are rejected; `a@-b.example` is accepted.

## Runs
| Command (from `apps/api`, env as mandated) | Exit | Result |
|---|---|---|
| `.venv/bin/pytest -q -p no:cacheprovider tests/security/test_stage2_audit.py -rxX` | 0 | 30 passed, 1 xfailed (S2-SEC-06), 51.6 s |
| `.venv/bin/pytest -q -p no:cacheprovider tests/security/test_stage2_confirm.py -rxX` | 0 | 8 passed, 1 xfailed (S2-SEC-12), 11.2 s |
| both files together | 0 | 38 passed, 2 xfailed, 56.5 s |
| `.venv/bin/ruff check tests/security/` and `.venv/bin/ruff format --check tests/security/test_stage2_confirm.py tests/security/test_stage2_audit.py` | 0 / 0 | all checks passed; 2 files already formatted |
| ad-hoc timing/redaction script (scratchpad, not committed) | 0 | numbers in the table above |
Not run by instruction: full API suite, Playwright, `make security-scan` (recorded by the implementing side and CI).

## New finding
**S2-SEC-12 — Low, backlog (not release-blocking).** Log redaction and `scrub_error` leave part or all of an over-long address.
Repro: `redact("provider rejected " + "u"*100 + "@customer.example")` returns `...uuuu(36)[EMAIL]`; `redact("bob@" + "l"*64 + ".example")`
is unchanged. Test: `C::test_log_redaction_covers_over_long_address_parts` (strict xfail). Fix: keep the bounded, linear shape but
make the redactor a superset, e.g. `[^\s@<>"',;]{1,254}@[^\s@<>"',;]{1,255}` (bounded, no nested quantifier) for logging/`scrub_error`
while `EMAIL_RE` stays strict for validation; one shared constant for both redactors.

## Everything still open for Stage 2
| ID | Severity | Status |
|---|---|---|
| S2-SEC-06 per-tenant provider secret stored, unused; shared SMTP account | Low (honest status) | Accepted, in KNOWN_LIMITATIONS, strict xfail |
| S2-SEC-12 over-long address parts survive log redaction | Low | Open, backlog, strict xfail |
| O-1 plus-tag / dot variants not suppressed with the opted-out address | Low (design) | Pinned by `C::test_plus_tag_and_dot_variants...`; decide policy |
| O-4 `in_reply_to`-only inbound linking has no sender check (S2-SEC-05 residual) | Low today, Medium when a real inbound adapter ships | Open, backlog |
No Critical or High finding is open from this pass; nothing here is release-blocking in my judgement.

## Observations (one line each)
- O-2 `scan_cap=5000` bounds the starvation fix; an extreme backlog of queued-job schedules could still delay another tenant by one tick.
- O-3 The opt-out check is an exact-substring test; a customised/hidden copy inside HTML-looking text would not be re-appended (plain-text mail, so visible).
- O-5 Placeholders with other casing or characters (`{Foo}`, `{a-b}`) do not match `[a-z_]+`, so they are neither rejected nor resolved and are sent literally (cosmetic).
- O-6 `a@-b.example` / `a@b-.example` are accepted by `valid_email` (unchanged); punycode TLDs rejected (unchanged).
- O-7 An untracked `apps/api/tests/qa/test_qa_stage2_confirm.py` (QA's, not mine) is in the worktree; I did not touch it.
- O-8 `_fail_schedule` only acts for `schedule_followup` jobs; a dead `send_draft` job leaves the draft `queued`/`draft` (no starvation effect).

## Not checked
Cross-tenant matrix and RLS for Stage 2 tables beyond what the passing audit tests cover; cookie/CSRF flags (Stage 1 control; the
audit file asserts CSRF on Stage 2 mutations and passed); file/parser limits; Stage 1 items; `pip-audit`, `npm audit`, `bandit`,
`gitleaks`, `semgrep` (scan not run by instruction); secrets in the web bundle; concurrency races beyond the existing audit tests;
real SMTP/Gmail/Graph adapters; LLM prompt-injection fixtures (Stage 2 has no model in the send path); the migration (`0006b`)
and the browser UI; the O-4 and O-1 behaviours were read from code / pinned but not driven end to end.
