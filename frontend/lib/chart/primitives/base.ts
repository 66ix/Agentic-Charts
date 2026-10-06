import type {
  IChartApi,
  ISeriesApi,
  ISeriesPrimitive,
  ISeriesPrimitiveAxisView,
  ISeriesPrimitivePaneRenderer,
  ISeriesPrimitivePaneView,
  SeriesAttachedParameter,
  SeriesType,
  Time,
} from "lightweight-charts";

import type { TimeMapper } from "../timeMapper";

export type DrawTarget = Parameters<ISeriesPrimitivePaneRenderer["draw"]>[0];
export type MediaScope = { context: CanvasRenderingContext2D; mediaSize: { width: number; height: number } };

export const FONT = "11px Inter, ui-sans-serif, system-ui, sans-serif";
export const FONT_BOLD = "600 11px Inter, ui-sans-serif, system-ui, sans-serif";

export function dash(ctx: CanvasRenderingContext2D, style: string | undefined) {
  ctx.setLineDash(style === "dashed" ? [6, 4] : style === "dotted" ? [2, 3] : []);
}

// ------------------------------------------------------ label collisions
// Every primitive on a chart draws its labels through one LabelRegistry per chart. The registry is cleared at
// the start of each paint (LabelResetPrimitive, attached first), and a label that would overlap one already
// drawn in that paint is nudged up or down to the nearest free spot instead of being drawn on top of it.

interface Rect {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

const GAP = 2;
const MAX_TRIES = 8;

export class LabelRegistry {
  private rects: Rect[] = [];

  reset() {
    this.rects = [];
  }

  /** Records an area that later labels must avoid (e.g. a multi-line box that cannot move). */
  block(r: Rect) {
    this.rects.push(r);
  }

  /** The y (centre) for a w×h label wanted at (left, y): y itself when free, else the nearest free shift. */
  place(left: number, y: number, w: number, h: number): number {
    const free = (cy: number) => {
      const r = { left, right: left + w, top: cy - h / 2, bottom: cy + h / 2 };
      return !this.rects.some((o) => r.left < o.right && r.right > o.left && r.top < o.bottom + GAP && r.bottom + GAP > o.top);
    };
    let best = y;
    if (!free(y)) {
      for (let i = 1; i <= MAX_TRIES; i++) {
        const up = y - i * (h + GAP);
        const down = y + i * (h + GAP);
        if (free(up)) {
          best = up;
          break;
        }
        if (free(down)) {
          best = down;
          break;
        }
      }
    }
    this.rects.push({ left, right: left + w, top: best - h / 2, bottom: best + h / 2 });
    return best;
  }
}

const registries = new WeakMap<object, LabelRegistry>();

/** The label registry shared by every primitive on `chart`. */
export function labelsFor(chart: object | null): LabelRegistry | null {
  if (!chart) return null;
  let r = registries.get(chart);
  if (!r) {
    r = new LabelRegistry();
    registries.set(chart, r);
  }
  return r;
}

/** Pill-shaped text label. With a registry, it moves out of the way of labels drawn earlier in the same paint. */
export function drawLabel(
  ctx: CanvasRenderingContext2D,
  text: string,
  x: number,
  y: number,
  opts: { color: string; bg?: string; align?: "left" | "right"; bold?: boolean; labels?: LabelRegistry | null },
) {
  if (!text) return;
  ctx.font = opts.bold ? FONT_BOLD : FONT;
  const w = ctx.measureText(text).width + 8;
  const h = 16;
  const left = opts.align === "right" ? x - w : x;
  if (opts.labels) y = opts.labels.place(left, y, w, h);
  if (opts.bg) {
    ctx.fillStyle = opts.bg;
    ctx.beginPath();
    ctx.roundRect(left, y - h / 2, w, h, 3);
    ctx.fill();
  }
  ctx.fillStyle = opts.color;
  ctx.textBaseline = "middle";
  ctx.textAlign = "left";
  ctx.fillText(text, left + 4, y + 0.5);
}

/** Renderer that draws in CSS-pixel (media) coordinates. */
export class MediaRenderer implements ISeriesPrimitivePaneRenderer {
  constructor(private readonly fn: (scope: MediaScope) => void) {}
  draw(target: DrawTarget) {
    target.useMediaCoordinateSpace((scope) => this.fn(scope as MediaScope));
  }
}

export class PaneView implements ISeriesPrimitivePaneView {
  constructor(
    private readonly make: () => ISeriesPrimitivePaneRenderer | null,
    private readonly z: "bottom" | "normal" | "top" = "normal",
  ) {}
  zOrder() {
    return this.z;
  }
  renderer() {
    return this.make();
  }
}

/**
 * Shared plumbing for every primitive: keeps chart/series handles and the
 * shared TimeMapper, and exposes requestUpdate().
 */
export abstract class PrimitiveBase implements ISeriesPrimitive<Time> {
  protected chart: IChartApi | null = null;
  protected series: ISeriesApi<SeriesType> | null = null;
  private requestUpdateFn: (() => void) | null = null;
  private readonly emptyAxis: readonly ISeriesPrimitiveAxisView[] = [];

  constructor(protected readonly mapper: TimeMapper) {}

  attached({ chart, series, requestUpdate }: SeriesAttachedParameter<Time>) {
    this.chart = chart as IChartApi;
    this.series = series as ISeriesApi<SeriesType>;
    this.requestUpdateFn = requestUpdate;
  }

  detached() {
    this.chart = null;
    this.series = null;
    this.requestUpdateFn = null;
  }

  requestUpdate() {
    this.requestUpdateFn?.();
  }

  priceAxisViews(): readonly ISeriesPrimitiveAxisView[] {
    return this.emptyAxis;
  }

  /** This chart's shared label registry (null before attach). */
  protected get labels(): LabelRegistry | null {
    return labelsFor(this.chart);
  }

  protected y(price: number): number | null {
    return this.series?.priceToCoordinate(price) ?? null;
  }

  protected x(time: number): number | null {
    return this.chart ? this.mapper.timeToX(this.chart, time) : null;
  }

  abstract paneViews(): readonly ISeriesPrimitivePaneView[];
}

/** Attached before every other primitive: its bottom-layer renderer runs first in each paint and clears the
 *  chart's label registry, so labels are laid out afresh every frame. Draws nothing. */
export class LabelResetPrimitive extends PrimitiveBase {
  private readonly views: readonly ISeriesPrimitivePaneView[] = [
    new PaneView(() => new MediaRenderer(() => this.labels?.reset()), "bottom"),
  ];

  paneViews() {
    return this.views;
  }
}
