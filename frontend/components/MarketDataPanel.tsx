"use client";

import clsx from "clsx";
import { Eye, EyeOff } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { usePersistentState } from "@/hooks/usePersistentState";
import { usePolled } from "@/hooks/usePolled";
import type { DockPanelProps } from "@/lib/dock";
import { displaySymbol, formatCompact, formatPct, formatPrice, splitSymbol } from "@/lib/format";
import {
  fetchCvd,
  fetchFunding,
  fetchLiquidationLevels,
  fetchLongShort,
  fetchOpenInterest,
  fetchWalls,
  liquidationOverlays,
  usd,
  wallOverlays,
  type LiquidationCluster,
  type MarketSource,
  type Wall,
} from "@/lib/marketdata";

const UP = "#22c55e";
const DOWN = "#ef4444";
const ACCENT = "#3b82f6";

const signed = (v: number, digits = 2) => `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(digits)}`;
const signedCompact = (v: number) => `${v >= 0 ? "+" : "−"}${formatCompact(Math.abs(v))}`;
const clockTime = (t: number) =>
  new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

function countdown(seconds: number): string {
  if (seconds <= 0) return "now";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  return h ? `${h}h ${String(m).padStart(2, "0")}m` : `${m}m ${String(s).padStart(2, "0")}s`;
}

function useNow(ms: number) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}

