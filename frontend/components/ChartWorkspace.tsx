"use client";

import clsx from "clsx";
import { Bell, Bot, CandlestickChart, Layers as LayersIcon, List, Loader2, MessageSquare, Pencil, SendHorizontal, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useAlerts, type FiredAlert, type SignalFired } from "@/hooks/useAlerts";
import { useHigherTfOverlays } from "@/hooks/useHigherTfOverlays";
import { useIsMobile } from "@/hooks/useMediaQuery";
import { readStored, usePersistentState, writeStored } from "@/hooks/usePersistentState";
import { useUndo } from "@/hooks/useUndo";
import { alertFromDrawing, alertOverlays, chartZones, SELL_SIGNALS } from "@/lib/alerts";
import { analyze } from "@/lib/api";
import { DEFAULT_INTERVAL, DEFAULT_SYMBOL } from "@/lib/config";
import { isCustom } from "@/lib/customSymbols";
import { CHAT_ID_KEY, CHATS_KEY, toSession, upsertSession, worthKeeping, type ChatSession } from "@/lib/chatHistory";
import { createJournalEntry, planToJournalEntry } from "@/lib/journal";
import { DEFAULT_SIZING, sizePlan, type SizingSettings } from "@/lib/sizing";
import { OPEN_PANEL_EVENT, type DockPanelProps } from "@/lib/dock";
import { offerGridPlan } from "@/lib/gridbot";
import { gridCoinOverlays, SPOT_ONLY_KEY } from "@/lib/spot";
import {
  composeOverlays,
  drawingsFor,
  isVisible,
  overlaysKey as overlaysKeyFor,
  pinsKey,
  type LayerVisibility,
  type PanelOverlays,
  type PinnedAnswer,
} from "@/lib/layers";
import { DEFAULT_INDICATOR_SETTINGS, INTERVAL_SECONDS, TIMEFRAMES } from "@/lib/types";
import type {
  AnalysisIntent,
  AnalyzeResponse,
  Candle,
  ChartCell as Cell,
  DataSource,
  Drawing,
  GridCoin,
  GridMode,
  IndicatorSettings,
  IndicatorState,
  Interval,
  LayoutState,
  MarketSetup,
  Overlay,
  SellWatch,
  ToolId,
  TopDownResult,
  TriggerInterval,
} from "@/lib/types";
import { CURRENT_WORKSPACE_KEY, saveWorkspace, WORKSPACES_KEY, type SavedWorkspace } from "@/lib/workspaces";

import AgentPanel, { type AgentMessage, type AgentPanelHandle } from "./AgentPanel";
import { type AgenticChartHandle, type CompareLine, type FeedInfo, type KimiVisibility } from "./AgenticChart";
import AlertsPanel from "./AlertsPanel";
import AlertToasts, { signalToast, type Toast } from "./AlertToasts";
import ChartCell from "./ChartCell";
import ChartHeader, { COMPARE_COLORS } from "./ChartHeader";
import Dock, { type DockTab } from "./Dock";
import { EXTRA_PANELS } from "./dockPanels";
import DrawingStyleBar from "./DrawingStyleBar";
import DrawingToolbar, { TOOL_HOTKEYS } from "./DrawingToolbar";
import IndicatorSettingsDialog from "./IndicatorSettingsDialog";
import LayersPanel from "./LayersPanel";
import SettingsDialog from "./SettingsDialog";
import ShortcutsDialog from "./ShortcutsDialog";
import SymbolSearch from "./SymbolSearch";
import Watchlist, { type WatchlistList, type WatchlistSort } from "./Watchlist";

const VALID_INTERVALS = new Set<string>(TIMEFRAMES.map((t) => t.value));
const uid = () => Math.random().toString(36).slice(2, 10);
const MAX_MESSAGES = 40;
/** How long a top-down walk shows each timeframe before stepping down to the next. */
const WALK_PAUSE_MS = 2500;

/** Resolves after `ms`, or as soon as `signal` aborts. */
function pause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const t = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => (clearTimeout(t), resolve()), { once: true });
  });
}
const DEFAULT_CELLS: Cell[] = [
  { symbol: DEFAULT_SYMBOL, interval: DEFAULT_INTERVAL },
  { symbol: "BTCUSDT", interval: "4h" },
  { symbol: "ETHUSDT", interval: "4h" },
  { symbol: "SOLUSDT", interval: "4h" },
];
const DEFAULT_WATCHLIST = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "INJUSDT", "DOGEUSDT", "LINKUSDT"];
const DEFAULT_LISTS: WatchlistList[] = [{ id: "main", name: "Main", symbols: DEFAULT_WATCHLIST }];
const DEFAULT_INDICATORS: IndicatorState = {
  ema20: true,
  ema50: false,
  psar: true,
  volume: true,
  rsi: false,
  macd: false,
  vwap: false,
  kimi: false,
};
const DEFAULT_LAYOUT: LayoutState = { logScale: false, grid: true, autoLevels: true };
const GRID_CLASS: Record<GridMode, string> = { 1: "grid-cols-1", 2: "grid-cols-2", 4: "grid-cols-2 grid-rows-2" };
const NEXT_GRID: Record<GridMode, GridMode> = { 1: 2, 2: 4, 4: 1 };

interface DockState {
  open: boolean;
  tab: string;
  width: number;
}
const DEFAULT_DOCK: DockState = { open: true, tab: "agent", width: 380 };

type SearchMode = "chart" | "watchlist" | "compare";
const SEARCH_TITLES: Record<SearchMode, string | undefined> = {
  chart: undefined,
  watchlist: "Add a coin to the watchlist",
  compare: "Compare with…",
};

function engineNote(r: AnalyzeResponse): string {
  const tf = TIMEFRAMES.find((t) => t.value === r.analysis_interval)?.label ?? r.analysis_interval;
  const llm = r.engine.intent === "rules" || r.engine.intent === "default" ? "rule parser" : r.engine.intent;
  return `${tf} · ${r.overlays.length} overlays · intent: ${llm} · detector: ${r.engine.detector}` +
    (r.data_source === "synthetic" ? " · demo data" : "");
}

/** What a change to the drawings list did, for the undo button's tooltip. */
function drawingChange(before: Drawing[], after: Drawing[]): string {
  if (after.length > before.length) return "new drawing";
  if (after.length < before.length) return before.length - after.length > 1 ? "delete drawings" : "delete drawing";
  return "drawing edit";
}

