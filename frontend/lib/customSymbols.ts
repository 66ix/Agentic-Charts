import { apiRequest, fetchKlines } from "./api";
import { displaySymbol } from "./format";
import type { Candle, Interval } from "./types";

/**
 * Charts that are not a single Binance pair: a ratio of two pairs ("ETHUSDT/BTCUSDT", like ETH/BTC but for any
 * two coins) and the market-cap indexes ("INDEX:TOTAL", "INDEX:TOTAL2", "INDEX:TOTAL3") the backend builds from
 * the top coins. They have no live stream, so the chart polls them.
 */

export const INDEXES = [
  { symbol: "INDEX:TOTAL", label: "TOTAL", hint: "Top-20 coins' market cap" },
  { symbol: "INDEX:TOTAL2", label: "TOTAL2", hint: "Without BTC" },
  { symbol: "INDEX:TOTAL3", label: "TOTAL3", hint: "Without BTC and ETH" },
] as const;

export const POLL_MS = 15_000;

export function isRatio(symbol: string): boolean {
  return symbol.includes("/");
}

export function isIndex(symbol: string): boolean {
  return symbol.startsWith("INDEX:");
}

export function isCustom(symbol: string): boolean {
  return isRatio(symbol) || isIndex(symbol);
}

export function customLabel(symbol: string): string {
  if (isIndex(symbol)) return symbol.slice(6);
  if (isRatio(symbol)) {
    const [a, b] = symbol.split("/");
    return `${displaySymbol(a)} ÷ ${displaySymbol(b)}`;
  }
  return displaySymbol(symbol);
}

/** a ÷ b bar by bar, on the times both have. */
export function ratioCandles(a: Candle[], b: Candle[]): Candle[] {
  const byTime = new Map(b.map((c) => [c.time, c]));
  const out: Candle[] = [];
  for (const x of a) {
    const y = byTime.get(x.time);
    if (!y || !(y.open > 0) || !(y.close > 0) || !(y.high > 0) || !(y.low > 0)) continue;
    const open = x.open / y.open;
    const close = x.close / y.close;
    out.push({
      time: x.time,
      open,
      close,
      high: Math.max(open, close, x.high / y.high),
      low: Math.min(open, close, x.low / y.low),
      volume: x.volume,
    });
  }
  return out;
}

export async function fetchCustom(
  symbol: string,
  interval: Interval,
  limit: number,
  signal?: AbortSignal,
): Promise<{ candles: Candle[]; source: string; note?: string }> {
  if (isIndex(symbol)) {
    const q = new URLSearchParams({ name: symbol.slice(6), interval, limit: String(limit) });
    const r = await apiRequest<{ candles: Candle[]; source: string; note?: string }>(`/api/index/klines?${q}`, {
      signal,
      timeoutMs: 60_000,
    });
    return r;
  }
  const [a, b] = symbol.split("/");
  const [ra, rb] = await Promise.all([fetchKlines(a, interval, limit, signal), fetchKlines(b, interval, limit, signal)]);
  const source = ra.source === "binance" && rb.source === "binance" ? "binance" : "synthetic";
  return { candles: ratioCandles(ra.candles, rb.candles), source };
}
