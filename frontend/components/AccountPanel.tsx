"use client";

import clsx from "clsx";
import { ChevronLeft, ChevronRight, Download, EyeOff, KeyRound, Loader2, RefreshCw, ShieldCheck, ShieldAlert, Trash2 } from "lucide-react";
import { useCallback, useEffect, useState, type ReactNode } from "react";

import { usePersistentState } from "@/hooks/usePersistentState";
import {
  classifyAccount,
  fetchAccountFills,
  fetchBinanceKey,
  fetchAccountPositions,
  fetchHoldingsWatch,
  fetchImportStatus,
  fetchPnlCalendar,
  removeBinanceKey,
  runHoldingsWatch,
  runImport,
  saveBinanceKey,
  saveHoldingsWatch,
  saveImportSettings,
  setAssetHidden,
  testBinanceKey,
  type AccountFill,
  type AccountMarket,
  type AccountPositions,
  type MergedHolding,
  type BinanceKeyStatus,
  type EarnPosition,
  type FillKind,
  type FuturesPosition,
  type HoldingsWatchSettings,
  type HoldingsWatchStatus,
  type ImportSettings,
  type ImportStatus,
  type PnlCalendar,
  type SpotHolding,
} from "@/lib/binance";
import { fetchCoach, type CoachFinding, type CoachReport } from "@/lib/coach";
import { displaySymbol, formatPrice } from "@/lib/format";
import type { DockPanelProps } from "@/lib/dock";
import { exitOverlays } from "@/lib/exitPlan";
import ExitPlanCard from "./ExitPlanCard";
import { fetchGridBots, type GridBot } from "@/lib/gridbot";

const STATUS_MS = 60_000;

type Tab = "setup" | "positions" | "pnl" | "coach" | "fills";

const KIND_LABEL: Record<FillKind, string> = { manual: "Mine", bot: "Bot", unknown: "Unknown" };
const KIND_CLASS: Record<FillKind, string> = {
  manual: "border-accent/50 text-accent",
  bot: "border-purple-400/50 text-purple-300",
  unknown: "border-yellow-400/40 text-yellow-300",
};

// Binance's permission flags, in the order the settings section lists them.
const PERMISSIONS: [string, string][] = [
  ["enableReading", "Reading"],
  ["enableSpotAndMarginTrading", "Spot & margin trading"],
  ["enableFutures", "Futures"],
  ["enableMargin", "Margin loans"],
  ["enableVanillaOptions", "Options"],
  ["enableWithdrawals", "Withdrawals"],
  ["enableInternalTransfer", "Internal transfer"],
  ["permitsUniversalTransfer", "Universal transfer"],
  ["enablePortfolioMarginTrading", "Portfolio margin"],
  ["enableFixApiTrade", "FIX API trading"],
];

function when(t: number): string {
  return new Date(t * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function qty(v: number): string {
  return v.toLocaleString("en-US", { maximumFractionDigits: 8 });
}

function money(v: number | null | undefined, signed = false): string {
  if (v == null || !Number.isFinite(v)) return "–";
  const s = Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `${v < 0 ? "−" : signed && v > 0 ? "+" : ""}${s}`;
}

function tone(v: number | null | undefined): string {
  return v == null || Math.abs(v) < 1e-9 ? "text-mute" : v > 0 ? "text-up" : "text-down";
}

function Section({ title, right, children }: { title: string; right?: ReactNode; children: ReactNode }) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-2">
        <div className="text-[10px] font-semibold uppercase tracking-wide text-mute">{title}</div>
        <div className="flex-1" />
        {right}
      </div>
      {children}
    </div>
  );
}

function Notice({ tone: t = "mute", children }: { tone?: "mute" | "warn" | "error"; children: ReactNode }) {
  return (
    <div
      className={clsx(
        "rounded border px-2 py-1.5 text-[11px] leading-snug",
        t === "error" ? "border-down/40 bg-down/10 text-down" : t === "warn" ? "border-yellow-400/40 bg-yellow-400/10 text-yellow-200" : "border-line bg-panel2/60 text-mute",
      )}
    >
      {children}
    </div>
  );
}

function KindBadge({ kind, overridden }: { kind: FillKind; overridden?: boolean }) {
  return (
    <span className={clsx("rounded border px-1 py-px text-[10px] font-medium", KIND_CLASS[kind])} title={overridden ? "Set by you" : undefined}>
      {KIND_LABEL[kind]}
      {overridden ? "*" : ""}
    </span>
  );
}

/** Mine / bot (a tracked bot, or any) / unknown, or back to the app's own classification. */
function KindSelect(p: { kind: FillKind; botId: string | null; overridden: boolean; bots: GridBot[]; busy: boolean; onChange(kind: FillKind | null, botId: string | null): void }) {
  const value = p.kind === "bot" && p.botId ? `bot:${p.botId}` : p.kind;
  return (
    <select
      value={value}
      disabled={p.busy}
      onChange={(e) => {
        const v = e.target.value;
        if (v === "auto") p.onChange(null, null);
        else if (v.startsWith("bot:")) p.onChange("bot", v.slice(4));
        else p.onChange(v as FillKind, null);
      }}
      className="h-6 max-w-[7.5rem] rounded border border-line bg-base px-1 text-[11px] text-ink outline-none disabled:opacity-50"
      title="Whose trade is this?"
    >
      <option value="manual">Mine</option>
      <option value="bot">Bot</option>
      {p.bots.map((b) => (
        <option key={b.id} value={`bot:${b.id}`}>
          Bot: {b.name}
        </option>
      ))}
      <option value="unknown">Unknown</option>
      {p.overridden && <option value="auto">Back to automatic</option>}
    </select>
  );
}

// ------------------------------------------------------------------ setup --

