"use client";

import { useCallback, useEffect, useState } from "react";
import { api, ApiError, describeError } from "@/lib/api";
import { dateTime } from "@/lib/format";
import type { Draft, Lifecycle, Quote } from "@/lib/types";
import { useSession } from "@/components/session";
import { Alert, Badge, Button, Card, Field, inputClass, Loading } from "@/components/ui";

const TRANSITION_LABELS: Record<string, string> = { sent: "Mark as sent", acknowledged: "Record acknowledgement", awaiting_customer: "Awaiting customer", won: "Record as won", lost: "Record as lost" };

export function FollowUpPanel({ quote, onQuoteChanged }: { quote: Quote; onQuoteChanged: () => Promise<void> }) {
  const { can } = useSession();
  const [lc, setLc] = useState<Lifecycle | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [evidence, setEvidence] = useState("");
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<Draft | null>(null);

  const load = useCallback(async () => {
    try {
      setLc(await api<Lifecycle>(`/quotes/${quote.id}/lifecycle`));
    } catch (e) {
      setError(describeError(e));
    }
  }, [quote.id]);
  useEffect(() => { void load(); }, [load, quote.lock_version, quote.status]);

  async function run(fn: () => Promise<unknown>, ok?: string) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await fn();
      if (ok) setNotice(ok);
      await load();
      await onQuoteChanged();
    } catch (e) {
      setError(e instanceof ApiError && e.code === "forbidden" && (e.extra.action === "opt_out") ? "This contact opted out; nothing was sent." : describeError(e));
    } finally {
      setBusy(false);
    }
  }

  if (!lc) return <Card title="Follow-up"><Loading /></Card>;
  const transitions = Object.entries(lc.allowed_transitions);
  const rec = lc.recommendation;
  const eligible = lc.contacts.filter((c) => c.email && !c.opted_out_at);
  return (
    <Card title="Follow-up & lifecycle">
      {error && <div className="mb-2"><Alert kind="error" testId="fu-error">{error}</Alert></div>}
      {notice && <div className="mb-2"><Alert kind="success" testId="fu-notice">{notice}</Alert></div>}
      <p className="text-sm">Lifecycle status: <Badge value={lc.status} testId="lifecycle-status" /></p>

      {can("edit_quotes") && transitions.length > 0 && (
        <div className="mt-3 flex flex-col gap-2">
          <Field label="Evidence (required when recording acknowledgement, won or lost)" htmlFor="evidence" hint="Only record what actually happened, e.g. “customer confirmed by phone on 3 Nov”."><input id="evidence" className={inputClass} value={evidence} onChange={(e) => setEvidence(e.target.value)} data-testid="evidence" /></Field>
          <div className="flex flex-wrap gap-2">
            {transitions.map(([to, needsEvidence]) => (
              <Button key={to} variant={to === "lost" ? "danger" : "secondary"} disabled={busy || (needsEvidence && evidence.trim().length === 0)} testId={`transition-${to}`} onClick={() => void run(() => api(`/quotes/${quote.id}/transition`, { method: "POST", json: { expected_lock_version: quote.lock_version, to_status: to, evidence: evidence || null } }).then(() => setEvidence("")), `Recorded: ${to.replace("_", " ")}.`)}>
                {TRANSITION_LABELS[to] ?? to}
              </Button>
            ))}
          </div>
        </div>
      )}

      <div className="mt-4 rounded-md border border-slate-200 bg-slate-50 p-3 text-sm" data-testid="recommendation">
        <p className="font-semibold">Recommendation: {rec.action.replace(/_/g, " ")} <span className="text-xs font-normal text-slate-500">({rec.priority})</span></p>
        <ul className="mt-1 list-disc pl-5 text-slate-700">{rec.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
        {rec.blocked_by.length > 0 && <p className="mt-1 text-amber-800">Blocked: {rec.blocked_by.join("; ")}</p>}
        {can("edit_quotes") && rec.action === "send_follow_up" && eligible.length > 0 && (
          <Button className="mt-2" variant="secondary" disabled={busy} testId="create-draft" onClick={() => void run(() => api(`/quotes/${quote.id}/drafts`, { method: "POST", json: { contact_id: rec.suggested_contact_id ?? eligible[0].id, template_key: rec.template_key } }), "Draft prepared. Review it below, then send explicitly.")}>Prepare draft</Button>
        )}
        {eligible.length === 0 && <p className="mt-1 text-xs text-slate-600">No eligible contact (add a contact with an e-mail address on the customer, or the contact opted out).</p>}
      </div>

      {lc.drafts.length > 0 && (
        <div className="mt-4">
          <h3 className="text-sm font-semibold">Drafts</h3>
          <ul className="divide-y divide-slate-100">
            {lc.drafts.map((d) => (
              <li key={d.id} className="py-2 text-sm" data-testid={`draft-${d.status}`}>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span><Badge value={d.status} /> <strong>{d.subject}</strong> → {d.to_email ?? "no recipient"}</span>
                  <span className="text-xs text-slate-500">{d.sent_at ? `sent ${dateTime(d.sent_at)} via ${d.provider}` : dateTime(d.created_at)}</span>
                </div>
                {d.last_error && <p className="text-xs text-red-700">{d.last_error}</p>}
                {d.status === "draft" && can("edit_quotes") && (
                  <div className="mt-1 flex gap-2">
                    <Button variant="ghost" onClick={() => setEditing(d)}>Edit</Button>
                    <Button disabled={busy} testId="send-draft" onClick={() => { if (window.confirm(`Send this e-mail to ${d.to_email}? This is a real send action.`)) void run(() => api(`/drafts/${d.id}/send`, { method: "POST" }), "Message sent and recorded."); }}>Send now</Button>
                    <Button variant="ghost" disabled={busy} onClick={() => void run(() => api(`/drafts/${d.id}/cancel`, { method: "POST" }))}>Discard</Button>
                  </div>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {editing && (
        <form className="mt-3 flex flex-col gap-2 rounded-md border border-slate-200 p-3" onSubmit={(e) => { e.preventDefault(); const fd = new FormData(e.currentTarget); void run(() => api(`/drafts/${editing.id}`, { method: "PATCH", json: { subject: fd.get("subject"), body: fd.get("body") } }).then(() => setEditing(null)), "Draft updated."); }}>
          <Field label="Subject" htmlFor="d-subject"><input id="d-subject" name="subject" className={inputClass} defaultValue={editing.subject} maxLength={300} data-testid="draft-subject" /></Field>
          <Field label="Body (plain text; placeholders in braces are allowed)" htmlFor="d-body"><textarea id="d-body" name="body" rows={8} className={inputClass} defaultValue={editing.body} data-testid="draft-body" /></Field>
          <div className="flex gap-2"><Button type="submit" disabled={busy}>Save draft</Button><Button variant="secondary" onClick={() => setEditing(null)}>Cancel</Button></div>
        </form>
      )}

      {lc.schedules.length > 0 && (
        <div className="mt-4">
          <h3 className="text-sm font-semibold">Scheduled follow-ups</h3>
          <ul className="text-sm">
            {lc.schedules.map((s) => (
              <li key={s.id} className="flex flex-wrap items-center justify-between gap-2 py-1" data-testid={`schedule-${s.status}`}>
                <span>#{s.attempt_no} · {dateTime(s.due_at)} · <Badge value={s.status} /> <span className="text-xs text-slate-500">{s.mode === "auto_send" ? "automatic (consented)" : "draft for manual send"}</span></span>
                {s.status === "pending" && can("edit_quotes") && <Button variant="ghost" disabled={busy} testId="cancel-schedule" onClick={() => void run(() => api(`/follow-up/schedules/${s.id}/cancel`, { method: "POST", json: { reason: "cancelled from quote page" } }), "Follow-up cancelled.")}>Cancel</Button>}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="mt-4">
        <h3 className="text-sm font-semibold">Timeline (recorded events only)</h3>
        {lc.timeline.length === 0 ? <p className="text-sm text-slate-600">Nothing recorded yet.</p> : (
          <ol className="mt-1 border-l border-slate-200 pl-3 text-sm" data-testid="timeline">
            {lc.timeline.map((t, i) => (
              <li key={i} className="py-1"><span className="text-xs text-slate-500">{dateTime(t.at)}</span> · <span className="font-medium">{t.title}</span>{t.detail && <span className="text-slate-600"> — {t.detail}</span>} <span className="text-xs text-slate-400">[{t.kind}{t.source ? `/${t.source}` : ""}]</span></li>
            ))}
          </ol>
        )}
      </div>
    </Card>
  );
}
