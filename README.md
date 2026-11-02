# QuotePilot AI

Multi-tenant SaaS for small B2B distributors: turn emailed RFQs into verified, approved quotations (Stage 1),
then follow up (Stage 2), understand customers and orders (Stage 3) and run a governed AI sales-operations
platform (Stage 4). Stage 1 is implemented; Stages 2–4 are fully designed in `docs/`.

**Status:** see `docs/ROADMAP.md` and `docs/RELEASE_GATES.md`. Only requirements marked VERIFIED in
`docs/REQUIREMENTS_TRACEABILITY.md` are done. No paid service is required or used.

## What Stage 1 does
Login → organisation settings & branding → versioned catalog import (CSV/XLSX, column mapping, validation report)
→ RFQ intake (digital PDF / XLSX / CSV / pasted text, parsed in a sandboxed subprocess) → extracted lines with
categorical confidence (exact / likely / ambiguous / unmatched) and source references → human verification →
deterministic Decimal pricing (discount, tax, per-currency rounding, margin floor) → submit → separation-of-duties
approval → versioned, immutable approved quotes → deterministic branded PDF and formula-safe CSV → searchable
history and a full audit trail. Tenant isolation is enforced in the service layer **and** by PostgreSQL row-level
security.

## Quick start (local, no Docker)
Requires PostgreSQL 16, Python 3.12+, [uv](https://docs.astral.sh/uv/), Node 22.
```bash
cp .env.example .env
make db-up            # start PostgreSQL service + create roles/databases (uses sudo -u postgres when root)
make api-install web-install
make demo             # migrate + seed two synthetic organisations; prints LOCAL-ONLY credentials
make api-run          # http://localhost:8000/api/v1/docs
make web-run          # http://localhost:3000  (in another shell)
```
Demo accounts: `owner|admin|sales|approver|readonly@northwind.example` and `…@contoso.example` with the password
printed by `make demo` (or `DEMO_PASSWORD` from `.env`). Sample RFQs: `tests/fixtures/rfq_clean.*`,
`rfq_ambiguous.*`, plus malicious samples for security testing.

## Quick start (Docker Compose)
```bash
cp .env.example .env
docker compose -f infra/docker-compose.yml up --build
```
> The development container used to build this repository had no Docker daemon, so the compose topology is
> written and reviewed but was not executed there. See `docs/KNOWN_LIMITATIONS.md`.

## Verification
```bash
make api-lint          # ruff + mypy (strict)
make api-test          # pytest: unit + integration (real PostgreSQL, RLS) + security suites
make web-check         # tsc + eslint + next build
make e2e               # Playwright: login → RFQ → review → quote → approval (2nd user) → PDF
make security-scan     # pip-audit, bandit, ruff S*, npm audit, secret grep → docs/evidence/
```
Recorded results live in `docs/evidence/`. CI (`.github/workflows/ci.yml`, `security.yml`) runs the same plus
gitleaks, semgrep and CodeQL. Branch protection must be enabled by a human (see `docs/RELEASE_GATES.md`).

## Repository layout
```
apps/api        FastAPI + SQLAlchemy + Alembic (Python)       apps/web   Next.js 16 + TypeScript + Tailwind
tests/e2e       Playwright                                     tests/fixtures  synthetic & malicious documents
infra/          docker-compose + Dockerfiles                   scripts/   db setup, fixtures, scans
docs/           PRD, HLD/LLD, data model, API contract, ADRs, threat model, security & privacy policy,
                test strategy, traceability, release gates, roadmap, runbook, known limitations, evidence
.claude/agents  project subagents incl. the read-only independent release-reviewer
```

## Security & privacy in one paragraph
Argon2id passwords, server-side revocable sessions, HttpOnly/SameSite cookies, CSRF double-submit, login rate
limiting, role matrix enforced server-side, 404 for cross-tenant IDs, PostgreSQL RLS with a non-owner runtime role,
sandboxed document parsing with size/time/memory limits and zip-bomb checks, formula-injection-safe exports,
private file storage behind authorized endpoints, redacted JSON logs, dependency/secret/static scanning in CI, and an
AI adapter that is disabled by default and whose output is never authoritative. We do not claim certification or
the absence of vulnerabilities; see `docs/THREAT_MODEL.md` and `docs/KNOWN_LIMITATIONS.md`.

## License
Proprietary / all rights reserved by the repository owner until a license is chosen.
