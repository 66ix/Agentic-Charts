"use client";

import clsx from "clsx";
import {
  Camera,
  ChevronDown,
  Columns2,
  Divide,
  Grid2x2,
  Keyboard,
  LineChart,
  Maximize2,
  Plus,
  Search,
  Settings,
  SlidersHorizontal,
  Square,
  X,
} from "lucide-react";

import { customLabel, INDEXES, isCustom } from "@/lib/customSymbols";
import { formatPct, formatPrice } from "@/lib/format";
import { TIMEFRAMES, type DataSource, type GridMode, type IndicatorSettings, type IndicatorState, type Interval } from "@/lib/types";

import type { CompareLine } from "./AgenticChart";
import LayoutsMenu from "./LayoutsMenu";
import { Menu, MenuToggle } from "./Menu";

const SOURCE_BADGE: Record<DataSource, { text: string; cls: string }> = {
  binance: { text: "LIVE", cls: "bg-up/15 text-up" },
  synthetic: { text: "DEMO DATA", cls: "bg-yellow-400/15 text-yellow-300" },
  client: { text: "LIVE", cls: "bg-up/15 text-up" },
  connecting: { text: "CONNECTING", cls: "bg-panel2 text-mute" },
  reconnecting: { text: "RECONNECTING", cls: "bg-orange-400/15 text-orange-300" },
  offline: { text: "OFFLINE", cls: "bg-down/15 text-down" },
};

export const COMPARE_COLORS = ["#f472b6", "#38bdf8", "#facc15", "#a3e635", "#fb923c"];

interface Props {
  symbol: string;
  interval: Interval;
  price: number | null;
  change24: number | null;
  source: DataSource;
  indicators: IndicatorState;
  indicatorSettings: IndicatorSettings;
  compare: CompareLine[];
  watchlist: string[];
  mobile: boolean;
  gridMode: GridMode;
  onInterval(i: Interval): void;
  onSearch(): void;
  onIndicators(next: IndicatorState): void;
  onIndicatorSettings(): void;
  onCompare(next: CompareLine[]): void;
  /** Open the symbol search to pick a coin to compare. */
  onCompareSearch(): void;
  /** Make the active chart a ratio or index chart. */
  onOpenSymbol(symbol: string): void;
  onScreenshot(): void;
  onFit(): void;
  onSettings(): void;
  onShortcuts(): void;
  onGridMode(mode: GridMode): void;
}

const GRID_MODES: Array<{ mode: GridMode; label: string; Icon: typeof Square }> = [
  { mode: 1, label: "One chart", Icon: Square },
  { mode: 2, label: "Two charts", Icon: Columns2 },
  { mode: 4, label: "Four charts", Icon: Grid2x2 },
];

