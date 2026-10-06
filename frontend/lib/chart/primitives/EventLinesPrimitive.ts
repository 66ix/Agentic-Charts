import type { ISeriesPrimitivePaneView } from "lightweight-charts";

import type { TimeMapper } from "../timeMapper";
import { drawLabel, FONT, MediaRenderer, PaneView, PrimitiveBase } from "./base";

/** One economic event (vertical dashed line + label) or news item (dot on the news lane) on the time axis. */
export type EventLine = { time: number; label: string; color: string; kind: "event" | "news"; title?: string };

const LABEL_ROWS = [10, 28]; // y of the event label rows at the top of the pane
const NEWS_LANE = 46; // y of the news dots
const DOT_R = 3;
const GAP = 4; // px between labels on a row

/**
 * Economic events and news on the time axis. Times between, before or after the bars all work (the TimeMapper
 * extrapolates), so upcoming events show to the right of the last candle. Events are dashed vertical lines with a
 * short label at the top of the pane; labels that would overlap move to a second row or are dropped (the line
 * stays). News are small dots on a lane under the labels. `itemAt(x, y)` finds the item under the mouse so the
 * chart can show its `title` as a tooltip.
 */
export class EventLinesPrimitive extends PrimitiveBase {
  private items: EventLine[];
  private readonly views: readonly ISeriesPrimitivePaneView[];
  /** Where each item was last drawn, for itemAt(). */
  private hits: { x: number; y: number; w: number; item: EventLine }[] = [];

  constructor(mapper: TimeMapper, items: EventLine[]) {
    super(mapper);
    this.items = items;
    this.views = [new PaneView(() => new MediaRenderer((s) => this.draw(s.context, s.mediaSize)), "bottom")];
  }

  setItems(items: EventLine[]) {
    this.items = items;
    this.requestUpdate();
  }

  paneViews() {
    return this.views;
  }

  /** The item drawn under (x, y) in pane pixels: a label, a news dot, or (anywhere in the pane) an event line. */
  itemAt(x: number, y: number): EventLine | null {
    let best: EventLine | null = null;
    let bestD = Infinity;
    for (const h of this.hits) {
      let d = Infinity;
      if (h.w > 0) d = x >= h.x && x <= h.x + h.w && Math.abs(y - h.y) <= 8 ? 0 : Infinity; // a label
      else if (h.item.kind === "news") d = Math.hypot(x - h.x, y - h.y) <= DOT_R + 3 ? Math.hypot(x - h.x, y - h.y) : Infinity;
      else if (Math.abs(x - h.x) <= 4) d = Math.abs(x - h.x); // an event line, anywhere along it
      if (d < bestD) {
        best = h.item;
        bestD = d;
      }
    }
    return best;
  }

  private draw(ctx: CanvasRenderingContext2D, size: { width: number; height: number }) {
    this.hits = [];
    if (!this.items.length) return;
    const events = this.items.filter((i) => i.kind === "event").sort((a, b) => a.time - b.time);
    const news = this.items.filter((i) => i.kind === "news");
    const rowEnds = LABEL_ROWS.map(() => -Infinity);

    ctx.save();
    ctx.lineWidth = 1;
    for (const ev of events) {
      const x0 = this.x(ev.time);
      if (x0 === null || x0 < 0 || x0 > size.width) continue;
      const x = Math.round(x0) + 0.5;
      ctx.globalAlpha = 0.55;
      ctx.strokeStyle = ev.color;
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, size.height);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.globalAlpha = 1;

      ctx.font = FONT;
      const w = ctx.measureText(ev.label).width + 8;
      const left = Math.min(Math.max(x - w / 2, 0), size.width - w);
      const row = rowEnds.findIndex((end) => left >= end + GAP);
      if (row === -1) {
        this.hits.push({ x, y: -100, w: 0, item: ev });
        continue;
      }
      rowEnds[row] = left + w;
      drawLabel(ctx, ev.label, left, LABEL_ROWS[row], { color: "#0b0e14", bg: ev.color, bold: true });
      this.hits.push({ x: left, y: LABEL_ROWS[row], w, item: ev });
      this.hits.push({ x, y: -100, w: 0, item: ev });
    }

    for (const n of news) {
      const x = this.x(n.time);
      if (x === null || x < 0 || x > size.width) continue;
      ctx.fillStyle = n.color;
      ctx.strokeStyle = "#0b0e14";
      ctx.beginPath();
      ctx.arc(x, NEWS_LANE, DOT_R, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
      this.hits.push({ x, y: NEWS_LANE, w: 0, item: n });
    }
    ctx.restore();
  }
}
