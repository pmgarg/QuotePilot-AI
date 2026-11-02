"use client";

import { useCallback, useEffect, useState } from "react";
import { api, describeError } from "@/lib/api";
import { date, money } from "@/lib/format";
import type { FxRate, Kpis } from "@/lib/types";
import { useSession } from "@/components/session";
import { Alert, Button, Card, Field, inputClass, Loading, PageHeader } from "@/components/ui";

const LABELS: Record<string, string> = {
  quote_volume: "Quote volume",
  conversion_rate: "Conversion rate",
  pending_value: "Pending value",
  won_value: "Won value",
  response_time_hours: "Response time (h, median)",
  processing_time_hours: "Processing time (h, median)",
  repeat_order_rate: "Repeat-order rate",
};

export default function InsightsPage() {
  const { can } = useSession();
  const [today] = useState(() => new Date().toISOString().slice(0, 10));
  const [from, setFrom] = useState(() => new Date(Date.now() - 90 * 86400000).toISOString().slice(0, 10));
  const [to, setTo] = useState(() => new Date().toISOString().slice(0, 10));
  const [kpis, setKpis] = useState<Kpis | null>(null);
  const [fx, setFx] = useState<FxRate[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [rate, setRate] = useState({ base_currency: "", quote_currency: "", rate: "", as_of: today });
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [k, f] = await Promise.all([api<Kpis>(`/kpis?date_from=${from}&date_to=${to}`), api<FxRate[]>("/fx-rates")]);
      setKpis(k);
      setFx(f);
    } catch (e) {
      setError(describeError(e));
    }
  }, [from, to]);
  useEffect(() => { void load(); }, [load]);

  async function addRate() {
    setBusy(true);
    setError(null);
    try {
      await api("/fx-rates", { method: "POST", json: rate });
      setRate({ ...rate, rate: "" });
      await load();
    } catch (e) {
      setError(describeError(e));
    } finally {
      setBusy(false);
    }
  }

  function fmt(key: string, m: Kpis["metrics"][string], currency: string): string {
    if (m.value === null || m.value === undefined) return "n/a";
    if (key.endsWith("_value")) return money(String(m.value), currency);
    if (key.endsWith("_rate")) return `${m.value}%`;
    return String(m.value);
  }

  return (
    <>
      <PageHeader title="Insights" subtitle="Every figure has a written definition, an explicit date range and a currency rule. Amounts without an FX rate are listed separately, never guessed." />
      {error && <div className="mb-4"><Alert kind="error">{error}</Alert></div>}
      <div className="mb-4 flex flex-wrap items-end gap-3">
        <Field label="From" htmlFor="kpi-from"><input id="kpi-from" type="date" className={inputClass} value={from} onChange={(e) => setFrom(e.target.value)} /></Field>
        <Field label="To" htmlFor="kpi-to"><input id="kpi-to" type="date" className={inputClass} value={to} onChange={(e) => setTo(e.target.value)} /></Field>
      </div>
      {!kpis ? <Loading /> : (
        <div className="grid gap-6">
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4" data-testid="kpi-grid">
            {Object.entries(kpis.metrics).map(([key, m]) => (
              <div key={key} className="rounded-lg border border-slate-200 bg-white p-4" data-testid={`kpi-${key}`}>
                <div className="text-xs uppercase tracking-wide text-slate-500">{LABELS[key] ?? key}</div>
                <div className="mt-1 text-2xl font-semibold text-slate-900">{fmt(key, m, kpis.reporting_currency)}</div>
                <details className="mt-2 text-xs text-slate-600"><summary className="cursor-pointer">Definition</summary>{kpis.definitions[key]}</details>
              </div>
            ))}
          </div>
          <Card title={`Currency rule — reporting currency ${kpis.reporting_currency}`}>
            {kpis.unconverted_count === 0 ? <p className="text-sm text-slate-700">All amounts converted with the FX table below.</p> : (
              <Alert kind="warning" testId="kpi-unconverted">{kpis.unconverted_count} amount(s) have no FX rate and are excluded from the totals: {Object.entries(kpis.unconverted).map(([c, v]) => `${c} ${v}`).join(", ")}. Add a rate below to include them.</Alert>
            )}
            <table className="data mt-3 w-full" data-testid="fx-table">
              <thead><tr><th>Pair</th><th>Rate</th><th>As of</th><th>Source</th></tr></thead>
              <tbody>{(fx ?? []).map((r) => <tr key={r.id}><td>{r.base_currency}/{r.quote_currency}</td><td>{r.rate}</td><td>{date(r.as_of)}</td><td>{r.source}</td></tr>)}</tbody>
            </table>
            {can("manage_connectors") && (
              <div className="mt-3 flex flex-wrap items-end gap-2">
                <Field label="Base" htmlFor="fx-base"><input id="fx-base" className={`${inputClass} w-20`} maxLength={3} value={rate.base_currency} onChange={(e) => setRate({ ...rate, base_currency: e.target.value.toUpperCase() })} /></Field>
                <Field label="Quote" htmlFor="fx-quote"><input id="fx-quote" className={`${inputClass} w-20`} maxLength={3} value={rate.quote_currency} onChange={(e) => setRate({ ...rate, quote_currency: e.target.value.toUpperCase() })} /></Field>
                <Field label="Rate (1 base = ? quote)" htmlFor="fx-rate"><input id="fx-rate" className={`${inputClass} w-32`} inputMode="decimal" value={rate.rate} onChange={(e) => setRate({ ...rate, rate: e.target.value })} /></Field>
                <Field label="As of" htmlFor="fx-asof"><input id="fx-asof" type="date" className={inputClass} value={rate.as_of} onChange={(e) => setRate({ ...rate, as_of: e.target.value })} /></Field>
                <Button onClick={() => void addRate()} disabled={busy || !rate.rate || rate.base_currency.length !== 3 || rate.quote_currency.length !== 3} testId="add-fx">Add rate</Button>
              </div>
            )}
          </Card>
        </div>
      )}
    </>
  );
}
