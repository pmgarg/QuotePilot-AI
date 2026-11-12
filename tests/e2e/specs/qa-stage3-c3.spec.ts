/**
 * QA (independent) browser check for Stage 3 release review 1, condition C3: what a sales user sees in the web UI for a
 * customer whose erasure was approved (soft-deleted, purge pending). Expected: the customer is gone from the customers
 * list, the profile page shows an error state (no crash, no name), the quote list withholds the name (and a name
 * search finds nothing), the quote itself stays readable, and an admin (manage_retention) still sees the name.
 */
import { expect, test, type Page } from "@playwright/test";
import { CLEAN_RFQ, USERS, login } from "../helpers";

async function csrf(page: Page): Promise<string> {
  return (await page.context().cookies()).find((c) => c.name === "qp_csrf")!.value;
}

test.describe("QA Stage 3 C3 — customer pending erasure in the UI", () => {
  test("sales sees a not-found state and no customer name; admin still sees the name on the quote list", async ({ page, browser }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));

    const name = `Pending Erasure Co ${Date.now()}`;
    const adminCtx = await browser.newContext();
    const admin = await adminCtx.newPage();
    await login(admin, USERS.admin);
    const aToken = await csrf(admin);
    const created = await admin.request.post("/api/v1/customers", { data: { name, reference: `PE-${Date.now().toString().slice(-7)}` }, headers: { "X-CSRF-Token": aToken } });
    expect(created.status(), await created.text()).toBe(201);
    const customer = await created.json();

    await login(page, USERS.sales);
    const sToken = await csrf(page);
    const rfq = await (await page.request.post("/api/v1/rfqs", { data: { text: CLEAN_RFQ, customer_id: customer.id }, headers: { "X-CSRF-Token": sToken } })).json();
    const qRes = await page.request.post(`/api/v1/rfqs/${rfq.id}/generate-quote`, { headers: { "X-CSRF-Token": sToken } });
    expect(qRes.status(), await qRes.text()).toBe(201);
    const quote = await qRes.json();
    // precondition: before the erasure the sales user sees the name on the quote list
    await page.goto("/quotes");
    await expect(page.getByTestId("quote-table")).toContainText(name);

    const dr = await page.request.post("/api/v1/retention/requests", { data: { customer_id: customer.id, reason: "erasure requested by the customer" }, headers: { "X-CSRF-Token": sToken } });
    expect(dr.status(), await dr.text()).toBe(201);
    const ap = await admin.request.post(`/api/v1/retention/requests/${(await dr.json()).id}/approve`, { data: {}, headers: { "X-CSRF-Token": aToken } });
    expect(ap.status(), await ap.text()).toBe(200);

    // customers list: not shown
    await page.goto("/customers");
    await expect(page.getByTestId("customer-table")).toBeVisible();
    await expect(page.getByTestId("customer-table")).not.toContainText(name);
    await expect(page.locator("body")).not.toContainText(name);

    // profile page: error state, no crash, no name
    await page.goto(`/customers/${customer.id}`);
    await expect(page.getByRole("alert").first()).toBeVisible();
    await expect(page.locator("body")).not.toContainText(name);
    await expect(page.getByTestId("generate-proposal")).toHaveCount(0);
    await expect(page.getByTestId("request-deletion")).toHaveCount(0);

    // quote list: the quote stays, the name is withheld, a name search finds nothing
    await page.goto("/quotes");
    const row = page.getByTestId("quote-table").locator("tbody tr").filter({ hasText: quote.number });
    await expect(row).toHaveCount(1);
    await expect(row).not.toContainText(name);
    await expect(page.locator("body")).not.toContainText(name);
    await page.getByTestId("quote-search").fill("Pending Erasure Co");
    await page.getByTestId("quote-search").press("Enter"); // the search runs on submit
    await expect(page.getByTestId("quote-table").locator("tbody tr").filter({ hasText: quote.number })).toHaveCount(0);
    await expect(page.locator("body")).not.toContainText(name);

    // quote detail: readable, no name; "convert to order" is not offered or is refused with an error, never a crash
    await page.goto(`/quotes/${quote.id}`);
    await expect(page.getByText(quote.number).first()).toBeVisible();
    await expect(page.locator("body")).not.toContainText(name);

    // admin (manage_retention) still sees the name on the quote list
    await admin.goto("/quotes");
    await expect(admin.getByTestId("quote-table")).toContainText(name);
    await adminCtx.close();

    expect(errors, `uncaught page errors: ${errors.join(" | ")}`).toEqual([]);
  });
});
