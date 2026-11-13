# Product Requirements Document — QuotePilot AI

Requirement IDs are stable and referenced by `REQUIREMENTS_TRACEABILITY.md`, tests and ADRs.
Priority: **C** = critical for the stage gate, **H** = high, **M** = medium.

## Cross-cutting (all stages)
| ID | Pri | Requirement |
|---|---|---|
| X-01 | C | Multi-tenant: every business record belongs to exactly one organization; no cross-tenant read/write/download. |
| X-02 | C | Server-side RBAC with roles Owner, Admin, Sales, Approver, Read-only (least privilege, see USER_JOURNEYS). |
| X-03 | C | All high-impact actions (approve, override, send, order creation, price change, export of sensitive data, delete) are server-authorized and audited. |
| X-04 | C | No paid dependency on the core path; hosted LLM/email/S3/billing adapters optional and off by default. |
| X-05 | C | Synthetic data only until a customer authorizes processing; consent + data-handling settings before any model upload. |
| X-06 | H | Money handled as Decimal with per-currency minor units; totals attribute discount and tax per line. |
| X-07 | H | Audit trail: actor, organization, action, entity, before/after summary, request id, timestamp. |
| X-08 | H | Accessible, responsive UI with explicit empty/loading/error/permission states. |
| X-09 | H | Docker Compose demo; documented production topology; CI with lint/type/test/security scans. |

## Stage 1 — RFQ to verified quotation
### Functional
| ID | Pri | Requirement |
|---|---|---|
| S1-F01 | C | Organizations with memberships and the five roles; owner cannot be removed if last owner. |
| S1-F02 | C | Email/password authentication using Argon2id (library), server-side revocable sessions, HttpOnly/SameSite cookies, CSRF protection, login rate limiting, generic error messages. |
| S1-F03 | H | Organization settings: legal name, address, brand colour/footer text, default currency, default tax rate, rounding mode, minimum margin %, default validity days, `allow_self_approval`. |
| S1-F04 | C | Catalog import from CSV/XLSX with column mapping (sku, description, unit, currency, list_price, optional cost, stock, valid_from/valid_to); validation report (row errors, duplicates, formula-like cells); immutable catalog versions; activate one version. |
| S1-F05 | H | Customer accounts (name, reference, email, default currency, notes) selectable on RFQs/quotes. |
| S1-F06 | C | RFQ intake: upload digital PDF/XLSX/CSV (size/type/time limits) or paste text; parser runs in an isolated subprocess; scanned/image PDFs flagged unsupported; each extracted item keeps a source reference (page/row/line + raw text). |
| S1-F07 | C | Extracted item list with categorical confidence (exact / likely / ambiguous / unmatched / unsupported) and editable quantity, unit, description; add/remove lines. |
| S1-F08 | H | Unit normalisation via alias table (EA, PCS, BOX, KG, M, L…); SKU normalisation (case, whitespace, separators). Unit mismatch between RFQ and catalog is flagged and blocks until user confirms. |
| S1-F09 | C | Matching: exact normalised SKU first; then ranked fuzzy description match (rapidfuzz) with thresholds; show top-N candidates, scores as category, and reason strings. |
| S1-F10 | C | Human verification: user selects a candidate, overrides qty/unit/currency, or marks line as "no match / custom"; nothing auto-finalises. |
| S1-F11 | C | Deterministic quote generation: unit price from active catalog version (currency must match quote currency or be explicitly entered), line discount %, tax %, rounding per org, totals: subtotal, discount, tax, grand total. |
| S1-F12 | C | Missing price / below minimum margin / unresolved match blocks submission; Owner/Admin may override with a mandatory reason, recorded in audit and shown on the quote. |
| S1-F13 | C | Quote editing with versions; optimistic concurrency (`expected_version`) returns 409 on conflict; approved versions are immutable, edits create a new draft version. |
| S1-F14 | C | Approval workflow: draft → pending_approval → approved / rejected; only Approver/Admin/Owner approve; submitter cannot approve own version unless org allows; decision reason stored. |
| S1-F15 | H | Quote status includes withdrawn (by privileged user) and expired (valid_until passed); expired/withdrawn quotes cannot be approved or exported as current. |
| S1-F16 | C | Branded PDF export of an approved version (deterministic bytes for identical input; stored privately; downloaded only via authorized endpoint) and CSV export with formula-injection neutralisation. |
| S1-F17 | H | Quotes list with search (number, customer, status) and per-quote history (versions, decisions, audit events). |
| S1-F18 | C | Audit events written in the same transaction as every mutation. |
| S1-F19 | H | Synthetic demo fixtures: two organizations, users per role, catalog, sample RFQs (clean, ambiguous, malicious). |
| S1-F20 | H | Responsive UI with states per USER_JOURNEYS; no cost/margin exposure to Sales/Read-only. |
| S1-F21 | M | AI provider adapter interface with `disabled` default and optional hosted provider; LLM used only to propose line extraction from pasted text; outputs schema-validated and merged as "proposed" lines. |

