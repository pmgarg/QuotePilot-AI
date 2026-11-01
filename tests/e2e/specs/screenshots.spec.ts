import { test } from "@playwright/test";
import path from "node:path";
import { CLEAN_RFQ, USERS, login } from "../helpers";

// Documentation screenshots. Run with SCREENSHOTS=1 (skipped by default so CI stays fast).
const OUT = path.resolve(__dirname, "../../../docs/screenshots");
test.skip(!process.env.SCREENSHOTS, "set SCREENSHOTS=1 to capture");

test("capture Stage 1 walkthrough", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 860 });
  await page.goto("/login");
  await page.screenshot({ path: path.join(OUT, "01-login.png") });
  await login(page, USERS.sales);
  await page.screenshot({ path: path.join(OUT, "02-dashboard.png"), fullPage: true });
  await page.goto("/catalog");
  await page.waitForSelector('[data-testid="version-table"]');
  await page.screenshot({ path: path.join(OUT, "03-catalog.png"), fullPage: true });
  await page.goto("/rfqs/new");
  await page.getByTestId("mode-text").check();
  await page.getByTestId("rfq-text").fill(CLEAN_RFQ + "- 20 pcs ball bearing 6204\n- 4 box SKU-1004 Flat washer M8\n");
  await page.getByTestId("rfq-customer").selectOption({ index: 1 });
  await page.screenshot({ path: path.join(OUT, "04-new-rfq.png"), fullPage: true });
  await page.getByTestId("rfq-submit").click();
  await page.waitForSelector('[data-testid="rfq-status"]');
  await page.screenshot({ path: path.join(OUT, "05-rfq-review.png"), fullPage: true });
  // resolve ambiguous + unit mismatch, then generate
  await page.getByTestId("select-4").selectOption({ index: 1 });
  await page.getByTestId("confirm-unit-5").click();
  await page.waitForTimeout(300);
  await page.getByTestId("generate-quote").click();
  await page.waitForSelector('[data-testid="grand-total"]');
  await page.screenshot({ path: path.join(OUT, "06-quote-draft.png"), fullPage: true });
  await page.getByTestId("submit-quote").click();
  await page.waitForSelector('[data-testid="version-status"]:has-text("pending")');
  const url = page.url();
  await page.getByTestId("logout").click();
  await login(page, USERS.approver);
  await page.goto(url);
  await page.waitForSelector('[data-testid="approve-quote"]');
  await page.screenshot({ path: path.join(OUT, "07-approval.png"), fullPage: true });
  await page.getByTestId("approve-quote").click();
  await page.waitForSelector('[data-testid="quote-status"]:has-text("approved")');
  await page.screenshot({ path: path.join(OUT, "08-approved-history.png"), fullPage: true });
});
