# Independent release review 4: Stage 1 at 287d09e

**Release:** Stage 1 (RFQ → verified quotation)
- **Commit:** `287d09e558b4dc4be0288f7fd8df7d3a644eb5b1`. Its parent is `f69ac2599cfe97770a5fb98f04b595ef49c3a485`, which is fix round 6.
- **Branch:** `claude/multi-stage-project-impl-8hz2wj`.
- **Dates:** the review started on 2026-11-10. An API usage limit interrupted it, and it was finished on 2026-11-12. On resuming I re-read `.git/logs/HEAD`: the last entry is still `f69ac25 → 287d09e`, so the branch did not move.

**Reviewer:** an independent agent with a fresh context and the read-only role from `.claude/agents/release-reviewer.md`.
- I used only Read, Glob and Grep.
- I did not implement, test or audit any of this code.
- I did not run a test, a build, a server, a scanner or git.
- I did not query GitHub.
- I edited no file.

## What I read

**Evidence**
- `docs/evidence/release-review-3-stage1-2026-11-02.md`
- `implementer-fixes6-stage1-2026-11-10.md`
- `implementer-fixes5-stage1-2026-11-02.md` (the R3-4 correction)
- `qa-stage1-reverify3-2026-11-10.md`
- `security-stage1-reverify3-2026-11-10.md`
- `scan-20261110-170807.md`

**Design and status documents**
- `docs/RELEASE_GATES.md`
- `docs/REQUIREMENTS_TRACEABILITY.md`
- `docs/KNOWN_LIMITATIONS.md`
- `docs/PRD.md` (X-01..09, S1-F01..F21, S1-A01..A20)
- `docs/API_CONTRACT.md` (catalog routes, by search)
- `docs/openapi.json` (by search)
- `README.md`
- `.env.example`
- `.git/logs/HEAD`

**Round-6 code**
- `apps/api/app/services/quotes.py`, read in full: `stale_catalog_lines`, `compute_blockers`, `update_draft`, `submit`, `approve`, `reject`, `withdraw`, `new_version`, `margin_override`, `_pricing_catalog_version_id`
- `apps/api/app/api/routers/catalogs.py`, read in full
- `apps/api/app/services/catalog_import.py::activate_version`, `active_version_for_org`
- `apps/web/src/app/(app)/quotes/[id]/page.tsx`, read in full
- `scripts/check_compose.py`
- `.github/workflows/ci.yml`
- `infra/docker-compose.yml`
- `tests/e2e/playwright.config.ts`
- Spot checks of unchanged controls: `app/db/session.py`, `services/exports/csv_export.py::neutralize` and `quote_exports.py::_check_exportable`

**Tests**
- `apps/api/tests/qa/test_qa_reverify_review3.py`: 17 tests
- `apps/api/tests/security/test_audit_followups4.py`: 19 tests
- `tests/e2e/specs/qa-reverify-review3.spec.ts`: 4 tests
- `tests/e2e/specs/review3-regressions.spec.ts`: 2 tests
- A search of all e2e specs for `skip`, `only` and `fixme`. The only hit is the self-skipping `screenshots.spec.ts`.

## Executive verdict

**Overall verdict: INCOMPLETE. Decision: BLOCKED as a PASS until the items below are done.** None of them is a code defect in the Stage 1 product.

**Code review result.** I found no Critical, High or Medium defect in the round-6 code, and nothing regressed in the round-5 paths it touches.
- `stale_catalog_lines` now filters by `organization_id` (`quotes.py:132-134`). It fails closed when a product is missing (`versions.get()` returns `None`, so the line counts as stale).
- `GET /catalogs/active-version` is authenticated, tenant-scoped and read-only. It returns only `{version_id}`.
- The editor's "Use active catalog" control sends no price computed in the browser: `price_touched:false` makes the save omit `unit_price` (`page.tsx:88`, `:118`). The server re-prices the line and refuses archived or foreign products (`quotes.py:470-485`).
- Every `catalog_version_mismatch` path I traced fails closed. Nothing prices a quote from an archived catalog, and nothing can turn a manual price into `catalog`.

