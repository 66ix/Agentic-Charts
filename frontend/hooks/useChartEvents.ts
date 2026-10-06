"use client";

import { useMemo } from "react";

import { usePersistentState } from "@/hooks/usePersistentState";
import { usePolled } from "@/hooks/usePolled";
import type { EventLine } from "@/lib/chart/primitives/EventLinesPrimitive";
import { EVENTS_ON_CHART_KEY, eventLines, fetchCalendar, fetchNews, newsLines } from "@/lib/events";

const EMPTY: EventLine[] = [];
const REFRESH_MS = 10 * 60_000;

/**
 * Lines for EventLinesPrimitive while the "Show on chart" toggle (EVENTS_ON_CHART_KEY) is on: high-impact economic
 * events from the last 7 days to the next 7, and the news about `symbol`'s coin. Refreshed every 10 minutes.
 */
export function useChartEvents(symbol: string): EventLine[] {
  const [on] = usePersistentState<boolean>(EVENTS_ON_CHART_KEY, false);
  const calendar = usePolled(
    on ? "calendar" : null,
    (signal) => fetchCalendar({ days: 7, pastDays: 7, impact: "high" }, signal),
    REFRESH_MS,
  );
  const news = usePolled(on ? `news:${symbol}` : null, (signal) => fetchNews(symbol, 40, signal), REFRESH_MS);
  return useMemo(
    () => (on ? [...eventLines(calendar.data?.events ?? []), ...newsLines(news.data?.items ?? [])] : EMPTY),
    [on, calendar.data, news.data],
  );
}
