import { describe, expect, it } from "vitest";
import { parseDeepLink } from "./deepLink";

describe("parseDeepLink", () => {
  it("reads the chart and the tab item", () => {
    expect(parseDeepLink("?symbol=INJUSDT&tf=4h&focus=desk:abc")).toEqual({ symbol: "INJUSDT", interval: "4h", tab: "desk", id: "abc" });
    expect(parseDeepLink("?symbol=btcusdt&focus=signal:x1")).toEqual({ symbol: "BTCUSDT", tab: "alerts", id: "x1" });
  });
  it("ignores what it doesn't know", () => {
    expect(parseDeepLink("")).toBeNull();
    expect(parseDeepLink("?symbol=INJUSDT&tf=7m&focus=nope:1")).toEqual({ symbol: "INJUSDT" });
  });
});
