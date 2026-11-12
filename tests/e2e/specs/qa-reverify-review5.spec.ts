/**
 * QA (independent) browser re-verification 5 of Stage 1 at 3586da2 (fix round 8): RFQ quantity cap (SEC4-6) in the
 * review UI. Browser evidence (Chromium against real uvicorn + Next.js). The RFQ is created through the API (setup);
 * everything asserted is read from the screen.
 */
import { expect, test, type Page } from "@playwright/test";
import { USERS, login } from "../helpers";

async function csrf(page: Page): Promise<string> {
  return (await page.context().cookies()).find((c) => c.name === "qp_csrf")!.value;
}

async function createRfq(page: Page, csv: string): Promise<string> {
  const customers = await (await page.request.get("/api/v1/customers")).json();
  const r = await page.request.post("/api/v1/rfqs", {
    multipart: { file: { name: "qa5.csv", mimeType: "text/csv", buffer: Buffer.from(csv) }, customer_id: customers[0].id },
    headers: { "X-CSRF-Token": await csrf(page) },
  });
  expect(r.status(), await r.text()).toBe(201);
  return (await r.json()).id;
}

test.describe("QA re-verification 5 — RFQ quantity cap in the review UI", () => {
  test("SEC4-6: an out-of-range quantity is empty and flagged, generate-quote is refused until a person enters one, then the flag clears and the quote carries that quantity", async ({ page }) => {
    await login(page, USERS.sales);
    const id = await createRfq(page, "SKU,Description,Qty,Unit\nSKU-1001,Hex bolt M8x40 zinc plated,1e12,EA\nSKU-1003,Hex nut M8 zinc plated,5,EA\n");
    await page.goto(`/rfqs/${id}`);
    const row1 = page.getByTestId("rfq-item-1");
    await expect(row1).toContainText(/quantity out of range/i);
    await expect(row1).toContainText(/Quantity required/);
    await expect(page.getByTestId("qty-1")).toHaveValue("");
    await expect(page.getByTestId("qty-2")).toHaveValue(/^5/);
    await expect(page.getByTestId("rfq-item-2")).not.toContainText(/quantity out of range/i);
    // generate is refused while the quantity is missing
    await page.getByTestId("generate-quote").click();
    await expect(page).toHaveURL(new RegExp(`/rfqs/${id}$`));
    await expect(page.getByText(/quantity/i).first()).toBeVisible();
    const noQuote = await (await page.request.get("/api/v1/quotes")).json();
    expect(noQuote.some((q: { rfq_id?: string }) => q.rfq_id === id)).toBe(false);
    // typing a value above the cap is refused by the server and the field does not pretend it was saved
    await page.getByTestId("qty-1").fill("10000001");
    await page.getByTestId("qty-1").blur();
    await expect(row1).toContainText(/quantity out of range/i);
    // a legitimate quantity clears the flag
    await page.getByTestId("qty-1").fill("250");
    await page.getByTestId("qty-1").blur();
    await expect(row1).not.toContainText(/quantity out of range/i);
    await expect(row1).not.toContainText(/Quantity required/);
    await page.getByTestId("generate-quote").click();
    await expect(page).toHaveURL(/\/quotes\//);
    const qid = page.url().split("/quotes/")[1];
    const quote = await (await page.request.get(`/api/v1/quotes/${qid}`)).json();
    const qtys = quote.current_version.lines.map((l: { quantity: string }) => Number(l.quantity)).sort((a: number, b: number) => a - b);
    expect(qtys).toEqual([5, 250]);
  });

  test("SEC4-6: exactly 10,000,000 is accepted unflagged; 0 and negative are flagged not-positive and empty", async ({ page }) => {
    await login(page, USERS.sales);
    const id = await createRfq(page, "SKU,Description,Qty,Unit\nSKU-1001,Hex bolt M8x40 zinc plated,10000000,EA\nSKU-1003,Hex nut M8 zinc plated,0,EA\nSKU-3002,Lithium grease EP2 pail 18kg,-4,EA\n");
    await page.goto(`/rfqs/${id}`);
    await expect(page.getByTestId("qty-1")).toHaveValue(/^10000000/);
    await expect(page.getByTestId("rfq-item-1")).not.toContainText(/quantity (out of range|not positive)/i);
    await expect(page.getByTestId("qty-2")).toHaveValue("");
    await expect(page.getByTestId("rfq-item-2")).toContainText(/quantity not positive/i);
    await expect(page.getByTestId("qty-3")).toHaveValue("");
    await expect(page.getByTestId("rfq-item-3")).toContainText(/quantity not positive/i);
  });
});
