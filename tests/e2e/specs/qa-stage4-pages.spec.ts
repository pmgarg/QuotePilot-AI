import { expect, test, type Browser, type Page } from "@playwright/test";
import { CLEAN_RFQ, USERS, login } from "../helpers";

// Independent QA of the four Stage 4 web pages (browser evidence). Role states, negative paths, and restore-state
// discipline: every test that changes shared state (plan, kill switch, workflows) restores it in `finally`.

async function csrf(page: Page) {
  return (await page.context().cookies()).find((c) => c.name === "qp_csrf")!.value;
}
async function post(page: Page, path: string, data: unknown = {}) {
  return page.request.post(`/api/v1${path}`, { data, headers: { "X-CSRF-Token": await csrf(page) } });
}
async function del(page: Page, path: string) {
  return page.request.delete(`/api/v1${path}`, { headers: { "X-CSRF-Token": await csrf(page) } });
}
async function as(browser: Browser, email: string) {
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  await login(page, email);
  return { ctx, page };
}
async function approvedQuote(browser: Browser) {
  const { ctx, page } = await as(browser, USERS.sales);
  const customers = await (await page.request.get("/api/v1/customers")).json();
  const rfqRes = await post(page, "/rfqs", { text: CLEAN_RFQ, customer_id: customers[0].id });
  expect(rfqRes.status(), await rfqRes.text()).toBe(201);
  const rfq = await rfqRes.json();
  const qRes = await post(page, `/rfqs/${rfq.id}/generate-quote`);
  expect(qRes.status(), await qRes.text()).toBe(201);
  const q = await qRes.json();
  const subRes = await post(page, `/quotes/${q.id}/submit`, { expected_lock_version: q.lock_version });
  expect(subRes.status(), await subRes.text()).toBe(200);
  const sub = await subRes.json();
  const ap = await as(browser, USERS.approver);
  const apRes = await post(ap.page, `/quotes/${q.id}/approve`, { expected_lock_version: sub.lock_version });
  expect(apRes.status(), await apRes.text()).toBe(200);
  const approved = await apRes.json();
  await ap.ctx.close();
  await ctx.close();
  return approved as { id: string; number: string };
}

