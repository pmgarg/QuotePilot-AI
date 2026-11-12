/**
 * QA (independent) browser evidence — Stage 1 behaviour with the Stage 2 panel present (editor, approval,
 * new version, withdraw) and cross-tenant isolation of Stage 2 screens/resources (S2-A04, S1-A14/A15/A16/A20).
 */
import { expect, test, type Browser, type Page } from "@playwright/test";
import { USERS, login } from "../helpers";

const TWO_LINE_RFQ = "1. 500 pcs SKU-1001 Hex bolt M8x40 zinc plated\n2. 500 pcs SKU-1003 Hex nut M8 zinc plated\n";

async function csrf(page: Page): Promise<string> {
  return (await page.context().cookies()).find((c) => c.name === "qp_csrf")!.value;
}

async function asUser(browser: Browser, email: string) {
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  await login(page, email);
  return { ctx, page };
}

async function pasteRfqAndGenerate(page: Page, reference: string): Promise<string> {
  await page.goto("/rfqs/new");
  await page.getByTestId("mode-text").check();
  await page.getByTestId("rfq-text").fill(TWO_LINE_RFQ);
  await page.getByTestId("rfq-customer").selectOption({ index: 1 });
  await page.getByTestId("rfq-reference").fill(reference);
  await page.getByTestId("rfq-submit").click();
  await expect(page.getByTestId("rfq-status")).toHaveText(/parsed/i);
  await page.getByTestId("generate-quote").click();
  await expect(page).toHaveURL(/\/quotes\//);
  await expect(page.getByTestId("quote-status")).toHaveText(/draft/i);
  return page.url().split("/quotes/")[1];
}

async function approveViaUi(browser: Browser, quoteId: string) {
  const { ctx, page } = await asUser(browser, USERS.approver);
  await page.goto(`/quotes/${quoteId}`);
  await page.getByTestId("decision-reason").fill("ok");
  await page.getByTestId("approve-quote").click();
  await expect(page.getByTestId("quote-status")).toHaveText(/approved/i);
  await ctx.close();
}

test.describe("QA Stage 2 — Stage 1 regressions and tenant isolation in the browser", () => {
  test("editor → submit → approve → follow-up panel → new version hides the panel → editor and approval still work", async ({ page, browser }) => {
    await login(page, USERS.sales);
    const quoteId = await pasteRfqAndGenerate(page, `QA-S2REG-${Date.now()}`);
    // no sales lifecycle before approval
    await expect(page.getByTestId("lifecycle-status")).toHaveCount(0);
    await expect(page.getByTestId("line-total-1")).toHaveText("446.25");
    await page.getByTestId("discount-1").fill("10");
    await page.getByTestId("save-draft").click();
    await expect(page.getByTestId("quote-notice")).toContainText("saved");
    await expect(page.getByTestId("line-total-1")).toHaveText("401.63"); // 500 × 0.85 × 0.9 = 382.50 + 5 % = 401.625 → HALF_UP
    await page.getByTestId("submit-quote").click();
    await expect(page.getByTestId("version-status")).toHaveText(/pending approval/i);
    await expect(page.getByTestId("lifecycle-status")).toHaveCount(0);
    await approveViaUi(browser, quoteId);
    await page.reload();
    await expect(page.getByTestId("quote-status")).toHaveText(/approved/i);
    await expect(page.getByTestId("lifecycle-status")).toHaveText(/approved/i);
    await expect(page.getByTestId("audit-table")).toContainText("quote.approve");
    const pdf = await page.request.get(`/api/v1/quotes/${quoteId}/versions/1/pdf`);
    expect(pdf.status()).toBe(200);
    // new version: back to an editable draft, lifecycle panel disappears, approved v1 remains downloadable
    await page.getByTestId("new-version").click();
    await expect(page.getByTestId("quote-notice")).toContainText("New draft version");
    await expect(page.getByTestId("version-status")).toHaveText(/draft/i);
    await expect(page.getByTestId("lifecycle-status")).toHaveCount(0);
    await expect(page.getByTestId("save-draft")).toBeVisible();
    await page.locator('input[aria-label="Quantity 1"]').fill("600");
    await page.getByTestId("save-draft").click();
    await expect(page.getByTestId("quote-notice")).toContainText("saved");
    await expect(page.getByTestId("submit-quote")).toBeEnabled();
    expect((await page.request.get(`/api/v1/quotes/${quoteId}/versions/1/pdf`)).status()).toBe(200);
    // S1-A16 concurrency still enforced on the v2 draft
    const { ctx, page: other } = await asUser(browser, USERS.admin);
    await other.goto(`/quotes/${quoteId}`);
    await other.getByTestId("discount-1").fill("5");
    await other.getByTestId("save-draft").click();
    await expect(other.getByTestId("quote-notice")).toContainText("saved");
    await page.getByTestId("discount-1").fill("7");
    await page.getByTestId("save-draft").click();
    await expect(page.getByTestId("conflict-banner")).toBeVisible();
    await ctx.close();
  });

  test("withdrawing an approved-and-sent quote from the UI closes the lifecycle and refuses exports", async ({ page, browser }) => {
    await login(page, USERS.sales);
    const quoteId = await pasteRfqAndGenerate(page, `QA-S2WD-${Date.now()}`);
    await page.getByTestId("submit-quote").click();
    await approveViaUi(browser, quoteId);
    await page.reload();
    await page.getByTestId("transition-sent").click();
    await expect(page.getByTestId("lifecycle-status")).toHaveText(/sent/i);
    await expect(page.getByRole("button", { name: "Withdraw" })).toHaveCount(0); // sales may not withdraw
    const { ctx, page: appr } = await asUser(browser, USERS.approver);
    await appr.goto(`/quotes/${quoteId}`);
    await appr.locator("#withdraw-reason").fill("pricing error found after sending");
    await appr.getByRole("button", { name: "Withdraw" }).click();
    await expect(appr.getByTestId("quote-notice")).toContainText("withdrawn");
    await expect(appr.getByTestId("quote-status")).toHaveText(/withdrawn/i);
    await expect(appr.getByTestId("lifecycle-status")).toHaveCount(0);
    expect((await appr.request.get(`/api/v1/quotes/${quoteId}/versions/1/pdf`)).status()).toBe(409);
    const lc = await (await appr.request.get(`/api/v1/quotes/${quoteId}/lifecycle`)).json();
    expect(lc.status).toBe("withdrawn");
    expect(lc.allowed_transitions).toEqual({});
    await ctx.close();
    await page.reload();
    await expect(page.getByText("Withdrawn", { exact: true })).toBeVisible();
    await expect(page.getByTestId("transition-sent")).toHaveCount(0);
  });

  test("S2-A04: another tenant cannot see the quote, its lifecycle, contacts, drafts or follow-ups", async ({ page, browser }) => {
    await login(page, USERS.sales);
    // a dedicated customer, so this test never changes the seeded customers' contact lists other specs rely on
    const token = await csrf(page);
    const ref = `QA-S2TEN-${Date.now()}`;
    const customer = await (await page.request.post("/api/v1/customers", { data: { name: `QA Tenancy ${ref}`, reference: ref }, headers: { "X-CSRF-Token": token } })).json();
    const customers = [customer];
    const rfq = await (await page.request.post("/api/v1/rfqs", { data: { text: TWO_LINE_RFQ, customer_id: customer.id }, headers: { "X-CSRF-Token": token } })).json();
    const quoteId = (await (await page.request.post(`/api/v1/rfqs/${rfq.id}/generate-quote`, { headers: { "X-CSRF-Token": token } })).json()).id as string;
    await page.goto(`/quotes/${quoteId}`);
    await page.getByTestId("submit-quote").click();
    await expect(page.getByTestId("version-status")).toHaveText(/pending approval/i);
    await approveViaUi(browser, quoteId);
    const contact = await (await page.request.post(`/api/v1/customers/${customers[0].id}/contacts`, { data: { full_name: "Isolated", email: `qa-iso-${Date.now()}@customer.example`, is_primary: true }, headers: { "X-CSRF-Token": token } })).json();
    const q = await (await page.request.get(`/api/v1/quotes/${quoteId}`)).json();
    expect((await page.request.post(`/api/v1/quotes/${quoteId}/transition`, { data: { expected_lock_version: q.lock_version, to_status: "sent" }, headers: { "X-CSRF-Token": token } })).status()).toBe(200);
    const draft = await (await page.request.post(`/api/v1/quotes/${quoteId}/drafts`, { data: { contact_id: contact.id }, headers: { "X-CSRF-Token": token } })).json();
    const schedules = await (await page.request.get(`/api/v1/follow-up/schedules?quote_id=${quoteId}`)).json();
    expect(schedules.length).toBeGreaterThan(0);

    const { ctx, page: other } = await asUser(browser, USERS.otherOwner);
    await other.goto(`/quotes/${quoteId}`);
    await expect(other.getByRole("alert").filter({ hasText: /not found/i })).toBeVisible();
    await expect(other.getByTestId("lifecycle-status")).toHaveCount(0);
    await other.goto("/follow-ups");
    await expect(other.locator(`a[href="/quotes/${quoteId}"]`)).toHaveCount(0);
    const t = await csrf(other);
    const probes: [string, string, Record<string, unknown> | undefined][] = [
      ["GET", `/api/v1/quotes/${quoteId}/lifecycle`, undefined],
      ["GET", `/api/v1/customers/${customers[0].id}/contacts`, undefined],
      ["GET", `/api/v1/drafts/${draft.id}`, undefined],
      ["POST", `/api/v1/drafts/${draft.id}/send`, undefined],
      ["POST", `/api/v1/follow-up/schedules/${schedules[0].id}/cancel`, {}],
      ["POST", `/api/v1/contacts/${contact.id}/opt-out`, { opted_out: true }],
      ["POST", `/api/v1/quotes/${quoteId}/transition`, { expected_lock_version: 0, to_status: "won", evidence: "x" }],
    ];
    for (const [method, url, data] of probes) {
      const r = await other.request.fetch(url, { method, data, headers: { "X-CSRF-Token": t } });
      expect(r.status(), `${method} ${url}`).toBe(404);
    }
    expect(await (await other.request.get(`/api/v1/communication-events?quote_id=${quoteId}`)).json()).toEqual([]);
    await ctx.close();
    // nothing changed on the owner's side
    const after = await (await page.request.get(`/api/v1/quotes/${quoteId}/lifecycle`)).json();
    expect(after.status).toBe("sent");
    expect(after.drafts[0].status).toBe("draft");
    expect(after.schedules.every((s: { status: string }) => s.status === "pending")).toBe(true);
  });
});
