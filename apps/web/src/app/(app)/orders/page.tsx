"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, describeError } from "@/lib/api";
import { dateTime, money } from "@/lib/format";
import type { OrderSummary, Proposal } from "@/lib/types";
import { useSession } from "@/components/session";
import { Alert, Badge, Button, Card, Empty, inputClass, Loading, PageHeader } from "@/components/ui";

export default function OrdersPage() {
  const { can } = useSession();
  const [orders, setOrders] = useState<OrderSummary[] | null>(null);
  const [proposals, setProposals] = useState<Proposal[] | null>(null);
  const [status, setStatus] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [reasons, setReasons] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    try {
      const [o, p] = await Promise.all([api<OrderSummary[]>(`/orders${status ? `?status=${status}` : ""}`), api<Proposal[]>("/order-proposals?status=proposed")]);
      setOrders(o);
      setProposals(p);
    } catch (e) {
      setError(describeError(e));
    }
  }, [status]);
  useEffect(() => { void load(); }, [load]);

  async function decide(id: string, approve: boolean) {
    setBusy(true);
    setError(null);
    try {
      await api(`/order-proposals/${id}/${approve ? "approve" : "reject"}`, { method: "POST", json: { reason: reasons[id] || null } });
      await load();
    } catch (e) {
      setError(describeError(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader title="Orders" subtitle="Orders are proposed from approved quotes, repeat-order proposals, imports or connectors, and become real only when an approver confirms them." />
      {error && <div className="mb-4"><Alert kind="error">{error}</Alert></div>}
      <div className="grid gap-6">
        <Card title="Repeat-order proposals awaiting a decision">
          {!proposals ? <Loading /> : proposals.length === 0 ? <Empty title="No open proposals">Generate one from a customer profile when their history suggests a reorder.</Empty> : (
            <ul className="divide-y divide-slate-200" data-testid="proposal-list">
              {proposals.map((p) => (
                <li key={p.id} className="py-3">
                  <div className="flex flex-wrap items-center gap-2 text-sm">
                    <Badge value={p.kind} /> <span className="text-slate-600">{dateTime(p.created_at)}</span> · {p.lines.length} line(s) · {p.currency}
                    <Link href={`/customers/${p.customer_id}`} className="text-brand underline">customer</Link>
                  </div>
                  <ul className="mt-1 list-disc pl-5 text-xs text-slate-700">{p.evidence.map((e, i) => <li key={i}>{e}</li>)}</ul>
                  {can("approve_order") && (
                    <div className="mt-2 flex flex-wrap items-center gap-2">
                      <input aria-label={`Decision reason for proposal ${p.id.slice(0, 8)}`} className={`${inputClass} w-72`} placeholder="reason (required to reject)" value={reasons[p.id] ?? ""} onChange={(e) => setReasons({ ...reasons, [p.id]: e.target.value })} />
                      <Button onClick={() => void decide(p.id, true)} disabled={busy} testId={`approve-proposal-${p.id.slice(0, 8)}`}>Approve → proposed order</Button>
                      <Button variant="secondary" onClick={() => void decide(p.id, false)} disabled={busy}>Reject</Button>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </Card>
        <Card title="Orders">
          <div className="mb-3 flex items-center gap-2">
            <label htmlFor="order-status" className="text-sm">Status</label>
            <select id="order-status" className={`${inputClass} w-48`} value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="">all</option>
              {["proposed", "confirmed", "fulfilled", "cancelled"].map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
          {!orders ? <Loading /> : orders.length === 0 ? <Empty title="No orders yet">Convert an approved quote from its quote page.</Empty> : (
            <table className="data w-full" data-testid="order-table">
              <thead><tr><th>Number</th><th>Status</th><th>Source</th><th>Total</th><th>Created</th><th>Confirmed</th></tr></thead>
              <tbody>
                {orders.map((o) => (
                  <tr key={o.id}>
                    <td><Link href={`/orders/${o.id}`} className="text-brand underline">{o.number}</Link></td>
                    <td><Badge value={o.status} /></td>
                    <td>{o.source}</td>
                    <td>{money(o.grand_total, o.currency)}</td>
                    <td>{dateTime(o.created_at)}</td>
                    <td>{o.confirmed_at ? dateTime(o.confirmed_at) : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      </div>
    </>
  );
}
