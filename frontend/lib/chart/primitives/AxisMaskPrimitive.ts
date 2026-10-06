import type { ISeriesPrimitive, ISeriesPrimitivePaneView, SeriesAttachedParameter, Time } from "lightweight-charts";

import { MediaRenderer, PaneView } from "./base";

/**
 * Blanks the price-axis tick labels below `from` (a fraction of the plot height).
 *
 * lightweight-charts v4 has a single right price scale for the whole chart, so
 * when RSI/MACD sub-panes are laid out with scaleMargins its price ticks would
 * run on into those panes. Price-axis views at z "normal" are drawn after the
 * tick marks but before the series value tags, so the RSI/MACD tags (and the
 * RSI 70/30 levels) stay visible on top of the mask.
 */
export class AxisMaskPrimitive implements ISeriesPrimitive<Time> {
  private from = 1;
  private requestUpdateFn: (() => void) | null = null;
  private readonly noViews: readonly ISeriesPrimitivePaneView[] = [];
  private readonly axisViews: readonly ISeriesPrimitivePaneView[];

  constructor(private readonly color: string) {
    const renderer = new MediaRenderer(({ context: ctx, mediaSize }) => {
      const y = Math.round(this.from * mediaSize.height);
      ctx.fillStyle = this.color;
      ctx.fillRect(1, y, mediaSize.width - 1, mediaSize.height - y); // x = 0 is the axis border
    });
    this.axisViews = [new PaneView(() => (this.from < 1 ? renderer : null), "normal")];
  }

  attached({ requestUpdate }: SeriesAttachedParameter<Time>) {
    this.requestUpdateFn = requestUpdate;
  }

  detached() {
    this.requestUpdateFn = null;
  }

  setFrom(from: number) {
    if (from === this.from) return;
    this.from = from;
    this.requestUpdateFn?.();
  }

  paneViews() {
    return this.noViews;
  }

  priceAxisPaneViews() {
    return this.axisViews;
  }
}
