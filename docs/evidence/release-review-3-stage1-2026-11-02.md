# Independent release review 3 — Stage 1 @d0aac67 (2026-11-02)

**Release:** Stage 1 (RFQ → verified quotation) · commit `d0aac67cd2020ac98deeb02661e8ef6444e1aec7` · branch
`claude/multi-stage-project-impl-8hz2wj` · review started 2026-11-02 and finished after an API-limit interruption
(HEAD re-confirmed as `d0aac67` on resumption, working tree clean).

**Reviewer:** an independent agent with a fresh context and the read-only role from `.claude/agents/release-reviewer.md`.
It used only Read/Glob/Grep plus read-only `git log/show/diff`, `ls`, `cat`, `grep`, `sed -n` and `wc`. It did not run
any test, start any server, query GitHub or edit any file except this report. It did not implement, test or audit
any of the code under review.

**Scope reviewed:** every file changed in `58976f3..d0aac67` (commits 597f4b0, 4cef4d4, e090089, d0aac67), plus
the code paths those changes touch:
- `apps/api/app/services/quotes.py`: `update_draft`, `compute_blockers`, `stale_catalog_lines`, `submit`,
  `approve`, `withdraw`, `new_version`, `_pricing_catalog_version_id`
- `apps/api/app/services/rfqs.py::update_item`
- `apps/api/app/api/routers/quotes.py::quote_out`
- migrations `0001`, `0002`, `0005` and `0005b`, plus `alembic/env.py`
- `scripts/revoke_sessions.py`, `infra/docker-compose.yml`, `apps/api/Dockerfile` and `apps/api/app/core/config.py`
- the installed uvicorn 0.54 `FORWARDED_ALLOW_IPS` handling
- `apps/web/src/app/(app)/quotes/[id]/page.tsx` and `apps/web/src/app/api/[...path]/route.ts`
- `apps/api/app/main.py`, `.github/workflows/ci.yml` and `tests/e2e/playwright.config.ts`

**Tests read:** `apps/api/tests/qa/test_qa_reverify_review2.py`, `apps/api/tests/security/test_audit_followups3.py`,
`tests/integration/test_review2_fixes.py`, `tests/e2e/specs/qa-reverify-review2.spec.ts`,
`review1-regressions.spec.ts`, and the FORCE-RLS assertions in `security/test_audit_followups.py` and
`security/test_tenant_isolation.py`.

**Documents read:** `QuotePilot_Claude_Code_Master_Prompt.md` (via CLAUDE.md and the role file), `docs/PRD.md`
(X-01..09, S1-F01..F21, S1-A01..A20), `REQUIREMENTS_TRACEABILITY.md`, `KNOWN_LIMITATIONS.md`, `API_CONTRACT.md`
(diff), `RELEASE_GATES.md`, `OPERATIONS_RUNBOOK.md`, `README.md`, release reviews 1 and 2, `implementer-fixes4`,
`qa-stage1-reverify2`, `security-stage1-reverify2`, `implementer-fixes5` and `scan-20261101-232659.md`.

## Executive verdict

**Overall verdict: INCOMPLETE. Decision: BLOCKED (not releasable as PASS).**

- **Code review result.** I found no Critical, High or Medium code defect in the round-5 changes. The D2-1/D2-2
  semantics in `update_draft`, the `catalog_version_mismatch` blocker, migration `0005b`, `approve()` clearing
  `withdrawn_reason`, the `rfqs.update_item` version check, the per-org audit rows in `revoke_sessions.py`, the I-1
  proxy fix and the web withdraw/banner logic are correct as I read them.
  - A manual price cannot become `catalog`: the price is reset to the product's immutable `list_price` before the
    equality check.
  - An archived or inactive product cannot be newly linked.
  - An untouched stale link cannot be submitted or approved.
  - `unit_price: null` only restores the catalog price; any confirmation is voided when the price changes.
