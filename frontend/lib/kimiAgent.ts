// Kimi + Agent: what the chart shows of Kimi Cooked once the agent's learned correction is in use
// (backend/app/kimi_agent.py). Plain Kimi stays one toggle away.

import type { KimiResult } from "./types";

/** localStorage key: true = show Kimi's own forecast line instead of Kimi + Agent. */
export const KIMI_PLAIN_KEY = "ac:kimi-plain";

/** `data` with its forecast replaced by Kimi + Agent's when that is in use and the user hasn't asked for plain Kimi.
 *  The textured scenario path moves with the line; the range is the band's end. */
export function withAgent(data: KimiResult, plain: boolean): KimiResult {
  const f = data.forecast;
  const a = f?.agent;
  if (!f || !a || !a.active || plain || a.path.length !== f.path.length) return data;
  const texture = f.texture.map((y, k) => y + (a.path[k] - f.path[k]));
  return {
    ...data,
    forecast: {
      ...f,
      path: a.path,
      band_high: a.band_high,
      band_low: a.band_low,
      texture,
      final: a.final,
      pct_change: a.pct_change,
      range_low: a.band_low[a.band_low.length - 1],
      range_high: a.band_high[a.band_high.length - 1],
      headline: `Kimi + Agent ${a.headline}`,
    },
  };
}

/** Whether Kimi + Agent is what the chart shows right now. */
export function agentInUse(data: KimiResult | null, plain: boolean): boolean {
  return !!data?.forecast?.agent?.active && !plain;
}
