"use client";

import clsx from "clsx";
import { useEffect, useState } from "react";

import { apiRequest } from "@/lib/api";

interface SessionRow {
  key: string;
  name: string;
  open: boolean;
  /** UNIX seconds */
  opens_at: number;
  closes_at: number;
}

const SHORT: Record<string, string> = { asia: "Asia", london: "London", ny: "NY" };

function dur(seconds: number): string {
  const m = Math.max(0, Math.round(seconds / 60));
  const h = Math.floor(m / 60);
  return h ? (m % 60 ? `${h}h ${m % 60}m` : `${h}h`) : `${m}m`;
}

/**
 * Asia, London and New York: which are open and when the next one opens, counting down. The times come from the
 * backend (backend/app/session_levels.py: weekday sessions, daylight saving handled), refreshed when one passes.
 */
export default function SessionClock() {
  const [rows, setRows] = useState<SessionRow[] | null>(null);
  const [now, setNow] = useState(() => Date.now() / 1000);

  useEffect(() => {
    const ctrl = new AbortController();
    let stale = 0;
    const load = () =>
      apiRequest<{ sessions: SessionRow[] }>("/api/sessions/clock", { signal: ctrl.signal })
        .then((r) => {
          setRows(r.sessions);
          stale = Math.min(...r.sessions.map((s) => (s.open ? s.closes_at : s.opens_at)));
        })
        .catch(() => undefined);
    void load();
    const timer = setInterval(() => {
      const t = Date.now() / 1000;
      setNow(t);
      if (stale && t >= stale) {
        stale = 0;
        void load();
      }
    }, 15_000);
    return () => {
      ctrl.abort();
      clearInterval(timer);
    };
  }, []);

  if (!rows?.length) return null;
  const live = rows.map((s) => ({ ...s, open: s.opens_at <= now && now < s.closes_at }));
  const next = live.filter((s) => !s.open).sort((a, b) => a.opens_at - b.opens_at)[0];
  const title = live
    .map((s) => `${s.name}: ${s.open ? `open, closes in ${dur(s.closes_at - now)}` : `opens in ${dur(s.opens_at - now)}`}`)
    .join("\n");
  return (
    <div className="flex shrink-0 items-center gap-2 whitespace-nowrap" title={title}>
      {live.map((s) => (
        <span key={s.key} className={clsx("inline-flex items-center gap-1", s.open ? "text-ink" : "text-mute")}>
          <span className={clsx("h-1.5 w-1.5 rounded-full", s.open ? "bg-up" : "bg-mute/40")} />
          {SHORT[s.key] ?? s.name}
        </span>
      ))}
      {next && (
        <span className={clsx(next.opens_at - now <= 3600 ? "text-yellow-300" : "text-mute")}>
          · {SHORT[next.key] ?? next.name} opens in {dur(next.opens_at - now)}
        </span>
      )}
    </div>
  );
}
