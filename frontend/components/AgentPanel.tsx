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
  ImagePlus,
} from "lucide-react";
import { Fragment, useEffect, useImperativeHandle, useRef, useState, type ReactNode, type Ref } from "react";

import { usePersistentState } from "@/hooks/usePersistentState";
import { searchSessions, type ChatSession } from "@/lib/chatHistory";
import { displaySymbol, formatPct, formatPrice } from "@/lib/format";
import { DEFAULT_SIZING, orderText, qtyText, sizePlan, type SizingSettings } from "@/lib/sizing";
import { openDockPanel } from "@/lib/dock";
import { ladderToPaperOrders, planToPaperOrders, placePaperOrders, type NewPaperOrder } from "@/lib/paper";
import { imageFrom } from "@/lib/screenshot";
import { ladderTestLine } from "@/lib/spot";
import {
  TIMEFRAMES,
  type AnswerDetail,
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
  /** The summary is still being written. */
  streaming?: boolean;
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
  /** The numbers the answer was written from, for "Numbers used". */
  facts?: Record<string, unknown>;
  /** The chart the answer was drawn on, and the question it answered (for pins and the journal). */
  symbol?: string;
  interval?: Interval;
  prompt?: string;
  lastPrice?: number;
  /** When it was answered (ms); older messages lack it. */
  at?: number;
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

const FOLLOW_UPS = ["Same on daily", "Does the daily agree?", "Give me a trade plan", "Alert me on these levels", "Any divergences?"];

/** Follow-up buttons that fit the last answer: a plan gets plan questions, a scan offers to plan its top coin. */
export function followUps(m: AgentMessage | undefined, spotOnly: boolean): string[] {
  if (!m || m.role !== "agent") return FOLLOW_UPS;
  if (m.plan) {
    return ["Alert me at the entry", "Where do I take profit?", "Does the daily agree?", "What would invalidate this?", "Alert me on a 5m confirmation in the zone"];
  }
  const top = m.setups?.[0];
  if (top) {
    const coin = displaySymbol(top.symbol);
    return [`Give me a ${top.direction} plan on ${coin}`, `Open ${coin} ${top.interval}`, "Same scan on 1h", "Same scan on daily"];
  }
  if (m.gridCoins?.[0]) {
    const coin = displaySymbol(m.gridCoins[0].symbol);
    return [`Suggest a grid bot for ${coin}`, "Best grid bot coins on daily", "Best spot buys"];
  }
  if (m.scan?.[0]) {
    const coin = displaySymbol(m.scan[0].symbol);
    return [`Give me a ${spotOnly ? "long" : "trade"} plan on ${coin}`, `Open ${coin}`, "Which are oversold?", "Same scan on daily"];
  }
  if (m.sells) return ["Where do I take profit?", "Plan a dip-buy ladder", "Best spot buys"];
  if (m.ladder) return ["Alert me on these levels", "Same ladder on daily", "Where do I take profit?"];
  if (m.walk) return ["Give me a trade plan", "Alert me on these levels", "Does the weekly agree?"];
  if (m.sources) return ["What does this mean for BTC?", "Any high-impact events today?", "Latest crypto headlines"];
  if (/\bkimi\b/i.test(m.prompt ?? "")) return ["Kimi on the daily", "Kimi on 1h", "Give me a trade plan", "Does the daily agree?"];
  const zone = ((m.facts?.indicators ?? {}) as { stoch_rsi?: { zone?: string } }).stoch_rsi?.zone;
  const extra = zone === "overbought" || zone === "oversold" ? `Is it ${zone} on the daily too?` : "Buyers or sellers in control?";
  return ["Give me a trade plan", "Does the daily agree?", extra, "Alert me on these levels", "Fear & Greed and BTC dominance?"];
}

/** What the chat box suggests: it follows the chart and the last answer instead of one fixed line. */
export function placeholderFor(m: AgentMessage | undefined, symbol: string | undefined, interval: string | undefined, n: number, spotOnly: boolean): string {
  const coin = symbol ? `${displaySymbol(symbol)}${interval ? ` ${interval}` : ""}` : "this chart";
  if (!m) {
    const first = ["Which of my coins are near demand?", "Is RSI overbought here?", "Best 4h setups across the market", "What does Kimi say?", "Buyers or sellers in control?"];
    return `Ask about ${coin}, e.g. "${first[n % first.length].toLowerCase()}", or paste a chart screenshot`;
  }
  const next = followUps(m, spotOnly);
  return `Follow up on ${coin}, e.g. "${next[n % next.length].toLowerCase()}"`;
}

/** "Numbers used": the facts an answer was written from, flattened to label/value lines. */
function factLines(obj: unknown, prefix = ""): [string, string][] {
  const fmt = (v: unknown): string =>
    v === null || v === undefined ? "–" : typeof v === "object" ? JSON.stringify(v) : String(v);
  const name = (k: string) => k.replace(/_/g, " ");
  if (Array.isArray(obj)) {
    if (obj.every((x) => typeof x !== "object" || x === null)) return [[prefix, obj.map(fmt).join(", ") || "none"]];
    return obj.slice(0, 8).map((x, i): [string, string] => [`${prefix} ${i + 1}`.trim(), Object.entries(x as object).map(([k, v]) => `${name(k)} ${fmt(v)}`).join(" · ")]);
  }
  if (obj && typeof obj === "object") return Object.entries(obj).flatMap(([k, v]) => factLines(v, prefix ? `${prefix} › ${name(k)}` : name(k)));
  return [[prefix, fmt(obj)]];
}

const DETAIL_LABEL: Record<AnswerDetail, string> = { short: "Short answers", normal: "Normal answers", detailed: "Detailed answers" };
const DETAIL_NEXT: Record<AnswerDetail, AnswerDetail> = { short: "normal", normal: "detailed", detailed: "short" };
const TREND_TONE: Record<string, string> = { up: "border-up/40 text-up", down: "border-down/40 text-down" };

/** "H4 up · D1 up · W1 mixed": whether the timeframes agree, at a glance. */
function TimeframeBadges({ facts }: { facts: Record<string, unknown> }) {
  const tf = facts.timeframe as string | undefined;
  const trend = facts.trend as string | undefined;
  const htf = (facts.higher_timeframes ?? {}) as Record<string, { trend?: string }>;
  const rows: [string, string][] = [
    ...(tf && trend ? [[tf, trend === "range" ? "mixed" : trend] as [string, string]] : []),
    ...Object.entries(htf).flatMap(([k, v]): [string, string][] => (v.trend ? [[k, v.trend]] : [])),
  ];
  if (rows.length < 2) return null;
  const agree = rows.every(([, t]) => t === rows[0][1]) && rows[0][1] !== "mixed";
  return (
    <div className="mt-1.5 flex flex-wrap items-center gap-1" title="Trend on this timeframe and the next two up (EMAs and price)">
      {rows.map(([k, t]) => (
        <span key={k} className={clsx("rounded border px-1.5 py-0.5 text-[10px]", TREND_TONE[t] ?? "border-line text-mute")}>
          {k} {t}
        </span>
      ))}
      {agree && <span className="text-[10px] text-mute">all agree</span>}
    </div>
  );
}

/** Copies an answer's text, for Discord or the journal. */
function CopyAnswer({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      onClick={() =>
        void navigator.clipboard?.writeText(text).then(() => {
          setDone(true);
          setTimeout(() => setDone(false), 1500);
        })
      }
      className="inline-flex shrink-0 items-center gap-1 rounded px-1 py-0.5 text-[10px] text-mute opacity-70 hover:bg-panel2 hover:text-ink group-hover:opacity-100"
      title="Copy this answer"
    >
      {done ? <Check className="h-3 w-3" /> : <ClipboardCopy className="h-3 w-3" />}
      {done ? "Copied" : "Copy"}
    </button>
  );
}

function NumbersUsed({ facts }: { facts: Record<string, unknown> }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="mt-1">
      <button type="button" onClick={() => setOpen(!open)} className="text-[10px] text-mute hover:text-ink">
        {open ? "Hide numbers used" : "Numbers used"}
      </button>
      {open && (
        <div className="mt-1 max-h-64 overflow-y-auto rounded border border-line bg-base/60 p-1.5 font-mono text-[10px] leading-4">
          {factLines(Object.fromEntries(Object.entries(facts).filter(([, v]) => typeof v !== "object" || v === null))).map(([k, val]) => (
            <div key={k} className="flex gap-2">
              <span className="shrink-0 text-mute">{k}</span>
              <span className="text-ink">{val}</span>
            </div>
          ))}
          {Object.entries(facts).filter(([, v]) => typeof v === "object" && v !== null).map(([section, v]) => (
            <div key={section} className="mt-1">
              <div className="font-semibold uppercase tracking-wide text-mute">{section.replace(/_/g, " ")}</div>
              {factLines(v).map(([k, val], i) => (
                <div key={i} className="flex gap-2">
                  {k && <span className="shrink-0 text-mute">{k}</span>}
                  <span className="break-all text-ink">{val}</span>
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

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
  spotOnly,
  onLog,
  onTrigger,
}: {
  plan: TradePlan;
  symbol?: string;
  /** No leverage: the size is capped at the cash, and no "needs Nx" is shown. */
  spotOnly?: boolean;
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
  const sized = sizePlan(plan, sizing, { spotOnly });
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
          {sized.capped ? `${sized.effectiveRiskPct.toFixed(2)}% (capped by cash)` : `${sizing.riskPct}%`} risk of ${sizing.account.toLocaleString()}
          {!spotOnly && sized.leverage > 1 && <> · needs {sized.leverage.toFixed(1)}x</>} · fees ~${sized.feesUsd.toFixed(2)}
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
              void navigator.clipboard?.writeText(orderText(plan, symbol, sized)).then(() => {
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
  /** A chart screenshot pasted, dropped or picked: read its levels and drawings onto the chart. */
  onImage?(file: File): void;
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
  /** The chart the agent is looking at, for the chat box's hint. */
  symbol?: string;
  interval?: Interval;
  /** How long answers are. */
  detail?: AnswerDetail;
  onDetail?(d: AnswerDetail): void;
}

/** The chart agent as a dock tab: the conversation fills the height, the prompt sits at the bottom. */
export default function AgentPanel(p: Props) {
  const [value, setValue] = useState("");
  const [showHistory, setShowHistory] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const lastAgent = [...p.messages].reverse().find((m) => m.role === "agent");
  const lastPrompt = [...p.messages].reverse().find((m) => m.role === "user")?.text;
  const [myShortcuts, setMyShortcuts] = usePersistentState<string[]>("ac:my-shortcuts", []);

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
        {p.detail && p.onDetail && (
          <button
            type="button"
            onClick={() => p.onDetail!(DETAIL_NEXT[p.detail!])}
            className="btn-ghost h-6 shrink-0 gap-1 border border-line px-1.5 text-[11px]"
            title="Answer length: short (one or two sentences), normal, or detailed (explains the reasoning). Click to change."
          >
            {DETAIL_LABEL[p.detail]}
          </button>
        )}
        {p.overlayCount > 0 && (
          <button type="button" onClick={p.onClearOverlays} className="btn-ghost h-6 shrink-0 gap-1 px-1.5 text-[11px]" title="Remove all the agent's drawings from this chart (Ctrl+Z brings them back). To remove one level, use its bin in the Layers tab (L).">
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
                      {m.streaming && <span className="ml-0.5 inline-block h-3 w-1.5 animate-pulse bg-accent/70 align-middle" aria-label="Writing" />}
                    </p>
                    {m.plan && (
                      <PlanCard
                        plan={m.plan}
                        symbol={m.symbol}
                        spotOnly={p.spotOnly}
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
                    {m.facts && <TimeframeBadges facts={m.facts} />}
                    {m.facts && <NumbersUsed facts={m.facts} />}
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
                      {m.role === "agent" && !m.streaming && m.text && <CopyAnswer text={m.text} />}
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
            {myShortcuts.map((s) => (
              <span key={`my-${s}`} className="group/sc inline-flex shrink-0 items-center rounded-full border border-yellow-400/40 bg-yellow-400/10 text-[11px] text-ink">
                <button type="button" disabled={p.busy} onClick={() => submit(s)} className="py-1 pl-2.5 pr-1 disabled:opacity-50" title={s}>
                  {s.length > 40 ? `${s.slice(0, 39)}…` : s}
                </button>
                <button
                  type="button"
                  onClick={() => setMyShortcuts(myShortcuts.filter((x) => x !== s))}
                  className="pr-2 text-mute opacity-50 hover:text-down group-hover/sc:opacity-100"
                  title="Remove this shortcut"
                  aria-label={`Remove shortcut ${s}`}
                >
                  ×
                </button>
              </span>
            ))}
            <button
              type="button"
              onClick={() => {
                const t = (value.trim() || lastPrompt || "").trim();
                if (t && !myShortcuts.includes(t)) setMyShortcuts([...myShortcuts, t].slice(-12));
              }}
              className="shrink-0 rounded-full border border-dashed border-line px-2.5 py-1 text-[11px] text-mute hover:border-accent/50 hover:text-ink"
              title="Save what's in the chat box (or your last question) as a shortcut button"
            >
              + Save as shortcut
            </button>
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
              {(p.messages.length === 0 ? SUGGESTIONS : followUps(lastAgent, p.spotOnly)).map((s) => (
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
            onDragOver={(e) => {
              if (p.onImage && Array.from(e.dataTransfer.items).some((i) => i.type.startsWith("image/"))) e.preventDefault();
            }}
            onDrop={(e) => {
              const file = imageFrom(e.dataTransfer);
              if (!file || !p.onImage) return;
              e.preventDefault();
              p.onImage(file);
            }}
          >
            {p.onImage && (
              <>
                <input
                  ref={fileRef}
                  type="file"
                  accept="image/png,image/jpeg,image/webp,image/gif"
                  className="hidden"
                  onChange={(e) => {
                    const file = e.target.files?.[0];
                    e.target.value = "";
                    if (file) p.onImage!(file);
                  }}
                />
                <button
                  type="button"
                  disabled={p.busy}
                  onClick={() => fileRef.current?.click()}
                  className="btn-ghost grid h-9 w-9 shrink-0 place-items-center border border-line p-0 disabled:opacity-40"
                  title="Read a chart screenshot: its levels, boxes and patterns are drawn on the chart (or paste one with Ctrl+V)"
                  aria-label="Read a chart screenshot"
                >
                  <ImagePlus className="h-4 w-4" />
                </button>
              </>
            )}
            <textarea
              ref={inputRef}
              rows={2}
              value={value}
              onChange={(e) => setValue(e.target.value)}
              onPaste={(e) => {
                const file = imageFrom(e.clipboardData);
                if (!file || !p.onImage) return;
                e.preventDefault();
                p.onImage(file);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  submit(value);
                }
              }}
              placeholder={placeholderFor(lastAgent, p.symbol, p.interval, p.messages.length, p.spotOnly)}
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
