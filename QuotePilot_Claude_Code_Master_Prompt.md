# CLAUDE CODE CLOUD — MASTER IMPLEMENTATION PROMPT

## Mission and operating rules

You are the lead engineer and delivery orchestrator for **QuotePilot AI** (working name), a commercially viable, security-conscious, multi-tenant SaaS product for small and medium B2B distributors and trading companies. Its long-term scope has FOUR connected stages:

1. **AI RFQ-to-Quotation Assistant** — extract customer requests, match seller catalog, prepare approved quotes.
2. **Sales Follow-up Assistant** — track quotes, draft appropriate follow-ups and run authorized communication workflows.
3. **Customer and Order Intelligence** — CRM-like histories, order insights, repeat orders, reports and integrations.
4. **AI Sales Operations Platform** — unify the lifecycle with bounded AI assistance, configurable workflows, approvals and operational analytics.

Act as a serious product engineering team, not as a one-off prototype generator. Design all four stages coherently; implement in vertical slices, starting with a deployable and tested Stage 1, then Stage 2, Stage 3 and Stage 4 in that order as execution capacity permits. **Do not claim any stage is complete unless its acceptance tests and independent release review pass.** If capacity is exhausted, leave a useful tested deliverable, an honest stage-status report, and a precise backlog, not pretend completion. Do not spend tokens on redundant planning or invent test results.

Primary constraints:
- Minimal cash outlay; target open-source dependencies and local/Docker demo first. Any paid model, SMS, email, CRM or deployment service must be optional and explicitly configured; NEVER create accounts, incur charges or provision paid resources without my approval.
- Use only synthetic demo data until a real customer explicitly authorizes processing; never upload confidential business data to a model without documented user consent and data-handling settings.
- All high-impact business actions (sending messages, creating orders, changing customer prices, exporting sensitive data, deleting records) require server-enforced authorization and appropriate human approval.
- Prefer maintainable, modular architecture, accessible UI, deterministic business rules, measurable quality and commercial usability. Do not invent prices, stock or customer information.
- The initial target customer is a distributor with 1–20 sales staff using spreadsheets, emailed RFQs and a manual quote workflow. Assume international usage and multi-currency needs, but launch with a narrow, configurable product scope.

## Required agent organization — mandatory

Establish project instructions in `CLAUDE.md` and suitable project-level subagents in `.claude/agents/`. Prefer supported Claude Code delegation features. If the environment cannot invoke subagents, implement the same roles as separately documented review passes, explicitly reporting that independent invocation was unavailable rather than claiming it happened.

Roles:

- **Lead / Orchestrator:** maintains roadmap, requirements traceability, risk register, milestone reports, budget-conscious task decomposition and final delivery summary. Coordinates but cannot self-certify acceptance.
- **Product / UX Designer:** writes personas, user journeys, UX specifications, pricing assumptions and acceptance criteria. Supplies clear error, loading and approval states.
- **Architect:** decides technology stack and records alternatives, boundaries, storage, API designs and security decisions in ADRs.
- **Implementer(s):** develop APIs, UI, persistence, workers, integrations, fixtures and migrations. Cannot be the sole reviewer of their own changes.
- **QA/Test Engineer:** independently derives tests from the requirements and acceptance criteria; executes unit, integration, E2E and negative tests; publishes reproducible commands and results.
- **Security Auditor:** performs threat modeling and authorized local security testing, dependency scans, secret scans, LLM prompt-injection tests, tenant isolation tests and privacy assessments; documents residual risks.
- **Independent Release Reviewer (mandatory):** create `.claude/agents/release-reviewer.md` with `name: release-reviewer`, a description instructing proactive use after each milestone, and **read-only tools** (e.g. `Read, Glob, Grep`). Give it a separate, fresh review context. It must read the actual source and relevant design documents, check QA/CI outputs, review security evidence, and report PASS / FAIL / INCOMPLETE per requirement. It MUST NOT modify implementation or write tests to make a failing release appear passing; use the orchestrator to record its findings faithfully. It must challenge missing evidence and request independent reruns from QA/security as required. The same agent must not implement and approve a given feature.

The orchestrator must explicitly INVOKE the release-reviewer for every stage and after any security-critical change. Invocation is not satisfied by merely creating its file. If an invocation is unavailable, mark the release gate INCOMPLETE. If the reviewer finds defects, assign fixes to the implementer, ask QA/security to retest, then invoke the independent reviewer again. No green status based on assertion alone.

Create a review template with:
`requirement_id | design_reference | code_reference | test_id | test_result/evidence | security_check | reviewer_verdict | open_issue`.
A stage passes only when: all critical requirements are implemented and verified; tests and CI are genuinely green; zero known unresolved Critical or High security vulnerabilities (or formal documented risk acceptance by a human owner, never AI auto-acceptance); no unauthorized cross-tenant access; and acceptance demos work. Document Medium/Low risks and limitations. Never state the system is "100% secure" or "has no vulnerabilities."

