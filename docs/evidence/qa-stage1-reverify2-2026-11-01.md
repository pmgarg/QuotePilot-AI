# QA re-verification evidence 2 — Stage 1 after release review 2 (2026-11-01)

Independent QA/Test Engineer re-verification of branch `claude/multi-stage-project-impl-8hz2wj` at HEAD
`58976f3` ("evidence: security scan on round-4 commit"; product code = round 4, `80aaf7f`). Inputs:
`docs/evidence/release-review-2-stage1-2026-11-01.md` (N2-1 A/B, N2-2, L2-1..L2-7, "Required retest"),
`docs/evidence/implementer-fixes4-stage1-2026-11-01.md` (claimed fixes and tests),
`docs/evidence/qa-stage1-reverify-2026-11-01.md` (predecessor QA: R-1..R-5), `docs/PRD.md` Stage 1 (S1-F11, S1-F12,
S1-F15, S1-F16, S1-A11, S1-A15, S1-A18, S1-A20), `docs/API_CONTRACT.md` (`POST /quotes/{id}/withdraw`),
`docs/USER_JOURNEYS.md` ("Withdraw approved quote": owner/admin/approver). No product code was modified; no existing
test was weakened, skipped or deleted. Only new files were added (listed under "Tests added").

**Provenance note.** R1–R4b ran against a working tree whose only additions were this round's QA files. At 23:46:43 UTC
(during R4b) the security auditor added `apps/api/tests/security/test_audit_followups3.py`; it is included in R5 and
accounted for there. Product code was unchanged throughout the runs: R5 printed its summary at 23:54:15 UTC; at
23:54:17 UTC another agent began modifying `apps/api/app/services/quotes.py` and `apps/api/app/api/routers/quotes.py`
in the shared working tree (uncommitted; a `stale_catalog_lines` / `catalog_version_mismatch` blocker, apparently for
the security auditor's NEW-13). Every figure in this report is therefore against the committed code of `58976f3`;
the defects below must be re-checked against whatever is committed next — the failing tests are in place for that.

**Headline.** N2-1 A, N2-1 B (as specified), N2-2 (both paths, API) and R-1..R-5 are **fixed** on HEAD. Probing
around them surfaced **two defects in the quote editor's save path** (one High: every "Save draft" turns catalog
prices into unconfirmed manual prices, which also neutralises the N2-1 A distinction from the browser), **one UI gap
for N2-2** (withdraw-while-draft is unreachable from the quote screen) and **one ineffective migration** (the L2-5
data fix updates zero rows because the owner role is subject to `FORCE ROW LEVEL SECURITY`). Details in "Defects".

## Environment
| Item | Value |
|---|---|
| Container | Claude Code cloud container, Ubuntu 24.04, no Docker daemon, no external network, no installs performed |
| Python / API venv | `apps/api/.venv` (pytest 8, FastAPI TestClient, pdfplumber, reportlab, openpyxl present) |
| PostgreSQL | 16 local service; dedicated QA database `quotepilot_qa` (runtime role `quotepilot_app` under RLS, `quotepilot_owner` for migrations; neither is superuser/BYPASSRLS). Reset at the start: `.venv/bin/alembic downgrade base` (0003→0002→0001→∅, exit 0) then `.venv/bin/alembic upgrade head` (0001→0002→0003→0004→0005, exit 0; `alembic current` = `0005 (head)`). |
| Env | `APP_ENV=test`, `DATABASE_URL`/`DATABASE_ADMIN_URL` → `…@localhost:5432/quotepilot_qa` (credentials redacted) |
| Node / Playwright | Node 22, `@playwright/test` 1.63.0, `@axe-core/playwright` 4.14, bundled Chromium pre-installed (`playwright install` not run) |
| E2E | `E2E_SKIP_WEB_BUILD=1 E2E_API_PORT=8031 E2E_WEB_PORT=3031`; API `127.0.0.1:8031` (uvicorn via `uv run`), web `localhost:3031` (`next start` on the prebuilt `apps/web/.next`); global setup migrates + seeds `quotepilot_qa` |
| AI provider | `disabled` |
| Evidence layers | **integration** = API + PostgreSQL + RLS through the ASGI TestClient (`apps/api/tests/qa`); **browser** = Playwright/Chromium against real uvicorn + Next.js (`tests/e2e`); **migration** = alembic replay inside a test |

