"use client";

import clsx from "clsx";
import {
  Bell,
  Bot,
  Check,
  ClipboardCopy,
  Coins,
  Eraser,
  ArrowLeft,
  Footprints,
  ExternalLink,
  History,
  Layers,
  Loader2,
  NotebookPen,
  Pin,
  PinOff,
  Search,
  SendHorizontal,
  SquarePen,
  Target,
  Trash2,
  User,
} from "lucide-react";
import { Fragment, useEffect, useImperativeHandle, useRef, useState, type ReactNode, type Ref } from "react";

import { usePersistentState } from "@/hooks/usePersistentState";
import { searchSessions, type ChatSession } from "@/lib/chatHistory";
import { displaySymbol, formatPct, formatPrice } from "@/lib/format";
import { DEFAULT_SIZING, orderText, qtyText, sizePlan, type SizingSettings } from "@/lib/sizing";
import { openDockPanel } from "@/lib/dock";
import { ladderToPaperOrders, planToPaperOrders, placePaperOrders, type NewPaperOrder } from "@/lib/paper";
import { ladderTestLine } from "@/lib/spot";
import {
  TIMEFRAMES,
  type AnswerSource,
  type GridCoin,
  type Interval,
  type LadderResult,
  type MarketSetup,
  type Overlay,
  type ScanResult,
  type SellSignal,
  type SellWatch,
  type TopDownResult,
  type TradePlan,
  type TriggerInterval,
} from "@/lib/types";

import { GridCoinList, SetupList } from "./ScannerPanel";
import TrackRecordLine from "./TrackRecordLine";

export interface AgentMessage {
  id: string;
  role: "user" | "agent" | "error";
  text: string;
  overlays?: Overlay[];
  meta?: string;
  alerts?: number;
  plan?: TradePlan;
  scan?: ScanResult[];
  /** Market-wide scanner setups ("best 5m setups right now"). */
  setups?: MarketSetup[];
  steps?: string[];
  /** Coins ranging well enough for a Spot Grid bot. */
  gridCoins?: GridCoin[];
  /** A top-down S/R walk: what was drawn or skipped on each timeframe. */
  walk?: TopDownResult;
  /** A spot buy-the-dip ladder with its backtest. */
  ladder?: LadderResult;
  /** Coins to sell or trim. */
  sells?: SellSignal[];
  /** The coins and timeframe that sell check covered. */
  sellWatch?: SellWatch;
  /** Web pages a general answer was based on. */
  sources?: AnswerSource[];
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
  "Best 4h setups across the market",
  "What does Kimi say?",
  "Find order blocks, FVGs and liquidity sweeps",
  "Open BTC daily and show key levels",
];

