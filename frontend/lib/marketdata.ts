// Futures, order-flow and market-cap index data (backend/app/futures_data.py, market_index.py), plus the
// overlay builders that draw order-book walls and estimated liquidation clusters on the price chart.

import { apiRequest } from "./api";
import { formatCompact } from "./format";
import type { BoxOverlay, Candle, HorizontalLineOverlay, Interval } from "./types";

/** "binance" = live, "synthetic" = demo data, "unavailable" = see `note` (e.g. Binance futures blocks the region). */
export type MarketSource = "binance" | "synthetic" | "unavailable";
export type FuturesPeriod = "5m" | "15m" | "30m" | "1h" | "2h" | "4h" | "6h" | "12h" | "1d";

interface Sourced {
  symbol: string;
  source: MarketSource;
  note?: string;
  /** True when Binance futures answered 451/403 (geo-blocked). */
  blocked?: boolean;
}

/** Rates are percent per settlement: 0.01 means 0.01%. */
export interface FundingRow {
  time: number;
  rate: number;
}

export interface FundingData extends Sourced {
  current?: {
    rate: number;
    /** Percent per year at the current rate. */
    annualized: number;
    next_funding_time: number | null;
    mark_price: number | null;
    index_price: number | null;
    interval_hours: number;
  };
  avg_24h?: number | null;
  history?: FundingRow[];
}

export interface OpenInterestRow {
  time: number;
  oi: number;
  oi_usd: number;
}

export interface OpenInterestData extends Sourced {
  period?: FuturesPeriod;
  rows?: OpenInterestRow[];
  oi?: number | null;
  oi_usd?: number | null;
  change_24h_pct?: number | null;
}

export interface LongShortRow {
  time: number;
  long_pct: number;
  short_pct: number;
  ratio: number;
}

export interface LongShortData extends Sourced {
  period?: FuturesPeriod;
  /** Every account's long/short split. */
  accounts?: LongShortRow[];
  /** Position split of the top 20% of traders by margin. */
  top_positions?: LongShortRow[];
  ratio?: number | null;
  long_pct?: number | null;
  top_ratio?: number | null;
  /** Absolute change of the account ratio vs 24h ago. */
  change_24h?: number | null;
}

/** Spot taker volume in the coin's units; `cvd` starts at 0 on the first bar returned. */
export interface CvdRow {
  time: number;
  buy: number;
  sell: number;
  delta: number;
  cvd: number;
}

export interface CvdTotals {
  buy: number;
  sell: number;
  delta: number;
  delta_usd: number;
  buy_pct: number | null;
}

export interface CvdData extends Sourced {
  interval?: Interval;
  rows?: CvdRow[];
  totals?: CvdTotals;
  last_24h?: CvdTotals;
}

export interface Wall {
  price: number;
  qty: number;
  usd: number;
  /** 0..1, relative to the largest wall on either side. */
  strength: number;
  distance_pct: number;
}

export interface WallsData extends Sourced {
  market?: "spot" | "futures";
  range_pct?: number;
  mid?: number | null;
  bids?: Wall[];
  asks?: Wall[];
  median_usd?: number | null;
  /** How far the fetched book reaches from the mid, in percent. */
  covered_pct?: { bids: number; asks: number };
}

/** An estimated band where leveraged positions would be liquidated. Not exchange data. */
export interface LiquidationCluster {
  price_low: number;
  price_high: number;
  /** "long" = longs liquidated (below price), "short" = shorts liquidated (above price). */
  side: "long" | "short";
  /** 0..1, relative to the strongest cluster. */
  weight: number;
  leverage_hint: string;
  distance_pct: number;
}

export interface RecentLiquidation {
  time: number;
  price: number;
  qty: number;
  usd: number;
  side: "long" | "short";
}

export interface LiquidationLevels extends Sourced {
  price?: number | null;
  clusters?: LiquidationCluster[];
  oi_weighted?: boolean;
  lookback_hours?: number;
  recent: RecentLiquidation[];
  recent_source: "binance" | "unavailable";
  recent_note?: string;
}

export type IndexName = "TOTAL" | "TOTAL2" | "TOTAL3";

/** Shaped like /api/klines, plus which coins went in and where supply came from. */
export interface IndexKlines {
  symbol: IndexName;
  interval: Interval;
  source: MarketSource;
  candles: Candle[];
  note: string;
  coins: string[];
  supply_source: "coingecko" | "static";
}

const q = (params: Record<string, string | number>) =>
  new URLSearchParams(Object.entries(params).map(([k, v]) => [k, String(v)])).toString();

