"use client";

import {
  ColorType,
  createChart,
  CrosshairMode,
  LineStyle,
  PriceScaleMode,
  TickMarkType,
  type IChartApi,
  type ISeriesApi,
  type MouseEventParams,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState, type ReactNode } from "react";

import KimiPanel from "./KimiPanel";

import { useChartEvents } from "@/hooks/useChartEvents";
import { useOrderbookHeatmap } from "@/hooks/useOrderbookHeatmap";
import { useSessionLevels } from "@/hooks/useSessionLevels";

import { apiRequest, fetchKimi, fetchKlines } from "@/lib/api";
import { mergeCandles, readCandles, writeCandles } from "@/lib/candleCache";
import { AxisMaskPrimitive } from "@/lib/chart/primitives/AxisMaskPrimitive";
import { LabelResetPrimitive } from "@/lib/chart/primitives/base";
import { BoxZonePrimitive } from "@/lib/chart/primitives/BoxZonePrimitive";
import { CountdownPrimitive } from "@/lib/chart/primitives/CountdownPrimitive";
import { DrawingLayerPrimitive } from "@/lib/chart/primitives/DrawingLayerPrimitive";
import { EventLinesPrimitive, type EventLine } from "@/lib/chart/primitives/EventLinesPrimitive";
import { HeatmapPrimitive } from "@/lib/chart/primitives/HeatmapPrimitive";
import { KimiPrimitive } from "@/lib/chart/primitives/KimiPrimitive";
import { LabeledRayPrimitive } from "@/lib/chart/primitives/LabeledRayPrimitive";
import { SessionLevelsPrimitive } from "@/lib/chart/primitives/SessionLevelsPrimitive";
import { TrendLinePrimitive } from "@/lib/chart/primitives/TrendLinePrimitive";
import { VolumeProfilePrimitive } from "@/lib/chart/primitives/VolumeProfilePrimitive";
import { TimeMapper } from "@/lib/chart/timeMapper";
import { HISTORY_BARS, WS_URL } from "@/lib/config";
import { customLabel, fetchCustom, isCustom, POLL_MS } from "@/lib/customSymbols";
import { formatCompact, formatPrice, pricePrecision } from "@/lib/format";
import { isVisible, type LayerVisibility } from "@/lib/layers";
import { sessionDrawing } from "@/lib/sessionLevels";
import {
  atr,
  bollinger,
  ema,
  macd,
  parabolicSar,
  rsi,
  stochRsi,
  volumeProfile,
  vwap,
  type BandsResult,
  type MacdResult,
  type Point,
} from "@/lib/indicators";
import {
  DEFAULT_INDICATOR_SETTINGS,
  INTERVAL_SECONDS,
  TOOL_POINTS,
  type Candle,
  type ChartPoint,
  type DataSource,
  type Drawing,
  type DrawingType,
  type IndicatorSettings,
  type IndicatorState,
  type Interval,
  type KimiResult,
  type LayoutState,
  type Overlay,
  type ToolId,
} from "@/lib/types";

export interface FeedInfo {
  price: number;
  open24: number | null;
  source: DataSource;
}

export interface AgenticChartHandle {
  screenshot(): HTMLCanvasElement | null;
  getCandles(): Candle[];
  fitContent(): void;
  /** Show the crosshair at this time (another chart in the grid is being hovered); null hides it. */
  setCrosshairTime(time: number | null): void;
}

/** Another symbol drawn on top of this chart in % (TradingView's "compare"). */
export interface CompareLine {
  symbol: string;
  color: string;
}

/** Kimi Cooked parts the Layers tab can hide. */
export interface KimiVisibility {
  sr: boolean;
  fib: boolean;
  forecast: boolean;
  signals: boolean;
}

const ALL_KIMI: KimiVisibility = { sr: true, fib: true, forecast: true, signals: true };

interface Props {
  symbol: string;
  interval: Interval;
  tool: ToolId;
  magnet: boolean;
  locked: boolean;
  indicators: IndicatorState;
  layout: LayoutState;
  overlays: Overlay[];
  drawings: Drawing[];
  selectedId: string | null;
  onDrawingsChange(next: Drawing[]): void;
  onSelect(id: string | null): void;
  onToolDone(): void;
  onFeed(info: FeedInfo): void;
  onDataReady?(candles: Candle[]): void;
  onError?(message: string | null): void;
  indicatorSettings?: IndicatorSettings;
  kimiParts?: KimiVisibility;
  compare?: CompareLine[];
  /** The user hovered a bar (for crosshair sync across the grid); null when the pointer left the chart. */
  onCrosshairTime?(time: number | null): void;
  /** An alert line or zone was dragged to a new price. */
  onAlertMove?(alertId: string, patch: { price?: number; price_low?: number; price_high?: number }): void;
  /** Layers tab visibility, for the indicator layers drawn here (session levels, order-book heatmap). */
  layers?: LayerVisibility;
}

const UP = "#22c55e";
const DOWN = "#ef4444";
const TOOL_COLORS: Record<DrawingType, string> = {
  trendline: "#3b82f6",
  hray: "#a855f7",
  fib: "#f59e0b",
  rect: "#3b82f6",
  text: "#e5e7eb",
  pattern: "#14b8a6",
  ruler: "#3b82f6",
};

const toTime = (t: number) => t as UTCTimestamp;

// Kimi Cooked's label colours (TradingView's teal, maroon, blue, purple and orange).
const KIMI_MARKER: Record<string, { color: string; shape: SeriesMarker<Time>["shape"] }> = {
  "B+": { color: "#089981", shape: "arrowUp" },
  "B-": { color: "#880e4f", shape: "arrowDown" },
  U: { color: "#2962ff", shape: "arrowUp" },
  Dn: { color: "#9c27b0", shape: "arrowDown" },
  "B+?": { color: "#ff9800", shape: "arrowUp" },
  "B-?": { color: "#ff9800", shape: "arrowDown" },
};
// The backend sees a candle as closed a moment after it closes; wait that long before asking for the new run.
const KIMI_REFRESH_DELAY_MS = 6000;
const uid = () => Math.random().toString(36).slice(2, 10);

interface Legend {
  c: Candle;
  change: number;
}

// ------------------------------------------------- indicator sub-panes
// lightweight-charts v4 has no native panes, so each sub-pane indicator gets its own overlay price scale, and
// each scale is squeezed into a horizontal band of the chart with scaleMargins (fractions of the plot height,
// from top/bottom). Panes stack under the price area in this order.
const PANES = ["rsi", "macd", "atr", "stochRsi", "cvd"] as const;
type PaneKey = (typeof PANES)[number];
const PANE_SCALE: Record<PaneKey, string> = { rsi: "rsi", macd: "macd", atr: "atr", stochRsi: "stoch", cvd: "cvd" };
const RSI_COLOR = "#c084fc";
const MACD_COLOR = "#3b82f6";
const SIGNAL_COLOR = "#f97316";
const ATR_COLOR = "#f472b6";
const STOCH_K = "#38bdf8";
const STOCH_D = "#f97316";
const CVD_COLOR = "#2dd4bf";
const HIST_UP = "rgba(34,197,94,0.45)";
const HIST_DOWN = "rgba(239,68,68,0.45)";
const SUB_PAD_TOP = 0.04; // room for the pane label
const SUB_PAD_BOTTOM = 0.02;
const VOL_SHARE = 0.18; // volume band at the bottom of the main area
const COMPARE_POLL_MS = 15_000;
const CVD_REFRESH_MS = 60_000;

interface PaneLayout {
  mainBottom: number; // where the main price area ends (fraction of plot height from the top)
  height: number; // of each sub-pane
  tops: Record<PaneKey, number | null>; // top of each band, null when off
}

/** Main price area on top, then each sub-pane that is on; panes get shorter as more are added. */
function paneLayout(ind: IndicatorState): PaneLayout {
  const on = PANES.filter((k) => !!ind[k]);
  const height = on.length <= 2 ? 0.18 : on.length === 3 ? 0.15 : 0.12;
  const mainBottom = 1 - height * on.length;
  const tops = Object.fromEntries(PANES.map((k) => [k, null])) as Record<PaneKey, number | null>;
  on.forEach((k, i) => (tops[k] = mainBottom + i * height));
  return { mainBottom, height, tops };
}

