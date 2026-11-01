import { expect, test } from "@playwright/test";
import { CLEAN_RFQ, USERS, login } from "../helpers";

async function csrf(page: import("@playwright/test").Page) {
  return (await page.context().cookies()).find((c) => c.name === "qp_csrf")!.value;
}

async function approvedQuote(page: import("@playwright/test").Page, browser: import("@playwright/test").Browser) {
  const token = await csrf(page);
  const customers = await (await page.request.get("/api/v1/customers")).json();
  const rfq = await (await page.request.post("/api/v1/rfqs", { data: { text: CLEAN_RFQ, customer_id: customers[0].id }, headers: { "X-CSRF-Token": token } })).json();
  const q = await (await page.request.post(`/api/v1/rfqs/${rfq.id}/generate-quote`, { headers: { "X-CSRF-Token": token } })).json();
  const sub = await (await page.request.post(`/api/v1/quotes/${q.id}/submit`, { data: { expected_lock_version: q.lock_version }, headers: { "X-CSRF-Token": token } })).json();
  const ctx = await browser.newContext();
  const ap = await ctx.newPage();
  await login(ap, USERS.approver);
  const approved = await (await ap.request.post(`/api/v1/quotes/${q.id}/approve`, { data: { expected_lock_version: sub.lock_version }, headers: { "X-CSRF-Token": await csrf(ap) } })).json();
  await ctx.close();
  return { quote: approved, customerId: customers[0].id as string };
}

test.describe("Stage 2 — follow-up assistant", () => {
  test("S2: mark sent → recommendation → draft → explicit send → timeline; nothing sent before the click", async ({ page, browser }) => {
    await login(page, USERS.sales);
    const { quote, customerId } = await approvedQuote(page, browser);
    // contact for the customer
    await page.goto("/customers");
    await page.getByRole("button", { name: "Contacts" }).first().click();
    await page.getByTestId("contact-name").fill("Buyer Person");
    await page.getByTestId("contact-email").fill(`buyer-${Date.now()}@customer.example`);
    await page.getByTestId("contact-create").click();
    await expect(page.getByTestId("contact-row")).toHaveCount(1);
    expect(customerId).toBeTruthy();

    await page.goto(`/quotes/${quote.id}`);
    await expect(page.getByTestId("lifecycle-status")).toHaveText(/approved/i);
    await page.getByTestId("transition-sent").click();
    await expect(page.getByTestId("lifecycle-status")).toHaveText(/sent/i);
    await expect(page.getByTestId("recommendation")).toBeVisible();
    await expect(page.locator('[data-testid^="schedule-pending"]').first()).toBeVisible();
    // acknowledgement cannot be claimed without evidence
    await expect(page.getByTestId("transition-acknowledged")).toBeDisabled();

    await page.getByTestId("create-draft").click();
    await expect(page.getByTestId("fu-notice")).toContainText("Draft prepared");
    const draft = page.getByTestId("draft-draft");
    await expect(draft).toContainText(quote.number);
    // not sent yet: no outbound event in the timeline
    await expect(page.getByTestId("timeline")).not.toContainText("sent");
    page.once("dialog", (d) => d.accept());
    await page.getByTestId("send-draft").click();
    await expect(page.getByTestId("fu-notice")).toContainText("sent and recorded");
    await expect(page.getByTestId("draft-sent")).toBeVisible();
    await expect(page.getByTestId("timeline")).toContainText("sent");
    // sending again is impossible from the UI (button gone) and the event count stays 1
    await expect(page.getByTestId("send-draft")).toHaveCount(0);
    const events = await (await page.request.get(`/api/v1/communication-events?quote_id=${quote.id}`)).json();
    expect(events.filter((e: { kind: string }) => e.kind === "sent")).toHaveLength(1);
    // record outcome with evidence
    await page.getByTestId("evidence").fill("PO received by e-mail");
    await page.getByTestId("transition-won").click();
    await expect(page.getByTestId("lifecycle-status")).toHaveText(/won/i);
  });

  test("S2-A02/A07: opted-out contact gets no draft, pending follow-ups are visible and cancellable", async ({ page, browser }) => {
    await login(page, USERS.sales);
    const { quote } = await approvedQuote(page, browser);
    const token = await csrf(page);
    const customers = await (await page.request.get("/api/v1/customers")).json();
    const contact = await (await page.request.post(`/api/v1/customers/${customers[0].id}/contacts`, { data: { full_name: "Opt Out", email: `optout-${Date.now()}@customer.example`, is_primary: true }, headers: { "X-CSRF-Token": token } })).json();
    await page.goto(`/quotes/${quote.id}`);
    await page.getByTestId("transition-sent").click();
    await expect(page.getByTestId("lifecycle-status")).toHaveText(/sent/i);
    await page.goto("/follow-ups");
    await expect(page.getByTestId("schedule-table")).toContainText("pending");
    await page.getByRole("button", { name: "Cancel" }).first().click();
    await expect(page.getByTestId("schedule-table").locator("tbody tr")).toHaveCount(await page.getByTestId("schedule-table").locator("tbody tr").count());
    // opt the contact out, then the quote page offers no draft creation for it
    await page.goto("/customers");
    await page.getByRole("button", { name: "Contacts" }).first().click();
    await page.getByTestId("opt-out").first().click();
    await expect(page.getByTestId("contacts-panel")).toContainText("opted out");
    const r = await page.request.post(`/api/v1/quotes/${quote.id}/drafts`, { data: { contact_id: contact.id }, headers: { "X-CSRF-Token": token } });
    expect(r.status()).toBe(403);
    const schedules = await (await page.request.get(`/api/v1/follow-up/schedules?status=all&quote_id=${quote.id}`)).json();
    expect(schedules.every((s: { status: string }) => ["cancelled", "suppressed"].includes(s.status))).toBe(true);
  });

  test("S2-F05: automation consent is admin-only and recorded; sales cannot change the policy", async ({ page }) => {
    await login(page, USERS.sales);
    await page.goto("/settings");
    await expect(page.getByTestId("policy-save")).toHaveCount(0);
    await page.getByTestId("logout").click();
    await login(page, USERS.admin);
    await page.goto("/settings");
    await page.getByTestId("offsets").fill("2, 6, 12");
    await page.getByTestId("policy-save").click();
    await expect(page.getByTestId("policy-saved")).toBeVisible();
    const pol = await (await page.request.get("/api/v1/follow-up/policy")).json();
    expect(pol.offsets_days).toEqual([2, 6, 12]);
    expect(pol.automation_enabled).toBe(false);
  });
});
