/**
 * QA (independent) browser re-verification 7 of Stage 1 at 8258112 (fix round 10): what a person sees in the RFQ review
 * screen for a line whose fuzzy matching was skipped (flag `match_skipped_budget`). Browser evidence (Chromium against
 * real uvicorn + Next.js). The catalog (5,000 products) and the RFQ (405 lines) are created through the API (setup); the
 * demo catalog is restored in `finally`.
 */
import { expect, test, type Browser, type Page } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { USERS, login } from "../helpers";

const DEMO_CATALOG = path.resolve(__dirname, "../../../apps/api/app/fixtures/data/demo_catalog.csv");
const MAPPING = JSON.stringify({ sku: "SKU", description: "Description", unit: "Unit", currency: "Currency", list_price: "List Price", cost_price: "Cost Price" });

async function csrf(page: Page): Promise<string> {
  return (await page.context().cookies()).find((c) => c.name === "qp_csrf")!.value;
}

async function importAndActivate(page: Page, csv: string, name: string) {
  const imp = await page.request.post("/api/v1/catalogs/import", { multipart: { file: { name, mimeType: "text/csv", buffer: Buffer.from(csv) }, mapping: MAPPING, catalog_name: "Main catalog" }, headers: { "X-CSRF-Token": await csrf(page) }, timeout: 120_000 });
  expect(imp.status(), await imp.text()).toBe(201);
  const act = await page.request.post(`/api/v1/catalogs/versions/${(await imp.json()).id}/activate`, { headers: { "X-CSRF-Token": await csrf(page) } });
  expect(act.status(), await act.text()).toBe(200);
}

async function asAdmin(browser: Browser, csv: string, name: string) {
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  await login(page, USERS.admin);
  await importAndActivate(page, csv, name);
  await ctx.close();
}

test("round 10: a line beyond the matching budget shows the flag, 'no candidate' text and the custom/remove actions; an exact SKU line after it still matches", async ({ page, browser }) => {
  test.setTimeout(300_000);
  const nouns = ["bolt", "nut", "washer", "screw", "cable", "grease", "gasket", "valve", "relay", "fuse"];
  const adj = ["hex", "flat", "zinc plated", "stainless", "brass", "nylon", "M8", "M10", "DN25", "12V"];
  let big = "SKU,Description,Unit,Currency,List Price,Cost Price\n";
  for (let i = 0; i < 5000; i++) big += `QA7-${1000 + i},${nouns[i % 10]} ${adj[(i * 7) % 10]} ${adj[(i * 3) % 10]} v${i},EA,AED,${1 + (i % 9)}.50,0.50\n`;
  try {
    await asAdmin(browser, big, "qa7-big.csv");
    await login(page, USERS.sales);
    const customers = await (await page.request.get("/api/v1/customers")).json();
    const lines: string[] = [];
    for (let i = 0; i < 405; i++) lines.push(`${(i % 90) + 1} pcs hex bolt zinc plated variant ${i}`);
    lines.push("10 pcs QA7-1007 some bolt");
    const r = await page.request.post("/api/v1/rfqs", { data: { text: lines.join("\n"), customer_id: customers[0].id }, headers: { "X-CSRF-Token": await csrf(page) }, timeout: 240_000 });
    expect(r.status(), (await r.text()).slice(0, 300)).toBe(201);
    const rfq = await r.json();
    await page.goto(`/rfqs/${rfq.id}`);
    const row = page.getByTestId("rfq-item-401");
    await expect(row).toBeVisible({ timeout: 60_000 });
    // what the user sees
    await expect(row).toContainText(/match skipped budget/i);
    await expect(page.getByTestId("match-401")).toHaveText(/unmatched/i);
    await expect(row).toContainText(/No catalog candidate/);
    await expect(page.getByTestId("select-401")).toHaveCount(0); // the UI offers no product choice for a line without candidates
    await expect(row.getByRole("button", { name: /Mark custom/ })).toBeVisible();
    // lines inside the budget carry no flag; the exact SKU line after the budget still matches
    await expect(page.getByTestId("rfq-item-400")).not.toContainText(/match skipped budget/i);
    await expect(page.getByTestId("match-406")).toHaveText(/exact/i);
    await expect(page.getByTestId("rfq-item-406")).not.toContainText(/match skipped budget/i);
    // generate-quote is refused while skipped lines are unresolved
    await page.getByTestId("generate-quote").click();
    await expect(page).toHaveURL(new RegExp(`/rfqs/${rfq.id}$`));
    // the page text does not explain the budget (observation recorded in the evidence file)
    expect(await page.getByText(/budget/i).count()).toBeGreaterThan(0); // only the badge text
    expect(await page.getByText(/too many|large catalog|skipped because/i).count()).toBe(0);
  } finally {
    await asAdmin(browser, fs.readFileSync(DEMO_CATALOG, "utf8"), "qa7-restore.csv");
  }
});
