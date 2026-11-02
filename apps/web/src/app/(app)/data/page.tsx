"use client";

import { useCallback, useEffect, useState } from "react";
import { api, describeError } from "@/lib/api";
import { dateTime } from "@/lib/format";
import type { Connector, DeletionRequest, StockLevel, SyncConflict, SyncRun } from "@/lib/types";
import { useSession } from "@/components/session";
import { Alert, Badge, Button, Card, Empty, Field, inputClass, Loading, PageHeader } from "@/components/ui";

const SCOPES = ["read:products", "read:stock", "read:orders", "read:customers"];

export default function DataPage() {
  const { can } = useSession();
  const [connectors, setConnectors] = useState<Connector[] | null>(null);
  const [conflicts, setConflicts] = useState<SyncConflict[] | null>(null);
  const [runs, setRuns] = useState<SyncRun[] | null>(null);
  const [stock, setStock] = useState<StockLevel[] | null>(null);
  const [deletions, setDeletions] = useState<DeletionRequest[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [newConn, setNewConn] = useState({ kind: "erp_stub", name: "", scopes: ["read:stock"] as string[] });
  const [sync, setSync] = useState({ connector: "", entity: "stock", key: "", records: "[]" });
  const [importEntity, setImportEntity] = useState("customers");
  const [importFile, setImportFile] = useState<File | null>(null);
  const [lastRun, setLastRun] = useState<SyncRun | null>(null);
  const manage = can("manage_connectors");

  const load = useCallback(async () => {
    try {
      const jobs: Promise<unknown>[] = [api<StockLevel[]>("/stock").then(setStock)];
      if (manage) {
        jobs.push(api<Connector[]>("/connectors").then(setConnectors), api<SyncConflict[]>("/sync-conflicts").then(setConflicts));
      }
      if (can("import_data")) jobs.push(api<SyncRun[]>("/data/runs").then(setRuns));
      if (can("manage_retention")) jobs.push(api<DeletionRequest[]>("/retention/requests").then(setDeletions));
      await Promise.all(jobs);
    } catch (e) {
      setError(describeError(e));
    }
  }, [manage, can]);
  useEffect(() => { void load(); }, [load]);

  async function act(fn: () => Promise<unknown>, ok?: string) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await fn();
      if (ok) setNotice(ok);
      await load();
    } catch (e) {
      setError(describeError(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader title="Data & connectors" subtitle="Imports, exports, connector syncs with a conflict queue (master data is never overwritten silently), live stock labels and the retention workflow." />
      {error && <div className="mb-4"><Alert kind="error">{error}</Alert></div>}
      {notice && <div className="mb-4"><Alert kind="success" testId="data-notice">{notice}</Alert></div>}
      <div className="grid gap-6">
        <Card title="Export / import (CSV, formula-safe)">
          <div className="flex flex-wrap gap-2">
            {["customers", "orders", "products"].map((e) => (
              can("export_data") ? <a key={e} className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm hover:bg-slate-50" href={`/api/v1/data/export/${e}`} data-testid={`export-${e}`}>Export {e}</a> : <span key={e} className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-500">Export {e} (owner/admin/approver only)</span>
            ))}
          </div>
          {can("import_data") && (
            <form className="mt-4 flex flex-wrap items-end gap-2" onSubmit={(e) => { e.preventDefault(); if (!importFile) return; const form = new FormData(); form.append("file", importFile); void act(async () => setLastRun(await api<SyncRun>(`/data/import/${importEntity}?idempotency_key=${encodeURIComponent(`${importFile.name}-${importFile.size}-${importFile.lastModified}`)}`, { method: "POST", form })), "Import processed."); }}>
              <Field label="Entity" htmlFor="import-entity"><select id="import-entity" className={inputClass} value={importEntity} onChange={(e) => setImportEntity(e.target.value)}><option value="customers">customers</option><option value="orders">orders</option></select></Field>
              <Field label="CSV file" htmlFor="import-file"><input id="import-file" type="file" accept=".csv,text/csv" onChange={(e) => setImportFile(e.target.files?.[0] ?? null)} data-testid="import-file" /></Field>
              <Button type="submit" disabled={busy || !importFile} testId="import-submit">Import</Button>
              <p className="w-full text-xs text-slate-600">Products are imported as catalog versions on the Catalog page. Valid rows are applied; failed rows can be downloaded, fixed and re-uploaded.</p>
            </form>
          )}
          {lastRun && (
            <Alert kind={lastRun.status === "completed" ? "success" : "warning"} testId="import-result">
              {lastRun.entity}: {lastRun.applied} applied, {lastRun.rejected} rejected of {lastRun.received}. {lastRun.rejected > 0 && <a className="underline" href={`/api/v1/data/runs/${lastRun.id}/failed.csv`}>Download failed rows</a>}
            </Alert>
          )}
          {runs && runs.length > 0 && (
            <table className="data mt-3 w-full" data-testid="run-table">
              <thead><tr><th>When</th><th>Kind</th><th>Entity</th><th>Status</th><th>Received</th><th>Applied</th><th>Conflicts</th><th>Rejected</th><th></th></tr></thead>
              <tbody>{runs.map((r) => <tr key={r.id}><td>{dateTime(r.created_at)}</td><td>{r.kind}</td><td>{r.entity}</td><td><Badge value={r.status} /></td><td>{r.received}</td><td>{r.applied}</td><td>{r.conflicts}</td><td>{r.rejected}</td><td>{r.kind === "csv_import" && r.rejected > 0 && <a className="text-brand underline" href={`/api/v1/data/runs/${r.id}/failed.csv`}>failed rows</a>}</td></tr>)}</tbody>
            </table>
          )}
        </Card>

        <Card title="Stock (only from connected, timestamped sources)">
          {!stock ? <Loading /> : stock.length === 0 ? <Empty title="No active catalog" /> : (
            <table className="data w-full" data-testid="stock-table">
              <thead><tr><th>SKU</th><th>Status</th><th>Quantity</th><th>Observed</th><th>Source</th></tr></thead>
              <tbody>{stock.map((s) => <tr key={s.product_id} data-testid={`stock-${s.sku}`}><td>{s.sku}</td><td><Badge value={s.status} /></td><td>{s.quantity ?? "unknown"}</td><td>{s.observed_at ? `${dateTime(s.observed_at)} (${s.age_hours} h ago)` : "never"}</td><td>{s.source ?? "—"}</td></tr>)}</tbody>
            </table>
          )}
        </Card>

        {manage && (
          <Card title="Connectors">
            {!connectors ? <Loading /> : (
              <>
                {connectors.length === 0 ? <Empty title="No connector yet" /> : (
                  <ul className="divide-y divide-slate-200 text-sm" data-testid="connector-list">
                    {connectors.map((c) => <li key={c.id} className="flex flex-wrap items-center gap-2 py-2"><strong>{c.name}</strong> <Badge value={c.kind} /> <Badge value={c.status} /> scopes: {c.scopes.join(", ")} · last sync {c.last_sync_at ? dateTime(c.last_sync_at) : "never"}{c.last_error && <span className="text-red-700"> · {c.last_error}</span>}{c.status === "active" && <Button variant="ghost" onClick={() => void act(() => api(`/connectors/${c.id}/disable`, { method: "POST" }), "Connector disabled.")} disabled={busy}>Disable</Button>}</li>)}
                  </ul>
                )}
                <div className="mt-4 grid gap-3 md:grid-cols-2">
                  <div className="rounded-md border border-slate-200 p-3">
                    <h3 className="mb-2 text-sm font-semibold">Connect</h3>
                    <Field label="Kind" htmlFor="conn-kind"><select id="conn-kind" className={inputClass} value={newConn.kind} onChange={(e) => setNewConn({ ...newConn, kind: e.target.value })}><option value="erp_stub">ERP (push stub — no network calls)</option><option value="csv">CSV drop</option></select></Field>
                    <Field label="Name" htmlFor="conn-name"><input id="conn-name" className={inputClass} value={newConn.name} onChange={(e) => setNewConn({ ...newConn, name: e.target.value })} /></Field>
                    <fieldset className="mt-2 text-sm"><legend className="font-medium">Declared scopes</legend>{SCOPES.map((s) => <label key={s} className="mr-3 inline-flex items-center gap-1"><input type="checkbox" checked={newConn.scopes.includes(s)} onChange={(e) => setNewConn({ ...newConn, scopes: e.target.checked ? [...newConn.scopes, s] : newConn.scopes.filter((x) => x !== s) })} />{s}</label>)}</fieldset>
                    <Button className="mt-2" onClick={() => void act(() => api("/connectors", { method: "POST", json: newConn }), "Connector created.")} disabled={busy || !newConn.name || newConn.scopes.length === 0} testId="create-connector">Connect</Button>
                  </div>
                  <div className="rounded-md border border-slate-200 p-3">
                    <h3 className="mb-2 text-sm font-semibold">Push a sync (records as JSON array)</h3>
                    <Field label="Connector" htmlFor="sync-conn"><select id="sync-conn" className={inputClass} value={sync.connector} onChange={(e) => setSync({ ...sync, connector: e.target.value })}><option value="">choose…</option>{connectors.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}</select></Field>
                    <Field label="Entity" htmlFor="sync-entity"><select id="sync-entity" className={inputClass} value={sync.entity} onChange={(e) => setSync({ ...sync, entity: e.target.value })}>{["stock", "products", "orders", "customers"].map((e) => <option key={e} value={e}>{e}</option>)}</select></Field>
                    <Field label="Idempotency key" htmlFor="sync-key"><input id="sync-key" className={inputClass} value={sync.key} onChange={(e) => setSync({ ...sync, key: e.target.value })} /></Field>
                    <Field label="Records" htmlFor="sync-records"><textarea id="sync-records" className={`${inputClass} font-mono`} rows={4} value={sync.records} onChange={(e) => setSync({ ...sync, records: e.target.value })} /></Field>
                    <Button className="mt-2" onClick={() => void act(async () => { const records = JSON.parse(sync.records) as unknown[]; setLastRun(await api<SyncRun>(`/connectors/${sync.connector}/sync`, { method: "POST", json: { entity: sync.entity, idempotency_key: sync.key, records } })); }, "Sync processed.")} disabled={busy || !sync.connector || !sync.key} testId="run-sync">Sync</Button>
                  </div>
                </div>
              </>
            )}
          </Card>
        )}

        {manage && (
          <Card title="Sync conflicts (review queue)">
            {!conflicts ? <Loading /> : conflicts.length === 0 ? <Empty title="No open conflicts" /> : (
              <table className="data w-full" data-testid="conflict-table">
                <thead><tr><th>Entity</th><th>Key</th><th>Fields</th><th>Local</th><th>Remote</th><th></th></tr></thead>
                <tbody>
                  {conflicts.map((c) => (
                    <tr key={c.id} data-testid={`conflict-${c.entity_key}`}>
                      <td>{c.entity}</td><td>{c.entity_key}</td><td>{c.fields.join(", ")}</td>
                      <td className="font-mono text-xs">{c.fields.map((f) => `${f}=${String(c.local_value[f] ?? "∅")}`).join(" ")}</td>
                      <td className="font-mono text-xs">{c.fields.map((f) => `${f}=${String(c.remote_value[f] ?? "∅")}`).join(" ")}</td>
                      <td className="whitespace-nowrap">
                        <Button variant="secondary" onClick={() => void act(() => api(`/sync-conflicts/${c.id}/resolve`, { method: "POST", json: { resolution: "keep_local" } }), "Kept local value.")} disabled={busy}>Keep local</Button>{" "}
                        <Button onClick={() => void act(() => api(`/sync-conflicts/${c.id}/resolve`, { method: "POST", json: { resolution: "apply_remote" } }), "Applied remote value.")} disabled={busy || c.fields.includes("new")} testId={`apply-remote-${c.entity_key}`}>Apply remote</Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>
        )}

        {can("manage_retention") && (
          <Card title="Deletion requests (retention)">
            {!deletions ? <Loading /> : deletions.length === 0 ? <Empty title="No deletion requests">Request one from a customer profile.</Empty> : (
              <table className="data w-full" data-testid="deletion-table">
                <thead><tr><th>Requested</th><th>Customer</th><th>Reason</th><th>Status</th><th>Purge after</th><th>Hold</th><th></th></tr></thead>
                <tbody>
                  {deletions.map((d) => (
                    <tr key={d.id} data-testid={`deletion-${d.subject_id.slice(0, 8)}`}>
                      <td>{dateTime(d.created_at)}</td><td className="font-mono text-xs">{d.subject_id.slice(0, 8)}</td><td>{d.reason}</td><td><Badge value={d.status} /></td><td>{d.purge_after ? dateTime(d.purge_after) : "—"}</td><td>{d.legal_hold ? "yes" : "no"}</td>
                      <td className="whitespace-nowrap">
                        {d.status === "requested" && <><Button onClick={() => void act(() => api(`/retention/requests/${d.id}/approve`, { method: "POST", json: {} }), "Deletion approved; the customer is hidden now and purged after the grace period.")} disabled={busy || d.legal_hold} testId={`approve-deletion-${d.subject_id.slice(0, 8)}`}>Approve</Button>{" "}<Button variant="secondary" onClick={() => { const reason = window.prompt("Rejection reason"); if (reason) void act(() => api(`/retention/requests/${d.id}/reject`, { method: "POST", json: { reason } }), "Request rejected."); }} disabled={busy}>Reject</Button>{" "}</>}
                        {d.status !== "executed" && <Button variant="ghost" onClick={() => void act(() => api(`/retention/requests/${d.id}/legal-hold`, { method: "POST", json: { hold: !d.legal_hold } }))} disabled={busy}>{d.legal_hold ? "Lift hold" : "Legal hold"}</Button>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>
        )}
      </div>
    </>
  );
}
