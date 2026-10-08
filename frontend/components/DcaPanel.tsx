"use client";

import clsx from "clsx";
import { Loader2 } from "lucide-react";
import { useState } from "react";

import { apiRequest } from "@/lib/api";
import type { DockPanelProps } from "@/lib/dock";
import { displaySymbol, formatPrice } from "@/lib/format";

/** Mirrors DcaStrategy / DcaResult in backend/app/dca.py. */
interface DcaStrategy {
  id: "lump" | "weekly" | "daily" | "dips";
  name: string;
  buys: number;
  invested: number;
  coins: number;
  avg_price: number;
  final_value: number;
  return_pct: number;
  max_drawdown_pct: number;
  curve: [number, number][];
}

interface DcaResult {
  symbol: string;
  days: number;
  budget: number;
  start_price: number;
  last_price: number;
  buy_hold_pct: number;
  strategies: DcaStrategy[];
  best: DcaStrategy["id"];
  summary: string;
  data_source: string;
}

const COLORS: Record<DcaStrategy["id"], string> = { lump: "#a78bfa", weekly: "#38bdf8", daily: "#eab308", dips: "#22c55e" };
const PERIODS = [90, 180, 365, 730];

function pct(v: number) {
  return `${v >= 0 ? "+" : ""}${v.toFixed(1)}%`;
}

/** Value over time of each strategy, one line each, with the budget as a dashed baseline. */
function Curves({ r }: { r: DcaResult }) {
  const W = 300;
  const H = 110;
  const all = r.strategies.flatMap((s) => s.curve.map(([, v]) => v));
  const lo = Math.min(r.budget, ...all);
  const hi = Math.max(r.budget, ...all);
  const t0 = r.strategies[0].curve[0]?.[0] ?? 0;
  const t1 = r.strategies[0].curve.at(-1)?.[0] ?? 1;
  const x = (t: number) => ((t - t0) / Math.max(1, t1 - t0)) * W;
  const y = (v: number) => H - ((v - lo) / Math.max(1e-9, hi - lo)) * H;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="h-28 w-full" preserveAspectRatio="none" role="img" aria-label="Value of each strategy over time">
      <line x1={0} x2={W} y1={y(r.budget)} y2={y(r.budget)} stroke="currentColor" className="text-mute" strokeDasharray="3 3" strokeWidth={0.6} />
      {r.strategies.map((s) => (
        <path
          key={s.id}
          d={s.curve.map(([t, v], i) => `${i ? "L" : "M"}${x(t).toFixed(1)},${y(v).toFixed(1)}`).join(" ")}
          fill="none"
          stroke={COLORS[s.id]}
          strokeWidth={s.id === r.best ? 1.8 : 1.1}
          vectorEffect="non-scaling-stroke"
        />
      ))}
    </svg>
  );
}

/**
 * DCA planner: the same budget spent on one coin as a lump sum, weekly, daily, or only on dips, over the last
 * 90 days to 2 years, so you can see which way of buying would have done best before you start.
 */
export default function DcaPanel(p: DockPanelProps) {
  const [coin, setCoin] = useState("");
  const [budget, setBudget] = useState("1000");
  const [days, setDays] = useState(365);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [r, setR] = useState<DcaResult | null>(null);
  const symbol = coin.trim() || p.symbol;

  const run = async () => {
    setBusy(true);
    try {
      setR(
        await apiRequest<DcaResult>("/api/dca", {
          method: "POST",
          body: JSON.stringify({ symbol, budget: Number(budget) || 1000, days }),
          timeoutMs: 60_000,
        }),
      );
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="shrink-0 border-b border-line px-3 py-1.5 text-[11px] text-mute">Which way of buying a coin would have done best</div>
      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-3 py-3 text-[12px]">
        <form
          className="flex flex-wrap items-center gap-1.5"
          onSubmit={(e) => {
            e.preventDefault();
            void run();
          }}
        >
          <input
            className="h-7 w-24 rounded border border-line bg-transparent px-1.5 text-[11px] outline-none focus:border-accent"
            placeholder={displaySymbol(p.symbol)}
            value={coin}
            onChange={(e) => setCoin(e.target.value)}
            aria-label="Coin"
          />
          <input
            className="h-7 w-20 rounded border border-line bg-transparent px-1.5 font-mono text-[11px] outline-none focus:border-accent"
            inputMode="decimal"
            value={budget}
            onChange={(e) => setBudget(e.target.value)}
            aria-label="Budget in USDT"
            title="Budget in USDT"
          />
          <select
            aria-label="Period"
            className="h-7 rounded border border-line bg-transparent px-1 text-[11px] outline-none"
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
          >
            {PERIODS.map((d) => (
              <option key={d} value={d}>
                {d >= 365 ? `${d / 365} year${d > 365 ? "s" : ""}` : `${d} days`}
              </option>
            ))}
          </select>
          <button type="submit" disabled={busy} className="btn-ghost h-7 border border-line px-2 text-[11px] disabled:opacity-50">
            {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : "Compare"}
          </button>
        </form>
        {error && <p className="text-down">{error}</p>}
        {!r && !error && (
          <p className="text-mute">
            Spends the same USDT on the coin four ways: all at once, every week, every day, or only when price is 10% or more below its 30-day high. All
            four invest the whole budget; nothing is sold.
          </p>
        )}
        {r && (
          <>
            <p className="text-ink">{r.summary}</p>
            <Curves r={r} />
            <div className="overflow-hidden rounded-md border border-line">
              <div className="grid grid-cols-[1fr_auto_auto_auto] gap-x-3 border-b border-line bg-panel2/60 px-2 py-1 text-[10px] uppercase tracking-wide text-mute">
                <span>Strategy</span>
                <span className="text-right">Avg price</span>
                <span className="text-right">Return</span>
                <span className="text-right">Worst</span>
              </div>
              {r.strategies.map((s) => (
                <div
                  key={s.id}
                  className={clsx("grid grid-cols-[1fr_auto_auto_auto] items-center gap-x-3 border-b border-line px-2 py-1 last:border-b-0", s.id === r.best && "bg-up/10")}
                  title={`${s.buys} buys · ${s.coins.toPrecision(6)} coins · worth $${s.final_value.toLocaleString()}`}
                >
                  <span className="flex min-w-0 items-center gap-1.5">
                    <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: COLORS[s.id] }} />
                    <span className="truncate text-ink">{s.name}</span>
                  </span>
                  <span className="text-right font-mono text-[11px]">{formatPrice(s.avg_price)}</span>
                  <span className={clsx("text-right font-mono text-[11px]", s.return_pct >= 0 ? "text-up" : "text-down")}>{pct(s.return_pct)}</span>
                  <span className="text-right font-mono text-[11px] text-down">{pct(s.max_drawdown_pct)}</span>
                </div>
              ))}
            </div>
            <p className="text-[11px] text-mute">
              {displaySymbol(r.symbol)} went from {formatPrice(r.start_price)} to {formatPrice(r.last_price)} ({pct(r.buy_hold_pct)}) over {r.days} days. &ldquo;Worst&rdquo; is
              the deepest fall from a high along the way. Fees 0.1% per buy.{r.data_source === "synthetic" ? " Demo prices." : ""}
            </p>
          </>
        )}
      </div>
    </div>
  );
}
