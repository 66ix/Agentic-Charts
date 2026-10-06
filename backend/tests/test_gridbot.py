"""Binance Spot Grid simulator: hand-checkable paths, rounding, triggers, stops, runtime text, service and API."""

import asyncio
import json
import math
import time

import httpx
import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import config
from app.gridbot import (BinanceShows, GridBotCreate, GridBotParams, GridBotPatch, GridBotService, SymbolFilters,
                         describe_result, estimate_filters, floor_step, format_runtime, grid_lines, parse_filters,
                         parse_runtime, profit_per_grid_pct, resolve_start, simulate)
from app.market_data import MarketData

FILTERS = SymbolFilters(tick_size=0.01, step_size=0.001)


def _df(bars):
    """(time, open, high, low, close) tuples → the frame MarketData.get_range returns."""
    df = pd.DataFrame(bars, columns=["time", "open", "high", "low", "close"])
    df["volume"] = 1.0
    return df


def _params(**kw):
    base = dict(symbol="TESTUSDT", lower=100, upper=130, grids=3, investment=324, fee_rate=0, start_time=0)
    return GridBotParams(**{**base, **kw})


# Lines 100 / 110 / 120 / 130. Start at 112: no order on 110, buy at 100, sells at 120 and 130 (2 base bought).
ZIGZAG = _df([
    (0, 112, 121, 112, 121),    # up through 120: sell fills (matched 1), buy placed at 110
    (60, 121, 121, 109, 109),   # down through 110: buy fills, sell back at 120
    (120, 109, 121, 109, 121),  # up through 120 again (matched 2)
    (180, 121, 121, 99, 99),    # down through 110 and 100: two buys
    (240, 99, 131, 99, 131),    # up through 110, 120, 130 (matched 5)
])


# --------------------------------------------------------------- simulate --


def test_zigzag_without_fees_is_exact():
    r = simulate(ZIGZAG, _params(), FILTERS)
    assert r.qty_per_order == 1.0  # 324 / (100 + 2 * 112)
    assert r.matched_trades == 5 and r.grid_profit == 50.0  # five grids of 10 each
    # Cash: 324 - 224 + 120 - 110 + 120 - 110 - 100 + 110 + 120 + 130 = 380, no base left.
    assert r.quote_held == 380.0 and r.base_held == 0.0 and r.current_value == 380.0
    assert r.total_pnl == 56.0 and r.floating_pnl == 6.0  # the two initial sells were bought at 112, not 110/120
    assert r.total_pnl_pct == pytest.approx(56 / 324 * 100, abs=1e-4)
    assert r.status == "running" and r.gap_line == 3 and r.last_price == 131
    assert [(o.side, o.price) for o in r.open_orders] == [("buy", 100), ("buy", 110), ("buy", 120)]
    sides = [(f.side, f.price, f.kind) for f in reversed(r.recent_fills)]
    assert sides == [("buy", 112, "initial"), ("sell", 120, "grid"), ("buy", 110, "grid"), ("sell", 120, "grid"),
                     ("buy", 110, "grid"), ("buy", 100, "grid"), ("sell", 110, "grid"), ("sell", 120, "grid"),
                     ("sell", 130, "grid")]
    assert all(f.profit == 10.0 for f in r.recent_fills if f.side == "sell" and f.kind == "grid")


def test_zigzag_with_fees_follows_binance_formula():
    c = 0.001
    r = simulate(ZIGZAG, _params(fee_rate=c, investment=1000), FILTERS)
    q = math.floor(1000 / (100 + 2 * 112 * (1 + c)) * 1000) / 1000  # rounded down to the 0.001 lot step
    assert r.qty_per_order == pytest.approx(q)
    pairs = [(120, 110), (120, 110), (110, 100), (120, 110), (130, 120)]  # (sell line, line below)
    grid = sum(q * (s * (1 - c) - b * (1 + c)) for s, b in pairs)
    assert r.matched_trades == 5 and r.grid_profit == pytest.approx(grid, abs=1e-6)
    quote = 1000 - 2 * q * 112 * (1 + c) - q * (110 + 110 + 100) * (1 + c) + q * (120 + 120 + 110 + 120 + 130) * (1 - c)
    assert r.total_pnl == pytest.approx(quote - 1000, abs=1e-6)
    assert r.floating_pnl == pytest.approx(r.total_pnl - r.grid_profit, abs=1e-6)
    assert r.fees_paid == pytest.approx(c * q * (2 * 112 + 320 + 600), abs=1e-6)
    # Binance's Profit/Grid: max on the lowest grid, min on the highest.
    assert r.profit_per_grid_max_pct == pytest.approx(((1 - c) * 110 / 100 - 1 - c) * 100, abs=1e-4)
    assert r.profit_per_grid_min_pct == pytest.approx(((1 - c) * 130 / 120 - 1 - c) * 100, abs=1e-4)