### Acceptance tests (Stage 1)
| ID | Scenario | Expected |
|---|---|---|
| S1-A01 | RFQ line with exact SKU | single candidate, category `exact`, auto-selected but still editable |
| S1-A02 | Description matches several products | category `ambiguous`, no auto-selection, generation blocked until resolved |
| S1-A03 | Unknown SKU/description | `unmatched`, blocked; user may enter custom line with explicit price (privileged) or remove |
| S1-A04 | Duplicate line in RFQ | flagged `duplicate`, user merges or keeps with confirmation |
| S1-A05 | Unsupported file (docx, exe, png) | 415 with clear message; nothing stored beyond audit |
| S1-A06 | Corrupted PDF/XLSX | RFQ state `failed` with reason; retry allowed; no server crash |
| S1-A07 | Formula injection (`=HYPERLINK`, `+cmd`, `@SUM`) in catalog/RFQ | stored as text, flagged in report; CSV export prefixes `'` |
| S1-A08 | Oversized file / too many rows / pages | 413 / validation error with limits stated |
| S1-A09 | Negative, zero, non-numeric quantity | 422 with field error |
| S1-A10 | RFQ unit `box` vs catalog `EA` | `unit_mismatch` flag; blocked until confirmed |
| S1-A11 | Currency and decimals (JPY 0 dp, KWD 3 dp, EUR comma decimals in CSV) | correct quantisation; mismatched currency blocks price auto-fill |
| S1-A12 | Rounding/tax/discount | line and totals match fixture expectations under ROUND_HALF_UP |
| S1-A13 | Sales/Read-only attempts approve | 403; submitter self-approval 403 unless allowed |
| S1-A14 | Other tenant reads/updates/downloads quote, RFQ, file, catalog | 404; RLS blocks even if service filter omitted |
| S1-A15 | Expired / withdrawn quote | approve/export-as-current rejected with 409 state error |
| S1-A16 | Two concurrent edits | second receives 409 with current version |
| S1-A17 | Parser timeout/crash | RFQ `failed`, error visible, retry works, no partial lines persisted |
| S1-A18 | Same approved version exported twice | identical PDF bytes (hash) |
| S1-A19 | Every mutation | audit row with actor/org/action/entity |
| S1-A20 | Browser E2E | login → catalog import → RFQ upload → review → generate → submit → approve (second user) → PDF download |

## Stage 2 — Sales follow-up assistant
| ID | Pri | Requirement |
|---|---|---|
| S2-F01 | C | Lifecycle states: approved → sent → acknowledged (only when recorded) → awaiting_customer → won / lost / expired; manual transitions audited. |
| S2-F02 | C | Follow-up policies per org (timing, max attempts, quiet hours, time zone) and per-contact opt-out / preferences. |
| S2-F03 | H | Recommendation engine (rules) produces next actions with explicit fact-based reasons; no LLM required. |
| S2-F04 | C | Draft generation: editable personalised email with accurate quote number, totals, validity, opt-out link text; templates per org. |
| S2-F05 | C | Sending requires explicit user action, or an org-admin-enabled consented automation; default draft-only; rate limits; recipient validation; audit. |
| S2-F06 | C | Provider adapters: simulator (default), SMTP, Gmail API, Microsoft Graph (optional, minimal scopes, encrypted token storage, revocation handling). |
| S2-F07 | C | Job runner (PostgreSQL queue with `SKIP LOCKED`, ADR-012 as amended; originally planned on Redis) with idempotency keys, capped exponential backoff, dead-letter view, no duplicate sends. |
| S2-F08 | H | Inbound sync (simulator / configured provider) links replies to quotes by thread/quote number; never fabricates replies. |
| S2-F09 | H | Sales timeline per quote/customer showing recorded events only. |
| S2-F10 | H | Pending follow-ups view with cancel/reschedule. |
| S2-F11 | H | Logs redact message bodies and addresses by default. |
| Acceptance | | S2-A01 no send without approval; S2-A02 opted-out contact never auto-messaged; S2-A03 duplicate trigger → single send; S2-A04 no cross-tenant contact access; S2-A05 invalid/expired OAuth token handled (job paused, admin notified, no crash); S2-A06 provider 429/5xx/timeout → retry with backoff then dead-letter; S2-A07 user sees and cancels pending follow-up; S2-A08 logs contain no message bodies/addresses. |

