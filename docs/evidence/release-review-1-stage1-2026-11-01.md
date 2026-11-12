# Independent release review 1 — Stage 1 @3169ad6 (2026-11-01)

Reviewer: fresh-context agent running the read-only role in `.claude/agents/release-reviewer.md` (Read/Glob/Grep plus
read-only git/gh inspection; no tests run, no files written). Recorded by the orchestrator without altering substance.

**Overall verdict: FAIL — Decision: BLOCKED.**

## Evidence observed by the reviewer
- CI on HEAD 3169ad6: `ci` run 41353247333 **failed** at "Lint and type-check" (`ruff format --check` → 4 QA test files
  would be reformatted); migrations, pytest, OpenAPI drift and the e2e job did not run. `security` run 41353247377 green.
- CI on 7a29d68 green (41348193483 / 41348193559) — before the fix round.
- QA (97+6 tests) and security (SEC-01..22) evidence were produced on 70351b7/7a29d68; post-fix pass counts
  (240 pytest, 12 Playwright, 52 security) were reported by the implementer only. The scan evidence header named 7a29d68.

## Findings
**Critical:** none found (which does not mean none exist).

**High**
- H-1 Approved quote document changeable after approval: `build_pdf_model` reads quote-level `valid_until` and live
  org/customer fields; `_check_exportable` looks only at quote-level status; `render_and_store_pdf` overwrites the
  stored approved PDF when the hash differs; `new_version` resets `valid_until`; Sales can set any `valid_until`.
  Repro (by reading): approve v1 → Sales `POST /versions` → `PATCH valid_until=2099-12-31` → `GET /versions/1/pdf`
  returns an APPROVED PDF with the new validity and overwrites the stored artifact. Affects S1-F13/F15/A15/A18, X-03.
- H-2 (gate) CI red on the release candidate; the implementer's "format clean" claim was false.

**Medium**
- M-1 Limiter key `f"{ip}|{email.lower()}"` is built before `strip()` → whitespace variants get fresh budgets.
- M-2 API trusts `X-Forwarded-For` from any client (`--proxy-headers --forwarded-allow-ips "*"`, API port published);
  with `WEB_TRUST_FORWARDED_HEADERS=true` the leftmost client value wins; no automated test of the proxy fix.
- M-3 Blocker message leaks margin % and the confidential floor to Sales/Read-only (cost recoverable by probing).
- M-4 Sales can bypass the margin floor: `{"product_id": null, "is_custom": true, "unit_price": "0.01"}` clears cost.
- M-5 (process) No independent QA/security re-verification on 3169ad6.

**Low / Info**
L-1 users/sessions RLS write policies loose and untested (users WITH CHECK only requires an org context; sessions
WITH CHECK allows inserting for any org-bound user; `create_member` makes a foreign user row visible for the
transaction). L-2 e-mail enumeration via 201/409 on member create. L-3 last-active-owner race (no lock). L-4 totals
overflow (`NUMERIC(18,3)` vs five max lines). L-5 caps truncate instead of rejecting (S1-A08). L-6 parser sandbox has
no FS/network isolation. L-7 no regression tests for SEC-05/SEC-09 web fixes. L-8 doc drift (threat-model test names,
runbook kill switch `scripts/revoke_sessions.py` missing, gate log, traceability rows X-05/07/08/09 and S1-A*, `org.update`
audit lacks before/after). L-9 product lookups rely on RLS only (SEC-20). L-10 weak SECRET_KEY guard.

**Confirmed fixed in code:** D2/SEC-04, D4, D5/SEC-03/O1, D6/SEC-13, D7/SEC-06 (per field), D8, O2, O4, SEC-01,
SEC-02 (policies exist), SEC-08/09/10/11/15/16/17/18/19. D1/D3/SEC-22 apply to the current version only (see H-1).
Implementer edits to QA/security tests were judged legitimate and not weakening; originals were not separately committed.

## Traceability verdicts (reviewer)
PASS: X-02, X-04, X-06, S1-F03..F11, S1-F17, S1-F18, S1-F19, S1-A01, A02, A04..A07, A10..A14, A16, A17, A19.
FAIL: X-03, X-09, S1-F02, S1-F12, S1-F13, S1-F15, S1-F20, S1-A03, S1-A15.
INCOMPLETE: X-01, X-05, X-07, X-08, S1-F01, S1-F14, S1-F16, S1-F21, S1-A08, S1-A09, S1-A18, S1-A20.

## Required retest
1. Format QA tests; full `ci` workflow green on the new HEAD.
2. Fix H-1, M-1..M-4 (or human-signed product decision for M-4) with regression tests (export v1 after new version +
   validity change; whitespace e-mail limiter; role-safe blocker text; custom-line floor; spoofed XFF under Docker CMD;
   browser open-redirect test).
3. QA: independently re-run `tests/qa` + Playwright on the new HEAD. 4. Security: re-run `tests/security`, re-verify
SEC-01/02/05/09 dynamically, add users/sessions RLS tests, assess L-1/L-3. 5. Docs fixes. 6. Fresh release review.
