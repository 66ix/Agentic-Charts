import type { ISeriesPrimitivePaneView } from "lightweight-charts";

import { formatPrice } from "../../format";
import { INTERVAL_SECONDS, type KimiForecast, type KimiResult } from "../../types";
import type { TimeMapper } from "../timeMapper";
import { dash, FONT, MediaRenderer, PaneView, PrimitiveBase, type LabelRegistry } from "./base";

// The script's colours (Kimi Cooked v5.7.4 inputs and drawing code).
const SUPPORT = [0, 208, 132] as const; // #00d084
const RESIST = [255, 71, 87] as const; // #ff4757
const GREY = [140, 140, 140] as const; // #8c8c8c
const FORECAST = [55, 66, 250] as const; // #3742fa
const GOLD = [255, 214, 10] as const; // #ffd60a
const FIB_BLUE = [10, 132, 255] as const; // #0a84ff
const FIB_EDGE = [176, 179, 184] as const; // #b0b3b8

type Rgb = readonly [number, number, number];
const rgba = ([r, g, b]: Rgb, a: number) => `rgba(${r}, ${g}, ${b}, ${a})`;
const LABEL_GAP = 13; // px between right-hand labels before one is dropped

/**
 * Draws Kimi Cooked's own picture: S/R zones and rays with their odds, the auto Fib ladder and golden pocket,
 * and the forecast (confidence band, best-guess line, textured scenario path, end label and next-candle call).
 * Its B+/B-/U/Dn/B+?/B-? labels are series markers, set by the chart.
 */
/** Which parts of Kimi's picture to draw (the Layers tab toggles them). */
export interface KimiParts {
  sr: boolean;
  fib: boolean;
  forecast: boolean;
}

export class KimiPrimitive extends PrimitiveBase {
  private readonly views: readonly ISeriesPrimitivePaneView[];

  constructor(
    mapper: TimeMapper,
    public data: KimiResult,
    private readonly parts: KimiParts = { sr: true, fib: true, forecast: true },
  ) {
    super(mapper);
    this.views = [
      new PaneView(() => new MediaRenderer((s) => this.drawBack(s.context, s.mediaSize.width)), "bottom"),
      new PaneView(() => new MediaRenderer((s) => this.drawFront(s.context, s.mediaSize.width)), "normal"),
    ];
  }

  paneViews() {
    return this.views;
  }

  /** x of the k-th candle after the last closed one (k = 0 is that candle). */
  private xAfter(k: number): number | null {
    return this.x(this.data.last_closed + k * INTERVAL_SECONDS[this.data.interval]);
  }

  private clampX(x: number | null, width: number, fallback: number) {
    return Math.max(0, Math.min(x ?? fallback, width));
  }

  // ------------------------------------------------ zones, pocket, band
  private drawBack(ctx: CanvasRenderingContext2D, width: number) {
    for (const lv of this.parts.sr ? this.data.levels : []) {
      const top = this.y(lv.zone_high);
      const bottom = this.y(lv.zone_low);
      if (top === null || bottom === null) continue;
      const left = this.clampX(this.x(lv.time_start), width, 0);
      const right = lv.time_end != null ? this.clampX(this.x(lv.time_end), width, width) : width;
      if (right - left < 1) continue;
      const rgb = lv.state === "broken" ? GREY : lv.side === "support" ? SUPPORT : RESIST;
      const h = Math.max(bottom - top, 1);
      ctx.fillStyle = rgba(rgb, lv.state === "broken" ? 0.05 : 0.08);
      ctx.fillRect(left, top, right - left, h);
      ctx.strokeStyle = rgba(rgb, lv.state === "broken" ? 0.15 : 0.3);
      ctx.lineWidth = 1;
      ctx.setLineDash([]);
      ctx.strokeRect(left + 0.5, top + 0.5, right - left - 1, h - 1);
    }

    const fib = this.parts.fib ? this.data.fib : null;
    if (fib) {
      const top = this.y(fib.pocket_high);
      const bottom = this.y(fib.pocket_low);
      if (top !== null && bottom !== null) {
        const left = this.clampX(this.x(fib.time_start), width, 0);
        const right = this.clampX(this.x(fib.time_end), width, width);
        if (right > left) {
          ctx.fillStyle = rgba(GOLD, 0.08);
          ctx.fillRect(left, top, right - left, Math.max(bottom - top, 1));
          ctx.strokeStyle = rgba(GOLD, 0.45);
          dash(ctx, "dashed");
          ctx.strokeRect(left + 0.5, top + 0.5, right - left - 1, Math.max(bottom - top, 1) - 1);
          ctx.setLineDash([]);
        }
      }
    }

    const f = this.parts.forecast ? this.data.forecast : null;
    if (f) this.drawBand(ctx, f);
  }

