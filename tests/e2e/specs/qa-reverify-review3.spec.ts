/**
 * QA (independent) browser re-verification 3 of Stage 1 at f69ac25 (fix rounds 5 and 6).
 * Derived from docs/PRD.md (S1-F11, S1-F12, S1-F14, S1-F15, S1-A20), docs/USER_JOURNEYS.md and
 * docs/evidence/release-review-3-stage1-2026-11-02.md ("Required retest": D2-1..D2-5, R3-6, R3-10).
 * Browser evidence (Chromium against real uvicorn + Next.js). Screens are driven; API calls only set up state or read
 * export status. Tests that change the active catalog restore the demo price list in `finally`.
 */
import { expect, test, type Browser, type Page } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { USERS, login } from "../helpers";

const DEMO_CATALOG = path.resolve(__dirname, "../../../apps/api/app/fixtures/data/demo_catalog.csv");
const TWO_LINE_RFQ = "1. 500 pcs SKU-1001 Hex bolt M8x40 zinc plated\n2. 500 pcs SKU-1003 Hex nut M8 zinc plated\n";
const MAPPING = JSON.stringify({ sku: "SKU", description: "Description", unit: "Unit", currency: "Currency", list_price: "List Price", cost_price: "Cost Price" });

async function csrf(page: Page): Promise<string> {
  return (await page.context().cookies()).find((c) => c.name === "qp_csrf")!.value;
}

async function pasteRfq(page: Page, text: string, reference: string) {
  await page.goto("/rfqs/new");
  await page.getByTestId("mode-text").check();
  await page.getByTestId("rfq-text").fill(text);
  await page.getByTestId("rfq-customer").selectOption({ index: 1 });
  await page.getByTestId("rfq-reference").fill(reference);
  await page.getByTestId("rfq-submit").click();
  await expect(page.getByTestId("rfq-status")).toHaveText(/parsed/i);
}

