# High-Level Design

## System context
```mermaid
C4Context
  title QuotePilot AI — system context
  Person(sales, "Sales / Approver / Admin", "Tenant users (browser)")
  System(qp, "QuotePilot AI", "Multi-tenant RFQ→quote→follow-up→intelligence SaaS")
  System_Ext(mail, "Mailbox provider (optional)", "Gmail / Microsoft Graph / SMTP — Stage 2")
  System_Ext(llm, "LLM provider (optional)", "Hosted or local model via adapter")
  System_Ext(erp, "ERP / CRM / inventory (optional)", "Connectors — Stage 3")
  System_Ext(bill, "Billing (test mode)", "Stage 4, no live payments without approval")
  Rel(sales, qp, "HTTPS, session cookie")
  Rel(qp, mail, "OAuth scoped, consented sends")
  Rel(qp, llm, "Schema-validated proposals, redacted input")
  Rel(qp, erp, "Idempotent sync, conflict queue")
  Rel(qp, bill, "Plan entitlements")
```

## Modules
```mermaid
flowchart LR
  subgraph Browser
    WEB[Next.js app<br/>client components, same-origin /api]
  end
  subgraph API[FastAPI monolith]
    AUTH[auth & sessions]
    TEN[tenancy & RBAC]
    CAT[catalog import/versions]
    RFQ[RFQ intake & parsers]
    MATCH[matching]
    QUOTE[pricing & quotes & approvals]
    EXPORT[PDF/CSV export]
    AUDIT[audit]
    AI[AI adapter]
    S2[follow-up: policies, drafts, jobs, providers]
    S3[intelligence: orders, KPIs, connectors]
    S4[governance: policy engine, tools, workflows, budgets, billing]
  end
  subgraph Workers[Worker process - Stage 2+]
    JOBS[job runner / scheduler]
  end
  PG[(PostgreSQL<br/>org-scoped + RLS)]
  OBJ[(Private object storage<br/>local dir / S3-compatible)]
  REDIS[(Redis - Stage 2+)]
  WEB -->|rewrite /api| API
  API --> PG
  API --> OBJ
  S2 --> REDIS
  JOBS --> REDIS
  JOBS --> PG
  RFQ -->|subprocess, timeout| PARSE[parser sandbox]
```
Single deployable API (modular monolith) with a separate worker process from Stage 2. No microservices.

## Tenant-aware data flow and trust boundaries
```mermaid
flowchart TB
  subgraph TB0[Untrusted: Internet]
    U[User browser]
    DOC[Uploaded documents]
    MAILIN[Inbound email - S2]
  end
  subgraph TB1[Trusted: API process]
    MW[Auth middleware: session → user → membership → org]
    SVC[Services: every query filtered by organization_id]
    DBS[DB session: SET LOCAL app.current_org]
  end
  subgraph TB2[Isolated: parser subprocess]
    P[pdfplumber / openpyxl / csv<br/>limits: size, pages, rows, seconds]
  end
  subgraph TB3[Data]
    DB[(PostgreSQL RLS policies)]
    OS[(Private storage: org/<id>/… keys)]
  end
  subgraph TB4[External, optional]
    LLM[LLM provider]
  end
  U -->|cookie + CSRF token| MW --> SVC --> DBS --> DB
  DOC --> MW --> P -->|structured rows only| SVC
  SVC --> OS
  SVC -->|redacted text, schema-validated reply| LLM
  MAILIN --> P
```
Trust rules: document and email content never becomes code, SQL, template or shell input. LLM output is parsed into
Pydantic models and merged as *proposed* data. Storage keys are server-generated; downloads go through authorized API.

## Auth and approval flow
```mermaid
sequenceDiagram
  participant B as Browser
  participant A as API
  participant DB as PostgreSQL
  B->>A: POST /api/auth/login (email, password)
  A->>DB: load user, verify Argon2id hash
  A->>DB: insert session (hashed token, expiry)
  A-->>B: Set-Cookie qp_session (HttpOnly, SameSite=Lax, Secure*) + csrf cookie
  B->>A: POST /api/quotes/{id}/submit (X-CSRF-Token)
  A->>A: resolve session → user → membership(role) → org
  A->>DB: BEGIN; SET LOCAL app.current_org; update quote; insert audit; COMMIT
  B->>A: POST /api/quotes/{id}/approve (approver account)
  A->>A: role in {owner, admin, approver} AND actor ≠ submitter (unless allowed)
  A->>DB: approval_decision + version.locked + audit
```
`*Secure` flag on when `APP_ENV != development`.

## Quote state machine
```mermaid
stateDiagram-v2
  [*] --> draft
  draft --> pending_approval: submit (no blockers or audited override)
  pending_approval --> approved: approve (approver ≠ submitter)
  pending_approval --> draft: reject (reason)
  approved --> draft: edit → new version (previous version stays approved/immutable)
  approved --> withdrawn: withdraw (privileged, reason)
  approved --> expired: valid_until passed (computed)
  approved --> sent: Stage 2
  sent --> awaiting_customer
  awaiting_customer --> won
  awaiting_customer --> lost
  awaiting_customer --> expired
```

