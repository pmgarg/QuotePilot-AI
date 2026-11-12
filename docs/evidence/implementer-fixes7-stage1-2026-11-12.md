# Implementer — Stage 1 fix round 7 (after release review 4)

Base: `287d09e`. Input: `release-review-4-stage1-2026-11-10.md` (verdict INCOMPLETE / BLOCKED on RR4-1 and RR4-2; no
Critical, High or Medium code defect) plus one defect in shared Stage 1 code reported by the Stage 3 security audit
(SEC3-11). Self-reported; product code changed, so independent QA and security re-verification follow.

| Finding | Change | Test |
|---|---|---|
| RR4-1 no CI on the gated HEAD, no counts | CI runs for the round-7 code commit are recorded in `RELEASE_GATES.md` with job conclusions, the pytest summary line and the Playwright summary (incl. flaky count) taken from the job logs | — |
| RR4-2 documentation | Traceability: every Stage 1 row carries the review-4 verdict and the re-verification-3 evidence; S1-A01..A20 split per ID; NFR CI row rewritten. `API_CONTRACT.md` lists `GET /catalogs/active-version`. `KNOWN_LIMITATIONS.md` I-3 text corrected (three failed pull_request runs, not one). Human-action wording corrected (a rejected version returns to draft; no new version is created). | — |
| RR4-3 editor paired rows by index | Saved line looked up by id; re-link sets `is_custom: false` | covered by existing browser tests; QA to add a remove-then-re-link test |
| (new) edits typed during an in-flight save were overwritten by the save response | The line editor is a `<fieldset disabled={busy}>`: inputs are locked while a save is in flight. Found when QA's D2-1 browser test failed under heavy machine load (final save sent a stale price). | `qa-reverify-review3.spec.ts` D2-1 (passes again) |
| R4-2 `check_compose.py` passed vacuously | Fails when `api` or `web` is missing from the rendered topology | `test_r4_2_…` (strict xfail removed — declared) |
| SEC3-11 scientific notation misparsed (`1e3` → 13, `8.5E2` → 8.52) in the shared decimal parser used by the catalog import | `money.parse_decimal` parses a pure exponent form exactly (finite, bounded exponent) and rejects exponent notation mixed with other text | unit `test_parse_decimal_scientific_notation_is_exact` (6), `…rejects_ambiguous_or_non_finite_exponent_forms` (7), `…currency_codes_containing_e_still_parse`; integration `test_import_scientific_notation_prices_are_exact_never_invented` |
| R3-9 / R4-1 platform total in tenant audit rows | `scripts/revoke_sessions.py` writes the per-tenant count only; the total goes to stdout | `test_r4_1_…` (strict xfail removed — declared) |
| RR4-4 substring lookup capped at 50, RR4-5 one compose test skips in CI, R3-8, R4-3, R4-4 | Not changed (Info); listed in the gate log | — |

## Changed product files
`apps/api/app/services/money.py`, `apps/web/src/app/(app)/quotes/[id]/page.tsx`, `scripts/check_compose.py`,
`scripts/revoke_sessions.py`.

## Runs (local PostgreSQL 16, DB `quotepilot_s1`; the machine was shared with four other agents' suites)
| # | Command | cwd | Exit | Result |
|---|---|---|---|---|
| 1 | `ruff check app tests && ruff format --check app tests && mypy app` | apps/api | 0 | clean |
| 2 | `pytest -q -p no:cacheprovider` (first run) | apps/api | 1 | 377 passed, 2 failed, 1 skipped, 1 xfailed — both failures were the compose-check tests reacting to the R4-2 change (one strict xfail now passing; one because `migrate` had been made a required service, reverted to `api`/`web`) |
| 3 | `pytest -q -p no:cacheprovider` (after the compose-check fixes) | apps/api | 0 | **379 passed, 1 skipped, 1 xfailed** (442 s); the skip is the compose-render test that needs a repo-root `.env` (RR4-5); the xfail was R4-1, fixed afterwards — `tests/security/test_audit_followups3.py` + `…4.py` re-run: 33 passed, 1 skipped |
| 4 | `tsc --noEmit && eslint src && npm run build` | apps/web | 0 | clean |
| 5 | `npx playwright test` (first run, under load) | tests/e2e | 1 | 31 passed, 1 failed (D2-1, the save race above), 1 skipped |
| 6 | `npx playwright test` (after the fieldset fix) | tests/e2e | 0 | **32 passed, 1 skipped** |

Note: during this round another agent's test run briefly used the default database `quotepilot_test` by mistake and
truncated it; Stage 1 verification moved to a dedicated database `quotepilot_s1`. No evidence run was affected.
