"use client";

import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { api, ApiError, describeError } from "@/lib/api";
import { date, dateTime, money, pct, qty } from "@/lib/format";
import type { Blocker, History, Product, Quote, QuoteLine } from "@/lib/types";
import { useSession } from "@/components/session";
import { Alert, BackLink, Badge, Button, Card, Field, inputClass, Loading, PageHeader } from "@/components/ui";
import { FollowUpPanel } from "@/components/followup";

type Draft = { id?: string; product_id?: string | null; sku?: string | null; description: string; quantity: string; unit: string; unit_price: string; discount_pct: string; tax_rate_pct: string; is_custom: boolean; price_source?: string; price_touched?: boolean };

function toDraft(l: QuoteLine): Draft {
  return { id: l.id, product_id: l.product_id, sku: l.sku, description: l.description, quantity: l.quantity, unit: l.unit, unit_price: l.unit_price ?? "", discount_pct: l.discount_pct, tax_rate_pct: l.tax_rate_pct, is_custom: l.is_custom, price_source: l.price_source, price_touched: false };
}

export default function QuotePage() {
  const { id } = useParams<{ id: string }>();
  const { can, session } = useSession();
  const [quote, setQuote] = useState<Quote | null>(null);
  const [history, setHistory] = useState<History | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [blockers, setBlockers] = useState<Blocker[] | null>(null);
  const [conflict, setConflict] = useState(false);
  const [busy, setBusy] = useState(false);
  const [lines, setLines] = useState<Draft[]>([]);
  const [notes, setNotes] = useState("");
  const [terms, setTerms] = useState("");
  const [validUntil, setValidUntil] = useState("");
  const [reason, setReason] = useState("");
  const [dirty, setDirty] = useState(false);

  const load = useCallback(async () => {
    try {
      const q = await api<Quote>(`/quotes/${id}`);
      setQuote(q);
      setLines(q.current_version.lines.map(toDraft));
      setNotes(q.current_version.notes ?? "");
      setTerms(q.current_version.terms ?? "");
      setValidUntil(q.valid_until ?? "");
      setDirty(false);
      setConflict(false);
      api<History>(`/quotes/${id}/history`).then(setHistory).catch(() => setHistory(null));
    } catch (e) {
      setError(describeError(e));
    }
  }, [id]);
  useEffect(() => { void load(); }, [load]);

  if (error && !quote) return <Alert kind="error">{error}</Alert>;
  if (!quote) return <Loading />;
  const version = quote.current_version;
  const isDraft = version.status === "draft" && quote.status !== "withdrawn";
  const canEdit = can("edit_quotes") && isDraft;
  const showCosts = can("view_costs");
  const isSubmitter = version.submitted_by === session?.user.id;

  async function action(path: string, body: Record<string, unknown> = {}, successMessage?: string) {
    setBusy(true);
    setError(null);
    setNotice(null);
    setBlockers(null);
    try {
      await api(`/quotes/${quote!.id}${path}`, { method: "POST", json: { expected_lock_version: quote!.lock_version, ...body } });
      if (successMessage) setNotice(successMessage);
      setReason("");
      await load();
    } catch (e) {
      if (e instanceof ApiError && e.code === "blocked") setBlockers(e.blockers);
      else if (e instanceof ApiError && e.code === "conflict") setConflict(true);
      else setError(describeError(e));
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    setBusy(true);
    setError(null);
    setBlockers(null);
    try {
      const payload = {
        expected_lock_version: quote!.lock_version,
        valid_until: validUntil || null,
        notes: notes || null,
        terms: terms || null,
        lines: lines.map((l) => ({ id: l.id, product_id: l.product_id ?? null, sku: l.sku ?? null, description: l.description, quantity: l.quantity, unit: l.unit, unit_price: l.unit_price === "" ? null : l.price_source === "catalog" && !l.price_touched ? undefined : l.unit_price, discount_pct: l.discount_pct, tax_rate_pct: l.tax_rate_pct, is_custom: l.is_custom })),
      };
      await api(`/quotes/${quote!.id}`, { method: "PATCH", json: payload });
      setNotice("Draft saved.");
      await load();
    } catch (e) {
      if (e instanceof ApiError && e.code === "conflict") setConflict(true);
      else setError(describeError(e));
    } finally {
      setBusy(false);
    }
  }

  function updateLine(i: number, patch: Partial<Draft>) {
    setLines((ls) => ls.map((l, idx) => (idx === i ? { ...l, ...patch } : l)));
    setDirty(true);
  }

  // R3-6: a catalog line priced from a list that is no longer this quote's price list can be re-linked to the same
  // SKU in the active catalog; the server re-prices it on save (never the browser).
  async function relinkToActiveCatalog(i: number) {
    const sku = lines[i]?.sku;
    if (!sku) return;
    setError(null);
    try {
      const { version_id: active } = await api<{ version_id: string | null }>("/catalogs/active-version");
      if (!active) throw new Error("No active catalog version.");
      const page = await api<{ items: Product[] }>(`/catalogs/versions/${active}/products?q=${encodeURIComponent(sku)}`);
      const match = page.items.find((p) => p.sku.toUpperCase() === sku.toUpperCase());
      if (!match) throw new Error(`SKU ${sku} is not in the active catalog; mark the line custom or remove it.`);
      updateLine(i, { product_id: match.id, price_source: "catalog", price_touched: false, unit_price: match.list_price });
      setNotice(`Line ${i + 1} re-linked to the active catalog; save the draft to re-price it.`);
    } catch (e) {
      setError(describeError(e));
    }
  }

  const approvedVersions = quote.versions.filter((v) => v.status === "approved");
  const withdrawnVersions = quote.versions.filter((v) => v.status === "withdrawn");
  const canWithdraw = approvedVersions.length > 0 || version.status === "pending_approval";
  return (
    <>
      <BackLink href="/quotes">All quotes</BackLink>
      <PageHeader
        title={`${quote.number} · v${version.version_no}`}
        subtitle={<span className="flex flex-wrap items-center gap-2"><Badge value={quote.effective_status} testId="quote-status" /><span>Version: <Badge value={version.status} testId="version-status" /></span><span>Currency {quote.currency}</span><span>Valid until {date(quote.valid_until)}</span></span>}
        actions={
          <>
            <a className="inline-flex items-center rounded-md border border-slate-300 bg-white px-3 py-2 text-sm hover:bg-slate-50" href={`/api/v1/quotes/${quote.id}/versions/${version.version_no}/pdf`} data-testid="download-pdf">{version.status === "approved" ? "Download PDF" : "Preview PDF (draft)"}</a>
            <a className="inline-flex items-center rounded-md border border-slate-300 bg-white px-3 py-2 text-sm hover:bg-slate-50" href={`/api/v1/quotes/${quote.id}/versions/${version.version_no}/csv`}>CSV</a>
          </>
        }
      />
      {conflict && <div className="mb-4"><Alert kind="warning" title="This quote was changed by someone else" testId="conflict-banner">Reload to see the latest version before making further changes. <Button variant="secondary" onClick={() => void load()}>Reload</Button></Alert></div>}
      {notice && <div className="mb-4"><Alert kind="success" testId="quote-notice">{notice}</Alert></div>}
      {error && <div className="mb-4"><Alert kind="error" testId="quote-error">{error}</Alert></div>}
      {quote.status === "withdrawn" && <div className="mb-4"><Alert kind="warning" title="Withdrawn">{quote.withdrawn_reason}</Alert></div>}
      {quote.status !== "withdrawn" && withdrawnVersions.length > 0 && <div className="mb-4"><Alert kind="warning" title="Earlier document withdrawn" testId="withdrawn-documents">Version{withdrawnVersions.length > 1 ? "s" : ""} {withdrawnVersions.map((v) => `v${v.version_no}`).join(", ")} {withdrawnVersions.length > 1 ? "were" : "was"} withdrawn{quote.withdrawn_reason ? `: ${quote.withdrawn_reason}` : ""}. Exports of withdrawn versions are refused.</Alert></div>}
      {(blockers ?? quote.blockers).length > 0 && (
        <div className="mb-4">
          <Alert kind="warning" title="Blockers (submission disabled until resolved)" testId="quote-blockers">
            <ul className="list-disc pl-5">
              {(blockers ?? quote.blockers).map((b, i) => (
                <li key={i}>{b.message}{b.overridable_by.length > 0 && <span className="text-xs"> — override by {b.overridable_by.join("/")} with a reason</span>}</li>
              ))}
            </ul>
          </Alert>
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-3">
        <Card className="lg:col-span-2" title="Lines">
          <div className="overflow-x-auto">
            <table className="data w-full" data-testid="quote-lines">
              <thead>
                <tr><th>#</th><th>SKU</th><th>Description</th><th>Qty</th><th>Unit</th><th>Unit price</th><th>Disc %</th><th>Tax %</th><th className="text-right">Line total</th>{showCosts && <th className="text-right">Margin</th>}{canEdit && <th></th>}</tr>
              </thead>
              <tbody>
                {lines.map((l, i) => {
                  const saved = version.lines[i];
                  const stale = canEdit && saved && (blockers ?? quote.blockers).some((b) => b.code === "catalog_version_mismatch" && b.line_no === saved.line_no);
                  return (
                    <tr key={l.id ?? `new-${i}`} data-testid={`quote-line-${i + 1}`}>
                      <td>{i + 1}</td>
                      <td className="font-mono text-xs">{l.sku ?? "—"}</td>
                      <td>{canEdit ? <input aria-label={`Description ${i + 1}`} className={inputClass} value={l.description} onChange={(e) => updateLine(i, { description: e.target.value })} /> : l.description}{l.is_custom && <Badge value="custom" />}{saved?.price_source === "manual" && <Badge value="manual price" />}{stale && <Button variant="secondary" className="ml-2" onClick={() => void relinkToActiveCatalog(i)} testId={`relink-${i + 1}`}>Use active catalog</Button>}{saved?.margin_override_reason && <p className="text-xs text-amber-800">Price confirmed by owner/admin: {saved.margin_override_reason}</p>}</td>
                      <td>{canEdit ? <input aria-label={`Quantity ${i + 1}`} className={`${inputClass} w-20`} value={l.quantity} onChange={(e) => updateLine(i, { quantity: e.target.value })} inputMode="decimal" /> : qty(l.quantity)}</td>
                      <td>{l.unit}</td>
                      <td>{canEdit ? <input aria-label={`Unit price ${i + 1}`} className={`${inputClass} w-28`} value={l.unit_price} placeholder="required" onChange={(e) => updateLine(i, { unit_price: e.target.value, price_touched: true })} inputMode="decimal" data-testid={`unit-price-${i + 1}`} /> : money(l.unit_price || null, quote.currency)}</td>
                      <td>{canEdit ? <input aria-label={`Discount ${i + 1}`} className={`${inputClass} w-20`} value={l.discount_pct} onChange={(e) => updateLine(i, { discount_pct: e.target.value })} inputMode="decimal" data-testid={`discount-${i + 1}`} /> : pct(l.discount_pct)}</td>
                      <td>{canEdit ? <input aria-label={`Tax ${i + 1}`} className={`${inputClass} w-20`} value={l.tax_rate_pct} onChange={(e) => updateLine(i, { tax_rate_pct: e.target.value })} inputMode="decimal" /> : pct(l.tax_rate_pct)}</td>
                      <td className="text-right" data-testid={`line-total-${i + 1}`}>{saved && !dirty ? money(saved.line_total, quote.currency) : "…"}</td>
                      {showCosts && <td className="text-right">{saved?.margin_pct != null ? <span className={Number(saved.margin_pct) < Number(session?.organization.min_margin_pct ?? 0) ? "text-red-700" : ""}>{pct(saved.margin_pct)}</span> : "—"}</td>}
                      {canEdit && <td><Button variant="ghost" onClick={() => { setLines(lines.filter((_, idx) => idx !== i)); setDirty(true); }}>Remove</Button></td>}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {canEdit && (
            <div className="mt-3 flex flex-wrap gap-2">
              <Button variant="secondary" onClick={() => { setLines([...lines, { description: "", quantity: "1", unit: "EA", unit_price: "", discount_pct: "0", tax_rate_pct: session?.organization.default_tax_rate ?? "0", is_custom: true }]); setDirty(true); }}>Add custom line</Button>
              <Button onClick={() => void save()} disabled={busy} testId="save-draft">Save draft</Button>
              {dirty && <span className="self-center text-xs text-amber-800">Unsaved changes — totals update after saving.</span>}
            </div>
          )}
          <div className="mt-4 grid gap-3 sm:grid-cols-3">
            <Field label="Valid until" htmlFor="valid-until"><input id="valid-until" type="date" className={inputClass} value={validUntil} onChange={(e) => { setValidUntil(e.target.value); setDirty(true); }} disabled={!canEdit} /></Field>
            <Field label="Notes (printed)" htmlFor="notes"><textarea id="notes" rows={2} className={inputClass} value={notes} onChange={(e) => { setNotes(e.target.value); setDirty(true); }} disabled={!canEdit} /></Field>
            <Field label="Terms (printed)" htmlFor="terms"><textarea id="terms" rows={2} className={inputClass} value={terms} onChange={(e) => { setTerms(e.target.value); setDirty(true); }} disabled={!canEdit} /></Field>
          </div>
        </Card>

        <div className="flex flex-col gap-6">
          <Card title="Totals">
            <dl className="grid grid-cols-2 gap-y-1 text-sm">
              <dt className="text-slate-600">Subtotal</dt><dd className="text-right" data-testid="subtotal">{money(version.subtotal, quote.currency)}</dd>
              <dt className="text-slate-600">Discount</dt><dd className="text-right">−{money(version.discount_total, quote.currency)}</dd>
              <dt className="text-slate-600">Tax</dt><dd className="text-right">{money(version.tax_total, quote.currency)}</dd>
              <dt className="font-semibold text-slate-900">Total {quote.currency}</dt><dd className="text-right font-semibold" data-testid="grand-total">{money(version.grand_total, quote.currency)}</dd>
            </dl>
          </Card>

          <Card title="Workflow">
            <div className="flex flex-col gap-2">
              {isDraft && can("submit_quote") && <Button onClick={() => void action("/submit", {}, "Submitted for approval.")} disabled={busy || dirty || quote.blockers.length > 0} testId="submit-quote">Submit for approval</Button>}
              {isDraft && dirty && <p className="text-xs text-amber-800">Save the draft before submitting.</p>}
              {version.status === "pending_approval" && can("approve_quote") && !(isSubmitter && !session?.organization.allow_self_approval) && (
                <>
                  <Field label="Decision reason (required to reject)" htmlFor="reason"><textarea id="reason" rows={2} className={inputClass} value={reason} onChange={(e) => setReason(e.target.value)} data-testid="decision-reason" /></Field>
                  <Button onClick={() => void action("/approve", { reason: reason || null }, "Quote approved.")} disabled={busy} testId="approve-quote">Approve</Button>
                  <Button variant="danger" onClick={() => void action("/reject", { reason }, "Quote rejected and returned to draft.")} disabled={busy || !reason.trim()} testId="reject-quote">Reject</Button>
                </>
              )}
              {version.status === "pending_approval" && can("approve_quote") && isSubmitter && !session?.organization.allow_self_approval && <Alert kind="info" testId="self-approval-blocked">You submitted this version, so another approver must decide (separation of duties).</Alert>}
              {version.status === "pending_approval" && !can("approve_quote") && <Alert kind="info">Waiting for an approver.</Alert>}
              {(quote.status === "approved" || quote.effective_status === "expired") && can("edit_quotes") && <Button variant="secondary" onClick={() => void action("/versions", {}, "New draft version created; the approved version remains in history.")} disabled={busy} testId="new-version">Create new version</Button>}
              {canWithdraw && can("withdraw_quote") && (
                <>
                  <Field label="Withdrawal reason" htmlFor="withdraw-reason"><input id="withdraw-reason" className={inputClass} value={reason} onChange={(e) => setReason(e.target.value)} /></Field>
                  <Button variant="danger" onClick={() => void action("/withdraw", { reason }, "Quote withdrawn.")} disabled={busy || !reason.trim()}>Withdraw</Button>
                </>
              )}
              {quote.status === "withdrawn" && can("edit_quotes") && <Button variant="secondary" onClick={() => void action("/versions", {}, "New draft version created.")} disabled={busy}>Create new version</Button>}
              {isDraft && can("override_pricing") && quote.blockers.some((b) => b.code === "below_min_margin" || b.code === "manual_price_unconfirmed") && (
                <div className="mt-2 border-t border-slate-200 pt-2">
                  <p className="mb-1 text-xs font-semibold text-slate-700">Privileged price confirmation (audited)</p>
                  <Field label="Reason (≥ 5 characters)" htmlFor="override-reason"><input id="override-reason" className={inputClass} value={reason} onChange={(e) => setReason(e.target.value)} data-testid="override-reason" /></Field>
                  {Array.from(new Set(quote.blockers.filter((b) => b.code === "below_min_margin" || b.code === "manual_price_unconfirmed").map((b) => b.line_no))).map((lineNo) => {
                    const line = version.lines.find((l) => l.line_no === lineNo);
                    return line ? <Button key={line.id} variant="secondary" className="mt-1" onClick={() => void action(`/lines/${line.id}/price-override`, { reason }, `Price confirmed for line ${line.line_no}.`)} disabled={busy || reason.trim().length < 5} testId={`override-${line.line_no}`}>Confirm price on line {line.line_no}</Button> : null;
                  })}
                </div>
              )}
            </div>
          </Card>

          <Card title="Versions & decisions">
            <ul className="divide-y divide-slate-100 text-sm">
              {quote.versions.map((v) => (
                <li key={v.id} className="py-2">
                  <div className="flex items-center justify-between"><span>v{v.version_no} <Badge value={v.status} /></span><span>{money(v.grand_total, quote.currency)}</span></div>
                  {v.approved_at && <p className="text-xs text-slate-500">Approved {dateTime(v.approved_at)}{approvedVersions.length > 0 && <> · <a className="text-brand hover:underline" href={`/api/v1/quotes/${quote.id}/versions/${v.version_no}/pdf`}>PDF</a></>}</p>}
                  {v.decisions.map((d) => <p key={d.id} className="text-xs text-slate-600">{d.decision} {dateTime(d.decided_at)}{d.reason ? ` — ${d.reason}` : ""}</p>)}
                </li>
              ))}
            </ul>
          </Card>
        </div>
      </div>

      {["approved", "sent", "acknowledged", "awaiting_customer", "won", "lost", "expired"].includes(quote.effective_status) && (
        <div className="mt-6"><FollowUpPanel quote={quote} onQuoteChanged={load} /></div>
      )}

      <Card title="Audit history" className="mt-6">
        {!history ? <Loading /> : history.audit.length === 0 ? <p className="text-sm text-slate-600">No events.</p> : (
          <table className="data w-full" data-testid="audit-table">
            <thead><tr><th>When</th><th>Action</th><th>Entity</th><th>Details</th></tr></thead>
            <tbody>
              {history.audit.map((e) => (
                <tr key={e.id}><td>{dateTime(e.created_at)}</td><td>{e.action}</td><td>{e.entity_type}</td><td className="font-mono text-xs">{JSON.stringify(e.summary)}</td></tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </>
  );
}
