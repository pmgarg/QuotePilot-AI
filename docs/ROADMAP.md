# Roadmap & Stage Status

Status vocabulary: NOT STARTED / IN PROGRESS / IMPLEMENTED (code + tests exist) / VERIFIED (QA + security evidence +
independent reviewer PASS) / BLOCKED.

| Stage | Status | Gate record | Notes |
|---|---|---|---|
| 1 RFQ-to-Quotation | IMPLEMENTED — release review 5: PASS WITH CONDITIONS (human conditions open); not VERIFIED | RELEASE_GATES.md § Stage 1 | PR #1 |
| 2 Follow-up Assistant | IMPLEMENTED — release review 1: PASS WITH CONDITIONS; not VERIFIED | RELEASE_GATES.md § Stage 2 | PR #2 (`claude/stage2-followup-assistant`) |
| 3 Customer & Order Intelligence | IMPLEMENTED — release review 1: PASS WITH CONDITIONS; not VERIFIED | RELEASE_GATES.md § Stage 3 | PR #3 (`claude/stage3-customer-order-intelligence`) |
| 4 Governed AI Sales Ops | Not on this branch (implemented on `claude/stage4-governed-platform`, gate open) | — | PR #4 |

## Stage 1 milestone plan (vertical slices)
| # | Slice | Exact verification |
|---|---|---|
| 1.1 | Foundation: settings, DB, migrations, RLS roles, auth, orgs, memberships, audit | `make api-test` (tests/test_auth*, test_tenancy*) |
| 1.2 | Catalog import + versions | `pytest -k catalog` |
| 1.3 | RFQ intake + parsers (CSV/XLSX/PDF/text) in subprocess | `pytest -k parser or rfq` |
| 1.4 | Matching + review | `pytest -k match` |
| 1.5 | Quotes: pricing engine, versions, approval, states, overrides | `pytest -k quote or pricing or approval` |
| 1.6 | PDF/CSV export, history, search | `pytest -k export or pdf` |
| 1.7 | Web UI (Next.js) for all screens | `make web-check`, `make e2e` |
| 1.8 | Security tests + scans | `pytest -m security`, `make security-scan` |
| 1.9 | QA evidence, security evidence, release-reviewer invocation, fixes | `docs/evidence/*`, `docs/RELEASE_GATES.md` |

## Stage 2 plan (backlog, executable)
2.1 lifecycle states + manual transitions; 2.2 follow-up policies & contact preferences; 2.3 recommendation rules;
2.4 templates + draft generation; 2.5 job runner (Redis, idempotency, backoff, DLQ); 2.6 provider adapters
(simulator → SMTP → Gmail/Graph); 2.7 inbound sync + timeline; 2.8 pending follow-ups UI; 2.9 security + gate.

## Stage 3 plan
3.1 customer profiles/contacts; 3.2 orders + conversion; 3.3 KPI service + dashboard; 3.4 CSV import/export;
3.5 connector framework + conflict queue; 3.6 stock sources; 3.7 retention/deletion; 3.8 security + gate.

Status: 3.1–3.7 implemented on `claude/stage3-customer-order-intelligence` (API, worker purge job, UI, 13 integration
tests + 4 browser tests); 3.8: independent QA, security audit, two fix rounds, re-verification and release review 1
(PASS WITH CONDITIONS, 2026-11-12) are recorded in RELEASE_GATES.md. Stage 3 is not VERIFIED.

## Stage 4 plan
4.1 policy engine + entitlements; 4.2 tool registry + bounded assistant; 4.3 workflow definitions/runner/dry-run;
4.4 budgets/circuit breakers/kill switch; 4.5 command center UI; 4.6 billing adapter (test mode) + onboarding +
help; 4.7 red-team suite + gate.
