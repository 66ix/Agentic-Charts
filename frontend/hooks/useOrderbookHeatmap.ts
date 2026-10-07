"use client";

import { useEffect, useState } from "react";

import { fetchHeatmap, heatmapStep, mergeHeatmap, type HeatmapData } from "@/lib/heatmap";
import { INTERVAL_SECONDS, type Interval } from "@/lib/types";

const POLL_MS = 10_000; // the backend samples every 10 s and stops for a symbol nobody has polled for 45 s

/**
 * The order-book heatmap of `symbol` while `on`: the first poll gets the history the backend holds, later ones
 * only the columns from the newest one on. Polling pauses while the tab is hidden, so the backend stops sampling
 * a symbol nobody is looking at.
 */
export function useOrderbookHeatmap(symbol: string, interval: Interval, on: boolean) {
  const [state, setState] = useState<{ data: HeatmapData | null; error: string | null }>({ data: null, error: null });

  useEffect(() => {
    setState({ data: null, error: null });
    if (!on) return;
    const ctrl = new AbortController();
    const step = heatmapStep(INTERVAL_SECONDS[interval]);
    let data: HeatmapData | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      if (!document.hidden) {
        try {
          const last = data?.columns[data.columns.length - 1];
          const res = await fetchHeatmap(symbol, step, last ? last[0] : null, ctrl.signal);
          data = mergeHeatmap(data, res);
          setState({ data, error: res.source === "unavailable" ? (res.note ?? "unavailable") : null });
        } catch (err) {
          if (ctrl.signal.aborted || (err as Error).name === "AbortError") return;
          setState((s) => ({ ...s, error: (err as Error).message || "unavailable" }));
        }
      }
      if (!ctrl.signal.aborted) timer = setTimeout(tick, POLL_MS);
    };
    tick();
    return () => {
      ctrl.abort();
      clearTimeout(timer);
    };
  }, [on, symbol, interval]);

  return state;
}
