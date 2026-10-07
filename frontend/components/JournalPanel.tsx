"use client";

import clsx from "clsx";
import { Ban, Eye, EyeOff, Loader2, Pencil, Plus, RefreshCw, RotateCcw, Send, Sparkles, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import EquityCurve from "@/components/EquityCurve";
import { usePersistentState } from "@/hooks/usePersistentState";
import { browserTimeZone, timeZones } from "@/lib/alerts";
import type { DockPanelProps } from "@/lib/dock";
import { displaySymbol, formatPrice } from "@/lib/format";
import {
  JOURNAL_EVENT,
  createJournalEntry,
  deleteJournalEntry,
  fetchJournal,
  fetchJournalStats,
  fetchReviewSettings,
  fetchWeeklyReview,
  journalOverlays,
  postmortemPending,
  regeneratePostmortem,
  saveReviewSettings,
  sendWeeklyReview,
  updateJournalEntry,
  type JournalDirection,
  type JournalEntry,
  type JournalGroupStats,
  type JournalStats,
  type ReviewSettings,
  type ReviewStatus,
  type WeeklyReview,
} from "@/lib/journal";

const REFRESH_MS = 30_000;
const STATUS_ORDER = { open: 0, pending: 1, closed: 2, cancelled: 3 } as const;

function fmtR(r: number | null | undefined, digits = 2): string {
  if (r == null || !Number.isFinite(r)) return "–";
  return `${r >= 0 ? "+" : "−"}${Math.abs(r).toFixed(digits)}R`;
}

function fmtUsd(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "";
  return `${v >= 0 ? "+" : "−"}$${Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function fmtPct(v: number | null | undefined): string {
  return v == null ? "–" : `${Math.round(v * 100)}%`;
}

function when(t: number): string {
  return new Date(t * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function tone(r: number | null | undefined): string {
  return r == null || Math.abs(r) < 1e-9 ? "text-mute" : r > 0 ? "text-up" : "text-down";
}

function StatusPill({ e }: { e: JournalEntry }) {
  const ev = e.evaluation;
  const label =
    ev.status === "closed" ? (ev.outcome === "win" ? "Win" : ev.outcome === "loss" ? "Loss" : "Breakeven")
      : ev.status === "open" ? "Open" : ev.status === "pending" ? "Pending" : "Cancelled";
  const cls =
    ev.status === "open" ? "border-accent/50 text-accent"
      : ev.status === "pending" ? "border-yellow-400/40 text-yellow-300"
        : ev.status === "cancelled" ? "border-line text-mute"
          : ev.outcome === "win" ? "border-up/50 text-up" : ev.outcome === "loss" ? "border-down/50 text-down" : "border-line text-mute";
  return <span className={clsx("rounded border px-1 py-px text-[10px] font-medium uppercase tracking-wide", cls)}>{label}</span>;
}

// ------------------------------------------------------------------ trades --

interface RowProps {
  e: JournalEntry;
  shown: boolean;
  livePrice: number | null;
  busy: boolean;
  onPick(): void;
  onToggleShow(): void;
  onClose(): void;
  onCancel(): void;
  onDelete(): void;
  onSaveNotes(notes: string): void;
  onRegenerate(): void;
}

function TradeRow({ e, shown, livePrice, busy, ...a }: RowProps) {
  const ev = e.evaluation;
  const long = e.direction === "long";
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(e.notes);
  useEffect(() => setDraft(e.notes), [e.notes]);
  const live = ev.status === "open" || ev.status === "pending";
  const r = ev.status === "open" ? ev.realized_r + ev.open_r : ev.realized_r;
  // Imported from Binance: the fills are the trade, so there is nothing to close or cancel here, and no R without a stop.
  const im = e.imported;

  return (
    <div className={clsx("border-b border-line/60 px-3 py-2 text-[12px]", ev.status === "cancelled" && "opacity-60")}>
      <div className="flex items-center gap-1.5">
        <StatusPill e={e} />
        <button type="button" onClick={a.onPick} className="font-medium text-ink hover:text-accent" title="Open on the chart">
          {displaySymbol(e.symbol)}
        </button>
        <span className={clsx("text-[11px] font-semibold", long ? "text-up" : "text-down")}>{long ? "Long" : "Short"}</span>
        {im && (
          <span className="rounded bg-panel2 px-1 py-px text-[10px] text-mute" title={`Imported from your Binance ${im.market} fills`}>
            Binance {im.market}
          </span>
        )}
        <span className="min-w-0 flex-1 truncate text-[11px] text-mute" title={e.setup}>{e.setup}</span>
        {busy && <Loader2 className="h-3.5 w-3.5 animate-spin text-mute" />}
        <button type="button" onClick={a.onToggleShow} className={clsx("btn-ghost h-6 w-6 p-0", shown && "text-accent")}
          title={shown ? "Hide from the chart" : "Show on the chart"}>
          {shown ? <Eye className="h-3.5 w-3.5" /> : <EyeOff className="h-3.5 w-3.5" />}
        </button>
        <button type="button" onClick={() => setEditing(!editing)} className="btn-ghost h-6 w-6 p-0" title="Notes">
          <Pencil className="h-3.5 w-3.5" />
        </button>
        {ev.status === "open" && !im && (
          <button type="button" onClick={a.onClose} className="btn-ghost h-6 w-6 p-0 hover:text-yellow-300"
            title={livePrice != null ? `Close now at ${formatPrice(livePrice)}` : "Close now at the last price"}>
            <X className="h-3.5 w-3.5" />
          </button>
        )}
        {ev.status === "pending" && (
          <button type="button" onClick={a.onCancel} className="btn-ghost h-6 w-6 p-0 hover:text-yellow-300" title="Cancel the order">
            <Ban className="h-3.5 w-3.5" />
          </button>
        )}
        <button type="button" onClick={a.onDelete} className="btn-ghost h-6 w-6 p-0 hover:text-down"
          title={im ? "Delete from the journal (later imports skip it)" : "Delete from the journal"}>
          <Trash2 className="h-3.5 w-3.5" />
        </button>
      </div>

      <div className="mt-1 grid grid-cols-[auto_1fr_auto] gap-x-2 font-mono text-[11px]">
        <span className="text-mute">Entry</span>
        <span className="truncate text-ink">
          {formatPrice(ev.fill_price ?? e.entry)}
          {im ? (
            im.exit_price != null && <span className="text-mute"> → {formatPrice(im.exit_price)} · {im.fills} fills</span>
          ) : (
            <>
              <span className="text-mute"> · stop </span>
              <span className="text-down">{formatPrice(ev.current_stop ?? e.stop ?? e.entry)}</span>
              <span className="text-mute"> · {e.targets.map((t) => formatPrice(t)).join(" / ")}</span>
            </>
          )}
        </span>
        <span className={clsx("text-right", tone(r))}>
          {ev.status === "pending" || ev.status === "cancelled" || e.stop == null ? "–" : fmtR(r)}
        </span>
        <span className="text-mute">{ev.status === "open" ? "Open" : ev.status === "closed" ? "Result" : "Taken"}</span>
        <span className="truncate text-mute">
          {ev.status === "open" && (
            <>
              realized <span className={tone(ev.realized_r)}>{fmtR(ev.realized_r)}</span>, open{" "}
              <span className={tone(ev.open_r)}>{fmtR(ev.open_r)}</span>
            </>
          )}
          {ev.status === "closed" && ev.closed_at != null && `closed ${when(ev.closed_at)}`}
          {(ev.status === "pending" || ev.status === "cancelled") && `${when(e.taken_at)} · ${e.entry_type}`}
        </span>
        <span className={clsx("text-right", tone(ev.pnl_usd))}>
          {fmtUsd(ev.status === "open" ? (ev.pnl_usd ?? 0) + (ev.open_pnl_usd ?? 0) : ev.pnl_usd)}
        </span>
        {ev.mfe_r != null && (
          <>
            <span className="text-mute">Range</span>
            <span className="truncate text-mute">
              best <span className="text-up">{fmtR(ev.mfe_r)}</span> · worst <span className="text-down">{fmtR(ev.mae_r)}</span>
            </span>
            <span className="text-right text-mute">{ev.exits.map((x) => (x.kind === "breakeven" ? "BE" : x.kind)).join(" ")}</span>
          </>
        )}
      </div>

      {(ev.error || ev.notes.length > 0) && (
        <div className="mt-1 space-y-0.5 text-[10px] leading-snug text-mute">
          {ev.error && <div className="text-yellow-300">{ev.error}</div>}
          {ev.notes.map((n) => (
            <div key={n}>{n}</div>
          ))}
        </div>
      )}
      {editing ? (
        <div className="mt-1.5 flex flex-col gap-1">
          <textarea
            value={draft}
            onChange={(ev2) => setDraft(ev2.target.value)}
            rows={3}
            maxLength={2000}
            className="w-full resize-y rounded border border-line bg-base px-2 py-1 text-[11px] text-ink outline-none focus:border-accent"
            placeholder="Why you took it, how you managed it…"
          />
          <div className="flex justify-end gap-1">
            <button type="button" className="btn-ghost h-6 px-2 text-[11px]" onClick={() => { setDraft(e.notes); setEditing(false); }}>
              Cancel
            </button>
            <button type="button" className="btn-ghost h-6 px-2 text-[11px] text-accent" onClick={() => { a.onSaveNotes(draft); setEditing(false); }}>
              Save notes
            </button>
          </div>
        </div>
      ) : (
        e.notes && <p className="mt-1 whitespace-pre-wrap text-[11px] text-ink/80">{e.notes}</p>
      )}
      {live && e.tags.length > 0 && <div className="mt-1 text-[10px] text-mute">{e.tags.map((t) => `#${t}`).join(" ")}</div>}
      {ev.status === "closed" && <PostMortemView e={e} busy={busy} onRegenerate={a.onRegenerate} />}
    </div>
  );
}