**Process gates now met.** Independent QA and security both re-verified the round-5 and round-6 code on `f69ac25` (R3-1 closed), and a scan file names that commit.

**Why it is not PASS**
1. **RR4-1 (Medium, gate):** the gated HEAD `287d09e` has no recorded CI run.
   - It adds 36 API tests and 4 browser tests that have never run in CI.
   - No single full-suite run covers both new files.
   - The recorded CI results give conclusions only, with no counts.
   - CI runs Playwright with `retries: 1`, so a flaky test would not show in a "success" conclusion.
   - I-3 is still not root-caused.
2. **RR4-2 (Low):** the documentation gate fails.
   - `REQUIREMENTS_TRACEABILITY.md` still says "independent re-verification of rounds 5–6 pending", and its NFR CI row is stale (R3-5 is not fully closed).
   - `API_CONTRACT.md` does not list the new endpoint.
   - `KNOWN_LIMITATIONS.md` and the human-actions text are inaccurate.
3. **Human decisions are still outstanding.** They cover the documented Lows, branch protection, whether Compose must be executed (X-09), whether a manual assistive-technology audit is required (X-08), and deferring the S1-F21 AI extraction. I do not decide or accept any of these.

No known Critical or High vulnerability is open.

## Status of the review-3 findings

| ID | Status at 287d09e | Basis |
|---|---|---|
| R3-1: no independent re-verification | **CLOSED** | QA re-verify 3 on f69ac25: full suite 330 passed; QA module 17 passed; Playwright 32 passed + 1 skipped; migrations base → head → 0005 → head. Security re-verify 3: NEW-13..17 and I-1 re-tested against a running system; `tests/security` 97 passed + 2 strict xfail; the scan names f69ac25. |
| R3-2: CI not recorded, I-3 | **PARTIALLY CLOSED** | `RELEASE_GATES.md` records `ci` and `security` runs, on both push and pull_request, as success for d0aac67 and f69ac25. The orchestrator recorded these; I did not verify them. I-3 is still not root-caused, although two consecutive commits are now green on both events. Nothing is recorded for 287d09e (RR4-1). |
| R3-3: compose start-up | **CLOSED (rendered and static only)** | The README and runbook use `--env-file .env` (`README.md:36`). `scripts/check_compose.py` runs as a CI step (`ci.yml:48-52`). Security rendered the topology: exit 0 with `--env-file`, exit 1 without it (fails closed), and the api/web environments hold no owner material. Compose has never been executed (human decision). Remaining Info items: R4-2 and R4-3. |
| R3-4: undeclared test edit | **CLOSED** | `implementer-fixes5…md:32-35` was corrected. QA confirmed that the diff of the file is exactly the declared edit. |
| R3-5: stale traceability | **NOT CLOSED** | See RR4-2. |
| R3-6: no UI path for `catalog_version_mismatch` | **CLOSED** | `page.tsx:106-123` and `catalogs.py:33-39`. QA verified it through the API and in the browser, including a refused re-link for a dropped SKU. The remaining limit (a SKU dropped from the catalog can only be removed, or replaced by a custom line that an owner or admin confirms) is documented. Small UI follow-ups are RR4-3 and RR4-4. |
| R3-7: lookup filtered by RLS only | **CLOSED** | `quotes.py:132-134` |
| R3-8: robustness of 0005b | OPEN (Info) | Unchanged, as declared. |
| R3-9: kill-switch audit totals | OPEN (Info) | Same issue as the security finding R4-1, which has a strict xfail. |
| R3-10: test gaps | **CLOSED** | Banner assertions now exist, including an HTML-injection check (`qa-reverify-review3.spec.ts:93-136`, `review3-regressions.spec.ts:13-33`). The stale pending approval is tested through the API and in the browser. A FORCE-RLS check runs after the replay (`test_r3_10_force_rls…`). `test_new15` was rewritten, and the always-skipping test was removed. |

