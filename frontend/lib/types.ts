// Mirrors backend/app/schemas.py. Keep the two in sync.

import type { GridPlan } from "./gridbot";

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
  /** Widen the price scale to keep this in view (default true). Off for wide reference sets such as grid bot ranges. */
  autoscale?: boolean;
}

export interface HorizontalLineOverlay extends OverlayBase {
  type: "horizontal_line";
  price: number;
  line_style?: LineStyleName;
  line_width?: number;
  time_start?: number | null;
  /** Price tag on the axis (default true). Off for dense sets of lines such as grid bots. */
  axis_label?: boolean;
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
  /** Scan the top coins by volume for the best setups (the Scanner tab). */
  scan_market?: boolean;
  trade_plan: "long" | "short" | "auto" | null;
  indicators_on: string[];
  indicators_off: string[];
  /** "Alert me when 1m shows a CHoCH inside the 4h demand". */
  zone_trigger?: ZoneTriggerIntent | null;
  grid_plan?: boolean;
  /** What a market scan ranks: trade setups, spot buys or grid coins. */
  scan_kind?: ScanKind;
  /** Walk 1D → 4H → 1H → 15m drawing only the valid levels. */
  top_down?: boolean;
  /** Where to sell coins held: the zones above price. */
  take_profit?: boolean;
  /** A spot buy-the-dip ladder with its 90-day test. */
  dip_ladder?: boolean;
  /** Not about a chart (the date, FOMC results, coin upgrades): answered in plain language. */
  general_question?: boolean;
  /** Spot: which coins to sell or trim (lost support, rejected at resistance). */
  sell_check?: boolean;
}

export type ScanKind = "setups" | "spot_buys" | "grid_coins";

/** How long the agent's answers are. */
export type AnswerDetail = "short" | "normal" | "detailed";

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
  /** What the entry zone is ("demand", "support", ..., "swing"), whether it is untested, and HTF confluence. */
  zone_kind?: string | null;
  zone_fresh?: boolean | null;
  zone_htf?: string[];
  /** The zone the entry is built on, when there is one. */
  zone_low?: number | null;
  zone_high?: number | null;
  /** How the matching backtest setup did on this coin and timeframe. */
  track_record?: TrackRecord | null;
}

/** How a plan's setup type did in the backtest on that coin and timeframe. Mirrors TrackRecord in schemas.py. */
export interface TrackRecord {
  /** Backtest setup id (BacktestSetup in lib/backtest.ts); null when nothing matches the plan's basis. */
  setup: string | null;
  /** "fresh 4h demand longs on INJ" */
  label: string;
  symbol: string;
  interval: Interval;
  status: "ok" | "small_sample" | "too_few_trades" | "short_history" | "no_match" | "unavailable";
  trades: number;
  wins: number;
  win_rate: number | null;
  avg_r: number | null;
  total_r: number | null;
  profit_factor: number | null;
  max_drawdown_r: number | null;
  /** Candles backtested (the Backtest tab's "Candles"). */
  bars: number;
  from_time: number | null;
  to_time: number | null;
  /** "last 1 year" */
  period: string;
  /** Exit rule (the Backtest tab's "Exit at"). */
  target: string;
  data_source: string;
  /** One line for the plan card. */
  summary: string;
  notes: string[];
}

/** One setup from the market-wide scanner. Mirrors MarketSetup in schemas.py. */
export interface MarketSetup {
  symbol: string;
  interval: Interval;
  direction: "long" | "short";
  last_price: number;
  change_pct: number | null;
  quote_volume: number | null;
  entry: number;
  stop: number;
  /** T1 */
  target: number;
  /** Reward-to-risk at T1. */
  rr: number;
  risk_pct: number;
  /** Entry distance from price, % (0 = at market). */
  distance_pct: number;
  distance_atr: number;
  basis: string;
  /** Trend per timeframe against the setup's direction; a range counts half. */
  agreement: { frames: Partial<Record<Interval, "up" | "down" | "range">>; aligned: number; total: number };
  track_record: TrackRecord | null;
  score: number;
  /** Rank among spot buys (longs at higher-timeframe demand); null when the setup isn't one. */
  spot_score?: number | null;
  plan: TradePlan;
  /** The plan as chart overlays. */
  overlays: Overlay[];
  data_source: string;
}

