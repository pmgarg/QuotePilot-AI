"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, describeError } from "@/lib/api";
import { dateTime } from "@/lib/format";
import type { RFQSummary } from "@/lib/types";
import { useSession } from "@/components/session";
import { Alert, Badge, Button, Card, Empty, inputClass, Loading, PageHeader } from "@/components/ui";

export default function RFQListPage() {
  const { can } = useSession();
  const [rows, setRows] = useState<RFQSummary[] | null>(null);
  const [status, setStatus] = useState("");
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api<RFQSummary[]>(`/rfqs${status ? `?status=${status}` : ""}`).then(setRows).catch((e) => setError(describeError(e)));
  }, [status]);
  return (
    <>
      <PageHeader title="RFQs" subtitle="Uploaded or pasted requests for quotation." actions={can("edit_quotes") ? <Link href="/rfqs/new"><Button testId="new-rfq">New RFQ</Button></Link> : null} />
      <Card>
        <div className="mb-3 flex items-center gap-2">
          <label htmlFor="rfq-status" className="text-sm text-slate-700">Status</label>
          <select id="rfq-status" className={`${inputClass} w-48`} value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">All</option>
            {["uploaded", "parsing", "parsed", "failed", "quoted"].map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </div>
        {error && <Alert kind="error">{error}</Alert>}
        {!rows ? <Loading /> : rows.length === 0 ? <Empty title="No RFQs yet" action={can("edit_quotes") ? <Link href="/rfqs/new"><Button>Upload your first RFQ</Button></Link> : undefined} /> : (
          <table className="data w-full" data-testid="rfq-table">
            <thead><tr><th>Reference</th><th>Source</th><th>Currency</th><th>Status</th><th>Created</th></tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td><Link href={`/rfqs/${r.id}`} className="text-brand hover:underline">{r.reference ?? r.id.slice(0, 8)}</Link></td>
                  <td>{r.source_kind}</td><td>{r.currency}</td><td><Badge value={r.status} /></td><td>{dateTime(r.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </>
  );
}
