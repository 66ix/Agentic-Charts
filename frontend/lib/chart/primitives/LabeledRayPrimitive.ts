import type { AutoscaleInfo, ISeriesPrimitiveAxisView, ISeriesPrimitivePaneView } from "lightweight-charts";

import { formatPrice } from "../../format";
import type { HorizontalLineOverlay } from "../../types";
import type { TimeMapper } from "../timeMapper";
import { dash, drawLabel, MediaRenderer, PaneView, PrimitiveBase } from "./base";

/**
 * Horizontal level drawn as a ray from `time_start` to the right edge (or full
 * width), with a label above the line and a coloured price tag on the axis.
 */
export class LabeledRayPrimitive extends PrimitiveBase {
  private readonly views: readonly ISeriesPrimitivePaneView[];
  private readonly axis: readonly ISeriesPrimitiveAxisView[];

  constructor(
    mapper: TimeMapper,
    public line: HorizontalLineOverlay,
  ) {
    super(mapper);
    this.views = [new PaneView(() => new MediaRenderer((s) => this.draw(s.context, s.mediaSize)), "normal")];
    this.axis = [
      {
        coordinate: () => this.y(this.line.price) ?? -100,
        // Without a fixed coordinate lightweight-charts treats this label as sitting at 0 when it lines labels
        // up, and pushes the last-price label to the top of the axis.
        fixedCoordinate: () => this.y(this.line.price) ?? -100,
        text: () => formatPrice(this.line.price),
        textColor: () => "#ffffff",
        backColor: () => this.line.color,
        visible: () => this.y(this.line.price) !== null,
        tickVisible: () => true,
      },
    ];
  }

  paneViews() {
    return this.views;
  }

  priceAxisViews() {
    return this.line.axis_label === false ? [] : this.axis;
  }

  autoscaleInfo(): AutoscaleInfo {
    return { priceRange: { minValue: this.line.price, maxValue: this.line.price } };
  }

  private draw(ctx: CanvasRenderingContext2D, size: { width: number; height: number }) {
    const y = this.y(this.line.price);
    if (y === null) return;
    const xs = this.line.time_start != null ? this.x(this.line.time_start) : 0;
    const left = Math.max(0, Math.min(xs ?? 0, size.width));
    const yy = Math.round(y) + 0.5;
    ctx.strokeStyle = this.line.color;
    ctx.lineWidth = this.line.line_width ?? 1;
    dash(ctx, this.line.line_style);
    ctx.beginPath();
    ctx.moveTo(left, yy);
    ctx.lineTo(size.width, yy);
    ctx.stroke();
    ctx.setLineDash([]);
    if (left > 0) {
      ctx.fillStyle = this.line.color;
      ctx.beginPath();
      ctx.arc(left, yy, 2.5, 0, Math.PI * 2);
      ctx.fill();
    }
    drawLabel(ctx, this.line.label, size.width - 8, yy - 10, {
      color: this.line.color,
      align: "right",
      bold: true,
      labels: this.labels,
    });
  }
}
