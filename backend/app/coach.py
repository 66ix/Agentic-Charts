"""Coaching from your own trades: habits in the spot round trips imported from Binance (binance_import.py), each with
the numbers behind it, and how your trades compare with the agent desk's zones on the same coins.

Only your own closed spot round trips count (fills you marked as a bot's, or unknown, are left out). Each habit needs
MIN_TRADES trades to be said at all, and says how many it rests on:

  sold_early     after your winning sells, how often price went EARLY_PCT% higher within AFTER_DAYS days, and the
                 average move you left (highest high in that window against your exit)
  hold_losers    losers held much longer than winners (HOLD_RATIO x the average time)
  payoff         average loss bigger than the average win: you need a high win rate just to break even
  chasing        buys after a CHASE_PCT% rise in the 24 hours before, against your other buys
  buying_high    buys in the top 20% of the 7-day range, against your other buys
  fees           fees as a share of what the trades made before fees
  desk_zones     buys inside a buy zone the desk was tracking at that moment (a call or a watched zone), against
                 your other buys on the same coins

Candles are 1h from Binance, from a week before your first trade to AFTER_DAYS days after your last exit. A report is
cached for CACHE_SECONDS; a new import or "refresh" builds it again.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

import numpy as np
import pandas as pd

from .market_data import MAX_RANGE_BARS

if TYPE_CHECKING:
    from .agent_desk import AgentDesk
    from .binance_import import BinanceImportService, RoundTrip
    from .market_data import MarketData

log = logging.getLogger(__name__)

MIN_TRADES = 5
AFTER_DAYS = 3
EARLY_PCT = 3.0
CHASE_PCT = 8.0
HIGH_POS = 0.8
HOLD_RATIO = 1.5
FEES_SHARE = 0.2
CACHE_SECONDS = 1800.0
HOUR = 3600
WEEK = 7 * 86400


@dataclass
class Trade:
    symbol: str
    opened_at: int
    closed_at: int
    entry: float
    exit: float
    pnl: float
    fees: float

    @property
    def pct(self) -> float:
        return (self.exit / self.entry - 1) * 100 if self.entry else 0.0

    @property
    def win(self) -> bool:
        return self.pnl > 0

    @property
    def hold_hours(self) -> float:
        return max(0.0, (self.closed_at - self.opened_at) / HOUR)


def trades_from(trips: list["RoundTrip"]) -> list[Trade]:
    """Your own closed spot longs."""
    out = []
    for t in trips:
        if (t.market == "spot" and t.direction == "long" and t.status == "closed" and t.kind == "manual"
                and t.closed_at and t.exit_price and t.entry_price > 0):
            out.append(Trade(t.symbol, t.opened_at, t.closed_at, t.entry_price, t.exit_price, t.realized_pnl, t.fees))
    return sorted(out, key=lambda t: t.opened_at)


def _avg(xs: list[float]) -> Optional[float]:
    return round(float(np.mean(xs)), 2) if xs else None


def _window(df: pd.DataFrame, start: int, end: int) -> pd.DataFrame:
    t = df["time"].to_numpy(dtype=np.int64)
    return df[(t >= start) & (t < end)]


def _finding(code: str, tone: str, title: str, detail: str, n: int, **numbers: Any) -> dict:
    return {"code": code, "tone": tone, "title": title, "detail": detail, "trades": n, "numbers": numbers}


def _split(trades: list[Trade], flag: list[bool]) -> tuple[list[Trade], list[Trade]]:
    return [t for t, f in zip(trades, flag) if f], [t for t, f in zip(trades, flag) if not f]


def findings(trades: list[Trade], candles: dict[str, pd.DataFrame], desk_zones: list[dict]) -> list[dict]:
    """The habits worth telling you about, most important first (see the module docstring). Pure."""
    out: list[dict] = []
    if len(trades) < MIN_TRADES:
        return out
    wins = [t for t in trades if t.win]
    losses = [t for t in trades if not t.win]

    # Sold early: what price did after your winning sells.
    left = []
    for t in wins:
        df = candles.get(t.symbol)
        if df is None or df.empty:
            continue
        after = _window(df, t.closed_at, t.closed_at + AFTER_DAYS * 86400)
        if len(after) >= AFTER_DAYS * 12:  # most of the window is in
            left.append((float(after["high"].max()) / t.exit - 1) * 100)
    if len(left) >= MIN_TRADES:
        early = sum(x >= EARLY_PCT for x in left)
        share = early / len(left)
        tone = "warn" if share >= 0.5 else "info"
        out.append(_finding(
            "sold_early", tone, "You may be selling winners early" if share >= 0.5 else "Your exits on winners",
            f"After {early} of {len(left)} winning sells, price went at least {EARLY_PCT:g}% higher within "
            f"{AFTER_DAYS} days (on average {np.mean(left):.1f}% above your exit at its best). "
            + ("Scaling out, or leaving part for the next level, would have kept more of the move." if share >= 0.5
               else "Most of your exits held up."), len(left), share=round(share, 3), avg_left_pct=_avg(left)))

    # Holding losers longer than winners.
    if len(wins) >= 3 and len(losses) >= 3:
        hw, hl = float(np.mean([t.hold_hours for t in wins])), float(np.mean([t.hold_hours for t in losses]))
        if hw > 0 and hl / hw >= HOLD_RATIO:
            out.append(_finding(
                "hold_losers", "warn", "Losers are held longer than winners",
                f"Your losing trades lasted {hl:.0f}h on average against {hw:.0f}h for winners ({hl / hw:.1f}x). "
                "Deciding the exit before the entry (where the idea is wrong) stops a loser from turning into a hope.",
                len(wins) + len(losses), hold_win_h=round(hw, 1), hold_loss_h=round(hl, 1)))

    # Payoff: average win against average loss.
    if len(wins) >= 3 and len(losses) >= 3:
        aw, al = float(np.mean([t.pct for t in wins])), float(np.mean([t.pct for t in losses]))
        payoff = aw / abs(al) if al else 0.0
        need = 1 / (1 + payoff) * 100 if payoff > 0 else 100.0
        rate = len(wins) / len(trades) * 100
        tone = "warn" if payoff < 1 and rate < need + 5 else "good" if payoff >= 1.5 else "info"
        out.append(_finding(
            "payoff", tone, "Average win against average loss",
            f"Winners made {aw:+.1f}% on average and losers {al:+.1f}% ({payoff:.2f} to 1). At that ratio you need to "
            f"win {need:.0f}% of trades to break even; you won {rate:.0f}%.",
            len(trades), avg_win_pct=round(aw, 2), avg_loss_pct=round(al, 2), payoff=round(payoff, 2),
            win_rate=round(rate, 1), breakeven_rate=round(need, 1)))

    # Chasing and buying high: where the entry sat.
    chase, high = [], []
    for t in trades:
        df = candles.get(t.symbol)
        before = _window(df, t.opened_at - 86400, t.opened_at) if df is not None else None
        week = _window(df, t.opened_at - WEEK, t.opened_at) if df is not None else None
        if before is None or len(before) < 12 or week is None or len(week) < 72:
            chase.append(None)
            high.append(None)
            continue
        chase.append((t.entry / float(before["open"].iloc[0]) - 1) * 100 >= CHASE_PCT)
        lo, hi = float(week["low"].min()), float(week["high"].max())
        high.append(hi > lo and (t.entry - lo) / (hi - lo) >= HIGH_POS)
    for code, flags, title, what in (
            ("chasing", chase, "Buying after a pump", f"after price had already risen {CHASE_PCT:g}%+ in 24 hours"),
            ("buying_high", high, "Buying near the top of the week's range", "in the top 20% of the 7-day range")):
        known = [(t, f) for t, f in zip(trades, flags) if f is not None]
        yes, no = _split([t for t, _ in known], [bool(f) for _, f in known])
        if len(yes) >= 3 and len(no) >= 3:
            ay, an = float(np.mean([t.pct for t in yes])), float(np.mean([t.pct for t in no]))
            worse = ay < an - 0.5
            out.append(_finding(
                code, "warn" if worse else "info", title,
                f"{len(yes)} of your buys came {what}: they averaged {ay:+.1f}% against {an:+.1f}% for the rest."
                + (" Waiting for a pullback into a zone has worked better for you." if worse else ""),
                len(known), count=len(yes), avg_pct=round(ay, 2), others_pct=round(an, 2)))

    # Fees.
    gross = sum(t.pnl + t.fees for t in trades)
    fees = sum(t.fees for t in trades)
    if gross > 0 and fees / gross >= FEES_SHARE:
        out.append(_finding(
            "fees", "warn", "Fees take a big share",
            f"Fees came to {fees:,.2f} USDT, {fees / gross * 100:.0f}% of what your trades made before fees. Paying "
            "fees in BNB (25% off) or trading less often keeps more.", len(trades), fees=round(fees, 2),
            share=round(fees / gross, 3)))

    # Inside the desk's zones or not.
    if desk_zones:
        coins = {z["symbol"] for z in desk_zones}
        mine = [t for t in trades if t.symbol in coins]
        inside = [any(z["symbol"] == t.symbol and z["from"] <= t.opened_at <= z["until"]
                      and z["low"] <= t.entry <= z["high"] for z in desk_zones) for t in mine]
        yes, no = _split(mine, inside)
        if len(yes) >= 3 and len(no) >= 3:
            ay, an = float(np.mean([t.pct for t in yes])), float(np.mean([t.pct for t in no]))
            out.append(_finding(
                "desk_zones", "good" if ay > an else "info", "Your buys inside the desk's zones",
                f"{len(yes)} of your buys on coins the desk follows were inside a buy zone it was tracking at the time: "
                f"they averaged {ay:+.1f}% against {an:+.1f}% for your other buys on those coins.",
                len(mine), count=len(yes), avg_pct=round(ay, 2), others_pct=round(an, 2)))

    order = {"warn": 0, "good": 1, "info": 2}
    return sorted(out, key=lambda f: order[f["tone"]])


def overview(trades: list[Trade]) -> dict:
    wins = [t for t in trades if t.win]
    return {"trades": len(trades), "wins": len(wins),
            "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else None,
            "avg_pct": _avg([t.pct for t in trades]), "pnl": round(sum(t.pnl for t in trades), 2),
            "fees": round(sum(t.fees for t in trades), 2),
            "first": trades[0].opened_at if trades else None, "last": trades[-1].closed_at if trades else None}


class CoachService:
    def __init__(self, market: "MarketData", binance: "BinanceImportService", desk: Optional["AgentDesk"] = None) -> None:
        self.market, self.binance, self.desk = market, binance, desk
        self._cache: Optional[tuple[float, int, dict]] = None

    async def report(self, refresh: bool = False, now: Optional[float] = None) -> dict:
        now = time.time() if now is None else now
        trades = trades_from(self.binance.trades("manual"))
        key = len(trades) + sum(t.closed_at for t in trades[-3:])
        hit = self._cache
        if hit and not refresh and now - hit[0] < CACHE_SECONDS and hit[1] == key:
            return hit[2]
        base = {"generated_at": int(now), "overview": overview(trades), "findings": [], "min_trades": MIN_TRADES}
        if len(trades) < MIN_TRADES:
            base["note"] = (f"{len(trades)} closed spot trade{'s' if len(trades) != 1 else ''} of your own imported so "
                            f"far; coaching needs at least {MIN_TRADES}. Import your fills in Account → Setup.")
            self._cache = (now, key, base)
            return base
        candles = await self._candles(trades, now)
        zones = self._desk_zones()
        base["findings"] = await asyncio.to_thread(findings, trades, candles, zones)
        base["coins"] = sorted({t.symbol for t in trades})
        self._cache = (now, key, base)
        return base

    def _desk_zones(self) -> list[dict]:
        if self.desk is None:
            return []
        return [{"symbol": c.symbol, "low": c.zone_low, "high": c.zone_high, "from": c.created_at,
                 "until": c.closed_at or c.expires_at} for c in self.desk.calls(limit=5000)
                + self.desk.calls(limit=5000, watched=True)]

    async def _candles(self, trades: list[Trade], now: float) -> dict[str, pd.DataFrame]:
        sem = asyncio.Semaphore(3)
        out: dict[str, pd.DataFrame] = {}

        async def one(sym: str) -> None:
            mine = [t for t in trades if t.symbol == sym]
            start = int(max(min(t.opened_at for t in mine) - WEEK, now - (MAX_RANGE_BARS - 10) * HOUR))
            end = int(min(now, max(t.closed_at for t in mine) + AFTER_DAYS * 86400 + HOUR))
            async with sem:
                try:
                    df, source = await self.market.get_range(sym, "1h", start, end)
                except Exception as exc:
                    log.info("Coach: candles for %s failed: %s", sym, exc)
                    return
            if source == "binance" or self.market.settings.data_source == "synthetic":
                out[sym] = df

        await asyncio.gather(*(one(s) for s in {t.symbol for t in trades}))
        return out
