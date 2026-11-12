# Security re-verification evidence — Stage 3 after fix round 1 (2026-11-12)

Auditor: security-auditor subagent (independent of the implementer). Worktree `/home/user/qp-stage3`, branch
`claude/stage3-customer-order-intelligence`, HEAD `ee865da`. Local testing only, database `quotepilot_sec3b`, app role
(non-owner, RLS enforced). No product code was changed. Scope: SEC3-01..11 of `security-stage3-2026-11-02.md`, the new
validation/idempotency/erasure code and the 0007/0007b migrations. Stage 1/2 merged changes are out of scope.
This is a bounded re-verification, not proof of absence of defects. Not run by instruction: full API suite, Playwright,
`make security-scan` (QA verifier / CI own those).

## 1. Checklist
| # | Finding / item | Verdict | Evidence |
|---|---|---|---|
| 1 | SEC3-01 stalled erasure | **MITIGATED** | `retention.enqueue_due_purges` re-queues with key `purge:{rid}:{n}` when earlier jobs are dead; `test_sec3_01_purge_resumes_after_a_dead_lettered_job` passes; my `test_sec3_01_stalled_purge_is_requeued_then_flagged...` (10 dead jobs → no 11th job, `summary.purge_stalled=true`, hold excludes the request) passes. Residual: a job stuck `running` after a worker crash is never reclaimed or flagged (SEC3-14). |
| 2 | SEC3-02 soft delete | **MITIGATED** | Customer-id routes below are closed (404) via `get_active_customer`; readable only with `manage_retention`. Open: an existing approved quote / open proposal of the customer can still become a new order (SEC3-12), quote list shows the name (SEC3-13). |
| 3 | SEC3-03 PII copies | **MITIGATED** | Gone after purge (tested): customer row, contacts, drafts, `rfqs.pasted_text`, conflicts, import failed rows (predecessor test), quote snapshot customer block, stored PDF file + key (my test; re-rendered PDF has no name). Still contains the subject: `communication_events.subject/snippet` (SEC3-15, not named in KNOWN_LIMITATIONS), audit summaries (`customer.create` carries the name), order notes, quote notes/terms, uploaded RFQ files (these four ARE named in KNOWN_LIMITATIONS l.185-187); `rfq_items.raw_text` not tested (purge code does not touch it; NOT CHECKED, falls under "free text" only loosely). |
| 4 | SEC3-04 / QA D3 | **CLOSED** (tested set) | 21 formerly-xfail cases + zero-tax test now pass: over-long, NUL, NaN/Inf/sNaN, 10^16, non-string e-mail, bare CR, tax <0/>100 are per-row/record rejections; valid rows applied (`applied >= 1`); no 500. Observation: only NUL is rejected among control characters; `\x07`, `\x1b`, bidi U+202E are accepted and stored (see section 6). QA's own D3 test was not run by me. |
| 5 | SEC3-06 / 07 | **CLOSED**, residual SEC3-16 | Key scope = `kind:connector:entity` (`uq_sync_run_scope_idem`); different payload under same key → 409 with no extra order/run (my test); one active connector per kind enforced by the partial unique index (direct SQL insert refused, `uq_connector_org_kind_active`); disable/enable audited, enable refused 409 while another is active, other tenant gets 404. Concurrent replay of one key: loser gets unhandled 500 (SEC3-16); no double application. |
| 6 | SEC3-08 / 09 / 10 | **CLOSED** | FX: `ge 1e-8, le 1e6, decimal_places=8` (1e-9 → 422). Orders: `version.version_no != current_version_no` → 409, line/total amounts copied from the version (`orders.py:95-141`). Catalog: `repriced_catalog_lines` → blocker `catalog_price_changed` for draft/pending lines; predecessor test passes. |
| 7 | Tenant isolation | **CLOSED** (tested) | 10 tables in 0007: connector_connections, deletion_requests, fx_rates, order_proposals, sync_runs, sync_conflicts, stock_snapshots, orders, order_events, order_lines — FORCE RLS + `tenant_isolation`, raw-SQL read/update/delete/insert-with-check as non-owner = 0 rows/refused (predecessor tests pass). 0007b adds no table (columns on `sync_runs`, partial index; FORCE RLS restored after backfill, test checks it). 404 matrix passes for 19 id-bearing routes, plus enable/disable (mine). `set_worker_scope` appears only in `services/jobs/queue.py` (3 sites; grep over `app/`). |
| 8 | Role gates / audit | **CLOSED** (tested) | Matrix test: every role outside the permission gets 403 on export/import/connectors/FX/retention/orders; `export.denied` audit row written for sales/readonly; order confirm proposer≠confirmer and retention requester≠approver unless the owner switch; cost/margin absent for roles without `view_costs`; export formula neutralisation (`'=…`) in customers/orders/failed.csv. Audit coverage test passes. |
| 9 | Declared test edits | **ACCEPTED** | See section 4. |