/** A coin ranging well enough for a Spot Grid bot. Mirrors GridCoin in schemas.py. */
export interface GridCoin {
  symbol: string;
  interval: Interval;
  last_price: number;
  low: number;
  high: number;
  width_pct: number;
  /** Times the close crossed the middle of the range. */
  crossings: number;
  /** Net move / total move; low = choppy. */
  efficiency: number;
  in_range_pct: number;
  /** 0 = bottom of the range, 100 = top. */
  position_pct: number;
  atr_pct: number;
  days: number;
  score: number;
  change_pct: number | null;
  quote_volume: number | null;
  note: string;
  data_source: string;
}

/** One market scan of one timeframe. Mirrors MarketScanResult in backend/app/market_scanner.py. */
export interface MarketScanResult {
  interval: Interval;
  /** ms */
  generated_at: number;
  seconds: number;
  trigger: "manual" | "timer" | "agent";
  universe: number;
  scanned: number;
  universe_source: "binance" | "fallback";
  data_source: string;
  longs: MarketSetup[];
  shorts: MarketSetup[];
  /** Longs at higher-timeframe demand, for buying coins outright (absent on scans from older versions). */
  spot_buys?: MarketSetup[];
  grid_coins?: GridCoin[];
  notes: string[];
}

// Top-down S/R walk (backend/app/top_down.py).
export interface WalkZone {
  kind: string;
  low: number;
  high: number;
  label: string;
  why: string;
  distance_pct: number;
  score: number;
  confirmed_by: string[];
}

export interface WalkStep {
  interval: Interval;
  /** "D1", "H4" */
  label: string;
  status: "drawn" | "skipped" | "failed";
  zones: WalkZone[];
  overlays: Overlay[];
  reason: string;
  summary: string;
  data_source: string;
}

export interface TopDownResult {
  symbol: string;
  last_price: number;
  steps: WalkStep[];
  generated_at: number;
  data_source: string;
  notes: string[];
}

// Spot buy-the-dip ladder (backend/app/dip_ladder.py).
export interface LadderRung {
  price: number;
  low: number;
  high: number;
  basis: string;
  weight_pct: number;
  amount: number;
  qty: number;
  distance_pct: number;
}

export interface LadderResult {
  plan: {
    symbol: string;
    timeframe: Interval;
    last_price: number;
    atr: number;
    budget: number;
    rungs: LadderRung[];
    take_profit: number;
    tp_basis: string;
    tp_gain_pct: number;
    avg_price: number;
    invalidation: number;
    notes: string[];
    data_source: string;
  };
  backtest: {
    days: number;
    timeframe: Interval;
    cycles: number;
    wins: number;
    fills: number;
    total_return_pct: number;
    buy_hold_pct: number;
    max_drawdown_pct: number;
    time_invested_pct: number;
    avg_hold_days: number | null;
    open_position: boolean;
    final_value: number;
    /** [UNIX seconds, value] */
    curve: [number, number][];
    notes: string[];
  } | null;
  overlays: Overlay[];
  generated_at: number;
}

/** A web page or headline an answer used. */
/** What a sell check covered. Mirrors SellWatch in schemas.py. */
export interface SellWatch {
  symbols: string[];
  interval: Interval;
}

