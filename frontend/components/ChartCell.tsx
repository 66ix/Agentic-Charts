"use client";

import clsx from "clsx";
import { forwardRef, useMemo, useState } from "react";

import { usePersistentState } from "@/hooks/usePersistentState";
import { useHigherTfOverlays } from "@/hooks/useHigherTfOverlays";
import { customLabel } from "@/lib/customSymbols";
import { formatPct, formatPrice } from "@/lib/format";
import { composeOverlays, drawingsFor, pinsKey, type LayerVisibility, type PanelOverlays, type PinnedAnswer } from "@/lib/layers";
import { TIMEFRAMES } from "@/lib/types";
import type { Candle, ChartCell as Cell, Drawing, IndicatorSettings, IndicatorState, LayoutState, Overlay, ToolId } from "@/lib/types";

import AgenticChart, { type AgenticChartHandle, type CompareLine, type FeedInfo, type KimiVisibility } from "./AgenticChart";

/** What the workspace passes to the active chart; inactive charts read their own saved state. */
export interface ActiveProps {
  tool: ToolId;
  magnet: boolean;
  locked: boolean;
  overlays: Overlay[];
  drawings: Drawing[];
  selectedId: string | null;
  onDrawingsChange(next: Drawing[]): void;
  onSelect(id: string | null): void;
  onPickOverlay?(o: Overlay | null): void;
  replayTime?: number | null;
  onToolDone(): void;
  onFeed(info: FeedInfo): void;
  onDataReady(candles: Candle[]): void;
  onError(message: string | null): void;
  onAlertMove?(alertId: string, patch: { price?: number; price_low?: number; price_high?: number }): void;
}

interface Props {
  cell: Cell;
  active: ActiveProps | null;
  showHeader: boolean;
  indicators: IndicatorState;
  indicatorSettings: IndicatorSettings;
  layout: LayoutState;
  visibility: LayerVisibility;
  kimiParts: KimiVisibility;
  panels: PanelOverlays;
  alertOverlays(symbol: string): Overlay[];
  compare: CompareLine[];
  onActivate(): void;
  onCrosshairTime(time: number | null): void;
}

const noop = () => undefined;

/**
 * One chart in the grid. The same AgenticChart instance serves whether the cell is active or not, so
 * switching the active cell doesn't reload candles. Inactive cells show their saved AI overlays, pins and
 * drawings read-only; clicking one makes it active.
 */
const ChartCell = forwardRef<AgenticChartHandle, Props>(function ChartCell(p, ref) {
  const { cell, active } = p;
  const [saved] = usePersistentState<Overlay[]>(`ac:overlays:${cell.symbol}:${cell.interval}`, []);
  const [pins] = usePersistentState<Record<string, PinnedAnswer>>(pinsKey(cell.symbol, cell.interval), {});
  const [drawings] = usePersistentState<Drawing[]>(`ac:drawings:${cell.symbol}`, []);
  const htf = useHigherTfOverlays(cell.symbol, cell.interval);
  const [feed, setFeed] = useState<FeedInfo | null>(null);
  const change = feed?.open24 && Number.isFinite(feed.price) ? ((feed.price - feed.open24) / feed.open24) * 100 : null;
  const tf = TIMEFRAMES.find((t) => t.value === cell.interval)?.label ?? cell.interval;

  const { alertOverlays, panels, visibility } = p;
  const passive = useMemo(() => {
    if (active) return null;
    const { visible } = composeOverlays({ symbol: cell.symbol, overlays: saved, pins, panels, alerts: alertOverlays(cell.symbol), visibility, htf });
    return { overlays: visible, drawings: visibility.drawings === false ? [] : drawingsFor(drawings, cell.interval) };
  }, [active, saved, pins, panels, alertOverlays, visibility, drawings, cell.symbol, cell.interval, htf]);

  return (
    <div
      className={clsx("relative flex min-h-0 min-w-0 flex-col", p.showHeader && "border border-line", p.showHeader && active && "border-accent/60")}
      onPointerDownCapture={active ? undefined : p.onActivate}
    >
      {p.showHeader && (
        <div className={clsx("flex h-6 shrink-0 items-center gap-2 border-b border-line px-2 text-[11px]", active ? "bg-accent/10" : "bg-panel")}>
          <span className={clsx("font-semibold", active ? "text-accent" : "text-ink")}>{customLabel(cell.symbol)}</span>
          <span className="text-mute">{tf}</span>
          {feed && Number.isFinite(feed.price) && <span className="font-mono text-ink">{formatPrice(feed.price)}</span>}
          {change != null && <span className={clsx("font-mono", change >= 0 ? "text-up" : "text-down")}>{formatPct(change)}</span>}
        </div>
      )}
      <div className="relative min-h-0 flex-1">
        <AgenticChart
          ref={ref}
          symbol={cell.symbol}
          interval={cell.interval}
          tool={active?.tool ?? "crosshair"}
          magnet={active?.magnet ?? false}
          locked={active?.locked ?? true}
          indicators={p.indicators}
          indicatorSettings={p.indicatorSettings}
          layout={p.layout}
          kimiParts={p.kimiParts}
          compare={p.compare}
          overlays={active?.overlays ?? passive?.overlays ?? []}
          drawings={active?.drawings ?? passive?.drawings ?? []}
          selectedId={active?.selectedId ?? null}
          onDrawingsChange={active?.onDrawingsChange ?? noop}
          onSelect={active?.onSelect ?? noop}
          onPickOverlay={active?.onPickOverlay}
          replayTime={active?.replayTime ?? null}
          onToolDone={active?.onToolDone ?? noop}
          onFeed={(f) => {
            setFeed(f);
            active?.onFeed(f);
          }}
          onDataReady={active?.onDataReady}
          onError={active?.onError}
          onAlertMove={active?.onAlertMove}
          onCrosshairTime={p.onCrosshairTime}
          layers={p.visibility}
        />
      </div>
    </div>
  );
});

export default ChartCell;
