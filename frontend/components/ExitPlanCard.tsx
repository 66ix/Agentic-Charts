"use client";

import clsx from "clsx";
import { BellRing, LineChart, Loader2, Trash2 } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { armExitPlan, buildExitPlan, deleteExitPlan, EXIT_PROFILES, type ExitPlan, type ExitProfile } from "@/lib/exitPlan";
import { formatPrice } from "@/lib/format";

/** An exit plan for one coin you hold: a profile, its rungs (struck through once you sold there), the line where
 *  holding it is wrong, and buttons to arm the alerts and draw it. */
export default function ExitPlanCard({
  symbol,
  onChart,
}: {
  symbol: string;
  /** Draw (or with null, clear) the plan on the charts showing this coin. */
  onChart?(plan: ExitPlan | null): void;
}) {
  const [profile, setProfile] = useState<ExitProfile>("quarters");
  const [plan, setPlan] = useState<ExitPlan | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [shown, setShown] = useState(false);

  const build = useCallback(
    async (p: ExitProfile) => {
      setBusy(true);
      setError(null);
      try {
        setPlan(await buildExitPlan(symbol, p));
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setBusy(false);
      }
    },
    [symbol],
  );
  useEffect(() => {
    void build(profile);
  }, [build, profile]);
  // The latest callback in a ref: the parent passes a new one each render, and drawing must not loop on it.
  const chartRef = useRef(onChart);
  chartRef.current = onChart;
  useEffect(() => {
    if (shown && plan) chartRef.current?.(plan);
  }, [shown, plan]);

  const coin = symbol.replace(/USDT$/, "");
  const act = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mt-1 rounded border border-accent/30 bg-panel2/40 p-2 text-[11px]">
      <div className="mb-1 flex flex-wrap items-center gap-1">
        {EXIT_PROFILES.map((p) => (
          <button
            key={p.id}
            type="button"
            title={p.hint}
            disabled={busy}
            onClick={() => setProfile(p.id)}
            className={clsx("rounded border px-1.5 py-0.5", profile === p.id ? "border-accent text-ink" : "border-line text-mute hover:text-ink")}
          >
            {p.label}
          </button>
        ))}
        {busy && <Loader2 className="h-3 w-3 animate-spin text-mute" />}
      </div>
      {error && <p className="text-down">{error}</p>}
      {plan && (
        <>
          <table className="w-full font-mono">
            <tbody>
              {plan.rungs.map((r, i) => (
                <tr key={i} className={clsx(r.done && "text-mute line-through")} title={r.label}>
                  <td className="pr-1 text-mute">TP{i + 1}</td>
                  <td className="text-up">{formatPrice(r.price)}</td>
                  <td className="text-right">
                    {r.sell_pct}% ≈ {r.sell_qty} {coin}
                  </td>
                  <td className="text-right">${Math.round(r.usdt).toLocaleString()}</td>
                  <td className={clsx("text-right", (r.pnl_vs_entry_pct ?? 0) >= 0 ? "text-up" : "text-down")}>
                    {r.pnl_vs_entry_pct != null ? `${r.pnl_vs_entry_pct >= 0 ? "+" : ""}${r.pnl_vs_entry_pct}%` : `+${r.gain_from_now_pct}%`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {plan.runner_qty > 0 && (
            <p className="text-mute">
              {plan.runner_qty} {coin} left to run.
            </p>
          )}
          {plan.invalidation ? (
            <p className="text-down">
              Wrong on a daily close below {formatPrice(plan.invalidation.price)} ({plan.invalidation.label}).
            </p>
          ) : null}
          {plan.notes.map((n) => (
            <p key={n} className="text-mute">
              {n}
            </p>
          ))}
          <div className="mt-1 flex flex-wrap gap-1">
            <button
              type="button"
              disabled={busy}
              className="btn-ghost h-6 gap-1 border border-line px-1.5 text-[11px]"
              title="One price alert per level and one at the invalidation (arming again replaces them)"
              onClick={() => void act(async () => setPlan(await armExitPlan(symbol)))}
            >
              <BellRing className="h-3 w-3" /> {plan.armed.length ? `Armed (${plan.armed.length}) · re-arm` : "Arm alerts"}
            </button>
            {onChart && (
              <button
                type="button"
                className="btn-ghost h-6 gap-1 border border-line px-1.5 text-[11px]"
                onClick={() => {
                  const next = !shown;
                  setShown(next);
                  onChart(next ? plan : null);
                }}
              >
                <LineChart className="h-3 w-3" /> {shown ? "Hide from chart" : "Show on chart"}
              </button>
            )}
            <button
              type="button"
              disabled={busy}
              className="btn-ghost h-6 gap-1 border border-line px-1.5 text-[11px]"
              title="Delete the plan and its alerts"
              onClick={() =>
                void act(async () => {
                  await deleteExitPlan(symbol);
                  onChart?.(null);
                  setPlan(null);
                })
              }
            >
              <Trash2 className="h-3 w-3" />
            </button>
          </div>
        </>
      )}
    </div>
  );
}


