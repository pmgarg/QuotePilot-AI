# QA evidence — Stage 3 re-verification after fix round 1 (2026-11-12)

Independent QA run at HEAD `ee865da` (branch `claude/stage3-customer-order-intelligence`), worktree `/home/user/qp-stage3`.
Inputs read: defect/observation sections of `qa-stage3-2026-11-02.md`, `implementer-fixes1-stage3-2026-11-12.md` (claims,
not facts), the Stage 3 gate log. Product code was not modified; nothing committed; no test weakened or deleted.
Environment: Linux 6.18 container, no Docker, PostgreSQL 16 local, database `quotepilot_qa3b` (empty at start), roles
`quotepilot_app` (RLS enforced) / `quotepilot_owner` (migrations), credentials redacted, `APP_ENV=test`. Evidence layers:
**integration** = API + PostgreSQL + RLS through the ASGI TestClient; **browser** = Playwright against real uvicorn
(:8111) + Next.js (:3111).

## Runs
| # | Command (cwd) | Exit | Result |
|---|---|---|---|
| R1 | `.venv/bin/pytest -q -p no:cacheprovider tests/qa/test_qa_stage3_*.py` (`apps/api`), before the new file existed | 0 | **46 passed**, 0 failed, 65 s |
| R2 | `.venv/bin/pytest -q -p no:cacheprovider tests/qa/test_qa_stage3_confirm.py` (`apps/api`), last run | 1 | 8 passed, **1 failed** (D6), 10.8 s |
| R3 | **full API suite** `.venv/bin/pytest -q -p no:cacheprovider -rfEX` (`apps/api`), run once, background process polled to completion | 1 | **1646 passed, 1 failed, 2 skipped, 3 xfailed, 1 xpassed** in 1250.9 s (20:50). The failure is QA's D6 test; every pre-existing test passed. Implementer/CI: 1635 passed / 5 skipped / 3 xfailed / 1 xpassed. Arithmetic: CI 1635 + 9 new tests = 1644 passed; here 1646 passed + 1 failed = 1647, i.e. 3 more tests ran here than there, exactly the 3 fewer skips (skip reasons are environment-gated; not investigated). XPASS: `tests/security/test_audit_followups8.py::test_sec7_1_...` (Stage 1, not Stage 3). |
| R4 | **full browser suite** `npx playwright test --reporter=list --trace off` (`tests/e2e`, `DATABASE_URL`/`DATABASE_ADMIN_URL` = qa3b, `E2E_API_PORT=8111 E2E_WEB_PORT=3111`) | 0 | **62 passed, 1 skipped**, 0 failed, 0 flaky, 3.3 min. `qa-reverify-review1.spec.ts:230` (a11y scan) passed first time; no violation to report. No isolation re-run was needed. |
| R5 | `.venv/bin/ruff check tests/qa/test_qa_stage3_confirm.py`; `ruff format --check ...` | 0 / 0 | clean |
| R6 | exploratory API session, scratch script outside the repo (`PYTHONPATH=. .venv/bin/python <scratchpad>/explore.py`, in-process TestClient, new tenant) | 0 | see log |

Env slip, disclosed: one `pytest` invocation (to re-run the confirm file after an edit) was issued **without the
exported environment**, so `tests/conftest.py` fell back to the default `quotepilot_test` database. It aborted in the
session fixture (`alembic upgrade head`: "Can't locate revision identified by '0008'", another branch's migration was
in that database), i.e. before any test body ran; it executed no truncation or test data. The database was not
inspected or modified by me afterwards. All reported runs R1-R6 used `quotepilot_qa3b`.

## Checklist
| # | Item | Verdict | Evidence |
|---|---|---|---|
| 1 | D1 order totals = approved quote with a line discount | CONFIRMED fixed | `test_order_totals_equal_the_approved_quote_document_with_line_discount` passes (R1); order copies 393.12 / 19.66 / 412.78. |
| 1 | D2 pending value | CONFIRMED fixed | `test_pending_value_includes_every_open_approved_quote_per_its_definition` passes; approved + sent quote both counted (2 / 892.50). |
| 1 | D3 over-long CSV cell | CONFIRMED fixed | `test_oversized_field_values_are_rejected_per_row_not_as_a_crash` passes; 201, 1 applied, 2 rejected with reasons. |
| 1 | D4 deletion-approved customer accepts processing | CONFIRMED fixed | `test_approval_stops_processing_immediately_and_keeps_commercial_records` passes; new RFQ/quote/contact/proposal -> 404 for sales, admin, owner (confirm test). |
| 1 | D5 2xx before commit | CONFIRMED fixed | `test_qa_stage3_consistency.py` (2 tests) pass in R1/R3; browser suite had no stale-page failure. |
| 2 | Test edits legitimate, property kept | CONFIRMED (see below) | diff `6c6fa2c..HEAD` reviewed; docs present. |
| 3 | S3-A01..A09 | CONFIRMED for all nine | table below. |
| 4 | Order totals vs approved document | CONFIRMED | AED, JPY, KWD, BHD each with a 7.5 % line discount and a 0 % tax line: `test_order_totals_equal_approved_document_for_discount_zero_tax_and_minor_units[...]` (4 params). No header-level discount exists in the model: `test_no_header_level_discount_exists_on_the_quote_document` (nothing to copy). |
| 5 | Retention | CONFIRMED except one residual copy (D6) | hidden from lists/export, 4 new-processing routes 404 for 3 roles, hold blocks approval and due purge, purge pseudonymises, conflicts/failed-row values/snapshot/PDF redacted, quotes+orders remain. D6: the reference stays in a failed row's error text. |
| 6 | Full runs | CONFIRMED | R3, R4. |
| 7 | Exploratory session | done, see log | no product defect; observations O12-O14. |

