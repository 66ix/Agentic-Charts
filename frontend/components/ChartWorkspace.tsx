"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { usePersistentState } from "@/hooks/usePersistentState";
import { analyze } from "@/lib/api";
import { DEFAULT_INTERVAL, DEFAULT_SYMBOL } from "@/lib/config";
import { TIMEFRAMES } from "@/lib/types";
import type {
  AnalyzeResponse,
  Candle,
  DataSource,
  Drawing,
  IndicatorState,
  Interval,
  LayoutState,
  Overlay,
  ToolId,
} from "@/lib/types";

import AgentPanel, { type AgentMessage } from "./AgentPanel";
import AgenticChart, { type AgenticChartHandle, type FeedInfo } from "./AgenticChart";
import ChartHeader from "./ChartHeader";
import DrawingToolbar, { TOOL_HOTKEYS } from "./DrawingToolbar";
import SymbolSearch from "./SymbolSearch";

const VALID_INTERVALS = new Set<string>(TIMEFRAMES.map((t) => t.value));
const uid = () => Math.random().toString(36).slice(2, 10);

function engineNote(r: AnalyzeResponse): string {
  const tf = TIMEFRAMES.find((t) => t.value === r.analysis_interval)?.label ?? r.analysis_interval;
  const llm = r.engine.intent === "rules" || r.engine.intent === "default" ? "rule parser" : r.engine.intent;
  return `${tf} · ${r.overlays.length} overlays · intent: ${llm} · detector: ${r.engine.detector}` +
    (r.data_source === "synthetic" ? " · demo data" : "");
}

