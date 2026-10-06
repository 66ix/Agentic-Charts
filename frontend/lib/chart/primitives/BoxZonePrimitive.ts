import type { AutoscaleInfo, ISeriesPrimitivePaneView } from "lightweight-charts";

import type { BoxOverlay } from "../../types";
import type { TimeMapper } from "../timeMapper";
import { drawLabel, MediaRenderer, PaneView, PrimitiveBase } from "./base";

/**
 * Translucent price zone (support / resistance / supply / demand).
 * Spans [time_start, time_end] horizontally, extending to the right edge when
 * time_end is null, and paints a matching band on the price axis.
 */
export class BoxZonePrimitive extends PrimitiveBase {
  private readonly views: readonly ISeriesPrimitivePaneView[];
  private readonly axisViews: readonly ISeriesPrimitivePaneView[];

  constructor(
    mapper: TimeMapper,
    public box: BoxOverlay,
  ) {
    super(mapper);
    this.views = [new PaneView(() => new MediaRenderer((s) => this.drawPane(s.context, s.mediaSize)), "bottom")];
    this.axisViews = [new PaneView(() => new MediaRenderer((s) => this.drawAxis(s.context, s.mediaSize)), "bottom")];
  }

  paneViews() {
    return this.views;
  }

  priceAxisPaneViews() {
    return this.axisViews;
  }

  /** Keep AI zones in view: the price scale expands to include them. */
  autoscaleInfo(): AutoscaleInfo {
    return { priceRange: { minValue: this.box.price_low, maxValue: this.box.price_high } };
  }

  private bounds(width: number) {
    const y1 = this.y(this.box.price_high);
    const y2 = this.y(this.box.price_low);
    if (y1 === null || y2 === null) return null;
    const xs = this.box.time_start != null ? this.x(this.box.time_start) : 0;
    const xe = this.box.time_end != null ? this.x(this.box.time_end) : width;
    const left = Math.max(0, Math.min(xs ?? 0, width));
    const right = Math.max(0, Math.min(xe ?? width, width));
    if (right - left < 1) return null;
    return { left, right, top: Math.min(y1, y2), bottom: Math.max(y1, y2) };
  }

  private drawPane(ctx: CanvasRenderingContext2D, size: { width: number; height: number }) {
    const b = this.bounds(size.width);
    if (!b) return;
    const h = Math.max(b.bottom - b.top, 2);
    ctx.fillStyle = this.box.color;
    ctx.fillRect(b.left, b.top, b.right - b.left, h);
    const border = this.box.border_color ?? this.box.color;
    ctx.strokeStyle = border;
    ctx.lineWidth = 1;
    ctx.setLineDash([]);
    ctx.beginPath();
    ctx.moveTo(b.left, b.top + 0.5);
    ctx.lineTo(b.right, b.top + 0.5);
    ctx.moveTo(b.left, b.top + h - 0.5);
    ctx.lineTo(b.right, b.top + h - 0.5);
    ctx.stroke();
    if (this.box.label) {
      // Zone labels sit at the zone's left edge; level labels (rays) sit at the
      // right edge, so the two rarely collide when a level is inside a zone.
      const labelY = h > 20 ? b.top + 10 : b.top - 9;
      drawLabel(ctx, this.box.label, Math.max(b.left + 6, 6), labelY, { color: "#e5e7eb", bg: border, bold: true });
    }
  }

  private drawAxis(ctx: CanvasRenderingContext2D, size: { width: number; height: number }) {
    const y1 = this.y(this.box.price_high);
    const y2 = this.y(this.box.price_low);
    if (y1 === null || y2 === null) return;
    ctx.fillStyle = this.box.color;
    ctx.fillRect(0, Math.min(y1, y2), size.width, Math.max(Math.abs(y2 - y1), 2));
  }
}