function KeySetup({ status, onChanged }: { status: BinanceKeyStatus | null; onChanged(s: BinanceKeyStatus): void }) {
  const [key, setKey] = useState("");
  const [secret, setSecret] = useState("");
  const [busy, setBusy] = useState<"save" | "test" | "remove" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const act = async (what: "save" | "test" | "remove", fn: () => Promise<BinanceKeyStatus>) => {
    setBusy(what);
    setError(null);
    try {
      onChanged(await fn());
      if (what === "save") {
        setKey("");
        setSecret("");
      }
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const input = "h-7 w-full rounded border border-line bg-base px-2 font-mono text-[11px] text-ink outline-none focus:border-accent";
  const s = status;
  return (
    <Section title="Binance API key (read-only)">
      <Notice>
        Create a key on Binance (Profile › API Management › Create API) and tick <b className="text-ink">only &quot;Enable Reading&quot;</b>.
        Leave trading (spot, margin, futures, options), withdrawals and transfers off. The app asks Binance what the key may
        do and refuses one that can trade or withdraw. The key is kept on the backend only (backend/.cache, file mode 600);
        the browser only ever sees its last 4 characters. Restricting the key to your server&apos;s IP adds another lock.
      </Notice>
      {s?.configured ? (
        <div className="space-y-1.5 rounded border border-line p-2">
          <div className="flex items-center gap-1.5">
            {s.ok ? <ShieldCheck className="h-4 w-4 text-up" /> : <ShieldAlert className={clsx("h-4 w-4", s.ok === false ? "text-down" : "text-mute")} />}
            <span className="font-mono text-[12px] text-ink">{s.masked}</span>
            <span className="text-[11px] text-mute">
              {s.source === "env" ? "from BINANCE_API_KEY on the server" : s.added_at ? `added ${when(s.added_at)}` : ""}
            </span>
          </div>
          <div className="text-[11px]">
            {s.ok === true && <span className="text-up">Read-only: OK{s.checked_at ? ` (checked ${when(s.checked_at)})` : ""}</span>}
            {s.ok === false && s.problems.length > 0 && (
              <span className="text-down">Refused: this key {s.problems.join(", ")}. The app will not use it.</span>
            )}
            {s.ok === false && !s.problems.length && s.error && <span className="text-yellow-300">{s.error}</span>}
            {s.ok == null && <span className="text-mute">Not checked since the server started; it is checked before the next use.</span>}
          </div>
          {s.permissions && (
            <div className="flex flex-wrap gap-1">
              {PERMISSIONS.filter(([k]) => k in (s.permissions ?? {})).map(([k, label]) => {
                const on = !!s.permissions?.[k];
                const good = k === "enableReading" ? on : !on;
                return (
                  <span key={k} className={clsx("rounded border px-1 py-px text-[10px]", good ? "border-line text-mute" : "border-down/50 text-down")}>
                    {label}: {on ? "on" : "off"}
                  </span>
                );
              })}
            </div>
          )}
          <div className="flex gap-1">
            <button type="button" disabled={!!busy} onClick={() => act("test", testBinanceKey)} className="btn-ghost h-6 border border-line px-1.5 text-[11px] disabled:opacity-50">
              {busy === "test" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />} Test
            </button>
            {s.source !== "env" && (
              <button
                type="button"
                disabled={!!busy}
                onClick={() => {
                  if (window.confirm("Remove the Binance key from the server? Imported fills and journal entries stay.")) void act("remove", removeBinanceKey);
                }}
                className="btn-ghost h-6 border border-line px-1.5 text-[11px] hover:text-down disabled:opacity-50"
              >
                <Trash2 className="h-3.5 w-3.5" /> Remove
              </button>
            )}
          </div>
          {s.source === "env" && <p className="text-[10px] text-mute">Set on the server, so it can only be changed there (BINANCE_API_KEY / BINANCE_API_SECRET).</p>}
        </div>
      ) : (
        <form
          className="space-y-1.5"
          onSubmit={(e) => {
            e.preventDefault();
            if (!key.trim() || !secret.trim()) {
              setError("Paste both the API key and the secret key.");
              return;
            }
            void act("save", () => saveBinanceKey(key, secret));
          }}
        >
          <label className="block text-[11px] text-mute">
            API key
            <input className={input} value={key} onChange={(e) => setKey(e.target.value)} autoComplete="off" spellCheck={false} />
          </label>
          <label className="block text-[11px] text-mute">
            Secret key
            <input className={input} type="password" value={secret} onChange={(e) => setSecret(e.target.value)} autoComplete="new-password" spellCheck={false} />
          </label>
          <button
            type="submit"
            disabled={!!busy}
            className="inline-flex h-7 items-center gap-1 rounded bg-accent px-3 text-[11px] font-semibold text-white disabled:opacity-50"
          >
            {busy === "save" ? <Loader2 className="h-3 w-3 animate-spin" /> : <KeyRound className="h-3 w-3" />} Check and save
          </button>
        </form>
      )}
      {error && <Notice tone="error">{error}</Notice>}
    </Section>
  );
}

function autoLabel(m: number): string {
  if (m === 0) return "Off";
  if (m < 60) return `Every ${m} min`;
  if (m < 1440) return `Every ${m / 60} h`;
  return "Daily";
}

function ImportSetup({ status, onStatus }: { status: ImportStatus; onStatus(s: ImportStatus): void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [symbols, setSymbols] = useState(status.settings.symbols.join(", "));
  const configured = status.key.configured && status.key.ok !== false;

  const save = async (patch: Partial<ImportSettings>) => {
    setError(null);
    try {
      onStatus(await saveImportSettings({ ...status.settings, ...patch }));
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      await runImport();
      onStatus(await fetchImportStatus());
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const last = status.last_import;
  const sel = "h-6 rounded border border-line bg-base px-1 text-[11px] text-ink outline-none";
  return (
    <Section title="Import fills">
      <p className="text-[11px] leading-snug text-mute">
        Spot fills (the coins in your wallet, your grid bots&apos; pairs and any pairs listed below) and USD-M futures fills
        are imported once each. Only your own closed trades go into the journal; bot fills feed the grid bots&apos; real vs
        simulated comparison.
      </p>
      <div className="grid grid-cols-2 gap-1.5 text-[11px] text-mute">
        <label className="flex items-center gap-1.5">
          Auto-import
          <select className={sel} value={status.settings.auto_minutes} onChange={(e) => void save({ auto_minutes: Number(e.target.value) })}>
            {status.auto_minutes_options.map((m) => (
              <option key={m} value={m}>
                {autoLabel(m)}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-1.5">
          <input type="checkbox" checked={status.settings.futures} onChange={(e) => void save({ futures: e.target.checked })} />
          USD-M futures
        </label>
        <label className="col-span-2 block">
          Other spot pairs (comma separated)
          <input
            className="h-7 w-full rounded border border-line bg-base px-2 font-mono text-[11px] text-ink outline-none focus:border-accent"
            value={symbols}
            onChange={(e) => setSymbols(e.target.value.toUpperCase())}
            onBlur={() => {
              const list = symbols.split(/[\s,;]+/).filter(Boolean);
              if (list.join(",") !== status.settings.symbols.join(",")) void save({ symbols: list });
            }}
            placeholder="e.g. SOLUSDT, INJUSDT"
          />
        </label>
        <div className="col-span-2">
          Hidden coins (never requested from Binance, left out of holdings)
          <div className="mt-0.5 flex flex-wrap gap-1">
            {(status.settings.hidden_assets ?? []).length === 0 && <span className="text-[10px]">None. Hide a coin from its holdings row.</span>}
            {(status.settings.hidden_assets ?? []).map((a) => (
              <button
                key={a}
                type="button"
                title="Show it again"
                onClick={() => void save({ hidden_assets: (status.settings.hidden_assets ?? []).filter((x) => x !== a) })}
                className="rounded border border-line px-1.5 font-mono text-[10px] text-ink hover:border-accent"
              >
                {a} ×
              </button>
            ))}
          </div>
          {(status.invalid_symbols ?? []).length > 0 && (
            <p className="mt-0.5 text-[10px]">Not on Binance, so never asked for again: {status.invalid_symbols!.join(", ")}</p>
          )}
        </div>
      </div>
      <div className="flex items-center gap-2">
        <button
          type="button"
          disabled={busy || status.running || !configured}
          onClick={() => void run()}
          className="inline-flex h-7 items-center gap-1 rounded bg-accent px-3 text-[11px] font-semibold text-white disabled:opacity-50"
          title={configured ? undefined : "Add a read-only key first"}
        >
          {busy || status.running ? <Loader2 className="h-3 w-3 animate-spin" /> : <Download className="h-3 w-3" />} Import now
        </button>
        <span className="text-[11px] text-mute">
          {status.fills} fills: {status.by_kind.manual} mine, {status.by_kind.bot} bot, {status.by_kind.unknown} unknown
        </span>
      </div>
      {busy && <p className="text-[11px] text-mute">The first import of a busy account can take a minute.</p>}
      {last && (
        <div className="space-y-0.5 text-[11px] text-mute">
          <div>
            Last import {when(last.at)}
            {last.auto ? " (auto)" : ""}
            {last.error ? (
              <span className="text-down">: {last.error}</span>
            ) : (
              <>
                : {last.new_fills ?? 0} new fills from {last.symbols ?? 0} pairs
                {last.journal && ` · journal +${last.journal.added}, ${last.journal.updated} updated, −${last.journal.removed}`}
                {last.seconds != null && ` · ${last.seconds}s`}
              </>
            )}
          </div>
          {(last.notes ?? []).map((n) => (
            <div key={n} className="text-yellow-300/90">
              {n}
            </div>
          ))}
        </div>
      )}
      {error && <Notice tone="error">{error}</Notice>}
      <Notice tone="warn">{status.spot_grid_note}</Notice>
    </Section>
  );
}

// -------------------------------------------------------------- positions --

function HoldingRow({ h, bots, busy, onClassify }: { h: SpotHolding; bots: GridBot[]; busy: boolean; onClassify(kind: FillKind | null, botId: string | null): void }) {
  return (
    <div className="border-b border-line/60 py-1.5">
      <div className="flex items-center gap-1.5">
        <span className="font-medium text-ink">{h.asset}</span>
        <KindBadge kind={h.kind} overridden={h.overridden} />
        <span className="min-w-0 flex-1 truncate text-[10px] text-mute" title={h.reason}>
          {h.reason}
        </span>
        <KindSelect kind={h.kind} botId={h.bot_id} overridden={h.overridden} bots={bots} busy={busy} onChange={onClassify} />
      </div>
      <div className="mt-0.5 grid grid-cols-4 gap-x-2 font-mono text-[11px]">
        <span className="text-ink" title="Your own (bots' coins excluded)">{qty(h.own_qty)}</span>
        <span className="text-mute" title="Average entry from your own buys">{h.avg_entry != null ? `@ ${formatPrice(h.avg_entry)}` : "no buys"}</span>
        <span className="text-ink" title="Value in USDT">{money(h.value)}</span>
        <span className={clsx("text-right", tone(h.unrealized_pnl))} title="Unrealized PnL of the coins with an imported buy">
          {money(h.unrealized_pnl, true)}
        </span>
      </div>
      {h.untracked_qty > 0 && h.avg_entry != null && (
        <div className="text-[10px] text-mute">{qty(h.untracked_qty)} without an imported buy (not in the average).</div>
      )}
    </div>
  );
}

function PositionRow({ p, bots, busy, onClassify }: { p: FuturesPosition; bots: GridBot[]; busy: boolean; onClassify(kind: FillKind | null, botId: string | null): void }) {
  return (
    <div className="border-b border-line/60 py-1.5">
      <div className="flex items-center gap-1.5">
        <span className="font-medium text-ink">{displaySymbol(p.symbol)}</span>
        <span className={clsx("text-[11px] font-semibold", p.side === "long" ? "text-up" : "text-down")}>
          {p.side === "long" ? "Long" : "Short"}
          {p.leverage ? ` ${p.leverage}x` : ""}
        </span>
        <KindBadge kind={p.kind} overridden={p.overridden} />
        <span className="min-w-0 flex-1 truncate text-[10px] text-mute" title={p.reason}>
          {p.reason}
        </span>
        <KindSelect kind={p.kind} botId={p.bot_id} overridden={p.overridden} bots={bots} busy={busy} onChange={onClassify} />
      </div>
      <div className="mt-0.5 grid grid-cols-4 gap-x-2 font-mono text-[11px]">
        <span className="text-ink">{qty(p.qty)}</span>
        <span className="text-mute">@ {formatPrice(p.entry_price)}</span>
        <span className="text-mute">{p.mark_price != null ? `mark ${formatPrice(p.mark_price)}` : ""}</span>
        <span className={clsx("text-right", tone(p.unrealized_pnl))}>{money(p.unrealized_pnl, true)}</span>
      </div>
      {p.liquidation_price != null && <div className="text-[10px] text-mute">Liquidation {formatPrice(p.liquidation_price)}</div>}
    </div>
  );
}

function EarnRow({ e }: { e: EarnPosition }) {
  const term =
    e.product === "locked"
      ? `Locked${e.duration_days ? ` ${e.duration_days}d` : ""}${e.redeem_at ? `, ends ${new Date(e.redeem_at * 1000).toLocaleDateString([], { month: "short", day: "numeric" })}` : ""}${e.auto_renew ? ", renews" : ""}`
      : "Flexible";
  return (
    <div className="grid grid-cols-[3.5rem_1fr_auto_auto] items-baseline gap-2 font-mono text-[11px]">
      <span className="font-sans font-medium text-ink">{e.asset}</span>
      <span className="truncate font-sans text-[10px] text-mute" title={term}>{term}</span>
      <span className="text-ink">{qty(e.qty)}</span>
      <span className="text-right">
        <span className="text-ink">{money(e.value)}</span>
        {e.apr_pct != null && <span className="ml-1.5 text-up">{e.apr_pct.toFixed(2)}%</span>}
      </span>
    </div>
  );
}

// ------------------------------------------------------------- PnL calendar --

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/** "+12.3", "−1.2k": short enough for a calendar cell. */
function compact(v: number): string {
  const a = Math.abs(v);
  const s = a >= 10_000 ? `${(a / 1000).toFixed(0)}k` : a >= 1000 ? `${(a / 1000).toFixed(1)}k` : a >= 100 ? a.toFixed(0) : a.toFixed(1);
  return `${v < 0 ? "−" : v > 0 ? "+" : ""}${s}`;
}

function monthKey(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function PnlView({ enabled, version }: { enabled: boolean; version: number }) {
  const [kind, setKind] = usePersistentState<"" | FillKind>("ac:account-pnl-kind", "");
  const [month, setMonth] = useState(() => monthKey(new Date()));
  const [data, setData] = useState<PnlCalendar | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!enabled) return;
    const ctrl = new AbortController();
    fetchPnlCalendar(kind || null, ctrl.signal)
      .then((r) => {
        setData(r);
        setError(null);
      })
      .catch((err: Error) => {
        if (err.name !== "AbortError") setError(err.message);
      });
    return () => ctrl.abort();
  }, [enabled, kind, version]);

  if (!enabled) return <p className="px-3 py-4 text-[12px] leading-relaxed text-mute">Add a read-only Binance key (Setup) and import your fills to see your daily PnL.</p>;

  const [y, mo] = month.split("-").map(Number);
  const first = new Date(y, mo - 1, 1);
  const daysIn = new Date(y, mo, 0).getDate();
  const lead = (first.getDay() + 6) % 7; // Monday first
  const byDate = new Map((data?.days ?? []).map((d) => [d.date, d]));
  const inMonth = (data?.days ?? []).filter((d) => d.date.startsWith(month));
  const monthTotal = inMonth.reduce((a, d) => a + d.pnl, 0);
  const scale = Math.max(1e-9, ...inMonth.map((d) => Math.abs(d.pnl)));
  const shift = (n: number) => setMonth(monthKey(new Date(y, mo - 1 + n, 1)));
  const today = new Date();
  const todayKey = `${monthKey(today)}-${String(today.getDate()).padStart(2, "0")}`;

  return (
    <div className="space-y-3 px-3 py-2 text-[12px]">
      <div className="flex items-center gap-1">
        <button type="button" onClick={() => shift(-1)} className="btn-ghost h-6 w-6 p-0" title="Previous month">
          <ChevronLeft className="h-3.5 w-3.5" />
        </button>
        <div className="min-w-[7.5rem] text-center text-[12px] font-medium text-ink">
          {first.toLocaleDateString([], { month: "long", year: "numeric" })}
        </div>
        <button type="button" onClick={() => shift(1)} className="btn-ghost h-6 w-6 p-0" title="Next month">
          <ChevronRight className="h-3.5 w-3.5" />
        </button>
        <div className="flex-1" />
        <select value={kind} onChange={(e) => setKind(e.target.value as "" | FillKind)} className="h-6 rounded border border-line bg-panel2 px-1 text-[11px] text-ink" title="Whose trades">
          <option value="">All trades</option>
          <option value="manual">Mine</option>
          <option value="bot">Bots</option>
          <option value="unknown">Unknown</option>
        </select>
      </div>
      {error && <Notice tone="error">{error}</Notice>}
      {!data && !error && <Loader2 className="mx-auto h-4 w-4 animate-spin text-mute" />}
      {data && (
        <>
          <div className="flex items-baseline gap-3 font-mono text-[11px]">
            <span className="text-mute">
              Month <span className={clsx("text-[13px] font-semibold", tone(monthTotal))}>{money(monthTotal, true)}</span> USD
            </span>
            <span className="text-mute">
              <span className="text-up">{inMonth.filter((d) => d.pnl > 0).length}</span> up ·{" "}
              <span className="text-down">{inMonth.filter((d) => d.pnl < 0).length}</span> down
            </span>
          </div>
          <div className="grid grid-cols-7 gap-0.5">
            {WEEKDAYS.map((w) => (
              <div key={w} className="pb-0.5 text-center text-[9px] uppercase text-mute">
                {w}
              </div>
            ))}
            {Array.from({ length: lead }, (_, i) => (
              <div key={`pad-${i}`} />
            ))}
            {Array.from({ length: daysIn }, (_, i) => {
              const date = `${month}-${String(i + 1).padStart(2, "0")}`;
              const d = byDate.get(date);
              const alpha = d ? 0.12 + 0.5 * Math.min(1, Math.abs(d.pnl) / scale) : 0;
              return (
                <div
                  key={date}
                  className={clsx("flex h-11 flex-col rounded border px-1 py-0.5", date === todayKey ? "border-accent/60" : "border-line/60")}
                  style={d && d.pnl !== 0 ? { backgroundColor: d.pnl > 0 ? `rgba(34,197,94,${alpha})` : `rgba(239,68,68,${alpha})` } : undefined}
                  title={d ? `${date}: ${money(d.pnl, true)} USD (spot ${money(d.spot, true)}, futures ${money(d.futures, true)}), ${d.closes} closing fill${d.closes === 1 ? "" : "s"}` : date}
                >
                  <span className="text-[9px] text-mute">{i + 1}</span>
                  {d && <span className={clsx("mt-auto truncate text-right font-mono text-[10px]", d.pnl === 0 ? "text-mute" : "text-ink")}>{compact(d.pnl)}</span>}
                </div>
              );
            })}
          </div>
          <div className="space-y-0.5 font-mono text-[11px] text-mute">
            <div>
              All time <span className={tone(data.total)}>{money(data.total, true)}</span> USD over {data.win_days + data.loss_days} trading days ({data.win_days} up, {data.loss_days} down)
            </div>
            {data.best && (
              <div>
                Best day {data.best.date} <span className="text-up">{money(data.best.pnl, true)}</span>
                {data.worst && (
                  <>
                    {" "}· worst {data.worst.date} <span className="text-down">{money(data.worst.pnl, true)}</span>
                  </>
                )}
              </div>
            )}
          </div>
          {data.fills === 0 && <Notice>No fills imported yet: run an import in Setup, then come back.</Notice>}
          {data.notes.map((n) => (
            <p key={n} className="text-[10px] leading-snug text-mute">
              {n}
            </p>
          ))}
        </>
      )}
    </div>
  );
}

const COACH_TONE: Record<CoachFinding["tone"], string> = {
  warn: "border-yellow-400/40 bg-yellow-400/5",
  good: "border-up/40 bg-up/5",
  info: "border-line bg-panel2/40",
};

function CoachView({ enabled, version }: { enabled: boolean; version: number }) {
  const [data, setData] = useState<CoachReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async (refresh = false, signal?: AbortSignal) => {
    setLoading(true);
    try {
      setData(await fetchCoach(refresh, signal));
      setError(null);
    } catch (err) {
      if ((err as Error).name !== "AbortError") setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!enabled) return;
    const ctrl = new AbortController();
    void load(false, ctrl.signal);
    return () => ctrl.abort();
  }, [enabled, load, version]);

  if (!enabled) return <p className="px-3 py-4 text-[12px] leading-relaxed text-mute">Add a read-only Binance key (Setup) and import your fills: the coach reads your own closed trades.</p>;
  const ov = data?.overview;
  return (
    <div className="space-y-3 px-3 py-2 text-[12px]">
      <div className="flex items-center gap-2 text-[11px] text-mute">
        <span>Habits in your own closed spot trades, with the numbers behind each.</span>
        <div className="flex-1" />
        <button type="button" onClick={() => void load(true)} className="btn-ghost h-6 w-6 p-0" title="Look again">
          <RefreshCw className={clsx("h-3.5 w-3.5", loading && "animate-spin")} />
        </button>
      </div>
      {error && <Notice tone="error">{error}</Notice>}
      {!data && !error && <Loader2 className="mx-auto h-4 w-4 animate-spin text-mute" />}
      {ov && ov.trades > 0 && (
        <div className="flex flex-wrap gap-x-3 font-mono text-[11px] text-mute">
          <span>
            {ov.trades} trades · won <span className="text-ink">{ov.win_rate}%</span>
          </span>
          <span>
            PnL <span className={tone(ov.pnl)}>{money(ov.pnl, true)}</span>
          </span>
          <span>fees {money(ov.fees)}</span>
        </div>
      )}
      {data?.note && <Notice>{data.note}</Notice>}
      {data && !data.note && data.findings.length === 0 && <Notice>Nothing stands out in your trades: no habit the coach looks for showed up.</Notice>}
      {data?.findings.map((f) => (
        <div key={f.code} className={clsx("rounded border px-2.5 py-2", COACH_TONE[f.tone])}>
          <div className="text-[12px] font-medium text-ink">{f.title}</div>
          <p className="mt-0.5 text-[11px] leading-snug text-mute">{f.detail}</p>
          <p className="mt-0.5 text-[10px] text-mute">On {f.trades} trades.</p>
        </div>
      ))}
    </div>
  );
}

/** Every coin owned, spot and Simple Earn together, with where it sits and a totals footer. */
function MergedHoldings({ rows, onHide, onChartOverlays }: { rows: MergedHolding[]; onHide(asset: string): void; onChartOverlays?: DockPanelProps["onChartOverlays"] }) {
  const [planFor, setPlanFor] = useState<string | null>(null);
  const shown = rows.filter((h) => (h.value ?? 0) >= 1 || h.value == null);
  const total = shown.reduce((a, h) => a + (h.value ?? 0), 0);
  const pnl = shown.reduce((a, h) => a + (h.unrealized_pnl ?? 0), 0);
  const day = (t: number) => new Date(t * 1000).toLocaleDateString([], { day: "numeric", month: "short" });
  return (
    <Section title="Everything you own" right={<span className="font-mono text-[11px] text-ink">{money(total)} USDT</span>}>
      {shown.map((h) => (
        <div key={h.key} className="flex flex-wrap items-center gap-x-2 gap-y-0.5 font-mono text-[11px]">
          <span className="w-14 font-sans font-medium text-ink">{h.asset}</span>
          <span className="text-ink">{qty(h.qty)}</span>
          {h.spot_qty > 0 && h.earn_qty > 0 && <span className="rounded border border-line px-1 font-sans text-[10px] text-mute">spot {qty(h.spot_qty)}</span>}
          {h.earn_qty > 0 && (
            <span className="rounded border border-accent/40 px-1 font-sans text-[10px] text-accent" title={h.locked_qty ? `${qty(h.locked_qty)} locked until ${h.redeem_at ? day(h.redeem_at) : "the term ends"}` : "Flexible: can be redeemed any time"}>
              Earn {qty(h.earn_qty)}
              {h.locked_qty > 0 ? ` · locked to ${h.redeem_at ? day(h.redeem_at) : "term end"}` : ""}
            </span>
          )}
          {h.tradable === false && <span className="font-sans text-[10px] text-mute" title="Binance has no USDT pair for it">no pair</span>}
          <span className="flex-1" />
          {h.avg_entry != null && <span className="text-mute">avg {formatPrice(h.avg_entry)}</span>}
          <span className="text-ink">{h.value != null ? `$${money(h.value)}` : "–"}</span>
          {h.unrealized_pnl != null && (
            <span className={h.unrealized_pnl >= 0 ? "text-up" : "text-down"} title={h.rewards_qty ? `Includes ${qty(h.rewards_qty)} ${h.asset} of Earn rewards, which cost nothing` : undefined}>
              {h.unrealized_pnl >= 0 ? "+" : "−"}${money(Math.abs(h.unrealized_pnl))}
            </span>
          )}
          {h.tradable !== false && (h.value ?? 0) >= 10 && (
            <button
              type="button"
              className={clsx("btn-ghost h-5 px-1 font-sans text-[10px]", planFor === h.symbol && "text-accent")}
              title="Where to sell it in pieces, and where holding it is wrong"
              onClick={() => setPlanFor(planFor === h.symbol ? null : h.symbol)}
            >
              Exit plan
            </button>
          )}
          <button type="button" className="btn-ghost h-5 w-5 p-0" title={`Hide ${h.asset}: never request it from Binance or show it here (undo in Setup)`} onClick={() => onHide(h.asset)}>
            <EyeOff className="h-3 w-3" />
          </button>
          {planFor === h.symbol && (
            <div className="w-full font-sans">
              <ExitPlanCard symbol={h.symbol} onChart={onChartOverlays ? (p) => onChartOverlays(`exit:${h.symbol}`, h.symbol, p ? exitOverlays(p) : []) : undefined} />
            </div>
          )}
        </div>
      ))}
      <div className="flex justify-between border-t border-line pt-1 font-mono text-[11px]">
        <span className="font-sans text-mute">Total, coins with a known entry</span>
        <span className={pnl >= 0 ? "text-up" : "text-down"}>
          {pnl >= 0 ? "+" : "−"}${money(Math.abs(pnl))}
        </span>
      </div>
    </Section>
  );
}

function PositionsView({ bots, enabled, onChartOverlays }: { bots: GridBot[]; enabled: boolean; onChartOverlays?: DockPanelProps["onChartOverlays"] }) {
  const [data, setData] = useState<AccountPositions | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async (refresh = false) => {
    setLoading(true);
    try {
      setData(await fetchAccountPositions(refresh));
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (enabled) void load();
  }, [enabled, load]);

  const classify = async (key: string, kind: FillKind | null, botId: string | null) => {
    setBusy(true);
    try {
      await classifyAccount([key], kind, botId);
      await load(true);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (!enabled) return <p className="px-3 py-4 text-[12px] leading-relaxed text-mute">Add a read-only Binance key (Setup) to see your positions.</p>;
  const m = data?.manual;
  const b = data?.bots;
  return (
    <div className="space-y-4 px-3 py-2 text-[12px]">
      <div className="flex items-center gap-2 text-[11px] text-mute">
        <span>{data ? `Updated ${when(data.updated_at)}` : ""}</span>
        <div className="flex-1" />
        <button type="button" onClick={() => void load(true)} className="btn-ghost h-6 w-6 p-0" title="Refresh from Binance">
          <RefreshCw className={clsx("h-3.5 w-3.5", loading && "animate-spin")} />
        </button>
      </div>
      {error && <Notice tone="error">{error}</Notice>}
      {!data && !error && <Loader2 className="mx-auto h-4 w-4 animate-spin text-mute" />}
      {m && b && (
        <>
          {m.holdings && m.holdings.length > 0 && (
            <MergedHoldings
              rows={m.holdings}
              onChartOverlays={onChartOverlays}
              onHide={(a) =>
                void setAssetHidden(a, true)
                  .then(() => load(true))
                  .catch((err: Error) => setError(err.message))
              }
            />
          )}
          <Section title="Your spot holdings">
            {m.spot.length === 0 && <p className="text-[11px] text-mute">No coins of your own in the spot wallet.</p>}
            {m.spot.map((h) => (
              <HoldingRow key={h.key} h={h} bots={bots} busy={busy} onClassify={(k, id) => void classify(h.key, k, id)} />
            ))}
            {m.cash.length > 0 && (
              <div className="font-mono text-[11px] text-mute">
                Cash: {m.cash.map((c) => `${money(c.qty)} ${c.asset}`).join(" · ")}
              </div>
            )}
          </Section>
          {m.earn && m.earn.length > 0 && (
            <Section title="Simple Earn" right={<span className="font-mono text-[11px] text-ink">{money(m.earn.reduce((a, e) => a + (e.value ?? 0), 0))} USDT</span>}>
              {m.earn.map((e) => (
                <EarnRow key={e.key} e={e} />
              ))}
            </Section>
          )}
          <Section title="Your USD-M futures positions">
            {m.futures.length === 0 && <p className="text-[11px] text-mute">No open positions.</p>}
            {m.futures.map((p) => (
              <PositionRow key={p.key} p={p} bots={bots} busy={busy} onClassify={(k, id) => void classify(p.key, k, id)} />
            ))}
          </Section>
          <Section title="Bots (not counted above)">
            <div className="text-[11px] text-mute">
              Trading Bots wallet:{" "}
              <span className="font-mono text-ink">{b.wallet ? `${money(b.wallet.usdt)} USDT` : "not reported"}</span>
              <span className="block text-[10px]">Binance reports only this wallet&apos;s total, not the coins or the bots in it.</span>
            </div>
            {b.spot.map((h) =>
              "key" in h ? (
                <HoldingRow key={h.key} h={h} bots={bots} busy={busy} onClassify={(k, id) => void classify(h.key, k, id)} />
              ) : (
                <div key={`${h.bot_id}-${h.asset}`} className="flex gap-2 font-mono text-[11px]">
                  <span className="text-ink">{h.asset}</span>
                  <span className="text-ink">{qty(h.qty)}</span>
                  <span className="truncate font-sans text-[10px] text-mute">
                    {bots.find((x) => x.id === h.bot_id)?.name ?? "bot"} · {h.reason}
                  </span>
                </div>
              ),
            )}
            {b.futures.map((p) => (
              <PositionRow key={p.key} p={p} bots={bots} busy={busy} onClassify={(k, id) => void classify(p.key, k, id)} />
            ))}
            {b.tracked.length > 0 && (
              <table className="w-full font-mono text-[11px]">
                <thead>
                  <tr className="text-[10px] text-mute">
                    <th className="text-left font-normal">Tracked grid bot (simulated)</th>
                    <th className="text-right font-normal">Coin</th>
                    <th className="text-right font-normal">Quote</th>
                  </tr>
                </thead>
                <tbody>
                  {b.tracked.map((t) => (
                    <tr key={t.bot_id}>
                      <td className="max-w-[9rem] truncate font-sans text-ink" title={t.name}>
                        {t.name}
                      </td>
                      <td className="text-right text-ink">{t.base_held != null ? qty(t.base_held) : "–"}</td>
                      <td className="text-right text-ink">{money(t.quote_held)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Section>
          {data.wallets.length > 0 && (
            <Section title="Wallets (USDT)">
              <div className="flex flex-wrap gap-x-3 gap-y-0.5 font-mono text-[11px]">
                {data.wallets
                  .filter((w) => w.usdt > 0)
                  .map((w) => (
                    <span key={w.wallet} className="text-mute">
                      {w.wallet} <span className="text-ink">{money(w.usdt)}</span>
                    </span>
                  ))}
              </div>
            </Section>
          )}
          {data.notes.map((n) => (
            <p key={n} className="text-[11px] text-yellow-300/90">
              {n}
            </p>
          ))}
        </>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ fills --

function FillsView({ bots, enabled, version }: { bots: GridBot[]; enabled: boolean; version: number }) {
  const [kind, setKind] = usePersistentState<"" | FillKind>("ac:account-fills-kind", "");
  const [market, setMarket] = usePersistentState<"" | AccountMarket>("ac:account-fills-market", "");
  const [fills, setFills] = useState<AccountFill[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const res = await fetchAccountFills({ kind: kind || undefined, market: market || undefined, limit: 300 }, signal);
      setFills(res.fills);
      setError(null);
    } catch (err) {
      if ((err as Error).name !== "AbortError") setError((err as Error).message);
    }
  }, [kind, market]);

  useEffect(() => {
    if (!enabled) return;
    const ctrl = new AbortController();
    void load(ctrl.signal);
    return () => ctrl.abort();
  }, [enabled, load, version]);

  // One order usually fills in several pieces; a classification applies to all of them.
  const classify = async (f: AccountFill, k: FillKind | null, botId: string | null) => {
    const keys = (fills ?? []).filter((x) => x.market === f.market && x.symbol === f.symbol && x.order_id === f.order_id).map((x) => x.key);
    setBusy(f.key);
    try {
      await classifyAccount(keys.length ? keys : [f.key], k, botId);
      await load();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  };

  if (!enabled) return <p className="px-3 py-4 text-[12px] leading-relaxed text-mute">Add a read-only Binance key (Setup) and import to see your fills.</p>;
  const sel = "h-6 rounded border border-line bg-base px-1 text-[11px] text-ink outline-none";
  return (
    <div className="text-[12px]">
      <div className="flex items-center gap-1.5 border-b border-line px-3 py-1.5">
        <select className={sel} value={kind} onChange={(e) => setKind(e.target.value as "" | FillKind)}>
          <option value="">All kinds</option>
          <option value="manual">Mine</option>
          <option value="bot">Bot</option>
          <option value="unknown">Unknown</option>
        </select>
        <select className={sel} value={market} onChange={(e) => setMarket(e.target.value as "" | AccountMarket)}>
          <option value="">Spot and futures</option>
          <option value="spot">Spot</option>
          <option value="futures">USD-M futures</option>
        </select>
      </div>
      {error && <div className="border-b border-line bg-down/10 px-3 py-2 text-[11px] text-down">{error}</div>}
      {fills === null && !error && <Loader2 className="mx-auto my-4 h-4 w-4 animate-spin text-mute" />}
      {fills !== null && fills.length === 0 && <p className="px-3 py-4 text-[12px] text-mute">No fills imported{kind || market ? " for this filter" : " yet"}.</p>}
      {(fills ?? []).map((f) => (
        <div key={f.key} className="border-b border-line/60 px-3 py-1.5">
          <div className="flex items-center gap-1.5">
            <span className={clsx("text-[11px] font-semibold", f.side === "buy" ? "text-up" : "text-down")}>{f.side === "buy" ? "Buy" : "Sell"}</span>
            <span className="font-medium text-ink">{displaySymbol(f.symbol)}</span>
            <span className="text-[10px] text-mute">{f.market === "futures" ? `futures${f.position_side !== "BOTH" ? ` ${f.position_side}` : ""}` : "spot"}</span>
            <KindBadge kind={f.kind} overridden={f.overridden} />
            <div className="flex-1" />
            {busy === f.key && <Loader2 className="h-3.5 w-3.5 animate-spin text-mute" />}
            <KindSelect kind={f.kind} botId={f.bot_id} overridden={f.overridden} bots={bots.filter((b) => b.params.symbol === f.symbol)} busy={!!busy} onChange={(k, id) => void classify(f, k, id)} />
          </div>
          <div className="mt-0.5 flex gap-2 font-mono text-[11px]">
            <span className="text-mute">{when(f.time)}</span>
            <span className="text-ink">
              {qty(f.qty)} @ {formatPrice(f.price)}
            </span>
            {f.realized_pnl != null && Math.abs(f.realized_pnl) > 0 && <span className={tone(f.realized_pnl)}>{money(f.realized_pnl, true)}</span>}
          </div>
          <div className="truncate text-[10px] text-mute" title={`${f.reason}${f.client_order_id ? ` · clientOrderId ${f.client_order_id}` : ""}`}>
            {f.kind === "bot" && f.bot_id && `${bots.find((b) => b.id === f.bot_id)?.name ?? "Grid bot"}: `}
            {f.reason}
          </div>
        </div>
      ))}
    </div>
  );
}

/** The key on its own, for the Settings dialog. */
export function BinanceKeySettings() {
  const [status, setStatus] = useState<BinanceKeyStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const ctrl = new AbortController();
    fetchBinanceKey(ctrl.signal)
      .then(setStatus)
      .catch((err: Error) => err.name !== "AbortError" && setError(err.message));
    return () => ctrl.abort();
  }, []);
  return (
    <div className="space-y-1.5">
      {error && <Notice tone="error">{error}</Notice>}
      {status && <KeySetup status={status} onChanged={setStatus} />}
      <p className="text-[11px] text-mute">The import, your positions and every imported fill are in the Account tab of the side panel.</p>
    </div>
  );
}

// -------------------------------------------------------------- holdings watch --

/** Sell-or-trim alerts on the coins held on Binance, added and removed as they are bought and sold. */
function HoldingsWatchSetup({ enabled: keyOk }: { enabled: boolean }) {
  const [st, setSt] = useState<HoldingsWatchStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const ctrl = new AbortController();
    fetchHoldingsWatch(ctrl.signal)
      .then(setSt)
      .catch((err) => {
        if ((err as Error).name !== "AbortError") setError((err as Error).message);
      });
    return () => ctrl.abort();
  }, []);
  if (!st) return error ? <Notice tone="error">{error}</Notice> : null;
  const s = st.settings;
  const save = async (patch: Partial<HoldingsWatchSettings>) => {
    setBusy(true);
    try {
      setSt(await saveHoldingsWatch({ ...s, ...patch }));
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const last = st.last;
  return (
    <Section
      title="Watch my holdings"
      right={
        s.enabled && (
          <button
            type="button"
            className="btn-ghost h-6 px-1.5 text-[11px]"
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              try {
                setSt(await runHoldingsWatch());
              } catch (err) {
                setError((err as Error).message);
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : "Check now"}
          </button>
        )
      }
    >
      <label className="flex items-center gap-2 text-[11px] text-ink">
        <input type="checkbox" checked={s.enabled} disabled={busy || (!keyOk && !s.enabled)} onChange={(e) => void save({ enabled: e.target.checked })} />
        Alert me when a coin I hold loses support or is rejected at resistance
      </label>
      <div className="flex flex-wrap items-center gap-2 text-[11px] text-mute">
        <span>Checked on every</span>
        <select
          aria-label="Timeframe"
          className="h-6 rounded border border-line bg-transparent px-1 text-[11px] text-ink outline-none"
          value={s.interval}
          disabled={busy}
          onChange={(e) => void save({ interval: e.target.value as HoldingsWatchSettings["interval"] })}
        >
          <option value="1h">1h</option>
          <option value="4h">4h</option>
          <option value="1d">1D</option>
        </select>
        <span>close</span>
        <label className="flex items-center gap-1">
          <input type="checkbox" checked={s.include_bots} disabled={busy} onChange={(e) => void save({ include_bots: e.target.checked })} />
          also my tracked grid bots&apos; coins
        </label>
      </div>
      {!keyOk && <Notice>Add a read-only Binance key above to use it.</Notice>}
      {error && <Notice tone="error">{error}</Notice>}
      {last?.error && <Notice tone="warn">{last.error}</Notice>}
      {s.enabled && last && !last.error && (
        <p className="text-[11px] text-mute">
          {last.coins.length
            ? `Watching ${last.coins.map((c) => displaySymbol(c.symbol) + (c.source === "grid bot" ? " (bot)" : "")).join(", ")}.`
            : "No coins worth watching right now."}
          {last.added.length > 0 && ` Added ${last.added.map(displaySymbol).join(", ")}.`}
          {last.removed.length > 0 && ` Stopped watching ${last.removed.map(displaySymbol).join(", ")} (sold).`} Checked {when(last.at)}.
        </p>
      )}
      <Notice>{st.bot_note}</Notice>
    </Section>
  );
}

// ------------------------------------------------------------------ panel --

/**
 * The user's Binance account through a read-only API key: the key itself (added, tested and removed here; stored on
 * the backend only), the fill import into the journal, open positions (own and bots' apart) and every imported fill
 * with whose it is (mine, a bot's, unknown) and why, which the user can correct.
 */
export default function AccountPanel(props: Partial<DockPanelProps> = {}) {
  const [tab, setTab] = usePersistentState<Tab>("ac:account-tab", "setup");
  const [status, setStatus] = useState<ImportStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [bots, setBots] = useState<GridBot[]>([]);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setStatus(await fetchImportStatus(signal));
      setError(null);
    } catch (err) {
      if ((err as Error).name !== "AbortError") setError((err as Error).message);
    }
  }, []);

  useEffect(() => {
    const ctrl = new AbortController();
    void load(ctrl.signal);
    fetchGridBots(ctrl.signal)
      .then((r) => setBots(r.bots))
      .catch(() => undefined);
    const timer = setInterval(() => void load(), STATUS_MS);
    return () => {
      ctrl.abort();
      clearInterval(timer);
    };
  }, [load]);

  const enabled = !!status?.key.configured && status.key.ok !== false;
  // A new import or key changes what the fills list shows.
  const version = (status?.last_import?.at ?? 0) + (status?.fills ?? 0);

  return (
    <div className="flex h-full min-h-0 flex-col text-xs">
      <div className="flex items-center gap-1 border-b border-line px-2 py-1.5">
        {(["setup", "positions", "pnl", "coach", "fills"] as const).map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => setTab(t)}
            className={clsx("rounded px-2 py-1 text-[11px] font-medium", tab === t ? "bg-panel2 text-ink" : "text-mute hover:text-ink")}
          >
            {t === "setup" ? "Setup" : t === "positions" ? "Positions" : t === "pnl" ? "PnL" : t === "coach" ? "Coach" : `Fills${status ? ` · ${status.fills}` : ""}`}
          </button>
        ))}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {error && <div className="border-b border-line bg-down/10 px-3 py-2 text-[11px] text-down">{error}</div>}
        {tab === "setup" && (
          <div className="space-y-4 px-3 py-2">
            {!status && !error && <Loader2 className="mx-auto h-4 w-4 animate-spin text-mute" />}
            {status && (
              <>
                <KeySetup status={status.key} onChanged={(key) => setStatus({ ...status, key })} />
                <ImportSetup key={status.settings.symbols.join(",")} status={status} onStatus={setStatus} />
                <HoldingsWatchSetup enabled={enabled} />
              </>
            )}
          </div>
        )}
        {tab === "positions" && <PositionsView bots={bots} enabled={enabled} onChartOverlays={props.onChartOverlays} />}
        {tab === "pnl" && <PnlView enabled={enabled} version={version} />}
        {tab === "coach" && <CoachView enabled={enabled} version={version} />}
        {tab === "fills" && <FillsView bots={bots} enabled={enabled} version={version} />}
      </div>
    </div>
  );
}
