"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, describeError } from "@/lib/api";
import { date, money, dateTime } from "@/lib/format";
import type { QuoteSummary } from "@/lib/types";
import { Alert, Badge, Button, Card, Empty, inputClass, Loading, PageHeader } from "@/components/ui";

export default function QuotesPage() {
  const [rows, setRows] = useState<QuoteSummary[] | null>(null);
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function load() {
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (status) params.set("status", status);
    try {
      setRows(await api<QuoteSummary[]>(`/quotes${params.size ? `?${params}` : ""}`));
    } catch (e) {
      setError(describeError(e));
    }
  }
  useEffect(() => { void load(); // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status]);

  return (
    <>
      <PageHeader title="Quotes" subtitle="Searchable history of every quotation and its versions." />
      <Card>
        <form className="mb-3 flex flex-wrap gap-2" onSubmit={(e) => { e.preventDefault(); void load(); }}>
          <input aria-label="Search quotes" className={`${inputClass} sm:w-72`} placeholder="Quote number or customer" value={q} onChange={(e) => setQ(e.target.value)} data-testid="quote-search" />
          <select aria-label="Status filter" className={`${inputClass} sm:w-48`} value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">All statuses</option>
            {["draft", "pending_approval", "approved", "withdrawn"].map((s) => <option key={s} value={s}>{s.replace("_", " ")}</option>)}
          </select>
          <Button type="submit" variant="secondary">Search</Button>
        </form>
        {error && <Alert kind="error">{error}</Alert>}
        {!rows ? <Loading /> : rows.length === 0 ? <Empty title="No quotes found">Generate a quote from a parsed RFQ.</Empty> : (
          <div className="overflow-x-auto">
            <table className="data w-full" data-testid="quote-table">
              <thead><tr><th>Number</th><th>Customer</th><th>Version</th><th>Status</th><th className="text-right">Total</th><th>Valid until</th><th>Updated</th></tr></thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id}>
                    <td><Link href={`/quotes/${r.id}`} className="text-brand hover:underline">{r.number}</Link></td>
                    <td>{r.customer_name ?? "—"}</td><td>v{r.current_version_no}</td><td><Badge value={r.effective_status} /></td>
                    <td className="text-right">{money(r.grand_total, r.currency)} {r.currency}</td><td>{date(r.valid_until)}</td><td>{dateTime(r.updated_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  );
}
