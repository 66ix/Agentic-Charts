// Exit plans (backend/app/exit_plan.py): where to sell a coin you hold in pieces, and where holding it is wrong.

import { apiRequest } from "./api";
import { formatPrice } from "./format";
import type { HorizontalLineOverlay, Overlay } from "./types";

export type ExitProfile = "quarters" | "thirds" | "cost_out";

export const EXIT_PROFILES: { id: ExitProfile; label: string; hint: string }[] = [
  { id: "quarters", label: "Quarters", hint: "25% at each of three levels, a quarter left to run" },
  { id: "thirds", label: "Thirds", hint: "A third at each of three levels" },
  { id: "cost_out", label: "Money back first", hint: "The first level sells enough to get your cost back; the rest is spread higher" },
];

export interface ExitRung {
  price: number;
  low: number;
  high: number;
  label: string;
  sell_qty: number;
  /** Share of the coins held, %. */
  sell_pct: number;
  usdt: number;
  gain_from_now_pct: number;
  pnl_vs_entry_pct: number | null;
  /** Gain (or loss) of this rung against your average entry. */
  realizes_usd: number | null;
  /** You sold there after the plan was made. */
  done: boolean;
}

export interface ExitPlan {
  symbol: string;
  profile: ExitProfile;
  qty: number;
  sellable_qty: number;
  avg_entry: number | null;
  price: number;
  rungs: ExitRung[];
  runner_qty: number;
  invalidation: { price: number; label: string; rule: string } | null;
  notes: string[];
  created_at: number;
  /** Ids of the price alerts armed for it. */
  armed: string[];
}

export function buildExitPlan(symbol: string, profile: ExitProfile, qty?: number, avgEntry?: number) {
  return apiRequest<ExitPlan>("/api/exit-plan", {
    method: "POST",
    body: JSON.stringify({ symbol, profile, qty, avg_entry: avgEntry }),
    timeoutMs: 60_000,
  });
}

export function fetchExitPlans(signal?: AbortSignal) {
  return apiRequest<{ plans: ExitPlan[] }>("/api/exit-plans", { signal });
}

export function armExitPlan(symbol: string) {
  return apiRequest<ExitPlan>(`/api/exit-plans/${encodeURIComponent(symbol)}/arm`, { method: "POST" });
}

export function deleteExitPlan(symbol: string) {
  return apiRequest<{ ok: boolean }>(`/api/exit-plans/${encodeURIComponent(symbol)}`, { method: "DELETE" });
}

/** The plan on the chart: a dashed line per rung with its size, and the invalidation in red. */
export function exitOverlays(p: ExitPlan): Overlay[] {
  const coin = p.symbol.replace(/USDT$/, "");
  const rungs: HorizontalLineOverlay[] = p.rungs
    .filter((r) => !r.done)
    .map((r, i) => ({
      type: "horizontal_line",
      id: `exit-${p.symbol}-${i}`,
      kind: "plan_target",
      price: r.price,
      color: "#22c55e",
      line_style: "dashed",
      line_width: 1,
      label: `Exit TP${i + 1}: sell ${r.sell_pct}% ≈ ${r.sell_qty} ${coin} ($${Math.round(r.usdt)})`,
    }));
  if (p.invalidation) {
    rungs.push({
      type: "horizontal_line",
      id: `exit-${p.symbol}-inv`,
      kind: "plan_stop",
      price: p.invalidation.price,
      color: "#ef4444",
      line_style: "dashed",
      line_width: 1,
      label: `Exit: wrong on a daily close below ${formatPrice(p.invalidation.price)}`,
    });
  }
  return rungs;
}
