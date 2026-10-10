import type { AlertHistoryItem } from "./alerts";
import { displaySymbol, formatPrice } from "./format";

/** A toast for an event other than a price or signal alert (those have their own socket messages). */
export interface EventToast {
  id: string;
  kind: "desk" | "trade" | "metric";
  title: string;
  text: string;
  /** The coin to open on click; absent for market-wide alerts. */
  symbol?: string;
  /** The dock tab to open on click. */
  tab: string;
}

/**
 * The toast for a live `{type: "history"}` socket message, or null when another message already toasts it (price
 * and signal alerts) or it isn't worth one (briefs). Market alerts are recorded as kind "signal" with symbol MARKET.
 */
export function toastFromHistory(item: AlertHistoryItem): EventToast | null {
  const base = { id: `h-${item.id}`, title: item.title, text: item.text };
  if (item.kind === "desk") return { ...base, kind: "desk", symbol: item.symbol || undefined, tab: "desk" };
  if (item.kind === "trade") return { ...base, kind: "trade", symbol: item.symbol || undefined, tab: "trades" };
  if (item.kind === "signal" && item.symbol === "MARKET") return { ...base, kind: "metric", tab: "market" };
  return null;
}

/** "(2) INJ 24.31 ▲1.2% · 4H": the browser tab's title, with the events that arrived while it was hidden. */
export function tabTitle(opts: {
  symbol: string;
  interval: string;
  price: number | null;
  change24: number | null;
  unread: number;
}): string {
  const parts = [displaySymbol(opts.symbol)];
  if (opts.price != null) parts.push(formatPrice(opts.price));
  if (opts.change24 != null && Number.isFinite(opts.change24)) {
    parts.push(`${opts.change24 >= 0 ? "▲" : "▼"}${Math.abs(opts.change24).toFixed(1)}%`);
  }
  const head = opts.unread > 0 ? `(${opts.unread > 99 ? "99+" : opts.unread}) ` : "";
  return `${head}${parts.join(" ")} · ${opts.interval.toUpperCase()}`;
}
