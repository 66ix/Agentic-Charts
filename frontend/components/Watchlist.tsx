"use client";

import clsx from "clsx";
import { ArrowDownUp, ChevronDown, GripVertical, MoreHorizontal, Plus, ScanSearch, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { fetchKlines, fetchTickers, fetchWatchlistScan } from "@/lib/api";
import { displaySymbol, formatPct, formatPrice } from "@/lib/format";
import type { Interval, ScanResult, Ticker } from "@/lib/types";
import { readStored, writeStored } from "@/hooks/usePersistentState";

const TICKERS_CACHE = "ac:cache:tickers";
const SCAN_CACHE = "ac:cache:watchlist-scan";

const TICKER_MS = 10_000;
const SCAN_MS = 120_000;
const SPARK_MS = 300_000;
const SUPPORT = new Set(["support", "demand"]);

export interface WatchlistList {
  id: string;
  name: string;
  symbols: string[];
}

export type WatchlistSort = "manual" | "change" | "zone" | "volume" | "name";

const SORTS: { v: WatchlistSort; label: string }[] = [
  { v: "manual", label: "My order" },
  { v: "change", label: "24h change" },
  { v: "zone", label: "Nearest zone" },
  { v: "volume", label: "Unusual volume" },
  { v: "name", label: "Name" },
];

interface Props {
  /** The list in the order shown (after the sort), so [ and ] step through it as you see it. */
  onOrder?(symbols: string[]): void;
  lists: WatchlistList[];
  activeList: string;
  onLists(next: WatchlistList[]): void;
  onActiveList(id: string): void;
  sort: WatchlistSort;
  onSort(sort: WatchlistSort): void;
  /** The active chart's symbol and timeframe (zones are scanned on it). */
  active: string;
  interval: Interval;
  /** Charts in the grid, for "Open in chart N". */
  cells: number;
  onPick(symbol: string): void;
  onOpenInCell(symbol: string, cell: number): void;
  onAdd(): void;
  onScan(): void;
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

function Sparkline({ closes }: { closes: number[] | undefined }) {
  if (!closes || closes.length < 2) return <span className="h-4 w-14" />;
  const lo = Math.min(...closes);
  const hi = Math.max(...closes);
  const pts = closes.map((c, i) => `${(i / (closes.length - 1)) * 56},${14 - ((c - lo) / (hi - lo || 1)) * 12 - 1}`).join(" ");
  const up = closes[closes.length - 1] >= closes[0];
  return (
    <svg width={56} height={14} className="shrink-0" aria-hidden>
      <polyline points={pts} fill="none" stroke={up ? "#22c55e" : "#ef4444"} strokeWidth={1.2} strokeLinejoin="round" />
    </svg>
  );
}

const uid = () => Math.random().toString(36).slice(2, 8);

export default function Watchlist(p: Props) {
  const list = p.lists.find((l) => l.id === p.activeList) ?? p.lists[0];
  const symbols = list?.symbols ?? [];
  // Last prices and scan rows are kept in storage, so the list shows them at once on load while it refreshes.
  const [tickers, setTickers] = useState<Record<string, Ticker>>({});
  const [scan, setScan] = useState<Record<string, ScanResult>>({});
  useEffect(() => {
    setTickers((t) => ({ ...readStored<Record<string, Ticker>>(TICKERS_CACHE, {}), ...t }));
    setScan((s) => ({ ...readStored<Record<string, ScanResult>>(SCAN_CACHE, {}), ...s }));
  }, []);
  useEffect(() => {
    const id = window.setTimeout(() => writeStored(TICKERS_CACHE, tickers), 2000);
    return () => window.clearTimeout(id);
  }, [tickers]);
  useEffect(() => {
    const id = window.setTimeout(() => writeStored(SCAN_CACHE, scan), 2000);
    return () => window.clearTimeout(id);
  }, [scan]);
  const [sparks, setSparks] = useState<Record<string, number[]>>({});
  const [menu, setMenu] = useState<{ symbol: string; x: number; y: number } | null>(null);
  const [listMenu, setListMenu] = useState(false);
  const [sortMenu, setSortMenu] = useState(false);
  const [renaming, setRenaming] = useState<string | null>(null);
  const dragFrom = useRef<string | null>(null);
  const key = symbols.join(",");

  useEffect(() => {
    if (!key) return;
    const ctrl = new AbortController();
    const load = () =>
      fetchTickers(key.split(","), ctrl.signal)
        .then((r) => setTickers((t) => ({ ...t, ...Object.fromEntries(r.tickers.map((x) => [x.symbol, x])) })))
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
        .then((rows) => setScan((s) => ({ ...s, ...Object.fromEntries(rows.map((r) => [r.symbol, r])) })))
        .catch(() => undefined);
    load();
    const id = setInterval(load, SCAN_MS);
    return () => {
      ctrl.abort();
      clearInterval(id);
    };
  }, [key, p.interval]);

  // A 48-hour sparkline per coin from hourly candles.
  useEffect(() => {
    if (!key) return;
    const ctrl = new AbortController();
    const load = () =>
      Promise.all(
        key.split(",").map((s) =>
          fetchKlines(s, "1h", 48, ctrl.signal)
            .then((r) => [s, r.candles.map((c) => c.close)] as const)
            .catch(() => null),
        ),
      ).then((rows) => {
        if (!ctrl.signal.aborted) setSparks((sp) => ({ ...sp, ...Object.fromEntries(rows.filter((r) => r !== null)) }));
      });
    load();
    const id = setInterval(load, SPARK_MS);
    return () => {
      ctrl.abort();
      clearInterval(id);
    };
  }, [key]);

  useEffect(() => {
    if (!menu && !listMenu && !sortMenu) return;
    const close = () => {
      setMenu(null);
      setListMenu(false);
      setSortMenu(false);
    };
    window.addEventListener("click", close);
    window.addEventListener("blur", close);
    return () => {
      window.removeEventListener("click", close);
      window.removeEventListener("blur", close);
    };
  }, [menu, listMenu, sortMenu]);

  const setSymbols = (next: string[]) => p.onLists(p.lists.map((l) => (l.id === list.id ? { ...l, symbols: next } : l)));
  const ordered = [...symbols];
  if (p.sort === "change") ordered.sort((a, b) => (tickers[b]?.change_pct ?? -1e9) - (tickers[a]?.change_pct ?? -1e9));
  if (p.sort === "zone") ordered.sort((a, b) => Math.abs(scan[a]?.distance_pct ?? 1e9) - Math.abs(scan[b]?.distance_pct ?? 1e9));
  if (p.sort === "volume") ordered.sort((a, b) => (scan[b]?.volume_ratio ?? 0) - (scan[a]?.volume_ratio ?? 0));
  if (p.sort === "name") ordered.sort();
  const orderKey = ordered.join(",");
  const onOrder = useRef(p.onOrder);
  useEffect(() => {
    onOrder.current = p.onOrder;
  });
  useEffect(() => {
    onOrder.current?.(orderKey ? orderKey.split(",") : []);
  }, [orderKey]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="relative flex h-9 shrink-0 items-center gap-1 border-b border-line px-2 text-xs">
        <button
          type="button"
          className="btn-ghost h-6 max-w-[45%] px-1.5 text-[12px] font-semibold text-ink"
          onClick={(e) => {
            e.stopPropagation();
            setListMenu((v) => !v);
          }}
          title="Switch list"
        >
          <span className="truncate">{list?.name ?? "Watchlist"}</span>
          <ChevronDown className="h-3.5 w-3.5 shrink-0" />
        </button>
        <span className="truncate text-[11px] text-mute">zones on {p.interval}</span>
        <div className="flex-1" />
        <button
          type="button"
          className={clsx("btn-ghost h-6 w-6 p-0", p.sort !== "manual" && "text-accent")}
          title={`Sort: ${SORTS.find((s) => s.v === p.sort)?.label}`}
          aria-label="Sort"
          onClick={(e) => {
            e.stopPropagation();
            setSortMenu((v) => !v);
          }}
        >
          <ArrowDownUp className="h-3.5 w-3.5" />
        </button>
        <button type="button" className="btn-ghost h-6 w-6 p-0" title="Ask the agent to scan these coins" aria-label="Scan watchlist" onClick={p.onScan}>
          <ScanSearch className="h-3.5 w-3.5" />
        </button>
        <button type="button" className="btn-ghost h-6 w-6 p-0" title="Add symbol" aria-label="Add symbol" onClick={p.onAdd}>
          <Plus className="h-3.5 w-3.5" />
        </button>

        {listMenu && (
          <div className="absolute left-2 top-8 z-40 w-56 rounded-md border border-line bg-panel p-1 text-[12px] shadow-xl" onClick={(e) => e.stopPropagation()}>
            {p.lists.map((l) => (
              <div key={l.id} className="group flex items-center gap-1">
                {renaming === l.id ? (
                  <input
                    autoFocus
                    defaultValue={l.name}
                    className="h-6 flex-1 rounded border border-accent bg-base px-1.5 text-ink outline-none"
                    onKeyDown={(e) => {
                      if (e.key === "Enter") (e.target as HTMLInputElement).blur();
                      if (e.key === "Escape") setRenaming(null);
                    }}
                    onBlur={(e) => {
                      const name = e.target.value.trim();
                      if (name) p.onLists(p.lists.map((x) => (x.id === l.id ? { ...x, name } : x)));
                      setRenaming(null);
                    }}
                  />
                ) : (
                  <button
                    type="button"
                    className={clsx("flex-1 truncate rounded px-2 py-1 text-left hover:bg-panel2", l.id === list?.id ? "text-accent" : "text-ink")}
                    onClick={() => {
                      p.onActiveList(l.id);
                      setListMenu(false);
                    }}
                    onDoubleClick={() => setRenaming(l.id)}
                  >
                    {l.name} <span className="text-mute">· {l.symbols.length}</span>
                  </button>
                )}
                <button type="button" className="hidden h-6 rounded px-1 text-[10px] text-mute hover:bg-panel2 group-hover:block" onClick={() => setRenaming(l.id)}>
                  Rename
                </button>
                {p.lists.length > 1 && (
                  <button
                    type="button"
                    className="hidden h-6 w-6 place-items-center rounded text-mute hover:bg-panel2 hover:text-down group-hover:grid"
                    aria-label={`Delete list ${l.name}`}
                    onClick={() => {
                      if (!window.confirm(`Delete the list "${l.name}"?`)) return;
                      const next = p.lists.filter((x) => x.id !== l.id);
                      p.onLists(next);
                      if (l.id === list?.id) p.onActiveList(next[0].id);
                    }}
                  >
                    <X className="h-3 w-3" />
                  </button>
                )}
              </div>
            ))}
            <div className="my-1 h-px bg-line" />
            <button
              type="button"
              className="w-full rounded px-2 py-1 text-left text-mute hover:bg-panel2 hover:text-ink"
              onClick={() => {
                const id = uid();
                p.onLists([...p.lists, { id, name: `List ${p.lists.length + 1}`, symbols: [] }]);
                p.onActiveList(id);
                setRenaming(id);
              }}
            >
              + New list
            </button>
          </div>
        )}
        {sortMenu && (
          <div className="absolute right-2 top-8 z-40 w-40 rounded-md border border-line bg-panel p-1 text-[12px] shadow-xl">
            {SORTS.map((s) => (
              <button
                key={s.v}
                type="button"
                className={clsx("w-full rounded px-2 py-1 text-left hover:bg-panel2", p.sort === s.v ? "text-accent" : "text-ink")}
                onClick={() => p.onSort(s.v)}
              >
                {s.label}
              </button>
            ))}
          </div>
        )}
      </div>

      <ul className="min-h-0 flex-1 overflow-y-auto py-1">
        {ordered.map((s) => {
          const t = tickers[s];
          const r = scan[s];
          const badge = zoneBadge(r);
          return (
            <li
              key={s}
              className="group relative"
              draggable={p.sort === "manual"}
              onDragStart={() => (dragFrom.current = s)}
              onDragOver={(e) => p.sort === "manual" && e.preventDefault()}
              onDrop={() => {
                const from = dragFrom.current;
                dragFrom.current = null;
                if (!from || from === s) return;
                const next = symbols.filter((x) => x !== from);
                next.splice(next.indexOf(s), 0, from);
                setSymbols(next);
              }}
              onContextMenu={(e) => {
                e.preventDefault();
                setMenu({ symbol: s, x: e.clientX, y: e.clientY });
              }}
            >
              <button
                type="button"
                onClick={() => p.onPick(s)}
                title={r?.signals.length ? r.signals.join("\n") : undefined}
                className={clsx("flex w-full flex-col gap-0.5 px-3 py-1.5 text-left transition-colors hover:bg-panel2", s === p.active && "bg-accent/10")}
              >
                <span className="flex items-center justify-between gap-2 text-xs">
                  <span className={clsx("flex items-center gap-1 font-medium", s === p.active ? "text-accent" : "text-ink")}>
                    {p.sort === "manual" && <GripVertical className="-ml-2 h-3 w-3 cursor-grab text-mute opacity-0 group-hover:opacity-60" />}
                    {displaySymbol(s)}
                    {r?.unusual_volume && (
                      <span className="rounded bg-orange-400/15 px-1 text-[9px] font-semibold text-orange-300" title={`Unusual volume: the last ${p.interval} bar is ${r.volume_ratio ?? "?"}x its 20-bar average`}>
                        VOL {r.volume_ratio != null ? `${r.volume_ratio}x` : ""}
                      </span>
                    )}
                  </span>
                  <Sparkline closes={sparks[s]} />
                  <span className="font-mono text-ink">{t ? formatPrice(t.price) : "—"}</span>
                </span>
                <span className="flex items-center justify-between gap-2 text-[10px]">
                  {badge ? (
                    <span className={clsx("rounded px-1 py-px font-medium", badge.cls)}>{badge.text}</span>
                  ) : (
                    <span className="text-mute">{r ? `${r.trend}${r.rsi != null ? ` · RSI ${Math.round(r.rsi)}` : ""}` : ""}</span>
                  )}
                  <span className={clsx("font-mono", (t?.change_pct ?? 0) >= 0 ? "text-up" : "text-down")}>{formatPct(t?.change_pct)}</span>
                </span>
              </button>
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
                  setMenu({ symbol: s, x: rect.left - 150, y: rect.bottom });
                }}
                className="absolute right-1 top-1 hidden h-4 w-4 place-items-center rounded text-mute hover:bg-line hover:text-ink group-hover:grid"
                aria-label={`More for ${displaySymbol(s)}`}
              >
                <MoreHorizontal className="h-3 w-3" />
              </button>
            </li>
          );
        })}
        {symbols.length === 0 && <li className="px-3 py-6 text-center text-xs text-mute">Add coins with +</li>}
      </ul>

      {menu && (
        <div
          className="fixed z-50 w-48 rounded-md border border-line bg-panel p-1 text-[12px] shadow-xl"
          style={{ left: Math.max(8, Math.min(menu.x, window.innerWidth - 200)), top: Math.min(menu.y, window.innerHeight - 220) }}
          onClick={(e) => e.stopPropagation()}
        >
          <div className="px-2 py-1 text-[10px] font-semibold uppercase tracking-wide text-mute">{displaySymbol(menu.symbol)}</div>
          {Array.from({ length: Math.max(1, p.cells) }, (_, i) => (
            <button
              key={i}
              type="button"
              className="w-full rounded px-2 py-1 text-left text-ink hover:bg-panel2"
              onClick={() => {
                p.onOpenInCell(menu.symbol, i);
                setMenu(null);
              }}
            >
              Open in chart {i + 1}
            </button>
          ))}
          {p.lists.length > 1 && <div className="my-1 h-px bg-line" />}
          {p.lists
            .filter((l) => l.id !== list?.id)
            .map((l) => (
              <button
                key={l.id}
                type="button"
                className="w-full rounded px-2 py-1 text-left text-ink hover:bg-panel2"
                onClick={() => {
                  p.onLists(p.lists.map((x) => (x.id === l.id && !x.symbols.includes(menu.symbol) ? { ...x, symbols: [...x.symbols, menu.symbol] } : x)));
                  setMenu(null);
                }}
              >
                Copy to {l.name}
              </button>
            ))}
          <div className="my-1 h-px bg-line" />
          <button
            type="button"
            className="w-full rounded px-2 py-1 text-left text-down hover:bg-panel2"
            onClick={() => {
              setSymbols(symbols.filter((x) => x !== menu.symbol));
              setMenu(null);
            }}
          >
            Remove from {list?.name}
          </button>
        </div>
      )}
    </div>
  );
}
