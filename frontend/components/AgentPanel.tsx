"use client";

import clsx from "clsx";
import {
  Bell,
  Bot,
  Check,
  ClipboardCopy,
  Eraser,
  Footprints,
  Loader2,
  NotebookPen,
  Pin,
  PinOff,
  SendHorizontal,
  Target,
  Trash2,
  User,
} from "lucide-react";
import { Fragment, useEffect, useImperativeHandle, useRef, useState, type ReactNode, type Ref } from "react";

import { usePersistentState } from "@/hooks/usePersistentState";
import { displaySymbol, formatPct, formatPrice } from "@/lib/format";
import { DEFAULT_SIZING, orderText, qtyText, sizePlan, type SizingSettings } from "@/lib/sizing";
import type { Interval, Overlay, ScanResult, TradePlan, TriggerInterval } from "@/lib/types";

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
  /** The chart the answer was drawn on, and the question it answered (for pins and the journal). */
  symbol?: string;
  interval?: Interval;
  prompt?: string;
  lastPrice?: number;
}

export interface AgentPanelHandle {
  focus(): void;
}

const SUGGESTIONS = [
  "Identify the current H4 supply zone and key resistance high",
  "Which of my coins are near demand?",
  "Give me a long setup",
  "What does Kimi say?",
  "Find order blocks, FVGs and liquidity sweeps",
  "Open BTC daily and show key levels",
];

const FOLLOW_UPS = ["Also show swings", "Same on daily", "Does the daily agree?", "Alert me on these levels", "Add RSI"];

/** Prices in an answer in bold: numbers with decimals or thousands separators within ±60% of the last price. */
function emphasize(text: string, last: number | undefined): ReactNode {
  if (!last) return text;
  const re = /\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+/g;
  const out: ReactNode[] = [];
  let i = 0;
  for (const m of text.matchAll(re)) {
    const v = Number(m[0].replace(/,/g, ""));
    if (!(v > last * 0.4 && v < last * 1.6)) continue;
    out.push(text.slice(i, m.index), <strong key={m.index} className="font-semibold text-white">{m[0]}</strong>);
    i = (m.index ?? 0) + m[0].length;
  }
  out.push(text.slice(i));
  return out;
}