export default function ChartWorkspace() {
  const chartRef = useRef<AgenticChartHandle>(null);
  const [symbol, setSymbol] = usePersistentState("ac:symbol", DEFAULT_SYMBOL);
  const [storedInterval, setInterval] = usePersistentState<Interval>("ac:interval", DEFAULT_INTERVAL);
  const interval: Interval = VALID_INTERVALS.has(storedInterval) ? storedInterval : DEFAULT_INTERVAL;
  const [indicators, setIndicators] = usePersistentState<IndicatorState>("ac:indicators", {
    ema20: true,
    ema50: false,
    psar: true,
    volume: true,
  });
  const [layout, setLayout] = usePersistentState<LayoutState>("ac:layout", {
    logScale: false,
    grid: true,
    autoLevels: true,
  });
  const [drawings, setDrawings] = usePersistentState<Drawing[]>(`ac:drawings:${symbol}`, []);

  const [tool, setTool] = useState<ToolId>("crosshair");
  const [magnet, setMagnet] = useState(false);
  const [locked, setLocked] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [overlays, setOverlays] = useState<Overlay[]>([]);
  const [messages, setMessages] = useState<AgentMessage[]>([]);
  const [busy, setBusy] = useState(false);
  const [agentOpen, setAgentOpen] = useState(true);
  const [searchOpen, setSearchOpen] = useState(false);
  const [feed, setFeed] = useState<FeedInfo>({ price: NaN, open24: null, source: "connecting" });
  const [error, setError] = useState<string | null>(null);
  const analysisCtrl = useRef<AbortController | null>(null);

  // Reset AI state when the market changes.
  useEffect(() => {
    analysisCtrl.current?.abort();
    setOverlays([]);
    setSelectedId(null);
    setBusy(false);
  }, [symbol, interval]);

  const runAnalysis = useCallback(
    async (prompt: string, opts: { silent?: boolean } = {}) => {
      analysisCtrl.current?.abort();
      const ctrl = new AbortController();
      analysisCtrl.current = ctrl;
      if (!opts.silent) setMessages((m) => [...m, { id: uid(), role: "user", text: prompt }]);
      setBusy(true);
      try {
        const candles: Candle[] = chartRef.current?.getCandles() ?? [];
        const res = await analyze(
          { symbol, interval, prompt, candles: candles.length >= 100 ? candles.slice(-500) : undefined },
          ctrl.signal,
        );
        if (ctrl.signal.aborted) return;
        setOverlays(res.overlays);
        setMessages((m) => [
          ...m,
          {
            id: uid(),
            role: "agent",
            text: opts.silent ? `Auto-detected levels. ${res.summary}` : res.summary,
            overlays: res.overlays,
            meta: engineNote(res),
          },
        ]);
      } catch (err) {
        if ((err as Error).name === "AbortError") return;
        setMessages((m) => [...m, { id: uid(), role: "error", text: (err as Error).message }]);
      } finally {
        if (analysisCtrl.current === ctrl) setBusy(false);
      }
    },
    [symbol, interval],
  );

  const onDataReady = useCallback(() => {
    if (layout.autoLevels) runAnalysis("", { silent: true });
  }, [layout.autoLevels, runAnalysis]);

  const deleteSelected = useCallback(() => {
    if (locked) return;
    if (selectedId) {
      setDrawings((ds) => ds.filter((d) => d.id !== selectedId));
      setSelectedId(null);
    } else if (drawings.length && window.confirm(`Delete all ${drawings.length} drawings on ${symbol}?`)) {
      setDrawings([]);
    }
  }, [locked, selectedId, drawings.length, symbol, setDrawings]);

  const screenshot = useCallback(() => {
    const canvas = chartRef.current?.screenshot();
    if (!canvas) return;
    canvas.toBlob((blob) => {
      if (!blob) return;
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `${symbol}-${interval}-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.png`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    });
  }, [symbol, interval]);

  // Keyboard shortcuts (ignored while typing).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement;
      if (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "Escape") {
        setTool("crosshair");
        setSelectedId(null);
      } else if ((e.key === "Delete" || e.key === "Backspace") && selectedId) {
        e.preventDefault();
        deleteSelected();
      } else if (e.key === "/") {
        e.preventDefault();
        setAgentOpen(true);
      } else if (TOOL_HOTKEYS[e.key.toLowerCase()] && !locked) {
        setTool(TOOL_HOTKEYS[e.key.toLowerCase()]);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectedId, deleteSelected, locked]);

  const change24 =
    feed.open24 && Number.isFinite(feed.price) ? ((feed.price - feed.open24) / feed.open24) * 100 : null;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <ChartHeader
        symbol={symbol}
        interval={interval}
        price={Number.isFinite(feed.price) ? feed.price : null}
        change24={change24}
        source={feed.source as DataSource}
        indicators={indicators}
        layout={layout}
        agentOpen={agentOpen}
        onInterval={setInterval}
        onSearch={() => setSearchOpen(true)}
        onIndicators={setIndicators}
        onLayout={setLayout}
        onScreenshot={screenshot}
        onFit={() => chartRef.current?.fitContent()}
        onToggleAgent={() => setAgentOpen((v) => !v)}
      />
      <div className="flex min-h-0 flex-1">
        <DrawingToolbar
          tool={tool}
          magnet={magnet}
          locked={locked}
          hasSelection={!!selectedId}
          drawingCount={drawings.length}
          onTool={setTool}
          onMagnet={setMagnet}
          onLock={(v) => {
            setLocked(v);
            if (v) {
              setTool("crosshair");
              setSelectedId(null);
            }
          }}
          onDelete={deleteSelected}
        />
        <main className="relative min-w-0 flex-1">
          <AgenticChart
            ref={chartRef}
            symbol={symbol}
            interval={interval}
            tool={tool}
            magnet={magnet}
            locked={locked}
            indicators={indicators}
            layout={layout}
            overlays={overlays}
            drawings={drawings}
            selectedId={selectedId}
            onDrawingsChange={setDrawings}
            onSelect={setSelectedId}
            onToolDone={() => setTool("crosshair")}
            onFeed={setFeed}
            onDataReady={onDataReady}
            onError={setError}
          />
          {error && (
            <div className="absolute left-1/2 top-3 z-30 -translate-x-1/2 rounded-md border border-down/40 bg-down/10 px-3 py-1.5 text-xs text-down">
              {error} Retrying…
            </div>
          )}
          <AgentPanel
            open={agentOpen}
            busy={busy}
            messages={messages}
            overlayCount={overlays.length}
            onSubmit={(p) => runAnalysis(p)}
            onClearOverlays={() => setOverlays([])}
            onClose={() => setAgentOpen(false)}
          />
        </main>
      </div>
      <SymbolSearch open={searchOpen} onClose={() => setSearchOpen(false)} onPick={setSymbol} />
    </div>
  );
}
