# Implementer — Stage 1 fix round 5 (after QA re-verification 2 and security re-verification 2)

Base: `58976f3`. Inputs: `qa-stage1-reverify2-2026-11-01.md` (D2-1..D2-5, O2-1..O2-5) and
`security-stage1-reverify2-2026-11-01.md` (NEW-13..NEW-17, I-1, I-3). The two independent agents' test files were
committed as delivered except for the edits listed under "Changes to the auditors' tests" (each edit is a remedy
choice, not a weakening, and is explained there).

## Fixes

| Finding | Severity | Change | Regression test |
|---|---|---|---|
| D2-1 / NEW-17 — every editor save relabelled catalog lines `manual` | High / Medium | `services/quotes.py::update_draft`: a `unit_price` equal to a `catalog` line's current price is ignored (provenance stays `catalog`); re-linking a product (re)prices from the catalog first, so a re-sent list price never becomes manual. Editor (`quotes/[id]/page.tsx`) omits `unit_price` for catalog lines the user did not touch (`price_touched`). | QA `test_editor_style_resave_of_an_untouched_catalog_line_keeps_the_catalog_price_and_raises_no_blocker`; sec `test_editor_resave_of_an_unchanged_catalog_price_keeps_price_source_catalog`; browser "S1-F11/N2-1: saving a quantity edit…" |
| D2-2 — unpinned draft unsaveable after a catalog activation | Medium | The N2-1 B version check runs only when the line's `product_id` actually changes; untouched lines are never re-validated on save. | QA `test_n2_1b_unpinned_draft_still_saves_after_a_catalog_activation_when_lines_are_resent_by_the_editor` |
| NEW-13 (part 2) — archived-version prices pass submission | Low | New blocker `catalog_version_mismatch` (`stale_catalog_lines`): a `catalog` line whose product is not in the quote's pricing version blocks submit/approve and is listed on `GET /quotes/{id}`; RFQ-pinned quotes are unaffected (pin documented in KNOWN_LIMITATIONS, O2-1). | sec `test_new13_archived_catalog_lines_are_blocked_or_repriced_on_submit` |
| NEW-13 (part 1) — RFQ without a frozen version accepted inactive products | Low | `services/rfqs.py::update_item`: when the RFQ has no frozen version only products of the *active* version can be selected. | sec `test_new13_rfq_without_catalog_version_refuses_products_of_inactive_versions` |
| D2-3 / NEW-14 (b, c) — 0005 data fix was a no-op (FORCE RLS) and keyed on the wrong column | Medium | New migration `0005b_withdrawal_data_fix`: relaxes FORCE RLS on the three tables for the statement, withdraws every approved version `<=` the last version with a `withdrawn` decision, restores FORCE. Idempotent; 0005 no longer contains the statement. | QA `test_l2_5_migration_0005_data_fix_*` (2); sec `test_new14_l2_5_data_fix_is_idempotent_against_round_4_data`, `…covers_the_legacy_withdraw_then_new_version_pattern` |
| D2-5 / NEW-14 (a) — `withdrawn_reason` persisted after a later approval | Low | `approve()` clears `quote.withdrawn_reason`. | QA `test_n2_2_approver_withdraws_v1_while_v2_draft_is_open…` (last assertion); sec `test_new14_withdraw_while_draft_then_approve_clears_the_withdrawal_marker` |
| D2-4 — withdraw-while-draft unreachable in the UI | Medium | Quote page shows the Withdraw control whenever an approved version exists (or the current one is pending), and an "Earlier document withdrawn" banner (`withdrawn-documents`) when any version is withdrawn while the header is not. | browser "N2-2: an approver can withdraw the approved v1 from the quote screen while a newer draft is open" |
| NEW-15 — platform-scope revocation audit rows unreadable | Low | `scripts/revoke_sessions.py` writes one tenant-scoped `ops.sessions_revoked` row per affected organisation (`summary.count` per org, `summary.total` overall), binding `app.current_org` before each insert. | sec `test_new15_platform_scope_revocation_audit_row_is_readable_by_an_operator` (remedy-adjusted, see below) |
| NEW-16 — `api` container inherited `QP_OWNER_PASSWORD` via `env_file` | Low | `infra/docker-compose.yml`: `api` has an explicit `environment:` allow-list and no `env_file`; `DATABASE_ADMIN_URL: ""` kept explicitly. `migrate` keeps `env_file`. | sec `test_new16_compose_api_service_receives_no_owner_credentials` |
| I-1 — `X-Forwarded-Port` passed through | Info | Added to the proxy's dropped `FORWARDING` set. | (static) |
| O2-4 — misleading 422 text | Obs | Message now names the pricing version ("the RFQ's version, or the active version for ad-hoc quotes"); `test_review2_fixes` assertion updated. | `test_n2_1b_products_from_inactive_catalog_versions_cannot_be_linked` |
| O2-1 / O2-2 / O2-3 | Obs | Documented in `docs/KNOWN_LIMITATIONS.md`; API contract rows for `PATCH /quotes/{id}`, `GET /quotes/{id}` and blocker codes updated. | — |
| I-3 — PR-triggered e2e run failed on 58976f3 | Open | Not root-caused: the trace artifact host is blocked from the development container (`CONNECT tunnel failed, 403`). Log analysis: failures began in the first spec with 404/409 responses to calls that followed successful creates and an `ERR_ABORTED` navigation — consistent with the web server returning errors mid-suite, not with a product rule (the push run of the same commit passed all 22). Mitigation: two specs now assert intermediate API calls with the response body; both CI runs on the new HEAD are required for the gate. | — |