/** A coin to sell or trim (backend/app/sell_check.py). Mirrors SellSignal in schemas.py. */
export interface SellSignal {
  symbol: string;
  interval: Interval;
  last_price: number;
  change_pct: number | null;
  action: "sell" | "trim";
  reason: string;
  sell_low: number;
  sell_high: number;
  /** "D1 support", "H4 supply" */
  zone: string;
  support_below: number | null;
  drop_pct: number | null;
  rsi: number | null;
  score: number;
  data_source: string;
}

export interface AnswerSource {
  title: string;
  url: string;
  source?: string;
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
  /** Stay armed after firing; fires again on a new crossing, at most every 5 minutes. Default false. */
  repeat?: boolean;
  /** ms; the alert disarms itself (expired) after this. */
  expires_at?: number | null;
  note?: string;
}

// Zone trigger alerts: a lower-timeframe confirmation inside a higher-timeframe zone. Mirrors schemas.py.
export type TriggerInterval = "1m" | "5m" | "15m";
export type Confirmation = "choch" | "sweep" | "engulfing" | "any";
export type TriggerZoneKind = "demand" | "supply" | "support" | "resistance" | "any";

/** Fixed prices (a zone picked on the chart, a plan's zone), or the nearest `kind` the detectors find on
 *  `timeframe`, looked up again whenever that timeframe closes. */
export interface TriggerZone {
  source: "fixed" | "detected";
  price_low?: number | null;
  price_high?: number | null;
  /** The way the confirmation must point; a fixed zone without one takes it from where price is. */
  direction?: "long" | "short" | null;
  timeframe?: Interval | null;
  kind?: TriggerZoneKind;
  /** Detected demand/supply: skip zones tested more than once (default true). */
  fresh_only?: boolean;
  label?: string;
}

export interface ZoneTriggerSpec {
  symbol: string;
  interval: TriggerInterval;
  zone: TriggerZone;
  confirm?: Confirmation;
  /** At most one fire per this many minutes (default 60). */
  cooldown_min?: number;
  repeat?: boolean;
  note?: string;
}

