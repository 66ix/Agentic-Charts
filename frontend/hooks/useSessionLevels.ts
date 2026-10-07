"use client";

import { usePolled } from "@/hooks/usePolled";
import { fetchSessionLevels } from "@/lib/sessionLevels";
import type { Interval } from "@/lib/types";

const REFRESH_MS = 30_000; // between polls the chart moves a running session's high and low with the newest candle

/** Session and period levels for a chart while `on`, refreshed every 30 seconds (null data while off). */
export function useSessionLevels(symbol: string, interval: Interval, on: boolean, orMinutes: number) {
  return usePolled(
    on ? `${symbol}:${interval}:${orMinutes}` : null,
    (signal) => fetchSessionLevels(symbol, interval, orMinutes, signal),
    REFRESH_MS,
  );
}
