"""Daily realized PnL from the imported Binance fills, for the PnL calendar (GET /api/binance/pnl-calendar).

Spot: each (symbol, kind, bot) keeps a running position at average cost. A buy adds its coins (less a fee taken in
the coin) and its cost (plus a fee paid in the quote asset); a sell realizes (price - average cost) x coins sold,
less its quote fee, on the day it fills. A sell with no imported buy before it (coins bought before the imported
history, deposits) realizes nothing and is counted in the notes. Futures: Binance's realizedPnl of each fill, less
its commission, on the day it fills (opening fills realize only their commission).

Only pairs quoted in a dollar stablecoin are counted, so every day is in USD. Fees paid in BNB or another asset,
futures funding and Earn rewards are not included; the notes say so. Days are calendar days in the browser's time
zone (`tz_offset`, minutes, as JavaScript's Date.getTimezoneOffset gives it: UTC minus local time).
"""

from __future__ import annotations

from datetime import datetime, timezone

from .binance_import import AccountFill, _fee
from .gridbot import split_symbol

USD_QUOTES = {"USDT", "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP"}


def _day(t: int, tz_offset: int) -> str:
    return datetime.fromtimestamp(t - tz_offset * 60, timezone.utc).date().isoformat()


def daily_pnl(fills: list[AccountFill], tz_offset: int = 0) -> dict:
    """{"days": [{"date", "pnl", "spot", "futures", "closes"}], "total", "win_days", "loss_days", "best", "worst",
    "notes"}; days with fills but no realized PnL or fees are left out."""
    days: dict[str, dict] = {}
    other_quote: set[str] = set()
    unmatched = other_fee = 0

    def book(f: AccountFill, market: str, pnl: float, closes: int) -> None:
        d = days.setdefault(_day(f.time, tz_offset), {"spot": 0.0, "futures": 0.0, "closes": 0})
        d[market] += pnl
        d["closes"] += closes

    groups: dict[tuple, list[AccountFill]] = {}
    for f in fills:
        if split_symbol(f.symbol)[1] not in USD_QUOTES:
            other_quote.add(f.symbol)
            continue
        groups.setdefault((f.market, f.symbol, f.position_side, f.kind, f.bot_id), []).append(f)

    for (market, *_), rows in groups.items():
        rows.sort(key=lambda f: (f.time_ms, f.trade_id))
        qty = cost = 0.0
        for f in rows:
            fee_q, fee_base, other = _fee(f)
            other_fee += other
            if market == "futures":
                if f.realized_pnl or fee_q:
                    book(f, "futures", (f.realized_pnl or 0.0) - fee_q, 1 if f.realized_pnl else 0)
                continue
            if f.side == "buy":
                qty += f.qty - fee_base
                cost += f.qty * f.price + fee_q
                continue
            if qty <= 1e-12:
                unmatched += 1
                continue
            sold = min(f.qty, qty)
            avg = cost / qty
            if f.qty > sold * 1.001:
                unmatched += 1  # part of it sold coins from before the imported history
            book(f, "spot", (f.price - avg) * sold - fee_q * sold / f.qty, 1)
            cost -= avg * sold
            qty -= sold

    out_days = []
    for date in sorted(days):
        d = days[date]
        out_days.append({"date": date, "pnl": round(d["spot"] + d["futures"], 2), "spot": round(d["spot"], 2),
                         "futures": round(d["futures"], 2), "closes": d["closes"]})
    wins = [d for d in out_days if d["pnl"] > 0]
    losses = [d for d in out_days if d["pnl"] < 0]
    notes = []
    if unmatched:
        notes.append(f"{unmatched} sell{'s' if unmatched != 1 else ''} had no imported buy before {'them' if unmatched != 1 else 'it'} "
                     "(coins bought before the imported history or deposited), so no PnL is counted for those coins.")
    if other_quote:
        notes.append("Pairs not quoted in a dollar stablecoin are left out: " + ", ".join(sorted(other_quote)[:6])
                     + ("…" if len(other_quote) > 6 else "") + ".")
    if other_fee:
        notes.append("Fees paid in BNB or another asset are not taken off.")
    notes.append("Realized PnL only: open positions, futures funding and Earn rewards are not included.")
    return {"days": out_days, "total": round(sum(d["pnl"] for d in out_days), 2), "win_days": len(wins),
            "loss_days": len(losses), "best": max(out_days, key=lambda d: d["pnl"]) if wins else None,
            "worst": min(out_days, key=lambda d: d["pnl"]) if losses else None, "currency": "USD", "notes": notes}
