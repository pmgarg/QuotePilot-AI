# Implementer — Stage 3 (customer & order intelligence) — self-reported, not independently verified

Branch `claude/stage3-customer-order-intelligence` (based on Stage 2 `cf8d409`, which includes Stage 1 fix round 5).
Scope: S3-F01..F08, S3-A01..A09 (PRD §Stage 3). Nothing here counts as VERIFIED until QA, the security auditor and
the independent release reviewer have recorded their own evidence.

## What was built
- Migration `0007_stage3_intelligence`: `orders`, `order_lines`, `order_events`, `order_proposals`, `stock_snapshots`,
  `fx_rates`, `connector_connections`, `sync_runs`, `sync_conflicts`, `deletion_requests` (all ENABLE + FORCE RLS,
  tenant policy; `deletion_requests` also under the worker scope), `organizations.order_seq`, `customers.deleted_at`.
  Grants only on the new tables. Up/down/up verified on an empty database.
- Services `app/services/intelligence/`: `orders` (approval-gated conversion, copied prices, events, separation of
  duties), `repeat_orders` (own-history heuristic, evidence, decision → proposed order), `kpis` + `fx` (definitions,
  date range, reporting currency, unconverted bucket), `data_io` (formula-safe role-aware exports, partial imports with
  a fix-and-re-upload CSV), `connectors` (declared scopes, idempotent push sync, conflict queue, no silent overwrite),
  `stock` (fresh/stale/unknown labels), `retention` (request → approve → soft delete → worker purge, legal hold).
  Queue handler `purge_customer`; worker tick `enqueue_due_purges`.
- Router `routers/intelligence.py` (see API_CONTRACT); permissions `approve_order`, `manage_connectors`,
  `import_data`, `export_data`, `manage_retention` added to the matrix.
- Web: `/orders`, `/orders/[id]`, `/insights`, `/data`, `/customers/[id]`; "Convert to order" on approved quotes.
- Docs: DATA_MODEL, API_CONTRACT, ROADMAP, KNOWN_LIMITATIONS (Stage 3 section), OPERATIONS_RUNBOOK,
  REQUIREMENTS_TRACEABILITY (Stage 3 rows = IMPLEMENTED), `.env.example` (Stage 3 settings), `docs/openapi.json`.

## Runs (APP_ENV=test, local PostgreSQL 16, DB `quotepilot_s3`, worktree `/home/user/qp-stage3`)
| # | Command | cwd | Exit | Result |
|---|---|---|---|---|
| 1 | `.venv/bin/ruff check app tests && .venv/bin/ruff format --check app tests && .venv/bin/mypy app` | apps/api | 0 | clean; "no issues found in 83 source files" |
| 2 | `.venv/bin/pytest -q -p no:cacheprovider` | apps/api | 1 → 0 | first run **353 passed, 1 failed, 1 skipped** (the auditor's RLS inventory test lists tenant tables explicitly; the ten Stage 3 tables were added to its list — they *are* RLS-forced, the inventory was stale); re-run of that test: 1 passed. Stage 3 file: 13 passed. |
| 3 | `npm run build` (after `tsc --noEmit`, `eslint src`) | apps/web | 0 | "Compiled successfully" |
| 4 | `npx playwright test --reporter=list` (E2E_API_PORT=8061, E2E_WEB_PORT=3061, DB quotepilot_s3) | tests/e2e | 1 → 0 | first run 31 passed, 2 failed (two assertions in the new Stage 3 spec matched the wrong rendering: `money()` prints no currency code, `Badge` renders `erp stub`); after correcting the spec: `specs/stage3-intelligence.spec.ts` **4 passed**; full suite 29 others passed, 1 skipped |
| 5 | `alembic upgrade head` / `downgrade 0006` / `upgrade head` | apps/api | 0 | migration reversible |

## Not done / honest gaps
- No independent QA, security audit or release review yet (gate INCOMPLETE).
- `make security-scan` not yet run on this branch (will be recorded under `docs/evidence/scan-*.md` before the gate).
- See KNOWN_LIMITATIONS §Stage 3 (push-model connectors, no cross-sell, append-only stock snapshots, RFQ files not
  purged, no sequence roll-over).
