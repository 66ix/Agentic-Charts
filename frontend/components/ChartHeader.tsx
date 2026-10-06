"use client";

import clsx from "clsx";
import { Bell, Bot, Camera, ChevronDown, Columns2, Grid2x2, LayoutGrid, LineChart, List, Maximize2, Search, Square } from "lucide-react";

import { displaySymbol, formatPct, formatPrice } from "@/lib/format";
import { TIMEFRAMES, type DataSource, type GridMode, type IndicatorState, type Interval, type LayoutState } from "@/lib/types";

import { Menu, MenuToggle } from "./Menu";

const SOURCE_BADGE: Record<DataSource, { text: string; cls: string }> = {
  binance: { text: "LIVE", cls: "bg-up/15 text-up" },
  synthetic: { text: "DEMO DATA", cls: "bg-yellow-400/15 text-yellow-300" },
  client: { text: "LIVE", cls: "bg-up/15 text-up" },
  connecting: { text: "CONNECTING", cls: "bg-panel2 text-mute" },
  reconnecting: { text: "RECONNECTING", cls: "bg-orange-400/15 text-orange-300" },
  offline: { text: "OFFLINE", cls: "bg-down/15 text-down" },
};

interface Props {
  symbol: string;
  interval: Interval;
  price: number | null;
  change24: number | null;
  source: DataSource;
  indicators: IndicatorState;
  layout: LayoutState;
  agentOpen: boolean;
  alertsOpen: boolean;
  armedAlerts: number;
  onInterval(i: Interval): void;
  onSearch(): void;
  onIndicators(next: IndicatorState): void;
  onLayout(next: LayoutState): void;
  onScreenshot(): void;
  onFit(): void;
  onToggleAgent(): void;
  onToggleAlerts(): void;
  gridMode: GridMode;
  onGridMode(mode: GridMode): void;
  watchlistOpen: boolean;
  onToggleWatchlist(): void;
}

const GRID_MODES: Array<{ mode: GridMode; label: string; Icon: typeof Square }> = [
  { mode: 1, label: "One chart", Icon: Square },
  { mode: 2, label: "Two charts", Icon: Columns2 },
  { mode: 4, label: "Four charts", Icon: Grid2x2 },
];

