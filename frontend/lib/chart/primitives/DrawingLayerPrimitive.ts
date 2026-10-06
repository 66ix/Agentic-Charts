import type { ISeriesPrimitivePaneView } from "lightweight-charts";

import { formatPrice } from "../../format";
import type { Drawing } from "../../types";
import type { TimeMapper } from "../timeMapper";
import { dash, drawLabel, FONT, MediaRenderer, PaneView, PrimitiveBase } from "./base";

const FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
const FIB_COLORS = ["#94a3b8", "#ef4444", "#f59e0b", "#22c55e", "#14b8a6", "#3b82f6", "#94a3b8"];
const PATTERN_LABELS = ["X", "A", "B", "C", "D"];
const HIT_PX = 6;

type Px = { x: number; y: number };

function distToSegment(p: Px, a: Px, b: Px): number {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const len2 = dx * dx + dy * dy;
  const t = len2 === 0 ? 0 : Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / len2));
  return Math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy));
}

function withAlpha(hex: string, alpha: number): string {
  if (!hex.startsWith("#") || hex.length !== 7) return hex;
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

/**
 * Renders every user-drawn object (trendlines, rays, rectangles, fib
 * retracements, text, XABCD patterns, ruler) plus the in-progress preview,
 * and hit-tests them for selection.
 */
export class DrawingLayerPrimitive extends PrimitiveBase {
  private drawings: Drawing[] = [];
  private preview: Drawing | null = null;
  private selectedId: string | null = null;
  private barsBetween: (t1: number, t2: number) => number = () => 0;
  private readonly views: readonly ISeriesPrimitivePaneView[];

  constructor(mapper: TimeMapper) {
    super(mapper);
    this.views = [new PaneView(() => new MediaRenderer((s) => this.draw(s.context, s.mediaSize)), "top")];
    this.barsBetween = (t1, t2) => Math.round(Math.abs(mapper.timeToLogical(t2) - mapper.timeToLogical(t1)));
  }

  paneViews() {
    return this.views;
  }

  setDrawings(drawings: Drawing[]) {
    this.drawings = drawings;
    this.requestUpdate();
  }

  setPreview(preview: Drawing | null) {
    this.preview = preview;
    this.requestUpdate();
  }

  setSelected(id: string | null) {
    this.selectedId = id;
    this.requestUpdate();
  }

  /** Id of the top-most drawing under (x, y), or null. */
  pick(x: number, y: number, width: number): string | null {
    const p = { x, y };
    for (let i = this.drawings.length - 1; i >= 0; i--) {
      const d = this.drawings[i];
      const pts = this.px(d);
      if (!pts) continue;
      if (this.hits(d, pts, p, width)) return d.id;
    }
    return null;
  }

  // ------------------------------------------------------------ helpers
  private px(d: Drawing): Px[] | null {
    const out: Px[] = [];
    for (const pt of d.points) {
      const x = this.x(pt.time);
      const y = this.y(pt.price);
      if (x === null || y === null) return null;
      out.push({ x, y });
    }
    return out;
  }

  private hits(d: Drawing, pts: Px[], p: Px, width: number): boolean {
    switch (d.type) {
      case "trendline":
      case "ruler":
        return pts.length === 2 && distToSegment(p, pts[0], pts[1]) <= HIT_PX;
      case "hray":
        return Math.abs(p.y - pts[0].y) <= HIT_PX && p.x >= pts[0].x - HIT_PX && p.x <= width;
      case "rect": {
        const [a, b] = pts;
        const l = Math.min(a.x, b.x) - HIT_PX;
        const r = Math.max(a.x, b.x) + HIT_PX;
        const t = Math.min(a.y, b.y) - HIT_PX;
        const btm = Math.max(a.y, b.y) + HIT_PX;
        return p.x >= l && p.x <= r && p.y >= t && p.y <= btm;
      }
      case "fib": {
        const [a, b] = pts;
        if (p.x < Math.min(a.x, b.x) - HIT_PX || p.x > Math.max(a.x, b.x) + HIT_PX) return false;
        return FIB_LEVELS.some((lv) => Math.abs(p.y - (b.y + (a.y - b.y) * lv)) <= HIT_PX);
      }
      case "text": {
        const w = (d.text?.length ?? 0) * 7 + 10;
        return p.x >= pts[0].x - 4 && p.x <= pts[0].x + w && Math.abs(p.y - pts[0].y) <= 10;
      }
      case "pattern":
        return pts.some((a, i) => i > 0 && distToSegment(p, pts[i - 1], a) <= HIT_PX);
    }
  }

  // -------------------------------------------------------------- draw
  private draw(ctx: CanvasRenderingContext2D, size: { width: number; height: number }) {
    for (const d of this.drawings) this.drawOne(ctx, d, size, d.id === this.selectedId, false);
    if (this.preview) this.drawOne(ctx, this.preview, size, false, true);
  }

  private drawOne(
    ctx: CanvasRenderingContext2D,
    d: Drawing,
    size: { width: number; height: number },
    selected: boolean,
    preview: boolean,
  ) {
    const pts = this.px(d);
    if (!pts || pts.length === 0) return;
    ctx.save();
    ctx.lineWidth = selected ? 2 : 1.5;
    ctx.strokeStyle = d.color;
    ctx.fillStyle = d.color;
    dash(ctx, preview && d.type !== "ruler" ? "dashed" : "solid");

    switch (d.type) {
      case "trendline":
        if (pts.length >= 2) this.segment(ctx, pts[0], pts[1]);
        break;

      case "hray": {
        const y = Math.round(pts[0].y) + 0.5;
        this.segment(ctx, { x: pts[0].x, y }, { x: size.width, y });
        drawLabel(ctx, formatPrice(d.points[0].price), size.width - 6, y - 10, {
          color: "#fff",
          bg: d.color,
          align: "right",
        });
        break;
      }

      case "rect":
        if (pts.length >= 2) {
          const [a, b] = pts;
          ctx.fillStyle = withAlpha(d.color, 0.15);
          ctx.fillRect(Math.min(a.x, b.x), Math.min(a.y, b.y), Math.abs(b.x - a.x), Math.abs(b.y - a.y));
          ctx.strokeRect(Math.min(a.x, b.x), Math.min(a.y, b.y), Math.abs(b.x - a.x), Math.abs(b.y - a.y));
        }
        break;

      case "fib":
        if (pts.length >= 2) this.drawFib(ctx, d, pts);
        break;

      case "text":
        ctx.font = "600 13px Inter, ui-sans-serif, system-ui, sans-serif";
        ctx.textBaseline = "middle";
        ctx.fillText(d.text || "Text", pts[0].x, pts[0].y);
        break;

      case "pattern":
        this.drawPattern(ctx, d, pts);
        break;

      case "ruler":
        if (pts.length >= 2) this.drawRuler(ctx, d, pts);
        break;
    }

    if (selected || preview) {
      ctx.setLineDash([]);
      for (const p of pts) {
        ctx.fillStyle = "#0b0e14";
        ctx.strokeStyle = d.color;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.arc(p.x, p.y, 4, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
      }
    }
    ctx.restore();
  }

  private segment(ctx: CanvasRenderingContext2D, a: Px, b: Px) {
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.stroke();
  }

  private drawFib(ctx: CanvasRenderingContext2D, d: Drawing, pts: Px[]) {
    const [a, b] = pts;
    const [pa, pb] = d.points;
    const left = Math.min(a.x, b.x);
    const right = Math.max(a.x, b.x);
    ctx.setLineDash([]);
    ctx.lineWidth = 1;
    FIB_LEVELS.forEach((lv, i) => {
      // 0 at the second point (end of the move), 1 at the first (start).
      const y = b.y + (a.y - b.y) * lv;
      const price = pb.price + (pa.price - pb.price) * lv;
      if (i > 0) {
        const prevY = b.y + (a.y - b.y) * FIB_LEVELS[i - 1];
        ctx.fillStyle = withAlpha(FIB_COLORS[i], 0.08);
        ctx.fillRect(left, Math.min(y, prevY), right - left, Math.abs(y - prevY));
      }
      ctx.strokeStyle = FIB_COLORS[i];
      this.segment(ctx, { x: left, y }, { x: right, y });
      drawLabel(ctx, `${lv} (${formatPrice(price)})`, left + 2, y - 8, { color: FIB_COLORS[i] });
    });
    ctx.strokeStyle = withAlpha(d.color, 0.5);
    dash(ctx, "dashed");
    this.segment(ctx, a, b);
  }

  private drawPattern(ctx: CanvasRenderingContext2D, d: Drawing, pts: Px[]) {
    ctx.fillStyle = withAlpha(d.color, 0.12);
    for (const tri of [[0, 1, 2], [2, 3, 4]]) {
      if (pts.length > tri[2]) {
        ctx.beginPath();
        tri.forEach((i, k) => (k === 0 ? ctx.moveTo(pts[i].x, pts[i].y) : ctx.lineTo(pts[i].x, pts[i].y)));
        ctx.closePath();
        ctx.fill();
      }
    }
    ctx.beginPath();
    pts.forEach((p, i) => (i === 0 ? ctx.moveTo(p.x, p.y) : ctx.lineTo(p.x, p.y)));
    ctx.stroke();
    pts.forEach((p, i) => {
      const up = i === 0 ? pts.length > 1 && pts[1].y > p.y : p.y < pts[i - 1].y;
      drawLabel(ctx, PATTERN_LABELS[i], p.x - 7, up ? p.y - 12 : p.y + 12, { color: "#fff", bg: d.color, bold: true });
    });
  }

  private drawRuler(ctx: CanvasRenderingContext2D, d: Drawing, pts: Px[]) {
    const [a, b] = pts;
    const [pa, pb] = d.points;
    const diff = pb.price - pa.price;
    const pct = pa.price !== 0 ? (diff / pa.price) * 100 : 0;
    const color = diff >= 0 ? "#3b82f6" : "#ef4444";
    ctx.fillStyle = withAlpha(color, 0.18);
    ctx.fillRect(Math.min(a.x, b.x), Math.min(a.y, b.y), Math.abs(b.x - a.x), Math.abs(b.y - a.y));
    ctx.strokeStyle = color;
    ctx.setLineDash([]);
    this.segment(ctx, a, b);
    const bars = this.barsBetween(pa.time, pb.time);
    const text = `${diff >= 0 ? "+" : ""}${formatPrice(diff)} (${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%) · ${bars} bars`;
    ctx.font = FONT;
    const w = ctx.measureText(text).width + 8;
    const cx = (a.x + b.x) / 2 - w / 2;
    const ty = diff >= 0 ? Math.min(a.y, b.y) - 14 : Math.max(a.y, b.y) + 14;
    drawLabel(ctx, text, cx, ty, { color: "#fff", bg: color });
  }
}
