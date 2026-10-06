"""Backtests: constructed demand-zone bounce and failure, no lookahead, the Kimi path and the API."""

import math
import os

os.environ["DATA_SOURCE"] = "synthetic"
os.environ["LLM_PROVIDER"] = "none"

import pandas as pd  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.backtest import (BacktestRequest, backtest_frame, compute_stats, describe_backtest,  # noqa: E402
                          kimi_trades, run_backtest)
from app.main import app  # noqa: E402
from app.market_data import MarketData, synthetic_klines  # noqa: E402

STEP = 14400
TOUCH = 120  # index of the candle that first touches the demand zone in demand_series
T0 = 1_700_000_000 - 1_700_000_000 % STEP


def _bar(o, c, wick=0.2):
    return (o, max(o, c) + wick, min(o, c) - wick, c)


def _frame(rows):
    return pd.DataFrame([{"time": T0 + i * STEP, "open": o, "high": h, "low": lo, "close": c, "volume": 1000.0}
                         for i, (o, h, lo, c) in enumerate(rows)])


def demand_series(win: bool) -> pd.DataFrame:
    """A chart with one clean H4 demand zone at 99.8–100.1: quiet range, base, impulse up, drift, a walk back down
    that touches the zone top, then either a bounce (win at 2R) or a drop through the stop."""
    rows = [_bar(100.2, 99.8) if i % 2 else _bar(99.8, 100.2) for i in range(80)]
    rows += [_bar(100.0, 100.1), _bar(100.1, 100.0), _bar(100.0, 100.1)]       # the base
    rows += [_bar(100.1, 103.5), _bar(103.5, 105.5)]                           # the impulse
    rows += [_bar(105.8, 106.2) if i % 2 else _bar(106.2, 105.8) for i in range(30)]
    p = 105.8
    while p > 101.3:                                                           # the walk back down
        rows.append(_bar(p, p - 0.9, 0.1))
        p -= 0.9
    rows.append((p, p + 0.1, 100.05, 100.5))                                   # first touch of the zone
    assert len(rows) - 1 == TOUCH
    p = 100.5
    for _ in range(10):
        q = p + 0.8 if win else p - 0.8
        rows.append(_bar(p, q, 0.1))
        p = q
    return _frame(rows)


# --------------------------------------------------------------- detectors --


def test_demand_zone_bounce_and_failure():
    for win, result in ((True, "win"), (False, "loss")):
        trades, notes = backtest_frame(demand_series(win), "demand_long", "4h", target="2R")
        assert len(trades) == 1, trades
        t = trades[0]
        assert t.direction == "long" and t.entry == pytest.approx(100.1)       # the zone's top edge
        assert t.stop < 99.8 and t.target == pytest.approx(t.entry + 2 * (t.entry - t.stop))
        assert t.result == result and t.basis.startswith("H4 demand 99.8–100.1")
        risk = t.entry - t.stop
        assert t.r == pytest.approx(((t.exit - t.entry) - (t.entry + t.exit) * 0.001) / risk, abs=1e-3)  # after fees
        assert (t.r > 1.5 if win else t.r < -1.0)
        assert t.entry_time == T0 + TOUCH * STEP                                # the touch candle
        assert not [n for n in notes if "still open" in n]


def test_no_trade_when_price_never_returns():
    df = demand_series(True).iloc[:TOUCH].reset_index(drop=True)  # cut just before the touch
    assert backtest_frame(df, "demand_long", "4h")[0] == []


def test_no_lookahead_appending_candles_keeps_earlier_trades():
    """The whole point of the walk-forward: future candles may add trades, never change the ones before them."""
    df = demand_series(True)
    full, _ = backtest_frame(df, "demand_long", "4h")
    assert full
    for cut in (TOUCH + 2, TOUCH + 6, TOUCH + 10, len(df)):
        part, _ = backtest_frame(df.iloc[:cut].reset_index(drop=True), "demand_long", "4h")
        shared = [t for t in part if t.exit_time <= int(df["time"].iloc[cut - 1])]
        assert shared == full[:len(shared)], cut

    # Same with a longer, noisier series and several setups.
    candles = synthetic_klines("ETHUSDT", "4h", 1200)
    big = pd.DataFrame([c.model_dump() for c in candles])
    for setup in ("demand_long", "supply_short", "support_long", "sweep_short"):
        whole, _ = backtest_frame(big, setup, "4h", target="next_level")
        for cut in (700, 900, 1100):
            part, _ = backtest_frame(big.iloc[:cut].reset_index(drop=True), setup, "4h", target="next_level")
            closed = [t for t in part if t.exit_time <= int(big["time"].iloc[cut - 1])]
            assert closed == whole[:len(closed)], (setup, cut)
            assert closed, (setup, cut)


