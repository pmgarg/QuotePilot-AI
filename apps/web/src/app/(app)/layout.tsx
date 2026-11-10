"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import { SessionProvider, useSession } from "@/components/session";
import { Loading } from "@/components/ui";

const NAV = [
  { href: "/dashboard", label: "Dashboard" },
  { href: "/command-center", label: "Command center" },
  { href: "/assistant", label: "Assistant" },
  { href: "/rfqs", label: "RFQs" },
  { href: "/quotes", label: "Quotes" },
  { href: "/follow-ups", label: "Follow-ups" },
  { href: "/orders", label: "Orders" },
  { href: "/customers", label: "Customers" },
  { href: "/insights", label: "Insights" },
  { href: "/data", label: "Data & connectors" },
  { href: "/workflows", label: "Workflows" },
  { href: "/platform", label: "Plan & controls" },
  { href: "/catalog", label: "Catalog" },
  { href: "/settings", label: "Settings" },
];

function Shell({ children }: { children: ReactNode }) {
  const { session, loading, logout } = useSession();
  const pathname = usePathname();
  if (loading) {
    return (
      <main className="p-8">
        <Loading label="Loading your workspace…" />
      </main>
    );
  }
  if (!session) return null;
  return (
    <div className="flex min-h-screen flex-col md:flex-row">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-white focus:px-3 focus:py-2">
        Skip to content
      </a>
      <aside className="border-b border-slate-200 bg-white md:w-60 md:border-b-0 md:border-r">
        <div className="px-4 py-4">
          <p className="text-lg font-semibold text-brand">QuotePilot AI</p>
          <p className="truncate text-xs text-slate-500" data-testid="org-name">
            {session.organization.name}
          </p>
        </div>
        <nav aria-label="Main" className="flex gap-1 overflow-x-auto px-2 pb-2 md:flex-col md:pb-4">
          {NAV.map((n) => {
            const active = pathname === n.href || pathname.startsWith(n.href + "/");
            return (
              <Link key={n.href} href={n.href} aria-current={active ? "page" : undefined} className={`rounded-md px-3 py-2 text-sm ${active ? "bg-slate-100 font-semibold text-brand" : "text-slate-700 hover:bg-slate-50"}`}>
                {n.label}
              </Link>
            );
          })}
        </nav>
        <div className="border-t border-slate-200 px-4 py-3 text-xs text-slate-600">
          <p className="truncate" data-testid="user-email">
            {session.user.email}
          </p>
          <p className="mt-0.5 uppercase tracking-wide" data-testid="user-role">
            {session.role}
          </p>
          <button type="button" onClick={() => void logout()} className="mt-2 text-brand hover:underline" data-testid="logout">
            Sign out
          </button>
        </div>
      </aside>
      <main id="main" className="flex-1 px-4 py-6 md:px-8">
        {children}
      </main>
    </div>
  );
}

export default function AppLayout({ children }: { children: ReactNode }) {
  return (
    <SessionProvider>
      <Shell>{children}</Shell>
    </SessionProvider>
  );
}
