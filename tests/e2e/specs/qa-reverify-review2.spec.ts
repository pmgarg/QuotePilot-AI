/**
 * QA (independent) browser re-verification of release review 2 items on HEAD 58976f3 (round 4 = 80aaf7f).
 * Derived from docs/PRD.md (S1-F11, S1-F12, S1-F15, S1-A15, S1-A20), docs/USER_JOURNEYS.md ("Withdraw approved
 * quote": owner/admin/approver) and docs/evidence/release-review-2-stage1-2026-11-01.md (N2-1, N2-2, L2-1).
 * Browser evidence (Chromium against real uvicorn + Next.js). The real screens are driven; API calls are used only
 * to set up state and to read export bytes/status, as a customer-facing download would.
 */
import { expect, test, type Browser, type Page } from "@playwright/test";
import path from "node:path";
import { USERS, login } from "../helpers";

const DEMO_CATALOG = path.resolve(__dirname, "../../../apps/api/app/fixtures/data/demo_catalog.csv");
const TWO_LINE_RFQ = "1. 500 pcs SKU-1001 Hex bolt M8x40 zinc plated\n2. 500 pcs SKU-1003 Hex nut M8 zinc plated\n";

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

async function submitViaUi(page: Page) {
  await expect(page.getByTestId("submit-quote")).toBeEnabled();
  await page.getByTestId("submit-quote").click();
  await expect(page.getByTestId("version-status")).toHaveText(/pending approval/i);
}

async function approveViaUi(browser: Browser, quoteUrl: string) {
  const { ctx, page } = await asUser(browser, USERS.approver);
  await page.goto(quoteUrl);
  await page.getByTestId("decision-reason").fill("ok");
  await page.getByTestId("approve-quote").click();
  await expect(page.getByTestId("quote-status")).toHaveText(/approved/i);
  await ctx.close();
}

/** Versions & decisions card entry for one version (e.g. "v1 WITHDRAWN · approved … · withdrawn …"). */
function versionEntry(page: Page, n: number) {
  return page.locator("li", { has: page.locator("span", { hasText: new RegExp(`^v${n}\\s`) }) }).first();
}

async function exportStatuses(page: Page, quoteId: string, n: number): Promise<number[]> {
  const pdf = await page.request.get(`/api/v1/quotes/${quoteId}/versions/${n}/pdf`);
  const csv = await page.request.get(`/api/v1/quotes/${quoteId}/versions/${n}/csv`);
  return [pdf.status(), csv.status()];
}