export interface ZoneTriggerIntent {
  timeframe: TriggerInterval;
  confirm: Confirmation;
  zone_kind: TriggerZoneKind;
  zone_timeframe: Interval | null;
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
  fire_count?: number;
  /** Disarmed because expires_at passed. */
  expired?: boolean;
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
  /** Market-wide scanner results ("best 5m setups right now"). */
  setups?: MarketSetup[];
  /** "Plan a grid bot on INJ": the grid planner's suggestion (lib/gridbot.ts). */
  grid_plan?: GridPlan | null;
  steps: string[];
  /** Zone trigger alerts the client should arm (POST /api/zone-triggers). */
  trigger_alerts?: ZoneTriggerSpec[];
  /** "Best grid bot coins". */
  grid_coins?: GridCoin[];
  /** A top-down walk for the client to play step by step. */
  top_down?: TopDownResult | null;
  /** A dip-buy ladder and its backtest. */
  ladder?: LadderResult | null;
  /** Web pages or headlines a plain-language answer used. */
  sources?: AnswerSource[];
  /** Sell or trim signals, strongest first. */
  sells?: SellSignal[];
  /** The coins and timeframe a sell check covered, to watch with signal alerts. */
  sell_watch?: SellWatch | null;
  /** The numbers the answer was written from (indicators, zones, market overview...), as the model saw them. */
  facts?: Record<string, unknown>;
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

/** Optional look of a drawing; anything left out uses the tool's default. */
export interface DrawingStyle {
  width?: number; // 1–4 px
  dash?: LineStyleName;
  /** Trendlines: extend past the first / second point. Horizontal rays: extendLeft draws a full-width line. */
  extendLeft?: boolean;
  extendRight?: boolean;
  /** Text notes: font size in px. */
  fontSize?: number;
  /** Only show on these timeframes; empty or missing = every timeframe. */
  timeframes?: Interval[];
}

export interface Drawing {
  id: string;
  type: DrawingType;
  points: ChartPoint[];
  color: string;
  text?: string;
  style?: DrawingStyle;
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

/** Lengths and colours the user can change in the Indicators menu. */
export interface IndicatorSettings {
  ema1: { length: number; color: string };
  ema2: { length: number; color: string };
  rsi: { length: number };
  macd: { fast: number; slow: number; signal: number };
  bb: { length: number; mult: number; color: string };
  atr: { length: number };
  stochRsi: { rsiLength: number; stochLength: number; k: number; d: number };
  vwapColor: string;
  /** Session and period levels (lib/sessionLevels.ts). */
  sessions: SessionLevelSettings;
}

/** Which session and period levels to draw; the backend computes them (backend/app/session_levels.py). */
export interface SessionLevelSettings {
  asia: boolean;
  london: boolean;
  ny: boolean;
  /** The previous session's high and low too, not only the latest session's. */
  previous: boolean;
  /** Shade each session's range. */
  boxes: boolean;
  day: boolean;
  week: boolean;
  month: boolean;
  /** Levels price has already traded through: drawn up to where they were taken, or left out. */
  showTaken: boolean;
  openingRange: "off" | "day" | "sessions";
  /** Opening-range length in minutes (multiples of 5). */
  orMinutes: number;
}

export const DEFAULT_INDICATOR_SETTINGS: IndicatorSettings = {
  ema1: { length: 20, color: "#f59e0b" },
  ema2: { length: 50, color: "#a855f7" },
  rsi: { length: 14 },
  macd: { fast: 12, slow: 26, signal: 9 },
  bb: { length: 20, mult: 2, color: "#38bdf8" },
  atr: { length: 14 },
  stochRsi: { rsiLength: 14, stochLength: 14, k: 3, d: 3 },
  vwapColor: "#22d3ee",
  sessions: {
    asia: true,
    london: true,
    ny: true,
    previous: true,
    boxes: true,
    day: true,
    week: true,
    month: true,
    showTaken: true,
    openingRange: "day",
    orMinutes: 30,
  },
};

export interface IndicatorState {
  ema20: boolean;
  ema50: boolean;
  psar: boolean;
  volume: boolean;
  // Added later: states saved before these existed lack the keys, so read them as `!!ind.rsi`.
  rsi: boolean;
  macd: boolean;
  vwap: boolean;
  /** Kimi Cooked v5.7.4, the user's own indicator (computed by the backend). */
  kimi: boolean;
  // Added with the indicator settings; read as `!!ind.x` like the ones above.
  bb?: boolean;
  atr?: boolean;
  stochRsi?: boolean;
  cvd?: boolean;
  /** Volume profile of the visible range, drawn on the right edge. */
  vprofile?: boolean;
  /** Asia / London / New York session levels, previous day / week / month and the opening range. */
  sessions?: boolean;
  /** Order-book heatmap behind the candles. */
  heatmap?: boolean;
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
  /** "local" (the browser's zone), "UTC", or an IANA zone like "Europe/London". Missing = local. */
  timezone?: string;
  /** Countdown to the candle close under the price label. Missing = on. */
  countdown?: boolean;
  /** Grid charts: move the crosshair on every chart together. Missing = on. */
  syncCrosshair?: boolean;
  /** Grid charts: every chart follows the active chart's symbol (each keeps its timeframe). */
  linkSymbol?: boolean;
}

// ------------------------------------------------------- kimi cooked --
// What GET /api/indicators/kimi returns. Mirrors the Kimi models in backend/app/schemas.py.

export interface KimiLevel {
  side: "support" | "resistance";
  price: number;
  zone_low: number;
  zone_high: number;
  time_start: number;
  /** The candle that broke it; null while it holds. */
  time_end: number | null;
  state: "active" | "expired" | "broken";
  /** % chance price reaches it within the forecast window. */
  odds: number | null;
  touches: number;
}

export interface KimiFib {
  time_start: number;
  time_end: number;
  swing_high: number;
  swing_low: number;
  down: boolean;
  levels: { ratio: number; price: number; odds: number }[];
  pocket_low: number;
  pocket_high: number;
}

export interface KimiSignal {
  type: "DIV" | "U/Dn" | "Early";
  direction: "long" | "short";
  /** The chart label: B+, B-, U, Dn, B+?, B-? */
  text: string;
  /** Where the label sits: the pivot candle (DIV, U/Dn) or the signal candle (Early). */
  time: number;
  /** The candle the signal could first be traded on. */
  confirm_time: number;
  price: number;
  entry: number;
  confluence: number;
  tier: "top" | "rest" | "warm-up";
  result: "open" | "win" | "loss" | "expiry";
  r: number | null;
}

export interface KimiForecast {
  /** The last closed candle; path[0] is its close and path[k] is k candles later. */
  start_time: number;
  step: number;
  horizon: number;
  path: number[];
  band_high: number[];
  band_low: number[];
  texture: number[];
  final: number;
  range_low: number;
  range_high: number;
  pct_change: number;
  vol_regime: "LOW" | "NORMAL" | "HIGH";
  headline: string;
  next_candle: { direction: "up" | "down"; right_pct: number | null; calls: number } | null;
}

/** One line of a Kimi pattern drawing; times past the last candle are future candles on the chart's grid. */
export interface KimiSegment {
  time_start: number;
  price_start: number;
  time_end: number;
  price_end: number;
  width: number;
  style: "solid" | "dashed" | "dotted";
}

export interface KimiPattern {
  /** Triple Top, Head & Shoulders, Double Bottom, Falling Wedge, Bull Flag, ... */
  name: string;
  /** The chart label: "2B", "2B ▲" after a break-out, "2B ✕" once invalidated. */
  text: string;
  direction: "bullish" | "bearish";
  time: number;
  /** watching = tracked for a break-out; formed = drawn but never tracked. */
  state: "watching" | "breakout" | "failed" | "formed";
  label_time: number;
  label_price: number;
  lines: KimiSegment[];
  breakout_level: number | null;
  invalidation: number | null;
  end_time: number | null;
  breakout_price: number | null;
  target: number | null;
}

/** A pattern break-out: its level and measured-move target lines. */
export interface KimiBreakout {
  name: string;
  direction: "bullish" | "bearish";
  time: number;
  time_end: number;
  price: number;
  target: number;
}

export interface KimiHarmonic {
  /** Gartley, Bat, Butterfly, Crab, Deep Crab, Alt Bat, Shark, 5-0, Three Drives, AB=CD */
  name: string;
  /** The chart label: "Gart ▲ ★2", then ⚠ / ✕ / ⋯ / ✓. */
  text: string;
  direction: "bullish" | "bearish";
  time: number;
  state: "active" | "failed" | "tp1" | "expired" | "compromised";
  points: { label: "X" | "A" | "B" | "C" | "D"; time: number; price: number }[];
  prz_low: number;
  prz_high: number;
  prz_shown: boolean;
  /** Right end of the PRZ box and the TP lines. */
  time_end: number;
  tp1: number;
  tp2: number;
  tp_basis: string;
  invalidation: number;
  prz_tier: number;
  end_time: number | null;
  ratios: Record<string, number | null>;
}

export interface KimiRow {
  label: string;
  value: string;
  tone: "up" | "down" | "mute" | null;
}

export interface KimiResult {
  symbol: string;
  interval: Interval;
  version: string;
  data_source: string;
  bars: number;
  last_closed: number;
  levels: KimiLevel[];
  fib: KimiFib | null;
  signals: KimiSignal[];
  forecast: KimiForecast | null;
  patterns: KimiPattern[];
  breakouts: KimiBreakout[];
  harmonics: KimiHarmonic[];
  verify: KimiRow[];
  stats: KimiRow[];
  notes: string[];
  seconds: number;
}
