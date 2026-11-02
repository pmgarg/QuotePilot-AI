"use client";

import { useEffect, useState, type FormEvent } from "react";
import { api, describeError } from "@/lib/api";
import type { Customer } from "@/lib/types";
import { useSession } from "@/components/session";
import { Alert, Button, Card, Empty, Field, inputClass, Loading, PageHeader } from "@/components/ui";

export default function CustomersPage() {
  const { can } = useSession();
  const [customers, setCustomers] = useState<Customer[] | null>(null);
  const [q, setQ] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<Customer | null>(null);

  async function load(query = q) {
    try {
      setCustomers(await api<Customer[]>(`/customers${query ? `?q=${encodeURIComponent(query)}` : ""}`));
    } catch (e) {
      setError(describeError(e));
    }
  }
  useEffect(() => {
    void load("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const form = e.currentTarget as HTMLFormElement;
    const fd = new FormData(form);
    const payload = { name: fd.get("name"), reference: fd.get("reference") || null, email: fd.get("email") || null, default_currency: fd.get("default_currency") ? String(fd.get("default_currency")).toUpperCase() : null, notes: fd.get("notes") || null };
    setError(null);
    try {
      if (editing) await api(`/customers/${editing.id}`, { method: "PATCH", json: payload });
      else await api("/customers", { method: "POST", json: payload });
      form.reset();
      setEditing(null);
      await load();
    } catch (err) {
      setError(describeError(err));
    }
  }

  return (
    <>
      <PageHeader title="Customers" subtitle="Accounts you quote to. Contacts and preferences arrive with Stage 2." />
      <div className="grid gap-6 lg:grid-cols-3">
        <Card className="lg:col-span-2" title="Directory">
          <form className="mb-3 flex gap-2" onSubmit={(e) => { e.preventDefault(); void load(); }}>
            <input aria-label="Search customers" className={inputClass} placeholder="Search by name or reference" value={q} onChange={(e) => setQ(e.target.value)} />
            <Button type="submit" variant="secondary">Search</Button>
          </form>
          {error && <Alert kind="error">{error}</Alert>}
          {!customers ? <Loading /> : customers.length === 0 ? <Empty title="No customers yet">Create one on the right to attach RFQs and quotes.</Empty> : (
            <table className="data w-full" data-testid="customer-table">
              <thead><tr><th>Name</th><th>Reference</th><th>Email</th><th>Currency</th><th></th></tr></thead>
              <tbody>
                {customers.map((c) => (
                  <tr key={c.id}>
                    <td>{c.name}</td><td>{c.reference ?? "—"}</td><td>{c.email ?? "—"}</td><td>{c.default_currency ?? "—"}</td>
                    <td>{can("edit_quotes") && <Button variant="ghost" onClick={() => setEditing(c)}>Edit</Button>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
        {can("edit_quotes") && (
          <Card title={editing ? `Edit ${editing.name}` : "New customer"}>
            <form key={editing?.id ?? "new"} onSubmit={submit} className="flex flex-col gap-3">
              <Field label="Name" htmlFor="c-name"><input id="c-name" name="name" required maxLength={200} className={inputClass} defaultValue={editing?.name ?? ""} data-testid="customer-name" /></Field>
              <Field label="Reference" htmlFor="c-ref"><input id="c-ref" name="reference" maxLength={100} className={inputClass} defaultValue={editing?.reference ?? ""} /></Field>
              <Field label="Email" htmlFor="c-email"><input id="c-email" name="email" type="email" className={inputClass} defaultValue={editing?.email ?? ""} /></Field>
              <Field label="Default currency" htmlFor="c-cur"><input id="c-cur" name="default_currency" maxLength={3} className={inputClass} defaultValue={editing?.default_currency ?? ""} placeholder="e.g. AED" /></Field>
              <Field label="Notes" htmlFor="c-notes"><textarea id="c-notes" name="notes" rows={2} className={inputClass} defaultValue={editing?.notes ?? ""} /></Field>
              <div className="flex gap-2">
                <Button type="submit" testId="customer-save">{editing ? "Save" : "Create"}</Button>
                {editing && <Button variant="secondary" onClick={() => setEditing(null)}>Cancel</Button>}
              </div>
            </form>
          </Card>
        )}
      </div>
    </>
  );
}
