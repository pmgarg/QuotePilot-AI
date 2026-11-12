# Security audit evidence — Stage 2 (sales follow-up assistant), 2026-11-02

Auditor: security-auditor subagent (independent of the Stage 2 implementer; did not implement Stage 2). Worktree
`/home/user/qp-stage2`, branch `claude/stage2-followup-assistant`, audited at **`cf8d409`** ("Merge Stage 1 fix round 5
into Stage 2"; the worktree additionally carries `e851ceb`, an evidence-only commit with the scan report
`scan-20261102-004612.md`). Product code was **not** modified. The auditor added only
`apps/api/tests/security/test_stage2_audit.py` and this document. Nothing was committed.

This document states what was tested, how, and what was found. It does not claim the absence of vulnerabilities and
does not certify Stage 2 as secure; the release gate remains a human decision informed by the independent reviewer.

**Concurrent working-tree files observed (not the auditor's, not relied upon):** `apps/api/tests/qa/test_qa_stage2_*.py`
(4 files) and `tests/e2e/specs/qa-stage2-*.spec.ts` (2 files) — the QA engineer working in parallel.

## 1. Scope and methodology

| Area | What was reviewed / exercised |
|---|---|
| Code review (every line) | `app/api/routers/followup.py`, `app/services/followup/{service,templates,recommend}.py`, `app/services/comms/{providers,secrets}.py`, `app/services/jobs/queue.py`, `app/services/lifecycle.py`, `app/workers/runner.py`, `app/models/followup.py`, `app/schemas/common.py` (Stage 2 block), `alembic/versions/20261101_0006_stage2_followup.py`, `app/db/session.py`, `app/core/{logging,config}.py`, `app/main.py`, `apps/web/src/components/{followup,contacts}.tsx`, `apps/web/src/app/(app)/follow-ups/page.tsx`, `infra/docker-compose.yml` (`worker`), `.env.example`; the Stage 1 files touched by Stage 2 (`main.py`, `logging.py`, `session.py`, `config.py`, `models/__init__.py` — diff `597f4b0..cf8d409`) |
| Black-box tests | `tests/security/test_stage2_audit.py` (31 tests, FastAPI `TestClient`, two tenants with one user per role, raw SQL as the `quotepilot_app` role under tenant / no-tenant / `app.worker` contexts, direct calls of the queue and worker functions, a fake `smtplib.SMTP`) against a dedicated PostgreSQL 16 database `quotepilot_sec`, migrated from an empty schema (`0001 → … → 0005b → 0006`) by the test session fixture |
| Live probe | `uvicorn :8072` + `next start :3072` (`APP_ENV=test`, seeded with `python -m app.fixtures.seed`, synthetic orgs only) through the web proxy: cookie flags, security headers on Stage 2 routes, CSRF / RBAC on `run-once`, simulator inbound, log redaction of the live API log. Servers stopped afterwards; ports verified closed |
| Scanners | pip-audit, bandit (`-ll` and all severities for the Stage 2 modules), ruff (project config and `--select S`), npm audit (web prod/full, e2e), web-bundle secret grep, compose config render. **gitleaks, semgrep, trufflehog, hadolint are not installed locally (CI-only)**; no Docker daemon (compose rendered, not executed) |
| Checklists | OWASP ASVS 5.0 V2/V3/V4/V5/V14 (+ V13 API), OWASP LLM Top 10 not applicable (no model call in Stage 2), `docs/THREAT_MODEL.md` row "Stage 2 providers (B6)", master prompt § Stage 2 ("explicit authorized user action or separately enabled, consented workflow approved by an organization admin, with rate limits, recipient validation and tenant-scoped audit logs"; "opt-out controls"; acceptance "opted-out contact never receives an automated message") |

Focus questions from the brief and their answers are in § 3 (per-control table). Only the local application built
from this repository was tested; no third-party host, account or paid service was used.

## 2. Environment

| Item | Value |
|---|---|
| Host | Claude Code cloud container, Linux 6.18, Python 3.13.18 (`apps/api/.venv`), Node 22, PostgreSQL 16 (local), SQLAlchemy 2.0.55, FastAPI 0.162.0, cryptography 50.0.2, Next 16.4.3 |
| Database | `quotepilot_sec` — empty before the audit; `alembic upgrade head` by the session fixture → `0006 (head)`; per-test `TRUNCATE … CASCADE` as the owner role; application queries as `quotepilot_app` (RLS enforced, `FORCE ROW LEVEL SECURITY` verified in `pg_class` for all 9 Stage 2 tables) |
| Env | `APP_ENV=test`, `DATABASE_URL_TEST`/`DATABASE_ADMIN_URL_TEST` → `quotepilot_sec`, `SECRET_KEY` test value, `STORAGE_DIR` temp; production/staging behaviour tested with a fresh `create_app()` after `APP_ENV=production|staging`, a random 64-char `SECRET_KEY` and `COOKIE_SECURE=false` |
| Not available | gitleaks, semgrep, trufflehog, hadolint, Docker daemon, external network (none needed) |

## 3. Per-control verification

Status: **OK** (verified by a passing test / observation), **FINDING** (see § 4), **PARTIAL**, **NOT TESTED**.
`T` = `tests/security/test_stage2_audit.py`.

| Control (brief question) | Evidence | Status |
|---|---|---|
| RLS on all 9 Stage 2 tables (`contacts, follow_up_policies, message_templates, quote_status_events, follow_up_schedules, communication_drafts, communication_events, integration_connections, job_runs`): SELECT/UPDATE/DELETE from another tenant's context touch 0 rows; INSERT of a foreign `organization_id` is refused by WITH CHECK; no-tenant context sees nothing; RLS enabled **and forced** | `T::test_rls_isolates_every_stage2_table_for_the_app_role` — `pg_class.relrowsecurity/relforcerowsecurity = (t,t)` for all 9; counts A ≥ 1, B = 0, none = 0; UPDATE/DELETE rowcount 0 from B; INSERT → "new row violates row-level security policy" | OK |
| `app.worker` scope widens exactly `follow_up_schedules` and `job_runs`, nothing else | same test: with `set_worker_scope` the app role reads those two tables across tenants and 0 rows of the other seven | OK |
| Is the worker scope transaction-local? Is it absent inside job bodies? Can a job body read another tenant? | `T::test_worker_scope_is_transaction_local_and_absent_inside_job_bodies` — after `claim_next` commits, `current_setting('app.worker', true)` is empty and `job_runs` is invisible; inside the handler `app.worker` = '' and `app.current_org` = the job's org; foreign contacts/jobs = 0 | OK |
| Can an API request path ever run with the worker scope? | `T::test_worker_scope_has_no_caller_outside_the_queue_module` (static: only `services/jobs/queue.py` calls `set_worker_scope`) **but** `POST /jobs/run-once` calls `queue.enqueue_due_schedules(db)` / `queue.run_once(db)` on the request session → `T::test_run_once_request_from_one_tenant_touches_only_that_tenants_queue` (strict xfail) | **FINDING S2-SEC-01** |
| Cross-tenant object routes return 404 (no existence oracle); list filters leak nothing; a draft cannot target a foreign contact or address | implementer's `test_cross_tenant_contacts_drafts_schedules_are_404` (11 routes) + `T::test_cross_tenant_remaining_stage2_routes_are_404_and_filters_are_empty` (reschedule, draft cancel, plan, template PATCH, job retry, integration revoke, contacts GET/POST, opt-in, transition; `schedules?quote_id`, `communication-events?quote_id`, `/jobs`, `/integrations` empty; foreign `contact_id` → 404; foreign `to_email` → 422) | OK |
| RBAC: readonly denied on 18 mutations; sales/approver denied on policy, templates, jobs, integrations, sync, simulator, run-once; opt-in owner/admin only; denied calls have no side effects | `T::test_rbac_matrix_for_stage2_routes`; live probe: sales → `run-once` 403 `manage_org` | OK |
| CSRF on every Stage 2 mutation (missing and wrong header) | `T::test_csrf_is_enforced_on_stage2_mutations` (9 routes × 2); live probe through the Next proxy: `run-once` without header → 403 `csrf` | OK |
| 403 vs 404 ordering | role check precedes the tenant lookup (readonly gets 403 for a foreign id — reveals nothing about existence); in-tenant role denials 403, foreign objects 404 | OK |
| Every mutating endpoint writes an `audit_events` row in the same transaction | `T::test_every_stage2_mutation_writes_an_audit_row_in_the_same_transaction` — 20 actions verified (`contact.create/update/opt_out/opt_in`, `followup.policy_update/template_update/schedule/cancel/reschedule`, `quote.transition`, `draft.create/update/sent/cancel/suppressed`, `job.retry`, `integration.connect/revoke`, `inbound.reply_received/opt_out`); summaries carry no address/body. **Exceptions:** `POST /integrations/sync-inbound`, `POST /jobs/run-once` → `T::test_sync_inbound_and_run_once_write_audit_rows` (strict xfail) | **FINDING S2-SEC-07** |
| Opt-out enforced server-side on the manual send path | implementer's `test_opted_out_contact_is_never_messaged` (403 `opt_out`, draft `cancelled`, `draft.suppressed` audited) | OK |
| Opt-out enforced on the automated schedule path | implementer's test (worker sends nothing to an opted-out contact); `T::test_automation_consent_is_rechecked_at_send_time` | OK |
| Opt-out enforced on the queued **retry** path (opt-out after the job was queued; admin re-queue) | `T::test_opt_out_is_enforced_on_the_queued_retry_path` — outbox empty after 3 ticks and after `job.retry` | OK |
| Opt-out is bound to the **address**, not to a contact row (re-created / duplicate contacts) | `T::test_opt_out_follows_the_address_when_a_contact_is_recreated`, `T::test_unsubscribe_reply_opts_out_every_contact_row_with_that_address` (strict xfail) | **FINDING S2-SEC-02** |
| Automation needs owner/admin consent, is audited, and is re-checked at send time after revocation | `T::test_automation_consent_is_rechecked_at_send_time` — `auto_send` schedules planned under consent are not sent after `automation_enabled=false`; two `followup.policy_update` audit rows with actor; `automation_consented_at` cleared | OK (but see S2-SEC-03 for what happens to the schedule) |
| Hourly send limit | `T::test_hourly_limit_recipient_binding_and_provider_acknowledgement` — limit 1 → second send 429 `rate_limited`, outbox 1. Check-then-insert without a lock (concurrent sends of different drafts can slightly exceed the limit) — **not tested** (I-7) | OK / PARTIAL |
| Recipient validation: recipient must equal the draft's contact on record; changed contact address → 422 | same test | OK |
| Send idempotency / duplicate triggers | implementer's test (second `send` → 200, outbox still 1); `send_idempotency_key` + `FOR UPDATE` reviewed | OK |
| Provider acknowledgement before "sent" | same `T` test: retryable provider failure → 503, draft stays `draft`, no `sent` event; implementer's dead-letter test | OK |
| SKIP LOCKED claim correctness | `T::test_skip_locked_claim_hands_a_job_to_exactly_one_worker` — a row locked by worker 1 is skipped by worker 2, claimed after release, then `running` is not re-claimed | OK |
| Retries / backoff / dead letters bounded; unknown kinds cannot run; payloads hold ids only | `T::test_job_payloads_hold_ids_only_and_unknown_kinds_dead_letter_boundedly` — payload keys ⊆ {schedule_id, draft_id, idempotency_key}, no addresses/bodies; unknown kind dead after `max_attempts`; `JobOut` exposes no payload/lock fields; retry only for dead/failed (409 otherwise) | OK |
| Dead-lettered schedules are resolved and cannot starve the scheduler | `T::test_dead_lettered_schedules_are_resolved_and_do_not_starve_other_tenants` (strict xfail) | **FINDING S2-SEC-03** |
| Secrets: AES-256-GCM envelope, key length enforced, AAD binds org+provider, never in responses / audit summaries / plaintext DB, revocation wipes | `T::test_integration_secret_is_never_exposed_and_revocation_wipes_it` — 16-byte key → 422; 201 response and `GET /integrations` contain no secret; DB blob = key-id `0x01` + nonce + ct+tag, plaintext absent; `audit_events.summary` has only `has_secret`; wrong AAD fails; revoke → `NULL`; one active provider per tenant | OK |
| …but the stored secret is actually used for sending | `T::test_smtp_provider_uses_the_tenants_stored_credential` (strict xfail): `decrypt()` has no caller in `app/`; SMTP credentials come from process env for every tenant | **FINDING S2-SEC-06** |
| `TOKEN_ENCRYPTION_KEY` handling | missing → `EncryptionUnavailable` → 422 "TOKEN_ENCRYPTION_KEY is not configured" (implementer test); wrong length → 422 (`T`); compose `worker` receives it, `api` does not (I-5) | OK / note |
| Logging redaction: no addresses, bodies, secrets in logs (`S2-A08`) | `T::test_send_path_logs_contain_no_addresses_bodies_or_secrets` — full send + inbound flow captured through `JsonFormatter`: zero `@` outside `[EMAIL]`, no body text, `sk-ant-…`/JWT/`api_key` masked; live API log: 0 raw addresses, 0 password strings | OK |
| Templates: placeholder whitelist; no second-pass expansion of customer data; `<script`/`javascript:` refused; body ≤ 20 000; template keys are tenant-scoped lookups | `T::test_templates_whitelist_placeholders_and_values_are_not_re_expanded` | OK |
| SMTP header injection via customer data (contact name with CRLF into Subject), reply-to with CRLF, plain-text only | `T::test_smtp_headers_resist_crlf_injection_from_customer_data` — header block has no `Bcc`/`X-Injected`, Subject is one line, `text/plain`, opt-out present; reply-to with CRLF → 422 | OK |
| CRLF in the admin-controlled `sender_name` | `T::test_crlf_in_admin_sender_name_cannot_inject_headers` (strict xfail) — nothing is injected (EmailPolicy refuses) but the send returns **500** | **FINDING S2-SEC-10** |
| Recipient address validation | `T::test_contact_addresses_with_a_trailing_newline_are_rejected` (strict xfail) | **FINDING S2-SEC-09** |
| HTML escaping in the UI | no `dangerouslySetInnerHTML` / `innerHTML` under `apps/web/src`; drafts/timeline rendered as React text nodes; API bodies are `text/plain` | OK (static) |
| Inbound: spoofed replies cannot cross tenants; quote-number collisions across tenants stay tenant-scoped; thread ids of another tenant do not link | `T::test_inbound_processing_cannot_reach_other_tenants_contacts_or_quotes` | OK |
| Inbound: unsubscribe abuse — can anyone opt out someone else's contact? | simulator injection is `manage_org` + non-production (`T::test_simulator_inbound_is_admin_only_and_bounded`, implementer test); real providers: inherent sender spoofing, by design accepted for *opt-out* (fails safe) | OK / accepted |
| Inbound: a reply from a non-contact address that mentions the quote number acknowledges the quote and suppresses automation | `T::test_inbound_from_an_unknown_address_does_not_change_quote_status` (strict xfail) | **FINDING S2-SEC-05** |
| Non-production gating of `simulator/inbound` and `run-once` | `T::test_dev_only_endpoints_are_disabled_in_production` (403 `simulator` / `worker`, docs/openapi 404) — but `APP_ENV=staging` keeps them on: `T::test_dev_only_endpoints_are_disabled_in_staging` (strict xfail) | OK / **FINDING S2-SEC-11** |
| Lifecycle transitions: evidence mandatory for acknowledged/won/lost, `occurred_at` not in the future, optimistic lock, readonly 403 | implementer's `test_lifecycle_transitions_require_evidence_for_claims`; code review `lifecycle.py:TRANSITIONS` | OK |
| Stage 1 controls regression (tenant isolation, RBAC/CSRF/headers, parsers/logging, Stage 1 audit follow-ups 1–3) | full `tests/security` + `tests/integration/test_stage2_followup.py` on `quotepilot_sec`: **112 passed, 1 skipped, 11 xfailed (all 11 are this audit's strict finding markers), 0 failed**, exit 0 | OK |
| Security headers / `no-store` on Stage 2 routes; error bodies without internals | `T::test_security_headers_and_no_store_on_stage2_routes`; live probe via proxy (CSP from the web layer, `no-store`, `DENY`, `nosniff`) | OK |
| Cookie flags (Stage 1) via the proxy in `APP_ENV=test` | `qp_session … HttpOnly; SameSite=lax`, `qp_csrf … SameSite=lax` (no `Secure` in test by design) | OK |
| Web bundle secrets / dev button | `.next` build `vbJUZ3rtebN4gJZmgA1cq` newer than every `src` file; no `TOKEN_ENCRYPTION_KEY|SMTP_PASSWORD|sk-ant-|AKIA` match in `.next/static`; 0 `NEXT_PUBLIC_` references; the "Run worker once (dev)" button ships in the bundle (UI-side only; the API decides) | OK / note |
| Compose topology for the new `worker` | rendered config: `worker` has 14 allow-listed vars, `DATABASE_ADMIN_URL=''`, no owner password (NEW-16 discipline kept); `migrate` still inherits all 30 `.env` keys incl. `SMTP_PASSWORD`, `TOKEN_ENCRYPTION_KEY` (I-5); not executed (no daemon) | OK (static) |

## 4. Findings

Severity follows the project's scale (Critical/High/Medium/Low). None Critical or High. All reproductions are
strict xfails in `apps/api/tests/security/test_stage2_audit.py` (`T`), so a fix turns them into XPASS → failures
until the marker is removed.

| ID | Severity | Title | Location | Reproduction | Impact | Remediation |
|---|---|---|---|---|---|---|
| S2-SEC-01 | **Medium** | `POST /jobs/run-once` runs the cross-tenant worker inside an API request | `app/api/routers/followup.py:618-626` (calls `queue.enqueue_due_schedules(db)` and `queue.run_once(db)` on the request session), `app/services/jobs/queue.py:95-142` (`set_worker_scope` + claim across all tenants) | `T::test_run_once_request_from_one_tenant_touches_only_that_tenants_queue`: tenant B has consented automation and a due schedule; tenant A's admin calls `run-once` → **`{"processed": 1}`**, B's draft is created and sent through B's provider from A's request; A's own queue is empty. Live probe (`APP_ENV=test`): admin 200 `{"processed":0}`, sales 403, no CSRF 403 | An admin of any tenant (a) triggers other tenants' outbound e-mail at a time of their choosing, (b) learns the number of due jobs across the platform, (c) makes the request block until every claimable job on the platform has run (the loop is unbounded), (d) leaves no audit trail (S2-SEC-07). Exposure is limited to non-production deployments — which include `staging` (S2-SEC-11) — and the UI advertises the button to every org admin | Restrict the helper to the caller's tenant: enqueue only `principal.org_id` schedules and claim with `organization_id = principal.org_id` under the normal tenant scope (no `set_worker_scope` on a request session); cap the number of jobs per call; write an audit row; or drop the route and document `python -m app.workers.runner --once`. Add an architecture test that `set_worker_scope` is unreachable from `app/api` |
| S2-SEC-02 | **Medium** | Opt-out is bound to the contact **row**, not to the address — suppression can be bypassed by re-creating or duplicating a contact | `app/services/followup/service.py:342-345` (`send_draft` checks `draft.contact_id`'s row only), `:190-199` (`create_draft` idem), `:685-687` (`record_inbound` opts out the **first** matching row via `db.scalar`), `app/api/routers/followup.py:76-99` (`create_contact` has no suppression check; sales may create), `:147-153` (opt-in restricted to owner/admin — the restriction is bypassed by re-creation) | `T::test_opt_out_follows_the_address_when_a_contact_is_recreated`: customer replies UNSUBSCRIBE (`opt_out` recorded) → a **sales** user creates a second contact with the same address → draft 201 → **send 200, outbox = 1**. `T::test_unsubscribe_reply_opts_out_every_contact_row_with_that_address`: two rows for one mailbox (case-different) → UNSUBSCRIBE opts out one; the other stays eligible | The acceptance criterion "an opted-out contact never receives an automated message" holds for the row, not for the person. A recorded unsubscribe (a legal consent fact) is defeated by data entry a sales user can perform without the owner/admin opt-in privilege; duplicate rows (imports, two customer accounts for one buyer) keep receiving automated follow-ups after an unsubscribe | Keep an organisation-level suppression keyed on `lower(email)` (a `suppressions` table or an index-backed check across `contacts`), consulted by `create_draft`, `send_draft`, `plan_schedules` and `process_due_schedule`; have `record_inbound` and `set_opt_out` suppress every row with that address; make opt-in an explicit, audited per-address action; add a regression test to the integration suite |
| S2-SEC-03 | **Medium** (availability, cross-tenant) | Dead-lettered schedules stay `pending` forever and starve the shared scheduler tick | `app/services/jobs/queue.py:99-105` (`SELECT … WHERE status='pending' AND due_at <= now ORDER BY due_at LIMIT 200` across all tenants), `:109-118` (`enqueue` returns `None` for the existing idempotency key, so a dead job blocks its schedule for good), `:160-177` (every non-`ValueError`/`LookupError` exception — including `ForbiddenError`, `StateError` — is retried 5× then `dead`; the schedule row is never updated), `app/services/followup/service.py:650-653` (`auto_send` schedules call `send_draft(mode="automation")`, which raises when consent was revoked) | `T::test_dead_lettered_schedules_are_resolved_and_do_not_starve_other_tenants`: consent on → quote sent (schedules `auto_send`) → consent off → tick: job retried 5×, `dead`; schedule still `pending`; further ticks enqueue 0. Then 200 such rows in tenant A (inserted directly; the organic path above creates them one by one) → tenant B's legitimately due follow-up is **never enqueued** (`enqueue_due_schedules() == 0`). The same state arises organically whenever a tenant connects `smtp` while `SMTP_HOST` is unset (`ProviderNotConfigured`, non-retryable → job `failed`, schedule `pending`) | One tenant (deliberately, or by misconfiguration) stops automated follow-ups for **every** tenant; the follow-ups page keeps showing the stuck rows as pending (misleading status); consent/state refusals burn five retries each and fill the dead-letter view | On terminal job failure mark the schedule `failed`/`suppressed` with the reason; treat `AppError` subclasses as terminal (not retryable); when `automation_enabled` is switched off, flip pending `auto_send` schedules to `draft_only`; exclude schedules whose job is `dead`/`failed` from the tick (join on `job_runs`) or page past them; alert on dead letters per tenant |
| S2-SEC-04 | **Medium** (consent / compliance) | The guaranteed opt-out instruction can be removed before sending | `app/services/followup/service.py:223` (`ensure_opt_out` only at generation), `:258-260` (`update_draft` stores the edited body after `validate_template` only), `:390-397` (`send_draft` sends `draft.body` verbatim) | `T::test_every_sent_message_carries_the_opt_out_instruction`: PATCH body "Hi, just checking in on {quote_number}." → 200, body without UNSUBSCRIBE → send 200 → **provider message has no opt-out text** | The PRD/implementer claim "guaranteed opt-out text" holds only for unedited drafts; a routine edit (the UI offers a free-text body) produces a commercial follow-up without the unsubscribe mechanism the inbound opt-out relies on (also a regulatory exposure in the target markets) | Apply `ensure_opt_out` in `send_draft` (idempotent) and/or refuse `update_draft` bodies that drop it; render the opt-out line outside the editable area in the UI |
| S2-SEC-05 | **Low** | Any inbound message mentioning the quote number is treated as the customer's reply | `app/services/followup/service.py:675-677` (link by `Q-YYYY-NNNNN` found anywhere in subject/snippet, regardless of sender), `:716-726` (`sent → acknowledged`, `source="inbound"`, synthetic evidence), `:610-614` (any inbound event suppresses the schedule: "customer replied") | `T::test_inbound_from_an_unknown_address_does_not_change_quote_status`: inbound from `stranger@elsewhere.example` "FW: Q-2026-00001 looks interesting" → quote **`acknowledged`** | With a real inbound provider (SMTP is outbound-only today; Gmail/Graph documented), anyone who sees a quote number (forwarded PDF, competitor, phishing) can move the quote to "acknowledged" and silence automated follow-ups; the lifecycle rule "only recorded facts move a quote" is met in letter but the fact is attributed to the wrong party. Exposure today: admin-only simulator; design debt for the shipped inbound adapters | Transition only when `from_email` matches a contact of the quote's customer **or** `in_reply_to` matches one of the quote's own outbound `provider_message_id`s; otherwise record the event as `unlinked`/`needs_review` without state change or suppression |
| S2-SEC-06 | **Low** (honest status / design) | Per-tenant provider secrets are encrypted but never used; SMTP credentials are one process-wide secret | `app/services/comms/secrets.py:40-45` (`decrypt` — no caller in `app/`, grep), `app/services/followup/service.py:128-144` (`provider_for` builds `SMTPProvider` from `get_settings().smtp_*`), `infra/docker-compose.yml` (`api` service receives neither `SMTP_*` nor `TOKEN_ENCRYPTION_KEY`) | `T::test_smtp_provider_uses_the_tenants_stored_credential`: tenant stores `tenant-a-only-password` via `/integrations`; `provider_for()` returns an `SMTPProvider` whose password is the **env** value | S2-A05 "secrets encrypted / revocation handled" is storage-only: revoking the integration does not revoke anything the sender uses; every tenant's mail goes out through one shared SMTP account (sender identity, bounce handling and credential rotation are platform-wide); in the compose topology manual sends via SMTP fail in the `api` container (missing env) while the worker can send | Either consume the stored secret (decrypt with the org/provider AAD in `provider_for`, keep host/port/from in `config`), or stop accepting `secret` for `smtp` and document the shared-credential model in `KNOWN_LIMITATIONS`/`PRIVACY_AND_RETENTION`; add `SMTP_*`/`TOKEN_ENCRYPTION_KEY` to the `api` allow-list when the feature is enabled |
| S2-SEC-07 | **Low** (audit) | `POST /integrations/sync-inbound` and `POST /jobs/run-once` write no audit row | `app/api/routers/followup.py:611-626`; `sync_inbound` creates/updates `integration_connections` (`service.py:742-766`) | `T::test_sync_inbound_and_run_once_write_audit_rows`: audit count unchanged after both calls although an integration row was created | Violates "every mutating endpoint writes an `audit_events` row"; combined with S2-SEC-01 the cross-tenant trigger is invisible | `audit.record(action="integration.sync", …)` with processed count; `audit.record(action="worker.run_once", …)` with counts |
| S2-SEC-09 | **Low** | `valid_email()` accepts a trailing newline | `app/services/comms/providers.py:22` (`EMAIL_RE = r"^…$"` — `$` matches before a final `\n`), `:74-75` | `T::test_contact_addresses_with_a_trailing_newline_are_rejected`: `"buyer@customer.example\n"` → **201** | The address is stored and used as a recipient (SMTP/EmailPolicy strips it), but `lower(contacts.email) = lower(from_email)` never matches an inbound UNSUBSCRIBE from that mailbox → the contact cannot opt out by reply; exact-match recipient binding in `send_draft` is also weakened | Use `\Z` (or `re.fullmatch`) and `.strip()` addresses on input (`ContactIn`, `DraftUpdate.to_email`, `PolicyUpdate.reply_to_email`, `IntegrationCreate.account_email`) |
| S2-SEC-10 | **Low** (robustness) | CR/LF in the admin-controlled `sender_name` crashes the SMTP send path (HTTP 500) | `app/schemas/common.py` `PolicyUpdate.sender_name` (length only), `app/services/comms/providers.py:125` (`msg["From"] = formataddr(…)` → `ValueError` from `EmailPolicy`), `app/main.py:88-97` (only `ProviderError` is mapped) | `T::test_crlf_in_admin_sender_name_cannot_inject_headers`: policy `sender_name="Sara\r\nBcc: x@evil.example"` → 200; manual send → **500**, draft stays `draft`, no audit row; on the automated path the job becomes `failed` (non-retryable). No header is injected | Not an injection (the stdlib refuses the value) but an unhandled error that leaves no trace in the audit log and surfaces as "Unexpected error" to the sales user | Strip CR/LF/control characters in `PolicyUpdate.sender_name` and in `OutboundMessage.from_name`; wrap header construction errors into a non-retryable `ProviderError` so the existing `draft.send_failed` path runs |
| S2-SEC-11 | **Low** (configuration) | Development-only endpoints stay enabled in `APP_ENV=staging` | `app/api/routers/followup.py:592, 622` (`settings.is_production`), `app/core/config.py:75-77` | `T::test_dev_only_endpoints_are_disabled_in_staging`: `run-once` → 200 under `staging`; production → 403 (`T::test_dev_only_endpoints_are_disabled_in_production`) | A staging environment with real-looking tenants exposes the cross-tenant trigger (S2-SEC-01) and synthetic inbound injection to every org admin; the web UI shows the "Run worker once (dev)" button there | Gate on `app_env in ("development", "test")` or an explicit `ENABLE_DEV_ENDPOINTS=true`; hide the UI button on the same flag |

Informational observations (no separate severity):

- **I-1** `UNSUBSCRIBE_RE` (`service.py:54`) matches the word `stop` anywhere ("please stop by our booth") → false-positive opt-outs. Fails safe (over-suppression) but silently removes customers from follow-up and requires an owner/admin to re-enable.
- **I-2** `redact()` is key- and pattern-based: a hypothetical `body`/`snippet`/`subject` key would not be masked (`T::test_send_path_logs_…` asserts the current behaviour). The product never logs these (verified by grep and by the captured log of a full send/inbound flow); worth adding to `_SENSITIVE_KEYS` as defence in depth.
- **I-3** `queue.run_job` classifies every `AppError` (consent, state, validation refusals) as retryable → five retries and a dead letter for a deterministic refusal (ties to S2-SEC-03).
- **I-4** Request log lines carry `"request_id": "-"` because `log_event` runs after the context var is reset (`main.py:52-73`) — Stage 1 behaviour, reduces traceability of the Stage 2 audit trail to logs.
- **I-5** Compose: `worker` is allow-listed (no owner DSN/password — good); `api` lacks `SMTP_*`/`TOKEN_ENCRYPTION_KEY` (documented opt-in, but manual SMTP sends then fail in that topology); `migrate` inherits all 30 `.env` keys including `SMTP_PASSWORD` and `TOKEN_ENCRYPTION_KEY` it does not need.
- **I-6** `ProviderError` details reach sales users verbatim (e.g. "SMTP is selected but SMTP_HOST / SMTP_FROM_EMAIL are not configured"; `main.py:88-97`) — minor configuration disclosure; `last_error` on drafts/jobs carries exception class names to admins.
- **I-7** `_rate_limit_ok` is check-then-insert without a lock; concurrent sends of *different* drafts can exceed `hourly_send_limit` by the degree of concurrency (not tested).
- **I-8** `IntegrationCreate.account_email` and `SimulatedInboundIn.from_email` are length-bounded but not validated as addresses.
- **I-9** `20261101_0006_stage2_followup.py` docstring header still says "Revision ID: 0004 / Revises: 0003" (cosmetic).
- **I-10** `claim_next`/`run_job` rely on `expire_on_commit=False`; any rollback between claim and run (as the auditor's first draft of a test did) makes the job object unusable. Robustness only.
- **I-11** An admin opt-in does not revive schedules suppressed by the opt-out (conservative; `T::test_rbac_matrix_…` documents it).

## 5. Tool results (command, exit code, summary)

| Tool / command (cwd `apps/api` unless stated) | Exit | Summary |
|---|---|---|
| `pytest -q -p no:cacheprovider tests/security/test_stage2_audit.py` (`quotepilot_sec`) | 0 | **31 tests: 20 passed, 11 xfailed (strict), 0 failed** (76.9 s first full run; re-runs of fixed tests 11.9 s) |
| `pytest -q -p no:cacheprovider tests/security tests/integration/test_stage2_followup.py` (`quotepilot_sec`) | 0 | **112 passed, 1 skipped, 11 xfailed, 0 failed** in 267 s. The skip is Stage 1's `test_audit_followups3.py:120` ("NEW-13 fixed at the RFQ item level"); the 11 xfails are this audit's strict finding markers; Stage 1 security suites (`test_tenant_isolation`, `test_rbac_csrf_headers`, `test_parsers_and_logging`, `test_audit_followups{,2,3}`) all pass at `cf8d409` |
| `pip-audit 2.10.1 --skip-editable` | 0 | No known vulnerabilities (1 skipped: local editable project) |
| `bandit 1.9.4 -q -r app -ll` / `-f json` | 0 | 8 441 LOC, **0 high, 0 medium**, 7 low (none in the Stage 2 modules: a targeted run over `services/followup`, `services/comms`, `services/jobs`, `workers`, `routers/followup.py`, `lifecycle.py` reports nothing at any severity) |
| `ruff 0.17.3 check app` / `ruff check --select S --ignore S101,S104 app` | 0 / 0 | All checks passed |
| `ruff check` + `ruff format --check tests/security/test_stage2_audit.py` | 0 / 0 | clean (after `ruff format`) |
| `npm audit --omit=dev` (`apps/web`) | 0 | 0 vulnerabilities |
| `npm audit` full (`apps/web`) | 1 | 16 moderate, all dev-only lint tooling via `minimatch` (`eslint*`, `typescript-eslint*`) — unchanged from the Stage 1 scans |
| `npm audit` full (`tests/e2e`) | 0 | 0 vulnerabilities |
| gitleaks / semgrep / trufflehog / hadolint | n/a | **not installed locally — CI-only** (`.github/workflows/security.yml`); the implementer's scan file records the same. Fallback grep of the Stage 2 sources for hard-coded `password|secret|token = "…"` → no matches |
| web bundle grep (`apps/web/.next/static`, build `vbJUZ3rtebN4gJZmgA1cq`, 0 `src` files newer) | 0 | no `TOKEN_ENCRYPTION_KEY|SMTP_PASSWORD|smtp_password|sk-ant-|AKIA…` match; 0 `NEXT_PUBLIC_` references in `src` |
| `docker compose config` (CLI, **no daemon**; `.env.example` as `.env`, random `SECRET_KEY`) | 0 | rendered; `api` 8 vars, `worker` 14 vars (no owner DSN/password), `migrate` 30 vars (I-5). Images not built, stack not run |
| Live probe (`uvicorn :8072`, `next start :3072`, `APP_ENV=test`, seed exit 0) | 0 | health 200; cookies `HttpOnly; SameSite=lax`; Stage 2 GET via proxy: `no-store`, CSP, `DENY`, `nosniff`; `run-once` admin 200 / no-CSRF 403 / sales 403; simulator inbound 201 (`quote_id: null`, unlinked); API log: 0 raw addresses, 0 password strings; **servers stopped, ports 8072/3072 verified closed** |

## 6. Residual risks

| Risk | Status / owner |
|---|---|
| S2-SEC-01 cross-tenant worker on the request path (+ S2-SEC-07 no audit, S2-SEC-11 staging exposure) — Medium | Open — implementer; strict xfails in `T` |
| S2-SEC-02 address-level suppression missing (re-created / duplicate contacts) — Medium | Open — implementer + product (suppression model); two strict xfails |
| S2-SEC-03 dead-lettered schedules starve the shared scheduler — Medium | Open — implementer; strict xfail; also an operations alerting gap |
| S2-SEC-04 opt-out text removable on edit — Medium | Open — implementer; strict xfail |
| S2-SEC-05 inbound linking by quote number only — Low today (simulator admin-only), rises to Medium when a real inbound adapter ships | Open — implementer/architect before Gmail/Graph/IMAP |
| S2-SEC-06 per-tenant secrets not consumed; shared SMTP credential — Low (honest status) | Open — architect decision + docs; S2-A05 should be re-worded in traceability |
| S2-SEC-09/10 input hygiene (`$` anchor, CR/LF in sender name) — Low | Open — implementer |
| Rate-limit race (I-7), `stop` false positives (I-1), retry classification (I-3) | Not tested / documented; implementer backlog |
| Real SMTP/IMAP/OAuth providers, provider timeouts under load, multi-node worker fairness | **Not exercised** (no network, no accounts); simulator and a fake `smtplib` only |
| Docker images / compose execution | Static render only (no daemon) — unchanged from Stage 1 rounds |
| gitleaks / semgrep | CI-only; not reproduced locally |
| The `app.worker` scope is a trusted-code boundary (ADR-012, KNOWN_LIMITATIONS) | Holds for the worker process (verified transaction-local and absent in job bodies); **does not hold for `run-once`** (S2-SEC-01) |

## 7. Explicitly NOT tested

- Browser-level (Playwright) tests for Stage 2: the auditor wrote none; the implementer's `stage2-followup.spec.ts` and the concurrent QA specs were not reviewed.
- Real e-mail transport: SMTP STARTTLS/auth behaviour, bounce handling, Gmail/Graph OAuth flows (not shipped), IMAP inbound (not shipped).
- Concurrency/load: hourly-limit race (I-7), many workers against one queue (SKIP LOCKED was exercised with two sessions only), parser/limiter interactions from Stage 1.
- The AI adapter (not involved in Stage 2) and prompt-injection fixtures.
- Data-retention/deletion behaviour for communication events and drafts (policy document review only).
- The concurrent QA files that appeared in the working tree during the audit.

## 8. Run log (abridged, verbatim where it matters; synthetic data and local-only credentials)

```
$ git -C /home/user/qp-stage2 rev-parse --short HEAD
e851ceb            # evidence-only commit on top of cf8d409 (audited code = cf8d409)

$ export DATABASE_URL_TEST=postgresql+psycopg://quotepilot_app:quotepilot_app@localhost:5432/quotepilot_sec \
         DATABASE_ADMIN_URL_TEST=postgresql+psycopg://quotepilot_owner:quotepilot_owner@localhost:5432/quotepilot_sec APP_ENV=test
$ .venv/bin/pytest -q -p no:cacheprovider tests/security/test_stage2_audit.py --no-header -rA
... (first full run) 4 failed, 17 passed, 10 xfailed in 76.86s
    # 3 failures were test-construction errors (fixed: status=all filter after a deliberate opt-out; the auditor's own
    # log line carried the body; the Bcc text was in the message body, not the header block); the 4th (CRLF sender
    # name → 500) is S2-SEC-10 and became a strict xfail
$ .venv/bin/pytest -q -p no:cacheprovider tests/security/test_stage2_audit.py -k "rbac_matrix or send_path_logs or smtp_headers_resist or crlf_in_admin"
3 passed, 27 deselected, 1 xfailed in 11.91s
$ .venv/bin/pytest -q -p no:cacheprovider tests/security tests/integration/test_stage2_followup.py --no-header -rfExX
112 passed, 1 skipped, 11 xfailed in 267.17s (0:04:27)
exit code: 0

$ .venv/bin/pip-audit --skip-editable            → No known vulnerabilities found            exit 0
$ .venv/bin/bandit -q -r app -ll                 → (no output)                                exit 0
$ .venv/bin/bandit -q -r app -f json | totals    → loc 8441 HIGH 0 MED 0 LOW 7
$ .venv/bin/ruff check app                       → All checks passed!                         exit 0
$ .venv/bin/ruff check --select S --ignore S101,S104 app → All checks passed!                exit 0
$ .venv/bin/ruff check tests/security/test_stage2_audit.py && ruff format --check …          exit 0
$ (apps/web) npm audit --omit=dev                → found 0 vulnerabilities                    exit 0
$ (apps/web) npm audit                           → 16 moderate severity vulnerabilities       exit 1
$ (tests/e2e) npm audit                          → found 0 vulnerabilities                    exit 0
$ command -v gitleaks semgrep trufflehog hadolint → NOT AVAILABLE (all four)
$ grep -rIlE "TOKEN_ENCRYPTION_KEY|SMTP_PASSWORD|smtp_password|sk-ant-|AKIA[0-9A-Z]{16}" apps/web/.next/static → (no match)
$ docker info → daemon NOT available; docker compose -f docker-compose.yml config (scratch copy, .env.example as .env)
api    | n_env: 8  | DATABASE_ADMIN_URL: '' | owner pw in env: False | TOKEN_ENCRYPTION_KEY: False | SMTP_PASSWORD: False
worker | n_env: 14 | DATABASE_ADMIN_URL: '' | owner pw in env: False | TOKEN_ENCRYPTION_KEY: True  | SMTP_PASSWORD: True
migrate| n_env: 30 | DATABASE_ADMIN_URL: 'postgresql+psycopg://quotepilot_o… | owner pw in env: True | TOKEN_ENCRYPTION_KEY: True | SMTP_PASSWORD: True

$ bash live_probe.sh   (APP_ENV=test, DB quotepilot_sec, API :8072, web :3072)
migrate exit: 0 / seed exit: 0 / api health: 200 web login page: 200 / login role: admin
set-cookie: qp_session=<redacted>; HttpOnly; Max-Age=604800; Path=/; SameSite=lax
set-cookie: qp_csrf=<redacted>; Max-Age=604800; Path=/; SameSite=lax
GET /api/v1/follow-up/policy via proxy → 200, X-Content-Type-Options: nosniff, X-Frame-Options: DENY, CSP (web), cache-control: no-store
POST /api/v1/jobs/run-once (admin, CSRF)        → {"processed":0}
POST /api/v1/jobs/run-once (admin, no CSRF)     → {"error":"forbidden","detail":"CSRF token missing or invalid",…,"action":"csrf"}
POST /api/v1/integrations/simulator/inbound     → {"event_id":"…","kind":"reply_received","quote_id":null}
POST /api/v1/jobs/run-once (sales)              → {"error":"forbidden","detail":"Role 'sales' may not perform 'manage_org'",…}
openapi paths: ['/api/v1/jobs', '/api/v1/jobs/{job_id}/retry', '/api/v1/integrations/simulator/inbound', '/api/v1/integrations/sync-inbound', '/api/v1/jobs/run-once']
API log: raw addresses 0 / [EMAIL] markers 0 / password strings 0
ports after stop: 8072/3072 closed   (leftover npm wrapper processes of the probe terminated by PID; verified again)
```
