import { API_URL } from "./config";
import type { AlertPatch } from "./alerts";
import type {
  AlertChannels,
  AnswerDetail,
  AlertSpec,
  AnalysisIntent,
  AnalyzeResponse,
  Candle,
  ChatTurn,
  Interval,
  KimiResult,
  LadderResult,
  MarketMetrics,
  Overlay,
  PriceAlert,
  ScanResult,
  Ticker,
  TopDownResult,
} from "./types";

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

/** JSON request to the backend with a timeout and readable errors. Feature modules (lib/gridbot.ts, …) use it too. */
export async function apiRequest<T>(path: string, init?: RequestInit & { timeoutMs?: number }): Promise<T> {
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
  return apiRequest<{ symbol: string; interval: Interval; source: string; candles: Candle[] }>(`/api/klines?${q}`, {
    signal,
  });
}

export function fetchMetrics(signal?: AbortSignal) {
  return apiRequest<MarketMetrics>("/api/market/metrics", { signal });
}

export function fetchSymbols(signal?: AbortSignal) {
  return apiRequest<{ symbols: string[] }>("/api/symbols", { signal });
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
    /** Spot only: no short plans or short setups. */
    spot_only?: boolean;
    /** The chart's indicator lengths, so the agent's readings match what the user sees. */
    indicator_settings?: {
      ema_fast: number;
      ema_slow: number;
      rsi: number;
      macd: { fast: number; slow: number; signal: number };
      bb: { length: number; mult: number };
      atr: number;
      stoch_rsi: { rsiLength: number; stochLength: number; k: number; d: number };
    };
    /** How long the answer should be. */
    detail?: AnswerDetail;
    /** The user's own note on this coin. */
    coin_note?: string;
    /** The agent's last answer on this coin in an earlier conversation. */
    previous_answer?: { time: number; prompt: string; summary: string; interval?: Interval; price?: number } | null;
  },
  signal?: AbortSignal,
) {
  return apiRequest<AnalyzeResponse>("/api/agent/analyze", {
    method: "POST",
    body: JSON.stringify(body),
    signal,
    timeoutMs: 90_000, // local LLMs can be slow on first load
  });
}

/**
 * `analyze`, streamed: `onResult` gets the drawings, plan and the rest as soon as they are ready (summary still
 * empty) and `onDelta` each piece of the summary as the model writes it. Resolves with the full answer. Uses the
 * plain request when the backend has no stream endpoint.
 */
