"use client";

import clsx from "clsx";
import { Loader2, Play, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { usePersistentState } from "@/hooks/usePersistentState";
import {
  DESK_CHANGED,
  DESK_SELECT_KEY,
  DESK_TIMEFRAMES,
  STATUS_LABEL,
  deskOverlays,
  fetchDesk,
  fetchDeskCalls,
  fetchDeskWallet,
  resetDeskWallet,
  runDesk,
  saveDeskSettings,
  type DeskCall,
  type DeskRun,
  type DeskSettings,
  type DeskState,
  type DeskTimeframe,
} from "@/lib/desk";
import type { DockPanelProps } from "@/lib/dock";
import { displaySymbol, formatPrice } from "@/lib/format";
import type { PaperWallet } from "@/lib/paper";

const REFRESH_MS = 30_000;
type Tab = "calls" | "record" | "settings";
type Filter = "active" | "closed" | "all" | "watched";

const STATUS_CLASS: Record<string, string> = {
  waiting: "border-line text-mute",
  open: "border-accent/50 text-accent",
  tp: "border-up/50 text-up",
  invalidated: "border-down/50 text-down",
  timed_out: "border-yellow-400/40 text-yellow-300",
  expired: "border-line text-mute",
  cancelled: "border-line text-mute",
};

function ago(ts: number): string {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m ago`;
  if (s < 86_400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86_400)}d ago`;
}

function usd(v: number | null | undefined, signed = false): string {
  if (v == null || !Number.isFinite(v)) return "–";
  const s = Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `${v < 0 ? "−" : signed && v > 0 ? "+" : ""}$${s}`;
}

function rText(r: number | null | undefined): string {
  return r == null || !Number.isFinite(r) ? "–" : `${r > 0 ? "+" : r < 0 ? "−" : ""}${Math.abs(r).toFixed(2)}R`;
}

function tone(v: number | null | undefined): string {
  return v == null || Math.abs(v) < 1e-9 ? "text-mute" : v > 0 ? "text-up" : "text-down";
}

function pct(a: number, b: number): string {
  const v = (b / a - 1) * 100;
  return `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(1)}%`;
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

function Segmented<T extends string>({ value, options, onChange }: { value: T; options: [T, string][]; onChange(v: T): void }) {
  return (
    <div className="inline-flex rounded border border-line bg-panel2 p-0.5">
      {options.map(([v, label]) => (
        <button
          key={v}
          type="button"
          onClick={() => onChange(v)}
          className={clsx("rounded px-2 py-0.5 text-[11px]", value === v ? "bg-panel text-ink" : "text-mute hover:text-ink")}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

function Check({ checked, onChange, label, hint }: { checked: boolean; onChange(v: boolean): void; label: string; hint?: string }) {
  return (
    <label className="flex cursor-pointer items-start gap-2 py-0.5 text-[12px] text-ink">
      <input type="checkbox" className="mt-0.5 accent-blue-500" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span>
        {label}
        {hint && <span className="block text-[10px] text-mute">{hint}</span>}
      </span>
    </label>
  );
}

// ------------------------------------------------------------------ calls --

function CallRow({ c, selected, onSelect }: { c: DeskCall; selected: boolean; onSelect(): void }) {
  const tf = c.interval.toUpperCase();
  const r = c.status === "open" ? c.open_r : c.r;
  return (
    <div className={clsx("border-b border-line/60", selected && "bg-panel2/60")}>
      <button type="button" onClick={onSelect} className="block w-full px-3 py-2 text-left hover:bg-panel2/40">
        <div className="flex items-center gap-1.5">
          <span className="font-medium text-ink">{displaySymbol(c.symbol)}</span>
          <span className="rounded bg-panel2 px-1 text-[10px] text-mute">{tf}</span>
          <span className={clsx("rounded border px-1 py-px text-[10px]", STATUS_CLASS[c.status])}>
            {c.shadow && c.status === "waiting" ? "Watching" : c.shadow && c.status === "open" ? "Watching (in zone)" : STATUS_LABEL[c.status]}
            {c.shadow && c.skip_reason ? ` · not called: ${c.skip_reason}` : ""}
          </span>
          {c.data_source === "synthetic" && <span className="text-[10px] text-yellow-300">demo</span>}
          <span className="flex-1" />
          <span className={clsx("font-mono text-[11px]", tone(r))} title={c.status === "waiting" ? `Expected ${rText(c.expected_r)} after fees` : undefined}>
            {c.status === "waiting" || c.status === "expired" ? `${Math.round(c.confidence * 100)}%` : rText(r)}
            {c.shadow && c.status === "waiting" && <span className={clsx("ml-1", tone(c.expected_r))}>{rText(c.expected_r)}</span>}
          </span>
        </div>
        <div className="mt-0.5 flex flex-wrap gap-x-2 font-mono text-[11px] text-mute">
          <span>
            buy <span className="text-ink">{formatPrice(c.zone_low)}–{formatPrice(c.zone_high)}</span>
          </span>
          <span>
            TP <span className="text-up">{formatPrice(c.tp)}</span>
          </span>
          <span>
            stop <span className="text-down">{formatPrice(c.stop)}</span>
          </span>
          <span className="ml-auto font-sans">{ago(c.created_at)}</span>
        </div>
      </button>
      {selected && (
        <div className="space-y-1 px-3 pb-2 text-[11px] leading-snug text-mute">
          <div className="text-ink">{c.setup}</div>
          <div>
            Confidence <span className="text-ink">{Math.round(c.confidence * 100)}%</span> of the take-profit first · {c.rr}R · expected{" "}
            <span className={tone(c.expected_r)}>{rText(c.expected_r)}</span> · limit {formatPrice(c.entry)} ({pct(c.price_at_call, c.entry)} from price then)
          </div>
          <div>{c.confidence_basis}</div>
          <div>
            Paper:{" "}
            {c.notional ? (
              <span className="text-ink">
                {usd(c.notional)} ({c.size_pct}% of the wallet)
              </span>
            ) : (
              c.paper_note || "nothing placed"
            )}
            {c.notional && c.paper_note ? ` · ${c.paper_note}` : ""}
          </div>
          {c.fill_price != null && (
            <div>
              Bought {formatPrice(c.fill_price)}
              {c.exit_price != null && ` · out ${formatPrice(c.exit_price)} (${pct(c.fill_price, c.exit_price)})`}
              {c.mfe_r != null && ` · best ${rText(c.mfe_r)}, worst ${rText(c.mae_r)}`}
            </div>
          )}
          {c.status === "waiting" && <div>Expires {new Date(c.expires_at * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })} if it doesn&apos;t fill.</div>}
          {c.notes.map((n) => (
            <div key={n}>{n}</div>
          ))}
        </div>
      )}
    </div>
  );
}

/** Why the last runs made the calls they made (or none). */
function LastRuns({ runs }: { runs: Record<string, DeskRun> }) {
  const rows = Object.entries(runs).sort(([a], [b]) => DESK_TIMEFRAMES.indexOf(a as DeskTimeframe) - DESK_TIMEFRAMES.indexOf(b as DeskTimeframe));
  if (!rows.length) return <p className="text-[11px]">It hasn&apos;t run yet: the first look comes at the next candle close, or press Run in Settings.</p>;
  return (
    <div className="space-y-1 text-[11px]">
      {rows.map(([tf, r]) => (
        <p key={tf}>
          <span className="text-ink">{tf.toUpperCase()}</span> {ago(r.at)}: {r.coins} coins, {r.zones ?? 0} new buy zone{r.zones === 1 ? "" : "s"}, {r.calls} called
          {r.watched ? `, ${r.watched} watched` : ""}.
          {r.calls === 0 && r.best && (
            <>
              {" "}
              Best was {displaySymbol(r.best.symbol)} {r.best.setup} at <span className={tone(r.best.expected_r)}>{rText(r.best.expected_r)}</span> expected after fees.
            </>
          )}
        </p>
      ))}
    </div>
  );
}

// ----------------------------------------------------------------- record --

function Record({ d, wallet }: { d: DeskState; wallet: PaperWallet | null }) {
  const s = d.summary;
  const cal = s.calibration;
  return (
    <div className="space-y-4 px-3 py-2">
      <Section title="What it learns from">
        <p className="text-[12px] leading-snug text-ink">
          {s.tracked} buy zone{s.tracked === 1 ? "" : "s"} tracked ({s.calls} called, {s.tracked - s.calls} watched); {s.level_hits} of the {s.learned_from} whose level was decided reached it.
          {s.calls === 0 && " The record below is from watched zones: none was traded."}
        </p>
        <p className="text-[10px] leading-snug text-mute">
          Each setup&apos;s edge is its hits against what random odds would give (1.0x = none), starting from the coin&apos;s backtest and moving with every finished zone. Older results count less.
        </p>
      </Section>
      <Section title="Is the confidence right?">
        <p className="text-[12px] leading-snug text-ink">{s.verdict}</p>
        {cal.bins.length > 0 && (
          <div className="space-y-1 pt-1">
            {cal.bins.map((b) => (
              <div key={b.from} className="grid grid-cols-[4.5rem_1fr_3rem] items-center gap-2 font-mono text-[11px]">
                <span className="text-mute">
                  {Math.round(b.from * 100)}–{Math.round(b.to * 100)}%
                </span>
                <div className="relative h-3 rounded bg-panel2">
                  <div className="absolute inset-y-0 left-0 rounded bg-accent/40" style={{ width: `${b.actual * 100}%` }} />
                  <div className="absolute inset-y-0 w-0.5 bg-ink" style={{ left: `${b.predicted * 100}%` }} title={`Said ${Math.round(b.predicted * 100)}%`} />
                </div>
                <span className="text-right text-ink" title={`${b.calls} zones`}>
                  {Math.round(b.actual * 100)}%
                </span>
              </div>
            ))}
            <p className="text-[10px] text-mute">Bar: how often zones (called and watched) in each range reached the take-profit. Line: what they said.</p>
          </div>
        )}
      </Section>
      <Section title={`Working now (last ${s.recent_days} days)`}>
        {s.working_now.length === 0 && <p className="text-[11px] text-mute">Not enough finished zones in the last {s.recent_days} days yet (5 per setup).</p>}
        {s.working_now.map((r) => (
          <div key={r.bucket} className="flex items-baseline gap-2 text-[11px]">
            <span className="min-w-0 flex-1 truncate text-ink" title={r.setup}>
              {r.setup}
            </span>
            <span className="font-mono text-mute" title={r.p != null ? `Random odds would do this ${Math.round(r.p * 100)}% of the time` : undefined}>
              {r.hits}/{r.zones} · {r.verdict}
            </span>
            <span className={clsx("w-12 text-right font-mono", r.verdict === "working" ? "text-up" : r.verdict === "not working" ? "text-down" : "text-mute")}>{r.lift.toFixed(2)}x</span>
          </div>
        ))}
      </Section>
      <Section title="By setup">
        {s.setups.length === 0 && <p className="text-[11px] text-mute">No finished zones yet. Results appear here as zones finish.</p>}
        {s.setups.length > 0 && (
          <table className="w-full text-[11px]">
            <thead>
              <tr className="text-[10px] text-mute">
                <th className="text-left font-normal">Setup</th>
                <th className="text-right font-normal" title="Reached the take-profit / finished, calls and watched zones">TP/all</th>
                <th className="text-right font-normal">Avg</th>
                <th className="text-right font-normal" title="Hits against what random odds would give: 1.0x = no edge">Edge</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {s.setups.map((r) => (
                <tr key={r.bucket}>
                  <td className="max-w-[11rem] truncate py-0.5 font-sans text-ink" title={r.setup}>
                    {r.setup}
                  </td>
                  <td className="text-right text-ink">
                    {r.tp}/{r.calls}
                  </td>
                  <td className={clsx("text-right", tone(r.avg_r))}>{rText(r.avg_r)}</td>
                  <td className={clsx("text-right", r.lift > 1.05 ? "text-up" : r.lift < 0.95 ? "text-down" : "text-mute")}>{r.lift.toFixed(2)}x</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>
      <Section title="The desk's paper wallet">
        {!wallet && <Loader2 className="h-4 w-4 animate-spin text-mute" />}
        {wallet && (
          <div className="space-y-1 font-mono text-[11px]">
            <div className="flex flex-wrap gap-x-3">
              <span className="text-mute">
                Equity <span className="text-ink">{usd(wallet.equity)}</span>
              </span>
              <span className={tone(wallet.return_pct)}>
                {wallet.return_pct >= 0 ? "+" : "−"}
                {Math.abs(wallet.return_pct).toFixed(2)}%
              </span>
              <span className="text-mute">
                cash {usd(wallet.cash)} · set aside {usd(wallet.reserved)}
              </span>
            </div>
            <div className="text-mute">
              Realized <span className={tone(wallet.realized_pnl)}>{usd(wallet.realized_pnl, true)}</span> · open{" "}
              <span className={tone(wallet.unrealized_pnl)}>{usd(wallet.unrealized_pnl, true)}</span>
              {wallet.stats.win_rate != null && ` · ${wallet.stats.wins}/${wallet.stats.sells} sells in profit`}
            </div>
            {wallet.holdings
              .filter((h) => h.qty > 0)
              .map((h) => (
                <div key={h.symbol} className="flex gap-2">
                  <span className="text-ink">{displaySymbol(h.symbol)}</span>
                  <span className="text-mute">@ {formatPrice(h.avg_cost)}</span>
                  <span className="flex-1" />
                  <span className="text-ink">{usd(h.value)}</span>
                  <span className={tone(h.unrealized_pnl)}>{usd(h.unrealized_pnl, true)}</span>
                </div>
              ))}
            {wallet.error && <div className="font-sans text-yellow-300">{wallet.error}</div>}
          </div>
        )}
      </Section>
    </div>
  );
}

// --------------------------------------------------------------- settings --

function Settings({ d, onSaved }: { d: DeskState; onSaved(d: DeskState): void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [cash, setCash] = useState("1000");
  const [confirmReset, setConfirmReset] = useState(false);
  // Sliders show their value while dragged and save once on release.
  const [draft, setDraft] = useState<Partial<DeskSettings>>({});
  const s = { ...d.settings, ...draft };
  // Patches merge onto the latest settings, so two quick clicks don't lose the first.
  const latest = useRef(d.settings);
  useEffect(() => {
    latest.current = d.settings;
  }, [d.settings]);

  const save = async (patch: Partial<DeskSettings>) => {
    setBusy("save");
    const next = { ...latest.current, ...patch };
    latest.current = next;
    try {
      onSaved(await saveDeskSettings(next));
      setDraft((x) => Object.fromEntries(Object.entries(x).filter(([k]) => !(k in patch))));
      setMsg(null);
    } catch (err) {
      setMsg((err as Error).message);
    } finally {
      setBusy(null);
    }
  };
  const run = async (tf: DeskTimeframe) => {
    setBusy(tf);
    setMsg(null);
    try {
      const res = await runDesk(tf);
      onSaved(res.status);
      setMsg(res.calls.length ? `${res.calls.length} new ${tf.toUpperCase()} call${res.calls.length === 1 ? "" : "s"}.` : `No ${tf.toUpperCase()} call worth making right now.`);
    } catch (err) {
      setMsg((err as Error).message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="space-y-4 px-3 py-2">
      {!d.enabled_by_server && <p className="rounded border border-yellow-400/40 bg-yellow-400/10 px-2 py-1.5 text-[11px] text-yellow-200">AGENT_DESK=off on the server: the desk only calls when you press Run.</p>}
      <Section title="Calls">
        <Check checked={s.enabled} onChange={(v) => void save({ enabled: v })} label="Make calls on its own" hint="At every candle close of the timeframes below" />
        <div className="flex items-center gap-3 pl-5 text-[12px]">
          {DESK_TIMEFRAMES.map((tf) => (
            <label key={tf} className="flex items-center gap-1">
              <input
                type="checkbox"
                className="accent-blue-500"
                checked={s.timeframes.includes(tf)}
                onChange={(e) => void save({ timeframes: e.target.checked ? [...s.timeframes, tf] : s.timeframes.filter((x) => x !== tf) })}
              />
              {tf.toUpperCase()}
            </label>
          ))}
        </div>
        <label className="flex items-center justify-between gap-2 py-1 text-[12px]">
          <span>
            Minimum confidence
            <span className="block text-[10px] text-mute">Calls below this aren&apos;t made</span>
          </span>
          <span className="flex items-center gap-1 font-mono">
            <input
              type="range"
              min={10}
              max={80}
              step={5}
              value={Math.round(s.min_confidence * 100)}
              onChange={(e) => setDraft((x) => ({ ...x, min_confidence: Number(e.target.value) / 100 }))}
              onPointerUp={() => draft.min_confidence != null && void save({ min_confidence: draft.min_confidence })}
              onKeyUp={() => draft.min_confidence != null && void save({ min_confidence: draft.min_confidence })}
              className="w-28 accent-blue-500"
            />
            {Math.round(s.min_confidence * 100)}%
          </span>
        </label>
        <label className="flex items-center justify-between gap-2 py-1 text-[12px]" title="Expected R after fees: confidence × reward − (1 − confidence) − fees. Lower it to let the desk call more.">
          <span>Least expected gain</span>
          <span className="flex items-center gap-1 font-mono">
            <input
              type="range"
              min={0}
              max={50}
              step={5}
              value={Math.round((s.min_expected_r ?? 0.1) * 100)}
              onChange={(e) => setDraft((x) => ({ ...x, min_expected_r: Number(e.target.value) / 100 }))}
              onPointerUp={() => draft.min_expected_r != null && void save({ min_expected_r: draft.min_expected_r })}
              onKeyUp={() => draft.min_expected_r != null && void save({ min_expected_r: draft.min_expected_r })}
              className="w-28 accent-blue-500"
            />
            {(s.min_expected_r ?? 0.1).toFixed(2)}R
          </span>
        </label>
        <label className="flex items-center justify-between gap-2 py-1 text-[12px]" title="Calls only: the desk watches any number of zones">
          <span>Calls running at most</span>
          <input
            type="number"
            min={1}
            max={200}
            value={s.max_active}
            onChange={(e) => Number(e.target.value) > 0 && void save({ max_active: Math.min(200, Number(e.target.value)) })}
            className="h-6 w-16 rounded border border-line bg-base px-1 text-right font-mono text-[12px] text-ink"
          />
        </label>
      </Section>
      <Section title={`Coins (${d.symbols.length})`}>
        <Check checked={s.follow_watchlist} onChange={(v) => void save({ follow_watchlist: v })} label="Follow my active watchlist" />
        <p className="font-mono text-[11px] leading-relaxed text-mute">{d.symbols.map(displaySymbol).join(" · ")}</p>
      </Section>
      <Section title={`Discord / Telegram${d.channels.discord || d.channels.telegram ? "" : " (not set up)"}`}>
        <Check checked={s.notify_new} onChange={(v) => void save({ notify_new: v })} label="New calls" />
        <Check checked={s.notify_fills} onChange={(v) => void save({ notify_fills: v })} label="When a buy fills" />
        <Check checked={s.notify_results} onChange={(v) => void save({ notify_results: v })} label="Results (take-profit, invalidated, timed out)" />
        <Check checked={s.notify_expired} onChange={(v) => void save({ notify_expired: v })} label="Calls that expire unfilled" />
      </Section>
      <Section title="Run now">
        <p className="text-[11px] text-mute">Look at the latest closed candle now instead of waiting for the next close.</p>
        <div className="flex gap-1.5">
          {DESK_TIMEFRAMES.map((tf) => (
            <button key={tf} type="button" disabled={!!busy} onClick={() => void run(tf)} className="btn-ghost h-7 gap-1 border border-line px-2 text-[11px]">
              {busy === tf ? <Loader2 className="h-3 w-3 animate-spin" /> : <Play className="h-3 w-3" />}
              {tf.toUpperCase()}
            </button>
          ))}
        </div>
        {d.running && <p className="text-[11px] text-mute">A run is in progress…</p>}
      </Section>
      <Section title="Paper wallet">
        <div className="flex items-center gap-1.5 text-[12px]">
          Start again with
          <input value={cash} onChange={(e) => setCash(e.target.value)} className="h-6 w-20 rounded border border-line bg-base px-1 text-right font-mono text-[12px] text-ink" />
          USDT
          {confirmReset ? (
            <button
              type="button"
              className="btn-ghost h-6 px-2 text-[11px] text-down"
              onBlur={() => setConfirmReset(false)}
              onClick={async () => {
                setConfirmReset(false);
                try {
                  await resetDeskWallet(Number(cash));
                  setMsg("The desk's wallet was reset; past calls keep their results.");
                } catch (err) {
                  setMsg((err as Error).message);
                }
              }}
            >
              Confirm reset
            </button>
          ) : (
            <button type="button" className="btn-ghost h-6 border border-line px-2 text-[11px]" onClick={() => setConfirmReset(true)}>
              Reset
            </button>
          )}
        </div>
      </Section>
      {msg && <p className="text-[11px] text-ink">{msg}</p>}
    </div>
  );
}

// ------------------------------------------------------------------ panel --

/**
 * The agent desk: spot buy calls the agent makes on its own on the watchlist at 1h/4h/1d closes, each with a buy
 * zone, take-profit, invalidation and a confidence it learns from its results; traded in a paper wallet of its own.
 */
export default function DeskPanel(props: DockPanelProps) {
  const { onChartOverlays, onPickSymbol } = props;
  const [tab, setTab] = usePersistentState<Tab>("ac:desk-tab", "calls");
  const [filter, setFilter] = usePersistentState<Filter>("ac:desk-filter", "active");
  const [d, setD] = useState<DeskState | null>(null);
  const [calls, setCalls] = useState<DeskCall[]>([]);
  const [watched, setWatched] = useState<DeskCall[]>([]);
  const [wallet, setWallet] = useState<PaperWallet | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    try {
      const [state, list, shadows] = await Promise.all([fetchDesk(signal), fetchDeskCalls("", signal), fetchDeskCalls("active", signal, 200, true)]);
      setD(state);
      setCalls(list.calls);
      setWatched(shadows.calls);
      setError(null);
      fetchDeskWallet(signal)
        .then(setWallet)
        .catch(() => undefined);
    } catch (err) {
      if ((err as Error).name !== "AbortError") setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const ctrl = new AbortController();
    void load(ctrl.signal);
    const id = setInterval(() => void load(), REFRESH_MS);
    return () => {
      ctrl.abort();
      clearInterval(id);
    };
  }, [load]);

  // The desk changed a call (alerts socket): refresh now rather than on the next poll.
  useEffect(() => {
    const now = () => void load();
    window.addEventListener(DESK_CHANGED, now);
    return () => window.removeEventListener(DESK_CHANGED, now);
  }, [load]);

    // The selected call on its coin's charts; cleared when another is picked or the panel closes.
  const drawn = useRef<string | null>(null);
  const draw = useRef(onChartOverlays);
  useEffect(() => {
    draw.current = onChartOverlays;
  }, [onChartOverlays]);
  useEffect(() => () => {
    if (drawn.current) draw.current("desk:selected", drawn.current, []);
  }, []);

  // A chart chip asked for this call: open the list with it selected (works on first mount too).
  const [asked, setAsked] = usePersistentState<string>(DESK_SELECT_KEY, "");
  useEffect(() => {
    if (!asked) return;
    const c = [...calls, ...watched].find((x) => x.id === asked);
    if (!c) return;
    setAsked("");
    setTab("calls");
    setFilter(c.status === "waiting" || c.status === "open" ? "active" : "closed");
    if (selected !== c.id) select(c);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [asked, calls, watched]);

  const select = (c: DeskCall) => {
    if (selected === c.id) {
      setSelected(null);
      if (drawn.current) onChartOverlays("desk:selected", drawn.current, []);
      drawn.current = null;
      return;
    }
    setSelected(c.id);
    if (drawn.current && drawn.current !== c.symbol) onChartOverlays("desk:selected", drawn.current, []);
    onChartOverlays("desk:selected", c.symbol, deskOverlays(c));
    drawn.current = c.symbol;
    onPickSymbol(c.symbol, c.interval);
  };

  const shown = useMemo(
    () =>
      filter === "watched"
        ? watched
        : filter === "all"
          ? [...calls, ...watched].sort((a, b) => b.created_at - a.created_at)
          : calls.filter((c) => (filter === "active" ? c.status === "waiting" || c.status === "open" : c.status !== "waiting" && c.status !== "open")),
    [calls, watched, filter],
  );
  const s = d?.summary;

  return (
    <div className="flex h-full min-h-0 flex-col text-xs">
      <div className="flex items-center gap-1 border-b border-line px-2 py-1.5">
        {(["calls", "record", "settings"] as const).map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => setTab(t)}
            className={clsx("rounded px-2 py-1 text-[11px] font-medium", tab === t ? "bg-panel2 text-ink" : "text-mute hover:text-ink")}
          >
            {t === "calls" ? `Calls${s ? ` · ${s.active}` : ""}` : t === "record" ? "Record" : "Settings"}
          </button>
        ))}
        <div className="flex-1" />
        <button type="button" onClick={() => void load()} className="btn-ghost h-6 w-6 p-0" title="Refresh">
          <RefreshCw className={clsx("h-3.5 w-3.5", loading && "animate-spin")} />
        </button>
      </div>
      {s && (
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 border-b border-line px-3 py-1.5 font-mono text-[11px] text-mute">
          {wallet && (
            <span>
              Wallet <span className="text-ink">{usd(wallet.equity)}</span>{" "}
              <span className={tone(wallet.return_pct)}>
                ({wallet.return_pct >= 0 ? "+" : "−"}
                {Math.abs(wallet.return_pct).toFixed(1)}%)
              </span>
            </span>
          )}
          <span>
            {s.waiting} waiting · {s.open} holding
          </span>
          <span>
            TP {s.tp}/{s.closed}
          </span>
          {s.total_r != null && <span className={tone(s.total_r)}>{rText(s.total_r)}</span>}
        </div>
      )}
      <div className="min-h-0 flex-1 overflow-y-auto">
        {error && <div className="border-b border-line bg-down/10 px-3 py-2 text-[11px] text-down">{error}</div>}
        {!d && !error && <Loader2 className="mx-auto mt-4 h-4 w-4 animate-spin text-mute" />}
        {d && tab === "calls" && (
          <>
            <div className="flex items-center gap-2 border-b border-line px-3 py-1.5">
              <Segmented<Filter> value={filter} options={[["active", "Running"], ["closed", "Finished"], ["watched", `Watching ${watched.length}`], ["all", "All"]]} onChange={setFilter} />
            </div>
            {filter === "watched" && (
              <p className="border-b border-line px-3 py-2 text-[11px] leading-snug text-mute">
                Buy zones the desk looked at but didn&apos;t call. It scores them like calls and learns from them, so it keeps learning about setups it
                doesn&apos;t trade yet. Never traded or announced.
              </p>
            )}
            {shown.length === 0 && (
              <div className="space-y-2 px-3 py-4 text-[12px] leading-relaxed text-mute">
                <p>
                  {filter === "active"
                    ? `No calls running. The desk looks at ${d.symbols.length} coins on ${d.settings.timeframes.map((t) => t.toUpperCase()).join(", ") || "no timeframes"} at every candle close and calls a buy zone only when its edge, after fees, is worth it.`
                    : filter === "watched"
                      ? "No zones being watched right now."
                      : filter === "all"
                        ? "No calls or watched zones yet."
                        : "No finished calls yet."}
                </p>
                {filter === "active" && <LastRuns runs={d.last_run} />}
              </div>
            )}
            {shown.map((c) => (
              <CallRow key={c.id} c={c} selected={selected === c.id} onSelect={() => select(c)} />
            ))}
          </>
        )}
        {d && tab === "record" && <Record d={d} wallet={wallet} />}
        {d && tab === "settings" && <Settings d={d} onSaved={setD} />}
      </div>
    </div>
  );
}
