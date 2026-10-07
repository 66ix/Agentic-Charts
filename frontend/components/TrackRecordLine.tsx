"use client";

import clsx from "clsx";
import { ChevronDown, FlaskConical, History } from "lucide-react";
import { useState } from "react";

import { openTrackRecordInBacktest } from "@/lib/backtest";
import { trackTone } from "@/lib/scanner";
import type { TrackRecord } from "@/lib/types";

function fmtR(r: number | null | undefined, digits = 2): string {
  if (r == null || !Number.isFinite(r)) return "–";
  return `${r >= 0 ? "+" : "−"}${Math.abs(r).toFixed(digits)}R`;
}

/**
 * A plan's track record as one compact line ("fresh 4h demand longs on INJ: 14 trades, 57% win, +0.60R avg, last
 * 1 year"); click it for the numbers and caveats, or open the same backtest, with its trades, in the Backtest tab.
 */
export default function TrackRecordLine({ tr }: { tr: TrackRecord }) {
  const [open, setOpen] = useState(false);
  const usable = tr.status === "ok" || tr.status === "small_sample";
  const demo = tr.data_source === "synthetic";
  const text = tr.summary.replace(/ \(demo data\)$/, "");
  const canOpen = !!tr.setup && tr.status !== "no_match";
  return (
    <div className="mt-1.5 border-t border-line pt-1.5">
      <div className="flex items-start gap-1">
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          title={[text, ...tr.notes].join("\n\n")}
          aria-expanded={open}
          className="flex min-w-0 flex-1 items-start gap-1 text-left"
        >
          <History className="mt-0.5 h-3 w-3 shrink-0 text-mute" />
          <span className="min-w-0 flex-1">
            <span className="text-mute">Track record: </span>
            <span className={trackTone(tr)}>{text}</span>
            {demo && (
              <span className="ml-1 rounded border border-yellow-400/40 px-1 text-[9px] font-semibold text-yellow-300">DEMO</span>
            )}
          </span>
          <ChevronDown className={clsx("mt-0.5 h-3 w-3 shrink-0 text-mute transition-transform", open && "rotate-180")} />
        </button>
        {canOpen && (
          <button
            type="button"
            onClick={() => openTrackRecordInBacktest(tr)}
            className="btn-ghost h-5 w-5 shrink-0 p-0"
            title="Open this backtest in the Backtest tab"
            aria-label="Open in Backtest"
          >
            <FlaskConical className="h-3 w-3" />
          </button>
        )}
      </div>
      {open && (
        <div className="mt-1 space-y-1 pl-4">
          {usable && (
            <div className="grid grid-cols-3 gap-x-2 gap-y-0.5 font-mono">
              <span className="text-mute">Trades <span className="text-ink">{tr.trades}</span></span>
              <span className="text-mute">Win <span className="text-ink">{Math.round((tr.win_rate ?? 0) * 100)}%</span></span>
              <span className="text-mute">Avg <span className={trackTone(tr)}>{fmtR(tr.avg_r)}</span></span>
              <span className="text-mute">Total <span className="text-ink">{fmtR(tr.total_r, 1)}</span></span>
              <span className="text-mute">PF <span className="text-ink">{tr.profit_factor == null ? "–" : tr.profit_factor.toFixed(2)}</span></span>
              <span className="text-mute">DD <span className="text-ink">{tr.max_drawdown_r == null ? "–" : `${tr.max_drawdown_r.toFixed(1)}R`}</span></span>
            </div>
          )}
          {tr.notes.map((n) => (
            <p key={n} className={/^(Computed on synthetic|This .* was already tested)/.test(n) ? "text-yellow-300/90" : "text-mute"}>{n}</p>
          ))}
          {canOpen && (
            <button
              type="button"
              onClick={() => openTrackRecordInBacktest(tr)}
              className="btn-ghost h-6 border border-line px-1.5 text-[11px]"
            >
              <FlaskConical className="h-3.5 w-3.5" /> Open in Backtest
            </button>
          )}
        </div>
      )}
    </div>
  );
}
