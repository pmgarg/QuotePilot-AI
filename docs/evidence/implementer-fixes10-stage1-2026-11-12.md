# Implementer evidence — Stage 1 fix round 10 (2026-11-12)

Base: `46c3bcb` (round 9 + harness fix + the as-delivered files of QA and security re-verification 6).
Written by the implementing side; it is **not** a verification. Product code changed again, so an independent
re-verification 7 and a release review are required before any PASS.

## Inputs
- Security re-verification 6 (`security-stage1-reverify6-2026-11-12.md`): SEC6-5 Medium, SEC6-1 Medium (admin only),
  SEC6-2 Medium (pre-existing, design), SEC6-3 / SEC6-4 Low, SEC6-6 Info.
- QA re-verification 6 (`qa-stage1-reverify6-2026-11-12.md`): D6-1 Low (= SEC6-5), D6-2 Low, D6-3 Info.
- CI on 7151e64: `api` job red on five QA tests that needed an environment variable CI does not set (QA corrected its
  own file in the delivery above); CodeQL code-scanning check green with 0 results.

## Changes
| ID | Change | Where |
|---|---|---|
| SEC6-5 / D6-1 | numbers with both separators must be well formed: complete thousands groups plus a decimal part (`1,234.56`, `1.234,56`, `1,00,000.50`); a leading-zero first group (`0,500.00`, `00.500,25`) and anything else (`.5,0`, `12,34.56`) is rejected | `apps/api/app/services/money.py` |
| SEC6-4 | a non-ASCII character whose compatibility form contains a digit (`㋀`, `㎡`) makes the cell not-a-number, in addition to characters with a numeric value | same |
| SEC6-1 | `normalize_sku` strips non-alphanumeric ends with two linear scans instead of a backtracking pattern (400,000-character hostile cell: 0.014 s) | `apps/api/app/services/units.py` |
| SEC6-3 | a negative stock figure is left unknown with the existing "unreadable" warning | `apps/api/app/services/catalog_import.py` |
| SEC6-2 (**mitigated, not closed**) | fuzzy matching compares at most the first 200 characters of a line, and one catalog index spends at most 2,000,000 description comparisons per RFQ parse / re-match; lines beyond the budget are matched by exact SKU only and flagged `match_skipped_budget` for a person. | `apps/api/app/services/matching.py`, `services/rfqs.py` |
| D6-2 | the seed also reads `DEMO_PASSWORD` from the repository-root `.env` (the documented quick start), after the environment and the working-directory `.env` | `apps/api/app/fixtures/seed.py` |
| D6-3 | README quick-start comment no longer says credentials are printed | `README.md` |

### SEC6-2 — what remains open
Measured on this machine with the cap in place: one line against 30,000 products costs 36 ms (short line) to 126 ms
(200 characters); 50,000 products: 67–286 ms. The auditor's test asks for < 20 ms per line at 30,000 products, which a
linear scan with this scorer cannot meet; it stays a strict xfail. The budget bounds one request to 2,000,000
comparisons — by the figures above roughly 2.5–8 s of API-process CPU in the worst case (computed, not measured end to
end), during which the process serves nothing else. Closing it needs a design change (matching in the worker/sandbox
process, or an index that avoids scanning every description). Effect of the budget on legitimate use: with a
5,000-product catalog the first 400 lines of an RFQ are fuzzy-matched, with 20,000 products the first 100; further
lines need an exact SKU or a manual choice. This is recorded as a known limitation and needs a human decision.

Not changed: SEC6-6 (pasted lines are stored with blanks collapsed — judged acceptable by QA); QA's judgement call on
zero-padded groups (`01,234` → 1.234) — both verifiers' tests pin the current reading, so it is kept and documented.

## Verifier test edits (declared)
`apps/api/tests/security/test_audit_followups7.py`: the strict `xfail` decorators of SEC6-1 (two tests), SEC6-3,
SEC6-4 and SEC6-5 were removed (16 lines); no assertion was changed. The SEC6-2 decorator is untouched. QA's files were
not edited by the implementer; QA's seven tests that failed at 7151e64 (D6-1 ×5, D6-2, D6-3) pass unedited.

## Tests added (implementer)
`apps/api/tests/unit/test_round9_fixes.py` (round-10 section): malformed two-separator numbers and digit-expanding
symbols rejected; well-formed grouped numbers incl. lakh grouping keep their value; `normalize_sku` linear and
unchanged for ordinary cells; matching budget (lines beyond it are `fuzzy_skipped`, an exact SKU still matches).
`apps/api/tests/integration/test_round9_fixes.py`: negative stock left unknown with a warning.

## Runs
See the table appended below.
