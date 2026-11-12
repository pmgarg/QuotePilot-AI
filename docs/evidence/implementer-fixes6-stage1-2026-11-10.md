# Implementer — Stage 1 fix round 6 (after release review 3)

Base: `d0aac67`. Input: `release-review-3-stage1-2026-11-02.md` (verdict INCOMPLETE / BLOCKED; R3-1..R3-10). Self-reported;
independent QA and security re-verification of this round are still required (R3-1).

| Finding | Change | Test |
|---|---|---|
| R3-2 CI not recorded | Push and pull_request runs of d0aac67 recorded in `RELEASE_GATES.md`; I-3 status stated (not root-caused) | — |
| R3-3 compose start-up | README/runbook use `docker compose --env-file .env -f infra/docker-compose.yml …`; `scripts/check_compose.py` renders the topology and fails if `api`/`web`/`worker` get owner credentials, an `env_file` or a non-empty `DATABASE_ADMIN_URL`; CI step + `make compose-check`. Verified locally: without `--env-file` Compose aborts on `SECRET_KEY` (fails closed, as the reviewer predicted); with it the check prints `compose check: OK`. Compose was still not *executed* (no daemon). | CI step |
| R3-4 undeclared test edit | Round-5 evidence corrected | — |
| R3-5 stale traceability | Stage 1 rows carry review-3 verdicts and current figures; S1-A rows split as the reviewer grouped them | — |
| R3-6 no UI path for `catalog_version_mismatch` | Editor shows **Use active catalog** on a stale line: it asks the server for the active version (`GET /catalogs/active-version`, new), finds the same SKU and re-links; the server re-prices on save. A *pending* ad-hoc quote whose catalog moved on must be rejected and re-versioned (documented, human sign-off requested). | e2e `review3-regressions` R3-6; integration `test_r3_10_pending_ad_hoc_quote_with_a_stale_catalog_line_cannot_be_approved` |
| R3-7 RLS-only lookup | `stale_catalog_lines` filters `Product.organization_id` explicitly | existing suite |
| R3-10 tests | withdrawn-documents banner browser test; stale pending quote approval test; NEW-15 test reads tenant-scoped rows instead of the cumulative `n_tup_ins` counter and drops the vacuous NULL check; the auditor's always-skipping repro test removed as its docstring instructed | as listed |
| R3-8, R3-9 (Info) | Not changed; noted in the gate log | — |

## Runs (local PostgreSQL 16, DB `quotepilot_test`)
| # | Command | cwd | Exit | Result |
|---|---|---|---|---|
| 1 | `ruff check app tests && ruff format --check app tests && mypy app` | apps/api | 0 | clean |
| 2 | `pytest -q -p no:cacheprovider --cov=app` | apps/api | 0 | **330 passed**, coverage 90 % (before the `active-version` endpoint was added; the endpoint is covered by the browser test below) |
| 3 | `tsc --noEmit && eslint src && npm run build` | apps/web | 0 | clean |
| 4 | `npx playwright test` (first attempt) | tests/e2e | 1 | 26 passed, 2 failed: the R3-6 spec clicked Save before the asynchronous re-link finished, and then could not restore the demo catalog, which broke the next spec's prices. Fixed: wait for the re-link notice; restore in `finally`; ask the server for the active version. |
| 5 | `npx playwright test` | tests/e2e | 0 | **28 passed, 1 skipped** (screenshots) |
| 6 | `docker compose --env-file .env -f infra/docker-compose.yml config` + `scripts/check_compose.py` | repo | 0 | `compose check: OK` |