def test_bnb_discount_lowers_the_fee():
    assert _params(fee_rate=0.001, bnb_discount=True).fee == pytest.approx(0.00075)
    full = simulate(ZIGZAG, _params(fee_rate=0.001, investment=1000), FILTERS)
    bnb = simulate(ZIGZAG, _params(fee_rate=0.001, bnb_discount=True, investment=1000), FILTERS)
    assert bnb.fee_rate == pytest.approx(0.00075) and bnb.grid_profit > full.grid_profit


def test_intrabar_path_goes_to_the_nearer_extreme_first():
    flat = (0, 112, 112, 112, 112)
    # Open 116 is nearer the high (121) than the low (109): up through 120 (sell), then down through 110 (buy).
    r = simulate(_df([flat, (60, 116, 121, 109, 112)]), _params(), FILTERS)
    assert r.matched_trades == 1
    assert [(f.side, f.price) for f in r.recent_fills if f.kind == "grid"] == [("buy", 110), ("sell", 120)]
    # Open 114 is nearer the low: down to 109 (no buy at 110 yet), up through 120; the buy placed at 110 on the
    # way up cannot fill on the same leg, and the close (112) does not reach it.
    r = simulate(_df([flat, (60, 114, 121, 109, 112)]), _params(), FILTERS)
    assert r.matched_trades == 1
    assert [(f.side, f.price) for f in r.recent_fills if f.kind == "grid"] == [("sell", 120)]


def test_arithmetic_and_geometric_lines():
    assert grid_lines(100, 400, 2, "arithmetic", 0.01).tolist() == [100, 250, 400]
    assert grid_lines(100, 400, 2, "geometric", 0.01).tolist() == [100, 200, 400]
    geo = grid_lines(100, 200, 10, "geometric", 0.0001)
    ratios = geo[1:] / geo[:-1]
    assert np.allclose(ratios, 2 ** 0.1, atol=1e-5) and len(geo) == 11
    lo, hi = profit_per_grid_pct(geo, 0.0)
    assert lo == pytest.approx(hi, abs=1e-3)  # geometric: the same % on every grid


def test_tick_and_step_rounding():
    assert grid_lines(1, 2, 3, "arithmetic", 0.01).tolist() == [1.0, 1.33, 1.67, 2.0]
    assert floor_step(1.23456, 0.01) == 1.23
    assert floor_step(0.3, 0.1) == 0.3  # float noise does not round it down to 0.2
    assert floor_step(7.9999, 1) == 7
    with pytest.raises(ValueError, match="closer together"):
        grid_lines(1, 1.02, 10, "arithmetic", 0.01)
    r = simulate(ZIGZAG, _params(investment=330), SymbolFilters(tick_size=0.01, step_size=0.01))
    assert r.qty_per_order == 1.01  # 330 / 324 = 1.0185 → 1.01
    est = estimate_filters(65000)
    assert est.tick_size == 0.01 and est.step_size == 1e-6 and est.source == "estimated"
    assert estimate_filters(0.15).tick_size == 1e-5 and estimate_filters(7.8).step_size == 0.01


def test_parse_exchange_info_filters():
    payload = {"symbols": [{"symbol": "BTCUSDT", "filters": [
        {"filterType": "PRICE_FILTER", "minPrice": "0.01", "tickSize": "0.01000000"},
        {"filterType": "LOT_SIZE", "stepSize": "0.00001000"},
        {"filterType": "NOTIONAL", "minNotional": "5.00000000"}]}]}
    f = parse_filters(payload)
    assert (f.tick_size, f.step_size, f.min_notional, f.source) == (0.01, 0.00001, 5.0, "binance")


