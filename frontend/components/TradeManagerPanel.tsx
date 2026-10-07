"use client";

import clsx from "clsx";
import { Check, Eye, EyeOff, Loader2, Plus, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { usePersistentState } from "@/hooks/usePersistentState";
import type { DockPanelProps } from "@/lib/dock";
import { displaySymbol, formatPrice } from "@/lib/format";
import { fetchAccountPositions } from "@/lib/binance";
import { fetchJournal, type JournalEntry } from "@/lib/journal";
import {
  deleteManagedTrade,
  fetchManagedTrades,
  manageTrade,
  tradeOverlays,
  updateManagedTrade,
  type ManagedTrade,
  type TradeAdvice,
  type TradePatch,
} from "@/lib/trades";
import { TIMEFRAMES, type Interval, type Overlay } from "@/lib/types";

const REFRESH_MS = 15_000;
const MANAGE_TFS: Interval[] = ["1m", "5m", "15m", "30m", "1h", "4h"];

const STATUS: Record<ManagedTrade["status"], { label: string; cls: string }> = {
  open: { label: "Open", cls: "bg-accent/15 text-accent" },
  stopped: { label: "Stopped", cls: "bg-down/15 text-down" },
  done: { label: "All targets", cls: "bg-up/15 text-up" },
  closed: { label: "Closed", cls: "bg-panel2 text-mute" },
};

function rText(r: number | null | undefined) {
  if (r == null) return "–";
  return `${r >= 0 ? "+" : ""}${r.toFixed(2)}R`;
}

function tfLabel(iv: string) {
  return TIMEFRAMES.find((t) => t.value === iv)?.label ?? iv;
}

/**
 * Trade manager: live trades the backend watches on closed candles. It says when to take a target off, move the stop
 * to breakeven, trail it under structure, or get out when structure breaks; the same advice goes to Telegram/Discord.
 * Nothing is sent to the exchange: "Done" records that you moved the stop there yourself.
 */
export default function TradeManagerPanel(props: DockPanelProps) {
  const { onChartOverlays } = props;
  const [trades, setTrades] = useState<ManagedTrade[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [adding, setAdding] = useState(false);
  const [hidden, setHidden] = usePersistentState<string[]>("ac:trades-hidden", []);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const r = await fetchManagedTrades(signal);
      setTrades(r.trades);
      setError(null);
    } catch (err) {
      if ((err as Error).name !== "AbortError") setError((err as Error).message);
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    const ctrl = new AbortController();
    void load(ctrl.signal);
    const id = setInterval(() => void load(), REFRESH_MS);
    return () => {
      ctrl.abort();
      clearInterval(id);
    };
  }, [load]);

  // Entry, stop, targets and a pending stop suggestion on the trade's chart.
  const drawFn = useRef(onChartOverlays);
  useEffect(() => {
    drawFn.current = onChartOverlays;
  }, [onChartOverlays]);
  const drawn = useRef(new Map<string, { symbol: string; sig: string }>());
  useEffect(() => {
    const want = new Map<string, { symbol: string; overlays: Overlay[] }>();
    for (const t of trades) if (!hidden.includes(t.id) && t.status === "open") want.set(`trade:${t.id}`, { symbol: t.symbol, overlays: tradeOverlays(t) });
    for (const [key, d] of drawn.current) {
      if (!want.has(key)) {
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
  }, [trades, hidden]);

  const patch = async (t: ManagedTrade, p: TradePatch) => {
    try {
      const next = await updateManagedTrade(t.id, p);
      setTrades((list) => list.map((x) => (x.id === t.id ? next : x)));
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const remove = async (t: ManagedTrade) => {
    if (!window.confirm(`Stop managing ${displaySymbol(t.symbol)} ${t.direction}?`)) return;
    try {
      await deleteManagedTrade(t.id);
      setTrades((list) => list.filter((x) => x.id !== t.id));
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const open = trades.filter((t) => t.status === "open");
  const past = trades.filter((t) => t.status !== "open");

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center gap-2 border-b border-line px-3 py-1.5 text-[11px] text-mute">
        <span className="truncate">Watches your live trades and says when to move the stop or take profit</span>
        <div className="flex-1" />
        <button type="button" className="btn-ghost h-6 shrink-0 gap-1 px-1.5 text-[11px]" onClick={() => setAdding((v) => !v)}>
          {adding ? <X className="h-3.5 w-3.5" /> : <Plus className="h-3.5 w-3.5" />} {adding ? "Cancel" : "Add trade"}
        </button>
      </div>

      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-3 py-3 text-[12px]">
        {adding && (
          <AddTrade
            symbol={props.symbol}
            interval={props.interval}
            price={props.price}
            onAdded={(t) => {
              setTrades((list) => [t, ...list.filter((x) => x.id !== t.id)]);
              setAdding(false);
              props.onPickSymbol(t.symbol);
            }}
          />
        )}
        {error && <p className="text-down">{error}</p>}
        {!loaded && <Loader2 className="h-4 w-4 animate-spin text-mute" />}
        {loaded && trades.length === 0 && !adding && (
          <p className="text-mute">
            No trades yet. Add one from your Binance positions or your journal, or type it in. The app only advises: you still move stops and take
            profit on the exchange.
          </p>
        )}
        {open.map((t) => (
          <TradeCard
            key={t.id}
            t={t}
            shown={!hidden.includes(t.id)}
            onToggleShown={() => setHidden((h) => (h.includes(t.id) ? h.filter((x) => x !== t.id) : [...h, t.id]))}
            onPatch={(p) => void patch(t, p)}
            onRemove={() => void remove(t)}
            onOpen={() => props.onPickSymbol(t.symbol)}
          />
        ))}
        {past.length > 0 && (
          <>
            <p className="pt-1 text-[10px] uppercase tracking-wide text-mute">Finished</p>
            {past.map((t) => (
              <TradeCard key={t.id} t={t} shown={false} onPatch={(p) => void patch(t, p)} onRemove={() => void remove(t)} onOpen={() => props.onPickSymbol(t.symbol)} />
            ))}
          </>
        )}
      </div>
    </div>
  );
}

function TradeCard(p: {
  t: ManagedTrade;
  shown: boolean;
  onToggleShown?(): void;
  onPatch(patch: TradePatch): void;
  onRemove(): void;
  onOpen(): void;
}) {
  const { t } = p;
  const [stopInput, setStopInput] = useState("");
  const long = t.direction === "long";
  const st = STATUS[t.status];
  const advice = [...t.advice].reverse();
  const fresh = advice.filter((a) => a.status === "new");
  const older = advice.filter((a) => a.status !== "new").slice(0, 3);
  return (
    <div className="rounded-md border border-line bg-base/40 p-2">
      <div className="flex items-center gap-1.5">
        <button type="button" onClick={p.onOpen} className="font-semibold text-ink hover:text-accent" title="Open this chart">
          {displaySymbol(t.symbol)}
        </button>
        <span className={long ? "text-up" : "text-down"}>{long ? "Long" : "Short"}</span>
        <span className="text-mute">{tfLabel(t.interval)}</span>
        <span className={clsx("rounded px-1 py-0.5 text-[10px]", st.cls)}>{st.label}</span>
        {t.source === "binance" && (
          <span className="rounded bg-yellow-400/15 px-1 py-0.5 text-[10px] text-yellow-300" title="Added from your Binance position; closed here when it closes there">
            Binance
          </span>
        )}
        {t.data_source && t.data_source !== "binance" && <span className="rounded bg-yellow-400/15 px-1 py-0.5 text-[10px] text-yellow-300">demo data</span>}
        <div className="flex-1" />
        {p.onToggleShown && (
          <button type="button" className="btn-ghost h-6 w-6 p-0" onClick={p.onToggleShown} title={p.shown ? "Hide its lines on the chart" : "Show its lines on the chart"}>
            {p.shown ? <Eye className="h-3.5 w-3.5" /> : <EyeOff className="h-3.5 w-3.5" />}
          </button>
        )}
        <button type="button" className="btn-ghost h-6 w-6 p-0" onClick={p.onRemove} title="Stop managing this trade" aria-label="Remove">
          <Trash2 className="h-3.5 w-3.5" />
        </button>
      </div>

      <div className="mt-1 grid grid-cols-[auto_1fr_auto_1fr] gap-x-2 gap-y-0.5 font-mono text-[11px]">
        <span className="text-mute">Entry</span>
        <span className="text-accent">{formatPrice(t.entry)}</span>
        <span className="text-mute">Stop</span>
        <span className="text-down">
          {formatPrice(t.stop)}
          {t.stop !== t.initial_stop && <span className="text-mute"> (was {formatPrice(t.initial_stop)})</span>}
        </span>
        <span className="text-mute">Now</span>
        <span className={(t.r_now ?? 0) >= 0 ? "text-up" : "text-down"}>
          {t.last_price != null ? formatPrice(t.last_price) : "–"} {t.status === "open" ? rText(t.r_now) : ""}
        </span>
        <span className="text-mute">{t.status === "open" ? "Best" : "Result"}</span>
        <span className={(t.status === "open" ? t.max_r : (t.exit_r ?? 0)) >= 0 ? "text-up" : "text-down"}>
          {t.status === "open" ? rText(t.max_r) : rText(t.exit_r)}
        </span>
      </div>
      {t.targets.length > 0 && (
        <p className="mt-0.5 font-mono text-[11px]">
          {t.targets.map((x, i) => (
            <span key={i} className={clsx("mr-2", i < t.targets_hit ? "text-mute line-through" : "text-up")}>
              T{i + 1} {formatPrice(x)}
            </span>
          ))}
        </p>
      )}

      {fresh.map((a) => (
        <AdviceRow key={a.id} a={a} onDone={() => p.onPatch({ advice_id: a.id, advice_status: "done" })} onDismiss={() => p.onPatch({ advice_id: a.id, advice_status: "dismissed" })} />
      ))}
      {older.map((a) => (
        <p key={a.id} className="mt-1 text-[11px] text-mute">
          {a.status === "done" ? "✓" : "–"} {a.text}
        </p>
      ))}

      {t.status === "open" && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[11px]">
          <input
            value={stopInput}
            onChange={(e) => setStopInput(e.target.value)}
            placeholder="Stop moved to…"
            inputMode="decimal"
            className="h-6 w-28 rounded border border-line bg-base px-1.5 font-mono text-ink outline-none focus:border-accent"
          />
          <button
            type="button"
            className="btn-ghost h-6 border border-line px-1.5"
            disabled={!Number(stopInput)}
            onClick={() => {
              p.onPatch({ stop: Number(stopInput) });
              setStopInput("");
            }}
          >
            Set stop
          </button>
          <button
            type="button"
            className="btn-ghost h-6 border border-line px-1.5"
            onClick={() => {
              const v = window.prompt("Closed at what price?", t.last_price != null ? String(t.last_price) : "");
              if (v && Number(v) > 0) p.onPatch({ close_price: Number(v) });
            }}
          >
            I closed it
          </button>
          <select
            value={t.trail}
            onChange={(e) => p.onPatch({ trail: e.target.value as ManagedTrade["trail"] })}
            className="h-6 rounded border border-line bg-base px-1 text-ink outline-none"
            title="How it suggests trailing the stop once the trade is past T1 or +1R"
          >
            <option value="structure">Trail under structure</option>
            <option value="atr">Trail by ATR</option>
            <option value="off">No trailing</option>
          </select>
        </div>
      )}
    </div>
  );
}

function AdviceRow({ a, onDone, onDismiss }: { a: TradeAdvice; onDone(): void; onDismiss(): void }) {
  const urgent = a.kind === "stop" || a.kind === "structure";
  return (
    <div className={clsx("mt-1.5 rounded border px-2 py-1.5 text-[11px]", urgent ? "border-down/40 bg-down/10" : "border-yellow-400/40 bg-yellow-400/10")}>
      <p className="text-ink">{a.text}</p>
      <div className="mt-1 flex items-center gap-1.5">
        <span className="text-[10px] text-mute">{new Date(a.time * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}</span>
        <div className="flex-1" />
        <button type="button" className="btn-ghost h-5 gap-1 border border-line px-1.5 text-[10px]" onClick={onDone} title={a.suggested_stop != null ? `Records your stop at ${formatPrice(a.suggested_stop)}` : "Mark as done"}>
          <Check className="h-3 w-3" /> {a.suggested_stop != null ? "Moved it" : "Done"}
        </button>
        <button type="button" className="btn-ghost h-5 border border-line px-1.5 text-[10px]" onClick={onDismiss}>
          Ignore
        </button>
      </div>
    </div>
  );
}

/** One of the user's own open positions from the Binance import (spot holding or USD-M position). */
interface BinancePosition {
  key: string;
  symbol: string;
  direction: "long" | "short";
  qty: number;
  entry: number | null;
  market: "spot" | "futures";
}

function AddTrade(p: { symbol: string; interval: Interval; price: number | null; onAdded(t: ManagedTrade): void }) {
  const [journal, setJournal] = useState<JournalEntry[] | null>(null);
  const [tf, setTf] = useState<Interval>(MANAGE_TFS.includes(p.interval) ? p.interval : "15m");
  const [f, setF] = useState({ symbol: p.symbol, direction: "long" as "long" | "short", entry: p.price ? String(p.price) : "", stop: "", targets: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [positions, setPositions] = useState<BinancePosition[]>([]);
  // The Binance position the form was filled from: the trade closes here when that position closes on Binance.
  const [picked, setPicked] = useState<BinancePosition | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    fetchJournal(ctrl.signal)
      .then((r) => setJournal(r.entries.filter((e) => e.evaluation.status === "open" && e.stop != null)))
      .catch(() => setJournal([]));
    // Only the user's own positions: holdings and positions classed as a bot's are left out. No key = no list.
    fetchAccountPositions(false, ctrl.signal)
      .then((r) =>
        setPositions([
          ...r.manual.spot
            .filter((h) => h.own_qty > 0 && (h.value == null || h.value >= 5))
            .map((h) => ({ key: h.key, symbol: h.symbol, direction: "long" as const, qty: h.own_qty, entry: h.avg_entry, market: "spot" as const })),
          ...r.manual.futures.map((f) => ({ key: f.key, symbol: f.symbol, direction: f.side, qty: f.qty, entry: f.entry_price || null, market: "futures" as const })),
        ]),
      )
      .catch(() => setPositions([]));
    return () => ctrl.abort();
  }, []);

  const pick = (b: BinancePosition) => {
    setPicked(b);
    setF({ symbol: b.symbol, direction: b.direction, entry: b.entry ? String(b.entry) : "", stop: "", targets: "" });
  };

  const submit = async (body: Parameters<typeof manageTrade>[0]) => {
    setBusy(true);
    setError(null);
    try {
      p.onAdded(await manageTrade(body));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const input = "h-7 rounded border border-line bg-base px-2 font-mono text-[12px] text-ink outline-none focus:border-accent";
  return (
    <div className="rounded-md border border-accent/40 bg-panel2/40 p-2">
      <div className="mb-2 flex items-center gap-2 text-[11px]">
        <span className="text-mute">Manage on</span>
        <select value={tf} onChange={(e) => setTf(e.target.value as Interval)} className="h-6 rounded border border-line bg-base px-1 text-ink outline-none">
          {MANAGE_TFS.map((v) => (
            <option key={v} value={v}>
              {tfLabel(v)}
            </option>
          ))}
        </select>
        <span className="text-mute">candles (swings and structure breaks)</span>
      </div>

      {positions.length > 0 && (
        <>
          <p className="mb-1 text-[11px] text-mute">Your positions on Binance (bots&apos; holdings left out). Pick one, then add its stop:</p>
          {positions.map((b) => (
            <button
              key={b.key}
              type="button"
              disabled={busy}
              onClick={() => pick(b)}
              className={clsx(
                "mb-1 flex w-full items-center gap-2 rounded border px-2 py-1 text-left text-[11px] hover:border-accent/60",
                picked?.key === b.key ? "border-accent" : "border-line",
              )}
            >
              <span className="font-medium text-ink">{displaySymbol(b.symbol)}</span>
              <span className={b.direction === "long" ? "text-up" : "text-down"}>{b.market === "spot" ? "spot" : b.direction}</span>
              <span className="font-mono text-mute">
                {formatPrice(b.qty)} {b.entry ? `@ ${formatPrice(b.entry)}` : "· entry unknown"}
              </span>
            </button>
          ))}
        </>
      )}

      {journal && journal.length > 0 && (
        <>
          <p className="mb-1 text-[11px] text-mute">Open trades in your journal:</p>
          {journal.map((e) => (
            <button
              key={e.id}
              type="button"
              disabled={busy}
              onClick={() => void submit({ journal_id: e.id, interval: tf })}
              className="mb-1 flex w-full items-center gap-2 rounded border border-line px-2 py-1 text-left text-[11px] hover:border-accent/60"
            >
              <span className="font-medium text-ink">{displaySymbol(e.symbol)}</span>
              <span className={e.direction === "long" ? "text-up" : "text-down"}>{e.direction}</span>
              <span className="font-mono text-mute">
                {formatPrice(e.entry)} · stop {formatPrice(e.stop ?? 0)}
              </span>
            </button>
          ))}
        </>
      )}
      {(positions.length > 0 || (journal && journal.length > 0)) && <p className="mb-1 mt-2 text-[11px] text-mute">Or type it in:</p>}

      <form
        className="grid grid-cols-2 gap-1.5"
        onSubmit={(ev) => {
          ev.preventDefault();
          const targets = f.targets.split(/[\s,]+/).map(Number).filter((x) => x > 0);
          const from = picked && picked.symbol === f.symbol && picked.direction === f.direction ? picked : null;
          void submit({
            symbol: f.symbol,
            interval: tf,
            direction: f.direction,
            entry: Number(f.entry),
            stop: Number(f.stop),
            targets,
            ...(from ? { source: "binance" as const, source_id: from.key, qty: from.qty } : {}),
          });
        }}
      >
        <input className={input} value={f.symbol} onChange={(e) => setF({ ...f, symbol: e.target.value.toUpperCase() })} placeholder="INJUSDT" aria-label="Symbol" />
        <select className={input} value={f.direction} onChange={(e) => setF({ ...f, direction: e.target.value as "long" | "short" })} aria-label="Direction">
          <option value="long">Long</option>
          <option value="short">Short</option>
        </select>
        <input className={input} value={f.entry} onChange={(e) => setF({ ...f, entry: e.target.value })} placeholder="Entry" inputMode="decimal" aria-label="Entry" />
        <input className={input} value={f.stop} onChange={(e) => setF({ ...f, stop: e.target.value })} placeholder="Stop" inputMode="decimal" aria-label="Stop" />
        <input className={clsx(input, "col-span-2")} value={f.targets} onChange={(e) => setF({ ...f, targets: e.target.value })} placeholder="Targets, e.g. 7.95 8.20" aria-label="Targets" />
        <button type="submit" disabled={busy || !Number(f.entry) || !Number(f.stop)} className="col-span-2 h-7 rounded bg-accent text-[12px] text-white disabled:opacity-50">
          {busy ? "Adding…" : "Manage this trade"}
        </button>
      </form>
      {error && <p className="mt-1.5 text-[11px] text-down">{error}</p>}
    </div>
  );
}