export function fetchFunding(symbol: string, limit = 100, signal?: AbortSignal) {
  return apiRequest<FundingData>(`/api/futures/funding?${q({ symbol, limit })}`, { signal });
}

export function fetchOpenInterest(symbol: string, period: FuturesPeriod = "1h", limit = 200, signal?: AbortSignal) {
  return apiRequest<OpenInterestData>(`/api/futures/open-interest?${q({ symbol, period, limit })}`, { signal });
}

export function fetchLongShort(symbol: string, period: FuturesPeriod = "1h", limit = 200, signal?: AbortSignal) {
  return apiRequest<LongShortData>(`/api/futures/long-short?${q({ symbol, period, limit })}`, { signal });
}

export function fetchCvd(symbol: string, interval: Interval, limit = 500, signal?: AbortSignal) {
  return apiRequest<CvdData>(`/api/cvd?${q({ symbol, interval, limit })}`, { signal, timeoutMs: 30_000 });
}

export function fetchWalls(symbol: string, rangePct = 5, market: "spot" | "futures" = "spot", signal?: AbortSignal) {
  return apiRequest<WallsData>(`/api/orderbook/walls?${q({ symbol, range_pct: rangePct, market })}`, { signal });
}

export function fetchLiquidationLevels(symbol: string, signal?: AbortSignal) {
  return apiRequest<LiquidationLevels>(`/api/futures/liquidation-levels?${q({ symbol })}`, { signal });
}

/** TOTAL / TOTAL2 / TOTAL3 candles (a top-20 approximation; see `note`). */
export function fetchIndexKlines(name: IndexName, interval: Interval, limit = 500, signal?: AbortSignal) {
  return apiRequest<IndexKlines>(`/api/index/klines?${q({ name, interval, limit })}`, { signal, timeoutMs: 60_000 });
}

export const usd = (v: number) => `$${formatCompact(v)}`;

const BID = [34, 197, 94] as const; // up
const ASK = [239, 68, 68] as const; // down
const LONG_LIQ = [249, 115, 22] as const; // orange: longs wiped out below price
const SHORT_LIQ = [168, 85, 247] as const; // purple: shorts wiped out above price
const rgba = ([r, g, b]: readonly [number, number, number], a: number) => `rgba(${r}, ${g}, ${b}, ${a})`;

/** Order-book walls as horizontal lines: green bids, red asks, thicker and more opaque the bigger the wall. */
export function wallOverlays(walls: WallsData | null | undefined): HorizontalLineOverlay[] {
  if (!walls || walls.source === "unavailable") return [];
  const line = (w: Wall, side: "bid" | "ask"): HorizontalLineOverlay => ({
    type: "horizontal_line",
    id: `wall-${side}-${w.price}`,
    kind: "wall",
    label: `${side === "bid" ? "Bid" : "Ask"} wall ${usd(w.usd)}${walls.source === "synthetic" ? " (demo)" : ""}`,
    price: w.price,
    color: rgba(side === "bid" ? BID : ASK, 0.45 + 0.5 * w.strength),
    line_style: "solid",
    line_width: Math.max(1, Math.round(1 + w.strength * 3)),
    strength: w.strength,
  });
  return [...(walls.bids ?? []).map((w) => line(w, "bid")), ...(walls.asks ?? []).map((w) => line(w, "ask"))];
}

/**
 * Estimated liquidation clusters as translucent full-width boxes. Weak or far-away clusters are left out so they
 * do not stretch the price scale (boxes and lines are included in autoscale).
 */
export function liquidationOverlays(
  levels: LiquidationLevels | LiquidationCluster[] | null | undefined,
  opts: { minWeight?: number; maxDistancePct?: number } = {},
): BoxOverlay[] {
  const { minWeight = 0.2, maxDistancePct = 10 } = opts;
  const clusters = Array.isArray(levels) ? levels : levels && levels.source !== "unavailable" ? levels.clusters ?? [] : [];
  return clusters
    .filter((c) => c.weight >= minWeight && Math.abs(c.distance_pct) <= maxDistancePct)
    .map((c) => {
      const rgb = c.side === "long" ? LONG_LIQ : SHORT_LIQ;
      return {
        type: "box",
        id: `liq-${c.side}-${c.price_low}`,
        kind: "liquidation_estimate",
        label: `Est. ${c.side} liqs (${c.leverage_hint})`,
        price_low: c.price_low,
        price_high: c.price_high,
        color: rgba(rgb, 0.06 + 0.16 * c.weight),
        border_color: rgba(rgb, 0.25 + 0.35 * c.weight),
        strength: c.weight,
        time_start: null,
        time_end: null,
      } satisfies BoxOverlay;
    });
}