def test_initial_orders_around_the_start_price():
    bars = _df([(0, 124, 124, 124, 124)])
    r = simulate(bars, _params(investment=334), FILTERS)  # 334 / (100 + 110 + 124) = 1
    assert r.gap_line == 2 and r.qty_per_order == 1.0 and r.base_held == 1.0
    assert [(o.side, o.price) for o in r.open_orders] == [("buy", 100), ("buy", 110), ("sell", 130)]
    above = simulate(_df([(0, 140, 140, 140, 140)]), _params(investment=330), FILTERS)
    assert above.gap_line == 3 and above.base_held == 0 and not above.in_range
    assert {o.side for o in above.open_orders} == {"buy"} and above.position_pct > 100
    below = simulate(_df([(0, 90, 90, 90, 90)]), _params(investment=270), FILTERS)
    assert below.gap_line == 0 and below.base_held == 3.0 and {o.side for o in below.open_orders} == {"sell"}


def test_investment_too_small():
    with pytest.raises(ValueError, match="too small"):
        simulate(ZIGZAG, _params(investment=0.1), FILTERS)
    with pytest.raises(ValueError, match="minimum order"):  # Binance's 5 USDT minimum, known from exchangeInfo
        simulate(ZIGZAG, _params(investment=10), FILTERS.model_copy(update={"min_notional": 5, "source": "binance"}))
    # With estimated filters it is only a warning.
    r = simulate(ZIGZAG, _params(investment=10), FILTERS.model_copy(update={"min_notional": 5}))
    assert any("too small" in n for n in r.notes)


def test_trigger_price():
    flat = (0, 112, 112, 112, 112)
    bars = _df([flat, (60, 112, 128, 112, 128), (120, 128, 128, 119, 119)])
    r = simulate(bars, _params(trigger_price=127, investment=330), FILTERS)
    # Laid out at 127: no order on 130, buys at 100/110/120, nothing to buy up front.
    assert r.status == "running" and r.triggered_at == 60 and r.start_price == 127
    assert r.qty_per_order == 1.0 and [(f.side, f.price) for f in r.recent_fills] == [("buy", 120)]
    assert r.gap_line == 2 and r.matched_trades == 0

    waiting = simulate(bars, _params(trigger_price=150), FILTERS)
    assert waiting.status == "waiting" and waiting.total_pnl == 0 and waiting.quote_held == 324
    assert not waiting.open_orders and "trigger" in waiting.notes[0]


def test_stop_loss_sells_everything():
    bars = _df([(0, 112, 112, 112, 112), (60, 112, 112, 90, 90), (120, 90, 140, 90, 140)])
    r = simulate(bars, _params(stop_loss=95, sell_on_stop=True), FILTERS)
    # Only the buy at 100 fills on the way down (110, nearest the start, has no order): 3 base, then the stop at
    # 95 sells all 3 and the 90 low and the rally after it are ignored.
    assert r.status == "stopped" and r.stop_reason == "stop_loss" and r.stopped_at == 60
    assert r.base_held == 0 and r.quote_held == pytest.approx(324 - 224 - 100 + 3 * 95)
    assert r.total_pnl == pytest.approx(-39) and r.grid_profit == 0 and r.floating_pnl == pytest.approx(-39)
    assert r.recent_fills[0].kind == "stop" and r.recent_fills[0].qty == 3 and not r.open_orders
    keep = simulate(bars, _params(stop_loss=95), FILTERS)  # without selling, the base is valued at the stop price
    assert keep.base_held == 3 and keep.total_pnl == pytest.approx(-39) and keep.last_price == 95


def test_take_profit_stops_at_its_price():
    bars = _df([(0, 112, 112, 112, 112), (60, 112, 140, 112, 140), (120, 140, 140, 100, 100)])
    r = simulate(bars, _params(take_profit=135, sell_on_stop=True), FILTERS)
    assert r.status == "stopped" and r.stop_reason == "take_profit" and r.last_price == 135
    assert r.matched_trades == 2 and r.base_held == 0  # 120 and 130 sold; later bars are ignored
    assert r.total_pnl == pytest.approx(120 + 130 - 224)


