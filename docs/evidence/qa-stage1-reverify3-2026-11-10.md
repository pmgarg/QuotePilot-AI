# QA (independent) re-verification 3 — Stage 1 @f69ac25 (fix rounds 5 and 6)

Date 2026-11-10 (UTC 17:03-17:25). Scope: the "Required retest" list for QA in `release-review-3-stage1-2026-11-02.md`
(D2-1..D2-5, R3-6, R3-10, explicit FORCE-RLS check after the replay, provenance of the committed QA file, R3-4).
I did not implement rounds 5 or 6. No product code was changed; nothing was committed. Tests are derived from PRD
S1-F11/F12/F13/F14/F15/A20, USER_JOURNEYS, API_CONTRACT and the review text, not from the implementation.

## Environment
| item | value |
|---|---|
| commit / branch | `f69ac25` / `claude/multi-stage-project-impl-8hz2wj`; working tree otherwise clean (untracked: my two new test files, plus files from the security agent, which I did not touch) |
| OS / DB | Linux 6.18; local PostgreSQL 16 (service, not Docker); database `quotepilot_qa` (schema dropped and recreated before the run) |
| `DATABASE_URL`, `DATABASE_URL_TEST` | `postgresql+psycopg://quotepilot_app:***@localhost:5432/quotepilot_qa` (non-owner role, RLS enforced) |
| `DATABASE_ADMIN_URL`, `DATABASE_ADMIN_URL_TEST` | `postgresql+psycopg://quotepilot_owner:***@localhost:5432/quotepilot_qa` |
| API tests | `apps/api/.venv` (Python 3.13), `-p no:cacheprovider` |
| Browser | Playwright Chromium, `APP_ENV=test`, `E2E_API_PORT=8091`, `E2E_WEB_PORT=3091`; web app built by me (`npm run build`, exit 0) |

## Runs
| # | Command | cwd | Exit | Result |
|---|---|---|---|---|
| 1 | `alembic upgrade head` (fresh schema) | apps/api | 0 | `0001 -> 0005 -> 0005b`, `current` = `0005b (head)` |
| 2 | `alembic downgrade 0005` ; `alembic current` | apps/api | 0 | `0005` |
| 3 | `alembic upgrade head` ; `alembic current` | apps/api | 0 | `0005b (head)` |
| 4 | `npm run build` | apps/web | 0 | build succeeded |
| 5 | `.venv/bin/pytest -q -p no:cacheprovider` (full suite, before my new file existed; 17:05:06-17:10:31) | apps/api | 0 | **330 passed, 0 failed, 0 skipped** (321.9 s) |
| 6 | `.venv/bin/pytest -q -p no:cacheprovider tests/qa/test_qa_reverify_review3.py` (final) | apps/api | 0 | **17 passed** (33.6 s). First iterations had 3 failures, all test-side: `sessions` is the documented ENABLE-only RLS exception (`security/test_audit_followups.py:248`); a reject returns the same version to draft so `new_version` is not allowed; the approved doc is v1. Fixed and re-run. |
| 7 | `pytest ... tests/qa/test_qa_reverify_review2.py tests/qa/test_qa_reverify_review3.py tests/security/test_audit_followups.py` (replay tests followed by the FORCE-RLS assertions) | apps/api | 0 | **72 passed** (111 s) |
| 8 | `npx playwright test --reporter=list` (full suite incl. my 4 new tests; 17:13:07-17:15:22) | tests/e2e | 0 | **32 passed, 1 skipped** (`screenshots.spec.ts`, self-skipped by design), 0 failed (2.2 min) |
| 9 | `ruff check` / `ruff format --check` on my new Python file | apps/api | 0 | clean (`mypy` is gated on `app` only; I did not annotate test code) |

Run 5 does not include the 17 tests of run 6; the combined API total on this HEAD is therefore 330 + 17 = 347 with no failures (run 5's 330 and run 6's 17 were executed separately, not as one pytest invocation).
Evidence level: runs 1-3, 5-7 are integration (API + PostgreSQL + RLS through the ASGI TestClient or alembic); run 8 is browser evidence
(Chromium, real uvicorn + Next.js). Unit tests are not used as evidence for any browser claim below.

