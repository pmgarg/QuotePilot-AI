# Known Limitations & Residual Risks (honest list; update every release)

## Stage 1 (current)
- Login rate limiter is in-process memory; horizontally scaled deployments need the Redis-backed limiter (Stage 2 runner adds Redis).
- OIDC/SSO not implemented; local accounts only. Member "invites" create local users with an admin-set initial password (no email).
- Parsers handle digitally readable documents only; scanned PDFs are flagged `unsupported_scanned` (OCR is an optional future component).
- The parser subprocess is isolated by interpreter flags and resource limits only; it runs as the API's OS user. Network and
  filesystem isolation (container, seccomp, read-only mounts) are deployment controls documented in the runbook.
- Fuzzy matching thresholds are heuristic, not calibrated; confidence is categorical by design.
- Unit conversion is alias normalisation only (no quantity conversion between units); mismatches require human confirmation.
- PDF branding is text-based (no logo upload yet); fonts limited to built-in Helvetica for determinism.
- No Docker daemon was available in the development container: `infra/docker-compose.yml` is written and linted but
  has not been executed end-to-end there; local PostgreSQL 16 was used for all recorded test evidence.
- Encryption at rest, TLS, backups and WAF are deployment responsibilities documented in the runbook.
- Multi-organization users are supported in the data model; the UI offers no org switcher yet.
- Medium: `npm audit` reports transitive `minimatch` advisories via `eslint-config-next` (dev-only tooling, not shipped).
- gitleaks and semgrep could not be run inside the development container (binary install blocked / registry rulesets
  need telemetry); both run in CI. A pattern-based secret grep ran locally as a fallback.
- Catalog header detection for import expects the header on the first row; RFQ spreadsheets may have the header lower.
- Quote PDFs are regenerated on download for drafts; approved PDFs are stored and byte-identical on repeat.

- Web CSP keeps `script-src 'unsafe-inline'` (Next.js inline runtime); a nonce-based policy is a follow-up.
- The login limiter is per API process (`--workers 2` doubles the budget); in the Docker topology all users share the
  web container's IP, so 10 wrong passwords for one address temporarily lock that address (5 min) — a deliberate
  brute-force trade-off until the Redis-backed limiter (Stage 2).
- `GET …/pdf` renders, stores (approved versions) and audits the download; the write is idempotent (same bytes/hash).
- Audit of `quote.update` records per-line pricing deltas (max 50 per save); description-only edits are summarised.
- Timing of login responses is equalised with a dummy Argon2 verification but not formally measured under load.

- Member creation answers 409 when an e-mail address is already registered on the platform (tenant admins can learn
  that an address exists). Accepted as Low pending a human product decision on an opaque invitation flow.
- Manual prices (custom lines or typed-over catalog prices) require an owner/admin confirmation with a reason before
  submission (policy adopted after review 1 finding M-4); approvers alone cannot clear that blocker.

- The per-account login limiter (50 failures / 5 min across all addresses) can be used to lock a known address out
  for five minutes; accepted over unlimited distributed guessing until the Redis limiter with progressive delays.
- At most 4 documents parse concurrently per API process; extra uploads wait up to the parse timeout (bounded, not queued).
- `X-Forwarded-Proto` from clients is dropped by the proxy; the API derives nothing from it today.

- Manual-price confirmations are per version (a new version copies prices but requires re-confirmation); the audit
  action is still named `quote.margin_override` for backward compatibility of the history view.
- H-1 immutability evidence rests on byte-identity of stored PDFs (PDF streams are compressed, so plain-text
  "not in bytes" checks are not meaningful).

- The margin blocker itself reveals *that* a price is below the floor; a Sales user probing discounts can infer the
  cost/(1 − floor) ratio for a line. Inherent to showing the blocker; accepted as Low.

- **Catalog version pinning (O2-1).** A quote generated from an RFQ prices from the catalog version that was active
  when the RFQ was parsed, for its whole life (every later version copies the pin). Ad-hoc quotes (`POST /quotes`,
  or RFQs parsed before any catalog was active) price from the *current* active version. After an activation, an
  ad-hoc draft keeps its old lines editable but cannot be submitted until each `catalog` line is re-linked to the new
  list (`catalog_version_mismatch` blocker); RFQ-pinned quotes stay on their pinned list (an archived list remains the
  authority for the RFQ it answered). Never-activated (imported-only) lists are never an authority on any route.
- **Withdrawn documents on a quote with an open draft (O2-2).** The quote header stays `draft`; the withdrawn state
  lives on the version rows (`versions[].status = withdrawn`) and is shown as an "Earlier document withdrawn" banner.
  The list filter `status=withdrawn` shows only fully withdrawn quotes. A pending version made obsolete by a
  withdrawal is `superseded` and can still be previewed as a PDF labelled "WITHDRAWN" (O2-3; not an approved document).