## Stage 1 sequence — RFQ to PDF
```mermaid
sequenceDiagram
  participant S as Sales (browser)
  participant A as API
  participant P as Parser subprocess
  participant DB as PostgreSQL
  participant ST as Storage
  S->>A: POST /api/rfqs (multipart file | text)
  A->>A: validate size/type; store original
  A->>ST: put org/<org>/rfq/<id>/original
  A->>P: parse(path, kind) with timeout
  P-->>A: items[{raw_text, qty, unit, sku?, source_ref}] | error
  A->>DB: rfq.status = parsed|failed, rfq_items, match_candidates
  S->>A: PATCH /api/rfqs/{id}/items/{iid} (select candidate / edit)
  S->>A: POST /api/rfqs/{id}/generate-quote
  A->>DB: quote + version 1 + lines (Decimal pricing)
  S->>A: POST /api/quotes/{id}/submit (expected_version)
  Note over A: approver account
  A->>DB: approve → version locked, decision, audit
  S->>A: POST /api/quotes/{id}/versions/{n}/pdf → render deterministic PDF → ST
  S->>A: GET /api/quotes/{id}/versions/{n}/pdf (authorized stream)
```

## Stage 2 sequence — follow-up
```mermaid
sequenceDiagram
  participant W as Worker
  participant DB as PostgreSQL
  participant R as Redis
  participant PR as Mail provider adapter
  W->>DB: find due follow_up_schedules (org policy, not opted out)
  W->>R: enqueue job(idempotency_key = schedule_id:attempt)
  W->>DB: create communication_draft (status draft)
  alt automation consented by org admin
    W->>PR: send(draft) with rate limit + recipient validation
    PR-->>W: provider_message_id | 429/5xx
    W->>DB: event sent | retry with capped backoff | dead_letter
  else default
    Note over W,DB: draft awaits explicit user send
  end
```

## Stage 3 flow — intelligence
Orders are created from approved/won quotes (approval required), connector syncs write to staging tables and a
conflict queue; KPI service computes metrics from recorded events with explicit definitions; exports pass through the
role-aware serializer so cost/margin fields are stripped for Sales/Read-only.

## Stage 4 flow — bounded assistant
```mermaid
sequenceDiagram
  participant U as User
  participant A as API (policy engine)
  participant L as LLM adapter
  participant T as Tool registry
  U->>A: ask("prepare follow-up for Q-1024")
  A->>A: tools = policy.allowed_tools(user.role, plan, org.kill_switch)
  A->>L: prompt(redacted context, tool schemas) within budget
  L-->>A: structured tool call(s)
  A->>A: validate schema + scope; reject unknown/out-of-scope
  A->>T: execute read tool (tenant-scoped query)
  A->>U: proposal with evidence citations
  U->>A: approve action
  A->>T: execute write tool under approval record + audit
```

## Deployment
```mermaid
flowchart LR
  subgraph Prod[Production - documented topology]
    LB[Reverse proxy / TLS<br/>Caddy or Nginx]
    WEBP[web container]
    APIP[api container x N]
    WRK[worker container - S2+]
    PGP[(Managed PostgreSQL<br/>backups, PITR)]
    S3P[(S3-compatible bucket<br/>private, SSE)]
    RDP[(Redis)]
    MON[Metrics/logs: Prometheus+Grafana or hosted]
  end
  LB --> WEBP --> APIP --> PGP
  APIP --> S3P
  APIP --> RDP
  WRK --> RDP & PGP & S3P
  APIP --> MON
```
Local demo: `infra/docker-compose.yml` (postgres, redis, api, web, migrate+seed). See OPERATIONS_RUNBOOK.md.

## Failure modes, timeouts, retries, idempotency (summary; details in LLD)
| Flow | Failure | Handling |
|---|---|---|
| Upload/parse | oversize, bad type, parser crash/timeout | 413/415 before storage; RFQ `failed` with reason; retry endpoint; no partial lines |
| Pricing | missing price, currency mismatch, below margin | blocker list on quote; submit refused; privileged override with reason |
| Concurrency | two editors | `expected_version` → 409 with current version |
| PDF | render error | version stays approved; export retriable; audit `export_failed` |
| Sessions | expiry/revocation | 401 → login; sessions table checked each request |
| Stage 2 jobs | provider 429/5xx/timeout | capped exponential backoff (max 5), idempotency key prevents duplicate send, dead-letter after cap |
| Stage 3 sync | conflict | staging + conflict queue, human resolution |
| Stage 4 agent | loop, oversize, budget | depth/turn caps, token budget, circuit breaker, kill switch |