## Per-item verdicts
| item | verdict | evidence |
|---|---|---|
| D2-1 (save relabels catalog lines manual) | **CLOSED** (API and browser) | API: price formatting variants `0.85/0.850000/0.8500/0.85000000` keep `catalog`; price key omitted + quantity + discount edit keep `catalog` with exact Decimal totals (31.88/19.66/412.78); a changed price becomes `manual` + `manual_price_unconfirmed`; retyping the catalog price into a manual line does **not** launder it back to `catalog`; `unit_price: null` restores `catalog`; a confirmed manual price is voided when the price changes again; a custom line with the catalog price typed in is `manual`. Browser: `qa-reverify-review3.spec.ts` D2-1 test (edit price -> badge + blockers + submit disabled; re-save and retype keep manual; clearing the field restores catalog and enables submit) and the predecessor's review-2 spec (green, run 8). |
| D2-2 (unpinned draft unsaveable after activation) | **CLOSED** | `test_d2_2_unpinned_draft_saves_after_activation_with_either_editor_shape_and_reports_a_blocker`: both the full re-send (product_id + price) and the slim shape save with 200, keep product and price 0.85, and report exactly `catalog_version_mismatch`; submit is 409. A *new* line linked to an archived-version product is 422 and atomic; a foreign-tenant product is 404. |
| D2-3 (migration data fix updated zero rows) | **CLOSED** | Replays (base->head->0005->head in runs 1-3, and `_replay_migration_0005` in the QA tests): legacy shapes in **two tenants** are repaired; a later legitimately approved version and a never-withdrawn approval are untouched; a second replay is a no-op; exports for repaired versions are 409. Explicit FORCE-RLS assertion after the replay (R3-10 request): every table with `organization_id` is ENABLE+FORCE except `sessions` (ENABLE only, documented); the owner sees 0 customers with no tenant context and exactly one org with one. |
| D2-4 (no Withdraw control while draft) | **CLOSED** (browser) | review-2 spec "approver can withdraw v1 while a newer draft is open" green; my D2-4/D2-5 spec withdraws from the UI, and v1 PDF/CSV are 409 afterwards while the v2 draft can still be submitted. |
| D2-5 (stale `withdrawn_reason`) | **CLOSED** (API and browser) | API: reason set on withdraw-while-draft, `null` after v2 is approved (statuses `withdrawn, approved`), v1 409 / v2 200 for four roles, a later withdrawal sets a fresh reason, a replacement after withdraw-while-pending clears it. Browser: after the approver approves v2 the API payload has `withdrawn_reason == null` and the banner no longer contains the reason. |
| R3-6 (Use active catalog) | **VERIFIED as claimed** (API and browser), with the limits below | API: `GET /catalogs/active-version` is auth-gated, tenant-scoped, `null` without a catalog, not moved by an un-activated import, readable by all four roles, returns only `{version_id}`. Re-link with the price key omitted gives `catalog` at the new price (0.91), clears the blocker and the quote can be submitted. Browser: a stale line shows "Use active catalog"; line 1 re-links and the server re-prices (0.91) on save, the button disappears; line 2's SKU absent from the new catalog yields "SKU-1003 is not in the active catalog" and changes nothing; removing the line unblocks submit. See O3-1 for the stale-price re-link. |
| R3-10 banner | **VERIFIED** (browser) | `withdrawn-documents` banner appears on the approver's and on Sales's page while v2 is a draft, names v1 and the reason; the reason `<img src=x onerror=...>` is rendered as text (no `img` element, `window.__qa_xss` undefined); after v2 is approved the banner still names v1 but not the reason. |
| R3-10 stale pending approval | **VERIFIED** (API and browser) | API: a pending ad-hoc quote whose catalog moved on is refused with 409 `catalog_version_mismatch` for approver, owner and admin, and stays `pending_approval`; reject returns the draft, which is still blocked; re-link, re-submit and approve gives 200 at 0.91 and all four roles can export v1. Browser: approve click shows the blocker panel, the version remains pending; reject -> draft; re-link flow as above. |
| R3-10 FORCE-RLS after replay | **ADDED and green** | `test_r3_10_force_rls_is_on_for_every_tenant_table_after_the_replay_and_policies_still_isolate`. |
| R3-4 provenance of `test_qa_reverify_review2.py` | **CONFIRMED with a limit** | `git diff 597f4b0 HEAD` of the file is exactly the declared edit (docstring + `["downgrade", "-1"]` -> `["downgrade", "0005"]`, 3 insertions/1 deletion). The file has 16 test functions, as the review-2 evidence says (16 collected, 11 pass + 5 expected failures at the time), and contains no `skip`/`xfail`. All 16 pass now, including the five former defect gates. **Limit:** the file's first commit is the implementer's `597f4b0`, so git cannot show my predecessor's original bytes; the comparison is against that commit and the evidence text, not against a QA-signed copy. I found no weakened assertion on reading the replay and D2 tests. |