function parseNum(s: string): number | null {
  const v = Number(s.replace(/,/g, "").trim());
  return s.trim() && Number.isFinite(v) && v > 0 ? v : null;
}

function AddTradeForm({ symbol, interval, price, onDone }: Pick<DockPanelProps, "symbol" | "interval" | "price"> & { onDone(): void }) {
  const [direction, setDirection] = useState<JournalDirection>("long");
  const [entryType, setEntryType] = useState<"limit" | "market">("limit");
  const [entry, setEntry] = useState(price != null ? String(price) : "");
  const [stop, setStop] = useState("");
  const [targets, setTargets] = useState("");
  const [setup, setSetup] = useState("manual");
  const [risk, setRisk] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const submit = async () => {
    const e = parseNum(entry);
    const s = parseNum(stop);
    const ts = targets.split(/[\s,;/]+/).filter(Boolean).map(parseNum);
    const long = direction === "long";
    if (e == null || s == null) return setError("Enter an entry and a stop price.");
    if (!ts.length || ts.some((t) => t == null)) return setError("Enter one or more target prices, separated by commas.");
    if (long ? s >= e : s <= e) return setError(`A ${direction}'s stop goes ${long ? "below" : "above"} the entry.`);
    if (ts.some((t) => (long ? t! <= e : t! >= e))) return setError(`A ${direction}'s targets go ${long ? "above" : "below"} the entry.`);
    const r = risk.trim() ? parseNum(risk) : null;
    if (risk.trim() && r == null) return setError("Risk must be a positive dollar amount.");
    setSaving(true);
    setError(null);
    try {
      await createJournalEntry({
        symbol, interval, direction, entry: e, stop: s, targets: ts as number[], setup: setup.trim() || "manual",
        source: "manual", entry_type: entryType, risk_usd: r,
      });
      onDone();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const input = "h-7 w-full rounded border border-line bg-base px-2 font-mono text-[11px] text-ink outline-none focus:border-accent";
  return (
    <div className="border-b border-line bg-panel2/50 px-3 py-2 text-[11px]">
      <div className="mb-1.5 flex items-center gap-1.5">
        <span className="font-semibold text-ink">Log a trade on {displaySymbol(symbol)}</span>
        <span className="text-mute">{interval}</span>
      </div>
      <div className="grid grid-cols-2 gap-1.5">
        <div className="col-span-2 flex gap-1">
          {(["long", "short"] as const).map((d) => (
            <button key={d} type="button" onClick={() => setDirection(d)}
              className={clsx("h-7 flex-1 rounded border text-[11px] font-semibold",
                direction === d ? (d === "long" ? "border-up/60 bg-up/10 text-up" : "border-down/60 bg-down/10 text-down") : "border-line text-mute")}>
              {d === "long" ? "Long" : "Short"}
            </button>
          ))}
          {(["limit", "market"] as const).map((t) => (
            <button key={t} type="button" onClick={() => setEntryType(t)}
              className={clsx("h-7 flex-1 rounded border text-[11px]", entryType === t ? "border-accent/60 text-accent" : "border-line text-mute")}>
              {t === "limit" ? "Limit" : "Market"}
            </button>
          ))}
        </div>
        <label className="text-mute">Entry<input className={input} value={entry} onChange={(e) => setEntry(e.target.value)} inputMode="decimal" /></label>
        <label className="text-mute">Stop<input className={input} value={stop} onChange={(e) => setStop(e.target.value)} inputMode="decimal" /></label>
        <label className="col-span-2 text-mute">
          Targets (comma separated)
          <input className={input} value={targets} onChange={(e) => setTargets(e.target.value)} placeholder="e.g. 25.4, 26.1" />
        </label>
        <label className="text-mute">Setup<input className={input} value={setup} onChange={(e) => setSetup(e.target.value)} maxLength={60} /></label>
        <label className="text-mute">Risk $ (optional)<input className={input} value={risk} onChange={(e) => setRisk(e.target.value)} inputMode="decimal" /></label>
      </div>
      {entryType === "market" && <p className="mt-1 text-mute">Market orders fill at the open of the next 1m candle; R is measured from your entry to the stop.</p>}
      {error && <p className="mt-1 text-down">{error}</p>}
      <div className="mt-1.5 flex justify-end gap-1">
        <button type="button" className="btn-ghost h-7 px-2 text-[11px]" onClick={onDone}>Cancel</button>
        <button type="button" disabled={saving} onClick={submit}
          className="inline-flex h-7 items-center gap-1 rounded bg-accent px-3 text-[11px] font-semibold text-white disabled:opacity-50">
          {saving && <Loader2 className="h-3 w-3 animate-spin" />} Log trade
        </button>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------- stats --

function Tile({ label, value, cls }: { label: string; value: string; cls?: string }) {
  return (
    <div className="rounded border border-line bg-panel2/60 px-2 py-1.5">
      <div className="text-[10px] uppercase tracking-wide text-mute">{label}</div>
      <div className={clsx("font-mono text-[13px] font-semibold", cls ?? "text-ink")}>{value}</div>
    </div>
  );
}

function Breakdown({ title, rows, name }: { title: string; rows: JournalGroupStats[]; name?(key: string): string }) {
  if (!rows.length) return null;
  return (
    <div>
      <div className="mb-0.5 text-[10px] font-semibold uppercase tracking-wide text-mute">{title}</div>
      <table className="w-full text-[11px]">
        <thead>
          <tr className="text-left text-mute">
            <th className="py-0.5 font-normal" />
            <th className="py-0.5 text-right font-normal">Trades</th>
            <th className="py-0.5 text-right font-normal">Win</th>
            <th className="py-0.5 text-right font-normal">Avg</th>
            <th className="py-0.5 text-right font-normal">Total</th>
          </tr>
        </thead>
        <tbody className="font-mono">
          {rows.map((g) => (
            <tr key={g.key} className="border-t border-line/50">
              <td className="max-w-[8rem] truncate py-0.5 font-sans text-ink" title={g.key}>{name ? name(g.key) : g.key}</td>
              <td className="py-0.5 text-right text-ink">{g.trades}</td>
              <td className="py-0.5 text-right text-ink">{fmtPct(g.win_rate)}</td>
              <td className={clsx("py-0.5 text-right", tone(g.avg_r))}>{fmtR(g.avg_r)}</td>
              <td className={clsx("py-0.5 text-right", tone(g.total_r))}>{fmtR(g.total_r, 1)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function StatsView({ symbol }: { symbol: string }) {
  const [scope, setScope] = usePersistentState<"all" | "coin">("ac:journal-stats-scope", "all");
  const [direction, setDirection] = usePersistentState<"" | JournalDirection>("ac:journal-stats-direction", "");
  const [stats, setStats] = useState<JournalStats | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const s = await fetchJournalStats({ symbol: scope === "coin" ? symbol : undefined, direction: direction || undefined }, signal);
      setStats(s);
      setError(null);
    } catch (err) {
      if ((err as Error).name !== "AbortError") setError((err as Error).message);
    }
  }, [scope, symbol, direction]);

  useEffect(() => {
    const ctrl = new AbortController();
    load(ctrl.signal);
    const timer = setInterval(() => load(), REFRESH_MS);
    const onChange = () => load();
    window.addEventListener(JOURNAL_EVENT, onChange);
    return () => {
      ctrl.abort();
      clearInterval(timer);
      window.removeEventListener(JOURNAL_EVENT, onChange);
    };
  }, [load]);

  const sel = "h-6 rounded border border-line bg-base px-1 text-[11px] text-ink outline-none";
  return (
    <div className="space-y-3 px-3 py-2 text-[12px]">
      <div className="flex items-center gap-1.5">
        <select className={sel} value={scope} onChange={(e) => setScope(e.target.value as "all" | "coin")}>
          <option value="all">All coins</option>
          <option value="coin">{displaySymbol(symbol)} only</option>
        </select>
        <select className={sel} value={direction} onChange={(e) => setDirection(e.target.value as "" | JournalDirection)}>
          <option value="">Longs and shorts</option>
          <option value="long">Longs</option>
          <option value="short">Shorts</option>
        </select>
      </div>
      {error && <p className="text-[11px] text-down">{error}</p>}
      {!stats && !error && <Loader2 className="mx-auto h-4 w-4 animate-spin text-mute" />}
      {stats && (
        <>
          <div className="grid grid-cols-3 gap-1.5">
            <Tile label="Win rate" value={fmtPct(stats.win_rate)} />
            <Tile label="Avg R" value={fmtR(stats.avg_r)} cls={tone(stats.avg_r)} />
            <Tile label="Total R" value={fmtR(stats.total_r, 1)} cls={tone(stats.total_r)} />
            <Tile label="Expectancy" value={fmtR(stats.expectancy)} cls={tone(stats.expectancy)} />
            <Tile label="Profit factor" value={stats.profit_factor == null ? "–" : stats.profit_factor.toFixed(2)} />
            <Tile label="Best / worst" value={`${fmtR(stats.best_r, 1)} ${fmtR(stats.worst_r, 1)}`} />
          </div>
          {stats.imported_closed > 0 && (
            <div className="grid grid-cols-3 gap-1.5">
              <Tile label="PnL (after fees)" value={fmtUsd(stats.pnl_usd) || "–"} cls={tone(stats.pnl_usd)} />
              <Tile label="From Binance" value={String(stats.imported_closed)} />
              <Tile label="Without R" value={String(stats.no_r)} />
            </div>
          )}
          <div className="text-[11px] text-mute">
            {stats.closed} closed ({stats.wins} won, {stats.losses} lost, {stats.breakeven} breakeven) · {stats.open} open
            {stats.open ? ` (${fmtR(stats.open_r)} unrealized)` : ""} · {stats.pending} pending. R is after fees.
          </div>
          <EquityCurve points={stats.equity} />
          <Breakdown title="By setup" rows={stats.by_setup} />
          <Breakdown title="By coin" rows={stats.by_symbol} name={displaySymbol} />
          <Breakdown title="By direction" rows={stats.by_direction} name={(k) => (k === "long" ? "Long" : "Short")} />
          {stats.notes.map((n) => (
            <p key={n} className="text-[11px] text-yellow-300/90">{n}</p>
          ))}
        </>
      )}
    </div>
  );
}

// ------------------------------------------------------------ post-mortems --

function PostMortemView({ e, busy, onRegenerate }: { e: JournalEntry; busy: boolean; onRegenerate(): void }) {
  const [open, setOpen] = usePersistentState<boolean>("ac:journal-postmortem-open", true);
  const pm = e.postmortem;
  const pending = postmortemPending(e);
  return (
    <div className="mt-1.5 rounded border border-line/70 bg-panel2/40 px-2 py-1.5 text-[11px]">
      <div className="flex items-center gap-1.5">
        <button type="button" onClick={() => setOpen(!open)} className="flex min-w-0 flex-1 items-center gap-1 text-left">
          <Sparkles className="h-3 w-3 shrink-0 text-accent" />
          <span className="font-semibold text-ink">Post-mortem</span>
          {pm && (
            <span className="truncate text-[10px] text-mute">
              {pm.engine === "template" ? "rules" : pm.engine} · {when(pm.generated_at)}
            </span>
          )}
        </button>
        <button type="button" onClick={onRegenerate} disabled={busy} className="btn-ghost h-5 px-1 text-[10px] disabled:opacity-50"
          title="Write the review again from the candles">
          <RotateCcw className={clsx("h-3 w-3", busy && "animate-spin")} /> {pm ? "Regenerate" : "Write now"}
        </button>
      </div>
      {open && (
        <div className="mt-1 space-y-1 leading-snug">
          {pm ? (
            <>
              {pending && <div className="text-[10px] text-yellow-300">Written for an earlier result; regenerate to update it.</div>}
              <p className="text-ink/90">{pm.summary}</p>
              {pm.lessons.length > 0 && (
                <ul className="space-y-0.5">
                  {pm.lessons.map((l) => (
                    <li key={l} className="flex gap-1.5 text-ink">
                      <span className="text-accent">→</span>
                      <span>{l}</span>
                    </li>
                  ))}
                </ul>
              )}
            </>
          ) : (
            <p className="text-mute">Writing the review from the candles… it appears here in a few seconds.</p>
          )}
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------ weekly review --

const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

function ReviewView() {
  const [days, setDays] = usePersistentState<number>("ac:journal-review-days", 7);
  const [review, setReview] = useState<WeeklyReview | null>(null);
  const [status, setStatus] = useState<ReviewStatus | null>(null);
  const [draft, setDraft] = useState<ReviewSettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [sending, setSending] = useState(false);
  const zones = useMemo(() => timeZones(), []);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setReview(await fetchWeeklyReview(days, signal));
      setError(null);
    } catch (err) {
      if ((err as Error).name !== "AbortError") setError((err as Error).message);
    }
  }, [days]);

  useEffect(() => {
    const ctrl = new AbortController();
    load(ctrl.signal);
    const timer = setInterval(() => load(), REFRESH_MS);
    const onChange = () => load();
    window.addEventListener(JOURNAL_EVENT, onChange);
    return () => {
      ctrl.abort();
      clearInterval(timer);
      window.removeEventListener(JOURNAL_EVENT, onChange);
    };
  }, [load]);

  useEffect(() => {
    const ctrl = new AbortController();
    fetchReviewSettings(ctrl.signal)
      .then((s) => {
        setStatus(s);
        // A schedule never saved starts in the browser's time zone.
        setDraft(s.last_sent_at == null && s.settings.timezone === "UTC" && !s.settings.enabled
          ? { ...s.settings, timezone: browserTimeZone() } : s.settings);
      })
      .catch((err: Error) => err.name !== "AbortError" && setError(err.message));
    return () => ctrl.abort();
  }, []);

  const save = async () => {
    if (!draft) return;
    setSaving(true);
    setNote(null);
    try {
      const s = await saveReviewSettings(draft);
      setStatus(s);
      setDraft(s.settings);
      setNote(s.settings.enabled ? `Sends every ${WEEKDAYS[s.settings.weekday]} at ${s.settings.time} (${s.settings.timezone}).` : "Schedule off.");
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const send = async () => {
    setSending(true);
    setNote(null);
    try {
      const res = await sendWeeklyReview(days);
      const ok = Object.entries(res.results).filter(([, v]) => v).map(([k]) => k);
      setNote(ok.length ? `Sent to ${ok.join(" and ")}.` : "Sending failed; see the backend log.");
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSending(false);
    }
  };

  const channels = status ? Object.entries(status.channels).filter(([, v]) => v).map(([k]) => k) : [];
  const sel = "h-6 rounded border border-line bg-base px-1 text-[11px] text-ink outline-none";
  const group = (label: string, g: JournalGroupStats | null, name = (k: string) => k) =>
    g && (
      <div className="flex items-baseline gap-1.5">
        <span className="w-20 shrink-0 text-mute">{label}</span>
        <span className="min-w-0 flex-1 truncate text-ink" title={g.key}>{name(g.key)}</span>
        <span className="font-mono text-mute">{g.trades}×</span>
        <span className={clsx("w-14 text-right font-mono", tone(g.avg_r))}>{fmtR(g.avg_r)}</span>
      </div>
    );

  return (
    <div className="space-y-3 px-3 py-2 text-[12px]">
      <div className="flex items-center gap-1.5">
        <select className={sel} value={days} onChange={(e) => setDays(Number(e.target.value))}>
          <option value={7}>Last 7 days</option>
          <option value={14}>Last 14 days</option>
          <option value={30}>Last 30 days</option>
        </select>
        {review && review.missing_postmortems > 0 && (
          <span className="text-[10px] text-mute">{review.missing_postmortems} post-mortem{review.missing_postmortems > 1 ? "s" : ""} still being written</span>
        )}
      </div>
      {error && <p className="text-[11px] text-down">{error}</p>}
      {!review && !error && <Loader2 className="mx-auto h-4 w-4 animate-spin text-mute" />}
      {review && (review.closed === 0 ? (
        <p className="text-[12px] leading-relaxed text-mute">
          No trades closed in the last {review.days} days{review.open ? ` (${review.open} still open)` : ""}. Each closed trade
          gets a post-mortem; this page sums them up.
        </p>
      ) : (
        <>
          <div className="grid grid-cols-3 gap-1.5">
            <Tile label="Win rate" value={fmtPct(review.win_rate)} />
            <Tile label="Avg R" value={fmtR(review.avg_r)} cls={tone(review.avg_r)} />
            <Tile label="Total R" value={fmtR(review.total_r, 1)} cls={tone(review.total_r)} />
          </div>
          <div className="text-[11px] text-mute">
            {review.closed} closed ({review.wins} won, {review.losses} lost, {review.breakeven} breakeven) · {review.open} open
          </div>
          <div className="space-y-0.5 text-[11px]">
            {group("Best setup", review.best_setup)}
            {group("Worst setup", review.worst_setup)}
            {group("Best coin", review.best_symbol, displaySymbol)}
            {group("Worst coin", review.worst_symbol, displaySymbol)}
          </div>
          {review.recurring.length > 0 && (
            <div>
              <div className="mb-0.5 text-[10px] font-semibold uppercase tracking-wide text-mute">Keeps coming back</div>
              {review.recurring.map((r) => (
                <div key={r.code} className="mb-1 text-[11px]">
                  <span className="text-ink">{r.name}</span> <span className="text-mute">({r.count}×)</span>
                  <div className="text-mute">{r.example}</div>
                </div>
              ))}
            </div>
          )}
          <div>
            <div className="mb-0.5 text-[10px] font-semibold uppercase tracking-wide text-mute">Trades</div>
            {review.trades.map((t) => (
              <div key={t.id} className="flex gap-1.5 border-t border-line/50 py-0.5 text-[11px]">
                <span className="w-16 shrink-0 text-ink">{displaySymbol(t.symbol)}</span>
                <span className="min-w-0 flex-1 truncate text-mute" title={t.lesson ?? t.setup}>{t.lesson ?? t.setup}</span>
                <span className={clsx("w-14 shrink-0 text-right font-mono", tone(t.realized_r))}>{fmtR(t.realized_r)}</span>
              </div>
            ))}
          </div>
        </>
      ))}

      <div className="space-y-1.5 border-t border-line pt-2">
        <div className="text-[10px] font-semibold uppercase tracking-wide text-mute">Send it every week</div>
        {draft ? (
          <>
            <label className="flex items-center gap-2 text-[11px] text-ink">
              <input type="checkbox" className="accent-blue-500" checked={draft.enabled}
                onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} />
              Send the review to {channels.length ? channels.join(" and ") : "Telegram / Discord"}
            </label>
            <div className="flex flex-wrap items-center gap-1.5">
              <select className={sel} value={draft.weekday} onChange={(e) => setDraft({ ...draft, weekday: Number(e.target.value) })}>
                {WEEKDAYS.map((d, i) => (
                  <option key={d} value={i}>{d}</option>
                ))}
              </select>
              <input type="time" className={sel} value={draft.time} onChange={(e) => setDraft({ ...draft, time: e.target.value })} />
              <select className={clsx(sel, "max-w-[11rem]")} value={draft.timezone} onChange={(e) => setDraft({ ...draft, timezone: e.target.value })}>
                {(zones.includes(draft.timezone) ? zones : [draft.timezone, ...zones]).map((z) => (
                  <option key={z} value={z}>{z}</option>
                ))}
              </select>
            </div>
            <div className="flex items-center gap-1.5">
              <button type="button" onClick={save} disabled={saving} className="btn-ghost h-6 px-2 text-[11px] text-accent disabled:opacity-50">
                {saving ? "Saving…" : "Save schedule"}
              </button>
              <div className="flex-1" />
              <button type="button" onClick={send} disabled={sending || !channels.length}
                className="btn-ghost h-6 px-2 text-[11px] disabled:opacity-50"
                title={channels.length ? "Send this review now" : "Configure Telegram or Discord on the backend first"}>
                <Send className="h-3 w-3" /> {sending ? "Sending…" : "Send now"}
              </button>
            </div>
            {!channels.length && (
              <p className="text-[10px] text-mute">No Telegram or Discord channel is configured on the backend (see Alerts).</p>
            )}
            {status?.last_sent_at != null && <p className="text-[10px] text-mute">Last sent {when(status.last_sent_at / 1000)}.</p>}
            {note && <p className="text-[11px] text-up">{note}</p>}
          </>
        ) : (
          <Loader2 className="h-4 w-4 animate-spin text-mute" />
        )}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------- panel --

/**
 * The trade journal: trades logged from the agent's plan card or by hand, tracked on live 1m candles (filled, hit
 * T1, stopped, still open), a post-mortem on each closed trade, stats by setup, coin and direction, and the weekly
 * review (optionally sent to Telegram / Discord on a schedule).
 */
export default function JournalPanel(p: DockPanelProps) {
  const [tab, setTab] = usePersistentState<"trades" | "stats" | "review">("ac:journal-tab", "trades");
  const [shown, setShown] = usePersistentState<string[]>("ac:journal-shown", []);
  const [entries, setEntries] = useState<JournalEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const drawn = useRef(new Map<string, { symbol: string; sig: string }>());
  const { onChartOverlays } = p;

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    try {
      const res = await fetchJournal(signal);
      setEntries(res.entries);
      setError(null);
    } catch (err) {
      if ((err as Error).name !== "AbortError") setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const ctrl = new AbortController();
    load(ctrl.signal);
    const timer = setInterval(() => load(), REFRESH_MS);
    const onChange = () => load();
    window.addEventListener(JOURNAL_EVENT, onChange);
    return () => {
      ctrl.abort();
      clearInterval(timer);
      window.removeEventListener(JOURNAL_EVENT, onChange);
    };
  }, [load]);

  // Keep the chart in step with the "show" toggles; only redraw a trade when its overlays actually changed.
  useEffect(() => {
    if (!entries) return;
    const want = new Map(entries.filter((e) => shown.includes(e.id)).map((e) => [`journal:${e.id}`, e]));
    for (const [key, prev] of drawn.current) {
      if (!want.has(key)) {
        onChartOverlays(key, prev.symbol, []);
        drawn.current.delete(key);
      }
    }
    for (const [key, e] of want) {
      const ovs = journalOverlays(e);
      const sig = JSON.stringify(ovs);
      if (drawn.current.get(key)?.sig === sig) continue;
      onChartOverlays(key, e.symbol, ovs);
      drawn.current.set(key, { symbol: e.symbol, sig });
    }
  }, [entries, shown, onChartOverlays]);

  // Post-mortems are written in the background after a close: look again soon (a few times) while one is missing.
  const waiting = entries?.some(postmortemPending) ?? false;
  const polls = useRef(0);
  useEffect(() => {
    if (!waiting) {
      polls.current = 0;
      return;
    }
    if (polls.current >= 6) return;
    const timer = setTimeout(() => {
      polls.current += 1;
      void load();
    }, 5_000);
    return () => clearTimeout(timer);
  }, [waiting, entries, load]);

  const sorted = useMemo(
    () =>
      [...(entries ?? [])].sort(
        (a, b) =>
          STATUS_ORDER[a.evaluation.status] - STATUS_ORDER[b.evaluation.status] ||
          (b.evaluation.closed_at ?? b.taken_at) - (a.evaluation.closed_at ?? a.taken_at),
      ),
    [entries],
  );

  const act = async (id: string, fn: () => Promise<unknown>) => {
    setBusy(id);
    try {
      await fn();
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const toggleShow = (e: JournalEntry) => {
    const on = shown.includes(e.id);
    setShown(on ? shown.filter((x) => x !== e.id) : [...shown, e.id]);
    if (!on && e.symbol !== p.symbol) p.onPickSymbol(e.symbol, e.interval);
  };

  const live = sorted.filter((e) => e.evaluation.status === "open" || e.evaluation.status === "pending").length;

  return (
    <div className="flex h-full min-h-0 flex-col text-xs">
      <div className="flex items-center gap-1 border-b border-line px-2 py-1.5">
        {(["trades", "stats", "review"] as const).map((t) => (
          <button key={t} type="button" onClick={() => setTab(t)}
            className={clsx("rounded px-2 py-1 text-[11px] font-medium", tab === t ? "bg-panel2 text-ink" : "text-mute hover:text-ink")}>
            {t === "trades" ? `Trades${live ? ` · ${live} live` : ""}` : t === "stats" ? "Stats" : "Review"}
          </button>
        ))}
        <div className="flex-1" />
        <button type="button" onClick={() => load()} className="btn-ghost h-6 w-6 p-0" title="Refresh">
          <RefreshCw className={clsx("h-3.5 w-3.5", loading && "animate-spin")} />
        </button>
        {tab === "trades" && (
          <button type="button" onClick={() => setAdding(!adding)} className={clsx("btn-ghost h-6 px-1.5 text-[11px]", adding && "text-accent")}>
            <Plus className="h-3.5 w-3.5" /> Add trade
          </button>
        )}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {error && <div className="border-b border-line bg-down/10 px-3 py-2 text-[11px] text-down">{error}</div>}
        {tab === "stats" ? (
          <StatsView symbol={p.symbol} />
        ) : tab === "review" ? (
          <ReviewView />
        ) : (
          <>
            {adding && (
              <AddTradeForm key={p.symbol} symbol={p.symbol} interval={p.interval} price={p.price} onDone={() => setAdding(false)} />
            )}
            {entries === null && !error && <Loader2 className="mx-auto my-4 h-4 w-4 animate-spin text-mute" />}
            {entries !== null && sorted.length === 0 && (
              <p className="px-3 py-4 text-[12px] leading-relaxed text-mute">
                No trades yet. Press &quot;Log trade&quot; on one of the agent&apos;s plans, or add one by hand. Each trade is
                tracked on 1-minute candles: when it fills, hits its targets or its stop.
              </p>
            )}
            {sorted.map((e) => (
              <TradeRow
                key={e.id}
                e={e}
                shown={shown.includes(e.id)}
                livePrice={e.symbol === p.symbol ? p.price : null}
                busy={busy === e.id}
                onPick={() => p.onPickSymbol(e.symbol, e.interval)}
                onToggleShow={() => toggleShow(e)}
                onClose={() =>
                  act(e.id, () =>
                    updateJournalEntry(e.id, { close: e.symbol === p.symbol && p.price != null ? { price: p.price } : {} }),
                  )
                }
                onCancel={() => act(e.id, () => updateJournalEntry(e.id, { cancel: true }))}
                onDelete={() => {
                  if (!window.confirm(`Delete this ${displaySymbol(e.symbol)} trade from the journal?`)) return;
                  setShown(shown.filter((x) => x !== e.id));
                  act(e.id, () => deleteJournalEntry(e.id));
                }}
                onSaveNotes={(notes) => act(e.id, () => updateJournalEntry(e.id, { notes }))}
                onRegenerate={() => act(e.id, () => regeneratePostmortem(e.id))}
              />
            ))}
          </>
        )}
      </div>
    </div>
  );
}