/** Tiny line chart; `baseline` draws a dashed reference (0 for CVD, 1 for the long/short ratio). */
function Sparkline({ values, color = ACCENT, height = 30, baseline }: { values: number[]; color?: string; height?: number; baseline?: number }) {
  if (values.length < 2) return <div style={{ height }} />;
  const lo = Math.min(...values, baseline ?? Infinity);
  const hi = Math.max(...values, baseline ?? -Infinity);
  const span = hi - lo || 1;
  const y = (v: number) => (height - 2 - ((v - lo) / span) * (height - 4)).toFixed(2);
  const pts = values.map((v, i) => `${((i / (values.length - 1)) * 100).toFixed(2)},${y(v)}`).join(" ");
  return (
    <svg viewBox={`0 0 100 ${height}`} preserveAspectRatio="none" className="w-full" style={{ height }} aria-hidden>
      {baseline !== undefined && (
        <line x1={0} x2={100} y1={y(baseline)} y2={y(baseline)} stroke="#374151" strokeDasharray="3 3" vectorEffect="non-scaling-stroke" />
      )}
      <polyline points={pts} fill="none" stroke={color} strokeWidth={1.25} vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

/** Bars around zero (funding history): green above, red below. */
function ZeroBars({ values, height = 30 }: { values: number[]; height?: number }) {
  if (!values.length) return <div style={{ height }} />;
  const max = Math.max(...values.map(Math.abs)) || 1;
  const mid = height / 2;
  const w = 100 / values.length;
  return (
    <svg viewBox={`0 0 100 ${height}`} preserveAspectRatio="none" className="w-full" style={{ height }} aria-hidden>
      <line x1={0} x2={100} y1={mid} y2={mid} stroke="#374151" vectorEffect="non-scaling-stroke" />
      {values.map((v, i) => {
        const h = Math.max((Math.abs(v) / max) * (mid - 1), 0.5);
        return <rect key={i} x={i * w + w * 0.15} width={w * 0.7} y={v >= 0 ? mid - h : mid} height={h} fill={v >= 0 ? UP : DOWN} />;
      })}
    </svg>
  );
}

function StrengthBar({ value, color }: { value: number; color: string }) {
  return (
    <div className="h-1 w-12 shrink-0 overflow-hidden rounded bg-panel2">
      <div className="h-full rounded" style={{ width: `${Math.round(Math.max(0.05, Math.min(1, value)) * 100)}%`, background: color }} />
    </div>
  );
}

function Stat({ label, value, tone, title }: { label: string; value: ReactNode; tone?: "up" | "down" | null; title?: string }) {
  return (
    <div className="min-w-0" title={title}>
      <div className="text-[10px] text-mute">{label}</div>
      <div className={clsx("truncate font-mono text-[12px]", tone === "up" ? "text-up" : tone === "down" ? "text-down" : "text-ink")}>
        {value}
      </div>
    </div>
  );
}

function ShowOnChart({ on, onToggle }: { on: boolean; onToggle(): void }) {
  return (
    <button
      type="button"
      onClick={onToggle}
      className={clsx("btn-ghost h-6 px-1.5 text-[11px]", on && "text-accent")}
      title={on ? "Remove from the chart" : "Draw on the chart"}
    >
      {on ? <Eye className="h-3.5 w-3.5" /> : <EyeOff className="h-3.5 w-3.5" />}
      Show on chart
    </button>
  );
}

function Section(p: { title: string; source?: MarketSource; note?: string; right?: ReactNode; children?: ReactNode }) {
  return (
    <section className="border-b border-line px-3 py-2.5">
      <div className="mb-1.5 flex items-center gap-2">
        <h3 className="text-[10px] font-semibold uppercase tracking-wide text-mute">{p.title}</h3>
        {p.source === "synthetic" && <span className="rounded bg-yellow-400/15 px-1 text-[9px] font-semibold text-yellow-300">DEMO</span>}
        <div className="flex-1" />
        {p.right}
      </div>
      {p.source === "unavailable" ? <p className="text-[11px] leading-snug text-mute">{p.note ?? "Not available right now."}</p> : p.children}
    </section>
  );
}

const Loading = () => <p className="text-[11px] text-mute">Loading…</p>;

function WallRow({ w, side }: { w: Wall; side: "bid" | "ask" }) {
  return (
    <div className="flex items-center gap-2 font-mono text-[11px]">
      <span className={clsx("w-20 shrink-0", side === "bid" ? "text-up" : "text-down")}>{formatPrice(w.price)}</span>
      <span className="w-14 shrink-0 text-ink">{usd(w.usd)}</span>
      <span className="w-14 shrink-0 text-mute">{formatPct(w.distance_pct)}</span>
      <StrengthBar value={w.strength} color={side === "bid" ? UP : DOWN} />
    </div>
  );
}

function ClusterRow({ c }: { c: LiquidationCluster }) {
  const color = c.side === "long" ? "#f97316" : "#a855f7";
  return (
    <div className="flex items-center gap-2 font-mono text-[11px]" title={`Mostly ${c.leverage_hint} positions, estimated`}>
      <span className="w-[7.5rem] shrink-0 truncate text-ink">
        {formatPrice(c.price_low)}–{formatPrice(c.price_high)}
      </span>
      <span className="w-12 shrink-0 text-mute">{formatPct(c.distance_pct)}</span>
      <span className="w-8 shrink-0 text-mute">{c.leverage_hint}</span>
      <StrengthBar value={c.weight} color={color} />
    </div>
  );
}

/**
 * Per-coin futures and order flow for the dock: funding, open interest, long/short ratios, CVD, order-book walls,
 * estimated liquidation clusters and recent liquidations. Walls and clusters can be drawn on the chart
 * (overlay keys "walls" and "liqs"); walls refresh every 15 s while shown.
 */
export default function MarketDataPanel({ symbol, interval, price, onChartOverlays }: DockPanelProps) {
  const isIndex = /^TOTAL\d?$/.test(symbol);
  const key = (k: string) => (isIndex ? null : `${k}:${symbol}`);
  const [wallsOn, setWallsOn] = usePersistentState("ac:walls-on-chart", false);
  const [liqsOn, setLiqsOn] = usePersistentState("ac:liqs-on-chart", false);

  const funding = usePolled(key("funding"), (s) => fetchFunding(symbol, 90, s), 60_000);
  const oi = usePolled(key("oi"), (s) => fetchOpenInterest(symbol, "1h", 168, s), 300_000);
  const ls = usePolled(key("ls"), (s) => fetchLongShort(symbol, "1h", 168, s), 300_000);
  const cvd = usePolled(isIndex ? null : `cvd:${symbol}:${interval}`, (s) => fetchCvd(symbol, interval, 200, s), 60_000);
  const walls = usePolled(key("walls"), (s) => fetchWalls(symbol, 5, "spot", s), wallsOn ? 15_000 : 60_000);
  const liq = usePolled(key("liq"), (s) => fetchLiquidationLevels(symbol, s), 60_000);
  const now = useNow(1000);

  // Overlays on the chart: redrawn on new data, cleared when switched off, on a new symbol and on unmount.
  const overlaysRef = useRef(onChartOverlays);
  useEffect(() => {
    overlaysRef.current = onChartOverlays;
  });
  useEffect(() => {
    if (wallsOn && !isIndex) overlaysRef.current("walls", symbol, wallOverlays(walls.data));
  }, [wallsOn, isIndex, symbol, walls.data]);
  useEffect(() => {
    if (!wallsOn) return;
    return () => overlaysRef.current("walls", symbol, []);
  }, [wallsOn, symbol]);
  useEffect(() => {
    if (liqsOn && !isIndex) overlaysRef.current("liqs", symbol, liquidationOverlays(liq.data));
  }, [liqsOn, isIndex, symbol, liq.data]);
  useEffect(() => {
    if (!liqsOn) return;
    return () => overlaysRef.current("liqs", symbol, []);
  }, [liqsOn, symbol]);

  if (isIndex) {
    return (
      <div className="flex h-full min-h-0 flex-col">
        <p className="px-3 py-4 text-[12px] leading-relaxed text-mute">
          Funding, open interest and order flow are per coin. {symbol} is a market-cap index; open a coin to see them.
        </p>
      </div>
    );
  }

  const parts = [funding, oi, ls, cvd, walls, liq];
  const demo = parts.some((p) => p.data?.source === "synthetic");
  const blocked = [funding, oi, ls].find((p) => p.data?.blocked)?.data?.note;
  const error = parts.find((p) => p.error && !p.data)?.error;
  const [base] = splitSymbol(symbol);

  const f = funding.data;
  const fc = f?.current;
  const nextIn = fc?.next_funding_time ? fc.next_funding_time - now / 1000 : null;
  const o = oi.data;
  const l = ls.data;
  const c = cvd.data;
  const day = c?.last_24h;
  const w = walls.data;
  const lv = liq.data;
  const clusters = lv?.clusters ?? [];
  const above = clusters.filter((x) => x.side === "short").sort((a, b) => a.distance_pct - b.distance_pct).slice(0, 4);
  const below = clusters.filter((x) => x.side === "long").sort((a, b) => b.distance_pct - a.distance_pct).slice(0, 4);
  const byDistance = (a: Wall, b: Wall) => Math.abs(a.distance_pct) - Math.abs(b.distance_pct);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-2 border-b border-line px-3 py-2 text-xs">
        <span className="font-semibold text-ink">{displaySymbol(symbol)}</span>
        <span className="text-mute">futures &amp; order flow</span>
        <div className="flex-1" />
        {demo && <span className="rounded bg-yellow-400/15 px-1.5 py-0.5 text-[10px] font-semibold text-yellow-300">DEMO DATA</span>}
        {price != null && <span className="font-mono text-[11px] text-mute">{formatPrice(price)}</span>}
      </div>
      {blocked && (
        <div className="border-b border-line bg-yellow-400/10 px-3 py-2 text-[11px] leading-snug text-yellow-200">
          Futures data is not available from your region. {blocked}
        </div>
      )}
      {error && <div className="border-b border-line bg-down/10 px-3 py-2 text-[11px] text-down">{error}</div>}

      <div className="min-h-0 flex-1 overflow-y-auto">
        <Section title="Funding" source={f?.source} note={f?.note}>
          {!fc ? (
            <Loading />
          ) : (
            <>
              <div className="grid grid-cols-3 gap-2">
                <Stat
                  label="Current"
                  value={`${signed(fc.rate, 4)}%`}
                  tone={fc.rate > 0 ? "up" : fc.rate < 0 ? "down" : null}
                  title={fc.rate >= 0 ? "Longs pay shorts at the next settlement" : "Shorts pay longs at the next settlement"}
                />
                <Stat label="Next in" value={nextIn != null ? countdown(nextIn) : "–"} title={`Every ${fc.interval_hours}h`} />
                <Stat label="Annualised" value={`${signed(fc.annualized, 1)}%`} />
              </div>
              <div className="mt-1 flex items-center justify-between text-[10px] text-mute">
                <span>Last {f?.history?.length ?? 0} settlements</span>
                {f?.avg_24h != null && <span>24h avg {signed(f.avg_24h, 4)}%</span>}
              </div>
              <ZeroBars values={(f?.history ?? []).map((r) => r.rate)} />
            </>
          )}
        </Section>

        <Section title="Open interest" source={o?.source} note={o?.note}>
          {o?.oi_usd == null ? (
            <Loading />
          ) : (
            <>
              <div className="grid grid-cols-3 gap-2">
                <Stat label="Value" value={usd(o.oi_usd)} />
                <Stat label={`In ${base}`} value={o.oi != null ? formatCompact(o.oi) : "–"} />
                <Stat
                  label="24h change"
                  value={o.change_24h_pct != null ? formatPct(o.change_24h_pct) : "–"}
                  tone={o.change_24h_pct == null ? null : o.change_24h_pct >= 0 ? "up" : "down"}
                />
              </div>
              <Sparkline values={(o.rows ?? []).map((r) => r.oi_usd)} />
            </>
          )}
        </Section>

        <Section title="Long / short accounts" source={l?.source} note={l?.note}>
          {l?.long_pct == null ? (
            <Loading />
          ) : (
            <>
              <div className="mb-1 flex h-2 overflow-hidden rounded">
                <div className="bg-up" style={{ width: `${l.long_pct}%` }} />
                <div className="flex-1 bg-down" />
              </div>
              <div className="flex justify-between font-mono text-[11px]">
                <span className="text-up">Long {l.long_pct.toFixed(1)}%</span>
                <span className="text-down">Short {(100 - l.long_pct).toFixed(1)}%</span>
              </div>
              <div className="mt-1.5 grid grid-cols-3 gap-2">
                <Stat label="Ratio" value={l.ratio?.toFixed(2) ?? "–"} />
                <Stat label="24h change" value={l.change_24h != null ? signed(l.change_24h) : "–"} />
                <Stat label="Top traders" value={l.top_ratio?.toFixed(2) ?? "–"} title="Long/short position ratio of the top 20% of traders by margin" />
              </div>
              <Sparkline values={(l.accounts ?? []).map((r) => r.ratio)} baseline={1} color="#a3a3a3" />
            </>
          )}
        </Section>

        <Section title={`CVD · spot · ${interval}`} source={c?.source} note={c?.note}>
          {!day ? (
            <Loading />
          ) : (
            <>
              <div className="grid grid-cols-3 gap-2">
                <Stat label={`24h delta (${base})`} value={signedCompact(day.delta)} tone={day.delta >= 0 ? "up" : "down"} />
                <Stat label="24h delta ($)" value={`${day.delta_usd >= 0 ? "+" : "−"}${usd(Math.abs(day.delta_usd))}`} tone={day.delta_usd >= 0 ? "up" : "down"} />
                <Stat label="Taker buys" value={day.buy_pct != null ? `${day.buy_pct.toFixed(1)}%` : "–"} />
              </div>
              <Sparkline values={(c?.rows ?? []).map((r) => r.cvd)} baseline={0} color={(c?.rows?.at(-1)?.cvd ?? 0) >= 0 ? UP : DOWN} />
              <p className="text-[10px] text-mute">Taker buys minus sells since the first of the last {c?.rows?.length ?? 0} bars.</p>
            </>
          )}
        </Section>

        <Section
          title="Order-book walls · spot ±5%"
          source={w?.source}
          note={w?.note}
          right={<ShowOnChart on={wallsOn} onToggle={() => setWallsOn(!wallsOn)} />}
        >
          {!w?.mid ? (
            <Loading />
          ) : (
            <div className="space-y-1">
              {[...(w.asks ?? [])].sort(byDistance).slice(0, 3).reverse().map((x) => <WallRow key={`a${x.price}`} w={x} side="ask" />)}
              <div className="border-t border-dashed border-line py-0.5 font-mono text-[10px] text-mute">mid {formatPrice(w.mid)}</div>
              {[...(w.bids ?? [])].sort(byDistance).slice(0, 3).map((x) => <WallRow key={`b${x.price}`} w={x} side="bid" />)}
              {!w.asks?.length && !w.bids?.length && <p className="text-[11px] text-mute">No outsized orders near price.</p>}
              {w.covered_pct && Math.min(w.covered_pct.bids, w.covered_pct.asks) < (w.range_pct ?? 5) - 0.1 && (
                <p className="text-[10px] text-mute">
                  The book data reaches −{w.covered_pct.bids}% / +{w.covered_pct.asks}% from the mid.
                </p>
              )}
            </div>
          )}
        </Section>

        <Section
          title="Liquidation clusters · estimated"
          source={lv?.source}
          note={lv?.note}
          right={<ShowOnChart on={liqsOn} onToggle={() => setLiqsOn(!liqsOn)} />}
        >
          {!lv ? (
            <Loading />
          ) : (
            <div className="space-y-1">
              <div className="text-[10px] text-mute">Shorts liquidated above</div>
              {above.length ? [...above].reverse().map((x) => <ClusterRow key={`s${x.price_low}`} c={x} />) : <p className="text-[11px] text-mute">None found</p>}
              <div className="pt-1 text-[10px] text-mute">Longs liquidated below</div>
              {below.length ? below.map((x) => <ClusterRow key={`l${x.price_low}`} c={x} />) : <p className="text-[11px] text-mute">None found</p>}
              <p className="pt-1 text-[10px] leading-snug text-mute">{lv.note}</p>
            </div>
          )}
        </Section>

        <Section title="Recent liquidations · Binance">
          {!lv ? (
            <Loading />
          ) : lv.recent_source !== "binance" ? (
            <p className="text-[11px] leading-snug text-mute">{lv.recent_note}</p>
          ) : lv.recent.length === 0 ? (
            <p className="text-[11px] text-mute">None for {displaySymbol(symbol)} in the last 24 hours.</p>
          ) : (
            <div className="space-y-0.5">
              {lv.recent.slice(0, 10).map((r) => (
                <div key={`${r.time}-${r.price}-${r.qty}`} className="flex items-center gap-2 font-mono text-[11px]">
                  <span className="w-11 shrink-0 text-mute">{clockTime(r.time)}</span>
                  <span className={clsx("w-16 shrink-0", r.side === "long" ? "text-down" : "text-up")}>
                    {r.side === "long" ? "Long liq" : "Short liq"}
                  </span>
                  <span className="w-20 shrink-0 text-ink">{formatPrice(r.price)}</span>
                  <span className="text-ink">{usd(r.usd)}</span>
                </div>
              ))}
              <p className="pt-1 text-[10px] text-mute">Binance sends at most one liquidation per coin per second.</p>
            </div>
          )}
        </Section>
      </div>
    </div>
  );
}
