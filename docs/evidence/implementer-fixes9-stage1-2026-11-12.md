# Implementer evidence — Stage 1 fix round 9 (2026-11-12)

Base: `8f9416e` (round 8b + CI change + the as-delivered files of QA and security re-verification 5).
Written by the implementing side; it is **not** a verification. Product code changed again, so independent QA and
security **re-verification 6** and a release review are required before any PASS.

## Inputs
- Security re-verification 5 (`security-stage1-reverify5-2026-11-12.md`): SEC5-5 **High** (pre-existing), SEC5-6
  Medium, SEC5-2 / SEC5-3 / SEC5-7 Low, SEC5-4 Info.
- QA re-verification 5 (`qa-stage1-reverify5-2026-11-12.md`): D5-1 Low (= SEC5-6), D5-2 Low/Info (= SEC5-7).
- The CodeQL code-scanning check on PR #1: identified from the job log of commit af8c902 as
  `py/clear-text-logging-sensitive-data` at `apps/api/app/fixtures/seed.py:171` (python SARIF: 1 result;
  javascript SARIF: 0 results).

## Changes
| ID | Change | Where |
|---|---|---|
| SEC5-5 (High) | RFQ text lines have their whitespace runs collapsed before any pattern is applied; lines longer than 1,000 characters are never searched for a quantity (kept as text if they name a SKU); SKU-like tokens are found run by run in linear time (`sku_tokens`), runs longer than 64 characters are not SKUs. Measured after the fix: 200,000-character hostile lines < 0.01 s (before: 10,000 characters ≈ 2.3 s per line, in the API process). | `apps/api/app/services/parsing/extract.py`, `services/matching.py` |
| SEC5-6 / D5-1 | a thousands group never starts with a zero: `0,500` = 0.500, `0,001` = 0.001 (was 500 and 1); `0,500,000` is rejected | `apps/api/app/services/money.py` |
| SEC5-7 / D5-2 | `..5`, `...5` rejected | same |
| SEC5-3 | every dash/hyphen (Unicode category Pd, plus U+2212 and U+2796) directly before a number is a minus sign | same |
| SEC5-2 | a character with a numeric value that is not a decimal digit (superscripts, circled digits, vulgar fractions) makes the cell not-a-number: `5²`, `10³`, `5①`, `½` → rejected (were 52, 103, 51, …) | same |
| CodeQL alert | the seed no longer prints the demo password and no longer generates one: `DEMO_PASSWORD` (environment or `.env`, ≥ 12 characters) is required, otherwise the seed exits with status 2 and an instruction. README, `.env.example`, runbook, Makefile help and CLAUDE.md say so. | `apps/api/app/fixtures/seed.py` and docs |

Not changed (documented in KNOWN_LIMITATIONS): SEC5-4 (mixed digit scripts in one number are read as one number; `5%`
is 5), and QA's observations (`2k pcs` → 2; a cell with letters around exactly one number is read as that number;
`AED - 5` → −5, which the importer then rejects as negative; the cap is checked before rounding).

## Corrections to earlier statements
- Fix-round-8 evidence said cells such as `1.234.567` are "now reported"; QA showed that this cell was already
  rejected at bf05198. The statement was wrong for that example.
- Fix-round-8b evidence first attributed the CodeQL alert to the polynomial pattern in the parser. The alert is the
  seed line above; it is older than round 8 (the seed has printed the password since the first Stage 1 commit), and the
  code-scanning check was never recorded for earlier commits. The parser slowdown was nevertheless real and was
  confirmed independently by the security re-verification (18.6 s for a 40,000-character cell at 3586da2).

## Verifier test edits (declared; to be judged by re-verification 6)
`apps/api/tests/security/test_audit_followups6.py`: the strict `xfail` markers of SEC5-2, SEC5-3, SEC5-5 (two tests),
SEC5-6 and SEC5-7 were removed (six decorators); no assertion was changed. QA's files were not edited; QA's seven
tests that failed at 6b3c452 (D5-1, D5-2) pass unedited.

## Tests added (implementer)
- `apps/api/tests/unit/test_round9_fixes.py` (38 cases): leading-zero comma decimals; typos and non-decimal digits;
  12 dash code points; linear-time line parsing on 200,000-character inputs; ordinary lines unchanged; `sku_tokens`
  equals a whole-text `SKU_TOKEN.findall` on ordinary text; the seed refuses to run without `DEMO_PASSWORD` and prints
  nothing to stdout.
- `apps/api/tests/integration/test_round9_fixes.py`: seed output lists the accounts but never the password; pasted RFQ
  text with five 20,000-blank lines is answered in < 2 s and the ordinary line in the same text is still read.

## Runs
See the table appended below.
