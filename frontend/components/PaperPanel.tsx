"use client";

import clsx from "clsx";
import { Loader2, RotateCcw, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import type { DockPanelProps } from "@/lib/dock";
import { displaySymbol, formatPct, formatPrice } from "@/lib/format";
import {
  PAPER_CHANGED_EVENT,
  cancelPaperOrder,
  fetchPaperWallet,
  paperOverlays,
  placePaperOrders,
  resetPaperWallet,
  usd,
  type PaperOrder,
  type PaperOrderType,
  type PaperSide,
  type PaperWallet,
} from "@/lib/paper";

const REFRESH_MS = 20_000;

function when(ts: number) {
  return new Date(ts * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function qtyText(q: number) {
  return q.toLocaleString("en-US", { maximumFractionDigits: q >= 100 ? 2 : q >= 1 ? 4 : 8 });
}

function orderLabel(o: PaperOrder) {
  const what = o.type === "stop" ? "Stop sell" : `${o.type === "market" ? "Market" : "Limit"} ${o.side}`;
  const size = o.quote != null ? `$${o.quote.toFixed(0)}` : o.qty != null ? qtyText(o.qty) : "all";
  return `${what} ${size}${o.price != null ? ` @ ${formatPrice(o.price)}` : ""}`;
}

/**
 * Spot paper trading: a pretend USDT wallet that buys and sells on real prices, so plans and dip-buy ladders can
 * be tried before real money. Fills are worked out on 1m candles by the backend (backend/app/paper.py).
 */
export default function PaperPanel(props: DockPanelProps) {
  const { onChartOverlays } = props;
  const [w, setW] = useState<PaperWallet | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setW(await fetchPaperWallet(signal));
      setError(null);
    } catch (err) {
      if ((err as Error).name !== "AbortError") setError((err as Error).message);
    }
  }, []);

  useEffect(() => {
    const ctrl = new AbortController();
    void load(ctrl.signal);
    const id = setInterval(() => void load(), REFRESH_MS);
    const onChanged = (e: Event) => {
      const next = (e as CustomEvent<PaperWallet | undefined>).detail;
      if (next) setW(next);
      else void load();
    };
    window.addEventListener(PAPER_CHANGED_EVENT, onChanged);
    return () => {
      ctrl.abort();
      clearInterval(id);
      window.removeEventListener(PAPER_CHANGED_EVENT, onChanged);
    };
  }, [load]);

  // Pending orders and the average cost on each coin's chart.
  const drawFn = useRef(onChartOverlays);
  useEffect(() => {
    drawFn.current = onChartOverlays;
  }, [onChartOverlays]);
  const drawn = useRef(new Map<string, string>());
  useEffect(() => {
    if (!w) return;
    const symbols = new Set([...w.orders.filter((o) => o.result.status === "pending").map((o) => o.symbol), ...w.holdings.map((h) => h.symbol)]);
    for (const sym of drawn.current.keys()) if (!symbols.has(sym)) symbols.add(sym);
    for (const sym of symbols) {
      const ovs = paperOverlays(w, sym);
      const sig = JSON.stringify(ovs);
      if (drawn.current.get(sym) === sig) continue;
      drawFn.current(`paper:${sym}`, sym, ovs);
      if (ovs.length) drawn.current.set(sym, sig);
      else drawn.current.delete(sym);
    }
  }, [w]);

  const act = async (fn: () => Promise<{ wallet: PaperWallet } | unknown>) => {
    setBusy(true);
    try {
      const res = (await fn()) as { wallet?: PaperWallet };
      if (res?.wallet) setW(res.wallet);
      setError(null);
      return true;
    } catch (err) {
      setError((err as Error).message);
      return false;
    } finally {
      setBusy(false);
    }
  };

  const pending = w?.orders.filter((o) => o.result.status === "pending") ?? [];
  const history = w?.orders.filter((o) => o.result.status !== "pending").slice(0, 40) ?? [];
  const held = w?.holdings.filter((h) => h.qty > 0) ?? [];

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center gap-2 border-b border-line px-3 py-1.5 text-[11px] text-mute">
        <span className="truncate">Spot paper trading on live prices, no real money</span>
        <div className="flex-1" />
        <button
          type="button"
          className="btn-ghost h-6 shrink-0 gap-1 px-1.5 text-[11px]"
          disabled={busy}
          onClick={() => {
            const v = window.prompt("Start again with how much paper USDT?", String(w?.start_cash ?? 10000));
            const cash = v == null ? NaN : Number(v.replace(/[$,\s]/g, ""));
            if (Number.isFinite(cash) && cash > 0) void act(() => resetPaperWallet(cash));
          }}
          title="Clear every order and start a fresh wallet"
        >
          <RotateCcw className="h-3.5 w-3.5" /> Reset
        </button>
      </div>

      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-3 py-3 text-[12px]">
        {error && <p className="text-down">{error}</p>}
        {!w && !error && <Loader2 className="h-4 w-4 animate-spin text-mute" />}
        {w && (
          <>
            <div className="grid grid-cols-2 gap-x-3 gap-y-1 rounded-md border border-line bg-base/40 p-2">
              <span className="text-mute">Equity</span>
              <span className="text-right font-mono text-ink">
                {usd(w.equity)} <span className={w.return_pct >= 0 ? "text-up" : "text-down"}>{formatPct(w.return_pct)}</span>
              </span>
              <span className="text-mute">Cash free</span>
              <span className="text-right font-mono">{usd(w.cash)}</span>
              {w.reserved > 0 && (
                <>
                  <span className="text-mute">Set aside for buys</span>
                  <span className="text-right font-mono">{usd(w.reserved)}</span>
                </>
              )}
              <span className="text-mute">Realized PnL</span>
              <span className={clsx("text-right font-mono", w.realized_pnl >= 0 ? "text-up" : "text-down")}>{usd(w.realized_pnl, true)}</span>
              <span className="text-mute">Open PnL</span>
              <span className={clsx("text-right font-mono", w.unrealized_pnl >= 0 ? "text-up" : "text-down")}>{usd(w.unrealized_pnl, true)}</span>
              {w.stats.sells > 0 && (
                <>
                  <span className="text-mute">Sells in profit</span>
                  <span className="text-right font-mono">
                    {w.stats.wins}/{w.stats.sells} ({w.stats.win_rate}%)
                  </span>
                </>
              )}
              <span className="col-span-2 text-[10px] text-mute">
                Started with {usd(w.start_cash)} on {when(w.created_at)} · fees {w.fee_pct}% per fill
                {w.data_source === "synthetic" ? " · demo prices" : ""}
              </span>
              {w.error && <span className="col-span-2 text-[11px] text-yellow-300">{w.error}</span>}
            </div>

            <OrderForm {...props} busy={busy} onPlace={(o) => act(() => placePaperOrders([o]))} />

            {held.length > 0 && (
              <section>
                <p className="pb-1 text-[10px] uppercase tracking-wide text-mute">Holdings</p>
                {held.map((h) => (
                  <div key={h.symbol} className="flex items-center gap-2 border-b border-line py-1 last:border-b-0">
                    <button type="button" className="w-20 shrink-0 text-left font-medium text-ink hover:text-accent" onClick={() => props.onPickSymbol(h.symbol)}>
                      {displaySymbol(h.symbol)}
                    </button>
                    <span className="min-w-0 flex-1 font-mono text-[11px] text-mute">
                      {qtyText(h.qty)} @ {formatPrice(h.avg_cost)}
                      <br />
                      {usd(h.value)}{" "}
                      <span className={(h.unrealized_pnl ?? 0) >= 0 ? "text-up" : "text-down"}>
                        {usd(h.unrealized_pnl, true)} {formatPct(h.unrealized_pct)}
                      </span>
                    </span>
                    <button
                      type="button"
                      className="btn-ghost h-6 border border-line px-1.5 text-[11px]"
                      disabled={busy}
                      title="Market sell everything held of this coin"
                      onClick={() => {
                        if (window.confirm(`Paper sell all ${displaySymbol(h.symbol)} at the market?`)) {
                          void act(() => placePaperOrders([{ symbol: h.symbol, side: "sell", type: "market", source: "manual" }]));
                        }
                      }}
                    >
                      Sell all
                    </button>
                  </div>
                ))}
              </section>
            )}

            {pending.length > 0 && (
              <section>
                <p className="pb-1 text-[10px] uppercase tracking-wide text-mute">Open orders</p>
                {pending.map((o) => (
                  <div key={o.id} className="flex items-center gap-2 border-b border-line py-1 text-[11px] last:border-b-0">
                    <button type="button" className="w-20 shrink-0 text-left font-medium text-ink hover:text-accent" onClick={() => props.onPickSymbol(o.symbol)}>
                      {displaySymbol(o.symbol)}
                    </button>
                    <span className="min-w-0 flex-1 truncate" title={o.note || undefined}>
                      <span className={o.type === "stop" ? "text-down" : o.side === "buy" ? "text-accent" : "text-up"}>{orderLabel(o)}</span>
                      {o.result.reason && <span className="text-mute"> · {o.result.reason}</span>}
                    </span>
                    <button
                      type="button"
                      className="btn-ghost h-6 w-6 justify-center p-0"
                      disabled={busy}
                      title="Cancel"
                      onClick={() => void act(() => cancelPaperOrder(o.id))}
                    >
                      <X className="h-3.5 w-3.5" />
                    </button>
                  </div>
                ))}
              </section>
            )}

            {history.length > 0 && (
              <section>
                <p className="pb-1 text-[10px] uppercase tracking-wide text-mute">History</p>
                {history.map((o) => (
                  <div key={o.id} className="flex items-baseline gap-2 border-b border-line py-1 text-[11px] last:border-b-0" title={o.note || undefined}>
                    <span className="w-20 shrink-0 text-ink">{displaySymbol(o.symbol)}</span>
                    <span className="min-w-0 flex-1">
                      {o.result.status === "filled" ? (
                        <>
                          <span className={o.side === "buy" ? "text-accent" : "text-up"}>
                            {o.side === "buy" ? "Bought" : o.type === "stop" ? "Stopped out" : "Sold"}
                          </span>{" "}
                          <span className="font-mono">
                            {qtyText(o.result.fill_qty ?? 0)} @ {formatPrice(o.result.fill_price ?? 0)}
                          </span>
                          {o.result.pnl != null && (
                            <span className={o.result.pnl >= 0 ? "text-up" : "text-down"}> {usd(o.result.pnl, true)}</span>
                          )}
                        </>
                      ) : (
                        <span className="text-mute">
                          {orderLabel(o)} · {o.result.reason || "cancelled"}
                        </span>
                      )}
                    </span>
                    <span className="shrink-0 text-[10px] text-mute">{o.result.filled_at ? when(o.result.filled_at) : ""}</span>
                  </div>
                ))}
              </section>
            )}

            {w.orders.length === 0 && (
              <p className="text-mute">
                No paper trades yet. Place one above, or use Paper buy on a long plan or a dip-buy ladder from the chart agent. Orders fill on real
                1m candles, so the results show how the plan would really have gone.
              </p>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function OrderForm(p: DockPanelProps & { busy: boolean; onPlace(o: Parameters<typeof placePaperOrders>[0][number]): Promise<boolean> }) {
  const [side, setSide] = useState<PaperSide>("buy");
  const [type, setType] = useState<PaperOrderType>("limit");
  const [price, setPrice] = useState("");
  const [amount, setAmount] = useState("100");
  const needsPrice = type !== "market";
  const priceNum = Number(price);
  const amountNum = Number(amount);
  const valid = (!needsPrice || priceNum > 0) && (side === "sell" ? amount === "" || amountNum > 0 : amountNum > 0);
  return (
    <form
      className="space-y-1.5 rounded-md border border-line bg-base/40 p-2"
      onSubmit={async (e) => {
        e.preventDefault();
        if (!valid) return;
        const ok = await p.onPlace({
          symbol: p.symbol,
          side,
          type,
          price: needsPrice ? priceNum : null,
          quote: side === "buy" ? amountNum : null,
          qty: side === "sell" && amount !== "" ? amountNum : null,
          source: "manual",
        });
        if (ok && needsPrice) setPrice("");
      }}
    >
      <div className="flex items-center gap-1.5">
        <span className="font-medium text-ink">{displaySymbol(p.symbol)}</span>
        {p.price != null && <span className="font-mono text-mute">{formatPrice(p.price)}</span>}
        <div className="flex-1" />
        {(["buy", "sell"] as const).map((s) => (
          <button
            key={s}
            type="button"
            className={clsx("h-6 rounded px-2 text-[11px] capitalize", side === s ? (s === "buy" ? "bg-up/20 text-up" : "bg-down/20 text-down") : "text-mute hover:text-ink")}
            onClick={() => {
              setSide(s);
              setAmount(s === "buy" ? "100" : "");
              if (s === "buy" && type === "stop") setType("limit");
            }}
          >
            {s}
          </button>
        ))}
      </div>
      <div className="flex items-center gap-1.5">
        <select
          aria-label="Order type"
          className="h-7 rounded border border-line bg-transparent px-1 text-[11px] outline-none"
          value={type}
          onChange={(e) => setType(e.target.value as PaperOrderType)}
        >
          <option value="limit">Limit</option>
          <option value="market">Market</option>
          {side === "sell" && <option value="stop">Stop</option>}
        </select>
        {needsPrice && (
          <input
            className="h-7 min-w-0 flex-1 rounded border border-line bg-transparent px-1.5 font-mono text-[11px] outline-none focus:border-accent"
            placeholder={p.price != null ? formatPrice(p.price) : "Price"}
            inputMode="decimal"
            value={price}
            onChange={(e) => setPrice(e.target.value)}
            aria-label="Price"
          />
        )}
        <input
          className="h-7 w-24 min-w-0 rounded border border-line bg-transparent px-1.5 font-mono text-[11px] outline-none focus:border-accent"
          placeholder={side === "buy" ? "USDT" : "All coins"}
          inputMode="decimal"
          value={amount}
          onChange={(e) => setAmount(e.target.value)}
          aria-label={side === "buy" ? "USDT to spend" : "Coins to sell (empty = all)"}
          title={side === "buy" ? "USDT to spend, fees included" : "Coins to sell; leave empty to sell everything held"}
        />
        <button type="submit" disabled={!valid || p.busy} className="btn-ghost h-7 border border-line px-2 text-[11px] disabled:opacity-50">
          Place
        </button>
      </div>
    </form>
  );
}