  private points(f: KimiForecast, ys: number[]): { x: number; y: number }[] | null {
    const out: { x: number; y: number }[] = [];
    for (let k = 0; k < ys.length; k++) {
      const x = this.x(f.start_time + k * f.step);
      const y = this.y(ys[k]);
      if (x === null || y === null) return null;
      out.push({ x, y });
    }
    return out;
  }

  private drawBand(ctx: CanvasRenderingContext2D, f: KimiForecast) {
    const hi = this.points(f, f.band_high);
    const lo = this.points(f, f.band_low);
    if (!hi || !lo) return;
    ctx.fillStyle = rgba(FORECAST, 0.08);
    ctx.beginPath();
    hi.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
    for (let i = lo.length - 1; i >= 0; i--) ctx.lineTo(lo[i].x, lo[i].y);
    ctx.closePath();
    ctx.fill();
    ctx.strokeStyle = rgba(FORECAST, 0.45);
    ctx.lineWidth = 1;
    ctx.setLineDash([]);
    for (const edge of [hi, lo]) {
      ctx.beginPath();
      edge.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
      ctx.stroke();
    }
  }

  // ------------------------------------------- lines, paths and labels
  private drawFront(ctx: CanvasRenderingContext2D, width: number) {
    const labelX = this.xAfter(3); // the script puts its level labels two bars right of the live candle
    const used: number[] = [];
    const place = (y: number) => {
      if (used.some((u) => Math.abs(u - y) < LABEL_GAP)) return false;
      used.push(y);
      return true;
    };
    const labels = this.labels;

    const fib = this.parts.fib ? this.data.fib : null;
    if (fib) {
      const left = this.clampX(this.x(fib.time_start), width, 0);
      const right = this.clampX(this.x(fib.time_end), width, width);
      for (const lv of fib.levels) {
        const y = this.y(lv.price);
        if (y === null) continue;
        const edge = lv.ratio === 0 || lv.ratio === 1;
        const key = lv.ratio === 0.618;
        const ext = lv.ratio > 1;
        const lineRgb = edge ? FIB_EDGE : key || ext ? GOLD : FIB_BLUE;
        ctx.strokeStyle = rgba(lineRgb, edge ? 0.7 : key ? 0.9 : ext ? 0.65 : 0.8);
        ctx.lineWidth = key || edge ? 2 : 1;
        dash(ctx, ext ? "dotted" : "dashed");
        ctx.beginPath();
        ctx.moveTo(left, Math.round(y) + 0.5);
        ctx.lineTo(right, Math.round(y) + 0.5);
        ctx.stroke();
        ctx.setLineDash([]);
        if (labelX !== null && place(y)) {
          const tag = edge ? (lv.ratio === 0 ? " · swing start" : " · swing now") : key ? " · golden pocket" : "";
          const color = edge ? "#d7dade" : key || ext ? rgba(GOLD, 1) : "#4da3ff";
          text(ctx, `Fib ${+lv.ratio.toFixed(3)}  ${formatPrice(lv.price)}${tag} · ${lv.odds}%`, labelX, y, color, labels);
        }
      }
    }

    for (const lv of this.parts.sr ? this.data.levels : []) {
      const y = this.y(lv.price);
      if (y === null) continue;
      const left = this.clampX(this.x(lv.time_start), width, 0);
      const broken = lv.state === "broken";
      const right = broken ? this.clampX(lv.time_end != null ? this.x(lv.time_end) : null, width, width) : width;
      const rgb = lv.side === "support" ? SUPPORT : RESIST;
      ctx.strokeStyle = broken ? rgba(GREY, 0.5) : rgba(rgb, lv.state === "expired" ? 0.4 : 1);
      ctx.lineWidth = 1;
      ctx.setLineDash([]);
      ctx.beginPath();
      ctx.moveTo(left, Math.round(y) + 0.5);
      ctx.lineTo(right, Math.round(y) + 0.5);
      ctx.stroke();
      // Odds tags on every level still standing; a Fib label at the same height wins, as on the script.
      if (!broken && labelX !== null && lv.odds !== null && place(y)) {
        text(ctx, `${lv.side === "support" ? "S" : "R"}  ${formatPrice(lv.price)} · ${lv.odds}%`, labelX, y, rgba(rgb, 1), labels);
      }
    }

    const f = this.parts.forecast ? this.data.forecast : null;
    if (f) this.drawForecast(ctx, f, width, labels);
  }

