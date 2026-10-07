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
  /** Largest fall of the bot's value from a high (the investment counts as the first high), %. */
  max_drawdown_pct: number;
  /** Share of the running time the price spent inside the range, %. */
  time_in_range_pct: number;
  /** [time, value] points; only from the history test. */
  equity?: [number, number][] | null;
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

// ------------------------------------------------------------- planner --
// Mirrors backend/app/grid_planner.py.

export type PlanTimeframe = "1h" | "4h" | "1d";

/** What POST /api/gridbot/plan takes. */
export interface GridPlanRequest {
  symbol: string;
  /** Quote asset, default 1000. */
  investment?: number;
  /** Timeframe the zones and ATR come from (default 4h). */
  timeframe?: PlanTimeframe;
  /** null lets the planner choose. */
  grid_type?: GridType | null;
  fee_rate?: number;
  bnb_discount?: boolean;
  /** Least profit per grid after fees, % (default 0.3). */
  min_net_pct?: number;
}

export interface PlanEdge {
  price: number;
  /** e.g. "H4 demand low (+D1)", "30-day high", "3 ATR below price". */
  basis: string;
  distance_atr: number;
  distance_pct: number;
}

export interface PlanZone {
  kind: string;
  low: number;
  high: number;
  label: string;
  score: number;
  distance_atr: number;
}

export interface GridPlan {
  symbol: string;
  base_asset: string;
  quote_asset: string;
  timeframe: PlanTimeframe;
  data_source: string;
  last_price: number;
  atr: number;
  atr_pct: number;
  lower: PlanEdge;
  upper: PlanEdge;
  grids: number;
  grid_type: GridType;
  investment: number;
  fee_rate: number;
  bnb_discount: boolean;
  /** Fee per fill after the BNB discount. */
  fee_per_fill: number;
  profit_per_grid_min_pct: number;
  profit_per_grid_max_pct: number;
  order_value: number;
  /** 0 = lower price, 100 = upper price. */
  position_pct: number;
  /** Why this range, type and count, one sentence each. */
  reasoning: string[];
  warnings: string[];
  zones: PlanZone[];
  /** Other grid counts worth testing on history. */
  compare_grids: number[];
  filters: SymbolFilters;
}

/** A grid to test on the last `days` days (POST /api/gridbot/backtest). */
export interface GridBacktestRequest {
  symbol: string;
  lower: number;
  upper: number;
  grids: number;
  grid_type: GridType;
  investment: number;
  fee_rate?: number;
  bnb_discount?: boolean;
  days: number;
  compare_grids?: number[];
}

export interface GridBacktestRow {
  grids: number;
  profit_per_grid_min_pct: number;
  profit_per_grid_max_pct: number;
  matched_trades: number;
  grid_profit: number;
  grid_apr_pct: number;
  total_pnl: number;
  total_pnl_pct: number;
  max_drawdown_pct: number;
  /** The grid count that was asked for. */
  chosen: boolean;
}

export interface GridBacktestResult {
  symbol: string;
  quote_asset: string;
  days: number;
  start_time: number;
  end_time: number;
  data_source: string;
  grid_profit: number;
  grid_profit_pct: number;
  matched_trades: number;
  grid_apr_pct: number;
  total_pnl: number;
  total_pnl_pct: number;
  total_apr_pct: number;
  max_drawdown_pct: number;
  time_in_range_pct: number;
  /** Buying the coin with the investment and holding it over the same period. */
  hold_return_pct: number;
  start_price: number;
  last_price: number;
  /** [time, value] of the bot, up to 400 points. */
  equity: [number, number][];
  alternatives: GridBacktestRow[];
  result: GridBotResult;
  notes: string[];
}

export function planGridBot(req: GridPlanRequest, signal?: AbortSignal) {
  return apiRequest<GridPlan>("/api/gridbot/plan", { method: "POST", body: JSON.stringify(req), signal, timeoutMs: 60_000 });
}

/** Replay a grid over past 1m candles (90 days is about 130 Binance calls the first time; cached after). */
export function backtestGrid(req: GridBacktestRequest, signal?: AbortSignal) {
  return apiRequest<GridBacktestResult>("/api/gridbot/backtest", {
    method: "POST",
    body: JSON.stringify(req),
    signal,
    timeoutMs: SLOW,
  });
}

