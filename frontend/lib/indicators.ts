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

/** Wilder's RSI: averages seeded with an SMA of the first `period` changes, then smoothed (RMA). */
export function rsi(candles: Candle[], period = 14): Point[] {
  const n = candles.length;
  if (n <= period) return [];
  const value = (g: number, l: number) => (l === 0 ? (g === 0 ? 50 : 100) : 100 - 100 / (1 + g / l));
  let gain = 0;
  let loss = 0;
  for (let i = 1; i <= period; i++) {
    const d = candles[i].close - candles[i - 1].close;
    if (d > 0) gain += d;
    else loss -= d;
  }
  gain /= period;
  loss /= period;
  const out: Point[] = [{ time: candles[period].time, value: value(gain, loss) }];
  for (let i = period + 1; i < n; i++) {
    const d = candles[i].close - candles[i - 1].close;
    gain = (gain * (period - 1) + Math.max(d, 0)) / period;
    loss = (loss * (period - 1) + Math.max(-d, 0)) / period;
    out.push({ time: candles[i].time, value: value(gain, loss) });
  }
  return out;
}

/** EMA of `values` seeded with the SMA of the first `period` values; result[k] is aligned to values[k + period - 1]. */
function emaValues(values: number[], period: number): number[] {
  if (values.length < period) return [];
  const k = 2 / (period + 1);
  let prev = values.slice(0, period).reduce((a, b) => a + b, 0) / period;
  const out = [prev];
  for (let i = period; i < values.length; i++) {
    prev = values[i] * k + prev * (1 - k);
    out.push(prev);
  }
  return out;
}

export interface MacdResult {
  macd: Point[];
  signal: Point[];
  hist: Point[];
}

/** MACD line = EMA(fast) - EMA(slow) of closes; signal = EMA(signal) of the MACD line; hist = MACD - signal. */
export function macd(candles: Candle[], fast = 12, slow = 26, signal = 9): MacdResult {
  const closes = candles.map((c) => c.close);
  const f = emaValues(closes, fast);
  const s = emaValues(closes, slow);
  const start = slow - 1; // first candle index where both EMAs exist
  const line: Point[] = s.map((sv, j) => ({
    time: candles[start + j].time,
    value: f[start - (fast - 1) + j] - sv,
  }));
  const sig = emaValues(line.map((p) => p.value), signal);
  const off = signal - 1;
  return {
    macd: line,
    signal: sig.map((v, j) => ({ time: line[off + j].time, value: v })),
    hist: sig.map((v, j) => ({ time: line[off + j].time, value: line[off + j].value - v })),
  };
}

/**
 * Volume-weighted average price of the typical price (h+l+c)/3.
 * Intraday intervals (< 1d) use a session VWAP that resets at each UTC day;
 * daily and above are anchored at the first candle (cumulative).
 */
export function vwap(candles: Candle[], intervalSeconds: number): Point[] {
  const intraday = intervalSeconds < 86400;
  const out: Point[] = [];
  let session = NaN;
  let pv = 0;
  let vol = 0;
  for (const c of candles) {
    const day = Math.floor(c.time / 86400);
    if (intraday && day !== session) {
      session = day;
      pv = 0;
      vol = 0;
    }
    const tp = (c.high + c.low + c.close) / 3;
    pv += tp * c.volume;
    vol += c.volume;
    out.push({ time: c.time, value: vol > 0 ? pv / vol : tp });
  }
  return out;
}