## Architecture and technology

First write a short technical decision record and choose a stack suitable for a small team. Preferred baseline unless a documented reason changes it:
- Frontend: Next.js + TypeScript + Tailwind CSS, accessible responsive UI; polished dashboards and quote editor.
- Backend: Python FastAPI, Pydantic schemas, SQLAlchemy, Alembic.
- Data: PostgreSQL, with organization/tenant scoping; enforce authorization server-side for every record and preferably defense-in-depth PostgreSQL RLS where appropriate. Add indexes, constraints and transaction safety.
- Background tasks: Redis-backed worker only when needed for import, OCR, message polling or retries; no unnecessary microservices.
- Files: private development storage abstraction with production-compatible S3-style object storage; do not expose customer files publicly.
- AI: modular provider adapter for optional hosted LLM and/or local model. Deterministic parsing/matching fallback must work without a paid AI key. No client-side model-provider keys. Structure and validate LLM output. AI-generated content is untrusted proposed data, not authority.
- Parsing: digitally readable PDFs, XLSX, CSV and pasted text first; scanned PDFs flagged as unsupported or routed to an explicitly optional OCR component.
- Quotations: deterministic currency-safe `Decimal` arithmetic, configurable rules, verified unit handling, totals/taxes/discounts clearly attributed; server-rendered PDF export.
- Auth: select well-maintained auth/OIDC components; no homemade password crypto; server-side tenant membership/RBAC; secure session cookies and CSRF protection as appropriate. Roles Owner, Admin, Sales, Reviewer/Approver, Read-only with least privilege. Keep secrets out of repo, prompts, logs and browser bundles.
- Deployment: Docker Compose for local demonstration and documented production topology with TLS, backups, monitoring, migrations and separate environments.
- Quality: pytest, TypeScript type checking, ESLint, Playwright E2E, API contract tests, GitHub Actions CI, dependency/secret/static scans. Never fabricate successful runs.

Create these documents before significant implementation, then maintain them as living documents:

```
docs/PRODUCT_VISION.md
docs/PRD.md
docs/MARKET_VALIDATION.md
docs/USER_JOURNEYS.md
docs/HLD.md
docs/LLD.md
docs/DATA_MODEL.md
docs/API_CONTRACT.md
docs/ARCHITECTURE_DECISIONS.md
docs/THREAT_MODEL.md
docs/SECURITY_POLICY.md
docs/PRIVACY_AND_RETENTION.md
docs/TEST_STRATEGY.md
docs/REQUIREMENTS_TRACEABILITY.md
docs/RELEASE_GATES.md
docs/ROADMAP.md
docs/OPERATIONS_RUNBOOK.md
docs/KNOWN_LIMITATIONS.md
```

Provide Mermaid diagrams: system/context, modules, per-stage sequence flows, tenant-aware data flow and trust boundaries, auth/approval flow, workflow state machines, data ERD and deployment. Provide OpenAPI schema and sample API requests/responses. Write down failure modes, timeouts, retries, idempotency, and audit events.

Model entities at least: organizations, users, memberships, roles, customer accounts/contacts, catalogs and catalog versions, products/prices, uploaded files, RFQs, requested items, match candidates, quotes/quote lines/versions, approval decisions, communication drafts, follow-up schedules and events, orders and order events, integration connections, workflow runs/steps, audit events, AI usage and billing-plan entitlements. Keep data minimization and retention controls in design. Avoid adding unused tables prematurely to Stage 1.

## Stage 1 — RFQ to verified quotation (implement FIRST)

User journey:
1. A tenant user logs in, configures its organization and uploads a CSV/XLSX price catalog with mapped columns for SKU, description, unit, currency, cost/list price, optional stock and validity.
2. The user uploads a digitally readable RFQ PDF/XLSX/CSV or pastes text. Safely parse and capture source references; present extracted item list with confidence/uncertainty indicators (not misleading numerical confidence unless calibrated).
3. Normalize units and product identifiers; exact SKU matching first, then carefully ranked description/fuzzy matches. Show candidates, source values, reasons for suggestions, and explicitly distinguish unmatched, ambiguous and exact matches.
4. A user verifies item matches, quantities, currency and prices. Generate draft quote using deterministic price/margin/discount/tax and rounding rules. If information is missing, block finalization or require explicit privileged override with audit reason; never hallucinate missing prices.
5. An authorized approver approves a versioned quote. Export branded PDF and optionally CSV. Show status and history. No email sending in Stage 1.

Minimum features: organizations/roles, auth, catalog import/versioning, RFQ uploads and parsing, candidate matching, human review/approval workflow, quotation edit/versioning, PDF export, searchable history, branded settings, synthetic demo fixtures, clear states and responsive UI.

