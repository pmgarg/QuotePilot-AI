import { expect, test } from "@playwright/test";
import path from "node:path";
import { CLEAN_RFQ, USERS, login, logout } from "../helpers";

const FIXTURES = path.resolve(__dirname, "../../fixtures");

test.describe("S1-A20 RFQ → review → quote → approval → PDF", () => {
  test("full browser flow with separation of duties and deterministic PDF", async ({ page, browser }) => {
    await login(page, USERS.sales);
    await expect(page.getByTestId("org-name")).toHaveText("Northwind Trading LLC");

    // paste an RFQ
    await page.getByTestId("new-rfq").first().click();
    await page.getByTestId("mode-text").check();
    await page.getByTestId("rfq-text").fill(CLEAN_RFQ);
    await page.getByTestId("rfq-customer").selectOption({ index: 1 });
    await page.getByTestId("rfq-reference").fill("RFQ-E2E");
    await page.getByTestId("rfq-submit").click();
    await expect(page.getByTestId("rfq-status")).toHaveText(/parsed/i);
    await expect(page.getByTestId("match-1")).toHaveText(/exact/i);
    await expect(page.getByTestId("match-2")).toHaveText(/exact/i);
    await expect(page.getByTestId("match-3")).toHaveText(/exact/i);

    // generate quote
    await page.getByTestId("generate-quote").click();
    await expect(page).toHaveURL(/\/quotes\//);
    await expect(page.getByTestId("quote-status")).toHaveText(/draft/i);
    const quoteUrl = page.url();
    await expect(page.getByTestId("line-total-1")).toHaveText("446.25"); // 500 × 0.85 + 5 % tax
    await expect(page.getByTestId("grand-total")).toHaveText("1,176.00"); // 446.25 + 131.25 + 598.50
    // sales cannot see costs/margins
    await expect(page.locator("th", { hasText: "Margin" })).toHaveCount(0);

    // submit for approval; sales cannot approve
    await page.getByTestId("submit-quote").click();
    await expect(page.getByTestId("version-status")).toHaveText(/pending approval/i);
    await expect(page.getByTestId("approve-quote")).toHaveCount(0);
    await logout(page);

    // approver approves
    await login(page, USERS.approver);
    await page.goto(quoteUrl);
    await expect(page.locator("th", { hasText: "Margin" })).toHaveCount(1);
    await page.getByTestId("decision-reason").fill("Margins fine");
    await page.getByTestId("approve-quote").click();
    await expect(page.getByTestId("quote-status")).toHaveText(/approved/i);
    await expect(page.getByTestId("quote-notice")).toContainText("approved");

    // PDF download through the authorized API, twice → identical bytes
    const quoteId = quoteUrl.split("/quotes/")[1];
    const pdf1 = await page.request.get(`/api/v1/quotes/${quoteId}/versions/1/pdf`);
    expect(pdf1.status()).toBe(200);
    expect(pdf1.headers()["content-type"]).toBe("application/pdf");
    const body1 = await pdf1.body();
    expect(body1.subarray(0, 5).toString()).toBe("%PDF-");
    const pdf2 = await page.request.get(`/api/v1/quotes/${quoteId}/versions/1/pdf`);
    expect(Buffer.compare(body1, await pdf2.body())).toBe(0);
    await expect(page.getByTestId("audit-table")).toContainText("quote.approve");

    // another tenant cannot see it (404 → error page state)
    const other = await browser.newContext();
    const otherPage = await other.newPage();
    await login(otherPage, USERS.otherOwner);
    const res = await otherPage.request.get(`/api/v1/quotes/${quoteId}`);
    expect(res.status()).toBe(404);
    await otherPage.goto(quoteUrl);
    await expect(otherPage.getByRole("alert").filter({ hasText: /not found/i })).toBeVisible();
    await other.close();
  });

  test("concurrent edit shows a conflict banner", async ({ browser }) => {
    const ctxA = await browser.newContext();
    const ctxB = await browser.newContext();
    const a = await ctxA.newPage();
    const b = await ctxB.newPage();
    await login(a, USERS.sales);
    await login(b, USERS.admin);
    await a.getByTestId("new-rfq").first().click();
    await a.getByTestId("mode-text").check();
    await a.getByTestId("rfq-text").fill("10 pcs SKU-1001 Hex bolt M8x40\n");
    await a.getByTestId("rfq-submit").click();
    await a.getByTestId("generate-quote").click();
    await expect(a).toHaveURL(/\/quotes\//);
    const url = a.url();
    await b.goto(url);
    await expect(b.getByTestId("save-draft")).toBeVisible();
    await a.getByTestId("discount-1").fill("5");
    await a.getByTestId("save-draft").click();
    await expect(a.getByTestId("quote-notice")).toContainText("saved");
    await b.getByTestId("discount-1").fill("10");
    await b.getByTestId("save-draft").click();
    await expect(b.getByTestId("conflict-banner")).toBeVisible();
    await ctxA.close();
    await ctxB.close();
  });

  test("read-only user sees permission states and cannot act", async ({ page }) => {
    await login(page, USERS.readonly);
    await page.goto("/rfqs/new");
    await expect(page.getByTestId("forbidden")).toBeVisible();
    await page.goto("/settings");
    await expect(page.getByTestId("org-save")).toHaveCount(0);
    await expect(page.getByTestId("forbidden")).toBeVisible();
    await page.goto("/quotes");
    const first = page.getByTestId("quote-table").locator("a").first();
    if (await first.count()) {
      await first.click();
      await expect(page.getByTestId("submit-quote")).toHaveCount(0);
      await expect(page.getByTestId("approve-quote")).toHaveCount(0);
    }
  });

  test("invalid login shows a generic error and no session", async ({ page }) => {
    await page.goto("/login");
    await page.getByTestId("login-email").fill(USERS.sales);
    await page.getByTestId("login-password").fill("definitely-wrong-password");
    await page.getByTestId("login-submit").click();
    await expect(page.getByTestId("login-error")).toContainText("Invalid email or password");
    await page.goto("/dashboard");
    await expect(page).toHaveURL(/\/login/);
  });

  test("catalog import through the UI creates and activates a new version; unsupported file is rejected", async ({ page }) => {
    await login(page, USERS.admin);
    await page.goto("/catalog");
    await page.getByTestId("catalog-file").setInputFiles(path.join(FIXTURES, "catalog_formula_injection.csv"));
    await page.getByTestId("map-sku").fill("SKU");
    await page.getByTestId("map-description").fill("Description");
    await page.getByTestId("map-list_price").fill("List Price");
    await page.getByTestId("map-unit").fill("Unit");
    await page.getByTestId("map-currency").fill("Currency");
    for (const k of ["cost_price", "stock_qty", "valid_from", "valid_to"]) await page.getByTestId(`map-${k}`).fill("");
    await page.getByTestId("catalog-import").click();
    await expect(page.getByTestId("import-report")).toContainText("2 products");
    await expect(page.getByTestId("import-report")).toContainText("duplicate sku");
    await expect(page.getByTestId("import-report")).toContainText("formula");
    // unsupported upload
    await page.getByTestId("catalog-file").setInputFiles(path.join(FIXTURES, "not_supported.docx"));
    await page.getByTestId("catalog-import").click();
    await expect(page.getByTestId("import-error")).toContainText(/not a valid XLSX|Unsupported/i);
  });

  test("XSS payload in a custom quote line is rendered as inert text", async ({ page }) => {
    await login(page, USERS.sales);
    await page.goto("/quotes");
    const payload = `<img src=x onerror="document.title='pwned'"> & <script>window.__x=1</script>`;
    const created = await page.request.post("/api/v1/quotes", { data: {}, headers: { "X-CSRF-Token": (await page.context().cookies()).find((c) => c.name === "qp_csrf")!.value } });
    expect(created.status()).toBe(201);
    const q = await created.json();
    const patched = await page.request.patch(`/api/v1/quotes/${q.id}`, {
      data: { expected_lock_version: q.lock_version, lines: [{ description: payload, quantity: "1", unit_price: "1", is_custom: true }] },
      headers: { "X-CSRF-Token": (await page.context().cookies()).find((c) => c.name === "qp_csrf")!.value },
    });
    expect(patched.status()).toBe(200);
    await page.goto(`/quotes/${q.id}`);
    await expect(page.getByTestId("quote-line-1")).toBeVisible();
    await expect(page.locator('input[aria-label="Description 1"]')).toHaveValue(payload);
    expect(await page.title()).not.toBe("pwned");
    expect(await page.evaluate(() => (window as unknown as { __x?: number }).__x)).toBeUndefined();
  });
});
