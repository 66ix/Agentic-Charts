import { describe, expect, it } from "vitest";

import type { AlertHistoryItem } from "./alerts";
import { tabTitle, toastFromHistory } from "./notifyClient";

const item = (over: Partial<AlertHistoryItem>): AlertHistoryItem => ({
  id: "1", time: 0, kind: "desk", symbol: "INJUSDT", title: "Desk call", text: "buy", ...over,
});

describe("toastFromHistory", () => {
  it("toasts desk, trade and market events with where to go", () => {
    expect(toastFromHistory(item({}))).toMatchObject({ kind: "desk", symbol: "INJUSDT", tab: "desk" });
    expect(toastFromHistory(item({ kind: "trade" }))).toMatchObject({ kind: "trade", tab: "trades" });
    const m = toastFromHistory(item({ kind: "signal", symbol: "MARKET" }));
    expect(m).toMatchObject({ kind: "metric", tab: "market" });
    expect(m?.symbol).toBeUndefined();
  });
  it("leaves alone what already toasts", () => {
    expect(toastFromHistory(item({ kind: "price" }))).toBeNull();
    expect(toastFromHistory(item({ kind: "signal" }))).toBeNull();
    expect(toastFromHistory(item({ kind: "brief", symbol: "" }))).toBeNull();
  });
});

describe("tabTitle", () => {
  it("shows unread, price, change and timeframe", () => {
    const t = tabTitle({ symbol: "INJUSDT", interval: "4h", price: 24.31, change24: 1.234, unread: 2 });
    expect(t.startsWith("(2) ")).toBe(true);
    expect(t).toContain("▲1.2%");
    expect(t.endsWith("· 4H")).toBe(true);
    expect(tabTitle({ symbol: "INJUSDT", interval: "1d", price: null, change24: -3, unread: 0 })).not.toContain("(");
  });
});
