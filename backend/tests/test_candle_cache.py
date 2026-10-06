"""SQLite candle cache: full history once, then only the bars Binance added since (network mocked)."""

import asyncio
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config
from app.candle_store import CandleStore
from app.market_data import MarketData, parse_binance_kline

H = 3600


def _kline(open_s, close):
    # Binance REST row: [openTime, o, h, l, c, v, closeTime, quoteVol, trades, takerBase, takerQuote, ignore]
    ms = open_s * 1000
    return [ms, str(close - 0.5), str(close + 1), str(close - 1), str(close), "10", ms + H * 1000 - 1, "0", 1, "0",
            "0", "0"]


class FakeBinance:
    """Serves `rows` like /api/v3/klines: `endTime` pages backwards, `startTime` forwards."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        q = req.url.params
        self.calls.append(dict(q))
        assert q["interval"] == "1h"
        limit = int(q["limit"])
        if "startTime" in q:
            sel = [r for r in self.rows if r[0] >= int(q["startTime"])][:limit]
        else:
            sel = [r for r in self.rows if r[0] <= int(q.get("endTime", 10**18))][-limit:]
        return httpx.Response(200, json=sel)


@pytest.fixture
def cache_path(monkeypatch, tmp_path):
    path = tmp_path / "candles.sqlite3"
    monkeypatch.setenv("DATA_SOURCE", "binance")
    monkeypatch.setenv("CANDLE_CACHE", "on")
    monkeypatch.setenv("CANDLE_CACHE_PATH", str(path))
    config.get_settings.cache_clear()
    yield path
    config.get_settings.cache_clear()


def _market(handler) -> MarketData:
    md = MarketData()
    md._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return md


def _klines(md: MarketData, *requests: tuple[str, str, int]):
    async def go():
        try:
            return [await md.get_klines(*r) for r in requests]
        finally:
            await md.close()

    return asyncio.run(go())


def _assert_series(candles, n, step):
    times = [c.time for c in candles]
    assert len(candles) == n and len(set(times)) == n
    assert all(b - a == step for a, b in zip(times, times[1:]))


def test_second_run_fetches_only_new_bars(cache_path):
    now_h = int(time.time()) // H * H
    # Binance as of an hour ago: its newest bar (now_h - 1h) is still open.
    binance = FakeBinance([_kline(now_h - H * (2000 - i), 100 + i) for i in range(2000)])
    [(candles, source)] = _klines(_market(binance), ("BTCUSDT", "1h", 300))
    assert source == "binance" and "startTime" not in binance.calls[0]
    _assert_series(candles, 300, H)
    store = CandleStore(cache_path)
    assert store.latest_time("BTCUSDT", "1h") == now_h - 2 * H  # the open bar is not stored
    assert len(store.load("BTCUSDT", "1h", 1000)) == 299
    store.close()

    # An hour later (a restart, so no in-memory cache): that bar closed higher and a new one opened.
    binance.rows[-1] = _kline(now_h - H, 5000)
    binance.rows.append(_kline(now_h, 5001))
    binance.calls.clear()
    (candles, source), (c3h, _) = _klines(_market(binance), ("BTCUSDT", "1h", 300), ("BTCUSDT", "3h", 50))
    assert source == "binance"
    # One request each, starting at the newest cached bar (the 1h call had just stored now_h - 1h).
    assert [c["startTime"] for c in binance.calls] == [str((now_h - 2 * H) * 1000), str((now_h - H) * 1000)]
    assert not any("endTime" in c for c in binance.calls)
    _assert_series(candles, 300, H)
    assert candles[-1].time == now_h and candles[-2].close == 5000  # re-fetched bar replaced the cached one
    assert candles[0].time == now_h - 299 * H
    _assert_series(c3h, 50, 3 * H)  # 3h is built from the cached 1h bars

    store = CandleStore(cache_path)
    assert store.latest_time("BTCUSDT", "1h") == now_h - H
    stored = store.load("BTCUSDT", "1h", 1000)
    store.close()
    _assert_series(stored, 300, H)


def test_stale_or_broken_cache_does_a_full_fetch(cache_path):
    now_h = int(time.time()) // H * H
    binance = FakeBinance([_kline(now_h - H * (3000 - i), 100 + i) for i in range(3000)])
    bars = [parse_binance_kline(r) for r in binance.rows]
    store = CandleStore(cache_path)
    store.save("BTCUSDT", "1h", bars[-2000:-1500])  # ends 1500 bars ago: more missing than requested
    store.close()
    [(candles, _)] = _klines(_market(binance), ("BTCUSDT", "1h", 200))
    assert binance.calls and not any("startTime" in c for c in binance.calls)
    _assert_series(candles, 200, H)

    # The cache is now the old run, a hole, then the 199 closed bars just stored. A request
    # reaching across the hole must not be served from it...
    binance.calls.clear()
    [(candles, _)] = _klines(_market(binance), ("BTCUSDT", "1h", 500))
    assert binance.calls and not any("startTime" in c for c in binance.calls)
    _assert_series(candles, 500, H)

    # ...while one inside the recent run is incremental again.
    binance.calls.clear()
    [(candles, _)] = _klines(_market(binance), ("BTCUSDT", "1h", 200))
    assert [c.get("startTime") for c in binance.calls] == [str((now_h - 2 * H) * 1000)]
    _assert_series(candles, 200, H)


def test_synthetic_data_is_never_stored(cache_path, monkeypatch):
    monkeypatch.setenv("DATA_SOURCE", "auto")
    config.get_settings.cache_clear()
    [(candles, source)] = _klines(_market(lambda r: httpx.Response(451, text="geo")), ("BTCUSDT", "1h", 100))
    assert source == "synthetic" and len(candles) == 100
    store = CandleStore(cache_path)
    assert store.latest_time("BTCUSDT", "1h") is None
    store.close()

    monkeypatch.setenv("DATA_SOURCE", "synthetic")
    config.get_settings.cache_clear()
    cache_path.unlink()
    _klines(MarketData(), ("BTCUSDT", "1h", 100))
    assert not cache_path.exists()


def test_corrupt_cache_falls_back_to_network(cache_path):
    cache_path.write_bytes(b"this is not a database" * 100)
    now_h = int(time.time()) // H * H
    binance = FakeBinance([_kline(now_h - H * (100 - i), 100 + i) for i in range(100)])
    [(candles, source)] = _klines(_market(binance), ("BTCUSDT", "1h", 50))
    assert source == "binance" and len(candles) == 50


def test_klines_since_filter(monkeypatch):
    monkeypatch.setenv("DATA_SOURCE", "synthetic")
    monkeypatch.setenv("LLM_PROVIDER", "none")
    config.get_settings.cache_clear()
    from app.main import app

    params = {"symbol": "INJUSDT", "interval": "1h", "limit": 100}
    with TestClient(app) as client:
        full = client.get("/api/klines", params=params).json()
        since = full["candles"][-5]["time"]
        part = client.get("/api/klines", params={**params, "since": since}).json()
        future = client.get("/api/klines", params={**params, "since": since + 10 * H}).json()
        bad = client.get("/api/klines", params={**params, "since": -1})
    config.get_settings.cache_clear()

    assert part.keys() == full.keys() and part["source"] == "synthetic"
    assert part["candles"] == full["candles"][-5:]
    assert future["candles"] == []
    assert bad.status_code == 422


# ------------------------------------------------------------- get_range --


class FakeRange:
    """/api/v3/klines with startTime/endTime paging forward over `rows` (1m bars)."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        q = req.url.params
        self.calls.append(dict(q))
        lo, hi, limit = int(q["startTime"]), int(q.get("endTime", 10**18)), int(q["limit"])
        return httpx.Response(200, json=[r for r in self.rows if lo <= r[0] <= hi][:limit])


