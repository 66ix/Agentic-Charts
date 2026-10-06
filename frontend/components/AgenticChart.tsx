"use client";

import {
  ColorType,
  createChart,
  CrosshairMode,
  LineStyle,
  PriceScaleMode,
  type IChartApi,
  type ISeriesApi,
  type MouseEventParams,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState, type ReactNode } from "react";

import { fetchKlines } from "@/lib/api";
import { mergeCandles, readCandles, writeCandles } from "@/lib/candleCache";
import { AxisMaskPrimitive } from "@/lib/chart/primitives/AxisMaskPrimitive";
import { BoxZonePrimitive } from "@/lib/chart/primitives/BoxZonePrimitive";
import { DrawingLayerPrimitive } from "@/lib/chart/primitives/DrawingLayerPrimitive";
import { LabeledRayPrimitive } from "@/lib/chart/primitives/LabeledRayPrimitive";
import { TrendLinePrimitive } from "@/lib/chart/primitives/TrendLinePrimitive";
import { TimeMapper } from "@/lib/chart/timeMapper";
import { HISTORY_BARS, WS_URL } from "@/lib/config";
import { formatCompact, formatPrice, pricePrecision } from "@/lib/format";
import { ema, macd, parabolicSar, rsi, vwap, type MacdResult, type Point } from "@/lib/indicators";
import {
  INTERVAL_SECONDS,
  TOOL_POINTS,
  type Candle,
  type ChartPoint,
  type DataSource,
  type Drawing,
  type DrawingType,
  type IndicatorState,
  type Interval,
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
}

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
const uid = () => Math.random().toString(36).slice(2, 10);

interface Legend {
  c: Candle;
  change: number;
}

// ------------------------------------------------- indicator sub-panes
// lightweight-charts v4 has no native panes, so RSI and MACD get their own
// overlay price scales, and each scale is squeezed into a horizontal band of
// the chart with scaleMargins (fractions of the plot height, from top/bottom).
const RSI_PERIOD = 14;
const MACD_PARAMS = [12, 26, 9] as const;
const VWAP_COLOR = "#22d3ee";
const RSI_COLOR = "#c084fc";
const MACD_COLOR = "#3b82f6";
const SIGNAL_COLOR = "#f97316";
const HIST_UP = "rgba(34,197,94,0.45)";
const HIST_DOWN = "rgba(239,68,68,0.45)";
const SUB_PANE = 0.18; // height of each sub-pane
const SUB_PAD_TOP = 0.04; // room for the pane label
const SUB_PAD_BOTTOM = 0.02;
const VOL_SHARE = 0.18; // volume band at the bottom of the main area

interface PaneLayout {
  mainBottom: number; // where the main price area ends (fraction of plot height from the top)
  rsi: number | null; // top of the RSI band, null when off
  macd: number | null; // top of the MACD band, null when off
}

/** Main price area on top, then RSI, then MACD, each sub-pane SUB_PANE tall when on. */
function paneLayout(ind: IndicatorState): PaneLayout {
  const mainBottom = 1 - SUB_PANE * (Number(!!ind.rsi) + Number(!!ind.macd));
  const rsiTop = ind.rsi ? mainBottom : null;
  const macdTop = ind.macd ? mainBottom + (ind.rsi ? SUB_PANE : 0) : null;
  return { mainBottom, rsi: rsiTop, macd: macdTop };
}

