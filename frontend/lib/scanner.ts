// Market-wide setup scanner: the API calls for backend/app/market_scanner.py and small formatters shared by the
// Scanner tab and the agent's answers.

import { apiRequest } from "./api";
import type { AlertChannels, Interval, MarketScanResult, MarketSetup, TrackRecord } from "./types";

export interface MarketScanStatus {
  interval: Interval;
  /** The last scan of this timeframe, or null when it never ran. */
  result: MarketScanResult | null;
  /** Timeframes being scanned right now. */
  running: Interval[];
  /** Timed scans: timeframe → minutes (MARKET_SCAN_SCHEDULE). */
  schedule: Partial<Record<Interval, number>>;
  /** Timeframe → ms of the next timed scan. */
  next_run: Partial<Record<Interval, number>>;
  /** Coins scanned (the top N by 24h volume). */
  top: number;
  /** Best setups sent to Telegram / Discord after each timed scan (0 = off). */
  notify_top: number;
  channels: Partial<AlertChannels>;
}

export function fetchMarketScan(interval: Interval, signal?: AbortSignal) {
  return apiRequest<MarketScanStatus>(`/api/market-scan?${new URLSearchParams({ interval })}`, { signal });
}

/** Scans now, or joins the scan of this timeframe already running. The first scan downloads a lot of candles. */
export function runMarketScan(interval: Interval, signal?: AbortSignal) {
  return apiRequest<MarketScanStatus>(`/api/market-scan/run?${new URLSearchParams({ interval })}`, {
    method: "POST",
    signal,
    timeoutMs: 300_000,
  });
}

/** Win rate and average R when the record has enough trades, else what is missing. */
export function trackShort(tr: TrackRecord | null | undefined): string {
  if (!tr) return "–";
  if (tr.status === "ok" || tr.status === "small_sample") {
    const r = tr.avg_r ?? 0;
    return `${Math.round((tr.win_rate ?? 0) * 100)}% · ${r >= 0 ? "+" : "−"}${Math.abs(r).toFixed(2)}R · ${tr.trades}${tr.status === "small_sample" ? "?" : ""}`;
  }
  return { too_few_trades: "few trades", short_history: "new coin", no_match: "no match", unavailable: "n/a" }[tr.status];
}

export function trackTone(tr: TrackRecord | null | undefined): string {
  if (!tr || (tr.status !== "ok" && tr.status !== "small_sample") || tr.avg_r == null) return "text-mute";
  return tr.avg_r > 0 ? "text-up" : tr.avg_r < 0 ? "text-down" : "text-ink";
}

/** "2/3": timeframes trending the setup's way (a range counts half). */
export function agreementText(s: MarketSetup): string {
  return s.agreement.total ? `${s.agreement.aligned}/${s.agreement.total}` : "–";
}

export function agreementTitle(s: MarketSetup): string {
  const frames = Object.entries(s.agreement.frames).map(([tf, t]) => `${tf} ${t}`);
  return `Trend per timeframe: ${frames.join(", ") || "n/a"}`;
}
