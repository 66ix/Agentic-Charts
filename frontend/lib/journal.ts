// Trade journal: types and API calls for backend/app/journal.py, plus chart overlays for a logged trade.
// Every change dispatches JOURNAL_EVENT on window so open panels refresh.

import { apiRequest } from "./api";
import { formatPrice } from "./format";
import type { Interval, Overlay, TradePlan } from "./types";

export const JOURNAL_EVENT = "ac-journal-changed";

export type JournalStatus = "pending" | "open" | "closed" | "cancelled";
export type JournalDirection = "long" | "short";
/** "exchange" = a trade imported from Binance, closed by the user's own fills. */
export type JournalExitKind = "T1" | "T2" | "T3" | "T4" | "T5" | "stop" | "breakeven" | "manual" | "exchange";

/** What POST /api/journal takes. Mirrors NewJournalEntry in journal.py. */
export interface NewJournalEntry {
  symbol: string;
  interval: Interval;
  direction: JournalDirection;
  /** Limit price, or the reference price of a market order (which fills at the next 1m open). */
  entry: number;
  stop: number;
  /** 1–5 prices on the profit side of the entry; the position is split equally across them. */
  targets: number[];
  /** e.g. "H4 demand", "Kimi B+", "manual". */
  setup?: string;
  source?: "agent_plan" | "kimi" | "manual";
  notes?: string;
  tags?: string[];
  entry_type?: "limit" | "market";
  size_qty?: number | null;
  /** Money lost at the stop; enables PnL in dollars. */
  risk_usd?: number | null;
  /** Fee per side, % of notional (default 0.1). */
  fee_pct?: number;
  manage?: "breakeven_after_t1" | "none";
  /** UNIX seconds; default now. */
  taken_at?: number | null;
  /** The zone the trade was taken from (a plan's entry zone), for its post-mortem. */
  zone_low?: number | null;
  zone_high?: number | null;
}

/** The review written when a trade closes. Mirrors PostMortem in journal.py; numbers come from `facts`. */
export interface PostMortem {
  generated_at: number; // UNIX s
  /** "template", or "provider:model" when the LLM wrote the text. */
  engine: string;
  summary: string;
  lessons: string[];
  lesson_codes: string[];
  facts: Record<string, unknown>;
  /** The result it was written for; a different result gets a new one. */
  closed_at: number | null;
  realized_r: number;
  data_source: string;
}

/** A round trip rebuilt from the user's Binance fills (Account tab). Its result comes from the fills. */
export interface ImportedTrade {
  external_id: string;
  market: "spot" | "futures";
  opened_at: number;
  closed_at: number | null;
  /** Largest position size in the round trip, in coins. */
  qty: number;
  entry_price: number;
  exit_price: number | null;
  /** Quote asset, after fees. */
  realized_pnl: number;
  fees: number;
  fills: number;
  quote_asset: string;
  notes: string[];
}

export interface JournalExit {
  time: number;
  price: number;
  kind: JournalExitKind;
  /** Share of the position closed here. */
  fraction: number;
  /** Price move in R for this share, before fees. */
  r: number;
}

export interface JournalEvaluation {
  status: JournalStatus;
  filled_at: number | null;
  fill_price: number | null;
  exits: JournalExit[];
  closed_at: number | null;
  /** Share still open (1 while pending). */
  remaining: number;
  current_stop: number | null;
  /** Closed share, after fees. */
  realized_r: number;
  gross_r: number;
  fees_r: number;
  /** Open share marked to the last price, before fees. */
  open_r: number;
  mfe_r: number | null;
  mae_r: number | null;
  /** Realized, after fees; null without risk_usd or size_qty. */
  pnl_usd: number | null;
  open_pnl_usd: number | null;
  outcome: "win" | "loss" | "breakeven" | null;
  last_price: number | null;
  data_source: string;
  /** Candles it was tracked on: 1m, or 5m/15m for trades older than ~13 months. */
  resolution: string;
  notes: string[];
  error: string | null;
  evaluated_at: number;
}

/** A stored entry with its evaluation, as GET /api/journal returns it. */
export interface JournalEntry extends Required<Omit<NewJournalEntry, "taken_at" | "size_qty" | "risk_usd" | "stop" | "source">> {
  id: string;
  /** null on trades imported from Binance (they have no stop unless the user gives one; no R then). */
  stop: number | null;
  source: NonNullable<NewJournalEntry["source"]> | "binance";
  imported: ImportedTrade | null;
  taken_at: number;
  size_qty: number | null;
  risk_usd: number | null;
  cancelled: boolean;
  manual_close: { time: number; price: number } | null;
  created_at: number;
  updated_at: number;
  evaluation: JournalEvaluation;
  /** Closed trades: written in the background shortly after the close. */
  postmortem?: PostMortem | null;
}