## Judgement of QA-test edits (diff `6c6fa2c..HEAD`, 3 files, `qa-stage3-ui.spec.ts` unchanged)
1. `connectors_stock::test_sync_is_idempotent_per_key_even_when_the_payload_changes` and `data_io::test_import_idempotency_key_semantics`
   (replay with a different payload: 201-first-run -> **409**, plus an identical replay still returns the first run).
   Legitimate consequence of a deliberate change, not a weakening: the original property (second payload never applied;
   exactly one audit row; stock still 10 / "Alpha Two" absent) is still asserted, and a new assertion covers the
   identical-replay path. **Judgement: 409 is the better behaviour** (a silent 201 hides that the client's data was
   dropped); consistent with API_CONTRACT line 7 ("State/concurrency -> 409"); documented in `docs/API_CONTRACT.md`
   lines 144-146 ("same key + different payload -> 409 (never a silent no-op)"). The PRD (S3-A07) is silent on key
   reuse, so it is not contradicted.
2. `connectors_stock::test_customer_master_data_differences_wait_in_the_queue`: reason string `reference and name required`
   -> `reference is required`. Legitimate (per-field validation); the property (the record is rejected with a reason
   and nothing is overwritten) is still asserted. Mild: the test still checks exact reason text, so it is coupled to wording.
3. `repeat_retention::test_requester_cannot_approve_and_legal_hold_blocks_approval_and_purge` and
   `::test_approval_stops_processing_immediately_and_keeps_commercial_records`: sales read of the erased customer / profile
   -> 404, reads moved to admin. Legitimate, with one narrowing: QA originally also cared that a record-keeping view of
   the subject stays reachable; that is still asserted (admin sees `deleted_at` on customer and profile; the purged
   name `Deleted customer <id8>` is still asserted through admin; quotes and orders remain reachable to sales). The
   new 404 for sales is itself asserted, so it is not a silent weakening. **Judgement: 404 for sales is acceptable**
   (data minimisation after an erasure approval; S3-F08 and the PRD do not grant sales a view of a customer being
   erased) and is documented in `docs/API_CONTRACT.md` lines 150-152. Trade-off to note: sales can still open the
   quote/order, which carry the customer id, but cannot open the customer; the UI should handle that 404 gracefully
   (O13). The edit comments say "Orchestrator edit" while the implementer evidence calls them implementer edits;
   attribution is inconsistent but harmless.

## Acceptance criteria (PRD §Stage 3)
| ID | Verdict | Covering tests |
|---|---|---|
| S3-A01 KPIs match fixtures | PASS | `qa_kpis::test_kpi_figures_match_hand_computed_fixture`, `::test_pending_value_includes_...`, `::test_date_range_boundaries_and_validation`, integration `test_kpis_match_fixture_and_apply_currency_rule` |
| S3-A02 cost/margin hidden | PASS | `qa_orders::test_cost_price_never_in_order_payloads_for_sales_and_readonly`, `qa_data_io::test_cost_columns_only_for_view_costs_roles_and_margin_arithmetic`, `security::test_costs_and_margins_are_hidden_from_sales_and_readonly_everywhere`; browser `qa-stage3-ui` tests 1-2 |
| S3-A03 multi-currency rules | PASS | `qa_kpis::test_fx_rule_uses_the_latest_rate_on_or_before_the_quote_date`, `::test_fx_inverse_pair_and_table_validation`; browser `stage3-intelligence` S3-F04 |
| S3-A04 sync conflict -> queue | PASS | `qa_connectors_stock::test_customer_master_data_differences_wait_in_the_queue`, `::test_product_cost_difference_is_a_conflict_not_visible_to_sales`; integration `test_sync_conflicts_never_overwrite_master_data`; browser S3-F06 |
| S3-A05 stale stock labelled | PASS | `qa_connectors_stock::test_stock_labels_follow_source_timestamps_not_sync_order`, `::test_csv_connector_source_is_labelled_...`; browser S3-F07 |
| S3-A06 unauthorised export 403 + audit | PASS | `qa_data_io::test_unauthorized_export_is_403_and_audited_with_actor_and_entity`; integration `test_bulk_export_is_role_gated_audited_and_formula_safe` |
| S3-A07 partial import recovery | PASS | `qa_data_io::test_partial_import_then_fix_and_reupload_the_failed_rows_file`, `::test_oversized_field_values_...`, `::test_orders_import_creates_proposed_orders_...`; browser `qa-stage3-ui` data page test |
| S3-A08 repeat-order audit trail | PASS | `qa_repeat_retention::test_proposal_is_never_executed_and_leaves_a_complete_audit_trail`, integration `test_repeat_order_proposal_has_evidence_and_full_audit_trail`; browser `qa-stage3-ui` test 2 |
| S3-A09 cross-tenant regression | PASS | `qa_tenancy` (4 tests: 18 id routes -> 404, RLS on all Stage 3 tables, per-tenant keys, worker scope), `security::test_every_stage3_route_returns_404_for_another_tenants_ids` |
All pass in R3 (integration layer; browser rows in R4). "PASS" is QA's verdict, not a gate verdict.