async function generateQuote(page: Page): Promise<string> {
  await page.getByTestId("generate-quote").click();
  await expect(page).toHaveURL(/\/quotes\//);
  await expect(page.getByTestId("quote-status")).toHaveText(/draft/i);
  return page.url().split("/quotes/")[1];
}

async function asUser(browser: Browser, email: string) {
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  await login(page, email);
  return { ctx, page };
}

async function importAndActivate(page: Page, csv: string, name: string) {
  const imp = await page.request.post("/api/v1/catalogs/import", { multipart: { file: { name, mimeType: "text/csv", buffer: Buffer.from(csv) }, mapping: MAPPING, catalog_name: "Main catalog" }, headers: { "X-CSRF-Token": await csrf(page) } });
  expect(imp.status(), await imp.text()).toBe(201);
  const act = await page.request.post(`/api/v1/catalogs/versions/${(await imp.json()).id}/activate`, { headers: { "X-CSRF-Token": await csrf(page) } });
  expect(act.status(), await act.text()).toBe(200);
}

async function restoreDemoCatalog(browser: Browser) {
  const ad = await asUser(browser, USERS.admin);
  await importAndActivate(ad.page, fs.readFileSync(DEMO_CATALOG, "utf8"), "qa3-restore.csv");
  await ad.ctx.close();
}

test.describe("QA re-verification 3 — rounds 5 and 6 in the browser", () => {
  test("D2-1: editing price in the editor yields manual+blocker; retyping the catalog price does not launder; clearing restores catalog", async ({ page }) => {
    await login(page, USERS.sales);
    await pasteRfq(page, TWO_LINE_RFQ, `QA3-D21-${Date.now()}`);
    await generateQuote(page);
    const price1 = page.getByTestId("unit-price-1");
    await expect(price1).toHaveValue(/0\.85/);
    // a real price change is manual and blocks
    await price1.fill("0.86");
    await page.getByTestId("save-draft").click();
    await expect(page.getByTestId("quote-notice")).toContainText("saved");
    await expect(page.getByTestId("quote-line-1")).toContainText(/manual price/i);
    await expect(page.getByTestId("quote-line-2")).not.toContainText(/manual price/i);
    await expect(page.getByTestId("quote-blockers")).toBeVisible();
    await expect(page.getByTestId("submit-quote")).toBeDisabled();
    // saving again with no change keeps manual (the editor now re-sends the typed price)
    await page.locator('input[aria-label="Quantity 2"]').fill("501");
    await page.getByTestId("save-draft").click();
    await expect(page.getByTestId("quote-line-1")).toContainText(/manual price/i);
    // retyping the catalog price into a manual line must not silently restore catalog provenance
    await price1.fill("0.85");
    await page.getByTestId("save-draft").click();
    await expect(page.getByTestId("quote-line-1")).toContainText(/manual price/i);
    await expect(page.getByTestId("quote-blockers")).toBeVisible();
    await expect(page.getByTestId("submit-quote")).toBeDisabled();
    // clearing the price asks the server to restore the catalog price
    await price1.fill("");
    await page.getByTestId("save-draft").click();
    await expect(page.getByTestId("quote-error")).toHaveCount(0);
    await expect(page.getByTestId("quote-line-1")).not.toContainText(/manual price/i);
    await expect(page.getByTestId("unit-price-1")).toHaveValue(/0\.85/);
    await expect(page.getByTestId("quote-blockers")).toHaveCount(0);
    await expect(page.getByTestId("submit-quote")).toBeEnabled();
  });

  test("D2-4/D2-5/R3-10: withdraw v1 from the UI while v2 is a draft; banner shows the reason as text (no HTML injection); approving v2 clears the reason", async ({ page, browser }) => {
    const reason = `<img src=x onerror="window.__qa_xss=1"> v1 priced wrong`;
    await login(page, USERS.sales);
    await pasteRfq(page, TWO_LINE_RFQ, `QA3-D25-${Date.now()}`);
    const quoteId = await generateQuote(page);
    const url = page.url();
    await page.getByTestId("submit-quote").click();
    await expect(page.getByTestId("version-status")).toHaveText(/pending approval/i);
    const ap = await asUser(browser, USERS.approver);
    await ap.page.goto(url);
    await ap.page.getByTestId("approve-quote").click();
    await expect(ap.page.getByTestId("quote-status")).toHaveText(/approved/i);
    await page.reload();
    await page.getByTestId("new-version").click();
    await expect(page.getByTestId("version-status")).toHaveText(/draft/i);
    await ap.page.goto(url);
    await ap.page.locator("#withdraw-reason").fill(reason);
    await ap.page.getByRole("button", { name: "Withdraw" }).click();
    await expect(ap.page.getByTestId("quote-notice")).toContainText(/withdrawn/i);
    const banner = ap.page.getByTestId("withdrawn-documents");
    await expect(banner).toContainText("v1");
    await expect(banner).toContainText('<img src=x onerror="window.__qa_xss=1">');
    expect(await ap.page.evaluate(() => (window as unknown as { __qa_xss?: number }).__qa_xss)).toBeUndefined();
    expect(await ap.page.locator("[data-testid=withdrawn-documents] img").count()).toBe(0);
    // sales sees the same banner on the draft and can still submit v2
    await page.reload();
    await expect(page.getByTestId("withdrawn-documents")).toContainText("v1");
    expect(await page.evaluate(() => (window as unknown as { __qa_xss?: number }).__qa_xss)).toBeUndefined();
    await expect(page.getByTestId("submit-quote")).toBeEnabled();
    await page.getByTestId("submit-quote").click();
    await expect(page.getByTestId("version-status")).toHaveText(/pending approval/i);
    await ap.page.goto(url);
    await ap.page.getByTestId("approve-quote").click();
    await expect(ap.page.getByTestId("quote-status")).toHaveText(/approved/i);
    // D2-5: the stale reason must be gone from the API payload and from the banner; v1 stays withdrawn
    const got = await (await ap.page.request.get(`/api/v1/quotes/${quoteId}`)).json();
    expect(got.status).toBe("approved");
    expect(got.withdrawn_reason).toBeNull();
    await expect(ap.page.getByTestId("withdrawn-documents")).not.toContainText("priced wrong");
    await expect(ap.page.getByTestId("withdrawn-documents")).toContainText("v1");
    expect((await ap.page.request.get(`/api/v1/quotes/${quoteId}/versions/1/pdf`)).status()).toBe(409);
    expect((await ap.page.request.get(`/api/v1/quotes/${quoteId}/versions/2/pdf`)).status()).toBe(200);
    await ap.ctx.close();
  });

  test("R3-6/R3-10: a pending ad-hoc quote whose catalog moved on cannot be approved in the UI; reject, then 'Use active catalog' re-links only what the active catalog contains", async ({ page, browser }) => {
    await login(page, USERS.sales);
    const token = await csrf(page);
    const customers = await (await page.request.get("/api/v1/customers")).json();
    const activeId = (await (await page.request.get("/api/v1/catalogs/active-version")).json()).version_id;
    const prods = async (sku: string) => (await (await page.request.get(`/api/v1/catalogs/versions/${activeId}/products?q=${sku}`)).json()).items.find((p: { sku: string }) => p.sku === sku);
    const [p1, p3] = [await prods("SKU-1001"), await prods("SKU-1003")];
    const created = await (await page.request.post("/api/v1/quotes", { data: { customer_id: customers[0].id }, headers: { "X-CSRF-Token": token } })).json();
    const patched = await page.request.patch(`/api/v1/quotes/${created.id}`, { data: { expected_lock_version: created.lock_version, lines: [{ quantity: "3", product_id: p1.id }, { quantity: "2", product_id: p3.id }] }, headers: { "X-CSRF-Token": token } });
    expect(patched.status(), await patched.text()).toBe(200);
    const sub = await page.request.post(`/api/v1/quotes/${created.id}/submit`, { data: { expected_lock_version: (await patched.json()).lock_version }, headers: { "X-CSRF-Token": token } });
    expect(sub.status(), await sub.text()).toBe(200);
    try {
      // new price list: SKU-1001 at 0.91, SKU-1003 dropped
      const rows = fs.readFileSync(DEMO_CATALOG, "utf8").split("\n").filter((l) => !l.startsWith("SKU-1003,")).join("\n").replace("SKU-1001,Hex bolt M8x40 zinc plated,EA,AED,0.85", "SKU-1001,Hex bolt M8x40 zinc plated,EA,AED,0.91");
      const ad = await asUser(browser, USERS.admin);
      await importAndActivate(ad.page, rows, "qa3-v2.csv");
      await ad.ctx.close();

      const ap = await asUser(browser, USERS.approver);
      await ap.page.goto(`/quotes/${created.id}`);
      await expect(ap.page.getByTestId("version-status")).toHaveText(/pending approval/i);
      await ap.page.getByTestId("approve-quote").click();
      await expect(ap.page.getByTestId("quote-blockers")).toContainText(/no longer this quote's price list/);
      await expect(ap.page.getByTestId("version-status")).toHaveText(/pending approval/i);
      await expect(ap.page.getByTestId("quote-status")).not.toHaveText(/^approved/i);
      await ap.page.getByTestId("decision-reason").fill("catalog moved on");
      await ap.page.getByTestId("reject-quote").click();
      await expect(ap.page.getByTestId("quote-status")).toHaveText(/draft/i);
      await ap.ctx.close();

      await page.goto(`/quotes/${created.id}`);
      await expect(page.getByTestId("quote-blockers")).toContainText(/Line 1/);
      await expect(page.getByTestId("quote-blockers")).toContainText(/Line 2/);
      await expect(page.getByTestId("submit-quote")).toBeDisabled();
      // line 2's SKU left the catalog: the re-link is refused with a clear message and changes nothing
      await page.getByTestId("relink-2").click();
      await expect(page.getByTestId("quote-error")).toContainText(/SKU-1003 is not in the active catalog/);
      await expect(page.getByTestId("unit-price-2")).toHaveValue(/\d/);
      // line 1 re-links; the server re-prices at 0.91 on save
      await page.getByTestId("relink-1").click();
      await expect(page.getByTestId("quote-notice")).toContainText(/re-linked/);
      await page.getByTestId("save-draft").click();
      await expect(page.getByTestId("unit-price-1")).toHaveValue(/0\.91/);
      await expect(page.getByTestId("relink-1")).toHaveCount(0);
      await expect(page.getByTestId("relink-2")).toHaveCount(1);
      await expect(page.getByTestId("quote-blockers")).not.toContainText(/Line 1 is priced/);
      await expect(page.getByTestId("submit-quote")).toBeDisabled();
      // the only remaining route for the dropped SKU is removing the line
      await page.getByTestId("quote-line-2").getByRole("button", { name: "Remove" }).click();
      await page.getByTestId("save-draft").click();
      await expect(page.getByTestId("quote-blockers")).toHaveCount(0);
      await expect(page.getByTestId("submit-quote")).toBeEnabled();
    } finally {
      await restoreDemoCatalog(browser);
    }
  });

  test("R3-6: GET /catalogs/active-version is auth-gated and returns only {version_id} to a read-only user", async ({ page, browser }) => {
    const anon = await browser.newContext();
    const r = await anon.request.get("/api/v1/catalogs/active-version");
    expect([401, 403]).toContain(r.status());
    await anon.close();
    await login(page, USERS.readonly);
    const ok = await page.request.get("/api/v1/catalogs/active-version");
    expect(ok.status()).toBe(200);
    expect(Object.keys(await ok.json())).toEqual(["version_id"]);
  });
});
