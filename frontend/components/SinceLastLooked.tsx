"use client";

import { X } from "lucide-react";
import { useEffect, useState } from "react";

import { apiRequest } from "@/lib/api";
import { displaySymbol } from "@/lib/format";

/** Away from a chart at least this long before it says what changed. */
const MIN_AWAY_S = 2 * 3600;
/** While a chart is open, it is marked as seen this often. */
const SEEN_EVERY_MS = 120_000;

interface Changes {
  symbol: string;
  interval: string;
  away: string;
  lines: string[];
  quiet?: boolean;
  note?: string;
}

const seenKey = (symbol: string, interval: string) => `ac:seen:${symbol}:${interval}`;

function readSeen(key: string): number | null {
  try {
    const v = Number(window.localStorage.getItem(key));
    return Number.isFinite(v) && v > 0 ? v : null;
  } catch {
    return null;
  }
}

function writeSeen(key: string) {
  try {
    window.localStorage.setItem(key, String(Math.floor(Date.now() / 1000)));
  } catch {
    // storage blocked: the card just won't show next time
  }
}

/**
 * "Since you last looked": back on a coin and timeframe after MIN_AWAY_S or more, what changed there (price, the
 * agent's zones, structure, Kimi's signals, the desk's calls, alerts that fired; backend/app/changes.py).
 */
export default function SinceLastLooked({ symbol, interval }: { symbol: string; interval: string }) {
  const [data, setData] = useState<Changes | null>(null);

  useEffect(() => {
    const key = seenKey(symbol, interval);
    const ctrl = new AbortController();
    // What changed since `prev`, if that was long enough ago; then this chart is seen as of now.
    const check = () => {
      const prev = readSeen(key);
      writeSeen(key);
      const now = Math.floor(Date.now() / 1000);
      if (!prev || now - prev < MIN_AWAY_S) return;
      apiRequest<Changes>("/api/changes", { method: "POST", body: JSON.stringify({ symbol, interval, since: prev }), signal: ctrl.signal, timeoutMs: 45_000 })
        .then((c) => {
          if (!c.quiet && c.lines.length) setData(c);
        })
        .catch(() => undefined);
    };
    setData(null);
    if (!document.hidden) check(); // opened in a background tab: it isn't seen until you look at it
    // Seen only while the tab is visible, so a tab left in the background shows the card when you come back to it.
    const id = setInterval(() => !document.hidden && writeSeen(key), SEEN_EVERY_MS);
    const onVisibility = () => {
      if (document.hidden) writeSeen(key);
      else check();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      ctrl.abort();
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVisibility);
      if (!document.hidden) writeSeen(key);
    };
  }, [symbol, interval]);

  if (!data) return null;
  return (
    <div className="absolute right-3 top-2 z-20 w-80 max-w-[calc(100%-1.5rem)] rounded-md border border-line bg-panel/95 p-2.5 text-[11px] leading-snug shadow-lg">
      <div className="mb-1 flex items-center gap-2">
        <span className="font-medium text-ink">
          Since you last looked at {displaySymbol(data.symbol)} {data.interval.toUpperCase()}
        </span>
        <span className="text-mute">({data.away} ago)</span>
        <span className="flex-1" />
        <button type="button" className="btn-ghost h-5 w-5 p-0" aria-label="Dismiss" onClick={() => setData(null)}>
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
      <ul className="space-y-0.5 text-mute">
        {data.lines.map((l) => (
          <li key={l} className="text-ink/90">
            {l}
          </li>
        ))}
      </ul>
    </div>
  );
}
