"use client";

const BASE = "/api/v1";

export class ApiError extends Error {
  status: number;
  code: string;
  detail: unknown;
  extra: Record<string, unknown>;
  constructor(status: number, body: Record<string, unknown> | null, fallback: string) {
    const code = (body?.error as string) ?? "error";
    const detail = body?.detail;
    super(typeof detail === "string" ? detail : fallback);
    this.status = status;
    this.code = code;
    this.detail = detail;
    this.extra = body ?? {};
  }
  get blockers(): { code: string; message: string; line_no: number | null; overridable_by: string[] }[] {
    return (this.extra.blockers as never) ?? [];
  }
}

function readCookie(name: string): string | null {
  if (typeof document === "undefined") return null;
  const m = document.cookie.match(new RegExp("(?:^|; )" + name.replace(/[$()*+.?[\\\]^{|}]/g, "\\$&") + "=([^;]*)"));
  return m ? decodeURIComponent(m[1]) : null;
}

export function csrfToken(): string | null {
  return readCookie("qp_csrf");
}

export async function api<T>(path: string, init: RequestInit & { json?: unknown; form?: FormData } = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const method = (init.method ?? "GET").toUpperCase();
  let body = init.body;
  if (init.json !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(init.json);
  } else if (init.form) {
    body = init.form;
  }
  if (method !== "GET" && method !== "HEAD") {
    const token = csrfToken();
    if (token) headers.set("X-CSRF-Token", token);
  }
  const res = await fetch(`${BASE}${path}`, { ...init, method, headers, body, credentials: "same-origin", cache: "no-store" });
  if (res.status === 204) return undefined as T;
  const ct = res.headers.get("content-type") ?? "";
  const parsed = ct.includes("application/json") ? ((await res.json()) as Record<string, unknown>) : null;
  if (!res.ok) {
    if (res.status === 401 && typeof window !== "undefined" && !window.location.pathname.startsWith("/login")) {
      const target = new URL("/login", window.location.origin);
      target.searchParams.set("next", window.location.pathname);
      window.location.assign(target.toString());
    }
    throw new ApiError(res.status, parsed, `Request failed (${res.status})`);
  }
  return parsed as T;
}

export async function login(email: string, password: string) {
  const pre = await fetch(`${BASE}/auth/csrf`, { credentials: "same-origin", cache: "no-store" });
  const { csrf_token } = (await pre.json()) as { csrf_token: string };
  const res = await fetch(`${BASE}/auth/login`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf_token },
    body: JSON.stringify({ email, password }),
  });
  const body = (await res.json().catch(() => null)) as Record<string, unknown> | null;
  if (!res.ok) throw new ApiError(res.status, body, "Login failed");
  return body as unknown as import("./types").SessionInfo;
}

export function describeError(e: unknown): string {
  if (e instanceof ApiError) {
    if (Array.isArray(e.detail)) {
      return (e.detail as { loc: string[]; msg: string }[]).map((d) => `${d.loc.slice(-1)[0]}: ${d.msg}`).join("; ");
    }
    return e.message + (e.extra.request_id ? ` (ref ${String(e.extra.request_id).slice(0, 8)})` : "");
  }
  if (e instanceof Error) return e.message;
  return "Unexpected error";
}
