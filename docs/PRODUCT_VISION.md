# Product Vision — QuotePilot AI

**One line:** Turn emailed RFQs into verified, approved quotations in minutes, then keep every quote moving to an order —
for B2B distributors with 1–20 sales people who run on spreadsheets today.

## Problem
Small distributors receive requests for quotation (RFQ) as PDFs, spreadsheets and free text. A salesperson re-types
items into a price list spreadsheet, guesses SKUs, applies margins by memory, and emails a quote. Errors (wrong unit,
stale price, missing approval) are common, follow-up is ad hoc, and nobody can see pipeline or conversion.

## Product principles
1. **Human-verified, AI-assisted.** The system proposes; a person verifies; an approver approves. Nothing is sent or
   priced on AI authority.
2. **Never invent.** Unknown price, stock or customer facts are shown as unknown. Finalization is blocked or requires
   an audited privileged override.
3. **Deterministic money.** `Decimal` arithmetic, configurable rounding/tax/discount rules, attributed totals.
4. **Tenant isolation by construction.** Organization scoping in every query plus PostgreSQL row-level security.
5. **Run anywhere for free first.** Docker Compose, open-source components, no paid API required for the core path.
6. **Commercially usable, narrow launch scope.** International/multi-currency aware, configurable, but Stage 1 is
   RFQ → quotation only.

## Four connected stages
| Stage | Outcome for the customer | Key capability |
|---|---|---|
| 1 RFQ-to-Quotation | Accurate quote in minutes, approved, branded PDF | Parsing, matching, pricing rules, approval, versioning |
| 2 Follow-up Assistant | No quote forgotten; compliant, personal follow-ups | Lifecycle tracking, drafts, consented sending, inbox sync |
| 3 Customer & Order Intelligence | Know your customers; convert and repeat | Profiles, quote→order, KPIs, imports/exports, connectors |
| 4 Governed AI Sales Ops | One governed command center with bounded AI | Policy-scoped tools, workflows, budgets, billing, admin |

## Success measures (first 6 months after Stage 1 launch)
- Median RFQ-to-approved-quote time < 15 minutes for RFQs ≤ 25 lines (measured in-app).
- ≥ 95 % of quote lines priced from catalog without manual price entry on exact-SKU RFQs.
- Zero pricing or tenant-isolation defects in production (tracked incidents).
- 3 design-partner distributors using it weekly (synthetic demo until a partner signs a data-processing agreement).

## Out of scope (all stages)
Payments collection, warehouse management, accounting ledgers, consumer e-commerce, autonomous unsupervised sending.
