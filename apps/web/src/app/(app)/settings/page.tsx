"use client";

import { useEffect, useState, type FormEvent } from "react";
import { api, describeError } from "@/lib/api";
import type { Member, Organization, Role } from "@/lib/types";
import { useSession } from "@/components/session";
import { Alert, Badge, Button, Card, Field, Forbidden, inputClass, Loading, PageHeader } from "@/components/ui";

const ROLES: Role[] = ["owner", "admin", "sales", "approver", "readonly"];

function OrgSettings() {
  const { can, session, refresh } = useSession();
  const [org, setOrg] = useState<Organization | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const editable = can("manage_org");

  useEffect(() => {
    api<Organization>("/org").then(setOrg).catch((e) => setError(describeError(e)));
  }, []);

  async function save(e: FormEvent) {
    e.preventDefault();
    if (!org) return;
    setBusy(true);
    setError(null);
    setSaved(false);
    const fd = new FormData(e.currentTarget as HTMLFormElement);
    const payload: Record<string, unknown> = {
      name: fd.get("name"),
      legal_name: fd.get("legal_name") || null,
      address: fd.get("address") || null,
      default_currency: String(fd.get("default_currency")).toUpperCase(),
      default_tax_rate: fd.get("default_tax_rate"),
      rounding_mode: fd.get("rounding_mode"),
      default_validity_days: Number(fd.get("default_validity_days")),
      brand_color: fd.get("brand_color"),
      pdf_footer: fd.get("pdf_footer") || null,
    };
    if (org.min_margin_pct !== undefined) payload.min_margin_pct = fd.get("min_margin_pct");
    if (session?.role === "owner") payload.allow_self_approval = fd.get("allow_self_approval") === "on";
    try {
      const updated = await api<Organization>("/org", { method: "PATCH", json: payload });
      setOrg(updated);
      setSaved(true);
      await refresh();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  if (error && !org) return <Alert kind="error">{error}</Alert>;
  if (!org) return <Loading />;
  return (
    <Card title="Organisation & branding">
      {!editable && <p className="mb-3 text-sm text-slate-600">Read-only view. Owners and admins can edit these settings.</p>}
      <form onSubmit={save} className="grid gap-4 sm:grid-cols-2" aria-busy={busy}>
        <Field label="Display name" htmlFor="name"><input id="name" name="name" className={inputClass} defaultValue={org.name} required maxLength={200} disabled={!editable} /></Field>
        <Field label="Legal name" htmlFor="legal_name"><input id="legal_name" name="legal_name" className={inputClass} defaultValue={org.legal_name ?? ""} disabled={!editable} /></Field>
        <div className="sm:col-span-2"><Field label="Address (printed on PDF)" htmlFor="address"><textarea id="address" name="address" className={inputClass} rows={2} defaultValue={org.address ?? ""} disabled={!editable} /></Field></div>
        <Field label="Default currency (ISO 4217)" htmlFor="default_currency"><input id="default_currency" name="default_currency" className={inputClass} defaultValue={org.default_currency} maxLength={3} minLength={3} disabled={!editable} /></Field>
        <Field label="Default tax rate %" htmlFor="default_tax_rate"><input id="default_tax_rate" name="default_tax_rate" className={inputClass} type="number" step="0.001" min={0} max={100} defaultValue={org.default_tax_rate} disabled={!editable} /></Field>
        <Field label="Rounding" htmlFor="rounding_mode">
          <select id="rounding_mode" name="rounding_mode" className={inputClass} defaultValue={org.rounding_mode} disabled={!editable}>
            <option value="HALF_UP">Half up (commercial)</option>
            <option value="HALF_EVEN">Half even (banker&apos;s)</option>
          </select>
        </Field>
        <Field label="Default validity (days)" htmlFor="default_validity_days"><input id="default_validity_days" name="default_validity_days" className={inputClass} type="number" min={1} max={365} defaultValue={org.default_validity_days} disabled={!editable} /></Field>
        {org.min_margin_pct !== undefined && (
          <Field label="Minimum margin % (below this needs owner/admin override)" htmlFor="min_margin_pct"><input id="min_margin_pct" name="min_margin_pct" className={inputClass} type="number" step="0.01" min={0} max={100} defaultValue={org.min_margin_pct} disabled={!editable} /></Field>
        )}
        <Field label="Brand colour" htmlFor="brand_color"><input id="brand_color" name="brand_color" className={inputClass} type="color" defaultValue={org.brand_color} disabled={!editable} /></Field>
        <div className="sm:col-span-2"><Field label="PDF footer" htmlFor="pdf_footer"><textarea id="pdf_footer" name="pdf_footer" className={inputClass} rows={2} defaultValue={org.pdf_footer ?? ""} maxLength={500} disabled={!editable} /></Field></div>
        <div className="flex items-center gap-2 sm:col-span-2">
          <input id="allow_self_approval" name="allow_self_approval" type="checkbox" defaultChecked={org.allow_self_approval} disabled={session?.role !== "owner"} />
          <label htmlFor="allow_self_approval" className="text-sm text-slate-800">Allow a user to approve a quote they submitted (owner-only setting; audited)</label>
        </div>
        {error && <div className="sm:col-span-2"><Alert kind="error">{error}</Alert></div>}
        {saved && <div className="sm:col-span-2"><Alert kind="success" testId="org-saved">Settings saved.</Alert></div>}
        {editable && <div className="sm:col-span-2"><Button type="submit" disabled={busy} testId="org-save">Save settings</Button></div>}
      </form>
    </Card>
  );
}

function Members() {
  const { can, session } = useSession();
  const [members, setMembers] = useState<Member[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const allowed = can("manage_members");

  async function load() {
    try {
      setMembers(await api<Member[]>("/org/members"));
    } catch (e) {
      setError(describeError(e));
    }
  }
  useEffect(() => {
    if (allowed) void load();
  }, [allowed]);

  async function create(e: FormEvent) {
    e.preventDefault();
    const form = e.currentTarget as HTMLFormElement;
    const fd = new FormData(form);
    setError(null);
    setInfo(null);
    try {
      await api("/org/members", { method: "POST", json: { email: fd.get("email"), full_name: fd.get("full_name"), role: fd.get("role"), password: fd.get("password") } });
      form.reset();
      setInfo("Member created. Share the initial password through a secure channel and ask them to change it.");
      await load();
    } catch (err) {
      setError(describeError(err));
    }
  }

  async function changeRole(m: Member, role: Role) {
    setError(null);
    try {
      await api(`/org/members/${m.id}`, { method: "PATCH", json: { role } });
      await load();
    } catch (err) {
      setError(describeError(err));
    }
  }

  if (!allowed) return <Card title="Members"><Forbidden action="manage members" /></Card>;
  return (
    <Card title="Members">
      {error && <div className="mb-3"><Alert kind="error">{error}</Alert></div>}
      {info && <div className="mb-3"><Alert kind="success">{info}</Alert></div>}
      {!members ? (
        <Loading />
      ) : (
        <table className="data w-full">
          <thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Status</th></tr></thead>
          <tbody>
            {members.map((m) => (
              <tr key={m.id}>
                <td>{m.user.full_name}</td>
                <td>{m.user.email}</td>
                <td>
                  <select aria-label={`Role for ${m.user.email}`} className={inputClass} value={m.role} onChange={(e) => void changeRole(m, e.target.value as Role)} disabled={m.user.id === session?.user.id}>
                    {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
                  </select>
                </td>
                <td><Badge value={m.user.is_active ? "active" : "archived"} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <form onSubmit={create} className="mt-6 grid gap-3 border-t border-slate-200 pt-4 sm:grid-cols-5">
        <input name="full_name" aria-label="Full name" placeholder="Full name" required className={inputClass} />
        <input name="email" aria-label="Email" placeholder="email@company.example" type="email" required className={inputClass} />
        <select name="role" aria-label="Role" className={inputClass} defaultValue="sales">{ROLES.map((r) => <option key={r} value={r}>{r}</option>)}</select>
        <input name="password" aria-label="Initial password" placeholder="Initial password (12+ chars)" type="password" minLength={12} required className={inputClass} autoComplete="new-password" />
        <Button type="submit" testId="member-create">Add member</Button>
      </form>
    </Card>
  );
}

export default function SettingsPage() {
  return (
    <>
      <PageHeader title="Settings" subtitle="Organisation, pricing rules, branding and members." />
      <div className="grid gap-6">
        <OrgSettings />
        <Members />
      </div>
    </>
  );
}
