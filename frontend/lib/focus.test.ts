import { describe, expect, it } from "vitest";
import type { AgentMessage } from "@/components/AgentPanel";
import { focusFrom } from "./focus";
import type { TradePlan } from "./types";

const NOW = 1_760_000_000_000;
const plan: TradePlan = {
  direction: "long", entry: 7.62, stop: 7.51, targets: [{ price: 7.9, label: "T1", rr: 2.5 }], basis: "H4 demand", risk_pct: 1.4,
  notes: [], zone_htf: [],
} as unknown as TradePlan;

const agent = (extra: Partial<AgentMessage>, agoMs = 60_000): AgentMessage => ({
  id: Math.random().toString(36), role: "agent", text: "x", symbol: "INJUSDT", interval: "4h", at: NOW - agoMs, ...extra,
});

describe("focusFrom", () => {
  it("takes the newest plan on this chart", () => {
    const f = focusFrom([agent({ plan })], "INJUSDT", "4h", NOW);
    expect(f?.kind).toBe("plan");
    expect(f?.plan?.stop).toBe(7.51);
    expect(f?.at).toBe(Math.floor((NOW - 60_000) / 1000));
  });

  it("falls back to a price asked about, then scan results", () => {
    expect(focusFrom([agent({ facts: { price_in_question: { price: 7.2 } } })], "INJUSDT", "4h", NOW)?.price).toBe(7.2);
    const scan = focusFrom([agent({ scan: [{ symbol: "SOLUSDT" }, { symbol: "ETHUSDT" }] as AgentMessage["scan"] })], "INJUSDT", "4h", NOW);
    expect(scan?.kind).toBe("scan");
    expect(scan?.symbols).toEqual(["SOLUSDT", "ETHUSDT"]);
  });

  it("is null when the newest answer is a day old, still streaming past, or about nothing", () => {
    expect(focusFrom([agent({ plan }, 25 * 3600_000)], "INJUSDT", "4h", NOW)).toBeNull();
    expect(focusFrom([agent({ plan }), agent({})], "INJUSDT", "4h", NOW)).toBeNull();
    expect(focusFrom([agent({ plan }), agent({ streaming: true, text: "" })], "INJUSDT", "4h", NOW)?.kind).toBe("plan");
  });

  it("skips answers about other coins", () => {
    expect(focusFrom([agent({ plan }), agent({ symbol: "SOLUSDT" })], "INJUSDT", "4h", NOW)?.kind).toBe("plan");
    expect(focusFrom([agent({ plan })], "SOLUSDT", "4h", NOW)).toBeNull();
  });
});
