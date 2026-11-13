import { expect, test, type Page } from "@playwright/test";
import { CLEAN_RFQ, USERS, login } from "../helpers";

async function csrf(page: Page) {
  return (await page.context().cookies()).find((c) => c.name === "qp_csrf")!.value;
}

async function approvedQuote(page: Page, browser: import("@playwright/test").Browser) {
  const token = await csrf(page);
  const customers = await (await page.request.get("/api/v1/customers")).json();
  const rfq = await (await page.request.post("/api/v1/rfqs", { data: { text: CLEAN_RFQ, customer_id: customers[0].id }, headers: { "X-CSRF-Token": token } })).json();
  const qRes = await page.request.post(`/api/v1/rfqs/${rfq.id}/generate-quote`, { headers: { "X-CSRF-Token": token } });
  expect(qRes.status(), await qRes.text()).toBe(201);
  const q = await qRes.json();
  const sub = await (await page.request.post(`/api/v1/quotes/${q.id}/submit`, { data: { expected_lock_version: q.lock_version }, headers: { "X-CSRF-Token": token } })).json();
  const ctx = await browser.newContext();
  const ap = await ctx.newPage();
  await login(ap, USERS.approver);
  const approved = await (await ap.request.post(`/api/v1/quotes/${q.id}/approve`, { data: { expected_lock_version: sub.lock_version }, headers: { "X-CSRF-Token": await csrf(ap) } })).json();
  await ctx.close();
  return approved as { id: string; number: string };
}

test.describe("Stage 4 — governed sales operations", () => {
  test("S4-F02/F03: assistant reads a quote, proposes an order that only runs after approval in the command center", async ({ page, browser }) => {
    await login(page, USERS.sales);
    const q = await approvedQuote(page, browser);
    await page.goto("/assistant");
    await expect(page.getByTestId("assistant-tools")).toContainText("get_quote");
    await expect(page.getByTestId("assistant-tools")).not.toContainText("export_customers");
    await page.getByTestId("assistant-input").fill(`show quote ${q.number}`);
    await page.getByTestId("assistant-send").click();
    await expect(page.getByTestId("assistant-action").first()).toContainText("executed");
    await expect(page.getByTestId("assistant-messages")).toContainText(q.number);
    await page.getByTestId("assistant-input").fill(`convert ${q.number} to an order`);
    await page.getByTestId("assistant-send").click();
    await expect(page.getByTestId("assistant-action").last()).toContainText(/proposed/i);
    const before = await (await page.request.get("/api/v1/orders")).json();
    expect(before.filter((o: { quote_id: string }) => o.quote_id === q.id)).toHaveLength(0);
    await page.goto("/command-center");
    await expect(page.getByTestId("cc-actions_awaiting_approval")).toContainText(/[1-9]/);
    await page.getByTestId("approve-action-propose_order").first().click();
    await expect(page.getByTestId("cc-notice")).toContainText(/executed/);
    const after = await (await page.request.get("/api/v1/orders")).json();
    expect(after.filter((o: { quote_id: string; status: string }) => o.quote_id === q.id && o.status === "proposed")).toHaveLength(1);
  });

  test("S4-F05/A09 and S4-A07: dry run has no side effects; the kill switch stops live automation", async ({ page, browser }) => {
    await login(page, USERS.sales);
    const q = await approvedQuote(page, browser);
    await page.goto("/workflows");
    await expect(page.getByTestId("forbidden")).toBeVisible(); // sales: forbidden state
    const ctx = await browser.newContext();
    const admin = await ctx.newPage();
    await login(admin, USERS.admin);
    await admin.goto("/workflows");
    const key = `e2e-${Date.now()}`;
    await admin.locator("#wf-key").fill(key);
    await admin.getByTestId("wf-validate").click();
    await expect(admin.getByTestId("wf-validation")).toContainText(/Valid/);
    await admin.getByTestId("wf-save").click();
    await expect(admin.getByTestId("wf-list")).toContainText(key);
    const ctxInput = admin.locator(`[id^="ctx-"]`).first();
    await ctxInput.fill(JSON.stringify({ quote_number: q.number }));
    await admin.getByTestId(`wf-dry-${key}`).click();
    await expect(admin.getByTestId("wf-runs")).toContainText("would execute");
    const orders = await (await admin.request.get("/api/v1/orders")).json();
    expect(orders.filter((o: { quote_id: string }) => o.quote_id === q.id)).toHaveLength(0);
    await admin.goto("/platform");
    await admin.locator("#ks-reason").fill("e2e drill");
    await admin.getByTestId("ks-engage").click();
    await expect(admin.getByTestId("ks-engaged")).toContainText("e2e drill");
    await page.goto("/assistant");
    await page.getByTestId("assistant-input").fill(`show quote ${q.number}`);
    await page.getByTestId("assistant-send").click();
    await expect(page.getByTestId("assistant-error")).toContainText(/kill switch/i);
    await admin.getByTestId("ks-release").click();
    await expect(admin.getByTestId("platform-notice")).toContainText(/released/i);
    await ctx.close();
  });
});
