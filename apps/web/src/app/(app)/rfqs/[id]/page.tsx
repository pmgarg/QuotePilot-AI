"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { api, ApiError, describeError } from "@/lib/api";
import { money, qty } from "@/lib/format";
import type { Blocker, Quote, RFQ, RFQItem } from "@/lib/types";
import { useSession } from "@/components/session";
import { Alert, BackLink, Badge, Button, Card, inputClass, Loading, PageHeader } from "@/components/ui";

function ItemRow({ rfq, item, editable, onChanged, onError }: { rfq: RFQ; item: RFQItem; editable: boolean; onChanged: () => Promise<void>; onError: (m: string | null) => void }) {
  const [qtyValue, setQtyValue] = useState(item.quantity ?? "");
  const [unit, setUnit] = useState(item.unit_raw ?? "");
  const [busy, setBusy] = useState(false);

  async function patch(body: Record<string, unknown>) {
    setBusy(true);
    onError(null);
    try {
      await api(`/rfqs/${rfq.id}/items/${item.id}`, { method: "PATCH", json: body });
      await onChanged();
    } catch (e) {
      onError(describeError(e));
    } finally {
      setBusy(false);
    }
  }
  async function remove() {
    setBusy(true);
    try {
      await api(`/rfqs/${rfq.id}/items/${item.id}`, { method: "DELETE" });
      await onChanged();
    } catch (e) {
      onError(describeError(e));
      setBusy(false);
    }
  }
  const source = Object.entries(item.source_ref).map(([k, v]) => `${k} ${String(v)}`).join(", ");
  return (
    <tr data-testid={`rfq-item-${item.line_no}`} className={item.is_duplicate && !item.duplicate_confirmed ? "bg-amber-50" : ""}>
      <td>{item.line_no}</td>
      <td>
        <p className="font-medium">{item.description || <span className="italic text-slate-600">(no description)</span>}</p>
        <p className="text-xs text-slate-500" title={item.raw_text}>Source: {source || "manual"} · “{item.raw_text.slice(0, 80)}{item.raw_text.length > 80 ? "…" : ""}”</p>
        <div className="mt-1 flex flex-wrap gap-1">
          {item.is_duplicate && <Badge value={item.duplicate_confirmed ? "duplicate confirmed" : "possible duplicate"} />}
          {item.unit_mismatch && <Badge value={item.unit_confirmed ? "unit confirmed" : "unit mismatch"} />}
          {item.flags.map((f) => <Badge key={f} value={f} />)}
        </div>
      </td>
      <td>
        <input aria-label={`Quantity line ${item.line_no}`} className={`${inputClass} w-24`} value={qtyValue} onChange={(e) => setQtyValue(e.target.value)} onBlur={() => qtyValue !== (item.quantity ?? "") && void patch({ quantity: qtyValue })} disabled={!editable || busy} inputMode="decimal" data-testid={`qty-${item.line_no}`} />
        {item.quantity === null && <p className="text-xs text-red-700">Quantity required</p>}
      </td>
      <td>
        <input aria-label={`Unit line ${item.line_no}`} className={`${inputClass} w-20`} value={unit} onChange={(e) => setUnit(e.target.value)} onBlur={() => unit !== (item.unit_raw ?? "") && void patch({ unit })} disabled={!editable || busy} />
        <p className="text-xs text-slate-500">{item.unit_normalized ?? (item.unit_raw ? "unknown" : "—")}</p>
      </td>
      <td>
        <Badge value={item.match_status} testId={`match-${item.line_no}`} />
        {item.candidates.length > 0 && (
          <select aria-label={`Product for line ${item.line_no}`} className={`${inputClass} mt-1`} value={item.selected_product_id ?? ""} onChange={(e) => void patch({ selected_product_id: e.target.value || null })} disabled={!editable || busy} data-testid={`select-${item.line_no}`}>
            <option value="">— choose a product —</option>
            {item.candidates.map((c) => (
              <option key={c.product_id} value={c.product_id}>
                {c.sku} · {c.description} · {money(c.list_price, c.currency)} {c.currency}/{c.unit} ({c.category}, {c.reason})
              </option>
            ))}
          </select>
        )}
        {item.candidates.length === 0 && !item.is_custom && <p className="text-xs text-slate-500">No catalog candidate. Mark as custom line or remove.</p>}
        {item.is_custom && <p className="text-xs text-indigo-800">Custom line — price must be entered on the quote.</p>}
      </td>
      <td className="whitespace-nowrap">
        {editable && (
          <div className="flex flex-col gap-1">
            {item.unit_mismatch && !item.unit_confirmed && <Button variant="secondary" onClick={() => void patch({ unit_confirmed: true })} disabled={busy} testId={`confirm-unit-${item.line_no}`}>Confirm unit</Button>}
            {item.is_duplicate && !item.duplicate_confirmed && <Button variant="secondary" onClick={() => void patch({ duplicate_confirmed: true })} disabled={busy}>Keep duplicate</Button>}
            {!item.selected_product_id && <Button variant="secondary" onClick={() => void patch({ is_custom: !item.is_custom })} disabled={busy}>{item.is_custom ? "Unmark custom" : "Mark custom"}</Button>}
            <Button variant="ghost" onClick={() => void remove()} disabled={busy} testId={`remove-${item.line_no}`}>Remove</Button>
          </div>
        )}
      </td>
    </tr>
  );
}