## 2. Route table (item 2) — customer pending erasure (`deleted_at` set, before purge)
| Route | Result for sales/approver/readonly | owner/admin (`manage_retention`) |
|---|---|---|
| `GET /customers`, `?q=` | hidden | hidden |
| `GET /customers/{id}`, `/profile`, `/customers/{id}/contacts` | 404 | 200 (read) |
| `PATCH /customers/{id}`, `POST /customers/{id}/contacts`, `PATCH /contacts/{id}` | 404 | 404 |
| `POST /rfqs` (JSON and multipart `customer_id`), `POST /quotes` | 404 | 404 |
| `POST /rfqs/{id}/generate-quote` (existing RFQ) | 404 (`create_quote` filters `deleted_at`; code reading) | 404 |
| `POST /customers/{id}/order-proposals` | 404 | 404 |
| `POST /retention/requests` again | 409 already deleted | 409 |
| `GET /data/export/customers` | excluded (export itself needs `export_data`) | excluded |
| `POST /data/import/customers` (reference exists) | row rejected "already exists" | same |
| `POST /data/import/orders` (customer_reference) | row rejected "unknown customer_reference" | same |
| Connector `orders` / `customers` sync matching by reference | record rejected "customer is being deleted" / "unknown customer_reference" | same |
| `apply_remote` conflict of the customer | 409 "Customer no longer exists" | same |
| **`POST /quotes/{id}/orders` for an existing approved quote** | **201 — OPEN (SEC3-12)** | 201 |
| **`POST /order-proposals/{id}/approve` for an open proposal** | **200, order created — OPEN (SEC3-12)** | 200 |
| **`GET /quotes` (customer_name column)** | **name shown — OPEN (SEC3-13)** | shown |
| `POST /contacts/{id}/opt-out`, `/quotes/{id}/transition|drafts` for existing quotes | NOT CHECKED (soft delete cancels pending drafts/schedules and opts contacts out) | |

## 3. Probe log (item 10, 12 probes)
1. Approve deletion, then propose an order from an existing approved quote of that customer → 201 (SEC3-12).
2. Approve deletion, then approve an open repeat-order proposal → 200 and a new order (SEC3-12).
3. After approval, `GET /quotes` as sales → `customer_name` still returned (SEC3-13).
4. Purge job left `running` (locked 3 days ago) → `enqueue_due_purges` returns 0 and sets no `purge_stalled` (SEC3-14).
5. Real send via simulator, then purge → `communication_events.snippet` still starts "Dear Buyer Person" (SEC3-15).
6. Same connector sync key + payload from two sessions at once, 8 rounds → orders applied once every time; loser always an unhandled `IntegrityError`/500 (SEC3-16).
7. Two approvers confirm one order at once, 8 rounds → 7 rounds recorded two `confirmed` events and two audit rows (SEC3-17).
8. Disable/enable round trip and direct SQL insert of a second active connector of one kind → DB refuses (pass).
9. Tenant A imports an order CSV naming tenant B's customer reference → row rejected "unknown customer_reference"; B's name absent from failed.csv (pass).
10. Same idempotency key, different payload, connector and CSV → 409, no extra orders/runs (pass).
11. Purge versus legal hold: hold placed while a job is queued → job dead → released → re-queued and executed (predecessor test, pass). A hold placed during the purge transaction is not row-locked (code reading only, narrow window; not reproduced).
12. Formula strings imported via CSV/connector (`=HYPERLINK`, `@x`, `-u`, `+cmd`) → exports prefix `'` (predecessor test, pass). Control characters other than NUL accepted (observation, section 6).

## 4. Declared edits to the predecessor's tests (`git diff 1a6af7b..HEAD`, two files only)
- xfail markers removed from 12 test functions (plus the `_xf` helper); the assertions are unchanged (diff shows only deletions of the decorators). Each now passes against fixed code (67 passed), so every property is asserted for real; strict xfail would have flagged an XPASS for no-ops.
- `_xf()` returns `()`: `pytest.param(..., marks=())` keeps all 21 SEC3-04 cases as ordinary parametrised tests; none removed or loosened (verified by counts: the file still collects 67 tests in total with the audit file).
- Audit-coverage test: `approved_quote` and the sales login moved before the deletion approval, because RFQ/quote creation for that customer is now refused. Not weakened for its purpose (audit rows). Side effect: that test then still proposes an order from the erased customer's quote, which is the SEC3-12 behaviour; the test does not assert it either way.
- Gap in the predecessor tests: `test_sec3_02_...` covers 8 routes only (not existing-quote conversion, proposals, quote list) — now covered by my SEC3-12/13 tests.

