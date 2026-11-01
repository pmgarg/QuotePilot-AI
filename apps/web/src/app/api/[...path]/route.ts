import { NextRequest, NextResponse } from "next/server";

/**
 * Runtime reverse proxy for the API so cookies stay first-party and the target can differ per
 * environment (docker: http://api:8000, local: http://localhost:8000) without rebuilding the bundle.
 * Nothing is cached; bodies stream through; no secrets are added.
 */
export const dynamic = "force-dynamic";

const HOP_BY_HOP = new Set(["connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length"]);
// Client-controlled forwarding headers are never trusted. Set WEB_TRUST_FORWARDED_HEADERS=true only when this app
// runs behind your own TLS reverse proxy that overwrites X-Forwarded-For (see docs/OPERATIONS_RUNBOOK.md).
const FORWARDING = new Set(["x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "x-real-ip", "forwarded"]);
const trustForwarded = process.env.WEB_TRUST_FORWARDED_HEADERS === "true";

function apiOrigin(): string {
  return (process.env.API_INTERNAL_URL ?? "http://localhost:8000").replace(/\/$/, "");
}

async function proxy(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }): Promise<Response> {
  const { path } = await ctx.params;
  const target = new URL(`${apiOrigin()}/api/${path.map(encodeURIComponent).join("/")}`);
  target.search = req.nextUrl.search;
  const headers = new Headers();
  req.headers.forEach((value, key) => {
    const k = key.toLowerCase();
    if (HOP_BY_HOP.has(k)) return;
    if (FORWARDING.has(k) && !trustForwarded) return;
    headers.set(key, value);
  });
  // no synthesised forwarding headers either: the API must not infer client facts from this hop (NEW-8)
  const hasBody = !["GET", "HEAD"].includes(req.method);
  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method: req.method,
      headers,
      body: hasBody ? req.body : undefined,
      // @ts-expect-error: Node fetch requires duplex for streamed request bodies
      duplex: hasBody ? "half" : undefined,
      redirect: "manual",
      cache: "no-store",
    });
  } catch {
    return NextResponse.json({ error: "upstream_unavailable", detail: "The API is not reachable." }, { status: 502 });
  }
  const out = new Headers();
  upstream.headers.forEach((value, key) => {
    if (!HOP_BY_HOP.has(key.toLowerCase()) && key.toLowerCase() !== "set-cookie") out.set(key, value);
  });
  for (const cookie of upstream.headers.getSetCookie()) out.append("set-cookie", cookie);
  return new Response(upstream.body, { status: upstream.status, statusText: upstream.statusText, headers: out });
}

export { proxy as GET, proxy as POST, proxy as PATCH, proxy as PUT, proxy as DELETE, proxy as HEAD, proxy as OPTIONS };
