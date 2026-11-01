# Security Policy

## Principles
Least privilege, defense in depth, secure defaults, no secrets in code, everything audited, humans approve high-impact
actions, AI is never an authority.

## Controls (Stage 1)
| Area | Policy |
|---|---|
| Passwords | Argon2id via `argon2-cffi`; min length 12; no composition rules; breached-password check is a Stage 4 option |
| Sessions | Server-side; 12 h idle / 7 d absolute; cookie HttpOnly, SameSite=Lax, Secure (non-dev), Path=/; rotation on login; revocation on logout and role change |
| CSRF | Double-submit cookie + header for all unsafe methods |
| Authorization | Roles owner/admin/sales/approver/readonly; matrix in USER_JOURNEYS.md; enforced in `services/authz.py`; tenant scoping + RLS |
| Separation of duties | Submitter cannot approve own version unless `allow_self_approval` (owner-set, audited) |
| Input validation | Pydantic v2 strict schemas; Decimal strings; ISO currency whitelist; length caps |
| Files | 10 MiB, csv/xlsx/pdf only, magic-byte sniffing, parser subprocess with timeouts/rlimits; never executed; private storage |
| Output encoding | React escaping; API JSON only; CSV formula neutralisation; PDF text-only |
| Headers | nosniff, DENY framing, no-store on API, CSP default-src 'none' on API; web CSP documented for proxy |
| Secrets | Env only; `.env.example` placeholders; gitleaks in CI; no `NEXT_PUBLIC_` secrets |
| Logging | JSON, request id, no bodies/documents/tokens; errors return request id only |
| Dependencies | `pip-audit`, `npm audit`, Dependabot config; upgrade cadence monthly or on Critical/High |
| Rate limiting | Login 10/5 min per ip+email (in-memory; Redis in multi-replica) |
| Transport | TLS terminated at reverse proxy; HSTS recommended in runbook |
| Database | App role without ownership/bypass RLS; statement timeout; backups per runbook |

## Vulnerability handling
- Severity by CVSS v3.1 bands. Critical/High must be fixed or have a *human-signed* risk acceptance in
  `RELEASE_GATES.md` before release; AI agents may not accept risk.
- Report issues privately to the repository owner (no public issue). Coordinated disclosure within 90 days.

## Secure development
- Every PR: lint, type check, unit/integration, E2E, dependency/secret/static scans green; implementer ≠ reviewer;
  release-reviewer verdict recorded for stage releases and security-critical changes.
- Branch protection (human action required): require status checks `api`, `web`, `e2e`, `security`; require 1 review;
  disallow force-push to `main`.

## Never claims
We do not claim SOC 2, ISO 27001, GDPR/PDPL certification, or that the system has no vulnerabilities.
