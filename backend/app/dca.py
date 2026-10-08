"""DCA planner: how a fixed budget would have done on one coin with different ways of buying it.

Over the last `days` of daily candles, the same budget is spent by:

  * Lump sum: everything at the first candle's open.
  * Weekly DCA: an equal amount at the open of every 7th day.
  * Daily DCA: an equal amount at every day's open.
  * Buy the dips: the weekly amount is saved each week and spent only when price opens DIP_PCT or more below its
    highest close of the previous DIP_LOOKBACK days; whatever is still saved at the end is spent on the last day,
    so all strategies invest the whole budget.

Fees are charged on every buy. Each strategy reports what it paid on average, the coins it ended with, the value at
the last close, the return, its worst drawdown and its value over time. Nothing is sold. Deterministic and pure.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

DIP_PCT = 10.0
DIP_LOOKBACK = 30
MIN_DAYS = 28

StrategyId = Literal["lump", "weekly", "daily", "dips"]
NAMES: dict[str, str] = {"lump": "Lump sum", "weekly": "Weekly DCA", "daily": "Daily DCA",
                         "dips": f"Buy the dips (−{DIP_PCT:g}% from the {DIP_LOOKBACK}-day high)"}


class DcaRequest(BaseModel):
    symbol: str
    budget: float = Field(1000.0, gt=0, le=1e9)
    days: int = Field(365, ge=MIN_DAYS, le=1000)
    fee_pct: float = Field(0.1, ge=0, le=1)


class DcaStrategy(BaseModel):
    id: StrategyId
    name: str
    buys: int
    invested: float
    coins: float
    avg_price: float
    final_value: float
    return_pct: float
    max_drawdown_pct: float = Field(..., description="Worst fall of the position's value from its high, %")
    curve: list[tuple[int, float]] = Field(default_factory=list, description="(UNIX s, value incl. cash saved)")


class DcaResult(BaseModel):
    symbol: str
    days: int
    budget: float
    start_price: float
    last_price: float
    buy_hold_pct: float
    strategies: list[DcaStrategy]
    best: StrategyId
    summary: str
    data_source: str = "binance"


def _run(sid: str, opens: np.ndarray, closes: np.ndarray, times: np.ndarray, budget: float, fee: float,
         highs_before: np.ndarray) -> DcaStrategy:
    n = len(opens)
    coins = 0.0
    spent = 0.0
    cash = budget  # not yet invested
    buys = 0
    weeks = max(1, (n + 6) // 7)
    saved = 0.0
    curve: list[tuple[int, float]] = []
    values = np.zeros(n)

    def buy(i: int, amount: float) -> None:
        nonlocal coins, spent, cash, buys
        amount = min(amount, cash)
        if amount <= 0:
            return
        coins += amount * (1 - fee) / opens[i]
        spent += amount
        cash -= amount
        buys += 1

    for i in range(n):
        if sid == "lump" and i == 0:
            buy(i, budget)
        elif sid == "daily":
            buy(i, budget / n)
        elif sid == "weekly" and i % 7 == 0:
            buy(i, budget / weeks)
        elif sid == "dips":
            if i % 7 == 0:
                saved += budget / weeks
            dip = highs_before[i] > 0 and opens[i] <= highs_before[i] * (1 - DIP_PCT / 100)
            if dip and saved > 0:
                buy(i, saved)
                saved = 0.0
            if i == n - 1 and cash > 0:
                buy(i, cash)  # the rest goes in at the end, so every strategy invests the budget
        values[i] = coins * closes[i] + cash
        if i % max(1, n // 120) == 0 or i == n - 1:
            curve.append((int(times[i]), round(float(values[i]), 2)))
    peak = np.maximum.accumulate(values)
    dd = float(((values - peak) / np.where(peak > 0, peak, 1)).min() * 100)
    final = coins * closes[-1] + cash
    return DcaStrategy(id=sid, name=NAMES[sid], buys=buys, invested=round(spent, 2), coins=coins,
                       avg_price=spent / coins if coins else 0.0, final_value=round(final, 2),
                       return_pct=round((final / budget - 1) * 100, 2), max_drawdown_pct=round(dd, 2), curve=curve)


def plan_dca(df: pd.DataFrame, req: DcaRequest, source: str = "binance") -> DcaResult:
    """`df`: daily candles, oldest first. CPU-light."""
    df = df.reset_index(drop=True)
    if len(df) < MIN_DAYS + 1:
        raise ValueError(f"Need at least {MIN_DAYS} days of candles")
    closes_all = df["close"].to_numpy(dtype=float)
    # Highest close of the previous DIP_LOOKBACK days, for each day (history before the window counts).
    roll = pd.Series(closes_all).rolling(DIP_LOOKBACK, min_periods=1).max().shift(1).to_numpy()
    test = df.tail(req.days)
    first = len(df) - len(test)
    opens, closes = test["open"].to_numpy(dtype=float), test["close"].to_numpy(dtype=float)
    times = test["time"].to_numpy(dtype=np.int64)
    highs_before = np.nan_to_num(roll[first:], nan=0.0)
    fee = req.fee_pct / 100
    strategies = [_run(s, opens, closes, times, req.budget, fee, highs_before) for s in ("lump", "weekly", "daily",
                                                                                          "dips")]
    best = max(strategies, key=lambda s: s.final_value)
    lump = strategies[0]
    bh = (closes[-1] / opens[0] - 1) * 100
    short = {"lump": "the lump sum", "weekly": "weekly DCA", "daily": "daily DCA", "dips": "buying the dips"}
    others = ", ".join(f"{short[s.id]} {s.return_pct:+.1f}%" for s in strategies if s is not best)
    summary = (f"Over the last {len(test)} days, {short[best.id]} did best: {best.return_pct:+.1f}% "
               f"(average price {best.avg_price:.6g}, worst drawdown {best.max_drawdown_pct:.1f}%), vs {others}. ")
    if bh < 0:
        summary += "Price fell over the period, so spreading the buys out helped."
    elif lump is best:
        summary += "Price mostly rose, so buying early won; spreading buys out is the safer choice when unsure."
    return DcaResult(symbol=req.symbol, days=len(test), budget=req.budget, start_price=float(opens[0]),
                     last_price=float(closes[-1]), buy_hold_pct=round(bh, 2), strategies=strategies, best=best.id,
                     summary=summary, data_source=source)