## Runs (exact commands, cwd, exit codes, counts)
All pytest runs: `export APP_ENV=test DATABASE_URL=postgresql+psycopg://quotepilot_app:***@localhost:5432/quotepilot_qa DATABASE_ADMIN_URL=postgresql+psycopg://quotepilot_owner:***@localhost:5432/quotepilot_qa`.
All Playwright runs: `E2E_SKIP_WEB_BUILD=1 E2E_API_PORT=8031 E2E_WEB_PORT=3031` plus the same two database URLs. Runs were strictly sequential (one database).

| # | When | Command | cwd | Exit | Result |
|---|---|---|---|---|---|
| R1 | before additions | `.venv/bin/pytest -q -p no:cacheprovider` | `apps/api` | 0 | **298 passed**, 0 failed, 0 skipped (364.3 s; 23:29:17–23:35:25 UTC). Matches the implementer's count. Includes the predecessor's 28 `test_qa_reverify_review1.py` tests — the four that failed on `ff99762` (R-1..R-4) now pass. |
| R2 | before additions | `npx playwright test --reporter=list` | `tests/e2e` | 0 | **21 passed**, 0 failed, 1 skipped (`screenshots.spec.ts`, self-skipped) (1.4 min; 23:40:28–23:41:51 UTC). Includes the predecessor's two formerly red browser tests (R-3 "removing a line…", R-5 axe) now green, and the implementer's extended SEC-09 redirect test (8 variants). |
| R3 | new QA module only | `.venv/bin/pytest -q -p no:cacheprovider tests/qa/test_qa_reverify_review2.py` | `apps/api` | 1 | **11 passed, 5 failed** of 16 (43.4 s). The 5 failures are the product defects D2-1, D2-2, D2-3 (×2 tests) and D2-5 below; left in place as the regression gate. (A first iteration had 2 test-side issues — Decimal formatting `0.9`/`37.50` — fixed before this run; the withdraw-while-draft probe was also decoupled from D2-1 by using a minimal line patch.) |
| R4 | after additions (first full run) | `npx playwright test --reporter=list` | `tests/e2e` | 1 | **23 passed, 3 failed, 1 skipped** (2.2 min; 23:42:50–23:45:10 UTC). All 21 pre-existing tests green. Failures, all in the new `qa-reverify-review2.spec.ts`: "S1-F11/N2-1: saving a quantity edit…" (**D2-1**: line 1 reads `1SKU-1001manual priceEA535.50Remove` after the save), "N2-2: an approver can withdraw the approved v1 … while a newer draft is open" (**D2-4**: no Withdraw button), and "N2-2: withdrawing a quote with two approved versions in the UI…" — a **test bug**: the Withdrawn banner is a non-error `Alert`, which renders `role="status"`, not `role="alert"`; the captured page snapshot already showed the banner with the reason, both versions `withdrawn`, the per-version decisions and the `quote.withdraw` audit row. Locator corrected and the spec re-run as R4b. |
| R4b | after additions (QA spec only, after the locator fix) | `npx playwright test specs/qa-reverify-review2.spec.ts --reporter=list` | `tests/e2e` | 1 | **3 passed, 2 failed** of 5 (45.7 s; 23:46:57–23:47:44 UTC): N2-2 two-version withdrawal via the UI, L2-1 redirect variants and N2-1 B pinned-draft save pass; "S1-F11/N2-1: saving a quantity edit…" (**D2-1**) and "N2-2: an approver can withdraw … while a newer draft is open" (**D2-4**) remain red as the regression gate. Final browser state on HEAD: 21 pre-existing + 3 QA green, 2 QA red (both product defects), 1 self-skipped. |
| R5 | after additions | `.venv/bin/pytest -q -p no:cacheprovider` | `apps/api` | 1 | **317 passed, 5 failed, 8 xfailed**, 0 skipped (364.5 s; 23:48:07–23:54:15 UTC). 330 collected = 298 pre-existing + 16 QA (this report) + 16 in `tests/security/test_audit_followups3.py`, which the security auditor added to the shared working tree at 23:46:43 UTC (its 8 strict `xfail`s are its own expected failures for its open findings, not QA results; it ran green otherwise). **All 298 pre-existing tests still pass**; the 5 failures are exactly the QA defect gates D2-1, D2-2, D2-3 (×2) and D2-5 (same per-test outcome as R3). |

