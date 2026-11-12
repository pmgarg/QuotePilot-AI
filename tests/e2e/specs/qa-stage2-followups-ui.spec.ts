/**
 * QA (independent) browser evidence for Stage 2 — quote lifecycle UI, drafts, follow-ups page, settings consent.
 * Derived from docs/PRD.md S2-F01/F04/F05/F10, S2-A01/A02/A07 and docs/USER_JOURNEYS.md. Real screens are driven
 * (Chromium against uvicorn + Next.js); API calls only set up state or read back facts a user could also see.
 */
import { expect, test, type Browser, type Page } from "@playwright/test";
import { CLEAN_RFQ, USERS, login } from "../helpers";

async function csrf(page: Page): Promise<string> {
  return (await page.context().cookies()).find((c) => c.name === "qp_csrf")!.value;
}

async function asUser(browser: Browser, email: string) {
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  await login(page, email);
  return { ctx, page };
}

/** Approved quote for a fresh Northwind customer (contact isolation between tests), approved by the approver. */
async function approvedQuote(page: Page, browser: Browser) {
  const token = await csrf(page);
  const ref = `QA-${Date.now()}`;
  const created = await page.request.post("/api/v1/customers", { data: { name: `QA Customer ${ref}`, reference: ref }, headers: { "X-CSRF-Token": token } });
  expect(created.status()).toBe(201);
  const customer = await created.json();
  const rfq = await (await page.request.post("/api/v1/rfqs", { data: { text: CLEAN_RFQ, customer_id: customer.id }, headers: { "X-CSRF-Token": token } })).json();
  const q = await (await page.request.post(`/api/v1/rfqs/${rfq.id}/generate-quote`, { headers: { "X-CSRF-Token": token } })).json();
  const sub = await (await page.request.post(`/api/v1/quotes/${q.id}/submit`, { data: { expected_lock_version: q.lock_version }, headers: { "X-CSRF-Token": token } })).json();
  const { ctx, page: ap } = await asUser(browser, USERS.approver);
  const approved = await (await ap.request.post(`/api/v1/quotes/${q.id}/approve`, { data: { expected_lock_version: sub.lock_version }, headers: { "X-CSRF-Token": await csrf(ap) } })).json();
  await ctx.close();
  expect(approved.status).toBe("approved");
  return { quote: approved, customerId: customer.id as string, customerRef: ref };
}

async function addContact(page: Page, customerId: string, email: string) {
  const r = await page.request.post(`/api/v1/customers/${customerId}/contacts`, { data: { full_name: "QA Buyer", email, is_primary: true }, headers: { "X-CSRF-Token": await csrf(page) } });
  expect(r.status()).toBe(201);
  return (await r.json()) as { id: string; email: string };
}

