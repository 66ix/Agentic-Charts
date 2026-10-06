import type { Candle } from "./types";

export interface Point {
  time: number;
  value: number;
}

export function ema(candles: Candle[], period: number): Point[] {
  const k = 2 / (period + 1);
  const out: Point[] = [];
  let prev: number | undefined;
  for (const c of candles) {
    prev = prev === undefined ? c.close : c.close * k + prev * (1 - k);
    out.push({ time: c.time, value: prev });
  }
  return out.slice(Math.min(period - 1, out.length));
}

/** Wilder's Parabolic SAR (matches backend/app/ta_agent.py). */
export function parabolicSar(candles: Candle[], step = 0.02, maxStep = 0.2): Point[] {
  const n = candles.length;
  if (n < 3) return [];
  const out: Point[] = [];
  let up = candles[1].high >= candles[0].high;
  let af = step;
  let ep = up ? candles[0].high : candles[0].low;
  let sar = up ? candles[0].low : candles[0].high;
  for (let i = 1; i < n; i++) {
    const c = candles[i];
    let cur = sar + af * (ep - sar);
    const p1 = candles[i - 1];
    const p2 = candles[Math.max(0, i - 2)];
    if (up) {
      cur = Math.min(cur, p1.low, p2.low);
      if (c.low < cur) {
        up = false;
        cur = ep;
        ep = c.low;
        af = step;
      } else if (c.high > ep) {
        ep = c.high;
        af = Math.min(af + step, maxStep);
      }
    } else {
      cur = Math.max(cur, p1.high, p2.high);
      if (c.high > cur) {
        up = true;
        cur = ep;
        ep = c.high;
        af = step;
      } else if (c.low < ep) {
        ep = c.low;
        af = Math.min(af + step, maxStep);
      }
    }
    sar = cur;
    out.push({ time: c.time, value: cur });
  }
  return out;
}
