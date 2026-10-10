// The agent desk (backend/app/agent_desk.py): spot buy calls the agent makes on its own at 1h/4h/1d closes, scored
// as candles come in, traded in its own paper wallet, and learned from (desk_learning.py).

import { apiRequest } from "./api";
import type { PaperWallet } from "./paper";
import type { BoxOverlay, HorizontalLineOverlay, Overlay } from "./types";

/** Window event: the desk changed a call (from the alerts socket). */
export const DESK_CHANGED = "ac:desk-changed";
/** Stored id of the call a chart chip asked the Desk tab to open. */
export const DESK_SELECT_KEY = "ac:desk-select";

export type DeskTimeframe = "15m" | "30m" | "1h" | "4h" | "1d" | "1w";
export const DESK_TIMEFRAMES: DeskTimeframe[] = ["15m", "30m", "1h", "4h", "1d", "1w"];
export type DeskStatus = "waiting" | "expired" | "open" | "tp" | "invalidated" | "timed_out" | "cancelled";

/** Mirrors DeskCall in backend/app/desk_calls.py. */
export interface DeskCall {
  id: string;
  symbol: string;
  interval: DeskTimeframe;
  /** A zone the desk watched but didn't call: scored and learned from, never traded or announced. */
  shadow: boolean;
  /** Watched zones: why it wasn't called ("confidence", "capacity", "running call", "one call per run"). */
  skip_reason?: string;
  created_at: number;
  bar_time: number;
  data_source: string;
  status: DeskStatus;
  price_at_call: number;
  entry: number;
  zone_low: number;
  zone_high: number;
  /** Invalidation: where the idea is wrong. */
  stop: number;
  tp: number;
  tp_label: string;
  tp_level: number;
  /** Share of the way to tp_level the take-profit sits (learned; 1 = at the level). */
  tp_fraction: number;
  rr: number;
  risk_pct: number;
  /** Chance of the take-profit before the invalidation, once bought. */
  confidence: number;
  confidence_basis: string;
  expected_r: number;
  kelly: number;
  size_pct: number;
  notional: number | null;
  paper_group: string | null;
  paper_note: string;
  setup: string;
  kind: string;
  bucket: string;
  features: Record<string, unknown>;
  expires_at: number;
  hold_seconds: number;
  filled_at: number | null;
  fill_price: number | null;
  closed_at: number | null;
  exit_price: number | null;
  /** After fees. */
  r: number | null;
  mfe_r: number | null;
  mae_r: number | null;
  level_hit: boolean | null;
  reach_r: number | null;
  reach_final: boolean;
  open_r: number | null;
  notes: string[];
  updated_at: number;
}

/** Mirrors DeskSettings in backend/app/agent_desk.py. */
export interface DeskSettings {
  enabled: boolean;
  timeframes: DeskTimeframe[];
  follow_watchlist: boolean;
  symbols: string[];
  min_confidence: number;
  /** Calls must expect at least this much R after fees. */
  min_expected_r?: number;
  /** Calls running at once; watched zones are not limited. */
  max_active: number;
  notify_new: boolean;
  notify_fills: boolean;
  notify_results: boolean;
  notify_expired: boolean;
}

export interface CalibrationBin {
  from: number;
  to: number;
  calls: number;
  predicted: number;
  actual: number;
}

export interface DeskSetupRow {
  bucket: string;
  setup: string;
  calls: number;
  tp: number;
  invalidated: number;
  timed_out: number;
  avg_r: number | null;
  total_r: number | null;
  /** The setup's learned edge: hits over what random odds would give (1 = none). */
  lift: number;
}

export interface DeskSummary {
  /** Calls and watched zones, everything the desk learns from. */
  tracked: number;
  watching: number;
  /** Tracked zones whose level outcome is known. */
  learned_from: number;
  level_hits: number;
  calls: number;
  active: number;
  waiting: number;
  open: number;
  closed: number;
  tp: number;
  invalidated: number;
  timed_out: number;
  expired: number;
  avg_r: number | null;
  total_r: number | null;
  calibration: {
    calls: number;
    bins: CalibrationBin[];
    brier: number | null;
    brier_base: number | null;
    hit_rate: number | null;
    mean_confidence: number | null;
    enough: boolean;
  };
  verdict: string;
  setups: DeskSetupRow[];
  /** Setups by how their zones did in the last `recent_days` days, best first. */
  /** `p`: how often random odds alone would do as well (or as badly); `verdict` says "too early" under 12 zones. */
  working_now: { bucket: string; setup: string; zones: number; hits: number; expected: number; lift: number; p?: number; verdict: string }[];
  recent_days: number;
}

/** What the last run of a timeframe found. */
export interface DeskRun {
  at: number;
  bar_time: number;
  coins: number;
  zones: number;
  calls: number;
  watched: number;
  /** The zone with the best expected R, called or not. */
  best: { symbol: string; interval: string; setup: string; expected_r: number } | null;
  seconds: number;
}

