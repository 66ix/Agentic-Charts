// Economic calendar and crypto news (backend/app/events.py), and the event lines drawn on the chart.

import { apiRequest } from "./api";
import type { EventLine } from "./chart/primitives/EventLinesPrimitive";

/** "live", "stale" = the last good copy (the feed just failed), "unavailable" = nothing could be fetched. */
export type FeedSource = "live" | "stale" | "unavailable";
export type Impact = "High" | "Medium" | "Low" | "Holiday";
export type ImpactFilter = "high" | "medium" | "all";

export interface EconomicEvent {
  time: number; // UNIX seconds
  title: string;
  country: string;
  impact: Impact | string;
  forecast: string | null;
  previous: string | null;
}

export interface CalendarResponse {
  events: EconomicEvent[];
  source: FeedSource;
  updated_at: number | null;
  countries: string[];
  note?: string;
}

export interface NewsItem {
  time: number;
  title: string;
  url: string;
  /** Publisher, e.g. "CoinDesk". */
  source: string;
  coins: string[];
  summary: string;
}

export interface NewsResponse {
  items: NewsItem[];
  source: FeedSource;
  updated_at: number | null;
  feeds: { name: string; ok: boolean; items: number }[];
  note?: string;
}

/** The persisted "Show on chart" toggle shared by the events panel and useChartEvents. */
export const EVENTS_ON_CHART_KEY = "ac:events-on-chart";

export const IMPACT_COLORS: Record<string, string> = {
  High: "#ef4444",
  Medium: "#f59e0b",
  Low: "#eab308",
  Holiday: "#6b7280",
};
export const NEWS_COLOR = "#60a5fa";

/** Events from `pastDays` ago to `days` ahead. impact: high, medium (= medium and high) or all. */
export function fetchCalendar(
  opts: { days?: number; impact?: ImpactFilter; pastDays?: number } = {},
  signal?: AbortSignal,
) {
  const q = new URLSearchParams({
    days: String(opts.days ?? 7),
    impact: opts.impact ?? "high",
    past_days: String(opts.pastDays ?? 0),
  });
  return apiRequest<CalendarResponse>(`/api/calendar?${q}`, { signal, timeoutMs: 30_000 });
}

/** Newest headlines first; with `symbol` (e.g. BTCUSDT) only those about that coin. */
export function fetchNews(symbol: string | null, limit = 30, signal?: AbortSignal) {
  const q = new URLSearchParams({ limit: String(limit) });
  if (symbol) q.set("symbol", symbol);
  return apiRequest<NewsResponse>(`/api/news?${q}`, { signal, timeoutMs: 30_000 });
}

/** Only web links are ever rendered as links. */
export function safeUrl(url: string): string | null {
  return /^https?:\/\//i.test(url) ? url : null;
}

const short = (s: string, n: number) => (s.length > n ? `${s.slice(0, n - 1).trimEnd()}…` : s);

export function eventLines(events: EconomicEvent[]): EventLine[] {
  return events.map((e) => ({
    time: e.time,
    kind: "event",
    label: short(e.title, 18),
    color: IMPACT_COLORS[e.impact] ?? IMPACT_COLORS.Low,
    title:
      `${e.country} · ${e.title} · ${e.impact} impact` +
      (e.forecast || e.previous ? ` · forecast ${e.forecast ?? "–"}, previous ${e.previous ?? "–"}` : ""),
  }));
}

export function newsLines(items: NewsItem[]): EventLine[] {
  return items.map((n) => ({ time: n.time, kind: "news", label: n.source, color: NEWS_COLOR, title: n.title }));
}
