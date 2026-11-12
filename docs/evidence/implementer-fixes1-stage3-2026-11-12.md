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

## Runs
See the runs table appended below after the merge with the Stage 2 branch (full API suite, lint, web build, browser suite).