## Traceability (mandatory template)

Legend: **PASS** means the code was read by me, or has not changed since an earlier independent verification, and independent QA or security evidence exists on `f69ac25`. CI for `287d09e` is still pending (RR4-1). I did not diff `f69ac25..287d09e`; that 287d09e changes only evidence and tests comes from the commit message and the orchestrator.

| requirement_id | design_reference | code_reference | test_id | test_result/evidence | security_check | reviewer_verdict | open_issue |
|---|---|---|---|---|---|---|---|
| X-01 tenant isolation | ADR-002, HLD | `db/session.py::set_tenant` (transaction-local); RLS 0001; FORCE restored by 0005b; explicit org filters, including `quotes.py:133` | security/test_tenant_isolation; `followups4::test_round6_quote_line_cannot_link_another_tenants_product`; QA `test_r3_10_force_rls_is_on_for_every_tenant_table…`; `test_r3_6_stale_line_cannot_be_relinked_to_archived_or_foreign…` | QA 330 + 17 passed; security 97 passed + 2 xfail (f69ac25) | 404 across tenants; FORCE on every org table except `sessions` (ENABLE only, documented) | PASS | RR4-1 |
| X-02 RBAC | USER_JOURNEYS | `services/authz.py`, `deps.py`; `approve`, `reject`, `withdraw`, `margin_override` role checks | security/test_rbac_csrf_headers; security live probes (sales withdraw 403, readonly PATCH 403, no CSRF 403) | independent, f69ac25 | server-side only; UI gating is cosmetic | PASS | — |
| X-03 audited high-impact actions | LLD audit | `audit.record` in every quote and catalog mutation; `revoke_sessions.py` writes per-org rows | integration audit tests; `followups4::test_new15_*` | independent | no NULL-org rows | PASS (endpoints); script residual is Info | R3-9 / R4-1 |
| X-04 no paid dependency | ADR-009 | `services/ai/provider.py` (disabled default); compose `AI_PROVIDER:-disabled` | unit AI-disabled test | unchanged | — | PASS | — |
| X-05 synthetic data & consent | PRIVACY_AND_RETENTION | fixtures, `routers/org.py` | test_review1_fixes::test_x05… | unchanged | — | PASS | — |
| X-06 Decimal money | ADR-004 | `pricing.py`, `money.py`; `Decimal(str(...))` in `update_draft` | unit/test_pricing; QA exact totals 31.88 / 19.66 / 412.78 | independent | NaN, Infinity, 1e999 and negative prices give 422 (security) | PASS | — |
| X-07 audit detail | PRD | `audit.py`; `quote.update` line deltas | QA audit tests | unchanged | no PII added | PASS | — |
| X-08 accessible, responsive UI | USER_JOURNEYS | `apps/web/src` | e2e axe scans | no manual assistive-technology or responsive audit | — | INCOMPLETE | human decision |
| X-09 Docker + CI | ADR-014 | `infra/docker-compose.yml`, `ci.yml`, `check_compose.py` | CI compose step; `followups4::test_new16_*` | rendered by security; CI for f69ac25 recorded; Compose never executed; no CI for 287d09e | api/web allow-list verified in the render | INCOMPLETE | RR4-1, human decision, R4-2, R4-3 |
| S1-F01 orgs & roles | DATA_MODEL | `routers/org.py` | qa org tests | unchanged | — | PASS | — |
| S1-F02 auth | ADR-003 | `services/auth.py`; proxy `FORWARDING` | auth integration; `followups4::test_i1_*`; live I-1 probe | independent | only `X-Forwarded-Prefix` passes the proxy, and nothing consumes it | PASS | R4-4 (Info) |
| S1-F03 org settings | PRD | `routers/org.py` | test_org_settings_role_gates | unchanged | — | PASS | — |
| S1-F04 catalog import & activation | LLD | `catalog_import.py` | test_catalog; QA active-version test (an import alone does not move the pointer) | independent | — | PASS | — |
| S1-F05 customers | PRD | `routers/customers.py` | customers CRUD test | unchanged | — | PASS | — |
| S1-F06 RFQ intake | ADR-005 | `services/parsing/*` | test_rfq_flow, security parser tests | unchanged; earlier security tests still pass | subprocess + limits | PASS | L-6 (documented) |
| S1-F07 items & confidence | LLD | `routers/rfqs.py` | test_rfq_flow | unchanged | — | PASS | — |
| S1-F08 unit/SKU normalisation | LLD | `services/units.py` | unit tests | unchanged | — | PASS | — |
| S1-F09 matching | ADR-006 | `services/matching.py` | unit + integration | unchanged | — | PASS | — |
| S1-F10 human verification | PRD | `rfqs.py::update_item` (not re-read this round; unchanged since review 3) | `followups3::test_new13_rfq_without…`; `followups4::test_new13_rfq_item_selected_before_activation…` | independent | inactive or archived product gives 422 | PASS | — |
| S1-F11 deterministic pricing | ADR-004 | `quotes.py:459-534` | QA `test_d2_1_*` (×7), `test_r3_6_relink…reprices_server_side`; security `test_new17_*` | independent, API + browser | no laundering into `catalog`; client `price_source` ignored | PASS | — |
| S1-F12 blockers & override | LLD | `compute_blockers`, `stale_catalog_lines`, `margin_override` | security `test_new13_stale_blocker_survives…`; QA D2-2, R3-10 | independent | blocker enforced in `submit` and `approve`; an override does not clear the mismatch | PASS | — |
| S1-F13 versions & concurrency | ADR-010 | `_check_lock`, `new_version` (copies the catalog pin) | test_quotes conflict tests; QA R3-6 atomicity (lock not advanced on 422) | independent | — | PASS | — |
| S1-F14 approval workflow | HLD | `approve()` (`quotes.py:653-713`) | QA `test_r3_10_pending_stale_quote_cannot_be_approved_by_any_approver_role…`; security R3-10 probe | independent, API + browser | separation of duties unchanged; a stale pending quote gets 409 for approver, owner and admin | PASS | wording in RR4-2 |
| S1-F15 withdrawn / expired | HLD | `withdraw()`, 0005b, `_check_exportable`, web banner | QA `test_d2_5_*` ×2, `test_d2_3_replay…`; security `test_new14_*`; e2e banner | independent, API + browser | withdrawn versions export 409; reason rendered as text | PASS | R3-8 (Info) |
| S1-F16 PDF/CSV export | ADR-007 | `exports/*`; `neutralize` (`= + - @ \t \r`) | export tests; e2e identical bytes | unchanged | formula neutralisation spot-checked | PASS | — |
| S1-F17 history & search | PRD | `routers/quotes.py` | test_quotes search/list | unchanged | — | PASS | — |
| S1-F18 audit in transaction | LLD | `audit.record` inside each service mutation | as X-03 | unchanged | — | PASS | — |
| S1-F19 fixtures | PRD | `fixtures/seed.py` | e2e global setup | unchanged | synthetic | PASS | — |
| S1-F20 UI & states | USER_JOURNEYS | `quotes/[id]/page.tsx` | QA e2e (4 tests), review3-regressions (2) | independent browser run, Chromium only | React escapes the reason; no cost column for sales | PASS | RR4-3 (Low), RR4-4 (Info) |
| S1-F21 AI adapter (M) | ADR-009 | `services/ai/*`, not wired to routes | unit | no live provider by design | disabled default + consent | INCOMPLETE (by design) | human decision on deferral |
| S1-A01, A02, A04–A10, A12, A13, A16, A17, A19 | PRD | as F rows above | test_rfq_flow, test_quotes, test_catalog, security/*, qa/* | code unchanged since earlier independent verification; suite green on f69ac25 (QA) | — | PASS | per-ID split missing (RR4-2) |
| S1-A03 custom line, privileged price | PRD | `update_draft` manual rules; `margin_override` | QA `test_d2_1_custom_line…`; security `test_new17_sending_the_list_price…` | independent | owner or admin confirmation | PASS | — |
| S1-A11 currency mismatch | PRD | `quotes.py:492-506` | QA review-2 `test_n2_1a_*` (still green) | independent | — | PASS | — |
| S1-A14 cross-tenant | PRD | org filters + FORCE RLS | security tenant matrix; round-6 cross-tenant link 404 | independent | 404, not 403 | PASS | — |
| S1-A15 expired / withdrawn | PRD | `_check_exportable`, `withdraw`, 0005b | QA D2-5, D2-3; security NEW-14 | independent | — | PASS | — |
| S1-A18 deterministic PDF | PRD | `store_approved_pdf` | unit + e2e identical bytes | unchanged | — | PASS | — |
| S1-A20 browser E2E | PRD | `tests/e2e/specs/*` | full Playwright suite | QA local 32 passed + 1 skipped (f69ac25); CI recorded as success for f69ac25 (orchestrator-recorded); nothing for 287d09e | — | PASS on f69ac25; must be re-confirmed on 287d09e | RR4-1, I-3 |

## Findings

All severities below are for release review 4. I use the prefix "RR4" because the security agent already used R4-1..R4-5.

### RR4-1: no CI evidence on the gated HEAD; counts and flaky retries not recorded; I-3 open

- **Severity:** Medium (gate).
- **Code refs:** `.github/workflows/ci.yml:46-52`, `tests/e2e/playwright.config.ts:32` (`retries: process.env.CI ? 1 : 0`).
- **Evidence:**
  - `RELEASE_GATES.md:99-105` records runs for `f69ac25` only.
  - `287d09e` adds `test_qa_reverify_review3.py` (17 tests), `test_audit_followups4.py` (19 tests, 2 of them strict xfail), and `qa-reverify-review3.spec.ts` (4 browser tests). None has run in CI.
  - The new files have never run together in one pytest invocation. QA ran 330 and 17 separately, and security ran only `tests/security`. QA observed a possible fixture flake (O3-4, `users_email_key`).
  - The recorded CI entries give conclusions only: no pytest totals and no Playwright passed/flaky counts. With CI retries, "success" can hide a flaky test.
- **Remediation:**
  - Record the `ci` and `security` runs for 287d09e (or its successor) on both push and pull_request, with job-level results.
  - Record the pytest summary line. My expected figure is 363 passed, 1 skipped, 2 xfailed; this is derived, not observed, and any difference must be explained.
  - Record the Playwright summary. Expected: 32 passed, 1 skipped, **0 flaky**. Any flaky test must be explained.
  - Keep I-3 open. One lead to check: pull_request runs test `refs/pull/N/merge`, not the branch commit.

### RR4-2: documentation does not match the recorded evidence or the implementation

- **Severity:** Low (documentation gate).
- **Code refs:** `docs/REQUIREMENTS_TRACEABILITY.md:10-46`; `docs/API_CONTRACT.md:23-26`; `docs/KNOWN_LIMITATIONS.md:61-65`; `docs/RELEASE_GATES.md:113-114`.
- **Evidence:**
  1. Traceability rows S1-F10..F15, F20, A03/A11, A15 and A18 still say "independent re-verification of rounds 5–6 pending", but that re-verification is now recorded.
  2. X-01, S1-F01 and the S1-A rows cite "implementer 329 API + 26 browser on d0aac67".
  3. The NFR CI row lists only the failures on 58976f3, 597f4b0 and e090089 and says "review 3 pending". This contradicts RELEASE_GATES, which records green runs on d0aac67 and f69ac25.
  4. S1-A01..A20 are still not split per ID.
  5. The X-08 row says "26 e2e".
  6. `API_CONTRACT.md` does not list `GET /catalogs/active-version`. `openapi.json` does list it.
  7. KNOWN_LIMITATIONS says I-3 was "one PR-triggered run on 58976f3"; RELEASE_GATES records three.
  8. The human-action text says a stale pending quote "must be rejected and re-versioned". In fact `reject()` returns the same version to draft (`quotes.py:738`), as the QA test shows. That QA test's name also says `…reject_new_version_relink`, but no new version is created.
- **Remediation:** update every Stage 1 row from the re-verify-3 evidence and the 287d09e CI run, and split S1-A01..A20 per ID. Add the endpoint to `API_CONTRACT.md`. Correct the I-3 and R3-6 wording.

### RR4-3: editor pairs unsaved rows with saved lines by array index

- **Severity:** Low (functional/UX; fails closed on the server).
- **Code refs:** `page.tsx:167-168` (`const saved = version.lines[i]`); `page.tsx:118` (`relinkToActiveCatalog` does not reset `is_custom`).
- **Evidence (from reading; not reproduced in a browser):**
  - If a user removes a row and does not save, later rows are paired with the wrong saved line. The "Use active catalog" button (new in round 6) and the "manual price" badge (existing) can then appear on the wrong row.
  - The server prevents any harm. Re-linking a current catalog line to its own product changes nothing. Re-linking a custom row sends `is_custom: true`, which the server turns into an unconfirmed manual line (`quotes.py:526-533`) that needs owner or admin confirmation.
  - There is no pricing bypass.
- **Remediation:** match rows by id (`version.lines.find(s => s.id === l.id)`), and set `is_custom: false` on re-link. QA should add a browser test that removes a row and then uses the re-link.

### RR4-4: re-link product lookup is a substring search capped at 50 results

- **Severity:** Info.
- **Code refs:** `page.tsx:115-116`; `catalogs.py:101,113-116`.
- **Evidence:** `?q=<sku>` matches by substring on SKU or description, uses the default `page_size=50`, and then looks for an exact SKU on the client. In a large catalog the exact SKU can fall beyond the first page. The user then sees a false "not in the active catalog" message, and nothing changes (fails closed).
- **Remediation:** add an exact-SKU filter or a server-side re-link action.

### RR4-5: one compose test always skips in CI

- **Severity:** Info (test quality).
- **Code refs:** `ci.yml:47` vs `:50`; `test_audit_followups4.py:399-403`.
- **Evidence:** CI runs pytest before `cp .env.example .env`, so `test_new16_rendered_compose_as_documented…` always skips there. The CI compose step covers the same property.
- **Remediation:** let the test create a temporary env file, or record the skip as explained.

### RR4-6: carried-over Info items (all documented)

- **Severity:** Info.
- **Items:**
  - R3-8: 0005b's try/finally can mask the original error, and the data fix leaves no audit trail.
  - R3-9 / R4-1: the platform-wide `total` is written into each tenant's audit row.
  - R4-2: `check_compose.py` passes vacuously if a service is renamed. It is a CI guard, so I recommend fixing it before Stage 2.
  - R4-3: inline comments in `.env.example` become values for `migrate`.
  - R4-4: the forwarding-header filter is a blocklist.
  - R4-5 / provenance: from now on, commit agents' test files before the implementer edits them.
- **Remediation:** as given in the security evidence. These items block nothing.

There are no Critical or High findings.

## Gate checklist (role file)

| Gate | Status |
|---|---|
| Every critical acceptance requirement has an implementation and runnable test evidence | **PASS**: independent QA and security evidence on f69ac25 for every C requirement |
| CI and representative E2E genuinely pass; failures and skips explained | **INCOMPLETE**: f69ac25 is recorded green (orchestrator-recorded, not verified by me); 287d09e has no run; no counts or flaky data; I-3 not root-caused (RR4-1) |
| No unresolved Critical/High without a human-signed acceptance | **PASS**: none open; the Lows are unsigned (human action) |
| Tenant isolation and backend permissions verified across API, files and AI retrieval | **PASS** for API and files; AI retrieval is not wired (N/A) |
| High-impact operations have genuine server-side approval gates | **PASS**: approve, reject, withdraw and override are role-checked on the server; the stale-catalog blocker is enforced in `submit` and `approve` |
| AI output schema-checked and never authoritative | **PASS**: the adapter is unwired and disabled by default |
| No secrets in frontend bundles, logs, telemetry or model calls | **PASS** on recorded evidence: bundle grep is clean and the request log carries no body. gitleaks and semgrep ran only in CI's `security` workflow (recorded as success; I did not inspect the output). |
| Documentation reflects implemented behaviour | **FAIL (Low)**: RR4-2 |
| Independent reviewer invoked with a fresh context | **PASS**: this report (interrupted once, resumed, HEAD re-confirmed) |

## Human actions required (I do not decide or accept any of these)

1. Sign or acknowledge the documented Lows in `RELEASE_GATES.md`: L-2, L-6, NEW-11/12, SEC-12, SEC-14, SEC-21 and L2-3, plus O2-1 (catalog pinning) and the R3-6 behaviour. That behaviour: a pending ad-hoc quote whose catalog moved on must be rejected, then re-linked; a SKU dropped from the catalog can only be removed or replaced by a confirmed custom line.
2. Enable branch protection on `main`, with required checks `api`, `web`, `e2e` and `security` and one review, and confirm it.
3. Decide whether the Docker Compose topology must actually be executed before Stage 1 is called released (X-09).
4. Decide whether a manual screen-reader and responsive audit is required for Stage 1 (X-08).
5. Decide whether Stage 1 may ship with S1-F21 (M priority) partial: the adapter exists, is disabled and is not wired to LLM line extraction.

## Required retest

- **Orchestrator / CI:** do RR4-1. Push the gated HEAD, record both `ci` events and both `security` events with pytest and Playwright summaries (including flaky counts), and update the I-3 status.
- **Implementer:** RR4-2 (traceability, API_CONTRACT, KNOWN_LIMITATIONS, gate wording). Optionally RR4-3 (pair rows by id, reset `is_custom`), R4-2 (fail when a service is missing) and RR4-5. If product code changes, list the changed files.
- **QA:** one full `pytest` invocation on the final HEAD that includes both new modules. If RR4-3 is fixed, a browser test that removes a row and then re-links.
- **Security:** a rerun is needed only if product code or the CI guard changes (RR4-3, R4-2). In that case, run a scan that names the new commit.
- **Reviewer:** re-invoke for a review 5 scoped to the diff, with the CI evidence. If only documentation and CI evidence change, that review can be short.

## What I did not verify

- **Test runs:** I ran no test, migration, build, server, scanner or browser. Every count above comes from documents written by other agents.
- **CI:** I did not query GitHub. Every CI statement is the orchestrator's record, not verified by me.
- **Commit contents:** I could not run `git diff f69ac25..287d09e`. That 287d09e adds only evidence and tests comes from its commit message and the orchestrator. The test and spec files I read are consistent with that.
- **Compose:** not executed, and no image was built. The render results come from the security agent.
- **Browser:** RR4-3 and RR4-4 come from reading the code, not from reproduction. Only Chromium was used by QA.
- **Documents not re-read in full this round:** HLD, LLD, THREAT_MODEL, SECURITY_POLICY and TEST_STRATEGY; `rfqs.py::update_item` and the parsers. The PASS for unchanged code rests on reviews 2 and 3, the independent re-runs on f69ac25, and my spot checks.
- **Untested elsewhere too:** migration 0005b on a database that genuinely predates round 4; concurrent editor saves racing a catalog activation.
- **No security certification:** this is not a penetration test or a security certification, and I make no claim that the system is secure.
