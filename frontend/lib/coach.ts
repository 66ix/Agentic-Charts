// Coaching from your own imported trades (backend/app/coach.py).

import { apiRequest } from "./api";

export interface CoachFinding {
  code: string;
  tone: "warn" | "good" | "info";
  title: string;
  detail: string;
  trades: number;
  numbers: Record<string, number | null>;
}

export interface CoachReport {
  generated_at: number;
  overview: { trades: number; wins: number; win_rate: number | null; avg_pct: number | null; pnl: number; fees: number; first: number | null; last: number | null };
  findings: CoachFinding[];
  min_trades: number;
  note?: string;
  coins?: string[];
}

export function fetchCoach(refresh = false, signal?: AbortSignal) {
  return apiRequest<CoachReport>(`/api/coach${refresh ? "?refresh=true" : ""}`, { signal, timeoutMs: 120_000 });
}
