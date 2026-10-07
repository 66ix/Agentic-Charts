import { apiRequest } from "./api";
import { formatPrice } from "./format";
import type { Interval, Overlay } from "./types";

/** Mirrors backend/app/trade_manager.py. */
export type AdviceKind = "stop" | "target" | "breakeven" | "trail" | "structure" | "done";

export interface TradeAdvice {
  id: string;
  time: number;
  kind: AdviceKind;
  text: string;
  price: number;
  suggested_stop: number | null;
  take_pct: number | null;
  status: "new" | "done" | "dismissed";
}

export interface NewManagedTrade {
  symbol: string;
  interval: Interval;
  direction: "long" | "short";
  entry: number;
  stop: number;
  targets: number[];
  qty?: number | null;
  opened_at?: number | null;
  source?: "journal" | "binance" | "manual";
  source_id?: string | null;
  breakeven_after_t1?: boolean;
  trail?: "structure" | "atr" | "off";
  atr_mult?: number;
  follow_advice?: boolean;
  notes?: string;
}

export interface ManagedTrade extends Required<Omit<NewManagedTrade, "qty" | "source_id">> {
  id: string;
  qty: number | null;
  source_id: string | null;
  initial_stop: number;
  status: "open" | "stopped" | "done" | "closed";
  targets_hit: number;
  last_bar: number | null;
  last_price: number | null;
  r_now: number | null;
  max_r: number;
  exit_price: number | null;
  exit_r: number | null;
  advice: TradeAdvice[];
  data_source: string;
}

export interface TradePatch {
  stop?: number;
  targets?: number[];
  trail?: "structure" | "atr" | "off";
  breakeven_after_t1?: boolean;
  follow_advice?: boolean;
  close_price?: number;
  advice_id?: string;
  advice_status?: "done" | "dismissed";
}

export function fetchManagedTrades(signal?: AbortSignal) {
  return apiRequest<{ trades: ManagedTrade[] }>("/api/trades/managed", { signal });
}

export function manageTrade(body: NewManagedTrade | { journal_id: string; interval?: Interval }) {
  return apiRequest<ManagedTrade>("/api/trades/managed", { method: "POST", body: JSON.stringify(body), timeoutMs: 45_000 });
}

export function updateManagedTrade(id: string, patch: TradePatch) {
  return apiRequest<ManagedTrade>(`/api/trades/managed/${id}`, { method: "PATCH", body: JSON.stringify(patch) });
}

export function deleteManagedTrade(id: string) {
  return apiRequest<{ ok: boolean }>(`/api/trades/managed/${id}`, { method: "DELETE" });
}

/** Entry, current stop, targets left and a pending suggested stop, as lines on the trade's chart. */
export function tradeOverlays(t: ManagedTrade): Overlay[] {
  if (t.status !== "open") return [];
  const prefix = `trade:${t.id}`;
  const out: Overlay[] = [
    { type: "horizontal_line", id: `${prefix}:entry`, kind: "trade", label: `${t.direction === "long" ? "Long" : "Short"} entry`, price: t.entry, color: "#3b82f6", line_style: "solid", time_start: t.opened_at },
    { type: "horizontal_line", id: `${prefix}:stop`, kind: "trade", label: t.stop === t.entry ? "Stop (breakeven)" : "Stop", price: t.stop, color: "#ef4444", line_style: "solid", time_start: t.opened_at },
  ];
  t.targets.forEach((p, i) => {
    if (i < t.targets_hit) return;
    out.push({ type: "horizontal_line", id: `${prefix}:t${i}`, kind: "trade", label: `T${i + 1}`, price: p, color: "#22c55e", line_style: "dashed", time_start: t.opened_at });
  });
  const pending = [...t.advice].reverse().find((a) => a.status === "new" && a.suggested_stop != null);
  if (pending?.suggested_stop != null) {
    out.push({ type: "horizontal_line", id: `${prefix}:suggested`, kind: "trade", label: `Move stop to ${formatPrice(pending.suggested_stop)}?`, price: pending.suggested_stop, color: "#facc15", line_style: "dotted", time_start: pending.time });
  }
  return out;
}
