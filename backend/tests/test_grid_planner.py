"""Grid bot planner: range from zones/ATR, grid type and count, the history test, and the chat agent's grid plans."""

import asyncio
import math
import time

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import config
from app.grid_planner import (GridBacktestRequest, GridPlanRequest, _pick_edge, build_plan, compare_counts,
                              plan_grid, run_backtest)
from app.gridbot import GridBotParams, GridBotService, SymbolFilters, profit_per_grid_pct, grid_lines, simulate
from app.market_data import MarketData
from app.ta_agent import Zone

FILTERS = SymbolFilters(tick_size=0.01, step_size=0.001, min_notional=5, source="binance")
H4 = 4 * 3600


def _wave(lo: float, hi: float, n: int = 400, period: int = 40, step: int = H4) -> pd.DataFrame:
    """A clean range: a sine between lo and hi, with wicks, ending mid-range on the way up."""
    t = np.arange(n) * step + 1_700_000_000 // step * step
    mid = (lo + hi) / 2 + (hi - lo) / 2 * np.sin(2 * np.pi * np.arange(n) / period)
    prev = np.roll(mid, 1)
    prev[0] = mid[0]
    span = (hi - lo) * 0.03
    return pd.DataFrame({"time": t, "open": prev, "high": np.maximum(prev, mid) + span,
                         "low": np.minimum(prev, mid) - span, "close": mid, "volume": 1.0})


def _zone(kind, lo, hi, score=0.6, htf=None):
    z = Zone(lo, hi, kind, 2, 0, 0, score)
    if htf:
        z.meta["htf"] = htf
    return z


# ------------------------------------------------------------------ edges --


def test_edge_prefers_a_zone_inside_the_atr_band():
    zones = [_zone("demand", 95, 96), _zone("support", 99.5, 99.8, score=0.9), _zone("supply", 108, 110)]
    # ATR 1, price 100: the support at 99.5 is too close (0.5 ATR), the demand at 95 fits.
    px, basis, z = _pick_edge(zones, "low", 100, 1.0, "H4", 90, "30-day low")
    assert px == 95 and basis == "H4 demand low" and z is zones[0]
    px, basis, z = _pick_edge(zones, "high", 100, 1.0, "H4", 120, "30-day high")
    assert px == 110 and basis == "H4 supply high"
    # Daily confluence wins over a slightly better score.
    zones = [_zone("demand", 95, 96, score=0.6, htf=["D1"]), _zone("support", 95.4, 96.4, score=0.65)]
    assert _pick_edge(zones, "low", 100, 1.0, "H4", 90, "x")[1] == "H4 demand low (+D1)"


def test_edge_falls_back_to_the_recent_extreme_then_atr():
    px, basis, z = _pick_edge([], "low", 100, 1.0, "H4", 94, "30-day low")
    assert (px, basis, z) == (94, "30-day low", None)
    px, basis, _ = _pick_edge([], "high", 100, 1.0, "H4", 100.5, "30-day high")  # too close: 3 ATR instead
    assert px == 103 and basis == "3 ATR above price"


# ------------------------------------------------------------------- plan --


def test_plan_on_a_range():
    df = _wave(90, 110)
    plan = build_plan(df, GridPlanRequest(symbol="TESTUSDT", investment=1000), FILTERS, "binance")
    last = float(df["close"].iloc[-1])
    assert plan.lower.price < last < plan.upper.price
    assert plan.grid_type == "arithmetic"  # about 25% wide or less
    lines = grid_lines(plan.lower.price, plan.upper.price, plan.grids, plan.grid_type, FILTERS.tick_size)
    lo, hi = profit_per_grid_pct(lines, 0.001)
    assert lo >= 0.3 - 1e-6 and plan.profit_per_grid_min_pct == pytest.approx(lo, abs=1e-4)
    assert plan.profit_per_grid_max_pct == pytest.approx(hi, abs=1e-4)
    assert plan.reasoning[0].startswith("Range = ") and f"{plan.grids} grids keeps profit per grid" in plan.reasoning[1]
    assert plan.compare_grids and plan.grids not in plan.compare_grids
    assert plan.params(start_time=0).grids == plan.grids


def test_wide_range_is_geometric_and_small_investment_caps_the_grids():
    df = _wave(60, 140)
    plan = build_plan(df, GridPlanRequest(symbol="TESTUSDT", investment=1000), FILTERS, "binance")
    assert plan.grid_type == "geometric" and "Geometric grids" in plan.reasoning[2]
    small = build_plan(df, GridPlanRequest(symbol="TESTUSDT", investment=60), FILTERS, "binance")
    assert small.grids <= 60 / (5 * 1.2) and any("capped" in w for w in small.warnings)
    asked = build_plan(df, GridPlanRequest(symbol="TESTUSDT", grid_type="arithmetic"), FILTERS, "binance")
    assert asked.grid_type == "arithmetic" and "as asked" in asked.reasoning[2]


def test_fees_set_the_floor_on_a_quiet_coin():
    df = _wave(99, 101)  # ATR far below the fees
    plan = build_plan(df, GridPlanRequest(symbol="TESTUSDT", min_net_pct=0.5), FILTERS, "binance")
    assert plan.profit_per_grid_min_pct >= 0.5 - 1e-6 and "widened to clear the fees" in plan.reasoning[1]


