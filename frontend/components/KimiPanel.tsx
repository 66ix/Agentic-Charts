"use client";

import clsx from "clsx";
import { ChevronDown, ChevronRight } from "lucide-react";

import { usePersistentState } from "@/hooks/usePersistentState";
import { formatPrice } from "@/lib/format";
import type { KimiResult, KimiRow } from "@/lib/types";

const TONE = { up: "text-up", down: "text-down", mute: "text-mute" } as const;
const RESULT = { win: "text-up", loss: "text-down", expiry: "text-mute", open: "text-ink" } as const;

function Rows({ title, rows }: { title: string; rows: KimiRow[] }) {
  return (
    <div>
      <div className="mb-0.5 text-[10px] font-semibold uppercase tracking-wide text-mute">{title}</div>
      <table className="w-full">
        <tbody>
          {rows.map((r) => (
            <tr key={r.label} className="align-top">
              <td className="whitespace-nowrap pr-3 text-mute">{r.label}</td>
              <td className={clsx("whitespace-nowrap text-right", r.tone ? TONE[r.tone] : "text-ink")}>{r.value}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * Kimi Cooked's two tables (PATH VERIFY and Signal Stats) and its latest signals, folded into a pill under the
 * chart legend so they don't cover the candles.
 */
export default function KimiPanel({ data, loading, error }: { data: KimiResult | null; loading: boolean; error: string | null }) {
  const [open, setOpen] = usePersistentState("ac:kimi-panel-open", false);
  const status = error ? "unavailable" : loading && !data ? "computing…" : null;
  const recent = data ? data.signals.slice(-6).reverse() : [];
  // Chart patterns and harmonics on the chart, newest first, with what they are waiting for.
  const shapes = data
    ? [
        ...(data.patterns ?? []).map((p) => ({
          name: p.name,
          direction: p.direction,
          time: p.time,
          detail:
            p.state === "watching" && p.breakout_level !== null
              ? `breaks ${formatPrice(p.breakout_level)}`
              : p.state === "breakout" && p.target !== null
                ? `broke out → ${formatPrice(p.target)}`
                : p.state,
        })),
        ...(data.harmonics ?? []).map((h) => ({
          name: h.name,
          direction: h.direction,
          time: h.time,
          detail: h.state === "active" || h.state === "compromised" ? `${h.state} · TP1 ${formatPrice(h.tp1)}` : h.state,
        })),
      ].sort((a, b) => b.time - a.time)
    : [];

  return (
    <div className="absolute left-3 top-7 z-20 max-w-[calc(100%-5rem)] font-mono text-[10px] leading-4">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        title={error ?? "Kimi Cooked tables"}
        className="flex items-center gap-1 rounded border border-line bg-panel/90 px-1.5 py-0.5 text-mute hover:text-ink"
      >
        {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
        <span className="text-ink">Kimi Cooked{data ? ` v${data.version}` : ""}</span>
        {status && <span className={error ? "text-down" : ""}>{status}</span>}
        {!status && data?.forecast && <span>{data.forecast.headline}</span>}
        {loading && data && <span>· updating</span>}
      </button>
      {open && data && (
        <div className="mt-1 max-h-[60vh] w-[26rem] max-w-full space-y-2 overflow-y-auto rounded border border-line bg-panel/95 p-2 shadow-lg">
          <Rows title="Path verify" rows={data.verify} />
          <Rows title="Signal stats · win% (W/N), edge vs random" rows={data.stats} />
          {recent.length > 0 && (
            <div>
              <div className="mb-0.5 text-[10px] font-semibold uppercase tracking-wide text-mute">Latest signals</div>
              <table className="w-full">
                <tbody>
                  {recent.map((s) => (
                    <tr key={`${s.text}-${s.time}`}>
                      <td className={clsx("pr-2", s.direction === "long" ? "text-up" : "text-down")}>{s.text}</td>
                      <td className="pr-2 text-mute">{new Date(s.confirm_time * 1000).toISOString().slice(5, 16).replace("T", " ")}</td>
                      <td className="pr-2 text-right text-ink">{formatPrice(s.entry)}</td>
                      <td className={clsx("text-right", RESULT[s.result])}>
                        {s.result}
                        {s.r !== null ? ` ${s.r >= 0 ? "+" : ""}${s.r.toFixed(2)}R` : ""}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {shapes.length > 0 && (
            <div>
              <div className="mb-0.5 text-[10px] font-semibold uppercase tracking-wide text-mute">Patterns</div>
              <table className="w-full">
                <tbody>
                  {shapes.map((p) => (
                    <tr key={`${p.name}-${p.time}`}>
                      <td className={clsx("pr-2", p.direction === "bullish" ? "text-up" : "text-down")}>{p.name}</td>
                      <td className="pr-2 text-mute">{new Date(p.time * 1000).toISOString().slice(5, 16).replace("T", " ")}</td>
                      <td className="text-right text-ink">{p.detail}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div className="space-y-1 border-t border-line pt-1.5 font-sans text-[10px] text-mute">
            <p>
              {data.bars.toLocaleString()} closed candles{data.data_source !== "binance" ? ` (${data.data_source} data)` : ""}.
              Signals are decided on the close; DIV and U/Dn labels sit on their pivot.
            </p>
            {data.notes.map((n) => (
              <p key={n}>{n}</p>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
