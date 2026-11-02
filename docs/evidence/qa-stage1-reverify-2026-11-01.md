# QA re-verification evidence — Stage 1 after release review 1 (2026-11-01)

Independent QA/Test Engineer re-verification of branch `claude/multi-stage-project-impl-8hz2wj` at HEAD
`ff99762` ("Stage 1 fix round 2: address independent release review 1"). Inputs:
`docs/evidence/release-review-1-stage1-2026-11-01.md` (findings and "Required retest"),
`docs/evidence/qa-stage1-2026-11-01.md` (predecessor QA: coverage map S1-A01..A20, defects D1–D8),
`docs/PRD.md` Stage 1, `docs/API_CONTRACT.md` (blocker `manual_price_unconfirmed`,
`POST /quotes/{id}/lines/{line_id}/price-override`), `docs/KNOWN_LIMITATIONS.md`. The implementer's own evidence for
this round is the commit message of `ff99762` and `git show ff99762 -- apps/api/tests tests/e2e`
(no `implementer-fixes2-*.md` file exists in `docs/evidence/`). No product code was modified; no existing test was
weakened, skipped or deleted.

**Provenance note.** All runs R1–R7 below completed by 22:43:45 UTC. At 22:43:46 another agent began modifying
product files in the shared working tree (uncommitted: `apps/api/app/services/quotes.py`, `exports/quote_exports.py`,
`core/config.py`, `api/routers/org.py`, `models/quote.py`, new migration `20261101_0004_review2_hardening.py`,
`apps/web/src/app/login/page.tsx`, `apps/api/Dockerfile`; plus `tests/security/test_audit_followups2.py` and
`docs/evidence/security-stage1-reverify-2026-11-01.md` from the security auditor). Every figure in this report was
therefore produced against the committed product code of `ff99762`; the QA database remained at migration `0003`.
The defects below must be re-checked against whatever the implementer commits next — the failing tests are in place
for that.

## Environment
| Item | Value |
|---|---|
| Container | Claude Code cloud container, Ubuntu 24.04, no Docker daemon, no external network |
| Python / API venv | Python 3.13, `apps/api/.venv` (pytest 8, FastAPI TestClient, reportlab, pdfplumber, openpyxl available) |
| PostgreSQL | 16 local service; dedicated QA database `quotepilot_qa` (app role `quotepilot_app` under RLS, `quotepilot_owner` for migrations). Found at revision `0001`; `alembic upgrade head` applied `0002` and `0003` cleanly (`0003 (head)`). `APP_ENV=test`; `DATABASE_URL`/`DATABASE_ADMIN_URL` → `…@localhost:5432/quotepilot_qa` (credentials redacted). |
| Node / Playwright | Node 22, `@playwright/test` 1.63.0, `@axe-core/playwright` 4.14, bundled Chromium pre-installed (`playwright install` not run) |
| E2E | API `127.0.0.1:8031` (uvicorn), web `localhost:3031` (`next start` on the prebuilt `apps/web/.next`, `E2E_SKIP_WEB_BUILD=1`); global setup migrates + seeds the same `quotepilot_qa` database |
| AI provider | `disabled` |
| Evidence layers | **unit** = pure functions; **integration** = API + PostgreSQL + RLS through the ASGI TestClient (`tests/integration`, `tests/security`, `tests/qa`); **browser** = Playwright against real uvicorn + Next.js (`tests/e2e`) |

## Runs (exact commands, cwd, exit codes, counts)
All pytest runs: `export APP_ENV=test DATABASE_URL=postgresql+psycopg://quotepilot_app:***@localhost:5432/quotepilot_qa DATABASE_ADMIN_URL=postgresql+psycopg://quotepilot_owner:***@localhost:5432/quotepilot_qa`.
All Playwright runs: `E2E_SKIP_WEB_BUILD=1 E2E_API_PORT=8031 E2E_WEB_PORT=3031` plus the same two database URLs.

