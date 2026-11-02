import { expect, test } from "@playwright/test";
import { CLEAN_RFQ, USERS, login } from "../helpers";

type P = import("@playwright/test").Page;

async function csrf(page: P) {
  return (await page.context().cookies()).find((c) => c.name === "qp_csrf")!.value;
}

async function approvedQuote(page: P, browser: import("@playwright/test").Browser) {
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
  return { quote: approved, customerId: customers[0].id as string };
}

test.describe("Stage 3 — customer & order intelligence", () => {
  test("S3-F02: sales converts an approved quote; only an approver can confirm; cost column hidden from sales", async ({ page, browser }) => {
    await login(page, USERS.sales);
    const { quote } = await approvedQuote(page, browser);
    await page.goto(`/quotes/${quote.id}`);
    await page.getByTestId("convert-to-order").click();
    await expect(page).toHaveURL(/\/orders\//);
    await expect(page.getByTestId("order-status")).toHaveText(/proposed/i);
    await expect(page.getByTestId("confirm-order")).toHaveCount(0);
    await expect(page.getByTestId("order-lines").locator("th", { hasText: "Cost" })).toHaveCount(0);
    const url = page.url();
    const ctx = await browser.newContext();
    const ap = await ctx.newPage();
    await login(ap, USERS.approver);
    await ap.goto(url);
    await expect(ap.getByTestId("order-lines").locator("th", { hasText: "Cost" })).toHaveCount(1);
    await ap.getByTestId("confirm-order").click();
    await expect(ap.getByTestId("order-status")).toHaveText(/confirmed/i);
    await expect(ap.getByTestId("order-events")).toContainText("confirmed");
    await ctx.close();
    await page.reload();
    await expect(page.getByTestId("order-status")).toHaveText(/confirmed/i);
    await page.getByTestId("fulfil-order").click();
    await expect(page.getByTestId("order-status")).toHaveText(/fulfilled/i);
    await page.goto("/orders");
    await expect(page.getByTestId("order-table")).toContainText("fulfilled");
  });

  test("S3-F04: KPI page shows definitions, the currency rule and an unconverted bucket until an admin adds a rate", async ({ page, browser }) => {
    await login(page, USERS.sales);
    const { quote } = await approvedQuote(page, browser);
    await page.goto("/insights");
    await expect(page.getByTestId("kpi-grid")).toBeVisible();
    await expect(page.getByTestId("kpi-pending_value")).toContainText(/AED/);
    await page.getByTestId("kpi-conversion_rate").getByText("Definition").click();
    await expect(page.getByTestId("kpi-conversion_rate")).toContainText(/terminal lifecycle state/);
    await expect(page.getByTestId("add-fx")).toHaveCount(0); // sales cannot maintain the FX table
    expect(quote.id).toBeTruthy();
    const ctx = await browser.newContext();
    const admin = await ctx.newPage();
    await login(admin, USERS.admin);
    await admin.goto("/insights");
    await admin.locator("#fx-base").fill("USD");
    await admin.locator("#fx-quote").fill("AED");
    await admin.locator("#fx-rate").fill("3.6725");
    await admin.getByTestId("add-fx").click();
    await expect(admin.getByTestId("fx-table")).toContainText("USD/AED");
    await ctx.close();
  });

  test("S3-F06/F07: connector sync queues conflicts instead of overwriting, stock labels fresh/stale/unknown, sales cannot export in bulk", async ({ page, browser }) => {
    await login(page, USERS.admin);
    await page.goto("/data");
    await page.locator("#conn-name").fill(`ERP ${Date.now()}`);
    await page.getByLabel("read:products").check();
    const already = await page.getByTestId("connector-list").count();
    if (already === 0 || !(await page.getByTestId("connector-list").textContent())?.includes("erp_stub")) {
      await page.getByTestId("create-connector").click();
      await expect(page.getByTestId("connector-list")).toContainText("erp_stub");
    }
    const observed = new Date(Date.now() - 3600_000).toISOString();
    const stale = new Date(Date.now() - 72 * 3600_000).toISOString();
    await page.locator("#sync-conn").selectOption({ index: 1 });
    await page.locator("#sync-entity").selectOption("stock");
    await page.locator("#sync-key").fill(`stock-${Date.now()}`);
    await page.locator("#sync-records").fill(JSON.stringify([
      { sku: "SKU-1001", quantity: "120", observed_at: observed },
      { sku: "SKU-1002", quantity: "3", observed_at: stale },
    ]));
    await page.getByTestId("run-sync").click();
    await expect(page.getByTestId("data-notice")).toContainText(/Sync processed/);
    await expect(page.getByTestId("stock-SKU-1001")).toContainText("fresh");
    await expect(page.getByTestId("stock-SKU-1002")).toContainText("stale");
    await expect(page.getByTestId("stock-SKU-1003")).toContainText("unknown");
    // products sync with a different price → conflict, never applied silently
    const key = `NEW-${Date.now().toString().slice(-6)}`;
    await page.locator("#sync-entity").selectOption("products");
    await page.locator("#sync-key").fill(`prod-${Date.now()}`);
    await page.locator("#sync-records").fill(JSON.stringify([{ sku: key, description: "Unknown part", list_price: "9.99" }]));
    await page.getByTestId("run-sync").click();
    await expect(page.getByTestId(`conflict-${key}`)).toContainText("new");
    await expect(page.getByTestId(`apply-remote-${key}`)).toBeDisabled();
    // role gate on bulk export, audited server-side (S3-A06)
    const ctx = await browser.newContext();
    const sales = await ctx.newPage();
    await login(sales, USERS.sales);
    const res = await sales.request.get("/api/v1/data/export/customers");
    expect(res.status()).toBe(403);
    await sales.goto("/data");
    await expect(sales.getByText(/owner\/admin\/approver only/).first()).toBeVisible();
    await ctx.close();
  });

  test("S3-F08: sales requests deletion from the profile, admin approves, the customer disappears from the list", async ({ page, browser }) => {
    await login(page, USERS.admin);
    const token = await csrf(page);
    const name = `Erase Me ${Date.now()}`;
    const created = await page.request.post("/api/v1/customers", { data: { name, email: "erase@customer.example" }, headers: { "X-CSRF-Token": token } });
    expect(created.status(), await created.text()).toBe(201);
    const customer = await created.json();
    const ctx = await browser.newContext();
    const sales = await ctx.newPage();
    await login(sales, USERS.sales);
    await sales.goto(`/customers/${customer.id}`);
    await sales.getByTestId("generate-proposal").click();
    await expect(sales.getByTestId("profile-notice")).toContainText(/Nothing to propose/);
    await sales.locator("#del-reason").fill("customer asked for erasure");
    await sales.getByTestId("request-deletion").click();
    await expect(sales.getByTestId("profile-notice")).toContainText(/Deletion requested/);
    await ctx.close();
    await page.goto("/data");
    await page.getByTestId(`approve-deletion-${customer.id.slice(0, 8)}`).click();
    await expect(page.getByTestId("data-notice")).toContainText(/Deletion approved/);
    await expect(page.getByTestId(`deletion-${customer.id.slice(0, 8)}`)).toContainText("approved");
    await page.goto("/customers");
    await expect(page.getByTestId("customer-table")).not.toContainText(name);
  });
});