test.describe("QA Stage 4 pages — role states and negative paths", () => {
  test("unauthenticated visitors are sent to login for every Stage 4 page", async ({ page }) => {
    for (const p of ["/command-center", "/assistant", "/workflows", "/platform"]) {
      await page.goto(p);
      await expect(page).toHaveURL(/\/login/);
    }
  });

  test("S4-F01: command center queues follow the role; empty approval state; admin-only queues hidden", async ({ browser }) => {
    const ro = await as(browser, USERS.readonly);
    await ro.page.goto("/command-center");
    await expect(ro.page.getByTestId("cc-queues")).toBeVisible();
    await expect(ro.page.getByTestId("cc-quotes_draft")).toBeVisible();
    await expect(ro.page.getByTestId("cc-sync_conflicts_open")).toHaveCount(0);
    await expect(ro.page.getByTestId("cc-deletion_requests_open")).toHaveCount(0);
    await expect(ro.page.getByText("Nothing to approve")).toBeVisible();
    await expect(ro.page.getByTestId("cc-kill-switch")).toHaveCount(0);
    await ro.ctx.close();
    const ad = await as(browser, USERS.admin);
    await ad.page.goto("/command-center");
    await expect(ad.page.getByTestId("cc-sync_conflicts_open")).toBeVisible();
    await expect(ad.page.getByTestId("cc-deletion_requests_open")).toBeVisible();
    await ad.ctx.close();
  });

  test("S4-F04: kill-switch banner shows on the command center and the assistant refuses; release restores", async ({ browser }) => {
    const ad = await as(browser, USERS.admin);
    const sales = await as(browser, USERS.sales);
    try {
      expect((await post(ad.page, "/platform/kill-switch", { reason: "qa banner drill" })).status()).toBe(200);
      await sales.page.goto("/command-center");
      await expect(sales.page.getByTestId("cc-kill-switch")).toContainText("qa banner drill");
      await sales.page.goto("/assistant");
      await sales.page.getByTestId("assistant-input").fill("hello");
      await sales.page.getByTestId("assistant-send").click();
      await expect(sales.page.getByTestId("assistant-error")).toContainText(/kill switch/i);
    } finally {
      await del(ad.page, "/platform/kill-switch");
    }
    await sales.page.goto("/command-center");
    await expect(sales.page.getByTestId("cc-kill-switch")).toHaveCount(0);
    await ad.ctx.close();
    await sales.ctx.close();
  });

  test("S4-F02: assistant tool list per role, rejected side-effect request for a read-only user, help for unknown asks", async ({ browser }) => {
    const ro = await as(browser, USERS.readonly);
    await ro.page.goto("/assistant");
    const tools = ro.page.getByTestId("assistant-tools");
    await expect(tools).toContainText("get_quote");
    for (const t of ["propose_order", "draft_follow_up", "export_customers", "send_follow_up", "request_customer_deletion"]) {
      await expect(tools).not.toContainText(t);
    }
    await expect(ro.page.getByTestId("assistant-input")).toHaveAttribute("maxlength", "4000");
    await expect(ro.page.getByTestId("assistant-send")).toBeDisabled(); // empty message cannot be sent
    await ro.page.getByTestId("assistant-input").fill("convert Q-2026-00001 to an order");
    await ro.page.getByTestId("assistant-send").click();
    await expect(ro.page.getByTestId("assistant-action").first()).toContainText(/rejected/i);
    await expect(ro.page.getByTestId("assistant-action").first()).toContainText(/not|forbidden/i);
    await ro.page.getByTestId("assistant-input").fill("tell me a joke");
    await ro.page.getByTestId("assistant-send").click();
    await expect(ro.page.getByTestId("assistant-messages")).toContainText("I can:");
    await ro.ctx.close();
  });

  test("S4-F02: a quote answer carries a citation link to the quote; another tenant's number returns nothing", async ({ browser }) => {
    const q = await approvedQuote(browser);
    const sales = await as(browser, USERS.sales);
    await sales.page.goto("/assistant");
    await sales.page.getByTestId("assistant-input").fill(`show quote ${q.number}`);
    await sales.page.getByTestId("assistant-send").click();
    await expect(sales.page.getByTestId("assistant-action").first()).toContainText("executed");
    await expect(sales.page.locator(`a[href="/quotes/${q.id}"]`).first()).toBeVisible();
    await sales.ctx.close();
    const other = await as(browser, USERS.otherOwner);
    await other.page.goto("/assistant");
    await other.page.getByTestId("assistant-input").fill(`show quote ${q.number}`);
    await other.page.getByTestId("assistant-send").click();
    await expect(other.page.getByTestId("assistant-action").first()).toContainText(/rejected/i);
    await expect(other.page.getByTestId("assistant-messages")).not.toContainText(/grand_total/);
    await expect(other.page.locator(`a[href="/quotes/${q.id}"]`)).toHaveCount(0);
    await other.ctx.close();
  });

  test("S4-F05: workflows page is forbidden for sales, approver and readonly", async ({ browser }) => {
    for (const u of [USERS.sales, USERS.approver, USERS.readonly]) {
      const s = await as(browser, u);
      await s.page.goto("/workflows");
      await expect(s.page.getByTestId("forbidden")).toBeVisible();
      await expect(s.page.getByTestId("wf-save")).toHaveCount(0);
      await s.ctx.close();
    }
  });

  test("S4-F05: invalid JSON and invalid definitions are explained; an invalid version cannot be activated", async ({ browser }) => {
    const ad = await as(browser, USERS.admin);
    const key = `qa-bad-${Date.now()}`;
    await ad.page.goto("/workflows");
    await ad.page.locator("#wf-key").fill(key);
    await ad.page.getByTestId("wf-definition").fill("{ not json");
    await ad.page.getByTestId("wf-validate").click();
    await expect(ad.page.getByText("Not valid JSON")).toBeVisible();
    await ad.page.getByTestId("wf-definition").fill(JSON.stringify({ name: "bad", steps: [{ id: "a", tool: "run_sql", args: {} }] }));
    await ad.page.getByTestId("wf-validate").click();
    await expect(ad.page.getByTestId("wf-validation")).toContainText("Invalid");
    await expect(ad.page.getByTestId("wf-validation")).toContainText(/unknown tool/i);
    await ad.page.getByTestId("wf-save").click();
    await expect(ad.page.getByTestId("wf-list")).toContainText(key);
    await expect(ad.page.getByTestId(`wf-activate-${key}`)).toBeDisabled();
    await expect(ad.page.getByTestId(`wf-live-${key}`)).toHaveCount(0); // not enabled: no live-run button
    await ad.ctx.close();
  });

  test("S4-F05/F03: live run pauses in the UI, step approval continues it, order only exists afterwards; workflow restored", async ({ browser }) => {
    const q = await approvedQuote(browser);
    const ad = await as(browser, USERS.admin);
    const key = `qa-live-${Date.now()}`;
    const def = { name: "qa manual", trigger: "manual", steps: [
      { id: "read", tool: "get_quote", args: { quote_number: "{{quote_number}}" } },
      { id: "order", tool: "propose_order", args: { quote_number: "{{quote_number}}" } } ] };
    try {
      await ad.page.goto("/workflows");
      await ad.page.locator("#wf-key").fill(key);
      await ad.page.getByTestId("wf-definition").fill(JSON.stringify(def));
      await ad.page.getByTestId("wf-validate").click();
      await expect(ad.page.getByTestId("wf-validation")).toContainText(/approval/i); // warning shown
      await ad.page.getByTestId("wf-save").click();
      await ad.page.getByTestId(`wf-activate-${key}`).click();
      await expect(ad.page.getByTestId("wf-notice")).toContainText(/activated/);
      await ad.page.locator(`[id^="ctx-"]`).first().fill(JSON.stringify({ quote_number: q.number }));
      await ad.page.getByTestId(`wf-live-${key}`).click();
      await expect(ad.page.getByTestId("wf-runs")).toContainText("awaiting");
      const before = await (await ad.page.request.get("/api/v1/orders")).json();
      expect(before.filter((o: { quote_id: string }) => o.quote_id === q.id)).toHaveLength(0);
      await ad.page.getByTestId("wf-approve-step").first().click();
      await expect(ad.page.getByTestId("wf-notice")).toContainText(/continued/);
      const after = await (await ad.page.request.get("/api/v1/orders")).json();
      expect(after.filter((o: { quote_id: string }) => o.quote_id === q.id)).toHaveLength(1);
      await expect(ad.page.getByTestId("wf-approve-step")).toHaveCount(0);
    } finally {
      const list = await (await ad.page.request.get("/api/v1/workflows")).json();
      for (const w of list.filter((x: { key: string }) => x.key === key)) await post(ad.page, `/workflows/${w.id}/disable`);
      await ad.ctx.close();
    }
  });

  test("S4-F06: platform page shows controls per role (owner plan buttons; admin kill switch; sales read-only)", async ({ browser }) => {
    const ow = await as(browser, USERS.owner);
    await ow.page.goto("/platform");
    await expect(ow.page.getByTestId("plan-limits")).toBeVisible();
    await expect(ow.page.getByTestId("plan-growth")).toBeVisible();
    await expect(ow.page.getByTestId("usage")).toBeVisible();
    await ow.ctx.close();
    const ad = await as(browser, USERS.admin);
    await ad.page.goto("/platform");
    await expect(ad.page.getByTestId("plan-growth")).toHaveCount(0); // plan change is owner-only
    await expect(ad.page.locator("#ks-reason")).toBeVisible();
    await expect(ad.page.getByTestId("support-export")).toBeVisible();
    await expect(ad.page.getByTestId("ks-engage")).toBeDisabled(); // reason required
    await ad.ctx.close();
    const sa = await as(browser, USERS.sales);
    await sa.page.goto("/platform");
    await expect(sa.page.getByTestId("plan-limits")).toBeVisible();
    await expect(sa.page.getByTestId("plan-growth")).toHaveCount(0);
    await expect(sa.page.locator("#ks-reason")).toHaveCount(0);
    await expect(sa.page.getByTestId("usage")).toHaveCount(0);
    await expect(sa.page.getByTestId("support-export")).toHaveCount(0);
    await sa.ctx.close();
  });

  test("S4-F06: owner plan change in test mode changes the assistant tool catalogue; plan restored", async ({ browser }) => {
    const ow = await as(browser, USERS.owner);
    const sa = await as(browser, USERS.sales);
    try {
      await ow.page.goto("/platform");
      await ow.page.getByTestId("plan-growth").click();
      await expect(ow.page.getByTestId("platform-notice")).toContainText(/test mode/i);
      await sa.page.goto("/assistant");
      await expect(sa.page.getByTestId("assistant-tools")).toContainText("send_follow_up");
    } finally {
      await ow.page.goto("/platform");
      const starter = ow.page.getByTestId("plan-starter");
      if (await starter.isEnabled()) await starter.click();
      await expect(ow.page.getByTestId("plan-starter")).toBeDisabled();
    }
    await sa.page.goto("/assistant");
    await expect(sa.page.getByTestId("assistant-tools")).not.toContainText("send_follow_up");
    await ow.ctx.close();
    await sa.ctx.close();
  });

  test("S4-F06: support export from the UI contains no customer data", async ({ browser }) => {
    const ad = await as(browser, USERS.admin);
    await ad.page.goto("/platform");
    const [dl] = await Promise.all([ad.page.waitForEvent("download"), ad.page.getByTestId("support-export").click()]);
    const fs = await import("node:fs");
    const body = fs.readFileSync((await dl.path())!, "utf8");
    expect(JSON.parse(body).contains_customer_data).toBe(false);
    expect(body).not.toMatch(/@northwind|Northwind Customer|buyer/i);
    await ad.ctx.close();
  });
});
