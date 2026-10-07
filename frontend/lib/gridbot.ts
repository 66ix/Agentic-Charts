// Binance Spot Grid bot tracker. Mirrors backend/app/gridbot.py. Keep the two in sync.

import { apiRequest } from "./api";
import { formatPrice } from "./format";
import type { BoxOverlay, HorizontalLineOverlay, MarkerOverlay, Overlay } from "./types";

export type GridType = "arithmetic" | "geometric";

/** A Spot Grid bot as set up on Binance. Give `runtime` ("12d 3h 45m", as Binance shows it) or `start_time`. */
export interface GridBotParams {
  symbol: string;
  lower: number;
  upper: number;
  /** Number of grids; there are grids + 1 price lines. 2–500. */
  grids: number;
  grid_type: GridType;
  /** In the quote asset (USDT). */
  investment: number;
  runtime?: string | null;
  /** UNIX seconds. Saved bots always have one (the runtime is turned into a start time when entered). */
  start_time?: number | null;
  /** When the bot was stopped by hand, UNIX seconds. */
  end_time?: number | null;
  /** 0.001 = 0.1% per fill. */
  fee_rate?: number;
  bnb_discount?: boolean;
  trigger_price?: number | null;
  take_profit?: number | null;
  stop_loss?: number | null;
  sell_on_stop?: boolean;
  /** Binance's "Qty per order", if the user has it; otherwise computed from the investment. */
  qty_per_order?: number | null;
}

/** Numbers copied from the bot on Binance, to compare with the simulation. */
export interface BinanceShows {
  matched_trades?: number | null;
  grid_profit?: number | null;
  total_pnl?: number | null;
  /** When they were read off Binance (UNIX s); the server stamps "now" when missing. */
  captured_at?: number | null;
}

export interface GridBot {
  id: string;
  name: string;
  params: GridBotParams & { start_time: number };
  binance: BinanceShows | null;
  created_at: number;
  updated_at: number;
}

export interface GridOrder {
  side: "buy" | "sell";
  price: number;
  qty: number;
  line: number;
}

export interface GridFill {
  /** Open time of the 1m bar it filled in, UNIX s. */
  time: number;
  side: "buy" | "sell";
  price: number;
  qty: number;
  line: number | null;
  /** "initial" = the market buy at creation, "stop" = the sell-all when the bot stopped. */
  kind: "grid" | "initial" | "stop";
  /** Grid profit of the matched trade a sell completed. */
  profit: number | null;
}

export interface GridDay {
  /** UTC midnight, UNIX s. */
  day: number;
  matched: number;
  grid_profit: number;
}

export interface CompareRow {
  binance: number;
  app: number;
  /** app − binance */
  diff: number;
  diff_pct: number | null;
}

export interface GridComparison {
  /** The app's numbers are taken at this time (when Binance's were copied). */
  at: number;
  matched_trades: CompareRow | null;
  grid_profit: CompareRow | null;
  total_pnl: CompareRow | null;
}

export interface SymbolFilters {
  tick_size: number;
  step_size: number;
  min_notional: number;
  source: "binance" | "estimated";
}

export interface GridBotResult {
  symbol: string;
  base_asset: string;
  quote_asset: string;
  /** "synthetic" = demo prices because Binance was unreachable. */
  data_source: string;
  status: "running" | "waiting" | "stopped";
  stop_reason: "take_profit" | "stop_loss" | "ended" | null;
  start_time: number;
  triggered_at: number | null;
  stopped_at: number | null;
  data_end: number;
  bars: number;
  runtime_minutes: number;
  runtime_text: string;
  grid_type: GridType;
  lines: number[];
  /** Index of the line without an order (running bots only): buys below it, sells above. */
  gap_line: number | null;
  qty_per_order: number;
  start_price: number | null;
  last_price: number;
  in_range: boolean;
  /** 0 = lower price, 100 = upper price; outside 0–100 when out of range. */
  position_pct: number;
  investment: number;
  matched_trades: number;
  matched_trades_24h: number;
  grid_profit: number;
  grid_profit_pct: number;
  floating_pnl: number;
  floating_pnl_pct: number;
  total_pnl: number;
  total_pnl_pct: number;
  grid_apr_pct: number;
  total_apr_pct: number;
  profit_per_grid_min_pct: number;
  profit_per_grid_max_pct: number;
  fee_rate: number;
  fees_paid: number;
  base_held: number;
  quote_held: number;
  current_value: number;
  open_orders: GridOrder[];
  /** Up to 200, newest first. */
  recent_fills: GridFill[];
  daily: GridDay[];
  filters: SymbolFilters;
  comparison: GridComparison | null;
  notes: string[];
}

export interface GridBotPatch {
  name?: string;
  /** Partial: only the fields sent change. A new `runtime` (without `start_time`) re-pins the start to now − runtime. */
  params?: Partial<GridBotParams>;
  /** null clears the comparison. */
  binance?: BinanceShows | null;
}

// The first run of a long bot downloads every 1m candle since it started (a month ≈ 44 Binance calls).
const SLOW = 180_000;

// ------------------------------------------------------------------ API --

/** Replay a bot without saving it. */
export function simulateGridBot(params: GridBotParams, binance?: BinanceShows | null, signal?: AbortSignal) {
  return apiRequest<GridBotResult>("/api/gridbot/simulate", {
    method: "POST",
    body: JSON.stringify({ ...params, binance: binance ?? null }),
    signal,
    timeoutMs: SLOW,
  });
}

