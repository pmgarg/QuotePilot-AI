"use client";

import { Suspense, useState, type FormEvent } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { describeError, login } from "@/lib/api";
import { Alert, Button, Field, inputClass } from "@/components/ui";

// Only same-origin absolute paths are accepted (blocks //evil, /\evil, javascript:, absolute URLs).
function safeNext(raw: string | null): string {
  // Accept only a plain same-origin path: no scheme, no authority, no backslashes, no dot segments, no encoding tricks.
  if (!raw || raw.length > 512 || !/^\/[A-Za-z0-9_\-/?=&]*$/.test(raw)) return "/dashboard";
  if (raw.startsWith("//") || raw.includes("\\") || /(^|\/)\.\.?(\/|$)/.test(raw) || raw.includes("//")) return "/dashboard";
  try {
    const u = new URL(raw, window.location.origin);
    if (u.origin !== window.location.origin || !u.pathname.startsWith("/") || u.pathname.startsWith("//")) return "/dashboard";
    return u.pathname + u.search;
  } catch {
    return "/dashboard";
  }
}

function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(email, password);
      router.replace(safeNext(params.get("next")));
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="flex flex-col gap-4" aria-busy={busy}>
      <Field label="Email" htmlFor="email">
        <input id="email" name="email" type="email" autoComplete="username" required className={inputClass} value={email} onChange={(e) => setEmail(e.target.value)} data-testid="login-email" />
      </Field>
      <Field label="Password" htmlFor="password">
        <input id="password" name="password" type="password" autoComplete="current-password" required className={inputClass} value={password} onChange={(e) => setPassword(e.target.value)} data-testid="login-password" />
      </Field>
      {error && (
        <Alert kind="error" testId="login-error">
          {error}
        </Alert>
      )}
      <Button type="submit" disabled={busy} testId="login-submit">
        {busy ? "Signing in…" : "Sign in"}
      </Button>
    </form>
  );
}

export default function LoginPage() {
  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center px-4 py-12">
      <div className="rounded-xl border border-slate-200 bg-white p-8 shadow-sm">
        <h1 className="text-2xl font-semibold text-slate-900">QuotePilot AI</h1>
        <p className="mb-6 mt-1 text-sm text-slate-600">Sign in to your organisation workspace.</p>
        <Suspense fallback={null}>
          <LoginForm />
        </Suspense>
      </div>
      <p className="mt-4 text-center text-xs text-slate-500">Demo credentials are printed by the seed script and are local-only.</p>
    </main>
  );
}
