/**
 * QA (independent) browser re-verification 4 of Stage 1 at bf05198 (fix round 7).
 * Derived from docs/evidence/release-review-4-stage1-2026-11-10.md (RR4-3 "Required retest": remove a row, then re-link)
 * and docs/PRD.md (S1-F11, S1-F12, S1-F14). Browser evidence (Chromium against real uvicorn + Next.js).
 * Tests that change the active catalog restore the demo price list in `finally`.
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

async function importAndActivate(page: Page, csv: string, name: string) {
  const imp = await page.request.post("/api/v1/catalogs/import", { multipart: { file: { name, mimeType: "text/csv", buffer: Buffer.from(csv) }, mapping: MAPPING, catalog_name: "Main catalog" }, headers: { "X-CSRF-Token": await csrf(page) } });
  expect(imp.status(), await imp.text()).toBe(201);
  const act = await page.request.post(`/api/v1/catalogs/versions/${(await imp.json()).id}/activate`, { headers: { "X-CSRF-Token": await csrf(page) } });
  expect(act.status(), await act.text()).toBe(200);
}

async function adminImport(browser: Browser, csv: string, name: string) {
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  await login(page, USERS.admin);
  await importAndActivate(page, csv, name);
  await ctx.close();
}

const restore = (browser: Browser) => adminImport(browser, fs.readFileSync(DEMO_CATALOG, "utf8"), "qa4-restore.csv");
const v2Catalog = () => fs.readFileSync(DEMO_CATALOG, "utf8").replace("SKU-1001,Hex bolt M8x40 zinc plated,EA,AED,0.85", "SKU-1001,Hex bolt M8x40 zinc plated,EA,AED,0.91");

/** Create a draft quote through the API with the given lines; returns the quote id. */
async function apiQuote(page: Page, lines: Record<string, unknown>[]): Promise<string> {
  const token = await csrf(page);
  const customers = await (await page.request.get("/api/v1/customers")).json();
  const created = await (await page.request.post("/api/v1/quotes", { data: { customer_id: customers[0].id }, headers: { "X-CSRF-Token": token } })).json();
  const patched = await page.request.patch(`/api/v1/quotes/${created.id}`, { data: { expected_lock_version: created.lock_version, lines }, headers: { "X-CSRF-Token": token } });
  expect(patched.status(), await patched.text()).toBe(200);
  return created.id;
}

async function activeProduct(page: Page, sku: string) {
  const activeId = (await (await page.request.get("/api/v1/catalogs/active-version")).json()).version_id;
  return (await (await page.request.get(`/api/v1/catalogs/versions/${activeId}/products?q=${sku}`)).json()).items.find((p: { sku: string }) => p.sku === sku);
}