export default function ChartWorkspace() {
  const mobile = useIsMobile();
  const cellRefs = useRef<Array<AgenticChartHandle | null>>([]);
  const agentRef = useRef<AgentPanelHandle>(null);

  // ------------------------------------------------------------------ charts in the grid
  const [cells, setCells, cellsLoaded] = usePersistentState<Cell[]>("ac:cells", DEFAULT_CELLS);
  const [storedGrid, setGridMode] = usePersistentState<GridMode>("ac:grid", 1);
  const gridMode: GridMode = mobile ? 1 : storedGrid;
  const [storedActive, setActive] = usePersistentState<number>("ac:active-cell", 0);
  const active = Math.min(Math.max(0, storedActive), gridMode === 1 ? 3 : gridMode - 1);
  const cell = cells[active] ?? DEFAULT_CELLS[0];
  const symbol = cell.symbol;
  const interval: Interval = VALID_INTERVALS.has(cell.interval) ? cell.interval : DEFAULT_INTERVAL;
  const [layout, setLayout] = usePersistentState<LayoutState>("ac:layout", DEFAULT_LAYOUT);
  const linked = !!layout.linkSymbol;
  const activeChart = useCallback(() => cellRefs.current[active] ?? null, [active]);

  const setCell = useCallback(
    (patch: Partial<Cell>) =>
      setCells((cs) => {
        const next = [...cs, ...DEFAULT_CELLS.slice(cs.length)];
        next[active] = { ...next[active], ...patch };
        // "Every chart follows the same coin": the others take the symbol and keep their timeframe.
        if (patch.symbol && linked) for (let i = 0; i < next.length; i++) next[i] = { ...next[i], symbol: patch.symbol };
        return next;
      }),
    [setCells, active, linked],
  );
  const setSymbol = useCallback((s: string) => setCell({ symbol: s }), [setCell]);
  const setInterval = useCallback((i: Interval) => setCell({ interval: i }), [setCell]);
  const pickSymbol = useCallback((s: string, i?: Interval) => setCell(i ? { symbol: s, interval: i } : { symbol: s }), [setCell]);

  useEffect(() => {
    if (linked) setCell({ symbol });
    // Only when the setting is switched on.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [linked]);

  // One-time move from the single-chart keys (ac:symbol / ac:interval) to the cell list.
  useEffect(() => {
    if (!cellsLoaded) return;
    try {
      const legacy = window.localStorage.getItem("ac:symbol");
      if (legacy && window.localStorage.getItem("ac:cells-migrated") !== "1") {
        const iv = JSON.parse(window.localStorage.getItem("ac:interval") ?? "null");
        setCells((cs) => [{ symbol: JSON.parse(legacy), interval: VALID_INTERVALS.has(iv) ? iv : DEFAULT_INTERVAL }, ...cs.slice(1)]);
      }
      window.localStorage.setItem("ac:cells-migrated", "1");
    } catch {
      /* storage unavailable */
    }
  }, [cellsLoaded, setCells]);

  // ------------------------------------------------------------------ watchlists
  const [lists, setLists, listsLoaded] = usePersistentState<WatchlistList[]>("ac:watchlists", DEFAULT_LISTS);
  const [activeListId, setActiveList] = usePersistentState<string>("ac:watchlist-list", "main");
  const [sort, setSort] = usePersistentState<WatchlistSort>("ac:watchlist-sort", "manual");
  const currentList = lists.find((l) => l.id === activeListId) ?? lists[0] ?? DEFAULT_LISTS[0];
  const watchlist = currentList.symbols;

  // One-time move from the single watchlist (ac:watchlist) to named lists.
  useEffect(() => {
    if (!listsLoaded) return;
    try {
      if (window.localStorage.getItem("ac:watchlists-migrated") === "1") return;
      const legacy = readStored<string[] | null>("ac:watchlist", null);
      if (Array.isArray(legacy) && legacy.length) setLists([{ id: "main", name: "Main", symbols: legacy }]);
      window.localStorage.setItem("ac:watchlists-migrated", "1");
    } catch {
      /* storage unavailable */
    }
  }, [listsLoaded, setLists]);

  const addToWatchlist = useCallback(
    (s: string) =>
      setLists((ls) =>
        ls.map((l) => (l.id === currentList.id && !l.symbols.includes(s) ? { ...l, symbols: [...l.symbols, s].slice(-60) } : l)),
      ),
    [setLists, currentList.id],
  );

  // ------------------------------------------------------------------ indicators, layers, side panel
  const [indicators, setIndicators] = usePersistentState<IndicatorState>("ac:indicators", DEFAULT_INDICATORS);
  const [storedSettings, setIndicatorSettings] = usePersistentState<IndicatorSettings>("ac:indicator-settings", DEFAULT_INDICATOR_SETTINGS);
  const indicatorSettings = useMemo(() => ({ ...DEFAULT_INDICATOR_SETTINGS, ...storedSettings }), [storedSettings]);
  const [visibility, setVisibility] = usePersistentState<LayerVisibility>("ac:layers", {});
  const [dock, setDock] = usePersistentState<DockState>("ac:dock", DEFAULT_DOCK);
  // Phones: the side panel covers the chart, so it starts closed and isn't saved.
  const [mobileTab, setMobileTab] = useState<string | null>(null);
  const [mobileDraw, setMobileDraw] = useState(false);
  const [compareStore, setCompareStore] = usePersistentState<Record<string, CompareLine[]>>("ac:compare", {});
  const [panels, setPanels] = usePersistentState<PanelOverlays>("ac:panel-overlays", {});
  const [quickbar, setQuickbar] = usePersistentState("ac:quickbar", true);
  const [spotOnly, setSpotOnly] = usePersistentState(SPOT_ONLY_KEY, true);

  const kimiParts = useMemo<KimiVisibility>(
    () => ({
      sr: isVisible(visibility, "kimiSR"),
      fib: isVisible(visibility, "kimiFib"),
      forecast: isVisible(visibility, "kimiForecast"),
      signals: isVisible(visibility, "kimiSignals"),
      patterns: isVisible(visibility, "kimiPatterns"),
      harmonics: isVisible(visibility, "kimiHarmonics"),
    }),
    [visibility],
  );
  const compareFor = useCallback(
    (s: string) => (isVisible(visibility, "compare") ? (compareStore[s] ?? []) : []),
    [compareStore, visibility],
  );
  const compare = compareStore[symbol] ?? [];
  const setCompare = useCallback(
    (next: CompareLine[]) => setCompareStore((m) => ({ ...m, [symbol]: next.slice(0, COMPARE_COLORS.length) })),
    [setCompareStore, symbol],
  );

  // ------------------------------------------------------------------ drawings, AI levels, pins, undo
  const drawingsKey = `ac:drawings:${symbol}`;
  const overlaysKey = overlaysKeyFor(symbol, interval);
  const [drawings, setDrawings] = usePersistentState<Drawing[]>(drawingsKey, []);
  const [overlays, , overlaysLoaded] = usePersistentState<Overlay[]>(overlaysKey, []);
  const [pins] = usePersistentState<Record<string, PinnedAnswer>>(pinsKey(symbol, interval), {});
  const [pinIndex, setPinIndex] = usePersistentState<string[]>("ac:pins-index", []);
  const { record, undo, redo, undoLabel, redoLabel } = useUndo();
  const [undoNote, setUndoNote] = useState<string | null>(null);

  const [tool, setTool] = useState<ToolId>("crosshair");
  const [magnet, setMagnet] = useState(false);
  const [locked, setLocked] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  /** An AI level clicked on the chart, to remove on its own. */
  const [pickedLevel, setPickedLevel] = useState<Overlay | null>(null);

  const drawingsRef = useRef(drawings);
  drawingsRef.current = drawings;
  const shownDrawings = useMemo(
    () => (isVisible(visibility, "drawings") ? drawingsFor(drawings, interval) : []),
    [drawings, interval, visibility],
  );
  const shownRef = useRef(shownDrawings);
  shownRef.current = shownDrawings;

  const changeDrawings = useCallback(
    (next: Drawing[], label?: string) => {
      record(drawingsKey, drawingsRef.current, next, label ?? drawingChange(drawingsRef.current, next));
      setDrawings(next);
    },
    [record, drawingsKey, setDrawings],
  );

  /** The chart edits the drawings it shows; the ones hidden on this timeframe or layer are kept as they are. */
  const onChartDrawings = useCallback(
    (next: Drawing[]) => {
      const shownIds = new Set(shownRef.current.map((d) => d.id));
      const byId = new Map(next.map((d) => [d.id, d]));
      const merged = drawingsRef.current.flatMap((d) => (!shownIds.has(d.id) ? [d] : byId.has(d.id) ? [byId.get(d.id)!] : []));
      const known = new Set(drawingsRef.current.map((d) => d.id));
      changeDrawings([...merged, ...next.filter((d) => !known.has(d.id))]);
    },
    [changeDrawings],
  );

  /** Replace the AI levels of a chart (this one or the one the agent moves to), undoably. */
  const changeOverlays = useCallback(
    (key: string, next: Overlay[], label: string) => {
      record(key, readStored<Overlay[]>(key, []), next, label);
      writeStored(key, next);
    },
    [record],
  );

  const doUndo = useCallback(() => {
    const label = undo();
    setUndoNote(label ? `Undid ${label}` : null);
  }, [undo]);
  const doRedo = useCallback(() => {
    const label = redo();
    setUndoNote(label ? `Redid ${label}` : null);
  }, [redo]);
  useEffect(() => {
    if (!undoNote) return;
    const id = setTimeout(() => setUndoNote(null), 1800);
    return () => clearTimeout(id);
  }, [undoNote]);

  const togglePin = useCallback(
    (m: AgentMessage) => {
      if (!m.symbol || !m.interval) return;
      const key = pinsKey(m.symbol, m.interval);
      const cur = readStored<Record<string, PinnedAnswer>>(key, {});
      if (cur[m.id]) {
        const rest = { ...cur };
        delete rest[m.id];
        writeStored(key, rest);
        setPinIndex((ix) => ix.filter((x) => x !== m.id));
      } else {
        const label = (m.prompt || m.text).slice(0, 80);
        writeStored(key, { ...cur, [m.id]: { label, overlays: m.overlays ?? [], at: Date.now() } });
        setPinIndex((ix) => [...ix.filter((x) => x !== m.id), m.id].slice(-200));
      }
    },
    [setPinIndex],
  );
  const unpin = useCallback(
    (id: string) => {
      const key = pinsKey(symbol, interval);
      const cur = readStored<Record<string, PinnedAnswer>>(key, {});
      delete cur[id];
      writeStored(key, cur);
      setPinIndex((ix) => ix.filter((x) => x !== id));
    },
    [symbol, interval, setPinIndex],
  );
  const pinnedSet = useMemo(() => new Set(pinIndex), [pinIndex]);

  const onChartOverlays = useCallback(
    (key: string, sym: string, ovs: Overlay[]) =>
      setPanels((prev) => {
        const next = { ...prev };
        if (ovs.length) next[key] = { symbol: sym, overlays: ovs };
        else delete next[key];
        return next;
      }),
    [setPanels],
  );

  // ------------------------------------------------------------------ alerts
  const [toasts, setToasts] = useState<Toast[]>([]);
  const onAlertsFired = useCallback((fired: FiredAlert[]) => {
    setToasts((t) => [...t, ...fired.map((f) => ({ id: uid(), alert: f.alert, price: f.price }))].slice(-4));
  }, []);
  const onSignalFired = useCallback((f: SignalFired) => setToasts((t) => [...t, signalToast(uid(), f)].slice(-4)), []);
  const alertsApi = useAlerts(onAlertsFired, onSignalFired);
  const { alerts, add: addAlerts, update: updateAlert, addTrigger, addSignal } = alertsApi;
  const armedAlerts = alerts.filter((a) => a.armed).length;
  const alertOverlaysFor = useCallback((s: string) => alertOverlays(alerts, s), [alerts]);

  const htf = useHigherTfOverlays(symbol, interval);
  const composed = useMemo(
    () => composeOverlays({ symbol, overlays, pins, panels, alerts: alertOverlaysFor(symbol), visibility, htf }),
    [symbol, overlays, pins, panels, alertOverlaysFor, visibility, htf],
  );
  const layerCounts = useMemo(() => {
    const kimi = indicators.kimi && !isCustom(symbol) ? 1 : 0;
    return {
      ...composed.counts,
      drawings: drawings.length,
      compare: compare.length,
      kimiSR: kimi,
      kimiFib: kimi,
      kimiForecast: kimi,
      kimiSignals: kimi,
      sessions: indicators.sessions && !isCustom(symbol) ? 1 : 0,
      heatmap: indicators.heatmap && !isCustom(symbol) ? 1 : 0,
      kimiPatterns: kimi,
      kimiHarmonics: kimi,
    };
  }, [composed.counts, drawings.length, compare.length, indicators.kimi, indicators.sessions, indicators.heatmap, symbol]);

  // ------------------------------------------------------------------ the chart agent
  const [messages, setStoredMessages] = usePersistentState<AgentMessage[]>("ac:chat", []);
  const [lastIntent, setLastIntent] = usePersistentState<AnalysisIntent | null>("ac:intent", null);
  const setMessages = useCallback(
    (fn: (m: AgentMessage[]) => AgentMessage[]) => setStoredMessages((m) => fn(m).slice(-MAX_MESSAGES)),
    [setStoredMessages],
  );
  const [busy, setBusy] = useState(false);
  /** The step a top-down walk is showing ("D1: 3 levels (1/4)"), while it plays. */
  const [walkNote, setWalkNote] = useState<string | null>(null);
  const walkingRef = useRef(false);
  const [quickAnswer, setQuickAnswer] = useState<AgentMessage | null>(null);
  const [searchMode, setSearchMode] = useState<SearchMode | null>(null);
  const [feed, setFeed] = useState<FeedInfo>({ price: NaN, open24: null, source: "connecting" });
  const [error, setError] = useState<string | null>(null);
  const [dialog, setDialog] = useState<"settings" | "indicators" | "shortcuts" | null>(null);
  const analysisCtrl = useRef<AbortController | null>(null);

  // Past conversations: "New chat" files the current one here, and opening one brings it back to continue.
  const [chats, setChats] = usePersistentState<ChatSession[]>(CHATS_KEY, []);
  const [chatId, setChatId] = usePersistentState<string>(CHAT_ID_KEY, "");
  const archiveChat = useCallback(() => {
    if (worthKeeping(messages)) setChats((list) => upsertSession(list, toSession(chatId || uid(), messages, lastIntent)));
  }, [messages, lastIntent, chatId, setChats]);
  const stopAnswer = useCallback(() => {
    // An answer still on its way belongs to the conversation it was asked in, not the next one.
    analysisCtrl.current?.abort();
    setBusy(false);
  }, []);
  const newChat = useCallback(() => {
    stopAnswer();
    archiveChat();
    setMessages(() => []);
    setLastIntent(null);
    setChatId(uid());
  }, [stopAnswer, archiveChat, setMessages, setLastIntent, setChatId]);
  const openChat = useCallback(
    (id: string) => {
      const chat = chats.find((c) => c.id === id);
      if (!chat) return;
      stopAnswer();
      archiveChat();
      // The opened one is the current conversation now; it goes back in the history when it is left.
      setChats((list) => list.filter((c) => c.id !== id));
      setMessages(() => chat.messages);
      setLastIntent(chat.intent);
      setChatId(chat.id);
    },
    [chats, stopAnswer, archiveChat, setChats, setMessages, setLastIntent, setChatId],
  );
  const deleteChat = useCallback((id: string) => setChats((list) => list.filter((c) => c.id !== id)), [setChats]);

  // Latest conversation state for the request, without re-creating runAnalysis on every message.
  const convoRef = useRef({ messages, overlays, lastIntent, watchlist, spotOnly });
  convoRef.current = { messages, overlays, lastIntent, watchlist, spotOnly };

  // Cancel in-flight analysis when the market changes (unless a top-down walk is the one changing it).
  useEffect(() => {
    if (walkingRef.current) return;
    analysisCtrl.current?.abort();
    setSelectedId(null);
    setBusy(false);
  }, [symbol, interval]);

  /**
   * Plays a top-down walk: each timeframe with valid levels in turn, highest first, drawing its levels and moving
   * the chart there, then pausing so they can be seen. Higher-timeframe levels stay on the lower charts
   * (useHigherTfOverlays). Stops early when the answer is stopped or a new question is asked.
   */
  const playWalk = useCallback(
    async (walk: TopDownResult, signal: AbortSignal) => {
      const drawn = walk.steps.filter((s) => s.status === "drawn");
      if (!drawn.length) return;
      walkingRef.current = true;
      try {
        for (const [i, step] of drawn.entries()) {
          if (signal.aborted) break;
          setWalkNote(`${step.label}: ${step.zones.length} level${step.zones.length === 1 ? "" : "s"} (${i + 1}/${drawn.length})`);
          changeOverlays(overlaysKeyFor(walk.symbol, step.interval), step.overlays, `top-down ${step.label} levels`);
          setCell({ symbol: walk.symbol, interval: step.interval });
          if (i < drawn.length - 1) await pause(WALK_PAUSE_MS, signal);
        }
      } finally {
        // Let the last timeframe switch render before market changes cancel analysis again.
        setTimeout(() => (walkingRef.current = false), 500);
        setWalkNote(null);
      }
    },
    [changeOverlays, setCell],
  );

  const runAnalysis = useCallback(
    async (prompt: string, opts: { silent?: boolean } = {}) => {
      analysisCtrl.current?.abort();
      const ctrl = new AbortController();
      analysisCtrl.current = ctrl;
      const convo = convoRef.current;
      if (!opts.silent) setMessages((m) => [...m, { id: uid(), role: "user", text: prompt }]);
      setBusy(true);
      try {
        const candles: Candle[] = activeChart()?.getCandles() ?? [];
        const history = convo.messages
          .filter((m) => m.role !== "error")
          .slice(-10)
          .map((m) => ({ role: m.role as "user" | "agent", text: m.text }));
        const res = await analyze(
          {
            symbol,
            interval,
            prompt,
            candles: candles.length >= 100 ? candles.slice(-500) : undefined,
            history: opts.silent ? [] : history,
            overlays: opts.silent ? [] : convo.overlays,
            previous_intent: opts.silent ? null : convo.lastIntent,
            watchlist: convo.watchlist,
            spot_only: convo.spotOnly,
          },
          ctrl.signal,
        );
        if (ctrl.signal.aborted) return;
        const target = res.navigate ?? { symbol, interval };
        // Saved under the chart the answer belongs to; when the agent moves the chart, that chart loads them.
        changeOverlays(`ac:overlays:${target.symbol}:${target.interval}`, res.overlays, opts.silent ? "auto levels" : "agent answer");
        if (res.navigate) setCell({ symbol: res.navigate.symbol, interval: res.navigate.interval });
        if (Object.keys(res.indicators ?? {}).length) setIndicators((ind) => ({ ...ind, ...res.indicators }));
        if (prompt) setLastIntent(res.intent);
        addAlerts(res.alerts ?? [], res.symbol);
        for (const t of res.trigger_alerts ?? []) void addTrigger(t);
        const answer: AgentMessage = {
          id: uid(),
          role: "agent",
          text: opts.silent ? `Auto-detected levels. ${res.summary}` : res.summary,
          overlays: res.overlays,
          meta: engineNote(res),
          alerts: (res.alerts?.length ?? 0) + (res.trigger_alerts?.length ?? 0) || undefined,
          plan: res.plan ?? undefined,
          scan: res.scan?.length ? res.scan : undefined,
          setups: res.setups?.length ? res.setups : undefined,
          gridCoins: res.grid_coins?.length ? res.grid_coins : undefined,
          walk: res.top_down ?? undefined,
          ladder: res.ladder ?? undefined,
          sources: res.sources?.length ? res.sources : undefined,
          sells: res.sells?.length ? res.sells : undefined,
          sellWatch: res.sell_watch?.symbols.length ? res.sell_watch : undefined,
          steps: res.steps?.length ? res.steps : undefined,
          symbol: target.symbol,
          interval: target.interval,
          prompt: prompt || undefined,
          lastPrice: res.stats?.last_price,
        };
        setMessages((m) => [...m, answer]);
        if (!opts.silent) setQuickAnswer(answer);
        if (res.grid_plan && !opts.silent) {
          // "Plan a grid bot on INJ": the Grid bots tab takes the plan to test, edit and track it.
          offerGridPlan(res.grid_plan);
          openTabRef.current("gridbots");
        }
        if (res.top_down && !opts.silent) await playWalk(res.top_down, ctrl.signal);
      } catch (err) {
        if ((err as Error).name === "AbortError") return;
        const msg: AgentMessage = { id: uid(), role: "error", text: (err as Error).message };
        setMessages((m) => [...m, msg]);
        if (!opts.silent) setQuickAnswer(msg);
      } finally {
        if (analysisCtrl.current === ctrl) setBusy(false);
      }
    },
    [symbol, interval, activeChart, setMessages, changeOverlays, setLastIntent, addAlerts, addTrigger, setCell, setIndicators, playWalk],
  );

  // Auto-detect levels on load, unless this chart already has saved AI overlays.
  const onDataReady = useCallback(() => {
    if (layout.autoLevels && overlaysLoaded && convoRef.current.overlays.length === 0) void runAnalysis("", { silent: true });
  }, [layout.autoLevels, overlaysLoaded, runAnalysis]);

  // ------------------------------------------------------------------ drawing actions
  const selectedDrawing = drawings.find((d) => d.id === selectedId);
  // Alerts watch a Binance pair, so ratio and index charts can't have them.
  const selectedAlert = selectedDrawing && !isCustom(symbol) ? alertFromDrawing(selectedDrawing) : null;

  const deleteSelected = useCallback(() => {
    if (locked) return;
    if (selectedId) {
      changeDrawings(drawingsRef.current.filter((d) => d.id !== selectedId));
      setSelectedId(null);
    } else if (drawingsRef.current.length && window.confirm(`Delete all ${drawingsRef.current.length} drawings on ${symbol}? (Ctrl+Z brings them back)`)) {
      changeDrawings([]);
    }
  }, [locked, selectedId, symbol, changeDrawings]);

  const duplicateSelected = useCallback(() => {
    const d = drawingsRef.current.find((x) => x.id === selectedId);
    if (!d || locked) return;
    const shift = 5 * INTERVAL_SECONDS[interval];
    const copy: Drawing = { ...d, id: uid(), points: d.points.map((pt) => ({ ...pt, time: pt.time + shift })) };
    changeDrawings([...drawingsRef.current, copy], "duplicate drawing");
    setSelectedId(copy.id);
  }, [selectedId, locked, interval, changeDrawings]);

  const alertOnSelected = useCallback(() => {
    if (!selectedAlert) return;
    addAlerts([selectedAlert], symbol);
    setUndoNote("Alert set");
  }, [selectedAlert, addAlerts, symbol]);

  const screenshot = useCallback(() => {
    const canvas = activeChart()?.screenshot();
    if (!canvas) return;
    canvas.toBlob((blob) => {
      if (!blob) return;
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `${symbol.replace(/[/:]/g, "-")}-${interval}-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.png`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    });
  }, [symbol, interval, activeChart]);

  // ------------------------------------------------------------------ side panel
  const agentVisible = mobile ? mobileTab === "agent" : dock.open && dock.tab === "agent";
  const openTab = useCallback(
    (id: string, toggle = false) => {
      if (mobile) setMobileTab((t) => (toggle && t === id ? null : id));
      else setDock((d) => (toggle && d.open && d.tab === id ? { ...d, open: false } : { ...d, open: true, tab: id }));
    },
    [mobile, setDock],
  );
  // Other tabs and cards open a tab with openDockPanel (lib/dock.ts), e.g. "Open in Backtest" on a plan card.
  useEffect(() => {
    const onOpen = (e: Event) => openTab((e as CustomEvent<string>).detail);
    window.addEventListener(OPEN_PANEL_EVENT, onOpen);
    return () => window.removeEventListener(OPEN_PANEL_EVENT, onOpen);
  }, [openTab]);
  const openTabRef = useRef(openTab);
  openTabRef.current = openTab;
  const closeDock = useCallback(() => (mobile ? setMobileTab(null) : setDock((d) => ({ ...d, open: false }))), [mobile, setDock]);
  const focusAgent = useCallback(() => {
    openTab("agent");
    setTimeout(() => agentRef.current?.focus(), 30);
  }, [openTab]);
  const askAgent = useCallback(
    (prompt: string) => {
      openTab("agent");
      void runAnalysis(prompt);
    },
    [openTab, runAnalysis],
  );

  const saveLayout = useCallback(() => {
    const list = readStored<SavedWorkspace[]>(WORKSPACES_KEY, []);
    const cur = readStored<string | null>(CURRENT_WORKSPACE_KEY, null);
    if (cur && list.some((w) => w.id === cur)) {
      writeStored(WORKSPACES_KEY, saveWorkspace(list, { id: cur }));
      setUndoNote(`Saved layout “${list.find((w) => w.id === cur)?.name}”`);
    } else {
      const name = window.prompt("Name this layout", `Layout ${list.length + 1}`);
      if (name === null) return;
      writeStored(WORKSPACES_KEY, saveWorkspace(list, { name }));
      setUndoNote("Layout saved");
    }
  }, []);

  // ------------------------------------------------------------------ keyboard shortcuts
  const stepWatchlist = (dir: 1 | -1) => {
    if (!watchlist.length) return;
    const i = watchlist.indexOf(symbol);
    setSymbol(watchlist[dir === 1 ? (i + 1) % watchlist.length : (i <= 0 ? watchlist.length : i) - 1]);
  };
  const keyHandler = useRef<(e: KeyboardEvent) => void>(() => undefined);
  keyHandler.current = (e: KeyboardEvent) => {
    const el = e.target as HTMLElement;
    const typing = el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT" || el.isContentEditable;
    const key = e.key.toLowerCase();
    if (e.ctrlKey || e.metaKey) {
      if (e.altKey) return;
      if (key === "k") {
        e.preventDefault();
        setSearchMode("chart");
        return;
      }
      if (typing || dialog) return;
      if (key === "z" && !e.shiftKey) {
        e.preventDefault();
        doUndo();
      } else if ((key === "z" && e.shiftKey) || key === "y") {
        e.preventDefault();
        doRedo();
      } else if (key === "s") {
        e.preventDefault();
        saveLayout();
      } else if (key === "d" && selectedId) {
        e.preventDefault();
        duplicateSelected();
      }
      return;
    }
    if (typing || dialog || searchMode) return;
    if (e.altKey) {
      if (e.code === "KeyR") {
        e.preventDefault();
        activeChart()?.fitContent();
      } else if (e.code === "KeyS") {
        e.preventDefault();
        screenshot();
      } else if (e.code === "KeyL") {
        e.preventDefault();
        setLayout((l) => ({ ...l, logScale: !l.logScale }));
      } else if ((e.key === "ArrowUp" || e.key === "ArrowDown") && watchlist.length) {
        e.preventDefault();
        stepWatchlist(e.key === "ArrowDown" ? 1 : -1);
      }
      return;
    }
    if (e.key === "Escape") {
      setTool("crosshair");
      setSelectedId(null);
      if (mobile) setMobileTab(null);
      return;
    }
    if ((e.key === "Delete" || e.key === "Backspace") && selectedId) {
      e.preventDefault();
      deleteSelected();
      return;
    }
    if ((e.key === "Delete" || e.key === "Backspace") && pickedLevel) {
      e.preventDefault();
      removePickedLevel();
      return;
    }
    if (e.key === "/") {
      e.preventDefault();
      focusAgent();
      return;
    }
    if (e.key === "?") {
      setDialog("shortcuts");
      return;
    }
    if (/^[0-9]$/.test(e.key)) {
      const tf = TIMEFRAMES[e.key === "0" ? 9 : Number(e.key) - 1];
      if (tf) setInterval(tf.value);
      return;
    }
    if (e.key === "[" || e.key === "]") {
      stepWatchlist(e.key === "]" ? 1 : -1);
      return;
    }
    if (e.key === " ") {
      e.preventDefault();
      setSearchMode("chart");
      return;
    }
    const panelKeys: Record<string, string> = { w: "watchlist", a: "alerts", l: "layers" };
    for (const p of EXTRA_PANELS) if (p.hotkey) panelKeys[p.hotkey] = p.id;
    if (panelKeys[key]) return openTab(panelKeys[key], true);
    if (key === "d") return mobile ? setMobileTab(null) : setDock((d) => ({ ...d, open: !d.open }));
    if (key === "s") return setSearchMode("chart");
    if (key === "g" && !mobile) return setGridMode(NEXT_GRID[gridMode]);
    if (key === "k") return setIndicators((ind) => ({ ...ind, kimi: !ind.kimi }));
    if (key === "i") return setDialog("indicators");
    if (TOOL_HOTKEYS[key] && !locked) setTool(TOOL_HOTKEYS[key]);
  };
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => keyHandler.current(e);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // ------------------------------------------------------------------ crosshair sync across the grid
  const onCrosshairTime = useCallback(
    (from: number, time: number | null) => {
      if (gridMode === 1 || layout.syncCrosshair === false) return;
      for (let i = 0; i < gridMode; i++) if (i !== from) cellRefs.current[i]?.setCrosshairTime(time);
    },
    [gridMode, layout.syncCrosshair],
  );

  const change24 = feed.open24 && Number.isFinite(feed.price) ? ((feed.price - feed.open24) / feed.open24) * 100 : null;
  const price = Number.isFinite(feed.price) ? feed.price : null;

  // ------------------------------------------------------------------ side-panel tabs
  /** "Log trade" on a plan card: track it in the journal, sized with the user's position-sizing settings. */
  const logTrade = useCallback(async (m: AgentMessage) => {
    if (!m.plan || !m.symbol || !m.interval) return false;
    const sized = sizePlan(m.plan, { ...DEFAULT_SIZING, ...readStored<Partial<SizingSettings>>("ac:sizing", {}) });
    try {
      await createJournalEntry(planToJournalEntry(m.plan, m.symbol, m.interval, sized ? { size_qty: sized.qty, risk_usd: sized.riskUsd } : {}));
      return true;
    } catch {
      return false;
    }
  }, []);

  /** A market-scanner setup in an agent answer: its chart with the plan drawn (the Scanner tab's overlay set). */
  const openSetup = useCallback(
    (s: MarketSetup) => {
      pickSymbol(s.symbol, s.interval);
      onChartOverlays("scanner", s.symbol, s.overlays);
      if (mobile) setMobileTab(null);
    },
    [pickSymbol, onChartOverlays, mobile],
  );
  /** A grid coin in an agent answer: its chart with the range drawn. */
  const openGridCoin = useCallback(
    (c: GridCoin) => {
      pickSymbol(c.symbol, c.interval);
      onChartOverlays("scanner", c.symbol, gridCoinOverlays(c));
      if (mobile) setMobileTab(null);
    },
    [pickSymbol, onChartOverlays, mobile],
  );
  /** "Alert on 5m confirmation" on a plan card: a trigger alert on the plan's entry zone. */
  const planTrigger = useCallback(
    async (m: AgentMessage, tf: TriggerInterval) => {
      const plan = m.plan;
      if (!plan || !m.symbol || plan.zone_low == null || plan.zone_high == null) return false;
      const made = await addTrigger({
        symbol: m.symbol,
        interval: tf,
        zone: {
          source: "fixed", price_low: plan.zone_low, price_high: plan.zone_high, direction: plan.direction,
          label: plan.basis.slice(0, 120),
        },
        confirm: "any",
      });
      return made != null;
    },
    [addTrigger],
  );
  const sellWatch = useCallback(
    async (w: SellWatch) => {
      const made = await Promise.all(
        SELL_SIGNALS.map((signal) => addSignal({ symbols: w.symbols, interval: w.interval, signal, repeat: true })),
      );
      return made.every((m) => m != null);
    },
    [addSignal],
  );
  const removePickedLevel = useCallback(() => {
    if (!pickedLevel) return;
    const cur = readStored<Overlay[]>(overlaysKey, []);
    const same = (o: Overlay) => JSON.stringify(o) === JSON.stringify(pickedLevel);
    changeOverlays(overlaysKey, cur.filter((o) => !same(o)), `remove ${pickedLevel.label || "AI level"}`);
    setPickedLevel(null);
  }, [pickedLevel, overlaysKey, changeOverlays]);
  useEffect(() => setPickedLevel(null), [overlaysKey]);
  const triggerZones = useMemo(() => chartZones(overlays, drawings, selectedId), [overlays, drawings, selectedId]);

  const dockProps: DockPanelProps = { symbol, interval, price, watchlist, onPickSymbol: pickSymbol, onChartOverlays };
  const tabs: DockTab[] = [
    {
      id: "agent",
      label: "Chart agent",
      icon: Bot,
      render: () => (
        <AgentPanel
          handleRef={agentRef}
          busy={busy}
          messages={messages}
          overlayCount={overlays.length}
          pinned={pinnedSet}
          onSubmit={(p) => void runAnalysis(p)}
          onClearOverlays={() => changeOverlays(overlaysKey, [], "clear AI levels")}
          onNewChat={newChat}
          chats={chats}
          onOpenChat={openChat}
          onDeleteChat={deleteChat}
          onPickSymbol={setSymbol}
          onOpenSetup={openSetup}
          onOpenGridCoin={openGridCoin}
          spotOnly={spotOnly}
          onSpotOnly={setSpotOnly}
          walkNote={walkNote}
          onTogglePin={togglePin}
          onLogTrade={logTrade}
          onPlanTrigger={planTrigger}
          onSellWatch={sellWatch}
        />
      ),
    },
    {
      id: "watchlist",
      label: "Watchlist",
      icon: List,
      render: () => (
        <Watchlist
          lists={lists}
          activeList={currentList.id}
          onLists={setLists}
          onActiveList={setActiveList}
          sort={sort}
          onSort={setSort}
          active={symbol}
          interval={interval}
          cells={mobile ? 1 : 4}
          onPick={(s) => {
            setSymbol(s);
            if (mobile) setMobileTab(null);
          }}
          onOpenInCell={(s, i) => {
            setCells((cs) => {
              const next = [...cs, ...DEFAULT_CELLS.slice(cs.length)];
              next[i] = { ...next[i], symbol: s };
              return next;
            });
            if (gridMode === 1 && i > 0) setGridMode(i > 1 ? 4 : 2);
            setActive(i);
          }}
          onAdd={() => setSearchMode("watchlist")}
          onScan={() => askAgent("Scan my watchlist: which coins are near a zone?")}
        />
      ),
    },
    {
      id: "alerts",
      label: "Alerts",
      icon: Bell,
      badge: armedAlerts,
      render: () => (
        <AlertsPanel {...dockProps} api={alertsApi} zones={triggerZones} />
      ),
    },
    {
      id: "layers",
      label: "Layers",
      icon: LayersIcon,
      render: () => (
        <LayersPanel
          symbol={symbol}
          visibility={visibility}
          counts={layerCounts}
          pins={Object.entries(pins).map(([id, p]) => ({ id, label: p.label, count: p.overlays.length }))}
          drawings={drawings}
          selectedId={selectedId}
          onVisibility={setVisibility}
          onUnpin={unpin}
          onSelectDrawing={(id) => {
            const d = drawings.find((x) => x.id === id);
            if (d?.style?.timeframes?.length && !d.style.timeframes.includes(interval)) setInterval(d.style.timeframes[0]);
            setSelectedId(id);
          }}
          onDeleteDrawing={(id) => changeDrawings(drawings.filter((d) => d.id !== id))}
          aiLevels={overlays}
          onDeleteAiLevel={(i) => {
            const o = overlays[i];
            changeOverlays(overlaysKey, overlays.filter((_, j) => j !== i), `remove ${o?.label || "AI level"}`);
          }}
        />
      ),
    },
    ...EXTRA_PANELS.map<DockTab>((def) => ({
      id: def.id,
      label: def.label,
      icon: def.icon,
      render: () => <def.Component {...dockProps} />,
    })),
  ];

  const cellList = gridMode === 1 ? [active] : Array.from({ length: gridMode }, (_, i) => i);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <ChartHeader
        symbol={symbol}
        interval={interval}
        price={price}
        change24={change24}
        source={feed.source as DataSource}
        indicators={indicators}
        indicatorSettings={indicatorSettings}
        compare={compare}
        watchlist={watchlist}
        mobile={mobile}
        gridMode={gridMode}
        onInterval={setInterval}
        onSearch={() => setSearchMode("chart")}
        onIndicators={setIndicators}
        onIndicatorSettings={() => setDialog("indicators")}
        onCompare={setCompare}
        onCompareSearch={() => setSearchMode("compare")}
        onOpenSymbol={setSymbol}
        onScreenshot={screenshot}
        onFit={() => activeChart()?.fitContent()}
        onSettings={() => setDialog("settings")}
        onShortcuts={() => setDialog("shortcuts")}
        onGridMode={setGridMode}
      />
      <div className="relative flex min-h-0 flex-1">
        {!mobile && (
          <DrawingToolbar
            tool={tool}
            magnet={magnet}
            locked={locked}
            hasSelection={!!selectedId}
            canAlert={!!selectedAlert}
            onAlert={alertOnSelected}
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
            undoLabel={undoLabel}
            redoLabel={redoLabel}
            onUndo={doUndo}
            onRedo={doRedo}
          />
        )}
        <main className="relative min-w-0 flex-1">
          <div className={`grid h-full w-full gap-px bg-line ${GRID_CLASS[gridMode]}`}>
            {cellList.map((i) => {
              const c = cells[i] ?? DEFAULT_CELLS[i];
              return (
                <ChartCell
                  key={`cell-${i}`}
                  ref={(h) => {
                    cellRefs.current[i] = h;
                  }}
                  cell={c}
                  showHeader={gridMode > 1}
                  indicators={indicators}
                  indicatorSettings={indicatorSettings}
                  layout={layout}
                  visibility={visibility}
                  kimiParts={kimiParts}
                  panels={panels}
                  alertOverlays={alertOverlaysFor}
                  compare={compareFor(c.symbol)}
                  onActivate={() => setActive(i)}
                  onCrosshairTime={(t) => onCrosshairTime(i, t)}
                  active={
                    i === active
                      ? {
                          tool,
                          magnet,
                          locked,
                          overlays: composed.visible,
                          drawings: shownDrawings,
                          selectedId,
                          onDrawingsChange: onChartDrawings,
                          onSelect: setSelectedId,
                          onPickOverlay: (o) => setPickedLevel(o && overlays.includes(o) ? o : null),
                          onToolDone: () => setTool("crosshair"),
                          onFeed: setFeed,
                          onDataReady,
                          onError: setError,
                          onAlertMove: (id, patch) => void updateAlert(id, patch),
                        }
                      : null
                  }
                />
              );
            })}
          </div>
          {pickedLevel && !selectedDrawing && (
            <div className="absolute left-1/2 top-2 z-20 flex -translate-x-1/2 items-center gap-2 rounded-md border border-line bg-panel px-2 py-1 text-[11px] shadow-lg">
              <span className="h-2.5 w-2.5 rounded-sm" style={{ background: pickedLevel.color }} />
              <span className="max-w-[16rem] truncate text-ink">{pickedLevel.label || "AI level"}</span>
              <button type="button" className="btn-ghost h-6 px-1.5 text-[11px] hover:text-down" onClick={removePickedLevel} title="Remove this level (Del); Ctrl+Z brings it back">
                Remove
              </button>
              <button type="button" className="btn-ghost h-6 w-6 p-0" aria-label="Close" onClick={() => setPickedLevel(null)}>
                ×
              </button>
            </div>
          )}
          {selectedDrawing && !locked && (
            <DrawingStyleBar
              drawing={selectedDrawing}
              interval={interval}
              canAlert={!!selectedAlert}
              onChange={(next) => changeDrawings(drawings.map((d) => (d.id === next.id ? next : d)), "style change")}
              onDuplicate={duplicateSelected}
              onDelete={deleteSelected}
              onAlert={alertOnSelected}
            />
          )}
          {error && (
            <div className="absolute left-1/2 top-12 z-30 -translate-x-1/2 rounded-md border border-down/40 bg-down/10 px-3 py-1.5 text-xs text-down">
              {error} Retrying…
            </div>
          )}
          {undoNote && (
            <div className="pointer-events-none absolute bottom-10 left-1/2 z-30 -translate-x-1/2 rounded-md border border-line bg-panel/95 px-3 py-1.5 text-xs text-ink shadow-lg">
              {undoNote}
            </div>
          )}
          {!agentVisible && !mobile && (
            <QuickPrompt
              expanded={quickbar}
              onExpanded={setQuickbar}
              busy={busy}
              answer={quickAnswer}
              onSubmit={(p) => void runAnalysis(p)}
              onOpen={focusAgent}
              onDismissAnswer={() => setQuickAnswer(null)}
            />
          )}
        </main>
        <Dock
          tabs={tabs}
          open={mobile ? mobileTab !== null : dock.open}
          tab={mobile ? (mobileTab ?? "agent") : dock.tab}
          width={dock.width}
          mobile={mobile}
          onSelect={(t) => openTab(t)}
          onClose={closeDock}
          onWidth={(w) => setDock((d) => ({ ...d, width: w }))}
        />
      </div>
      {mobile && mobileDraw && mobileTab === null && (
        <DrawingToolbar
          horizontal
          tool={tool}
          magnet={magnet}
          locked={locked}
          hasSelection={!!selectedId}
          canAlert={!!selectedAlert}
          onAlert={alertOnSelected}
          drawingCount={drawings.length}
          onTool={setTool}
          onMagnet={setMagnet}
          onLock={setLocked}
          onDelete={deleteSelected}
          undoLabel={undoLabel}
          redoLabel={redoLabel}
          onUndo={doUndo}
          onRedo={doRedo}
        />
      )}
      {mobile && (
        <nav aria-label="Sections" className="flex h-14 shrink-0 items-stretch overflow-x-auto border-t border-line bg-panel scrollbar-none">
          <MobileTab label="Chart" icon={CandlestickChart} on={mobileTab === null && !mobileDraw} onClick={() => { setMobileTab(null); setMobileDraw(false); }} />
          <MobileTab label="Draw" icon={Pencil} on={mobileTab === null && mobileDraw} onClick={() => { setMobileTab(null); setMobileDraw((v) => !v); }} />
          {tabs.map((t) => (
            <MobileTab key={t.id} label={t.label.replace("Chart agent", "Agent")} icon={t.icon} badge={t.badge} on={mobileTab === t.id} onClick={() => openTab(t.id)} />
          ))}
        </nav>
      )}
      <SymbolSearch
        open={searchMode !== null}
        title={searchMode ? SEARCH_TITLES[searchMode] : undefined}
        allowCustom={searchMode !== "watchlist"}
        onClose={() => setSearchMode(null)}
        onPick={(s) => {
          if (searchMode === "watchlist") addToWatchlist(s);
          else if (searchMode === "compare") {
            if (s !== symbol && !compare.some((c) => c.symbol === s)) {
              const used = new Set(compare.map((c) => c.color));
              setCompare([...compare, { symbol: s, color: COMPARE_COLORS.find((c) => !used.has(c)) ?? COMPARE_COLORS[0] }]);
            }
          } else setSymbol(s);
        }}
      />
      <SettingsDialog open={dialog === "settings"} layout={layout} onLayout={setLayout} onClose={() => setDialog(null)} />
      <IndicatorSettingsDialog
        open={dialog === "indicators"}
        settings={indicatorSettings}
        onChange={setIndicatorSettings}
        onClose={() => setDialog(null)}
      />
      <ShortcutsDialog open={dialog === "shortcuts"} onClose={() => setDialog(null)} />
      <AlertToasts toasts={toasts} onDismiss={(id) => setToasts((t) => t.filter((x) => x.id !== id))} />
    </div>
  );
}

function MobileTab({ label, icon: Icon, on, badge, onClick }: {
  label: string;
  icon: typeof Bot;
  on: boolean;
  badge?: number;
  onClick(): void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={on}
      className={clsx("relative flex min-w-16 flex-1 flex-col items-center justify-center gap-0.5 px-2 text-[10px]", on ? "text-accent" : "text-mute")}
    >
      <Icon className="h-5 w-5" />
      <span className="whitespace-nowrap">{label}</span>
      {!!badge && (
        <span className="absolute right-3 top-1.5 grid h-3.5 min-w-3.5 place-items-center rounded-full bg-yellow-400 px-0.5 text-[9px] font-bold text-black">
          {badge}
        </span>
      )}
    </button>
  );
}

/**
 * The agent's prompt while its panel is closed: one line at the bottom of the chart, with the latest answer
 * above it until dismissed, so the chart keeps its full width.
 */
function QuickPrompt(p: {
  expanded: boolean;
  onExpanded(v: boolean): void;
  busy: boolean;
  answer: AgentMessage | null;
  onSubmit(prompt: string): void;
  onOpen(): void;
  onDismissAnswer(): void;
}) {
  const [value, setValue] = useState("");
  if (!p.expanded) {
    return (
      <button
        type="button"
        onClick={() => p.onExpanded(true)}
        title="Ask the chart agent"
        aria-label="Show the agent prompt"
        className="absolute bottom-9 right-16 z-20 grid h-9 w-9 place-items-center rounded-full border border-line bg-panel/95 text-accent shadow-xl hover:border-accent"
      >
        {p.busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Bot className="h-4 w-4" />}
      </button>
    );
  }
  const submit = () => {
    const t = value.trim();
    if (!t || p.busy) return;
    p.onSubmit(t);
    setValue("");
  };
  return (
    <div className="absolute bottom-9 left-1/2 z-20 w-[min(560px,calc(100%-96px))] -translate-x-1/2">
      {p.answer && !p.busy && (
        <div className="mb-1.5 rounded-lg border border-line bg-panel/95 px-3 py-2 text-[12px] leading-relaxed text-ink shadow-xl backdrop-blur">
          <div className="flex items-start gap-2">
            <p className={clsx("line-clamp-4 min-w-0 flex-1 whitespace-pre-line", p.answer.role === "error" && "text-down")}>{p.answer.text}</p>
            <button type="button" className="btn-ghost h-5 w-5 shrink-0 p-0" aria-label="Dismiss answer" onClick={p.onDismissAnswer}>
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
          <button type="button" className="mt-1 text-[11px] text-accent hover:underline" onClick={p.onOpen}>
            Open the conversation
          </button>
        </div>
      )}
      <div className="flex items-center gap-1.5 rounded-lg border border-line bg-panel/95 px-2 shadow-xl backdrop-blur focus-within:border-accent">
        <button type="button" className="btn-ghost h-7 w-7 shrink-0 p-0" title="Open the chart agent (/)" aria-label="Open the chart agent" onClick={p.onOpen}>
          <MessageSquare className="h-4 w-4" />
        </button>
        <input
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") submit();
            if (e.key === "Escape") (e.target as HTMLInputElement).blur();
          }}
          placeholder="Ask the chart agent, e.g. “key levels on the daily”"
          className="h-9 min-w-0 flex-1 bg-transparent text-[13px] text-ink outline-none placeholder:text-mute"
        />
        {p.busy ? (
          <Loader2 className="h-4 w-4 shrink-0 animate-spin text-accent" />
        ) : (
          <button type="button" className="btn-ghost h-7 w-7 shrink-0 p-0" aria-label="Send" disabled={!value.trim()} onClick={submit}>
            <SendHorizontal className="h-4 w-4" />
          </button>
        )}
        <button type="button" className="btn-ghost h-7 w-7 shrink-0 p-0" title="Shrink to a button" aria-label="Hide prompt bar" onClick={() => p.onExpanded(false)}>
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
    </div>
  );
}
