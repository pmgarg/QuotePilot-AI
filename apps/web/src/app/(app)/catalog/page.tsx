"use client";

import { useEffect, useState, type FormEvent } from "react";
import { api, describeError } from "@/lib/api";
import { money, dateTime } from "@/lib/format";
import type { Catalog, CatalogVersion, Product } from "@/lib/types";
import { useSession } from "@/components/session";
import { Alert, Badge, Button, Card, Empty, Field, Forbidden, inputClass, Loading, PageHeader } from "@/components/ui";

const FIELDS: { key: string; label: string; required: boolean }[] = [
  { key: "sku", label: "SKU / part number", required: true },
  { key: "description", label: "Description", required: true },
  { key: "list_price", label: "List price", required: true },
  { key: "unit", label: "Unit", required: false },
  { key: "currency", label: "Currency", required: false },
  { key: "cost_price", label: "Cost price", required: false },
  { key: "stock_qty", label: "Stock quantity", required: false },
  { key: "valid_from", label: "Valid from", required: false },
  { key: "valid_to", label: "Valid to", required: false },
];

const DEFAULT_GUESS: Record<string, string> = { sku: "SKU", description: "Description", list_price: "List Price", unit: "Unit", currency: "Currency", cost_price: "Cost Price", stock_qty: "Stock", valid_from: "Valid From", valid_to: "Valid To" };

function ImportWizard({ onDone }: { onDone: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [mapping, setMapping] = useState<Record<string, string>>(DEFAULT_GUESS);
  const [catalogName, setCatalogName] = useState("Main catalog");
  const [result, setResult] = useState<CatalogVersion | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!file) return;
    setBusy(true);
    setError(null);
    setResult(null);
    const form = new FormData();
    form.append("file", file);
    form.append("mapping", JSON.stringify(Object.fromEntries(Object.entries(mapping).filter(([, v]) => v.trim()))));
    form.append("catalog_name", catalogName);
    try {
      const v = await api<CatalogVersion>("/catalogs/import", { method: "POST", form });
      setResult(v);
      onDone();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="Import a price list (CSV or XLSX)">
      <form onSubmit={submit} className="flex flex-col gap-4" aria-busy={busy}>
        <Field label="File (max 10 MiB; first row must be the header)" htmlFor="catalog-file">
          <input id="catalog-file" type="file" accept=".csv,.xlsx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" onChange={(e) => setFile(e.target.files?.[0] ?? null)} className="text-sm" data-testid="catalog-file" />
        </Field>
        <Field label="Catalog name" htmlFor="catalog-name"><input id="catalog-name" className={inputClass} value={catalogName} onChange={(e) => setCatalogName(e.target.value)} maxLength={200} /></Field>
        <fieldset className="grid gap-2 sm:grid-cols-3">
          <legend className="mb-1 text-sm font-medium text-slate-800">Column mapping (header name or 0-based column index)</legend>
          {FIELDS.map((f) => (
            <label key={f.key} className="flex flex-col text-xs text-slate-700">
              {f.label}{f.required ? " *" : ""}
              <input className={inputClass} value={mapping[f.key] ?? ""} onChange={(e) => setMapping({ ...mapping, [f.key]: e.target.value })} required={f.required} data-testid={`map-${f.key}`} />
            </label>
          ))}
        </fieldset>
        {error && <Alert kind="error" testId="import-error">{error}</Alert>}
        <div><Button type="submit" disabled={!file || busy} testId="catalog-import">{busy ? "Importing…" : "Import as new version"}</Button></div>
      </form>
      {result && (
        <div className="mt-4" data-testid="import-report">
          <Alert kind={result.import_report.skipped ? "warning" : "success"} title={`Version ${result.version_no} imported: ${result.import_report.imported} products, ${result.import_report.skipped} rows skipped`}>
            {result.import_report.errors.slice(0, 20).map((e, i) => <p key={i}>Row {e.row}: {e.message}</p>)}
            {result.import_report.warnings.slice(0, 20).map((w, i) => <p key={`w${i}`} className="text-amber-800">Row {w.row}: {w.message}</p>)}
            <p className="mt-1">Activate the version below to use it for matching.</p>
          </Alert>
        </div>
      )}
    </Card>
  );
}

