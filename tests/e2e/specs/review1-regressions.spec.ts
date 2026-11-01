import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { CLEAN_RFQ, USERS, login } from "../helpers";

async function csrf(page: import("@playwright/test").Page) {
  return (await page.context().cookies()).find((c) => c.name === "qp_csrf")!.value;
}

test.describe("Release review 1 regressions", () => {
  test("SEC-09: post-login redirect never leaves the origin", async ({ page }) => {
    for (const next of ["/\\evil.com", "//evil.com", "https://evil.com/x", "javascript:alert(1)", "/%5Cevil.com"]) {
      await page.goto(`/login?next=${encodeURIComponent(next)}`);
      await page.getByTestId("login-email").fill(USERS.sales);
      await page.getByTestId("login-password").fill(process.env.DEMO_PASSWORD ?? "E2E-Demo-Password-2026!");
      await page.getByTestId("login-submit").click();
      await expect(page).toHaveURL(/\/dashboard$/);
      expect(new URL(page.url()).hostname).toBe("localhost");
      await page.getByTestId("logout").click();
    }
  });

  test("H-1: approved v1 document is immutable after a new version and validity edit", async ({ page, browser }) => {
    await login(page, USERS.sales);
    const token = await csrf(page);
    const rfq = await (await page.request.post("/api/v1/rfqs", { data: { text: CLEAN_RFQ }, headers: { "X-CSRF-Token": token } })).json();
    const q = await (await page.request.post(`/api/v1/rfqs/${rfq.id}/generate-quote`, { headers: { "X-CSRF-Token": token } })).json();
    const sub = await (await page.request.post(`/api/v1/quotes/${q.id}/submit`, { data: { expected_lock_version: q.lock_version }, headers: { "X-CSRF-Token": token } })).json();
    const appr = await browser.newContext();
    const ap = await appr.newPage();
    await login(ap, USERS.approver);
    const approved = await (await ap.request.post(`/api/v1/quotes/${q.id}/approve`, { data: { expected_lock_version: sub.lock_version }, headers: { "X-CSRF-Token": await csrf(ap) } })).json();
    expect(approved.status).toBe("approved");
    const pdf1 = await (await page.request.get(`/api/v1/quotes/${q.id}/versions/1/pdf`)).body();
    // sales creates v2 and tries to push validity into the far future
    const v2 = await (await page.request.post(`/api/v1/quotes/${q.id}/versions`, { data: { expected_lock_version: approved.lock_version }, headers: { "X-CSRF-Token": token } })).json();
    const patched = await page.request.patch(`/api/v1/quotes/${q.id}`, { data: { expected_lock_version: v2.lock_version, valid_until: "2099-12-31" }, headers: { "X-CSRF-Token": token } });
    expect(patched.status()).toBe(200);
    const pdf1Again = await (await page.request.get(`/api/v1/quotes/${q.id}/versions/1/pdf`)).body();
    expect(Buffer.compare(pdf1, pdf1Again)).toBe(0);
    expect(pdf1Again.toString("latin1")).not.toContain("2099-12-31");
    await appr.close();
  });

  test("M-3: sales never sees margin figures or the floor in blocker text", async ({ page }) => {
    await login(page, USERS.sales);
    const token = await csrf(page);
    const rfq = await (await page.request.post("/api/v1/rfqs", { data: { text: "10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n" }, headers: { "X-CSRF-Token": token } })).json();
    const q = await (await page.request.post(`/api/v1/rfqs/${rfq.id}/generate-quote`, { headers: { "X-CSRF-Token": token } })).json();
    const line = q.current_version.lines[0];
    const r = await page.request.patch(`/api/v1/quotes/${q.id}`, { data: { expected_lock_version: q.lock_version, lines: [{ id: line.id, discount_pct: "40" }] }, headers: { "X-CSRF-Token": token } });
    const body = await r.json();
    const texts = body.blockers.map((b: { message: string }) => b.message).join(" ");
    expect(texts).not.toMatch(/\d+(\.\d+)?%/);
    await page.goto(`/quotes/${q.id}`);
    await expect(page.getByTestId("quote-blockers")).toBeVisible();
    await expect(page.getByTestId("quote-blockers")).not.toContainText("%");
  });

  test("X-08: core screens have no serious or critical accessibility violations (axe)", async ({ page }) => {
    await login(page, USERS.sales);
    for (const path of ["/dashboard", "/rfqs/new", "/quotes", "/customers", "/catalog", "/settings"]) {
      await page.goto(path);
      await page.waitForLoadState("networkidle");
      const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
      const serious = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
      expect(serious, `${path}: ${serious.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`).join("; ")}`).toEqual([]);
    }
  });
});
