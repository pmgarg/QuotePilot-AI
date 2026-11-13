"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { api, describeError } from "@/lib/api";
import { dateTime, money, qty } from "@/lib/format";
import type { Order } from "@/lib/types";
import { useSession } from "@/components/session";
import { Alert, BackLink, Badge, Button, Card, Field, inputClass, Loading, PageHeader } from "@/components/ui";

export default function OrderPage() {
  const { id } = useParams<{ id: string }>();
  const { can, session } = useSession();
  const [order, setOrder] = useState<Order | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setOrder(await api<Order>(`/orders/${id}`));
    } catch (e) {
      setError(describeError(e));
    }
  }, [id]);
  useEffect(() => { void load(); }, [load]);

  async function act(path: string, ok: string) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      setOrder(await api<Order>(`/orders/${id}/${path}`, { method: "POST", json: { reason: reason || null } }));
      setNotice(ok);
      setReason("");
    } catch (e) {
      setError(describeError(e));
    } finally {
      setBusy(false);
    }
  }

  if (error && !order) return <Alert kind="error">{error}</Alert>;
  if (!order) return <Loading />;
  const showCost = order.lines.some((l) => "cost_price" in l);
  const isProposer = order.created_by === session?.user.id;
  return (
    <>
      <BackLink href="/orders">Orders</BackLink>
      <PageHeader title={order.number} subtitle={<span>Status <Badge value={order.status} testId="order-status" /> · source {order.source} · {order.currency}{order.quote_id && <> · from <Link className="text-brand underline" href={`/quotes/${order.quote_id}`}>quote v{order.quote_version_no}</Link></>}</span>} />
      {error && <div className="mb-4"><Alert kind="error">{error}</Alert></div>}
      {notice && <div className="mb-4"><Alert kind="success" testId="order-notice">{notice}</Alert></div>}
      <div className="grid gap-6 lg:grid-cols-3">
        <Card title="Lines" className="lg:col-span-2">
          <table className="data w-full" data-testid="order-lines">
            <thead><tr><th>#</th><th>SKU</th><th>Description</th><th>Qty</th><th>Unit price</th>{showCost && <th>Cost</th>}<th>Tax %</th><th>Total</th></tr></thead>
            <tbody>
              {order.lines.map((l) => (
                <tr key={l.id}><td>{l.line_no}</td><td>{l.sku ?? "—"}</td><td>{l.description}</td><td>{qty(l.quantity)} {l.unit}</td><td>{money(l.unit_price, order.currency)}</td>{showCost && <td>{money(l.cost_price ?? null, order.currency)}</td>}<td>{l.tax_rate_pct}</td><td>{money(l.line_total, order.currency)}</td></tr>
              ))}
            </tbody>
            <tfoot><tr><td colSpan={showCost ? 7 : 6} className="text-right font-semibold">Grand total</td><td className="font-semibold" data-testid="order-total">{money(order.grand_total, order.currency)}</td></tr></tfoot>
          </table>
        </Card>
        <div className="grid gap-6">
          <Card title="Actions">
            <div className="grid gap-3">
              <Field label="Reason (required to cancel)" htmlFor="order-reason"><input id="order-reason" className={inputClass} value={reason} onChange={(e) => setReason(e.target.value)} /></Field>
              {order.status === "proposed" && can("approve_order") && !(isProposer && !session?.organization.allow_self_approval) && <Button onClick={() => void act("confirm", "Order confirmed.")} disabled={busy} testId="confirm-order">Confirm order</Button>}
              {order.status === "proposed" && can("approve_order") && isProposer && !session?.organization.allow_self_approval && <Alert kind="info" testId="order-self-approval-blocked">You proposed this order, so another approver must confirm it.</Alert>}
              {order.status === "proposed" && !can("approve_order") && <Alert kind="info">Waiting for an approver to confirm.</Alert>}
              {order.status === "confirmed" && can("edit_quotes") && <Button onClick={() => void act("fulfil", "Order marked fulfilled.")} disabled={busy} testId="fulfil-order">Mark fulfilled</Button>}
              {(order.status === "proposed" || order.status === "confirmed") && can("approve_order") && <Button variant="danger" onClick={() => void act("cancel", "Order cancelled.")} disabled={busy || !reason.trim()} testId="cancel-order">Cancel order</Button>}
            </div>
          </Card>
          <Card title="Events">
            <ol className="text-sm" data-testid="order-events">
              {order.events.map((e) => <li key={e.id} className="border-b border-slate-100 py-1"><Badge value={e.kind} /> <span className="text-slate-600">{dateTime(e.occurred_at)}</span> · {e.source}{e.detail ? ` — ${e.detail}` : ""}</li>)}
            </ol>
          </Card>
        </div>
      </div>
    </>
  );
}
