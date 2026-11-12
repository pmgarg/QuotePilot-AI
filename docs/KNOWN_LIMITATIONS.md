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
- **Large RFQs against large catalogs lose suggestions (RR5-2).** Matching for one request stops after 8 s: for
  example about 100 lines against a 20,000-product catalog (2,000,000 comparisons) will not finish, so the later
  lines carry `match_skipped_budget` and need an exact SKU or a manual choice. Each large job also pays roughly 0.1 s
  to start the matcher process and ship the catalog descriptions to it.
- **Matcher resource limits.** The matcher process applies CPU/memory/file limits when the platform allows it and
  continues silently without them when it does not (the document parser reports that case; the matcher does not).
- **Adding a single RFQ line (SEC8-2, open).** `POST /rfqs/{id}/items` builds a catalog index and matches in the API
  process without a rate limit: about 1.4–2.1 s per call on a 30,000-product catalog. The bulk paths (create,
  re-parse) are limited and use the bounded matcher.
- **Matcher output reader (SEC8-1).** A pathologically nested line from the matcher process would raise instead of
  being ignored; the matcher is our own code and only prints flat lines.
- **Candidate order on equal scores (QA O1).** Suggestions with identical similarity scores may be listed in a
  different order between runs; observed once, not reproduced.
- **Zero-padded groups.** "01,234" is read as 1.234 and "012,345" as 12.345 (a group that starts with a zero is never
  a thousands group); a person might have meant 1,234. Such cells are unusual; the reading is pinned by tests.
- **Demo seed.** `make demo` and the Compose `migrate` service need `DEMO_PASSWORD` (≥ 12 characters); the seed
  never prints or generates a password. Demo accounts that already exist keep the password they were created with.
- **Value ranges.** RFQ quantities read from files are stored only up to 10,000,000 with three decimals (the same cap
  as manual entry); larger values are left empty with the flag `quantity_out_of_range`. Catalog list and cost prices
  must be below 10^12 (the column capacity). The quote editor accepts manual unit prices up to 10,000,000 only, so a
  catalog-priced line above that amount can be quoted at the catalog price but not manually re-priced.


## Stage 2 (implemented on `claude/stage2-followup-assistant`, gate pending)
- Providers: the simulator (default) and SMTP are implemented; Gmail and Microsoft Graph adapters are designed and
  the OAuth setup is documented, but intentionally not shipped untested (the API rejects connecting them).
- Inbound synchronisation exists for the simulator only; SMTP is outbound-only (IMAP/Graph inbound is a follow-up).
- Quiet hours / weekend skipping use the organisation policy time zone, not each contact's time zone.
- The background worker is a single process; the `app.worker` RLS scope is a trusted-code boundary (see ADR-012).
- Draft bodies are plain text; HTML templates and attachments (e.g. the quote PDF) are not sent yet.
- **Log redaction of malformed addresses (S2-SEC-12).** Addresses are removed from log lines with a bounded pattern:
  a local part longer than 64 characters is only partly redacted and a domain label longer than 63 characters not at
  all. Such addresses are invalid and are rejected as contacts; they can only reach a log through hostile or corrupt
  text.
- **Abandoned jobs (S2-RR1-4, fixed in round 3).** A job `running` longer than `JOB_VISIBILITY_TIMEOUT_SECONDS`
  (default 900) is reclaimed at the start of every worker tick and every tenant-scoped run-once: back to `queued` with
  its attempt count kept, or `dead` (schedule marked `failed`, admin retry re-arms it) when attempts are exhausted.
  Residual: the timeout must exceed the longest legitimate handler run, otherwise a slow but live job could be run a
  second time. A send is not delivered twice once the draft is committed as `sent`; if a worker dies after the provider
  accepted the message but before that commit, the redelivery is possible (at-least-once; the idempotency key is
  stored on the draft but not passed to the provider). Reclaims are logged, not audited (no actor).
- **Terminal schedule failure (S2-RR1-2, tested in round 3).** A permanent provider error ends the job `failed`, an
  exhausted/abandoned job ends `dead`; both mark the schedule `failed`, and the admin retry re-arms it.
- **S2-A05 (expired or invalid OAuth token).** Only exercised by proxy (a misconfigured SMTP provider, an outage);
  no OAuth provider exists, and "job paused, admin notified" is not demonstrated.
- **Opt-out matching.** Suppression compares addresses case-insensitively after trimming; plus-tags and dots are
  significant (`jane+x@…` is a different address from `jane@…`).
- **Inbound replies.** A message is linked to a quote by sender/reply headers; linking by `In-Reply-To` alone has no
  sender check (only the simulator exists today).
