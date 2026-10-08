import { apiRequest } from "./api";
import { formatPrice } from "./format";
import type { HorizontalLineOverlay, LadderResult, TradePlan } from "./types";

/** Window event the Paper tab listens for, so orders placed from an agent answer show up at once. */
export const PAPER_CHANGED_EVENT = "ac:paper-changed";

export type PaperSide = "buy" | "sell";
export type PaperOrderType = "market" | "limit" | "stop";
export type PaperSource = "manual" | "plan" | "ladder" | "agent";

/** An order as placed. Mirrors NewPaperOrder in backend/app/paper.py. */
export interface NewPaperOrder {
  symbol: string;
  side: PaperSide;
  type: PaperOrderType;
  /** Limit or stop price; market orders use the last price. */
  price?: number | null;
  /** Coins; a sell without it sells everything held. */
  qty?: number | null;
  /** Buys: USDT to spend, fees included, instead of qty. */
  quote?: number | null;
  group?: string | null;
  source?: PaperSource;
  note?: string;
}

export interface PaperOrderResult {
  status: "pending" | "filled" | "cancelled";
  filled_at: number | null;
  fill_price: number | null;
  fill_qty: number | null;
  fee: number;
  /** Sells: proceeds after fees minus the average cost of what sold. */
  pnl: number | null;
  reason: string;
}

export interface PaperOrder extends NewPaperOrder {
  id: string;
  placed_at: number;
  cancelled_at: number | null;
  market_price: number | null;
  result: PaperOrderResult;
}

export interface PaperHolding {
  symbol: string;
  qty: number;
  avg_cost: number;
  last_price: number | null;
  value: number | null;
  unrealized_pnl: number | null;
  unrealized_pct: number | null;
  realized_pnl: number;
}

/** Mirrors Wallet in backend/app/paper.py. */
export interface PaperWallet {
  start_cash: number;
  fee_pct: number;
  created_at: number;
  cash: number;
  reserved: number;
  holdings_value: number;
  equity: number;
  return_pct: number;
  realized_pnl: number;
  unrealized_pnl: number;
  holdings: PaperHolding[];
  /** Newest first. */
  orders: PaperOrder[];
  stats: { sells: number; wins: number; win_rate: number | null; best: number | null; worst: number | null; fees: number };
  data_source: string;
  error: string | null;
  evaluated_at: number;
}

export function fetchPaperWallet(signal?: AbortSignal) {
  return apiRequest<PaperWallet>("/api/paper", { signal, timeoutMs: 60_000 });
}

/** Places orders together (one group when there are several). Tells the Paper tab to refresh. */
export async function placePaperOrders(orders: NewPaperOrder[]) {
  const res = await apiRequest<{ orders: PaperOrder[]; wallet: PaperWallet }>("/api/paper/orders", {
    method: "POST",
    body: JSON.stringify({ orders }),
    timeoutMs: 60_000,
  });
  window.dispatchEvent(new CustomEvent(PAPER_CHANGED_EVENT, { detail: res.wallet }));
  return res;
}

export function cancelPaperOrder(id: string) {
  return apiRequest<{ wallet: PaperWallet }>(`/api/paper/orders/${encodeURIComponent(id)}`, { method: "DELETE", timeoutMs: 60_000 });
}

export function resetPaperWallet(startCash: number, feePct?: number) {
  return apiRequest<{ wallet: PaperWallet }>("/api/paper/reset", {
    method: "POST",
    body: JSON.stringify({ start_cash: startCash, ...(feePct != null ? { fee_pct: feePct } : {}) }),
  });
}

/**
 * A long plan as paper orders: a limit buy at the entry, a stop that sells everything at the stop, and the targets
 * selling equal shares (the last one sells what is left). The stop and targets cancel each other once the coins
 * are gone, like an OCO order.
 */
export function planToPaperOrders(plan: TradePlan, symbol: string, qty: number): NewPaperOrder[] {
  const n = plan.targets.length;
  const share = n ? qty / n : 0;
  const note = `Plan from ${plan.basis}`.slice(0, 300);
  return [
    { symbol, side: "buy", type: "limit", price: plan.entry, qty, source: "plan", note },
    { symbol, side: "sell", type: "stop", price: plan.stop, source: "plan", note: "Stop" },
    ...plan.targets.map<NewPaperOrder>((t, i) => ({
      symbol,
      side: "sell",
      type: "limit",
      price: t.price,
      qty: i === n - 1 ? null : share,
      source: "plan",
      note: t.label.split(" ")[0],
    })),
  ];
}

/** A dip-buy ladder as paper orders: each rung a limit buy for its USDT amount, and the take profit selling all. */
export function ladderToPaperOrders(ladder: LadderResult): NewPaperOrder[] {
  const pl = ladder.plan;
  return [
    ...pl.rungs.map<NewPaperOrder>((r, i) => ({
      symbol: pl.symbol,
      side: "buy",
      type: "limit",
      price: r.price,
      quote: r.amount,
      source: "ladder",
      note: `Rung ${i + 1}: ${r.basis}`.slice(0, 300),
    })),
    { symbol: pl.symbol, side: "sell", type: "limit", price: pl.take_profit, source: "ladder", note: `Take profit: ${pl.tp_basis}`.slice(0, 300) },
  ];
}

const BUY = "#38bdf8";
const SELL = "#22c55e";
const STOP = "#ef4444";
const AVG = "#eab308";

/** Pending paper orders and the average cost of what is held, as dashed lines on `symbol`'s chart. */
export function paperOverlays(w: PaperWallet, symbol: string): HorizontalLineOverlay[] {
  const out: HorizontalLineOverlay[] = [];
  for (const o of w.orders) {
    if (o.symbol !== symbol || o.result.status !== "pending" || o.price == null) continue;
    const kind = o.type === "stop" ? "Stop" : o.side === "buy" ? "Buy" : "Sell";
    out.push({
      type: "horizontal_line",
      id: `paper:${o.id}`,
      price: o.price,
      label: `Paper ${kind} ${formatPrice(o.price)}`,
      color: o.type === "stop" ? STOP : o.side === "buy" ? BUY : SELL,
      line_style: "dashed",
      line_width: 1,
      kind: "paper",
      autoscale: false,
    });
  }
  const h = w.holdings.find((x) => x.symbol === symbol && x.qty > 0);
  if (h) {
    out.push({
      type: "horizontal_line",
      id: `paper:avg:${symbol}`,
      price: h.avg_cost,
      label: `Paper avg cost ${formatPrice(h.avg_cost)}`,
      color: AVG,
      line_style: "dotted",
      line_width: 1,
      kind: "paper",
      autoscale: false,
    });
  }
  return out;
}

export function usd(v: number | null | undefined, sign = false): string {
  if (v == null || !Number.isFinite(v)) return "–";
  const s = Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `${v < 0 ? "−" : sign ? "+" : ""}$${s}`;
}