- **Why it is not PASS.** The mandatory gates are not met on `d0aac67`:
  1. **No independent QA or security re-verification of the round-5 code exists** (R3-1). The only evidence that
     D2-1..D2-5 and NEW-13..NEW-17 are fixed is the implementer's own run of the agents' tests. The newest security
     scan is for `80aaf7f`.
  2. **CI on `d0aac67` is reported but not verified by me and not recorded in the repository** (R3-2). Run IDs
     41369753932 and 41369758251 appear only in the orchestrator's brief, not in any evidence file. I-3 (the flaky
     PR-triggered e2e run) is not root-caused.
  3. **The documentation gate fails** (R3-5): `REQUIREMENTS_TRACEABILITY.md` was not updated for round 4 or round 5.
  4. Carried-over items remain open:
     - X-08 has no manual assistive-technology audit.
     - X-09: the compose topology has never been executed, and round 5 added an unverified start-up risk (R3-3).
     - No human has acknowledged the documented Lows (L2-7).
     - Branch protection is not confirmed.

## Traceability (mandatory template)

Legend: **PASS** means the code path is unchanged since an independent verification, which still applies. **INCOMPLETE
(code OK)** means I reviewed the round-5 change and found no defect, but no independent QA, security or CI evidence on
the candidate has been recorded.

