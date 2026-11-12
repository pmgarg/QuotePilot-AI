import { expect, test, type Page } from "@playwright/test";
import path from "node:path";
import fs from "node:fs";
import { USERS, login } from "../helpers";

// Release review 3 follow-ups: R3-6 (re-link a stale catalog line from the editor) and R3-10 (withdrawn-document banner).

async function csrf(page: Page) {
  return (await page.context().cookies()).find((c) => c.name === "qp_csrf")!.value;
}

test.describe("Release review 3 regressions", () => {
  test("R3-10: approver withdraws v1 while v2 is a draft; the page shows the earlier-document banner", async ({ page, browser }) => {
    await login(page, USERS.sales);
    const token = await csrf(page);
    const customers = await (await page.request.get("/api/v1/customers")).json();
    const rfq = await (await page.request.post("/api/v1/rfqs", { data: { text: "10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n", customer_id: customers[0].id }, headers: { "X-CSRF-Token": token } })).json();
    const qRes = await page.request.post(`/api/v1/rfqs/${rfq.id}/generate-quote`, { headers: { "X-CSRF-Token": token } });
    expect(qRes.status(), await qRes.text()).toBe(201);
    const q = await qRes.json();
    const sub = await (await page.request.post(`/api/v1/quotes/${q.id}/submit`, { data: { expected_lock_version: q.lock_version }, headers: { "X-CSRF-Token": token } })).json();
    const ctx = await browser.newContext();
    const ap = await ctx.newPage();
    await login(ap, USERS.approver);
    const approved = await (await ap.request.post(`/api/v1/quotes/${q.id}/approve`, { data: { expected_lock_version: sub.lock_version }, headers: { "X-CSRF-Token": await csrf(ap) } })).json();
    const v2 = await (await page.request.post(`/api/v1/quotes/${q.id}/versions`, { data: { expected_lock_version: approved.lock_version }, headers: { "X-CSRF-Token": token } })).json();
    const w = await ap.request.post(`/api/v1/quotes/${q.id}/withdraw`, { data: { expected_lock_version: v2.lock_version, reason: "v1 priced wrong" }, headers: { "X-CSRF-Token": await csrf(ap) } });
    expect(w.status(), await w.text()).toBe(200);
    await ctx.close();
    await page.goto(`/quotes/${q.id}`);
    await expect(page.getByTestId("withdrawn-documents")).toContainText("v1");
    await expect(page.getByTestId("withdrawn-documents")).toContainText("v1 priced wrong");
  });

  test("R3-6: a draft line left on an archived catalog version can be re-linked to the active catalog from the editor", async ({ page, browser }) => {
    await login(page, USERS.sales);
    const token = await csrf(page);
    const customers = await (await page.request.get("/api/v1/customers")).json();
    const activeId = (await (await page.request.get("/api/v1/catalogs/active-version")).json()).version_id;
    const products = await (await page.request.get(`/api/v1/catalogs/versions/${activeId}/products?q=SKU-1001`)).json();
    const product = products.items.find((p: { sku: string }) => p.sku === "SKU-1001");
    const created = await (await page.request.post("/api/v1/quotes", { data: { customer_id: customers[0].id }, headers: { "X-CSRF-Token": token } })).json();
    const linked = await page.request.patch(`/api/v1/quotes/${created.id}`, { data: { expected_lock_version: created.lock_version, lines: [{ quantity: "3", product_id: product.id }] }, headers: { "X-CSRF-Token": token } });
    expect(linked.status(), await linked.text()).toBe(200);
    // an admin activates a new price list through the UI-equivalent API (the demo catalog with a new SKU-1001 price)
    const ctx = await browser.newContext();
    const ad = await ctx.newPage();
    await login(ad, USERS.admin);
    const demo = fs.readFileSync(path.resolve(__dirname, "../../../apps/api/app/fixtures/data/demo_catalog.csv"), "utf8").replace("SKU-1001,Hex bolt M8x40 zinc plated,EA,AED,0.85", "SKU-1001,Hex bolt M8x40 zinc plated,EA,AED,0.91");
    const mapping = JSON.stringify({ sku: "SKU", description: "Description", unit: "Unit", currency: "Currency", list_price: "List Price", cost_price: "Cost Price" });
    const imp = await ad.request.post("/api/v1/catalogs/import", { multipart: { file: { name: "r3.csv", mimeType: "text/csv", buffer: Buffer.from(demo) }, mapping, catalog_name: "Main catalog" }, headers: { "X-CSRF-Token": await csrf(ad) } });
    expect(imp.status(), await imp.text()).toBe(201);
    const act = await ad.request.post(`/api/v1/catalogs/versions/${(await imp.json()).id}/activate`, { headers: { "X-CSRF-Token": await csrf(ad) } });
    expect(act.status(), await act.text()).toBe(200);
    await ctx.close();

    try {
      await page.goto(`/quotes/${created.id}`);
      await expect(page.getByTestId("quote-blockers")).toContainText(/no longer this quote's price list/);
      await page.getByTestId("relink-1").click();
      await expect(page.getByTestId("quote-notice")).toContainText(/re-linked/);
      await page.getByRole("button", { name: /save draft/i }).click();
      await expect(page.getByTestId("quote-blockers")).toHaveCount(0);
      await expect(page.getByTestId("unit-price-1")).toHaveValue(/0\.91/);
      await expect(page.getByTestId("submit-quote")).toBeEnabled();
    } finally {
      // restore the demo price list so later specs keep their expected prices
      const ctx2 = await browser.newContext();
      const ad2 = await ctx2.newPage();
      await login(ad2, USERS.admin);
      const original = fs.readFileSync(path.resolve(__dirname, "../../../apps/api/app/fixtures/data/demo_catalog.csv"), "utf8");
      const back = await ad2.request.post("/api/v1/catalogs/import", { multipart: { file: { name: "restore.csv", mimeType: "text/csv", buffer: Buffer.from(original) }, mapping, catalog_name: "Main catalog" }, headers: { "X-CSRF-Token": await csrf(ad2) } });
      expect(back.status(), await back.text()).toBe(201);
      const re = await ad2.request.post(`/api/v1/catalogs/versions/${(await back.json()).id}/activate`, { headers: { "X-CSRF-Token": await csrf(ad2) } });
      expect(re.status(), await re.text()).toBe(200);
      await ctx2.close();
    }
  });
});