/** Sets the scaleMargins of every price scale (price, "vol", "rsi", "macd") so the panes stack without overlap. */
function layoutPanes(chart: IChartApi, ind: IndicatorState): PaneLayout {
  const l = paneLayout(ind);
  const below = 1 - l.mainBottom; // height taken by the sub-panes
  chart.priceScale("right").applyOptions({
    scaleMargins: { top: 0.08, bottom: below + l.mainBottom * (ind.volume ? 0.22 : 0.06) },
  });
  chart.priceScale("vol").applyOptions({ scaleMargins: { top: l.mainBottom * (1 - VOL_SHARE), bottom: below } });
  const band = (top: number | null) => {
    const t = top ?? 1 - SUB_PANE; // a hidden pane keeps a valid band; nothing is drawn there
    return { top: t + SUB_PAD_TOP, bottom: Math.max(0, 1 - t - SUB_PANE) + SUB_PAD_BOTTOM };
  };
  chart.priceScale("rsi").applyOptions({ scaleMargins: band(l.rsi) });
  chart.priceScale("macd").applyOptions({ scaleMargins: band(l.macd) });
  return l;
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
}

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
  const rsiRef = useRef<ISeriesApi<"Line"> | null>(null);
  const macdRef = useRef<{ hist: ISeriesApi<"Histogram">; line: ISeriesApi<"Line">; signal: ISeriesApi<"Line"> } | null>(null);
  const filledRef = useRef(new Set<object>()); // indicator series currently holding data
  const axisMaskRef = useRef<AxisMaskPrimitive | null>(null);
  const mapperRef = useRef(new TimeMapper(INTERVAL_SECONDS[props.interval]));
  const layerRef = useRef<DrawingLayerPrimitive | null>(null);
  const overlayPrims = useRef<Array<BoxZonePrimitive | LabeledRayPrimitive | TrendLinePrimitive>>([]);
  const candlesRef = useRef<Candle[]>([]);
  const pendingRef = useRef<ChartPoint[]>([]);
  const dragRef = useRef<{ id: string; start: ChartPoint; orig: ChartPoint[] } | null>(null);
  const tapRef = useRef<((x: number, y: number) => void) | null>(null);
  const pressRef = useRef<{ x: number; y: number; moved: boolean } | null>(null);
  const feedThrottle = useRef(0);

  // Latest props for long-lived chart callbacks.
  const propsRef = useRef(props);
  propsRef.current = props;

  const [legend, setLegend] = useState<Legend | null>(null);
  const [textInput, setTextInput] = useState<{ x: number; y: number; point: ChartPoint } | null>(null);
  const [loading, setLoading] = useState(true);
  const [plotHeight, setPlotHeight] = useState(0); // container minus time axis, for sub-pane labels
  const [paneVals, setPaneVals] = useState<PaneValues>({ rsi: null, macd: null });

  useImperativeHandle(ref, () => ({
    screenshot: () => chartRef.current?.takeScreenshot() ?? null,
    getCandles: () => candlesRef.current,
    fitContent: () => chartRef.current?.timeScale().fitContent(),
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
    const e20 = chart.addLineSeries({ ...lineOpts, color: "#f59e0b" });
    const e50 = chart.addLineSeries({ ...lineOpts, color: "#a855f7" });
    const psar = chart.addLineSeries({
      ...lineOpts,
      color: "#e5e7eb",
      lineVisible: false,
      pointMarkersVisible: true,
      pointMarkersRadius: 1.5,
    });
    const vwapLine = chart.addLineSeries({ ...lineOpts, color: VWAP_COLOR });
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
    const macdFormat = { type: "custom" as const, formatter: formatIndicator, minMove: 1e-10 };
    const macdHist = chart.addHistogramSeries({
      priceScaleId: "macd",
      priceFormat: macdFormat,
      lastValueVisible: false,
      priceLineVisible: false,
    });
    const macdLine = chart.addLineSeries({ ...lineOpts, color: MACD_COLOR, priceScaleId: "macd", priceFormat: macdFormat, lastValueVisible: true });
    const macdSignal = chart.addLineSeries({ ...lineOpts, color: SIGNAL_COLOR, priceScaleId: "macd", priceFormat: macdFormat, lastValueVisible: true });

    const axisMask = new AxisMaskPrimitive("#0b0e14"); // hides price ticks inside the sub-panes
    candles.attachPrimitive(axisMask);

    const layer = new DrawingLayerPrimitive(mapperRef.current);
    candles.attachPrimitive(layer);

    chartRef.current = chart;
    candleRef.current = candles;
    volumeRef.current = volume;
    ema20Ref.current = e20;
    ema50Ref.current = e50;
    psarRef.current = psar;
    vwapRef.current = vwapLine;
    rsiRef.current = rsiLine;
    macdRef.current = { hist: macdHist, line: macdLine, signal: macdSignal };
    axisMaskRef.current = axisMask;
    layerRef.current = layer;

    // Sub-pane labels are placed in px: track the plot height (container minus time axis).
    // The time axis only gets its height on the first paint, hence the size-change hook too.
    const measure = () => setPlotHeight(Math.max(0, el.clientHeight - chart.timeScale().height()));
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    chart.timeScale().subscribeSizeChange(measure);
    const filled = filledRef.current;

    return () => {
      ro.disconnect();
      chart.timeScale().unsubscribeSizeChange(measure);
      filled.clear();
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
      layerRef.current = null;
      overlayPrims.current = [];
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
    const filled = filledRef.current;
    // Computed lazily (and once) so indicators that are off cost nothing.
    let rsiPts: Point[] | undefined;
    let macdRes: MacdResult | undefined;
    const getRsi = () => (rsiPts ??= rsi(data, RSI_PERIOD));
    const getMacd = () => (macdRes ??= macd(data, ...MACD_PARAMS));
    const histColor = (v: number) => (v >= 0 ? HIST_UP : HIST_DOWN);
    const m = macdRef.current;
    const feeds: Array<[ISeriesApi<"Line" | "Histogram"> | null, boolean, () => Point[], ((v: number) => string)?]> = [
      [ema20Ref.current, ind.ema20, () => ema(data, 20)],
      [ema50Ref.current, ind.ema50, () => ema(data, 50)],
      [psarRef.current, ind.psar, () => parabolicSar(data)],
      [vwapRef.current, !!ind.vwap, () => vwap(data, INTERVAL_SECONDS[propsRef.current.interval])],
      [rsiRef.current, !!ind.rsi, getRsi],
      [m?.hist ?? null, !!ind.macd, () => getMacd().hist, histColor],
      [m?.line ?? null, !!ind.macd, () => getMacd().macd],
      [m?.signal ?? null, !!ind.macd, () => getMacd().signal],
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
    const next: PaneValues = {
      rsi: r === undefined ? null : r.toFixed(1),
      macd:
        mv === undefined || sv === undefined || hv === undefined
          ? null
          : { macd: formatIndicator(mv), signal: formatIndicator(sv), hist: formatIndicator(hv), up: hv >= 0 },
    };
    setPaneVals((prev) => (JSON.stringify(prev) === JSON.stringify(next) ? prev : next));
  }, []);

  const emitFeed = useCallback((source: DataSource, force = false) => {
    const now = performance.now();
    if (!force && now - feedThrottle.current < 250) return;
    feedThrottle.current = now;
    const data = candlesRef.current;
    const last = data[data.length - 1];
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
    let source: DataSource = "connecting";
    let disposed = false;

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
          .setVisibleLogicalRange({ from: Math.max(0, data.length - 160), to: data.length + 10 });
        source = src;
        emitFeed(source, true);
        setLoading(false);
      };

      try {
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
      clearInterval(pingTimer);
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
    const { ema20, ema50, psar, volume } = props.indicators;
    ema20Ref.current?.applyOptions({ visible: ema20 });
    ema50Ref.current?.applyOptions({ visible: ema50 });
    psarRef.current?.applyOptions({ visible: psar });
    volumeRef.current?.applyOptions({ visible: volume });
    vwapRef.current?.applyOptions({ visible: !!props.indicators.vwap });
    rsiRef.current?.applyOptions({ visible: !!props.indicators.rsi });
    const m = macdRef.current;
    for (const s of m ? [m.hist, m.line, m.signal] : []) s.applyOptions({ visible: !!props.indicators.macd });
    if (chartRef.current) axisMaskRef.current?.setFrom(layoutPanes(chartRef.current, props.indicators).mainBottom);
    refreshIndicators(true);
  }, [props.indicators, refreshIndicators]);

  useEffect(() => {
    chartRef.current?.applyOptions({
      grid: {
        vertLines: { visible: props.layout.grid },
        horzLines: { visible: props.layout.grid },
      },
      rightPriceScale: { mode: props.layout.logScale ? PriceScaleMode.Logarithmic : PriceScaleMode.Normal },
    });
  }, [props.layout.grid, props.layout.logScale]);

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
    markers.sort((a, b) => (a.time as number) - (b.time as number));
    series.setMarkers(markers);
  }, [props.overlays, loading]);

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

  // Drag-to-move the selected drawing (crosshair tool, unlocked).
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
    const down = (e: PointerEvent) => {
      const p = propsRef.current;
      if (e.button !== 0) return;
      const { x, y } = local(e);
      pressRef.current = inPane(x, y) ? { x, y, moved: false } : null;
      if (p.tool !== "crosshair" || p.locked) return;
      const id = layerRef.current?.pick(x, y, chart.timeScale().width());
      if (!id) return;
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
      const drag = dragRef.current;
      if (!drag) return;
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
  return (
    <div className="relative h-full w-full">
      <div ref={containerRef} className={`h-full w-full ${props.tool !== "crosshair" ? "cursor-crosshair" : ""}`} />
      {lg && (
        <div className="pointer-events-none absolute left-3 top-2 z-10 flex gap-3 font-mono text-[11px] text-mute">
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
        </div>
      )}
      {plotHeight > 0 && panes.rsi !== null && (
        <PaneLabel top={panes.rsi * plotHeight}>
          <span>RSI {RSI_PERIOD}</span>
          {paneVals.rsi && <span style={{ color: RSI_COLOR }}>{paneVals.rsi}</span>}
        </PaneLabel>
      )}
      {plotHeight > 0 && panes.macd !== null && (
        <PaneLabel top={panes.macd * plotHeight}>
          <span>MACD {MACD_PARAMS.join(" ")}</span>
          {paneVals.macd && (
            <>
              <span style={{ color: MACD_COLOR }}>{paneVals.macd.macd}</span>
              <span style={{ color: SIGNAL_COLOR }}>{paneVals.macd.signal}</span>
              <span className={paneVals.macd.up ? "text-up" : "text-down"}>{paneVals.macd.hist}</span>
            </>
          )}
        </PaneLabel>
      )}
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