export default function ChartHeader(p: Props) {
  const badge = SOURCE_BADGE[p.source] ?? SOURCE_BADGE.connecting;
  const ind = p.indicators;
  const st = p.indicatorSettings;
  const set = (patch: Partial<IndicatorState>) => p.onIndicators({ ...ind, ...patch });
  const comparing = new Set(p.compare.map((c) => c.symbol));
  const addCompare = (symbol: string) => {
    if (comparing.has(symbol) || symbol === p.symbol || p.compare.length >= COMPARE_COLORS.length) return;
    const used = new Set(p.compare.map((c) => c.color));
    p.onCompare([...p.compare, { symbol, color: COMPARE_COLORS.find((c) => !used.has(c)) ?? COMPARE_COLORS[0] }]);
  };
  const custom = isCustom(p.symbol);

  return (
    <div className="flex h-11 shrink-0 items-center gap-1 overflow-x-auto border-b border-line bg-panel px-2 scrollbar-none">
      <button type="button" onClick={p.onSearch} className="btn-ghost shrink-0 gap-2 px-2 text-sm" title="Change coin (S)">
        <span className="font-semibold text-ink">{customLabel(p.symbol)}</span>
        {!p.mobile && <span className="text-xs text-mute">{custom ? (p.symbol.startsWith("INDEX:") ? "Index" : "Ratio") : "Binance"}</span>}
        <ChevronDown className="h-3.5 w-3.5 text-mute" />
      </button>

      <div className="mr-2 flex shrink-0 items-baseline gap-2 font-mono text-sm">
        <span className="text-ink">{p.price != null && Number.isFinite(p.price) ? formatPrice(p.price) : "—"}</span>
        {p.change24 != null && <span className={clsx("text-xs", p.change24 >= 0 ? "text-up" : "text-down")}>{formatPct(p.change24)}</span>}
        <span className={clsx("rounded px-1.5 py-0.5 font-sans text-[10px] font-semibold tracking-wide", badge.cls)}>{badge.text}</span>
      </div>

      <div className="mx-1 h-5 w-px shrink-0 bg-line" />

      <div className="flex shrink-0 items-center" role="tablist" aria-label="Timeframe">
        {TIMEFRAMES.map((tf, i) => (
          <button
            key={tf.value}
            type="button"
            role="tab"
            aria-selected={p.interval === tf.value}
            title={`${tf.label} (${(i + 1) % 10})`}
            onClick={() => p.onInterval(tf.value)}
            className={clsx(
              "h-7 rounded px-2 text-xs font-medium transition-colors",
              p.interval === tf.value ? "bg-accent/15 text-accent" : "text-mute hover:bg-panel2 hover:text-ink",
            )}
          >
            {tf.label}
          </button>
        ))}
      </div>

      <div className="mx-1 h-5 w-px shrink-0 bg-line" />

      <Menu
        title="Indicators"
        trigger={
          <>
            <LineChart className="h-4 w-4" />
            <span className="hidden text-xs sm:inline">Indicators</span>
          </>
        }
      >
        <MenuToggle label={`EMA ${st.ema1.length}`} checked={ind.ema20} onChange={(v) => set({ ema20: v })} />
        <MenuToggle label={`EMA ${st.ema2.length}`} checked={ind.ema50} onChange={(v) => set({ ema50: v })} />
        <MenuToggle label="Bollinger Bands" hint={`${st.bb.length} ${st.bb.mult}`} checked={!!ind.bb} onChange={(v) => set({ bb: v })} />
        <MenuToggle label="VWAP" checked={!!ind.vwap} onChange={(v) => set({ vwap: v })} />
        <MenuToggle label="Parabolic SAR" checked={ind.psar} onChange={(v) => set({ psar: v })} />
        <MenuToggle label="Volume" checked={ind.volume} onChange={(v) => set({ volume: v })} />
        <MenuToggle label="Volume profile" hint="visible range" checked={!!ind.vprofile} onChange={(v) => set({ vprofile: v })} />
        <div className="my-1 h-px bg-line" />
        <MenuToggle label="RSI" hint={`${st.rsi.length}`} checked={!!ind.rsi} onChange={(v) => set({ rsi: v })} />
        <MenuToggle label="MACD" hint={`${st.macd.fast} ${st.macd.slow} ${st.macd.signal}`} checked={!!ind.macd} onChange={(v) => set({ macd: v })} />
        <MenuToggle label="Stoch RSI" checked={!!ind.stochRsi} onChange={(v) => set({ stochRsi: v })} />
        <MenuToggle label="ATR" hint={`${st.atr.length}`} checked={!!ind.atr} onChange={(v) => set({ atr: v })} />
        <MenuToggle label="CVD" hint="buy − sell volume" checked={!!ind.cvd} onChange={(v) => set({ cvd: v })} />
        <div className="my-1 h-px bg-line" />
        <MenuToggle label="Kimi Cooked" hint="v5.7.4" checked={!!ind.kimi} onChange={(v) => set({ kimi: v })} />
        <button type="button" onClick={p.onIndicatorSettings} className="mt-1 flex w-full items-center gap-1.5 rounded px-2 py-1.5 text-left text-xs text-ink hover:bg-panel2">
          <SlidersHorizontal className="h-3.5 w-3.5" /> Lengths and colours… <span className="ml-auto text-[10px] text-mute">I</span>
        </button>
      </Menu>

      <Menu
        title="Compare coins"
        trigger={
          <>
            <Plus className="h-4 w-4" />
            <span className="hidden text-xs sm:inline">Compare</span>
            {p.compare.length > 0 && <span className="rounded bg-accent/20 px-1 text-[10px] text-accent">{p.compare.length}</span>}
          </>
        }
      >
        {p.compare.map((c) => (
          <div key={c.symbol} className="flex items-center gap-1.5 rounded px-2 py-1 text-xs">
            <span className="h-2.5 w-2.5 rounded-full" style={{ background: c.color }} />
            <span className="flex-1 text-ink">{customLabel(c.symbol)}</span>
            {!custom && !isCustom(c.symbol) && (
              <button type="button" title={`Chart ${customLabel(p.symbol)} ÷ ${customLabel(c.symbol)}`} className="btn-ghost h-6 w-6 p-0" onClick={() => p.onOpenSymbol(`${p.symbol}/${c.symbol}`)}>
                <Divide className="h-3.5 w-3.5" />
              </button>
            )}
            <button type="button" aria-label={`Stop comparing ${c.symbol}`} className="btn-ghost h-6 w-6 p-0" onClick={() => p.onCompare(p.compare.filter((x) => x.symbol !== c.symbol))}>
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        ))}
        {p.compare.length > 0 && <p className="px-2 pb-1 text-[10px] text-mute">The price scale shows % change while comparing.</p>}
        <div className="px-2 pb-0.5 pt-1 text-[10px] font-semibold uppercase tracking-wide text-mute">Add</div>
        <div className="flex flex-wrap gap-1 px-2 pb-1">
          {[...p.watchlist.filter((s) => s !== p.symbol).slice(0, 8), ...INDEXES.map((i) => i.symbol)].map((s) => (
            <button
              key={s}
              type="button"
              disabled={comparing.has(s)}
              onClick={() => addCompare(s)}
              className="rounded border border-line px-1.5 py-0.5 text-[11px] text-ink hover:border-accent disabled:opacity-40"
            >
              {customLabel(s)}
            </button>
          ))}
        </div>
        <button type="button" onClick={p.onCompareSearch} className="flex w-full items-center gap-1.5 rounded px-2 py-1.5 text-left text-xs text-ink hover:bg-panel2">
          <Search className="h-3.5 w-3.5" /> Find another coin…
        </button>
      </Menu>

      {!p.mobile && <LayoutsMenu />}

      {!p.mobile && (
        <div className="flex shrink-0 items-center" role="radiogroup" aria-label="Chart grid">
          {GRID_MODES.map(({ mode, label, Icon }) => (
            <button
              key={mode}
              type="button"
              role="radio"
              aria-checked={p.gridMode === mode}
              title={`${label} (G)`}
              aria-label={label}
              onClick={() => p.onGridMode(mode)}
              className={clsx("btn-ghost", p.gridMode === mode && "bg-panel2 text-ink")}
            >
              <Icon className="h-4 w-4" />
            </button>
          ))}
        </div>
      )}

      <button type="button" className="btn-ghost shrink-0" title="Fit chart (Alt+R)" aria-label="Fit chart" onClick={p.onFit}>
        <Maximize2 className="h-4 w-4" />
      </button>
      <button type="button" className="btn-ghost shrink-0" title="Save screenshot (Alt+S)" aria-label="Save screenshot" onClick={p.onScreenshot}>
        <Camera className="h-4 w-4" />
      </button>

      <div className="flex-1" />

      {!p.mobile && (
        <button type="button" className="btn-ghost shrink-0" title="Keyboard shortcuts (?)" aria-label="Keyboard shortcuts" onClick={p.onShortcuts}>
          <Keyboard className="h-4 w-4" />
        </button>
      )}
      <button type="button" className="btn-ghost shrink-0" title="Settings" aria-label="Settings" onClick={p.onSettings}>
        <Settings className="h-4 w-4" />
      </button>
    </div>
  );
}
