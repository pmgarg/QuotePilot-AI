import { expect, test } from "@playwright/test";
import { login, USERS } from "../helpers";

// Fix round 11 (QA J7-4): a line without catalog suggestions can be linked to a catalog product by its exact SKU,
// and the message no longer claims that no catalog product exists.
test.describe("RFQ review — select a catalog product by exact SKU", () => {
  test("a line without suggestions is resolved by SKU, a wrong SKU is refused, and the choice can be cleared", async ({ page }) => {
    await login(page, USERS.sales);
    await page.goto("/rfqs/new");
    await page.getByTestId("mode-text").check();
    await page.getByTestId("rfq-text").fill("7 pcs Qqqq zzzz xxxx vvvv wwww\n");
    await page.getByTestId("rfq-customer").selectOption({ index: 1 });
    await page.getByTestId("rfq-submit").click();
    await expect(page.getByTestId("rfq-status")).toHaveText(/parsed/i);

    const row = page.getByTestId("rfq-item-1");
    await expect(row.getByTestId("match-1")).toHaveText(/unmatched/i);
    await expect(row.getByTestId("no-candidate-1")).toContainText("No catalog suggestion. Enter the exact SKU");

    // unknown SKU: nothing is selected and the reason is shown
    await row.getByTestId("sku-input-1").fill("NO-SUCH-SKU-999");
    await row.getByTestId("sku-use-1").click();
    await expect(page.getByText('No product with SKU "NO-SUCH-SKU-999" in the catalog used for this request.')).toBeVisible();
    await expect(row.getByTestId("no-candidate-1")).toBeVisible();

    // exact SKU (case-insensitive): the line becomes resolved
    await row.getByTestId("sku-input-1").fill("sku-1001");
    await row.getByTestId("sku-use-1").click();
    await expect(row.getByTestId("sku-selected-1")).toContainText("Catalog product selected by SKU.");
    await expect(row.getByTestId("match-1")).toHaveText(/resolved/i);

    // clearing returns the line to the unresolved state
    await row.getByTestId("sku-selected-1").getByRole("button", { name: "Clear" }).click();
    await expect(row.getByTestId("no-candidate-1")).toBeVisible();
  });
});
