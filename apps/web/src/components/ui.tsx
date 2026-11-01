"use client";

import Link from "next/link";
import type { ReactNode } from "react";

export function Alert({ kind = "info", title, children, testId }: { kind?: "info" | "error" | "success" | "warning"; title?: string; children?: ReactNode; testId?: string }) {
  const styles = {
    info: "border-sky-300 bg-sky-50 text-sky-900",
    error: "border-red-300 bg-red-50 text-red-900",
    success: "border-emerald-300 bg-emerald-50 text-emerald-900",
    warning: "border-amber-300 bg-amber-50 text-amber-900",
  }[kind];
  return (
    <div role={kind === "error" ? "alert" : "status"} data-testid={testId} className={`rounded-md border px-3 py-2 text-sm ${styles}`}>
      {title && <p className="font-semibold">{title}</p>}
      {children}
    </div>
  );
}

const badgeColors: Record<string, string> = {
  draft: "bg-slate-200 text-slate-800",
  pending_approval: "bg-amber-200 text-amber-900",
  approved: "bg-emerald-200 text-emerald-900",
  rejected: "bg-red-200 text-red-900",
  withdrawn: "bg-slate-300 text-slate-900",
  expired: "bg-red-100 text-red-800",
  superseded: "bg-slate-100 text-slate-700",
  parsed: "bg-emerald-100 text-emerald-900",
  failed: "bg-red-200 text-red-900",
  uploaded: "bg-slate-200 text-slate-800",
  parsing: "bg-sky-100 text-sky-900",
  quoted: "bg-indigo-100 text-indigo-900",
  exact: "bg-emerald-200 text-emerald-900",
  likely: "bg-sky-200 text-sky-900",
  ambiguous: "bg-amber-200 text-amber-900",
  unmatched: "bg-red-200 text-red-900",
  resolved: "bg-emerald-100 text-emerald-900",
  custom: "bg-indigo-100 text-indigo-900",
  active: "bg-emerald-200 text-emerald-900",
  imported: "bg-slate-200 text-slate-800",
  archived: "bg-slate-100 text-slate-600",
};

export function Badge({ value, testId }: { value: string; testId?: string }) {
  return (
    <span data-testid={testId} className={`inline-block rounded px-2 py-0.5 text-xs font-medium uppercase tracking-wide ${badgeColors[value] ?? "bg-slate-200 text-slate-800"}`}>
      {value.replace(/_/g, " ")}
    </span>
  );
}

export function Button({ children, variant = "primary", type = "button", disabled, onClick, testId, className = "" }: { children: ReactNode; variant?: "primary" | "secondary" | "danger" | "ghost"; type?: "button" | "submit"; disabled?: boolean; onClick?: () => void; testId?: string; className?: string }) {
  const base = "inline-flex items-center justify-center rounded-md px-3 py-2 text-sm font-medium focus:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50";
  const styles = {
    primary: "bg-brand text-white hover:bg-brand-dark focus-visible:ring-brand",
    secondary: "border border-slate-300 bg-white text-slate-900 hover:bg-slate-50 focus-visible:ring-slate-400",
    danger: "bg-red-600 text-white hover:bg-red-700 focus-visible:ring-red-500",
    ghost: "text-brand hover:bg-slate-100 focus-visible:ring-slate-400",
  }[variant];
  return (
    <button type={type} disabled={disabled} onClick={onClick} data-testid={testId} className={`${base} ${styles} ${className}`}>
      {children}
    </button>
  );
}

export function Field({ label, htmlFor, hint, error, children }: { label: string; htmlFor: string; hint?: string; error?: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={htmlFor} className="text-sm font-medium text-slate-800">
        {label}
      </label>
      {children}
      {hint && <p className="text-xs text-slate-500">{hint}</p>}
      {error && (
        <p className="text-xs text-red-700" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

export const inputClass = "w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 shadow-sm focus:border-brand focus:outline-none focus:ring-1 focus:ring-brand";

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <p role="status" aria-live="polite" className="text-sm text-slate-500" data-testid="loading">
      {label}
    </p>
  );
}

export function Empty({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed border-slate-300 p-8 text-center" data-testid="empty-state">
      <p className="text-base font-semibold text-slate-800">{title}</p>
      {children && <div className="mt-1 text-sm text-slate-600">{children}</div>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
      <div>
        <h1 className="text-2xl font-semibold text-slate-900">{title}</h1>
        {subtitle && <div className="mt-1 text-sm text-slate-600">{subtitle}</div>}
      </div>
      {actions && <div className="flex flex-wrap gap-2">{actions}</div>}
    </div>
  );
}

export function Card({ title, children, className = "" }: { title?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`rounded-lg border border-slate-200 bg-white p-4 shadow-sm ${className}`}>
      {title && <h2 className="mb-3 text-base font-semibold text-slate-900">{title}</h2>}
      {children}
    </section>
  );
}

export function Forbidden({ action }: { action: string }) {
  return (
    <Alert kind="warning" title="Not permitted" testId="forbidden">
      Your role cannot {action}. Ask an administrator if you need this permission.
    </Alert>
  );
}

export function BackLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <Link href={href} className="text-sm text-brand hover:underline">
      ← {children}
    </Link>
  );
}