test.describe("QA re-verification — review 2 items in the browser", () => {
  test("S1-F11/N2-1: saving a quantity edit in the editor keeps catalog prices and leaves the quote submittable by Sales", async ({ page }) => {
    // The editor re-sends every line field on "Save draft". A catalog-priced line must stay a catalog price
    // (no "manual price" badge, no confirmation blocker) when only the quantity changed.
    await login(page, USERS.sales);
    await pasteRfq(page, TWO_LINE_RFQ, `QA-RESAVE-${Date.now()}`);
    await generateQuote(page);
    await expect(page.getByTestId("quote-blockers")).toHaveCount(0);
    await expect(page.getByTestId("submit-quote")).toBeEnabled();
    await expect(page.getByTestId("line-total-1")).toHaveText("446.25"); // 500 × 0.85 + 5 %

    await page.locator('input[aria-label="Quantity 1"]').fill("600");
    await page.getByTestId("save-draft").click();
    await expect(page.getByTestId("quote-notice")).toContainText("saved");
    await expect(page.getByTestId("quote-error")).toHaveCount(0);
    await expect(page.getByTestId("line-total-1")).toHaveText("535.50"); // 600 × 0.85 + 5 %
    await expect(page.getByTestId("quote-line-1")).not.toContainText(/manual price/i);
    await expect(page.getByTestId("quote-line-2")).not.toContainText(/manual price/i);
    await expect(page.getByTestId("quote-blockers")).toHaveCount(0);
    await submitViaUi(page);
  });

  test("N2-2: withdrawing a quote with two approved versions in the UI blocks both documents; a new draft does not revive them", async ({ page, browser }) => {
    await login(page, USERS.sales);
    await pasteRfq(page, TWO_LINE_RFQ, `QA-N22A-${Date.now()}`);
    const quoteId = await generateQuote(page);
    const quoteUrl = page.url();
    await submitViaUi(page);
    await approveViaUi(browser, quoteUrl);
    await page.reload();
    await page.getByTestId("new-version").click();
    await expect(page.getByTestId("quote-notice")).toContainText(/new draft version/i);
    await expect(page.getByTestId("version-status")).toHaveText(/draft/i);
    await submitViaUi(page);
    await approveViaUi(browser, quoteUrl);
    await page.reload();
    await expect(page.getByTestId("quote-status")).toHaveText(/approved/i);
    for (const n of [1, 2]) expect(await exportStatuses(page, quoteId, n)).toEqual([200, 200]);

    // owner withdraws from the quote screen
    const ow = await asUser(browser, USERS.owner);
    await ow.page.goto(quoteUrl);
    await ow.page.locator("#withdraw-reason").fill("tender lost — withdraw both issued documents");
    await ow.page.getByRole("button", { name: "Withdraw" }).click();
    await expect(ow.page.getByTestId("quote-notice")).toContainText(/withdrawn/i);
    await expect(ow.page.getByTestId("quote-status")).toHaveText(/withdrawn/i);
    await expect(ow.page.getByRole("status").filter({ hasText: "tender lost" })).toBeVisible(); // warning banner with the reason
    await expect(versionEntry(ow.page, 1)).toContainText(/withdrawn/i);
    await expect(versionEntry(ow.page, 2)).toContainText(/withdrawn/i);
    await expect(versionEntry(ow.page, 1)).toContainText(/approved .*withdrawn/is); // decision trail per version
    await expect(ow.page.locator(`a[href$="/versions/1/pdf"]:visible`).filter({ hasText: /^PDF$/ })).toHaveCount(0);
    await expect(ow.page.locator(`a[href$="/versions/2/pdf"]:visible`).filter({ hasText: /^PDF$/ })).toHaveCount(0);

    // every role is refused the withdrawn documents
    for (const n of [1, 2]) {
      expect(await exportStatuses(page, quoteId, n)).toEqual([409, 409]);
      expect(await exportStatuses(ow.page, quoteId, n)).toEqual([409, 409]);
    }
    const ro = await asUser(browser, USERS.readonly);
    for (const n of [1, 2]) expect(await exportStatuses(ro.page, quoteId, n)).toEqual([409, 409]);

    // a replacement draft (v3) does not revive v1 or v2
    await ow.page.getByRole("button", { name: "Create new version" }).click();
    await expect(ow.page.getByTestId("quote-notice")).toContainText(/new draft version/i);
    await expect(ow.page.getByTestId("quote-status")).toHaveText(/draft/i);
    await expect(ow.page.getByRole("heading", { level: 1 })).toContainText("v3");
    await expect(versionEntry(ow.page, 1)).toContainText(/withdrawn/i);
    await expect(versionEntry(ow.page, 2)).toContainText(/withdrawn/i);
    await expect(versionEntry(ow.page, 3)).toContainText(/draft/i);
    await expect(ow.page.getByTestId("download-pdf")).toHaveText(/preview pdf \(draft\)/i);
    for (const n of [1, 2]) {
      expect(await exportStatuses(page, quoteId, n)).toEqual([409, 409]);
      expect(await exportStatuses(ro.page, quoteId, n)).toEqual([409, 409]);
    }
    expect((await page.request.get(`/api/v1/quotes/${quoteId}/versions/3/pdf`)).status()).toBe(200);
    await ro.ctx.close();
    await ow.ctx.close();
  });

  test("N2-2: an approver can withdraw the approved v1 from the quote screen while a newer draft is open", async ({ page, browser }) => {
    // API_CONTRACT: withdraw is "allowed while a newer draft is open (the draft stays editable)"; USER_JOURNEYS
    // gives the journey to owner/admin/approver. The screen must offer it in that state.
    await login(page, USERS.sales);
    await pasteRfq(page, TWO_LINE_RFQ, `QA-N22B-${Date.now()}`);
    const quoteId = await generateQuote(page);
    const quoteUrl = page.url();
    await submitViaUi(page);
    await approveViaUi(browser, quoteUrl);
    await page.reload();
    await page.getByTestId("new-version").click();
    await expect(page.getByTestId("version-status")).toHaveText(/draft/i);
    expect(await exportStatuses(page, quoteId, 1)).toEqual([200, 200]);

    const ap = await asUser(browser, USERS.approver);
    await ap.page.goto(quoteUrl);
    await expect(ap.page.getByTestId("quote-status")).toHaveText(/draft/i);
    await expect(versionEntry(ap.page, 1)).toContainText(/approved/i);
    const withdraw = ap.page.getByRole("button", { name: "Withdraw" });
    await expect(withdraw, "the approved v1 document is still issued; the approver needs the Withdraw control").toBeVisible();
    await ap.page.locator("#withdraw-reason").fill("v1 priced wrong");
    await withdraw.click();
    await expect(ap.page.getByTestId("quote-notice")).toContainText(/withdrawn/i);
    await expect(versionEntry(ap.page, 1)).toContainText(/withdrawn/i);
    await expect(ap.page.getByTestId("version-status")).toHaveText(/draft/i); // the draft stays editable
    expect(await exportStatuses(page, quoteId, 1)).toEqual([409, 409]);
    await ap.ctx.close();
  });

  test("L2-1: further post-login redirect variants are neutralised and a legitimate same-origin next is honoured", async ({ page }) => {
    const hostile = [
      "/\\\\evil.com",
      "/%2F%2Fevil.com",
      "%2F%2Fevil.com",
      "https:evil.com",
      "http:/evil.com",
      "//evil.com/%2F",
      "/%5C%5Cevil.com",
      "/evil.com%00",
      "/dashboard/..%2F..%2F%2Fevil.com",
      "/%09/evil.com",
      "/∕∕evil.com",
      "/%E2%88%95%E2%88%95evil.com",
      "//localhost@evil.com",
      "/@evil.com",
      "/\\/evil.com",
      " //evil.com",
    ];
    const password = process.env.DEMO_PASSWORD ?? "E2E-Demo-Password-2026!";
    for (const next of hostile) {
      await page.goto(`/login?next=${encodeURIComponent(next)}`);
      await page.getByTestId("login-email").fill(USERS.sales);
      await page.getByTestId("login-password").fill(password);
      await page.getByTestId("login-submit").click();
      await expect(page, `next=${JSON.stringify(next)}`).toHaveURL(/\/dashboard$/);
      expect(new URL(page.url()).hostname).toBe("localhost");
      await page.getByTestId("logout").click();
      await expect(page).toHaveURL(/\/login/);
    }
    // raw (un-encoded) scheme-relative value in the query string
    await page.goto("/login?next=//evil.com");
    await page.getByTestId("login-email").fill(USERS.sales);
    await page.getByTestId("login-password").fill(password);
    await page.getByTestId("login-submit").click();
    await expect(page).toHaveURL(/\/dashboard$/);
    await page.getByTestId("logout").click();
    await expect(page).toHaveURL(/\/login/);
    // positive controls: the sanitiser must still honour a plain same-origin path (with and without a query)
    for (const [next, expected] of [["/quotes", /\/quotes$/], ["/quotes?status=approved", /\/quotes\?status=approved$/]] as const) {
      await page.goto(`/login?next=${encodeURIComponent(next)}`);
      await page.getByTestId("login-email").fill(USERS.sales);
      await page.getByTestId("login-password").fill(password);
      await page.getByTestId("login-submit").click();
      await expect(page).toHaveURL(expected);
      expect(new URL(page.url()).hostname).toBe("localhost");
      await page.getByTestId("logout").click();
      await expect(page).toHaveURL(/\/login/);
    }
  });

  test("N2-1B: after an admin activates a new catalog version in the UI, a draft generated from the earlier version still saves at its pinned prices", async ({ page, browser }) => {
    await login(page, USERS.sales);
    await pasteRfq(page, "1. 10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n", `QA-PIN-${Date.now()}`);
    const quoteId = await generateQuote(page);
    const quoteUrl = page.url();
    await expect(page.getByTestId("line-total-1")).toHaveText("8.93"); // 10 × 0.85 + 5 %
    const before = await (await page.request.get(`/api/v1/quotes/${quoteId}`)).json();
    const pinned = before.current_version.pricing_snapshot.catalog_version_id as string;
    expect(pinned).toBeTruthy();

    // admin imports the (identical) demo price list as a new version and activates it through the Catalog screen
    const ad = await asUser(browser, USERS.admin);
    await ad.page.goto("/catalog");
    await ad.page.getByTestId("catalog-file").setInputFiles(DEMO_CATALOG);
    await ad.page.getByTestId("catalog-import").click();
    await expect(ad.page.getByTestId("import-report")).toContainText(/products/i);
    const catalogs = await (await ad.page.request.get("/api/v1/catalogs")).json();
    const versions = catalogs.flatMap((c: { versions: { version_no: number; id: string; status: string }[] }) => c.versions);
    const newest = versions.reduce((a: { version_no: number }, b: { version_no: number }) => (b.version_no > a.version_no ? b : a));
    expect(newest.id).not.toBe(pinned);
    await ad.page.getByTestId(`activate-${newest.version_no}`).click();
    await expect(ad.page.getByTestId(`version-status-${newest.version_no}`)).toHaveText(/active/i);
    const after = await (await ad.page.request.get("/api/v1/catalogs")).json();
    expect(after.some((c: { active_version_id: string }) => c.active_version_id === newest.id)).toBe(true);
    await ad.ctx.close();

    // the sales draft (pinned to the previous version) is still saveable and keeps its price
    await page.goto(quoteUrl);
    await page.locator('input[aria-label="Quantity 1"]').fill("12");
    await page.getByTestId("save-draft").click();
    await expect(page.getByTestId("quote-notice")).toContainText("saved");
    await expect(page.getByTestId("quote-error")).toHaveCount(0);
    await expect(page.getByTestId("line-total-1")).toHaveText("10.71"); // 12 × 0.85 + 5 %
    const saved = await (await page.request.get(`/api/v1/quotes/${quoteId}`)).json();
    expect(saved.current_version.pricing_snapshot.catalog_version_id).toBe(pinned);
    expect(saved.current_version.lines[0].unit_price).toBe("0.85");
    expect(saved.current_version.lines[0].product_id).toBe(before.current_version.lines[0].product_id);
  });
});
