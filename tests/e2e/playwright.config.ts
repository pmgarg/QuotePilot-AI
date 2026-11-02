import { defineConfig, devices } from "@playwright/test";
import path from "node:path";

const ROOT = path.resolve(__dirname, "../..");
const API_DIR = path.join(ROOT, "apps/api");
const WEB_DIR = path.join(ROOT, "apps/web");
export const DEMO_PASSWORD = process.env.DEMO_PASSWORD ?? "E2E-Demo-Password-2026!";
const DATABASE_URL = process.env.DATABASE_URL ?? "postgresql+psycopg://quotepilot_app:quotepilot_app@localhost:5432/quotepilot_test";
const DATABASE_ADMIN_URL = process.env.DATABASE_ADMIN_URL ?? "postgresql+psycopg://quotepilot_owner:quotepilot_owner@localhost:5432/quotepilot_test";
const API_PORT = Number(process.env.E2E_API_PORT ?? 8011);
const WEB_PORT = Number(process.env.E2E_WEB_PORT ?? 3011);

const apiEnv = {
  ...process.env,
  APP_ENV: "test",
  SECRET_KEY: process.env.SECRET_KEY ?? "e2e-only-secret-key-e2e-only-secret-key-e2e",
  DATABASE_URL,
  DATABASE_ADMIN_URL,
  STORAGE_DIR: path.join(ROOT, "var/e2e-storage"),
  CORS_ORIGINS: `http://localhost:${WEB_PORT}`,
  DEMO_PASSWORD,
  // CI keeps the API's JSON request log (method, path, status, request id) in the job output for post-mortems (I-3);
  // Python buffers stdout when piped, so the buffer must be disabled or nothing appears before exit.
  LOG_LEVEL: process.env.CI ? "INFO" : "WARNING",
  PYTHONUNBUFFERED: "1",
};

export default defineConfig({
  testDir: "./specs",
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  timeout: 90_000,
  reporter: [["list"], ["html", { open: "never" }]],
  globalSetup: "./global-setup.ts",
  use: {
    baseURL: `http://localhost:${WEB_PORT}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    ...devices["Desktop Chrome"],
  },
  webServer: [
    {
      command: `uv run uvicorn app.main:app --host 127.0.0.1 --port ${API_PORT}`,
      cwd: API_DIR,
      url: `http://127.0.0.1:${API_PORT}/api/v1/health`,
      reuseExistingServer: false,
      timeout: 120_000,
      env: apiEnv,
      stdout: process.env.CI ? "pipe" : "ignore", // access log in the CI job output for post-mortems (I-3)
      stderr: "pipe",
    },
    {
      command: `npx next start --port ${WEB_PORT}`,
      cwd: WEB_DIR,
      url: `http://localhost:${WEB_PORT}/login`,
      reuseExistingServer: false,
      timeout: 180_000,
      env: { ...process.env, API_INTERNAL_URL: `http://127.0.0.1:${API_PORT}`, PORT: String(WEB_PORT) },
      stdout: process.env.CI ? "pipe" : "ignore",
      stderr: "pipe",
    },
  ],
});
