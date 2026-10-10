// GET /api/status (backend/app/main.py `status`): the AI model, market data, the Binance key, alert channels and
// every background job's last run and error.

import { apiRequest } from "./api";

export type JobState = "ok" | "waiting" | "error" | "overdue" | "off";

export interface JobStatus {
  name: string;
  label: string;
  every_seconds: number;
  enabled: boolean;
  state: JobState;
  last_ok: number | null;
  last_fail: number | null;
  last_error: string | null;
  detail: string;
  runs: number;
  failures: number;
}

export interface AppStatus {
  time: number;
  llm: {
    provider: string;
    model: string;
    configured: boolean;
    /** Skipped for 30 s after a connection failure. */
    paused: boolean;
    last_ok_at: number | null;
    last_error: string | null;
    last_error_at: number | null;
    /** Ollama: answers on /api/tags. Cloud models aren't pinged (null). */
    reachable: boolean | null;
    installed?: boolean;
    problem: string | null;
  };
  market: { data_source: string; binance_reachable: boolean; streams: Record<string, unknown>; liquidation_stream: boolean };
  binance_key: { configured: boolean; ok: boolean | null; masked: string | null; problems: string[]; error: string | null; checked_at: number | null };
  channels: { telegram: boolean; discord: boolean };
  /** Per channel: when it last delivered, its last failure, and counts over the last 24 h. */
  delivery?: Record<string, { last_ok: number | null; last_fail: number | null; last_error: string | null; sent_24h: number; failed_24h: number }>;
  database: { path: string; memory: boolean };
  jobs: JobStatus[];
}

export function fetchStatus(signal?: AbortSignal) {
  return apiRequest<AppStatus>("/api/status", { signal, timeoutMs: 15_000 });
}

/** Things worth a badge on the Status tab: the model not answering, Binance down, failing or stalled jobs. */
export function statusProblems(s: AppStatus | null): number {
  if (!s) return 0;
  let n = s.jobs.filter((j) => j.state === "error" || j.state === "overdue").length;
  if (s.llm.problem || s.llm.paused) n += 1;
  if (!s.market.binance_reachable && s.market.data_source !== "synthetic") n += 1;
  if (s.binance_key.configured && s.binance_key.ok === false) n += 1;
  return n;
}
