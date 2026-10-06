import type { IChartApi, Logical } from "lightweight-charts";

/**
 * Maps arbitrary UNIX times (between bars, before the first bar, or in the
 * future) to chart x-coordinates and back.
 *
 * Lightweight Charts only knows the times of existing bars, so drawings and
 * overlays anchored to other times (higher-timeframe levels, future targets)
 * go through logical indexes, interpolating inside the data and extrapolating
 * outside it with the bar interval.
 */
export class TimeMapper {
  private times: number[] = [];
  constructor(private step: number) {}

  setData(times: number[], step: number) {
    this.times = times;
    this.step = step;
  }

  get length() {
    return this.times.length;
  }

  get lastTime(): number | undefined {
    return this.times[this.times.length - 1];
  }

  timeToLogical(t: number): number {
    const ts = this.times;
    const n = ts.length;
    if (n === 0) return 0;
    if (t <= ts[0]) return (t - ts[0]) / this.step;
    if (t >= ts[n - 1]) return n - 1 + (t - ts[n - 1]) / this.step;
    let lo = 0;
    let hi = n - 1;
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (ts[mid] <= t) lo = mid;
      else hi = mid;
    }
    return lo + (t - ts[lo]) / (ts[hi] - ts[lo]);
  }

  logicalToTime(l: number): number {
    const ts = this.times;
    const n = ts.length;
    if (n === 0) return 0;
    if (l <= 0) return Math.round(ts[0] + l * this.step);
    if (l >= n - 1) return Math.round(ts[n - 1] + (l - (n - 1)) * this.step);
    const i = Math.floor(l);
    return Math.round(ts[i] + (l - i) * (ts[i + 1] - ts[i]));
  }

  /** Bar time at or before `t` (used to snap markers to real bars). */
  barTimeAtOrBefore(t: number): number | undefined {
    const l = Math.floor(this.timeToLogical(t) + 1e-9);
    return this.times[Math.max(0, Math.min(this.times.length - 1, l))];
  }

  barIndexNear(t: number): number {
    return Math.max(0, Math.min(this.times.length - 1, Math.round(this.timeToLogical(t))));
  }

  timeToX(chart: IChartApi, t: number): number | null {
    return chart.timeScale().logicalToCoordinate(this.timeToLogical(t) as Logical);
  }

  xToTime(chart: IChartApi, x: number): number | null {
    const l = chart.timeScale().coordinateToLogical(x);
    return l === null ? null : this.logicalToTime(l);
  }
}