- **Platform-wide session revocation audit (NEW-15).** `scripts/revoke_sessions.py --user-email/--all` writes one
  `ops.sessions_revoked` audit row per affected organisation (tenant-readable); the platform total is in each row's
  `summary.total` and on stdout. There is no platform-scope audit table.
- **CI e2e flake (I-3).** One PR-triggered `ci` run on 58976f3 failed in the browser job with symptoms of the web
  server returning errors mid-suite (404/409 after successful creates, `ERR_ABORTED` navigations) while the push run of
  the same commit was green. The trace artifact could not be downloaded from the development container (host blocked);
  the specs now assert every intermediate API call with its response body so a recurrence is diagnosable. Both the
  push- and the PR-triggered runs must be green on the gated HEAD.


## Stage 2 (implemented on `claude/stage2-followup-assistant`, gate pending)
- Providers: the simulator (default) and SMTP are implemented; Gmail and Microsoft Graph adapters are designed and
  the OAuth setup is documented, but intentionally not shipped untested (the API rejects connecting them).
- Inbound synchronisation exists for the simulator only; SMTP is outbound-only (IMAP/Graph inbound is a follow-up).
- Quiet hours / weekend skipping use the organisation policy time zone, not each contact's time zone.
- The background worker is a single process; the `app.worker` RLS scope is a trusted-code boundary (see ADR-012).
- Draft bodies are plain text; HTML templates and attachments (e.g. the quote PDF) are not sent yet.

## Stage 3 (implemented on `claude/stage3-customer-order-intelligence`, gate pending)
- Connectors are **push-model stubs** (`erp_stub`, `csv`): an operator or an external system posts records to the sync
  endpoint. No pull adapters, schedulers or outbound network calls ship (no paid or third-party services). Real
  ERP/CRM adapters implement the same scope/idempotency/conflict contract.
- A remote product that does not exist locally is staged as a conflict (`fields: ["new"]`) and cannot be applied from
  the queue; new SKUs enter through a catalog version import so pricing history stays versioned.
- Applying a remote value to a product edits the *active* catalog version in place (audited); historical quote lines
  keep their copied prices. There is no "create a new catalog version from the ERP" automation.
- Stock snapshots are append-only and unbounded (no compaction job yet); the staleness window is one setting per deployment.
- KPIs are computed on request without caching; the date range is capped at three years. Response time uses the first
  submitted quote per RFQ; processing time the first approval per quote.
- FX rates are manually maintained reporting rates per tenant; no rate feed is consulted. Converted figures use the
  latest rate on or before the quote date (pending value: today's date).
- Product CSV *import* is the Stage 1 catalog import (versioned, mapped, reported); the Stage 3 import endpoint covers
  customers and orders. Imported orders are always `proposed` and need confirmation.
- Repeat-order proposals use a median-interval heuristic (≥2 confirmed orders of a SKU; next date within 14 days);
  cross-sell proposals are not implemented. Prices come from the active catalog or, explicitly labelled, the last order.
- Deletion requests cover customers (with their contacts, drafts and RFQ text). Quotes and orders are kept with a
  pseudonymised customer for commercial record-keeping; uploaded RFQ files are not deleted by the purge job yet.
- Order numbers are per tenant (`SO-YYYY-NNNNN`); there is no year roll-over reset of the sequence.

## Stage 4 (implemented on `claude/stage4-governed-platform`, gate pending)
- The assistant's default planner is a deterministic intent parser (no network, no cost). The optional hosted planner
  (`AI_PROVIDER=anthropic`, server key, owner consent) is implemented but was never called in this repository (no paid
  calls); its output passes the same policy gate, so it can only propose registered tools.
- The assistant plans from the user's message only (single pass); tool results are never fed back into planning. This
  limits multi-step reasoning by design (prompt-injection surface) — multi-step automation is what workflows are for.
- Approval of an assistant-proposed action is a human-in-the-loop checkpoint by the requester (or an owner/admin);
  separation of duties for the business effect stays with the downstream gates (order confirmation, deletion approval,
  quote approval, the send gate).
- Compensation hooks exist for `propose_order` (cancel the proposed order) and `draft_follow_up` (cancel the draft);
  other side effects stop the run with an explanation.
- Billing is a test-mode adapter only (synthetic references, no payment provider, no invoices); plan limits are
  enforced server-side. Onboarding wizard, in-app help pages and a demo-workspace reset are not built.
- The circuit breaker looks at the last N live runs of the tenant in the cooldown window; it is per tenant, not per
  workflow. The platform kill switch is an environment flag (process restart to change).
- Workflow compensation after a failed *approved* step is not run (the run ends `failed`; earlier executed steps are not
  undone). A failed approved manual action is persisted, but the handler's partial work is rolled back to a savepoint.
- Workflow trigger `quote.won` is not supported (no dispatch exists); validation rejects it.
- Step timeouts are measured after the step returns (no pre-emptive cancellation of a running handler).

