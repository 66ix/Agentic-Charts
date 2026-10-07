// Order-book heatmap: the API (GET /api/orderbook/heatmap, backend/app/orderbook_heatmap.py) and merging the
// incremental polls into one history for HeatmapPrimitive.

import { apiRequest } from "./api";

/** One time column: [time, mid, first bin, resting notional per bin from the first]. Bin i spans
 *  [i × bin_size, (i + 1) × bin_size). */
export type HeatmapColumn = [number, number, number, number[]];

export interface HeatmapWall {
  side: "bid" | "ask";
  price: number;
  usd: number;
}

export interface HeatmapResponse {
  symbol: string;
  source: "binance" | "synthetic" | "unavailable";
  note: string | null;
  bin_size: number | null;
  cadence: number;
  step: number;
  started_at: number | null;
  collecting: boolean;
  price: number | null;
  columns: HeatmapColumn[];
  walls: HeatmapWall[];
}

export interface HeatmapData {
  symbol: string;
  source: HeatmapResponse["source"];
  note: string | null;
  binSize: number;
  step: number;
  startedAt: number | null;
  columns: HeatmapColumn[];
  walls: HeatmapWall[];
}

export function fetchHeatmap(symbol: string, step: number, since: number | null, signal?: AbortSignal) {
  const q = new URLSearchParams({ symbol, step: String(Math.round(step)) });
  if (since !== null) q.set("since", String(since));
  return apiRequest<HeatmapResponse>(`/api/orderbook/heatmap?${q}`, { signal });
}

/** Column width for a chart interval: two columns per candle, never finer than the backend samples. */
export function heatmapStep(intervalSeconds: number): number {
  return Math.max(10, Math.round(intervalSeconds / 2));
}

/** `prev` with the columns of a poll laid over it: the poll's first column (possibly the same, still filling
 *  column) and everything after replace what was there. A new bin size (the backend started over) drops `prev`. */
export function mergeHeatmap(prev: HeatmapData | null, res: HeatmapResponse, maxColumns = 3000): HeatmapData | null {
  if (!res.bin_size) return prev && prev.symbol === res.symbol ? { ...prev, source: res.source, note: res.note } : null;
  const keep = prev && prev.symbol === res.symbol && prev.binSize === res.bin_size && prev.step === res.step;
  const from = res.columns[0]?.[0] ?? Infinity;
  const columns = [...(keep ? prev.columns.filter((c) => c[0] < from) : []), ...res.columns].slice(-maxColumns);
  return {
    symbol: res.symbol,
    source: res.source,
    note: res.note,
    binSize: res.bin_size,
    step: res.step,
    startedAt: res.started_at,
    columns,
    walls: res.walls,
  };
}