export default function ChartHeader(p: Props) {
  const badge = SOURCE_BADGE[p.source] ?? SOURCE_BADGE.connecting;
  return (
    <div className="flex h-11 items-center gap-1 overflow-x-auto border-b border-line bg-panel px-2 scrollbar-none">
      <button type="button" onClick={p.onSearch} className="btn-ghost shrink-0 gap-2 px-2 text-sm" title="Change symbol">
        <span className="font-semibold text-ink">{displaySymbol(p.symbol)}</span>
        <span className="text-xs text-mute">Binance</span>
        <ChevronDown className="h-3.5 w-3.5 text-mute" />
      </button>

      <div className="mr-2 flex shrink-0 items-baseline gap-2 font-mono text-sm">
        <span className="text-ink">{p.price != null && Number.isFinite(p.price) ? formatPrice(p.price) : "—"}</span>
        {p.change24 != null && (
          <span className={clsx("text-xs", p.change24 >= 0 ? "text-up" : "text-down")}>{formatPct(p.change24)}</span>
        )}
        <span className={clsx("rounded px-1.5 py-0.5 font-sans text-[10px] font-semibold tracking-wide", badge.cls)}>
          {badge.text}
        </span>
      </div>

      <div className="mx-1 h-5 w-px shrink-0 bg-line" />

      <div className="flex shrink-0 items-center" role="tablist" aria-label="Timeframe">
        {TIMEFRAMES.map((tf) => (
          <button
            key={tf.value}
            type="button"
            role="tab"
            aria-selected={p.interval === tf.value}
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
            <span className="text-xs">Indicators</span>
          </>
        }
      >
        <MenuToggle label="EMA 20" hint="amber" checked={p.indicators.ema20} onChange={(v) => p.onIndicators({ ...p.indicators, ema20: v })} />
        <MenuToggle label="EMA 50" hint="violet" checked={p.indicators.ema50} onChange={(v) => p.onIndicators({ ...p.indicators, ema50: v })} />
        <MenuToggle label="Parabolic SAR" checked={p.indicators.psar} onChange={(v) => p.onIndicators({ ...p.indicators, psar: v })} />
        <MenuToggle label="VWAP" hint="cyan" checked={!!p.indicators.vwap} onChange={(v) => p.onIndicators({ ...p.indicators, vwap: v })} />
        <MenuToggle label="Volume" checked={p.indicators.volume} onChange={(v) => p.onIndicators({ ...p.indicators, volume: v })} />
        <MenuToggle label="RSI 14" checked={!!p.indicators.rsi} onChange={(v) => p.onIndicators({ ...p.indicators, rsi: v })} />
        <MenuToggle label="MACD" hint="12 26 9" checked={!!p.indicators.macd} onChange={(v) => p.onIndicators({ ...p.indicators, macd: v })} />
      </Menu>

      <Menu title="Chart layout" trigger={<LayoutGrid className="h-4 w-4" />}>
        <MenuToggle label="Auto AI levels" hint="on load" checked={p.layout.autoLevels} onChange={(v) => p.onLayout({ ...p.layout, autoLevels: v })} />
        <MenuToggle label="Log scale" checked={p.layout.logScale} onChange={(v) => p.onLayout({ ...p.layout, logScale: v })} />
        <MenuToggle label="Grid lines" checked={p.layout.grid} onChange={(v) => p.onLayout({ ...p.layout, grid: v })} />
      </Menu>

      <div className="flex shrink-0 items-center" role="radiogroup" aria-label="Chart grid">
        {GRID_MODES.map(({ mode, label, Icon }) => (
          <button
            key={mode}
            type="button"
            role="radio"
            aria-checked={p.gridMode === mode}
            title={label}
            aria-label={label}
            onClick={() => p.onGridMode(mode)}
            className={clsx("btn-ghost", p.gridMode === mode && "bg-panel2 text-ink")}
          >
            <Icon className="h-4 w-4" />
          </button>
        ))}
      </div>

      <button type="button" className="btn-ghost shrink-0" title="Fit chart" aria-label="Fit chart" onClick={p.onFit}>
        <Maximize2 className="h-4 w-4" />
      </button>
      <button type="button" className="btn-ghost shrink-0" title="Search symbol" aria-label="Search symbol" onClick={p.onSearch}>
        <Search className="h-4 w-4" />
      </button>
      <button type="button" className="btn-ghost shrink-0" title="Save screenshot" aria-label="Save screenshot" onClick={p.onScreenshot}>
        <Camera className="h-4 w-4" />
      </button>

      <div className="flex-1" />

      <button
        type="button"
        onClick={p.onToggleWatchlist}
        title="Watchlist"
        aria-label="Watchlist"
        aria-pressed={p.watchlistOpen}
        className={clsx("btn-ghost mr-1 shrink-0", p.watchlistOpen && "bg-panel2 text-ink")}
      >
        <List className="h-4 w-4" />
      </button>
      <button
        type="button"
        onClick={p.onToggleAlerts}
        title="Price alerts"
        aria-label="Price alerts"
        aria-pressed={p.alertsOpen}
        className={clsx("btn-ghost relative mr-1 shrink-0", p.alertsOpen && "bg-panel2 text-ink")}
      >
        <Bell className="h-4 w-4" />
        {p.armedAlerts > 0 && (
          <span className="absolute -right-0.5 -top-0.5 grid h-3.5 min-w-3.5 place-items-center rounded-full bg-yellow-400 px-0.5 text-[9px] font-bold text-black">
            {p.armedAlerts}
          </span>
        )}
      </button>
      <button
        type="button"
        onClick={p.onToggleAgent}
        className={clsx(
          "flex h-7 shrink-0 items-center gap-1.5 rounded-md px-2.5 text-xs font-medium transition-colors",
          p.agentOpen ? "bg-accent text-white" : "bg-accent/15 text-accent hover:bg-accent/25",
        )}
      >
        <Bot className="h-4 w-4" />
        Agent
      </button>
    </div>
  );
}
