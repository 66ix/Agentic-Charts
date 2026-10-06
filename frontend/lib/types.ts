// Mirrors backend/app/schemas.py. Keep the two in sync.

export const TIMEFRAMES = [
  { value: "1m", label: "1m" },
  { value: "5m", label: "5m" },
  { value: "15m", label: "15m" },
  { value: "30m", label: "30m" },
  { value: "1h", label: "1h" },
  { value: "3h", label: "3h" },
  { value: "4h", label: "4h" },
  { value: "1d", label: "D" },
  { value: "1w", label: "W" },
  { value: "1M", label: "M" },
] as const;

export type Interval = (typeof TIMEFRAMES)[number]["value"];

export const INTERVAL_SECONDS: Record<Interval, number> = {
  "1m": 60,
  "5m": 300,
  "15m": 900,
  "30m": 1800,
  "1h": 3600,
  "3h": 10800,
  "4h": 14400,
  "1d": 86400,
  "1w": 604800,
  "1M": 2592000,
};

export interface Candle {
  time: number; // UNIX seconds
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export type DataSource = "binance" | "synthetic" | "client" | "connecting" | "reconnecting" | "offline";
export type LineStyleName = "solid" | "dashed" | "dotted";

interface OverlayBase {
  id?: string | null;
  label: string;
  color: string;
  kind?: string | null;
  strength?: number | null;
}

export interface HorizontalLineOverlay extends OverlayBase {
  type: "horizontal_line";
  price: number;
  line_style?: LineStyleName;
  line_width?: number;
  time_start?: number | null;
}

export interface BoxOverlay extends OverlayBase {
  type: "box";
  price_high: number;
  price_low: number;
  border_color?: string | null;
  time_start?: number | null;
  time_end?: number | null;
}

export interface MarkerOverlay extends OverlayBase {
  type: "marker";
  time: number;
  price: number;
  position: "above" | "below";
  shape: "arrowUp" | "arrowDown" | "circle" | "square";
}

export interface TrendlineOverlay extends OverlayBase {
  type: "trendline";
  time1: number;
  price1: number;
  time2: number;
  price2: number;
  extend_right?: boolean;
  line_style?: LineStyleName;
}

export type Overlay = HorizontalLineOverlay | BoxOverlay | MarkerOverlay | TrendlineOverlay;

export interface CustomLevel {
  kind: "line" | "zone";
  price: number | null;
  price_low: number | null;
  price_high: number | null;
  label: string;
}

export interface AnalysisIntent {
  features: string[];
  timeframe: Interval | null;
  window_timeframes: Interval[];
  max_zones: number;
  answer_hint: string;
  custom_levels: CustomLevel[];
  remove: string[];
  keep_existing: boolean;
  alert_prices: number[];
  alert_targets: string[];
  symbol: string | null;
  switch_chart: boolean;
  scan_watchlist: boolean;
  scan_filter: string;
  trade_plan: "long" | "short" | "auto" | null;
  indicators_on: string[];
  indicators_off: string[];
}

export interface Navigate {
  symbol: string;
  interval: Interval;
}

export interface PlanTarget {
  price: number;
  label: string;
  rr: number;
}

/** Entry/stop/targets built from detected levels. Mirrors TradePlan in schemas.py. */
export interface TradePlan {
  direction: "long" | "short";
  entry: number;
  stop: number;
  targets: PlanTarget[];
  basis: string;
  risk_pct: number;
  notes: string[];
}

/** One coin's row in a watchlist scan. Mirrors ScanResult in schemas.py. */
export interface ScanResult {
  symbol: string;
  interval: Interval;
  last_price: number;
  change_pct: number | null;
  trend: "up" | "down" | "range";
  rsi: number | null;
  nearest_kind: string | null;
  nearest_low: number | null;
  nearest_high: number | null;
  distance_pct: number | null;
  signals: string[];
  score: number;
  data_source: string;
}

export interface Ticker {
  symbol: string;
  price: number;
  change_pct: number | null;
  source: string;
}

export interface ChatTurn {
  role: "user" | "agent";
  text: string;
}

/** An alert the agent asks the client to arm. Mirrors AlertSpec in schemas.py. */
export interface AlertSpec {
  kind: "cross" | "zone";
  price: number | null;
  price_low: number | null;
  price_high: number | null;
  label: string;
}

/** A price alert stored and evaluated by the backend. Mirrors PriceAlert in schemas.py. */
export interface PriceAlert extends AlertSpec {
  id: string;
  symbol: string;
  armed: boolean;
  created_at: number; // ms
  triggered_at?: number | null; // ms
  triggered_price?: number | null;
  /** Where price was last seen relative to the level, so it fires on the transition. */
  last_side?: "above" | "below" | "inside" | null;
}

/** Notification channels configured on the backend. */
export interface AlertChannels {
  telegram: boolean;
  discord: boolean;
}

export interface AnalyzeResponse {
  symbol: string;
  interval: Interval;
  analysis_interval: Interval;
  overlays: Overlay[];
  summary: string;
  intent: AnalysisIntent;
  stats: {
    last_price: number;
    atr: number;
    trend: "up" | "down" | "range";
    ema_fast: number;
    ema_slow: number;
    swing_highs: number;
    swing_lows: number;
  };
  engine: Record<string, string>;
  data_source: string;
  alerts: AlertSpec[];
  navigate: Navigate | null;
  indicators: Record<string, boolean>;
  scan: ScanResult[];
  plan: TradePlan | null;
  steps: string[];
  generated_at: string;
}

export interface Metric {
  key: string;
  label: string;
  value: number;
  display: string;
  change_pct: number | null;
  source: "live" | "mock";
  note?: string | null;
}

export interface MarketMetrics {
  metrics: Metric[];
  updated_at: string;
}

// ------------------------------------------------------------- drawings --

export type ToolId =
  | "crosshair"
  | "trendline"
  | "hray"
  | "fib"
  | "rect"
  | "text"
  | "pattern"
  | "ruler";

export interface ChartPoint {
  time: number; // UNIX seconds; may fall between or beyond bars
  price: number;
}

export type DrawingType = Exclude<ToolId, "crosshair">;

export interface Drawing {
  id: string;
  type: DrawingType;
  points: ChartPoint[];
  color: string;
  text?: string;
}

/** Points each tool needs before the drawing is complete. */
export const TOOL_POINTS: Record<DrawingType, number> = {
  trendline: 2,
  hray: 1,
  fib: 2,
  rect: 2,
  text: 1,
  pattern: 5,
  ruler: 2,
};

export interface IndicatorState {
  ema20: boolean;
  ema50: boolean;
  psar: boolean;
  volume: boolean;
}

/** One chart in the multi-chart grid. */
export interface ChartCell {
  symbol: string;
  interval: Interval;
}

export type GridMode = 1 | 2 | 4;

export interface LayoutState {
  logScale: boolean;
  grid: boolean;
  autoLevels: boolean;
}