Static checks on the added files: `.venv/bin/ruff check` and `ruff format --check` on
`tests/qa/test_qa_reverify_review2.py` → clean; `npx tsc --noEmit -p tsconfig.json` in `tests/e2e` → exit 0.

## Review of the implementer's round-4 test changes (`git show 80aaf7f -- apps/api/tests tests/e2e`)
| File | Nature | Legitimate? |
|---|---|---|
| `apps/api/tests/integration/test_review2_fixes.py` (new, 5 tests) | N2-1 A (USD product on an AED quote), N2-1 B (inactive + archived version), N2-2 (two approved versions; withdraw while draft), L2-4 (trigger) | yes. Gaps closed by QA below: no USD-quote/AED-product mirror, no editor-shaped payload, no RFQ-review path, no multi-line atomicity, no four-role matrix, no later-approved-version path, no pending-while-withdrawn, no role/lock/reason matrix, no migration data-fix replay |
| `apps/api/tests/security/test_audit_followups2.py` | docstring wording only | yes |
| `tests/e2e/specs/review1-regressions.spec.ts` | three more hostile `next` values added to SEC-09 (`/..//evil.com`, `/%2e%2e/%2e%2e//evil.com`, `/quotes/..//evil.com/x`) | yes (strictly stronger) |

Nothing was skipped, deleted or loosened.

## Per-item verification
Legend: **impl** = implementer's test in `tests/integration/test_review2_fixes.py`; **QA** = added in this round
(`apps/api/tests/qa/test_qa_reverify_review2.py` = integration; `tests/e2e/specs/qa-reverify-review2.spec.ts` = browser).