/** Sets the scaleMargins of every price scale (price, "vol" and one per pane) so the panes stack without overlap. */
function layoutPanes(chart: IChartApi, ind: IndicatorState): PaneLayout {
  const l = paneLayout(ind);
  const below = 1 - l.mainBottom; // height taken by the sub-panes
  chart.priceScale("right").applyOptions({
    scaleMargins: { top: 0.08, bottom: below + l.mainBottom * (ind.volume ? 0.22 : 0.06) },
  });
  chart.priceScale("vol").applyOptions({ scaleMargins: { top: l.mainBottom * (1 - VOL_SHARE), bottom: below } });
  for (const k of PANES) {
    const t = l.tops[k] ?? 1 - l.height; // a hidden pane keeps a valid band; nothing is drawn there
    chart.priceScale(PANE_SCALE[k]).applyOptions({
      scaleMargins: { top: t + SUB_PAD_TOP, bottom: Math.max(0, 1 - t - l.height) + SUB_PAD_BOTTOM },
    });
  }
  return l;
}

/** Close time of the bar opening at `time` (months vary in length). */
function barClose(time: number, interval: Interval): number {
  if (interval !== "1M") return time + INTERVAL_SECONDS[interval];
  const d = new Date(time * 1000);
  return Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 1) / 1000;
}

