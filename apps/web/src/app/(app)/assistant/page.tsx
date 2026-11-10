"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, describeError } from "@/lib/api";
import type { AssistantMessage, AssistantSession, ToolInfo } from "@/lib/types";
import { Alert, Badge, Button, Card, Loading, PageHeader, inputClass } from "@/components/ui";

export default function AssistantPage() {
  const [session, setSession] = useState<AssistantSession | null>(null);
  const [tools, setTools] = useState<ToolInfo[] | null>(null);
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const started = useRef(false);

  const start = useCallback(async () => {
    try {
      const [s, t] = await Promise.all([api<AssistantSession>("/assistant/sessions", { method: "POST", json: { title: "Assistant" } }), api<ToolInfo[]>("/assistant/tools")]);
      setSession({ ...s, messages: [] });
      setTools(t);
    } catch (e) {
      setError(describeError(e));
    }
  }, []);
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    void start();
  }, [start]);

  async function send() {
    if (!session || !text.trim()) return;
    setBusy(true);
    setError(null);
    const userMsg: AssistantMessage = { id: `local-${Date.now()}`, role: "user", content: text, proposed_actions: [], citations: [], created_at: new Date().toISOString() };
    try {
      const reply = await api<AssistantMessage>(`/assistant/sessions/${session.id}/messages`, { method: "POST", json: { text } });
      setSession({ ...session, messages: [...session.messages, userMsg, reply] });
      setText("");
    } catch (e) {
      setError(describeError(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader title="Assistant" subtitle="Answers come only from your organisation's records through tools your role and plan allow. Anything with side effects waits for your approval in the command center." />
      {error && <div className="mb-4"><Alert kind="error" testId="assistant-error">{error}</Alert></div>}
      <div className="grid gap-6 lg:grid-cols-3">
        <Card title="Conversation" className="lg:col-span-2">
          {!session ? <Loading /> : (
            <>
              <ol className="mb-4 grid gap-3" data-testid="assistant-messages" aria-live="polite">
                {session.messages.map((m) => (
                  <li key={m.id} className={`rounded-md p-3 text-sm ${m.role === "user" ? "bg-slate-100" : "border border-slate-200 bg-white"}`}>
                    <div className="mb-1 text-xs uppercase text-slate-500">{m.role}</div>
                    <p className="whitespace-pre-wrap">{m.content}</p>
                    {m.proposed_actions.map((a, i) => (
                      <div key={i} className="mt-2 rounded border border-slate-200 p-2" data-testid="assistant-action">
                        <div className="flex flex-wrap items-center gap-2"><strong>{a.tool}</strong> <Badge value={a.status} />{a.needs_approval && <Link href="/command-center#actions" className="text-brand underline">approve in command center</Link>}</div>
                        {a.reason && <p className="text-xs text-red-700">{a.reason}</p>}
                        {a.result && <pre className="mt-1 max-h-48 overflow-auto bg-slate-50 p-2 text-xs">{JSON.stringify(a.result, null, 2)}</pre>}
                      </div>
                    ))}
                    {m.citations.length > 0 && <p className="mt-1 text-xs text-slate-600">Sources: {m.citations.map((c, i) => c.type === "quote" ? <Link key={i} className="mr-2 text-brand underline" href={`/quotes/${c.id}`}>{c.number}</Link> : <Link key={i} className="mr-2 text-brand underline" href={`/customers/${c.id}`}>customer</Link>)}</p>}
                  </li>
                ))}
              </ol>
              <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); void send(); }}>
                <label htmlFor="assistant-input" className="sr-only">Message</label>
                <input id="assistant-input" className={inputClass} value={text} onChange={(e) => setText(e.target.value)} placeholder="e.g. show quote Q-2026-00001" maxLength={4000} data-testid="assistant-input" />
                <Button type="submit" disabled={busy || !text.trim()} testId="assistant-send">Send</Button>
              </form>
            </>
          )}
        </Card>
        <Card title="Tools available to you">
          {!tools ? <Loading /> : (
            <ul className="text-sm" data-testid="assistant-tools">
              {tools.map((t) => <li key={t.name} className="border-b border-slate-100 py-1"><strong>{t.name}</strong> <Badge value={t.scope} />{t.requires_approval && <Badge value="needs approval" />}<p className="text-xs text-slate-600">{t.description}</p></li>)}
            </ul>
          )}
        </Card>
      </div>
    </>
  );
}