/** What PATCH /api/journal/{id} takes; only the fields given change. */
export interface JournalPatch {
  notes?: string;
  tags?: string[];
  setup?: string;
  /** Cancel an order that has not filled (a filled trade answers 409: close it instead). */
  cancel?: boolean;
  /** Close what is still open; price defaults to the last 1m close, time to now. */
  close?: { price?: number; time?: number };
}

export interface JournalGroupStats {
  key: string;
  trades: number;
  wins: number;
  losses: number;
  breakeven: number;
  win_rate: number;
  avg_r: number;
  total_r: number;
}

/** GET /api/journal/stats. R is after fees; only closed trades count towards the rates. */
export interface JournalStats {
  count: number;
  closed: number;
  open: number;
  pending: number;
  cancelled: number;
  wins: number;
  losses: number;
  breakeven: number;
  win_rate: number | null;
  avg_r: number | null;
  total_r: number;
  avg_win_r: number | null;
  avg_loss_r: number | null;
  expectancy: number | null;
  profit_factor: number | null;
  best_r: number | null;
  worst_r: number | null;
  open_r: number;
  /** Closed trades imported from Binance, and how many of them have no stop (left out of the R numbers). */
  imported_closed: number;
  no_r: number;
  /** Realized PnL of the closed trades that have one, after fees; null when none has. */
  pnl_usd: number | null;
  by_setup: JournalGroupStats[];
  by_symbol: JournalGroupStats[];
  by_direction: JournalGroupStats[];
  equity: { time: number; r_cum: number }[];
  notes: string[];
}

export interface JournalStatsFilter {
  symbol?: string;
  setup?: string;
  direction?: JournalDirection;
}

function changed() {
  if (typeof window !== "undefined") window.dispatchEvent(new CustomEvent(JOURNAL_EVENT));
}

export function fetchJournal(signal?: AbortSignal) {
  return apiRequest<{ entries: JournalEntry[] }>("/api/journal", { signal, timeoutMs: 45_000 });
}

/** Log a trade; resolves to the stored entry with its first evaluation. */
export async function createJournalEntry(body: NewJournalEntry): Promise<JournalEntry> {
  const res = await apiRequest<{ entry: JournalEntry }>("/api/journal", {
    method: "POST",
    body: JSON.stringify(body),
    timeoutMs: 45_000,
  });
  changed();
  return res.entry;
}

