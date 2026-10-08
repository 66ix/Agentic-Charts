"""Spot buy-the-dip ladder: split a budget into limit buys at the demand and support zones below price, sell at the
next resistance, and test the same rule on the last 90 days.

Spot only: nothing here borrows, shorts or uses leverage. Buying a coin outright has no liquidation, so the plan's
"invalidation" is where the dip-buy idea is wrong (a close below the deepest zone), not an automatic stop.

Plan (`build_ladder`):
  * Rungs. Support and demand zones below price from the chart agent's detectors (ta_agent.py), with daily
    confluence when the ladder is planned on a lower timeframe, between MIN_DIP_PCT and MAX_DIP_PCT below price and
    within MAX_DIP_ATR ATRs. The best `rungs` by score (higher-timeframe confluence counts) are kept; each buys at
    the top of its zone, where price reaches it first. Too few zones → the rest step down RUNG_STEP_ATR ATRs at a
    time from the last one.
  * Size. Deeper rungs buy more (weights 1, 1.5, 2, ...), so a deeper dip lowers the average price faster.
  * Take profit. The bottom of the nearest resistance or supply zone at least MIN_TP_PCT above price, else the
    recent high, else TP_FALLBACK_ATR ATRs up.

Backtest (`backtest_ladder`), walk-forward so it never sees the future: while no coins are held, the ladder is
rebuilt once a week from the candles known at that time. A rung fills when a bar's low reaches it (at the rung, or
at the open on a gap below it). Once coins are held, everything is sold when a later bar's high reaches the take
profit, and a new ladder is built. Fees are paid on every fill. Reported: completed cycles, return against simply
holding the coin, max drawdown, time with coins held, and the value curve.
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Literal, Optional

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, field_validator

from .market_data import INTERVAL_SECONDS, MarketData, candles_to_df
from .pricefmt import _fmt
from .schemas import HorizontalLineOverlay
from .ta_agent import TF_LABEL, Zone, atr, cluster_levels, find_swings, htf_zones, mark_confluence, \
    supply_demand_zones

LOOKBACK = 300           # candles a ladder is built from
MIN_CANDLES = 80
MIN_DIP_PCT = 0.5        # a rung closer than this to price is a market buy, not a dip
MAX_DIP_PCT = 35.0
MAX_DIP_ATR = 15.0
RUNG_STEP_ATR = 2.0
MIN_TP_PCT = 1.0
TP_FALLBACK_ATR = 3.0
RECENT_HIGH_BARS = 60
INVALIDATION_ATR = 0.5
DEFAULT_FEE = 0.001
MAX_CURVE = 240

BUY = "#14b8a6"
SELL = "#22c55e"
STOP = "#ef4444"


class LadderRequest(BaseModel):
    symbol: str
    budget: float = Field(1000.0, gt=0, description="Quote asset to spend if every rung fills")
    rungs: int = Field(3, ge=1, le=6)
    timeframe: Literal["1h", "4h", "1d"] = "4h"
    days: int = Field(90, ge=7, le=180, description="How far back to test")
    fee_rate: float = Field(DEFAULT_FEE, ge=0, le=0.01)

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, v: str) -> str:
        s = v.replace("/", "").replace("-", "").replace(" ", "").upper()
        if not s.isalnum() or not 2 <= len(s) <= 20:
            raise ValueError("symbol must look like BTCUSDT")
        return s


class LadderRung(BaseModel):
    price: float = Field(..., description="Limit buy price")
    low: float
    high: float
    basis: str = Field(..., description="'H4 demand + D1', '2 ATR below the last rung'")
    weight_pct: float = Field(..., description="Share of the budget")
    amount: float = Field(..., description="Quote asset this rung spends")
    qty: float
    distance_pct: float = Field(..., description="Below price, %")


class LadderPlan(BaseModel):
    symbol: str
    timeframe: str
    last_price: float
    atr: float
    budget: float
    rungs: list[LadderRung]
    take_profit: float
    tp_basis: str
    tp_gain_pct: float = Field(..., description="Take profit above the average price if every rung fills, %")
    avg_price: float = Field(..., description="Average buy price if every rung fills (fees left out)")
    invalidation: float
    notes: list[str] = Field(default_factory=list)
    data_source: str = "binance"


class LadderBacktest(BaseModel):
    days: int
    timeframe: str
    cycles: int = Field(..., description="Ladders that bought and then sold at the take profit")
    wins: int
    fills: int
    total_return_pct: float
    buy_hold_pct: float
    max_drawdown_pct: float
    time_invested_pct: float
    avg_hold_days: Optional[float] = None
    open_position: bool = Field(..., description="Coins still held at the end (valued at the last close)")
    final_value: float
    curve: list[tuple[int, float]] = Field(default_factory=list, description="(UNIX seconds, value)")
    notes: list[str] = Field(default_factory=list)


class LadderResult(BaseModel):
    """Mirrors LadderResult in frontend/lib/types.ts."""

    plan: LadderPlan
    backtest: Optional[LadderBacktest] = None
    overlays: list[HorizontalLineOverlay] = Field(default_factory=list)
    generated_at: int


# ------------------------------------------------------------------ plan --


def _zones(df: pd.DataFrame, daily: Optional[pd.DataFrame]) -> tuple[list[Zone], float, float]:
    df = df.reset_index(drop=True)
    atr_s = atr(df)
    a = float(atr_s.iloc[-1])
    last = float(df["close"].iloc[-1])
    highs, lows = find_swings(df, a)
    zones = cluster_levels(highs + lows, a, last, len(df)) + supply_demand_zones(df, atr_s)
    if daily is not None and len(daily) >= 30:
        mark_confluence(zones, {"1d": htf_zones(daily)})
    return zones, a, last


def build_ladder(df: pd.DataFrame, req: LadderRequest, tf: str, daily: Optional[pd.DataFrame] = None,
                 source: str = "binance") -> LadderPlan:
    """The ladder from the candles in `df` (oldest first). CPU-bound."""
    if len(df) < MIN_CANDLES:
        raise ValueError(f"Need at least {MIN_CANDLES} candles to plan a ladder")
    zones, a, last = _zones(df.tail(LOOKBACK), daily)
    tfl = TF_LABEL.get(tf, tf.upper())

    below = []
    for z in zones:
        if z.kind not in ("support", "demand"):
            continue
        dip = (last - z.price_high) / last * 100
        if MIN_DIP_PCT <= dip <= MAX_DIP_PCT and (last - z.price_high) <= MAX_DIP_ATR * a:
            below.append(z)
    below.sort(key=lambda z: -(z.score - (last - z.price_high) / last * 2))
    picked: list[Zone] = []
    for z in below:
        if all(z.overlap_ratio(p) <= 0.3 and abs(z.price_high - p.price_high) > 0.5 * a for p in picked):
            picked.append(z)
        if len(picked) == req.rungs:
            break
    picked.sort(key=lambda z: -z.price_high)

    levels: list[tuple[float, float, float, str]] = []  # (buy price, low, high, basis)
    for z in picked:
        htf = z.meta.get("htf")
        levels.append((z.price_high, z.price_low, z.price_high,
                       f"{tfl} {z.kind}" + (f" + {'/'.join(htf)}" if htf else "")))
    while len(levels) < req.rungs:
        prev = levels[-1][0] if levels else last
        price = prev - RUNG_STEP_ATR * a
        if price <= 0:
            break
        basis = f"{RUNG_STEP_ATR:g} ATR below " + ("the last rung" if levels else "price")
        levels.append((price, price, price, basis))

    weights = [1 + 0.5 * i for i in range(len(levels))]
    total_w = sum(weights)
    rungs = []
    for (price, lo, hi, basis), w in zip(levels, weights):
        amount = req.budget * w / total_w
        rungs.append(LadderRung(price=price, low=lo, high=hi, basis=basis, weight_pct=round(w / total_w * 100, 1),
                                amount=round(amount, 2), qty=amount / price,
                                distance_pct=round((last - price) / last * 100, 2)))
    qty = sum(r.qty for r in rungs)
    avg = req.budget / qty if qty else last

    above = sorted((z for z in zones if z.kind in ("resistance", "supply")
                    and z.price_low >= last * (1 + MIN_TP_PCT / 100)), key=lambda z: z.price_low)
    recent_high = float(df["high"].tail(RECENT_HIGH_BARS).max())
    if above:
        tp, tp_basis = above[0].price_low, f"bottom of the {tfl} {above[0].kind} {_fmt(above[0].price_low)}–" \
                                           f"{_fmt(above[0].price_high)}"
    elif recent_high >= last * (1 + MIN_TP_PCT / 100):
        tp, tp_basis = recent_high, f"the {RECENT_HIGH_BARS}-bar high"
    else:
        tp, tp_basis = last + TP_FALLBACK_ATR * a, f"{TP_FALLBACK_ATR:g} ATR above price"

    deepest = min((r.low for r in rungs), default=last)
    invalidation = max(deepest - INVALIDATION_ATR * a, deepest * 0.5)
    notes = []
    if len(picked) < len(rungs):
        notes.append(f"Only {len(picked)} zone{'s' if len(picked) != 1 else ''} below price fit, so "
                     f"{len(rungs) - len(picked)} rung{'s' if len(rungs) - len(picked) != 1 else ''} step down by ATR.")
    notes.append(f"A close below {_fmt(invalidation)} means the zones failed: time to rethink, not a forced exit, "
                 f"since spot has no liquidation.")
    return LadderPlan(symbol=req.symbol, timeframe=tf, last_price=last, atr=a, budget=req.budget, rungs=rungs,
                      take_profit=tp, tp_basis=tp_basis, tp_gain_pct=round((tp / avg - 1) * 100, 2),
                      avg_price=avg, invalidation=invalidation, notes=notes, data_source=source)


def ladder_overlays(plan: LadderPlan) -> list[HorizontalLineOverlay]:
    out = [HorizontalLineOverlay(id=f"ladder-{i}", kind="plan_entry", price=r.price, color=BUY, line_style="dashed",
                                 line_width=2, label=f"Buy {i + 1}: {r.weight_pct:g}% ({r.basis})")
           for i, r in enumerate(plan.rungs)]
    out.append(HorizontalLineOverlay(id="ladder-tp", kind="plan_target", price=plan.take_profit, color=SELL,
                                     line_style="solid", line_width=2, label=f"Take profit ({plan.tp_basis})"))
    out.append(HorizontalLineOverlay(id="ladder-inv", kind="plan_stop", price=plan.invalidation, color=STOP,
                                     line_style="dotted", line_width=1, label="Ladder invalidation"))
    return out


# -------------------------------------------------------------- backtest --


def backtest_ladder(df: pd.DataFrame, req: LadderRequest, tf: str) -> LadderBacktest:
    """Walk-forward replay of the ladder rule over the last `req.days` days of `df` (oldest first). CPU-bound."""
    df = df.reset_index(drop=True)
    bar_s = INTERVAL_SECONDS[tf]
    test_bars = int(req.days * 86400 / bar_s)
    start = max(MIN_CANDLES, len(df) - test_bars)
    if len(df) - start < 10:
        raise ValueError("Not enough history to test the ladder")
    rebuild_every = max(1, int(7 * 86400 / bar_s))
    fee = req.fee_rate
    o, h, l, c, t = (df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close", "time"))

    cash, qty = req.budget, 0.0
    plan: Optional[LadderPlan] = None
    filled: list[bool] = []
    built_at = -10**9
    cycle_cost = 0.0
    cycle_start = 0
    cycles = wins = fills = invested_bars = 0
    holds: list[float] = []
    curve: list[tuple[int, float]] = []
    peak, max_dd = req.budget, 0.0

    for i in range(start, len(df)):
        if qty == 0 and (plan is None or i - built_at >= rebuild_every):
            window = df.iloc[max(0, i - LOOKBACK):i]
            try:
                plan = build_ladder(window, req.model_copy(update={"budget": cash}), tf)
            except ValueError:
                plan = None
            filled = [False] * (len(plan.rungs) if plan else 0)
            built_at = i
        if plan is not None:
            if qty > 0 and h[i] >= plan.take_profit:  # sell everything at the take profit (fills were earlier bars)
                proceeds = qty * plan.take_profit * (1 - fee)
                cash += proceeds
                cycles += 1
                wins += proceeds > cycle_cost
                holds.append((t[i] - t[cycle_start]) / 86400)
                qty, cycle_cost = 0.0, 0.0
                plan, filled = None, []
            else:
                for k, r in enumerate(plan.rungs):
                    if filled[k] or l[i] > r.price:
                        continue
                    price = min(r.price, o[i])
                    spend = min(r.amount, cash)
                    if spend <= 0:
                        continue
                    if qty == 0:
                        cycle_start = i
                    qty += spend * (1 - fee) / price
                    cash -= spend
                    cycle_cost += spend
                    filled[k] = True
                    fills += 1
        invested_bars += qty > 0
        value = cash + qty * c[i]
        peak = max(peak, value)
        max_dd = max(max_dd, (peak - value) / peak * 100 if peak else 0.0)
        curve.append((int(t[i]), round(value, 2)))

    final = cash + qty * c[-1]
    n = len(df) - start
    step = max(1, math.ceil(len(curve) / MAX_CURVE))
    days = round(n * bar_s / 86400)
    notes = [f"Walk-forward: while no coins were held the ladder was rebuilt each week from the candles known then, "
             f"so the test never saw the future. Fees {fee * 100:g}% per fill.",
             "A rung counts as filled when a candle's low touched it; in a fast wick a real order can miss.",
             "Past results are no promise for the next weeks."]
    if days < req.days:
        notes.insert(0, f"Only {days} days of {TF_LABEL.get(tf, tf)} history were available.")
    return LadderBacktest(
        days=days, timeframe=tf, cycles=cycles, wins=wins, fills=fills,
        total_return_pct=round((final / req.budget - 1) * 100, 2),
        buy_hold_pct=round((c[-1] / c[start] - 1) * 100, 2) if c[start] else 0.0,
        max_drawdown_pct=round(max_dd, 2), time_invested_pct=round(invested_bars / n * 100, 1) if n else 0.0,
        avg_hold_days=round(float(np.mean(holds)), 1) if holds else None, open_position=qty > 0,
        final_value=round(final, 2), curve=curve[::step] + ([curve[-1]] if (len(curve) - 1) % step else []),
        notes=notes)


async def plan_ladder(market: MarketData, req: LadderRequest, with_backtest: bool = True) -> LadderResult:
    tf = req.timeframe
    bars = LOOKBACK + int(req.days * 86400 / INTERVAL_SECONDS[tf]) + 5
    main, daily = await asyncio.gather(
        market.get_klines(req.symbol, tf, bars),
        market.get_klines(req.symbol, "1d", 300) if tf != "1d" else asyncio.sleep(0, result=None),
        return_exceptions=True)
    if isinstance(main, BaseException):
        raise main
    candles, source = main
    df = candles_to_df(candles)
    ddf = candles_to_df(daily[0]) if daily and not isinstance(daily, BaseException) else None
    plan = await asyncio.to_thread(build_ladder, df, req, tf, ddf, source)
    if source == "synthetic":
        plan.notes.append("Synthetic demo data: these prices are not real.")
    bt = None
    if with_backtest:
        try:
            bt = await asyncio.to_thread(backtest_ladder, df, req, tf)
        except ValueError as exc:
            plan.notes.append(f"No backtest: {exc}.")
    return LadderResult(plan=plan, backtest=bt, overlays=ladder_overlays(plan), generated_at=int(time.time() * 1000))


def ladder_facts(res: LadderResult) -> dict:
    p, b = res.plan, res.backtest
    out: dict = {
        "symbol": p.symbol, "timeframe": p.timeframe, "budget": p.budget, "last_price": p.last_price,
        "rungs": [{"buy_at": r.price, "share_pct": r.weight_pct, "dip_pct": r.distance_pct, "basis": r.basis}
                  for r in p.rungs],
        "avg_price_if_all_fill": p.avg_price, "take_profit": p.take_profit, "tp_basis": p.tp_basis,
        "tp_gain_pct": p.tp_gain_pct, "invalidation": p.invalidation, "notes": p.notes,
    }
    if b:
        out["backtest"] = {"days": b.days, "cycles": b.cycles, "wins": b.wins, "return_pct": b.total_return_pct,
                           "buy_and_hold_pct": b.buy_hold_pct, "max_drawdown_pct": b.max_drawdown_pct,
                           "time_holding_pct": b.time_invested_pct, "still_holding": b.open_position}
    return out


def describe_ladder(res: LadderResult) -> str:
    p, b = res.plan, res.backtest
    rungs = ", ".join(f"{_fmt(r.price)} ({r.weight_pct:g}%, {r.basis})" for r in p.rungs)
    text = (f"Dip-buy ladder on {p.symbol} ({TF_LABEL.get(p.timeframe, p.timeframe)}): buy at {rungs}. If all fill, "
            f"the average is {_fmt(p.avg_price)}; take profit at {_fmt(p.take_profit)} ({p.tp_basis}, "
            f"+{p.tp_gain_pct:g}% on the average). A close below {_fmt(p.invalidation)} means the zones failed.")
    if b:
        text += (f" Over the last {b.days} days the same rule completed {b.cycles} cycle{'s' if b.cycles != 1 else ''} "
                 f"and returned {b.total_return_pct:+g}% against {b.buy_hold_pct:+g}% for holding, with a "
                 f"{b.max_drawdown_pct:g}% max drawdown" + (" (coins still held at the end)." if b.open_position else "."))
    return text