- Fix round 1 (2026-11-10) behaviour worth knowing:
  - Suppression is keyed on the e-mail address inside the tenant. An unsubscribe reply from an address that has **no
    contact row** is recorded as an unlinked event only; the address itself is not stored as a suppression (events do
    not keep raw addresses), so a contact created later for that address is not opted out automatically.
  - "No follow-ups" (`preferred_channel=none`) blocks manual drafts/sends as well as automation for that address; a
    user who wants to write to such a person must change the preference first.
  - When automation consent is withdrawn after schedules were planned, a due follow-up degrades to the default
    draft-only behaviour: a draft is prepared for a human, nothing is sent, the schedule becomes `drafted` with
    `mode = draft_only`, and `followup.degraded_to_draft` is audited (QA D-13, fix round 2). Restoring consent later
    does not auto-send that draft.
  - The edited draft is stored without the opt-out footer; it is appended when the message is sent. Placeholders are
    resolved when an edit is saved (a customer name that itself contains a placeholder-like token is therefore
    expanded once at edit time if the user changes the body).
  - A quote number in an inbound message links it to the quote only when the sender is a contact of the quote's
    customer or the message answers one of our own messages for that quote; otherwise it stays an unlinked event.
  - Per-tenant SMTP credentials are stored encrypted but **not used for sending** (S2-SEC-06, accepted): every tenant
    sends through the process-wide `SMTP_*` account. Using the stored secret needs per-tenant host/user configuration
    and is a Stage 2 follow-up; the strict-xfail test for it remains.
  - The hourly send limit is serialised per organisation by a row lock on the policy; the provider call happens
    inside that lock, so sends of one organisation are sequential.
  - Provider acknowledgement followed by a database failure before commit can still produce a duplicate on retry
    (no provider-side idempotency key); the window is small and unchanged.

## Stage 3 (implemented on `claude/stage3-customer-order-intelligence`, gate pending)
- Connectors are **push-model stubs** (`erp_stub`, `csv`): an operator or an external system posts records to the sync
  endpoint. No pull adapters, schedulers or outbound network calls ship (no paid or third-party services). Real
  ERP/CRM adapters implement the same scope/idempotency/conflict contract.
- A remote product that does not exist locally is staged as a conflict (`fields: ["new"]`) and cannot be applied from
  the queue; new SKUs enter through a catalog version import so pricing history stays versioned.
- Applying a remote value to a product edits the *active* catalog version in place (audited); historical quote lines
  keep their copied prices. There is no "create a new catalog version from the ERP" automation. Open (draft/pending)
  quotes with a catalog-priced line for that product get the blocker `catalog_price_changed` and cannot be submitted
  or approved until re-priced or overridden; already approved versions are not changed.
- Only one *active* connector per kind and tenant; a disabled connector can be re-enabled through the API
  (`POST /connectors/{id}/enable`), the data page has no button for it yet.
- Import/sync idempotency: the stored request hash covers the raw CSV bytes or the canonical JSON of the records, so a
  byte-different but semantically equal re-upload under the same key is refused with 409 (use a new key).
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
  pseudonymised customer for commercial record-keeping. The purge also redacts the subject in the connector conflict
  queue, in import-run failed rows and in connector run rejection reasons, pseudonymises the customer block of stored quote document snapshots, blanks subject, body
  snippet and provider headers of `communication_events` linked to the customer's quotes or contacts (rows kept for
  timeline integrity) and deletes the stored PDFs (they are re-rendered from the pseudonymised snapshot, so the PDF hash of an erased customer's
  document changes — erasure takes precedence over byte identity for that one field). **Not covered yet**: uploaded
  RFQ source files in object storage, `rfq_items.raw_text` (the purge only nulls `rfqs.pasted_text`),
  free-text fields that mention the customer elsewhere (quote notes, audit
  summaries, order notes), and backups.
- A purge job that keeps failing is re-queued up to 10 times and then marked `purge_stalled` for an operator; there
  is no alerting on that state. A purge job left `running` by a crashed worker is reclaimed after the job
  visibility timeout (`queue.reclaim_abandoned`, called from `retention.enqueue_due_purges`; SEC3-14 fixed, test passes).
- Purge redaction of copies matches the subject's reference, e-mail and name by substring in CSV import failed rows and
  in connector run rejection reasons; it can over-redact, and a copy that spells the subject differently is not found.
  Orders keep their `customer_id`, `external_ref` and notes after erasure (commercial record; human decision pending,
  release review 1 condition C1).
- Order numbers are per tenant (`SO-YYYY-NNNNN`); there is no year roll-over reset of the sequence.

## Stage 4
Not implemented; designs and backlogs exist (ROADMAP.md, PRD.md, LLD.md).