def test_end_time_and_runtime():
    r = simulate(ZIGZAG, _params(end_time=180), FILTERS)  # stopped before the 4th bar
    assert r.status == "stopped" and r.stop_reason == "ended" and r.bars == 3 and r.matched_trades == 2
    assert r.runtime_minutes == 3 and r.runtime_text == "3m"
    live = simulate(ZIGZAG, _params(), FILTERS, now=3 * 86400)
    assert live.runtime_minutes == 3 * 1440 and live.runtime_text == "3d 0h 0m"
    assert live.matched_trades_24h == 0  # all fills were three days ago
    assert live.grid_apr_pct == pytest.approx(50 / 324 * 100 * 365 / 3, abs=0.01)
    assert live.total_apr_pct == pytest.approx(56 / 324 * 100 * 365 / 3, abs=0.01)


def test_daily_buckets():
    day = 86400
    bars = _df([(0, 112, 121, 112, 121), (day + 60, 121, 121, 109, 109), (day + 120, 109, 121, 109, 121),
                (2 * day + 60, 121, 121, 120.5, 121)])
    r = simulate(bars, _params(), FILTERS, now=2 * day + 180)
    assert [(d.day, d.matched, d.grid_profit) for d in r.daily] == [(0, 1, 10), (day, 1, 10), (2 * day, 0, 0)]
    assert r.matched_trades_24h == 0  # the last sell (bar at day + 120) is just over 24h old
    assert simulate(bars, _params(), FILTERS, now=2 * day + 120).matched_trades_24h == 1


def test_comparison_uses_the_moment_binance_was_read():
    shows = BinanceShows(matched_trades=2, grid_profit=19.5, total_pnl=20.0, captured_at=150)
    r = simulate(ZIGZAG, _params(), FILTERS, binance=shows)
    cmp = r.comparison
    assert cmp is not None and cmp.at == 150
    # At t=150 (through the bar at 120) the app has 2 matched trades and 20 grid profit.
    assert (cmp.matched_trades.app, cmp.matched_trades.diff) == (2, 0)
    assert cmp.grid_profit.app == 20 and cmp.grid_profit.diff == pytest.approx(0.5)
    assert cmp.grid_profit.diff_pct == pytest.approx(0.5 / 19.5 * 100, abs=1e-3)
    assert r.matched_trades == 5  # the live numbers are untouched
    assert "5 matched trades" in describe_result(r)


# ------------------------------------------------------------- runtime --


@pytest.mark.parametrize("text,seconds", [
    ("12d 3h 45m", 12 * 86400 + 3 * 3600 + 45 * 60), ("3d 4h", 3 * 86400 + 4 * 3600), ("45m", 2700),
    ("6h 10m", 6 * 3600 + 600), ("3d4h12m", 3 * 86400 + 4 * 3600 + 720), ("2 days 5 hours", 2 * 86400 + 5 * 3600),
    ("1w 2d", 9 * 86400), ("12D 3H 45M 10s", 12 * 86400 + 3 * 3600 + 45 * 60 + 10), ("1h, 30 mins", 5400),
])
def test_parse_runtime(text, seconds):
    assert parse_runtime(text) == seconds


@pytest.mark.parametrize("text", ["", "abc", "5x", "3d 4", "30s", "3 months"])
def test_parse_runtime_rejects(text):
    with pytest.raises(ValueError):
        parse_runtime(text)


def test_resolve_start_floors_to_the_minute():
    now = 1_700_000_123
    p = _params(start_time=None, runtime="1h 2m")
    assert resolve_start(p, now) == (now - 3720) // 60 * 60
    assert resolve_start(_params(start_time=now - 100), now) == (now - 100) // 60 * 60
    with pytest.raises(ValueError, match="too long"):
        resolve_start(_params(start_time=None, runtime="400d"), now)
    with pytest.raises(ValueError, match="future"):
        resolve_start(_params(start_time=now + 600), now)
    with pytest.raises(ValueError, match="runtime"):
        resolve_start(_params(start_time=None), now)
    assert format_runtime(12 * 1440 + 3 * 60 + 45) == "12d 3h 45m" and format_runtime(370) == "6h 10m"


@pytest.mark.parametrize("bad", [
    dict(lower=130, upper=100), dict(lower=100, upper=100), dict(grids=1), dict(grids=501), dict(investment=0),
    dict(runtime="soon"), dict(take_profit=125), dict(stop_loss=105), dict(symbol="BTC/US DT!"),
    dict(start_time=600, end_time=60),
])
def test_param_validation(bad):
    with pytest.raises(ValidationError):
        _params(**bad)


