"""Exit plans: rungs above price sized from the holding, the invalidation under it, alerts armed once, and rungs you
sold at marked done."""

import asyncio
import os
import time

os.environ["DATA_SOURCE"] = "synthetic"

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app.db import Database  # noqa: E402
from app.exit_plan import ExitPlanService, build_exit_plan  # noqa: E402
from app.market_data import MarketData, candles_to_df, synthetic_klines  # noqa: E402


def _frames(sym="INJUSDT"):
    return (candles_to_df(synthetic_klines(sym, "4h", 500)), candles_to_df(synthetic_klines(sym, "1d", 400)))


def test_rungs_ascend_fit_the_holding_and_the_invalidation_is_below():
    df4, df1 = _frames()
    last = float(df4["close"].iloc[-1])
    plan = build_exit_plan("INJUSDT", 120.0, 120.0, last * 0.8, df4, df1, "quarters")
    prices = [r.price for r in plan.rungs]
    assert prices == sorted(prices) and all(p > last for p in prices)
    assert sum(r.sell_qty for r in plan.rungs) <= 120.0 + 1e-6 and plan.runner_qty > 0
    assert all(r.usdt >= 10 for r in plan.rungs)
    if plan.invalidation:
        assert plan.invalidation.price < last


def test_cost_out_gets_the_money_back_first_and_falls_back_when_under_water():
    df4, df1 = _frames()
    last = float(df4["close"].iloc[-1])
    plan = build_exit_plan("INJUSDT", 100.0, 100.0, last * 0.7, df4, df1, "cost_out")
    first = plan.rungs[0]
    assert plan.profile == "cost_out" and abs(first.sell_qty * first.price - 100 * last * 0.7) < 1.0
    under = build_exit_plan("INJUSDT", 100.0, 100.0, last * 1.2, df4, df1, "cost_out")
    assert under.profile == "quarters" and any("Money back" in n for n in under.notes)
    # The average is a rung of its own, unless a level already sits within an ATR of it.
    assert any(abs(r.price / (last * 1.2) - 1) < 0.05 for r in under.rungs)
    assert any("Under water" in n for n in under.notes)


def test_locked_coins_are_left_out_and_open_space_is_spaced_by_atr():
    df4, df1 = _frames()
    plan = build_exit_plan("INJUSDT", 120.0, 20.0, None, df4, df1, "thirds")
    assert sum(r.sell_qty for r in plan.rungs) <= 20.0 + 1e-6 and any("locked" in n for n in plan.notes)
    # A straight climb: nothing above price.
    n = 300
    c = np.linspace(10, 40, n)
    up = pd.DataFrame({"time": np.arange(n) * 14400, "open": c - 0.05, "high": c + 0.1, "low": c - 0.1, "close": c,
                       "volume": 1.0})
    daily = up.iloc[::6].reset_index(drop=True).assign(time=lambda d: np.arange(len(d)) * 86400)
    plan = build_exit_plan("XUSDT", 10.0, 10.0, None, up, daily, "thirds")
    assert any("open space" in n for n in plan.notes) and len(plan.rungs) == 3


class FakeAlerts:
    def __init__(self):
        self.alerts, self.n = {}, 0

    async def add(self, symbol, specs):
        out = []
        for s in specs:
            self.n += 1

            class A:
                id = f"a{self.n}"
            self.alerts[A.id] = s
            out.append(A)
        return out

    async def remove(self, aid):
        return self.alerts.pop(aid, None) is not None


class Fill:
    def __init__(self, price, t):
        self.market, self.side, self.kind, self.symbol, self.price, self.time = "spot", "sell", "manual", "INJUSDT", price, t


def test_arm_once_and_mark_sold_rungs_done():
    md, alerts = MarketData(), FakeAlerts()

    class Binance:
        _fills: dict = {}

        class account:
            @staticmethod
            def status():
                return {"configured": False}

    svc = ExitPlanService(md, Database("memory"), alerts, Binance())

    async def go():
        plan = await svc.build("INJUSDT", "quarters", qty=200.0, avg_entry=5.0)
        await svc.arm("INJUSDT")
        await svc.arm("INJUSDT")  # re-arming replaces, doesn't duplicate
        await md.close()
        return plan

    plan = asyncio.run(go())
    n = len(plan.rungs) + (1 if plan.invalidation else 0)
    assert len(alerts.alerts) == n and all(s.label.startswith("Exit INJ") for s in alerts.alerts.values())
    Binance._fills = {"f": Fill(plan.rungs[0].price * 1.002, int(time.time()) + 5)}
    again = svc.get("INJUSDT")
    assert again.rungs[0].done and not any(r.done for r in again.rungs[1:])
    assert asyncio.run(svc.delete("INJUSDT")) and not alerts.alerts
