"use client";

import clsx from "clsx";
import { Bell, Bot, ChevronDown, Eraser, Footprints, Loader2, SendHorizontal, Sparkles, Target, Trash2, User } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { displaySymbol, formatPct, formatPrice } from "@/lib/format";
import type { Overlay, ScanResult, TradePlan } from "@/lib/types";

export interface AgentMessage {
  id: string;
  role: "user" | "agent" | "error";
  text: string;
  overlays?: Overlay[];
  meta?: string;
  alerts?: number;
  plan?: TradePlan;
  scan?: ScanResult[];
  steps?: string[];
}

const SUGGESTIONS = [
  "Identify the current H4 supply zone and key resistance high",
  "Which of my coins are near demand?",
  "Give me a long setup",
  "Find order blocks, FVGs and liquidity sweeps",
  "Open BTC daily and show key levels",
  "Full analysis: zones, windows, trendlines",
];

const FOLLOW_UPS = ["Also show swings", "Same on daily", "Does the daily agree?", "Alert me on these levels", "Add RSI"];

function PlanCard({ plan }: { plan: TradePlan }) {
  const long = plan.direction === "long";
  return (
    <div className="mt-1.5 rounded-md border border-line bg-base/60 p-2 text-[11px]">
      <div className="mb-1 flex items-center gap-1.5">
        <Target className={clsx("h-3.5 w-3.5", long ? "text-up" : "text-down")} />
        <span className={clsx("font-semibold", long ? "text-up" : "text-down")}>{long ? "Long" : "Short"} plan</span>
        <span className="truncate text-mute">from {plan.basis}</span>
      </div>
      <div className="grid grid-cols-[auto_1fr_auto] gap-x-3 gap-y-0.5 font-mono">
        <span className="text-mute">Entry</span>
        <span className="text-accent">{formatPrice(plan.entry)}</span>
        <span />
        <span className="text-mute">Stop</span>
        <span className="text-down">{formatPrice(plan.stop)}</span>
        <span className="text-mute">−{plan.risk_pct}%</span>
        {plan.targets.map((t) => (
          <span key={t.label} className="contents">
            <span className="text-mute">{t.label.split(" ")[0]}</span>
            <span className="text-up">{formatPrice(t.price)}</span>
            <span className="text-ink">{t.rr}R</span>
          </span>
        ))}
      </div>
      {plan.notes.map((n) => (
        <p key={n} className="mt-1 text-mute">{n}</p>
      ))}
    </div>
  );
}

function ScanTable({ rows, onPick }: { rows: ScanResult[]; onPick(symbol: string): void }) {
  return (
    <div className="mt-1.5 overflow-hidden rounded-md border border-line">
      {rows.slice(0, 8).map((r) => (
        <button
          key={r.symbol}
          type="button"
          onClick={() => onPick(r.symbol)}
          className="flex w-full items-start gap-2 border-b border-line px-2 py-1 text-left text-[11px] last:border-b-0 hover:bg-panel2"
          title={`Open ${displaySymbol(r.symbol)}`}
        >
          <span className="w-20 shrink-0 font-medium text-ink">{displaySymbol(r.symbol)}</span>
          <span className={clsx("w-14 shrink-0 font-mono", (r.change_pct ?? 0) >= 0 ? "text-up" : "text-down")}>
            {formatPct(r.change_pct)}
          </span>
          <span className="min-w-0 flex-1 truncate text-mute">{r.signals.slice(0, 2).join(" · ") || r.trend}</span>
        </button>
      ))}
    </div>
  );
}

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
  onPickSymbol(symbol: string): void;
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
        <span className="truncate text-mute">draws levels, switches charts, scans your watchlist</span>
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
        <div ref={listRef} className="max-h-72 space-y-3 overflow-y-auto px-3 py-3 text-[13px] leading-relaxed">
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
                {m.steps && (
                  <ul className="mb-1 space-y-0.5 text-[11px] text-mute">
                    {m.steps.map((s, i) => (
                      <li key={i} className="flex items-center gap-1">
                        <Footprints className="h-3 w-3" /> {s}
                      </li>
                    ))}
                  </ul>
                )}
                <p className={clsx(m.role === "user" ? "text-mute" : m.role === "error" ? "text-down" : "text-ink")}>{m.text}</p>
                {m.plan && <PlanCard plan={m.plan} />}
                {m.scan && <ScanTable rows={m.scan} onPick={p.onPickSymbol} />}
                {m.overlays && m.overlays.length > 0 && (
                  <div className="mt-1.5 flex flex-wrap gap-1">
                    {m.overlays
                      .filter((o) => o.type !== "marker" && o.label)
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
          placeholder={p.messages.length ? 'Follow up, e.g. "same on ETH", "line at 25.4" or "alert me at the entry"' : 'Ask the agent, e.g. "Which of my coins are near demand?"'}
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