def test_stop_first_on_a_tie_and_time_exit():
    rows = [_bar(100.2, 99.8) if i % 2 else _bar(99.8, 100.2) for i in range(80)]
    rows += [_bar(100.0, 100.1), _bar(100.1, 100.0), _bar(100.0, 100.1)]
    rows += [_bar(100.1, 103.5), _bar(103.5, 105.5)]
    rows += [_bar(105.8, 106.2) if i % 2 else _bar(106.2, 105.8) for i in range(30)]
    p = 105.8
    while p > 101.3:
        rows.append(_bar(p, p - 0.9, 0.1))
        p -= 0.9
    rows.append((p, p + 0.1, 100.05, 100.5))
    rows.append((100.5, 108.0, 95.0, 100.0))   # both the stop and the target inside one candle
    rows += [_bar(100.0, 100.0) for _ in range(5)]
    trades, _ = backtest_frame(_frame(rows), "demand_long", "4h", target="2R")
    assert len(trades) == 1 and trades[0].result == "loss" and trades[0].exit < 99.8

    flat = rows[:TOUCH + 1] + [_bar(100.3, 100.3, 0.05) for _ in range(20)]
    trades, _ = backtest_frame(_frame(flat), "demand_long", "4h", target="2R", max_hold_bars=5)
    assert len(trades) == 1 and trades[0].result == "timeout" and trades[0].bars_held == 5
    assert abs(trades[0].r) < 0.3


def test_one_trade_at_a_time_and_each_zone_once():
    candles = synthetic_klines("SOLUSDT", "4h", 1500)
    df = pd.DataFrame([c.model_dump() for c in candles])
    for setup in ("demand_long", "resistance_short"):
        trades, _ = backtest_frame(df, setup, "4h")
        assert trades
        for a, b in zip(trades, trades[1:]):
            assert b.entry_time >= a.exit_time  # never two open at once
        bases = [t.basis for t in trades]
        assert len(bases) == len(set(bases))   # every zone traded at most once


def test_short_setups_place_the_stop_above_the_zone():
    candles = synthetic_klines("BTCUSDT", "1h", 1500)
    df = pd.DataFrame([c.model_dump() for c in candles])
    trades, _ = backtest_frame(df, "supply_short", "1h")
    assert trades
    for t in trades:
        assert t.stop > t.entry > t.target and t.result in ("win", "loss", "timeout")
        assert t.r == pytest.approx(
            ((t.entry - t.exit) - (t.entry + t.exit) * 0.001) / (t.stop - t.entry), abs=1e-3)


def test_sweep_entry_is_the_sweep_candle_close():
    candles = synthetic_klines("INJUSDT", "4h", 1200)
    df = pd.DataFrame([c.model_dump() for c in candles])
    trades, _ = backtest_frame(df, "sweep_long", "4h", stop_buffer_atr=0.25)
    assert trades
    times = df["time"].tolist()
    for t in trades[:5]:
        i = times.index(t.entry_time)
        assert t.entry == pytest.approx(float(df["close"].iloc[i]))
        assert t.stop < float(df["low"].iloc[i])  # beyond the sweep wick
        assert "swept low" in t.basis


def test_stats_arithmetic_and_summary():
    df = demand_series(True)
    trades, notes = backtest_frame(df, "demand_long", "4h")
    stats, equity = compute_stats(trades)
    assert stats.count == 1 and stats.wins == 1 and stats.win_rate == 1.0
    assert equity[0].r_cum == trades[0].r and stats.max_drawdown_r == 0.0

    fake = [trades[0].model_copy(update={"r": r, "result": res, "exit_time": trades[0].exit_time + i})
            for i, (r, res) in enumerate([(2.0, "win"), (-1.0, "loss"), (1.0, "win"), (-1.0, "loss"),
                                          (-0.2, "timeout")])]
    stats, equity = compute_stats(fake)
    assert (stats.count, stats.wins, stats.losses, stats.timeouts) == (5, 2, 2, 1)
    assert stats.win_rate == pytest.approx(0.4) and stats.total_r == pytest.approx(0.8)
    assert stats.avg_r == pytest.approx(0.16) and stats.profit_factor == pytest.approx(3 / 2.2, abs=1e-4)
    assert stats.expectancy == pytest.approx(0.4 * 1.5 + 0.6 * (-2.2 / 3), abs=1e-4)
    assert stats.max_drawdown_r == pytest.approx(1.2) and [p.r_cum for p in equity] == pytest.approx(
        [2.0, 1.0, 2.0, 1.0, 0.8])
    empty, eq = compute_stats([])
    assert empty.count == 0 and empty.win_rate is None and eq == []


# -------------------------------------------------------------------- kimi --


