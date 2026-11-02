/**
 * QA (independent) browser re-verification of release review 1 items on HEAD ff99762.
 * Derived from docs/PRD.md (S1-A03, S1-A13, S1-A18, S1-A20, S1-F12, S1-F13, S1-F16) and
 * docs/evidence/release-review-1-stage1-2026-11-01.md (H-1, M-3, M-4, X-08). Browser evidence (Chromium against
 * real uvicorn + Next.js). Each test exercises the real screens; API calls are used only to set up state and to read
 * export bytes, as a customer-facing download would.
 */
import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Browser, type Page } from "@playwright/test";
import { CLEAN_RFQ, USERS, login } from "../helpers";

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

async function approveViaUi(browser: Browser, quoteUrl: string) {
  const { ctx, page } = await asUser(browser, USERS.approver);
  await page.goto(quoteUrl);
  await page.getByTestId("decision-reason").fill("ok");
  await page.getByTestId("approve-quote").click();
  await expect(page.getByTestId("quote-status")).toHaveText(/approved/i);
  await ctx.close();
}

test.describe("QA re-verification — review 1 items in the browser", () => {
  test("M-4: a catalog price typed over in the editor needs an owner/admin confirmation; approvers cannot clear it", async ({ page, browser }) => {
    await login(page, USERS.sales);
    await pasteRfq(page, "1. 10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n", `QA-M4-${Date.now()}`);
    const quoteId = await generateQuote(page);
    await expect(page.getByTestId("quote-blockers")).toHaveCount(0);
    await expect(page.getByTestId("submit-quote")).toBeEnabled();

    // Sales types a *higher* price over the catalog price (no margin problem at all)
    await page.getByTestId("unit-price-1").fill("5.00");
    await page.getByTestId("save-draft").click();
    await expect(page.getByTestId("quote-notice")).toContainText("saved");
    await expect(page.getByTestId("line-total-1")).toHaveText("52.50"); // 10 × 5 + 5 % tax
    await expect(page.getByTestId("quote-line-1")).toContainText(/manual price/i);
    const blockers = page.getByTestId("quote-blockers");
    await expect(blockers).toContainText(/manually entered price/i);
    await expect(blockers).toContainText(/owner\/admin/i);
    await expect(blockers).not.toContainText("%"); // no margin figures for Sales
    await expect(page.getByTestId("submit-quote")).toBeDisabled();
    await expect(page.getByTestId("override-1")).toHaveCount(0); // Sales has no confirmation control

    // an approver sees the blocker but has no confirmation control; a direct API attempt is refused
    const ap = await asUser(browser, USERS.approver);
    await ap.page.goto(`/quotes/${quoteId}`);
    await expect(ap.page.getByTestId("quote-blockers")).toContainText(/manually entered price/i);
    await expect(ap.page.getByTestId("override-1")).toHaveCount(0);
    await expect(ap.page.getByTestId("override-reason")).toHaveCount(0);
    const q = await (await ap.page.request.get(`/api/v1/quotes/${quoteId}`)).json();
    const line = q.current_version.lines[0];
    const denied = await ap.page.request.post(`/api/v1/quotes/${quoteId}/lines/${line.id}/price-override`, {
      data: { expected_lock_version: q.lock_version, reason: "approver says fine" },
      headers: { "X-CSRF-Token": await csrf(ap.page) },
    });
    expect(denied.status()).toBe(403);
    await ap.ctx.close();

    // the owner confirms with a reason; the reason is required (button disabled until ≥ 5 chars)
    const ow = await asUser(browser, USERS.owner);
    await ow.page.goto(`/quotes/${quoteId}`);
    await expect(ow.page.getByTestId("override-1")).toBeDisabled();
    await ow.page.getByTestId("override-reason").fill("frame agreement price confirmed with customer");
    await ow.page.getByTestId("override-1").click();
    await expect(ow.page.getByTestId("quote-notice")).toContainText(/price confirmed/i);
    await expect(ow.page.getByTestId("quote-blockers")).toHaveCount(0);
    await expect(ow.page.getByTestId("audit-table")).toContainText("quote.margin_override");
    await ow.ctx.close();

    // Sales can now submit; the confirmation is visible on the line
    await page.reload();
    await expect(page.getByTestId("quote-blockers")).toHaveCount(0);
    await expect(page.getByTestId("quote-line-1")).toContainText(/Price confirmed by owner\/admin/);
    await expect(page.getByTestId("submit-quote")).toBeEnabled();
    await page.getByTestId("submit-quote").click();
    await expect(page.getByTestId("version-status")).toHaveText(/pending approval/i);
  });

  test("H-1: organisation and customer edits made in the UI after approval never change the approved v1 document", async ({ page, browser }) => {
    await login(page, USERS.sales);
    await pasteRfq(page, CLEAN_RFQ, `QA-H1-${Date.now()}`);
    const quoteId = await generateQuote(page);
    const quoteUrl = page.url();
    await page.getByTestId("submit-quote").click();
    await expect(page.getByTestId("version-status")).toHaveText(/pending approval/i);
    await approveViaUi(browser, quoteUrl);
    await page.reload();
    await expect(page.getByTestId("quote-status")).toHaveText(/approved/i);
    const before = await (await page.request.get(`/api/v1/quotes/${quoteId}/versions/1/pdf`)).body();
    const csvBefore = await (await page.request.get(`/api/v1/quotes/${quoteId}/versions/1/csv`)).text();
    const q1 = await (await page.request.get(`/api/v1/quotes/${quoteId}`)).json();
    const sha1 = q1.versions.find((v: { version_no: number }) => v.version_no === 1).pdf_sha256;
    expect(sha1).toBeTruthy();

    const org = await (await page.request.get("/api/v1/org")).json();
    const customers = await (await page.request.get("/api/v1/customers")).json();
    const customer = customers[0];
    const ow = await asUser(browser, USERS.owner);
    try {
      // owner edits the letterhead in Settings
      await ow.page.goto("/settings");
      await ow.page.locator("#legal_name").fill("Northwind Rebranded L.L.C. (QA)");
      await ow.page.locator("#pdf_footer").fill("QA FOOTER CHANGED AFTER APPROVAL");
      const saved = ow.page.waitForResponse((r) => r.url().includes("/api/v1/org") && r.request().method() === "PATCH");
      await ow.page.getByTestId("org-save").click();
      expect((await saved).status()).toBe(200);
      // sales renames the customer in the Customers screen
      await page.goto("/customers");
      await page.getByTestId("customer-table").locator("tr", { hasText: customer.name }).getByRole("button", { name: "Edit" }).click();
      await page.getByTestId("customer-name").fill("QA Renamed Customer");
      const savedCustomer = page.waitForResponse((r) => r.url().includes(`/api/v1/customers/${customer.id}`) && r.request().method() === "PATCH");
      await page.getByTestId("customer-save").click();
      expect((await savedCustomer).status()).toBe(200);
      await expect(page.getByTestId("customer-table")).toContainText("QA Renamed Customer");
      // sales opens a new version and edits it
      await page.goto(quoteUrl);
      await page.getByTestId("new-version").click();
      await expect(page.getByTestId("quote-notice")).toContainText(/new draft version/i);
      await expect(page.getByTestId("version-status")).toHaveText(/draft/i);
      await page.getByTestId("unit-price-1").fill("0.90");
      await page.getByTestId("save-draft").click();
      await expect(page.getByTestId("quote-notice")).toContainText("saved");

      // the approved v1 document is byte-identical for every role; the draft v2 preview differs
      for (const email of [USERS.sales, USERS.readonly, USERS.approver]) {
        const u = await asUser(browser, email);
        const again = await u.page.request.get(`/api/v1/quotes/${quoteId}/versions/1/pdf`);
        expect(again.status()).toBe(200);
        expect(Buffer.compare(before, await again.body())).toBe(0);
        const csvAgain = await (await u.page.request.get(`/api/v1/quotes/${quoteId}/versions/1/csv`)).text();
        if (email === USERS.approver) {
          // approvers get the extra confidential cost column (S1-F16); the approved rows themselves are unchanged
          expect(csvAgain).toContain("cost_price");
          for (const row of csvBefore.split("\n").slice(1).filter(Boolean)) expect(csvAgain).toContain(row.replace(/\r$/, ""));
        } else {
          expect(csvAgain).toBe(csvBefore);
        }
        await u.ctx.close();
      }
      const q2 = await (await page.request.get(`/api/v1/quotes/${quoteId}`)).json();
      expect(q2.versions.find((v: { version_no: number }) => v.version_no === 1).pdf_sha256).toBe(sha1);
      const draft = await (await page.request.get(`/api/v1/quotes/${quoteId}/versions/2/pdf`)).body();
      expect(Buffer.compare(before, draft)).not.toBe(0);
      // the versions card still offers the v1 PDF from history
      await expect(page.locator(`a[href="/api/v1/quotes/${quoteId}/versions/1/pdf"]`)).toHaveCount(1);
      await expect(page.getByTestId("download-pdf")).toHaveAttribute("href", `/api/v1/quotes/${quoteId}/versions/2/pdf`); // header offers the draft preview
    } finally {
      // restore the shared demo data for the other specs
      await ow.page.request.patch("/api/v1/org", {
        data: { legal_name: org.legal_name, pdf_footer: org.pdf_footer },
        headers: { "X-CSRF-Token": await csrf(ow.page) },
      });
      await page.request.patch(`/api/v1/customers/${customer.id}`, {
        data: { name: customer.name, reference: customer.reference, email: customer.email, default_currency: customer.default_currency, notes: customer.notes },
        headers: { "X-CSRF-Token": await csrf(page) },
      });
      await ow.ctx.close();
    }
  });

  test("S1-F11: removing a line that is not the last one in the editor saves cleanly", async ({ page }) => {
    await login(page, USERS.sales);
    await pasteRfq(page, CLEAN_RFQ, `QA-REMOVE-${Date.now()}`);
    await generateQuote(page);
    await expect(page.getByTestId("quote-lines").locator("tbody tr")).toHaveCount(3);
    await page.getByTestId("quote-line-1").getByRole("button", { name: "Remove" }).click();
    await expect(page.getByTestId("quote-lines").locator("tbody tr")).toHaveCount(2);
    await page.getByTestId("save-draft").click();
    await expect(page.getByTestId("quote-error")).toHaveCount(0);
    await expect(page.getByTestId("quote-notice")).toContainText("saved");
    await page.reload();
    await expect(page.getByTestId("quote-lines").locator("tbody tr")).toHaveCount(2);
    await expect(page.getByTestId("quote-line-1")).toContainText("SKU-1003"); // the former line 2 is now line 1
    await expect(page.getByTestId("grand-total")).toHaveText("729.75"); // 131.25 + 598.50
  });

  test("M-3: read-only and sales blocker text carries no margin figures; approver sees them", async ({ page, browser }) => {
    await login(page, USERS.sales);
    await pasteRfq(page, "1. 10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n", `QA-M3-${Date.now()}`);
    const quoteId = await generateQuote(page);
    await page.getByTestId("discount-1").fill("40");
    await page.getByTestId("save-draft").click();
    await expect(page.getByTestId("quote-notice")).toContainText("saved");
    const salesBlockers = page.getByTestId("quote-blockers");
    await expect(salesBlockers).toContainText(/minimum margin policy/i);
    await expect(salesBlockers).not.toContainText("%");
    await expect(page.getByTestId("submit-quote")).toBeDisabled();

    const ro = await asUser(browser, USERS.readonly);
    await ro.page.goto(`/quotes/${quoteId}`);
    await expect(ro.page.getByTestId("quote-blockers")).toBeVisible();
    await expect(ro.page.getByTestId("quote-blockers")).not.toContainText("%");
    await expect(ro.page.locator("th", { hasText: "Margin" })).toHaveCount(0);
    await expect(ro.page.getByTestId("save-draft")).toHaveCount(0);
    await ro.ctx.close();

    const ap = await asUser(browser, USERS.approver);
    await ap.page.goto(`/quotes/${quoteId}`);
    await expect(ap.page.getByTestId("quote-blockers")).toContainText(/\d+(\.\d+)?%/);
    await expect(ap.page.locator("th", { hasText: "Margin" })).toHaveCount(1);
    await ap.ctx.close();
  });

  test("X-08: login, review, editor (with blockers), approval and settings screens have no serious/critical axe violations", async ({ page, browser }) => {
    // every screen is scanned before the assertion so one run lists every gating violation
    const gating: Record<string, string[]> = {};
    const scan = async (p: Page, label: string) => {
      await p.waitForLoadState("networkidle");
      const results = await new AxeBuilder({ page: p }).withTags(["wcag2a", "wcag2aa"]).analyze();
      const serious = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
      if (serious.length > 0) gating[label] = serious.map((v) => `${v.id} (${v.impact}): ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`);
      return results.violations.filter((v) => v.impact === "moderate" || v.impact === "minor").map((v) => v.id);
    };
    const minor: Record<string, string[]> = {};
    await page.goto("/login");
    minor["/login"] = await scan(page, "/login");
    await login(page, USERS.sales);
    await pasteRfq(page, ["1. 500 pcs SKU-1001 Hex bolt M8x40 zinc plated", "2. 10 box SKU-1004 Flat washer M8", "3. 3 pcs SKU-9999 unknown widget", ""].join("\n"), `QA-A11Y-${Date.now()}`);
    await page.getByTestId("generate-quote").click();
    await expect(page.getByTestId("rfq-blockers")).toBeVisible();
    minor["rfq review with blockers"] = await scan(page, "rfq review with blockers");
    await page.getByTestId("confirm-unit-2").click();
    await page.getByTestId("remove-3").click();
    const quoteId = await generateQuote(page);
    await page.getByTestId("unit-price-1").fill("0.40");
    await page.getByTestId("save-draft").click();
    await expect(page.getByTestId("quote-blockers")).toBeVisible();
    minor["quote editor with blockers (sales)"] = await scan(page, "quote editor with blockers (sales)");

    const ow = await asUser(browser, USERS.owner);
    await ow.page.goto(`/quotes/${quoteId}`);
    await expect(ow.page.getByTestId("override-reason")).toBeVisible();
    minor["quote editor with override controls (owner)"] = await scan(ow.page, "quote editor with override controls (owner)");
    await ow.page.goto("/settings");
    await expect(ow.page.getByTestId("org-save")).toBeVisible();
    minor["/settings (owner)"] = await scan(ow.page, "/settings (owner)");
    await ow.ctx.close();

    const ro = await asUser(browser, USERS.readonly);
    await ro.page.goto(`/quotes/${quoteId}`);
    await expect(ro.page.getByTestId("quote-blockers")).toBeVisible();
    minor["quote (read-only)"] = await scan(ro.page, "quote (read-only)");
    await ro.ctx.close();
    test.info().annotations.push({ type: "axe moderate/minor (not gating)", description: JSON.stringify(minor) });
    expect(gating, JSON.stringify(gating, null, 1)).toEqual({});
  });
});
