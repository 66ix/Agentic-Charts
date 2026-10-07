"use client";

import clsx from "clsx";
import { ArrowLeft, Eye, EyeOff, Grid3x3, Pencil, Plus, RefreshCw, Sparkles, Trash2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { usePersistentState } from "@/hooks/usePersistentState";
import type { DockPanelProps } from "@/lib/dock";
import { displaySymbol, formatPrice, splitSymbol } from "@/lib/format";
import {
  backtestGrid,
  createGridBot,
  deleteGridBot,
  fetchGridBotResult,
  fetchGridBots,
  fetchGridRealCompare,
  formatQuote,
  GRID_PLAN_EVENT,
  gridLinesFor,
  gridOverlays,
  planGridBot,
  planOverlays,
  simulateGridBot,
  takeGridPlan,
  updateGridBot,
  type BinanceShows,
  type CompareRow,
  type GridBacktestResult,
  type GridBot,
  type GridBotParams,
  type GridBotResult,
  type GridDay,
  type GridPlan,
  type GridRealCompare,
  type GridType,
  type PlanTimeframe,
} from "@/lib/gridbot";
import type { Overlay } from "@/lib/types";

const REFRESH_MS = 30_000;
const DISCLAIMER =
  "Results are simulated from 1-minute candles, so they can differ slightly from Binance (several fills inside " +
  "one minute, fee settings, Binance's fee reserve).";

type View =
  | { kind: "list" }
  | { kind: "add" }
  | { kind: "plan"; seed: GridPlan | null; n: number }
  | { kind: "edit"; id: string }
  | { kind: "detail"; id: string };

function without<T>(map: Record<string, T>, key: string): Record<string, T> {
  if (!(key in map)) return map;
  const next = { ...map };
  delete next[key];
  return next;
}

/**
 * Binance Spot Grid bot tracker. Enter a bot's settings as Binance shows them and see the same numbers (total
 * PnL, grid profit, floating PnL, matched trades, APR), replayed by the backend on 1-minute candles and refreshed
 * every 30 seconds. Each bot can be drawn on the chart (grid lines, range box, recent fills). "Plan" suggests a
 * new bot from the chart (range, grids, type, with the reasons), tests it on the last 7/30/90 days and tracks it.
 */
export default function GridBotPanel(props: DockPanelProps) {
  const { onChartOverlays } = props;
  const [bots, setBots] = useState<GridBot[]>([]);
  const [results, setResults] = useState<Record<string, GridBotResult>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [listError, setListError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [view, setView] = useState<View>({ kind: "list" });
  const [shown, setShown] = usePersistentState<string[]>("ac:gridbot-shown", []);

  const loadResult = useCallback(async (id: string, signal?: AbortSignal) => {
    try {
      const r = await fetchGridBotResult(id, signal);
      setResults((m) => ({ ...m, [id]: r }));
      setErrors((e) => without(e, id));
    } catch (err) {
      if ((err as Error).name === "AbortError") return;
      setErrors((e) => ({ ...e, [id]: (err as Error).message }));
    }
  }, []);

  useEffect(() => {
    const ctrl = new AbortController();
    fetchGridBots(ctrl.signal)
      .then(({ bots: list }) => {
        setBots(list);
        setLoaded(true);
        for (const b of list) void loadResult(b.id, ctrl.signal);
      })
      .catch((err: Error) => {
        if (err.name === "AbortError") return;
        setListError(err.message);
        setLoaded(true);
      });
    return () => ctrl.abort();
  }, [loadResult]);

  // Live numbers: the backend recomputes each bot once per 1m bar, so polling every 30 s is cheap.
  const botsRef = useRef(bots);
  useEffect(() => {
    botsRef.current = bots;
  }, [bots]);
  useEffect(() => {
    const timer = setInterval(() => {
      for (const b of botsRef.current) void loadResult(b.id);
    }, REFRESH_MS);
    return () => clearInterval(timer);
  }, [loadResult]);

  // Chart overlays for the bots toggled on. Only calls back when a bot's drawing actually changed.
  const drawFn = useRef(onChartOverlays);
  useEffect(() => {
    drawFn.current = onChartOverlays;
  }, [onChartOverlays]);
  const drawn = useRef(new Map<string, { symbol: string; sig: string }>());
  useEffect(() => {
    const want = new Map<string, { symbol: string; overlays: Overlay[] }>();
    for (const b of bots) {
      if (shown.includes(b.id)) {
        want.set(`gridbot:${b.id}`, { symbol: b.params.symbol, overlays: gridOverlays(b, results[b.id] ?? null) });
      }
    }
    for (const [key, d] of drawn.current) {
      const next = want.get(key);
      if (!next || next.symbol !== d.symbol) {
        drawFn.current(key, d.symbol, []);
        drawn.current.delete(key);
      }
    }
    for (const [key, { symbol, overlays }] of want) {
      const sig = JSON.stringify(overlays);
      if (drawn.current.get(key)?.sig === sig) continue;
      drawFn.current(key, symbol, overlays);
      drawn.current.set(key, { symbol, sig });
    }
  }, [bots, results, shown]);

  // A plan from the chat agent ("plan a grid bot on INJ"): waiting when the tab mounts, or announced by an event.
  useEffect(() => {
    const take = () => {
      const plan = takeGridPlan();
      if (plan) setView({ kind: "plan", seed: plan, n: Date.now() });
    };
    take();
    window.addEventListener(GRID_PLAN_EVENT, take);
    return () => window.removeEventListener(GRID_PLAN_EVENT, take);
  }, []);
  const startPlan = () => setView({ kind: "plan", seed: null, n: Date.now() });

  const toggleShown = (id: string) => setShown((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]));

  const onSaved = (bot: GridBot, result: GridBotResult) => {
    setBots((list) => (list.some((b) => b.id === bot.id) ? list.map((b) => (b.id === bot.id ? bot : b)) : [...list, bot]));
    setResults((m) => ({ ...m, [bot.id]: result }));
    setErrors((e) => without(e, bot.id));
    setShown((s) => (s.includes(bot.id) ? s : [...s, bot.id]));
    setView({ kind: "detail", id: bot.id });
  };

  const onDelete = async (bot: GridBot) => {
    if (!window.confirm(`Delete "${bot.name}"? This only removes it from the app, not from Binance.`)) return;
    try {
      await deleteGridBot(bot.id);
      setBots((list) => list.filter((b) => b.id !== bot.id));
      setShown((s) => s.filter((x) => x !== bot.id));
      setView({ kind: "list" });
    } catch (err) {
      setErrors((e) => ({ ...e, [bot.id]: (err as Error).message }));
    }
  };

  const current = view.kind === "edit" || view.kind === "detail" ? bots.find((b) => b.id === view.id) : undefined;

  return (
    <div className="flex h-full min-h-0 flex-col text-xs">
      <div className="flex items-center gap-2 border-b border-line px-3 py-2">
        {view.kind !== "list" ? (
          <button type="button" onClick={() => setView({ kind: "list" })} className="btn-ghost h-6 w-6 p-0" aria-label="Back">
            <ArrowLeft className="h-4 w-4" />
          </button>
        ) : (
          <Grid3x3 className="h-4 w-4 text-accent" />
        )}
        <span className="truncate font-semibold text-ink">
          {view.kind === "add"
            ? "Add grid bot"
            : view.kind === "plan"
              ? "Plan a grid bot"
              : view.kind === "edit"
                ? "Edit grid bot"
                : current?.name ?? "Grid bots"}
        </span>
        <div className="flex-1" />
        {view.kind === "list" && (
          <>
            <button type="button" onClick={startPlan} className="btn-ghost h-6 px-1.5 text-[11px]" title="Suggest a bot for a coin">
              <Sparkles className="h-3.5 w-3.5" /> Plan
            </button>
            <button type="button" onClick={() => setView({ kind: "add" })} className="btn-ghost h-6 px-1.5 text-[11px]">
              <Plus className="h-3.5 w-3.5" /> Add bot
            </button>
          </>
        )}
        {view.kind === "detail" && current && (
          <>
            <ShowToggle on={shown.includes(current.id)} onClick={() => toggleShown(current.id)} />
            <button type="button" onClick={() => setView({ kind: "edit", id: current.id })} className="btn-ghost h-6 w-6 p-0" title="Edit">
              <Pencil className="h-3.5 w-3.5" />
            </button>
            <button type="button" onClick={() => onDelete(current)} className="btn-ghost h-6 w-6 p-0 hover:text-down" title="Delete">
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          </>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {view.kind === "list" && (
          <div className="space-y-2 p-3">
            {listError && <div className="rounded border border-down/40 bg-down/10 px-2 py-1.5 text-[11px] text-down">{listError}</div>}
            {loaded && !listError && bots.length === 0 && (
              <div className="space-y-2 py-2 text-[12px] leading-relaxed text-mute">
                <p>
                  Track a Binance Spot Grid bot here: enter its price range, number of grids, investment and how long it
                  has been running, and the app shows its profit, matched trades and APR the way Binance does.
                </p>
                <p>
                  <button type="button" onClick={() => setView({ kind: "add" })} className="text-accent hover:underline">
                    Add your first bot
                  </button>{" "}
                  or{" "}
                  <button type="button" onClick={startPlan} className="text-accent hover:underline">
                    let the app plan one
                  </button>{" "}
                  from the chart.
                </p>
              </div>
            )}
            {!loaded && <div className="text-mute">Loading bots…</div>}
            {bots.map((b) => (
              <BotCard
                key={b.id}
                bot={b}
                result={results[b.id] ?? null}
                error={errors[b.id] ?? null}
                shown={shown.includes(b.id)}
                onToggle={() => toggleShown(b.id)}
                onOpen={() => setView({ kind: "detail", id: b.id })}
                onPickSymbol={() => props.onPickSymbol(b.params.symbol)}
              />
            ))}
          </div>
        )}
        {view.kind === "add" && (
          <BotForm symbol={props.symbol} price={props.price} onSaved={onSaved} onCancel={() => setView({ kind: "list" })} />
        )}
        {view.kind === "plan" && (
          <PlanView
            key={view.n}
            symbol={props.symbol}
            price={props.price}
            seed={view.seed}
            onDraw={onChartOverlays}
            onPickSymbol={props.onPickSymbol}
            onTracked={onSaved}
            onCancel={() => setView({ kind: "list" })}
          />
        )}
        {view.kind === "edit" && current && (
          <BotForm
            symbol={props.symbol}
            price={props.price}
            bot={current}
            onSaved={onSaved}
            onCancel={() => setView({ kind: "detail", id: current.id })}
          />
        )}
        {view.kind === "detail" && current && (
          <BotDetail
            bot={current}
            result={results[current.id] ?? null}
            error={errors[current.id] ?? null}
            onRefresh={() => loadResult(current.id)}
          />
        )}
      </div>
      <p className="border-t border-line px-3 py-2 text-[10px] leading-snug text-mute">{DISCLAIMER}</p>
    </div>
  );
}

// ---------------------------------------------------------------- pieces --

function ShowToggle({ on, onClick }: { on: boolean; onClick(): void }) {
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      className={clsx("btn-ghost h-6 w-6 p-0", on && "text-accent")}
      title={on ? "Hide from chart" : "Show on chart"}
      aria-pressed={on}
    >
      {on ? <Eye className="h-3.5 w-3.5" /> : <EyeOff className="h-3.5 w-3.5" />}
    </button>
  );
}

function tone(v: number) {
  return v > 0 ? "text-up" : v < 0 ? "text-down" : "text-ink";
}

function pct(v: number) {
  return `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(2)}%`;
}

function Stat({ label, children, className }: { label: string; children: ReactNode; className?: string }) {
  return (
    <div className="min-w-0">
      <div className="text-[10px] text-mute">{label}</div>
      <div className={clsx("truncate font-mono text-[11px]", className ?? "text-ink")}>{children}</div>
    </div>
  );
}

function StatusChip({ r }: { r: GridBotResult }) {
  const text =
    r.status === "waiting"
      ? "Waiting for trigger"
      : r.status === "stopped"
        ? r.stop_reason === "take_profit"
          ? "Stopped: take profit"
          : r.stop_reason === "stop_loss"
            ? "Stopped: stop loss"
            : "Stopped"
        : r.in_range
          ? "Running"
          : "Out of range";
  const color = r.status === "running" ? (r.in_range ? "bg-up" : "bg-yellow-400") : "bg-mute";
  return (
    <span className="inline-flex items-center gap-1 whitespace-nowrap rounded border border-line bg-panel2 px-1.5 py-0.5 text-[10px] text-ink">
      <span className={clsx("h-1.5 w-1.5 rounded-full", color)} />
      {text}
    </span>
  );
}

/** Where the price sits between the lower and upper price. */
function RangeBar({ r }: { r: GridBotResult }) {
  const lo = r.lines[0];
  const hi = r.lines[r.lines.length - 1];
  const x = Math.min(100, Math.max(0, r.position_pct));
  return (
    <div>
      <div className="relative h-1.5 rounded-full bg-panel2">
        <div className="absolute inset-y-0 left-0 rounded-full bg-accent/30" style={{ width: `${x}%` }} />
        <div
          className={clsx(
            "absolute top-1/2 h-2.5 w-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-panel",
            r.in_range ? "bg-accent" : "bg-yellow-400",
          )}
          style={{ left: `${x}%` }}
          title={`Price ${formatPrice(r.last_price)}`}
        />
      </div>
      <div className="mt-0.5 flex justify-between font-mono text-[10px] text-mute">
        <span>{formatPrice(lo)}</span>
        <span className="text-ink">{formatPrice(r.last_price)}</span>
        <span>{formatPrice(hi)}</span>
      </div>
      {!r.in_range && r.status === "running" && (
        <div className="mt-1 text-[11px] text-yellow-300">
          Price is {r.position_pct > 100 ? "above" : "below"} the range, so the bot is not trading until it comes back.
        </div>
      )}
    </div>
  );
}

/** The numbers on Binance's bot card. */
function Headline({ r }: { r: GridBotResult }) {
  const q = r.quote_asset;
  return (
    <div className="space-y-2">
      <div className="flex items-baseline gap-2">
        <span className="text-[10px] text-mute">Total PnL</span>
        <span className={clsx("font-mono text-sm font-semibold", tone(r.total_pnl))}>{formatQuote(r.total_pnl, q)}</span>
        <span className={clsx("font-mono text-[11px]", tone(r.total_pnl))}>{pct(r.total_pnl_pct)}</span>
      </div>
      <div className="grid grid-cols-3 gap-x-3 gap-y-1.5">
        <Stat label="Grid profit" className={tone(r.grid_profit)}>{formatQuote(r.grid_profit, "")}</Stat>
        <Stat label="Floating PnL" className={tone(r.floating_pnl)}>{formatQuote(r.floating_pnl, "")}</Stat>
        <Stat label="Grid APR">{r.grid_apr_pct.toFixed(1)}%</Stat>
        <Stat label="Matched trades">
          {r.matched_trades} <span className="text-mute">/ 24h {r.matched_trades_24h}</span>
        </Stat>
        <Stat label="Runtime">{r.runtime_text}</Stat>
        <Stat label="Total APR" className={tone(r.total_apr_pct)}>{r.total_apr_pct.toFixed(1)}%</Stat>
      </div>
    </div>
  );
}

function DemoBadge({ r }: { r: GridBotResult }) {
  if (r.data_source !== "synthetic") return null;
  return (
    <span className="rounded bg-yellow-400/15 px-1 py-0.5 text-[10px] font-semibold text-yellow-300" title="Binance was unreachable">
      DEMO DATA
    </span>
  );
}

function BotCard(p: {
  bot: GridBot;
  result: GridBotResult | null;
  error: string | null;
  shown: boolean;
  onToggle(): void;
  onOpen(): void;
  onPickSymbol(): void;
}) {
  const { bot, result: r } = p;
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={p.onOpen}
      onKeyDown={(e) => e.key === "Enter" && p.onOpen()}
      className="cursor-pointer space-y-2 rounded-lg border border-line bg-panel p-2.5 hover:border-accent/50"
    >
      <div className="flex items-center gap-1.5">
        <div className="min-w-0 flex-1">
          <div className="truncate font-medium text-ink">{bot.name}</div>
          <div className="truncate text-[11px] text-mute">
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                p.onPickSymbol();
              }}
              className="hover:text-accent"
            >
              {displaySymbol(bot.params.symbol)}
            </button>{" "}
            · {bot.params.grids} {bot.params.grid_type} grids · {formatQuote(bot.params.investment, splitSymbol(bot.params.symbol)[1], false)}
          </div>
        </div>
        {r && <DemoBadge r={r} />}
        {r && <StatusChip r={r} />}
        <ShowToggle on={p.shown} onClick={p.onToggle} />
      </div>
      {p.error && <div className="text-[11px] text-down">{p.error}</div>}
      {!r && !p.error && (
        <div className="text-[11px] text-mute">
          Replaying 1-minute candles… (a long-running bot can take a minute the first time)
        </div>
      )}
      {r && (
        <>
          <Headline r={r} />
          <RangeBar r={r} />
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- detail --

function fmtTime(ts: number) {
  return new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function qtyText(v: number) {
  return v.toLocaleString("en-US", { maximumFractionDigits: 8 });
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="space-y-1.5">
      <div className="text-[10px] font-semibold uppercase tracking-wide text-mute">{title}</div>
      {children}
    </div>
  );
}

function CompareTable({ r }: { r: GridBotResult }) {
  const c = r.comparison;
  if (!c) return null;
  const rows: [string, CompareRow | null, boolean][] = [
    ["Matched trades", c.matched_trades, false],
    ["Grid profit", c.grid_profit, true],
    ["Total PnL", c.total_pnl, true],
  ];
  const fmt = (v: number, money: boolean) => (money ? formatQuote(v, "", false) : String(Math.round(v)));
  return (
    <Section title={`Compared with Binance (${fmtTime(c.at)})`}>
      <table className="w-full font-mono text-[11px]">
        <thead>
          <tr className="text-[10px] text-mute">
            <th className="text-left font-normal" />
            <th className="text-right font-normal">Binance</th>
            <th className="text-right font-normal">App</th>
            <th className="text-right font-normal">Diff</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([label, row, money]) =>
            row ? (
              <tr key={label}>
                <td className="font-sans text-mute">{label}</td>
                <td className="text-right text-ink">{fmt(row.binance, money)}</td>
                <td className="text-right text-ink">{fmt(row.app, money)}</td>
                <td className="text-right text-ink">
                  {money ? formatQuote(row.diff, "") : `${row.diff > 0 ? "+" : ""}${Math.round(row.diff)}`}
                  {row.diff_pct != null && <span className="text-mute"> ({pct(row.diff_pct)})</span>}
                </td>
              </tr>
            ) : null,
          )}
        </tbody>
      </table>
      <p className="text-[10px] text-mute">The app&apos;s numbers are taken at the moment you copied Binance&apos;s.</p>
    </Section>
  );
}

/** Matched trades per UTC day, as small columns; hover a day for its numbers. */
function DailyBars({ days, quote }: { days: GridDay[]; quote: string }) {
  const shown = days.slice(-30);
  const [hover, setHover] = useState<number | null>(null);
  if (!shown.length) return null;
  const W = 300;
  const H = 56;
  const band = W / shown.length;
  const bw = Math.min(24, Math.max(2, band - 2));
  const max = Math.max(1, ...shown.map((d) => d.matched));
  const active = shown[hover ?? shown.length - 1];
  const day = new Date(active.day * 1000).toLocaleDateString([], { month: "short", day: "numeric", timeZone: "UTC" });
  return (
    <Section title="Matched trades per day (UTC)">
      <div className="font-mono text-[11px] text-ink">
        {day}: {active.matched} matched · <span className={tone(active.grid_profit)}>{formatQuote(active.grid_profit, quote)}</span>
      </div>
      <svg viewBox={`0 0 ${W} ${H + 1}`} className="h-16 w-full" preserveAspectRatio="none" onMouseLeave={() => setHover(null)}>
        {shown.map((d, i) => {
          const h = (d.matched / max) * (H - 2);
          const x = i * band + (band - bw) / 2;
          const y = H - h;
          const r = Math.min(4, bw / 2, h);
          // Square at the baseline, rounded at the top.
          const path =
            h > 0
              ? `M${x},${H} L${x},${y + r} Q${x},${y} ${x + r},${y} L${x + bw - r},${y} ` +
                `Q${x + bw},${y} ${x + bw},${y + r} L${x + bw},${H} Z`
              : "";
          return (
            <g key={d.day} onMouseEnter={() => setHover(i)}>
              <rect x={i * band} y={0} width={band} height={H} fill="transparent" />
              {path && <path d={path} fill="#3b82f6" opacity={hover == null || hover === i ? 1 : 0.45} />}
            </g>
          );
        })}
        <line x1={0} x2={W} y1={H + 0.5} y2={H + 0.5} stroke="#1f2633" strokeWidth={1} vectorEffect="non-scaling-stroke" />
      </svg>
    </Section>
  );
}

function BotDetail({ bot, result: r, error, onRefresh }: { bot: GridBot; result: GridBotResult | null; error: string | null; onRefresh(): void }) {
  const [allFills, setAllFills] = useState(false);
  if (!r) {
    return (
      <div className="space-y-2 p-3">
        {error ? <div className="text-down">{error}</div> : <div className="text-mute">Replaying 1-minute candles…</div>}
        <button type="button" onClick={onRefresh} className="btn-ghost h-6 px-1.5 text-[11px]">
          <RefreshCw className="h-3.5 w-3.5" /> Retry
        </button>
      </div>
    );
  }
  const base = r.base_asset;
  const q = r.quote_asset;
  const sells = r.open_orders.filter((o) => o.side === "sell").slice(0, 6).reverse();
  const buys = r.open_orders.filter((o) => o.side === "buy").slice(-6).reverse();
  const fills = allFills ? r.recent_fills : r.recent_fills.slice(0, 15);
  return (
    <div className="space-y-4 p-3">
      <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-mute">
        <span>
          {displaySymbol(bot.params.symbol)} · {r.lines.length - 1} {r.grid_type} grids · started {fmtTime(r.start_time)}
        </span>
        <DemoBadge r={r} />
        <StatusChip r={r} />
      </div>
      {error && <div className="text-[11px] text-down">{error}</div>}
      <Headline r={r} />
      <RangeBar r={r} />

      <Section title="Bot">
        <div className="grid grid-cols-2 gap-x-3 gap-y-1.5">
          <Stat label="Investment">{formatQuote(r.investment, q, false)}</Stat>
          <Stat label="Value now">{formatQuote(r.current_value, q, false)}</Stat>
          <Stat label="Profit per grid (after fees)">
            {r.profit_per_grid_min_pct.toFixed(2)}% – {r.profit_per_grid_max_pct.toFixed(2)}%
          </Stat>
          <Stat label="Qty per order">
            {qtyText(r.qty_per_order)} {base}
          </Stat>
          <Stat label={`Holding ${base}`}>{qtyText(r.base_held)}</Stat>
          <Stat label={`Holding ${q}`}>{formatQuote(r.quote_held, "", false)}</Stat>
          <Stat label="Fee per fill">{(r.fee_rate * 100).toFixed(3)}%</Stat>
          <Stat label="Fees paid">{formatQuote(r.fees_paid, q, false)}</Stat>
          <Stat label="Max drawdown" className={r.max_drawdown_pct > 0 ? "text-down" : "text-ink"}>
            {r.max_drawdown_pct > 0 ? `−${r.max_drawdown_pct.toFixed(2)}%` : "0.00%"}
          </Stat>
          <Stat label="Time in range">{r.time_in_range_pct.toFixed(0)}%</Stat>
        </div>
      </Section>

      <CompareTable r={r} />
      <RealVsSim bot={bot} />
      <DailyBars days={r.daily} quote={q} />

      {r.open_orders.length > 0 && (
        <Section title={`Open orders (${r.open_orders.length}) nearest the price`}>
          <div className="space-y-0.5 font-mono text-[11px]">
            {sells.map((o) => (
              <div key={o.line} className="flex justify-between">
                <span className="text-down">Sell</span>
                <span className="text-ink">{formatPrice(o.price)}</span>
              </div>
            ))}
            <div className="border-t border-dashed border-line py-0.5 text-center text-[10px] text-mute">price {formatPrice(r.last_price)}</div>
            {buys.map((o) => (
              <div key={o.line} className="flex justify-between">
                <span className="text-up">Buy</span>
                <span className="text-ink">{formatPrice(o.price)}</span>
              </div>
            ))}
          </div>
        </Section>
      )}

      <Section title="Latest fills">
        {fills.length === 0 && <div className="text-mute">No fills yet.</div>}
        <table className="w-full font-mono text-[11px]">
          <tbody>
            {fills.map((f, i) => (
              <tr key={`${f.time}-${f.kind}-${f.line}-${i}`}>
                <td className="whitespace-nowrap pr-2 font-sans text-mute">{fmtTime(f.time)}</td>
                <td className={clsx("pr-2", f.side === "buy" ? "text-up" : "text-down")}>
                  {f.kind === "initial" ? "Start buy" : f.kind === "stop" ? "Stop sell" : f.side === "buy" ? "Buy" : "Sell"}
                </td>
                <td className="text-right text-ink">{formatPrice(f.price)}</td>
                <td className={clsx("text-right", f.profit != null ? tone(f.profit) : "text-mute")}>
                  {f.profit != null ? formatQuote(f.profit, "") : ""}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!allFills && r.recent_fills.length > 15 && (
          <button type="button" onClick={() => setAllFills(true)} className="text-[11px] text-accent hover:underline">
            Show {r.recent_fills.length - 15} more
          </button>
        )}
      </Section>

      {r.notes.length > 0 && (
        <ul className="list-disc space-y-0.5 pl-4 text-[11px] text-mute">
          {r.notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ form --

interface FormState {
  name: string;
  symbol: string;
  lower: string;
  upper: string;
  grids: string;
  gridType: GridType;
  investment: string;
  startMode: "runtime" | "start";
  runtime: string;
  start: string;
  fee: string;
  bnb: boolean;
  trigger: string;
  tp: string;
  sl: string;
  sellOnStop: boolean;
  stoppedAt: string;
  qty: string;
  bMatched: string;
  bGrid: string;
  bTotal: string;
}

function toLocalInput(ts: number | null | undefined): string {
  if (ts == null) return "";
  const d = new Date(ts * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fromLocalInput(v: string): number | null {
  if (!v) return null;
  const t = new Date(v).getTime();
  return Number.isFinite(t) ? Math.floor(t / 1000) : null;
}

const str = (v: number | null | undefined) => (v == null ? "" : String(v));

function num(v: string): number | null {
  const s = v.trim().replace(/,/g, "");
  if (!s) return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
}

function initialForm(symbol: string, bot?: GridBot): FormState {
  const p = bot?.params;
  const b = bot?.binance;
  return {
    name: bot?.name ?? "",
    symbol: p?.symbol ?? symbol,
    lower: str(p?.lower),
    upper: str(p?.upper),
    grids: str(p?.grids ?? 20),
    gridType: p?.grid_type ?? "arithmetic",
    investment: str(p?.investment),
    startMode: bot ? "start" : "runtime",
    runtime: "",
    start: toLocalInput(p?.start_time),
    fee: str(+((p?.fee_rate ?? 0.001) * 100).toFixed(4)),
    bnb: p?.bnb_discount ?? false,
    trigger: str(p?.trigger_price),
    tp: str(p?.take_profit),
    sl: str(p?.stop_loss),
    sellOnStop: p?.sell_on_stop ?? false,
    stoppedAt: toLocalInput(p?.end_time),
    qty: str(p?.qty_per_order),
    bMatched: str(b?.matched_trades),
    bGrid: str(b?.grid_profit),
    bTotal: str(b?.total_pnl),
  };
}

/** Form → request pieces; a plain-English message when something required is missing. */
function readForm(f: FormState): { params: GridBotParams; binance: BinanceShows | null } | string {
  const lower = num(f.lower);
  const upper = num(f.upper);
  const grids = num(f.grids);
  const investment = num(f.investment);
  if (!f.symbol.trim()) return "Enter the coin pair, e.g. BTCUSDT.";
  if (lower == null || upper == null) return "Enter the lower and upper price.";
  if (grids == null || !Number.isInteger(grids)) return "Enter the number of grids (a whole number, 2–500).";
  if (investment == null) return "Enter the investment.";
  const params: GridBotParams = {
    symbol: f.symbol.trim().toUpperCase(),
    lower,
    upper,
    grids,
    grid_type: f.gridType,
    investment,
    fee_rate: (num(f.fee) ?? 0.1) / 100,
    bnb_discount: f.bnb,
    trigger_price: num(f.trigger),
    take_profit: num(f.tp),
    stop_loss: num(f.sl),
    sell_on_stop: f.sellOnStop,
    end_time: fromLocalInput(f.stoppedAt),
    qty_per_order: num(f.qty),
  };
  if (f.startMode === "runtime") {
    if (!f.runtime.trim()) return "Enter how long the bot has been running, e.g. 3d 4h 12m.";
    params.runtime = f.runtime.trim();
  } else {
    const start = fromLocalInput(f.start);
    if (start == null) return "Enter when the bot was started.";
    params.start_time = start;
    params.runtime = null;
  }
  const shows: BinanceShows = { matched_trades: num(f.bMatched), grid_profit: num(f.bGrid), total_pnl: num(f.bTotal) };
  const any = shows.matched_trades != null || shows.grid_profit != null || shows.total_pnl != null;
  return { params, binance: any ? shows : null };
}

function Field({ label, hint, children, className }: { label: string; hint?: string; children: ReactNode; className?: string }) {
  return (
    <label className={clsx("block space-y-0.5", className)}>
      <span className="text-[10px] text-mute">{label}</span>
      {children}
      {hint && <span className="block text-[10px] text-mute">{hint}</span>}
    </label>
  );
}

const INPUT =
  "w-full rounded border border-line bg-panel2 px-2 py-1 font-mono text-[12px] text-ink outline-none placeholder:text-mute/60 focus:border-accent";

function BotForm(p: { symbol: string; price: number | null; bot?: GridBot; onSaved(bot: GridBot, result: GridBotResult): void; onCancel(): void }) {
  const [f, setF] = useState<FormState>(() => initialForm(p.symbol, p.bot));
  const [more, setMore] = useState(() => {
    const b = p.bot?.params;
    return !!b && !!(b.trigger_price || b.take_profit || b.stop_loss || b.end_time || b.qty_per_order || b.bnb_discount);
  });
  const [busy, setBusy] = useState<"preview" | "save" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<GridBotResult | null>(null);
  const set = <K extends keyof FormState>(k: K, v: FormState[K]) => setF((s) => ({ ...s, [k]: v }));
  const quote = splitSymbol(f.symbol.trim().toUpperCase())[1] || "USDT";
  const priceHint = p.price != null && f.symbol.trim().toUpperCase() === p.symbol ? `Price now ${formatPrice(p.price)}` : undefined;

  const run = async (mode: "preview" | "save") => {
    const read = readForm(f);
    if (typeof read === "string") {
      setError(read);
      return;
    }
    setBusy(mode);
    setError(null);
    try {
      if (mode === "preview") {
        setPreview(await simulateGridBot(read.params, read.binance));
      } else if (p.bot) {
        const old = p.bot.binance;
        const same =
          old &&
          read.binance &&
          old.matched_trades === read.binance.matched_trades &&
          old.grid_profit === read.binance.grid_profit &&
          old.total_pnl === read.binance.total_pnl;
        const params: Partial<GridBotParams> = { ...read.params };
        if (f.startMode === "runtime") delete params.start_time; // a new runtime re-pins the start to now − runtime
        const out = await updateGridBot(p.bot.id, {
          name: f.name.trim() || undefined,
          params,
          binance: same ? old : read.binance,
        });
        p.onSaved(out.bot, out.result);
      } else {
        const out = await createGridBot({ name: f.name.trim(), params: read.params, binance: read.binance });
        p.onSaved(out.bot, out.result);
      }
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <form
      className="space-y-3 p-3"
      onSubmit={(e) => {
        e.preventDefault();
        void run("save");
      }}
    >
      <p className="text-[11px] leading-snug text-mute">Copy the settings from your bot on Binance (Bot details).</p>
      <div className="grid grid-cols-2 gap-2">
        <Field label="Coin pair" hint={priceHint}>
          <input className={INPUT} value={f.symbol} onChange={(e) => set("symbol", e.target.value.toUpperCase())} placeholder="BTCUSDT" />
        </Field>
        <Field label="Name (optional)">
          <input className={clsx(INPUT, "font-sans")} value={f.name} onChange={(e) => set("name", e.target.value)} placeholder="My BTC grid" />
        </Field>
        <Field label="Lower price">
          <input className={INPUT} inputMode="decimal" value={f.lower} onChange={(e) => set("lower", e.target.value)} />
        </Field>
        <Field label="Upper price">
          <input className={INPUT} inputMode="decimal" value={f.upper} onChange={(e) => set("upper", e.target.value)} />
        </Field>
        <Field label="Number of grids">
          <input className={INPUT} inputMode="numeric" value={f.grids} onChange={(e) => set("grids", e.target.value)} />
        </Field>
        <Field label={`Investment (${quote})`}>
          <input className={INPUT} inputMode="decimal" value={f.investment} onChange={(e) => set("investment", e.target.value)} />
        </Field>
      </div>

      <div className="flex gap-1">
        {(["arithmetic", "geometric"] as const).map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => set("gridType", t)}
            className={clsx(
              "flex-1 rounded border px-2 py-1 text-[11px]",
              f.gridType === t ? "border-accent bg-accent/15 text-ink" : "border-line text-mute hover:text-ink",
            )}
          >
            {t === "arithmetic" ? "Arithmetic" : "Geometric"}
          </button>
        ))}
      </div>

      <div className="space-y-1.5">
        <div className="flex gap-3 text-[11px]">
          {(["runtime", "start"] as const).map((m) => (
            <label key={m} className="flex items-center gap-1 text-mute">
              <input type="radio" checked={f.startMode === m} onChange={() => set("startMode", m)} />
              {m === "runtime" ? "Running for" : "Started at"}
            </label>
          ))}
        </div>
        {f.startMode === "runtime" ? (
          <Field label="Runtime, as Binance shows it" hint={p.bot ? "A new runtime moves the start to now minus this." : undefined}>
            <input className={INPUT} value={f.runtime} onChange={(e) => set("runtime", e.target.value)} placeholder="3d 4h 12m" />
          </Field>
        ) : (
          <Field label="Start date and time (your time zone)">
            <input type="datetime-local" className={INPUT} value={f.start} onChange={(e) => set("start", e.target.value)} />
          </Field>
        )}
      </div>

      <button type="button" onClick={() => setMore(!more)} className="text-[11px] text-accent hover:underline">
        {more ? "Hide" : "More"} settings (fees, trigger, take profit, stop loss)
      </button>
      {more && (
        <div className="grid grid-cols-2 gap-2">
          <Field label="Fee per fill (%)">
            <input className={INPUT} inputMode="decimal" value={f.fee} onChange={(e) => set("fee", e.target.value)} />
          </Field>
          <label className="flex items-end gap-1.5 pb-1 text-[11px] text-mute">
            <input type="checkbox" checked={f.bnb} onChange={(e) => set("bnb", e.target.checked)} />
            Pay fees in BNB (25% off)
          </label>
          <Field label="Trigger price">
            <input className={INPUT} inputMode="decimal" value={f.trigger} onChange={(e) => set("trigger", e.target.value)} />
          </Field>
          <Field label="Qty per order (if shown)">
            <input className={INPUT} inputMode="decimal" value={f.qty} onChange={(e) => set("qty", e.target.value)} />
          </Field>
          <Field label="Take profit price">
            <input className={INPUT} inputMode="decimal" value={f.tp} onChange={(e) => set("tp", e.target.value)} />
          </Field>
          <Field label="Stop loss price">
            <input className={INPUT} inputMode="decimal" value={f.sl} onChange={(e) => set("sl", e.target.value)} />
          </Field>
          <label className="col-span-2 flex items-center gap-1.5 text-[11px] text-mute">
            <input type="checkbox" checked={f.sellOnStop} onChange={(e) => set("sellOnStop", e.target.checked)} />
            Sell all base coins when the bot stops
          </label>
          <Field label="Stopped at (only if you stopped it)" className="col-span-2">
            <input type="datetime-local" className={INPUT} value={f.stoppedAt} onChange={(e) => set("stoppedAt", e.target.value)} />
          </Field>
        </div>
      )}

      <div className="space-y-1.5 rounded border border-line p-2">
        <div className="text-[11px] text-ink">What Binance shows now (optional)</div>
        <p className="text-[10px] leading-snug text-mute">Copy these from the bot to see how close the app is.</p>
        <div className="grid grid-cols-3 gap-2">
          <Field label="Matched trades">
            <input className={INPUT} inputMode="numeric" value={f.bMatched} onChange={(e) => set("bMatched", e.target.value)} />
          </Field>
          <Field label="Grid profit">
            <input className={INPUT} inputMode="decimal" value={f.bGrid} onChange={(e) => set("bGrid", e.target.value)} />
          </Field>
          <Field label="Total PnL">
            <input className={INPUT} inputMode="decimal" value={f.bTotal} onChange={(e) => set("bTotal", e.target.value)} />
          </Field>
        </div>
      </div>

      {error && <div className="rounded border border-down/40 bg-down/10 px-2 py-1.5 text-[11px] text-down">{error}</div>}
      {busy && (
        <div className="text-[11px] text-mute">
          Replaying 1-minute candles… a bot that has run for weeks can take a minute the first time.
        </div>
      )}

      <div className="flex gap-2">
        <button
          type="submit"
          disabled={!!busy}
          className="rounded bg-accent px-3 py-1 text-[12px] font-medium text-white hover:bg-accent/90 disabled:opacity-50"
        >
          {busy === "save" ? "Saving…" : p.bot ? "Save changes" : "Save bot"}
        </button>
        <button
          type="button"
          disabled={!!busy}
          onClick={() => void run("preview")}
          className="btn-ghost border border-line px-3 text-[12px] disabled:opacity-50"
        >
          {busy === "preview" ? "Checking…" : "Preview"}
        </button>
        <div className="flex-1" />
        <button type="button" onClick={p.onCancel} className="btn-ghost px-2 text-[12px]">
          Cancel
        </button>
      </div>

      {preview && (
        <div className="space-y-2 rounded-lg border border-line bg-panel p-2.5">
          <div className="flex items-center gap-1.5 text-[11px] text-mute">
            Preview (not saved) <DemoBadge r={preview} /> <StatusChip r={preview} />
          </div>
          <Headline r={preview} />
          <RangeBar r={preview} />
          <CompareTable r={preview} />
        </div>
      )}
    </form>
  );
}

// ------------------------------------------------------------------ plan --

const PLAN_KEY = "gridplan";
const PLAN_DAYS = [7, 30, 90] as const;
const TIMEFRAMES: PlanTimeframe[] = ["1h", "4h", "1d"];

interface PlanDraft {
  lower: string;
  upper: string;
  grids: string;
  gridType: GridType;
}

type DraftGrid = Pick<GridBotParams, "lower" | "upper" | "grids" | "grid_type">;

function draftFrom(plan: GridPlan): PlanDraft {
  return { lower: String(plan.lower.price), upper: String(plan.upper.price), grids: String(plan.grids), gridType: plan.grid_type };
}

function readDraft(d: PlanDraft): DraftGrid | string {
  const lower = num(d.lower);
  const upper = num(d.upper);
  const grids = num(d.grids);
  if (lower == null || upper == null || lower <= 0) return "Enter the lower and upper price.";
  if (upper <= lower) return "The upper price must be above the lower price.";
  if (grids == null || !Number.isInteger(grids) || grids < 2 || grids > 500) return "Enter the number of grids (a whole number, 2–500).";
  return { lower, upper, grids, grid_type: d.gridType };
}

/** Profit per grid after both fills' fees, min and max %, as the backend computes it (gridbot.profit_per_grid_pct). */
function draftProfit(g: DraftGrid, fee: number): [number, number] {
  const lines = gridLinesFor(g);
  let lo = Infinity;
  let hi = -Infinity;
  for (let i = 1; i < lines.length; i++) {
    const v = (((1 - fee) * lines[i]) / lines[i - 1] - 1 - fee) * 100;
    lo = Math.min(lo, v);
    hi = Math.max(hi, v);
  }
  return [lo, hi];
}

/** Grid counts to test next to the chosen one: the planner's own, or half to double an edited count. */
function altCounts(plan: GridPlan, grids: number): number[] {
  if (grids === plan.grids) return plan.compare_grids;
  const out = new Set<number>();
  for (const f of [0.5, 0.75, 1.5, 2]) {
    const g = Math.round(grids * f);
    if (g >= 2 && g <= 500 && g !== grids) out.add(g);
  }
  return [...out].sort((a, b) => a - b);
}

function Segmented<T extends string>({ value, options, onChange }: { value: T; options: [T, string][]; onChange(v: T): void }) {
  return (
    <div className="flex gap-1">
      {options.map(([v, label]) => (
        <button
          key={v}
          type="button"
          onClick={() => onChange(v)}
          className={clsx(
            "flex-1 rounded border px-2 py-1 text-[11px]",
            value === v ? "border-accent bg-accent/15 text-ink" : "border-line text-mute hover:text-ink",
          )}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

function EdgeRow({ label, edge }: { label: string; edge: GridPlan["lower"] }) {
  return (
    <div className="flex items-baseline gap-2">
      <span className="w-10 text-[10px] text-mute">{label}</span>
      <span className="font-mono text-[12px] text-ink">{formatPrice(edge.price)}</span>
      <span className="min-w-0 flex-1 truncate text-[11px] text-mute" title={edge.basis}>
        {edge.basis} · {Math.abs(edge.distance_atr).toFixed(1)} ATR {label === "Lower" ? "below" : "above"} ({Math.abs(edge.distance_pct).toFixed(1)}%)
      </span>
    </div>
  );
}

/** The bot's value over the test (green above the investment, red below), with the investment dashed. */
function EquityLine({ points, base }: { points: [number, number][]; base: number }) {
  if (points.length < 2) return null;
  const W = 300;
  const H = 60;
  const t0 = points[0][0];
  const t1 = points[points.length - 1][0];
  let lo = base;
  let hi = base;
  for (const [, v] of points) {
    lo = Math.min(lo, v);
    hi = Math.max(hi, v);
  }
  const span = hi - lo || 1;
  const x = (t: number) => ((t - t0) / (t1 - t0 || 1)) * W;
  const y = (v: number) => H - 2 - ((v - lo) / span) * (H - 4);
  const d = points.map(([t, v], i) => `${i ? "L" : "M"}${x(t).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const last = points[points.length - 1][1];
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="h-16 w-full" preserveAspectRatio="none" aria-label="Bot value over the test">
      <line x1={0} x2={W} y1={y(base)} y2={y(base)} stroke="#475569" strokeDasharray="3 3" strokeWidth={1} vectorEffect="non-scaling-stroke" />
      <path d={d} fill="none" stroke={last >= base ? "#22c55e" : "#ef4444"} strokeWidth={1.5} vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

function BacktestView({ t, onPick, busy }: { t: GridBacktestResult; onPick(grids: number): void; busy: boolean }) {
  const q = t.quote_asset;
  const r = t.result;
  return (
    <div className="space-y-3 rounded-lg border border-line bg-panel p-2.5">
      <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-mute">
        <span>
          Last {t.days} days · {fmtTime(t.start_time)} – {fmtTime(t.end_time)} · {formatPrice(t.start_price)} → {formatPrice(t.last_price)}
        </span>
        <DemoBadge r={r} />
      </div>
      <div className="grid grid-cols-3 gap-x-3 gap-y-1.5">
        <Stat label="Grid profit" className={tone(t.grid_profit)}>
          {formatQuote(t.grid_profit, "")} <span className="text-mute">{pct(t.grid_profit_pct)}</span>
        </Stat>
        <Stat label="Matched trades">{t.matched_trades}</Stat>
        <Stat label="Grid APR">{t.grid_apr_pct.toFixed(1)}%</Stat>
        <Stat label="Max drawdown" className={t.max_drawdown_pct > 0 ? "text-down" : "text-ink"}>
          {t.max_drawdown_pct > 0 ? `−${t.max_drawdown_pct.toFixed(2)}%` : "0.00%"}
        </Stat>
        <Stat label="Time in range">{t.time_in_range_pct.toFixed(0)}%</Stat>
        <Stat label="Total PnL" className={tone(t.total_pnl)}>
          {formatQuote(t.total_pnl, "")} <span className="text-mute">{pct(t.total_pnl_pct)}</span>
        </Stat>
        <Stat label="Total APR" className={tone(t.total_apr_pct)}>{t.total_apr_pct.toFixed(1)}%</Stat>
        <Stat label={`Holding ${r.base_asset} instead`} className={tone(t.hold_return_pct)}>{pct(t.hold_return_pct)}</Stat>
        <Stat label="Fees paid">{formatQuote(r.fees_paid, q, false)}</Stat>
      </div>
      <EquityLine points={t.equity} base={r.investment} />

      {t.alternatives.length > 1 && (
        <Section title="Other grid counts, same range and period">
          <table className="w-full font-mono text-[11px]">
            <thead>
              <tr className="text-[10px] text-mute">
                <th className="text-left font-normal">Grids</th>
                <th className="text-right font-normal">Profit/grid</th>
                <th className="text-right font-normal">Matched</th>
                <th className="text-right font-normal">Grid profit</th>
                <th className="text-right font-normal">Max DD</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {t.alternatives.map((a) => (
                <tr key={a.grids} className={clsx(a.chosen && "text-accent")}>
                  <td className={a.chosen ? "text-accent" : "text-ink"}>{a.grids}</td>
                  <td className="text-right text-ink">
                    {a.profit_per_grid_min_pct.toFixed(2)}–{a.profit_per_grid_max_pct.toFixed(2)}%
                  </td>
                  <td className="text-right text-ink">{a.matched_trades}</td>
                  <td className={clsx("text-right", tone(a.grid_profit))}>{formatQuote(a.grid_profit, "")}</td>
                  <td className="text-right text-ink">{a.max_drawdown_pct.toFixed(1)}%</td>
                  <td className="pl-1 text-right font-sans">
                    {a.chosen ? (
                      <span className="text-[10px] text-mute">this</span>
                    ) : (
                      <button type="button" disabled={busy} onClick={() => onPick(a.grids)} className="text-[11px] text-accent hover:underline disabled:opacity-50">
                        Use
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="text-[10px] text-mute">More grids fill more often but earn less per fill; fewer grids the reverse.</p>
        </Section>
      )}

      {t.notes.length > 0 && (
        <ul className="list-disc space-y-0.5 pl-4 text-[10px] text-mute">
          {t.notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * "Plan a bot": the backend suggests a range, a number of grids and the grid type for a coin from its ATR, recent
 * range and support/resistance and supply/demand zones, with the reasons. Everything can be edited, tested on the
 * last 7/30/90 days (with a few other grid counts next to it) and then tracked like a bot entered by hand. The
 * range being planned is drawn on the coin's charts until the view closes.
 */
function PlanView(p: {
  symbol: string;
  price: number | null;
  seed: GridPlan | null;
  onDraw(key: string, symbol: string, overlays: Overlay[]): void;
  onPickSymbol(symbol: string): void;
  onTracked(bot: GridBot, result: GridBotResult): void;
  onCancel(): void;
}) {
  const { seed } = p;
  const [symbol, setSymbol] = useState(seed?.symbol ?? p.symbol);
  const [investment, setInvestment] = useState(String(seed?.investment ?? 1000));
  const [timeframe, setTimeframe] = useState<PlanTimeframe>(seed?.timeframe ?? "4h");
  const [typePick, setTypePick] = useState<GridType | "auto">("auto");
  const [plan, setPlan] = useState<GridPlan | null>(seed);
  const [draft, setDraft] = useState<PlanDraft | null>(seed ? draftFrom(seed) : null);
  const [test, setTest] = useState<GridBacktestResult | null>(null);
  const [tested, setTested] = useState<string | null>(null);
  const [days, setDays] = useState<number>(30);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState<"plan" | "test" | "track" | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Memoised so the drawing effect below only runs when the grid actually changes.
  const read = useMemo(() => (draft ? readDraft(draft) : null), [draft]);
  const grid = read && typeof read !== "string" ? read : null;
  const inv = num(investment);
  const sig = grid && plan ? JSON.stringify([plan.symbol, grid, inv]) : null;
  const edited = !!plan && !!draft && JSON.stringify(draft) !== JSON.stringify(draftFrom(plan));

  // The range on the chart while planning (planOverlays keeps it out of the price auto-scale).
  const drawFn = useRef(p.onDraw);
  useEffect(() => {
    drawFn.current = p.onDraw;
  }, [p.onDraw]);
  const drawnOn = useRef<string | null>(null);
  useEffect(() => {
    const sym = plan?.symbol ?? null;
    if (drawnOn.current && drawnOn.current !== sym) drawFn.current(PLAN_KEY, drawnOn.current, []);
    if (sym) drawFn.current(PLAN_KEY, sym, grid ? planOverlays(grid, plan) : []);
    drawnOn.current = sym;
  }, [plan, grid]);
  useEffect(() => {
    const drawn = drawnOn;
    const draw = drawFn;
    return () => {
      if (drawn.current) draw.current(PLAN_KEY, drawn.current, []);
    };
  }, []);

  const suggest = async () => {
    const sym = symbol.trim().toUpperCase();
    if (!sym) {
      setError("Enter the coin pair, e.g. INJUSDT.");
      return;
    }
    if (inv == null || inv <= 0) {
      setError("Enter the investment.");
      return;
    }
    setBusy("plan");
    setError(null);
    try {
      const out = await planGridBot({ symbol: sym, investment: inv, timeframe, grid_type: typePick === "auto" ? null : typePick });
      setPlan(out);
      setDraft(draftFrom(out));
      setTest(null);
      setTested(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const runTest = async (d: number, g: DraftGrid | null = grid) => {
    if (!plan) return;
    if (!g) {
      setError(typeof read === "string" ? read : "Suggest a plan first.");
      return;
    }
    if (inv == null || inv <= 0) {
      setError("Enter the investment.");
      return;
    }
    setDays(d);
    setBusy("test");
    setError(null);
    try {
      const out = await backtestGrid({
        symbol: plan.symbol,
        ...g,
        investment: inv,
        fee_rate: plan.fee_rate,
        bnb_discount: plan.bnb_discount,
        days: d,
        compare_grids: altCounts(plan, g.grids),
      });
      setTest(out);
      setTested(JSON.stringify([plan.symbol, g, inv]));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const pickGrids = (grids: number) => {
    if (!draft || !grid) return;
    setDraft({ ...draft, grids: String(grids) });
    void runTest(days, { ...grid, grids });
  };

  const track = async () => {
    if (!plan || !grid || inv == null) return;
    setBusy("track");
    setError(null);
    try {
      const out = await createGridBot({
        name: name.trim() || `${displaySymbol(plan.symbol)} grid (planned)`,
        params: {
          symbol: plan.symbol,
          ...grid,
          investment: inv,
          fee_rate: plan.fee_rate,
          bnb_discount: plan.bnb_discount,
          // Tracked from now: set the same bot up on Binance, then edit the start here if it differs.
          start_time: Math.floor(Date.now() / 1000),
        },
      });
      p.onTracked(out.bot, out.result);
    } catch (err) {
      setError((err as Error).message);
      setBusy(null);
    }
  };

  const quote = plan?.quote_asset || splitSymbol(symbol.trim().toUpperCase())[1] || "USDT";
  const priceHint = p.price != null && symbol.trim().toUpperCase() === p.symbol ? `Price now ${formatPrice(p.price)}` : undefined;
  const profit = grid && plan ? draftProfit(grid, plan.fee_per_fill) : null;

  return (
    <div className="space-y-4 p-3">
      <p className="text-[11px] leading-snug text-mute">
        Suggests a price range, number of grids and grid type from the coin&apos;s ATR, its recent range and its support,
        resistance, supply and demand zones, and says why. Edit anything, test it on the last 7, 30 or 90 days, then
        track it.
      </p>
      <form
        className="space-y-2"
        onSubmit={(e) => {
          e.preventDefault();
          void suggest();
        }}
      >
        <div className="grid grid-cols-2 gap-2">
          <Field label="Coin pair" hint={priceHint}>
            <input className={INPUT} value={symbol} onChange={(e) => setSymbol(e.target.value.toUpperCase())} placeholder="INJUSDT" />
          </Field>
          <Field label={`Investment (${quote})`}>
            <input className={INPUT} inputMode="decimal" value={investment} onChange={(e) => setInvestment(e.target.value)} />
          </Field>
        </div>
        <Field label="Zones and ATR from">
          <Segmented<PlanTimeframe> value={timeframe} options={TIMEFRAMES.map((t) => [t, t.toUpperCase()])} onChange={setTimeframe} />
        </Field>
        <Field label="Grid type">
          <Segmented<GridType | "auto">
            value={typePick}
            options={[
              ["auto", "Let the planner choose"],
              ["arithmetic", "Arithmetic"],
              ["geometric", "Geometric"],
            ]}
            onChange={setTypePick}
          />
        </Field>
        <div className="flex gap-2">
          <button
            type="submit"
            disabled={!!busy}
            className="rounded bg-accent px-3 py-1 text-[12px] font-medium text-white hover:bg-accent/90 disabled:opacity-50"
          >
            {busy === "plan" ? "Planning…" : plan ? "Suggest again" : "Suggest a bot"}
          </button>
          <div className="flex-1" />
          <button type="button" onClick={p.onCancel} className="btn-ghost px-2 text-[12px]">
            Cancel
          </button>
        </div>
      </form>

      {error && <div className="rounded border border-down/40 bg-down/10 px-2 py-1.5 text-[11px] text-down">{error}</div>}

      {plan && draft && (
        <>
          <Section title={`Plan for ${displaySymbol(plan.symbol)}`}>
            <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-mute">
              <span>
                Price {formatPrice(plan.last_price)} · ATR {formatPrice(plan.atr)} ({plan.atr_pct.toFixed(2)}%) on {plan.timeframe.toUpperCase()}
              </span>
              {plan.data_source === "synthetic" && (
                <span className="rounded bg-yellow-400/15 px-1 py-0.5 text-[10px] font-semibold text-yellow-300" title="Binance was unreachable">
                  DEMO DATA
                </span>
              )}
            </div>
            <div className="space-y-0.5">
              <EdgeRow label="Upper" edge={plan.upper} />
              <EdgeRow label="Lower" edge={plan.lower} />
            </div>
            <ul className="list-disc space-y-0.5 pl-4 text-[11px] leading-snug text-ink">
              {plan.reasoning.map((r) => (
                <li key={r}>{r}</li>
              ))}
            </ul>
            {plan.warnings.length > 0 && (
              <ul className="space-y-0.5 text-[11px] leading-snug text-yellow-300">
                {plan.warnings.map((w) => (
                  <li key={w}>{w}</li>
                ))}
              </ul>
            )}
          </Section>

          <Section title="Grid">
            <div className="grid grid-cols-3 gap-2">
              <Field label="Lower price">
                <input className={INPUT} inputMode="decimal" value={draft.lower} onChange={(e) => setDraft({ ...draft, lower: e.target.value })} />
              </Field>
              <Field label="Upper price">
                <input className={INPUT} inputMode="decimal" value={draft.upper} onChange={(e) => setDraft({ ...draft, upper: e.target.value })} />
              </Field>
              <Field label="Grids">
                <input className={INPUT} inputMode="numeric" value={draft.grids} onChange={(e) => setDraft({ ...draft, grids: e.target.value })} />
              </Field>
            </div>
            <Segmented<GridType>
              value={draft.gridType}
              options={[
                ["arithmetic", "Arithmetic"],
                ["geometric", "Geometric"],
              ]}
              onChange={(v) => setDraft({ ...draft, gridType: v })}
            />
            {typeof read === "string" ? (
              <div className="text-[11px] text-down">{read}</div>
            ) : (
              profit && (
                <div className="grid grid-cols-2 gap-x-3 gap-y-1.5">
                  <Stat label="Profit per grid (after fees)" className={profit[0] <= 0 ? "text-down" : "text-ink"}>
                    {profit[0].toFixed(2)}% – {profit[1].toFixed(2)}%
                  </Stat>
                  <Stat label="Per order">
                    {inv != null && grid ? `≈ ${formatQuote(inv / grid.grids, quote, false)}` : "–"}
                  </Stat>
                </div>
              )
            )}
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px]">
              {edited && (
                <button type="button" onClick={() => setDraft(draftFrom(plan))} className="text-accent hover:underline">
                  Back to the suggested grid
                </button>
              )}
              {plan.symbol !== p.symbol ? (
                <button type="button" onClick={() => p.onPickSymbol(plan.symbol)} className="text-accent hover:underline">
                  Open {displaySymbol(plan.symbol)} to see the range on the chart
                </button>
              ) : (
                <span className="text-mute">The range is drawn on the chart in amber.</span>
              )}
            </div>
          </Section>

          <Section title="Test on history">
            <div className="flex gap-1">
              {PLAN_DAYS.map((d) => (
                <button
                  key={d}
                  type="button"
                  disabled={!!busy || !grid}
                  onClick={() => void runTest(d)}
                  className={clsx(
                    "flex-1 rounded border px-2 py-1 text-[11px] disabled:opacity-50",
                    test && test.days === d ? "border-accent bg-accent/15 text-ink" : "border-line text-mute hover:text-ink",
                  )}
                >
                  {busy === "test" && days === d ? "Testing…" : `Last ${d} days`}
                </button>
              ))}
            </div>
            {busy === "test" && (
              <div className="text-[11px] text-mute">Replaying 1-minute candles… 90 days can take a minute the first time.</div>
            )}
            {test && tested !== sig && busy !== "test" && (
              <div className="text-[11px] text-yellow-300">The grid changed since this test; run it again to update.</div>
            )}
            {test && <BacktestView t={test} onPick={pickGrids} busy={!!busy} />}
          </Section>

          <Section title="Track it">
            <div className="flex gap-2">
              <input
                className={clsx(INPUT, "flex-1 font-sans")}
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder={`${displaySymbol(plan.symbol)} grid (planned)`}
                aria-label="Bot name"
              />
              <button
                type="button"
                disabled={!!busy || !grid}
                onClick={() => void track()}
                className="whitespace-nowrap rounded bg-accent px-3 py-1 text-[12px] font-medium text-white hover:bg-accent/90 disabled:opacity-50"
              >
                {busy === "track" ? "Saving…" : "Track this bot"}
              </button>
            </div>
            <p className="text-[10px] leading-snug text-mute">
              Tracked from now. The app does not place orders: set the same bot up on Binance yourself, then edit the
              start time here if it differs.
            </p>
          </Section>
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------- real vs simulated --

/**
 * The bot's real fills next to the simulated ones. Binance has no public API for Spot Grid bots, so "real" means
 * fills on the user's spot account (imported with a read-only key, Account tab) that match this bot.
 */
function RealVsSim({ bot }: { bot: GridBot }) {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<GridRealCompare | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setData(await fetchGridRealCompare(bot.id));
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, [bot.id]);

  const toggle = () => {
    if (!open && !data) void load();
    setOpen(!open);
  };

  return (
    <div className="space-y-1.5">
      <button type="button" onClick={toggle} className="text-[10px] font-semibold uppercase tracking-wide text-mute hover:text-ink">
        {open ? "▾" : "▸"} Real vs simulated
      </button>
      {open && (
        <div className="space-y-2">
          <p className="text-[10px] leading-snug text-mute">
            Binance has no public API for Spot Grid bots, so the app cannot read the bot itself. With a read-only API key
            (Account tab), fills on your spot account that match this bot are listed next to the simulated ones.
          </p>
          {error && <div className="text-[11px] text-down">{error}</div>}
          {loading && !data && <div className="text-[11px] text-mute">Loading…</div>}
          {data && (
            <>
              <div className="grid grid-cols-3 gap-x-3 gap-y-1.5">
                <Stat label="Real fills">{data.real_fills}</Stat>
                <Stat label="Simulated fills">{data.sim_fills}</Stat>
                <Stat label="In both">{data.matched}</Stat>
                <Stat label="Only real">{data.real_only}</Stat>
                <Stat label="Only simulated">{data.sim_only}</Stat>
                <Stat label="Real net" className={tone(data.real_net_quote)}>{formatQuote(data.real_net_quote, "")}</Stat>
              </div>
              {data.rows.length > 0 && (
                <table className="w-full font-mono text-[11px]">
                  <thead>
                    <tr className="text-[10px] text-mute">
                      <th className="text-left font-normal" />
                      <th className="text-left font-normal" />
                      <th className="text-right font-normal">Price</th>
                      <th className="text-right font-normal">Real</th>
                      <th className="text-right font-normal">Sim</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.rows.slice(0, 30).map((row, i) => (
                      <tr key={`${row.time}-${row.side}-${i}`}>
                        <td className="whitespace-nowrap pr-2 font-sans text-mute">{fmtTime(row.time)}</td>
                        <td className={row.side === "buy" ? "text-up" : "text-down"}>{row.side === "buy" ? "Buy" : "Sell"}</td>
                        <td className="text-right text-ink">{formatPrice(row.price)}</td>
                        <td className="text-right text-ink">{row.real ? qtyText(row.real.qty) : <span className="text-mute">–</span>}</td>
                        <td className="text-right text-ink">{row.sim ? formatPrice(row.sim.price) : <span className="text-mute">–</span>}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              {data.notes.length > 0 && (
                <ul className="list-disc space-y-0.5 pl-4 text-[10px] text-mute">
                  {data.notes.map((n) => (
                    <li key={n}>{n}</li>
                  ))}
                </ul>
              )}
              <button type="button" disabled={loading} onClick={() => void load()} className="btn-ghost h-6 px-1.5 text-[11px] disabled:opacity-50">
                <RefreshCw className="h-3.5 w-3.5" /> Refresh
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}
