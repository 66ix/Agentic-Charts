"use client";

import clsx from "clsx";
import { Gauge, Plus, Trash2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { createMetricAlert, deleteMetricAlert, fetchMetricAlerts } from "@/lib/api";
import { formatCompact } from "@/lib/format";
import type { MetricAlert, MetricAlertSpec, MetricKey } from "@/lib/types";

const INPUT =
  "h-7 min-w-0 rounded border border-line bg-panel2 px-2 text-[12px] text-ink outline-none focus:border-accent";

const NAMES: Record<MetricKey, string> = {
  fear_greed: "Fear & Greed",
  btc_dominance: "BTC dominance",
  market_cap: "Market cap",
  volume_24h: "24h volume",
  open_interest: "Open interest",
  liquidations: "Liquidations",
};
const POINTS: MetricKey[] = ["fear_greed", "btc_dominance"];

function fmt(metric: MetricKey, v: number): string {
  if (metric === "fear_greed") return v.toFixed(0);
  if (metric === "btc_dominance") return `${v.toFixed(2)}%`;
  return `$${formatCompact(v)}`;
}

export function describeMetricAlert(a: MetricAlertSpec): string {
  if (a.condition === "moves") {
    const unit = POINTS.includes(a.metric) ? ` point${a.value === 1 ? "" : "s"}` : "%";
    return `${NAMES[a.metric]} moves ${a.value}${unit}`;
  }
  return `${NAMES[a.metric]} ${a.condition} ${fmt(a.metric, a.value)}`;
}

/** "1.5T", "300b", "25" → a number; the dollar metrics take t/b/m/k suffixes. */
function parseValue(s: string): number {
  const m = s.trim().toLowerCase().replace(/[$,%\s]/g, "").match(/^(\d+(?:\.\d+)?)([tbmk])?$/);
  if (!m) return NaN;
  const scale = { t: 1e12, b: 1e9, m: 1e6, k: 1e3 }[m[2] as "t" | "b" | "m" | "k"] ?? 1;
  return parseFloat(m[1]) * scale;
}

/** Alerts on the market header bar, checked on the server every minute and fired once. */
export default function MarketAlerts() {
  const [alerts, setAlerts] = useState<MetricAlert[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [metric, setMetric] = useState<MetricKey>("fear_greed");
  const [condition, setCondition] = useState<MetricAlertSpec["condition"]>("below");
  const [value, setValue] = useState("");

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setAlerts((await fetchMetricAlerts(signal)).alerts);
    } catch {
      if (!signal?.aborted) setAlerts((a) => a ?? []);
    }
  }, []);

  useEffect(() => {
    const ctrl = new AbortController();
    void load(ctrl.signal);
    const id = window.setInterval(() => void load(), 30_000);
    return () => {
      ctrl.abort();
      window.clearInterval(id);
    };
  }, [load]);

  const add = async () => {
    const v = condition === "moves" ? parseFloat(value) : POINTS.includes(metric) ? parseFloat(value) : parseValue(value);
    if (!(v > 0)) {
      setError("Enter a number above 0.");
      return;
    }
    try {
      await createMetricAlert({ metric, condition, value: v });
      setError(null);
      setValue("");
      setAdding(false);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't set the alert.");
    }
  };

  const remove = async (id: string) => {
    setAlerts((a) => a?.filter((x) => x.id !== id) ?? null);
    try {
      await deleteMetricAlert(id);
    } catch {
      void load();
    }
  };

  const unit = condition === "moves" ? (POINTS.includes(metric) ? "points" : "%") : metric === "btc_dominance" ? "%" : metric === "fear_greed" ? "0–100" : "$ (e.g. 3T)";

  return (
    <div className="border-t border-line">
      <div className="flex items-center gap-2 px-3 py-1.5">
        <Gauge className="h-3.5 w-3.5 text-accent" />
        <span className="text-[10px] font-semibold uppercase tracking-wide text-mute">Market alerts</span>
        <div className="flex-1" />
        <button
          type="button"
          onClick={() => setAdding((a) => !a)}
          className={clsx("btn-ghost h-6 px-1.5 text-[11px]", adding && "bg-panel2 text-ink")}
        >
          <Plus className="h-3.5 w-3.5" /> New
        </button>
      </div>
      {adding && (
        <div className="flex flex-wrap items-center gap-1.5 px-3 pb-2">
          <select className={INPUT} value={metric} onChange={(e) => setMetric(e.target.value as MetricKey)}>
            {(Object.keys(NAMES) as MetricKey[]).map((k) => (
              <option key={k} value={k}>
                {NAMES[k]}
              </option>
            ))}
          </select>
          <select
            className={INPUT}
            value={condition}
            onChange={(e) => setCondition(e.target.value as MetricAlertSpec["condition"])}
          >
            <option value="below">below</option>
            <option value="above">above</option>
            <option value="moves">moves by</option>
          </select>
          <input
            className={clsx(INPUT, "w-24")}
            inputMode="decimal"
            placeholder={unit}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void add()}
          />
          <button type="button" onClick={() => void add()} className="btn h-7 px-2 text-[11px]">
            Set
          </button>
        </div>
      )}
      {error && <p className="px-3 pb-1.5 text-[11px] text-down">{error}</p>}
      {alerts && alerts.length === 0 && !adding && (
        <p className="px-3 pb-2 text-[11px] leading-relaxed text-mute">
          Get pinged on the header stats: ask the agent &quot;alert me when Fear &amp; Greed drops below 25&quot; or
          &quot;tell me if BTC dominance moves 1%&quot;.
        </p>
      )}
      {alerts?.map((a) => (
        <div key={a.id} className="group flex items-center gap-2 px-3 py-1 hover:bg-panel2/60">
          <span className={clsx("h-1.5 w-1.5 shrink-0 rounded-full", a.armed ? "bg-accent" : "bg-mute")} />
          <span className={clsx("flex-1 truncate text-[12px]", a.armed ? "text-ink" : "text-mute line-through")}>
            {describeMetricAlert(a)}
          </span>
          {!a.armed && a.triggered_value != null && (
            <span className="text-[10px] text-mute">fired at {fmt(a.metric, a.triggered_value)}</span>
          )}
          {a.armed && a.base_value != null && (
            <span className="text-[10px] text-mute">from {fmt(a.metric, a.base_value)}</span>
          )}
          <button
            type="button"
            onClick={() => void remove(a.id)}
            className="btn-ghost h-5 w-5 p-0 opacity-60 group-hover:opacity-100"
            aria-label="Delete market alert"
          >
            <Trash2 className="h-3 w-3" />
          </button>
        </div>
      ))}
    </div>
  );
}
