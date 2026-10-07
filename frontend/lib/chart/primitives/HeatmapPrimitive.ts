import type { ISeriesPrimitivePaneView } from "lightweight-charts";

import { formatCompact } from "../../format";
import type { HeatmapData } from "../../heatmap";
import type { TimeMapper } from "../timeMapper";
import { drawLabel, MediaRenderer, PaneView, PrimitiveBase } from "./base";

// Bookmap-like ramp from little resting size to a lot: dark blue, cyan, yellow, orange, white. [t, r, g, b, alpha]
const STOPS: [number, number, number, number, number][] = [
  [0, 30, 64, 175, 0.12],
  [0.35, 14, 165, 233, 0.32],
  [0.65, 250, 204, 21, 0.5],
  [0.85, 249, 115, 22, 0.65],
  [1, 255, 247, 237, 0.8],
];
const STEPS = 48;
const PALETTE: string[] = Array.from({ length: STEPS }, (_, i) => {
  const t = i / (STEPS - 1);
  const k = Math.max(1, STOPS.findIndex(([s]) => s >= t));
  const [t0, ...a] = STOPS[k - 1];
  const [t1, ...b] = STOPS[k];
  const f = (t - t0) / (t1 - t0 || 1);
  const [r, g, bl, al] = a.map((v, j) => v + (b[j] - v) * f);
  return `rgba(${Math.round(r)}, ${Math.round(g)}, ${Math.round(bl)}, ${al.toFixed(3)})`;
});
const FLOOR = 0.04; // cells under 4% of the scale are left clear, so the candles stay readable
const GAMMA = 0.6; // lifts mid-sized liquidity, so walls stand out without everything else disappearing

/**
 * Resting order-book liquidity over time, painted behind the candles: one column per time step, one cell per
 * price bin, coloured by its resting size against the 97th percentile of the history (so one huge wall doesn't
 * wash everything else out). A wall shows as a bright band that starts when it is placed and ends when it is
 * pulled or eaten. The newest snapshot's walls are tagged with their size at the right end of the heatmap.
 */
export class HeatmapPrimitive extends PrimitiveBase {
  private data: HeatmapData | null = null;
  private scale = 1;
  private readonly views: readonly ISeriesPrimitivePaneView[];

  constructor(mapper: TimeMapper) {
    super(mapper);
    this.views = [new PaneView(() => new MediaRenderer((s) => this.draw(s.context, s.mediaSize)), "bottom")];
  }

  paneViews() {
    return this.views;
  }

  set(data: HeatmapData | null) {
    this.data = data;
    if (data?.columns.length) {
      const sample: number[] = [];
      const cols = data.columns.slice(-600);
      const stride = Math.max(1, Math.floor(cols.reduce((n, c) => n + c[3].length, 0) / 20_000));
      let k = 0;
      for (const c of cols) for (const v of c[3]) if (v > 0 && k++ % stride === 0) sample.push(v);
      sample.sort((a, b) => a - b);
      this.scale = sample[Math.floor(sample.length * 0.97)] || 1;
    }
    this.requestUpdate();
  }

  private draw(ctx: CanvasRenderingContext2D, size: { width: number; height: number }) {
    const d = this.data;
    if (!d?.columns.length) return;
    const ys = new Map<number, number | null>();
    const yOf = (bin: number) => {
      let y = ys.get(bin);
      if (y === undefined) {
        y = this.y(bin * d.binSize);
        ys.set(bin, y);
      }
      return y;
    };
    const floor = this.scale * FLOOR;
    for (const [t, , first, vals] of d.columns) {
      const xa = this.x(t);
      const xb = this.x(t + d.step);
      if (xa === null || xb === null || xb < 0 || xa > size.width) continue;
      const left = Math.floor(xa);
      const w = Math.max(1, Math.ceil(xb) - left);
      for (let i = 0; i < vals.length; i++) {
        const v = vals[i];
        if (v < floor) continue;
        const top = yOf(first + i + 1);
        const bottom = yOf(first + i);
        if (top === null || bottom === null || bottom < 0 || top > size.height) continue;
        const idx = Math.min(STEPS - 1, Math.floor(STEPS * Math.min(1, v / this.scale) ** GAMMA));
        ctx.fillStyle = PALETTE[idx];
        ctx.fillRect(left, Math.floor(top), w, Math.max(1, Math.ceil(bottom) - Math.floor(top)));
      }
    }
    // The newest snapshot's walls: a tick and their size just right of the last column.
    const last = d.columns[d.columns.length - 1];
    const xr = this.x(last[0] + d.step);
    if (xr === null || xr > size.width - 40) return;
    for (const wall of d.walls) {
      const y = this.y(wall.price);
      if (y === null || y < 0 || y > size.height) continue;
      const color = wall.side === "bid" ? "#4ade80" : "#f87171";
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(xr, Math.round(y) + 0.5);
      ctx.lineTo(xr + 6, Math.round(y) + 0.5);
      ctx.stroke();
      drawLabel(ctx, `$${formatCompact(wall.usd)}`, xr + 8, y, { color, bg: "rgba(11, 14, 20, 0.75)", labels: this.labels });
    }
  }
}
