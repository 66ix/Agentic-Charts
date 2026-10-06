import type { ISeriesPrimitiveAxisView, ISeriesPrimitivePaneView } from "lightweight-charts";

import type { TimeMapper } from "../timeMapper";
import { PrimitiveBase } from "./base";

/** "03:12" (or "2d 04h") until `close`, UNIX seconds. */
export function countdownText(close: number, now = Date.now() / 1000): string {
  const left = Math.max(0, Math.round(close - now));
  const d = Math.floor(left / 86400);
  const h = Math.floor((left % 86400) / 3600);
  const m = Math.floor((left % 3600) / 60);
  const s = left % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  if (d > 0) return `${d}d ${pad(h)}h`;
  if (h > 0) return `${pad(h)}:${pad(m)}:${pad(s)}`;
  return `${pad(m)}:${pad(s)}`;
}

/**
 * Time left until the live candle closes, as a small tag on the price axis just under the last-price label
 * (where TradingView shows it). The chart calls `tick()` every second.
 */
export class CountdownPrimitive extends PrimitiveBase {
  private price: number | null = null;
  private close = 0;
  private color = "#1f2633";
  private readonly axis: readonly ISeriesPrimitiveAxisView[];
  private readonly none: readonly ISeriesPrimitivePaneView[] = [];

  constructor(mapper: TimeMapper) {
    super(mapper);
    this.axis = [
      {
        coordinate: () => this.at(),
        fixedCoordinate: () => this.at(), // see LabeledRayPrimitive: keeps the last-price label in place
        text: () => countdownText(this.close),
        textColor: () => "#e5e7eb",
        backColor: () => this.color,
        visible: () => this.price !== null && this.close > Date.now() / 1000,
        tickVisible: () => false,
      },
    ];
  }

  /** Just under the last-price label. */
  private at(): number {
    return this.price === null ? -100 : (this.y(this.price) ?? -100) + 19;
  }

  paneViews() {
    return this.none;
  }

  priceAxisViews() {
    return this.axis;
  }

  set(price: number | null, closeTime: number, up: boolean) {
    this.price = price;
    this.close = closeTime;
    this.color = up ? "#166534" : "#991b1b";
    this.requestUpdate();
  }

  tick() {
    this.requestUpdate();
  }
}
