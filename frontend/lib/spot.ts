// Spot tools shared by the agent's answers, the Scanner tab and the Dip ladder tab: spot mode, grid coins on the
// chart, and the hand-off from a grid coin to the Grid bots tab.

import { openDockPanel } from "./dock";
import { offerGridPlan, planGridBot } from "./gridbot";
import type { GridCoin, LadderResult, Overlay } from "./types";

/** Spot only (default on): the agent never builds short plans and scans show longs, spot buys and grid coins. */
export const SPOT_ONLY_KEY = "ac:spot-only";

const RANGE = "rgba(245, 158, 11, 0.55)";

/** A grid coin's range on its chart: a faint box with its edges. Kept out of the price auto-scale. */
export function gridCoinOverlays(c: GridCoin): Overlay[] {
  return [
    {
      type: "box",
      id: `gridcoin-${c.symbol}`,
      kind: "grid_range",
      label: `Range ${c.width_pct.toFixed(1)}% · ${c.crossings} crossings`,
      price_low: c.low,
      price_high: c.high,
      color: "rgba(245, 158, 11, 0.07)",
      border_color: RANGE,
      autoscale: false,
    },
  ];
}

/** Plans a Spot Grid bot for `symbol` and opens it in the Grid bots tab to test on 90 days. */
export async function planGridFor(symbol: string): Promise<void> {
  const plan = await planGridBot({ symbol });
  offerGridPlan(plan);
  openDockPanel("gridbots");
}

/** One line for a ladder's backtest: return against holding and the cycles. */
export function ladderTestLine(r: LadderResult): string | null {
  const b = r.backtest;
  if (!b) return null;
  const sign = (v: number) => `${v >= 0 ? "+" : ""}${v.toFixed(1)}%`;
  return `${b.days}d test: ${sign(b.total_return_pct)} vs ${sign(b.buy_hold_pct)} holding · ${b.cycles} cycle${b.cycles === 1 ? "" : "s"} · max DD ${b.max_drawdown_pct.toFixed(1)}%${b.open_position ? " · still holding" : ""}`;
}