function PlanCard({
  plan,
  symbol,
  onLog,
  onTrigger,
}: {
  plan: TradePlan;
  symbol?: string;
  onLog?(): Promise<boolean>;
  /** Arms a trigger alert: a `tf` confirmation inside the plan's entry zone. */
  onTrigger?(tf: TriggerInterval): Promise<boolean>;
}) {
  const [sizing] = usePersistentState<SizingSettings>("ac:sizing", DEFAULT_SIZING);
  const [copied, setCopied] = useState(false);
  const [logged, setLogged] = useState<"idle" | "busy" | "done" | "error">("idle");
  const [triggerTf, setTriggerTf] = usePersistentState<TriggerInterval>("ac:plan-trigger-tf", "5m");
  const [armed, setArmed] = useState<"idle" | "busy" | "done" | "error">("idle");
  const zone = plan.zone_low != null && plan.zone_high != null ? [plan.zone_low, plan.zone_high] : null;
  const long = plan.direction === "long";
  const sized = sizePlan(plan, sizing);
  return (
    <div className="mt-1.5 rounded-md border border-line bg-base/60 p-2 text-[11px]">
      <div className="mb-1 flex items-center gap-1.5">
        <Target className={clsx("h-3.5 w-3.5", long ? "text-up" : "text-down")} />
        <span className={clsx("font-semibold", long ? "text-up" : "text-down")}>{long ? "Long" : "Short"} plan</span>
        <span className="truncate text-mute">from {plan.basis}</span>
      </div>
      <div className="grid grid-cols-[auto_1fr_auto_auto] gap-x-3 gap-y-0.5 font-mono">
        <span className="text-mute">Entry</span>
        <span className="text-accent">{formatPrice(plan.entry)}</span>
        <span />
        <span />
        <span className="text-mute">Stop</span>
        <span className="text-down">{formatPrice(plan.stop)}</span>
        <span className="text-mute">−{plan.risk_pct}%</span>
        <span className="text-down">{sized ? `−$${sized.riskUsd.toFixed(2)}` : ""}</span>
        {plan.targets.map((t, i) => (
          <Fragment key={t.label}>
            <span className="text-mute">{t.label.split(" ")[0]}</span>
            <span className="text-up">{formatPrice(t.price)}</span>
            <span className="text-ink">{t.rr}R</span>
            <span className="text-up">{sized ? `+$${sized.targets[i].pnlUsd.toFixed(2)}` : ""}</span>
          </Fragment>
        ))}
      </div>
      {sized && symbol && (
        <div className="mt-1.5 border-t border-line pt-1.5 text-mute">
          Size <span className="font-mono text-ink">{qtyText(sized.qty)}</span> (~${sized.notional.toFixed(0)}) for{" "}
          {sizing.riskPct}% risk of ${sizing.account.toLocaleString()}
          {sized.leverage > 1 && <> · needs {sized.leverage.toFixed(1)}x</>} · fees ~${sized.feesUsd.toFixed(2)}
        </div>
      )}
      {sized?.warnings.map((w) => (
        <p key={w} className="mt-1 text-yellow-300">{w}</p>
      ))}
      {plan.notes.map((n) => (
        <p key={n} className="mt-1 text-mute">{n}</p>
      ))}
      {symbol && (
        <div className="mt-1.5 flex flex-wrap gap-1">
          <button
            type="button"
            className="btn-ghost h-6 border border-line px-1.5 text-[11px]"
            title="Copy entry, stop, targets and size as one line"
            onClick={() => {
              void navigator.clipboard?.writeText(orderText(plan, symbol, sized, sizing)).then(() => {
                setCopied(true);
                setTimeout(() => setCopied(false), 1500);
              });
            }}
          >
            {copied ? <Check className="h-3.5 w-3.5 text-up" /> : <ClipboardCopy className="h-3.5 w-3.5" />}
            {copied ? "Copied" : "Copy order"}
          </button>
          {onLog && (
            <button
              type="button"
              disabled={logged === "busy" || logged === "done"}
              className="btn-ghost h-6 border border-line px-1.5 text-[11px] disabled:opacity-60"
              title="Track this plan in the trade journal"
              onClick={async () => {
                setLogged("busy");
                setLogged((await onLog()) ? "done" : "error");
              }}
            >
              {logged === "done" ? <Check className="h-3.5 w-3.5 text-up" /> : <NotebookPen className="h-3.5 w-3.5" />}
              {logged === "done" ? "In journal" : logged === "error" ? "Not saved, retry" : "Log trade"}
            </button>
          )}
          {onTrigger && zone && (
            <span className="inline-flex items-center rounded border border-line">
              <button
                type="button"
                disabled={armed === "busy" || armed === "done"}
                className="btn-ghost h-6 px-1.5 text-[11px] disabled:opacity-60"
                title={`Alert once per touch when a ${triggerTf} candle confirms the ${long ? "bounce" : "rejection"} inside ${formatPrice(zone[0])}–${formatPrice(zone[1])}`}
                onClick={async () => {
                  setArmed("busy");
                  setArmed((await onTrigger(triggerTf)) ? "done" : "error");
                }}
              >
                {armed === "done" ? <Check className="h-3.5 w-3.5 text-up" /> : <Bell className="h-3.5 w-3.5" />}
                {armed === "done" ? "Trigger set" : armed === "error" ? "Not saved, retry" : `Alert on ${triggerTf} confirmation`}
              </button>
              <select
                aria-label="Trigger timeframe"
                className="h-6 border-l border-line bg-transparent px-0.5 text-[11px] text-mute outline-none"
                value={triggerTf}
                onChange={(e) => {
                  setTriggerTf(e.target.value as TriggerInterval);
                  setArmed("idle");
                }}
              >
                <option value="1m">1m</option>
                <option value="5m">5m</option>
                <option value="15m">15m</option>
              </select>
            </span>
          )}
        </div>
      )}
    </div>
  );
}