| requirement_id | design_reference | code_reference | test_id | test_result/evidence | security_check | reviewer_verdict | open_issue |
|---|---|---|---|---|---|---|---|
| X-01 tenant isolation | ADR-002, HLD | `db/session.py`, RLS 0001; FORCE restored by 0005b `finally` | security/test_tenant_isolation, test_audit_followups (FORCE on all tenant tables, runs after the QA replay), QA `test_withdraw_cannot_be_used_to_leak…` | independent QA R1/R5 on 58976f3; implementer 329 passed + 1 skipped on HEAD (not independent) | RLS FORCE; 0005b relaxes FORCE only inside the migration transaction under ACCESS EXCLUSIVE | PASS | R3-7 (new `stale_catalog_lines` query relies on RLS only; Info) |
| X-02 RBAC | USER_JOURNEYS | `services/authz.py`, `deps.py` | security/test_rbac_csrf_headers; QA withdraw role matrix | unchanged since review 2; QA on 58976f3 | server-side; UI `canWithdraw` is cosmetic, server checks `WITHDRAW_QUOTE` | PASS | — |
| X-03 audited high-impact actions | LLD audit | `services/audit.py`; `scripts/revoke_sessions.py` per-org rows | integration audit tests; sec `test_new3…`, `test_new15…` | endpoints unchanged; script change only implementer-run | per-org rows readable in tenant scope | PASS (endpoints) / INCOMPLETE (ops script) | R3-9 Info |
| X-04 no paid dependency | ADR-009 | `services/ai/provider.py` | unit AI-disabled test | unchanged | compose now sets `AI_PROVIDER` default `disabled` | PASS | — |
| X-05 synthetic data & consent | PRIVACY_AND_RETENTION | fixtures, `routers/org.py` | test_review1_fixes::test_x05… | unchanged | — | PASS | — |
| X-06 Decimal money | ADR-004 | `pricing.py`, `money.py`; `Decimal` equality on `Numeric(18,6)` in `update_draft` | unit/test_pricing etc. | unchanged | no floats introduced | PASS | — |
| X-07 audit detail | PRD | `audit.py`; request log now adds `request_id` and `ms` (`main.py`) | qa audit tests | path only, no query/body in the log | no PII added | PASS | — |
| X-08 accessible UI | USER_JOURNEYS | `apps/web/src` | e2e axe scans | no manual screen-reader or responsive audit | — | INCOMPLETE | carried over |
| X-09 Docker + CI | ADR-014 | `infra/docker-compose.yml`, `.github/workflows` | sec `test_new16…` (static) | compose never executed; CI on HEAD unverified by me | api env allow-list | INCOMPLETE | R3-2, R3-3 |
| S1-F01 orgs & roles | DATA_MODEL | `routers/org.py` | qa org tests | unchanged | — | PASS | — |
| S1-F02 auth | ADR-003 | `services/auth.py`; proxy `FORWARDING` + `x-forwarded-port` | auth integration, SEC-09 e2e | unchanged except I-1 (static) | — | PASS | — |
| S1-F03 org settings | PRD | `routers/org.py` | test_org_settings_role_gates | unchanged | — | PASS | — |
| S1-F04 catalog import | LLD | `catalog_import.py` | test_catalog | unchanged; `list_price` written only at import (immutable per version) | — | PASS | — |
| S1-F05 customers | PRD | `routers/customers.py` | customers CRUD test | unchanged | — | PASS | — |
| S1-F06 RFQ intake | ADR-005 | `services/parsing/*` | test_rfq_flow, security parsers | unchanged | — | PASS | — |
| S1-F07 items & confidence | LLD | `routers/rfqs.py` | test_rfq_flow | unchanged | — | PASS | — |
| S1-F08 unit/SKU normalisation | LLD | `services/units.py` | unit tests | unchanged | — | PASS | — |
| S1-F09 matching | ADR-006 | `services/matching.py` | unit + integration | unchanged | — | PASS | — |
| S1-F10 human verification | PRD | `rfqs.py::update_item` (only the active version is selectable when there is no frozen version) | sec `test_new13_rfq_without_catalog_version…`; QA `test_n2_1b_rfq_review…` | implementer-run only | inactive/archived refused (422) | INCOMPLETE (code OK) | R3-1 |
| S1-F11 deterministic pricing | ADR-004 | `quotes.py::update_draft` lines 465-516 | QA `test_editor_style_resave…`, `test_n2_1a_*`; sec `test_editor_resave…`; e2e "S1-F11/N2-1" | implementer-run only | no manual→catalog path; cross-currency → `none` | INCOMPLETE (code OK) | R3-1, R3-6 |
| S1-F12 blockers & override | LLD | `compute_blockers` + `stale_catalog_lines`; `margin_override` | sec `test_new13_archived…`, `test_price_override_is_invalidated…` | implementer-run only | blocker checked in `submit` and `approve` (db passed) | INCOMPLETE (code OK) | R3-1, R3-6 |
| S1-F13 versions & concurrency | ADR-010 | `_check_lock`, `new_version` (copies pin + lines; stale lines then blocked) | test_quotes conflict tests; sec `test_new13_repro_new_version…` | implementer-run only | — | INCOMPLETE (code OK) | R3-1 |
| S1-F14 approval workflow | HLD | `approve()` (clears `withdrawn_reason`; blockers with db) | sec `test_new14_withdraw_while_draft…`; QA N2-2 tests | implementer-run only | SoD unchanged | INCOMPLETE (code OK) | R3-1 |
| S1-F15 withdrawn/expired | HLD | `withdraw()`, migration `0005b`, web `canWithdraw`/banner | QA L2-5 ×2, N2-2 ×4; sec NEW-14 b/c; e2e D2-4 | implementer-run only | per-version status; exports 409 | INCOMPLETE (code OK) | R3-1, R3-8 |
| S1-F16 PDF/CSV export | ADR-007 | `services/exports/*` | export tests, e2e identical bytes | unchanged | — | PASS | — |
| S1-F17 history & search | PRD | `routers/quotes.py` list/history | test_quotes search/list | unchanged; `quote_out` now passes `db` | — | PASS | — |
| S1-F18 audit in transaction | LLD | `audit.record` in each service mutation | as X-03 | unchanged for endpoints | — | PASS | — |
| S1-F19 fixtures | PRD | `fixtures/seed.py` | seed in e2e | unchanged | synthetic | PASS | — |
| S1-F20 UI & states | USER_JOURNEYS | `quotes/[id]/page.tsx` (withdraw control, banner, `price_touched`) | e2e QA spec (D2-1, D2-4) | implementer-run only; the banner has no assertion | React escapes `withdrawn_reason` | INCOMPLETE (code OK) | R3-1, R3-10 |
| S1-F21 AI adapter | ADR-009 | `services/ai/*` (not wired) | unit | no live provider by design | disabled default | INCOMPLETE (by design) | — |
| S1-A01, A02, A04–A10, A12–A14, A16–A19 | PRD acceptance | as rows above | test_rfq_flow, test_quotes, test_catalog, security/*, qa/* | code paths unchanged since independent verification | — | PASS | — |
| S1-A03 custom line, privileged explicit price | PRD | `update_draft` manual-price rules | sec `test_price_override…`, `test_n2_1a_cross_currency…` | implementer-run only | owner/admin confirmation required | INCOMPLETE (code OK) | R3-1 |
| S1-A11 currency mismatch blocks auto-fill | PRD | `update_draft` currency branch | QA `test_n2_1a_*` | implementer-run only | — | INCOMPLETE (code OK) | R3-1 |
| S1-A15 expired/withdrawn | PRD | `withdraw`, `0005b`, `_check_exportable` | QA N2-2/L2-5, sec NEW-14 | implementer-run only | — | INCOMPLETE (code OK) | R3-1 |
| S1-A20 browser E2E | PRD | `tests/e2e/specs/stage1-flow.spec.ts` | e2e suite (26 passed + 1 skipped per implementer) | CI not verified by me; I-3 flake not root-caused | — | INCOMPLETE | R3-2 |

### Open items from review 2 and from the re-verification round

| item | code / test | reviewer_verdict |
|---|---|---|
| N2-1 A (other-currency re-link) | `update_draft` currency branch; QA + sec tests | INCOMPLETE (code OK); round 5 re-touched this code |
| N2-1 B (inactive/archived link) | `link_changed` guard + `catalog_version_mismatch`; `rfqs.update_item` | INCOMPLETE (code OK) |
| N2-2 (withdraw all versions; withdraw while draft) | `withdraw()`; web `canWithdraw` | INCOMPLETE (code OK) |
| L2-1 (NEW-7 redirect variants) | e2e specs (independently verified on 58976f3; only the logout settle was added) | PASS |
| L2-2 / NEW-16 (owner credentials in the api container) | compose allow-list, no `env_file` | INCOMPLETE: static only; see R3-3 |
| L2-3 (blocker reveals cost ratio) | KNOWN_LIMITATIONS | Documented; awaiting human acknowledgement |
| L2-4 (identity triggers) | 0005 unchanged apart from the removed statement | PASS |
| L2-5 / D2-3 / NEW-14 b,c (legacy withdrawal data fix) | migration `0005b` | INCOMPLETE (code OK); R3-8 Info |
| L2-6 (documentation drift) | traceability, gate log | FAIL (R3-5, Low) |
| L2-7 (human sign-off of Lows) | RELEASE_GATES "Human actions required" | OPEN (human) |
| D2-1 / NEW-17 (save relabels catalog lines manual) | `update_draft` 510-516 + editor `price_touched` | INCOMPLETE (code OK) |
| D2-2 (unpinned draft unsaveable after activation) | version check only when `link_changed` | INCOMPLETE (code OK); R3-6 usability |
| D2-4 (withdraw-while-draft missing in UI) | `page.tsx` `canWithdraw` | INCOMPLETE (code OK) |
| D2-5 / NEW-14 a (stale `withdrawn_reason`) | `approve()` line 682 | INCOMPLETE (code OK) |
| NEW-13 (inactive/archived prices via RFQ and stale lines) | `rfqs.update_item`; `stale_catalog_lines` | INCOMPLETE (code OK) |
| NEW-15 (unreadable platform-scope audit rows) | per-org rows in `revoke_sessions.py` | INCOMPLETE (code OK); R3-9 Info |
| I-1 (`X-Forwarded-Port` passed through) | `route.ts` `FORWARDING` | PASS (static; trivial change) |
| I-3 (flaky PR-triggered e2e) | diagnostics only (log piping, response bodies in asserts) | OPEN |

### Changes the implementer made to the agents' tests

| Edit | Judgement |
|---|---|
| Strict `xfail` markers removed from `test_audit_followups3.py` (fixed findings) | Legitimate and required, since strict xfail fails on XPASS. |
| `L2_5_DATA_FIX` now loaded from `0005b` | Legitimate: the statement moved there. The comment on line 31 still says 0005 (cosmetic). |
| `test_new13_archived_catalog_lines…` asserts "re-save 200 + blocker; submit 409" instead of a 422 on re-save | Legitimate remedy choice. The security property (no submission at an archived price) is still asserted, and the exact blocker list on PATCH is also checked. Not weakened. |
| `test_new15…` asserts one tenant-scoped row (`scope=user`, `total=1`) | Legitimate for the chosen remedy. Its `organization_id IS NULL` assertion is vacuous under FORCE RLS, and `n_tup_ins` is a cumulative statistics counter (possible flake). See R3-10. |
| `test_new13_repro_inactive_product…` now always `pytest.skip`s | Acceptable (the skip is explained), but it is dead code; remove it or convert it. |
| `test_review2_fixes.py`: 422 message text | Legitimate (O2-4). |
| `qa/test_qa_reverify_review2.py::_replay_migration_0005`: `downgrade -1` → `downgrade 0005` (commit 4cef4d4) | Legitimate and equivalent at head `0005b`, but **not declared**: the round-5 evidence says the QA file is "unchanged". See R3-4. |

## Findings

| ID | Severity | Title | Code refs | Evidence | Remediation |
|---|---|---|---|---|---|
| R3-1 | **Medium (gate/process)** | Round-5 code has no independent QA or security re-verification | `597f4b0..d0aac67` | QA re-verify 2 and security re-verify 2 both ran on `58976f3`. Fix round 5 changed `quotes.py` (+76/−), `rfqs.py`, migration `0005b`, `revoke_sessions.py`, compose and the web quote page. The only evidence on the new code is `implementer-fixes5` (329 passed + 1 skipped; 26 browser passed + 1 skipped), i.e. the implementer certifying their own fixes with the agents' tests. The newest security scan (`scan-20261101-232659.md`) is for commit `80aaf7f`. | QA: re-run the full API and browser suites on `d0aac67`, reproduce D2-1..D2-5, and confirm that its committed test files match what it delivered. Security: re-verify NEW-13..NEW-17 and I-1 dynamically, re-run the scanners, and render `docker compose config` for the new api service. Record both as evidence. |
| R3-2 | **Medium (gate)** | CI status on the candidate is unverified and unrecorded; I-3 not root-caused | `.github/workflows/ci.yml`, `tests/e2e/playwright.config.ts` | No file in `docs/` names runs 41369753932 or 41369758251 or any CI result for `d0aac67`. Earlier PR-triggered e2e runs failed on 58976f3, 597f4b0 and e090089 (per the brief) without a root cause. Round 5 only added diagnostics (an `expect` on intermediate responses, a logout settle and log piping). | Record both `ci` runs and the `security` run for `d0aac67` in RELEASE_GATES or an evidence file, with job-level results and test counts. Either root-cause I-3 from the newly piped logs or record N consecutive green PR-triggered reruns and document the flake. Do not read CI as green from a single run. |
| R3-3 | Low (likely; needs verification) | The compose `api` allow-list may break the documented quick start | `infra/docker-compose.yml` api `environment:` (`SECRET_KEY: ${SECRET_KEY:?…}`, `APP_ENV: ${APP_ENV:-production}`); README/OPERATIONS_RUNBOOK `docker compose -f infra/docker-compose.yml up` | Before round 5 the api took `SECRET_KEY` and `APP_ENV` from `env_file: ../.env`. Now they come from Compose **interpolation**. By default Compose reads `.env` from the project directory, which is the compose file's directory `infra/`, not the repo root where the runbook puts `.env`. Likely outcomes: (a) `docker compose up` aborts with "set SECRET_KEY in .env"; or (b) with `SECRET_KEY` exported but `APP_ENV` unset, the API runs as `production` and refuses the `.env.example` placeholder secret at start-up (`config.py` weak-secret guard). Both fail closed, not insecurely. `migrate` still reads `../.env`, so the two services can disagree. Also, `FORWARDED_ALLOW_IPS: ""` makes uvicorn trust no proxy at all, which is safe but contradicts the Dockerfile comment ("127.0.0.1 only"). No `docker compose config` run is recorded for round 5, and `test_new16` only checks that `env_file` is absent. | Document `docker compose --env-file .env -f infra/docker-compose.yml up` (or move `.env` handling into `infra/`), or use a dedicated `env_file: ../.env.api` without owner keys. Add `docker compose config` with `.env.example` to CI, asserting the api environment. Align the Dockerfile comment. |
| R3-4 | Low (process/docs) | The round-5 evidence misstates the changes to the QA tests | `implementer-fixes5-stage1-2026-11-02.md` ("unchanged"); commit `4cef4d4` | `_replay_migration_0005` was changed after the evidence was written. Both agents' files were first committed by the implementer in `597f4b0`, so git cannot show what was delivered. | Declare the 4cef4d4 edit in the evidence. QA and security confirm (in their re-run, R3-1) that the committed files are theirs plus the declared edits. |
| R3-5 | Low (documentation gate) | Traceability and gate log are stale | `docs/REQUIREMENTS_TRACEABILITY.md` (last changed in 80aaf7f) | Rows still cite "293/293 on 60adb00", "pending review 2" and "round 4 fixes pending review 3". NFR CI says "not yet observed on GitHub — pending first push". S1-A01..A20 are lumped into one row. S1-F12 does not list `catalog_version_mismatch`. No row references `0005b`. | Update every Stage 1 row from recorded evidence on the gated HEAD, split S1-A01..A20 per ID, and record CI runs. |
| R3-6 | Low (functional/UX) | The `catalog_version_mismatch` blocker cannot be resolved from the web UI | `page.tsx` (no product picker; only "Add custom line"); `compute_blockers` in `approve()` | The blocker tells the user to "re-link the product to the active catalog", but the editor cannot change `product_id`. The only UI route is to remove the line and add a **custom** line with a manual price, which again needs owner/admin confirmation (the routine-confirmation pattern D2-1 removed). It affects quotes from RFQs parsed with no active catalog and ad-hoc quotes (API-only creation). A pending ad-hoc quote also becomes unapprovable after any activation and must be rejected. | Add a "re-link to active catalog" action (server re-prices from the active version) or a product picker in the editor. Document that an activation blocks in-flight ad-hoc approvals. |
| R3-7 | Info | A new product lookup is filtered by RLS only | `quotes.py:129-131` (`select(Product.id, Product.catalog_version_id).where(Product.id.in_(pids))`) | This contradicts the CLAUDE.md rule "organization_id in the service layer AND RLS" (the review-1 L-9 pattern). It is not exploitable, because `pids` come from the tenant's own lines, which were org-checked when linked. | Add `Product.organization_id == org.id`. |
| R3-8 | Info | Robustness of migration `0005b` | `20261101_0005b_withdrawal_data_fix.py` | It is correct: single transaction (`alembic/env.py`), ACCESS EXCLUSIVE locks, idempotent, and keyed on `approval_decisions`, so later approvals with a higher `version_no` are untouched. Minor points: (1) if the UPDATE fails, the `finally` `ALTER … FORCE` raises in the aborted transaction and hides the original error behind a chained one (rollback still restores FORCE); (2) `quotes` is relaxed although the statement does not use it; (3) the data fix leaves no audit trail of which versions it withdrew; (4) the down-migration is a no-op (documented); (5) restoration of FORCE is only checked implicitly, because `security/test_audit_followups` happens to run after the QA replay tests. | Drop the try/finally (rely on transaction rollback) or re-raise explicitly. Log affected ids or write an `ops.data_fix` audit row per org. Add an explicit `relforcerowsecurity` assertion to the replay tests. |
| R3-9 | Info | Edge cases in the kill-switch audit | `scripts/revoke_sessions.py:57-79` | When zero sessions are revoked by `--user-email`/`--all`, no audit row is written for the operator action. `summary.total` puts the platform-wide count into every tenant's row. That count is not exposed by any API today, since audit rows surface only through the quote history by entity id. | Record the platform total on stdout only, or document it. Consider an operator log for no-op revocations. |
| R3-10 | Info (test quality) | Test gaps | e2e `qa-reverify-review2.spec.ts` D2-4; sec `test_new15`, `test_new13_repro_inactive…` | The new `withdrawn-documents` banner has no assertion. `test_new15` relies on `pg_stat_user_tables.n_tup_ins` (stats are flushed asynchronously, so it can flake) and has a vacuous NULL-org check. One test is a permanent skip. No test covers approving a pending ad-hoc quote after a catalog activation (the `approve()` blocker path). | Add these assertions and tests during the QA re-run. |

There are no Critical or High findings, and no known Critical or High vulnerability is open.

## Gate checklist (role file)

| Gate | Status |
|---|---|
| Every critical acceptance requirement has an implementation and runnable test evidence | **INCOMPLETE**: implementation and tests exist, but independent evidence on `d0aac67` is missing for the rows changed in round 5 (R3-1) |
| CI and representative E2E genuinely pass; failures and skips explained | **INCOMPLETE**: reported, not recorded or verified by me; I-3 unexplained (R3-2); the skip (screenshots, NEW-13 repro) is explained |
| No unresolved Critical/High without a human-signed acceptance | PASS: none open; the Lows are not signed (human action) |
| Tenant isolation and backend permissions verified (API, files, AI) | PASS on code (FORCE RLS restored by 0005b; R3-7 Info); AI retrieval not wired |
| High-impact operations have server-side approval gates | PASS: approve/withdraw/override are role-gated on the server; the stale-catalog blocker is enforced in `submit` and `approve` |
| AI output schema-checked, never authoritative | PASS (adapter unwired, disabled by default) |
| No secrets in frontend, logs, telemetry or model calls | PASS on code: the request log carries path, status, id and ms only; the compose api no longer receives owner credentials (static; R3-3) |
| Documentation reflects implemented behaviour | **FAIL (Low)**: R3-5, R3-4; API_CONTRACT and KNOWN_LIMITATIONS were updated correctly |
| Independent reviewer invoked with fresh context | PASS (this report) |

## Human actions required

1. Acknowledge or sign the documented Lows in RELEASE_GATES (L2-7): L-2, L-6, NEW-11/12, SEC-12, SEC-14, SEC-21,
   L2-3, and now O2-1 (catalog pinning) and the R3-6 behaviour. I do not accept risk.
2. Enable and confirm branch protection on `main` (required checks `api`, `web`, `e2e`, `security`, one review).
3. Decide whether executing the Docker Compose topology (X-09, R3-3) is required before Stage 1 is called released.
4. Decide whether a manual screen-reader and responsive audit (X-08) is required for Stage 1.

## Required retest before release review 4

- **QA (independent):**
  - Run the full API suite, migrations `base → head → 0005 → head`, and the full Playwright suite on `d0aac67` (or
    its successor).
  - Reproduce D2-1..D2-5 closed, in the browser too.
  - Add the R3-10 assertions and tests: the `withdrawn-documents` banner, approval blocked for a stale ad-hoc quote,
    and an explicit FORCE-RLS check after the replay.
  - Confirm the provenance of the committed QA file (R3-4).
- **Security (independent):**
  - Re-verify NEW-13..NEW-17 and I-1 dynamically on the new HEAD.
  - Run the scanners and write a scan file naming the commit.
  - Run `docker compose config` with `.env.example` exactly as the README instructs (R3-3).
  - Confirm the provenance of the committed security test file.
- **Implementer:** R3-3 (env-file or documentation fix plus a compose-config CI check), R3-5 (traceability), R3-4
  (evidence correction), and optionally R3-6, R3-7 and R3-8. Each with tests where applicable.
- **Orchestrator:** record both `ci` runs and the `security` run for the gated HEAD in evidence; resolve or document
  I-3 (R3-2); then re-invoke the release reviewer.

## What I did not verify

- **Test runs:** I ran no test, migration, build, server or scanner. Every pass/fail count above comes from documents
  written by other agents.
- **CI:** I did not query GitHub. Every CI statement is "reported, unverified by me".
- **Compose behaviour:** whether Compose reads `.env` from `infra/` or from the working directory for this repository's
  documented command is based on my knowledge of Compose v2 defaults, not on execution (R3-3). No Docker image was
  built or run.
- **Browser behaviour:** I read the web UI changes; no browser evidence exists for the new banner.
- **Pre-0005b databases:** migration behaviour on a database that genuinely predates round 4 is untested anywhere; the
  replay tests simulate legacy shapes.
- **Not reviewed in full:** the unchanged Stage-1 code outside the touched paths was not re-read line by line in
  this review. Its PASS rests on review 2 plus the unchanged git diff since the independent verification on
  `58976f3`.
- **No security certification:** this review is not a penetration test, and it does not certify the system as
  secure.