Acceptance tests (expand them): exact match; ambiguous match; unknown SKU; duplicate line; unsupported file; corrupted document; spreadsheet formula injection on import/export; excessive sizes; invalid/negative quantities; mismatched units; currencies and decimals; rounding/tax/discount consistency; forbidden user approvals; unauthorized other-tenant quote read/download/update; expired/withdrawn quote; concurrent update conflict; failure recovery; deterministic generated PDFs and audit history. Include real end-to-end browser tests of upload → review → approval → PDF.

## Stage 2 — Sales follow-up assistant

Build upon Stage 1 (not a disconnected chatbot):
- Track quote lifecycle: draft, approval pending, approved, sent, viewed/acknowledged (only where genuinely known), awaiting customer, won/lost, expired.
- Configure follow-up timing and opt-out; recommend follow-up actions based on explicitly available facts and customer preferences. Draft editable personalized emails with accurate quote references and opt-out controls. Never falsely claim to have sent an email or received a reply.
- Inbound communication integrations (Gmail/Microsoft Graph, when configured) must use minimally scoped OAuth and encrypted token storage. For early demos, use a simulator/fake provider with synthetic messages; document OAuth setup without using real credentials.
- Outbound sending requires an explicit authorized user action or separately enabled, consented workflow approved by an organization admin, with rate limits, recipient validation and tenant-scoped audit logs. Default to draft-only.
- Background job retries with capped exponential backoff, idempotency keys, dead-letter/error views, no duplicate sends, provider outages and revocation handling.
- A clear sales timeline, customer communication preferences, templates, workflow configuration and explanation of recommendations.

Acceptance tests: no sending without approval; unsubscribed/opted-out contact never receives an automated message; duplicate triggers do not duplicate sends; no cross-tenant contact access; invalid/expired OAuth token handled safely; provider 429/5xx and timeout recovery; user can see and cancel pending follow-ups; logs redact sensitive content.

## Stage 3 — Customer and order intelligence

- Customer/contact profiles, quote-to-order conversion tracking, repeat-order assistant, quotation analytics and simple sales pipeline visibility.
- Dashboard for quote volume, conversion, pending value, response/processing time and repeat orders. Metrics must have clear definitions and must not pretend untracked events occurred.
- CSV import/export and optional connector adapters (ERP/inventory/CRM) with clearly specified permissions. Sync safely with idempotency, conflict handling and human-review flows; do not overwrite ERP master data silently.
- Inventory/stock claims only from explicitly connected, timestamped sources; label unknown, stale or incomplete stock data. Suggest repeat orders and cross-sells only from available catalog/customer history; obtain approval for any order creation or external updates.
- Tenant-aware reporting and role-aware access to sensitive costs, margins and contacts; retention/deletion workflows.

Acceptance tests: KPI accuracy against fixed fixtures, permissioned cost/margin fields, multi-currency aggregation rules, synchronization conflicts, stale inventory handling, unauthorized export prevention, recovery from partial import, repeat-order audit trail and cross-tenant leak regression tests.

## Stage 4 — Governed AI sales operations platform

- Unified command center for RFQs, quote approvals, customer communication drafts, order preparation, reporting and configurable organization workflows.
- Add a bounded AI assistant that can search only tenant-authorized business records, propose next actions, explain recommendations with evidence and invoke narrowly scoped tools, never unrestricted arbitrary SQL, shell or HTTP.
- Tool access determined by backend policy and acting user's entitlements, NOT by prompt content or model outputs. Explicit action scopes and an approval checkpoint for sending, order/price changes, deletion, bulk export or other sensitive actions.
- Enforce budgets, token/call limits, rate limits, timeouts, maximum workflow depth, retry caps, circuit breakers, rollback/compensation where feasible, audit records and emergency kill switches.
- Configurable workflows must be validated, versioned and tested; support dry-run and human escalation. Provide workflow history and failure explanations.
- Package a coherent commercial experience: subscription-plan feature limits (test-mode billing adapter first; no live payments without approval), onboarding, demo workspace, help pages, support/export flows, operational metrics, and tenant administration.

Acceptance tests: prompt injection embedded in RFQ or email cannot cause unauthorized tool use; one tenant's model context and retrieval cannot see another's data; unapproved messages/orders never execute; adversarial and malformed tool output rejected; abusive loops and oversized requests are stopped; billing/limits enforced by backend; kill switch terminates risky workflows; error logs and model traces contain no secrets or raw confidential content by default.

## Security — design requirement, not a final cosmetic scan

Use OWASP ASVS 5.0 and the OWASP Top 10 for LLM Applications (2025) / LLMSVS as checklists where relevant. Perform STRIDE-style threat modeling across browser, auth, API, background workers, document parsers, DB, object storage, external connectors, LLM provider and tenant boundaries.

