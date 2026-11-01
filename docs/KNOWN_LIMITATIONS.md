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

## Stage 2–4
## Stage 2 (implemented on `claude/stage2-followup-assistant`, gate pending)
- Providers: the simulator (default) and SMTP are implemented; Gmail and Microsoft Graph adapters are designed and
  the OAuth setup is documented, but intentionally not shipped untested (the API rejects connecting them).
- Inbound synchronisation exists for the simulator only; SMTP is outbound-only (IMAP/Graph inbound is a follow-up).
- Quiet hours / weekend skipping use the organisation policy time zone, not each contact's time zone.
- The background worker is a single process; the `app.worker` RLS scope is a trusted-code boundary (see ADR-012).
- Draft bodies are plain text; HTML templates and attachments (e.g. the quote PDF) are not sent yet.

## Stage 3–4
Not implemented; designs and backlogs exist (ROADMAP.md, PRD.md, LLD.md).
