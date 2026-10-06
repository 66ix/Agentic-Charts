import type { Interval, Overlay } from "./types";

/**
 * Props every tab in the right-hand dock receives from the workspace. Tabs are rendered full height inside
 * the dock (about 320–480 px wide), so a tab's root is `flex h-full min-h-0 flex-col` with its own scroll area.
 */
export interface DockPanelProps {
  /** The active chart's symbol and timeframe. */
  symbol: string;
  interval: Interval;
  /** Live price of the active chart; null while loading. */
  price: number | null;
  watchlist: string[];
  /** Move the active chart (optionally to a timeframe too). */
  onPickSymbol(symbol: string, interval?: Interval): void;
  /**
   * Draw this tab's overlays on every chart showing `symbol`. Each `key` holds one set: calling again with the
   * same key replaces it, and an empty list clears it. Namespace keys by feature, e.g. `gridbot:<id>`.
   */
  onChartOverlays(key: string, symbol: string, overlays: Overlay[]): void;
}
