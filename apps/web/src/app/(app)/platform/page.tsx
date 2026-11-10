"use client";

import { useCallback, useEffect, useState } from "react";
import { api, describeError } from "@/lib/api";
import type { PlanInfo } from "@/lib/types";
import { useSession } from "@/components/session";
import { Alert, Badge, Button, Card, Field, inputClass, Loading, PageHeader } from "@/components/ui";

interface KillSwitchState { engaged: boolean; platform: boolean; reason: string | null; runs_terminated: number | null }

export default function PlatformPage() {
  const { can } = useSession();
  const [plan, setPlan] = useState<PlanInfo | null>(null);
  const [ks, setKs] = useState<KillSwitchState | null>(null);
  const [usage, setUsage] = useState<Record<string, unknown> | null>(null);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [p, k] = await Promise.all([api<PlanInfo>("/platform/plan"), api<KillSwitchState>("/platform/kill-switch")]);
      setPlan(p);
      setKs(k);
      if (can("view_ops")) setUsage(await api<Record<string, unknown>>("/platform/usage"));
    } catch (e) {
      setError(describeError(e));
    }
  }, [can]);
  useEffect(() => { void load(); }, [load]);

  async function act(fn: () => Promise<unknown>, ok: string) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await fn();
      setNotice(ok);
      await load();
    } catch (e) {
      setError(describeError(e));
    } finally {
      setBusy(false);
    }
  }

  async function supportExport() {
    await act(async () => {
      const data = await api<Record<string, unknown>>("/platform/support-export");
      const a = document.createElement("a");
      a.href = `data:application/json;charset=utf-8,${encodeURIComponent(JSON.stringify(data, null, 2))}`;
      a.download = "quotepilot-support-export.json";
      a.click();
    }, "Support export downloaded (configuration and counts only).");
  }

  return (
    <>
      <PageHeader title="Plan & controls" subtitle="Plan limits are enforced by the server. Billing runs in test mode only — no payment is ever taken." />
      {error && <div className="mb-4"><Alert kind="error">{error}</Alert></div>}
      {notice && <div className="mb-4"><Alert kind="success" testId="platform-notice">{notice}</Alert></div>}
      {!plan || !ks ? <Loading /> : (
        <div className="grid gap-6 lg:grid-cols-2">
          <Card title={`Plan: ${plan.plan_name}`}>
            <p className="text-sm">Status <Badge value={plan.status} /> · billing mode <Badge value={plan.billing_mode} /></p>
            <table className="data mt-3 w-full text-sm" data-testid="plan-limits">
              <thead><tr><th>Limit</th><th>Value</th><th>Used</th></tr></thead>
              <tbody>{Object.entries(plan.limits).map(([k, v]) => <tr key={k}><td>{k}</td><td>{Array.isArray(v) ? v.join(", ") : String(v)}</td><td>{plan.usage[k] ?? "—"}</td></tr>)}</tbody>
            </table>
            {can("manage_billing") && (
              <div className="mt-3 flex flex-wrap gap-2">
                {Object.entries(plan.plans).map(([code, p]) => <Button key={code} variant={code === plan.plan_code ? "secondary" : "primary"} disabled={busy || code === plan.plan_code} onClick={() => void act(() => api("/platform/plan", { method: "POST", json: { plan_code: code } }), `Plan changed to ${p.name} (test mode).`)} testId={`plan-${code}`}>{p.name} · {p.price_per_month}/mo</Button>)}
              </div>
            )}
          </Card>
          <Card title="Kill switch">
            {ks.engaged ? <Alert kind="error" title="Engaged" testId="ks-engaged">{ks.platform ? "Platform-wide switch set by the operator." : ks.reason}{ks.runs_terminated !== null && ` · ${ks.runs_terminated} run(s) terminated`}</Alert> : <p className="text-sm text-slate-700">Not engaged. Engaging it stops the assistant and every workflow step for this organisation immediately.</p>}
            {can("kill_switch") && !ks.platform && (
              ks.engaged ? <Button className="mt-3" variant="secondary" onClick={() => void act(() => api("/platform/kill-switch", { method: "DELETE" }), "Kill switch released.")} disabled={busy} testId="ks-release">Release</Button> : (
                <div className="mt-3 grid gap-2">
                  <Field label="Reason" htmlFor="ks-reason"><input id="ks-reason" className={inputClass} value={reason} onChange={(e) => setReason(e.target.value)} /></Field>
                  <Button variant="danger" onClick={() => void act(() => api("/platform/kill-switch", { method: "POST", json: { reason } }), "Kill switch engaged.")} disabled={busy || reason.trim().length < 3} testId="ks-engage">Engage kill switch</Button>
                </div>
              )
            )}
          </Card>
          {usage && (
            <Card title="Usage (30 days)" className="lg:col-span-2">
              <pre className="max-h-64 overflow-auto bg-slate-50 p-2 text-xs" data-testid="usage">{JSON.stringify(usage, null, 2)}</pre>
              <Button className="mt-2" variant="secondary" onClick={() => void supportExport()} disabled={busy} testId="support-export">Download support export</Button>
            </Card>
          )}
        </div>
      )}
    </>
  );
}
