# Implementer evidence — Stage 3 fix round 1 (2026-11-12)

Branch `claude/stage3-customer-order-intelligence`. Inputs: the independent security audit
(`security-stage3-2026-11-02.md`, SEC3-01..11) and the independent QA report (`qa-stage3-2026-11-02.md`, D1..D5, O1..O11).
This file is written by the implementing side; it is **not** a verification. Independent QA and security re-verification
and the release review are still owed. Nothing here is VERIFIED.

## Security findings
| Finding | Change | Where |
|---|---|---|
| SEC3-01 approved erasure could stall after a dead purge job | `enqueue_due_purges` re-queues with keys `purge:{request}:{n}` up to `MAX_PURGE_JOBS = 10`, then flags `purge_stalled` | `services/intelligence/retention.py` |
| SEC3-02 customer pending erasure still readable/processable | `services/customers.py::get_active_customer` (404 unless `include_deleted`, which only `manage_retention` roles get on read routes); used by customers, follow-up, RFQ, intelligence routers and `quotes.create_quote` | `services/customers.py`, routers |
| SEC3-03 copies of the subject outside the customer row | purge redacts connector conflicts and import failed rows, pseudonymises the customer block of stored quote snapshots, deletes stored PDFs (re-rendered on demand) | `retention.py::_redact_copies` |
| SEC3-04/05 unvalidated import and sync records (500s, over-long values, non-finite numbers) | `services/intelligence/inputs.py` (`text_field`, `decimal_field`, `order_line`, bounds); per-row savepoints catching `DBAPIError`; connector handlers validate every record | `inputs.py`, `data_io.py`, `connectors.py` |
| SEC3-06 idempotency key shared across entities / silently answered with a different payload | keys namespaced by `(kind, connector, entity)`; request hash stored; different payload → 409 | `data_io.py::scope_key/find_run/replay_or_conflict`, migration `0007b` (`uq_sync_run_scope_idem`) |
| SEC3-07 duplicate connectors of a kind under concurrency / no re-enable | partial unique index `uq_connector_org_kind_active`; `POST /connectors/{id}/enable` | migration `0007b`, `routers/intelligence.py` |
| SEC3-08 FX rate bounds | `rate` constrained to `[0.00000001, 1000000]`, 8 decimals | `schemas/common.py` |
| SEC3-09 order from a superseded approved version | only the current approved version converts; totals copied from it | `services/intelligence/orders.py` |
| SEC3-10 in-place catalog price change vs open quotes | blocker `catalog_price_changed` on draft/pending catalog-priced lines | `services/quotes.py::repriced_catalog_lines` |
| SEC3-11 scientific notation misparsed by the shared parser | fixed in Stage 1 fix round 7 (`services/money.py`); reaches this branch through the Stage 1 merge | Stage 1 |

## QA defects
| Defect | Change | Where |
|---|---|---|
| D1 order totals ≠ approved quote with a line discount | orders copy subtotal/discount/tax/grand total from the approved version instead of recomputing | `orders.py` |
| D2 `pending_value` ignored sent/acknowledged/awaiting_customer quotes | `OPEN_AFTER_APPROVAL` statuses | `kpis.py` |
| D3 over-long cell → HTTP 500 | per-row validation and savepoints (same change as SEC3-04) | `inputs.py`, `data_io.py`, `connectors.py` |
| D4 customer pending erasure accepted new RFQs/contacts | `get_active_customer` (same change as SEC3-02) | routers |
| D5 (cross-stage) 2xx sent before the transaction committed | DB dependency is function-scoped: commit/rollback happens when the path operation returns, before the response is sent; a failed commit now surfaces as an error response | `api/deps.py` (the same change is made on the Stage 1 branch as fix round 8) |
| O4 `last_order_at` took the first confirmed order in list order | latest `confirmed_at` | `routers/intelligence.py` |