export async function updateJournalEntry(id: string, patch: JournalPatch): Promise<JournalEntry> {
  const res = await apiRequest<{ entry: JournalEntry }>(`/api/journal/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
    timeoutMs: 45_000,
  });
  changed();
  return res.entry;
}

export async function deleteJournalEntry(id: string): Promise<void> {
  await apiRequest<{ ok: boolean }>(`/api/journal/${encodeURIComponent(id)}`, { method: "DELETE" });
  changed();
}

/** (Re)writes a closed trade's post-mortem now. */
export async function regeneratePostmortem(id: string): Promise<JournalEntry> {
  const res = await apiRequest<{ entry: JournalEntry }>(`/api/journal/${encodeURIComponent(id)}/postmortem`, {
    method: "POST",
    timeoutMs: 120_000,
  });
  changed();
  return res.entry;
}

/** True while a closed trade waits for its post-mortem (none yet, or one written for an earlier result). */
export function postmortemPending(e: JournalEntry): boolean {
  const ev = e.evaluation;
  if (ev.status !== "closed") return false;
  const pm = e.postmortem;
  return !pm || pm.closed_at !== ev.closed_at || Math.abs(pm.realized_r - ev.realized_r) > 1e-6;
}

// ----------------------------------------------------------- weekly review --

/** GET /api/journal/review: the closed trades of the last `days` days. Mirrors weekly_review in postmortem.py. */
export interface WeeklyReview {
  from: number;
  to: number;
  days: number;
  closed: number;
  wins: number;
  losses: number;
  breakeven: number;
  win_rate: number | null;
  avg_r: number | null;
  total_r: number;
  best_r: number | null;
  worst_r: number | null;
  best_setup: JournalGroupStats | null;
  worst_setup: JournalGroupStats | null;
  best_symbol: JournalGroupStats | null;
  worst_symbol: JournalGroupStats | null;
  /** Lessons that came up in two or more post-mortems. */
  recurring: { code: string; name: string; count: number; example: string }[];
  trades: {
    id: string;
    symbol: string;
    setup: string;
    direction: JournalDirection;
    realized_r: number;
    outcome: "win" | "loss" | "breakeven" | null;
    closed_at: number | null;
    lesson: string | null;
  }[];
  missing_postmortems: number;
  open: number;
  /** Closed trades imported from Binance without a stop: not in the R numbers, only counted with their PnL. */
  imported_without_stop: number;
  imported_pnl: number | null;
  data_source: string;
  /** The message the schedule sends. */
  text: string;
}

/** Mirrors ReviewSettings in postmortem.py. */
export interface ReviewSettings {
  enabled: boolean;
  /** 0 = Monday … 6 = Sunday. */
  weekday: number;
  /** Local send time, "HH:MM". */
  time: string;
  timezone: string;
  days: number;
}

export interface ReviewStatus {
  settings: ReviewSettings;
  channels: { telegram: boolean; discord: boolean };
  last_sent_at: number | null; // ms
}

export function fetchWeeklyReview(days?: number, signal?: AbortSignal) {
  return apiRequest<WeeklyReview>(`/api/journal/review${days ? `?days=${days}` : ""}`, { signal, timeoutMs: 45_000 });
}

export function fetchReviewSettings(signal?: AbortSignal) {
  return apiRequest<ReviewStatus>("/api/journal/review/settings", { signal });
}

export function saveReviewSettings(settings: ReviewSettings) {
  return apiRequest<ReviewStatus>("/api/journal/review/settings", { method: "PUT", body: JSON.stringify(settings) });
}

/** Sends the review to Telegram / Discord now. Rejects (400) when no channel is configured. */
export function sendWeeklyReview(days?: number) {
  return apiRequest<WeeklyReview & { results: Record<string, boolean> }>(
    `/api/journal/review/send${days ? `?days=${days}` : ""}`,
    { method: "POST", timeoutMs: 45_000 },
  );
}

export function fetchJournalStats(filter: JournalStatsFilter = {}, signal?: AbortSignal) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(filter)) if (v) q.set(k, v);
  const qs = q.toString();
  return apiRequest<JournalStats>(`/api/journal/stats${qs ? `?${qs}` : ""}`, { signal, timeoutMs: 45_000 });
}

const TF_LABELS = new Set(["M1", "M5", "M15", "M30", "H1", "H3", "H4", "D1", "W1", "MN"]);

/** The setup type of a plan's basis: "H4 Demand (fresh) + D1 23.9–24.2" → "H4 demand". Same rule as the backend. */
export function planSetupName(basis: string): string {
  const words: string[] = [];
  for (const w of basis.split(/\s+/).filter(Boolean)) {
    if (/^[\d(+]/.test(w)) break;
    if (w.toLowerCase() !== "last") words.push(TF_LABELS.has(w) ? w : w.toLowerCase());
  }
  return words.join(" ").slice(0, 60) || "agent plan";
}

/** The agent's plan card → a journal entry (limit order at the plan's entry, all its targets). */
export function planToJournalEntry(
  plan: TradePlan,
  symbol: string,
  interval: Interval,
  extra: Partial<NewJournalEntry> = {},
): NewJournalEntry {
  return {
    symbol,
    interval,
    direction: plan.direction,
    entry: plan.entry,
    stop: plan.stop,
    targets: plan.targets.map((t) => t.price),
    setup: planSetupName(plan.basis),
    source: "agent_plan",
    notes: plan.basis ? `From ${plan.basis}` : "",
    entry_type: "limit",
    zone_low: plan.zone_low ?? null,
    zone_high: plan.zone_high ?? null,
    ...extra,
  };
}

const BLUE = "#3b82f6";
const RED = "#ef4444";
const GREEN = "#22c55e";
const SLATE = "#94a3b8";

const EXIT_COLOR: Record<string, string> = { stop: RED, breakeven: SLATE, manual: SLATE };

/** Entry, stop and target lines from when the trade was taken (segments ending at the close once it is closed),
 *  plus markers where it filled and exited. */
export function journalOverlays(e: JournalEntry): Overlay[] {
  const ev = e.evaluation;
  const long = e.direction === "long";
  if (e.stop == null) return importedOverlays(e);
  const stop = e.stop;
  const risk = Math.abs(e.entry - stop);
  const riskPct = e.entry ? ((risk / e.entry) * 100).toFixed(2) : "0";
  const start = e.taken_at;
  const end = ev.status === "closed" || ev.status === "cancelled" ? (ev.closed_at ?? e.manual_close?.time ?? null) : null;
  const tag = `${long ? "Long" : "Short"} ${e.setup}`;

  const line = (price: number, label: string, color: string, kind: string, dashed = false, width = 2): Overlay =>
    end
      ? { type: "trendline", time1: start, price1: price, time2: end, price2: price, label, color, kind,
          line_style: dashed ? "dashed" : "solid" }
      : { type: "horizontal_line", price, label, color, kind, line_style: dashed ? "dashed" : "solid",
          line_width: width, time_start: start };

  const out: Overlay[] = [
    {
      type: "box", label: "", kind: "plan_risk", price_low: Math.min(e.entry, stop), price_high: Math.max(e.entry, stop),
      color: "rgba(239, 68, 68, 0.10)", border_color: null, time_start: start, time_end: end,
    },
    {
      type: "box", label: "", kind: "plan_reward",
      price_low: Math.min(e.entry, e.targets[e.targets.length - 1]), price_high: Math.max(e.entry, e.targets[e.targets.length - 1]),
      color: "rgba(34, 197, 94, 0.08)", border_color: null, time_start: start, time_end: end,
    },
    line(e.entry, `${tag} entry`, BLUE, "plan_entry"),
    line(stop, `Stop (−${riskPct}%)`, RED, "plan_stop"),
  ];
  e.targets.forEach((t, i) => {
    const rr = risk ? Math.abs(t - e.entry) / risk : 0;
    out.push(line(t, `T${i + 1} (${rr.toFixed(1)}R)`, GREEN, "plan_target", true));
  });
  if (ev.status === "open" && ev.current_stop != null && ev.current_stop !== stop) {
    out.push(line(ev.current_stop, "Stop at breakeven", SLATE, "plan_stop", true, 1));
  }
  if (ev.filled_at != null && ev.fill_price != null) {
    out.push({
      type: "marker", time: ev.filled_at, price: ev.fill_price, position: long ? "below" : "above",
      shape: long ? "arrowUp" : "arrowDown", label: `Filled ${formatPrice(ev.fill_price)}`, color: BLUE, kind: "journal_fill",
    });
  }
  for (const x of ev.exits) {
    const name = x.kind === "breakeven" ? "BE" : x.kind === "stop" ? "Stop" : x.kind === "manual" ? "Closed" : x.kind;
    out.push({
      type: "marker", time: x.time, price: x.price, position: long ? "above" : "below", shape: "circle",
      label: `${name} ${x.r >= 0 ? "+" : ""}${x.r.toFixed(2)}R`, color: EXIT_COLOR[x.kind] ?? GREEN, kind: "journal_exit",
    });
  }
  return out.map((o, i) => ({ ...o, id: `journal-${e.id}-${i}` }));
}

/** A trade imported from Binance (no stop or targets): its entry and exit fills, joined by a dashed line. */
function importedOverlays(e: JournalEntry): Overlay[] {
  const ev = e.evaluation;
  const im = e.imported;
  const long = e.direction === "long";
  const entry = ev.fill_price ?? e.entry;
  const start = ev.filled_at ?? e.taken_at;
  const exit = ev.exits[ev.exits.length - 1];
  const tag = `${long ? "Long" : "Short"} (Binance ${im?.market ?? "fills"})`;
  const out: Overlay[] = [
    {
      type: "marker", time: start, price: entry, position: long ? "below" : "above", shape: long ? "arrowUp" : "arrowDown",
      label: `${tag} ${formatPrice(entry)}`, color: BLUE, kind: "journal_fill",
    },
  ];
  if (exit) {
    const win = (im?.realized_pnl ?? 0) >= 0;
    const pnl = im ? ` ${im.realized_pnl >= 0 ? "+" : ""}${im.realized_pnl.toFixed(2)} ${im.quote_asset}` : "";
    out.push(
      { type: "trendline", time1: start, price1: entry, time2: exit.time, price2: exit.price, label: "",
        color: win ? GREEN : RED, kind: "journal_exit", line_style: "dashed" },
      { type: "marker", time: exit.time, price: exit.price, position: long ? "above" : "below", shape: "circle",
        label: `Closed ${formatPrice(exit.price)}${pnl}`, color: win ? GREEN : RED, kind: "journal_exit" },
    );
  } else {
    out.push({ type: "horizontal_line", price: entry, label: `${tag} entry`, color: BLUE, kind: "plan_entry",
      line_style: "solid", line_width: 1, time_start: start });
  }
  return out.map((o, i) => ({ ...o, id: `journal-${e.id}-${i}` }));
}
