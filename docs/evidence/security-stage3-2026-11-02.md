# Security audit evidence — Stage 3, customer & order intelligence (2026-11-02, completed 2026-11-10)

Auditor: security-auditor subagent, independent of the Stage 3 implementer. Worktree `/home/user/qp-stage3`, branch
`claude/stage3-customer-order-intelligence`. Audited commit **`8ec4cc3`**. During the audit HEAD moved to `b4256fb`, which
only adds `docs/evidence/scan-20261102-004622.md` (`git diff --stat 8ec4cc3..b4256fb` = 1 file, evidence only). Product
code is identical at both commits. The audit was interrupted by an API usage limit and a PostgreSQL restart, then
resumed on 2026-11-10. Every test listed below was re-run after the restart.

The auditor did **not** change product code. Files added: `apps/api/tests/security/test_stage3_audit.py`,
`apps/api/tests/security/test_stage3_audit_inputs.py` and this document. Nothing was committed.

This document records what was tested, how, and what was found. It does not claim that no vulnerabilities exist,
and it does not call the system "secure".

## 1. Scope

| In scope | Method |
|---|---|
| Migration `20261101_0007_stage3_intelligence.py`: 10 tables, RLS policies, grants, `organizations.order_seq`, `customers.deleted_at` | Catalog queries (`pg_class`, `pg_policies`, `information_schema`) as the runtime role; raw-SQL RLS probes with and without tenant context |
| `app/services/intelligence/*` (orders, repeat_orders, kpis, fx, data_io, connectors, stock, retention), `app/api/routers/intelligence.py`, `app/services/authz.py` (5 new permissions), `app/services/jobs/queue.py` (`purge_customer`), `app/workers/runner.py` | Line-by-line code review + black-box API tests (FastAPI TestClient, real PostgreSQL 16) |
| Regression of Stage 1/2 controls touched by Stage 3: customers routes, contacts, quotes/versions, catalog pricing, CSRF, headers, error bodies, audit redaction | Code review + tests; the existing suites were re-run |
| Web pages `apps/web/src/app/(app)/{orders,insights,data,customers/[id]}` | Code review only: no `dangerouslySetInnerHTML`; cost column depends on the API omitting `cost_price`; exports are same-origin links |
| Tooling | pip-audit, bandit, ruff (incl. S rules), npm audit (prod + full), secret greps; gitleaks / semgrep / trufflehog not available |