def _m1(open_s, close):
    ms = open_s * 1000
    return [ms, str(close), str(close + 1), str(close - 1), str(close), "5", ms + 59_999, "0", 1, "0", "0", "0"]


def test_get_range_pages_and_caches(cache_path):
    now = int(time.time()) // 60 * 60
    start = now - 2500 * 60
    rows = [_m1(start + i * 60, 100 + i % 7) for i in range(2501)]
    fake = FakeRange(rows)
    md = _market(fake)

    async def run():
        df, src = await md.get_range("BTCUSDT", "1m", start)
        assert src == "binance"
        assert len(df) == 2501 and df["time"].iloc[0] == start and df["time"].iloc[-1] == now
        assert (df["time"].diff().dropna() == 60).all()
        first_calls = len(fake.calls)
        assert first_calls == 3  # 1000 + 1000 + 501
        fake.calls.clear()
        df2, _ = await md.get_range("BTCUSDT", "1m", start + 600)
        assert len(df2) == 2501 - 10
        # Only the bars after the last closed cached one are fetched again.
        assert len(fake.calls) == 1 and int(fake.calls[0]["startTime"]) >= (now - 60) * 1000
        await md.close()

    asyncio.run(run())


def test_get_range_synthetic_when_binance_blocked(monkeypatch, cache_path):
    monkeypatch.setenv("DATA_SOURCE", "synthetic")
    config.get_settings.cache_clear()
    md = MarketData()

    async def run():
        start = int(time.time()) - 3 * 86400
        df, src = await md.get_range("SOLUSDT", "1m", start)
        assert src == "synthetic"
        assert abs(len(df) - 3 * 1440) <= 2
        await md.close()

    asyncio.run(run())
