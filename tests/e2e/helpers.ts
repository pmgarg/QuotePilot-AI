import { expect, type Page } from "@playwright/test";
import { DEMO_PASSWORD } from "./playwright.config";

export const USERS = {
  owner: "owner@northwind.example",
  admin: "admin@northwind.example",
  sales: "sales@northwind.example",
  approver: "approver@northwind.example",
  readonly: "readonly@northwind.example",
  otherOwner: "owner@contoso.example",
};

export async function login(page: Page, email: string, password = DEMO_PASSWORD) {
  await page.goto("/login");
  await page.getByTestId("login-email").fill(email);
  await page.getByTestId("login-password").fill(password);
  await page.getByTestId("login-submit").click();
  await expect(page.getByTestId("user-email")).toHaveText(email);
}

export async function logout(page: Page) {
  await page.getByTestId("logout").click();
  await expect(page).toHaveURL(/\/login/);
}

export const CLEAN_RFQ = `Request for Quotation RFQ-E2E-${Date.now()}
1. 500 pcs SKU-1001 Hex bolt M8x40 zinc plated
2. 500 pcs SKU-1003 Hex nut M8 zinc plated
3. 2 ea SKU-3002 Lithium grease EP2 pail 18kg
`;
