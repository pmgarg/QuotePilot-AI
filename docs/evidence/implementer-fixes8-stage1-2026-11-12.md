# Implementer evidence — Stage 1 fix round 8 (2026-11-12)

Base: `bf05198` (fix round 7) plus the as-delivered files of independent QA and security re-verification 4.
Written by the implementing side; it is **not** a verification. Product code changed, so an independent QA and security
re-verification 5 and a release review 5 are required before any PASS.

## Why this round exists
- Security re-verification 4 (`security-stage1-reverify4-2026-11-12.md`): the round-7 fix of the shared decimal parser
  was incomplete (**SEC4-0, Medium**: `1 e3`, `1e 3`, `1ee3` were still read as 13) and RFQ quantities had no sane cap
  (**SEC4-6, Medium**: HTTP 500 at ≥ 1e15, values above the API cap copied into quote lines); Lows SEC4-1..5.
- The Stage 3 QA run found a cross-stage defect in Stage 1 code (**D5, Medium**): mutating endpoints answered 2xx
  before the transaction was committed.

## Changes
| ID | Change | Where |
|---|---|---|
| SEC4-0, SEC4-3, SEC4-4, SEC4-5 | `parse_decimal` rewritten as a strict grammar: exactly one number per cell, optionally wrapped in a currency code/symbol or unit; letters inside the number, two digit groups, a stray `e`, exponent notation mixed with text → `None`. U+2212 (and small/full-width forms) is a minus sign; negative zero is never returned. Nothing is assembled from fragments any more (the old code stripped every non-numeric character and parsed what was left). | `apps/api/app/services/money.py` |
| SEC4-1 | floats/Decimals that are non-finite or beyond 10^30 → `None`; a zero with an absurd exponent → `None` | same |
| SEC4-2 | catalog import: list/cost price that does not fit NUMERIC(18,6) is a row error (`… is too large`); an oversized stock value is left unknown with a warning | `apps/api/app/services/catalog_import.py` |
| SEC4-6 | RFQ items: a parsed quantity is stored only when 0 < q ≤ 10,000,000 (three decimals, `ROUND_HALF_UP`); otherwise empty with flag `quantity_out_of_range` / `quantity_not_positive`. Quote generation treats an out-of-range stored quantity as missing (blocker) and never copies it. The AI extraction schema cap is aligned to the same 10,000,000. | `apps/api/app/services/rfqs.py`, `services/quotes.py`, `services/ai/schemas.py` |
| D5 | the DB dependency is function-scoped (`Depends(get_db, scope="function")`): commit/rollback runs when the path operation (and response serialisation) has finished and **before** the response is sent. A failed commit now reaches the caller as an error. | `apps/api/app/api/deps.py` |
| I-3 | limitation text updated: D5 is the probable cause of the three historical pull_request browser failures (matching symptoms, race reproduced locally); stated as an inference, not as proof | `docs/KNOWN_LIMITATIONS.md` |
| docs | API contract: durability statement, quantity flags, import number rules | `docs/API_CONTRACT.md` |

Behaviour a user can notice: cells such as `Rs.500`, `12 per 6`, `1.234.567` or `1 e3` are now reported instead of
being read as some number; `Rs. 500`, `AED 12.50`, `1 234,56`, `(12.50)`, `−5` keep their obvious meaning.

## Tests added (implementer)
- `apps/api/tests/unit/test_pricing.py`: 22 rejected fragments, 13 decorated numbers with sign/value, negative zero / non-finite / magnitude.
- `apps/api/tests/integration/test_round8_fixes.py`: D5 (probe from a second connection at `http.response.start`: row and
  audit row are committed; a 422 commits nothing), SEC4-2 (4 oversized prices), SEC4-6 (4 oversized quantities, cap value kept).
  The D5 test was run against the old dependency scope and failed there (1 failed, 1 passed), then passed with the fix.

## Verifier test edits (declared; to be judged by re-verification 5)
`apps/api/tests/security/test_audit_followups5.py` (security auditor's file):
- strict `xfail` markers removed for the fixed findings SEC4-0 (two tests), SEC4-1, SEC4-3, SEC4-5, SEC4-6b; the shared
  markers `_X2` (SEC4-2) and `_X6` (SEC4-6) are now empty. Assertions unchanged.
- `test_sec4_0_catalog_import_spaced_exponent_price_is_not_stored_as_13`: its upload contained only the two hostile
  rows; with both rejected the import has no valid row and answers 422, so the test's own `201` precondition could not
  hold. One valid row (`Z3,…,5`) was added. The assertion about `Z1` is unchanged.
- **Left as is**: `test_sec4_4_digit_groups_separated_by_words_are_not_concatenated` is still marked strict xfail. It
  cannot pass for a reason inside the test (`Decimal("0x10")` raises `InvalidOperation` while building the expected
  value), not because of the product: all four inputs now return `None` (asserted in `tests/unit/test_pricing.py`).
  The auditor should repair or drop the marker during re-verification 5.
- `ruff format` was applied to the verifier files where CI's format check required it (no assertion changed).
`apps/api/tests/qa/test_qa_reverify_review4.py` (QA's file): not edited.

## Runs
See the table appended below.
