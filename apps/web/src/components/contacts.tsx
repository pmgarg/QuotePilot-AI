"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, describeError } from "@/lib/api";
import { dateTime } from "@/lib/format";
import type { Contact } from "@/lib/types";
import { useSession } from "@/components/session";
import { Alert, Badge, Button, Field, inputClass, Loading } from "@/components/ui";

export function ContactsPanel({ customerId }: { customerId: string }) {
  const { can } = useSession();
  const [contacts, setContacts] = useState<Contact[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    try {
      setContacts(await api<Contact[]>(`/customers/${customerId}/contacts`));
    } catch (e) {
      setError(describeError(e));
    }
  }, [customerId]);
  useEffect(() => { void load(); }, [load]);

  async function create(e: FormEvent) {
    e.preventDefault();
    const form = e.currentTarget as HTMLFormElement;
    const fd = new FormData(form);
    setError(null);
    try {
      await api(`/customers/${customerId}/contacts`, { method: "POST", json: { full_name: fd.get("full_name"), email: fd.get("email") || null, phone: fd.get("phone") || null, preferred_channel: fd.get("preferred_channel"), timezone: fd.get("timezone") || "UTC", is_primary: fd.get("is_primary") === "on" } });
      form.reset();
      await load();
    } catch (err) {
      setError(describeError(err));
    }
  }

  async function optOut(c: Contact, opted: boolean) {
    setError(null);
    try {
      await api(`/contacts/${c.id}/opt-out`, { method: "POST", json: { opted_out: opted, source: "manual" } });
      await load();
    } catch (err) {
      setError(describeError(err));
    }
  }

  return (
    <div className="mt-4 border-t border-slate-200 pt-3" data-testid="contacts-panel">
      <h3 className="text-sm font-semibold">Contacts & communication preferences</h3>
      {error && <Alert kind="error">{error}</Alert>}
      {!contacts ? <Loading /> : contacts.length === 0 ? <p className="text-sm text-slate-600">No contacts yet. Follow-ups need a contact with an e-mail address.</p> : (
        <ul className="divide-y divide-slate-100 text-sm">
          {contacts.map((c) => (
            <li key={c.id} className="flex flex-wrap items-center justify-between gap-2 py-2" data-testid="contact-row">
              <span>{c.full_name} {c.is_primary && <Badge value="primary" />} · {c.email ?? "no e-mail"} · prefers {c.preferred_channel} · {c.timezone}
                {c.opted_out_at && <span className="ml-2 text-red-700">opted out ({c.opt_out_source}, {dateTime(c.opted_out_at)})</span>}
              </span>
              {can("edit_quotes") && (c.opted_out_at ? (can("manage_org") ? <Button variant="ghost" onClick={() => void optOut(c, false)}>Re-enable (admin)</Button> : null) : <Button variant="ghost" onClick={() => void optOut(c, true)} testId="opt-out">Opt out</Button>)}
            </li>
          ))}
        </ul>
      )}
      {can("edit_quotes") && (
        <form onSubmit={create} className="mt-3 grid gap-2 sm:grid-cols-6">
          <input name="full_name" aria-label="Contact name" placeholder="Full name" required className={inputClass} data-testid="contact-name" />
          <input name="email" aria-label="Contact e-mail" placeholder="email@customer.example" type="email" className={inputClass} data-testid="contact-email" />
          <input name="phone" aria-label="Contact phone" placeholder="Phone" className={inputClass} />
          <select name="preferred_channel" aria-label="Preferred channel" className={inputClass} defaultValue="email"><option value="email">e-mail</option><option value="phone">phone</option><option value="none">no follow-ups</option></select>
          <input name="timezone" aria-label="Time zone" placeholder="Asia/Dubai" className={inputClass} />
          <label className="flex items-center gap-2 text-sm"><input type="checkbox" name="is_primary" /> primary</label>
          <div className="sm:col-span-6"><Button type="submit" variant="secondary" testId="contact-create">Add contact</Button></div>
        </form>
      )}
    </div>
  );
}