/** GET /api/desk */
export interface DeskState {
  settings: DeskSettings;
  symbols: string[];
  /** AGENT_DESK on the server; off = it never calls on its own. */
  enabled_by_server: boolean;
  running: boolean;
  last_run: Record<string, DeskRun>;
  next_run: Record<string, number>;
  summary: DeskSummary;
  channels: { telegram?: boolean; discord?: boolean };
}

export function fetchDesk(signal?: AbortSignal) {
  return apiRequest<DeskState>("/api/desk", { signal });
}

export function fetchDeskCalls(status: "active" | "closed" | "" = "", signal?: AbortSignal, limit = 300, watched = false) {
  const q = new URLSearchParams({ limit: String(limit) });
  if (status) q.set("status", status);
  if (watched) q.set("watched", "true");
  return apiRequest<{ calls: DeskCall[] }>(`/api/desk/calls?${q}`, { signal });
}

export function saveDeskSettings(s: DeskSettings) {
  return apiRequest<DeskState>("/api/desk/settings", { method: "PUT", body: JSON.stringify(s) });
}

/** The active watchlist, which the desk follows when follow_watchlist is on. */
export function syncDeskSymbols(symbols: string[]) {
  return apiRequest<DeskState>("/api/desk/symbols", { method: "PUT", body: JSON.stringify({ symbols }) });
}

/** Look at the latest closed candle now (the first run on a busy watchlist can take a minute). */
export function runDesk(interval: DeskTimeframe) {
  return apiRequest<{ calls: DeskCall[]; status: DeskState }>(`/api/desk/run?interval=${interval}`, { method: "POST", timeoutMs: 300_000 });
}

export function fetchDeskWallet(signal?: AbortSignal) {
  return apiRequest<PaperWallet>("/api/desk/wallet", { signal, timeoutMs: 60_000 });
}

export function resetDeskWallet(startCash: number) {
  return apiRequest<PaperWallet>("/api/desk/wallet/reset", { method: "POST", body: JSON.stringify({ start_cash: startCash }) });
}

export const STATUS_LABEL: Record<DeskStatus, string> = {
  waiting: "Waiting to buy",
  open: "Holding",
  tp: "Take-profit",
  invalidated: "Invalidated",
  timed_out: "Timed out",
  expired: "Expired",
  cancelled: "Cancelled",
};

/** A call on the chart: its buy zone, take-profit and invalidation. */
export function deskOverlays(c: DeskCall): Overlay[] {
  const tf = c.interval.toUpperCase();
  const demo = c.data_source === "synthetic" ? " (demo)" : "";
  const from = c.created_at - (c.created_at % 60);
  const zone: BoxOverlay = {
    type: "box",
    id: `desk-zone-${c.id}`,
    kind: "desk_zone",
    label: `Desk ${tf} buy zone (${Math.round(c.confidence * 100)}% if filled)${demo}`,
    price_low: c.zone_low,
    price_high: c.zone_high,
    color: demo ? "rgba(250, 204, 21, 0.12)" : "rgba(167, 139, 250, 0.16)",
    border_color: demo ? "rgba(250, 204, 21, 0.8)" : "rgba(167, 139, 250, 0.8)",
    time_start: from,
  };
  const line = (id: string, label: string, price: number, color: string, style: "solid" | "dashed"): HorizontalLineOverlay => ({
    type: "horizontal_line",
    id: `desk-${id}-${c.id}`,
    kind: "desk_level",
    label,
    price,
    color,
    line_style: style,
    line_width: 1,
    time_start: from,
  });
  return [
    zone,
    line("tp", `Desk ${tf} take-profit (${c.rr}R)`, c.tp, "#22c55e", "dashed"),
    line("stop", `Desk ${tf} invalidation`, c.stop, "#ef4444", "dashed"),
    ...(c.fill_price ? [line("fill", `Desk ${tf} bought`, c.fill_price, "#a78bfa", "solid")] : []),
  ];
}

/** The chip a running call puts on its coin's chart. Confidence is the chance of the take-profit once bought, so it
 *  says "if filled" and next to it the odds a random walk would give; "uncalibrated" until the desk has enough
 *  finished zones to check its numbers. */
export function deskChip(c: DeskCall, calibrated: boolean): { text: string; tone: "demo" | "up" | "mute"; title: string } {
  const tf = c.interval.toUpperCase();
  const random = Math.round(100 / (1 + Math.max(c.rr, 0.01)));
  const state = c.status === "open" ? "holding" : "waiting to buy";
  return {
    text: `Desk ${tf} · ${state} · ${Math.round(c.confidence * 100)}% if filled (random ${random}%)${calibrated ? "" : " · uncalibrated"}`,
    tone: c.data_source === "synthetic" ? "demo" : c.status === "open" ? "up" : "mute",
    title: `${c.setup}\nBuy ${c.zone_low}–${c.zone_high}, take-profit ${c.tp}, wrong below ${c.stop}. Click to open it in the Desk tab.`,
  };
}
