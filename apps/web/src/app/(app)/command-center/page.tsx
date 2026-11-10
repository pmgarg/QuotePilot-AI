"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, describeError } from "@/lib/api";
import { dateTime } from "@/lib/format";
import type { CommandCenter, Invocation } from "@/lib/types";
import { Alert, Badge, Button, Card, Empty, inputClass, Loading, PageHeader } from "@/components/ui";

const QUEUES: { key: string; label: string; href: string }[] = [
  { key: "rfqs_to_review", label: "RFQs to review", href: "/rfqs" },
  { key: "quotes_draft", label: "Draft quotes", href: "/quotes?status=draft" },
  { key: "quotes_pending_approval", label: "Quotes awaiting approval", href: "/quotes?status=pending_approval" },
  { key: "drafts_awaiting_send", label: "Follow-up drafts to send", href: "/follow-ups" },
  { key: "follow_ups_due_7d", label: "Follow-ups due in 7 days", href: "/follow-ups" },
  { key: "orders_proposed", label: "Orders to confirm", href: "/orders" },
  { key: "actions_awaiting_approval", label: "Assistant actions to approve", href: "/command-center#actions" },
  { key: "workflow_runs_awaiting_approval", label: "Workflow steps to approve", href: "/workflows" },
  { key: "sync_conflicts_open", label: "Sync conflicts", href: "/data" },
  { key: "deletion_requests_open", label: "Deletion requests", href: "/data" },
];

export default function CommandCenterPage() {
  const [cc, setCc] = useState<CommandCenter | null>(null);
  const [pending, setPending] = useState<Invocation[] | null>(null);
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [c, p] = await Promise.all([api<CommandCenter>("/command-center"), api<Invocation[]>("/actions?status=proposed")]);
      setCc(c);
      setPending(p.filter((i) => i.requires_approval && !i.run_id));
    } catch (e) {
      setError(describeError(e));
    }
  }, []);
  useEffect(() => { void load(); }, [load]);

  async function decide(id: string, approve: boolean) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const out = await api<Invocation & { download?: { filename: string; content_b64: string } }>(`/actions/${id}/${approve ? "approve" : "reject"}`, { method: "POST", json: { reason: reasons[id] || null } });
      if (out.download) {
        const a = document.createElement("a");
        a.href = `data:text/csv;base64,${out.download.content_b64}`;
        a.download = out.download.filename;
        a.click();
      }
      setNotice(approve ? `“${out.tool}” ${out.status}.` : `“${out.tool}” rejected.`);
      await load();
    } catch (e) {
      setError(describeError(e));
      await load();
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader title="Command center" subtitle="Everything waiting for a human decision, in one place. Counts follow your role." />
      {error && <div className="mb-4"><Alert kind="error">{error}</Alert></div>}
      {notice && <div className="mb-4"><Alert kind="success" testId="cc-notice">{notice}</Alert></div>}
      {!cc ? <Loading /> : (
        <div className="grid gap-6">
          {cc.kill_switch.engaged && <Alert kind="error" title="Kill switch engaged" testId="cc-kill-switch">Automation and the assistant are stopped{cc.kill_switch.reason ? `: ${cc.kill_switch.reason}` : ""}.</Alert>}
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5" data-testid="cc-queues">
            {QUEUES.filter((q) => q.key in cc.queues).map((q) => (
              <Link key={q.key} href={q.href} className="rounded-lg border border-slate-200 bg-white p-4 hover:border-brand" data-testid={`cc-${q.key}`}>
                <div className="text-xs uppercase tracking-wide text-slate-500">{q.label}</div>
                <div className="mt-1 text-2xl font-semibold text-slate-900">{cc.queues[q.key]}</div>
              </Link>
            ))}
          </div>
          <Card title="Actions awaiting your approval">
            <span id="actions" />
            {!pending ? <Loading /> : pending.length === 0 ? <Empty title="Nothing to approve">Side-effect actions proposed by the assistant appear here and never run without approval.</Empty> : (
              <ul className="divide-y divide-slate-200" data-testid="pending-actions">
                {pending.map((i) => (
                  <li key={i.id} className="py-3 text-sm">
                    <div className="flex flex-wrap items-center gap-2"><Badge value={i.scope} /> <strong>{i.tool}</strong> <span className="font-mono text-xs text-slate-600">{JSON.stringify(i.args)}</span> <span className="text-slate-500">{dateTime(i.created_at)}</span></div>
                    <div className="mt-2 flex flex-wrap items-center gap-2">
                      <input aria-label={`Reason for ${i.tool}`} className={`${inputClass} w-64`} placeholder="reason (optional)" value={reasons[i.id] ?? ""} onChange={(e) => setReasons({ ...reasons, [i.id]: e.target.value })} />
                      <Button onClick={() => void decide(i.id, true)} disabled={busy} testId={`approve-action-${i.tool}`}>Approve &amp; run</Button>
                      <Button variant="secondary" onClick={() => void decide(i.id, false)} disabled={busy}>Reject</Button>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      )}
    </>
  );
}