| # | When | Command | cwd | Exit | Result |
|---|---|---|---|---|---|
| R1 | before additions | `.venv/bin/pytest -q -p no:cacheprovider` | `apps/api` | 0 | **252 passed**, 0 failed, 0 skipped, 0 xfailed (207.3 s). Includes all 97 predecessor QA tests (D1–D8 regression tests now green) and the implementer's 12 `test_review1_fixes.py` tests. |
| R2 | before additions | `npx playwright test specs/qa-stage1-negative.spec.ts specs/review1-regressions.spec.ts specs/screenshots.spec.ts specs/stage1-flow.spec.ts --reporter=list` (= every pre-existing spec) | `tests/e2e` | 0 | **16 passed**, 0 failed, 1 skipped (`screenshots.spec.ts`, self-skipped) (54.3 s): 6 `stage1-flow` + 6 `qa-stage1-negative` + 4 `review1-regressions` (incl. axe). Global setup migrated + seeded `quotepilot_qa`; API on :8031, web on :3031. |
| R3 | new QA module only | `.venv/bin/pytest -q -p no:cacheprovider tests/qa/test_qa_reverify_review1.py` | `apps/api` | 1 | **24 passed, 4 failed** of 28 (41.2 s). The 4 failures are product defects R-1…R-4 below; left in place. |
| R4 | after additions (first full run) | `npx playwright test --reporter=list` | `tests/e2e` | 1 | **18 passed, 3 failed, 1 skipped** (1.6 min). All 16 pre-existing tests green. Failures, all in the new `qa-reverify-review1.spec.ts`: "S1-F11: removing a line…" (**R-3** in the browser: the editor shows "Unexpected error (ref …)"), "X-08 …" (**R-5**, axe `link-in-text-block` on the RFQ review screen; the scan stopped at the first failing screen), and "H-1: organisation and customer edits…" — a **test bug** (the approver's CSV legitimately carries the `cost_price` column, so a cross-role byte compare is wrong; the PDF bytes were identical for every role). The spec was corrected (approver CSV compared row-wise; axe scan collects all screens before asserting) and the whole suite re-run as R6. |
| R6 | after additions (corrected spec) | `npx playwright test --reporter=list` | `tests/e2e` | 1 | **18 passed, 3 failed, 1 skipped** (1.5 min). Pre-existing 16 green. "S1-F11: removing a line…" → **R-3** (as R4); "X-08 …" → **R-5**, now with the complete list: the RFQ review screen is the *only* screen with a gating violation (login, editor with blockers for sales, editor with confirmation controls for owner, `/settings`, read-only quote are clean). "H-1: organisation and customer edits…" passed every document assertion (v1 PDF byte-identical for sales/read-only/approver after the Settings/Customers edits and the new version, CSV rows unchanged, stored sha unchanged, draft v2 differs) and failed only on the final navigation assert, whose locator `a:has-text("PDF")` matched the header "Preview PDF (draft)" link — a test bug, corrected and re-run as R7. |
| R7 | after additions (QA spec only, after locator fix) | `npx playwright test specs/qa-reverify-review1.spec.ts --reporter=list` | `tests/e2e` | 1 | **3 passed, 2 failed** of 5 (40.1 s): H-1 UI, M-4 UI and M-3 UI pass; "S1-F11: removing a line…" (**R-3**) and "X-08 …" (**R-5**, RFQ review screen only) remain red as the regression gate. Final browser state on HEAD therefore: 16 pre-existing + 3 QA green, 2 QA red (both product defects), 1 self-skipped. |
| R5 | after additions | `.venv/bin/pytest -q -p no:cacheprovider` | `apps/api` | 1 | **283 passed, 4 failed, 6 xfailed**, 0 skipped (338.5 s). 293 collected = 252 pre-existing + 28 QA (this report) + 13 in `tests/security/test_audit_followups2.py`, which the security auditor added to the working tree while this run was prepared (its 6 `xfail`s are its own expected failures, not QA findings; it runs against a separate `quotepilot_sec` database). All 252 pre-existing tests still pass; the 4 failures are the QA defects **R-1…R-4** (same per-test outcome as R3). |

Static checks on the added files: `.venv/bin/ruff check` and `ruff format --check` on
`tests/qa/test_qa_reverify_review1.py` → clean (so CI's "Lint and type-check" job, which failed on 3169ad6 for
unformatted QA tests, is not re-broken); `npx tsc --noEmit -p tsconfig.json` in `tests/e2e` → exit 0.

## Review of the implementer's edits to existing tests (`git show ff99762 -- apps/api/tests tests/e2e`)
| File | Nature of change | Legitimate? |
|---|---|---|
| `tests/qa/test_qa_catalog_audit_tenancy.py`, `test_qa_org_members_auth.py`, parts of `test_qa_quote_lifecycle.py`, `test_qa_rfq_review.py` | `ruff format` reflows only (line wrapping, parenthesised asserts); no literal, status code or assertion changed | yes |
| `tests/integration/test_quotes.py::test_currency_mismatch_blocks_autofill`, `tests/qa/test_qa_rfq_review.py::test_unknown_item_custom_line_requires_explicit_price`, `tests/qa/test_qa_quote_lifecycle.py::test_custom_line_without_price_blocks_submit_until_priced`, `_below_margin_quote`, `test_margin_override_cleared_after_price_edit` | `blockers == []` after a manual price → `["manual_price_unconfirmed"]` (policy change M-4, documented in KNOWN_LIMITATIONS and API_CONTRACT). `test_custom_line_without_price…` was *extended* with submit-409 → admin price-override → submit-200. | yes — encodes the new policy; strictly stronger |
| `tests/conftest.py` | resets the new `account_limiter` between tests | yes (test isolation) |
| `tests/e2e/specs/qa-stage1-negative.spec.ts` | custom-line test extended with the admin confirmation step; notice/label wording `override recorded` → `price confirmed`, `Margin override:` → `Price confirmed by owner/admin:` | yes — follows the UI copy change; assertions not weakened |

Nothing was skipped, deleted or loosened. One quality gap in the implementer's **new** tests (not a weakening):
`tests/integration/test_review1_fixes.py:46` (`assert b"2099-12-31" not in again.content and b"Renamed After Approval"
not in again.content`) and `tests/e2e/specs/review1-regressions.spec.ts:40`
(`expect(pdf1Again.toString("latin1")).not.toContain("2099-12-31")`) are **vacuous**: the PDF is produced by
reportlab with FlateDecode page streams, so no visible text ever appears as a raw byte substring (verified: a rendered
PDF whose extracted text contains the customer name does not contain it as bytes). Only their byte-identity
assertions are evidence. The QA tests below assert on text extracted with pdfplumber instead.

## Per-item verification
Legend: **impl** = implementer's regression test in `tests/integration/test_review1_fixes.py` / `tests/e2e/specs/review1-regressions.spec.ts`;
**QA** = added in this round (`tests/qa/test_qa_reverify_review1.py` = API/integration, `tests/e2e/specs/qa-reverify-review1.spec.ts` = browser).

| Item | Tests that prove the fix | Gap in impl test → QA probe added | Verdict |
|---|---|---|---|
| **H-1** approved document mutable after approval | impl `test_h1_approved_document_immutable_after_new_version_and_validity_edit`, `test_h1_expiry_judged_on_frozen_validity`; browser impl "H-1: approved v1 document is immutable…" (byte identity) | impl covers org *name* only, text-absence asserts vacuous, no customer rename, no CSV, no storage-loss, no second approved version. QA: `test_h1_customer_rename_and_brand_change_do_not_alter_approved_pdf_or_csv` (customer rename + reference, org legal name/address/brand colour/footer, v2 with edited lines/validity/notes; PDF via GET and POST byte-identical for sales/approver/readonly/owner, extracted text unchanged and free of the new values, CSV byte-identical, v2 draft renders the new data), `test_h1_stored_pdf_lost_is_rerendered_from_snapshot_with_identical_hash`, `test_h1_frozen_expiry_blocks_v1_while_a_later_approved_version_exports` (snapshot `valid_until` frozen in the past → 409 for PDF GET/POST and CSV for every role while v2 exports; v3 header 2099 does not revive v1), `test_h1_approval_freezes_validity_so_a_later_header_expiry_cannot_be_undone_by_sales`; browser QA "H-1: organisation and customer edits made in the UI after approval…" (Settings + Customers screens, new version via UI) | **FIXED** (all QA probes pass). Residual: **R-1** — withdrawal does not stick to the version the way expiry does |
| **M-1** limiter key before `strip()` | impl `test_m1_login_limiter_ignores_email_whitespace_and_case` (per-IP bucket, 12 attempts) | the *per-account* bucket was untested. QA: `test_m1_per_account_bucket_catches_distributed_guessing_with_whitespace_and_case_variants` — distinct source addresses cannot be presented through the TestClient (it always reports `testclient`), so they are **simulated by resetting the per-IP limiter after every attempt**; only the account bucket accumulates: 50 (= 5 × per-IP budget) wrong attempts across six whitespace/case variants → 401, the 51st → 429, the correct password is also held back, another account is unaffected, hits are counted once per attempt; `test_m1_per_ip_bucket_is_shared_by_whitespace_and_case_variants` | **FIXED** (single process; multi-replica remains a documented limitation) |
| **M-3** blocker text leaks margin % and floor | impl `test_m3_blocker_text_is_role_aware` (floor 12 %, checks `"%"`/`"12"` absent for sales, present for approver); browser impl "M-3: sales never sees margin figures…" | impl checks sales only and a floor value (12) that collides with line numbers. QA: `test_m3_blocker_text_hides_margin_and_floor_from_sales_and_readonly_on_every_surface` (floor 37.5 %; GET, submit-409 payload, read-only, read-only submit → 403, no `cost_price`/`margin_pct` fields; owner/admin/approver still see the figures) **passes**; `test_m3_generate_quote_409_from_rfq…` passes; **`test_m3_confidential_floor_is_not_in_the_quote_payload_for_sales_and_readonly` FAILS** — see **R-2**; browser QA "M-3: read-only and sales blocker text…" | **PARTIAL** — messages fixed, the floor itself is still readable by Sales/Read-only in `versions[].pricing_snapshot.min_margin_pct` |
| **M-4** Sales bypasses margin floor via custom line | impl `test_m4_manual_price_requires_privileged_confirmation` (custom line: blocker, sales 403, admin 200, submit; typed-over catalog price: blocker presence only) | typed-over path not taken past the blocker. QA: `test_m4_typed_over_catalog_price_full_lifecycle` (higher price so no margin blocker; `overridable_by`; sales/approver/readonly → 403; reason < 5 chars / missing → 422; owner clears; a later price edit by sales voids the confirmation and re-raises the blocker; the editor's re-send of the same values keeps it; re-selecting the catalog product restores `catalog` price with nothing to confirm; submit 200), `test_m4_margin_override_alias_and_price_override_are_the_same_privileged_action` (alias, approver 403, exactly one audit row with reason/line), `test_m4_new_version_copies_manual_price_but_not_its_confirmation`, `test_m4_generated_custom_line_needs_price_then_confirmation_in_browser_order` (RFQ review → custom → `missing_price` → `manual_price_unconfirmed` → approver 403 → admin clears); browser QA "M-4: a catalog price typed over in the editor…" (submit disabled, no confirmation control for sales/approver, approver API 403, owner confirms with reason, label on the line, sales submits) | **FIXED** (see observation O-R3 on re-confirmation per version) |
| **L-3** last-active-owner race | impl `test_l3_last_owner_guard_is_serialised` (two owners deactivate each other concurrently; exactly one wins) | O1 (admin vs owner) not re-asserted. QA: `test_l3_admin_may_not_deactivate_or_demote_any_owner_even_when_another_owner_exists` (admin refused for both owners even with two active owners; owner may retire the second owner, then demotion/deactivation of the last one → 409) | **FIXED** (observation O-R4: refusal is 422 not 403) |
| **L-4** totals overflow | impl `test_l4_total_overflow_rejected` (3 × 1e14 lines → 422 "maximum") | boundary and atomicity untested. QA: `test_l4_total_cap_boundary_is_exact_and_a_refused_save_changes_nothing` (99 999 980 000 001 accepted; adding a second such line → 422 and the quote, lock_version and total are unchanged; exactly 1e14 → 422) | **FIXED**. Probing this surfaced **R-3/R-4** (500 on line replacement/removal) |
| **L-5** caps truncate instead of rejecting (S1-A08) | impl `test_l5_oversized_rfq_and_catalog_are_rejected_not_truncated` (CSV RFQ rows and catalog rows with caps monkeypatched to 5) | PDF page cap and XLSX untested. QA: `test_l5_pdf_with_more_pages_than_the_cap_fails_cleanly_without_partial_lines` (reportlab 3-page PDF vs `max_pdf_pages=2` → `failed`, reason `too_large … 2 pages`, `items == []`, generate → 409, reparse still failed, two `rfq.parse_failed` audit rows; 2 pages → parsed with one item per page), `test_l5_pdf_page_cap_default_is_enforced_without_monkeypatching` (real default: 51 pages refused, 50 parsed → 50 items), `test_l5_xlsx_with_more_rows_than_the_cap_is_rejected_not_truncated` | **FIXED** (observation O-R2 on where the RFQ spreadsheet cap is enforced) |
| **L-10** weak SECRET_KEY guard | impl `test_l10_weak_secret_refused_outside_dev` (staging + marker) | QA `test_l10_secret_key_guard_matrix` ×6: production + 64 identical chars → refused; strong key accepted; marker embedded in an otherwise strong key → refused; staging e2e placeholder → refused; development/test placeholders still allowed | **FIXED** |
| **X-05** AI consent | impl `test_x05_ai_consent_is_owner_only_and_recorded` (admin 422, owner 200, provider stays disabled) | QA `test_x05_consent_role_matrix_audit_and_visibility`: sales/readonly/approver → 403, admin → 422, consent timestamp visible to admin, absent from the Sales (public) org view together with `min_margin_pct`, `org.update` audit row with `fields`/`after` and actor = owner, withdrawal clears the timestamp | **FIXED** |
| **X-08** accessibility | browser impl "X-08: core screens have no serious or critical accessibility violations (axe)" (6 static routes as sales) | dynamic screens with blockers, override controls and other roles unscanned. Browser QA "X-08: login, review, editor (with blockers), approval and settings screens…" scans `/login`, RFQ review with blockers, quote editor with blockers (sales), editor with confirmation controls (owner), `/settings` (owner) and the read-only quote view; moderate/minor ids recorded as a non-gating annotation | **PARTIAL** — the six static screens and five of the six dynamic/role screens are clean; the RFQ review screen has one serious violation (**R-5**) |
| **D1** expired approved quote exportable | predecessor `test_expired_approved_quote_cannot_be_exported_as_current[GET]`/`[POST]`, `test_expired_approved_quote_is_reported_expired` (pass in R1) + QA H-1 expiry tests above | — | **FIXED** |
| **D2** withdrawn-while-pending approvable | predecessor `test_withdrawn_pending_quote_cannot_change_state[approve]`/`[reject]`/`[submit]` (pass) + QA `test_withdrawn_approved_version_blocks_every_export_and_revives_only_via_new_version` (approve/reject/submit on a withdrawn *approved* quote → 409 — those asserts pass; the test fails later on **R-1**) | — | **FIXED** for the pending case |
| **D3** withdrawn/expired CSV exportable | predecessor `test_withdrawn_approved_quote_cannot_be_exported_as_csv_current` (pass) + QA (PDF GET/POST + CSV × 4 roles → 409 while withdrawn) | — | **FIXED** while the quote is withdrawn; **R-1** once a new version exists |
| **D4** override dropped by unchanged save | predecessor `test_margin_override_survives_save_with_unchanged_price` (pass); QA M-4 lifecycle re-send step | — | **FIXED** |
| **D5** last active owner deactivatable | predecessor `test_last_active_owner_guard_counts_only_active_owners` (pass); impl L-3; QA L-3 | — | **FIXED** |
| **D6** logout without CSRF | predecessor `test_logout_requires_csrf_token` (pass) | — | **FIXED** |
| **D7** 500 on out-of-range quantity | predecessor `test_patch_item_rejects_invalid_quantity[1e400]`/`[9999…]`, `test_add_item_rejects_out_of_range_quantity[…]` (pass) | — | **FIXED** |
| **D8** leading minus made positive | predecessor `test_leading_minus_quantity_in_pasted_text_is_not_silently_made_positive` (pass) | — | **FIXED** |

## Coverage map re-check — S1-A01..A20 on the current suites
| ID | Current evidence (layer) | Still unit-only / missing? |
|---|---|---|
| S1-A01, A02, A04, A10 | predecessor QA integration + browser (`qa-stage1-negative` test 1, `stage1-flow`) | no |
| S1-A03 | integration (predecessor, updated to the M-4 policy) + QA `test_m4_generated_custom_line_needs_price_then_confirmation_in_browser_order` + browser (`qa-stage1-negative` test 2 with admin confirmation; QA M-4 browser test) | no — the "privileged" wording of S1-A03 is now implemented and tested |
| S1-A05, A06 | integration (+ browser for the catalog 415) | no |
| S1-A07 | integration (RFQ CSV path, every prefix) + unit | no |
| S1-A08 | integration: impl L-5 (CSV/catalog rows, monkeypatched caps) + QA PDF pages (monkeypatched **and** real default 50/51) + QA XLSX rows + predecessor pasted-text boundary (100 000/100 001) + 10 MiB byte cap | no. Not exercised with a real 50 000-row catalog fixture (size); caps proven with reduced limits plus the real PDF page cap |
| S1-A09 | integration (D7 fixed: 422 with field) | no |
| S1-A11, A12 | integration + unit | no |
| S1-A13 | integration + browser | no |
| S1-A14 | integration + security + browser | no |
| S1-A15 | integration (predecessor D1–D3 tests, impl H-1, QA H-1/withdrawn) | no, but **R-1** is an open failing case |
| S1-A16 | integration + browser conflict banner | no |
| S1-A17 | integration with `run_parser` stubbed (predecessor) + QA L-5 real-subprocess page-cap failure (clean `failed`, no partial lines, retry) + unit sandbox timeout | a **genuine wall-clock timeout/crash of the real subprocess** is still unit-only (`test_sandbox_timeout_and_crash_are_contained`); a multi-second hang was not provoked at integration level |
| S1-A18 | integration (stored-once artifact, identical across roles/calls, storage-loss re-render) + browser byte identity | no |
| S1-A19 | integration (23 mutation types) + QA X-05 audit before/after | no |
| S1-A20 | browser (`stage1-flow`, `qa-stage1-negative`, `review1-regressions`, QA `qa-reverify-review1`) | no |

No acceptance ID is unit-only; S1-A17's genuine timeout path is the only remaining unit-only sub-case.

## Tests added (new files only; nothing existing was changed)
| Path | Layer | Count | What it verifies |
|---|---|---|---|
| `apps/api/tests/qa/test_qa_reverify_review1.py` | integration | 28 | H-1 ×4, M-4 ×4, M-3 ×3, M-1 ×2, L-4 ×1 + line replace/remove ×2, L-5 ×3, L-10 ×6 (parametrised), L-3/O1 ×1, X-05 ×1, withdrawn-approved lifecycle ×1 (see table above) |
| `tests/e2e/specs/qa-reverify-review1.spec.ts` | browser (Chromium) | 5 | M-4 typed-over price in the editor with approver/owner contexts; H-1 via Settings and Customers screens + new version; editor Remove of a non-last line (S1-F11); M-3 for read-only/sales/approver; X-08 axe on six dynamic/role-specific screens |
| `docs/evidence/qa-stage1-reverify-2026-11-01.md` | — | — | this report |

## Defects
Severity: Critical / High / Medium / Low. Each has a failing test left in place.

### R-1 — Withdrawn approved version becomes exportable as an APPROVED document again once a new version is opened — **Medium** (S1-F15, S1-A15; residual of D3/H-1)
- Where: `apps/api/app/services/exports/quote_exports.py:93-108` (`_check_exportable` tests `quote.status == "withdrawn"`, a quote-level flag) and `apps/api/app/services/quotes.py:777-778` (`new_version` sets `quote.status = "draft"` and clears `withdrawn_reason`). Expiry was moved into the version's `document_snapshot` by the H-1 fix; withdrawal was not, so it does not stick to the version.
- Request: approve v1 → approver `POST /quotes/{id}/withdraw` (200; v1 exports now 409) → Sales `POST /quotes/{id}/versions` (201) → `GET /quotes/{id}/versions/1/pdf` or `/csv`.
- Expected: `409 invalid_state` — the decision trail of v1 reads `["approved", "withdrawn"]`; a withdrawn document must not be re-issued as APPROVED.
- Actual: `200 application/pdf` (stored artifact labelled APPROVED, validity still in the future) and `200 text/csv`. Any Sales user can revive a withdrawn document by opening a replacement draft.
- Test: `tests/qa/test_qa_reverify_review1.py::test_withdrawn_approved_version_blocks_every_export_and_revives_only_via_new_version`.

### R-2 — Confidential margin floor is exposed to Sales and Read-only through `pricing_snapshot` — **Medium** (review-1 M-3 not fully fixed; S1-F16 cost confidentiality)
- Where: `apps/api/app/services/quotes.py:266-273` (`_snapshot` stores `"min_margin_pct": str(org.min_margin_pct)` in every version's `pricing_snapshot`), `apps/api/app/schemas/common.py:330` (`QuoteVersionOut.pricing_snapshot: dict[str, Any]` serialised for every role), `apps/api/app/api/routers/quotes.py:34-50` (`_version_out` filters line costs by `VIEW_COSTS` but not the snapshot). The same value is hidden from these roles in `GET /org` (`OrganizationPublic`) and was removed from the blocker text by the M-3 fix.
- Request: as Sales or Read-only, `GET /quotes/{id}` or `GET /quotes/{id}/history` on any quote.
- Expected: no `min_margin_pct` (or any other margin/cost figure) in the payload for roles without `VIEW_COSTS`.
- Actual: `current_version.pricing_snapshot.min_margin_pct` (and every entry of `versions[]`) carries the floor verbatim (`"37.5"` in the test tenant), so the sanitised blocker message is defeated by one GET.
- Test: `test_m3_confidential_floor_is_not_in_the_quote_payload_for_sales_and_readonly`.

### R-3 — Removing a line that is not the last one fails with 500 (unique-constraint violation) — **High** (S1-F11 quote editor; `apps/web` "Remove" button)
- Where: `apps/api/app/services/quotes.py:362-506` (`update_draft`): lines are renumbered in payload order (`line.line_no = n`, line 410) and removed lines are deleted afterwards (line 504) in the same flush; SQLAlchemy emits the UPDATE/INSERT of the renumbered line before the DELETE, violating `uq_quote_line_no` (`app/models/quote.py:105`). The exception surfaces as `500 {"error":"internal_error"}` and an `unhandled error` log line.
- Request: a 2-line draft; `PATCH /quotes/{id}` with `{"expected_lock_version": n, "lines": [{"id": <line 2 id>}]}` — exactly what the editor sends after "Remove" on line 1 (it re-sends the remaining lines with their ids).
- Expected: `200`, one line with `line_no = 1`.
- Actual: `500 internal_error` (`psycopg.errors.UniqueViolation: duplicate key value violates unique constraint "uq_quote_line_no"`). The browser shows the generic error and the removal is lost. Removing the *last* line works, which is why existing tests never hit it.
- Tests: `test_editor_remove_of_a_non_last_line_saves_without_server_error` (API); browser `qa-reverify-review1.spec.ts` "S1-F11: removing a line that is not the last one in the editor saves cleanly" (fails: after "Remove" + "Save draft" the page shows the generic alert "Unexpected error (ref <request id>)", no "saved" notice; screenshot under `tests/e2e/test-results/`).

### R-4 — Replacing a line with a new one in a single save fails with 500 — **Medium** (same root cause as R-3; API contract "`lines` is the full desired list")
- Where: as R-3 (`quotes.py:409-410` INSERT of the new line at `line_no = 2` precedes the DELETE of the old line 2).
- Request: 2-line draft; `PATCH /quotes/{id}` with `lines: [{"id": <line 1 id>}, {"description": "Replacement", "quantity": "1", "unit_price": "3", "is_custom": true}]`.
- Expected: `200`, line 2 replaced.
- Actual: `500 internal_error` (`uq_quote_line_no`).
- Test: `test_replacing_a_line_with_a_new_one_in_a_single_save_is_not_a_server_error`.

### R-5 — RFQ review screen: link distinguishable by colour only (axe `link-in-text-block`, impact serious) — **Low** (X-08, WCAG 2.1 SC 1.4.1)
- Where: `apps/web/src/app/(app)/rfqs/[id]/page.tsx:190` — `<Link href="/catalog" className="text-brand hover:underline">Manage catalog</Link>` inside a paragraph of body text; the link is identified by colour alone (underline only on hover). The implementer's X-08 scan covered `/rfqs/new`, not a parsed RFQ's review screen, so this screen was never scanned.
- Request: open any parsed RFQ (`/rfqs/{id}`) and run axe with tags `wcag2a`, `wcag2aa`.
- Expected: no serious/critical violations (the gate the implementer's own test applies to the six static screens).
- Actual: `link-in-text-block (serious)` on `.hover\:underline.text-brand[href$="catalog"]`. No other scanned screen (login, RFQ `/rfqs/new`, quote editor with blockers as Sales, editor with confirmation controls as Owner, `/settings` as Owner, read-only quote view, plus the implementer's six static routes) has a serious or critical violation.
- Test: browser `qa-reverify-review1.spec.ts` "X-08: login, review, editor (with blockers), approval and settings screens have no serious/critical axe violations".

### Observations (no failing test; for product/implementer decision)
- **O-R1** Vacuous text-absence assertions in the implementer's H-1 tests (`test_review1_fixes.py:46`, `review1-regressions.spec.ts:40`): reportlab output is Flate-compressed, so `b"2099-12-31" not in content` is always true. Replace with text extraction (pdfplumber) as done in the QA tests; the byte-identity assertions remain valid evidence.
- **O-R2** `apps/api/app/services/parsing/sandbox.py:38` passes `max_catalog_rows` (50 000) to the worker for *every* CSV/XLSX, including RFQs; RFQ spreadsheets are rejected afterwards by `len(items) > max_rfq_rows` in `rfqs.py`. The outcome is correct (`failed`, `too_large`, no lines) but a 2 001–50 000-row RFQ spreadsheet is fully parsed in the sandbox before rejection. The page cap for PDFs is applied inside the worker. The CSV/XLSX caps count the header row as a row.
- **O-R3** `new_version` copies manual prices but not their confirmation (`margin_override_reason/_by` are not copied), so every new version of a quote with a confirmed manual or below-floor price needs a fresh owner/admin confirmation (`test_m4_new_version_copies_manual_price_but_not_its_confirmation` documents the behaviour). Defensible (the audit row names the v1 line), but USER_JOURNEYS/API_CONTRACT do not state it.
- **O-R4** Role restrictions inside `PATCH /org` and `PATCH /org/members/{id}` (`org.py:49,53,91,141,145,154`) answer `422 validation_error` ("Only an owner can …") whereas `docs/API_CONTRACT.md:7` says "Role denial → 403". The security property holds; the status code is inconsistent with the contract and with `price-override` (403).
- **O-R5** After confirmation, the line shows "Price confirmed by owner/admin: <reason>" and the audit action is still named `quote.margin_override` even when the confirmation concerned a manual price with a healthy margin; cosmetic.

## Not tested / limitations
- Distinct source IP addresses cannot be presented through the ASGI TestClient (`request.client.host` is always `testclient`); the per-account limiter was isolated by resetting the per-IP limiter between attempts (documented in the test). Real multi-address brute force and the multi-worker budget (`--workers 2`) remain untested; the Redis-backed limiter is a Stage 2 item.
- M-2 (forwarded-header trust under the Docker CMD) and the compose topology: no Docker daemon in this container; not re-verified here (security scope).
- A genuine multi-second parser hang/crash inside the real subprocess (S1-A17) was not provoked at integration level (unit evidence only); the real page cap, oversized spreadsheets, corrupted files and the stubbed timeout/crash paths are integration-tested.
- Catalog cap with a real 50 000-row fixture not exercised (monkeypatched cap + 10 MiB byte cap + zip-bomb guard are).
- Accessibility: axe WCAG 2 A/AA automated rules only (serious/critical gating, moderate/minor recorded); no manual screen-reader or keyboard audit.
- `tests/e2e/specs/screenshots.spec.ts` self-skips (walkthrough capture), untouched.
- CI status on `ff99762` was not checked from this container (no network).
