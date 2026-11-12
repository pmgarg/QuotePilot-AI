# Security re-check, Stage 3 fix rounds 2 and 3 (release review 1, condition C3), 2026-11-12

Auditor: security-auditor subagent. Worktree `/home/user/qp-stage3`, branch `claude/stage3-customer-order-intelligence`,
HEAD `096d9d4`. Local only. Database `quotepilot_sec3c`, app role (non-owner, RLS enforced), `APP_ENV=test`. No product
code changed. New tests: `apps/api/tests/security/test_stage3_c3.py` (12 tests). Bounded check, not proof of absence of
defects. Not run by instruction: full API suite and browser suite (QA verifier).

## 1. Checklist
| # | Item | Verdict | Evidence |
|---|---|---|---|
| 1 | SEC3-12..17 at this head | **MITIGATED** (tested set) | `test_stage3_confirm.py`: 12 passed, 0 xfail. Code read: `orders.py::propose_from_quote` and `repeat_orders.py::decide` call `get_active_customer` (approve only; reject stays possible); `get_order(for_update=True)` with `populate_existing` on confirm/fulfil/cancel router paths; `quotes.py:117-125` nulls the name and removes it from the name search unless `manage_retention`; `retention.py` redacts `communication_events` subject/snippet/payload; `connectors.py:152-161` savepoint + `find_run` replay. Git: `b0e87f8` removed only the 6 xfail decorators from `test_stage3_confirm.py` (0 assertion lines touched); `bc251f8` removed the SEC3-14 decorator only. Audit file `53a65f6` only moves `approved_quote`, the sales login and the order proposal before the erasure approval; the assertions of `test_every_stage3_mutation_writes_an_audit_row_and_failed_mutations_write_none` are unchanged. |
| 2 | Race tests, 3 runs | **MITIGATED** | 3 runs of `test_stage3_confirm.py`, 12 passed each, no failure (SEC3-16 and SEC3-17 tests = 8 rounds each). |
| 3a | Surfaces for a customer pending erasure | **MITIGATED**, one weakness (SEC3-C3-1) | `test_c3_a_no_read_surface_...`: sales, approver, readonly on 17 GET routes (quote list/search/detail/history, orders list/detail, proposals, KPIs, customers list/detail/profile/contacts, data runs, conflicts) plus approver exports of customers/orders: name and e-mail absent (200/404 only). KPI payload carries no customer names. **Quote PDF still prints the name (see SEC3-C3-1).** Quote CSV does not. |
| 3b | `reclaim_abandoned(kind="purge_customer", count_dead=False)` | **MITIGATED** | Stale `running` purge job: requeued, no second job created, executed once (`...stale_running_purge_job_is_requeued...`). Duplicate job for an executed request: succeeds, `last_error` NULL, summary unchanged, one `retention.purged` audit row (`...rerun_on_an_executed_request...`). Dead purge job: new job queued, dead one not counted, an unrelated stale `schedule_followup` job stays `running` and its schedule stays `pending` (kind filter, `_fail_schedule` is a no-op for other kinds). Legal hold set while the job is `running`: after reclaim the job dies on "Legal hold", request stays `approved`, customer untouched; releasing the hold resumes and executes. |
| 3c | Cross-tenant reclaim | **MITIGATED** | Tenant B owner `POST /jobs/run-once`, `reclaim_abandoned(db, B)` and `run_once(org_id=B)`: A's stale `running` job and A's `queued` job unchanged, A's request still `approved`; B cannot list A's jobs or requests, approve gives 404. The worker run then executes A's purge under A's own scope; B's customers untouched. `set_worker_scope` still appears only in `services/jobs/queue.py` (4 call sites, grep). |
| 4a | CSV same-key race (`_start_run`) | **MITIGATED** | 3 concurrent uploads x 8 rounds: all 201, customer applied once, one run row, `applied=1`; replay with another payload afterwards: 409, nothing applied. 2 racers with different payloads x 8 rounds: always one 201 and one 409, exactly one of the two customers exists. Unique violation with no winner (`find_run` stubbed to None): `IntegrityError` propagates, no run row added. Plus the implementing side's 8-round orders test (not re-run by me). |
| 4b | Purge vs run reports | **MITIGATED** (tested set) | Real CSV customers failed row (name, reference, e-mail) and connector customer rejections (same values): reports quote the subject before the purge (precondition asserted), none of name, e-mail, `cust-1` remains in any `sync_runs.report` after it. See section 5 for what is left. |
| 5 | Scan | RAN, partly unavailable | Section 4. |
| 6 | Lint | **PASS** | `ruff check app tests`: All checks passed, exit 0. `ruff format --check app tests`: 157 files already formatted, exit 0. No `os.environ`, no git or outside-repo paths, no wall-clock assertions (timestamps are set in the past, never compared). |

