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

/** Pill-shaped text label. */
export function drawLabel(
  ctx: CanvasRenderingContext2D,
  text: string,
  x: number,
  y: number,
  opts: { color: string; bg?: string; align?: "left" | "right"; bold?: boolean },
) {
  if (!text) return;
  ctx.font = opts.bold ? FONT_BOLD : FONT;
  const w = ctx.measureText(text).width + 8;
  const h = 16;
  const left = opts.align === "right" ? x - w : x;
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

  protected y(price: number): number | null {
    return this.series?.priceToCoordinate(price) ?? null;
  }

  protected x(time: number): number | null {
    return this.chart ? this.mapper.timeToX(this.chart, time) : null;
  }

  abstract paneViews(): readonly ISeriesPrimitivePaneView[];
}
