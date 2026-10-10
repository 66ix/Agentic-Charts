"use client";

import clsx from "clsx";
import { ChevronLeft, ChevronRight, Loader2, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { usePersistentState } from "@/hooks/usePersistentState";
import { fetchPnlCalendar, type AccountMarket, type FillKind, type PnlCalendar, type PnlDay } from "@/lib/binance";

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const KINDS: [FillKind | "all", string][] = [
  ["manual", "Mine"],
  ["bot", "Bots"],
  ["all", "All"],
];
const MARKETS: [AccountMarket | "", string][] = [
  ["", "Spot + futures"],
  ["spot", "Spot"],
  ["futures", "Futures"],
];

function usd(v: number, signed = true): string {
  const s = Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `${v < 0 ? "−" : signed && v > 0 ? "+" : ""}$${s}`;
}

function short(v: number): string {
  const a = Math.abs(v);
  const s = a >= 1000 ? `${(a / 1000).toFixed(a >= 10_000 ? 0 : 1)}k` : a >= 100 ? a.toFixed(0) : a.toFixed(2);
  return `${v < 0 ? "−" : "+"}${s}`;
}

const pad = (n: number) => String(n).padStart(2, "0");
const ymd = (y: number, m: number, d: number) => `${y}-${pad(m + 1)}-${pad(d)}`;

/**
 * Daily realized PnL from the imported Binance fills (GET /api/binance/pnl-calendar), as a month calendar: green
 * days made money, red days lost it, the shade is the size against the month's biggest day. Spot sells count on the
 * day they happen, at the average cost of the coins sold, so trimming a position shows up without closing it.
 */
export default function PnlCalendarView({ enabled, version }: { enabled: boolean; version: number }) {
  const [kind, setKind] = usePersistentState<FillKind | "all">("ac:pnl-calendar-kind", "manual");
  const [market, setMarket] = usePersistentState<AccountMarket | "">("ac:pnl-calendar-market", "");
  const [month, setMonth] = useState(() => {
    const d = new Date();
    return { y: d.getFullYear(), m: d.getMonth() };
  });
  const [data, setData] = useState<PnlCalendar | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [picked, setPicked] = useState<string | null>(null);

  const load = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      try {
        setData(await fetchPnlCalendar(kind, market, signal));
        setError(null);
      } catch (err) {
        if ((err as Error).name !== "AbortError") setError((err as Error).message);
      } finally {
        setLoading(false);
      }
    },
    [kind, market],
  );

  useEffect(() => {
    if (!enabled) return;
    const ctrl = new AbortController();
    void load(ctrl.signal);
    return () => ctrl.abort();
  }, [enabled, load, version]);

  const byDate = useMemo(() => new Map((data?.days ?? []).map((d) => [d.date, d])), [data]);
  const prefix = `${month.y}-${pad(month.m + 1)}-`;
  const monthDays = (data?.days ?? []).filter((d) => d.date.startsWith(prefix));
  const monthTotal = monthDays.reduce((s, d) => s + d.pnl, 0);
  const biggest = Math.max(1e-9, ...monthDays.map((d) => Math.abs(d.pnl)));
  const green = monthDays.filter((d) => d.pnl > 0).length;
  const red = monthDays.filter((d) => d.pnl < 0).length;

  const first = new Date(month.y, month.m, 1);
  const lead = (first.getDay() + 6) % 7; // weeks start on Monday
  const count = new Date(month.y, month.m + 1, 0).getDate();
  const cells: (number | null)[] = [...Array(lead).fill(null), ...Array.from({ length: count }, (_, i) => i + 1)];
  while (cells.length % 7) cells.push(null);
  const today = (() => {
    const d = new Date();
    return ymd(d.getFullYear(), d.getMonth(), d.getDate());
  })();
  const shift = (n: number) =>
    setMonth(({ y, m }) => {
      const d = new Date(y, m + n, 1);
      return { y: d.getFullYear(), m: d.getMonth() };
    });
  const pickedDay: PnlDay | undefined = picked ? byDate.get(picked) : undefined;

  if (!enabled) return <p className="px-3 py-4 text-[12px] leading-relaxed text-mute">Add a read-only Binance key (Setup) and import your fills to see your daily PnL.</p>;
  return (
    <div className="space-y-3 px-3 py-2 text-[12px]">
      <div className="flex flex-wrap items-center gap-1.5">
        <select value={kind} onChange={(e) => setKind(e.target.value as FillKind | "all")} className="rounded border border-line bg-panel2 px-1.5 py-0.5 text-[11px] text-ink">
          {KINDS.map(([v, l]) => (
            <option key={v} value={v}>
              {l}
            </option>
          ))}
        </select>
        <select value={market} onChange={(e) => setMarket(e.target.value as AccountMarket | "")} className="rounded border border-line bg-panel2 px-1.5 py-0.5 text-[11px] text-ink">
          {MARKETS.map(([v, l]) => (
            <option key={v} value={v}>
              {l}
            </option>
          ))}
        </select>
        <div className="flex-1" />
        <button type="button" onClick={() => void load()} className="btn-ghost h-6 w-6 p-0" title="Reload">
          <RefreshCw className={clsx("h-3.5 w-3.5", loading && "animate-spin")} />
        </button>
      </div>

      <div className="flex items-center gap-2">
        <button type="button" onClick={() => shift(-1)} className="btn-ghost h-6 w-6 p-0" aria-label="Previous month">
          <ChevronLeft className="h-4 w-4" />
        </button>
        <div className="flex-1 text-center">
          <div className="text-[12px] font-medium text-ink">{first.toLocaleDateString([], { month: "long", year: "numeric" })}</div>
          <div className={clsx("font-mono text-[13px] font-semibold", monthTotal > 0 ? "text-up" : monthTotal < 0 ? "text-down" : "text-mute")}>{usd(monthTotal)}</div>
          <div className="text-[10px] text-mute">
            {green} green · {red} red day{red === 1 ? "" : "s"}
          </div>
        </div>
        <button type="button" onClick={() => shift(1)} className="btn-ghost h-6 w-6 p-0" aria-label="Next month">
          <ChevronRight className="h-4 w-4" />
        </button>
      </div>

      {error && <div className="rounded border border-down/40 bg-down/10 px-2 py-1.5 text-[11px] text-down">{error}</div>}
      {!data && !error && <Loader2 className="mx-auto h-4 w-4 animate-spin text-mute" />}

      <div className="grid grid-cols-7 gap-0.5">
        {WEEKDAYS.map((w) => (
          <div key={w} className="pb-0.5 text-center text-[9px] uppercase text-mute">
            {w}
          </div>
        ))}
        {cells.map((d, i) => {
          if (d == null) return <div key={`e${i}`} />;
          const key = ymd(month.y, month.m, d);
          const day = byDate.get(key);
          const a = day ? 0.15 + 0.55 * Math.min(1, Math.abs(day.pnl) / biggest) : 0;
          const bg = day && day.pnl !== 0 ? (day.pnl > 0 ? `rgba(34, 197, 94, ${a})` : `rgba(239, 68, 68, ${a})`) : undefined;
          return (
            <button
              key={key}
              type="button"
              onClick={() => setPicked(picked === key ? null : key)}
              style={{ background: bg }}
              className={clsx(
                "flex h-11 flex-col items-center justify-center rounded border text-[10px]",
                picked === key ? "border-accent" : key === today ? "border-mute/60" : "border-line",
                !day && "bg-panel2/40",
              )}
              title={day ? `${usd(day.pnl)} · ${day.closes} close${day.closes === 1 ? "" : "s"}` : undefined}
            >
              <span className="text-mute">{d}</span>
              {day && <span className={clsx("font-mono font-semibold", day.pnl >= 0 ? "text-up" : "text-down")}>{short(day.pnl)}</span>}
            </button>
          );
        })}
      </div>

      {pickedDay && (
        <div className="rounded border border-line bg-panel2/60 px-2 py-1.5 text-[11px]">
          <div className="flex items-center gap-2">
            <span className="text-ink">{new Date(`${pickedDay.date}T12:00:00`).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })}</span>
            <span className={clsx("font-mono font-semibold", pickedDay.pnl >= 0 ? "text-up" : "text-down")}>{usd(pickedDay.pnl)}</span>
            <span className="text-mute">
              {pickedDay.wins} win{pickedDay.wins === 1 ? "" : "s"} · {pickedDay.losses} loss{pickedDay.losses === 1 ? "" : "es"}
            </span>
          </div>
          {pickedDay.symbols.map((s) => (
            <div key={s.symbol} className="flex font-mono text-[11px]">
              <span className="text-mute">{s.symbol}</span>
              <span className="flex-1" />
              <span className={s.pnl >= 0 ? "text-up" : "text-down"}>{usd(s.pnl)}</span>
            </div>
          ))}
        </div>
      )}
      {picked && !pickedDay && <p className="text-[11px] text-mute">Nothing closed that day.</p>}

      {data && (
        <div className="space-y-1 text-[10px] leading-snug text-mute">
          <p>
            All imported history: <span className={clsx("font-mono", data.total >= 0 ? "text-up" : "text-down")}>{usd(data.total)}</span>
            {data.first_fill_at ? ` since ${new Date(data.first_fill_at * 1000).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" })}` : ""}. Realized PnL after fees, in USD
            (stablecoin pairs). Import again in Setup to pick up new trades.
          </p>
          {data.notes.map((n) => (
            <p key={n} className="text-yellow-300/90">
              {n}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}
