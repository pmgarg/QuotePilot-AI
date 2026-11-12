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
  `ops.sessions_revoked` audit row per affected organisation with that organisation's own count (tenant-readable); the
  platform-wide total is printed to stdout only (R4-1). There is no platform-scope audit table.
- **CI e2e flake (I-3).** Three pull_request-triggered `ci` runs (on 58976f3, 597f4b0 and e090089) failed in the
  browser job while the push runs of the same commits were green: logins landed back on `/login`, submit clicks left the
  version in `draft`, some API calls returned 404/409 after successful creates. No product rule failed. Since d0aac67
  (API request log enabled in CI) both events have been green on every commit. The trace artifacts
  could not be downloaded from the development container (host blocked), so the failed runs themselves were never
  inspected. **Probable cause found in fix round 8 (D5):** the database dependency committed *after* the response had
  been sent, so a client that immediately issued its next request could read the state from before the commit — a
  session row not yet visible (back to `/login`), a version still `draft` after a successful submit, a 404 right after
  a 201. All three recorded symptoms match, and the race was reproduced locally (3 of 400 immediate reads after a
  create returned 404; a deterministic test now pins the order). This is an inference: it cannot be proven for the
  three historical runs. Diagnostics stay enabled; every gated HEAD needs both events green, and CI retries (1) mean
  flaky counts must be read from the job log, not from the conclusion.
- **Numbers read from files (round 8).** The shared decimal parser accepts exactly one number per cell, optionally
  wrapped in a currency code/symbol or unit, and returns nothing for anything else ("12 per 6", "1 e3", "Rs.500",
  exponent notation mixed with text). Such cells become row errors (catalog import) or an empty, flagged quantity (RFQ
  items) for a human to fix; they are never guessed. Two ambiguities remain by design and are documented in the code:
  "1,234" is read as 1234 (thousands) and "12,5" as 12.5; "0,500" is 0.500 (a group never starts with a zero).
  Thousands written with dots only ("1.234.567") are rejected (as before round 8). Cells a person would understand
  but the parser refuses on purpose: "Rs.500", "12 m²", "12 (approx)", "12,", "(-5)". Cells it reads although a person
  might hesitate: a short text with exactly one number ("delivery within 2 weeks" → 2, "2k pcs" → 2), a dash used as
  a separator ("AED - 5" → −5, which the catalog import then rejects as negative), mixed digit scripts in one number
  ("1٢3" → 123) and "5%" → 5. None of these assembles a number from fragments.
- **RFQ text lines (round 9).** Lines longer than 1,000 characters (after collapsing blanks) are not searched for a
  quantity; they are kept as an item without quantity only if they name a SKU. Tokens longer than 64 characters are
  never treated as SKUs.
- **Matching cost (SEC6-2 / SEC7-1, mitigated in round 11).** Fuzzy matching compares every unmatched RFQ line with
  every catalog description. Bounds: 200 characters per line and per catalog description, 2,000,000 comparisons per
  RFQ parse, and a wall-clock deadline of `MATCH_TIMEOUT_SECONDS` (default 8) per parse. Jobs up to 50,000
  comparisons (products x lines) run in the API process as before (worst case about 1-2 s of CPU); larger jobs run in
  a resource-limited worker process (`app.services.match_worker`), at most two such processes per API process, so the
  API keeps serving other requests while it waits. A line that was not matched because of the budget, the deadline, a
  busy matcher or a worker failure is still matched by exact SKU and is otherwise left for a person (flag
  `match_skipped_budget`, matching proceeds in line order so the skipped lines are the later ones). Measured with a
  30,000-product x 300-character catalog and 450 adversarial lines: 11.6 s per request (about 3.6 s of that is loading
  and storing), 51 lines matched, 399 flagged; other requests answered in at most 0.34 s meanwhile (the former
  in-process matcher blocked them for 11 s at a smaller size). Still true: with very large catalogs a long RFQ loses
  fuzzy suggestions for its later lines; a person can enter the exact SKU on such a line in the RFQ screen. One
  parse can occupy a database transaction for the deadline. Matching still starts a Python process per large job
  (about 0.1 s) and ships the catalog descriptions to it (about 200 bytes per product).
- **Rate limits on expensive endpoints (SEC7-7).** Per user and in process memory (like the login limiter, so
  per API replica and reset by a restart): 30 RFQ creations or re-parses and 10 catalog imports per 5 minutes
  (`RFQ_PARSE_RATE_LIMIT_ATTEMPTS`, `CATALOG_IMPORT_RATE_LIMIT_ATTEMPTS`, `EXPENSIVE_RATE_LIMIT_WINDOW_SECONDS`). Beyond
  that the API answers 429 with `Retry-After`. Refused requests are not audited (as with the login limiter). Other
  expensive paths (quote generation, exports) are not limited. A shared limiter (Redis) is needed for several replicas.
- **Zero-padded groups.** "01,234" is read as 1.234 and "012,345" as 12.345 (a group that starts with a zero is never
  a thousands group); a person might have meant 1,234. Such cells are unusual; the reading is pinned by tests.
- **Demo seed.** `make demo` and the Compose `migrate` service need `DEMO_PASSWORD` (≥ 12 characters); the seed
  never prints or generates a password. Demo accounts that already exist keep the password they were created with.
- **Value ranges.** RFQ quantities read from files are stored only up to 10,000,000 with three decimals (the same cap
  as manual entry); larger values are left empty with the flag `quantity_out_of_range`. Catalog list and cost prices
  must be below 10^12 (the column capacity). The quote editor accepts manual unit prices up to 10,000,000 only, so a
  catalog-priced line above that amount can be quoted at the catalog price but not manually re-priced.


## Stage 2–4
Not implemented; designs and backlogs exist (ROADMAP.md, PRD.md, LLD.md).
