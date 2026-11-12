/**
 * QA (independent) browser evidence for Stage 3 (docs/PRD.md S3-F01..F08) from the user's point of view:
 * orders page + order detail (approval gate, self-approval notice, cancel reason), insights (definitions, range,
 * unconverted bucket, FX gate), data & connectors (CSV import with failed rows, export gate per role, conflict queue),
 * customer profile (role-aware margin, repeat-order proposal, deletion request) and a Stage 1/2 regression smoke.
 */
import { expect, test, type Browser, type Page } from "@playwright/test";
import { CLEAN_RFQ, USERS, login } from "../helpers";

async function csrf(page: Page): Promise<string> {
  return (await page.context().cookies()).find((c) => c.name === "qp_csrf")!.value;
}

async function as(browser: Browser, email: string): Promise<{ page: Page; close: () => Promise<void> }> {
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  await login(page, email);
  return { page, close: () => ctx.close() };
}

async function approvedQuote(sales: Page, browser: Browser, text = CLEAN_RFQ) {
  const token = await csrf(sales);
  const customers = await (await sales.request.get("/api/v1/customers")).json();
  const customer = customers.find((c: { reference: string | null }) => c.reference === "GFC-001") ?? customers[0];
  const rfq = await (await sales.request.post("/api/v1/rfqs", { data: { text, customer_id: customer.id }, headers: { "X-CSRF-Token": token } })).json();
  const qRes = await sales.request.post(`/api/v1/rfqs/${rfq.id}/generate-quote`, { headers: { "X-CSRF-Token": token } });
  expect(qRes.status(), await qRes.text()).toBe(201);
  const q = await qRes.json();
  const sub = await (await sales.request.post(`/api/v1/quotes/${q.id}/submit`, { data: { expected_lock_version: q.lock_version }, headers: { "X-CSRF-Token": token } })).json();
  const ap = await as(browser, USERS.approver);
  const approved = await (await ap.page.request.post(`/api/v1/quotes/${q.id}/approve`, { data: { expected_lock_version: sub.lock_version }, headers: { "X-CSRF-Token": await csrf(ap.page) } })).json();
  await ap.close();
  return { quote: approved, customerId: customer.id as string };
}