/** Formatters for the time axis and crosshair label in the chosen timezone ("local", "UTC" or an IANA zone). */
function timeFormatters(tz: string | undefined) {
  const timeZone = !tz || tz === "local" ? undefined : tz;
  const mk = (o: Intl.DateTimeFormatOptions) => {
    try {
      return new Intl.DateTimeFormat("en-GB", { ...o, timeZone });
    } catch {
      return new Intl.DateTimeFormat("en-GB", o); // unknown zone: the browser's own
    }
  };
  const year = mk({ year: "numeric" });
  const month = mk({ month: "short" });
  const day = mk({ day: "numeric" });
  const hm = mk({ hour: "2-digit", minute: "2-digit", hour12: false });
  const hms = mk({ hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  const full = mk({ weekday: "short", day: "2-digit", month: "short", year: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
  const d = (t: Time) => new Date((t as number) * 1000);
  return {
    tickMarkFormatter: (t: Time, type: TickMarkType) =>
      type === TickMarkType.Year
        ? year.format(d(t))
        : type === TickMarkType.Month
          ? month.format(d(t))
          : type === TickMarkType.DayOfMonth
            ? day.format(d(t))
            : type === TickMarkType.TimeWithSeconds
              ? hms.format(d(t))
              : hm.format(d(t)),
    timeFormatter: (t: Time) => full.format(d(t)),
  };
}

/** Indicator values span ~1e-8 (MACD on micro-priced coins) to 1e4 (MACD on BTC daily). */
function formatIndicator(v: number): string {
  const a = Math.abs(v);
  if (a >= 1000) return v.toFixed(0);
  if (a >= 1) return v.toFixed(2);
  return a === 0 ? "0" : v.toPrecision(3);
}

interface PaneValues {
  rsi: string | null;
  macd: { macd: string; signal: string; hist: string; up: boolean } | null;
  atr: string | null;
  stoch: { k: string; d: string } | null;
  cvd: string | null;
}

const NO_PANE_VALUES: PaneValues = { rsi: null, macd: null, atr: null, stoch: null, cvd: null };

function PaneLabel({ top, children }: { top: number; children: ReactNode }) {
  return (
    <div className="pointer-events-none absolute inset-x-0 z-10 border-t border-line" style={{ top: Math.round(top) }}>
      <div className="flex gap-2 px-3 pt-0.5 font-mono text-[10px] leading-4 text-mute">{children}</div>
    </div>
  );
}

const AgenticChart = forwardRef<AgenticChartHandle, Props>(function AgenticChart(props, ref) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const ema20Ref = useRef<ISeriesApi<"Line"> | null>(null);
  const ema50Ref = useRef<ISeriesApi<"Line"> | null>(null);
  const psarRef = useRef<ISeriesApi<"Line"> | null>(null);
  const vwapRef = useRef<ISeriesApi<"Line"> | null>(null);
  const bbRef = useRef<{ upper: ISeriesApi<"Line">; mid: ISeriesApi<"Line">; lower: ISeriesApi<"Line"> } | null>(null);
  const rsiRef = useRef<ISeriesApi<"Line"> | null>(null);
  const macdRef = useRef<{ hist: ISeriesApi<"Histogram">; line: ISeriesApi<"Line">; signal: ISeriesApi<"Line"> } | null>(null);
  const atrRef = useRef<ISeriesApi<"Line"> | null>(null);
  const stochRef = useRef<{ k: ISeriesApi<"Line">; d: ISeriesApi<"Line"> } | null>(null);
  const cvdRef = useRef<ISeriesApi<"Line"> | null>(null);
  const compareRef = useRef(new Map<string, ISeriesApi<"Line">>());
  const filledRef = useRef(new Set<object>()); // indicator series currently holding data
  const axisMaskRef = useRef<AxisMaskPrimitive | null>(null);
  const mapperRef = useRef(new TimeMapper(INTERVAL_SECONDS[props.interval]));
  const layerRef = useRef<DrawingLayerPrimitive | null>(null);
  const countdownRef = useRef<CountdownPrimitive | null>(null);
  const profileRef = useRef<VolumeProfilePrimitive | null>(null);
  const eventsRef = useRef<EventLinesPrimitive | null>(null);
  const heatmapRef = useRef<HeatmapPrimitive | null>(null);
  const sessionsRef = useRef<SessionLevelsPrimitive | null>(null);
  const overlayPrims = useRef<Array<BoxZonePrimitive | LabeledRayPrimitive | TrendLinePrimitive>>([]);
  const kimiPrimRef = useRef<KimiPrimitive | null>(null);
  const markersRef = useRef<{ ai: SeriesMarker<Time>[]; kimi: SeriesMarker<Time>[] }>({ ai: [], kimi: [] });
  const kimiTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const rightOffsetRef = useRef(12); // bars of empty space right of the last candle (more for Kimi's forecast)
  const candlesRef = useRef<Candle[]>([]);
  const pendingRef = useRef<ChartPoint[]>([]);
  const dragRef = useRef<{ id: string; start: ChartPoint; orig: ChartPoint[] } | null>(null);
  const alertDragRef = useRef<{ prim: LabeledRayPrimitive | BoxZonePrimitive; alertId: string; startPrice: number; orig: number[] } | null>(null);
  const tapRef = useRef<((x: number, y: number) => void) | null>(null);
  const pressRef = useRef<{ x: number; y: number; moved: boolean } | null>(null);
  const feedThrottle = useRef(0);
  const syncingRef = useRef(false); // the crosshair is being moved by another chart: don't echo it back

  // Latest props for long-lived chart callbacks.
  const propsRef = useRef(props);
  propsRef.current = props;
  const settings = props.indicatorSettings ?? DEFAULT_INDICATOR_SETTINGS;
  const settingsRef = useRef(settings);
  settingsRef.current = settings;
  const kimiParts = props.kimiParts ?? ALL_KIMI;
  const custom = isCustom(props.symbol);

  const [legend, setLegend] = useState<Legend | null>(null);
  const [eventTip, setEventTip] = useState<{ x: number; y: number; item: EventLine } | null>(null);
  const [textInput, setTextInput] = useState<{ x: number; y: number; point: ChartPoint } | null>(null);
  const [loading, setLoading] = useState(true);
  const [plotHeight, setPlotHeight] = useState(0); // container minus time axis, for sub-pane labels
  const [paneVals, setPaneVals] = useState<PaneValues>(NO_PANE_VALUES);
  const [kimi, setKimi] = useState<{ data: KimiResult | null; loading: boolean; error: string | null }>({
    data: null,
    loading: false,
    error: null,
  });
  const [kimiTick, setKimiTick] = useState(0); // bumped when a candle closes, to fetch the next run
  const [barCount, setBarCount] = useState(0); // bumped when a bar opens (CVD refresh)
  const [cvdError, setCvdError] = useState<string | null>(null);
  const [compareChange, setCompareChange] = useState<Record<string, number>>({});
  const [alertHover, setAlertHover] = useState(false);

  useImperativeHandle(ref, () => ({
    screenshot: () => chartRef.current?.takeScreenshot() ?? null,
    getCandles: () => candlesRef.current,
    fitContent: () => chartRef.current?.timeScale().fitContent(),
    setCrosshairTime: (time: number | null) => {
      const chart = chartRef.current;
      const series = candleRef.current;
      if (!chart || !series) return;
      syncingRef.current = true;
      try {
        if (time === null) chart.clearCrosshairPosition();
        else {
          const t = mapperRef.current.barTimeAtOrBefore(time);
          const bar = candlesRef.current.find((c) => c.time === t);
          if (bar) chart.setCrosshairPosition(bar.close, toTime(bar.time), series);
        }
      } finally {
        syncingRef.current = false;
      }
    },
  }));

  // ------------------------------------------------------------- create
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const chart = createChart(el, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: "#0b0e14" },
        textColor: "#8b93a1",
        fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif",
        fontSize: 11,
      },
      grid: { vertLines: { color: "#161b24" }, horzLines: { color: "#161b24" } },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: "#4b5563", labelBackgroundColor: "#1f2633" },
        horzLine: { color: "#4b5563", labelBackgroundColor: "#1f2633" },
      },
      rightPriceScale: { borderColor: "#1f2633", scaleMargins: { top: 0.08, bottom: 0.22 } },
      timeScale: { borderColor: "#1f2633", timeVisible: true, secondsVisible: false, rightOffset: 12 },
    });
    const candles = chart.addCandlestickSeries({
      upColor: UP,
      downColor: DOWN,
      borderVisible: false,
      wickUpColor: UP,
      wickDownColor: DOWN,
    });
    const volume = chart.addHistogramSeries({
      priceFormat: { type: "volume" },
      priceScaleId: "vol",
      lastValueVisible: false,
      priceLineVisible: false,
    });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    const lineOpts = { lineWidth: 1 as const, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };
    const s0 = settingsRef.current;
    const e20 = chart.addLineSeries({ ...lineOpts, color: s0.ema1.color });
    const e50 = chart.addLineSeries({ ...lineOpts, color: s0.ema2.color });
    const psar = chart.addLineSeries({
      ...lineOpts,
      color: "#e5e7eb",
      lineVisible: false,
      pointMarkersVisible: true,
      pointMarkersRadius: 1.5,
    });
    const vwapLine = chart.addLineSeries({ ...lineOpts, color: s0.vwapColor });
    const bb = {
      upper: chart.addLineSeries({ ...lineOpts, color: s0.bb.color }),
      mid: chart.addLineSeries({ ...lineOpts, color: s0.bb.color, lineStyle: LineStyle.Dashed }),
      lower: chart.addLineSeries({ ...lineOpts, color: s0.bb.color }),
    };
    // Sub-pane series sit on their own overlay scales; their last values and the
    // RSI 70/30 levels are labelled on the right axis, inside their band.
    const rsiLine = chart.addLineSeries({
      ...lineOpts,
      color: RSI_COLOR,
      priceScaleId: "rsi",
      lastValueVisible: true,
      priceFormat: { type: "price", precision: 1, minMove: 0.1 },
      autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 100 } }),
    });
    for (const price of [70, 30]) {
      rsiLine.createPriceLine({ price, color: "#6b7280", lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: true, title: "" });
    }
    const indFormat = { type: "custom" as const, formatter: formatIndicator, minMove: 1e-10 };
    const macdHist = chart.addHistogramSeries({
      priceScaleId: "macd",
      priceFormat: indFormat,
      lastValueVisible: false,
      priceLineVisible: false,
    });
    const macdLine = chart.addLineSeries({ ...lineOpts, color: MACD_COLOR, priceScaleId: "macd", priceFormat: indFormat, lastValueVisible: true });
    const macdSignal = chart.addLineSeries({ ...lineOpts, color: SIGNAL_COLOR, priceScaleId: "macd", priceFormat: indFormat, lastValueVisible: true });
    const atrLine = chart.addLineSeries({ ...lineOpts, color: ATR_COLOR, priceScaleId: "atr", priceFormat: indFormat, lastValueVisible: true });
    const stochK = chart.addLineSeries({
      ...lineOpts,
      color: STOCH_K,
      priceScaleId: "stoch",
      lastValueVisible: true,
      priceFormat: { type: "price", precision: 1, minMove: 0.1 },
      autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 100 } }),
    });
    for (const price of [80, 20]) {
      stochK.createPriceLine({ price, color: "#6b7280", lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: true, title: "" });
    }
    const stochD = chart.addLineSeries({
      ...lineOpts,
      color: STOCH_D,
      priceScaleId: "stoch",
      autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 100 } }),
    });
    const cvdLine = chart.addLineSeries({ ...lineOpts, color: CVD_COLOR, priceScaleId: "cvd", priceFormat: indFormat, lastValueVisible: true });

    // First primitive on the series: clears the shared label registry at the start of every paint.
    candles.attachPrimitive(new LabelResetPrimitive(mapperRef.current));
    const heatmap = new HeatmapPrimitive(mapperRef.current); // first of the bottom layer: behind everything else
    candles.attachPrimitive(heatmap);
    const sessions = new SessionLevelsPrimitive(mapperRef.current);
    candles.attachPrimitive(sessions);
    const axisMask = new AxisMaskPrimitive("#0b0e14"); // hides price ticks inside the sub-panes
    candles.attachPrimitive(axisMask);
    const profile = new VolumeProfilePrimitive(mapperRef.current);
    candles.attachPrimitive(profile);
    const events = new EventLinesPrimitive(mapperRef.current, []);
    candles.attachPrimitive(events);
    const countdown = new CountdownPrimitive(mapperRef.current);
    candles.attachPrimitive(countdown);

    const layer = new DrawingLayerPrimitive(mapperRef.current);
    candles.attachPrimitive(layer);

    chartRef.current = chart;
    candleRef.current = candles;
    volumeRef.current = volume;
    ema20Ref.current = e20;
    ema50Ref.current = e50;
    psarRef.current = psar;
    vwapRef.current = vwapLine;
    bbRef.current = bb;
    rsiRef.current = rsiLine;
    macdRef.current = { hist: macdHist, line: macdLine, signal: macdSignal };
    atrRef.current = atrLine;
    stochRef.current = { k: stochK, d: stochD };
    cvdRef.current = cvdLine;
    axisMaskRef.current = axisMask;
    layerRef.current = layer;
    countdownRef.current = countdown;
    profileRef.current = profile;
    eventsRef.current = events;
    heatmapRef.current = heatmap;
    sessionsRef.current = sessions;

    // Sub-pane labels are placed in px: track the plot height (container minus time axis).
    // The time axis only gets its height on the first paint, hence the size-change hook too.
    const measure = () => setPlotHeight(Math.max(0, el.clientHeight - chart.timeScale().height()));
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    chart.timeScale().subscribeSizeChange(measure);
    const filled = filledRef.current;
    const compares = compareRef.current;

    return () => {
      ro.disconnect();
      chart.timeScale().unsubscribeSizeChange(measure);
      filled.clear();
      compares.clear();
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
      layerRef.current = null;
      countdownRef.current = null;
      profileRef.current = null;
      eventsRef.current = null;
      heatmapRef.current = null;
      sessionsRef.current = null;
      overlayPrims.current = [];
      kimiPrimRef.current = null;
    };
  }, []);

  // ------------------------------------------------------------ helpers
  const toPoint = useCallback((x: number, y: number): ChartPoint | null => {
    const chart = chartRef.current;
    const series = candleRef.current;
    if (!chart || !series) return null;
    const time = mapperRef.current.xToTime(chart, x);
    const price = series.coordinateToPrice(y);
    if (time === null || price === null) return null;
    let pt: ChartPoint = { time, price };
    if (propsRef.current.magnet) {
      const idx = mapperRef.current.barIndexNear(time);
      const bar = candlesRef.current[idx];
      if (bar) {
        const nearest = [bar.open, bar.high, bar.low, bar.close].reduce((best, p) => {
          const d = Math.abs((series.priceToCoordinate(p) ?? 0) - y);
          return d < best.d ? { p, d } : best;
        }, { p: price as number, d: Infinity });
        pt = { time: bar.time, price: nearest.p };
      }
    }
    return pt;
  }, []);

  const refreshIndicators = useCallback((full: boolean) => {
    const data = candlesRef.current;
    if (!data.length) return;
    const ind = propsRef.current.indicators;
    const st = settingsRef.current;
    const filled = filledRef.current;
    // Computed lazily (and once) so indicators that are off cost nothing.
    let rsiPts: Point[] | undefined;
    let macdRes: MacdResult | undefined;
    let bands: BandsResult | undefined;
    let atrPts: Point[] | undefined;
    let stoch: { k: Point[]; d: Point[] } | undefined;
    const getRsi = () => (rsiPts ??= rsi(data, st.rsi.length));
    const getMacd = () => (macdRes ??= macd(data, st.macd.fast, st.macd.slow, st.macd.signal));
    const getBands = () => (bands ??= bollinger(data, st.bb.length, st.bb.mult));
    const getAtr = () => (atrPts ??= atr(data, st.atr.length));
    const getStoch = () => (stoch ??= stochRsi(data, st.stochRsi.rsiLength, st.stochRsi.stochLength, st.stochRsi.k, st.stochRsi.d));
    const histColor = (v: number) => (v >= 0 ? HIST_UP : HIST_DOWN);
    const m = macdRef.current;
    const bb = bbRef.current;
    const sk = stochRef.current;
    const feeds: Array<[ISeriesApi<"Line" | "Histogram"> | null, boolean, () => Point[], ((v: number) => string)?]> = [
      [ema20Ref.current, ind.ema20, () => ema(data, st.ema1.length)],
      [ema50Ref.current, ind.ema50, () => ema(data, st.ema2.length)],
      [psarRef.current, ind.psar, () => parabolicSar(data)],
      [vwapRef.current, !!ind.vwap, () => vwap(data, INTERVAL_SECONDS[propsRef.current.interval])],
      [bb?.upper ?? null, !!ind.bb, () => getBands().upper],
      [bb?.mid ?? null, !!ind.bb, () => getBands().mid],
      [bb?.lower ?? null, !!ind.bb, () => getBands().lower],
      [rsiRef.current, !!ind.rsi, getRsi],
      [m?.hist ?? null, !!ind.macd, () => getMacd().hist, histColor],
      [m?.line ?? null, !!ind.macd, () => getMacd().macd],
      [m?.signal ?? null, !!ind.macd, () => getMacd().signal],
      [atrRef.current, !!ind.atr, getAtr],
      [sk?.k ?? null, !!ind.stochRsi, () => getStoch().k],
      [sk?.d ?? null, !!ind.stochRsi, () => getStoch().d],
    ];
    for (const [series, on, compute, color] of feeds) {
      if (!series) continue;
      if (!on) {
        // Drop data of switched-off series on a full refresh so stale points from
        // another symbol or timeframe don't linger on the shared time scale.
        if (full && filled.delete(series)) series.setData([]);
        continue;
      }
      const pts = compute();
      const toData = (p: Point) => ({ time: toTime(p.time), value: p.value, ...(color && { color: color(p.value) }) });
      if (full) {
        series.setData(pts.map(toData));
        filled.add(series);
      } else if (pts.length) series.update(toData(pts[pts.length - 1]));
    }

    const lastOf = (pts: Point[]) => pts[pts.length - 1]?.value;
    const r = ind.rsi ? lastOf(getRsi()) : undefined;
    const mv = ind.macd ? lastOf(getMacd().macd) : undefined;
    const sv = ind.macd ? lastOf(getMacd().signal) : undefined;
    const hv = ind.macd ? lastOf(getMacd().hist) : undefined;
    const av = ind.atr ? lastOf(getAtr()) : undefined;
    const kv = ind.stochRsi ? lastOf(getStoch().k) : undefined;
    const dv = ind.stochRsi ? lastOf(getStoch().d) : undefined;
    const lastClose = data[data.length - 1].close;
    setPaneVals((prev) => {
      const next: PaneValues = {
        ...prev,
        rsi: r === undefined ? null : r.toFixed(1),
        macd:
          mv === undefined || sv === undefined || hv === undefined
            ? null
            : { macd: formatIndicator(mv), signal: formatIndicator(sv), hist: formatIndicator(hv), up: hv >= 0 },
        atr: av === undefined ? null : `${formatIndicator(av)} (${((av / lastClose) * 100).toFixed(2)}%)`,
        stoch: kv === undefined || dv === undefined ? null : { k: kv.toFixed(1), d: dv.toFixed(1) },
      };
      return JSON.stringify(prev) === JSON.stringify(next) ? prev : next;
    });
  }, []);

  /** AI-overlay markers and Kimi's labels share the series' single marker list. */
  const applyMarkers = useCallback(() => {
    const { ai, kimi: k } = markersRef.current;
    const all = [...ai, ...k].sort((a, b) => (a.time as number) - (b.time as number));
    candleRef.current?.setMarkers(all);
  }, []);

  const emitFeed = useCallback((source: DataSource, force = false) => {
    const data = candlesRef.current;
    const last = data[data.length - 1];
    if (last) countdownRef.current?.set(last.close, barClose(last.time, propsRef.current.interval), last.close >= last.open);
    const now = performance.now();
    if (!force && now - feedThrottle.current < 250) return;
    feedThrottle.current = now;
    if (!last) return;
    const dayAgo = last.time - 86400;
    const ref = data.find((c) => c.time >= dayAgo);
    propsRef.current.onFeed({ price: last.close, open24: ref ? ref.open : null, source });
  }, []);

  // --------------------------------------------------------- data feed
  useEffect(() => {
    const { symbol, interval } = props;
    const abort = new AbortController();
    let ws: WebSocket | null = null;
    let retry = 0;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let pingTimer: ReturnType<typeof setInterval> | undefined;
    let pollTimer: ReturnType<typeof setInterval> | undefined;
    let source: DataSource = "connecting";
    let disposed = false;
    const customSymbol = isCustom(symbol);

    setLoading(true);
    setLegend(null);
    pendingRef.current = [];
    layerRef.current?.setPreview(null);

    const applyCandle = (c: Candle) => {
      const data = candlesRef.current;
      const last = data[data.length - 1];
      if (last && c.time < last.time) return; // stale
      if (last && c.time === last.time) data[data.length - 1] = c;
      else {
        data.push(c);
        mapperRef.current.setData(data.map((d) => d.time), INTERVAL_SECONDS[interval]);
        setBarCount((n) => n + 1);
        if (last && propsRef.current.indicators.kimi) {
          // The previous candle just closed: Kimi Cooked has a new run.
          clearTimeout(kimiTimer.current);
          kimiTimer.current = setTimeout(() => setKimiTick((t) => t + 1), KIMI_REFRESH_DELAY_MS);
        }
      }
      candleRef.current?.update({ time: toTime(c.time), open: c.open, high: c.high, low: c.low, close: c.close });
      volumeRef.current?.update({
        time: toTime(c.time),
        value: c.volume,
        color: c.close >= c.open ? "rgba(34,197,94,0.35)" : "rgba(239,68,68,0.35)",
      });
      refreshIndicators(!last || c.time !== last.time);
      emitFeed(source);
    };

    const connect = () => {
      if (disposed) return;
      const q = new URLSearchParams({ symbol, interval });
      ws = new WebSocket(`${WS_URL}/ws/klines?${q}`);
      ws.onopen = () => {
        retry = 0;
        pingTimer = setInterval(() => ws?.readyState === WebSocket.OPEN && ws.send('{"type":"ping"}'), 25_000);
      };
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.type === "status") {
          source = msg.source;
          emitFeed(source, true);
        } else if (msg.type === "kline" && msg.symbol === symbol && msg.interval === interval) {
          source = msg.source;
          applyCandle(msg.candle as Candle);
        }
      };
      ws.onclose = () => {
        clearInterval(pingTimer);
        if (disposed) return;
        source = "reconnecting";
        emitFeed(source, true);
        retryTimer = setTimeout(connect, Math.min(1000 * 2 ** retry++, 15_000));
      };
    };

    // Ratios and indexes have no stream: poll their newest bars.
    const poll = () => {
      pollTimer = setInterval(() => {
        fetchCustom(symbol, interval, 3, abort.signal)
          .then((r) => r.candles.forEach(applyCandle))
          .catch(() => undefined);
      }, POLL_MS);
    };

    const load = async () => {
      // Puts a full dataset on the chart: the cached bars at once, then the network's.
      const render = (data: Candle[], src: DataSource) => {
        candlesRef.current = data;
        const lastBar = data[data.length - 1];
        setLegend(lastBar ? { c: lastBar, change: ((lastBar.close - lastBar.open) / lastBar.open) * 100 } : null);
        mapperRef.current.setData(data.map((d) => d.time), INTERVAL_SECONDS[interval]);
        const precision = pricePrecision(data[data.length - 1]?.close ?? 1);
        candleRef.current?.applyOptions({
          priceFormat: { type: "price", precision, minMove: 1 / 10 ** precision },
        });
        candleRef.current?.setData(
          data.map((c) => ({ time: toTime(c.time), open: c.open, high: c.high, low: c.low, close: c.close })),
        );
        volumeRef.current?.setData(
          data.map((c) => ({
            time: toTime(c.time),
            value: c.volume,
            color: c.close >= c.open ? "rgba(34,197,94,0.35)" : "rgba(239,68,68,0.35)",
          })),
        );
        refreshIndicators(true);
        chartRef.current
          ?.timeScale()
          .setVisibleLogicalRange({ from: Math.max(0, data.length - 160), to: data.length - 2 + rightOffsetRef.current });
        source = src;
        emitFeed(source, true);
        setBarCount(data.length);
        setLoading(false);
      };

      try {
        if (customSymbol) {
          const res = await fetchCustom(symbol, interval, HISTORY_BARS, abort.signal);
          if (disposed) return;
          render(res.candles, (res.source === "binance" ? "client" : "synthetic") as DataSource);
          propsRef.current.onError?.(null);
          propsRef.current.onDataReady?.(res.candles);
          poll();
          return;
        }
        // A retry keeps what an earlier attempt drew from the cache rather than re-reading it.
        let cached: Candle[] | null = candlesRef.current.length ? candlesRef.current : null;
        if (!cached) {
          const hit = await readCandles(symbol, interval);
          if (disposed) return;
          const last = hit?.candles[hit.candles.length - 1];
          const gap = last ? Date.now() / 1000 - last.time : Infinity;
          // Only real Binance bars, and only when the missing ones fit in one request.
          if (hit?.source === "binance" && gap < HISTORY_BARS * INTERVAL_SECONDS[interval]) {
            cached = hit.candles;
            render(cached, "connecting");
          }
        }

        let data: Candle[] | null = null;
        let src = "";
        if (cached) {
          // Ask only for the bars from the newest cached one on (it may have been open) and merge them in.
          const since = cached[cached.length - 1].time;
          const delta = await fetchKlines(symbol, interval, HISTORY_BARS, abort.signal, since);
          const first = delta.candles[0]?.time;
          if (delta.source === "binance" && first !== undefined && first <= since) {
            data = mergeCandles(cached, delta.candles);
            src = delta.source;
          }
        }
        if (!data) {
          const res = await fetchKlines(symbol, interval, HISTORY_BARS, abort.signal);
          data = res.candles;
          src = res.source;
        }
        if (disposed) return;
        render(data, src as DataSource);
        propsRef.current.onError?.(null);
        propsRef.current.onDataReady?.(data);
        connect();
        if (src === "binance") void writeCandles(symbol, interval, data, src);
      } catch (err) {
        if (disposed || (err as Error).name === "AbortError") return;
        setLoading(false);
        propsRef.current.onFeed({ price: NaN, open24: null, source: "offline" });
        propsRef.current.onError?.((err as Error).message);
        retryTimer = setTimeout(load, 5000); // keep retrying until the API is up
      }
    };
    load();

    return () => {
      disposed = true;
      abort.abort();
      clearTimeout(retryTimer);
      clearTimeout(kimiTimer.current);
      clearInterval(pingTimer);
      clearInterval(pollTimer);
      if (ws) {
        ws.onclose = null;
        ws.close();
      }
      candlesRef.current = [];
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.symbol, props.interval]);

  // -------------------------------------------------- indicator toggles
  useEffect(() => {
    const ind = props.indicators;
    ema20Ref.current?.applyOptions({ visible: ind.ema20, color: settings.ema1.color });
    ema50Ref.current?.applyOptions({ visible: ind.ema50, color: settings.ema2.color });
    psarRef.current?.applyOptions({ visible: ind.psar });
    volumeRef.current?.applyOptions({ visible: ind.volume });
    vwapRef.current?.applyOptions({ visible: !!ind.vwap, color: settings.vwapColor });
    const bb = bbRef.current;
    for (const s of bb ? [bb.upper, bb.mid, bb.lower] : []) s.applyOptions({ visible: !!ind.bb, color: settings.bb.color });
    rsiRef.current?.applyOptions({ visible: !!ind.rsi });
    const m = macdRef.current;
    for (const s of m ? [m.hist, m.line, m.signal] : []) s.applyOptions({ visible: !!ind.macd });
    atrRef.current?.applyOptions({ visible: !!ind.atr });
    const sk = stochRef.current;
    for (const s of sk ? [sk.k, sk.d] : []) s.applyOptions({ visible: !!ind.stochRsi });
    cvdRef.current?.applyOptions({ visible: !!ind.cvd });
    if (chartRef.current) axisMaskRef.current?.setFrom(layoutPanes(chartRef.current, ind).mainBottom);
    refreshIndicators(true);
  }, [props.indicators, settings, refreshIndicators]);

  useEffect(() => {
    const compare = (props.compare ?? []).length > 0;
    chartRef.current?.applyOptions({
      grid: {
        vertLines: { visible: props.layout.grid },
        horzLines: { visible: props.layout.grid },
      },
      rightPriceScale: {
        mode: compare ? PriceScaleMode.Percentage : props.layout.logScale ? PriceScaleMode.Logarithmic : PriceScaleMode.Normal,
      },
    });
  }, [props.layout.grid, props.layout.logScale, props.compare]);

  // Timezone of the time axis and crosshair label.
  useEffect(() => {
    const f = timeFormatters(props.layout.timezone);
    chartRef.current?.applyOptions({
      localization: { timeFormatter: f.timeFormatter },
      timeScale: { tickMarkFormatter: f.tickMarkFormatter },
    });
  }, [props.layout.timezone]);

  // Candle countdown on the price axis, ticking every second.
  const countdownOn = props.layout.countdown !== false;
  useEffect(() => {
    const cd = countdownRef.current;
    if (!cd) return;
    if (!countdownOn) {
      cd.set(null, 0, true);
      return;
    }
    const last = candlesRef.current[candlesRef.current.length - 1];
    if (last) cd.set(last.close, barClose(last.time, props.interval), last.close >= last.open);
    const id = setInterval(() => cd.tick(), 1000);
    return () => clearInterval(id);
  }, [countdownOn, props.interval, loading]);
  useEffect(() => {
    if (!countdownOn) countdownRef.current?.set(null, 0, true);
  });

  // ------------------------------------------------ volume profile (visible range)
  const profileOn = !!props.indicators.vprofile;
  useEffect(() => {
    const chart = chartRef.current;
    const prim = profileRef.current;
    if (!chart || !prim) return;
    if (!profileOn) {
      prim.set([], null);
      return;
    }
    let frame = 0;
    const update = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const range = chart.timeScale().getVisibleLogicalRange();
        const data = candlesRef.current;
        if (!range || !data.length) return;
        const from = Math.max(0, Math.floor(range.from));
        const to = Math.min(data.length - 1, Math.ceil(range.to));
        const vp = volumeProfile(data.slice(from, to + 1));
        prim.set(vp?.bins ?? [], vp?.poc ?? null);
      });
    };
    update();
    chart.timeScale().subscribeVisibleLogicalRangeChange(update);
    return () => {
      cancelAnimationFrame(frame);
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(update);
    };
  }, [profileOn, loading, barCount]);

  // ------------------------------------------------ CVD (from the backend: needs taker-buy volume)
  const cvdOn = !!props.indicators.cvd;
  useEffect(() => {
    const series = cvdRef.current;
    if (!series) return;
    if (!cvdOn || custom) {
      series.setData([]);
      setPaneVals((v) => ({ ...v, cvd: null }));
      setCvdError(cvdOn && custom ? "not available for ratio and index charts" : null);
      return;
    }
    const abort = new AbortController();
    const load = () => {
      const q = new URLSearchParams({ symbol: props.symbol, interval: props.interval, limit: String(HISTORY_BARS) });
      apiRequest<{ rows: { time: number; delta: number; cvd: number }[]; source: string }>(`/api/cvd?${q}`, { signal: abort.signal })
        .then((r) => {
          series.setData(r.rows.map((row) => ({ time: toTime(row.time), value: row.cvd })));
          const last = r.rows[r.rows.length - 1];
          const dayAgo = r.rows.find((row) => row.time >= (last?.time ?? 0) - 86400);
          setPaneVals((v) => ({
            ...v,
            cvd: last ? `${formatCompact(last.cvd)} · 24h ${formatCompact(last.cvd - (dayAgo?.cvd ?? last.cvd))}` : null,
          }));
          setCvdError(r.source === "binance" ? null : "demo data");
        })
        .catch((err: Error) => {
          if (err.name !== "AbortError") setCvdError("unavailable");
        });
    };
    load();
    const id = setInterval(load, CVD_REFRESH_MS);
    return () => {
      abort.abort();
      clearInterval(id);
    };
    // barCount: a new bar opened, so the previous one's final delta is in
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cvdOn, custom, props.symbol, props.interval, Math.floor(barCount / 1)]);

  // ------------------------------------------------ compare symbols (in %)
  const compareKey = (props.compare ?? []).map((c) => `${c.symbol}:${c.color}`).join(",");
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const map = compareRef.current;
    const want = new Map((props.compare ?? []).map((c) => [c.symbol, c]));
    for (const [sym, series] of map) {
      if (!want.has(sym)) {
        chart.removeSeries(series);
        map.delete(sym);
      }
    }
    const abort = new AbortController();
    const load = (full: boolean) => {
      for (const c of want.values()) {
        let series = map.get(c.symbol);
        if (!series) {
          series = chart.addLineSeries({ color: c.color, lineWidth: 2, priceLineVisible: false, lastValueVisible: true, title: customLabel(c.symbol) });
          map.set(c.symbol, series);
        } else series.applyOptions({ color: c.color });
        const s = series;
        const req = isCustom(c.symbol)
          ? fetchCustom(c.symbol, props.interval, full ? HISTORY_BARS : 3, abort.signal)
          : fetchKlines(c.symbol, props.interval, full ? HISTORY_BARS : 3, abort.signal);
        req
          .then((r) => {
            const pts = r.candles.map((k) => ({ time: toTime(k.time), value: k.close }));
            if (full) s.setData(pts);
            else pts.forEach((pt) => s.update(pt));
            const first = r.candles[0]?.close;
            const last = r.candles[r.candles.length - 1]?.close;
            if (full && first && last) setCompareChange((m) => ({ ...m, [c.symbol]: (last / first - 1) * 100 }));
          })
          .catch(() => undefined);
      }
    };
    load(true);
    const id = want.size ? setInterval(() => load(false), COMPARE_POLL_MS) : undefined;
    return () => {
      abort.abort();
      clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [compareKey, props.interval, loading]);

  // ---------------------------------------------------- AI overlays
  useEffect(() => {
    const series = candleRef.current;
    if (!series) return;
    for (const p of overlayPrims.current) series.detachPrimitive(p);
    overlayPrims.current = [];
    const markers: SeriesMarker<Time>[] = [];
    const mapper = mapperRef.current;
    for (const ov of props.overlays) {
      if (ov.type === "box") overlayPrims.current.push(new BoxZonePrimitive(mapper, ov));
      else if (ov.type === "horizontal_line") overlayPrims.current.push(new LabeledRayPrimitive(mapper, ov));
      else if (ov.type === "trendline") overlayPrims.current.push(new TrendLinePrimitive(mapper, ov));
      else if (ov.type === "marker") {
        const t = mapper.barTimeAtOrBefore(ov.time);
        if (t !== undefined)
          markers.push({
            time: toTime(t),
            position: ov.position === "above" ? "aboveBar" : "belowBar",
            shape: ov.shape,
            color: ov.color,
            text: ov.label,
            size: 0.6,
          });
      }
    }
    for (const p of overlayPrims.current) series.attachPrimitive(p);
    markersRef.current.ai = markers;
    applyMarkers();
  }, [props.overlays, loading, applyMarkers]);

  // ---------------------------------------------------- economic events and news (Calendar tab → "Show on chart")
  const eventItems = useChartEvents(custom ? "BTCUSDT" : props.symbol);
  useEffect(() => {
    eventsRef.current?.setItems(eventItems);
  }, [eventItems]);

  // ---------------------------------------------------- session / period levels and the order-book heatmap
  const layers = props.layers ?? {};
  const sessionsOn = !!props.indicators.sessions && !custom && isVisible(layers, "sessions");
  const sessionSettings = settings.sessions ?? DEFAULT_INDICATOR_SETTINGS.sessions;
  const levels = useSessionLevels(props.symbol, props.interval, sessionsOn, sessionSettings.orMinutes);
  useEffect(() => {
    sessionsRef.current?.set(sessionDrawing(sessionsOn ? levels.data : null, sessionSettings));
  }, [levels.data, sessionsOn, sessionSettings]);

  const heatmapOn = !!props.indicators.heatmap && !custom && isVisible(layers, "heatmap");
  const heat = useOrderbookHeatmap(props.symbol, props.interval, heatmapOn);
  useEffect(() => {
    heatmapRef.current?.set(heatmapOn ? heat.data : null);
  }, [heat.data, heatmapOn]);
  const chips: { text: string; demo: boolean }[] = [];
  if (sessionsOn && (levels.error || levels.data?.source === "synthetic"))
    chips.push({ text: levels.error ? `Session levels: ${levels.error}` : "Session levels: demo data", demo: !levels.error });
  if (heatmapOn && heat.error) chips.push({ text: `Heatmap: ${heat.error}`, demo: false });
  else if (heatmapOn && heat.data?.source === "synthetic") chips.push({ text: "Heatmap: demo order book", demo: true });
  else if (heatmapOn && heat.data?.startedAt)
    chips.push({
      text: `Order book since ${new Date(heat.data.startedAt * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`,
      demo: false,
    });

  // ---------------------------------------------------- Kimi Cooked
  const kimiOn = !!props.indicators.kimi && !custom;
  useEffect(() => {
    if (!kimiOn) {
      setKimi({ data: null, loading: false, error: null });
      return;
    }
    const abort = new AbortController();
    const { symbol, interval } = props;
    // Keep the drawing of the same chart while the next run loads; drop another chart's.
    setKimi((k) => ({
      data: k.data && k.data.symbol === symbol && k.data.interval === interval ? k.data : null,
      loading: true,
      error: null,
    }));
    fetchKimi(symbol, interval, abort.signal)
      .then((data) => setKimi({ data, loading: false, error: null }))
      .catch((err: Error) => {
        if (err.name === "AbortError") return;
        setKimi((k) => ({ ...k, loading: false, error: err.message }));
      });
    return () => abort.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kimiOn, props.symbol, props.interval, kimiTick]);

  const kimiPartsKey = `${kimiParts.sr}${kimiParts.fib}${kimiParts.forecast}${kimiParts.signals}`;
  useEffect(() => {
    const series = candleRef.current;
    if (!series) return;
    if (kimiPrimRef.current) series.detachPrimitive(kimiPrimRef.current);
    kimiPrimRef.current = null;
    markersRef.current.kimi = [];
    const data = kimi.data;
    if (data && !loading && data.symbol === props.symbol && data.interval === props.interval) {
      const prim = new KimiPrimitive(mapperRef.current, data, kimiParts);
      series.attachPrimitive(prim);
      kimiPrimRef.current = prim;
      // Kimi runs on more history than the chart loads; labels older than the first loaded candle are left out.
      const first = candlesRef.current[0]?.time ?? Infinity;
      markersRef.current.kimi = !kimiParts.signals
        ? []
        : data.signals.filter((s) => s.time >= first).map((s) => {
            const style = KIMI_MARKER[s.text] ?? { color: "#9ca3af", shape: "circle" as const };
            return {
              time: toTime(s.time),
              position: s.direction === "long" ? "belowBar" : "aboveBar",
              shape: style.shape,
              color: style.color,
              text: s.text,
              size: s.type === "U/Dn" ? 0.6 : 0.8,
            };
          });
    }
    applyMarkers();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kimi.data, loading, props.symbol, props.interval, applyMarkers, kimiPartsKey]);

  // Room on the right for the forecast and its label while Kimi Cooked is on.
  const kimiHorizon = kimiOn && kimiParts.forecast ? (kimi.data?.forecast?.horizon ?? 0) : 0;
  useEffect(() => {
    const ts = chartRef.current?.timeScale();
    if (!ts) return;
    const offset = kimiHorizon ? kimiHorizon + 36 : 12;
    rightOffsetRef.current = offset;
    ts.applyOptions({ rightOffset: offset });
    ts.scrollToPosition(offset, false);
  }, [kimiHorizon]);

  // ---------------------------------------------------- user drawings
  useEffect(() => {
    layerRef.current?.setDrawings(props.drawings);
  }, [props.drawings]);

  useEffect(() => {
    layerRef.current?.setSelected(props.selectedId);
  }, [props.selectedId]);

  useEffect(() => {
    pendingRef.current = [];
    layerRef.current?.setPreview(null);
    setTextInput(null);
    chartRef.current?.applyOptions({
      crosshair: { mode: props.magnet ? CrosshairMode.Magnet : CrosshairMode.Normal },
    });
  }, [props.tool, props.magnet]);

  // Click: place points for the active tool, or select in crosshair mode.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;

    // Taps come from our own pointer handling (below) rather than
    // chart.subscribeClick: LWC turns a quick second click into a dblclick,
    // which would swallow the second point of a two-click drawing.
    tapRef.current = (x: number, y: number) => {
      const p = propsRef.current;
      if (p.tool === "crosshair") {
        const width = chart.timeScale().width();
        p.onSelect(p.locked ? null : (layerRef.current?.pick(x, y, width) ?? null));
        return;
      }
      if (p.locked) return;
      const pt = toPoint(x, y);
      if (!pt) return;
      const type = p.tool as DrawingType;
      if (type === "text") {
        setTextInput({ x, y, point: pt });
        return;
      }
      if (type === "ruler" && pendingRef.current.length === 0) {
        // A finished ruler is replaced by the next measurement.
        p.onDrawingsChange(p.drawings.filter((d) => d.type !== "ruler"));
      }
      pendingRef.current = [...pendingRef.current, pt];
      if (pendingRef.current.length >= TOOL_POINTS[type]) {
        const drawing: Drawing = { id: uid(), type, points: pendingRef.current, color: TOOL_COLORS[type] };
        pendingRef.current = [];
        layerRef.current?.setPreview(null);
        p.onDrawingsChange([...propsRef.current.drawings.filter((d) => type !== "ruler" || d.type !== "ruler"), drawing]);
        if (type !== "ruler") p.onSelect(drawing.id);
        p.onToolDone();
      }
    };

    const onMove = (param: MouseEventParams<Time>) => {
      const p = propsRef.current;
      if (param.point && param.time !== undefined) {
        const idx = candlesRef.current.findIndex((c) => c.time === (param.time as number));
        const c = candlesRef.current[idx];
        if (c) setLegend({ c, change: ((c.close - c.open) / c.open) * 100 });
      } else if (!param.point) {
        const data = candlesRef.current;
        const c = data[data.length - 1];
        setLegend(c ? { c, change: ((c.close - c.open) / c.open) * 100 } : null);
      }
      const tip = param.point ? eventsRef.current?.itemAt(param.point.x, param.point.y) : null;
      setEventTip((cur) => (tip ? (cur?.item === tip ? cur : { x: param.point!.x, y: param.point!.y, item: tip }) : null));
      if (!syncingRef.current) p.onCrosshairTime?.(param.point && param.time !== undefined ? (param.time as number) : null);
      if (p.tool === "crosshair" || !pendingRef.current.length || !param.point) return;
      const pt = toPoint(param.point.x, param.point.y);
      if (!pt) return;
      const type = p.tool as DrawingType;
      layerRef.current?.setPreview({ id: "preview", type, points: [...pendingRef.current, pt], color: TOOL_COLORS[type] });
    };

    chart.subscribeCrosshairMove(onMove);
    return () => {
      tapRef.current = null;
      chart.unsubscribeCrosshairMove(onMove);
    };
  }, [toPoint]);

  // Drag-to-move the selected drawing, or an alert line / zone (crosshair tool, unlocked).
  useEffect(() => {
    const el = containerRef.current;
    const chart = chartRef.current;
    if (!el || !chart) return;
    const local = (e: PointerEvent) => {
      const r = el.getBoundingClientRect();
      return { x: e.clientX - r.left, y: e.clientY - r.top };
    };
    const inPane = (x: number, y: number) =>
      x >= 0 && y >= 0 && x <= chart.timeScale().width() && y <= el.clientHeight - chart.timeScale().height();
    /** The alert primitive under y, if any: lines within 5 px, zones anywhere inside. */
    const alertAt = (y: number) => {
      const series = candleRef.current;
      if (!series || !propsRef.current.onAlertMove) return null;
      for (const prim of overlayPrims.current) {
        if (prim instanceof LabeledRayPrimitive && prim.line.kind === "alert") {
          const py = series.priceToCoordinate(prim.line.price);
          if (py !== null && Math.abs(py - y) <= 5) return prim;
        } else if (prim instanceof BoxZonePrimitive && prim.box.kind === "alert") {
          const t = series.priceToCoordinate(prim.box.price_high);
          const b = series.priceToCoordinate(prim.box.price_low);
          if (t !== null && b !== null && y >= Math.min(t, b) - 3 && y <= Math.max(t, b) + 3) return prim;
        }
      }
      return null;
    };
    const down = (e: PointerEvent) => {
      const p = propsRef.current;
      if (e.button !== 0) return;
      const { x, y } = local(e);
      pressRef.current = inPane(x, y) ? { x, y, moved: false } : null;
      if (p.tool !== "crosshair" || p.locked || !inPane(x, y)) return;
      const id = layerRef.current?.pick(x, y, chart.timeScale().width());
      if (!id) {
        const prim = alertAt(y);
        const price = candleRef.current?.coordinateToPrice(y);
        if (prim && price != null) {
          const alertId = (prim instanceof LabeledRayPrimitive ? prim.line.id : prim.box.id)?.replace(/^alert-/, "") ?? "";
          const orig = prim instanceof LabeledRayPrimitive ? [prim.line.price] : [prim.box.price_low, prim.box.price_high];
          alertDragRef.current = { prim, alertId, startPrice: price as number, orig };
          chart.applyOptions({ handleScroll: false, handleScale: false });
        }
        return;
      }
      const d = p.drawings.find((dr) => dr.id === id);
      const start = toPoint(x, y);
      if (!d || !start) return;
      dragRef.current = { id, start, orig: d.points };
      p.onSelect(id);
      chart.applyOptions({ handleScroll: false, handleScale: false });
    };
    const move = (e: PointerEvent) => {
      const press = pressRef.current;
      if (press && !press.moved) {
        const { x, y } = local(e);
        press.moved = Math.hypot(x - press.x, y - press.y) > 4;
      }
      const ad = alertDragRef.current;
      if (ad) {
        const price = candleRef.current?.coordinateToPrice(local(e).y);
        if (price == null) return;
        const dp = (price as number) - ad.startPrice;
        if (ad.prim instanceof LabeledRayPrimitive) ad.prim.line = { ...ad.prim.line, price: ad.orig[0] + dp };
        else ad.prim.box = { ...ad.prim.box, price_low: ad.orig[0] + dp, price_high: ad.orig[1] + dp };
        ad.prim.requestUpdate();
        return;
      }
      const drag = dragRef.current;
      if (!drag) {
        if (!pressRef.current && propsRef.current.tool === "crosshair") {
          const { x, y } = local(e);
          setAlertHover(inPane(x, y) && !!alertAt(y));
        }
        return;
      }
      const { x, y } = local(e);
      const cur = toPoint(x, y);
      if (!cur) return;
      const mapper = mapperRef.current;
      const dl = mapper.timeToLogical(cur.time) - mapper.timeToLogical(drag.start.time);
      const dp = cur.price - drag.start.price;
      const p = propsRef.current;
      p.onDrawingsChange(
        p.drawings.map((d) =>
          d.id === drag.id
            ? {
                ...d,
                points: drag.orig.map((pt) => ({
                  time: mapper.logicalToTime(mapper.timeToLogical(pt.time) + dl),
                  price: pt.price + dp,
                })),
              }
            : d,
        ),
      );
    };
    const up = (e: PointerEvent) => {
      const press = pressRef.current;
      pressRef.current = null;
      const ad = alertDragRef.current;
      if (ad) {
        alertDragRef.current = null;
        chart.applyOptions({ handleScroll: true, handleScale: true });
        if (press?.moved) {
          // Round to what the price axis shows, so the alert reads "7.3000", not "7.299642…".
          const round = (v: number) => Number(v.toFixed(pricePrecision(v)));
          const patch =
            ad.prim instanceof LabeledRayPrimitive
              ? { price: round(ad.prim.line.price) }
              : { price_low: round(ad.prim.box.price_low), price_high: round(ad.prim.box.price_high) };
          propsRef.current.onAlertMove?.(ad.alertId, patch);
        }
        return;
      }
      if (dragRef.current) {
        dragRef.current = null;
        chart.applyOptions({ handleScroll: true, handleScale: true });
        return;
      }
      if (press && !press.moved && e.button === 0) tapRef.current?.(press.x, press.y);
    };
    el.addEventListener("pointerdown", down, { capture: true });
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    return () => {
      el.removeEventListener("pointerdown", down, { capture: true });
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
  }, [toPoint]);

  // Escape cancels an in-progress drawing.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      pendingRef.current = [];
      layerRef.current?.setPreview(null);
      setTextInput(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const commitText = (value: string) => {
    if (textInput && value.trim()) {
      const d: Drawing = { id: uid(), type: "text", points: [textInput.point], color: TOOL_COLORS.text, text: value.trim() };
      props.onDrawingsChange([...props.drawings, d]);
      props.onSelect(d.id);
    }
    setTextInput(null);
    props.onToolDone();
  };

  const lg = legend?.c;
  const panes = paneLayout(props.indicators);
  const paneTop = (k: PaneKey) => (plotHeight > 0 && panes.tops[k] !== null ? panes.tops[k]! * plotHeight : null);
  const st = settings;
  return (
    <div className="relative h-full w-full">
      <div
        ref={containerRef}
        className={`h-full w-full ${props.tool !== "crosshair" ? "cursor-crosshair" : alertHover ? "cursor-ns-resize" : ""}`}
      />
      {lg && (
        <div className="pointer-events-none absolute left-3 top-2 z-10 flex flex-wrap gap-x-3 font-mono text-[11px] text-mute">
          {(["open", "high", "low", "close"] as const).map((k) => (
            <span key={k}>
              {k[0].toUpperCase()}{" "}
              <span className={lg.close >= lg.open ? "text-up" : "text-down"}>{formatPrice(lg[k])}</span>
            </span>
          ))}
          <span className={legend!.change >= 0 ? "text-up" : "text-down"}>
            {legend!.change >= 0 ? "+" : ""}
            {legend!.change.toFixed(2)}%
          </span>
          <span>
            Vol <span className="text-ink">{formatCompact(lg.volume)}</span>
          </span>
          {(props.compare ?? []).map((c) => (
            <span key={c.symbol} style={{ color: c.color }}>
              {customLabel(c.symbol)}
              {compareChange[c.symbol] !== undefined && ` ${compareChange[c.symbol] >= 0 ? "+" : ""}${compareChange[c.symbol].toFixed(2)}%`}
            </span>
          ))}
        </div>
      )}
      {paneTop("rsi") !== null && (
        <PaneLabel top={paneTop("rsi")!}>
          <span>RSI {st.rsi.length}</span>
          {paneVals.rsi && <span style={{ color: RSI_COLOR }}>{paneVals.rsi}</span>}
        </PaneLabel>
      )}
      {paneTop("macd") !== null && (
        <PaneLabel top={paneTop("macd")!}>
          <span>MACD {st.macd.fast} {st.macd.slow} {st.macd.signal}</span>
          {paneVals.macd && (
            <>
              <span style={{ color: MACD_COLOR }}>{paneVals.macd.macd}</span>
              <span style={{ color: SIGNAL_COLOR }}>{paneVals.macd.signal}</span>
              <span className={paneVals.macd.up ? "text-up" : "text-down"}>{paneVals.macd.hist}</span>
            </>
          )}
        </PaneLabel>
      )}
      {paneTop("atr") !== null && (
        <PaneLabel top={paneTop("atr")!}>
          <span>ATR {st.atr.length}</span>
          {paneVals.atr && <span style={{ color: ATR_COLOR }}>{paneVals.atr}</span>}
        </PaneLabel>
      )}
      {paneTop("stochRsi") !== null && (
        <PaneLabel top={paneTop("stochRsi")!}>
          <span>
            Stoch RSI {st.stochRsi.rsiLength} {st.stochRsi.stochLength} {st.stochRsi.k} {st.stochRsi.d}
          </span>
          {paneVals.stoch && (
            <>
              <span style={{ color: STOCH_K }}>{paneVals.stoch.k}</span>
              <span style={{ color: STOCH_D }}>{paneVals.stoch.d}</span>
            </>
          )}
        </PaneLabel>
      )}
      {paneTop("cvd") !== null && (
        <PaneLabel top={paneTop("cvd")!}>
          <span>CVD (taker buy − sell)</span>
          {paneVals.cvd && <span style={{ color: CVD_COLOR }}>{paneVals.cvd}</span>}
          {cvdError && <span className="text-yellow-300">{cvdError}</span>}
        </PaneLabel>
      )}
      {chips.length > 0 && (
        <div className={`pointer-events-none absolute left-3 z-10 flex flex-wrap gap-1 ${kimiOn ? "top-12" : "top-7"}`}>
          {chips.map((c) => (
            <span
              key={c.text}
              className={`rounded px-1.5 py-0.5 text-[10px] font-semibold ${c.demo ? "bg-yellow-400/15 text-yellow-300" : "bg-panel/80 text-mute"}`}
            >
              {c.text}
            </span>
          ))}
        </div>
      )}
      {eventTip && (
        <div
          className="pointer-events-none absolute z-20 max-w-72 rounded border border-line bg-panel px-2 py-1 text-[11px] text-ink shadow-lg"
          style={{ left: Math.max(4, eventTip.x - 140), top: eventTip.y + 14 }}
        >
          {eventTip.item.title ?? eventTip.item.label}
        </div>
      )}
      {kimiOn && <KimiPanel data={kimi.data} loading={kimi.loading} error={kimi.error} />}
      {loading && (
        <div className="pointer-events-none absolute inset-0 z-10 grid place-items-center text-sm text-mute">
          Loading candles…
        </div>
      )}
      {textInput && (
        <input
          autoFocus
          placeholder="Note text, Enter to place"
          className="absolute z-20 w-56 rounded border border-accent bg-panel px-2 py-1 text-xs text-ink outline-none"
          style={{ left: textInput.x, top: textInput.y - 14 }}
          onKeyDown={(e) => {
            if (e.key === "Enter") commitText((e.target as HTMLInputElement).value);
            if (e.key === "Escape") setTextInput(null);
          }}
          onBlur={(e) => commitText(e.target.value)}
        />
      )}
    </div>
  );
});

export default AgenticChart;
