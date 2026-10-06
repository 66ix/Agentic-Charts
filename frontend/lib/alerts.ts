import { formatPrice } from "./format";
import type { AlertSpec, Drawing, Overlay, PriceAlert } from "./types";

export const ALERT_COLOR = "#fbbf24";

/** A horizontal ray or rectangle the user drew → an alert spec, or null for other drawings. */
export function alertFromDrawing(d: Drawing): AlertSpec | null {
  if (d.type === "hray" && d.points[0]) {
    const price = d.points[0].price;
    return { kind: "cross", price, price_low: null, price_high: null, label: `Ray ${formatPrice(price)}` };
  }
  if (d.type === "rect" && d.points.length >= 2) {
    const lo = Math.min(d.points[0].price, d.points[1].price);
    const hi = Math.max(d.points[0].price, d.points[1].price);
    return { kind: "zone", price: null, price_low: lo, price_high: hi, label: `Box ${formatPrice(lo)}–${formatPrice(hi)}` };
  }
  return null;
}

export function describeAlert(a: AlertSpec, fired = false): string {
  if (a.kind === "zone" && a.price_low != null && a.price_high != null) {
    return `${fired ? "entered" : "enters"} ${formatPrice(a.price_low)}–${formatPrice(a.price_high)}`;
  }
  return `${fired ? "crossed" : "crosses"} ${a.price != null ? formatPrice(a.price) : "?"}`;
}

/** Armed alerts for the chart's symbol, drawn as amber dotted lines / outlined boxes. */
export function alertOverlays(alerts: PriceAlert[], symbol: string): Overlay[] {
  const out: Overlay[] = [];
  for (const a of alerts) {
    if (!a.armed || a.symbol !== symbol) continue;
    if (a.kind === "zone" && a.price_low != null && a.price_high != null) {
      out.push({
        type: "box", id: `alert-${a.id}`, kind: "alert", label: `Alert: ${a.label}`, color: "rgba(251,191,36,0.06)",
        border_color: ALERT_COLOR, price_low: a.price_low, price_high: a.price_high,
      });
    } else if (a.price != null) {
      out.push({
        type: "horizontal_line", id: `alert-${a.id}`, kind: "alert", label: `Alert: ${a.label}`, color: ALERT_COLOR,
        price: a.price, line_style: "dotted", line_width: 1,
      });
    }
  }
  return out;
}
