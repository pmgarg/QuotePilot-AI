"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { api, describeError } from "@/lib/api";
import { dateTime, money } from "@/lib/format";
import type { CustomerProfile, Proposal } from "@/lib/types";
import { useSession } from "@/components/session";
import { Alert, BackLink, Badge, Button, Card, Field, inputClass, Loading, PageHeader } from "@/components/ui";

export default function CustomerProfilePage() {
  const { id } = useParams<{ id: string }>();
  const { can } = useSession();
  const [profile, setProfile] = useState<CustomerProfile | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setProfile(await api<CustomerProfile>(`/customers/${id}/profile`));
    } catch (e) {
      setError(describeError(e));
    }
  }, [id]);
  useEffect(() => { void load(); }, [load]);

  async function act(fn: () => Promise<string>) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      setNotice(await fn());
      await load();
    } catch (e) {
      setError(describeError(e));
    } finally {
      setBusy(false);
    }
  }

  if (error && !profile) return <Alert kind="error">{error}</Alert>;
  if (!profile) return <Loading />;
  const c = profile.customer;
  return (
    <>
      <BackLink href="/customers">Customers</BackLink>
      <PageHeader title={c.name} subtitle={<span>{c.reference ?? "no reference"} · {c.email ?? "no e-mail"} · {c.default_currency ?? "org currency"}{c.deleted_at && <> · <Badge value="deletion in progress" /></>}</span>} />
      {error && <div className="mb-4"><Alert kind="error">{error}</Alert></div>}
      {notice && <div className="mb-4"><Alert kind="success" testId="profile-notice">{notice}</Alert></div>}
      <div className="grid gap-6 lg:grid-cols-3">
        <Card title="History">
          <dl className="grid grid-cols-2 gap-2 text-sm" data-testid="profile-stats">
            {Object.entries(profile.stats).map(([k, v]) => <div key={k}><dt className="text-slate-500">{k.replace(/_/g, " ")}</dt><dd className="font-semibold">{v === null ? "—" : k.endsWith("_at") ? dateTime(String(v)) : k.endsWith("pct") ? `${v}%` : String(v)}</dd></div>)}
          </dl>
          {!("avg_margin_pct" in profile.stats) && <p className="mt-2 text-xs text-slate-500">Cost and margin figures are visible to owners, admins and approvers only.</p>}
        </Card>
        <Card title="Repeat-order assistant">
          <p className="text-sm text-slate-700">Proposals are computed from this organisation&apos;s confirmed orders only and always need an approver&apos;s decision. Open proposals: {profile.open_proposals}.</p>
          {can("edit_quotes") && !c.deleted_at && <Button className="mt-2" onClick={() => void act(async () => { const p = await api<Proposal | null>(`/customers/${id}/order-proposals`, { method: "POST" }); return p ? `Proposal created with ${p.lines.length} line(s); an approver decides on the Orders page.` : "Nothing to propose: no repeat pattern in this customer's confirmed orders."; })} disabled={busy} testId="generate-proposal">Generate proposal</Button>}
        </Card>
        <Card title="Retention">
          {c.deleted_at ? <Alert kind="warning">Deletion approved on {dateTime(c.deleted_at)}; personal data is purged after the grace period.</Alert> : can("edit_quotes") ? (
            <div className="grid gap-2">
              <Field label="Reason for deletion request" htmlFor="del-reason"><input id="del-reason" className={inputClass} value={reason} onChange={(e) => setReason(e.target.value)} /></Field>
              <Button variant="danger" onClick={() => void act(async () => { await api("/retention/requests", { method: "POST", json: { customer_id: id, reason } }); setReason(""); return "Deletion requested; an owner/admin must approve it."; })} disabled={busy || reason.trim().length < 3} testId="request-deletion">Request deletion</Button>
            </div>
          ) : <p className="text-sm text-slate-600">Deletion requests need the edit permission.</p>}
        </Card>
        <Card title="Contacts" className="lg:col-span-3">
          {profile.contacts.length === 0 ? <p className="text-sm text-slate-600">No contacts.</p> : <ul className="text-sm">{profile.contacts.map((ct) => <li key={ct.id}>{ct.full_name} · {ct.email ?? "—"} {ct.is_primary && <Badge value="primary" />} {ct.opted_out_at && <Badge value="opted out" />}</li>)}</ul>}
        </Card>
        <Card title="Quotes" className="lg:col-span-3">
          <table className="data w-full"><thead><tr><th>Number</th><th>Status</th><th>Total</th><th>Created</th></tr></thead><tbody>{profile.quotes.map((q) => <tr key={q.id}><td><Link className="text-brand underline" href={`/quotes/${q.id}`}>{q.number}</Link></td><td><Badge value={q.effective_status} /></td><td>{money(q.grand_total, q.currency)}</td><td>{dateTime(q.created_at)}</td></tr>)}</tbody></table>
        </Card>
        <Card title="Orders" className="lg:col-span-3">
          <table className="data w-full" data-testid="profile-orders"><thead><tr><th>Number</th><th>Status</th><th>Source</th><th>Total</th><th>Confirmed</th></tr></thead><tbody>{profile.orders.map((o) => <tr key={o.id}><td><Link className="text-brand underline" href={`/orders/${o.id}`}>{o.number}</Link></td><td><Badge value={o.status} /></td><td>{o.source}</td><td>{money(o.grand_total, o.currency)}</td><td>{o.confirmed_at ? dateTime(o.confirmed_at) : "—"}</td></tr>)}</tbody></table>
        </Card>
      </div>
    </>
  );
}