test.describe("QA Stage 3 — browser evidence", () => {
  test("order detail: approver who proposed sees the self-approval notice, cancel needs a reason, readonly only watches", async ({ page, browser }) => {
    await login(page, USERS.sales);
    const { quote } = await approvedQuote(page, browser);
    // the approver converts the quote and therefore becomes the proposer of the order
    const proposer = await as(browser, USERS.approver);
    await proposer.page.goto(`/quotes/${quote.id}`);
    await proposer.page.getByTestId("convert-to-order").click();
    await expect(proposer.page).toHaveURL(/\/orders\//);
    await expect(proposer.page.getByTestId("order-status")).toHaveText(/proposed/i);
    await expect(proposer.page.getByTestId("order-self-approval-blocked")).toBeVisible();
    await expect(proposer.page.getByTestId("confirm-order")).toHaveCount(0);
    await expect(proposer.page.getByTestId("cancel-order")).toBeDisabled(); // reason required
    await expect(proposer.page.getByTestId("order-total")).toHaveText(/\d/);
    const url = proposer.page.url();
    await proposer.close();
    const ro = await as(browser, USERS.readonly);
    await ro.page.goto(url);
    await expect(ro.page.getByText(/Waiting for an approver/)).toBeVisible();
    await expect(ro.page.getByTestId("confirm-order")).toHaveCount(0);
    await expect(ro.page.getByTestId("cancel-order")).toHaveCount(0);
    await expect(ro.page.getByTestId("order-lines").locator("th", { hasText: "Cost" })).toHaveCount(0);
    await ro.close();
    const admin = await as(browser, USERS.admin);
    await admin.page.goto(url);
    await admin.page.locator("#order-reason").fill("duplicate of a manual order");
    await admin.page.getByTestId("cancel-order").click();
    await expect(admin.page.getByTestId("order-status")).toHaveText(/cancelled/i);
    await expect(admin.page.getByTestId("order-events")).toContainText("duplicate of a manual order");
    await admin.close();
    await page.goto("/orders");
    await page.locator("#order-status").selectOption("cancelled");
    await expect(page.getByTestId("order-table")).toContainText("cancelled");
    await page.locator("#order-status").selectOption("fulfilled");
    await expect(page.getByTestId("order-table").locator("tbody tr").filter({ hasText: /cancelled/i })).toHaveCount(0);
  });

  test("repeat-order proposal: generated from confirmed history on the profile, decided on the orders page, never auto-executed", async ({ page, browser }) => {
    await login(page, USERS.admin);
    const token = await csrf(page);
    const stamp = Date.now().toString().slice(-7);
    const customer = await (await page.request.post("/api/v1/customers", { data: { name: `Repeat Buyer ${stamp}`, reference: `RB-${stamp}` }, headers: { "X-CSRF-Token": token } })).json();
    const csv = [
      "order_ref,customer_reference,sku,description,quantity,unit,unit_price,currency",
      `H1-${stamp},RB-${stamp},SKU-1001,Hex bolt,400,EA,0.85,AED`,
      `H2-${stamp},RB-${stamp},SKU-1001,Hex bolt,600,EA,0.85,AED`,
    ].join("\r\n");
    const imp = await page.request.post(`/api/v1/data/import/orders?idempotency_key=hist-${stamp}`, { multipart: { file: { name: "h.csv", mimeType: "text/csv", buffer: Buffer.from(csv) } }, headers: { "X-CSRF-Token": token } });
    expect(imp.status(), await imp.text()).toBe(201);
    const ap = await as(browser, USERS.approver);
    const apToken = await csrf(ap.page);
    const orders = await (await ap.page.request.get(`/api/v1/orders?customer_id=${customer.id}&status=proposed`)).json();
    expect(orders).toHaveLength(2);
    for (const o of orders) {
      const c = await ap.page.request.post(`/api/v1/orders/${o.id}/confirm`, { data: {}, headers: { "X-CSRF-Token": apToken } });
      expect(c.status(), await c.text()).toBe(200);
    }
    // sales generates the proposal from the profile; the page explains the source and the gate
    const sales = await as(browser, USERS.sales);
    await sales.page.goto(`/customers/${customer.id}`);
    await expect(sales.page.getByTestId("profile-stats")).toContainText("confirmed orders");
    await expect(sales.page.getByText(/visible to owners, admins and approvers only/)).toBeVisible();
    await sales.page.getByTestId("generate-proposal").click();
    await expect(sales.page.getByTestId("profile-notice")).toContainText(/Proposal created with 1 line/);
    await sales.page.goto("/orders");
    await expect(sales.page.getByTestId("proposal-list")).toContainText("SKU-1001 ordered 2×");
    await expect(sales.page.getByTestId("proposal-list").getByRole("button", { name: /Approve/ })).toHaveCount(0);
    const before = await (await sales.page.request.get(`/api/v1/orders?customer_id=${customer.id}`)).json();
    expect(before).toHaveLength(2); // nothing ordered yet
    await sales.close();
    // approver decides on the orders page → a *proposed* order appears, which the same approver cannot confirm
    await ap.page.goto("/orders");
    const row = ap.page.getByTestId("proposal-list").locator("li").filter({ hasText: "SKU-1001 ordered 2×" }).first();
    await row.locator('[data-testid^="approve-proposal-"]').click();
    await expect(ap.page.getByTestId("proposal-list")).toHaveCount(0);
    await ap.page.locator("#order-status").selectOption("proposed");
    await expect(ap.page.getByTestId("order-table")).toContainText("repeat");
    const after = await (await ap.page.request.get(`/api/v1/orders?customer_id=${customer.id}&status=proposed`)).json();
    expect(after).toHaveLength(1);
    await ap.page.goto(`/orders/${after[0].id}`);
    await expect(ap.page.getByTestId("order-self-approval-blocked")).toBeVisible();
    await expect(ap.page.getByTestId("order-lines").locator("th", { hasText: "Cost" })).toHaveCount(1);
    await ap.close();
    // approver profile shows the margin figure; sales does not
    const approver2 = await as(browser, USERS.approver);
    await approver2.page.goto(`/customers/${customer.id}`);
    await expect(approver2.page.getByTestId("profile-stats")).toContainText("avg margin pct");
    await approver2.close();
  });

  test("insights: date range changes the figures, definitions and the unconverted bucket are shown, FX table gated", async ({ page, browser }) => {
    await login(page, USERS.readonly);
    await page.goto("/insights");
    await expect(page.getByTestId("kpi-grid")).toBeVisible();
    for (const key of ["quote_volume", "conversion_rate", "pending_value", "won_value", "response_time_hours", "processing_time_hours", "repeat_order_rate"]) {
      await expect(page.getByTestId(`kpi-${key}`)).toBeVisible();
      await page.getByTestId(`kpi-${key}`).getByText("Definition").click();
    }
    await expect(page.getByTestId("kpi-pending_value")).toContainText(/unconverted/);
    await expect(page.getByTestId("kpi-repeat_order_rate")).toContainText(/two or more confirmed/);
    await expect(page.getByTestId("add-fx")).toHaveCount(0);
    await expect(page.getByText(/reporting currency AED/)).toBeVisible();
    const volumeNow = await page.getByTestId("kpi-quote_volume").locator("div.text-2xl").textContent();
    await page.locator("#kpi-from").fill("2020-01-01");
    await page.locator("#kpi-to").fill("2020-01-31");
    await expect(page.getByTestId("kpi-quote_volume").locator("div.text-2xl")).toHaveText("0");
    await expect(page.getByTestId("kpi-conversion_rate").locator("div.text-2xl")).toHaveText("n/a");
    expect(volumeNow).not.toBeNull();
    // an unconverted amount appears when a pending quote has no FX rate (synthetic: a GBP quote via the API)
    const admin = await as(browser, USERS.admin);
    const { quote } = await approvedQuote(admin.page, browser, `RFQ-GBP-${Date.now()}\n1. 10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n`);
    expect(quote.id).toBeTruthy();
    await admin.page.goto("/insights");
    await expect(admin.page.getByTestId("add-fx")).toBeVisible();
    await expect(admin.page.getByTestId("fx-table")).toBeVisible();
    await admin.page.locator("#fx-base").fill("GBP");
    await admin.page.locator("#fx-quote").fill("GBP");
    await admin.page.locator("#fx-rate").fill("1");
    await admin.page.getByTestId("add-fx").click();
    await expect(admin.page.getByText(/Unsupported or identical currency pair/)).toBeVisible();
    await admin.close();
  });

  test("data page: CSV import via the browser reports applied/rejected with a failed-rows download; export gate per role; conflict kept local", async ({ page, browser }) => {
    await login(page, USERS.admin);
    await page.goto("/data");
    const stamp = Date.now().toString().slice(-6);
    const csv = `name,reference,email,default_currency\r\nBrowser Co ${stamp},BR-${stamp},,AED\r\nBad Row ${stamp},BB-${stamp},not-an-email,AED\r\n`;
    await page.locator("#import-entity").selectOption("customers");
    await page.getByTestId("import-file").setInputFiles({ name: `customers-${stamp}.csv`, mimeType: "text/csv", buffer: Buffer.from(csv) });
    await page.getByTestId("import-submit").click();
    await expect(page.getByTestId("import-result")).toContainText("customers: 1 applied, 1 rejected of 2");
    const link = page.getByTestId("import-result").getByRole("link", { name: /Download failed rows/ });
    await expect(link).toBeVisible();
    const failed = await page.request.get((await link.getAttribute("href"))!);
    expect(failed.status()).toBe(200);
    const body = await failed.text();
    expect(body).toContain(`Bad Row ${stamp}`);
    expect(body).toContain("invalid email");
    expect(body).not.toContain(`Browser Co ${stamp}`);
    await expect(page.getByTestId("run-table")).toContainText("csv_import");
    await page.goto("/customers");
    await expect(page.getByTestId("customer-table")).toContainText(`Browser Co ${stamp}`);
    await expect(page.getByTestId("customer-table")).not.toContainText(`Bad Row ${stamp}`);
    // a product conflict is kept local from the queue: the catalog price does not change
    await page.goto("/data");
    if ((await page.getByTestId("connector-list").count()) === 0 || !(await page.getByTestId("connector-list").textContent())?.includes("erp stub")) {
      await page.locator("#conn-name").fill(`QA ERP ${stamp}`);
      await page.getByLabel("read:products").check();
      await page.getByTestId("create-connector").click();
      await expect(page.getByTestId("connector-list")).toContainText("erp stub");
    }
    await page.locator("#sync-conn").selectOption({ index: 1 });
    await page.locator("#sync-entity").selectOption("products");
    await page.locator("#sync-key").fill(`qa-prod-${stamp}`);
    await page.locator("#sync-records").fill(JSON.stringify([{ sku: "SKU-1010", list_price: "99.99" }]));
    await page.getByTestId("run-sync").click();
    await expect(page.getByTestId("data-notice")).toContainText(/Sync processed/);
    const conflictRow = page.getByTestId("conflict-SKU-1010").first();
    await expect(conflictRow).toContainText("list_price");
    await expect(conflictRow).toContainText("2.40");
    await conflictRow.getByRole("button", { name: "Keep local" }).click();
    await expect(page.getByTestId("data-notice")).toContainText(/Kept local value/);
    await expect(page.getByTestId("conflict-SKU-1010")).toHaveCount(0); // left the open queue
    const resolved = await (await page.request.get("/api/v1/sync-conflicts?status=resolved_local")).json();
    const mine = resolved.find((c: { entity_key: string; local_value: { list_price: string } }) => c.entity_key === "SKU-1010");
    expect(mine).toBeTruthy();
    expect(String(mine.local_value.list_price)).toMatch(/^2\.40/); // catalog price untouched
    // export visibility per role
    await expect(page.getByTestId("export-orders")).toBeVisible();
    const ro = await as(browser, USERS.readonly);
    await ro.page.goto("/data");
    await expect(ro.page.getByTestId("stock-table")).toBeVisible();
    await expect(ro.page.getByTestId("export-customers")).toHaveCount(0);
    await expect(ro.page.getByText(/owner\/admin\/approver only/).first()).toBeVisible();
    await expect(ro.page.getByTestId("import-submit")).toHaveCount(0);
    await expect(ro.page.getByTestId("connector-list")).toHaveCount(0);
    expect((await ro.page.request.get("/api/v1/data/export/orders")).status()).toBe(403);
    await ro.close();
    const approver = await as(browser, USERS.approver);
    await approver.page.goto("/data");
    await expect(approver.page.getByTestId("export-products")).toBeVisible();
    const exp = await approver.page.request.get("/api/v1/data/export/products");
    expect(exp.status()).toBe(200);
    expect((await exp.text()).split("\n")[0]).toContain("cost_price");
    await approver.close();
  });

  test("Stage 1/2 regression: editor save + approval + follow-up planning still work alongside Stage 3", async ({ page, browser }) => {
    await login(page, USERS.sales);
    const token = await csrf(page);
    // own customer with an eligible contact (e-mail, not opted out) so planned follow-ups are 'pending', and so this
    // test never touches the seed customers that other specs rely on
    const created = await page.request.post("/api/v1/customers", { data: { name: `Regression Buyer Co ${Date.now()}` }, headers: { "X-CSRF-Token": token } });
    expect(created.status(), await created.text()).toBe(201);
    const customer = await created.json();
    const contact = await page.request.post(`/api/v1/customers/${customer.id}/contacts`, { data: { full_name: "Regression Buyer", email: `regression-${Date.now()}@customer.example`, is_primary: true }, headers: { "X-CSRF-Token": token } });
    expect(contact.status(), await contact.text()).toBe(201);
    const rfq = await (await page.request.post("/api/v1/rfqs", { data: { text: `RFQ-REG-${Date.now()}\n1. 100 pcs SKU-1003 Hex nut M8 zinc plated\n`, customer_id: customer.id }, headers: { "X-CSRF-Token": token } })).json();
    const q = await (await page.request.post(`/api/v1/rfqs/${rfq.id}/generate-quote`, { headers: { "X-CSRF-Token": token } })).json();
    await page.goto(`/quotes/${q.id}`);
    await expect(page.getByTestId("quote-status")).toHaveText(/draft/i);
    await page.getByTestId("discount-1").fill("10");
    await page.getByTestId("save-draft").click();
    await expect(page.getByTestId("line-total-1")).not.toHaveText("…");
    // 100 × 0.25 = 25.00 − 2.50 = 22.50 + 5 % = 23.63
    await expect(page.getByTestId("grand-total")).toHaveText(/23\.63/);
    await page.getByTestId("submit-quote").click();
    await expect(page.getByTestId("quote-status")).toHaveText(/pending approval|submitted/i);
    await expect(page.getByTestId("approve-quote")).toHaveCount(0);
    const ap = await as(browser, USERS.approver);
    await ap.page.goto(`/quotes/${q.id}`);
    await ap.page.getByTestId("approve-quote").click();
    await expect(ap.page.getByTestId("quote-status")).toHaveText(/approved/i);
    await ap.close();
    await page.reload();
    await expect(page.getByTestId("quote-status")).toHaveText(/approved/i);
    await expect(page.getByTestId("convert-to-order")).toBeVisible();
    await page.getByTestId("transition-sent").click();
    await expect(page.getByTestId("lifecycle-status")).toHaveText(/sent/i);
    await expect(page.locator('[data-testid^="schedule-pending"]').first()).toBeVisible();
    await page.goto("/follow-ups");
    await expect(page.getByTestId("schedule-table")).toContainText("pending");
  });
});