## Exploratory session (API level, ~30 min incl. scripting; no UI click-through beyond the browser suite)
1. Sales confirms an order -> 403; second order for the same quote -> 409; cancel without reason -> 422; fulfil a `proposed` order -> 409.
2. 4 approvers confirm the same order concurrently -> exactly one 200, three 409.
3. KPIs: `date_from > date_to` -> 422; 40-year range -> 422; garbage date -> 422.
4. Customer named `=HYPERLINK(...)` exported -> cell prefixed with `'`.
5. Imports: empty file, binary bytes, header-only, semicolon-delimited + BOM, unknown entity (`invoices`) -> 422/422/201 (0 rows)/422/404.
6. Connector sync: `records` as a string -> 422; stock record dated year 2999 -> run 201 (record rejected per record, not inspected further); new SKU -> conflict.
7. Conflicts: `apply_remote` on a new SKU -> 409; dismiss -> 200; dismiss again -> 409.
8. FX rate `0`, `-1`, `abc` -> 422; `1e3` -> accepted as 1000 (inside the documented bound).
9. Stock `q=%` -> `[]` (wildcard escaped); `q` of 5000 chars -> 422.
10. Retention: duplicate open request -> 409; approver (no manage_retention) approves -> 403; admin approves -> 200; reject after approval -> 409.
11. After approval, customer profile and contacts: sales/approver/readonly 404, admin 200; quote for the erased customer -> 404; the old quote still readable (200).
12. Customers CSV with a row whose reference equals the erased customer's -> 201 (what the row did was not inspected: owner-role psql sees no rows under RLS; covered by `test_soft_delete_hides_the_customer_from_lists_exports_and_imports`).
Nothing wrong was seen beyond the observations below.

## Defects
### D6 — Purge leaves the erased customer's reference in an import failed row's error text (Low)
- Test: `apps/api/tests/qa/test_qa_stage3_confirm.py::test_no_sync_run_report_keeps_the_erased_customers_identifiers` (fails, no xfail).
- Where: `apps/api/app/services/intelligence/data_io.py:257` builds `errors.append(f"reference {ref} already exists")`; `retention.py:211-226` (`_redact_copies`) redacts `failed_rows[].values` only, not `errors`.
- Request: admin imports a customers CSV row (reference `CUST-1`, bad currency `ZZZ`) -> 201, 1 rejected; erasure approved and purged by the worker.
- Expected: after purge no run report contains the subject's reference (the purge nulls `customers.reference`).
- Actual: `{"failed_rows":[{"row":2,"errors":["unsupported currency ZZZ","reference CUST-1 already exists"],"values":{"redacted":"erased"}}]}`.
  Name, e-mail and values are redacted; the reference (an external business key) survives. Severity Low: identifier only, reachable by owner/admin via `/data/runs`.
  The sibling test `::test_worker_purge_pseudonymises_customer_and_redacts_copies_but_keeps_quotes_and_orders` passes.
- Not checked: error texts built from other fields (e.g. e-mail) in the same module.

## Observations (one line each)
- O12 — Customers CSV with `;` delimiter or an empty file is answered "Missing required column(s): name" (generic; no hint about delimiter/empty file).
- O13 — Sales can open a quote/order of an erased customer but gets 404 on the customer/profile; the web page for that case was not exercised.
- O14 — Header-only CSV import creates a `csv_import` run with 0 rows (201) and consumes the idempotency key.
- O15 — Skip count differs from CI (2 here vs 5); not investigated.
- Earlier observations O1-O11 of the first QA report were not re-examined except O4 (fixed per the implementer; not retested here).

## Not checked
- UI behaviour of the erased-customer 404 for sales; UI click-through for the exploratory items (API level only).
- Real worker runner loop (`python -m app.workers.runner`) - purge was driven in-process via `enqueue_due_purges` + `queue.run_once`.
- SEC3-01..11 as a security re-verification (security auditor's scope); only the behaviours named in the checklist.
- KPI performance, stock snapshot growth, connector sync-run reports of kind `connector` for subject identifiers (the D6 test query covers all `sync_runs` kinds and flagged only `csv_import`).
- CI results (taken from the implementer evidence only for comparison).
