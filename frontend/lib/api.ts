import { API_URL } from "./config";
import type {
  AlertChannels,
  AlertSpec,
  AnalysisIntent,
  AnalyzeResponse,
  Candle,
  ChatTurn,
  Interval,
  MarketMetrics,
  Overlay,
  PriceAlert,
  ScanResult,
  Ticker,
} from "./types";

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit & { timeoutMs?: number }): Promise<T> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), init?.timeoutMs ?? 20_000);
  const outer = init?.signal;
  outer?.addEventListener("abort", () => ctrl.abort(), { once: true });
  try {
    const res = await fetch(`${API_URL}${path}`, {
      ...init,
      signal: ctrl.signal,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    });
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const body = await res.json();
        detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
      } catch {
        /* non-JSON error body */
      }
      throw new ApiError(detail, res.status);
    }
    return (await res.json()) as T;
  } catch (err) {
    if (err instanceof ApiError) throw err;
    if ((err as Error).name === "AbortError" && !outer?.aborted) {
      throw new ApiError("Request timed out", 0);
    }
    if ((err as Error).name === "AbortError") throw err;
    throw new ApiError(`Cannot reach the API at ${API_URL}. Is the backend running?`, 0);
  } finally {
    clearTimeout(timer);
  }
}

/** `since` (UNIX seconds) returns only the bars opening at or after it, for topping up a cached series. */
export function fetchKlines(symbol: string, interval: Interval, limit: number, signal?: AbortSignal, since?: number) {
  const q = new URLSearchParams({ symbol, interval, limit: String(limit) });
  if (since !== undefined) q.set("since", String(since));
  return request<{ symbol: string; interval: Interval; source: string; candles: Candle[] }>(`/api/klines?${q}`, {
    signal,
  });
}

export function fetchMetrics(signal?: AbortSignal) {
  return request<MarketMetrics>("/api/market/metrics", { signal });
}

export function fetchSymbols(signal?: AbortSignal) {
  return request<{ symbols: string[] }>("/api/symbols", { signal });
}

export function analyze(
  body: {
    symbol: string;
    interval: Interval;
    prompt: string;
    candles?: Candle[];
    history?: ChatTurn[];
    overlays?: Overlay[];
    previous_intent?: AnalysisIntent | null;
    watchlist?: string[];
  },
  signal?: AbortSignal,
) {
  return request<AnalyzeResponse>("/api/agent/analyze", {
    method: "POST",
    body: JSON.stringify(body),
    signal,
    timeoutMs: 90_000, // local LLMs can be slow on first load
  });
}

export function fetchTickers(symbols: string[], signal?: AbortSignal) {
  const q = new URLSearchParams({ symbols: symbols.join(",") });
  return request<{ tickers: Ticker[] }>(`/api/tickers?${q}`, { signal });
}

export function fetchWatchlistScan(symbols: string[], interval: Interval, signal?: AbortSignal) {
  const q = new URLSearchParams({ symbols: symbols.join(","), interval });
  return request<ScanResult[]>(`/api/watchlist/scan?${q}`, { signal, timeoutMs: 60_000 });
}

// ------------------------------------------------------------- alerts --

export function fetchAlerts(signal?: AbortSignal) {
  return request<{ alerts: PriceAlert[]; channels: AlertChannels }>("/api/alerts", { signal });
}

export function createAlerts(symbol: string, alerts: AlertSpec[]) {
  return request<{ alerts: PriceAlert[] }>("/api/alerts", { method: "POST", body: JSON.stringify({ symbol, alerts }) });
}

export function deleteAlert(id: string) {
  return request<{ ok: boolean }>(`/api/alerts/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export function rearmAlert(id: string) {
  return request<{ alert: PriceAlert }>(`/api/alerts/${encodeURIComponent(id)}/rearm`, { method: "POST" });
}

export function clearTriggeredAlerts() {
  return request<{ removed: number }>("/api/alerts/clear-triggered", { method: "POST" });
}

/** Sends a test message to every configured channel → which ones delivered it. */
export function testAlertChannels() {
  return request<{ results: Partial<Record<keyof AlertChannels, boolean>> }>("/api/alerts/test", { method: "POST" });
}
