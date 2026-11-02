"use client";

import { useEffect, useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { api, describeError } from "@/lib/api";
import type { Customer, RFQ } from "@/lib/types";
import { useSession } from "@/components/session";
import { Alert, Button, Card, Field, Forbidden, inputClass, PageHeader } from "@/components/ui";

export default function NewRFQPage() {
  const router = useRouter();
  const { can } = useSession();
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [mode, setMode] = useState<"file" | "text">("file");
  const [file, setFile] = useState<File | null>(null);
  const [text, setText] = useState("");
  const [customerId, setCustomerId] = useState("");
  const [reference, setReference] = useState("");
  const [currency, setCurrency] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<Customer[]>("/customers").then(setCustomers).catch(() => setCustomers([]));
  }, []);

  if (!can("edit_quotes")) return <Forbidden action="create RFQs" />;

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      let rfq: RFQ;
      if (mode === "file") {
        if (!file) throw new Error("Choose a file first");
        const form = new FormData();
        form.append("file", file);
        if (customerId) form.append("customer_id", customerId);
        if (reference) form.append("reference", reference);
        if (currency) form.append("currency", currency.toUpperCase());
        rfq = await api<RFQ>("/rfqs", { method: "POST", form });
      } else {
        rfq = await api<RFQ>("/rfqs", { method: "POST", json: { text, customer_id: customerId || null, reference: reference || null, currency: currency ? currency.toUpperCase() : null } });
      }
      router.push(`/rfqs/${rfq.id}`);
    } catch (err) {
      setError(describeError(err));
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader title="New RFQ" subtitle="Upload a digitally readable PDF, XLSX or CSV, or paste the request text. Scanned PDFs are not supported yet." />
      <Card>
        <form onSubmit={submit} className="flex flex-col gap-4" aria-busy={busy}>
          <div role="radiogroup" aria-label="Input mode" className="flex gap-4 text-sm">
            <label className="flex items-center gap-2"><input type="radio" name="mode" checked={mode === "file"} onChange={() => setMode("file")} data-testid="mode-file" /> Upload file</label>
            <label className="flex items-center gap-2"><input type="radio" name="mode" checked={mode === "text"} onChange={() => setMode("text")} data-testid="mode-text" /> Paste text</label>
          </div>
          {mode === "file" ? (
            <Field label="RFQ document (PDF, XLSX or CSV, max 10 MiB)" htmlFor="rfq-file">
              <input id="rfq-file" type="file" accept=".pdf,.xlsx,.csv,application/pdf,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" onChange={(e) => setFile(e.target.files?.[0] ?? null)} className="text-sm" data-testid="rfq-file" />
            </Field>
          ) : (
            <Field label="Pasted request text" htmlFor="rfq-text" hint="One requested item per line works best, e.g. “10 pcs SKU-1001 Hex bolt M8x40”.">
              <textarea id="rfq-text" rows={10} className={inputClass} value={text} onChange={(e) => setText(e.target.value)} maxLength={100000} data-testid="rfq-text" />
            </Field>
          )}
          <div className="grid gap-3 sm:grid-cols-3">
            <Field label="Customer" htmlFor="rfq-customer">
              <select id="rfq-customer" className={inputClass} value={customerId} onChange={(e) => setCustomerId(e.target.value)} data-testid="rfq-customer">
                <option value="">— none —</option>
                {customers.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
              </select>
            </Field>
            <Field label="Customer reference" htmlFor="rfq-ref"><input id="rfq-ref" className={inputClass} value={reference} onChange={(e) => setReference(e.target.value)} maxLength={100} data-testid="rfq-reference" /></Field>
            <Field label="Currency override" htmlFor="rfq-cur" hint="Defaults to the customer's or organisation currency"><input id="rfq-cur" className={inputClass} value={currency} onChange={(e) => setCurrency(e.target.value)} maxLength={3} placeholder="AED" /></Field>
          </div>
          {error && <Alert kind="error" testId="rfq-error">{error}</Alert>}
          <div><Button type="submit" disabled={busy || (mode === "file" ? !file : !text.trim())} testId="rfq-submit">{busy ? "Parsing…" : "Upload and parse"}</Button></div>
        </form>
      </Card>
    </>
  );
}
