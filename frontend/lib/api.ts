import { API_URL } from "./config";
import type { AnalyzeResponse, Candle, Interval, MarketMetrics } from "./types";

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

export function fetchKlines(symbol: string, interval: Interval, limit: number, signal?: AbortSignal) {
  const q = new URLSearchParams({ symbol, interval, limit: String(limit) });
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
  body: { symbol: string; interval: Interval; prompt: string; candles?: Candle[] },
  signal?: AbortSignal,
) {
  return request<AnalyzeResponse>("/api/agent/analyze", {
    method: "POST",
    body: JSON.stringify(body),
    signal,
    timeoutMs: 90_000, // local LLMs can be slow on first load
  });
}
