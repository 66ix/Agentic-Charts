import { describe, expect, it } from "vitest";
import { deskChip, deskOverlays, type DeskCall } from "./desk";
import { overlayLayer, panelLayer } from "./layers";

const call = (over: Partial<DeskCall> = {}): DeskCall =>
  ({
    id: "c1", symbol: "INJUSDT", interval: "4h", shadow: false, created_at: 1_760_000_000, status: "waiting", data_source: "binance",
    zone_low: 7.4, zone_high: 7.5, entry: 7.5, stop: 7.3, tp: 7.9, rr: 2, confidence: 0.62, setup: "H4 demand",
    fill_price: null, ...over,
  }) as DeskCall;

describe("desk on the chart", () => {
  it("sits on its own layer", () => {
    expect(panelLayer("desk:active:INJUSDT")).toBe("desk");
    for (const o of deskOverlays(call())) expect(overlayLayer(o)).toBe("desk");
  });

  it("says the confidence is 'if filled', next to random odds, and flags uncalibrated numbers", () => {
    const chip = deskChip(call(), false);
    expect(chip.text).toBe("Desk 4H · waiting to buy · 62% if filled (random 33%) · uncalibrated");
    expect(chip.tone).toBe("mute");
    expect(deskChip(call({ status: "open" }), true).text).toBe("Desk 4H · holding · 62% if filled (random 33%)");
    expect(deskChip(call({ data_source: "synthetic" }), true).tone).toBe("demo");
  });

  it("puts the timeframe on every line", () => {
    const labels = deskOverlays(call({ fill_price: 7.45 })).map((o) => o.label ?? "");
    expect(labels.every((l) => l.includes("4H"))).toBe(true);
  });
});
