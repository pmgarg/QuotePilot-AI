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
