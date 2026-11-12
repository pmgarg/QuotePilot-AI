# Privacy, Data Handling & Retention (draft for professional review)

## Data inventory (Stage 1)
| Data | Source | Purpose | Retention default | Location |
|---|---|---|---|---|
| User account (email, name, Argon2 hash) | tenant admin | authentication | life of account + 30 d | PostgreSQL |
| Sessions (hash, ip, UA) | login | security | until expiry/logout; purged after 30 d | PostgreSQL |
| Organization settings | admin | branding/pricing | life of tenant | PostgreSQL |
| Catalog incl. cost prices | admin upload | pricing | life of tenant (versions immutable; purge on request) | PostgreSQL + original file in storage |
| Customer accounts (name, email) | sales | quoting | 24 months after last activity, configurable | PostgreSQL |
| RFQ files & parsed text | customer document | quoting | 24 months, configurable | storage + PostgreSQL |
| Quotes/versions/PDF | generated | commercial record | 7 years default (commercial law varies) | PostgreSQL + storage |
| Audit events | system | accountability | 7 years | PostgreSQL |
| AI usage metadata (no content) | optional adapter | budgets | 24 months | PostgreSQL |

## Roles
QuotePilot operator = processor for tenant business data; tenant = controller. Each tenant must obtain lawful basis for
customer contact data. A Data Processing Agreement template is required before any real customer data (not included).

## Consent & AI
Hosted LLM processing is off by default. Enabling requires an Owner to accept the data-handling notice in settings
(Stage 1 config flag `AI_PROVIDER` + Owner consent record), which names the provider, region and retention terms.
Inputs are redacted (no cost/margin/credentials), bounded, and only RFQ text is sent; outputs are stored as proposals.

## Subprocessors / data flows (template)
| Subprocessor | Purpose | Data | Status |
|---|---|---|---|
| Hosting (TBD) | run containers/DB | all | not selected |
| Object storage (S3-compatible, TBD) | files/PDFs | documents | optional |
| LLM provider (Anthropic or local) | extraction proposals | redacted RFQ text | optional, consent required |
| Mail provider (Gmail/Microsoft) | Stage 2 sending/sync | messages, tokens | optional, OAuth scoped |

## Data subject & tenant rights
Export: tenant admin can export quotes/customers (CSV) — Stage 3 adds full export. Deletion: Stage 3 deletion workflow
(request → approval by a different owner/admin → immediate soft delete → worker purge after `RETENTION_GRACE_DAYS`;
legal hold blocks both; audit retained). The purge pseudonymises the customer row, deletes contacts and drafts, nulls
RFQ pasted text, redacts the subject in connector conflicts and import failed rows, pseudonymises the customer block
of stored quote snapshots and deletes stored PDFs. Not covered: uploaded RFQ source files, free text elsewhere and
backups (see KNOWN_LIMITATIONS). Access requests handled by tenant admin with operator support.

## Breach response (draft)
1. Detect (alerts, reports) → 2. Contain (revoke sessions/tokens, rotate secrets, isolate tenant) → 3. Assess scope using
audit log and storage access logs → 4. Notify tenants within 72 h of confirmation with facts known → 5. Remediate and
post-mortem in `docs/evidence/incidents/`. Contacts and templates to be completed by the human owner.

## Compliance questions needing professional review
GDPR Art. 28 terms, UAE PDPL cross-border rules, India DPDP consent notices, record-keeping durations for quotations
per jurisdiction, e-signature validity of PDF quotes, VAT/e-invoicing interplay, employee monitoring rules for mailbox sync.