test.describe("QA re-verification 4 — round 7 editor in the browser", () => {
  test("RR4-3: removing a row without saving keeps the manual-price badge on the right row, before and after saving", async ({ page }) => {
    await login(page, USERS.sales);
    await pasteRfq(page, TWO_LINE_RFQ, `QA4-RM-${Date.now()}`);
    await generateQuote(page);
    // make line 2 (SKU-1003) manual
    await page.getByTestId("unit-price-2").fill("9.99");
    await page.getByTestId("save-draft").click();
    await expect(page.getByTestId("quote-notice")).toContainText("saved");
    await expect(page.getByTestId("quote-line-2")).toContainText(/manual price/i);
    await expect(page.getByTestId("quote-line-1")).not.toContainText(/manual price/i);
    // remove row 1 WITHOUT saving: the surviving row (SKU-1003) must still carry its own badge
    await page.getByTestId("quote-line-1").getByRole("button", { name: "Remove" }).click();
    await expect(page.getByTestId("quote-line-2")).toHaveCount(0);
    await expect(page.getByTestId("quote-line-1")).toContainText("SKU-1003");
    await expect(page.getByTestId("quote-line-1")).toContainText(/manual price/i);
    await expect(page.getByTestId("unit-price-1")).toHaveValue("9.99");
    await page.getByTestId("save-draft").click();
    await expect(page.getByTestId("quote-notice")).toContainText("saved");
    await expect(page.getByTestId("quote-lines").locator("tbody tr")).toHaveCount(1);
    await expect(page.getByTestId("quote-line-1")).toContainText("SKU-1003");
    await expect(page.getByTestId("quote-line-1")).toContainText(/manual price/i);
  });

  test("RR4-3: remove a non-stale row without saving; re-link button stays on the stale row (not the neighbour); re-link, then save re-prices", async ({ page, browser }) => {
    await login(page, USERS.sales);
    const p1 = await activeProduct(page, "SKU-1001");
    // line 1: custom (never stale); line 2: catalog-linked (becomes stale)
    const id = await apiQuote(page, [{ description: "QA4 custom widget", quantity: "1", unit: "EA", unit_price: "5.00", is_custom: true }, { quantity: "3", product_id: p1.id }]);
    try {
      await adminImport(browser, v2Catalog(), "qa4-v2.csv");
      await page.goto(`/quotes/${id}`);
      await expect(page.getByTestId("quote-lines").locator("tbody tr")).toHaveCount(2);
      await expect(page.getByTestId("relink-2")).toHaveCount(1);
      await expect(page.getByTestId("relink-1")).toHaveCount(0);
      await expect(page.getByTestId("quote-line-1")).toContainText(/custom/i);
      // remove row 1 without saving: the stale row moves to position 1 and keeps its button; the button is not lost
      await page.getByTestId("quote-line-1").getByRole("button", { name: "Remove" }).click();
      await expect(page.getByTestId("quote-lines").locator("tbody tr")).toHaveCount(1);
      await expect(page.getByTestId("quote-line-1")).toContainText("SKU-1001");
      await expect(page.getByTestId("relink-1")).toHaveCount(1);
      await expect(page.getByTestId("relink-2")).toHaveCount(0);
      await page.getByTestId("relink-1").click();
      await expect(page.getByTestId("quote-notice")).toContainText(/re-linked/);
      await page.getByTestId("save-draft").click();
      await expect(page.getByTestId("unit-price-1")).toHaveValue(/0\.91/);
      await expect(page.getByTestId("relink-1")).toHaveCount(0);
      await expect(page.getByTestId("quote-blockers")).toHaveCount(0);
      await expect(page.getByTestId("quote-line-1")).not.toContainText(/manual price/i);
      const got = await (await page.request.get(`/api/v1/quotes/${id}`)).json();
      expect(got.current_version.lines).toHaveLength(1);
      expect(got.current_version.lines[0].price_source).toBe("catalog");
      expect(got.current_version.lines[0].unit_price).toBe("0.91");
      await expect(page.getByTestId("submit-quote")).toBeEnabled();
    } finally {
      await restore(browser);
    }
  });

  test("RR4-3: adding then removing an unsaved custom line does not shift badges or buttons of saved rows", async ({ page, browser }) => {
    await login(page, USERS.sales);
    const [p1, p3] = [await activeProduct(page, "SKU-1001"), await activeProduct(page, "SKU-1003")];
    const id = await apiQuote(page, [{ quantity: "3", product_id: p1.id }, { quantity: "2", product_id: p3.id }]);
    try {
      await adminImport(browser, v2Catalog().split("\n").filter((l) => !l.startsWith("SKU-1003,")).join("\n"), "qa4-v2b.csv");
      await page.goto(`/quotes/${id}`);
      await expect(page.getByTestId("relink-1")).toHaveCount(1);
      await expect(page.getByTestId("relink-2")).toHaveCount(1);
      // remove row 1 (unsaved) and add a new custom row: no button may appear on the new row
      await page.getByTestId("quote-line-1").getByRole("button", { name: "Remove" }).click();
      await page.getByRole("button", { name: "Add custom line" }).click();
      await expect(page.getByTestId("quote-lines").locator("tbody tr")).toHaveCount(2);
      await expect(page.getByTestId("quote-line-1")).toContainText("SKU-1003");
      await expect(page.getByTestId("relink-1")).toHaveCount(1);
      await expect(page.getByTestId("relink-2")).toHaveCount(0);
      await expect(page.getByTestId("quote-line-2")).toContainText(/custom/i);
      // the dropped SKU still refuses to re-link, and nothing changes on the row
      await page.getByTestId("relink-1").click();
      await expect(page.getByTestId("quote-error")).toContainText(/SKU-1003 is not in the active catalog/);
      await expect(page.getByTestId("quote-line-1")).not.toContainText(/custom/i);
    } finally {
      await restore(browser);
    }
  });

  test("RR4-3: a custom line (product link dropped by the server) is never offered 'Use active catalog'; the is_custom reset in re-link is therefore unreachable from the UI (observation); the equivalent API payload converts it to a catalog line", async ({ page, browser }) => {
    await login(page, USERS.sales);
    const p1 = await activeProduct(page, "SKU-1001");
    const id = await apiQuote(page, [{ quantity: "3", product_id: p1.id, is_custom: true }]);
    const before = await (await page.request.get(`/api/v1/quotes/${id}`)).json();
    expect(before.current_version.lines[0].is_custom).toBe(true);
    expect(before.current_version.lines[0].product_id).toBeNull(); // server drops the link on custom lines
    try {
      await adminImport(browser, v2Catalog(), "qa4-v2c.csv");
      await page.goto(`/quotes/${id}`);
      await expect(page.getByTestId("quote-line-1")).toContainText(/custom/i);
      await expect(page.getByTestId("relink-1")).toHaveCount(0);
      // what the editor would send after a re-link (product_id of the active product, is_custom false, price key omitted)
      const ln = before.current_version.lines[0];
      const active = await activeProduct(page, "SKU-1001");
      const r = await page.request.patch(`/api/v1/quotes/${id}`, { data: { expected_lock_version: before.lock_version, lines: [{ id: ln.id, product_id: active.id, sku: ln.sku, description: ln.description, quantity: ln.quantity, unit: ln.unit, discount_pct: ln.discount_pct, tax_rate_pct: ln.tax_rate_pct, is_custom: false }] }, headers: { "X-CSRF-Token": await csrf(page) } });
      expect(r.status(), await r.text()).toBe(200);
      const l2 = (await r.json()).current_version.lines[0];
      expect(l2.is_custom).toBe(false);
      expect(l2.price_source).toBe("catalog");
      expect(l2.unit_price).toBe("0.91");
      await page.reload();
      await expect(page.getByTestId("quote-line-1")).not.toContainText(/custom/i);
      await expect(page.getByTestId("quote-blockers")).toHaveCount(0);
    } finally {
      await restore(browser);
    }
  });

  test("round 7: while a save is in flight the editor is locked; typing is not lost or corrupted; no duplicate PATCH; next edit persists", async ({ page }) => {
    await login(page, USERS.sales);
    await pasteRfq(page, TWO_LINE_RFQ, `QA4-FLIGHT-${Date.now()}`);
    await generateQuote(page);
    let patches = 0;
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    await page.route("**/api/v1/quotes/*", async (route) => {
      if (route.request().method() !== "PATCH") return route.continue();
      patches += 1;
      await gate; // hold the save in flight
      await route.continue();
    });
    await page.getByTestId("unit-price-1").fill("0.86");
    await page.locator('input[aria-label="Quantity 2"]').fill("501");
    await page.getByTestId("save-draft").click();
    await expect(page.getByTestId("quote-editor")).toHaveAttribute("disabled", "");
    await expect(page.getByTestId("unit-price-1")).toBeDisabled();
    await expect(page.locator('input[aria-label="Quantity 2"]')).toBeDisabled();
    await expect(page.getByTestId("quote-line-1").getByRole("button", { name: "Remove" })).toBeDisabled();
    await expect(page.getByRole("button", { name: "Add custom line" })).toBeDisabled();
    await expect(page.getByTestId("save-draft")).toBeDisabled();
    // typing at the locked field and a forced click change nothing
    await page.getByTestId("unit-price-1").click({ force: true });
    await page.keyboard.type("777");
    await page.getByTestId("save-draft").click({ force: true, trial: false }).catch(() => undefined);
    await expect(page.getByTestId("unit-price-1")).toHaveValue("0.86");
    release();
    await expect(page.getByTestId("quote-notice")).toContainText("saved");
    await expect(page.getByTestId("quote-editor")).not.toHaveAttribute("disabled", /.*/);
    expect(patches).toBe(1);
    await expect(page.getByTestId("unit-price-1")).toHaveValue("0.86");
    await expect(page.locator('input[aria-label="Quantity 2"]')).toHaveValue("501");
    await expect(page.getByTestId("quote-line-1")).toContainText(/manual price/i);
    // a later edit is accepted and persisted (the lock_version moved on correctly)
    await page.unroute("**/api/v1/quotes/*");
    await page.getByTestId("unit-price-1").fill("0.87");
    const [saved] = await Promise.all([page.waitForResponse((r) => r.request().method() === "PATCH" && r.url().includes("/quotes/")), page.getByTestId("save-draft").click()]);
    expect(saved.status()).toBe(200);
    await expect(page.getByTestId("quote-error")).toHaveCount(0);
    await expect(page.getByTestId("unit-price-1")).toHaveValue("0.87");
    const id = page.url().split("/quotes/")[1];
    const got = await (await page.request.get(`/api/v1/quotes/${id}`)).json();
    expect(got.current_version.lines[0].unit_price).toBe("0.87");
    expect(got.current_version.lines[1].quantity).toMatch(/^501/);
  });

  test("round 7: a failed save (409 stale lock) unlocks the editor and keeps what the user typed", async ({ page }) => {
    await login(page, USERS.sales);
    await pasteRfq(page, TWO_LINE_RFQ, `QA4-FAIL-${Date.now()}`);
    await generateQuote(page);
    await page.route("**/api/v1/quotes/*", async (route) => {
      if (route.request().method() !== "PATCH") return route.continue();
      await route.fulfill({ status: 422, contentType: "application/json", body: JSON.stringify({ error: { code: "validation_error", message: "forced failure" } }) });
    });
    await page.getByTestId("unit-price-1").fill("0.86");
    await page.getByTestId("save-draft").click();
    await expect(page.getByTestId("quote-editor")).not.toHaveAttribute("disabled", /.*/);
    await expect(page.getByTestId("unit-price-1")).toHaveValue("0.86");
    await page.unroute("**/api/v1/quotes/*");
    await page.getByTestId("save-draft").click();
    await expect(page.getByTestId("quote-notice")).toContainText("saved");
    await expect(page.getByTestId("quote-line-1")).toContainText(/manual price/i);
  });
});
