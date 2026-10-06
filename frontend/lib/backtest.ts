// Setup backtests: types and the API call for backend/app/backtest.py, plus chart overlays for the trades.

import { apiRequest } from "./api";
import type { Interval, Overlay } from "./types";

export type BacktestSetup =
  | "demand_long"
  | "supply_short"
  | "support_long"
  | "resistance_short"
  | "sweep_long"
  | "sweep_short"
  | "kimi_long"
  | "kimi_short"
  | "kimi_any";

export type BacktestTarget = "1.5R" | "2R" | "3R" | "next_level";

/** Setups in the order the form lists them, with plain names. */
export const BACKTEST_SETUPS: { value: BacktestSetup; label: string; hint: string }[] = [
  { value: "demand_long", label: "Long from fresh demand", hint: "Buy the first touch of an untested demand zone" },
  { value: "supply_short", label: "Short from fresh supply", hint: "Sell the first touch of an untested supply zone" },
  { value: "support_long", label: "Long at support", hint: "Buy the first retest of a support cluster" },
  { value: "resistance_short", label: "Short at resistance", hint: "Sell the first retest of a resistance cluster" },
  { value: "sweep_long", label: "Long after a sweep of a low", hint: "Buy the close of a candle that wicked under a swing low" },
  { value: "sweep_short", label: "Short after a sweep of a high", hint: "Sell the close of a candle that wicked over a swing high" },
  { value: "kimi_long", label: "Kimi Cooked long signals", hint: "Kimi's own resolved long signals and R" },
  { value: "kimi_short", label: "Kimi Cooked short signals", hint: "Kimi's own resolved short signals and R" },
  { value: "kimi_any", label: "All Kimi Cooked signals", hint: "Kimi's own resolved signals and R" },
];

export const BACKTEST_TARGETS: { value: BacktestTarget; label: string }[] = [
  { value: "1.5R", label: "1.5R" },
  { value: "2R", label: "2R" },
  { value: "3R", label: "3R" },
  { value: "next_level", label: "Next level" },
];

export interface BacktestRequest {
  symbol: string;
  interval: Interval;
  setup: BacktestSetup;
  /** Closed candles to test on, 100–5000 (default 2000). */
  bars?: number;
  /** Stop beyond the zone or wick by this many ATRs (default 0.25). */
  stop_buffer_atr?: number;
  target?: BacktestTarget;
  /** Exit at the close after this many candles (default 50). */
  max_hold_bars?: number;
  /** Fee per side, % (default 0.1). */
  fee_pct?: number;
}

export interface BacktestTrade {
  direction: "long" | "short";
  entry_time: number;
  entry: number;
  stop: number;
  target: number;
  exit_time: number;
  exit: number;
  /** After fees. */
  r: number;
  result: "win" | "loss" | "timeout";
  bars_held: number;
  basis: string;
}

export interface BacktestStats {
  count: number;
  wins: number;
  losses: number;
  timeouts: number;
  win_rate: number | null;
  avg_r: number | null;
  total_r: number;
  expectancy: number | null;
  profit_factor: number | null;
  max_drawdown_r: number;
  avg_bars_held: number | null;
  avg_win_r: number | null;
  avg_loss_r: number | null;
  best_r: number | null;
  worst_r: number | null;
}

export interface BacktestResult {
  symbol: string;
  interval: Interval;
  setup: BacktestSetup;
  setup_name: string;
  bars: number;
  from_time: number;
  to_time: number;
  data_source: string;
  /** The exit rule used ("kimi" for Kimi setups, which use Kimi's own). */
  target: string;
  trades: BacktestTrade[];
  stats: BacktestStats;
  equity: { time: number; r_cum: number }[];
  /** Plain-English caveats: demo data, small sample, how trades were taken. */
  notes: string[];
  seconds: number;
}

export function runBacktest(req: BacktestRequest, signal?: AbortSignal) {
  return apiRequest<BacktestResult>("/api/backtest", {
    method: "POST",
    body: JSON.stringify(req),
    signal,
    timeoutMs: 120_000, // first runs download history
  });
}

const BLUE = "#3b82f6";
const RED = "#ef4444";
const GREEN = "#22c55e";
const SLATE = "#94a3b8";
const RESULT_COLOR = { win: GREEN, loss: RED, timeout: SLATE } as const;

/** One trade: entry/stop/target segments over its life, shaded risk and reward, and entry/exit markers. */
export function backtestTradeOverlays(t: BacktestTrade, step: number): Overlay[] {
  const long = t.direction === "long";
  // A trade opened and closed on the same candle still gets a visible width.
  const end = Math.max(t.exit_time, t.entry_time + step);
  const seg = (price: number, label: string, color: string, kind: string, dashed = false): Overlay => ({
    type: "trendline", time1: t.entry_time, price1: price, time2: end, price2: price, label, color, kind,
    line_style: dashed ? "dashed" : "solid",
  });
  const out: Overlay[] = [
    {
      type: "box", label: "", kind: "plan_risk", price_low: Math.min(t.entry, t.stop), price_high: Math.max(t.entry, t.stop),
      color: "rgba(239, 68, 68, 0.14)", border_color: null, time_start: t.entry_time, time_end: end,
    },
    {
      type: "box", label: "", kind: "plan_reward", price_low: Math.min(t.entry, t.target), price_high: Math.max(t.entry, t.target),
      color: "rgba(34, 197, 94, 0.12)", border_color: null, time_start: t.entry_time, time_end: end,
    },
    seg(t.entry, `${long ? "Long" : "Short"} entry`, BLUE, "plan_entry"),
    seg(t.stop, "Stop", RED, "plan_stop"),
    seg(t.target, "Target", GREEN, "plan_target", true),
    ...entryExitMarkers(t),
  ];
  return out.map((o, i) => ({ ...o, id: `backtest-${t.entry_time}-${i}` }));
}

function entryExitMarkers(t: BacktestTrade): Overlay[] {
  const long = t.direction === "long";
  return [
    {
      type: "marker", time: t.entry_time, price: t.entry, position: long ? "below" : "above",
      shape: long ? "arrowUp" : "arrowDown", label: long ? "L" : "S", color: BLUE, kind: "backtest_entry",
    },
    {
      type: "marker", time: t.exit_time, price: t.exit, position: long ? "above" : "below", shape: "circle",
      label: `${t.r >= 0 ? "+" : ""}${t.r.toFixed(1)}R`, color: RESULT_COLOR[t.result], kind: "backtest_exit",
    },
  ];
}

/** Every trade as entry and exit markers only (lines for hundreds of trades would bury the candles). */
export function backtestAllOverlays(res: BacktestResult): Overlay[] {
  return res.trades
    .flatMap(entryExitMarkers)
    .sort((a, b) => (a.type === "marker" && b.type === "marker" ? a.time - b.time : 0))
    .map((o, i) => ({ ...o, id: `backtest-all-${i}` }));
}
