/**
 * QA (independent) browser evidence for Stage 1 negative / edge flows.
 * Derived from docs/PRD.md (S1-A02, S1-A03, S1-A04, S1-A10, S1-A12, S1-A13, S1-A14, S1-A20) and docs/USER_JOURNEYS.md
 * screen states (RFQ › Review blockers, Quote › Editor missing price / override, Quote › Approval forbidden states).
 */
import { expect, test, type Page } from "@playwright/test";
import { USERS, login, logout } from "../helpers";

async function csrf(page: Page): Promise<string> {
  return (await page.context().cookies()).find((c) => c.name === "qp_csrf")!.value;
}

async function pasteRfq(page: Page, text: string, reference: string) {
  await page.goto("/rfqs/new");
  await page.getByTestId("mode-text").check();
  await page.getByTestId("rfq-text").fill(text);
  await page.getByTestId("rfq-reference").fill(reference);
  await page.getByTestId("rfq-submit").click();
  await expect(page.getByTestId("rfq-status")).toHaveText(/parsed/i);
}

test.describe("QA Stage 1 — review blockers, editor blockers, approval states", () => {
  test("duplicate, unit-mismatch and unknown lines block generation until resolved in the review screen", async ({ page }) => {
    await login(page, USERS.sales);
    await pasteRfq(
      page,
      [
        "1. 500 pcs SKU-1001 Hex bolt M8x40 zinc plated",
        "2. 500 pcs SKU-1001 Hex bolt M8x40 zinc plated",
        "3. 10 box SKU-1004 Flat washer M8",
        "4. 3 pcs SKU-9999 unknown widget",
        "",
      ].join("\n"),
      `QA-REVIEW-${Date.now()}`,
    );
    await expect(page.getByTestId("rfq-item-2")).toContainText(/possible duplicate/i);
    await expect(page.getByTestId("rfq-item-3")).toContainText(/unit mismatch/i);
    await expect(page.getByTestId("match-4")).toHaveText(/unmatched/i);
    await expect(page.getByTestId("match-1")).toHaveText(/exact/i);

    // generation is refused with precise, human-readable blockers
    await page.getByTestId("generate-quote").click();
    const blockers = page.getByTestId("rfq-blockers");
    await expect(blockers).toBeVisible();
    await expect(blockers).toContainText(/duplicate/i);
    await expect(blockers).toContainText(/unit/i);
    await expect(blockers).toContainText(/Line 4/);
    await expect(page).toHaveURL(/\/rfqs\//);

    // resolve: confirm unit, keep duplicate explicitly, remove unknown line
    await page.getByTestId("confirm-unit-3").click();
    await expect(page.getByTestId("rfq-item-3")).toContainText(/unit confirmed/i);
    await page.getByTestId("rfq-item-2").getByRole("button", { name: "Keep duplicate" }).click();
    await expect(page.getByTestId("rfq-item-2")).toContainText(/duplicate confirmed/i);
    await page.getByTestId("remove-4").click();
    await expect(page.getByTestId("rfq-item-4")).toHaveCount(0);

    await page.getByTestId("generate-quote").click();
    await expect(page).toHaveURL(/\/quotes\//);
    await expect(page.getByTestId("quote-status")).toHaveText(/draft/i);
    await expect(page.getByTestId("quote-lines").locator("tbody tr")).toHaveCount(3);
    await expect(page.getByTestId("quote-line-3")).toContainText("SKU-1004");
    await expect(page.getByTestId("quote-line-3")).toContainText("EA"); // catalog unit, not the RFQ's "box"
    await expect(page.getByTestId("line-total-1")).toHaveText("446.25");
    await expect(page.getByTestId("line-total-2")).toHaveText("446.25"); // confirmed duplicate kept
    await expect(page.getByTestId("quote-blockers")).toHaveCount(0);
  });

  test("custom line without a price blocks submission; entering a price unblocks it", async ({ page }) => {
    await login(page, USERS.sales);
    await page.goto("/quotes");
    const created = await page.request.post("/api/v1/quotes", { data: {}, headers: { "X-CSRF-Token": await csrf(page) } });
    expect(created.status()).toBe(201);
    const q = await created.json();
    await page.goto(`/quotes/${q.id}`);
    await expect(page.getByTestId("quote-blockers")).toContainText(/no lines/i);
    await expect(page.getByTestId("submit-quote")).toBeDisabled();

    await page.getByRole("button", { name: "Add custom line" }).click();
    await page.locator('input[aria-label="Description 1"]').fill("Special bracket, powder coated");
    await page.locator('input[aria-label="Quantity 1"]').fill("2");
    await page.getByTestId("save-draft").click();
    await expect(page.getByTestId("quote-notice")).toContainText("saved");
    await expect(page.getByTestId("quote-blockers")).toContainText(/no unit price/i);
    await expect(page.getByTestId("submit-quote")).toBeDisabled();
    await expect(page.getByTestId("quote-line-1")).toContainText(/custom/i);

    await page.getByTestId("unit-price-1").fill("15");
    await page.getByTestId("save-draft").click();
    await expect(page.getByTestId("quote-notice")).toContainText("saved");
    await expect(page.getByTestId("quote-blockers")).toHaveCount(0);
    await expect(page.getByTestId("line-total-1")).toHaveText("31.50"); // 2 × 15 + 5 % tax
    await expect(page.getByTestId("quote-line-1")).toContainText(/manual price/i);
    await expect(page.getByTestId("submit-quote")).toBeEnabled();
    await page.getByTestId("submit-quote").click();
    await expect(page.getByTestId("version-status")).toHaveText(/pending approval/i);
  });

  test("below-margin price needs a privileged, reasoned override that stays visible to sales", async ({ page, browser }) => {
    await login(page, USERS.admin);
    await pasteRfq(page, "10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n", `QA-MARGIN-${Date.now()}`);
    await page.getByTestId("generate-quote").click();
    await expect(page).toHaveURL(/\/quotes\//);
    const quoteUrl = page.url();
    await page.getByTestId("unit-price-1").fill("0.55"); // cost 0.52 → ~5 % margin < 12 % floor
    await page.getByTestId("save-draft").click();
    await expect(page.getByTestId("quote-notice")).toContainText("saved");
    const blockers = page.getByTestId("quote-blockers");
    await expect(blockers).toContainText(/below the organisation floor/i);
    await expect(blockers).toContainText(/override by owner\/admin/i);
    await expect(page.getByTestId("submit-quote")).toBeDisabled();
    await expect(page.getByTestId("override-1")).toBeDisabled(); // reason required
    await page.getByTestId("override-reason").fill("strategic account, approved by GM");
    await page.getByTestId("override-1").click();
    await expect(page.getByTestId("quote-notice")).toContainText(/override recorded/i);
    await expect(page.getByTestId("quote-blockers")).toHaveCount(0);
    await expect(page.getByTestId("quote-line-1")).toContainText(/Margin override: strategic account/);
    await expect(page.getByTestId("audit-table")).toContainText("quote.margin_override");
    await expect(page.getByTestId("submit-quote")).toBeEnabled();
    await page.getByTestId("submit-quote").click();
    await expect(page.getByTestId("version-status")).toHaveText(/pending approval/i);

    // sales sees the override reason but never the margin column
    const ctx = await browser.newContext();
    const salesPage = await ctx.newPage();
    await login(salesPage, USERS.sales);
    await salesPage.goto(quoteUrl);
    await expect(salesPage.getByTestId("quote-line-1")).toContainText(/Margin override/);
    await expect(salesPage.locator("th", { hasText: "Margin" })).toHaveCount(0);
    await expect(salesPage.getByTestId("approve-quote")).toHaveCount(0);
    await ctx.close();
  });

  test("submitter cannot approve own version; owner approves; new version; read-only sees history and PDF", async ({ page, browser }) => {
    await login(page, USERS.approver);
    await pasteRfq(page, "2 ea SKU-3002 Lithium grease EP2 pail 18kg\n", `QA-SOD-${Date.now()}`);
    await page.getByTestId("generate-quote").click();
    await expect(page).toHaveURL(/\/quotes\//);
    const quoteUrl = page.url();
    const quoteId = quoteUrl.split("/quotes/")[1];
    await page.getByTestId("submit-quote").click();
    await expect(page.getByTestId("version-status")).toHaveText(/pending approval/i);
    await expect(page.getByTestId("self-approval-blocked")).toBeVisible();
    await expect(page.getByTestId("approve-quote")).toHaveCount(0);
    // even a direct API call is refused server-side (S1-A13)
    const direct = await page.request.post(`/api/v1/quotes/${quoteId}/approve`, {
      data: { expected_lock_version: 999 },
      headers: { "X-CSRF-Token": await csrf(page) },
    });
    expect(direct.status()).toBe(403);
    expect((await direct.json()).action).toBe("self_approval");

    const ownerCtx = await browser.newContext();
    const owner = await ownerCtx.newPage();
    await login(owner, USERS.owner);
    await owner.goto(quoteUrl);
    await owner.getByTestId("decision-reason").fill("Checked margins");
    await owner.getByTestId("approve-quote").click();
    await expect(owner.getByTestId("quote-status")).toHaveText(/approved/i);
    await ownerCtx.close();

    // the approver can now create a new draft version; v1 stays approved in history
    await page.reload();
    await expect(page.getByTestId("quote-status")).toHaveText(/approved/i);
    await page.getByTestId("new-version").click();
    await expect(page.getByTestId("quote-notice")).toContainText(/New draft version/i);
    await expect(page.getByTestId("version-status")).toHaveText(/draft/i);
    await expect(page.getByRole("heading", { level: 1 })).toContainText("v2");
    const v1pdf = await page.request.get(`/api/v1/quotes/${quoteId}/versions/1/pdf`);
    expect(v1pdf.status()).toBe(200);
    expect(v1pdf.headers()["content-type"]).toBe("application/pdf");

    // read-only: history and approved PDF visible, no actions
    const roCtx = await browser.newContext();
    const ro = await roCtx.newPage();
    await login(ro, USERS.readonly);
    await ro.goto(quoteUrl);
    await expect(ro.getByTestId("audit-table")).toContainText("quote.approve");
    await expect(ro.getByTestId("audit-table")).toContainText("quote.new_version");
    await expect(ro.getByTestId("submit-quote")).toHaveCount(0);
    await expect(ro.getByTestId("approve-quote")).toHaveCount(0);
    await expect(ro.getByTestId("new-version")).toHaveCount(0);
    await expect(ro.getByTestId("save-draft")).toHaveCount(0);
    await expect(ro.locator("th", { hasText: "Margin" })).toHaveCount(0);
    const roPdf = await ro.request.get(`/api/v1/quotes/${quoteId}/versions/1/pdf`);
    expect(roPdf.status()).toBe(200);
    expect(Buffer.compare(await roPdf.body(), await v1pdf.body())).toBe(0); // S1-A18 across users
    const roCsv = await ro.request.get(`/api/v1/quotes/${quoteId}/versions/1/csv`);
    expect(roCsv.status()).toBe(200);
    expect(await roCsv.text()).not.toContain("cost_price");
    await roCtx.close();
  });

  test("another tenant cannot open an RFQ or download its original file; the UI shows a not-found state", async ({ page, browser }) => {
    await login(page, USERS.sales);
    const content = "Item,Part No,Qty,UOM\nHex bolt M8x40 zinc plated,SKU-1001,5,pcs\n";
    const form = await page.request.post("/api/v1/rfqs", {
      multipart: { file: { name: "qa.csv", mimeType: "text/csv", buffer: Buffer.from(content) } },
      headers: { "X-CSRF-Token": await csrf(page) },
    });
    expect(form.status()).toBe(201);
    const rfq = await form.json();
    expect(rfq.status).toBe("parsed");
    const ownFile = await page.request.get(`/api/v1/files/${rfq.source_file_id}`);
    expect(ownFile.status()).toBe(200);
    expect((await ownFile.body()).toString()).toBe(content);

    const other = await browser.newContext();
    const otherPage = await other.newPage();
    await login(otherPage, USERS.otherOwner);
    expect((await otherPage.request.get(`/api/v1/rfqs/${rfq.id}`)).status()).toBe(404);
    expect((await otherPage.request.get(`/api/v1/files/${rfq.source_file_id}`)).status()).toBe(404);
    await otherPage.goto(`/rfqs/${rfq.id}`);
    await expect(otherPage.getByRole("alert").filter({ hasText: /not found/i })).toBeVisible();
    await other.close();
    await logout(page);
  });

  test("login is rate limited after repeated failures and the UI shows the error state", async ({ page }) => {
    const email = `qa-ratelimit-${Date.now()}@northwind.example`;
    await page.goto("/login");
    const statuses: number[] = [];
    for (let i = 0; i < 11; i++) {
      await page.getByTestId("login-email").fill(email);
      await page.getByTestId("login-password").fill("definitely-wrong-password");
      const [res] = await Promise.all([
        page.waitForResponse((r) => r.url().includes("/api/v1/auth/login") && r.request().method() === "POST"),
        page.getByTestId("login-submit").click(),
      ]);
      statuses.push(res.status());
      await expect(page.getByTestId("login-error")).toBeVisible();
    }
    expect(statuses.slice(0, 10)).toEqual(Array(10).fill(401));
    expect(statuses[10]).toBe(429);
    await expect(page.getByTestId("login-error")).toContainText(/too many/i);
    await page.goto("/dashboard");
    await expect(page).toHaveURL(/\/login/);
  });
});
