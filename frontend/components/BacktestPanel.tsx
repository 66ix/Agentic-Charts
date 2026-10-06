"use client";

import clsx from "clsx";
import { FlaskConical, Loader2, MapPin, Play, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import EquityCurve from "@/components/EquityCurve";
import { usePersistentState } from "@/hooks/usePersistentState";
import {
  BACKTEST_SETUPS,
  BACKTEST_TARGETS,
  backtestAllOverlays,
  backtestTradeOverlays,
  runBacktest,
  type BacktestResult,
  type BacktestSetup,
  type BacktestTarget,
  type BacktestTrade,
} from "@/lib/backtest";
import type { DockPanelProps } from "@/lib/dock";
import { displaySymbol, formatPrice } from "@/lib/format";
import { INTERVAL_SECONDS, TIMEFRAMES, type Interval } from "@/lib/types";

interface Form {
  /** Empty = the active chart's coin / timeframe. */
  coin: string;
  interval: "" | Interval;
  setup: BacktestSetup;
  bars: number;
  target: BacktestTarget;
  maxHold: number;
  fee: number;
}

const DEFAULT_FORM: Form = { coin: "", interval: "", setup: "demand_long", bars: 2000, target: "2R", maxHold: 50, fee: 0.1 };
const RESULT_CLS = { win: "text-up", loss: "text-down", timeout: "text-mute" } as const;

function fmtR(r: number | null | undefined, digits = 2): string {
  if (r == null || !Number.isFinite(r)) return "–";
  return `${r >= 0 ? "+" : "−"}${Math.abs(r).toFixed(digits)}R`;
}

function tone(r: number | null | undefined): string {
  return r == null || Math.abs(r) < 1e-9 ? "text-ink" : r > 0 ? "text-up" : "text-down";
}

function date(t: number, withTime = true): string {
  return new Date(t * 1000).toLocaleString([], withTime
    ? { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }
    : { year: "numeric", month: "short", day: "numeric" });
}

function normCoin(s: string): string {
  const v = s.replace(/[\s/$-]/g, "").toUpperCase();
  return !v ? "" : /(USDT|USDC|FDUSD|BTC|ETH)$/.test(v) && v.length > 4 ? v : `${v}USDT`;
}

function Card({ label, value, cls }: { label: string; value: string; cls?: string }) {
  return (
    <div className="rounded border border-line bg-panel2/60 px-2 py-1.5">
      <div className="text-[10px] uppercase tracking-wide text-mute">{label}</div>
      <div className={clsx("font-mono text-[13px] font-semibold", cls ?? "text-ink")}>{value}</div>
    </div>
  );
}

/**
 * Backtest a setup on the active coin (or any other): the agent's own zone, support/resistance and sweep detectors
 * replayed candle by candle with no lookahead, or Kimi Cooked's own resolved signals. Click a trade to see it on the
 * chart.
 */
export default function BacktestPanel(p: DockPanelProps) {
  const [form, setForm] = usePersistentState<Form>("ac:backtest-form", DEFAULT_FORM);
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [picked, setPicked] = useState<number | null>(null);
  const [showAll, setShowAll] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const drawnFor = useRef<string | null>(null);
  const { onChartOverlays } = p;

  const f = { ...DEFAULT_FORM, ...form };
  const symbol = normCoin(f.coin) || p.symbol;
  const interval = f.interval || p.interval;
  const set = (patch: Partial<Form>) => setForm({ ...f, ...patch });
  const kimi = f.setup.startsWith("kimi");

  useEffect(() => () => abortRef.current?.abort(), []);

  const clearChart = () => {
    if (drawnFor.current) onChartOverlays("backtest", drawnFor.current, []);
    drawnFor.current = null;
    setPicked(null);
    setShowAll(false);
  };

  const run = async () => {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setRunning(true);
    setError(null);
    clearChart();
    try {
      const res = await runBacktest(
        { symbol, interval, setup: f.setup, bars: f.bars, target: f.target, max_hold_bars: f.maxHold, fee_pct: f.fee },
        ctrl.signal,
      );
      setResult(res);
    } catch (err) {
      if ((err as Error).name !== "AbortError") setError((err as Error).message);
    } finally {
      if (abortRef.current === ctrl) setRunning(false);
    }
  };

  const draw = (res: BacktestResult, overlays: ReturnType<typeof backtestAllOverlays>) => {
    if (drawnFor.current && drawnFor.current !== res.symbol) onChartOverlays("backtest", drawnFor.current, []);
    if (res.symbol !== p.symbol || res.interval !== p.interval) p.onPickSymbol(res.symbol, res.interval);
    onChartOverlays("backtest", res.symbol, overlays);
    drawnFor.current = res.symbol;
  };

  const pickTrade = (i: number, t: BacktestTrade) => {
    if (!result) return;
    setPicked(i);
    setShowAll(false);
    draw(result, backtestTradeOverlays(t, INTERVAL_SECONDS[result.interval]));
  };

  const toggleAll = () => {
    if (!result) return;
    if (showAll) return clearChart();
    setShowAll(true);
    setPicked(null);
    draw(result, backtestAllOverlays(result));
  };

  const s = result?.stats;
  const field = "h-7 w-full rounded border border-line bg-base px-2 text-[11px] text-ink outline-none focus:border-accent";
  const num = (v: string, lo: number, hi: number, fallback: number) => {
    const n = Number(v);
    return Number.isFinite(n) ? Math.min(hi, Math.max(lo, n)) : fallback;
  };

  return (
    <div className="flex h-full min-h-0 flex-col text-xs">
      <div className="flex items-center gap-1.5 border-b border-line px-3 py-2">
        <FlaskConical className="h-4 w-4 text-accent" />
        <span className="font-semibold text-ink">Backtest a setup</span>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="grid grid-cols-2 gap-1.5 border-b border-line px-3 py-2 text-[11px] text-mute">
          <label>
            Coin
            <input className={clsx(field, "font-mono")} value={f.coin} placeholder={displaySymbol(p.symbol)}
              onChange={(e) => set({ coin: e.target.value })} title="Leave empty to use the chart's coin" />
          </label>
          <label>
            Timeframe
            <select className={field} value={f.interval} onChange={(e) => set({ interval: e.target.value as "" | Interval })}>
              <option value="">Chart ({p.interval})</option>
              {TIMEFRAMES.map((t) => (
                <option key={t.value} value={t.value}>{t.label}</option>
              ))}
            </select>
          </label>
          <label className="col-span-2">
            Setup
            <select className={field} value={f.setup} onChange={(e) => set({ setup: e.target.value as BacktestSetup })}>
              {BACKTEST_SETUPS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
            <span className="mt-0.5 block text-[10px]">{BACKTEST_SETUPS.find((o) => o.value === f.setup)?.hint}</span>
          </label>
          <label>
            Candles
            <input className={clsx(field, "font-mono")} type="number" min={100} max={5000} step={100} value={f.bars}
              onChange={(e) => set({ bars: num(e.target.value, 100, 5000, 2000) })} />
          </label>
          <label className={clsx(kimi && "opacity-50")}>
            Exit at
            <select className={field} value={f.target} disabled={kimi} onChange={(e) => set({ target: e.target.value as BacktestTarget })}>
              {BACKTEST_TARGETS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </label>
          <label className={clsx(kimi && "opacity-50")}>
            Max hold (candles)
            <input className={clsx(field, "font-mono")} type="number" min={1} max={2000} value={f.maxHold} disabled={kimi}
              onChange={(e) => set({ maxHold: num(e.target.value, 1, 2000, 50) })} />
          </label>
          <label className={clsx(kimi && "opacity-50")}>
            Fee per side %
            <input className={clsx(field, "font-mono")} type="number" min={0} max={1} step={0.01} value={f.fee} disabled={kimi}
              onChange={(e) => set({ fee: num(e.target.value, 0, 1, 0.1) })} />
          </label>
          <div className="col-span-2 flex items-center gap-2 pt-1">
            <span className="min-w-0 flex-1 truncate">
              {displaySymbol(symbol)} · {interval} · last {f.bars.toLocaleString()} candles
            </span>
            <button type="button" onClick={run} disabled={running}
              className="inline-flex h-7 items-center gap-1 rounded bg-accent px-3 text-[11px] font-semibold text-white disabled:opacity-60">
              {running ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
              {running ? "Running…" : "Run"}
            </button>
          </div>
        </div>

        {error && <div className="border-b border-line bg-down/10 px-3 py-2 text-[11px] text-down">{error}</div>}
        {!result && !running && !error && (
          <p className="px-3 py-4 text-[12px] leading-relaxed text-mute">
            Pick a setup and press Run. The agent&apos;s detectors are replayed candle by candle using only the candles
            before each trade, so you see how the setup would actually have played out.
          </p>
        )}

        {result && s && (
          <div className="space-y-3 px-3 py-2">
            <div className="text-[11px] text-mute">
              <span className="font-medium text-ink">{result.setup_name}</span> on {displaySymbol(result.symbol)}{" "}
              {result.interval}, {date(result.from_time, false)} – {date(result.to_time, false)} ({result.bars.toLocaleString()} candles,{" "}
              {result.seconds}s)
              {result.data_source === "synthetic" && (
                <span className="ml-1 rounded border border-yellow-400/40 px-1 text-[10px] font-semibold text-yellow-300">DEMO DATA</span>
              )}
            </div>
            <div className="grid grid-cols-3 gap-1.5">
              <Card label="Trades" value={String(s.count)} />
              <Card label="Win rate" value={s.win_rate == null ? "–" : `${Math.round(s.win_rate * 100)}%`} />
              <Card label="Avg R" value={fmtR(s.avg_r)} cls={tone(s.avg_r)} />
              <Card label="Total R" value={fmtR(s.total_r, 1)} cls={tone(s.total_r)} />
              <Card label="Profit factor" value={s.profit_factor == null ? "–" : s.profit_factor.toFixed(2)} />
              <Card label="Max drawdown" value={s.max_drawdown_r ? `${s.max_drawdown_r.toFixed(1)}R` : "0R"} />
            </div>
            <div className="text-[11px] text-mute">
              {s.wins} won · {s.losses} lost · {s.timeouts} timed out
              {s.avg_bars_held != null && ` · held ${s.avg_bars_held} candles on average`}
            </div>
            <EquityCurve points={result.equity} />
            {result.notes.length > 0 && (
              <ul className="space-y-1 text-[11px] leading-snug">
                {result.notes.map((n) => (
                  <li key={n} className={/^(Only |No trades|These results come from synthetic)/.test(n) ? "text-yellow-300/90" : "text-mute"}>
                    {n}
                  </li>
                ))}
              </ul>
            )}
            {result.trades.length > 0 && (
              <div>
                <div className="mb-1 flex items-center gap-1">
                  <span className="text-[10px] font-semibold uppercase tracking-wide text-mute">Trades</span>
                  <div className="flex-1" />
                  <button type="button" onClick={toggleAll} className={clsx("btn-ghost h-6 px-1.5 text-[11px]", showAll && "text-accent")}>
                    <MapPin className="h-3.5 w-3.5" /> {showAll ? "Hide from chart" : "Show all on chart"}
                  </button>
                  {picked !== null && (
                    <button type="button" onClick={clearChart} className="btn-ghost h-6 w-6 p-0" title="Clear the chart">
                      <X className="h-3.5 w-3.5" />
                    </button>
                  )}
                </div>
                <div className="overflow-hidden rounded border border-line">
                  {[...result.trades].reverse().map((t, k) => {
                    const i = result.trades.length - 1 - k;
                    return (
                      <button key={`${t.entry_time}-${i}`} type="button" onClick={() => pickTrade(i, t)}
                        title={`${t.basis}\nEntry ${formatPrice(t.entry)} · stop ${formatPrice(t.stop)} · target ${formatPrice(t.target)} · exit ${formatPrice(t.exit)}`}
                        className={clsx("flex w-full items-center gap-2 border-b border-line/60 px-2 py-1 text-left text-[11px] last:border-b-0 hover:bg-panel2",
                          picked === i && "bg-panel2")}>
                        <span className={clsx("w-9 shrink-0 font-semibold", t.direction === "long" ? "text-up" : "text-down")}>
                          {t.direction === "long" ? "Long" : "Short"}
                        </span>
                        <span className="w-24 shrink-0 text-mute">{date(t.entry_time)}</span>
                        <span className="min-w-0 flex-1 truncate font-mono text-ink">{formatPrice(t.entry)}</span>
                        <span className={clsx("w-14 shrink-0 text-right", RESULT_CLS[t.result])}>{t.result}</span>
                        <span className={clsx("w-14 shrink-0 text-right font-mono", tone(t.r))}>{fmtR(t.r)}</span>
                      </button>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