def test_month_of_bars_is_fast():
    n = 43_200
    rng = np.random.default_rng(7)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.0008, n)))
    open_ = np.concatenate([[100.0], close[:-1]])
    df = pd.DataFrame({"time": np.arange(n) * 60, "open": open_, "high": np.maximum(open_, close) * 1.0004,
                       "low": np.minimum(open_, close) * 0.9996, "close": close, "volume": 1.0})
    p = _params(lower=float(close.min()), upper=float(close.max()), grids=150, investment=10_000)
    t0 = time.perf_counter()
    r = simulate(df, p, SymbolFilters(tick_size=0.0001, step_size=0.0001))
    assert time.perf_counter() - t0 < 1.5
    assert r.matched_trades > 100 and len(r.daily) == 31 and len(r.recent_fills) == 200
    assert r.total_pnl == pytest.approx(r.current_value - 10_000)


# ------------------------------------------------------------- service --


def _kline_row(open_s, price):
    ms = open_s * 1000
    return [ms, str(price), str(price + 0.5), str(price - 0.5), str(price), "1", ms + 59_999, "0", 1, "0", "0", "0"]


@pytest.fixture
def binance_env(monkeypatch):
    monkeypatch.setenv("DATA_SOURCE", "binance")
    monkeypatch.setenv("CANDLE_CACHE", "off")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def test_service_reads_exchange_info_and_candles(binance_env):
    now = int(time.time()) // 60 * 60
    start = now - 600
    # A sine around 115 (±12, period 4 minutes): it crosses 110 and 120 again and again.
    rows = [_kline_row(start + i * 60, round(115 + 12 * math.sin(i * math.pi / 2), 2)) for i in range(11)]
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        q = req.url.params
        if req.url.path == "/api/v3/exchangeInfo":
            if q["symbol"] == "NOPEUSDT":
                return httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."})
            return httpx.Response(200, json={"symbols": [{"symbol": q["symbol"], "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"}, {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                {"filterType": "NOTIONAL", "minNotional": "5"}]}]})
        lo, hi = int(q["startTime"]), int(q.get("endTime", 10**15))
        return httpx.Response(200, json=[r for r in rows if lo <= r[0] <= hi][: int(q["limit"])])

    md = MarketData()
    md._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    svc = GridBotService(md, store="memory")

    async def run():
        r = await svc.simulate(_params(start_time=start, investment=1000, fee_rate=0.001))
        assert r.data_source == "binance" and r.filters.source == "binance" and r.filters.min_notional == 5
        assert r.bars == 11 and r.matched_trades > 0 and r.status == "running"
        n_info = calls.count("/api/v3/exchangeInfo")
        await svc.simulate(_params(start_time=start, investment=1000))
        assert calls.count("/api/v3/exchangeInfo") == n_info  # filters are cached
        with pytest.raises(ValueError, match="no spot pair"):
            await svc.simulate(_params(symbol="NOPEUSDT", start_time=start))
        r2 = await svc.simulate_from(symbol="testusdt", lower=100, upper=130, grids=3, investment=1000,
                                     runtime="10m")
        assert r2.symbol == "TESTUSDT" and r2.start_time == start
        with pytest.raises(ValueError, match="lower price"):
            await svc.simulate_from(symbol="TESTUSDT", lower=130, upper=100, grids=3, investment=1000, runtime="1h")
        await md.close()

    asyncio.run(run())


class FakeMarket:
    """get_range over a deterministic wave (period 2h, 103–127), flagged as synthetic like the demo feed."""

    def __init__(self):
        self.calls = 0

    async def get_range(self, symbol, interval, start, end=None):
        self.calls += 1
        now = int(time.time())
        end = now if end is None else min(end, now)
        t = np.arange(start - start % 60, end + 1, 60)
        mid = 115 + 12 * np.sin(2 * np.pi * t / 7200)
        df = pd.DataFrame({"time": t, "open": mid, "high": mid + 0.3, "low": mid - 0.3, "close": mid, "volume": 1.0})
        return df, "synthetic"

    def binance_usable(self):
        return False