## Defects
None found at Critical, High, Medium or Low severity in this round. All new tests pass; no failing test was left in place.

## Observations (no failing test)
- **O3-1 (Info).** A client that re-links a stale line to the active product but still sends the old price (`unit_price: "0.85"`) gets a `manual` line at 0.85 with `manual_price_unconfirmed` (server: `quotes.py` link_changed reprices to 0.91, then 0.85 differs, so it becomes manual). It is never `catalog` at the archived price. The browser omits the price key after "Use active catalog", so the UI is unaffected. Test accepts either safe outcome: `test_r3_6_relink_that_also_resends_the_stale_price_never_yields_a_wrong_catalog_price`.
- **O3-2 (Info).** For a stale line whose SKU left the new catalog the only UI route is removal (or an API custom line needing owner/admin confirmation); this is the documented limit (R3-6 remainder).
- **O3-3 (Info).** `sessions` has RLS enabled but not forced by design; any "FORCE on every organization_id table" assertion must exempt it (done in my test; matches the existing security test).
- **O3-4 (Info).** One single-test invocation (`-k pending_stale`, run during development, after a failed full-file run) errored at fixture setup with `users_email_key` duplicate on `owner@alphatrading.example`. It did not reproduce in four later runs of the same file or in the full suite. I did not investigate further; noting it as a possible test-isolation flake (per-test `TRUNCATE` runs in an autouse fixture).

## Files created
- `apps/api/tests/qa/test_qa_reverify_review3.py` (17 tests: D2-1 matrix x7 incl. 4 parametrized, D2-2, R3-6 x3, R3-10 stale approval, active-version endpoint, D2-5 x2, FORCE-RLS, D2-3 multi-tenant replay)
- `tests/e2e/specs/qa-reverify-review3.spec.ts` (4 browser tests)
- this report

## Not tested
- Docker / `docker compose up`: no daemon; `scripts/check_compose.py` / R3-3 not run by me (security scope).
- CI runs on GitHub (R3-2), branch protection, I-3 flake root cause: not queried or reproduced.
- Pre-0005b real databases: only simulated legacy shapes via owner-role SQL.
- `stale_catalog_lines` cross-tenant (R3-7 explicit `organization_id` filter): cannot be exercised without forging a cross-tenant line under RLS; covered only by RLS and code reading.
- X-08 manual assistive-technology audit; responsive layouts; browsers other than Chromium; `make api-lint` (ruff/mypy over `app`) not re-run by me.
- Scanners and dynamic security re-verification (NEW-13..NEW-17, I-1): security agent's scope.
- No result here is a statement that the system is secure.

## Cleanup
Playwright's web servers were stopped by the runner; `curl` to ports 8091 and 3091 refused, and no uvicorn/next process remained.
