"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, describeError } from "@/lib/api";
import { money, dateTime } from "@/lib/format";
import type { Catalog, QuoteSummary, RFQSummary } from "@/lib/types";
import { useSession } from "@/components/session";
import { Alert, Badge, Button, Card, Empty, Loading, PageHeader } from "@/components/ui";

export default function DashboardPage() {
  const { session, can } = useSession();
  const [rfqs, setRfqs] = useState<RFQSummary[] | null>(null);
  const [quotes, setQuotes] = useState<QuoteSummary[] | null>(null);
  const [catalogs, setCatalogs] = useState<Catalog[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([api<RFQSummary[]>("/rfqs"), api<QuoteSummary[]>("/quotes"), api<Catalog[]>("/catalogs")])
      .then(([r, q, c]) => {
        setRfqs(r);
        setQuotes(q);
        setCatalogs(c);
      })
      .catch((e) => setError(describeError(e)));
  }, []);

  if (error) return <Alert kind="error">{error}</Alert>;
  if (!rfqs || !quotes || !catalogs) return <Loading />;
  const hasCatalog = catalogs.some((c) => c.active_version_id);
  const pending = quotes.filter((q) => q.status === "pending_approval");

  return (
    <>
      <PageHeader
        title="Dashboard"
        subtitle={`Welcome, ${session?.user.full_name}.`}
        actions={
          can("edit_quotes") ? (
            <Link href="/rfqs/new">
              <Button testId="new-rfq">New RFQ</Button>
            </Link>
          ) : null
        }
      />
      {!hasCatalog && (
        <div className="mb-6">
          <Empty title="No active price catalog yet" action={can("import_catalog") ? <Link href="/catalog"><Button>Import a catalog</Button></Link> : undefined}>
            RFQ lines cannot be matched until an administrator imports and activates a catalog.
          </Empty>
        </div>
      )}
      <div className="grid gap-6 lg:grid-cols-2">
        <Card title={`Pending approvals (${pending.length})`}>
          {pending.length === 0 ? (
            <p className="text-sm text-slate-600">Nothing waiting for approval.</p>
          ) : (
            <ul className="divide-y divide-slate-100">
              {pending.map((q) => (
                <li key={q.id} className="flex items-center justify-between py-2 text-sm">
                  <Link href={`/quotes/${q.id}`} className="text-brand hover:underline">
                    {q.number} · {q.customer_name ?? "No customer"}
                  </Link>
                  <span>
                    {money(q.grand_total, q.currency)} {q.currency}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>
        <Card title="Recent RFQs">
          {rfqs.length === 0 ? (
            <p className="text-sm text-slate-600">No RFQs yet.</p>
          ) : (
            <ul className="divide-y divide-slate-100">
              {rfqs.slice(0, 8).map((r) => (
                <li key={r.id} className="flex items-center justify-between py-2 text-sm">
                  <Link href={`/rfqs/${r.id}`} className="text-brand hover:underline">
                    {r.reference ?? r.id.slice(0, 8)} · {r.source_kind}
                  </Link>
                  <span className="flex items-center gap-2 text-slate-500">
                    {dateTime(r.created_at)} <Badge value={r.status} />
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>
        <Card title="Recent quotes" className="lg:col-span-2">
          {quotes.length === 0 ? (
            <p className="text-sm text-slate-600">No quotes yet.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="data w-full">
                <thead>
                  <tr>
                    <th>Number</th>
                    <th>Customer</th>
                    <th>Status</th>
                    <th className="text-right">Total</th>
                    <th>Updated</th>
                  </tr>
                </thead>
                <tbody>
                  {quotes.slice(0, 10).map((q) => (
                    <tr key={q.id}>
                      <td>
                        <Link href={`/quotes/${q.id}`} className="text-brand hover:underline">
                          {q.number}
                        </Link>
                      </td>
                      <td>{q.customer_name ?? "—"}</td>
                      <td>
                        <Badge value={q.effective_status} />
                      </td>
                      <td className="text-right">
                        {money(q.grand_total, q.currency)} {q.currency}
                      </td>
                      <td>{dateTime(q.updated_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>
    </>
  );
}