Include tests and controls for:
- Broken authentication, authorization, horizontal/vertical privilege escalation, IDOR/BOLA, tenant isolation, insecure direct file access and object-store ACLs.
- XSS, SQL injection, CSRF, SSRF, command/template injection, unsafe deserialization, CSV formula injection, malicious PDF/XLSX processing, zip bombs and unsafe file types. Restrict file sizes, parsing time, content types and outbound network; never execute document contents.
- Secrets handling and scanning, scoped OAuth permissions, secure session/token storage, environment isolation, least-privilege service accounts, encrypted transport, encryption at rest as deployment permits, backup security, deletion and audit logs. No private data in public storage, error pages or telemetry.
- LLM-specific indirect prompt injection, confidential-data exfiltration, system-prompt extraction attempts, misleading source evidence, output schema validation, overly powerful tools, retrieval contamination, runaway tokens and unbounded API costs. Treat all uploaded documents, emails, tool responses and LLM outputs as untrusted data.
- Resilience: race conditions, replay, duplicate jobs, retries, partial failures, resource exhaustion, rate limits, signed URL expiry, backup/restore rehearsal and incident response.

Set up automated CI with formatting/lint/typecheck, pytest, migration test, API and end-to-end tests, dependency vulnerability checks for Python/JS, secret scanner, static security scanning, and an authorized local dynamic scan if compatible. Create a reproducible security test dataset including malicious RFQs, malformed spreadsheets, hidden document prompts, cross-tenant requests and synthetic secrets. Never run intrusive scans on third-party or production targets without authorization. Report tool availability and actual scan results, not assumptions. Make CI checks and independent reviewer a required merge condition in documentation; explain any repository-settings action that a human must enable.

Document a data retention/deletion policy, privacy notice assumptions, consent requirements, breach-response draft, subprocessor/data flow inventory, and target-market compliance questions needing professional review. Do not claim SOC 2, ISO 27001, GDPR/UAE compliance certification, or a guarantee of no data leaks.

## Delivery discipline, proof, and budget

1. Inspect the repository and environment. Create `CLAUDE.md` and project subagents. Keep an initial stage plan with priorities and exact test commands.
2. Deliver *complete design for all four stages* up front in the listed documentation, with requirement IDs and explicit dependencies; keep docs concise and actionable, not speculative boilerplate.
3. Implement a complete Stage 1 vertical slice first, from login and catalog upload through RFQ review and approved PDF. Make it runnable without a paid API using synthetic demo data. Test it in a real browser where tools permit.
4. Trigger QA tests, security audit and **independent release-reviewer**. Fix defects and rerun until stage gate is genuinely satisfied, or record blocked status. Do not bypass failing gates.
5. Implement Stages 2, 3, then 4 incrementally, repeating the same evidence-based release cycle for each. If the available session/credit cannot cover all four, prioritize shipping Stage 1 cleanly; leave complete architectural designs and executable issue checklists for remaining stages.
6. Maintain `docs/REQUIREMENTS_TRACEABILITY.md` mapped to actual tests and code; update the status to NOT STARTED / PARTIAL / IMPLEMENTED / VERIFIED / BLOCKED. Only VERIFIED counts as done.
7. Produce synthetic fixtures, seeded demo credentials using secure local-only instructions, Docker-based startup, sample screenshots if supported, setup scripts, `make` targets or equivalents, admin and customer walkthroughs, and a safe deployment plan.
8. Report progress with exactly what was built, tests run (including command, exit result, and counts), open bugs, coverage where available, identified vulnerabilities and mitigations, security reviewer verdict, and the next prioritized steps. Provide branch/PR when supported. Never conceal failures or imply manual verification that did not happen.
9. Optimize for Claude cloud credit by delegating only useful independent tasks, limiting repeated reviews to changed scopes, and running local deterministic checks before expensive exploratory agents. Do not create paid external resources. Maintain notes that support new sessions continuing from the repository.

### Required final artifact layout (adapt if justified)

```
CLAUDE.md
.claude/agents/{release-reviewer,security-auditor,qa-engineer,...}.md
docs/{all required design,traceability,security,release documents}
apps/web/
apps/api/
workers/                    # if warranted
infra/                      # local Docker and deployment notes
tests/{unit,integration,e2e,security}/
scripts/
.github/workflows/
README.md
.env.example                # placeholders only, NEVER secrets
```

## Begin now

First inspect the repo and report a concise architecture decision, the four-stage requirements matrix, the first testable vertical slice and the independent-review process. Then CREATE the actual documents, agents and implementation. Do not stop after providing a plan. Start with Stage 1, invoke the reviewer only after QA/security evidence is available, fix real failures, and progress to subsequent stages as resources permit. Leave an honest, reproducible handoff if unfinished.
