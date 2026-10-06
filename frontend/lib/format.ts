export function pricePrecision(price: number): number {
  if (!Number.isFinite(price) || price <= 0) return 2;
  if (price >= 1000) return 2;
  if (price >= 1) return 4;
  if (price >= 0.01) return 5;
  return 8;
}

export function formatPrice(price: number, precision = pricePrecision(price)): string {
  return price.toLocaleString("en-US", { minimumFractionDigits: precision, maximumFractionDigits: precision });
}

export function formatPct(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "";
  return `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`;
}

export function formatCompact(v: number): string {
  return Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 2 }).format(v);
}

const QUOTES = ["USDT", "USDC", "FDUSD", "BUSD", "BTC", "ETH", "BNB", "TRY", "EUR"];

export function splitSymbol(symbol: string): [string, string] {
  for (const q of QUOTES) {
    if (symbol.endsWith(q) && symbol.length > q.length) return [symbol.slice(0, -q.length), q];
  }
  return [symbol, ""];
}

export function displaySymbol(symbol: string): string {
  const [base, quote] = splitSymbol(symbol);
  return quote ? `${base}/${quote}` : base;
}