| Item | Evidence | Verdict |
|---|---|---|
| **N2-1 A** other-currency re-link keeps catalog price | impl `test_n2_1a_…` (USD product, AED quote, minimal payload). QA `test_n2_1a_usd_quote_linking_an_active_aed_product_carries_no_price_or_cost_and_needs_confirmation` (USD quote ← AED active product: `unit_price=None`, `price_source="none"`, `missing_price`, no `cost_price`/`margin_pct` for the owner, submit 409; typed price → `manual_price_unconfirmed`; approver 403; owner confirms; submit 200) **passes**; `test_n2_1a_editor_style_full_resend_relink_to_other_currency_product_never_yields_a_catalog_price` (editor payload with the stale AED price → `manual` + blocker, no cost; re-link back with no price → `catalog` 0.85, no blockers) **passes** | **FIXED at the API**. Caveat **D2-1**: from the browser every saved line is already `manual`, so the distinction is moot there |
| **N2-1 B** inactive/archived versions linkable | impl `test_n2_1b_…`. QA `test_n2_1b_rfq_review_cannot_select_a_product_from_an_imported_but_inactive_version` (RFQ review path: 422, selection unchanged, generated price 0.85) **passes**; `test_n2_1b_archived_version_product_is_refused_atomically_and_a_foreign_product_is_404` (three-line save with one archived product → 422, quantity change dropped, lock unchanged; foreign tenant product → 404; new active product → 0.90 `catalog`) **passes**; `test_n2_1b_rfq_pinned_quote_prices_only_from_its_own_version_and_new_versions_keep_the_pin` **passes** (see O2-1); `test_n2_1b_unpinned_draft_still_saves_after_a_catalog_activation_when_lines_are_resent_by_the_editor` **FAILS → D2-2** | **FIXED as specified**; regression **D2-2** introduced by the check |
| **N2-2** withdrawal covers every approved version; withdraw while draft | impl two tests. QA `test_n2_2_two_approved_versions_stay_withdrawn_for_every_role_through_v3_draft_and_v3_approval` (v1, v2 approved → withdraw: 409 for PDF GET/POST + CSV × 4 roles × 2 versions, decision trail per version, one audit row `withdrawn_versions=[1,2]`; v3 draft → still 409; v3 preview "NOT APPROVED"; v3 approved → 200 for all roles, v1/v2 still 409, stored hashes unchanged; second withdrawal adds exactly one decision to v3) **passes**; `test_n2_2_approver_withdraws_v1_while_v2_draft_is_open_then_approved_v2_exports_while_v1_stays_blocked` — every N2-2 assertion passes (v1 409 × 4 roles, draft editable and previewable, nothing left to withdraw, **approved v2 exports for all roles while v1 stays blocked**, later v3 keeps v2 exportable); the test fails only on its last assertion → **D2-5** (stale `withdrawn_reason`); `test_n2_2_withdraw_while_v2_is_pending_supersedes_v2_withdraws_v1_and_v3_can_still_be_approved` **passes**; `test_n2_2_withdraw_while_draft_role_lock_and_reason_matrix_leaves_v1_exportable_and_the_draft_untouched` (sales/readonly 403, stale lock 409, blank/missing reason 422, each leaving v1 exportable and the draft untouched; admin allowed; rejected-only quote 409) **passes**; `test_withdraw_cannot_be_used_to_leak_or_alter_lines_of_another_tenant` **passes**. Browser QA "N2-2: withdrawing a quote with two approved versions in the UI…" (owner withdraws from the quote screen: status badge, Withdrawn banner with the reason, both version badges `withdrawn`, decision trail, no PDF links; v1/v2 PDF+CSV `409` for sales/owner/read-only; "Create new version" → v3 draft, still `409`, header offers only the draft preview) **passes** (R4b); "N2-2: an approver can withdraw the approved v1 from the quote screen while a newer draft is open" **FAILS → D2-4** | **FIXED at the API (both paths)**; **D2-4**: the withdraw-while-draft path has no control in the UI; **D2-5** Low |
| **R-1** withdrawn version revived by a new version | predecessor `test_withdrawn_approved_version_blocks_every_export_and_revives_only_via_new_version` passes (R1); QA N2-2 tests above | **FIXED** |
| **R-2** floor in `pricing_snapshot` | predecessor `test_m3_confidential_floor_is_not_in_the_quote_payload_for_sales_and_readonly` passes (R1); QA `test_r2_margin_floor_is_absent_from_every_quote_payload_for_sales_and_readonly_but_visible_to_privileged` (floor 37.5: GET quote, history, list, `/org`, PATCH-with-blocker and submit-409 payloads deep-walked for `min_margin_pct`/`cost_price`/`margin_pct` and the literal value; owner/admin/approver still see it) **passes** | **FIXED** (`routers/quotes.py:50` filters the key for non-privileged roles) |
| **R-3 / R-4** 500 on line removal / replacement | predecessor API tests pass (R1); predecessor browser "S1-F11: removing a line…" passes (R2/R4); QA `test_r3_r4_editor_style_remove_first_reorder_and_replace_middle_line_in_single_saves` (reverse order, remove-first + replace in one save, round-trip) **passes** | **FIXED** (`quotes.py:533-540` parks line numbers on negatives before renumbering) |
| **R-5** `link-in-text-block` on the RFQ review screen | `rfqs/[id]/page.tsx:190` now `className="text-brand underline"`; predecessor browser "X-08: login, review, editor…" passes (R2/R4); implementer X-08 passes | **FIXED** |
| **L2-1** NEW-7 redirect variants | impl SEC-09 now 8 variants (R2/R4 pass). Browser QA "L2-1: further post-login redirect variants…" (16 more hostile values incl. double-encoded, scheme-without-slashes, `%5C%5C`, NUL, tab, U+2215, `//localhost@evil.com`, leading space; raw `?next=//evil.com`; **positive controls** `/quotes` and `/quotes?status=approved` honoured so the sanitiser is proven to do more than always return `/dashboard`) **passes** (R4/R4b) | **VERIFIED** |
| **L2-4** identity triggers | impl test passes; `alembic downgrade base`/`upgrade head` clean; QA migration replay re-creates both triggers (asserted in `test_l2_5_…_left_under_a_withdrawn_quote` before its data-fix assertion fails) | VERIFIED (security scope otherwise) |
| **L2-5** data fix for legacy withdrawn approvals | QA `test_l2_5_migration_0005_data_fix_withdraws_an_approved_version_left_under_a_withdrawn_quote` and `…_also_covers_a_withdrawn_document_whose_quote_already_has_a_new_version` both **FAIL → D2-3** | **NOT FIXED** (migration is a no-op) |

