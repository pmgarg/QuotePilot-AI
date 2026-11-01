# Implementer evidence — Stage 1 fix round 2 (commit ff99762) — reconstructed record

Note: this file was referenced by later evidence but was never committed with ff99762 (the write was skipped by a
failed command chain). It is reconstructed from the commit message, diff and the test modules of ff99762, as the
QA and security re-verification agents also did. The authoritative per-finding verification of that round is in
`qa-stage1-reverify-2026-11-01.md` and `security-stage1-reverify-2026-11-01.md`.

Scope of ff99762 (review 1 findings): H-1 document snapshot + store-once PDF (migration 0003), M-1 limiter
normalisation + per-account bucket, M-2 forwarded-header trust boundary and unpublished API port, M-3 role-aware
blocker text, M-4 `manual_price_unconfirmed` + price-override, L-1 per-command users/sessions policies, L-3 org row
lock, L-4 totals cap, L-5 oversize rejection, L-9 explicit product scoping, L-10 weak-secret guard, X-05 owner AI
consent, X-08 axe scan; tests `tests/integration/test_review1_fixes.py` (12) and `tests/e2e/specs/review1-regressions.spec.ts` (4).
Reported runs at the time: 252 pytest passed, 16 Playwright passed (implementer-run; independently re-run by QA: 252/16).