## 5. Runs
| Command (from `apps/api`, env as mandated) | Exit | Result |
|---|---|---|
| `.venv/bin/pytest -q -p no:cacheprovider tests/security/test_stage3_audit.py tests/security/test_stage3_audit_inputs.py` (run 1) | 0 | 67 passed, 83.4 s |
| same (run 2, after adding my tests) | 0 | 67 passed, 93.0 s |
| `.venv/bin/pytest -q -p no:cacheprovider tests/security/test_stage3_confirm.py` | 0 | 5 passed, 7 xfailed, 18.4 s |
| same with `--runxfail` (proves each xfail fails for its stated reason) | 1 | 7 failed, 5 passed (the 7 are SEC3-12 x2, 13, 14, 15, 16, 17) |
| `.venv/bin/ruff check tests/security/test_stage3_confirm.py` | 0 | All checks passed |
| `.venv/bin/ruff format --check tests/security/test_stage3_confirm.py` | 0 | already formatted |
`git status`: only untracked `apps/api/tests/security/test_stage3_confirm.py` and this file from me (an untracked QA file `tests/qa/test_qa_stage3_confirm.py` is not mine). Nothing committed.

## 6. New findings (none Critical/High; none release-blocking)
| ID | Sev | Finding | Repro | Fix advice | Blocking? |
|---|---|---|---|---|---|
| SEC3-12 | Medium | Existing approved quote / open repeat-order proposal of a customer pending erasure can still be turned into a new order (`orders.py:propose_from_quote`, `repeat_orders.decide` do not check `deleted_at`; `_soft_delete` leaves proposals open). Contradicts "stop processing". Orders still need `approve_order` confirmation. | `test_sec3_12_*` (2 tests) | Reject in both paths with 409; cancel open proposals in `_soft_delete`. | Backlog |
| SEC3-13 | Low | `GET /quotes` (and search by name) exposes the name of a customer pending erasure to all roles; profile/contacts hide it. | `test_sec3_13_*` | Null the name in list/detail when `deleted_at` is set unless `manage_retention`. | Backlog |
| SEC3-14 | Medium | A purge job stuck `running` (worker crash) is never reclaimed; `enqueue_due_purges` skips it and does not flag `purge_stalled`. The erasure stalls silently. Owner/admin `POST /jobs/{id}/retry` is the only recovery (not verified for `running`). | `test_sec3_14_*` | Reclaim jobs with `locked_at` older than N minutes in `claim_next`, or count stale `running` as failed in `enqueue_due_purges`. | Backlog |
| SEC3-15 | Medium | Purge keeps `communication_events.subject/snippet` (first 200 chars of the sent body, greeting the contact by name). Not named in KNOWN_LIMITATIONS. | `test_sec3_15_*` | Blank subject/snippet/payload of events whose quote belongs to the subject, or document it. | Backlog |
| SEC3-16 | Low | Concurrent replay of one sync idempotency key: loser raises unhandled `IntegrityError` (500). Data applied once. Same pattern likely in `import_data`. | `test_sec3_16_*` | Catch the unique violation, re-read the run, return the replay/409. | Backlog |
| SEC3-17 | Low | Concurrent confirmation of one order succeeds twice (no row lock/conditional update): duplicate `confirmed` event and audit row, `confirmed_by` overwritten. No authz bypass, no money effect. | `test_sec3_17_*` | `SELECT … FOR UPDATE` or `UPDATE … WHERE status = 'proposed'`. | Backlog |

## 7. Consolidated list still open for Stage 3
SEC3-12 Medium, SEC3-14 Medium, SEC3-15 Medium, SEC3-13 Low, SEC3-16 Low, SEC3-17 Low. Documented limitations that
remain by design: audit summaries, order/quote notes, uploaded RFQ files and backups keep subject data; no alerting on
`purge_stalled`. SEC3-01..04, 06..11 are closed or mitigated as in section 1; SEC3-05 was withdrawn.

## 8. Observations (one line each)
- `text_field` rejects only NUL; other control characters and bidi marks are stored and exported (neutralised only at the cell start). Low, backlog.
- `set_worker_scope` stays bound for the whole `enqueue_due_purges` transaction after `set_tenant` (flag is transaction-local, not reset); statements there filter by org explicitly. Low.
- `purge()` and `decide()` take no row lock, so a legal hold set during the purge transaction can lose the race (narrow).
- Failed-rows redaction matches by substring of name/e-mail/reference; a very short customer name could redact unrelated rows (over-redaction, safe direction).
- `resolve_conflict` has no row lock: two concurrent resolutions could both apply.
- `quote_versions.notes/terms` and `rfq_items.raw_text` may carry the subject; not purged.

## 9. Not checked
Full API/browser suites and the security scan (not requested); QA's D3/D4 tests; quote lifecycle/draft routes of an erased customer's existing quotes beyond code reading; KPI figures; stock label logic; `rfq_items.raw_text` after purge; `/jobs/{id}/retry` on a `running` purge job; real LLM/SMTP adapters (disabled); backups and object storage beyond the local PDF key; load behaviour of 5 MB CSVs; browser bundle secrets (no Stage 3 change examined).