export function fetchGridBots(signal?: AbortSignal) {
  return apiRequest<{ bots: GridBot[] }>("/api/gridbots", { signal });
}

export function createGridBot(body: { name?: string; params: GridBotParams; binance?: BinanceShows | null }) {
  return apiRequest<{ bot: GridBot; result: GridBotResult }>("/api/gridbots", {
    method: "POST",
    body: JSON.stringify(body),
    timeoutMs: SLOW,
  });
}

export function updateGridBot(id: string, patch: GridBotPatch) {
  return apiRequest<{ bot: GridBot; result: GridBotResult }>(`/api/gridbots/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
    timeoutMs: SLOW,
  });
}

export function deleteGridBot(id: string) {
  return apiRequest<{ ok: boolean }>(`/api/gridbots/${encodeURIComponent(id)}`, { method: "DELETE" });
}

/** The bot's numbers now. The backend recomputes them at most once per 1m bar. */
export function fetchGridBotResult(id: string, signal?: AbortSignal) {
  return apiRequest<GridBotResult>(`/api/gridbots/${encodeURIComponent(id)}/result`, { signal, timeoutMs: SLOW });
}

// ------------------------------------------------------------- helpers --

/** The price lines from the settings alone (not rounded to the tick), for drawing before a result arrives. */
export function gridLinesFor(p: Pick<GridBotParams, "lower" | "upper" | "grids" | "grid_type">): number[] {
  const n = Math.max(1, Math.round(p.grids));
  return Array.from({ length: n + 1 }, (_, i) =>
    p.grid_type === "geometric" ? p.lower * (p.upper / p.lower) ** (i / n) : p.lower + ((p.upper - p.lower) * i) / n,
  );
}

/** "+12.34 USDT" (signed, two decimals; more for tiny amounts). */
export function formatQuote(v: number, quote = "USDT", signed = true): string {
  const digits = Math.abs(v) > 0 && Math.abs(v) < 0.01 ? 6 : 2;
  const s = Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  const sign = v < 0 ? "−" : signed && v > 0 ? "+" : "";
  return `${sign}${s}${quote ? ` ${quote}` : ""}`;
}

const BUY = "rgba(34, 197, 94, 0.55)";
const SELL = "rgba(239, 68, 68, 0.55)";
const IDLE = "rgba(148, 163, 184, 0.45)";
const MARKERS = 60;

/**
 * The bot drawn on the price chart: each grid line as a thin dotted ray from the start (buy side green, sell side
 * red, nothing on the empty line; grey when the bot is waiting or stopped), a faint box over the range labelled
 * "Grid bot 120–180 (30)", and arrows for the latest fills (up below the bar for buys, down above for sells).
 * Pass the bot's result when there is one; without it the lines come from the settings and are drawn grey.
 */
export function gridOverlays(bot: Pick<GridBot, "id" | "params">, result: GridBotResult | null): Overlay[] {
  const p = bot.params;
  const lines = result?.lines.length ? result.lines : gridLinesFor(p);
  const gap = result?.status === "running" ? result.gap_line : null;
  const start = result?.start_time ?? p.start_time ?? null;
  const prefix = `gridbot:${bot.id}`;
  const out: Overlay[] = [];

  const lo = lines[0];
  const hi = lines[lines.length - 1];
  const box: BoxOverlay = {
    type: "box",
    id: `${prefix}:range`,
    kind: "gridbot",
    label: `Grid bot ${formatPrice(lo)}–${formatPrice(hi)} (${lines.length - 1})`,
    price_low: lo,
    price_high: hi,
    color: "rgba(59, 130, 246, 0.05)",
    border_color: "rgba(59, 130, 246, 0.35)",
    time_start: start,
    time_end: result?.stopped_at ?? null,
    // A wide range would zoom the price scale out to fit it; the chart keeps scaling to the candles.
    autoscale: false,
  };
  out.push(box);

  lines.forEach((price, k) => {
    if (gap != null && k === gap) return;
    const line: HorizontalLineOverlay = {
      type: "horizontal_line",
      id: `${prefix}:line:${k}`,
      kind: "gridbot",
      label: "",
      price,
      color: gap == null ? IDLE : k < gap ? BUY : SELL,
      line_style: "dotted",
      line_width: 1,
      time_start: start,
      // Hundreds of price tags would bury the axis; tag only the range ends.
      axis_label: k === 0 || k === lines.length - 1,
      autoscale: false,
    };
    out.push(line);
  });

  for (const f of (result?.recent_fills ?? []).slice(0, MARKERS)) {
    const buy = f.side === "buy";
    const marker: MarkerOverlay = {
      type: "marker",
      id: `${prefix}:fill:${f.time}:${f.line ?? f.kind}:${f.side}`,
      kind: "gridbot",
      label: f.kind === "initial" ? "Bot start" : f.kind === "stop" ? "Bot stop" : "",
      time: f.time,
      price: f.price,
      position: buy ? "below" : "above",
      shape: buy ? "arrowUp" : "arrowDown",
      color: buy ? "#22c55e" : "#ef4444",
    };
    out.push(marker);
  }
  return out;
}