export function FollowUpSettings() {
  const { can, session } = useSession();
  const [policy, setPolicy] = useState<import("@/lib/types").FollowUpPolicy | null>(null);
  const [templates, setTemplates] = useState<import("@/lib/types").MessageTemplate[] | null>(null);
  const [integrations, setIntegrations] = useState<import("@/lib/types").Integration[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const load = useCallback(async () => {
    try {
      setPolicy(await api("/follow-up/policy"));
      setTemplates(await api("/follow-up/templates"));
      if (can("manage_org")) setIntegrations(await api("/integrations"));
    } catch (e) {
      setError(describeError(e));
    }
  }, [can]);
  useEffect(() => { void load(); }, [load]);
  const editable = can("manage_org");

  async function savePolicy(e: FormEvent) {
    e.preventDefault();
    const fd = new FormData(e.currentTarget as HTMLFormElement);
    setError(null);
    setNotice(null);
    try {
      await api("/follow-up/policy", { method: "PATCH", json: {
        offsets_days: String(fd.get("offsets_days")).split(",").map((x) => Number(x.trim())).filter((n) => Number.isFinite(n)),
        max_attempts: Number(fd.get("max_attempts")), quiet_hours_start: Number(fd.get("quiet_hours_start")), quiet_hours_end: Number(fd.get("quiet_hours_end")),
        timezone: fd.get("timezone"), skip_weekends: fd.get("skip_weekends") === "on", hourly_send_limit: Number(fd.get("hourly_send_limit")),
        sender_name: fd.get("sender_name") || null, reply_to_email: fd.get("reply_to_email") || null, automation_enabled: fd.get("automation_enabled") === "on",
      } });
      setNotice("Follow-up policy saved.");
      await load();
    } catch (err) {
      setError(describeError(err));
    }
  }

  async function saveTemplate(t: import("@/lib/types").MessageTemplate, e: FormEvent) {
    e.preventDefault();
    const fd = new FormData(e.currentTarget as HTMLFormElement);
    setError(null);
    try {
      await api(`/follow-up/templates/${t.id}`, { method: "PATCH", json: { subject: fd.get("subject"), body: fd.get("body") } });
      setNotice(`Template "${t.name}" saved.`);
      await load();
    } catch (err) {
      setError(describeError(err));
    }
  }

  if (!policy || !templates) return <Loading />;
  return (
    <div className="grid gap-6">
      <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
        <h2 className="mb-3 text-base font-semibold">Follow-up policy</h2>
        {error && <div className="mb-2"><Alert kind="error">{error}</Alert></div>}
        {notice && <div className="mb-2"><Alert kind="success" testId="policy-saved">{notice}</Alert></div>}
        <form onSubmit={savePolicy} className="grid gap-3 sm:grid-cols-3">
          <Field label="Follow-up offsets (days after sending, comma separated)" htmlFor="offsets_days"><input id="offsets_days" name="offsets_days" className={inputClass} defaultValue={policy.offsets_days.join(", ")} disabled={!editable} data-testid="offsets" /></Field>
          <Field label="Max follow-ups per quote" htmlFor="max_attempts"><input id="max_attempts" name="max_attempts" type="number" min={0} max={10} className={inputClass} defaultValue={policy.max_attempts} disabled={!editable} /></Field>
          <Field label="Time zone" htmlFor="tz"><input id="tz" name="timezone" className={inputClass} defaultValue={policy.timezone} disabled={!editable} /></Field>
          <Field label="Quiet hours start (0-23)" htmlFor="qs"><input id="qs" name="quiet_hours_start" type="number" min={0} max={23} className={inputClass} defaultValue={policy.quiet_hours_start} disabled={!editable} /></Field>
          <Field label="Quiet hours end (0-23)" htmlFor="qe"><input id="qe" name="quiet_hours_end" type="number" min={0} max={23} className={inputClass} defaultValue={policy.quiet_hours_end} disabled={!editable} /></Field>
          <Field label="Hourly send limit" htmlFor="rl"><input id="rl" name="hourly_send_limit" type="number" min={1} max={1000} className={inputClass} defaultValue={policy.hourly_send_limit} disabled={!editable} /></Field>
          <Field label="Sender name" htmlFor="sn"><input id="sn" name="sender_name" className={inputClass} defaultValue={policy.sender_name ?? ""} disabled={!editable} /></Field>
          <Field label="Reply-to e-mail" htmlFor="rt"><input id="rt" name="reply_to_email" type="email" className={inputClass} defaultValue={policy.reply_to_email ?? ""} disabled={!editable} /></Field>
          <label className="flex items-center gap-2 text-sm"><input type="checkbox" name="skip_weekends" defaultChecked={policy.skip_weekends} disabled={!editable} /> skip weekends</label>
          <div className="sm:col-span-3 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm">
            <label className="flex items-start gap-2"><input type="checkbox" name="automation_enabled" defaultChecked={policy.automation_enabled} disabled={!editable} className="mt-1" data-testid="automation-consent" />
              <span><strong>Automated sending</strong> — when enabled by an owner/admin, due follow-ups are sent automatically to eligible, non-opted-out contacts within the hourly limit. Default is off: follow-ups are only prepared as drafts for a person to send.
                {policy.automation_consented_at && <span className="block text-xs text-slate-600">Consent recorded {dateTime(policy.automation_consented_at)} (by {session?.user.email === undefined ? "" : "an admin/owner"}).</span>}</span></label>
          </div>
          {editable && <div className="sm:col-span-3"><Button type="submit" testId="policy-save">Save policy</Button></div>}
        </form>
      </section>
      <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
        <h2 className="mb-1 text-base font-semibold">Message templates</h2>
        <p className="mb-3 text-xs text-slate-600">Plain text. Placeholders: {"{contact_name} {customer_name} {quote_number} {grand_total} {currency} {valid_until} {org_name} {sender_name} {days_since_sent} {opt_out_text}"}. The opt-out text is always appended.</p>
        <div className="grid gap-4">
          {templates.map((t) => (
            <form key={t.id} onSubmit={(e) => void saveTemplate(t, e)} className="grid gap-2 rounded-md border border-slate-100 p-3">
              <p className="text-sm font-semibold">{t.name} <span className="font-mono text-xs text-slate-500">({t.key})</span></p>
              <input name="subject" aria-label={`Subject ${t.key}`} className={inputClass} defaultValue={t.subject} disabled={!editable} />
              <textarea name="body" aria-label={`Body ${t.key}`} rows={5} className={inputClass} defaultValue={t.body} disabled={!editable} />
              {editable && <div><Button type="submit" variant="secondary">Save template</Button></div>}
            </form>
          ))}
        </div>
      </section>
      {editable && integrations && (
        <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
          <h2 className="mb-1 text-base font-semibold">Mail integrations</h2>
          <p className="mb-3 text-xs text-slate-600">Default is the built-in simulator (no e-mail leaves the system). SMTP uses server-side settings; Gmail / Microsoft Graph are documented in the runbook and not shipped in this release. Secrets are encrypted at rest and never shown.</p>
          {integrations.length === 0 ? <p className="text-sm text-slate-600">No connection configured — simulator in use.</p> : (
            <ul className="text-sm">{integrations.map((i) => <li key={i.id} className="flex items-center justify-between py-1"><span>{i.provider} <Badge value={i.status === "active" ? "active" : i.status === "revoked" ? "archived" : "failed"} /> {i.account_email ?? ""} {i.last_error && <span className="text-red-700">· {i.last_error}</span>}</span>{i.status === "active" && <Button variant="ghost" onClick={() => void api(`/integrations/${i.id}/revoke`, { method: "POST" }).then(load).catch((e) => setError(describeError(e)))}>Revoke</Button>}</li>)}</ul>
          )}
          <div className="mt-2 flex gap-2">
            <Button variant="secondary" onClick={() => void api("/integrations", { method: "POST", json: { provider: "simulator" } }).then(load).catch((e) => setError(describeError(e)))}>Use simulator</Button>
            <Button variant="secondary" onClick={() => void api("/integrations", { method: "POST", json: { provider: "smtp" } }).then(load).catch((e) => setError(describeError(e)))}>Use SMTP (server-configured)</Button>
          </div>
        </section>
      )}
    </div>
  );
}
