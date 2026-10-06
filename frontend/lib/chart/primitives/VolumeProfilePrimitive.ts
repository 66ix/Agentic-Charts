import type { ISeriesPrimitivePaneView } from "lightweight-charts";

import type { ProfileBin } from "../../indicators";
import type { TimeMapper } from "../timeMapper";
import { drawLabel, MediaRenderer, PaneView, PrimitiveBase } from "./base";

/** Volume profile of the visible candles as horizontal bars on the right edge, with the point of control. */
export class VolumeProfilePrimitive extends PrimitiveBase {
  private bins: ProfileBin[] = [];
  private poc: number | null = null;
  private readonly views: readonly ISeriesPrimitivePaneView[];

  constructor(mapper: TimeMapper) {
    super(mapper);
    this.views = [new PaneView(() => new MediaRenderer((s) => this.draw(s.context, s.mediaSize.width)), "bottom")];
  }

  paneViews() {
    return this.views;
  }

  set(bins: ProfileBin[], poc: number | null) {
    this.bins = bins;
    this.poc = poc;
    this.requestUpdate();
  }

  private draw(ctx: CanvasRenderingContext2D, width: number) {
    if (!this.bins.length) return;
    const max = Math.max(...this.bins.map((b) => b.volume));
    if (!(max > 0)) return;
    const span = width * 0.22;
    for (const b of this.bins) {
      const top = this.y(b.high);
      const bottom = this.y(b.low);
      if (top === null || bottom === null) continue;
      const w = (b.volume / max) * span;
      const isPoc = this.poc !== null && this.poc >= b.low && this.poc <= b.high;
      ctx.fillStyle = isPoc ? "rgba(250, 204, 21, 0.35)" : "rgba(148, 163, 184, 0.16)";
      ctx.fillRect(width - w, top + 0.5, w, Math.max(1, bottom - top - 1));
    }
    if (this.poc !== null) {
      const y = this.y(this.poc);
      if (y !== null) {
        ctx.strokeStyle = "rgba(250, 204, 21, 0.7)";
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 3]);
        ctx.beginPath();
        ctx.moveTo(width - span, Math.round(y) + 0.5);
        ctx.lineTo(width, Math.round(y) + 0.5);
        ctx.stroke();
        ctx.setLineDash([]);
        drawLabel(ctx, "POC (visible)", width - span - 4, y, { color: "#facc15", align: "right", labels: this.labels });
      }
    }
  }
}
