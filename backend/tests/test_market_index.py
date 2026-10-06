"""TOTAL / TOTAL2 / TOTAL3 market-cap indexes: aggregation, alignment, membership and fallbacks."""

import asyncio
import os

import httpx
import pytest

from app import config
from app.market_index import STATIC_SUPPLY, MarketIndexService, build_index
from app.schemas import Candle

H = 3600
T0 = 1_700_000_000 // H * H


def _bars(n, price, start=T0, wick=0.0):
    return [(start + i * H, price, price * (1 + wick), price * (1 - wick), price, 10.0) for i in range(n)]


def test_build_index_sums_price_times_supply():
    series = {"BTC": _bars(5, 100.0), "ETH": _bars(5, 10.0)}
    candles, used = build_index(series, {"BTC": 2.0, "ETH": 30.0}, limit=10)
    assert used == ["BTC", "ETH"] and len(candles) == 5
    c = candles[0]
    assert c["open"] == c["close"] == 2 * 100 + 30 * 10 and c["volume"] == 10 * 100 + 10 * 10


def test_build_index_wicks_are_cap_weighted():
    series = {"A": _bars(3, 100.0, wick=0.02), "B": _bars(3, 100.0, wick=0.0)}
    candles, _ = build_index(series, {"A": 1.0, "B": 3.0}, limit=3)
    c = candles[-1]
    assert c["close"] == 400 and c["high"] == pytest.approx(400 * (1 + 0.25 * 0.02))
    assert c["low"] == pytest.approx(400 * (1 - 0.25 * 0.02))


def test_coin_missing_a_bar_is_left_out_of_the_whole_series():
    gappy = [b for b in _bars(6, 5.0) if b[0] != T0 + 2 * H]
    young = _bars(3, 7.0, start=T0 + 3 * H)
    candles, used = build_index({"BTC": _bars(6, 100.0), "SOL": gappy, "NEW": young, "ETH": _bars(6, 10.0)},
                                {"BTC": 1.0, "SOL": 1.0, "NEW": 1.0, "ETH": 1.0}, limit=6)
    assert used == ["BTC", "ETH"] and len(candles) == 6
    assert {c["close"] for c in candles} == {110.0}  # the level never jumps


def test_a_bar_most_coins_lack_yet_is_dropped():
    series = {"A": _bars(5, 1.0), "B": _bars(4, 1.0), "C": _bars(4, 1.0)}  # only A has opened the newest bar
    candles, used = build_index(series, {"A": 1.0, "B": 1.0, "C": 1.0}, limit=10)
    assert len(candles) == 4 and used == ["A", "B", "C"]
    assert build_index({}, {}, 10) == ([], [])


class FakeMarket:
    """list_symbols + get_klines like MarketData, from fixed prices."""

    def __init__(self, prices, source="binance", listed=None):
        self.prices = prices
        self.source = source
        self.listed = listed or [f"{c}USDT" for c in prices]
        self.calls = []

    async def list_symbols(self):
        return self.listed

    async def get_klines(self, symbol, interval, limit):
        self.calls.append(symbol)
        p = self.prices[symbol.removesuffix("USDT")]
        return [Candle(time=T0 + i * H, open=p, high=p, low=p, close=p, volume=1.0) for i in range(limit)], \
            self.source


@pytest.fixture
def auto_mode(monkeypatch):
    monkeypatch.setenv("DATA_SOURCE", "auto")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def _gecko(rows):
    def handler(req):
        assert req.url.path == "/api/v3/coins/markets" and req.url.params["vs_currency"] == "usd"
        return httpx.Response(200, json=rows)
    return handler


def _service(market, handler):
    svc = MarketIndexService(market)
    svc._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return svc


def _run(svc, fn):
    async def go():
        try:
            return await fn()
        finally:
            await svc.close()
    return asyncio.run(go())


def test_total_2_3_membership_with_coingecko_supply(auto_mode):
    rows = [{"symbol": "btc", "circulating_supply": 2}, {"symbol": "eth", "circulating_supply": 10},
            {"symbol": "usdt", "circulating_supply": 1e9}, {"symbol": "sol", "circulating_supply": 100},
            {"symbol": "steth", "circulating_supply": 5}, {"symbol": "xyz", "circulating_supply": 50}]
    market = FakeMarket({"BTC": 1000.0, "ETH": 100.0, "SOL": 10.0, "STETH": 100.0},
                        listed=["BTCUSDT", "ETHUSDT", "SOLUSDT", "STETHUSDT"])  # XYZ has no Binance pair
    svc = _service(market, _gecko(rows))

    async def go():
        out = [await svc.klines(n, "1h", 20) for n in ("TOTAL", "total2", "TOTAL3")]
        return out + [await svc.klines("TOTAL", "1h", 20)]

    total, total2, total3, again = _run(svc, go)
    assert total["coins"] == ["BTC", "ETH", "SOL"] and total["candles"][-1]["close"] == 2000 + 1000 + 1000
    assert total2["symbol"] == "TOTAL2" and total2["coins"] == ["ETH", "SOL"] and total2["candles"][-1]["close"] == 2000
    assert total3["coins"] == ["SOL"] and total3["candles"][-1]["close"] == 1000
    assert total["source"] == "binance" and total["supply_source"] == "coingecko" and "top 3" in total["note"]
    assert "except BTC and ETH" in total3["note"] and len(total["candles"]) == 20
    assert again is total  # cached


def test_static_supply_when_coingecko_is_down(auto_mode):
    def down(req):
        raise httpx.ConnectError("offline")

    svc = _service(FakeMarket({"BTC": 100.0, "ETH": 10.0}), down)
    out = _run(svc, lambda: svc.klines("TOTAL", "4h", 10))
    assert out["supply_source"] == "static" and "built-in table" in out["note"]
    expected = 100 * STATIC_SUPPLY["BTC"] + 10 * STATIC_SUPPLY["ETH"]
    assert out["candles"][-1]["close"] == pytest.approx(expected, rel=1e-6)


def test_demo_prices_are_flagged_and_never_mixed(auto_mode):
    svc = _service(FakeMarket({"BTC": 100.0, "ETH": 10.0}, source="synthetic"), _gecko([]))
    out = _run(svc, lambda: svc.klines("TOTAL", "1h", 10))
    assert out["source"] == "synthetic" and "Demo data" in out["note"]

    svc = _service(FakeMarket({"BTC": 100.0}), _gecko([]))
    with pytest.raises(ValueError):
        _run(svc, lambda: svc.klines("TOTAL4", "1h", 10))


def test_index_endpoint_offline():
    os.environ["DATA_SOURCE"] = "synthetic"
    config.get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r = client.get("/api/index/klines", params={"name": "TOTAL2", "interval": "4h", "limit": 50})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["symbol"] == "TOTAL2" and body["source"] == "synthetic" and len(body["candles"]) == 50
        assert "BTC" not in body["coins"] and body["supply_source"] == "static"
        assert client.get("/api/index/klines", params={"name": "TOTAL9"}).status_code == 422
        assert client.get("/api/index/klines", params={"name": "TOTAL", "interval": "2d"}).status_code == 422
    config.get_settings.cache_clear()
