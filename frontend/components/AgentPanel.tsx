"use client";

import clsx from "clsx";
import { Bell, Bot, ChevronDown, Eraser, Loader2, SendHorizontal, Sparkles, Trash2, User } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import type { Overlay } from "@/lib/types";

export interface AgentMessage {
  id: string;
  role: "user" | "agent" | "error";
  text: string;
  overlays?: Overlay[];
  meta?: string;
  alerts?: number;
}

const SUGGESTIONS = [
  "Identify the current H4 supply zone and key resistance high",
  "Show daily support and resistance",
  "Mark swing highs and lows with market structure",
  "Full analysis: zones, windows, trendlines",
];

const FOLLOW_UPS = ["Also show swings", "Same on daily", "Remove the trendlines", "Alert me on these levels"];

function swatch(o: Overlay) {
  if (o.type === "box") return o.border_color ?? o.color;
  return o.color;
}

interface Props {
  open: boolean;
  busy: boolean;
  messages: AgentMessage[];
  overlayCount: number;
  onSubmit(prompt: string): void;
  onClearOverlays(): void;
  onClearChat(): void;
  onClose(): void;
}

export default function AgentPanel(p: Props) {
  const [value, setValue] = useState("");
  const listRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" });
  }, [p.messages.length, p.busy, p.open]);

  useEffect(() => {
    if (p.open) inputRef.current?.focus();
  }, [p.open]);

  const submit = (text: string) => {
    const t = text.trim();
    if (!t || p.busy) return;
    p.onSubmit(t);
    setValue("");
  };

  return (
    <div
      className={clsx(
        "pointer-events-auto absolute bottom-10 left-1/2 z-30 flex w-[min(640px,calc(100%-24px))] -translate-x-1/2 flex-col overflow-hidden rounded-xl border border-line bg-panel/95 shadow-2xl backdrop-blur transition-all",
        !p.open && "hidden",
      )}
    >
      <div className="flex items-center gap-2 border-b border-line px-3 py-2 text-xs">
        <Sparkles className="h-4 w-4 text-accent" />
        <span className="font-semibold text-ink">Chart Agent</span>
        <span className="text-mute">draws zones and levels from your request</span>
        <div className="flex-1" />
        {p.overlayCount > 0 && (
          <button type="button" onClick={p.onClearOverlays} className="btn-ghost h-6 gap-1 px-1.5 text-[11px]" title="Remove AI overlays">
            <Eraser className="h-3.5 w-3.5" /> Clear {p.overlayCount}
          </button>
        )}
        {p.messages.length > 0 && (
          <button type="button" onClick={p.onClearChat} className="btn-ghost h-6 w-6 p-0" title="Start a new conversation" aria-label="Clear conversation">
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        )}
        <button type="button" onClick={p.onClose} className="btn-ghost h-6 w-6 p-0" aria-label="Minimize agent">
          <ChevronDown className="h-4 w-4" />
        </button>
      </div>

      {(p.messages.length > 0 || p.busy) && (
        <div ref={listRef} className="max-h-60 space-y-3 overflow-y-auto px-3 py-3 text-[13px] leading-relaxed">
          {p.messages.map((m) => (
            <div key={m.id} className="flex gap-2">
              <div
                className={clsx(
                  "mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-full",
                  m.role === "user" ? "bg-panel2 text-mute" : m.role === "error" ? "bg-down/20 text-down" : "bg-accent/20 text-accent",
                )}
              >
                {m.role === "user" ? <User className="h-3 w-3" /> : <Bot className="h-3 w-3" />}
              </div>
              <div className="min-w-0 flex-1">
                <p className={clsx(m.role === "user" ? "text-mute" : m.role === "error" ? "text-down" : "text-ink")}>{m.text}</p>
                {m.overlays && m.overlays.length > 0 && (
                  <div className="mt-1.5 flex flex-wrap gap-1">
                    {m.overlays
                      .filter((o) => o.type !== "marker")
                      .map((o, i) => (
                        <span key={o.id ?? i} className="inline-flex items-center gap-1 rounded bg-panel2 px-1.5 py-0.5 text-[11px] text-ink/80">
                          <span className="h-2 w-2 rounded-sm" style={{ background: swatch(o) }} />
                          {o.label}
                        </span>
                      ))}
                  </div>
                )}
                {m.alerts ? (
                  <p className="mt-1 inline-flex items-center gap-1 text-[11px] text-yellow-300">
                    <Bell className="h-3 w-3" /> {m.alerts} alert{m.alerts === 1 ? "" : "s"} armed
                  </p>
                ) : null}
                {m.meta && <p className="mt-1 text-[10px] text-mute">{m.meta}</p>}
              </div>
            </div>
          ))}
          {p.busy && (
            <div className="flex items-center gap-2 text-mute">
              <Loader2 className="h-4 w-4 animate-spin" /> Analysing market structure…
            </div>
          )}
        </div>
      )}

      {!p.busy && (p.messages.length === 0 || p.overlayCount > 0) && (
        <div className="flex flex-wrap gap-1.5 px-3 pt-3">
          {(p.messages.length === 0 ? SUGGESTIONS : FOLLOW_UPS).map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => submit(s)}
              className="rounded-full border border-line px-2.5 py-1 text-[11px] text-mute transition-colors hover:border-accent/50 hover:text-ink"
            >
              {s}
            </button>
          ))}
        </div>
      )}

      <form
        className="flex items-center gap-2 p-3"
        onSubmit={(e) => {
          e.preventDefault();
          submit(value);
        }}
      >
        <input
          ref={inputRef}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder={p.messages.length ? 'Follow up, e.g. "line at 25.4" or "alert me if it enters the zone"' : 'Ask the agent, e.g. "Identify the current H4 supply zone"'}
          className="h-9 flex-1 rounded-lg border border-line bg-base px-3 text-sm text-ink outline-none placeholder:text-mute focus:border-accent/60"
          aria-label="Agent prompt"
        />
        <button
          type="submit"
          disabled={p.busy || !value.trim()}
          className="grid h-9 w-9 place-items-center rounded-lg bg-accent text-white transition-opacity disabled:opacity-40"
          aria-label="Send"
        >
          {p.busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <SendHorizontal className="h-4 w-4" />}
        </button>
      </form>
    </div>
  );
}
