import { MismatchDirection, type ISeriesPrimitiveAxisView, type ISeriesPrimitivePaneView } from "lightweight-charts";

import { formatPrice } from "../../format";
import type { LevelBox, LevelLine, SessionDrawing } from "../../sessionLevels";
import type { TimeMapper } from "../timeMapper";
import { drawLabel, MediaRenderer, PaneView, PrimitiveBase } from "./base";

/** "#rrggbb" → "rgba(r, g, b, a)". */
function withAlpha(hex: string, a: number): string {
  const h = hex.replace("#", "");
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
  return `rgba(${r}, ${g}, ${b}, ${a})`;
}

interface Bar {
  time: number;
  high: number;
  low: number;
}

/**
 * Session highs and lows, previous day / week / month levels and opening ranges (lib/sessionLevels.ts).
 *
 * Session ranges are faint boxes over their hours (behind the candles); each level is a line from where its
 * window opened to the right edge with a short label there ("LON H", "PDH"), or, once price has traded through
 * it, a fainter stub that ends where it was taken, labelled at its start. Only the untaken previous day / week /
 * month levels get price tags on the axis. Levels of a session that is still running follow the newest candle
 * between polls. Levels never widen the price scale.
 */
export class SessionLevelsPrimitive extends PrimitiveBase {
  private drawing: SessionDrawing = { lines: [], boxes: [] };
  private axis: ISeriesPrimitiveAxisView[] = [];
  private readonly views: readonly ISeriesPrimitivePaneView[];

  constructor(mapper: TimeMapper) {
    super(mapper);
    this.views = [
      new PaneView(() => new MediaRenderer((s) => this.drawBoxes(s.context, s.mediaSize.width)), "bottom"),
      new PaneView(() => new MediaRenderer((s) => this.drawLines(s.context, s.mediaSize.width)), "normal"),
    ];
  }

  set(drawing: SessionDrawing) {
    this.drawing = drawing;
    this.axis = drawing.lines
      .filter((l) => l.axis)
      .map((l) => ({
        coordinate: () => this.y(l.price) ?? -100,
        fixedCoordinate: () => this.y(l.price) ?? -100, // see LabeledRayPrimitive
        text: () => formatPrice(l.price),
        textColor: () => "#0b0e14",
        backColor: () => l.color,
        visible: () => this.y(l.price) !== null,
        tickVisible: () => true,
      }));
    this.requestUpdate();
  }

  paneViews() {
    return this.views;
  }

  priceAxisViews() {
    return this.axis;
  }

  /** The newest candle, for levels of a session that is still running. */
  private lastBar(): Bar | null {
    const d = this.series?.dataByIndex(Number.MAX_SAFE_INTEGER, MismatchDirection.NearestLeft) as
      | { time?: unknown; high?: number; low?: number }
      | null
      | undefined;
    if (!d || typeof d.time !== "number" || d.high === undefined || d.low === undefined) return null;
    return { time: d.time, high: d.high, low: d.low };
  }

  private livePrice(l: LevelLine, bar: Bar | null): number {
    if (!l.live || !bar || bar.time < l.live.open || bar.time >= l.live.close) return l.price;
    return l.live.side === "high" ? Math.max(l.price, bar.high) : Math.min(l.price, bar.low);
  }

  private liveRange(b: LevelBox, bar: Bar | null): [number, number] {
    if (!b.live || !bar || bar.time < b.live.open || bar.time >= b.live.close) return [b.high, b.low];
    return [Math.max(b.high, bar.high), Math.min(b.low, bar.low)];
  }

  private span(start: number, end: number | null, width: number): [number, number] | null {
    const xs = this.x(start);
    const xe = end === null ? width : this.x(end);
    if (xs === null || xe === null) return null;
    const left = Math.max(0, Math.min(xs, width));
    const right = Math.max(0, Math.min(xe, width));
    return right - left >= 1 ? [left, right] : null;
  }

  private drawBoxes(ctx: CanvasRenderingContext2D, width: number) {
    const bar = this.lastBar();
    for (const b of this.drawing.boxes) {
      const sp = this.span(b.start, b.end, width);
      const [hi, lo] = this.liveRange(b, bar);
      const top = this.y(hi);
      const bottom = this.y(lo);
      if (top === null || bottom === null) continue;
      const h = Math.max(bottom - top, 1);
      if (sp) {
        ctx.fillStyle = withAlpha(b.color, b.fill);
        ctx.fillRect(sp[0], top, sp[1] - sp[0], h);
        if (b.label) {
          ctx.strokeStyle = withAlpha(b.color, 0.7);
          ctx.lineWidth = 1;
          ctx.setLineDash([]);
          ctx.strokeRect(Math.round(sp[0]) + 0.5, Math.round(top) + 0.5, Math.max(1, Math.round(sp[1] - sp[0]) - 1), Math.max(1, Math.round(h) - 1));
        }
      }
      if (b.extendTo !== undefined) {
        // An opening range's high and low carry on to the end of its day or session.
        const ext = this.span(b.end, b.extendTo, width);
        if (ext) {
          ctx.strokeStyle = withAlpha(b.color, 0.55);
          ctx.lineWidth = 1;
          ctx.setLineDash([3, 3]);
          ctx.beginPath();
          for (const y of [top, bottom]) {
            ctx.moveTo(ext[0], Math.round(y) + 0.5);
            ctx.lineTo(ext[1], Math.round(y) + 0.5);
          }
          ctx.stroke();
          ctx.setLineDash([]);
        }
      }
      if (b.label && sp) drawLabel(ctx, b.label, sp[0], top - 9, { color: b.color, labels: this.labels });
    }
  }

  private drawLines(ctx: CanvasRenderingContext2D, width: number) {
    const bar = this.lastBar();
    for (const l of this.drawing.lines) {
      const y0 = this.y(this.livePrice(l, bar));
      const sp = this.span(l.start, l.end, width);
      if (y0 === null || !sp) continue;
      const y = Math.round(y0) + 0.5;
      ctx.globalAlpha = l.taken ? 0.45 : 0.9;
      ctx.strokeStyle = l.color;
      ctx.lineWidth = 1;
      ctx.setLineDash(l.dashed ? [5, 4] : []);
      ctx.beginPath();
      ctx.moveTo(sp[0], y);
      ctx.lineTo(sp[1], y);
      ctx.stroke();
      ctx.setLineDash([]);
      if (l.taken && sp[1] < width) {
        // Where it was taken: a small cross.
        ctx.beginPath();
        ctx.moveTo(sp[1] - 3, y - 3);
        ctx.lineTo(sp[1] + 3, y + 3);
        ctx.moveTo(sp[1] - 3, y + 3);
        ctx.lineTo(sp[1] + 3, y - 3);
        ctx.stroke();
      }
      ctx.globalAlpha = l.taken ? 0.6 : 1;
      if (l.end === null) drawLabel(ctx, l.label, width - 8, y - 9, { color: l.color, align: "right", bold: true, labels: this.labels });
      else drawLabel(ctx, l.label, sp[0] + 2, y - 9, { color: l.color, labels: this.labels });
      ctx.globalAlpha = 1;
    }
  }
}