## Stage 3 — Customer and order intelligence
| ID | Pri | Requirement |
|---|---|---|
| S3-F01 | C | Customer/contact profiles with preferences, history, role-aware cost/margin visibility. |
| S3-F02 | C | Quote → order conversion (manual or connector) with order events; order creation requires approval. |
| S3-F03 | H | Repeat-order assistant from own history only; proposals require approval. |
| S3-F04 | C | KPI dashboard: quote volume, conversion rate, pending value, response/processing time, repeat-order rate; each with written definition, date range and currency rule (reporting currency with explicit FX table and "unconverted" bucket). |
| S3-F05 | H | CSV import/export for customers, orders, products with formula-safe output and partial-import recovery. |
| S3-F06 | H | Connector adapters (ERP/inventory/CRM) with declared scopes, idempotent sync, conflict queue; no silent overwrite of master data. |
| S3-F07 | H | Stock claims only from connected timestamped sources; unknown/stale labelled. |
| S3-F08 | H | Retention/deletion workflows for customer data (soft delete → purge job) with audit. |
| Acceptance | | S3-A01 KPIs match fixtures; S3-A02 cost/margin hidden from Sales/Read-only in API and exports; S3-A03 multi-currency aggregation rules; S3-A04 sync conflict → review queue; S3-A05 stale inventory labelled; S3-A06 unauthorized export → 403 and audited; S3-A07 partial import recovery; S3-A08 repeat-order audit trail; S3-A09 cross-tenant leak regression suite green. |

## Stage 4 — Governed AI sales operations platform
| ID | Pri | Requirement |
|---|---|---|
| S4-F01 | C | Unified command center (RFQs, approvals, drafts, orders, reports) with org-configurable workflows. |
| S4-F02 | C | Bounded assistant: retrieval restricted to tenant-authorized records; tool catalogue defined by backend policy and the acting user's entitlements; no arbitrary SQL/shell/HTTP. |
| S4-F03 | C | Action scopes and approval checkpoints for sending, order/price changes, deletion, bulk export. |
| S4-F04 | C | Budgets: token/call limits, rate limits, timeouts, max workflow depth, retry caps, circuit breakers, kill switch, compensation hooks. |
| S4-F05 | H | Workflow definitions validated, versioned, dry-run capable, with history and failure explanations. |
| S4-F06 | H | Commercial packaging: plan entitlements (test-mode billing adapter), onboarding, demo workspace, help, support export, tenant admin, ops metrics. **Status: PARTIAL** — onboarding wizard, in-app help pages and demo-workspace reset are not built (human decision open, release review 1 C1). |
| S4-F07 | C | Model traces and logs contain no secrets or raw confidential content by default. |
| Acceptance | | S4-A01 prompt injection in RFQ/email cannot trigger unauthorized tool; S4-A02 tenant context isolation in retrieval; S4-A03 unapproved message/order never executes; S4-A04 malformed/adversarial tool output rejected; S4-A05 loops/oversized requests stopped; S4-A06 billing/limits enforced server-side; S4-A07 kill switch terminates workflows; S4-A08 logs/traces free of secrets and raw content; S4-A09 workflow dry-run produces no side effects. |

## Explicit dependencies
Stage 2 depends on S1-F13/F14/F18 (approved versions, audit). Stage 3 depends on S2-F01 (lifecycle) and S1-F05.
Stage 4 depends on all prior audit/approval/tenant primitives and introduces policy engine, workflow runner, billing.
