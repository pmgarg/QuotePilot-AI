# QuotePilot AI — project instructions for Claude Code

QuotePilot AI is a multi-tenant SaaS for small/medium B2B distributors: RFQ → verified quotation (Stage 1),
sales follow-up (Stage 2), customer & order intelligence (Stage 3), governed AI sales-operations platform (Stage 4).
`QuotePilot_Claude_Code_Master_Prompt.md` is the authoritative specification. `docs/` is the living design.

## Non-negotiable rules
- **No paid resources.** Never create accounts, provision cloud services, or call paid APIs without explicit human approval.
  Hosted LLM / email / SMS / S3 adapters are optional and disabled unless configured via environment variables.
- **Synthetic data only.** Fixtures in `apps/api/app/fixtures/` and `tests/fixtures/` are synthetic. Never add real customer data.
- **Never invent prices, stock or customer facts.** Missing data blocks finalization or requires a privileged, audited override.
- **Server-enforced authorization.** Every record access is scoped by `organization_id` in the service layer AND by PostgreSQL RLS.
  High-impact actions (approve, override, export, delete) are gated by role checks on the server, never the UI.
- **AI output is untrusted proposed data.** It is schema-validated and never authoritative for pricing, permissions or actions.
- **Secrets never enter the repo, prompts, logs or browser bundles.** `.env.example` holds placeholders only.
- **Honest status.** Only `VERIFIED` in `docs/REQUIREMENTS_TRACEABILITY.md` counts as done. Never report a test as passing
  without a recorded command, exit code and counts. Never state the system is "100% secure".

## Repository layout
```
apps/api/        FastAPI + SQLAlchemy + Alembic (Python 3.12+, managed with uv)
apps/web/        Next.js 16 + TypeScript + Tailwind (App Router, src/)
infra/           docker-compose, Dockerfiles, deployment notes
tests/           e2e (Playwright) and security datasets; unit/integration tests live in apps/api/tests
scripts/         dev/demo/scan helpers
docs/            design, security, traceability and release documents (living)
.claude/agents/  project subagents (release-reviewer is READ-ONLY)
```

## Commands (exact)
```bash
make db-up          # start local PostgreSQL 16 (service or docker) and create roles/databases
make api-install    # uv sync --all-extras (apps/api)
make api-test       # cd apps/api && uv run pytest -q           (unit + integration; needs DATABASE_URL)
make api-lint       # ruff check + ruff format --check + mypy
make web-install    # cd apps/web && npm ci
make web-check      # tsc --noEmit + eslint + next build
make e2e            # Playwright: boots api+web against seeded demo DB, runs tests/e2e
make security-scan  # pip-audit, npm audit, bandit, gitleaks/semgrep when available; writes docs/evidence/
make demo           # migrate + seed synthetic demo org(s) and print local-only credentials
```
Environment variables are documented in `.env.example`. Tests use `DATABASE_URL_TEST` (default
`postgresql+psycopg://quotepilot_app:quotepilot_app@localhost:5432/quotepilot_test`) and `DATABASE_ADMIN_URL_TEST`
for migrations (owner role). RLS is only enforced when the app connects as the non-owner role.

## Agent organization (see `.claude/agents/`)
| Role | Agent file | May edit code? |
|---|---|---|
| Lead / Orchestrator | (main session) | coordinates, records findings, cannot self-certify |
| Product / UX | `product-designer.md` | docs only |
| Architect | `architect.md` | docs/ADRs only |
| Implementer | `implementer.md` | yes — never sole reviewer of own work |
| QA / Test Engineer | `qa-engineer.md` | tests + evidence only |
| Security Auditor | `security-auditor.md` | security tests/evidence only |
| **Independent Release Reviewer** | `release-reviewer.md` | **NO** (Read/Glob/Grep only) |

Release process: implementer finishes → QA runs suites and records evidence under `docs/evidence/` → security auditor
runs scans/adversarial tests and records evidence → orchestrator **invokes `release-reviewer`** with a fresh context →
findings recorded verbatim in `docs/RELEASE_GATES.md` → defects assigned back → retest → reviewer re-invoked.
If subagent invocation is unavailable, the gate is marked INCOMPLETE, never PASS.

## Coding conventions
- Python: ruff (line length 100), mypy strict, Pydantic v2 schemas, services in `app/services/`, routers in `app/api/`.
  Money is `Decimal` quantized per currency minor units with `ROUND_HALF_UP`. No float money anywhere.
- All DB access goes through `app/db/session.py::tenant_session()` which sets `app.current_org` for RLS.
- Return 404 (not 403) for records outside the caller's tenant to avoid existence oracles; 403 for role denials inside tenant.
- Document parsers: enforce size, page/row caps and wall-clock timeouts; run in a subprocess; never evaluate formulas or scripts.
- CSV/XLSX export: neutralize cells beginning with `= + - @ \t \r` (formula injection).
- Frontend: client components call same-origin `/api/*` (rewritten to the API). No provider keys in the browser bundle.
- Every mutating endpoint writes an `audit_events` row within the same transaction.
- Tests: pytest markers `integration` (needs PostgreSQL) and `security`. E2E in `tests/e2e` with Playwright.

## Continuing from a fresh session
Read `docs/ROADMAP.md` (stage status), `docs/RELEASE_GATES.md` (latest gate verdicts), `docs/KNOWN_LIMITATIONS.md`,
and `docs/REQUIREMENTS_TRACEABILITY.md`. Evidence for every claim lives in `docs/evidence/`.