/** One-tap shortcuts, always shown above the prompt. */
const SHORTCUTS = [
  "Top-down S/R walk",
  "Best spot buys",
  "Suggest a grid bot for this coin",
  "Best grid bot coins",
  "Where do I take profit?",
  "Plan a dip-buy ladder",
  "What should I sell or trim?",
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
      {plan.track_record && <TrackRecordLine tr={plan.track_record} />}
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
          {long && sized && sized.qty > 0 && (
            <PaperBuyButton
              orders={() => planToPaperOrders(plan, symbol, sized.qty)}
              title={`Paper trade it: limit buy ${qtyText(sized.qty)} at ${formatPrice(plan.entry)}, stop and targets as sell orders`}
            />
          )}
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

function WalkCard({ walk }: { walk: TopDownResult }) {
  return (
    <div className="mt-1.5 overflow-hidden rounded-md border border-line text-[11px]">
      {walk.steps.map((s) => (
        <div key={s.interval} className="flex items-start gap-2 border-b border-line px-2 py-1 last:border-b-0">
          <span className={clsx("w-8 shrink-0 font-mono font-semibold", s.status === "drawn" ? "text-accent" : "text-mute")}>{s.label}</span>
          <span className="min-w-0 flex-1 text-mute">
            {s.status === "drawn" ? (
              <>
                <span className="text-ink">{s.zones.length} level{s.zones.length === 1 ? "" : "s"}</span>
                {s.summary ? ` · ${s.summary}` : ""}
              </>
            ) : (
              <>
                {s.status === "skipped" ? "Skipped" : "Failed"}: {s.reason}
              </>
            )}
          </span>
        </div>
      ))}
    </div>
  );
}

function LadderCard({ ladder }: { ladder: LadderResult }) {
  const pl = ladder.plan;
  const test = ladderTestLine(ladder);
  return (
    <div className="mt-1.5 rounded-md border border-line bg-base/60 p-2 text-[11px]">
      <div className="mb-1 flex items-center gap-1.5">
        <Layers className="h-3.5 w-3.5 text-up" />
        <span className="font-semibold text-up">Dip-buy ladder</span>
        <span className="truncate text-mute">
          {displaySymbol(pl.symbol)} {pl.timeframe} · ${pl.budget.toLocaleString()}
        </span>
      </div>
      <div className="grid grid-cols-[auto_1fr_auto_auto] gap-x-3 gap-y-0.5 font-mono">
        {pl.rungs.map((r, i) => (
          <Fragment key={i}>
            <span className="text-mute">Buy {i + 1}</span>
            <span className="text-accent" title={r.basis}>{formatPrice(r.price)}</span>
            <span className="text-mute">−{r.distance_pct.toFixed(1)}%</span>
            <span className="text-ink">${r.amount.toFixed(0)}</span>
          </Fragment>
        ))}
        <span className="text-mute">Sell</span>
        <span className="text-up" title={pl.tp_basis}>{formatPrice(pl.take_profit)}</span>
        <span className="text-up">+{pl.tp_gain_pct.toFixed(1)}%</span>
        <span />
        <span className="text-mute">Invalid</span>
        <span className="text-down">{formatPrice(pl.invalidation)}</span>
        <span />
        <span />
      </div>
      {test && <p className="mt-1.5 border-t border-line pt-1.5 text-mute">{test}</p>}
      {pl.notes.map((n) => (
        <p key={n} className="mt-1 text-mute">{n}</p>
      ))}
      <div className="mt-1.5 flex flex-wrap gap-1">
        <PaperBuyButton
          orders={() => ladderToPaperOrders(ladder)}
          title={`Paper trade it: ${pl.rungs.length} limit buys for $${pl.budget.toLocaleString()} and a sell at ${formatPrice(pl.take_profit)}`}
          label="Paper buy ladder"
        />
      </div>
    </div>
  );
}

/** Places a plan or ladder in the paper wallet and opens the Paper trading tab. */
function PaperBuyButton({ orders, title, label = "Paper buy" }: { orders(): NewPaperOrder[]; title: string; label?: string }) {
  const [state, setState] = useState<"idle" | "busy" | "done">("idle");
  const [error, setError] = useState<string | null>(null);
  return (
    <>
      <button
        type="button"
        disabled={state !== "idle"}
        className="btn-ghost h-6 border border-line px-1.5 text-[11px] disabled:opacity-60"
        title={title}
        onClick={async () => {
          setState("busy");
          try {
            await placePaperOrders(orders());
            setState("done");
            setError(null);
            openDockPanel("paper");
          } catch (err) {
            setState("idle");
            setError((err as Error).message);
          }
        }}
      >
        {state === "done" ? <Check className="h-3.5 w-3.5 text-up" /> : <Coins className="h-3.5 w-3.5" />}
        {state === "done" ? "In paper wallet" : label}
      </button>
      {error && <p className="w-full text-down">{error}</p>}
    </>
  );
}

function SellList({ rows, onPick }: { rows: SellSignal[]; onPick(symbol: string): void }) {
  return (
    <div className="mt-1.5 overflow-hidden rounded-md border border-line">
      {rows.slice(0, 8).map((r) => (
        <button
          key={r.symbol}
          type="button"
          onClick={() => onPick(r.symbol)}
          className="flex w-full items-start gap-2 border-b border-line px-2 py-1 text-left text-[11px] last:border-b-0 hover:bg-panel2"
          title={r.reason}
        >
          <span className="w-20 shrink-0 font-medium text-ink">{displaySymbol(r.symbol)}</span>
          <span className={clsx("w-9 shrink-0 font-semibold uppercase", r.action === "sell" ? "text-down" : "text-yellow-300")}>
            {r.action}
          </span>
          <span className="min-w-0 flex-1 text-mute">
            <span className="font-mono text-ink">
              {formatPrice(r.sell_low)}–{formatPrice(r.sell_high)}
            </span>{" "}
            {r.zone}
            {r.support_below != null && r.drop_pct != null && (
              <>
                {" "}
                · next support <span className="font-mono">{formatPrice(r.support_below)}</span> ({r.drop_pct.toFixed(1)}%)
              </>
            )}
          </span>
        </button>
      ))}
    </div>
  );
}

/** "Alert me on these": arms lost-support and rejected-at-resistance signal alerts on the coins a sell check
 * covered, so the check keeps running on every candle close and pings you (Telegram / Discord when set up). */
function SellWatchButton({ watch, onWatch }: { watch: SellWatch; onWatch(w: SellWatch): Promise<boolean> }) {
  const [state, setState] = useState<"idle" | "busy" | "done" | "error">("idle");
  const n = watch.symbols.length;
  const coins = n === 1 ? displaySymbol(watch.symbols[0]) : `these ${n} coins`;
  return (
    <button
      type="button"
      disabled={state === "busy" || state === "done"}
      className="btn-ghost mt-1.5 h-6 border border-line px-1.5 text-[11px] disabled:opacity-60"
      title={`Alert on every ${watch.interval} close when ${n === 1 ? "it loses" : "one of them loses"} support or is rejected at resistance`}
      onClick={async () => {
        setState("busy");
        setState((await onWatch(watch)) ? "done" : "error");
      }}
    >
      {state === "done" ? <Check className="h-3.5 w-3.5 text-up" /> : <Bell className="h-3.5 w-3.5" />}
      {state === "done"
        ? `Watching ${coins} on ${watch.interval}`
        : state === "error"
          ? "Not saved, retry"
          : `Alert me when ${coins} need selling (${watch.interval})`}
    </button>
  );
}

function Sources({ rows }: { rows: AnswerSource[] }) {
  return (
    <div className="mt-1.5 space-y-0.5 text-[11px]">
      {rows.slice(0, 6).map((s) => (
        <a key={s.url} href={s.url} target="_blank" rel="noreferrer" className="flex items-center gap-1 truncate text-mute hover:text-accent">
          <ExternalLink className="h-3 w-3 shrink-0" />
          <span className="truncate">{s.title || s.source || s.url}</span>
        </a>
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
  /** Files the conversation in the history and starts an empty one. */
  onNewChat(): void;
  chats: ChatSession[];
  onOpenChat(id: string): void;
  onDeleteChat(id: string): void;
  onPickSymbol(symbol: string): void;
  /** A setup from a market scan: open its chart with the plan drawn. */
  onOpenSetup?(setup: MarketSetup): void;
  /** A grid coin: open its chart with its range drawn. */
  onOpenGridCoin?(coin: GridCoin): void;
  /** Spot only: no short plans, scans show longs, spot buys and grid coins. */
  spotOnly: boolean;
  onSpotOnly(v: boolean): void;
  /** What a top-down walk is drawing right now ("D1: 2 levels"). */
  walkNote?: string | null;
  onTogglePin(message: AgentMessage): void;
  /** Adds an answer's plan to the trade journal → saved. */
  onLogTrade?(message: AgentMessage): Promise<boolean>;
  /** Arms a lower-timeframe trigger alert on an answer's plan zone → saved. */
  onPlanTrigger?(message: AgentMessage, tf: TriggerInterval): Promise<boolean>;
  /** Arms the sell signal alerts on the coins a sell check covered → saved. */
  onSellWatch?(watch: SellWatch): Promise<boolean>;
  handleRef?: Ref<AgentPanelHandle>;
}

/** The chart agent as a dock tab: the conversation fills the height, the prompt sits at the bottom. */
export default function AgentPanel(p: Props) {
  const [value, setValue] = useState("");
  const [showHistory, setShowHistory] = useState(false);
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
        <button
          type="button"
          onClick={() => p.onSpotOnly(!p.spotOnly)}
          className={clsx("btn-ghost h-6 shrink-0 gap-1 border px-1.5 text-[11px]", p.spotOnly ? "border-up/50 text-up" : "border-line")}
          title={p.spotOnly ? "Spot only: no shorts, futures or leverage. Click to allow short plans." : "Short plans allowed. Click for spot only."}
          aria-pressed={p.spotOnly}
        >
          {p.spotOnly ? "Spot only" : "Spot + futures"}
        </button>
        {p.overlayCount > 0 && (
          <button type="button" onClick={p.onClearOverlays} className="btn-ghost h-6 shrink-0 gap-1 px-1.5 text-[11px]" title="Remove the agent's drawings from this chart (Ctrl+Z brings them back)">
            <Eraser className="h-3.5 w-3.5" /> Clear {p.overlayCount}
          </button>
        )}
        <button
          type="button"
          onClick={() => setShowHistory((v) => !v)}
          className={clsx("btn-ghost h-6 shrink-0 gap-1 px-1.5 text-[11px]", showHistory && "text-accent")}
          title="Past conversations"
          aria-pressed={showHistory}
        >
          <History className="h-3.5 w-3.5" /> {p.chats.length || ""}
        </button>
        {p.messages.length > 0 && (
          <button
            type="button"
            onClick={() => {
              p.onNewChat();
              setShowHistory(false);
            }}
            className="btn-ghost h-6 w-6 shrink-0 p-0"
            title="New conversation (this one is kept in the history)"
            aria-label="New conversation"
          >
            <SquarePen className="h-3.5 w-3.5" />
          </button>
        )}
      </div>

      {showHistory ? (
        <ChatHistory
          chats={p.chats}
          onBack={() => setShowHistory(false)}
          onOpen={(id) => {
            p.onOpenChat(id);
            setShowHistory(false);
          }}
          onDelete={p.onDeleteChat}
        />
      ) : (
        <>
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
                    {m.setups && (
                      <div className="mt-1.5">
                        <SetupList rows={m.setups} onPick={(s) => (p.onOpenSetup ? p.onOpenSetup(s) : p.onPickSymbol(s.symbol))} />
                      </div>
                    )}
                    {m.walk && <WalkCard walk={m.walk} />}
                    {m.ladder && <LadderCard ladder={m.ladder} />}
                    {m.gridCoins && (
                      <div className="mt-1.5">
                        <GridCoinList rows={m.gridCoins} onPick={(c) => (p.onOpenGridCoin ? p.onOpenGridCoin(c) : p.onPickSymbol(c.symbol))} />
                      </div>
                    )}
                    {m.sells && <SellList rows={m.sells} onPick={p.onPickSymbol} />}
                    {m.sellWatch && p.onSellWatch && <SellWatchButton watch={m.sellWatch} onWatch={p.onSellWatch} />}
                    {m.sources && <Sources rows={m.sources} />}
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
                <Loader2 className="h-4 w-4 animate-spin" /> {p.walkNote ?? "Analysing market structure…"}
              </div>
            )}
          </div>

          <div className="flex shrink-0 gap-1.5 overflow-x-auto px-3 pt-2">
            {SHORTCUTS.map((s) => (
              <button
                key={s}
                type="button"
                disabled={p.busy}
                onClick={() => submit(s)}
                className="shrink-0 rounded-full border border-accent/40 bg-accent/10 px-2.5 py-1 text-[11px] text-ink transition-colors hover:border-accent disabled:opacity-50"
              >
                {s}
              </button>
            ))}
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
        </>
      )}
    </div>
  );
}

function ago(ms: number): string {
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 7 * 86400) return `${Math.floor(s / 86400)}d ago`;
  return new Date(ms).toLocaleDateString();
}

/** Past conversations, newest first: open one to read it and carry on, or delete it. */
function ChatHistory(p: { chats: ChatSession[]; onBack(): void; onOpen(id: string): void; onDelete(id: string): void }) {
  const [query, setQuery] = useState("");
  const shown = searchSessions(p.chats, query);
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex shrink-0 items-center gap-2 px-3 py-2">
        <button type="button" onClick={p.onBack} className="btn-ghost h-7 w-7 shrink-0 p-0" title="Back to the conversation" aria-label="Back">
          <ArrowLeft className="h-4 w-4" />
        </button>
        <label className="flex h-7 min-w-0 flex-1 items-center gap-1.5 rounded-md border border-line bg-base px-2 focus-within:border-accent/60">
          <Search className="h-3.5 w-3.5 shrink-0 text-mute" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search conversations"
            className="min-w-0 flex-1 bg-transparent text-[12px] text-ink outline-none placeholder:text-mute"
            aria-label="Search conversations"
          />
        </label>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-3">
        {shown.length === 0 && (
          <p className="pt-2 text-[12px] text-mute">
            {p.chats.length ? "No conversation matches." : "No past conversations yet. Starting a new one keeps the current one here."}
          </p>
        )}
        {shown.map((c) => {
          const tf = TIMEFRAMES.find((t) => t.value === c.interval)?.label ?? c.interval;
          return (
            <div key={c.id} className="group flex items-start gap-1 border-b border-line last:border-b-0">
              <button type="button" onClick={() => p.onOpen(c.id)} className="min-w-0 flex-1 py-2 text-left hover:text-white" title="Open and continue this conversation">
                <p className="truncate text-[12px] text-ink">{c.title}</p>
                <p className="text-[10px] text-mute">
                  {c.symbol ? `${displaySymbol(c.symbol)}${tf ? ` ${tf}` : ""} · ` : ""}
                  {c.messages.length} messages · {ago(c.updatedAt)}
                </p>
              </button>
              <button
                type="button"
                onClick={() => p.onDelete(c.id)}
                className="btn-ghost mt-1.5 h-6 w-6 shrink-0 p-0 opacity-60 group-hover:opacity-100"
                title="Delete this conversation"
                aria-label="Delete conversation"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}