Observations not changed in this round (documented, not defects): O1 (quantity string form in proposals), O2 (audit row
order inside one transaction), O5 (name/notes truncation on customer import is kept; reference/email are rejected),
O6 (formula neutralisation round trip), O7 (200-row caps without pagination), O8 ("applied" wording), O9 (currency code
in tables), O10/O11 (browser-suite data accumulation and an order-dependent Stage 2 spec).

## Verifier test edits (declared)
Tests written by the independent verifiers were changed only where two verifiers' expectations contradicted each other
or where a fixed finding's `xfail` marker had to go. Each edit carries a comment in the test. Re-verification must judge them.
- `tests/security/test_stage3_audit.py`, `tests/security/test_stage3_audit_inputs.py`: `xfail` markers removed for fixed
  findings (the helper `_xf` now returns no marker); the audit-coverage test's setup was reordered so that it creates its
  connector before disabling another one (one active connector per kind).
- `tests/qa/test_qa_stage3_connectors_stock.py::test_sync_is_idempotent_per_key_even_when_the_payload_changes` and
  `tests/qa/test_qa_stage3_data_io.py::test_import_idempotency_key_semantics`: QA expected a replay with a *different*
  payload to return the first run (201); the security audit required it to be refused. Decision: **409**; an identical
  replay still returns the first run, and the property QA checked (nothing from the second payload is applied) is kept
  and still asserted.
- `tests/qa/test_qa_stage3_connectors_stock.py::test_customer_master_data_differences_wait_in_the_queue`: rejection
  reason is now per field (`reference is required` instead of `reference and name required`).
- `tests/qa/test_qa_stage3_repeat_retention.py` (two tests): QA read a customer pending erasure as *sales*; after
  SEC3-02 sales gets 404 and only `manage_retention` roles read it. The tests now assert the 404 for sales and read
  through admin. All other assertions are unchanged.
