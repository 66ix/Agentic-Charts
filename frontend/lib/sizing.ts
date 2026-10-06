import { displaySymbol, formatPrice, splitSymbol } from "./format";
import type { TradePlan } from "./types";

/** The user's account settings for sizing trade plans (Settings → Position sizing). */
export interface SizingSettings {
  /** Account size in the quote currency (USDT). */
  account: number;
  /** Risk per trade, % of the account. */
  riskPct: number;
  /** Exchange fee per side, %. */
  feePct: number;
  /** Highest leverage the user is willing to use. */
  maxLeverage: number;
}

export const DEFAULT_SIZING: SizingSettings = { account: 1000, riskPct: 1, feePct: 0.1, maxLeverage: 10 };

export interface SizedPlan {
  qty: number;
  notional: number;
  riskUsd: number;
  /** Entry + exit fees if the stop is hit. */
  feesUsd: number;
  /** Leverage needed to hold `notional` with the whole account as margin (1 = no leverage). */
  leverage: number;
  targets: { label: string; price: number; share: number; pnlUsd: number }[];
  warnings: string[];
}

function roundQty(qty: number, price: number): number {
  // About $0.01 of resolution, without exchange step sizes at hand.
  const decimals = Math.max(0, Math.min(8, Math.ceil(Math.log10(Math.max(price, 1e-9) * 100))));
  const f = 10 ** decimals;
  return Math.floor(qty * f) / f;
}

/** Quantity so that hitting the stop loses `riskPct` of the account, fees included. Targets share equally. */
export function sizePlan(plan: TradePlan, s: SizingSettings): SizedPlan | null {
  const dist = Math.abs(plan.entry - plan.stop);
  if (!(s.account > 0) || !(s.riskPct > 0) || !(dist > 0)) return null;
  const fee = s.feePct / 100;
  const riskUsd = (s.account * s.riskPct) / 100;
  const qty = roundQty(riskUsd / (dist + fee * (plan.entry + plan.stop)), plan.entry);
  const notional = qty * plan.entry;
  const leverage = Math.max(1, notional / s.account);
  const share = plan.targets.length ? 1 / plan.targets.length : 1;
  const sign = plan.direction === "long" ? 1 : -1;
  const targets = plan.targets.map((t) => ({
    label: t.label.split(" ")[0],
    price: t.price,
    share,
    pnlUsd: qty * share * ((t.price - plan.entry) * sign - fee * (plan.entry + t.price)),
  }));
  const warnings: string[] = [];
  if (leverage > s.maxLeverage) {
    warnings.push(`Needs ${leverage.toFixed(1)}x leverage, above your ${s.maxLeverage}x limit: the stop is very tight for this risk.`);
  }
  if (qty <= 0) warnings.push("Risk is too small for this coin's price.");
  return { qty, notional, riskUsd, feesUsd: fee * qty * (plan.entry + plan.stop), leverage, targets, warnings };
}

function qtyText(qty: number): string {
  return qty >= 100 ? qty.toLocaleString("en-US", { maximumFractionDigits: 2 }) : String(+qty.toPrecision(6));
}

/** One line to paste into an exchange or a trading journal. */
export function orderText(plan: TradePlan, symbol: string, sized: SizedPlan | null, s: SizingSettings): string {
  const [base] = splitSymbol(symbol);
  const parts = [
    `${plan.direction.toUpperCase()} ${displaySymbol(symbol)}`,
    `Entry ${formatPrice(plan.entry)} (limit)`,
    `Stop ${formatPrice(plan.stop)}`,
    ...plan.targets.map((t, i) => `TP${i + 1} ${formatPrice(t.price)}${plan.targets.length > 1 ? ` (${Math.round(100 / plan.targets.length)}%)` : ""}`),
  ];
  if (sized) {
    parts.push(`Qty ${qtyText(sized.qty)} ${base} (~$${sized.notional.toFixed(2)})`);
    parts.push(`Risk $${sized.riskUsd.toFixed(2)} (${s.riskPct}%)`);
    if (sized.leverage > 1) parts.push(`Lev ${sized.leverage.toFixed(1)}x`);
  }
  return parts.join(" · ");
}

export { qtyText };
