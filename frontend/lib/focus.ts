// What "this" is in the conversation: the newest plan, asked-about price or scan the agent gave on this chart, sent
// with the next question so "what would invalidate this?" answers about it (backend/app/focus.py).

import type { AgentMessage } from "@/components/AgentPanel";
import { displaySymbol, formatPrice } from "./format";
import type { Focus, Interval } from "./types";

/** A focus older than this is stale. */
export const FOCUS_MAX_AGE_MS = 24 * 3600_000;

/** The newest agent answer on this chart (a plan first, then a price it was asked about, then scan results), or
 *  null when there is none or it is over a day old. */
export function focusFrom(messages: AgentMessage[], symbol: string, interval: Interval, nowMs = Date.now()): Focus | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m.role !== "agent" || m.streaming) continue;
    if (m.symbol && m.symbol !== symbol) continue;
    const atMs = m.at ?? 0;
    if (!atMs || nowMs - atMs > FOCUS_MAX_AGE_MS) return null;
    const base = { symbol, interval: m.interval ?? interval, at: Math.floor(atMs / 1000) };
    if (m.plan) {
      const p = m.plan;
      return { ...base, kind: "plan", plan: p, label: `${displaySymbol(symbol)} ${p.direction} ${formatPrice(p.entry)} → stop ${formatPrice(p.stop)}` };
    }
    const asked = (m.facts?.price_in_question as { price?: number } | undefined)?.price;
    if (typeof asked === "number" && asked > 0) {
      return { ...base, kind: "price", price: asked, label: `${displaySymbol(symbol)} at ${formatPrice(asked)}` };
    }
    const symbols = (m.setups?.map((s) => s.symbol) ?? m.scan?.map((s) => s.symbol) ?? []).slice(0, 10);
    if (symbols.length) {
      return { ...base, kind: "scan", symbols, label: `the scan: ${symbols.slice(0, 3).map(displaySymbol).join(", ")}${symbols.length > 3 ? "…" : ""}` };
    }
    // The newest answer on this chart is about something else: nothing is "this".
    return null;
  }
  return null;
}
