import type { ISeriesPrimitivePaneView } from "lightweight-charts";

import type { TrendlineOverlay } from "../../types";
import type { TimeMapper } from "../timeMapper";
import { dash, drawLabel, MediaRenderer, PaneView, PrimitiveBase } from "./base";

/** AI-generated trendline through two anchor points, optionally extended right. */
export class TrendLinePrimitive extends PrimitiveBase {
  private readonly views: readonly ISeriesPrimitivePaneView[];

  constructor(
    mapper: TimeMapper,
    public line: TrendlineOverlay,
  ) {
    super(mapper);
    this.views = [new PaneView(() => new MediaRenderer((s) => this.draw(s.context, s.mediaSize)))];
  }

  paneViews() {
    return this.views;
  }

  private draw(ctx: CanvasRenderingContext2D, size: { width: number; height: number }) {
    const x1 = this.x(this.line.time1);
    const x2 = this.x(this.line.time2);
    const y1 = this.y(this.line.price1);
    const y2 = this.y(this.line.price2);
    if (x1 === null || x2 === null || y1 === null || y2 === null || x1 === x2) return;
    let ex = x2;
    let ey = y2;
    if (this.line.extend_right) {
      ex = size.width;
      ey = y1 + ((y2 - y1) * (ex - x1)) / (x2 - x1);
    }
    ctx.strokeStyle = this.line.color;
    ctx.lineWidth = 1.5;
    dash(ctx, this.line.line_style);
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(ex, ey);
    ctx.stroke();
    ctx.setLineDash([]);
    drawLabel(ctx, this.line.label, x2 + 6, y2 - 10, { color: this.line.color });
  }
}
