"use client";

import clsx from "clsx";
import { Grid3x3, Loader2, Play, Radar, Timer, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { usePersistentState } from "@/hooks/usePersistentState";
import { usePolled } from "@/hooks/usePolled";
import type { DockPanelProps } from "@/lib/dock";
import { displaySymbol, formatPrice } from "@/lib/format";
import {
  agreementText,
  agreementTitle,
  fetchMarketScan,
  runMarketScan,
  trackShort,
  trackTone,
  type MarketScanStatus,
} from "@/lib/scanner";
import { gridCoinOverlays, planGridFor, SPOT_ONLY_KEY } from "@/lib/spot";
import { TIMEFRAMES, type GridCoin, type Interval, type MarketScanResult, type MarketSetup } from "@/lib/types";

import TrackRecordLine from "./TrackRecordLine";

type Side = "spot" | "all" | "long" | "short" | "grid";

interface Prefs {
  interval: Interval;
  side: Side;
}

const DEFAULT_PREFS: Prefs = { interval: "4h", side: "spot" };
const POLL_MS = 30_000;
const SIDES: { v: Side; label: string; title: string; futures?: boolean }[] = [
  { v: "spot", label: "Spot buys", title: "Longs at a support or demand zone backed by a higher timeframe: coins to buy outright" },
  { v: "all", label: "Both", title: "Long and short setups", futures: true },
  { v: "long", label: "Longs", title: "Every long setup" },
  { v: "short", label: "Shorts", title: "Short setups (futures)", futures: true },
  { v: "grid", label: "Grid coins", title: "Coins ranging cleanly enough for a Spot Grid bot" },
];

/** Grid coins as two-line rows: coin, range width, crossings, days; the range and a note below. */
export function GridCoinList({ rows, selected, onPick }: { rows: GridCoin[]; selected?: string | null; onPick(c: GridCoin): void }) {
  return (
    <div className="overflow-hidden rounded border border-line">
      <div className="flex items-center gap-2 border-b border-line bg-panel2/60 px-2 py-0.5 text-[10px] uppercase tracking-wide text-mute">
        <span className="min-w-0 flex-1">Coin</span>
        <span className="w-12 text-right" title="Range width, % of its bottom">Width</span>
        <span className="w-10 text-right" title="Times the close crossed the middle of the range">Cross</span>
        <span className="w-12 text-right" title="Where price sits in the range: 0% = bottom, 100% = top">In range</span>
      </div>
      {rows.map((c) => (
        <button
          key={c.symbol}
          type="button"
          onClick={() => onPick(c)}
          title={`${c.note}\nClick to open the chart with the range drawn`}
          className={clsx(
            "block w-full border-b border-line/60 px-2 py-1 text-left text-[11px] last:border-b-0 hover:bg-panel2",
            selected === c.symbol && "bg-panel2",
          )}
        >
          <div className="flex items-center gap-2">
            <span className="flex min-w-0 flex-1 items-center gap-1.5">
              <Grid3x3 className="h-3 w-3 shrink-0 text-amber-400" />
              <span className="truncate font-medium text-ink">{displaySymbol(c.symbol)}</span>
            </span>
            <span className="w-12 text-right font-mono text-ink">{c.width_pct.toFixed(1)}%</span>
            <span className="w-10 text-right font-mono text-mute">{c.crossings}</span>
            <span className="w-12 text-right font-mono text-mute">{c.position_pct.toFixed(0)}%</span>
          </div>
          <div className="mt-0.5 flex gap-2 pl-[1.1rem] font-mono text-[10px] text-mute">
            <span>{formatPrice(c.low)} – {formatPrice(c.high)}</span>
            <span>· {c.days.toFixed(0)}d</span>
          </div>
        </button>
      ))}
    </div>
  );
}

function ago(ms: number, now: number): string {
  const m = Math.max(0, Math.round((now - ms) / 60000));
  if (m < 1) return "just now";
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  return h < 48 ? `${h}h ago` : `${Math.round(h / 24)}d ago`;
}

function clock(ms: number): string {
  return new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** Setups as two-line rows: coin, side, R:R, distance, agreement and track record; entry, stop and T1 below. */
export function SetupList({ rows, selected, onPick, spot }: { rows: MarketSetup[]; selected?: string | null; onPick(s: MarketSetup): void; spot?: boolean }) {
  return (
    <div className="overflow-hidden rounded border border-line">
      <div className="flex items-center gap-2 border-b border-line bg-panel2/60 px-2 py-0.5 text-[10px] uppercase tracking-wide text-mute">
        <span className="min-w-0 flex-1">Coin</span>
        <span className="w-10 text-right">R:R</span>
        <span className="w-12 text-right">Dist</span>
        <span className="w-9 text-right" title="Timeframes trending the setup's way (a range counts half)">TFs</span>
        <span className="w-24 text-right" title="Backtested track record: win rate · average R · trades">Track</span>
      </div>
      {rows.map((s) => {
        const key = `${s.symbol}:${s.direction}`;
        const long = s.direction === "long";
        return (
          <button
            key={key}
            type="button"
            onClick={() => onPick(s)}
            title={`${s.basis}${s.track_record?.summary ? `\nTrack record: ${s.track_record.summary}` : ""}\nClick to open the chart with the plan`}
            className={clsx(
              "block w-full border-b border-line/60 px-2 py-1 text-left text-[11px] last:border-b-0 hover:bg-panel2",
              selected === key && "bg-panel2",
            )}
          >
            <div className="flex items-center gap-2">
              <span className="flex min-w-0 flex-1 items-center gap-1.5">
                <span className={clsx("w-9 shrink-0 font-semibold", long ? "text-up" : "text-down")}>{long ? (spot ? "Buy" : "Long") : "Short"}</span>
                <span className="truncate font-medium text-ink">{displaySymbol(s.symbol)}</span>
              </span>
              <span className="w-10 text-right font-mono text-ink">{s.rr.toFixed(1)}R</span>
              <span className="w-12 text-right font-mono text-mute" title={`${s.distance_atr} ATR from price`}>
                {s.distance_pct === 0 ? "at mkt" : `${s.distance_pct.toFixed(1)}%`}
              </span>
              <span className="w-9 text-right font-mono text-mute" title={agreementTitle(s)}>{agreementText(s)}</span>
              <span className={clsx("w-24 truncate text-right font-mono", trackTone(s.track_record))}>{trackShort(s.track_record)}</span>
            </div>
            <div className="mt-0.5 flex gap-2 pl-[2.6rem] font-mono text-[10px] text-mute">
              <span>E <span className="text-accent">{formatPrice(s.entry)}</span></span>
              <span>S <span className="text-down">{formatPrice(s.stop)}</span></span>
              <span>T1 <span className="text-up">{formatPrice(s.target)}</span></span>
            </div>
          </button>
        );
      })}
    </div>
  );
}

/**
 * The market-wide setup scanner: the best long and short plans across the top coins by 24h volume, ranked by
 * reward-to-risk, distance to entry, timeframe agreement and track record. Clicking a setup opens its chart with the
 * plan drawn. Scans run on demand here and on the backend's timer (MARKET_SCAN_SCHEDULE).
 */
export default function ScannerPanel(p: DockPanelProps) {
  const [prefs, setPrefs] = usePersistentState<Prefs>("ac:scanner", DEFAULT_PREFS);
  const [spotOnly] = usePersistentState(SPOT_ONLY_KEY, true);
  const stored = { ...DEFAULT_PREFS, ...prefs };
  // Spot mode has no use for shorts: "Both" and "Shorts" fall back to the spot buys.
  const pr = spotOnly && (stored.side === "all" || stored.side === "short") ? { ...stored, side: "spot" as Side } : stored;
  const [gridBusy, setGridBusy] = useState<string | null>(null);
  const [scanned, setScanned] = useState<MarketScanResult | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [drawnFor, setDrawnFor] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const { onChartOverlays } = p;

  const polled = usePolled<MarketScanStatus>(pr.interval, (signal) => fetchMarketScan(pr.interval, signal), POLL_MS);
  const status = polled.data;
  // The newer of the polled result (timer scans) and the one this tab just ran.
  const fromRun = scanned?.interval === pr.interval ? scanned : null;
  const result = [status?.result ?? null, fromRun].reduce<MarketScanResult | null>(
    (best, r) => (r && (!best || r.generated_at > best.generated_at) ? r : best),
    null,
  );
  const busy = running || !!status?.running.includes(pr.interval);

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 30_000);
    return () => {
      clearInterval(t);
      abortRef.current?.abort();
    };
  }, []);

  const run = async () => {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setRunning(true);
    setError(null);
    try {
      const res = await runMarketScan(pr.interval, ctrl.signal);
      if (res.result) setScanned(res.result);
      setNow(Date.now());
    } catch (err) {
      if ((err as Error).name !== "AbortError") setError((err as Error).message);
    } finally {
      if (abortRef.current === ctrl) setRunning(false);
    }
  };

  const clearChart = () => {
    if (drawnFor) onChartOverlays("scanner", drawnFor, []);
    setDrawnFor(null);
    setSelected(null);
  };

  const pick = (s: MarketSetup) => {
    if (drawnFor && drawnFor !== s.symbol) onChartOverlays("scanner", drawnFor, []);
    p.onPickSymbol(s.symbol, s.interval);
    onChartOverlays("scanner", s.symbol, s.overlays);
    setDrawnFor(s.symbol);
    setSelected(`${s.symbol}:${s.direction}`);
  };

  const pickGrid = (c: GridCoin) => {
    if (drawnFor && drawnFor !== c.symbol) onChartOverlays("scanner", drawnFor, []);
    p.onPickSymbol(c.symbol, c.interval);
    onChartOverlays("scanner", c.symbol, gridCoinOverlays(c));
    setDrawnFor(c.symbol);
    setSelected(c.symbol);
  };

  const planGrid = async (symbol: string) => {
    setGridBusy(symbol);
    setError(null);
    try {
      await planGridFor(symbol);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setGridBusy(null);
    }
  };

  const rows = !result
    ? []
    : pr.side === "spot"
      ? [...(result.spot_buys ?? [])].sort((a, b) => (b.spot_score ?? 0) - (a.spot_score ?? 0))
      : pr.side === "grid"
        ? []
        : [...(pr.side !== "short" ? result.longs : []), ...(pr.side !== "long" ? result.shorts : [])].sort((a, b) => b.score - a.score);
  const gridRows = result && pr.side === "grid" ? (result.grid_coins ?? []) : [];
  const picked = rows.find((s) => `${s.symbol}:${s.direction}` === selected);
  const pickedGrid = gridRows.find((c) => c.symbol === selected);
  const every = status?.schedule[pr.interval];
  const next = status?.next_run[pr.interval];
  const field = "h-7 rounded border border-line bg-base px-2 text-[11px] text-ink outline-none focus:border-accent";

  return (
    <div className="flex h-full min-h-0 flex-col text-xs">
      <div className="flex items-center gap-1.5 border-b border-line px-3 py-2">
        <Radar className="h-4 w-4 text-accent" />
        <span className="font-semibold text-ink">Best setups across the market</span>
      </div>
      <div className="flex flex-wrap items-center gap-1.5 border-b border-line px-3 py-2 text-[11px] text-mute">
        <select className={field} value={pr.interval} aria-label="Timeframe"
          onChange={(e) => setPrefs({ ...pr, interval: e.target.value as Interval })}>
          {TIMEFRAMES.map((t) => (
            <option key={t.value} value={t.value}>{t.label}</option>
          ))}
        </select>
        <div className="flex overflow-hidden rounded border border-line">
          {SIDES.filter((s) => !(spotOnly && s.futures)).map((s) => (
            <button key={s.v} type="button" title={s.title} onClick={() => setPrefs({ ...pr, side: s.v })}
              className={clsx("h-7 px-2 text-[11px]", pr.side === s.v ? "bg-accent/15 text-accent" : "text-mute hover:text-ink")}>
              {s.label}
            </button>
          ))}
        </div>
        <div className="flex-1" />
        {drawnFor && (
          <button type="button" onClick={clearChart} className="btn-ghost h-7 w-7 p-0" title="Remove the plan from the chart">
            <X className="h-3.5 w-3.5" />
          </button>
        )}
        <button type="button" onClick={run} disabled={busy}
          className="inline-flex h-7 items-center gap-1 rounded bg-accent px-3 text-[11px] font-semibold text-white disabled:opacity-60">
          {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
          {busy ? "Scanning…" : "Scan"}
        </button>
      </div>

      <div className="min-h-0 flex-1 space-y-2 overflow-y-auto px-3 py-2">
        {result && (
          <div className="text-[11px] text-mute">
            {result.universe_source === "binance" ? `Top ${result.universe} coins by 24h volume` : `${result.universe} fallback coins`}{" "}
            on {result.interval} · {result.scanned} scanned · {ago(result.generated_at, now)} ({result.seconds}s
            {result.trigger === "timer" ? ", timed" : ""})
            {result.data_source === "synthetic" && (
              <span className="ml-1 rounded border border-yellow-400/40 px-1 text-[10px] font-semibold text-yellow-300">DEMO DATA</span>
            )}
          </div>
        )}
        {every && (
          <div className="flex items-center gap-1 text-[11px] text-mute">
            <Timer className="h-3 w-3" /> Scans every {every} min{next ? `, next ${clock(next)}` : ""}
            {status && status.notify_top > 0 && (status.channels.telegram || status.channels.discord) && (
              <> · top {status.notify_top} sent to {[status.channels.telegram && "Telegram", status.channels.discord && "Discord"].filter(Boolean).join(" and ")}</>
            )}
          </div>
        )}
        {(error || polled.error) && <div className="rounded bg-down/10 px-2 py-1 text-[11px] text-down">{error || polled.error}</div>}
        {result?.notes.map((n) => (
          <p key={n} className="text-[11px] leading-snug text-yellow-300/90">{n}</p>
        ))}
        {!result && !busy && !polled.loading && (
          <p className="py-2 text-[12px] leading-relaxed text-mute">
            Press Scan to rank the best {spotOnly ? "spot buys, long setups and grid coins" : "long and short setups"} across the top {status?.top ?? 100} coins by 24h volume:
            entries at detected zones, ranked by reward-to-risk, how close the entry is, how many timeframes agree and
            how the same setup did on that coin in the backtest. The first scan downloads a lot of candles and can take
            a minute.
          </p>
        )}
        {busy && !result && (
          <div className="flex items-center gap-2 py-2 text-mute">
            <Loader2 className="h-4 w-4 animate-spin" /> Scanning the market…
          </div>
        )}
        {result && pr.side === "spot" && !result.spot_buys && (
          <p className="text-[12px] text-mute">This scan is from an older version; press Scan for spot buys and grid coins.</p>
        )}
        {result && pr.side === "grid" && gridRows.length === 0 && (
          <p className="text-[12px] text-mute">No coin has been ranging cleanly enough for a grid bot on {result.interval}.</p>
        )}
        {result && pr.side !== "grid" && rows.length === 0 && (pr.side !== "spot" || result.spot_buys) && (
          <p className="text-[12px] text-mute">
            {pr.side === "spot" ? "No longs at higher-timeframe demand right now." : "No setups with at least 1R to T1 right now."}
          </p>
        )}
        {rows.length > 0 && <SetupList rows={rows} selected={selected} onPick={pick} spot={spotOnly} />}
        {gridRows.length > 0 && <GridCoinList rows={gridRows} selected={selected} onPick={pickGrid} />}
        {pickedGrid && (
          <div className="rounded border border-line bg-base/60 p-2 text-[11px]">
            <p className="text-mute">{pickedGrid.note}</p>
            <button
              type="button"
              disabled={gridBusy === pickedGrid.symbol}
              onClick={() => void planGrid(pickedGrid.symbol)}
              className="mt-1.5 inline-flex h-6 items-center gap-1 rounded border border-line px-1.5 text-[11px] text-ink hover:border-accent disabled:opacity-60"
            >
              {gridBusy === pickedGrid.symbol ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Grid3x3 className="h-3.5 w-3.5" />}
              Plan a grid bot and test it on 90 days
            </button>
          </div>
        )}
        {picked && (
          <div className="rounded border border-line bg-base/60 p-2 text-[11px]">
            <div className="text-mute">
              <span className={clsx("font-semibold", picked.direction === "long" ? "text-up" : "text-down")}>
                {picked.direction === "long" ? "Long" : "Short"} {displaySymbol(picked.symbol)}
              </span>{" "}
              from {picked.basis} · risk {picked.risk_pct}%
            </div>
            {picked.plan.notes.map((n) => (
              <p key={n} className="mt-1 text-mute">{n}</p>
            ))}
            {picked.track_record && <TrackRecordLine tr={picked.track_record} />}
          </div>
        )}
      </div>
    </div>
  );
}
