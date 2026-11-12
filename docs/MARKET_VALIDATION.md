# Market Validation (assumptions to test — not verified claims)

## Target segment
Distributors / trading companies, 2–50 employees, 1–20 sales staff, 50–5,000 SKUs, quoting 10–300 RFQs per month,
often across borders (e.g. GCC, EU, South Asia trade lanes) with multi-currency price lists. Tools today: Excel/Sheets,
Outlook/Gmail, WhatsApp, occasionally a basic ERP (Tally, Zoho Books, Odoo CE, SAP B1) whose quoting module is unused.

## Pain hypotheses (to validate with ≥ 10 interviews before paid launch)
| ID | Hypothesis | Validation signal |
|---|---|---|
| MV-01 | Re-keying RFQ lines costs 20–60 min per RFQ | time-and-motion in 5 interviews |
| MV-02 | Unit/SKU mismatches cause ≥ 1 costly error per month | incident anecdotes, credit notes |
| MV-03 | Owners want approval control over discounts/margins | ask who signs off today |
| MV-04 | Follow-up is forgotten for > 30 % of quotes | sample of last 50 quotes |
| MV-05 | Buyers will pay USD 25–60 / seat / month | pricing conversation, pilot LOI |

## Competitive landscape (positioning, not a claim of superiority)
Generic CPQ (Salesforce CPQ, DealHub) is enterprise-priced and configuration-heavy; ERP quote modules lack document
parsing and follow-up; email-AI tools do not understand catalogs or approvals. QuotePilot's wedge is the document →
catalog → approved quote loop for small teams, with governance built in.

## Pricing assumptions (test-mode only; no live billing until approved)
| Plan | Seats | Monthly price (assumption) | Limits |
|---|---|---|---|
| Starter | up to 3 | USD 49 flat | 100 RFQs/mo, 1 catalog, no connectors |
| Team | per seat | USD 29/seat | 1,000 RFQs/mo, connectors, follow-up automation |
| Business | per seat | USD 59/seat | unlimited RFQs, workflows, AI assistant, SSO |
Hosted LLM usage is metered and capped per plan (Stage 4 entitlements). Self-hosted deployment is possible at every tier.

## Go-to-market (minimal cash)
Design partners via industry associations and LinkedIn outreach; template-driven demo workspace; open-core
self-host option for trust; content on "RFQ hygiene". No paid acquisition in the first two quarters.

## Compliance questions requiring professional review before real customer data
GDPR/UK GDPR roles (processor), UAE PDPL, India DPDP Act, data residency per customer, e-invoicing interactions,
retention periods for quotes (commercial records laws vary by country). See `PRIVACY_AND_RETENTION.md`.
