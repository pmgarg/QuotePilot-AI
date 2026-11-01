# Requirements Traceability Matrix

Status: NOT STARTED / PARTIAL / IMPLEMENTED / VERIFIED / BLOCKED. **Only VERIFIED counts as done** (QA evidence +
security evidence + independent release-reviewer PASS recorded in RELEASE_GATES.md). Updated by the orchestrator only
from recorded evidence in `docs/evidence/`.

## Stage 1
| requirement_id | design_reference | code_reference | test_id | test_result/evidence | security_check | reviewer_verdict | open_issue | status |
|---|---|---|---|---|---|---|---|---|
| X-01 tenant isolation | ADR-002, HLD trust boundaries | `app/db/session.py::set_tenant`, migration 0001 RLS policies, `services/*` org filters | security/test_tenant_isolation.py (API 404 matrix, raw-SQL RLS, app role NOBYPASSRLS); e2e cross-tenant 404 | 104/104 api tests pass (implementer evidence #5); e2e 6/6 | RLS forced; WITH CHECK on insert | pending (reviewer not yet invoked) | | IMPLEMENTED |
| X-02 RBAC | USER_JOURNEYS matrix, LLD authz | `services/authz.py`, `api/deps.py::Principal.require` | security/test_rbac_csrf_headers::test_vertical_privilege_matrix; integration tests per route; e2e read-only | pass | matrix tests | pending (reviewer not yet invoked) | | IMPLEMENTED |
| X-03 audited high-impact actions | LLD audit | `services/audit.py` called in every mutation | integration/test_quotes::test_audit_trail_for_every_mutation, history tests | pass | append-only grant verified | pending (reviewer not yet invoked) | | IMPLEMENTED |
| X-04 no paid dependency | ADR-009 | `services/ai/provider.py` DisabledProvider default | unit/test_extract_and_exports::test_ai_disabled_by_default_and_strict_parsing | pass |  | pending (reviewer not yet invoked) | | IMPLEMENTED |
| X-06 Decimal money | ADR-004 | `services/pricing.py`, `services/money.py` | unit/test_pricing (12 cases), integration totals/JPY/KWD | pass |  | pending (reviewer not yet invoked) | | IMPLEMENTED |
| S1-F01 orgs & roles | DATA_MODEL | `models/org.py`, routers/org members | integration/test_auth_and_org::test_members_crud_and_last_owner_guard, test_role_change_revokes_sessions | pass | last-owner guard | pending (reviewer not yet invoked) | | IMPLEMENTED |
| S1-F02 auth | ADR-003 | `services/auth.py`, `routers/auth.py`, `core/security.py` | integration/test_auth_and_org (cookie, csrf, generic error, rate limit, logout, reuse); security headers test; e2e invalid login | pass | argon2id, HttpOnly/SameSite, CSRF, 429 | pending (reviewer not yet invoked) | | IMPLEMENTED |
| S1-F03 org settings | PRD | `routers/org.py` | integration/test_auth_and_org::test_org_settings_role_gates | pass | sales 403, owner-only self-approval | pending (reviewer not yet invoked) | | IMPLEMENTED |
| S1-F04 catalog import/versioning | LLD catalog | `services/catalog_import.py` | integration/test_catalog (6 tests); e2e UI import | pass | formula flags, zip bomb 413, macro 415, oversize 413 | pending (reviewer not yet invoked) | | IMPLEMENTED |
| S1-F05 customers | PRD | `routers/customers.py` | integration/test_auth_and_org::test_customers_crud_and_validation | pass | SQLi-shaped search | pending (reviewer not yet invoked) | | IMPLEMENTED |
| S1-F06 RFQ intake & parsing | ADR-005, LLD parsing | `services/parsing/*`, `services/rfqs.py` | integration/test_rfq_flow (file kinds, unsupported, corrupted, scanned, JS pdf, hidden prompt, limits); security/test_parsers_and_logging | pass | subprocess + rlimits + timeout | pending (reviewer not yet invoked) | | IMPLEMENTED |
| S1-F07 extracted items & confidence | LLD matching | `models/rfq.py`, `routers/rfqs.py`, web review page | integration/test_rfq_flow::test_text_rfq_parsed_matched_and_sourced; e2e | pass |  | pending (reviewer not yet invoked) | | IMPLEMENTED |
| S1-F08 unit/SKU normalisation | LLD | `services/units.py` | unit/test_units_and_matching; integration unit mismatch | pass |  | pending (reviewer not yet invoked) | | IMPLEMENTED |
| S1-F09 matching | ADR-006 | `services/matching.py` | unit/test_units_and_matching (exact/token/ambiguous/unmatched); integration | pass |  | pending (reviewer not yet invoked) | | IMPLEMENTED |
| S1-F10 human verification | PRD | `services/rfqs.py::update_item`, review UI | integration/test_rfq_flow::test_review_edits_and_generation; e2e | pass |  | pending (reviewer not yet invoked) | | IMPLEMENTED |
| S1-F11 deterministic pricing | ADR-004 | `services/pricing.py`, `services/quotes.py::_recompute` | unit/test_pricing; integration totals, JPY/KWD, discount | pass |  | pending (reviewer not yet invoked) | | IMPLEMENTED |
| S1-F12 blockers & override | LLD pricing | `services/quotes.py::compute_blockers/margin_override` | integration/test_quotes::test_margin_floor_requires_privileged_override, test_currency_mismatch_blocks_autofill | pass | sales 403 on override | pending (reviewer not yet invoked) | | IMPLEMENTED |
| S1-F13 versions & concurrency | ADR-010 | `services/quotes.py::_check_lock/new_version` | integration/test_quotes::test_concurrent_update_conflict, test_new_version_after_approval_keeps_history; e2e conflict banner | pass |  | pending (reviewer not yet invoked) | | IMPLEMENTED |
| S1-F14 approval workflow | HLD state machine | `services/quotes.py::submit/approve/reject` | integration/test_quotes::test_submit_approve_flow_with_separation_of_duties, test_self_approval_blocked_unless_org_allows, test_reject…; e2e | pass | SoD 403 | pending (reviewer not yet invoked) | | IMPLEMENTED |
| S1-F15 withdrawn/expired | HLD | `services/quotes.py::withdraw/effective_status` | integration/test_quotes::test_withdraw_and_expired | pass |  | pending (reviewer not yet invoked) | | IMPLEMENTED |
| S1-F16 PDF/CSV export | ADR-007 | `services/exports/*` | unit pdf determinism + csv neutralisation; integration/test_quotes::test_pdf_csv_exports_deterministic_and_role_aware, test_csv_export_neutralises_formulas; e2e identical bytes | pass | cost hidden for sales/readonly | pending (reviewer not yet invoked) | | IMPLEMENTED |
| S1-F17 history & search | PRD | `routers/quotes.py` list/history | integration/test_quotes::test_quote_search_and_list, test_audit_trail… | pass | SQLi-shaped search | pending (reviewer not yet invoked) | | IMPLEMENTED |
| S1-F18 audit | LLD audit | `services/audit.py` | as X-03 | pass |  | pending (reviewer not yet invoked) | | IMPLEMENTED |
| S1-F19 fixtures | PRD | `app/fixtures/seed.py`, `scripts/make_fixtures.py` | seed used by e2e global setup | pass | synthetic only | pending (reviewer not yet invoked) | | IMPLEMENTED |
| S1-F20 UI & states | USER_JOURNEYS | `apps/web/src/app/(app)/**` | e2e: permission states, conflict banner, failed-parse banner (unit of UI logic), XSS | pass (6/6) | no cost columns for sales | pending (reviewer not yet invoked) | | IMPLEMENTED |
| S1-F21 AI adapter | ADR-009 | `services/ai/*` | unit parse/redaction tests; no live call | pass | disabled default | pending (reviewer not yet invoked) | | PARTIAL (no live provider test by design) |
| SEC-headers/CSRF/cookies | SECURITY_POLICY | `main.py` middleware, `api/deps.py::csrf_protect` | security/test_rbac_csrf_headers | pass |  | pending (reviewer not yet invoked) | | IMPLEMENTED |
| NFR CI | ADR-014 | `.github/workflows/ci.yml`, `security.yml` | not yet observed on GitHub | pending first push |  | pending (reviewer not yet invoked) | | PARTIAL |

## Stage 2 (designed)
| requirement_id | design_reference | status |
|---|---|---|
| S2-F01..F11, S2-A01..A08 | PRD §Stage 2, LLD §Stage 2, DATA_MODEL later tables, ADR-012 | NOT STARTED |

## Stage 3 (designed)
| S3-F01..F08, S3-A01..A09 | PRD §Stage 3, LLD §Stage 3 | NOT STARTED |

## Stage 4 (designed)
| S4-F01..F07, S4-A01..A09 | PRD §Stage 4, LLD §Stage 4, ADR-013 | NOT STARTED |