function ScanTable({ rows, onPick }: { rows: ScanResult[]; onPick(symbol: string): void }) {
  return (
    <div className="mt-1.5 overflow-hidden rounded-md border border-line">
      {rows.slice(0, 10).map((r) => (
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
  busy: boolean;
  messages: AgentMessage[];
  overlayCount: number;
  /** Ids of answers whose drawings are pinned to their chart. */
  pinned: Set<string>;
  onSubmit(prompt: string): void;
  onClearOverlays(): void;
  onClearChat(): void;
  onPickSymbol(symbol: string): void;
  onTogglePin(message: AgentMessage): void;
  /** Adds an answer's plan to the trade journal → saved. */
  onLogTrade?(message: AgentMessage): Promise<boolean>;
  /** Arms a lower-timeframe trigger alert on an answer's plan zone → saved. */
  onPlanTrigger?(message: AgentMessage, tf: TriggerInterval): Promise<boolean>;
  handleRef?: Ref<AgentPanelHandle>;
}

/** The chart agent as a dock tab: the conversation fills the height, the prompt sits at the bottom. */
export default function AgentPanel(p: Props) {
  const [value, setValue] = useState("");
  const listRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useImperativeHandle(p.handleRef, () => ({ focus: () => inputRef.current?.focus() }));

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" });
  }, [p.messages.length, p.busy]);

  const submit = (text: string) => {
    const t = text.trim();
    if (!t || p.busy) return;
    p.onSubmit(t);
    setValue("");
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center gap-1 border-b border-line px-3 py-1.5 text-[11px] text-mute">
        <span className="truncate">Draws levels, plans trades, switches charts, scans your watchlist</span>
        <div className="flex-1" />
        {p.overlayCount > 0 && (
          <button type="button" onClick={p.onClearOverlays} className="btn-ghost h-6 shrink-0 gap-1 px-1.5 text-[11px]" title="Remove the agent's drawings from this chart (Ctrl+Z brings them back)">
            <Eraser className="h-3.5 w-3.5" /> Clear {p.overlayCount}
          </button>
        )}
        {p.messages.length > 0 && (
          <button type="button" onClick={p.onClearChat} className="btn-ghost h-6 w-6 shrink-0 p-0" title="Start a new conversation" aria-label="Clear conversation">
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        )}
      </div>

      <div ref={listRef} className="min-h-0 flex-1 space-y-3 overflow-y-auto px-3 py-3 text-[13px] leading-relaxed">
        {p.messages.length === 0 && !p.busy && (
          <p className="text-[12px] text-mute">
            Ask in plain English. The agent finds zones, swings and liquidity, builds trade plans, sets alerts, reads
            Kimi Cooked and your watchlist. Every price it draws comes from the detectors, never from the model.
          </p>
        )}
        {p.messages.map((m) => {
          const isPinned = p.pinned.has(m.id);
          const drawable = m.role === "agent" && !!m.overlays?.length && !!m.symbol;
          return (
            <div key={m.id} className="group flex gap-2">
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
                <p className={clsx(m.role === "user" ? "text-mute" : m.role === "error" ? "text-down" : "text-ink")}>
                  {m.role === "agent" ? emphasize(m.text, m.lastPrice) : m.text}
                </p>
                {m.plan && (
                  <PlanCard
                    plan={m.plan}
                    symbol={m.symbol}
                    onLog={p.onLogTrade ? () => p.onLogTrade!(m) : undefined}
                    onTrigger={p.onPlanTrigger ? (tf) => p.onPlanTrigger!(m, tf) : undefined}
                  />
                )}
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
                <div className="mt-1 flex items-center gap-2">
                  {m.meta && <p className="text-[10px] text-mute">{m.meta}</p>}
                  {drawable && (
                    <button
                      type="button"
                      onClick={() => p.onTogglePin(m)}
                      className={clsx(
                        "ml-auto inline-flex shrink-0 items-center gap-1 rounded px-1 py-0.5 text-[10px]",
                        isPinned ? "bg-accent/15 text-accent" : "text-mute opacity-70 hover:bg-panel2 hover:text-ink group-hover:opacity-100",
                      )}
                      title={isPinned ? "Unpin: these drawings go when the next answer replaces them" : `Keep these drawings on ${displaySymbol(m.symbol!)} when you ask something else`}
                    >
                      {isPinned ? <PinOff className="h-3 w-3" /> : <Pin className="h-3 w-3" />}
                      {isPinned ? "Pinned" : "Pin"}
                    </button>
                  )}
                </div>
              </div>
            </div>
          );
        })}
        {p.busy && (
          <div className="flex items-center gap-2 text-mute">
            <Loader2 className="h-4 w-4 animate-spin" /> Analysing market structure…
          </div>
        )}
      </div>

      {!p.busy && (
        <div className="flex shrink-0 flex-wrap gap-1.5 px-3 pt-2">
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
        className="flex shrink-0 items-end gap-2 p-3"
        onSubmit={(e) => {
          e.preventDefault();
          submit(value);
        }}
      >
        <textarea
          ref={inputRef}
          rows={2}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit(value);
            }
          }}
          placeholder={p.messages.length ? 'Follow up, e.g. "same on ETH", "line at 25.4" or "alert me at the entry"' : 'Ask the agent, e.g. "Which of my coins are near demand?"'}
          className="min-h-9 flex-1 resize-none rounded-lg border border-line bg-base px-3 py-2 text-sm text-ink outline-none placeholder:text-mute focus:border-accent/60"
          aria-label="Agent prompt"
        />
        <button
          type="submit"
          disabled={p.busy || !value.trim()}
          className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-accent text-white transition-opacity disabled:opacity-40"
          aria-label="Send"
        >
          {p.busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <SendHorizontal className="h-4 w-4" />}
        </button>
      </form>
    </div>
  );
}
