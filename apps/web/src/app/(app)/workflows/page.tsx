"use client";

import { useCallback, useEffect, useState } from "react";
import { api, describeError } from "@/lib/api";
import { dateTime } from "@/lib/format";
import type { Workflow, WorkflowRun } from "@/lib/types";
import { useSession } from "@/components/session";
import { Alert, Badge, Button, Card, Empty, Field, Forbidden, inputClass, Loading, PageHeader } from "@/components/ui";

const EXAMPLE = JSON.stringify(
  {
    name: "Approved quote → proposed order",
    trigger: "quote.approved",
    steps: [
      { id: "read", tool: "get_quote", args: { quote_number: "{{quote_number}}" } },
      { id: "order", tool: "propose_order", args: { quote_number: "{{quote_number}}" }, on_failure: "compensate" },
    ],
  },
  null,
  2,
);

export default function WorkflowsPage() {
  const { can } = useSession();
  const [workflows, setWorkflows] = useState<Workflow[] | null>(null);
  const [runs, setRuns] = useState<WorkflowRun[] | null>(null);
  const [key, setKey] = useState("approved-to-order");
  const [def, setDef] = useState(EXAMPLE);
  const [validation, setValidation] = useState<{ ok: boolean; errors: string[]; warnings: string[] } | null>(null);
  const [context, setContext] = useState('{"quote_number": ""}');
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const allowed = can("manage_workflows");

  const load = useCallback(async () => {
    if (!allowed) return;
    try {
      const [w, r] = await Promise.all([api<Workflow[]>("/workflows"), api<WorkflowRun[]>("/workflow-runs")]);
      setWorkflows(w);
      setRuns(r);
    } catch (e) {
      setError(describeError(e));
    }
  }, [allowed]);
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

  function parsed(text: string): Record<string, unknown> {
    try {
      return JSON.parse(text) as Record<string, unknown>;
    } catch {
      throw new Error("Not valid JSON");
    }
  }

  if (!allowed) return <Forbidden action="manage workflows" />;
  return (
    <>
      <PageHeader title="Workflows" subtitle="Versioned, validated definitions built only from registered tools. Dry runs record intended effects without changing anything; live steps with side effects pause for approval." />
      {error && <div className="mb-4"><Alert kind="error">{error}</Alert></div>}
      {notice && <div className="mb-4"><Alert kind="success" testId="wf-notice">{notice}</Alert></div>}
      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Definition">
          <Field label="Key" htmlFor="wf-key"><input id="wf-key" className={inputClass} value={key} onChange={(e) => setKey(e.target.value)} /></Field>
          <Field label="Definition (JSON)" htmlFor="wf-def"><textarea id="wf-def" rows={14} className={`${inputClass} font-mono text-xs`} value={def} onChange={(e) => setDef(e.target.value)} data-testid="wf-definition" /></Field>
          <div className="mt-2 flex gap-2">
            <Button variant="secondary" onClick={() => void act(async () => setValidation(await api("/workflows/validate", { method: "POST", json: { key, definition: parsed(def) } })), "Validated.")} disabled={busy} testId="wf-validate">Validate</Button>
            <Button onClick={() => void act(() => api("/workflows", { method: "POST", json: { key, definition: parsed(def) } }), "New version saved (draft).")} disabled={busy} testId="wf-save">Save as new version</Button>
          </div>
          {validation && (
            <div className="mt-3" data-testid="wf-validation">
              <Alert kind={validation.ok ? "success" : "error"} title={validation.ok ? "Valid" : "Invalid"}>
                <ul className="list-disc pl-5">{validation.errors.map((e, i) => <li key={i}>{e}</li>)}{validation.warnings.map((w, i) => <li key={`w${i}`}>{w}</li>)}</ul>
              </Alert>
            </div>
          )}
        </Card>
        <Card title="Workflows">
          {!workflows ? <Loading /> : workflows.length === 0 ? <Empty title="No workflows yet" /> : (
            <ul className="divide-y divide-slate-200 text-sm" data-testid="wf-list">
              {workflows.map((w) => {
                const latest = w.versions[w.versions.length - 1];
                return (
                  <li key={w.id} className="py-3">
                    <div className="flex flex-wrap items-center gap-2"><strong>{w.name}</strong> <span className="font-mono text-xs">{w.key}</span> <Badge value={w.enabled ? "enabled" : "disabled"} /> active v{w.active_version_no ?? "—"} · latest v{latest?.version_no}</div>
                    <Field label="Run context (JSON)" htmlFor={`ctx-${w.id}`}><input id={`ctx-${w.id}`} className={`${inputClass} font-mono text-xs`} value={context} onChange={(e) => setContext(e.target.value)} /></Field>
                    <div className="mt-2 flex flex-wrap gap-2">
                      {latest && <Button variant="secondary" onClick={() => void act(() => api(`/workflows/${w.id}/activate?version_no=${latest.version_no}`, { method: "POST", json: {} }), `v${latest.version_no} activated.`)} disabled={busy || !latest.validation.ok} testId={`wf-activate-${w.key}`}>Activate v{latest.version_no}</Button>}
                      <Button variant="secondary" onClick={() => void act(() => api(`/workflows/${w.id}/runs`, { method: "POST", json: { mode: "dry_run", context: parsed(context), version_no: latest?.version_no } }), "Dry run recorded (no side effects).")} disabled={busy} testId={`wf-dry-${w.key}`}>Dry run</Button>
                      {w.enabled && <Button onClick={() => void act(() => api(`/workflows/${w.id}/runs`, { method: "POST", json: { mode: "live", context: parsed(context) } }), "Live run started.")} disabled={busy} testId={`wf-live-${w.key}`}>Run live</Button>}
                      {w.enabled && <Button variant="ghost" onClick={() => void act(() => api(`/workflows/${w.id}/disable`, { method: "POST" }), "Disabled.")} disabled={busy}>Disable</Button>}
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </Card>
        <Card title="Runs" className="lg:col-span-2">
          {!runs ? <Loading /> : runs.length === 0 ? <Empty title="No runs yet" /> : (
            <ul className="divide-y divide-slate-200 text-sm" data-testid="wf-runs">
              {runs.map((r) => (
                <li key={r.id} className="py-3">
                  <div className="flex flex-wrap items-center gap-2"><Badge value={r.status} /> <Badge value={r.mode} /> trigger {r.trigger} · v{r.version_no} · {dateTime(r.created_at)} · steps used {r.budget_used}</div>
                  <ol className="mt-1 pl-5 text-xs">{r.steps.map((s) => <li key={s.index}>{s.step_id} ({s.tool}): <Badge value={s.status} />{s.error && <span className="text-red-700"> {s.error}</span>}</li>)}</ol>
                  {r.failure_explanation && <p className="text-xs text-red-700">{r.failure_explanation}</p>}
                  {r.status === "awaiting_approval" && (
                    <div className="mt-2 flex gap-2">
                      <Button onClick={() => void act(() => api(`/workflow-runs/${r.id}/approve`, { method: "POST", json: {} }), "Step approved; run continued.")} disabled={busy} testId="wf-approve-step">Approve step</Button>
                      <Button variant="secondary" onClick={() => void act(() => api(`/workflow-runs/${r.id}/reject`, { method: "POST", json: { reason: "rejected in the UI" } }), "Step rejected; run stopped.")} disabled={busy}>Reject step</Button>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </>
  );
}