test.describe("QA Stage 2 — lifecycle, drafts, follow-ups and consent in the browser", () => {
  test("S2-F01/F09: claims need evidence in the UI, every transition is recorded, read-only users only see", async ({ page, browser }) => {
    await login(page, USERS.sales);
    const { quote, customerId } = await approvedQuote(page, browser);
    await addContact(page, customerId, `qa-lc-${Date.now()}@customer.example`);
    await page.goto(`/quotes/${quote.id}`);
    await expect(page.getByTestId("lifecycle-status")).toHaveText(/approved/i);
    // from approved: only "sent" (no evidence) and "lost" (evidence) exist; "lost" stays disabled without evidence
    await expect(page.getByTestId("transition-sent")).toBeEnabled();
    await expect(page.getByTestId("transition-lost")).toBeDisabled();
    await expect(page.getByTestId("transition-won")).toHaveCount(0);
    await expect(page.getByTestId("transition-acknowledged")).toHaveCount(0);
    await page.getByTestId("transition-sent").click();
    await expect(page.getByTestId("lifecycle-status")).toHaveText(/sent/i);
    await expect(page.getByTestId("fu-notice")).toContainText("Recorded");
    // from sent: acknowledgement / won / lost need evidence, awaiting customer does not
    await expect(page.getByTestId("transition-acknowledged")).toBeDisabled();
    await expect(page.getByTestId("transition-won")).toBeDisabled();
    await expect(page.getByTestId("transition-awaiting_customer")).toBeEnabled();
    await page.getByTestId("evidence").fill("customer confirmed receipt by phone on 2 Nov");
    await expect(page.getByTestId("transition-acknowledged")).toBeEnabled();
    await page.getByTestId("transition-acknowledged").click();
    await expect(page.getByTestId("lifecycle-status")).toHaveText(/acknowledged/i);
    const timeline = page.getByTestId("timeline");
    await expect(timeline).toContainText("approved → sent");
    await expect(timeline).toContainText("sent → acknowledged");
    await expect(timeline).toContainText("customer confirmed receipt by phone");
    await expect(timeline).not.toContainText("[comm_out");
    // schedules were planned (draft-only mode) and are visible on the quote
    await expect(page.locator('[data-testid^="schedule-pending"]').first()).toContainText("draft for manual send");
    // the evidence field is cleared after a recorded transition (no accidental reuse)
    await expect(page.getByTestId("evidence")).toHaveValue("");
    const events = await (await page.request.get(`/api/v1/quotes/${quote.id}/lifecycle`)).json();
    expect(events.events.map((e: { to_status: string; source: string }) => [e.to_status, e.source])).toEqual([["sent", "manual"], ["acknowledged", "manual"]]);

    // read-only: sees status, recommendation and timeline but no actions anywhere
    const { ctx, page: ro } = await asUser(browser, USERS.readonly);
    await ro.goto(`/quotes/${quote.id}`);
    await expect(ro.getByTestId("lifecycle-status")).toHaveText(/acknowledged/i);
    await expect(ro.getByTestId("recommendation")).toBeVisible();
    await expect(ro.getByTestId("timeline")).toContainText("sent → acknowledged");
    await expect(ro.locator('[data-testid^="transition-"]')).toHaveCount(0);
    await expect(ro.getByTestId("create-draft")).toHaveCount(0);
    await expect(ro.getByTestId("evidence")).toHaveCount(0);
    await ro.goto("/follow-ups");
    await expect(ro.getByTestId("schedule-table")).toBeVisible();
    await expect(ro.getByTestId("schedule-table").getByRole("button", { name: "Cancel" })).toHaveCount(0);
    await expect(ro.getByTestId("job-table")).toHaveCount(0);
    await ctx.close();
  });

  test("S2-F04/A01: the draft editor rejects unknown placeholders, discarding a draft sends nothing, sending needs the confirm", async ({ page, browser }) => {
    await login(page, USERS.sales);
    const { quote, customerId } = await approvedQuote(page, browser);
    const contact = await addContact(page, customerId, `qa-draft-${Date.now()}@customer.example`);
    await page.goto(`/quotes/${quote.id}`);
    await page.getByTestId("transition-sent").click();
    await expect(page.getByTestId("lifecycle-status")).toHaveText(/sent/i);
    await page.getByTestId("create-draft").click();
    await expect(page.getByTestId("fu-notice")).toContainText("Draft prepared");
    const draft = page.getByTestId("draft-draft");
    await expect(draft).toContainText(quote.number);
    await expect(draft).toContainText(contact.email);
    // edit with a forbidden placeholder → error, draft unchanged
    await draft.getByRole("button", { name: "Edit" }).click();
    await page.getByTestId("draft-body").fill("Hello {contact_name}, see {quote_number} — {evil_placeholder}");
    await page.getByRole("button", { name: "Save draft" }).click();
    await expect(page.getByTestId("fu-error")).toContainText(/Unknown placeholders/i);
    // a successful edit keeps the draft a draft
    await page.getByTestId("draft-body").fill("Hello {contact_name}, a short personal note about {quote_number}. {opt_out_text}");
    await page.getByRole("button", { name: "Save draft" }).click();
    await expect(page.getByTestId("fu-notice")).toContainText("Draft updated");
    await expect(page.getByTestId("draft-sent")).toHaveCount(0);
    // dismissing the confirm dialog must not send
    page.once("dialog", (d) => d.dismiss());
    await page.getByTestId("send-draft").click();
    await expect(page.getByTestId("draft-sent")).toHaveCount(0);
    let events = await (await page.request.get(`/api/v1/communication-events?quote_id=${quote.id}`)).json();
    expect(events).toEqual([]);
    // discard → cancelled, nothing left to send, nothing recorded as sent
    await draft.getByRole("button", { name: "Discard" }).click();
    await expect(page.getByTestId("draft-cancelled")).toBeVisible();
    await expect(page.getByTestId("send-draft")).toHaveCount(0);
    events = await (await page.request.get(`/api/v1/communication-events?quote_id=${quote.id}`)).json();
    expect(events).toEqual([]);
    await expect(page.getByTestId("timeline")).not.toContainText("[comm_out");
    // a fresh draft sent with the confirm → exactly one outbound fact
    await page.getByTestId("create-draft").click();
    page.once("dialog", (d) => d.accept());
    await page.getByTestId("send-draft").click();
    await expect(page.getByTestId("fu-notice")).toContainText("sent and recorded");
    await expect(page.getByTestId("draft-sent")).toHaveCount(1);
    await expect(page.getByTestId("timeline")).toContainText("[comm_out");
    events = await (await page.request.get(`/api/v1/communication-events?quote_id=${quote.id}`)).json();
    expect(events.filter((e: { kind: string }) => e.kind === "sent")).toHaveLength(1);
  });

  test("S2-F10/A07: follow-ups page lists pending follow-ups; reschedule and cancel work and cancelled ones are never processed", async ({ page, browser }) => {
    await login(page, USERS.sales);
    const { quote, customerId } = await approvedQuote(page, browser);
    await addContact(page, customerId, `qa-sched-${Date.now()}@customer.example`);
    const token = await csrf(page);
    const sent = await page.request.post(`/api/v1/quotes/${quote.id}/transition`, { data: { expected_lock_version: quote.lock_version, to_status: "sent" }, headers: { "X-CSRF-Token": token } });
    expect(sent.status()).toBe(200);
    await page.goto("/follow-ups");
    const table = page.getByTestId("schedule-table");
    const rows = table.locator("tbody tr", { has: page.locator(`a[href="/quotes/${quote.id}"]`) });
    await expect(rows.first()).toBeVisible();
    const before = await rows.count();
    expect(before).toBeGreaterThanOrEqual(2);
    await expect(rows.first()).toContainText("draft only");
    await expect(rows.first()).toContainText("pending");
    // reschedule the first follow-up of this quote to a fixed date via the prompt
    page.once("dialog", (d) => d.accept("2027-03-02T09:00"));
    await rows.first().getByRole("button", { name: "Reschedule" }).click();
    await expect(page.getByText(/2027/).first()).toBeVisible();
    const rescheduled = (await (await page.request.get(`/api/v1/follow-up/schedules?quote_id=${quote.id}`)).json()) as { id: string; due_at: string; attempt_no: number }[];
    expect(rescheduled.some((s) => s.due_at.startsWith("2027-03-02"))).toBe(true);
    // cancel one → it leaves the pending list and shows under "cancelled"
    await rows.first().getByRole("button", { name: "Cancel" }).click();
    await expect(rows).toHaveCount(before - 1);
    await page.locator("#sched-status").selectOption("cancelled");
    await expect(table.locator("tbody tr", { has: page.locator(`a[href="/quotes/${quote.id}"]`) }).first()).toContainText("cancelled");
    // observation (not asserted): the cancellation reason/actor is kept in the audit log only; the table shows the planning reason
    // the worker (run by an admin) does not touch cancelled schedules and sends nothing
    const { ctx, page: admin } = await asUser(browser, USERS.admin);
    await admin.goto("/follow-ups");
    await admin.getByRole("button", { name: "Run worker once (dev)" }).click();
    await expect(admin.getByTestId("job-table").or(admin.getByText("No jobs recorded."))).toBeVisible();
    const after = (await (await admin.request.get(`/api/v1/follow-up/schedules?status=all&quote_id=${quote.id}`)).json()) as { status: string }[];
    expect(after.filter((s) => s.status === "cancelled")).toHaveLength(1);
    expect(after.some((s) => s.status === "sent")).toBe(false);
    const events = await (await admin.request.get(`/api/v1/communication-events?quote_id=${quote.id}`)).json();
    expect(events).toEqual([]);
    await ctx.close();
  });

  test("S2-F05: automation consent is admin-only in the settings UI, recorded with a timestamp, and reversible", async ({ page, browser }) => {
    await login(page, USERS.sales);
    await page.goto("/settings");
    await expect(page.getByTestId("automation-consent")).toBeDisabled();
    await expect(page.getByTestId("policy-save")).toHaveCount(0);
    await expect(page.getByTestId("offsets")).toBeDisabled();
    const { ctx, page: admin } = await asUser(browser, USERS.admin);
    await admin.goto("/settings");
    await expect(admin.getByTestId("automation-consent")).toBeEnabled();
    const wasChecked = await admin.getByTestId("automation-consent").isChecked();
    expect(wasChecked).toBe(false); // default draft-only in the demo org
    await admin.getByTestId("automation-consent").check();
    await admin.getByTestId("policy-save").click();
    await expect(admin.getByTestId("policy-saved")).toBeVisible();
    await expect(admin.getByText(/Consent recorded/)).toBeVisible();
    let pol = await (await admin.request.get("/api/v1/follow-up/policy")).json();
    expect(pol.automation_enabled).toBe(true);
    expect(pol.automation_consented_at).toBeTruthy();
    // template with an unknown placeholder is refused with a visible error and not saved
    const subj = admin.locator('input[aria-label="Subject follow_up_1"]');
    const original = await subj.inputValue();
    await subj.fill("Quotation {quote_number} {evil}");
    await admin.locator("form", { has: subj }).getByRole("button", { name: "Save template" }).click();
    await expect(admin.getByRole("alert").filter({ hasText: /Unknown placeholders/i })).toBeVisible();
    const tpl = (await (await admin.request.get("/api/v1/follow-up/templates")).json()).find((t: { key: string }) => t.key === "follow_up_1");
    expect(tpl.subject).toBe(original);
    // withdraw consent again (leave the demo org as found)
    await admin.reload();
    await admin.getByTestId("automation-consent").uncheck();
    await admin.getByTestId("policy-save").click();
    await expect(admin.getByTestId("policy-saved")).toBeVisible();
    pol = await (await admin.request.get("/api/v1/follow-up/policy")).json();
    expect(pol.automation_enabled).toBe(false);
    expect(pol.automation_consented_at).toBeNull();
    await ctx.close();
  });

  test("S2-A02: opting a contact out in the UI stops drafts and suppresses pending follow-ups; only admins can re-enable", async ({ page, browser }) => {
    await login(page, USERS.sales);
    const { quote, customerId, customerRef } = await approvedQuote(page, browser);
    const email = `qa-optout-${Date.now()}@customer.example`;
    await addContact(page, customerId, email);
    await page.goto(`/quotes/${quote.id}`);
    await page.getByTestId("transition-sent").click();
    await expect(page.getByTestId("lifecycle-status")).toHaveText(/sent/i);
    await expect(page.getByTestId("create-draft")).toBeVisible();
    await expect(page.locator('[data-testid^="schedule-pending"]').first()).toBeVisible();
    await page.goto("/customers");
    await page.getByTestId(`contacts-${customerRef}`).click();
    const row = page.getByTestId("contact-row").filter({ hasText: email });
    await row.getByTestId("opt-out").click();
    await expect(row).toContainText("opted out (manual");
    await expect(row.getByRole("button", { name: /Re-enable/ })).toHaveCount(0); // sales cannot opt back in
    await page.goto(`/quotes/${quote.id}`);
    await expect(page.getByTestId("create-draft")).toHaveCount(0);
    await expect(page.getByTestId("recommendation")).toContainText(/opted out/);
    await expect(page.locator('[data-testid^="schedule-pending"]')).toHaveCount(0);
    await expect(page.locator('[data-testid^="schedule-suppressed"]').first()).toBeVisible();
    const { ctx, page: admin } = await asUser(browser, USERS.admin);
    await admin.goto("/customers");
    await admin.getByTestId(`contacts-${customerRef}`).click();
    const adminRow = admin.getByTestId("contact-row").filter({ hasText: email });
    await expect(adminRow.getByRole("button", { name: /Re-enable/ })).toBeVisible();
    await ctx.close();
  });
});
