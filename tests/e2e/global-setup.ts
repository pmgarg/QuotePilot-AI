import { execSync } from "node:child_process";
import path from "node:path";

// Migrates and seeds the synthetic demo organisations (idempotent). Playwright starts the `webServer` entries
// BEFORE this function runs, so nothing here may change what a running server serves: the web build therefore
// happens in the web server command itself (see playwright.config.ts), not here.
export default async function globalSetup() {
  const ROOT = path.resolve(__dirname, "../..");
  const API_DIR = path.join(ROOT, "apps/api");
  const env = {
    ...process.env,
    APP_ENV: "test",
    SECRET_KEY: process.env.SECRET_KEY ?? "e2e-only-secret-key-e2e-only-secret-key-e2e",
    DATABASE_URL: process.env.DATABASE_URL ?? "postgresql+psycopg://quotepilot_app:quotepilot_app@localhost:5432/quotepilot_test",
    DATABASE_ADMIN_URL: process.env.DATABASE_ADMIN_URL ?? "postgresql+psycopg://quotepilot_owner:quotepilot_owner@localhost:5432/quotepilot_test",
    STORAGE_DIR: path.join(ROOT, "var/e2e-storage"),
    DEMO_PASSWORD: process.env.DEMO_PASSWORD ?? "E2E-Demo-Password-2026!",
  };
  execSync("uv run alembic upgrade head", { cwd: API_DIR, env, stdio: "inherit" });
  execSync("uv run python -m app.fixtures.seed", { cwd: API_DIR, env, stdio: "pipe" });
}