## Tests added (new files only; nothing existing was changed)
| Path | Layer | Count | What it verifies |
|---|---|---|---|
| `apps/api/tests/qa/test_qa_reverify_review2.py` | integration (+ migration replay) | 16 | N2-1 A ×2, editor re-save ×1, N2-1 B ×4, N2-2 ×4, cross-tenant withdraw ×1, L2-5 ×2, R-2 ×1, R-3/R-4 ×1 |
| `tests/e2e/specs/qa-reverify-review2.spec.ts` | browser (Chromium) | 5 | editor save keeps catalog prices (S1-F11/N2-1); N2-2 two-version withdrawal via the UI + v3; N2-2 withdraw-while-draft control; L2-1 redirect variants + positive controls; N2-1 B pinned draft still saves after a UI catalog activation |
| `docs/evidence/qa-stage1-reverify2-2026-11-01.md` | — | — | this report |

## Defects
Severity: Critical / High / Medium / Low. Each has a failing test left in place (file:test named).

### D2-1 — Every "Save draft" in the quote editor turns catalog prices into unconfirmed manual prices — **High** (S1-F11 deterministic catalog pricing, S1-F12 blockers, S1-A20 Sales journey; undermines N2-1 A from the browser)
- Where: `apps/api/app/services/quotes.py:467-469` (`if "unit_price" in payload and payload["unit_price"] is not None: … line.price_source = "manual"` — unconditional, no comparison with the current catalog price or with `before_price`) together with `apps/web/src/app/(app)/quotes/[id]/page.tsx:88` (the editor re-sends `unit_price: l.unit_price` for every line, including untouched catalog lines; `toDraft` pre-fills it with the catalog price at line 14). The API rule is unchanged since the first commit; it became user-visible when round 2 (M-4) made every manual price a `manual_price_unconfirmed` blocker, and no browser test since then has saved a catalog-priced quote and then submitted it.
- Request (API shape = the editor's): 3-line catalog quote, `PATCH /quotes/{id}` with `lines: [{id, product_id, sku, description, quantity: "11", unit, unit_price: "0.85", discount_pct, tax_rate_pct, is_custom: false}, …]` (only the quantity of line 1 changed).
- Expected: `200`, all lines `price_source == "catalog"`, `blockers == []`, submit `200`.
- Actual: `200`, **all three lines** `price_source == "manual"`, three `manual_price_unconfirmed` blockers ("Line n has a manually entered price; an owner/admin must confirm it with a reason"), submit `409 blocked`. In the browser (R4 "S1-F11/N2-1: saving a quantity edit…"): after Quantity → 600 and "Save draft", every line shows the "manual price" badge, the blocker panel appears and "Submit for approval" is disabled — a Sales user cannot submit any quote they edited (quantity, discount, notes, validity) without an owner/admin confirming *every* line; the `catalog` provenance is lost and the line is then also exempt from the "other currency" handling of N2-1 A.
- Tests: `tests/qa/test_qa_reverify_review2.py::test_editor_style_resave_of_an_untouched_catalog_line_keeps_the_catalog_price_and_raises_no_blocker`; browser `qa-reverify-review2.spec.ts` "S1-F11/N2-1: saving a quantity edit in the editor keeps catalog prices and leaves the quote submittable by Sales".
- Note for the fix: treat a re-sent `unit_price` equal to the line's current price as "unchanged" (or have the editor send `unit_price` only when the user edited it); the predecessor's M-4 "re-send keeps the confirmation" semantics (`quotes.py:497-503`) already follow the compare-with-before pattern.

### D2-2 — An unpinned draft can no longer be saved after a new catalog version is activated — **Medium** (regression of the N2-1 B check; S1-F13 editing)
- Where: `apps/api/app/services/quotes.py:437-441` — the new check `product.catalog_version_id != allowed_version → 422` runs for every line whose payload carries `product_id`, i.e. for every untouched line on every editor save (`page.tsx:88`), not only when the link changes. For quotes without an RFQ pin (`pricing_snapshot.catalog_version_id` is null: quotes created via `POST /quotes`, or from an RFQ parsed before any catalog was active) `_pricing_catalog_version_id` resolves to the *current* active version, so lines linked while the previous version was active fail the check after an activation.
- Request: `POST /quotes` → link SKU-1001 from active v1 (200, 0.85) → admin imports + activates v2 → `PATCH /quotes/{id}` with the editor payload (`product_id` = the v1 product, `quantity: "12"`, `notes`).
- Expected: `200`; the untouched line keeps its product and price (or, if the product should be re-resolved, a defined re-pricing) — a draft must not become read-only because the catalog moved on.
- Actual: `422 validation_error "Product is not in the active catalog version used for this quote" line_no 1`; nothing on the draft (quantity, notes) can be saved any more from the editor. RFQ-pinned quotes are unaffected (verified: browser "N2-1B: after an admin activates a new catalog version in the UI, a draft generated from the earlier version still saves at its pinned prices" **passes** in R4/R4b, and API `test_n2_1b_rfq_pinned_quote_…`).
- Test: `test_n2_1b_unpinned_draft_still_saves_after_a_catalog_activation_when_lines_are_resent_by_the_editor`.

### D2-3 — Migration 0005's L2-5 data fix updates zero rows (owner is subject to FORCE RLS) — **Medium** (L2-5 claimed fixed; S1-F15 for legacy data)
- Where: `apps/api/alembic/versions/20261101_0005_session_identity_guard.py:45-47` — `UPDATE quote_versions v SET status='withdrawn' FROM quotes q WHERE … q.withdrawn_reason IS NOT NULL AND v.status='approved'` runs as `quotepilot_owner` with no `app.current_org` setting. `quotes` and `quote_versions` have `FORCE ROW LEVEL SECURITY` (`20261101_0001_stage1_initial.py:377-383`, `TENANT_TABLES`), and `quotepilot_owner` is neither superuser nor `BYPASSRLS` (`pg_roles`: `rolsuper=f, rolbypassrls=f`), so the policy `organization_id = NULLIF(current_setting('app.current_org', true), '')::uuid` hides every row and the statement is a no-op in every environment. Additionally, even with visibility, the predicate misses the shape the reviewer flagged (R-1/N2-2 revival): once a replacement draft exists, `new_version` has cleared `withdrawn_reason` (`quotes.py:828`), while the version row still says `approved` and its decision trail says `withdrawn`.
- Request: (a) approved v1 → withdraw → set the version row back to `approved` (legacy shape, quote still `withdrawn`); (b) approved v1 → withdraw → new version v2 → set v1 back to `approved` (legacy revived shape, exports `200` as APPROVED before the fix). Then `alembic downgrade -1 && alembic upgrade head`.
- Expected: both v1 rows `withdrawn` after the upgrade; exports `409`.
- Actual: both rows remain `approved` (`"0005 data fix left the legacy approved version as 'approved'"`); in shape (b) the withdrawn document stays exportable as APPROVED (`200`). The identity triggers are (re)created correctly.
- Tests: `test_l2_5_migration_0005_data_fix_withdraws_an_approved_version_left_under_a_withdrawn_quote`, `test_l2_5_migration_0005_data_fix_also_covers_a_withdrawn_document_whose_quote_already_has_a_new_version`.
- Note for the fix: under `FORCE ROW LEVEL SECURITY` the owner cannot simply `SET row_security = off`; the data fix needs to iterate organisations with `app.current_org` set, temporarily switch the two tables to `NO FORCE ROW LEVEL SECURITY`, or run as a `BYPASSRLS` role — and its predicate should key on `approval_decisions.decision = 'withdrawn'` (or the audit trail) rather than on `quotes.withdrawn_reason`. The migration test in CI (`upgrade/downgrade/upgrade` on an empty database) cannot detect either problem; the QA replay tests above do.

### D2-4 — Withdrawing while a newer draft is open is impossible from the quote screen — **Medium** (N2-2 second path, S1-F15, USER_JOURNEYS "Withdraw approved quote")
- Where: `apps/web/src/app/(app)/quotes/[id]/page.tsx:203` — the Withdraw reason field and button render only when `quote.status === "approved" || quote.status === "pending_approval"`; after "Create new version" `quote.status` is `draft`, so the control disappears although an approved v1 document is still issued and the API now accepts the withdrawal (`API_CONTRACT.md:42`). Related: after an API withdrawal in that state the screen shows no "Withdrawn" banner (`page.tsx:123` keys on `quote.status === "withdrawn"`), so the reason is invisible; the Versions card does show the v1 badge `withdrawn`.
- Request (browser): Sales: RFQ → quote → submit; Approver approves; Sales "Create new version" (v2 draft). Approver opens the quote.
- Expected: a Withdraw control (the journey belongs to owner/admin/approver); after use, v1 `withdrawn`, v1 PDF/CSV `409`, the v2 draft stays editable.
- Actual: no Withdraw control on the page (`getByRole("button", { name: "Withdraw" })` not found); the approver must use the API directly. The fix is therefore API-only; the review-2 scenario "withdrawing while a draft is current was impossible" still holds for users of the UI.
- Test: browser `qa-reverify-review2.spec.ts` "N2-2: an approver can withdraw the approved v1 from the quote screen while a newer draft is open".

### D2-5 — `withdrawn_reason` persists on a quote whose later version was approved — **Low** (data hygiene; feeds D2-3's predicate and the UI banner)
- Where: `apps/api/app/services/quotes.py:743-746` sets `quote.withdrawn_reason` on the withdraw-while-draft path; `approve()` (`quotes.py:631-634`) does not clear it. `new_version` clears it (`:828`), `approve` does not.
- Request: approved v1 → v2 draft → approver withdraws v1 (reason "v1 priced wrong") → Sales submits v2 → approver approves v2 → `GET /quotes/{id}`.
- Expected: `status == "approved"`, `withdrawn_reason == null` (the withdrawal concerned v1 only; the API payload should not describe an approved quote as withdrawn).
- Actual: `status == "approved"`, `withdrawn_reason == "v1 priced wrong"`. Everything else on this path is correct (v2 exports for every role, v1 stays `409`).
- Test: last assertion of `test_n2_2_approver_withdraws_v1_while_v2_draft_is_open_then_approved_v2_exports_while_v1_stays_blocked` (all N2-2 assertions before it pass).

### Observations (no failing test; for product/implementer decision)
- **O2-1** Pinning semantics vs. the error text: a quote generated from an RFQ keeps that RFQ's catalog version as its only price authority for ever (`_pricing_catalog_version_id`, `quotes.py:266-274`; `new_version` copies the pin, `:800-801`). After an activation, linking a product from the *active* version is refused with "Product is not in the active catalog version used for this quote", and products of the *archived* pinned version remain linkable at archived prices. The security invariant the reviewer asked for holds (no never-active price list is ever used), but PRD S1-F11 says "unit price from active catalog version", and neither USER_JOURNEYS nor API_CONTRACT documents the pin or how a draft can move to the new price list. Documented in `test_n2_1b_rfq_pinned_quote_prices_only_from_its_own_version_and_new_versions_keep_the_pin`.
- **O2-2** The withdraw-while-draft state is represented only by `withdrawn_reason` on a `draft` quote plus per-version statuses; the quotes list filter `status=withdrawn` and `effective_status` do not show such quotes as having a withdrawn document, and the quote page shows no banner (see D2-4). A per-quote derived flag ("has withdrawn documents") would make the state visible.
- **O2-3** A pending version made obsolete by a withdrawal (`superseded`) can still be previewed as a PDF labelled "WITHDRAWN" by every role (status 200). Defensible (it is not an approved document), but not documented.
- **O2-4** The implementer's N2-1 B test asserts `"active catalog version" in r.text` — the same misleading message as O2-1; if the message is corrected the test will need the same edit.
- **O2-5** The N2-1 B check runs for untouched lines (root of D2-2) and the manual-price rule runs for unchanged prices (root of D2-1): both would be avoided by diffing the payload against the stored line before applying "link/price changed" semantics, which `update_draft` already does for the margin-override invalidation.

## Not tested / limitations
- No Docker daemon: compose topology (L2-2) and the image were not run here (security scope).
- The L2-5 replay uses `alembic downgrade -1 / upgrade head` inside a test (session-scoped migration fixture left at head afterwards); it was not run against a database that genuinely predates round 4.
- Browser evidence is Chromium only; axe scans are the predecessor's and the implementer's (unchanged, green).
- CI status on `58976f3` was not checked from this container (no network).