class StubSignal:
    """Stands in for one kimi_v574.Signal record."""

    def __init__(self, bar, d, result, r, typ=0, res_bar=None, tp=2.0, sl=1.0, entry=100.0):
        self.bar, self.dir, self.result, self.r, self.typ = bar, d, result, r, typ
        self.res_bar = bar + 5 if res_bar is None else res_bar
        self.tp, self.sl, self.entry, self.conf, self.tier = tp, sl, entry, 7, 1


def test_kimi_trades_use_kimis_own_numbers():
    times = [T0 + i * STEP for i in range(40)]
    closes = [100.0 + i * 0.1 for i in range(40)]
    sigs = [StubSignal(2, 1, 1, 1.85), StubSignal(10, -1, 2, -1.003), StubSignal(16, 1, 3, 0.4),
            StubSignal(20, 1, 0, float("nan")),                      # still open: skipped
            StubSignal(24, 1, 1, 1.85, typ=15),                      # random control: skipped
            StubSignal(28, -1, 2, float("nan"))]                     # unscored: skipped
    trades, still_open = kimi_trades(sigs, times, closes, None, 0.15)
    assert still_open == 1 and [t.result for t in trades] == ["win", "loss", "timeout"]
    assert [t.r for t in trades] == [1.85, -1.003, 0.4]
    win = trades[0]
    assert win.direction == "long" and win.entry == 100.0 and win.stop == pytest.approx(99.0)
    assert win.target == pytest.approx(102.0 + 100 * 2 * 0.0015)      # Kimi charges its costs on the target
    assert win.bars_held == 5 and "confluence 7" in win.basis
    assert [t.direction for t in kimi_trades(sigs, times, closes, "long", 0.15)[0]] == ["long", "long"]
    assert [t.direction for t in kimi_trades(sigs, times, closes, "short", 0.15)[0]] == ["short"]


def test_kimi_backtest_end_to_end_on_demo_data():
    import asyncio

    async def run():
        market = MarketData()
        res = await run_backtest(market, None, BacktestRequest(symbol="SOLUSDT", interval="4h", setup="kimi_any",
                                                               bars=600))
        await market.close()
        return res

    res = asyncio.run(run())
    assert res.data_source == "synthetic" and res.target == "kimi"
    assert res.stats.count == len(res.trades) and res.trades
    assert all(t.result in ("win", "loss", "timeout") for t in res.trades)
    assert any("Kimi Cooked's own exit rules" in n for n in res.notes)
    assert any("synthetic demo data" in n for n in res.notes)
    assert math.isfinite(res.stats.total_r) and len(res.equity) == len(res.trades)
    assert "Kimi Cooked" in describe_backtest(res)


# ---------------------------------------------------------------------- API --


def test_backtest_api():
    with TestClient(app) as client:
        r = client.post("/api/backtest", json={"symbol": "sol/usdt", "interval": "4h", "setup": "demand_long",
                                               "bars": 800, "target": "2R"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["symbol"] == "SOLUSDT" and body["setup_name"] == "Long from fresh demand"
        assert body["bars"] <= 800 and body["from_time"] < body["to_time"]
        assert body["data_source"] == "synthetic" and any("synthetic" in n for n in body["notes"])
        assert set(body["stats"]) >= {"count", "wins", "losses", "timeouts", "win_rate", "avg_r", "total_r",
                                      "expectancy", "profit_factor", "max_drawdown_r", "avg_bars_held"}
        assert len(body["equity"]) == len(body["trades"]) == body["stats"]["count"]
        if body["trades"]:
            t = body["trades"][0]
            assert set(t) >= {"entry_time", "entry", "stop", "target", "exit_time", "exit", "r", "result", "basis"}
        assert body["seconds"] >= 0

        assert client.post("/api/backtest", json={"symbol": "SOLUSDT", "setup": "nope"}).status_code == 422
        assert client.post("/api/backtest", json={"symbol": "SOLUSDT", "setup": "demand_long",
                                                  "bars": 50}).status_code == 422
        assert client.post("/api/backtest", json={"symbol": "SOLUSDT", "interval": "2d",
                                                  "setup": "demand_long"}).status_code == 422


def test_small_sample_and_no_trade_notes():
    import asyncio

    async def run():
        market = MarketData()
        res = await run_backtest(market, None, BacktestRequest(symbol="ETHUSDT", interval="1d", setup="demand_long",
                                                               bars=150))
        await market.close()
        return res

    res = asyncio.run(run())
    assert res.stats.count < 20
    first = res.notes[0] if res.notes else ""
    assert ("No trades" in first or "too small a sample" in first or "synthetic" in first)
    assert any("too small a sample" in n or "No trades" in n for n in res.notes)
