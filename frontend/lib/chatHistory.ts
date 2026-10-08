import type { AgentMessage } from "@/components/AgentPanel";
import type { AnalysisIntent, Interval } from "./types";

/** A past conversation with the chart agent, kept so it can be read again or picked up where it stopped. */
export interface ChatSession {
  id: string;
  title: string;
  symbol?: string;
  interval?: Interval;
  /** ms since epoch of the newest message. */
  updatedAt: number;
  messages: AgentMessage[];
  intent: AnalysisIntent | null;
}

export const CHATS_KEY = "ac:chats";
export const CHAT_ID_KEY = "ac:chat-id";
const MAX_CHATS = 50;

/** Whether a conversation holds anything worth keeping: a question the user asked. */
export function worthKeeping(messages: AgentMessage[]): boolean {
  return messages.some((m) => m.role === "user");
}

/** The conversation as a history entry: titled by its first question, filed under its newest chart. */
export function toSession(id: string, messages: AgentMessage[], intent: AnalysisIntent | null, now = Date.now()): ChatSession {
  const first = messages.find((m) => m.role === "user");
  const charted = [...messages].reverse().find((m) => m.symbol);
  const title = first?.text.trim() || "Auto-detected levels";
  return {
    id,
    title: title.length > 80 ? `${title.slice(0, 79)}…` : title,
    symbol: charted?.symbol,
    interval: charted?.interval,
    updatedAt: now,
    messages,
    intent,
  };
}

/** Puts a conversation at the top of the history (replacing an older copy of it), newest first, capped. */
export function upsertSession(list: ChatSession[], session: ChatSession): ChatSession[] {
  return [session, ...list.filter((s) => s.id !== session.id)].slice(0, MAX_CHATS);
}

/** Conversations whose title, chart or messages contain every word of the query. */
export function searchSessions(list: ChatSession[], query: string): ChatSession[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return list;
  return list.filter((s) => {
    const hay = [s.title, s.symbol ?? "", ...s.messages.map((m) => m.text)].join(" ").toLowerCase();
    return words.every((w) => hay.includes(w));
  });
}

/** The agent's newest answer on `symbol` in an earlier conversation, so a new question can follow up on it. */
export function lastAnswerOn(
  list: ChatSession[],
  symbol: string,
  excludeId: string,
): { time: number; prompt: string; summary: string; interval?: Interval; price?: number } | null {
  let best: { time: number; prompt: string; summary: string; interval?: Interval; price?: number } | null = null;
  for (const s of list) {
    if (s.id === excludeId) continue;
    s.messages.forEach((m, i) => {
      if (m.role !== "agent" || m.symbol !== symbol || !m.text.trim() || !m.prompt) return;
      const time = m.at ?? s.updatedAt - (s.messages.length - 1 - i) * 1000;
      if (!best || time > best.time) {
        best = { time, prompt: m.prompt.slice(0, 500), summary: m.text.slice(0, 1500), interval: m.interval, price: m.lastPrice };
      }
    });
  }
  return best;
}