def test_store_round_trip(tmp_path):
    path = tmp_path / "bots.json"
    svc = GridBotService(FakeMarket(), store=str(path))

    async def run():
        bot, result = await svc.create(GridBotCreate(name="", params=_params(start_time=None, runtime="1d")))
        assert bot.name == "TESTUSDT 100–130" and bot.params.start_time is not None
        assert result.data_source == "synthetic" and any("Demo data" in n for n in result.notes)
        return bot

    bot = asyncio.run(run())
    again = GridBotService(FakeMarket(), store=str(path))
    assert [b.id for b in again.list()] == [bot.id]
    assert again.get(bot.id).params.start_time == bot.params.start_time  # the runtime was pinned once
    assert json.loads(path.read_text())["bots"][0]["params"]["runtime"] == "1d"


def test_result_cached_per_bar():
    market = FakeMarket()
    svc = GridBotService(market, store="memory")

    async def run():
        bot, _ = await svc.create(GridBotCreate(params=_params(start_time=None, runtime="2h")), now=time.time())
        calls = market.calls
        a = await svc.result(bot.id)
        b = await svc.result(bot.id)
        assert a is b and market.calls <= calls + 1
        updated = await svc.update(bot.id, GridBotPatch(params={"grids": 6}))
        assert updated is not None and updated[0].params.grids == 6 and len(updated[1].lines) == 7
        assert updated[0].params.start_time == bot.params.start_time
        assert await svc.result("missing") is None

    asyncio.run(run())


# ------------------------------------------------------------------ API --


def test_api_round_trip():
    from app.main import app

    params = {"symbol": "TEST/USDT", "lower": 100, "upper": 130, "grids": 6, "grid_type": "geometric",
              "investment": 1000, "runtime": "1d 2h"}
    with TestClient(app) as client:
        app.state.gridbots = GridBotService(FakeMarket(), store="memory")

        r = client.post("/api/gridbot/simulate", json=params)
        assert r.status_code == 200, r.text
        sim = r.json()
        assert sim["symbol"] == "TESTUSDT" and sim["data_source"] == "synthetic" and sim["matched_trades"] > 0
        assert len(sim["lines"]) == 7 and sim["runtime_text"] in ("1d 2h 0m", "1d 2h 1m")
        assert client.get("/api/gridbots").json() == {"bots": []}  # simulate saves nothing

        for bad, msg in (({"lower": 140}, "below the upper"), ({"grids": 1}, "grids"),
                         ({"investment": 0}, "investment"), ({"runtime": "forever"}, "runtime"),
                         ({"investment": 0.01}, "too small"),
                         ({"runtime": "500d"}, "too long")):
            r = client.post("/api/gridbot/simulate", json={**params, **bad})
            assert r.status_code == 422 and msg in r.json()["detail"], (bad, r.text)

        r = client.post("/api/gridbots", json={"name": "My TEST bot", "params": params,
                                               "binance": {"matched_trades": 3, "grid_profit": 1.5}})
        assert r.status_code == 200, r.text
        bot = r.json()["bot"]
        assert bot["name"] == "My TEST bot" and bot["params"]["start_time"] > 0
        assert bot["binance"]["captured_at"] > 0 and r.json()["result"]["comparison"]["matched_trades"]["binance"] == 3

        assert [b["id"] for b in client.get("/api/gridbots").json()["bots"]] == [bot["id"]]
        res = client.get(f"/api/gridbots/{bot['id']}/result").json()
        assert res["matched_trades"] == sim["matched_trades"] and res["comparison"] is not None

        r = client.patch(f"/api/gridbots/{bot['id']}", json={"name": "Renamed", "params": {"runtime": "3h"},
                                                             "binance": None})
        assert r.status_code == 200, r.text
        assert r.json()["bot"]["name"] == "Renamed" and r.json()["bot"]["binance"] is None
        assert r.json()["result"]["runtime_minutes"] in (180, 181) and r.json()["result"]["comparison"] is None
        r = client.patch(f"/api/gridbots/{bot['id']}", json={"params": {"upper": 90}})
        assert r.status_code == 422 and "below the upper" in r.json()["detail"]
        assert client.get("/api/gridbots").json()["bots"][0]["params"]["upper"] == 130  # unchanged

        assert client.delete(f"/api/gridbots/{bot['id']}").json() == {"ok": True}
        assert client.delete(f"/api/gridbots/{bot['id']}").status_code == 404
        assert client.get(f"/api/gridbots/{bot['id']}/result").status_code == 404
        assert client.patch("/api/gridbots/nope", json={"name": "x"}).status_code == 404