export default function RFQReviewPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { can } = useSession();
  const [rfq, setRfq] = useState<RFQ | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [blockers, setBlockers] = useState<Blocker[]>([]);
  const [busy, setBusy] = useState(false);
  const [newLine, setNewLine] = useState({ description: "", quantity: "", unit: "", sku_hint: "" });

  const load = useCallback(async () => {
    try {
      setRfq(await api<RFQ>(`/rfqs/${id}`));
    } catch (e) {
      setError(describeError(e));
    }
  }, [id]);
  useEffect(() => { void load(); }, [load]);

  if (error && !rfq) return <Alert kind="error">{error}</Alert>;
  if (!rfq) return <Loading />;
  const editable = can("edit_quotes") && rfq.status !== "quoted";

  async function generate() {
    setBusy(true);
    setError(null);
    setBlockers([]);
    try {
      const quote = await api<Quote>(`/rfqs/${rfq!.id}/generate-quote`, { method: "POST" });
      router.push(`/quotes/${quote.id}`);
    } catch (e) {
      if (e instanceof ApiError && e.code === "blocked") setBlockers(e.blockers);
      else setError(describeError(e));
      setBusy(false);
    }
  }
  async function reparse() {
    setBusy(true);
    try {
      await api(`/rfqs/${rfq!.id}/reparse`, { method: "POST" });
      await load();
    } catch (e) {
      setError(describeError(e));
    } finally {
      setBusy(false);
    }
  }
  async function addLine() {
    setError(null);
    try {
      await api(`/rfqs/${rfq!.id}/items`, { method: "POST", json: { description: newLine.description, quantity: newLine.quantity, unit: newLine.unit || null, sku_hint: newLine.sku_hint || null } });
      setNewLine({ description: "", quantity: "", unit: "", sku_hint: "" });
      await load();
    } catch (e) {
      setError(describeError(e));
    }
  }

  const unresolved = rfq.items.filter((i) => !i.selected_product_id && !i.is_custom).length;
  return (
    <>
      <BackLink href="/rfqs">All RFQs</BackLink>
      <PageHeader
        title={`RFQ ${rfq.reference ?? rfq.id.slice(0, 8)}`}
        subtitle={<span className="flex items-center gap-2">Source: {rfq.source_kind} · Currency {rfq.currency} · <Badge value={rfq.status} testId="rfq-status" />{rfq.source_file_id && <a className="text-brand hover:underline" href={`/api/v1/files/${rfq.source_file_id}`}>download original</a>}</span>}
        actions={
          <>
            {editable && rfq.status !== "parsing" && <Button variant="secondary" onClick={() => void reparse()} disabled={busy}>Re-parse</Button>}
            {editable && <Button onClick={() => void generate()} disabled={busy || rfq.status !== "parsed"} testId="generate-quote">Generate draft quote</Button>}
          </>
        }
      />
      {rfq.status === "failed" && <div className="mb-4"><Alert kind="error" title="Parsing failed" testId="rfq-failed">{rfq.failure_reason}. You can still add lines manually below, or re-parse after fixing the document.</Alert></div>}
      {rfq.status === "quoted" && <div className="mb-4"><Alert kind="info">A quote was generated from this RFQ; lines are locked. Open it from the Quotes page.</Alert></div>}
      {Array.isArray(rfq.parse_meta.flags) && (rfq.parse_meta.flags as string[]).length > 0 && <div className="mb-4"><Alert kind="warning">Parser notes: {(rfq.parse_meta.flags as string[]).join(", ")}</Alert></div>}
      {error && <div className="mb-4"><Alert kind="error">{error}</Alert></div>}
      {blockers.length > 0 && (
        <div className="mb-4">
          <Alert kind="warning" title="Resolve these before generating a quote" testId="rfq-blockers">
            <ul className="list-disc pl-5">{blockers.map((b, i) => <li key={i}>{b.message}</li>)}</ul>
          </Alert>
        </div>
      )}
      <Card title={`Extracted lines (${rfq.items.length}) — ${unresolved} unresolved`}>
        <p className="mb-3 text-xs text-slate-600">Confidence is categorical: <strong>exact</strong> = SKU found in the active catalog; <strong>likely</strong>/<strong>ambiguous</strong> = description similarity, you must choose; <strong>unmatched</strong> = nothing similar found.</p>
        <div className="overflow-x-auto">
          <table className="data w-full">
            <thead><tr><th>#</th><th>Requested</th><th>Qty</th><th>Unit</th><th>Match</th><th></th></tr></thead>
            <tbody>{rfq.items.map((item) => <ItemRow key={item.id} rfq={rfq} item={item} editable={editable} onChanged={load} onError={setError} />)}</tbody>
          </table>
        </div>
        {editable && (
          <div className="mt-4 grid gap-2 border-t border-slate-200 pt-4 sm:grid-cols-5">
            <input aria-label="New line description" placeholder="Description" className={inputClass} value={newLine.description} onChange={(e) => setNewLine({ ...newLine, description: e.target.value })} />
            <input aria-label="New line quantity" placeholder="Qty" className={inputClass} value={newLine.quantity} onChange={(e) => setNewLine({ ...newLine, quantity: e.target.value })} inputMode="decimal" />
            <input aria-label="New line unit" placeholder="Unit" className={inputClass} value={newLine.unit} onChange={(e) => setNewLine({ ...newLine, unit: e.target.value })} />
            <input aria-label="New line SKU" placeholder="SKU (optional)" className={inputClass} value={newLine.sku_hint} onChange={(e) => setNewLine({ ...newLine, sku_hint: e.target.value })} />
            <Button variant="secondary" onClick={() => void addLine()} disabled={!newLine.description || !newLine.quantity}>Add line</Button>
          </div>
        )}
      </Card>
      <p className="mt-3 text-xs text-slate-500">Quantities shown: {rfq.items.map((i) => qty(i.quantity)).join(", ")}. <Link href="/catalog" className="text-brand hover:underline">Manage catalog</Link></p>
    </>
  );
}