  private drawForecast(ctx: CanvasRenderingContext2D, f: KimiForecast, width: number, labels: LabelRegistry | null) {
    const path = this.points(f, f.path);
    const tex = this.points(f, f.texture);
    if (!path || !tex) return;
    const dir = (a: number, b: number) => (b > a ? SUPPORT : b < a ? RESIST : GREY);

    // Best guess (the scored line): a thin dotted guide coloured up / down / no call.
    ctx.lineWidth = 1;
    dash(ctx, "dotted");
    for (let k = 1; k < path.length; k++) {
      ctx.strokeStyle = rgba(dir(f.path[k - 1], f.path[k]), 0.7);
      ctx.beginPath();
      ctx.moveTo(path[k - 1].x, path[k - 1].y);
      ctx.lineTo(path[k].x, path[k].y);
      ctx.stroke();
    }
    ctx.setLineDash([]);

    // Textured scenario: each step green or red by its own direction, bold near-term, fading to the horizon.
    for (let k = 1; k < tex.length; k++) {
      const confidence = 1 - (k / f.horizon) * 0.6;
      ctx.strokeStyle = rgba(dir(f.texture[k - 1], f.texture[k]), confidence * 0.9);
      ctx.lineWidth = Math.floor(1 + confidence * 2);
      ctx.beginPath();
      ctx.moveTo(tex[k - 1].x, tex[k - 1].y);
      ctx.lineTo(tex[k].x, tex[k].y);
      ctx.stroke();
    }

    // Next-candle exhaustion call, on the live candle.
    const nc = f.next_candle;
    const nx = this.x(f.start_time + f.step);
    const ny = this.y(nc?.direction === "up" ? f.band_low[1] : f.band_high[1]);
    if (nc && nx !== null && ny !== null) {
      const up = nc.direction === "up";
      const tip = up ? ny + 8 : ny - 8;
      ctx.fillStyle = rgba(up ? SUPPORT : RESIST, 1);
      ctx.beginPath();
      ctx.moveTo(nx, tip);
      ctx.lineTo(nx - 5, tip + (up ? 9 : -9));
      ctx.lineTo(nx + 5, tip + (up ? 9 : -9));
      ctx.closePath();
      ctx.fill();
    }

    // End label, as the script prints it.
    const end = path[path.length - 1];
    const pct = `${f.pct_change >= 0 ? "+" : ""}${f.pct_change.toFixed(2)}%`;
    const lines = [
      f.headline,
      `Range: ${formatPrice(f.range_low)} - ${formatPrice(f.range_high)}`,
      `(${pct}) | Vol: ${f.vol_regime}`,
      `% at levels = chance to reach in ${f.horizon} bars`,
    ];
    if (nc) {
      const record = nc.right_pct !== null ? `right ${Math.round(nc.right_pct)}% here` : "~54% in tests";
      lines.push(`Next candle: ${nc.direction === "up" ? "▲" : "▼"} (stretched move) · ${record}`);
    }
    const bg = f.final > f.path[0] ? "rgba(76, 175, 80, 0.8)" : f.final < f.path[0] ? "rgba(242, 54, 69, 0.8)" : "rgba(120, 123, 134, 0.8)";
    box(ctx, lines, end.x + 8, end.y, bg, width, labels);
  }
}

function text(ctx: CanvasRenderingContext2D, s: string, x: number, y: number, color: string, labels: LabelRegistry | null) {
  ctx.font = FONT;
  if (labels) y = labels.place(x, y, ctx.measureText(s).width, 13);
  ctx.textBaseline = "middle";
  ctx.textAlign = "left";
  ctx.lineWidth = 3;
  ctx.strokeStyle = "rgba(11, 14, 20, 0.85)"; // halo against the chart background
  ctx.strokeText(s, x, y + 0.5);
  ctx.fillStyle = color;
  ctx.fillText(s, x, y + 0.5);
}

/** Multi-line label right of (x, y), pointing at it like label.style_label_left; moved inside the pane when the
 *  chart is scrolled so far right that it would be cut off. */
function box(
  ctx: CanvasRenderingContext2D,
  lines: string[],
  x: number,
  y: number,
  bg: string,
  width: number,
  labels: LabelRegistry | null,
) {
  ctx.font = FONT;
  const lh = 14;
  const w = Math.max(...lines.map((l) => ctx.measureText(l).width)) + 12;
  const h = lines.length * lh + 8;
  const top = y - h / 2;
  ctx.fillStyle = bg;
  if (x + w > width - 4) {
    x = Math.max(4, width - w - 4);
  } else {
    ctx.beginPath();
    ctx.moveTo(x - 6, y);
    ctx.lineTo(x, y - 5);
    ctx.lineTo(x, y + 5);
    ctx.closePath();
    ctx.fill();
  }
  ctx.beginPath();
  ctx.roundRect(x, top, w, h, 3);
  ctx.fill();
  labels?.block({ left: x, top, right: x + w, bottom: top + h });
  ctx.fillStyle = "#ffffff";
  ctx.textBaseline = "middle";
  ctx.textAlign = "left";
  lines.forEach((l, i) => ctx.fillText(l, x + 6, top + 4 + lh * i + lh / 2));
}