## Changes to the auditors' tests (transparent)
- `tests/security/test_audit_followups3.py`: removed the eight `xfail(strict=True)` markers of fixed findings (they
  XPASS-failed the suite after the fixes); `L2_5_DATA_FIX` now loads `DATA_FIX` from migration 0005b (the statement
  moved); `test_new13_archived_catalog_lines_…` asserts the D2-2 behaviour (re-save 200 + blocker, submit 409) instead
  of a 422 on re-save; `test_new15_…` asserts the chosen remedy (one tenant-scoped row, readable in the tenant scope,
  no NULL-organisation rows).
- `tests/qa/test_qa_reverify_review2.py` and `tests/e2e/specs/qa-reverify-review2.spec.ts`: unchanged.
- `tests/integration/test_review2_fixes.py`: assertion text updated for the corrected 422 message (O2-4).

## Runs (this round, APP_ENV=test, local PostgreSQL 16, DB `quotepilot_test`)
| # | Command | cwd | Exit | Result |
|---|---|---|---|---|
| 1 | `.venv/bin/ruff check app tests && .venv/bin/ruff format --check app tests && .venv/bin/mypy app` | apps/api | 0 | clean; mypy "no issues found in 58 source files" |
| 2 | `.venv/bin/pytest -q -p no:cacheprovider --cov=app --cov-report=term` | apps/api | 0 | **329 passed, 1 skipped** in 431 s; coverage 90 % (the skip is the auditor's NEW-13 repro, superseded by the fix) |
| 3 | `npx tsc --noEmit && npx eslint src && npm run build` | apps/web | 0 | clean |
| 4 | `npx tsc --noEmit -p .` | tests/e2e | 0 | clean |
| 5 | `E2E_SKIP_WEB_BUILD=1 E2E_API_PORT=8051 E2E_WEB_PORT=3051 npx playwright test --reporter=list` | tests/e2e | 0 | **26 passed, 1 skipped** (screenshots) in 1.7 min — includes the QA's 5 new review-2 browser tests and the 2 previously failing D2-1/D2-4 scenarios |
| 6 | `alembic upgrade head` / `downgrade 0005` / `upgrade head` (via the suite's migrated-database fixture and the two migration replay tests) | apps/api | 0 | 0005b applies, is idempotent and withdraws the legacy shapes (QA L2-5 tests + sec NEW-14 b/c) |

A first local e2e attempt in this round ran concurrently with the API suite against the same database and failed
25/27 with "Invalid email or password" (the API suite truncates the tables the e2e run had just seeded). That was a
self-inflicted collision, not a product defect; run 5 above is the sequential re-run. CI runs both jobs on separate
runners.
