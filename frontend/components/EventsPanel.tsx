"use client";

import clsx from "clsx";
import { ExternalLink, Eye, EyeOff } from "lucide-react";
import { useEffect, useState } from "react";

import { usePersistentState } from "@/hooks/usePersistentState";
import { usePolled } from "@/hooks/usePolled";
import type { DockPanelProps } from "@/lib/dock";
import {
  EVENTS_ON_CHART_KEY,
  IMPACT_COLORS,
  fetchCalendar,
  fetchNews,
  safeUrl,
  type EconomicEvent,
  type ImpactFilter,
} from "@/lib/events";
import { displaySymbol, splitSymbol } from "@/lib/format";

const IMPACT_OPTIONS: { value: ImpactFilter; label: string }[] = [
  { value: "high", label: "High" },
  { value: "medium", label: "Medium+" },
  { value: "all", label: "All" },
];

function useNow(ms: number) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}

function countdown(seconds: number): string {
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
  return `${m}m ${String(s).padStart(2, "0")}s`;
}

function ago(seconds: number): string {
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

const dayKey = (t: number) => new Date(t * 1000).toDateString();

function dayLabel(t: number, now: number): string {
  const d = new Date(t * 1000);
  const today = new Date(now);
  const tomorrow = new Date(now + 86_400_000);
  if (d.toDateString() === today.toDateString()) return "Today";
  if (d.toDateString() === tomorrow.toDateString()) return "Tomorrow";
  return d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
}

function Toggle<T extends string>({ value, options, onChange }: { value: T; options: { value: T; label: string }[]; onChange(v: T): void }) {
  return (
    <div className="flex overflow-hidden rounded border border-line text-[10px]">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          onClick={() => onChange(o.value)}
          className={clsx("px-1.5 py-0.5", value === o.value ? "bg-panel2 text-ink" : "text-mute hover:text-ink")}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

function EventRow({ e, past }: { e: EconomicEvent; past: boolean }) {
  const time = new Date(e.time * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return (
    <div className={clsx("flex items-start gap-2 py-1 text-[11px]", past && "opacity-50")}>
      <span className="w-10 shrink-0 font-mono text-mute">{time}</span>
      <span className="mt-1 h-2 w-2 shrink-0 rounded-full" style={{ background: IMPACT_COLORS[e.impact] ?? IMPACT_COLORS.Low }} title={`${e.impact} impact`} />
      <div className="min-w-0 flex-1">
        <div className="text-ink">
          <span className="text-mute">{e.country}</span> {e.title}
        </div>
        {(e.forecast || e.previous) && (
          <div className="font-mono text-[10px] text-mute">
            {e.forecast && `Forecast ${e.forecast}`}
            {e.forecast && e.previous && " · "}
            {e.previous && `Previous ${e.previous}`}
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * Economic calendar (grouped by day, impact colours, countdown to the next high-impact release, a "Show on chart"
 * toggle shared with useChartEvents) and crypto news for the active coin or all coins.
 */
export default function EventsPanel({ symbol }: DockPanelProps) {
  const [impact, setImpact] = usePersistentState<ImpactFilter>("ac:events-impact", "medium");
  const [onChart, setOnChart] = usePersistentState<boolean>(EVENTS_ON_CHART_KEY, false);
  const [scope, setScope] = usePersistentState<"coin" | "all">("ac:news-scope", "coin");
  const now = useNow(1000);
  const [base] = splitSymbol(symbol);

  const calendar = usePolled(`calendar:${impact}`, (s) => fetchCalendar({ days: 7, pastDays: 1, impact }, s), 15 * 60_000);
  const newsSymbol = scope === "coin" ? symbol : null;
  const news = usePolled(`news:${newsSymbol ?? "all"}`, (s) => fetchNews(newsSymbol, 40, s), 5 * 60_000);

  const cal = calendar.data;
  const nowS = now / 1000;
  const startOfToday = new Date(now).setHours(0, 0, 0, 0) / 1000;
  const events = (cal?.events ?? []).filter((e) => e.time >= startOfToday);
  const nextHigh = events.find((e) => e.impact === "High" && e.time >= nowS);
  const days: { key: string; label: string; events: EconomicEvent[] }[] = [];
  for (const e of events) {
    const k = dayKey(e.time);
    if (days.at(-1)?.key !== k) days.push({ key: k, label: dayLabel(e.time, now), events: [] });
    days[days.length - 1].events.push(e);
  }
  const items = news.data?.items ?? [];

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-2 border-b border-line px-3 py-2 text-xs">
        <span className="font-semibold text-ink">Economic calendar</span>
        <div className="flex-1" />
        <button
          type="button"
          onClick={() => setOnChart(!onChart)}
          className={clsx("btn-ghost h-6 px-1.5 text-[11px]", onChart && "text-accent")}
          title="High-impact events and this coin's news as lines on the chart"
        >
          {onChart ? <Eye className="h-3.5 w-3.5" /> : <EyeOff className="h-3.5 w-3.5" />}
          Show on chart
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        <section className="border-b border-line px-3 py-2">
          <div className="mb-1.5 flex items-center gap-2">
            <Toggle<ImpactFilter> value={impact} options={IMPACT_OPTIONS} onChange={setImpact} />
            <div className="flex-1" />
            {cal?.countries && <span className="text-[10px] text-mute">{cal.countries.join(", ")}</span>}
          </div>
          {nextHigh && (
            <div className="mb-2 rounded border border-down/40 bg-down/10 px-2 py-1.5 text-[11px]">
              <span className="text-mute">Next high impact: </span>
              <span className="text-ink">{nextHigh.title}</span>
              <span className="font-mono text-down"> in {countdown(nextHigh.time - nowS)}</span>
            </div>
          )}
          {cal?.note && <p className="mb-1 text-[11px] leading-snug text-mute">{cal.note}</p>}
          {calendar.error && !cal && <p className="text-[11px] text-down">{calendar.error}</p>}
          {!cal && !calendar.error && <p className="text-[11px] text-mute">Loading…</p>}
          {cal && cal.source !== "unavailable" && days.length === 0 && (
            <p className="text-[11px] text-mute">No events at this impact level in the next 7 days.</p>
          )}
          {days.map((d) => (
            <div key={d.key} className="mt-1">
              <div className="text-[10px] font-semibold uppercase tracking-wide text-mute">{d.label}</div>
              {d.events.map((e) => (
                <EventRow key={`${e.time}-${e.title}-${e.country}`} e={e} past={e.time < nowS} />
              ))}
            </div>
          ))}
          {cal?.source !== "unavailable" && <p className="mt-1 text-[10px] text-mute">Source: Forex Factory. Times are local.</p>}
        </section>

        <section className="px-3 py-2">
          <div className="mb-1.5 flex items-center gap-2">
            <h3 className="text-[10px] font-semibold uppercase tracking-wide text-mute">News</h3>
            <div className="flex-1" />
            <Toggle<"coin" | "all">
              value={scope}
              options={[
                { value: "coin", label: base || displaySymbol(symbol) },
                { value: "all", label: "All" },
              ]}
              onChange={setScope}
            />
          </div>
          {news.data?.note && <p className="mb-1 text-[11px] leading-snug text-mute">{news.data.note}</p>}
          {news.error && !news.data && <p className="text-[11px] text-down">{news.error}</p>}
          {!news.data && !news.error && <p className="text-[11px] text-mute">Loading…</p>}
          {news.data && news.data.source !== "unavailable" && items.length === 0 && (
            <p className="text-[11px] text-mute">No recent headlines about {base || symbol}.</p>
          )}
          {items.map((n) => {
            const href = safeUrl(n.url);
            return (
              <div key={n.url} className="border-b border-line/60 py-1.5 text-[11px] last:border-0">
                {href ? (
                  <a href={href} target="_blank" rel="noopener noreferrer" className="group text-ink hover:text-accent">
                    {n.title}
                    <ExternalLink className="ml-1 inline h-3 w-3 text-mute group-hover:text-accent" />
                  </a>
                ) : (
                  <span className="text-ink">{n.title}</span>
                )}
                <div className="mt-0.5 flex flex-wrap items-center gap-1 text-[10px] text-mute">
                  <span>{n.source}</span>
                  <span>· {ago(nowS - n.time)}</span>
                  {n.coins.slice(0, 4).map((c) => (
                    <span key={c} className="rounded bg-panel2 px-1 text-ink">
                      {c}
                    </span>
                  ))}
                </div>
              </div>
            );
          })}
          {news.data?.feeds && news.data.feeds.some((f) => !f.ok) && news.data.source !== "unavailable" && (
            <p className="mt-1 text-[10px] text-mute">
              Not reachable now: {news.data.feeds.filter((f) => !f.ok).map((f) => f.name).join(", ")}
            </p>
          )}
        </section>
      </div>
    </div>
  );
}