/** The chat agent's grid plan, handed to the Grid bots tab ("plan a grid bot on INJ"). The tab may mount after the
 *  answer arrives, so the plan waits here until it is taken. */
export const GRID_PLAN_EVENT = "ac-grid-plan";
let pendingPlan: GridPlan | null = null;

export function offerGridPlan(plan: GridPlan) {
  pendingPlan = plan;
  if (typeof window !== "undefined") window.dispatchEvent(new CustomEvent(GRID_PLAN_EVENT));
}

export function takeGridPlan(): GridPlan | null {
  const p = pendingPlan;
  pendingPlan = null;
  return p;
}

const PLAN = "rgba(245, 158, 11, 0.6)";
const MAX_PLAN_LINES = 200;

/**
 * A plan being edited, drawn on the chart: an amber box over the range, dashed edges with price tags labelled with
 * what each edge is built on, and dotted lines in between (thinned to MAX_PLAN_LINES). Like the bots' overlays these
 * stay out of the price auto-scale, so a wide range doesn't zoom the chart out.
 */
export function planOverlays(
  draft: Pick<GridBotParams, "lower" | "upper" | "grids" | "grid_type">,
  plan: GridPlan | null,
): Overlay[] {
  const { lower, upper } = draft;
  if (!(lower > 0) || !(upper > lower) || !(draft.grids >= 1)) return [];
  const lines = gridLinesFor(draft);
  const edge = (side: "lower" | "upper") => (plan && plan[side].price === draft[side] ? ` (${plan[side].basis})` : "");
  const out: Overlay[] = [
    {
      type: "box",
      id: "gridplan:range",
      kind: "gridplan",
      label: `Grid plan ${formatPrice(lower)}–${formatPrice(upper)} (${draft.grids} ${draft.grid_type})`,
      price_low: lower,
      price_high: upper,
      color: "rgba(245, 158, 11, 0.06)",
      border_color: "rgba(245, 158, 11, 0.45)",
      autoscale: false,
    },
    {
      type: "horizontal_line", id: "gridplan:upper", kind: "gridplan", label: `Upper${edge("upper")}`, price: upper,
      color: "#f59e0b", line_style: "dashed", line_width: 1, autoscale: false,
    },
    {
      type: "horizontal_line", id: "gridplan:lower", kind: "gridplan", label: `Lower${edge("lower")}`, price: lower,
      color: "#f59e0b", line_style: "dashed", line_width: 1, autoscale: false,
    },
  ];
  const every = Math.max(1, Math.ceil((lines.length - 2) / MAX_PLAN_LINES));
  for (let k = 1; k < lines.length - 1; k += every) {
    out.push({
      type: "horizontal_line", id: `gridplan:line:${k}`, kind: "gridplan", label: "", price: lines[k], color: PLAN,
      line_style: "dotted", line_width: 1, axis_label: false, autoscale: false,
    });
  }
  return out;
}

// --------------------------------------------------- real vs simulated --

export interface RealVsSimRow {
  time: number;
  side: "buy" | "sell";
  price: number;
  /** The fill on the user's Binance spot account, if one matched. */
  real: { time: number; price: number; qty: number; key: string } | null;
  /** The simulator's fill at the same line and about the same time, if any. */
  sim: GridFill | null;
}

/** GET /api/binance/gridbots/{id}/compare: the bot's real fills (from the account import) next to the simulated. */
export interface GridRealCompare {
  bot_id: string;
  name: string;
  symbol: string;
  quote_asset: string;
  real_fills: number;
  sim_fills: number;
  matched: number;
  real_only: number;
  sim_only: number;
  real_sells: number;
  sim_matched_trades: number;
  /** Sells minus buys minus quote fees of the real fills. */
  real_net_quote: number;
  rows: RealVsSimRow[];
  /** Only simulated fills from this time are compared (the simulator keeps the latest 200). */
  since: number;
  notes: string[];
  /** Always false: Binance has no public API for Spot Grid bots. */
  spot_grid_api: boolean;
}

export function fetchGridRealCompare(id: string, signal?: AbortSignal) {
  return apiRequest<GridRealCompare>(`/api/binance/gridbots/${encodeURIComponent(id)}/compare`, { signal, timeoutMs: SLOW });
}