- `tests/integration/test_stage3_intelligence.py` (implementer's own): the retention test asserts sales 404 / admin sees `deleted_at`.
Product messages were restored so that QA's other exact-string expectations hold unchanged
(`tax_rate_pct must be between 0 and 100`, `unsupported currency <code>`, `unknown customer_reference <ref>`).

## Found while merging Stage 2 forward
- `retention.enqueue_due_purges` bound the cross-tenant worker scope itself. Stage 2's security audit
  (`tests/security/test_stage2_audit.py::test_worker_scope_has_no_caller_outside_the_queue_module`, S2-SEC-01) allows
  that only inside `services/jobs/queue.py`. The cross-tenant scan now lives there (`queue.due_purge_requests`);
  retention switches to the owning tenant's scope for everything else. The test was the single failure of the full
  API suite on the merged tree (below) and passes after the change, unedited.
- A fully red browser run on the first merged tree (every page "couldn't load") was a harness artifact — the web build
  was replaced under a running server — fixed on the Stage 1 branch (build inside the web-server command) and merged.

## Runs (2026-11-12; DB `quotepilot_s3fix` recreated empty so the migration chain 0006 → 0006b → 0007 → 0007b runs from scratch; ports 8081/3081)
| Tree | Command (cwd) | Exit | Result |
|---|---|---|---|
| merge with Stage 2 @27b252f (before round 9) | full API suite | 1 | 1 failed, 856 passed, 2 skipped, 2 xfailed (failing test not named in that run's output; the `-r` flag hid it) |
| same | full browser suite | 1 | harness artifact, see above — not a product result |
| 869883a (Stage 2 @be754fd incl. Stage 1 round 9) | `ruff check` / `ruff format --check` / `mypy app` (`apps/api`) | 0 / 0 / 0 | clean; 139 files formatted; 85 source files |
| same | `.venv/bin/pytest -q -p no:cacheprovider -rfEx` (full API suite) | 1 | **1 failed** (`test_worker_scope_has_no_caller_outside_the_queue_module`), **1281 passed**, 2 skipped, 1 xfailed in 1049 s |
| same | `npx tsc --noEmit`, `npx eslint .` (`apps/web`) | 0 / 0 | clean |
| same | `npx playwright test --reporter=list --trace off` (`tests/e2e`) | 0 | **60 passed, 1 skipped**, 0 failed, 0 flaky |
| after the worker-scope change | `.venv/bin/pytest -q -p no:cacheprovider tests/security/test_stage2_audit.py tests/security/test_stage3_audit.py tests/integration/test_stage3_intelligence.py tests/qa/test_qa_stage3_repeat_retention.py tests/qa/test_qa_stage3_tenancy.py` | 0 | 75 passed, 1 xfailed (S2-SEC-06, accepted) |

A full API run on the tree that contains the worker-scope change has **not** been made on this branch; it is made on
the Stage 4 branch after the merge (recorded there) and is owed here by the independent re-verification.
The Stage 1 CI issue noted in the Stage 2 evidence (five QA tests reading `DATABASE_ADMIN_URL_TEST` directly) applies
to this branch's `api` job as well until the corrected file is merged.

## Runs after merging Stage 2 with Stage 1 fix round 10 (commit 620835e; DB `quotepilot_s3fix`, ports 8081/3081; other suites running)
| Command (cwd) | Exit | Result |
|---|---|---|
| `ruff check` / `ruff format --check` / `mypy app` (`apps/api`) | 0 / 0 / 0 | clean; 141 files formatted; 85 source files |
| `.venv/bin/pytest -q -p no:cacheprovider -rfEx` (full API suite) | 0 | **1436 passed, 2 skipped, 2 xfailed** in 1430 s (xfails: S2-SEC-06 accepted in Stage 2; Stage 1 SEC6-2 open) |
| `npx tsc --noEmit`, `npx eslint .` (`apps/web`) | 0 / 0 | clean |
| `npx playwright test --reporter=list --trace off` (`tests/e2e`) | 0 | **60 passed, 1 skipped**, 0 failed, 0 flaky |

This is the first full API run of this branch that includes the worker-scope change (`queue.due_purge_requests`); it
supersedes the note above that such a run was still owed by the implementing side. The Stage 1 CI timing issue
described in the Stage 2 evidence applies to this branch's `api` job as well until Stage 1 fix round 11 is merged.

## CI after merging Stage 1 fix round 11 and the Stage 2 fixes (PR #3, queried 2026-11-12): all 15 checks green
`api` success: pytest `1635 passed, 5 skipped, 3 xfailed, 1 xpassed`; `e2e` success: Playwright `61 passed`, `1 skipped`,
**`1 flaky`** — `qa-reverify-review1.spec.ts:230` (the accessibility scan across six screens) failed once and passed on
its retry in one of the two runs; not investigated. `web`, `security` jobs and the CodeQL check success (the inherited
`py/polynomial-redos` alerts are gone). Before this merge the PR's CI was red (inherited Stage 1 timing test; the
browser click failures and CodeQL alerts described in the Stage 2 evidence).
Independent re-verification and the release review are still owed.

## Fix round 2 (security re-verification findings SEC3-12..17)
Branch `stage3-fix1`; the verifier's tests/evidence were committed unmodified first (ae7a7b4). Nothing is marked VERIFIED.

| Finding | Change (file) | Tests |
|---|---|---|
| SEC3-12 | `services/intelligence/orders.py::propose_from_quote` and `repeat_orders.py::decide` (approve branch only) call `get_active_customer` -> 404 for a customer pending erasure; reject of a proposal stays possible | `test_stage3_confirm.py::test_sec3_12_*` (2), `test_stage3_fixes2.py::test_open_proposal_of_an_erased_customer_can_still_be_rejected` |
| SEC3-13 | `api/routers/quotes.py::list_quotes`: `customer_name` null and name search excludes the customer unless `manage_retention`. Quote detail has no name field; the customer profile was already 404 for other roles | `test_sec3_13_*`, `test_stage3_fixes2.py::test_erased_customer_name_stays_visible_to_retention_roles_and_hidden_in_search` |
| SEC3-15 | `retention.py::_redact_copies`: `communication_events` linked by quote or contact lose `subject`, `snippet`, `payload` (replaced by `{"redacted":"erased"}`); rows kept; count `communication_events_redacted` in summary and `retention.purged` audit | `test_sec3_15_*`, `test_stage3_fixes2.py::test_purge_redacts_event_text_keeps_rows_and_counts_them` |
| SEC3-17 | `orders.py::get_order(for_update=True)` (`SELECT ... FOR UPDATE`, `populate_existing`), used by `_transition` in `api/routers/intelligence.py` so confirm, fulfil and cancel all serialise; loser gets 409 | `test_sec3_17_*`, `test_stage3_fixes2.py::test_concurrent_confirm_loser_gets_409_with_one_event_and_one_audit_row` |
| SEC3-16 | `connectors.py::sync`: run insert in a savepoint; on `IntegrityError` re-read the run and `replay_or_conflict` (same payload: existing run, different: 409). CSV imports (`data_io._start_run`) have the same pattern and were NOT changed | `test_sec3_16_*` |
| SEC3-14 | not touched (Stage 2 queue visibility timeout arrives by merge); xfail left in place | `test_sec3_14_*` still xfail |

Declared decorator removals in `tests/security/test_stage3_confirm.py` (decorators only, no assertion touched): the `xfail`
of `test_sec3_12_existing_quote_of_an_erased_customer_cannot_start_a_new_order`,
`test_sec3_12_open_proposal_of_an_erased_customer_cannot_become_an_order`,
`test_sec3_13_quote_list_does_not_show_the_name_of_a_customer_pending_erasure`,
`test_sec3_15_purge_clears_the_sent_message_snippet_that_names_the_contact`,
`test_sec3_17_two_concurrent_confirmations_of_one_order_are_recorded_once`,
`test_sec3_16_connector_sync_replayed_concurrently_is_applied_once_without_a_500`.

Not covered by the purge (checked in code): `rfq_items.raw_text` (only `rfqs.pasted_text` is nulled); documented.

**Open: one existing test fails because of SEC3-12.** `tests/security/test_stage3_audit.py::test_every_stage3_mutation_writes_an_audit_row_and_failed_mutations_write_none`
fails at line 584 (`KeyError: 'id'`): `o = sales.post(f"/quotes/{q['id']}/orders", json={}).json()` now gets 404 because the
test approves the erasure (line ~550) before ordering from that customer's quote. It was not edited; the fix is to move that
`POST /quotes/{id}/orders` before the erasure approval (the verifier's evidence section 4 already anticipated this).

| Command (cwd `apps/api`, env as mandated, DB `quotepilot_s3fix`) | Exit | Result |
|---|---|---|
| `pytest -q -p no:cacheprovider tests/security/test_stage3_confirm.py tests/integration/test_stage3_fixes2.py` run 1/2/3 | 0 | 15 passed, 1 xfailed (each run; 30.3 s, 29.2 s, 29.7 s) |
| `pytest -q -p no:cacheprovider` on confirm, audit, audit_inputs, intelligence, `tests/qa/test_qa_stage3_*.py`, fixes2 | 1 | 140 passed, 1 failed (the audit test above), 1 xfailed, 244 s |
| same audit file + confirm + fixes2 with `-k "not every_stage3_mutation"` (after the mypy fix) | 0 | 33 passed, 1 deselected, 1 xfailed |
| `ruff check app tests` / `ruff format --check app tests` / `mypy app` | 0 / 0 / 0 | clean / 151 files formatted / no issues in 87 files |

### Orchestrator addendum to fix round 2 — one verifier test reordered (declared)
`tests/security/test_stage3_audit.py::test_every_stage3_mutation_writes_an_audit_row_and_failed_mutations_write_none`
proposed an order from a quote of the customer whose erasure it had just approved; SEC3-12 now refuses that (404).
The order is proposed before the approval instead (two lines moved, comment in the test); no assertion changed. The
security re-verification's own evidence anticipated this. After the edit:
`pytest tests/security/test_stage3_audit.py tests/security/test_stage3_confirm.py` → 30 passed, 1 xfailed (SEC3-14,
which is fixed in the Stage 2 job queue and arrives by merge).

### Orchestrator addendum — SEC3-14 and QA D6, after merging the Stage 2 queue fix (2026-11-12)
- **SEC3-14** (purge job left `running` by a crashed worker): the Stage 2 job queue now has a visibility timeout
  (`queue.reclaim_abandoned`, merged from the Stage 2 branch). `retention.enqueue_due_purges` calls it for purge jobs
  first (`kind="purge_customer"`), so an abandoned purge job is put back in the queue (or, with attempts exhausted,
  marked dead and re-queued by the existing logic). `reclaim_abandoned` got a `kind` filter and a `count_dead` flag.
  The auditor's test passes; its strict-xfail decorator was removed (declared; assertion unchanged).
- **QA D6** (Low; `test_qa_stage3_confirm.py::test_no_sync_run_report_keeps_the_erased_customers_identifiers`): the
  purge now also replaces the *error texts* of failed import rows that mention the subject
  (`retention.py::_redact_copies`), not only their values. QA's test passes unedited.
- Merge conflicts with the Stage 2 branch (`.env.example`, `core/config.py`, `services/jobs/queue.py`,
  `docs/RELEASE_GATES.md`) were resolved by keeping both sides.
- Run after these changes: `.venv/bin/pytest -q -p no:cacheprovider tests/qa/test_qa_stage3_*.py
  tests/security/test_stage3_confirm.py tests/security/test_stage3_audit.py tests/security/test_stage3_audit_inputs.py
  tests/integration/test_stage3_fixes2.py tests/integration/test_stage3_intelligence.py` → exit 0, **151 passed**,
  no xfail. `ruff check`, `ruff format --check`, `mypy app` clean. The full API and browser suites on this tree run
  in CI (recorded in the gate log).

## Fix round 3 (after release review 1; orchestrator, 2026-11-12)
Scope: S3-RR1-4 (CSV same-key race), S3-RR1-3 in part (connector run rejection reasons redacted on purge), S3-RR1-9
(multi-round confirm race in the implementing side's test), S3-RR1-5..7 (documents). No verifier test was edited.

| Change | File |
|---|---|
| run row inserted in a savepoint; a lost race raises `ReplayedRun`, the endpoint answers as a sequential replay | `app/services/intelligence/data_io.py`, `app/api/routers/intelligence.py` |
| `_redact_copies` walks `sync_runs` of kind `connector` too; counter `sync_rejections_redacted` | `app/services/intelligence/retention.py` |
| three tests added/changed | `tests/integration/test_stage3_fixes2.py` |

Runs (database `quotepilot_s3fix`, `APP_ENV=test`, at 096d9d4):

| Command | Exit | Result |
|---|---|---|
| `uv run pytest -q tests/integration/test_stage3_fixes2.py` | 0 | `6 passed` |
| `uv run ruff check app tests && uv run ruff format --check app tests && uv run mypy app` | 0 | `All checks passed!`, `155 files already formatted`, `Success: no issues found in 87 source files` |
| `uv run pytest -q -p no:cacheprovider` (whole API suite; collected before the C3 verifier files existed) | 0 | `1687 passed, 2 skipped, 5 xfailed in 1505.45s (0:25:05)` |

A first attempt at the whole-suite run was stopped by the 10-minute background limit of the tooling before it
finished (no result); the run above is the repeat with a longer limit. The five expected failures are Stage 1/2 items
(none in Stage 3). Browser suite: not run by the implementing side in this round (the QA C3 re-check runs it).
