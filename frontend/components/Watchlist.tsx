"use client";

import clsx from "clsx";
import { Plus, ScanSearch, X } from "lucide-react";
import { useEffect, useState } from "react";

import { fetchTickers, fetchWatchlistScan } from "@/lib/api";
import { displaySymbol, formatPct, formatPrice } from "@/lib/format";
import type { Interval, ScanResult, Ticker } from "@/lib/types";

const TICKER_MS = 10_000;
const SCAN_MS = 120_000;
const SUPPORT = new Set(["support", "demand"]);

interface Props {
  symbols: string[];
  active: string;
  interval: Interval;
  onPick(symbol: string): void;
  onRemove(symbol: string): void;
  onAdd(): void;
  onScan(): void;
  onClose(): void;
}

/** "In H4 demand" / "Supply 0.8%" from a scan row: only when price is in or near a zone. */
function zoneBadge(r: ScanResult | undefined): { text: string; cls: string } | null {
  if (!r || r.nearest_kind == null || r.distance_pct == null) return null;
  const cls = SUPPORT.has(r.nearest_kind) ? "bg-up/15 text-up" : "bg-down/15 text-down";
  const kind = r.nearest_kind[0].toUpperCase() + r.nearest_kind.slice(1);
  if (r.distance_pct === 0) return { text: `In ${r.nearest_kind}`, cls };
  if (Math.abs(r.distance_pct) <= 1.5) return { text: `${kind} ${Math.abs(r.distance_pct).toFixed(1)}%`, cls };
  return null;
}

export default function Watchlist(p: Props) {
  const [tickers, setTickers] = useState<Record<string, Ticker>>({});
  const [scan, setScan] = useState<Record<string, ScanResult>>({});
  const key = p.symbols.join(",");

  useEffect(() => {
    if (!key) return;
    const ctrl = new AbortController();
    const load = () =>
      fetchTickers(key.split(","), ctrl.signal)
        .then((r) => setTickers(Object.fromEntries(r.tickers.map((t) => [t.symbol, t]))))
        .catch(() => undefined);
    load();
    const id = setInterval(load, TICKER_MS);
    return () => {
      ctrl.abort();
      clearInterval(id);
    };
  }, [key]);

  useEffect(() => {
    if (!key) return;
    const ctrl = new AbortController();
    const load = () =>
      fetchWatchlistScan(key.split(","), p.interval, ctrl.signal)
        .then((rows) => setScan(Object.fromEntries(rows.map((r) => [r.symbol, r]))))
        .catch(() => undefined);
    load();
    const id = setInterval(load, SCAN_MS);
    return () => {
      ctrl.abort();
      clearInterval(id);
    };
  }, [key, p.interval]);

  return (
    <aside className="flex w-60 shrink-0 flex-col border-l border-line bg-panel" aria-label="Watchlist">
      <div className="flex h-9 items-center gap-1 border-b border-line px-2 text-xs">
        <span className="font-semibold text-ink">Watchlist</span>
        <span className="text-mute">· zones on {p.interval}</span>
        <div className="flex-1" />
        <button type="button" className="btn-ghost h-6 w-6 p-0" title="Ask the agent to scan these coins" aria-label="Scan watchlist" onClick={p.onScan}>
          <ScanSearch className="h-3.5 w-3.5" />
        </button>
        <button type="button" className="btn-ghost h-6 w-6 p-0" title="Add symbol" aria-label="Add symbol" onClick={p.onAdd}>
          <Plus className="h-3.5 w-3.5" />
        </button>
        <button type="button" className="btn-ghost h-6 w-6 p-0" title="Hide watchlist" aria-label="Hide watchlist" onClick={p.onClose}>
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
      <ul className="flex-1 overflow-y-auto py-1">
        {p.symbols.map((s) => {
          const t = tickers[s];
          const r = scan[s];
          const badge = zoneBadge(r);
          return (
            <li key={s} className="group relative">
              <button
                type="button"
                onClick={() => p.onPick(s)}
                title={r?.signals.length ? r.signals.join("\n") : undefined}
                className={clsx(
                  "flex w-full flex-col gap-0.5 px-3 py-1.5 text-left transition-colors hover:bg-panel2",
                  s === p.active && "bg-accent/10",
                )}
              >
                <span className="flex items-baseline justify-between gap-2 text-xs">
                  <span className={clsx("font-medium", s === p.active ? "text-accent" : "text-ink")}>{displaySymbol(s)}</span>
                  <span className="font-mono text-ink">{t ? formatPrice(t.price) : "—"}</span>
                </span>
                <span className="flex items-center justify-between gap-2 text-[10px]">
                  {badge ? (
                    <span className={clsx("rounded px-1 py-px font-medium", badge.cls)}>{badge.text}</span>
                  ) : (
                    <span className="text-mute">{r ? `${r.trend}${r.rsi != null ? ` · RSI ${Math.round(r.rsi)}` : ""}` : ""}</span>
                  )}
                  <span className={clsx("font-mono", (t?.change_pct ?? 0) >= 0 ? "text-up" : "text-down")}>
                    {formatPct(t?.change_pct)}
                  </span>
                </span>
              </button>
              <button
                type="button"
                onClick={() => p.onRemove(s)}
                className="absolute right-1 top-1 hidden h-4 w-4 place-items-center rounded text-mute hover:bg-line hover:text-ink group-hover:grid"
                aria-label={`Remove ${displaySymbol(s)}`}
              >
                <X className="h-3 w-3" />
              </button>
            </li>
          );
        })}
        {p.symbols.length === 0 && <li className="px-3 py-6 text-center text-xs text-mute">Add coins with +</li>}
      </ul>
    </aside>
  );
}
