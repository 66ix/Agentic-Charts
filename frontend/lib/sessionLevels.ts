// Session and period levels: the API (GET /api/levels/sessions, backend/app/session_levels.py) and the lines and
// boxes the chart draws from it (SessionLevelsPrimitive).

import { apiRequest } from "./api";
import { DEFAULT_INDICATOR_SETTINGS, type Interval, type SessionLevelSettings } from "./types";

export interface SessionWindow {
  key: "asia" | "london" | "ny";
  name: string;
  label: string;
  which: "latest" | "previous";
  open: number;
  close: number;
  /** The session is in progress: its high and low still move. */
  live: boolean;
  high: number;
  low: number;
  /** When price first traded through the level after the session; null while it holds. */
  high_taken_at: number | null;
  low_taken_at: number | null;
}

export interface PeriodWindow extends Omit<SessionWindow, "key" | "which"> {
  key: "day" | "week" | "month";
}

export interface OpeningRange {
  key: "day" | "asia" | "london" | "ny";
  name: string;
  label: string;
  open: number;
  /** open + N minutes */
  close: number;
  /** Close of the day or session the range belongs to. */
  until: number;
  live: boolean;
  high: number;
  low: number;
}

export interface SessionLevelsData {
  symbol: string;
  interval: string | null;
  source: "binance" | "synthetic";
  note?: string;
  or_minutes: number;
  price: number | null;
  sessions: SessionWindow[];
  periods: PeriodWindow[];
  opening_ranges: OpeningRange[];
}

export function fetchSessionLevels(symbol: string, interval: Interval, orMinutes: number, signal?: AbortSignal) {
  const q = new URLSearchParams({ symbol, interval, or_minutes: String(orMinutes) });
  return apiRequest<SessionLevelsData>(`/api/levels/sessions?${q}`, { signal });
}

export const LEVEL_COLORS: Record<string, string> = {
  asia: "#c084fc",
  london: "#38bdf8",
  ny: "#fb923c",
  day: "#facc15",
  week: "#2dd4bf",
  month: "#f472b6",
};
const DAY_OR_COLOR = "#cbd5e1";

/** One horizontal level: from `start` to `end` (null = the right edge), labelled at its right end. */
export interface LevelLine {
  price: number;
  start: number;
  end: number | null;
  color: string;
  label: string;
  dashed: boolean;
  /** Price tag on the axis (untaken period levels only, so the axis stays readable). */
  axis: boolean;
  /** Taken out: drawn fainter and labelled at its start. */
  taken: boolean;
  /** Follows the newest candle while its session runs: [open, close) and which side. */
  live?: { open: number; close: number; side: "high" | "low" };
}

/** A shaded range: a session's high-low over its hours, or an opening range. */
export interface LevelBox {
  start: number;
  end: number;
  high: number;
  low: number;
  color: string;
  /** Fill opacity. */
  fill: number;
  label: string | null;
  live?: { open: number; close: number };
  /** Opening range: dashed high/low lines continue to here. */
  extendTo?: number;
}

export interface SessionDrawing {
  lines: LevelLine[];
  boxes: LevelBox[];
}

/** Lines and boxes for the levels the settings turn on. */
export function sessionDrawing(data: SessionLevelsData | null, settings: SessionLevelSettings | undefined): SessionDrawing {
  const s = { ...DEFAULT_INDICATOR_SETTINGS.sessions, ...settings };
  const lines: LevelLine[] = [];
  const boxes: LevelBox[] = [];
  if (!data) return { lines, boxes };
  for (const w of data.sessions) {
    if (!s[w.key] || (w.which === "previous" && !s.previous)) continue;
    const color = LEVEL_COLORS[w.key];
    const prefix = w.which === "previous" ? "p" : "";
    if (s.boxes) {
      boxes.push({
        start: w.open,
        end: w.close,
        high: w.high,
        low: w.low,
        color,
        fill: w.which === "latest" ? 0.07 : 0.04,
        label: null,
        live: w.live ? { open: w.open, close: w.close } : undefined,
      });
    }
    for (const side of ["high", "low"] as const) {
      const takenAt = w[`${side}_taken_at`];
      if (takenAt !== null && !s.showTaken) continue;
      lines.push({
        price: w[side],
        start: w.open,
        end: takenAt,
        color,
        label: `${prefix}${w.label} ${side === "high" ? "H" : "L"}`,
        dashed: w.which === "previous",
        axis: false,
        taken: takenAt !== null,
        live: w.live ? { open: w.open, close: w.close, side } : undefined,
      });
    }
  }
  for (const p of data.periods) {
    if (!s[p.key]) continue;
    for (const side of ["high", "low"] as const) {
      const takenAt = p[`${side}_taken_at`];
      if (takenAt !== null && !s.showTaken) continue;
      lines.push({
        price: p[side],
        start: p.open,
        end: takenAt,
        color: LEVEL_COLORS[p.key],
        label: `${p.label}${side === "high" ? "H" : "L"}`,
        dashed: false,
        axis: takenAt === null,
        taken: takenAt !== null,
      });
    }
  }
  if (s.openingRange !== "off") {
    const seen = new Set<string>();
    for (const r of data.opening_ranges) {
      if ((s.openingRange === "day") !== (r.key === "day")) continue;
      if (r.key !== "day" && !s[r.key]) continue;
      const id = `${r.open}:${r.high}:${r.low}`;
      if (seen.has(id)) continue;
      seen.add(id);
      boxes.push({
        start: r.open,
        end: r.close,
        high: r.high,
        low: r.low,
        color: r.key === "day" ? DAY_OR_COLOR : LEVEL_COLORS[r.key],
        fill: 0.14,
        label: `${r.label} ${data.or_minutes}m`,
        live: r.live ? { open: r.open, close: r.close } : undefined,
        extendTo: r.until,
      });
    }
  }
  return { lines, boxes };
}

