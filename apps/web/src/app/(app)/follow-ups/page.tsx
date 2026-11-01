"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, describeError } from "@/lib/api";
import { dateTime } from "@/lib/format";
import type { Draft, Job, Schedule } from "@/lib/types";
import { useSession } from "@/components/session";
import { Alert, Badge, Button, Card, Empty, inputClass, Loading, PageHeader } from "@/components/ui";

export default function FollowUpsPage() {
  const { can } = useSession();
  const [schedules, setSchedules] = useState<Schedule[] | null>(null);
  const [drafts, setDrafts] = useState<Draft[] | null>(null);
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [status, setStatus] = useState("pending");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [s, d] = await Promise.all([api<Schedule[]>(`/follow-up/schedules?status=${status}`), api<Draft[]>("/drafts?status=draft")]);
      setSchedules(s);
      setDrafts(d);
      if (can("manage_org")) setJobs(await api<Job[]>("/jobs"));
    } catch (e) {
      setError(describeError(e));
    }
  }, [status, can]);
  useEffect(() => { void load(); }, [load]);

  async function act(fn: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await fn();
      await load();
    } catch (e) {
      setError(describeError(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader title="Follow-ups" subtitle="Pending follow-ups, drafts awaiting a human send, and background job health. Nothing is sent without an explicit action or an admin-consented policy." />
      {error && <div className="mb-4"><Alert kind="error">{error}</Alert></div>}
      <div className="grid gap-6">
        <Card title="Scheduled follow-ups">
          <div className="mb-3 flex items-center gap-2">
            <label htmlFor="sched-status" className="text-sm">Status</label>
            <select id="sched-status" className={`${inputClass} w-48`} value={status} onChange={(e) => setStatus(e.target.value)}>
              {["pending", "drafted", "sent", "cancelled", "suppressed", "superseded", "all"].map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
          {!schedules ? <Loading /> : schedules.length === 0 ? <Empty title="No follow-ups in this state" /> : (
            <table className="data w-full" data-testid="schedule-table">
              <thead><tr><th>Quote</th><th>#</th><th>Due</th><th>Mode</th><th>Status</th><th>Reason</th><th></th></tr></thead>
              <tbody>
                {schedules.map((s) => (
                  <tr key={s.id}>
                    <td><Link href={`/quotes/${s.quote_id}`} className="text-brand hover:underline">open quote</Link></td>
                    <td>{s.attempt_no}</td><td>{dateTime(s.due_at)}</td><td>{s.mode === "auto_send" ? "automatic" : "draft only"}</td><td><Badge value={s.status} /></td><td className="text-xs">{s.reason}{s.last_error ? ` · ${s.last_error}` : ""}</td>
                    <td className="whitespace-nowrap">
                      {s.status === "pending" && can("edit_quotes") && (
                        <>
                          <Button variant="ghost" disabled={busy} onClick={() => { const d = window.prompt("New due date/time (ISO, e.g. 2026-12-01T09:00)"); if (d) void act(() => api(`/follow-up/schedules/${s.id}/reschedule`, { method: "POST", json: { due_at: new Date(d).toISOString() } })); }}>Reschedule</Button>
                          <Button variant="ghost" disabled={busy} onClick={() => void act(() => api(`/follow-up/schedules/${s.id}/cancel`, { method: "POST", json: { reason: "cancelled from follow-ups page" } }))}>Cancel</Button>
                        </>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
        <Card title="Drafts awaiting a human send">
          {!drafts ? <Loading /> : drafts.length === 0 ? <p className="text-sm text-slate-600">No drafts waiting.</p> : (
            <ul className="divide-y divide-slate-100 text-sm">
              {drafts.map((d) => (
                <li key={d.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                  <span><Link href={`/quotes/${d.quote_id}`} className="text-brand hover:underline">{d.subject}</Link> → {d.to_email ?? "no recipient"} <span className="text-xs text-slate-500">{dateTime(d.created_at)}</span></span>
                  <span className="text-xs text-slate-500">Review and send from the quote page</span>
                </li>
              ))}
            </ul>
          )}
        </Card>
        {can("manage_org") && (
          <Card title="Background jobs (retries, dead letters)">
            {!jobs ? <Loading /> : jobs.length === 0 ? <p className="text-sm text-slate-600">No jobs recorded.</p> : (
              <table className="data w-full" data-testid="job-table">
                <thead><tr><th>Kind</th><th>Status</th><th>Attempts</th><th>Next run</th><th>Last error</th><th></th></tr></thead>
                <tbody>
                  {jobs.map((j) => (
                    <tr key={j.id}>
                      <td>{j.kind}</td><td><Badge value={j.status === "dead" ? "failed" : j.status} /> {j.status === "dead" && <span className="text-xs text-red-700">dead letter</span>}</td><td>{j.attempts}/{j.max_attempts}</td><td>{dateTime(j.next_run_at)}</td><td className="text-xs text-red-700">{j.last_error}</td>
                      <td>{(j.status === "dead" || j.status === "failed") && <Button variant="secondary" disabled={busy} onClick={() => void act(() => api(`/jobs/${j.id}/retry`, { method: "POST" }))}>Retry</Button>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <div className="mt-3"><Button variant="secondary" disabled={busy} onClick={() => void act(() => api("/jobs/run-once", { method: "POST" }))}>Run worker once (dev)</Button></div>
          </Card>
        )}
      </div>
    </>
  );
}
