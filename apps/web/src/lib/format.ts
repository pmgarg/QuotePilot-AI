const ZERO_DP = new Set(["JPY", "KRW", "VND", "CLP", "ISK", "UGX", "XOF", "XAF"]);
const THREE_DP = new Set(["BHD", "KWD", "OMR", "JOD", "IQD", "LYD", "TND"]);

export function minorUnits(currency: string): number {
  if (ZERO_DP.has(currency)) return 0;
  if (THREE_DP.has(currency)) return 3;
  return 2;
}

export function money(value: string | number | null | undefined, currency: string): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) return String(value);
  const d = minorUnits(currency);
  return `${n.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d })}`;
}

export function qty(value: string | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString(undefined, { maximumFractionDigits: 3 }) : value;
}

export function pct(value: string | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return `${Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 })}%`;
}

export function dateTime(value: string | null | undefined): string {
  if (!value) return "—";
  return new Date(value).toLocaleString();
}

export function date(value: string | null | undefined): string {
  if (!value) return "—";
  return value.slice(0, 10);
}