Checklists used: OWASP ASVS 5.0 V4 (access control), V5 (validation), V8 (data protection), V12 (files), V14; PRD
S3-A01..A09; `docs/THREAT_MODEL.md`; the CLAUDE.md non-negotiables (no invented prices, server-side authorization,
404 for another tenant's records, an audit row in the same transaction).

## 2. Environment

| Item | Value |
|---|---|
| Host | Claude Code cloud container, Python 3.13 (`apps/api/.venv`), Node 22, PostgreSQL 16.15 (local service) |
| Database | `quotepilot_sec3` (empty before the run; migrated by `tests/conftest.py`). Runtime role `quotepilot_app` (`rolsuper=f`, `rolbypassrls=f`); migrations as `quotepilot_owner` (also `rolsuper=f`, `rolbypassrls=f`) |
| Env for tests | `APP_ENV=test DATABASE_URL_TEST=postgresql+psycopg://quotepilot_app:…@localhost:5432/quotepilot_sec3 DATABASE_ADMIN_URL_TEST=postgresql+psycopg://quotepilot_owner:…@localhost:5432/quotepilot_sec3`, pytest `-p no:cacheprovider` |
| Live servers | **None started.** All dynamic testing went through the in-process TestClient. Ports 8082/3082 were never bound by the auditor. |
| Network | No third-party calls; connectors are push-model stubs with no outbound I/O |

## 3. Methodology

1. Read the spec (§Stage 3, §Security), THREAT_MODEL, SECURITY_POLICY, PRIVACY_AND_RETENTION, API_CONTRACT (Stage 3),
   DATA_MODEL, KNOWN_LIMITATIONS §Stage 3, the implementer's self-report and the newest scan file.
2. Reviewed every Stage 3 code path for: tenant filter + RLS, permission check before any state change, 404 vs 403,
   input bounds versus the column types, the audit row inside the transaction, and the soft-delete filter.
3. Wrote tests. Passing controls are plain tests. Each finding is a `strict=True` xfail tagged with its finding id, so
   a fix turns it into XPASS, which fails the run until the marker is removed.
4. Re-ran every finding test with `--runxfail` to confirm that it fails **for the stated reason**. This caught one
   mis-targeted test (SEC3-09 first hit a non-existent route); it was corrected and re-verified.
5. Ran the full API suite and the tool scans.

## 4. Findings

Severity is the auditor's judgement for this product at this stage: an internal multi-tenant B2B app, where the
high-privilege routes need owner/admin/approver. **No Critical or High findings.** No cross-tenant read or write was found.

| ID | Sev | Title | Location (file:line @8ec4cc3) | Repro test (`apps/api/tests/security/…`) | Impact | Remediation |
|---|---|---|---|---|---|---|
| SEC3-01 | Medium | An approved erasure can silently never execute. If a purge job dead-letters (for example, a legal hold is placed after the job was queued, so `purge()` raises `StateError`, which is retryable and dies after 5 attempts), the request is never re-enqueued: the idempotency key `purge:{rid}` already exists, so `enqueue_due_purges` skips it after the hold is lifted. | `app/services/jobs/queue.py:88-93,174-185`; `app/services/intelligence/retention.py:233-258` | `test_stage3_audit.py::test_sec3_01_purge_resumes_after_a_dead_lettered_job` (request stays `approved` with `legal_hold=false` and a past `purge_after`; job `dead`) | A data-subject erasure that was approved and audited is never carried out. Nothing tells the requester or approver. Partial mitigation: an owner/admin can find and retry the dead job via `POST /jobs/{id}/retry`. | In `_handle_purge`, return without raising when `legal_hold` is set (the next tick re-checks it). Or let `enqueue_due_purges` re-enqueue when the previous purge job is `dead`/`failed` (e.g. key `purge:{rid}:{attempt}`). Show stuck approved requests on the retention view. |
| SEC3-02 | Medium | Soft delete is only a list filter. After a deletion is approved, the customer and its PII can still be read and changed until the purge runs: `GET /customers/{id}/profile` (with contacts), `GET/POST /customers/{id}/contacts`, `PATCH /customers/{id}` (new name/email), new RFQs and quotes for the customer, and connector order/customer syncs that match it by `reference`. | `app/api/routers/customers.py:62,72`; `app/api/routers/intelligence.py:497-503`; `app/api/routers/followup.py:62,77`; `app/api/routers/rfqs.py:113-119`; `app/services/quotes.py:240`; `app/services/intelligence/connectors.py:337,398-404` | `test_stage3_audit.py::test_sec3_02_soft_deleted_customer_is_not_processed_any_further` (all 8 probes reproduced: profile, contacts read, contact create, customer PATCH, RFQ create, quote create, connector order attach, connector customer conflict) | During the grace period (`RETENTION_GRACE_DAYS`, 30 by default), staff can still view and add personal data for a subject whose erasure was approved, and new commercial records keep linking to it. This contradicts "approval soft-deletes immediately: … stop processing" (retention.py:142 docstring, API_CONTRACT). | Add one `get_active_customer()` helper that filters `deleted_at IS NULL`, and use it on every customer-scoped write and on profile/contacts reads. Return 404 (or 409 "customer deleted"). Connectors should reject records that match a deleted customer. |
| SEC3-03 | Medium | The purge leaves copies of PII outside the customer row: `sync_conflicts.local_value/remote_value` (name, e-mail), `sync_runs.report.failed_rows` (raw CSV rows, including e-mail addresses), `quote_versions.document_snapshot` (customer name) and the stored approved PDFs. Also retained: `audit_events.summary` of `customer.create` (name) and uploaded RFQ files (documented in KNOWN_LIMITATIONS). | `app/services/intelligence/retention.py:187-230` | `test_stage3_audit.py::test_sec3_03_purge_removes_pii_copies_outside_the_customer_row` (leftovers found: `sync_conflicts`, `sync_runs.report.failed_rows`, `quote_versions.document_snapshot`) | After an "executed" erasure the subject's name and e-mail can still be read in the conflict queue, the import-run report and the failed-rows CSV (`GET /data/runs/{id}/failed.csv`), and in quote document snapshots/PDFs. | Extend `purge()` to redact matching conflicts and import-run rows. Decide, and document in PRIVACY_AND_RETENTION, whether approved quote documents are kept under a commercial-records basis; if not, pseudonymise the snapshots and delete the PDFs/RFQ files. Keep the audit row but drop the name from the `customer.create` summary. |
| SEC3-04 | Medium | Untrusted CSV and connector fields reach PostgreSQL unbounded. A single row with an over-long `sku`/`unit`/`order_ref`/`email`, a NUL byte, a value of 10^16 or more in a NUMERIC column, a non-string `email`, or a bare CR inside an unquoted CSV field (`csv.Error`) causes an **unhandled 500 and rolls back the whole import or sync** (S3-A07 partial recovery fails). Connector order lines also accept `tax_rate_pct` < 0 or > 100 (the CSV import rejects these), and a `tax_rate_pct` of `0` is silently replaced by the org default (`parse_decimal(..) or default` on a falsy `Decimal`). An over-long `unit` is staged as a conflict and then 500s on `apply_remote`. | `app/services/intelligence/data_io.py:128-147,199-207,259-283`; `app/services/intelligence/orders.py:186-199`; `app/services/intelligence/connectors.py:231-238,336,380-392` | `test_stage3_audit_inputs.py::test_sec3_04_*` (21 strict-xfail cases: 6 order-CSV, 3 customer-CSV, 11 connector records, 1 `apply_remote`), plus `::test_sec3_04_connector_zero_tax_is_kept` | Availability and integrity of imports: one hostile or mis-exported row blocks the whole batch, and connector orders can carry negative or above-100 % tax, which skews totals and KPIs (they still need `approve_order` confirmation). The 500 body is generic; no internals leak (`test_sec3_04_crash_path_does_not_leak_internals` passes). Exploiting this needs `import_data`/`manage_connectors` (owner/admin), or an external system those roles have wired up. | Validate every field against its column (length, NUL, `Decimal.is_finite()`, magnitude below 10^15, tax 0–100) and turn failures into per-row errors. Catch `csv.Error` and return 422. Use `x if x is not None else default` instead of `or`. Clip or reject `unit` at 16 characters when staging conflicts. |
| SEC3-05 | — | *Withdrawn.* Suspected: `Infinity` passes `d < 0` and reaches the catalog through the conflict queue. Not reproduced: `parse_decimal` returns `None` for non-finite values, so `NaN`/`sNaN`/`Infinity` are rejected per record (passing tests). | — | `test_sec3_04_connector_rejects_hostile_records_per_record[…NaN/sNaN/Infinity…]` (pass) | — | — |
| SEC3-06 | Low | Idempotency keys share one namespace per tenant across connectors, entities and CSV imports (`uq_sync_run_idem` is `(organization_id, idempotency_key)`). A push of `stock` reusing a key first used for `products` (or a CSV import, or another connector) gets back the earlier, unrelated run with 201, and the new data is never processed. | `app/services/intelligence/connectors.py:127-131`; `app/api/routers/intelligence.py:346-350`; migration 0007 `uq_sync_run_idem` | `test_stage3_audit_inputs.py::test_sec3_06_idempotency_key_namespace_is_bound_to_entity_and_connector` | Silent data loss for integrators that reuse keys per entity. Not cross-tenant (`test_idempotency_keys_are_per_tenant` passes). | Scope the key by `(kind, connection_id, entity)` and store a request hash; return 409 when the same key arrives with a different scope or payload. |
| SEC3-07 | Low | Disabling a connector is irreversible: there is no enable route, and `(organization_id, kind)` is unique, so the tenant can never connect that kind again. | `app/services/intelligence/connectors.py:71-76`; `app/api/routers/intelligence.py:424-438` | `test_stage3_audit_inputs.py::test_sec3_07_disabled_connector_can_be_replaced_or_re_enabled` | A mistaken or malicious admin click is a permanent self-DoS of the integration; recovery needs database surgery. | Add an audited re-enable or delete route (`manage_connectors`), or make the unique index partial (`WHERE status = 'active'`). |
| SEC3-08 | Low | An FX rate below `NUMERIC(18,8)` precision (e.g. `1e-9`) passes Pydantic `gt=0`, rounds to 0 and violates `ck_fx_rate_positive`, giving a 500. | `app/schemas/common.py:679` | `test_stage3_audit_inputs.py::test_sec3_08_fx_rate_below_column_precision_is_refused_not_500` | Unhandled 500 (generic body); no data impact. | `Field(ge=Decimal("0.00000001"), decimal_places=8)`. |
| SEC3-09 | Medium | A superseded approved version can still be converted. Approving v2 leaves v1 `approved`, and `POST /quotes/{id}/orders {"version_no": 1}` then proposes an order with v1's lines, prices and totals, although the customer's current approved document is v2. | `app/services/intelligence/orders.py:94-96` (with `app/services/quotes.py:822-880`, which does not mark the old version superseded) | `test_stage3_audit_inputs.py::test_sec3_09_superseded_approved_version_is_not_convertible` (201, `quote_version_no` 1, `grand_total` 1307.25 = v1 total) | Orders are priced from an outdated document. A confirmation by `approve_order` is still required, and the order shows `quote_version_no`, so this is a business-integrity risk, not a privilege bypass. | Allow conversion only of the current approved version (`version_no == quote.current_version_no`), or mark earlier approved versions `superseded` when a later one is approved. |
| SEC3-10 | Medium | `apply_remote` changes product prices of the **active** catalog version in place. Stage 1's stale-price guard (`stale_catalog_lines`) only detects a change of catalog *version*, so a draft quote line priced from the catalog before the change (0.85) is still submitted and approved at 0.85 labelled `price_source = "catalog"` while the catalog now says 1.70. This also breaks the "catalog versions immutable" statement in PRIVACY_AND_RETENTION and makes `pricing_snapshot.catalog_version_id` ambiguous. | `app/services/intelligence/connectors.py:440-459`; `app/services/quotes.py:122-139` | `test_stage3_audit_inputs.py::test_sec3_10_in_place_catalog_price_change_is_surfaced_on_open_quotes` | A quote is approved with a price its own label says comes from the catalog when it no longer does, and the catalog price history cannot be reconstructed from versions. KNOWN_LIMITATIONS mentions the in-place edit, but not this effect on open quotes. | Make `apply_remote` copy-on-write (a new catalog version activated through the normal audited path, so the existing stale-line detection fires), or re-check catalog-priced lines against the current product price at submit/approve and block on a mismatch. |
| SEC3-11 | Medium | `parse_decimal()` removes every character except digits, `,`, `.`, `-` and `()` (`_NUM_CLEAN`), including the exponent letter. So `1e3` → **13**, `8.5E2` → **8.52**, `2.5e2` → **2.52**, and `1e-2` → rejected. This is shared code: the Stage 1 CSV catalog import (string cells), the Stage 3 CSV order import and connector pushes. | `app/services/money.py:103,106-150`; callers `app/services/catalog_import.py:162,176,182`, `app/services/intelligence/data_io.py:260-262`, `app/services/intelligence/connectors.py:231,282,380-381` | `test_stage3_audit_inputs.py::test_sec3_11_scientific_notation_is_not_silently_misparsed`, `::test_sec3_11_scientific_notation_price_on_import_is_not_invented` | Prices and quantities are invented silently from scientific notation, which spreadsheet and ERP exports commonly emit, against the "never invent prices" rule. XLSX numeric cells are unaffected (they take the `float` path). | Detect `[eE]`: either parse with `Decimal` (requiring a finite result in range) or reject the value with a row error. Add regression cases to the Stage 1 unit tests. |

### Informational (no test failure; recorded for the reviewer)

- **I-1 `organizations` grants.** The runtime role holds table-wide `SELECT, INSERT, UPDATE, DELETE` on `organizations`
  (0001 grant; 0004 only narrowed `users`/`sessions`). `allocate_number` needs `UPDATE (order_seq)`, and it has it. The
  only barrier between tenants is RLS `org_self` (`USING id = app.current_org`, which also serves as WITH CHECK for
  UPDATE). Verified: tenant B's `UPDATE organizations SET order_seq … WHERE id = A` affects 0 rows and B cannot read
  A's counter (`test_order_sequence_update_is_scoped_to_the_callers_org_row`). Defence in depth: replace the table-wide
  grant with column grants (`order_seq`, `quote_seq`, settings columns) and revoke `DELETE`, as was done for users/sessions.
- **I-2 KPI visibility.** `GET /kpis` returns pending and won *revenue* to every role, including `readonly`. It contains
  no cost or margin (`test_costs_and_margins_are_hidden_from_sales_and_readonly_everywhere`). This is consistent with
  quotes being readable by `readonly`. A product decision, not a defect.
- **I-3 Cancel has no separation of duties.** Any `approve_order` holder may cancel an order they proposed. A reason is
  required and audited. Acceptable by design; recorded so the reviewer can confirm the intent.

## 5. Per-control results (passing checks)

| Control | Result | Evidence (test in `apps/api/tests/security/`) |
|---|---|---|
| RLS ENABLE + FORCE + `tenant_isolation` (USING + WITH CHECK) on all 10 Stage 3 tables; worker clause only on `deletion_requests`; runtime role has exactly S/I/U/D; no BYPASSRLS | PASS | `test_stage3_audit.py::test_stage3_tables_force_rls_with_tenant_policy_and_app_role_grants` |
| Raw SQL as tenant B or with no tenant sees 0 rows in each of the 10 tables (fixture populates all 10 for A); UPDATE/DELETE of A's rows affect 0 rows | PASS | `::test_rls_blocks_every_stage3_table_for_the_other_tenant_even_with_raw_sql` |
| WITH CHECK refuses inserts for another tenant (orders, proposals, fx, connectors, runs, deletion requests) | PASS | `::test_with_check_refuses_inserting_rows_for_another_tenant_in_every_stage3_table` |
| `order_seq` cannot be bumped or read across tenants | PASS | `::test_order_sequence_update_is_scoped_to_the_callers_org_row` |
| Worker scope: never set by API code; transaction-local; a purge handler under tenant B cannot find A's request | PASS | `::test_worker_scope_is_never_set_by_the_api_and_purge_cannot_cross_tenants` |
| 19 Stage 3 object routes return 404 for another tenant's ids; 6 list endpoints and the `customer_id` filter return nothing; same-SKU stock and FX do not cross tenants; A's objects unchanged | PASS | `::test_every_stage3_route_returns_404_for_another_tenants_ids` |
| Idempotency keys are per tenant (same key in A and B → distinct runs) | PASS | `::test_idempotency_keys_are_per_tenant` |
| Authorization matrix: 24 routes × 5 roles, every role outside the permission gets 403 (+ multipart imports) and no state is left behind | PASS | `::test_authorization_matrix_denies_every_role_outside_the_permission` |
| Separation of duties: proposer ≠ confirmer (incl. repeat-proposal → order); deletion requester ≠ approver; `allow_self_approval` lifts both, as documented | PASS | `::test_separation_of_duties_on_orders_proposals_and_deletions` |
| Cost/margin hidden from sales/readonly on order detail, order list, profile, KPIs, stock and proposals; visible to approver; `export.denied` audited with role + entity | PASS | `::test_costs_and_margins_are_hidden_from_sales_and_readonly_everywhere` |
| Formula neutralisation in the failed-rows CSV (attacker-controlled header **and** values, incl. `\t`/`\r` prefixes) and in customer/order exports; `attachment` + `no-store` | PASS | `::test_failed_rows_csv_and_exports_neutralise_formulas_in_every_reflected_column` |
| Audit: 13 Stage 3 mutation kinds write an attributable row (actor + request id, no e-mail in the summary); failed mutations (422/403/409) write none and change nothing | PASS | `::test_every_stage3_mutation_writes_an_audit_row_and_failed_mutations_write_none` |
| Soft delete hides the customer from list, search, export, CSV-import matching and repeat proposals | PASS | `::test_soft_delete_hides_the_customer_from_lists_exports_and_imports` (gaps: SEC3-02) |
| Purge is idempotent (one `retention.purged` row; handler replay is a no-op); a legal hold set before the tick blocks enqueueing; hold cannot be toggled after execution | PASS | `::test_purge_is_idempotent_and_blocked_by_legal_hold` |
| Profile contacts show no more fields or rows than the Stage 2 contacts route | PASS | `::test_customer_profile_contacts_follow_the_stage2_contact_route_visibility` |
| Order prices are copied from the approved document (catalog tampered ×10 afterwards, totals unchanged; Decimal sums) | PASS | `::test_order_prices_are_copied_not_recomputed_and_totals_use_decimal` |
| CSV caps: > 5 MB → 413, > 10 000 rows → 413, non-UTF-8 → 422, key length 1..160, BOM tolerated, rejected uploads leave no run | PASS | `test_stage3_audit_inputs.py::test_csv_import_caps_and_encoding` |
| 1 500 order groups in one CSV: completes (< 60 s bound asserted), list endpoint capped at 200 | PASS | `::test_csv_order_import_with_many_groups_stays_bounded` |
| Connector: 5 001 records → 422, undeclared scope → 403, unknown entity/kind/scope → 422, wrong-typed records rejected per record, accounting-negative `(5)` rejected | PASS | `::test_connector_caps_scopes_and_types` |
| NaN / sNaN / Infinity in prices and quantities rejected per record (connector + CSV) | PASS | `::test_sec3_04_connector_rejects_hostile_records_per_record[…]`, `::test_sec3_04_csv_order_import_rejects_bad_rows_without_losing_the_run[quantity NaN/Infinity, negative tax]` |
| Conflict resolution: approver 403; `apply_remote` changes only the conflicted fields; customer conflicts cannot set `organization_id`/`deleted_at`/`reference` | PASS | `::test_connector_conflict_resolution_is_authorised_audited_and_scoped` |
| FX: NaN/Infinity/≤0/>1e6/non-numeric/future date/identical or unsupported pair/over-long source → 422 | PASS | `::test_fx_rate_validation` (precision gap: SEC3-08) |
| Stock: naive, future, unparseable and epoch timestamps rejected; negative quantity rejected; case-insensitive SKU; latest snapshot wins; SQLi/LIKE wildcards inert | PASS | `::test_stock_timestamps_and_sku_matching` |
| Order notes/reasons bounded (2 000), whitespace reason rejected, HTML/formula text stored inert | PASS | `::test_order_notes_and_reasons_are_bounded_and_inert` |
| CSRF enforced on all 17 Stage 3 POST routes (missing and wrong token → 403 `action=csrf`); exports `no-store`, `nosniff` | PASS | `::test_csrf_is_enforced_on_every_stage3_mutation` |
| The 500 from the SEC3-04 path does not leak driver or trace details | PASS | `::test_sec3_04_crash_path_does_not_leak_internals` |
| Stage 1/2 regression (tenant isolation, RBAC/CSRF/headers, parsers, logging redaction, audit follow-ups 1–3, Stage 1/2 integration and QA suites) | PASS | full suite run 4, § 7 (the only failures are QA's Stage 3 findings) |

## 6. Tool results

| Tool | Command (cwd) | Exit | Result |
|---|---|---|---|
| pip-audit | `.venv/bin/pip-audit --skip-editable` (apps/api) | 0 | `No known vulnerabilities found` (editable `quotepilot-api` skipped) |
| bandit | `.venv/bin/bandit -r app -q -f txt -ll` (apps/api) | 0 | no medium/high findings (empty output) |
| ruff (project rules incl. S*) | `.venv/bin/ruff check app --output-format concise` (apps/api) | 0 | `All checks passed!` |
| ruff S-only, app + tests | `.venv/bin/ruff check app tests --select S` (apps/api) | 1 | 2 169 hits, **all `S101`** (assert in tests; excluded by project policy); no other S rule fires |
| ruff on the new audit tests | `.venv/bin/ruff check …/test_stage3_audit*.py` + `ruff format --check` | 0 / 0 | clean (S608 on the f-string table loops is suppressed inline; table names come from a constant tuple) |
| npm audit (prod) | `npm audit --omit=dev` (apps/web) | 0 | `found 0 vulnerabilities` |
| npm audit (all) | `npm audit` (apps/web) | 1 | 16 moderate, all transitive `minimatch` via eslint / eslint-config-next (dev tooling, not shipped). Unchanged from `scan-20261102-004622.md` |
| built bundle grep | `grep -rIlE "sk-ant-\|AKIA…\|ANTHROPIC_API_KEY\|SECRET_KEY\|quotepilot_owner" .next/static .next/server`; `grep -rhoE "NEXT_PUBLIC_[A-Z_]+" .next/static` (apps/web) | 0 | no matches; no `NEXT_PUBLIC_*` variables. The build in `.next` predates the audit; the auditor did not rebuild it |
| repo secret grep | `grep -rInE "(sk-ant-…\|AKIA…\|BEGIN … PRIVATE KEY\|ghp_…)"` excluding node_modules/.venv/.next/.git/evidence | 0 | only synthetic fixtures in tests (`sk-ant-api03-abcdefghijklmnop…`) and the allow-listed sample in `.gitleaks.toml:15` |
| gitleaks / semgrep / trufflehog | `which …` | — | **NOT AVAILABLE** locally (CI-only jobs, not observed by the auditor). No clean result is assumed |

## 7. Test runs (verbatim counts)

| # | Command (cwd `apps/api`, env § 2) | Exit | Result |
|---|---|---|---|
| 1 | `.venv/bin/pytest -q -p no:cacheprovider tests/security/test_stage3_audit.py tests/security/test_stage3_audit_inputs.py -rA` | 0 | **35 passed, 32 xfailed** in 128.67 s (before the SEC3-09 test correction; see run 4) |
| 2 | `.venv/bin/pytest -q -p no:cacheprovider tests/security tests/integration tests/unit -ra` (2026-11-02, before the 3 added checks) | 0 | 246 passed, 1 skipped, 31 xfailed in 458.70 s |
| 3 | `--runxfail` on every finding test (isolated) | 1 (expected) | each failure matches its stated reason; assertion payloads quoted in § 4 |
| 4 | `.venv/bin/pytest -q -p no:cacheprovider tests -rA` (full API suite incl. the QA agent's untracked Stage 3 files, final, 2026-11-10) | 1 | **4 failed, 429 passed, 1 skipped, 32 xfailed** in 774.84 s. All 32 xfails are this audit's strict findings; no XPASS. The 4 failures are all in the QA agent's own Stage 3 files (QA findings, not regressions): `test_qa_stage3_data_io::test_oversized_field_values_are_rejected_per_row_not_as_a_crash` (same defect as **SEC3-04**), `test_qa_stage3_repeat_retention::test_approval_stops_processing_immediately_and_keeps_commercial_records` (same defect as **SEC3-02**), `test_qa_stage3_kpis::test_pending_value_includes_every_open_approved_quote_per_its_definition` and `test_qa_stage3_orders::test_order_totals_equal_the_approved_quote_document_with_line_discount` (order net 393.13 vs approved 393.12: `orders.py:130` folds the discount into `unit_price` and re-rounds; outside this audit's scope, see QA evidence). Every Stage 1/2 suite and every security suite passed. |

Per-file totals (final run): `test_stage3_audit.py` **16 passed, 3 xfailed**; `test_stage3_audit_inputs.py` **19 passed,
29 xfailed** (21 of them are SEC3-04 parametrised cases).

Process note (honest record): on 2026-11-10 the auditor started a diagnostic `--runxfail` run while a background
full-suite run was using the same database. The two collided (deadlock on TRUNCATE, duplicate users). The background
run was stopped (exit 144) and discarded, and run 4 was repeated with nothing else touching `quotepilot_sec3`.

## 8. Residual risks

| Risk | Status / owner |
|---|---|
| SEC3-01..04, 06..11 open | Assign to the implementer; re-test by removing the xfail markers (strict xfail turns any fix into a visible XPASS) |
| PII retained by design after purge: audit rows, approved quote documents/PDFs, RFQ files (KNOWN_LIMITATIONS) | Needs a documented legal basis in PRIVACY_AND_RETENTION (professional review), or extended purge (SEC3-03) |
| Push connectors trust the operator or external system holding admin credentials; no per-connector credential/HMAC, no rate limit on sync beyond `max_sync_records` | Accepted for the stub stage; real adapters need scoped credentials and rate limits |
| `organizations` table-wide UPDATE/DELETE grant for the runtime role (I-1) | Defence in depth; RLS holds today |
| Stock snapshots unbounded (append-only) | Storage growth only; documented |
| Worker scope (`app.worker=true`) relies on worker code discipline; any future handler that sets it widens `job_runs`/`follow_up_schedules`/`deletion_requests` visibility for that transaction | Keep `set_worker_scope` out of handlers; consider a separate DB role for the scheduler |

## 9. Not tested

- No live servers or browser run: the Stage 3 web pages were reviewed as code only. No Playwright spec was added
  (QA owns `tests/e2e/specs/qa-stage3-ui.spec.ts`).
- Concurrency races (simultaneous confirm/cancel, two purges, parallel imports with the same key). The order number
  uses an atomic row `UPDATE … RETURNING` (reviewed, not stress-tested).
- KPI numeric accuracy (QA scope); only tenant scoping and cost exposure were checked.
- Real ERP/CRM pull adapters (not shipped), OAuth for connectors, outbound network egress controls.
- Docker/compose topology, TLS, backups and restore.
- gitleaks/semgrep/trufflehog (not installed); no DAST tool.
- Statement-timeout behaviour of very large KPI ranges (capped at 3 years by code; not load-tested).

## 10. Run log (tail)

```
$ git log --oneline 8ec4cc3..HEAD
b4256fb evidence: security scan on Stage 3 branch          # evidence-only commit; product code unchanged

$ pytest … tests/security/test_stage3_audit.py tests/security/test_stage3_audit_inputs.py -rA
35 passed, 32 xfailed in 128.67s (0:02:08)                 # exit 0

$ pytest --runxfail … (selected finding tests)
E AssertionError: ['profile (incl. contacts) still readable', 'contacts still readable', 'new contact (PII) can be attached', 'customer still editable', 'new RFQ can be filed for the customer', 'new quote can be created for the customer', …]   # SEC3-02 (8 gaps)
E AssertionError: ['quote_versions.document_snapshot', 'sync_conflicts.local_value/remote_value', 'sync_runs.report.failed_rows']   # SEC3-03
E AssertionError: ('approved', False, 2026-11-09 16:43:57+00:00)   # SEC3-01: request never executed, job dead after 5 attempts
E AssertionError: (201, 1, '1307.25', '1307.25')                    # SEC3-09: order from superseded v1
E assert ('f4fc…' != 'f4fc…')                                       # SEC3-06: same run returned for another entity
E AssertionError: (200, 'catalog', '0.85')                          # SEC3-10: approved at the stale price, labelled catalog
E AssertionError: {"error":"invalid_state","detail":"A 'erp_stub' connector already exists"}   # SEC3-07
E psycopg.errors.CheckViolation: … "ck_fx_rate_positive"            # SEC3-08
E psycopg.errors.StringDataRightTruncation: value too long for type character varying(16)   # SEC3-04 apply_remote
E AssertionError: {'1e3': Decimal('13'), '1E+3': Decimal('13'), '2.5e2': Decimal('2.52'), …}   # SEC3-11
E assert Decimal('105') == Decimal('100')                           # SEC3-04 zero tax replaced by 5 %

$ psql … organizations grants (runtime role)
DELETE, INSERT, SELECT, UPDATE (table-wide); policy org_self USING (id = app.current_org)
```
