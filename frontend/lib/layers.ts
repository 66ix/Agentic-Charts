import type { Overlay } from "./types";

/**
 * Everything on the chart belongs to one layer, and each layer has an eye toggle in the Layers tab, so a busy
 * chart (AI levels + a trade plan + Kimi Cooked + a grid bot) can be thinned out without deleting anything.
 */
export type LayerId =
  | "zones"
  | "levels"
  | "structure"
  | "smc"
  | "volume"
  | "custom"
  | "plan"
  | "pinned"
  | "alerts"
  | "drawings"
  | "kimiSR"
  | "kimiFib"
  | "kimiForecast"
  | "kimiSignals"
  | "gridbots"
  | "journal"
  | "backtest"
  | "walls"
  | "liqs"
  | "events"
  | "compare"
  | "panels";

export interface LayerInfo {
  id: LayerId;
  label: string;
  group: "Chart agent" | "Kimi Cooked" | "Yours" | "Tools and data";
}

export const LAYERS: LayerInfo[] = [
  { id: "zones", label: "Zones (S/R, supply, demand)", group: "Chart agent" },
  { id: "levels", label: "Window highs and lows", group: "Chart agent" },
  { id: "structure", label: "Swings, trendlines, sweeps, patterns", group: "Chart agent" },
  { id: "smc", label: "Order blocks and FVGs", group: "Chart agent" },
  { id: "volume", label: "Volume profile levels", group: "Chart agent" },
  { id: "plan", label: "Trade plan", group: "Chart agent" },
  { id: "pinned", label: "Pinned answers", group: "Chart agent" },
  { id: "kimiSR", label: "S/R zones and odds", group: "Kimi Cooked" },
  { id: "kimiFib", label: "Fib ladder", group: "Kimi Cooked" },
  { id: "kimiForecast", label: "Forecast", group: "Kimi Cooked" },
  { id: "kimiSignals", label: "Signal labels", group: "Kimi Cooked" },
  { id: "drawings", label: "My drawings", group: "Yours" },
  { id: "custom", label: "Levels you asked for", group: "Yours" },
  { id: "alerts", label: "Alert lines", group: "Yours" },
  { id: "journal", label: "Journal trades", group: "Yours" },
  { id: "gridbots", label: "Grid bots", group: "Tools and data" },
  { id: "backtest", label: "Backtest trades", group: "Tools and data" },
  { id: "walls", label: "Order-book walls", group: "Tools and data" },
  { id: "liqs", label: "Liquidation levels", group: "Tools and data" },
  { id: "events", label: "Events and news", group: "Tools and data" },
  { id: "compare", label: "Compared symbols", group: "Tools and data" },
  { id: "panels", label: "Other panels", group: "Tools and data" },
];

export type LayerVisibility = Partial<Record<LayerId, boolean>>;

export const isVisible = (vis: LayerVisibility, id: LayerId) => vis[id] !== false;

const KIND_LAYER: Array<[RegExp, LayerId]> = [
  [/^(support|resistance|supply|demand)$/, "zones"],
  [/^window_/, "levels"],
  [/^(swing_|trendline|sweep|pattern_)/, "structure"],
  [/^(fvg_|ob_)/, "smc"],
  [/^(poc|vah|val)$/, "volume"],
  [/^custom_/, "custom"],
  [/^plan_/, "plan"],
  [/^alert$/, "alerts"],
];

/** Layer of an agent overlay, by its `kind`. */
export function overlayLayer(o: Overlay): LayerId {
  const kind = o.kind ?? "";
  for (const [re, id] of KIND_LAYER) if (re.test(kind)) return id;
  return "zones";
}

/** Layer of a set a dock tab put on the chart, by its key ("gridbot:<id>", "journal:<id>", "walls", …). */
export function panelLayer(key: string): LayerId {
  const head = key.split(":")[0];
  const map: Record<string, LayerId> = {
    gridbot: "gridbots",
    journal: "journal",
    backtest: "backtest",
    walls: "walls",
    liqs: "liqs",
    compare: "compare",
  };
  return map[head] ?? "panels";
}

/** An agent answer whose drawings stay on its chart when later answers replace the AI levels. */
export interface PinnedAnswer {
  label: string;
  overlays: Overlay[];
  at: number;
}

/** Drawings the dock tabs put on charts, by key ("gridbot:<id>", …), each for one symbol. */
export type PanelOverlays = Record<string, { symbol: string; overlays: Overlay[] }>;

export const pinsKey = (symbol: string, interval: string) => `ac:pins:${symbol}:${interval}`;

/**
 * Everything drawn on one chart from the agent, pinned answers, dock tabs and alerts, minus hidden layers, plus
 * how many objects each layer holds (for the Layers tab). An overlay that appears twice (a pinned answer that is
 * also the latest one) is drawn once.
 */
export function composeOverlays(opts: {
  symbol: string;
  overlays: Overlay[];
  pins: Record<string, PinnedAnswer>;
  panels: PanelOverlays;
  alerts: Overlay[];
  visibility: LayerVisibility;
}): { visible: Overlay[]; counts: Partial<Record<LayerId, number>> } {
  const visible: Overlay[] = [];
  const counts: Partial<Record<LayerId, number>> = {};
  const seen = new Set<string>();
  // The same zone from two answers (a pinned one and the latest) is drawn once.
  const shape = (o: Overlay) =>
    o.type === "box" ? `box:${o.kind}:${o.price_low}:${o.price_high}` : o.type === "horizontal_line" ? `line:${o.kind}:${o.price}` : null;
  const add = (o: Overlay, layer: LayerId, own = layer) => {
    for (const k of [o.id, shape(o)]) {
      if (k && seen.has(k)) return;
    }
    for (const k of [o.id, shape(o)]) if (k) seen.add(k);
    counts[layer] = (counts[layer] ?? 0) + 1;
    if (isVisible(opts.visibility, layer) && isVisible(opts.visibility, own)) visible.push(o);
  };
  for (const o of opts.overlays) add(o, overlayLayer(o));
  for (const p of Object.values(opts.pins)) for (const o of p.overlays) add(o, "pinned", overlayLayer(o));
  for (const [key, set] of Object.entries(opts.panels)) {
    if (set.symbol === opts.symbol) for (const o of set.overlays) add(o, panelLayer(key));
  }
  for (const o of opts.alerts) add(o, "alerts");
  return { visible, counts };
}

/** Drawings shown on this timeframe (a drawing can be limited to some timeframes). */
export function drawingsFor<T extends { style?: { timeframes?: string[] } }>(drawings: T[], interval: string): T[] {
  return drawings.filter((d) => !d.style?.timeframes?.length || d.style.timeframes.includes(interval));
}
