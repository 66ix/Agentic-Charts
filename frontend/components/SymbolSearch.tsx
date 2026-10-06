"use client";

import clsx from "clsx";
import { Search, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { fetchSymbols } from "@/lib/api";
import { customLabel, INDEXES } from "@/lib/customSymbols";
import { displaySymbol } from "@/lib/format";

const POPULAR = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "INJUSDT", "BNBUSDT", "XRPUSDT"];

const QUOTES = ["USDT", "USDC", "FDUSD", "BUSD"];

/** "ETH/BTC" → "ETHUSDT/BTCUSDT" when both coins trade against USDT (a ratio chart); null otherwise. */
function ratioOf(q: string, all: Set<string>): string | null {
  const parts = q.toUpperCase().replace(/\s/g, "").split(/[/÷]/);
  if (parts.length !== 2 || !parts[0] || !parts[1] || QUOTES.includes(parts[1])) return null;
  const pair = (c: string) => (c.endsWith("USDT") ? c : `${c}USDT`);
  const [a, b] = [pair(parts[0]), pair(parts[1])];
  return a !== b && all.has(a) && all.has(b) ? `${a}/${b}` : null;
}

interface Row {
  symbol: string;
  hint: string;
}

export default function SymbolSearch({ open, onClose, onPick, title, allowCustom = true }: {
  open: boolean;
  onClose(): void;
  onPick(symbol: string): void;
  /** What picking does, shown in the box ("Compare with…"). */
  title?: string;
  /** Offer ratio charts (ETH/BTC) and the TOTAL indexes. */
  allowCustom?: boolean;
}) {
  const [all, setAll] = useState<string[]>(POPULAR);
  const [q, setQ] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    setQ("");
    setActive(0);
    setTimeout(() => inputRef.current?.focus(), 0);
    const ctrl = new AbortController();
    fetchSymbols(ctrl.signal).then((r) => setAll(r.symbols)).catch(() => undefined);
    return () => ctrl.abort();
  }, [open]);

  const results = useMemo<Row[]>(() => {
    const pairs = (list: string[]) => list.map((symbol) => ({ symbol, hint: "Binance · Spot" }));
    const indexes = allowCustom ? INDEXES.map((i) => ({ symbol: i.symbol, hint: `Index · ${i.hint}` })) : [];
    const needle = q.replace(/[\s/÷-]/g, "").toUpperCase();
    if (!needle) return [...pairs([...POPULAR, ...all.filter((s) => !POPULAR.includes(s))].slice(0, 50)), ...indexes];
    const ratio = allowCustom ? ratioOf(q, new Set(all)) : null;
    const starts = all.filter((s) => s.startsWith(needle));
    const contains = all.filter((s) => !s.startsWith(needle) && s.includes(needle));
    return [
      ...(ratio ? [{ symbol: ratio, hint: "Ratio chart, one coin priced in the other" }] : []),
      ...indexes.filter((i) => i.symbol.includes(needle)),
      ...pairs([...starts, ...contains].slice(0, 50)),
    ];
  }, [q, all, allowCustom]);

  if (!open) return null;

  const pick = (s: string) => {
    onPick(s);
    onClose();
  };

  return (
    <div className="fixed inset-0 z-50 grid place-items-start bg-black/60 pt-24" onMouseDown={onClose}>
      <div
        className="mx-auto w-[min(480px,calc(100vw-32px))] overflow-hidden rounded-lg border border-line bg-panel shadow-2xl"
        onMouseDown={(e) => e.stopPropagation()}
        role="dialog"
        aria-label="Symbol search"
      >
        <div className="flex items-center gap-2 border-b border-line px-3">
          <Search className="h-4 w-4 text-mute" />
          <input
            ref={inputRef}
            value={q}
            onChange={(e) => {
              setQ(e.target.value);
              setActive(0);
            }}
            onKeyDown={(e) => {
              if (e.key === "ArrowDown") setActive((a) => Math.min(a + 1, results.length - 1));
              else if (e.key === "ArrowUp") setActive((a) => Math.max(a - 1, 0));
              else if (e.key === "Enter" && results[active]) pick(results[active].symbol);
              else if (e.key === "Escape") onClose();
            }}
            placeholder={title ?? (allowCustom ? "Search a coin, e.g. INJ, or ETH/BTC for a ratio" : "Search a coin, e.g. INJ")}
            className="h-11 flex-1 bg-transparent text-sm text-ink outline-none placeholder:text-mute"
          />
          <button type="button" className="btn-ghost" onClick={onClose} aria-label="Close">
            <X className="h-4 w-4" />
          </button>
        </div>
        <ul className="max-h-80 overflow-y-auto py-1">
          {results.map((r, i) => (
            <li key={r.symbol}>
              <button
                type="button"
                onMouseEnter={() => setActive(i)}
                onClick={() => pick(r.symbol)}
                className={clsx(
                  "flex w-full items-center justify-between px-4 py-2 text-left text-sm",
                  i === active ? "bg-panel2 text-ink" : "text-ink/80",
                )}
              >
                <span className="font-medium">{r.symbol.includes(":") || r.symbol.includes("/") ? customLabel(r.symbol) : displaySymbol(r.symbol)}</span>
                <span className="text-xs text-mute">{r.hint}</span>
              </button>
            </li>
          ))}
          {results.length === 0 && <li className="px-4 py-6 text-center text-sm text-mute">No matches</li>}
        </ul>
      </div>
    </div>
  );
}