def test_compare_counts():
    assert compare_counts(40) == [20, 27, 60, 80]
    assert compare_counts(2) == [3, 4]


# ---------------------------------------------------------------- backtest --


ZIGZAG = pd.DataFrame([(0, 112, 121, 112, 121), (60, 121, 121, 109, 109), (120, 109, 121, 109, 121),
                       (180, 121, 121, 99, 99), (240, 99, 131, 99, 131)],
                      columns=["time", "open", "high", "low", "close"])


def test_drawdown_time_in_range_and_equity_by_hand():
    params = GridBotParams(symbol="TESTUSDT", lower=100, upper=130, grids=3, investment=324, fee_rate=0, start_time=0)
    r = simulate(ZIGZAG, params, FILTERS, curve=True)
    # Values at the closes: 341, 328, 351, 317, 380 (see test_gridbot's zigzag); worst fall 351 → 317.
    assert [v for _, v in r.equity] == [341, 328, 351, 317, 380]
    assert r.max_drawdown_pct == pytest.approx((351 - 317) / 351 * 100, abs=1e-3)
    assert r.time_in_range_pct == 60.0  # 121, 109, 121 inside; 99 and 131 outside
    assert simulate(ZIGZAG, params, FILTERS).equity is None


def test_backtest_compares_grid_counts():
    req = GridBacktestRequest(symbol="TESTUSDT", lower=100, upper=130, grids=3, investment=324, fee_rate=0, days=1,
                              compare_grids=[3, 6, 2])
    out = run_backtest(ZIGZAG, req, FILTERS, "binance", 0, 300)
    assert [r.grids for r in out.alternatives] == [2, 3, 6] and [r.chosen for r in out.alternatives] == [0, 1, 0]
    assert out.matched_trades == 5 and out.grid_profit == 50 and out.time_in_range_pct == 60
    assert out.hold_return_pct == pytest.approx((131 / 112 - 1) * 100, abs=1e-3)
    assert out.result.equity is None and len(out.equity) == 5
    with pytest.raises(ValueError):
        GridBacktestRequest(symbol="TESTUSDT", lower=130, upper=100, grids=3, investment=100)


# ------------------------------------------------------- service, API, agent --


@pytest.fixture
def synthetic_env(monkeypatch):
    monkeypatch.setenv("DATA_SOURCE", "synthetic")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def test_plan_and_backtest_endpoints(synthetic_env):
    from app.main import app

    with TestClient(app) as client:
        app.state.gridbots = GridBotService(MarketData(), store="memory")
        r = client.post("/api/gridbot/plan", json={"symbol": "inj/usdt", "investment": 500})
        assert r.status_code == 200, r.text
        plan = r.json()
        assert plan["symbol"] == "INJUSDT" and plan["data_source"] == "synthetic"
        assert plan["lower"]["price"] < plan["last_price"] < plan["upper"]["price"]
        assert any("Demo data" in w for w in plan["warnings"])
        body = {"symbol": "INJUSDT", "lower": plan["lower"]["price"], "upper": plan["upper"]["price"],
                "grids": plan["grids"], "grid_type": plan["grid_type"], "investment": 500, "days": 2,
                "compare_grids": plan["compare_grids"]}
        r = client.post("/api/gridbot/backtest", json=body)
        assert r.status_code == 200, r.text
        bt = r.json()
        assert bt["days"] == 2 and len(bt["alternatives"]) == len(plan["compare_grids"]) + 1
        assert 0 <= bt["time_in_range_pct"] <= 100 and bt["max_drawdown_pct"] >= 0 and bt["equity"]
        assert client.post("/api/gridbot/backtest", json={**body, "days": 400}).status_code == 422
        assert client.post("/api/gridbot/plan", json={"symbol": "INJUSDT", "timeframe": "5m"}).status_code == 422


def test_agent_answers_plan_a_grid_bot(synthetic_env):
    from app.agent import run_analysis
    from app.llm import LLMClient
    from app.schemas import AnalyzeRequest

    async def run():
        md = MarketData()
        try:
            req = AnalyzeRequest(symbol="BTCUSDT", interval="1h", prompt="plan a grid bot on INJ")
            res = await run_analysis(req, md, LLMClient(config.Settings(llm_provider="none")),
                                     gridbots=GridBotService(md, store="memory"))
        finally:
            await md.close()
        return res

    res = asyncio.run(run())
    assert res.intent.grid_plan and res.intent.trade_plan is None
    assert res.symbol == "INJUSDT" and res.grid_plan is not None
    gp = res.grid_plan
    assert gp["symbol"] == "INJUSDT" and gp["grids"] >= 2 and gp["reasoning"]
    assert res.summary.startswith("Grid bot plan for INJUSDT") and "grids keeps profit per grid" in res.summary


def test_plan_grid_service_uses_estimated_filters_offline(synthetic_env):
    async def run():
        md = MarketData()
        try:
            return await plan_grid(GridBotService(md, store="memory"), GridPlanRequest(symbol="ETHUSDT",
                                                                                       timeframe="1h"))
        finally:
            await md.close()

    plan = asyncio.run(run())
    assert plan.filters.source == "estimated" and plan.timeframe == "1h"
    assert math.isclose(plan.order_value, plan.investment / (plan.grids + 1), abs_tol=0.01)
    assert plan.lower.basis and plan.upper.basis and time.time() > 0
