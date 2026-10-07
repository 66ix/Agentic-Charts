"""Order-book heatmap: bucketing, columns, walls, the sampler's lifecycle and the demo book (network mocked)."""

import asyncio
import os
import time

import httpx
import numpy as np
import pytest

from app import config
from app.config import Settings
from app.market_data import MarketData
from app.orderbook_heatmap import (
    DemoBook,
    OrderbookHeatmapService,
    Sample,
    bucket_book,
    heatmap_columns,
    nice_step,
    pick_bin_size,
    walls_from_sample,
)


@pytest.fixture
def auto_mode(monkeypatch):
    monkeypatch.setenv("DATA_SOURCE", "auto")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def _settings(**kw) -> Settings:
    return Settings(**{"data_source": "auto", "heatmap_interval_seconds": 0.05, "heatmap_history_minutes": 1,
                       "heatmap_range_pct": 0.5, **kw})


def _book(wall: float = 900.0) -> dict:
    bids = [[f"{100 - 0.01 * i:.2f}", "5"] for i in range(1, 300)] + [["99.50", str(wall)]]
    asks = [[f"{100 + 0.01 * i:.2f}", "5"] for i in range(1, 300)]
    return {"bids": bids, "asks": asks}


# ------------------------------------------------------------------------------------------ pure helpers


def test_nice_step_and_bin_size():
    assert [nice_step(x) for x in (0.7, 1.0, 1.3, 2.2, 3.0, 7.0, 0.0031)] == [1.0, 1.0, 2.0, 2.5, 5.0, 10.0, 0.005]
    bids = [(100 - 0.01 * i, 1.0) for i in range(1, 300)]
    asks = [(100 + 0.01 * i, 1.0) for i in range(1, 300)]
    assert pick_bin_size(bids, asks, 100.0, 3.0) == 0.05  # ±3 reached over ~120 bins
    assert pick_bin_size(bids[:5], asks[:5], 100.0, 3.0) == 0.01  # never finer than 0.01% of price


def test_bucket_book_sums_notional_per_bin():
    bids = [(99.99, 1.0), (99.96, 2.0), (99.90, 4.0), (90.0, 100.0)]  # 90 is outside ±5%
    asks = [(100.01, 1.0), (100.04, 3.0)]
    first, values = bucket_book(bids, asks, 0.05, 100.0, 5.0)
    assert first == round(99.90 / 0.05) and len(values) == 3
    assert values[0] == pytest.approx(99.90 * 4) and values[1] == pytest.approx(99.96 * 2 + 99.99)
    assert values[2] == pytest.approx(100.01 + 100.04 * 3)
    assert bucket_book([], [], 0.05, 100.0, 5.0)[1].size == 0


def test_columns_average_samples_and_respect_since():
    s = [Sample(1000, 100.0, 10, np.array([10, 20], np.float32)),
         Sample(1010, 101.0, 11, np.array([40, 60], np.float32)),
         Sample(1070, 102.0, 10, np.array([0, 5, 7], np.float32))]
    cols = heatmap_columns(s, 60)
    # Bin 10 only reached by the first sample: 10, not (10 + 0) / 2. Bin 11: (20 + 40) / 2, bin 12: 60.
    assert cols[0] == [960, 100.5, 10, [10, 30, 60]]
    assert cols[1] == [1020, 102.0, 11, [5, 7]]  # zero edge bins trimmed
    assert heatmap_columns(s, 60, since=1030) == [cols[1]] and len(heatmap_columns(s, 10)) == 3


def test_walls_are_big_bins_on_each_side():
    vals = np.full(40, 100.0, np.float32)
    vals[3], vals[30], vals[35] = 900, 1200, 300
    s = Sample(0, 20.0, 0, vals)  # bins of 1.0 from 0 to 40, mid 20
    walls = walls_from_sample(s, 1.0)
    assert walls == [{"side": "bid", "price": 3.5, "usd": 900}, {"side": "ask", "price": 30.5, "usd": 1200}]


def test_demo_book_has_walls_that_come_and_go():
    book = DemoBook("BTCUSDT", 65000.0, 3.0)
    seen, t = set(), 0.0
    for _ in range(400):
        bids, asks = book.book(65000.0, t)
        assert max(p for p, _ in bids) < 65000 < min(p for p, _ in asks)
        seen |= {round(w["price"]) for w in book.walls}
        t += 10
    assert len(seen) > 5 and len(book.walls) <= 10
    book.walls = [{"side": "bid", "price": 64000.0, "usd": 1e6, "until": t + 999}]
    book.book(63900.0, t)  # price traded down through the bid wall: eaten
    assert book.walls == [] or all(w["price"] != 64000.0 for w in book.walls)