## 2. Runs
| Command (from `apps/api`, env as mandated) | Result |
|---|---|
| `uv run pytest -q -p no:cacheprovider tests/security/test_stage3_confirm.py` x3 | 12 passed, 22.2 s / 26.0 s / 27.1 s (each run; I read the pytest summary line, the shell exit shown was `tail`'s) |
| `uv run pytest -q -p no:cacheprovider tests/security/test_stage3_c3.py` (final, run twice) | 11 passed, 1 xfailed (SEC3-C3-1), ~27 s. First run had 4 failures, all errors in my own tests (wrong expectations: dead-job count, soft delete cancelling my schedule, `received=None` defaults to 0, tenant GUC reset after commit); fixed before the final runs, no product behaviour hidden by it. |
| `uv run ruff check app tests` / `ruff format --check app tests` | exit 0 / exit 0 |

## 3. Findings
| ID | Sev | Finding | Reproduction | Advice |
|---|---|---|---|---|
| SEC3-C3-1 | Low (privacy) | The PDF of an approved quote of a customer pending erasure still prints the customer name (read from the approval snapshot) for every role including `readonly` (export permission = all roles). The fix for SEC3-13 covers JSON lists only. Pre-existing, not widened by the fixes; the name disappears at the purge (snapshot pseudonymised, stored PDF deleted). | `test_c3_a_quote_exports_do_not_print_the_name_of_a_customer_pending_erasure` (strict xfail) | Render "Customer withheld" for roles without `manage_retention` while `deleted_at` is set, or accept as part of C1. |
No Critical, High or Medium finding from this pass. No new race, no 500, no cross-tenant effect found in the probed paths.
Nits (no action required): `queue.reclaim_abandoned` reuses the name `kind` as loop variable after the SQL ran (harmless today, easy to break); a dead purge job (legal hold) logs one "job failed" warning line; `enqueue_due_purges` leaves the worker scope bound after its first call (known, statements filter by org).

## 4. Scan at this head (`make security-scan`, exit 0, file `docs/evidence/scan-20261112-221628.md`, commit 096d9d4)
| Tool | Ran | Result | Exit |
|---|---|---|---|
| pip-audit | yes | No known vulnerabilities | 0 |
| bandit (`-ll`, medium and above only) | yes | 0 findings | 0 |
| ruff (S* rules, `app`) | yes | all checks passed | 0 |
| npm audit `--omit=dev` | yes | 0 vulnerabilities | 0 |
| npm audit (all) | yes | 8 moderate, all dev-only (`minimatch` via the eslint/typescript-eslint chain; the suggested fix is a breaking downgrade of eslint-config-next) | 1 |
| gitleaks | **not available** | not run | n/a |
| fallback secret grep | yes | 1 hit: the placeholder `sk-ant-api03-abcdefghijklmnop` in the `.gitleaks.toml` allowlist, not a secret | 0 |
| semgrep | **not available** locally (runs in CI only) | not run | n/a |
| `.next/static` secret grep | yes | no pattern found; I did not check how old that build is | 0 |
The scan left one new untracked evidence file; I did not commit it.

## 5. Observations for human decision C1 (not defects to fix now)
State after a purge, measured with the C3 probes on synthetic data:
- `sync_runs.report`, kinds `csv_import` and `connector`: rows and reasons that contain the subject's reference, e-mail or name are redacted (substring match, so over-redaction is possible). Values that identify the subject in other ways (a contact's personal name, a different spelling) are not matched; not tested.
- `orders.external_ref` kept (an imported order "PO-CUST-1-77" still carries the reference); `orders.customer_id` kept (by design, points at the pseudonymised row). Order `notes` from CSV/connector are `CSV import <key>` / `connector <kind>` and carry no subject.
- `rfq_items.raw_text` kept (1 row after purge in my fixture; `purge()` only nulls `rfqs.pasted_text`). Whether the name is inside depends on what the buyer typed on that line.
- `quote_versions.notes` kept when it names the customer (documented limitation). Audit summaries: `customer.create` carries the name when created through the API; my fixture created the customer directly, so this was NOT measured here.
- Quote approval snapshot and stored PDF are handled (pseudonymised, deleted and re-rendered); the pre-purge PDF is SEC3-C3-1.

## 6. Not checked
Full API and browser suites; web UI behaviour for the erased-customer 404; real worker process loop (driven in-process); real LLM, SMTP and storage adapters (disabled); backups; quote lifecycle routes (`transition`, drafts) of an erased customer's existing quotes beyond what earlier evidence covers; `resolve_conflict` and legal-hold row locking (still no lock, narrow race, unchanged); control-character handling in imports (unchanged from the previous pass); freshness of the `.next` build used by the bundle grep; QA's `test_qa_stage3_c3.py` and e2e spec (not mine, not run).

## 7. Residual risks
| Risk | Sev | State |
|---|---|---|
| SEC3-C3-1 PDF shows the name during the grace period | Low | open, xfail test |
| Purge scope beyond customer row (`external_ref`, `rfq_items.raw_text`, notes, audit summaries, uploaded RFQ files, backups) | Medium (privacy) | owner decision C1 |
| Legal hold set during the purge transaction can lose the race (no row lock in `purge`/`decide`/hold) | Low | unchanged, backlog |
| No alerting on `purge_stalled`; cap of 10 purge jobs is an agent decision (C2 list) | Low | unchanged |
| gitleaks and semgrep absent locally; semgrep only in CI | Low (process) | open |
| Redaction by substring can miss variants of the subject's identifiers | Low | accepted by design so far |
This file states what was tested at HEAD `096d9d4`; it is not a claim that the system is secure.