function ProductSearch({ versionId, showCost }: { versionId: string; showCost: boolean }) {
  const [q, setQ] = useState("");
  const [items, setItems] = useState<Product[] | null>(null);
  const [total, setTotal] = useState(0);
  useEffect(() => {
    const t = setTimeout(() => {
      api<{ items: Product[]; total: number }>(`/catalogs/versions/${versionId}/products?q=${encodeURIComponent(q)}&page_size=50`).then((p) => { setItems(p.items); setTotal(p.total); }).catch(() => setItems([]));
    }, 250);
    return () => clearTimeout(t);
  }, [q, versionId]);
  return (
    <Card title="Products in the active version">
      <input aria-label="Search products" className={inputClass} placeholder="Search SKU or description" value={q} onChange={(e) => setQ(e.target.value)} data-testid="product-search" />
      <p className="mt-2 text-xs text-slate-500">{total} matching products</p>
      {!items ? <Loading /> : (
        <div className="mt-2 overflow-x-auto">
          <table className="data w-full">
            <thead><tr><th>SKU</th><th>Description</th><th>Unit</th><th className="text-right">List price</th>{showCost && <th className="text-right">Cost</th>}<th>Stock</th><th>Flags</th></tr></thead>
            <tbody>
              {items.map((p) => (
                <tr key={p.id}>
                  <td className="font-mono text-xs">{p.sku}</td><td>{p.description}</td><td>{p.unit}</td>
                  <td className="text-right">{money(p.list_price, p.currency)} {p.currency}</td>
                  {showCost && <td className="text-right">{p.cost_price ? money(p.cost_price, p.currency) : "—"}</td>}
                  <td>{p.stock_qty ?? <span className="italic text-slate-600">unknown</span>}</td>
                  <td>{p.flags.map((f) => <Badge key={f} value={f} />)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

export default function CatalogPage() {
  const { can } = useSession();
  const [catalogs, setCatalogs] = useState<Catalog[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    try {
      setCatalogs(await api<Catalog[]>("/catalogs"));
    } catch (e) {
      setError(describeError(e));
    }
  }
  useEffect(() => { void load(); }, []);

  async function activate(v: CatalogVersion) {
    setError(null);
    try {
      await api(`/catalogs/versions/${v.id}/activate`, { method: "POST" });
      await load();
    } catch (e) {
      setError(describeError(e));
    }
  }

  const active = catalogs?.flatMap((c) => c.versions).find((v) => v.status === "active");
  return (
    <>
      <PageHeader title="Catalog" subtitle="Immutable, versioned price lists. Only the active version is used for matching and pricing." />
      {error && <div className="mb-4"><Alert kind="error">{error}</Alert></div>}
      <div className="grid gap-6">
        {can("import_catalog") ? <ImportWizard onDone={load} /> : <Card title="Import"><Forbidden action="import catalogs" /></Card>}
        <Card title="Versions">
          {!catalogs ? <Loading /> : catalogs.length === 0 ? <Empty title="No catalog imported yet">Import a CSV/XLSX price list to start matching RFQ lines.</Empty> : (
            <table className="data w-full" data-testid="version-table">
              <thead><tr><th>Catalog</th><th>Version</th><th>Status</th><th>Products</th><th>Imported</th><th></th></tr></thead>
              <tbody>
                {catalogs.flatMap((c) => c.versions.map((v) => (
                  <tr key={v.id}>
                    <td>{c.name}</td><td>v{v.version_no}</td><td><Badge value={v.status} testId={`version-status-${v.version_no}`} /></td><td>{v.row_count}</td><td>{dateTime(v.created_at)}</td>
                    <td>{can("import_catalog") && v.status !== "active" && <Button variant="secondary" onClick={() => void activate(v)} testId={`activate-${v.version_no}`}>Activate</Button>}</td>
                  </tr>
                )))}
              </tbody>
            </table>
          )}
        </Card>
        {active && <ProductSearch versionId={active.id} showCost={can("view_costs")} />}
      </div>
    </>
  );
}