# ------------------------------------------------------------------------------------------ service


def _service(handler, **kw):
    md = MarketData()
    md._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OrderbookHeatmapService(md, None, _settings(**kw), idle_seconds=0.3), md


def _run(svc, md, fn):
    async def go():
        try:
            return await fn()
        finally:
            await svc.close()
            await md.close()
    return asyncio.run(go())


def test_live_sampling_columns_walls_and_idle_stop(auto_mode):
    calls = []

    def depth(req):
        calls.append(req.url.params["limit"])
        assert req.url.path == "/api/v3/depth"
        return httpx.Response(200, json=_book())

    svc, md = _service(depth)

    async def go():
        first = await svc.get("BTCUSDT", 1)
        await asyncio.sleep(0.25)
        more = await svc.get("BTCUSDT", 1, since=first["columns"][-1][0])
        await asyncio.sleep(0.8)  # nobody polls: the sampler stops
        n = len(calls)
        await asyncio.sleep(0.2)
        return first, more, n, svc.stats()

    first, more, n, stats = _run(svc, md, go)
    assert first["source"] == "binance" and first["note"] is None and first["collecting"]
    assert first["bin_size"] == 0.01 and first["price"] == pytest.approx(100.0)
    assert first["walls"][0] == {"side": "bid", "price": 99.505, "usd": round(99.5 * 905)}
    assert more["columns"] and more["columns"][0][0] >= first["columns"][-1][0]
    assert calls[0] == "1000" and len(calls) == n and not stats[0]["collecting"]


def test_history_is_bounded(auto_mode):
    svc, md = _service(lambda r: httpx.Response(200, json=_book()), heatmap_interval_seconds=0.05,
                       heatmap_history_minutes=1)
    tr_len = []

    async def go():
        await svc.get("ETHUSDT")
        tr = svc._tracks["ETHUSDT"]
        tr_len.append(tr.samples.maxlen)
        for i in range(tr.samples.maxlen + 50):  # far more than fits
            tr.samples.append(tr.samples[-1]._replace(time=int(time.time())))
        return len(tr.samples)

    held = _run(svc, md, go)
    assert held == tr_len[0] == int(60 / 0.05) + 2


def test_unknown_symbol_and_binance_mode_failure(auto_mode):
    svc, md = _service(lambda r: httpx.Response(400, json={"msg": "Invalid symbol."}))
    out = _run(svc, md, lambda: svc.get("NOPEUSDT"))
    assert out["source"] == "unavailable" and "no spot order book" in out["note"] and out["columns"] == []

    svc, md = _service(lambda r: httpx.Response(503), data_source="binance")
    out = _run(svc, md, lambda: svc.get("BTCUSDT"))
    assert out["source"] == "unavailable" and "did not answer" in out["note"]


def test_failure_in_auto_mode_serves_a_marked_demo_book_with_backfill(auto_mode):
    svc, md = _service(lambda r: httpx.Response(503))

    class Hub:
        def last_price(self, symbol):
            return 7.5

    svc.hub = Hub()
    out = _run(svc, md, lambda: svc.get("INJUSDT", 5))
    assert out["source"] == "synthetic" and "demo" in out["note"] and out["price"] == pytest.approx(7.5)
    assert len(out["columns"]) >= 10 and out["started_at"] < time.time() - 50  # a minute of backfill
    assert all(c[3] for c in out["columns"])


def test_too_many_symbols_evicts_the_oldest(auto_mode, monkeypatch):
    monkeypatch.setattr("app.orderbook_heatmap.MAX_SYMBOLS", 2)
    svc, md = _service(lambda r: httpx.Response(200, json=_book()))

    async def go():
        for s in ("AAAUSDT", "BBBUSDT", "CCCUSDT"):
            await svc.get(s)
        return set(svc._tracks)

    assert _run(svc, md, go) == {"BBBUSDT", "CCCUSDT"}


def test_endpoint_offline():
    os.environ["DATA_SOURCE"] = "synthetic"
    config.get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r = client.get("/api/orderbook/heatmap", params={"symbol": "eth/usdt", "step": 60})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["symbol"] == "ETHUSDT" and body["source"] == "synthetic" and "Demo" in body["note"]
        assert body["columns"] and body["bin_size"] > 0 and body["step"] == 60 and body["walls"]
        last = body["columns"][-1][0]
        again = client.get("/api/orderbook/heatmap", params={"symbol": "ETHUSDT", "step": 60, "since": last}).json()
        assert again["columns"][0][0] == last
        assert client.get("/api/orderbook/heatmap", params={"step": -1}).status_code == 422
    config.get_settings.cache_clear()