export async function analyzeStream(
  body: Parameters<typeof analyze>[0],
  handlers: { onResult: (res: AnalyzeResponse) => void; onDelta: (text: string) => void },
  signal?: AbortSignal,
): Promise<AnalyzeResponse> {
  const ctrl = new AbortController();
  // Long answers keep streaming, so the timeout only covers waiting for the next event.
  let timer = setTimeout(() => ctrl.abort(), 90_000);
  const touch = () => {
    clearTimeout(timer);
    timer = setTimeout(() => ctrl.abort(), 90_000);
  };
  signal?.addEventListener("abort", () => ctrl.abort(), { once: true });
  let res: Response;
  try {
    res = await fetch(`${API_URL}/api/agent/analyze/stream`, {
      method: "POST",
      body: JSON.stringify(body),
      signal: ctrl.signal,
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    });
  } catch (err) {
    clearTimeout(timer);
    if ((err as Error).name === "AbortError") {
      if (signal?.aborted) throw err;
      throw new ApiError("Request timed out", 0);
    }
    throw new ApiError(`Cannot reach the API at ${API_URL}. Is the backend running?`, 0);
  }
  if (res.status === 404 || res.status === 405 || !res.body) {
    clearTimeout(timer);
    const full = await analyze(body, signal);
    handlers.onResult({ ...full, summary: "" });
    handlers.onDelta(full.summary);
    return full;
  }
  try {
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const err = await res.json();
        detail = typeof err.detail === "string" ? err.detail : JSON.stringify(err.detail ?? err);
      } catch {
        /* non-JSON error body */
      }
      throw new ApiError(detail, res.status);
    }
    const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      touch();
      buf += value;
      let cut: number;
      while ((cut = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, cut);
        buf = buf.slice(cut + 2);
        const data = chunk
          .split("\n")
          .filter((l) => l.startsWith("data:"))
          .map((l) => l.slice(5).trimStart())
          .join("\n");
        if (!data) continue;
        const ev = JSON.parse(data) as
          | { type: "result"; response: AnalyzeResponse }
          | { type: "done"; response: AnalyzeResponse }
          | { type: "delta"; text: string }
          | { type: "error"; status: number; detail: string };
        if (ev.type === "result") handlers.onResult(ev.response);
        else if (ev.type === "delta") handlers.onDelta(ev.text);
        else if (ev.type === "done") return ev.response;
        else throw new ApiError(ev.detail, ev.status);
      }
    }
    throw new ApiError("The answer was cut off. Try again.", 0);
  } catch (err) {
    if ((err as Error).name === "AbortError" && !signal?.aborted) throw new ApiError("Request timed out", 0);
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

/** Kimi Cooked on the chart's closed candles. The first run on a chart takes a second or two. */
/** Top-down S/R walk on its own (the agent's "Top-down S/R walk" does the same and plays it on the chart). */
export function fetchTopDown(symbol: string, signal?: AbortSignal) {
  return apiRequest<TopDownResult>("/api/top-down", { method: "POST", body: JSON.stringify({ symbol }), signal, timeoutMs: 60_000 });
}

export function fetchDipLadder(
  body: { symbol: string; budget?: number; rungs?: number; timeframe?: "1h" | "4h" | "1d"; days?: number },
  signal?: AbortSignal,
) {
  return apiRequest<LadderResult>("/api/dip-ladder", { method: "POST", body: JSON.stringify(body), signal, timeoutMs: 90_000 });
}

export function fetchKimi(symbol: string, interval: Interval, signal?: AbortSignal) {
  const q = new URLSearchParams({ symbol, interval });
  return apiRequest<KimiResult>(`/api/indicators/kimi?${q}`, { signal, timeoutMs: 60_000 });
}

export function fetchTickers(symbols: string[], signal?: AbortSignal) {
  const q = new URLSearchParams({ symbols: symbols.join(",") });
  return apiRequest<{ tickers: Ticker[] }>(`/api/tickers?${q}`, { signal });
}

export function fetchWatchlistScan(symbols: string[], interval: Interval, signal?: AbortSignal) {
  const q = new URLSearchParams({ symbols: symbols.join(","), interval });
  return apiRequest<ScanResult[]>(`/api/watchlist/scan?${q}`, { signal, timeoutMs: 60_000 });
}

// ------------------------------------------------------------- alerts --

export function fetchAlerts(signal?: AbortSignal) {
  return apiRequest<{ alerts: PriceAlert[]; channels: AlertChannels }>("/api/alerts", { signal });
}

export function createAlerts(symbol: string, alerts: AlertSpec[]) {
  return apiRequest<{ alerts: PriceAlert[] }>("/api/alerts", { method: "POST", body: JSON.stringify({ symbol, alerts }) });
}

export function deleteAlert(id: string) {
  return apiRequest<{ ok: boolean }>(`/api/alerts/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export function rearmAlert(id: string) {
  return apiRequest<{ alert: PriceAlert }>(`/api/alerts/${encodeURIComponent(id)}/rearm`, { method: "POST" });
}

export function clearTriggeredAlerts() {
  return apiRequest<{ removed: number }>("/api/alerts/clear-triggered", { method: "POST" });
}

/** Edit an alert (drag a line, rename, repeat, expiry). Moving the level never fires it. Signal alerts, the
 *  alert history and the brief have their calls in lib/alerts.ts. */
export function updateAlert(id: string, patch: AlertPatch) {
  return apiRequest<{ alert: PriceAlert }>(`/api/alerts/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

/** Sends a test message to every configured channel → which ones delivered it. */
export function testAlertChannels() {
  return apiRequest<{ results: Partial<Record<keyof AlertChannels, boolean>> }>("/api/alerts/test", { method: "POST" });
}
