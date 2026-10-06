"use client";

import clsx from "clsx";
import { forwardRef, useState } from "react";

import { usePersistentState } from "@/hooks/usePersistentState";
import { displaySymbol, formatPct, formatPrice } from "@/lib/format";
import { TIMEFRAMES } from "@/lib/types";
import type { ChartCell as Cell, Drawing, IndicatorState, LayoutState, Overlay, ToolId } from "@/lib/types";

import AgenticChart, { type AgenticChartHandle, type FeedInfo } from "./AgenticChart";

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
  onToolDone(): void;
  onFeed(info: FeedInfo): void;
  onDataReady(): void;
  onError(message: string | null): void;
}

interface Props {
  cell: Cell;
  active: ActiveProps | null;
  showHeader: boolean;
  indicators: IndicatorState;
  layout: LayoutState;
  onActivate(): void;
}

const noop = () => undefined;

/**
 * One chart in the grid. The same AgenticChart instance serves whether the cell is active or not, so
 * switching the active cell doesn't reload candles. Inactive cells show their saved AI overlays and
 * drawings read-only; clicking one makes it active.
 */
const ChartCell = forwardRef<AgenticChartHandle, Props>(function ChartCell({ cell, active, showHeader, indicators, layout, onActivate }, ref) {
  const [overlays] = usePersistentState<Overlay[]>(`ac:overlays:${cell.symbol}:${cell.interval}`, []);
  const [drawings] = usePersistentState<Drawing[]>(`ac:drawings:${cell.symbol}`, []);
  const [feed, setFeed] = useState<FeedInfo | null>(null);
  const change = feed?.open24 && Number.isFinite(feed.price) ? ((feed.price - feed.open24) / feed.open24) * 100 : null;
  const tf = TIMEFRAMES.find((t) => t.value === cell.interval)?.label ?? cell.interval;

  return (
    <div
      className={clsx("relative flex min-h-0 min-w-0 flex-col", showHeader && "border border-line", showHeader && active && "border-accent/60")}
      onPointerDownCapture={active ? undefined : onActivate}
    >
      {showHeader && (
        <div className={clsx("flex h-6 shrink-0 items-center gap-2 border-b border-line px-2 text-[11px]", active ? "bg-accent/10" : "bg-panel")}>
          <span className={clsx("font-semibold", active ? "text-accent" : "text-ink")}>{displaySymbol(cell.symbol)}</span>
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
          indicators={indicators}
          layout={layout}
          overlays={active?.overlays ?? overlays}
          drawings={active?.drawings ?? drawings}
          selectedId={active?.selectedId ?? null}
          onDrawingsChange={active?.onDrawingsChange ?? noop}
          onSelect={active?.onSelect ?? noop}
          onToolDone={active?.onToolDone ?? noop}
          onFeed={(f) => {
            setFeed(f);
            active?.onFeed(f);
          }}
          onDataReady={active?.onDataReady}
          onError={active?.onError}
        />
      </div>
    </div>
  );
});

export default ChartCell;
