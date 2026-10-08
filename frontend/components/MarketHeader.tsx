"use client";

import clsx from "clsx";
import { Activity } from "lucide-react";

import { useMarketMetrics } from "@/hooks/useMarketMetrics";
import { formatPct } from "@/lib/format";
import type { Metric } from "@/lib/types";

import SessionClock from "./SessionClock";

function fngColor(v: number) {
  if (v < 25) return "text-down";
  if (v < 45) return "text-orange-400";
  if (v <= 55) return "text-yellow-300";
  return "text-up";
}

function MetricItem({ m }: { m: Metric }) {
  const isFng = m.key === "fear_greed";
  return (
    <div className="flex shrink-0 items-center gap-1.5 whitespace-nowrap" title={m.note ?? (m.source === "mock" ? "Mocked value" : "Live")}>
      <span className="text-mute">{m.label}:</span>
      {isFng ? (
        <span className={clsx("font-medium", fngColor(m.value))}>{m.display}</span>
      ) : (
        <span className="font-medium text-ink">{m.display}</span>
      )}
      {m.change_pct != null && !isFng && (
        <span className={m.change_pct >= 0 ? "text-up" : "text-down"}>{formatPct(m.change_pct)}</span>
      )}
      {m.source === "mock" && <span className="h-1 w-1 rounded-full bg-mute/60" aria-label="mocked" />}
    </div>
  );
}

export default function MarketHeader() {
  const { data, error } = useMarketMetrics();
  return (
    <header className="flex h-9 items-center gap-6 overflow-x-auto border-b border-line bg-panel px-4 text-xs scrollbar-none">
      <div className="flex shrink-0 items-center gap-2 font-semibold text-ink">
        <Activity className="h-4 w-4 text-accent" />
        Agentic Charts
      </div>
      {data?.metrics.map((m) => <MetricItem key={m.key} m={m} />)}
      {!data && !error && <span className="text-mute">Loading market data…</span>}
      {error && !data && <span className="text-down">{error}</span>}
      <div className="ml-auto flex shrink-0 items-center pl-4">
        <SessionClock />
      </div>
    </header>
  );
}
