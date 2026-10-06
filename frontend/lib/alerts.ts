import { formatPrice } from "./format";
import type { AlertSpec, Drawing, Overlay, PriceAlert } from "./types";

export const ALERT_COLOR = "#fbbf24";

const uid = () => Math.random().toString(36).slice(2, 10);

export function newAlert(spec: AlertSpec, symbol: string): PriceAlert {
  return { ...spec, id: uid(), symbol, armed: true, created_at: Date.now() };
}

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

function sideOf(a: PriceAlert, price: number): PriceAlert["last_side"] {
  if (a.kind === "zone") {
    if (a.price_low != null && a.price_high != null && price >= a.price_low && price <= a.price_high) return "inside";
    return price > (a.price_high ?? Infinity) ? "above" : "below";
  }
  return price >= (a.price ?? 0) ? "above" : "below";
}

/**
 * Check one armed alert against a new price. Fires on the transition only:
 * a cross alert when price moves to the other side of the level, a zone alert
 * when price moves into the zone (or jumps straight through it).
 */
export function evaluateAlert(a: PriceAlert, price: number): { alert: PriceAlert; fired: boolean } {
  if (!a.armed || !Number.isFinite(price)) return { alert: a, fired: false };
  const side = sideOf(a, price);
  const prev = a.last_side;
  let fired = false;
  if (prev && prev !== side) {
    fired = a.kind === "cross" || side === "inside" || prev !== "inside"; // above→below skips over the zone
  }
  if (fired) return { alert: { ...a, armed: false, last_side: side, triggered_at: Date.now(), triggered_price: price }, fired };
  return { alert: side === prev ? a : { ...a, last_side: side }, fired: false };
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
