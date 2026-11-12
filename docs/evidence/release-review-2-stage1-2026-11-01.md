# Independent release review 2 — Stage 1 @60adb00 (2026-11-01)

Reviewer: fresh-context agent with the read-only role (Read/Glob/Grep + read-only git/gh). No tests run, no files written.
Recorded by the orchestrator without altering substance.

**Overall verdict: FAIL — Decision: BLOCKED** (two new Medium defects; all review-1 items verified fixed; CI green).

## Evidence observed by the reviewer
- CI on 60adb00: `ci` 41360270209 success (api: ruff/format/mypy clean, migrations 0001→0004 up/down/up, 293 passed,
  coverage 89 %, OpenAPI drift clean; web: pass; e2e: 21 passed, 1 skipped); `security` 41360270288 success (pip-audit,
  npm audit, bandit, ruff-S, semgrep, gitleaks, CodeQL python + js/ts).
- Independent dynamic QA/security evidence was produced on ff99762; their regression tests now pass in CI on 60adb00.
- Every review-1 finding (H-1, H-2, M-1..M-4, L-1, L-3, L-4, L-5, L-9, L-10), NEW-3/4/5/6/7/10, R-3/R-4/R-5 and O-R4
  were verified fixed in code; implementer test edits judged legitimate (nothing weakened).

## Findings
- **N2-1 Medium** (S1-F11/F12/A11): in the quote editor, re-linking a line to (A) a product in another currency kept
  the old `catalog` price with no blocker (cost cleared → no margin check, no manual confirmation); (B) products from
  imported-but-inactive or archived catalog versions could be linked and priced as `catalog`.
- **N2-2 Medium** (S1-F15/A15): withdrawal marked only the current version; after v1→v2 approved→withdraw→v3, v1
  exported again as APPROVED; withdrawing while a draft is current was impossible (409).
- L2-1 NEW-7 redirect variants untested; L2-2 API container received owner credentials in compose; L2-3 blocker reveals
  cost/(1−floor) ratio (document); L2-4 `sessions_update` WITH CHECK still permissive (grants only); L2-5 no data migration
  for legacy withdrawn approvals; L2-6 documentation drift (traceability counts, version statuses, compose comment,
  scan header, missing fixes2 evidence file, test docstring); L2-7 documented Lows not human-signed.

## Traceability verdicts
FAIL: S1-F11, S1-F12, S1-F15, S1-A15. INCOMPLETE: X-08 (no responsive/screen-reader evidence), X-09 (Docker never run),
S1-F21 (adapter not wired, by design). All other X-*, S1-F*, S1-A* rows: PASS.

## Required retest
Implementer: fix N2-1 (both variants) and N2-2 (multi-version + withdraw-while-draft) with tests; add NEW-7 variants;
remove `DATABASE_ADMIN_URL` from the compose api service; update traceability/docs. QA: reproduce N2-1/N2-2 and
re-run suites on the new HEAD. Security: re-verify NEW-3/5/7/8 dynamically; assess L2-2/L2-4; build the image where a
daemon exists. CI green on the new HEAD. Human owner: acknowledge documented Lows and enable branch protection.
Then release review 3.
