// Links in alert cards (backend notify.app_link): /?symbol=INJUSDT&tf=4h&focus=desk:<id>. Opening one moves the
// chart there and opens the tab the alert belongs to.

import { TIMEFRAMES, type Interval } from "./types";

export type DeepLinkTab = "desk" | "alerts" | "trades";

export interface DeepLink {
  symbol: string;
  interval?: Interval;
  tab?: DeepLinkTab;
  /** The item to show in that tab (a desk call id, an alert id). */
  id?: string;
}

const FOCUS_TAB: Record<string, DeepLinkTab> = { desk: "desk", alert: "alerts", signal: "alerts", trade: "trades" };

export function parseDeepLink(search: string): DeepLink | null {
  const q = new URLSearchParams(search);
  const symbol = (q.get("symbol") ?? "").toUpperCase().replace(/[^A-Z0-9:/]/g, "");
  if (!symbol) return null;
  const tf = q.get("tf") ?? "";
  const out: DeepLink = { symbol };
  if (TIMEFRAMES.some((t) => t.value === tf)) out.interval = tf as Interval;
  const [kind, id] = (q.get("focus") ?? "").split(":");
  if (kind && FOCUS_TAB[kind]) {
    out.tab = FOCUS_TAB[kind];
    if (id) out.id = id;
  }
  return out;
}
