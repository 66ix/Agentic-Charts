"""Funding, open interest, long/short, CVD, order-book walls and estimated liquidation levels (network mocked)."""

import asyncio
import os
import time

import httpx
import pytest

from app import config
from app.config import Settings
from app.derivatives import DerivativesService, LiquidationEvent
from app.futures_data import (
    REGION_NOTE,
    FuturesDataService,
    cvd_rows,
    estimate_liquidation_clusters,
    find_walls,
    parse_funding,
    parse_open_interest,
    parse_ratio_rows,
)
from app.market_data import MarketData

H = 3600


@pytest.fixture
def auto_mode(monkeypatch):
    """MarketData reads the cached global settings: run these tests as DATA_SOURCE=auto (test_api sets synthetic)."""
    monkeypatch.setenv("DATA_SOURCE", "auto")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def _settings(tmp_path, **kw) -> Settings:
    return Settings(**{"data_source": "auto", "liquidations_store": str(tmp_path / "l.json"), **kw})


def _service(tmp_path, futures_handler, spot_handler=None, **kw):
    s = _settings(tmp_path, **kw)
    md = MarketData()
    md._client = httpx.AsyncClient(transport=httpx.MockTransport(spot_handler or (lambda r: httpx.Response(500))))
    svc = FuturesDataService(md, None, s)
    svc._client = httpx.AsyncClient(transport=httpx.MockTransport(futures_handler))
    return svc, md


def _run(svc, md, coro_fn):
    async def go():
        try:
            return await coro_fn()
        finally:
            await svc.close()
            await md.close()
    return asyncio.run(go())


# ------------------------------------------------------------------------------------------ pure parsing


def test_parse_funding_interval_annualised_and_average():
    now = 1_700_000_000
    hist = [{"fundingTime": (now - k * 4 * H) * 1000, "fundingRate": "0.0001" if k < 6 else "0.0005"}
            for k in range(10, 0, -1)]
    out = parse_funding({"lastFundingRate": "0.00025", "nextFundingTime": (now + H) * 1000, "markPrice": "100.5",
                         "indexPrice": "100.4"}, hist, now=now)
    cur = out["current"]
    assert cur["interval_hours"] == 4 and cur["rate"] == 0.025 and cur["next_funding_time"] == now + H
    assert cur["annualized"] == round(0.025 * 6 * 365, 2) and cur["mark_price"] == 100.5
    assert out["history"][0] == {"time": now - 40 * H, "rate": 0.05} and out["history"][-1]["rate"] == 0.01
    assert out["avg_24h"] == 0.01  # the five 4h settlements inside the last day


def test_parse_open_interest_and_ratios():
    rows = [{"timestamp": (1_700_000_000 + i * H) * 1000, "sumOpenInterest": str(10 + i),
             "sumOpenInterestValue": str(1000 + i * 10)} for i in range(30)]
    oi = parse_open_interest(list(reversed(rows)))
    assert oi["oi_usd"] == 1290 and oi["rows"][0]["time"] == 1_700_000_000
    assert oi["change_24h_pct"] == round((1290 / 1050 - 1) * 100, 2)  # vs the bar 24h earlier
    assert parse_open_interest(rows[:5])["change_24h_pct"] is None
    ratio = parse_ratio_rows([{"timestamp": 1_700_000_000_000, "longAccount": "0.6250", "shortAccount": "0.3750",
                               "longShortRatio": "1.6667"}])
    assert ratio == [{"time": 1_700_000_000, "long_pct": 62.5, "short_pct": 37.5, "ratio": 1.6667}]


def _kline(t, close, vol, buy):
    # [openTime, o, h, l, c, v, closeTime, quoteVol, trades, takerBuyBase, takerBuyQuote, ignore]
    return [t * 1000, str(close), str(close + 1), str(close - 1), str(close), str(vol), t * 1000 + H * 1000 - 1,
            str(vol * close), 10, str(buy), str(buy * close), "0"]


def test_cvd_rows_and_resampling():
    t0 = 1_700_006_400  # a multiple of 3h
    raw = [_kline(t0 + i * H, 100, 10, 7 if i % 2 == 0 else 2) for i in range(6)]
    rows = cvd_rows(raw)
    assert [r["delta"] for r in rows] == [4, -6, 4, -6, 4, -6] and rows[-1]["cvd"] == -6
    assert rows[0]["delta_usd"] == 400 and rows[0]["buy"] == 7 and rows[0]["sell"] == 3
    three = cvd_rows(raw, 3 * H)
    assert [r["time"] for r in three] == [t0, t0 + 3 * H] and three[0]["buy"] == 16 and three[0]["delta"] == 2
    assert three[1]["cvd"] == 2 - 8


def test_find_walls_buckets_and_strength():
    bids = [(100 - 0.01 * i, 10.0) for i in range(1, 400)]
    asks = [(100 + 0.01 * i, 10.0) for i in range(1, 400)]
    bids[49] = (bids[49][0], 400.0)  # 99.50: a big bid
    asks[149] = (asks[149][0], 900.0)  # 101.50: a bigger ask
    asks[300] = (asks[300][0], 5000.0)  # 103.01: outside a 2% range
    out = find_walls(bids, asks, range_pct=2.0)
    assert out["mid"] == 100.0
    [bid] = out["bids"]
    [ask] = out["asks"]
    assert bid["price"] == pytest.approx(99.5) and bid["distance_pct"] == -0.5
    assert ask["price"] == pytest.approx(101.5) and ask["strength"] == 1.0 and 0 < bid["strength"] < 1
    assert ask["usd"] == pytest.approx(101.5 * 900 + sum(p * q for p, q in asks if 101.5 < p < 101.6), rel=0.02)
    assert out["covered_pct"]["asks"] == pytest.approx(2.0, abs=0.02)
    assert find_walls([], asks)["bids"] == []


def _flat_bars(n, price=100.0, vol=1000.0, start=1_700_000_000):
    return [(start + i * H, price, price, price, price, vol) for i in range(n)]


def test_liquidation_clusters_sit_at_leverage_levels():
    clusters = estimate_liquidation_clusters(_flat_bars(48), 100.0, bucket_pct=0.25)
    longs = sorted((c for c in clusters if c["side"] == "long"), key=lambda c: -c["price_high"])
    shorts = sorted((c for c in clusters if c["side"] == "short"), key=lambda c: c["price_low"])
    # long liq = p × (1 − 1/lev + 0.005), short liq = p × (1 + 1/lev − 0.005)
    for c, level in zip(longs, (99.5, 98.5, 96.5, 90.5)):
        assert c["price_low"] <= level <= c["price_high"]
    for c, level in zip(shorts, (100.5, 101.5, 103.5, 109.5)):
        assert c["price_low"] <= level <= c["price_high"]
    assert [c["leverage_hint"] for c in longs] == ["100x", "50x", "25x", "10x"]
    assert max(c["weight"] for c in clusters) == 1.0 and all(c["price_high"] < 100 for c in longs)
    assert longs[0]["distance_pct"] < 0 < shorts[0]["distance_pct"]


def test_liquidation_levels_already_hit_are_gone():
    bars = _flat_bars(48)
    t, *_ = bars[40]
    bars[40] = (t, 100.0, 100.0, 97.0, 100.0, 1000.0)  # a wick to 97 took out 100x and 50x longs opened before it
    clusters = estimate_liquidation_clusters(bars, 100.0, bucket_pct=0.25)
    longs = [c for c in clusters if c["side"] == "long"]
    # Only the 7 bars after the wick still have 100x/50x longs: far less weight than before the wick.
    near = [c for c in longs if c["price_high"] > 98]
    assert near and all(c["weight"] < 0.5 for c in near)
    assert estimate_liquidation_clusters([], 100.0) == []


def test_open_interest_weighting_moves_the_clusters():
    bars = [(1_700_000_000 + i * H, p, p, p, p, 1000.0) for i, p in enumerate([100.0] * 24 + [110.0] * 24)]
    oi = {b[0]: 1e6 + (5e5 if i >= 24 else 0) + i for i, b in enumerate(bars)}  # OI jumped when price hit 110
    plain = estimate_liquidation_clusters(bars, 110.0)
    weighted = estimate_liquidation_clusters(bars, 110.0, oi_usd=oi)

    def share_near_110(cs):
        return sum(c["weight"] for c in cs if c["price_low"] > 100) / sum(c["weight"] for c in cs)

    assert share_near_110(weighted) > share_near_110(plain)


# ------------------------------------------------------------------------------------------ service over HTTP


def _ratio_rows(n, long_=0.6):
    return [{"symbol": "BTCUSDT", "timestamp": (1_700_000_000 + i * H) * 1000, "longAccount": str(long_),
             "shortAccount": str(1 - long_), "longShortRatio": str(long_ / (1 - long_))} for i in range(n)]


def _futures_ok(calls):
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        p = req.url.path
        if p == "/fapi/v1/premiumIndex":
            return httpx.Response(200, json={"symbol": "BTCUSDT", "markPrice": "60000", "indexPrice": "59990",
                                             "lastFundingRate": "0.0001",
                                             "nextFundingTime": (int(time.time()) + H) * 1000})
        if p == "/fapi/v1/fundingRate":
            now = int(time.time())
            return httpx.Response(200, json=[{"fundingTime": (now - k * 8 * H) * 1000, "fundingRate": "0.0002"}
                                             for k in range(5, 0, -1)])
        if p == "/futures/data/openInterestHist":
            n = int(req.url.params["limit"])
            return httpx.Response(200, json=[{"timestamp": (1_700_000_000 + i * H) * 1000, "sumOpenInterest": "1",
                                              "sumOpenInterestValue": str(1e9 + i * 1e6)} for i in range(n)])
        if p == "/futures/data/globalLongShortAccountRatio":
            return httpx.Response(200, json=_ratio_rows(int(req.url.params["limit"]), 0.6))
        if p == "/futures/data/topLongShortPositionRatio":
            return httpx.Response(200, json=_ratio_rows(int(req.url.params["limit"]), 0.45))
        if p == "/fapi/v1/depth":
            bids = [["99.9", "1"], ["99", "500"]] + [[str(99.5 - i * 0.05), "2"] for i in range(40)]
            asks = [["100.1", "1"]] + [[str(100.5 + i * 0.05), "2"] for i in range(40)]
            return httpx.Response(200, json={"bids": bids, "asks": asks})
        return httpx.Response(404)
    return handler


def test_service_live_funding_oi_long_short(tmp_path, auto_mode):
    calls = []
    svc, md = _service(tmp_path, _futures_ok(calls))

    async def go():
        f = await svc.funding("BTCUSDT", 5)
        oi = await svc.open_interest("BTCUSDT", "1h", 30)
        ls = await svc.long_short("BTCUSDT", "1h", 30)
        await svc.funding("BTCUSDT", 5)  # cached
        return f, oi, ls

    f, oi, ls = _run(svc, md, go)
    assert f["source"] == "binance" and f["current"]["rate"] == 0.01 and f["current"]["annualized"] == 10.95
    assert f["avg_24h"] == 0.02 and len(f["history"]) == 5 and f["current"]["mark_price"] == 60000
    assert oi["source"] == "binance" and oi["oi_usd"] == 1e9 + 29e6 and oi["change_24h_pct"] is not None
    assert ls["ratio"] == pytest.approx(1.5) and ls["long_pct"] == 60 and ls["top_ratio"] == pytest.approx(0.45 / 0.55)
    assert ls["change_24h"] == 0 and len(ls["accounts"]) == 30 and len(ls["top_positions"]) == 30
    assert calls.count("/fapi/v1/premiumIndex") == 1


def test_short_oi_window_fetches_the_day_change(tmp_path, auto_mode):
    calls = []
    svc, md = _service(tmp_path, _futures_ok(calls))
    oi = _run(svc, md, lambda: svc.open_interest("BTCUSDT", "5m", 10))
    assert oi["change_24h_pct"] == round(((1e9 + 24e6) / 1e9 - 1) * 100, 2)  # from a second 1h × 25 request
    assert calls.count("/futures/data/openInterestHist") == 2


def test_geo_block_is_reported_and_not_retried(tmp_path, auto_mode):
    calls = []

    def blocked(req):
        calls.append(req.url.path)
        return httpx.Response(451, json={"code": 0, "msg": "Service unavailable from a restricted location"})

    svc, md = _service(tmp_path, blocked)

    async def go():
        return [await svc.funding("BTCUSDT"), await svc.open_interest("BTCUSDT"), await svc.long_short("BTCUSDT"),
                await svc.walls("BTCUSDT", market="futures")]

    for out in _run(svc, md, go):
        assert out["source"] == "unavailable" and out["blocked"] and out["note"] == REGION_NOTE
    assert len(calls) == 1  # after the first 451 nothing else goes out


def test_unknown_perpetual_and_network_failure(tmp_path, auto_mode):
    def handler(req):
        if req.url.params.get("symbol") == "FOOUSDT":
            return httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."})
        raise httpx.ConnectError("offline")

    svc, md = _service(tmp_path, handler)

    async def go():
        return await svc.funding("FOOUSDT"), await svc.open_interest("BTCUSDT")

    missing, offline = _run(svc, md, go)
    assert missing["source"] == "unavailable" and "FOOUSDT" in missing["note"]
    assert offline["source"] == "synthetic" and offline["rows"] and "demo" in offline["note"]


def test_binance_mode_never_serves_demo_data(tmp_path, auto_mode):
    def handler(req):
        raise httpx.ConnectError("offline")

    svc, md = _service(tmp_path, handler, data_source="binance")
    out = _run(svc, md, lambda: svc.funding("BTCUSDT"))
    assert out["source"] == "unavailable" and "current" not in out


def test_cvd_from_spot_klines(tmp_path, auto_mode):
    now_h = int(time.time()) // H * H
    rows = [_kline(now_h - (29 - i) * H, 100, 10, 8) for i in range(30)]
    seen = []

    def spot(req):
        seen.append(dict(req.url.params))
        assert req.url.path == "/api/v3/klines"
        return httpx.Response(200, json=rows[-int(req.url.params["limit"]):])

    svc, md = _service(tmp_path, lambda r: httpx.Response(500), spot)
    out = _run(svc, md, lambda: svc.cvd("BTCUSDT", "1h", 30))
    assert out["source"] == "binance" and len(out["rows"]) == 30
    assert out["rows"][-1] == {"time": now_h, "buy": 8, "sell": 2, "delta": 6, "cvd": 180}
    assert out["last_24h"]["delta"] == 6 * 24 and out["last_24h"]["buy_pct"] == 80
    assert out["totals"]["delta_usd"] == 600 * 30 and seen[0]["interval"] == "1h"


def test_cvd_three_hour_is_built_from_one_hour(tmp_path, auto_mode):
    now_h = int(time.time()) // (3 * H) * (3 * H)
    rows = [_kline(now_h - (32 - i) * H, 100, 10, 6) for i in range(33)]

    def spot(req):
        assert req.url.params["interval"] == "1h"
        return httpx.Response(200, json=rows[-int(req.url.params["limit"]):])

    svc, md = _service(tmp_path, lambda r: httpx.Response(500), spot)
    out = _run(svc, md, lambda: svc.cvd("BTCUSDT", "3h", 10))
    assert len(out["rows"]) == 10 and all(r["time"] % (3 * H) == 0 for r in out["rows"])
    assert out["rows"][-2]["buy"] == 18 and out["rows"][-2]["delta"] == 6


def test_spot_walls_and_futures_walls(tmp_path, auto_mode):
    book = {"bids": [[str(100 - 0.01 * i), "5"] for i in range(1, 300)] + [["98", "900"]],
            "asks": [[str(100 + 0.01 * i), "5"] for i in range(1, 300)]}

    def spot(req):
        assert req.url.path == "/api/v3/depth" and req.url.params["limit"] == "1000"
        return httpx.Response(200, json=book)

    svc, md = _service(tmp_path, _futures_ok([]), spot)

    async def go():
        return await svc.walls("BTCUSDT", 5), await svc.walls("BTCUSDT", 5, market="futures")

    spot_w, fut_w = _run(svc, md, go)
    assert spot_w["source"] == "binance" and spot_w["market"] == "spot"
    assert spot_w["bids"][0]["price"] == 98 and spot_w["bids"][0]["strength"] == 1 and spot_w["asks"] == []
    assert spot_w["covered_pct"]["bids"] == pytest.approx(2.99, abs=0.02)
    assert fut_w["source"] == "binance" and fut_w["bids"][0]["price"] == 99


def test_synthetic_mode_serves_flagged_demo_data(tmp_path):
    os.environ["DATA_SOURCE"] = "synthetic"  # what test_api sets for the whole session
    config.get_settings.cache_clear()
    deriv = DerivativesService(_settings(tmp_path, data_source="synthetic"))
    md = MarketData()
    svc = FuturesDataService(md, deriv, _settings(tmp_path, data_source="synthetic"))

    async def go():
        try:
            return await asyncio.gather(svc.funding("ETHUSDT"), svc.open_interest("ETHUSDT"),
                                        svc.long_short("ETHUSDT"), svc.cvd("ETHUSDT", "4h", 100),
                                        svc.walls("ETHUSDT"), svc.liquidation_levels("ETHUSDT"),
                                        svc.futures_context("ETHUSDT"), svc.funding("ETHUSDT", 10))
        finally:
            await svc.close()
            await md.close()
            await deriv.close()

    f, oi, ls, cvd, walls, liq, ctx, f10 = asyncio.run(go())
    config.get_settings.cache_clear()
    for part in (f, oi, ls, cvd, walls, liq):
        assert part["source"] == "synthetic"
    assert f["current"]["rate"] == f10["current"]["rate"]  # demo data does not depend on the request size
    assert len(oi["rows"]) == 200 and len(cvd["rows"]) == 100 and walls["bids"] and walls["asks"]
    assert liq["clusters"] and liq["recent"] == [] and liq["recent_source"] == "unavailable"
    assert {"funding", "open_interest", "long_short", "cvd_24h", "walls", "est_liquidations"} <= set(ctx)
    assert ctx["notes"] and len(str(ctx)) < 2000


def test_liquidation_levels_with_live_data(tmp_path, auto_mode):
    now_h = int(time.time()) // H * H
    spot_rows = [_kline(now_h - (167 - i) * H, 100 + (i % 5) * 0.1, 1000, 500) for i in range(168)]

    def spot(req):
        if req.url.path == "/api/v3/depth":
            return httpx.Response(200, json={"bids": [["99.9", "1"], ["99.5", "400"]], "asks": [["100.1", "9"]]})
        return httpx.Response(200, json=spot_rows[-int(req.url.params["limit"]):])

    calls = []
    svc, md = _service(tmp_path, _futures_ok(calls), spot)
    deriv = DerivativesService(_settings(tmp_path))
    deriv.stream_connected = True
    deriv.liquidations.add_events([LiquidationEvent("BTCUSDT", time.time() - 30, 99.0, 50, "long")])
    svc.derivatives = deriv

    async def go():
        try:
            return await svc.liquidation_levels("BTCUSDT"), await svc.futures_context("BTCUSDT")
        finally:
            await deriv.close()

    liq, ctx = _run(svc, md, go)
    assert liq["source"] == "binance" and liq["oi_weighted"] and liq["clusters"]
    assert "Estimated" in liq["note"] and liq["recent"][0]["usd"] == 4950 and liq["recent_source"] == "binance"
    assert ctx["est_liquidations"]["below"]["distance_pct"] < 0 < ctx["est_liquidations"]["above"]["distance_pct"]
    assert ctx["funding"]["rate_pct"] == 0.01 and ctx["sources"]["funding"] == "binance"


def test_endpoints_offline():
    os.environ["DATA_SOURCE"] = "synthetic"
    config.get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        for path, params in (("/api/futures/funding", {"symbol": "btc/usdt", "limit": 20}),
                             ("/api/futures/open-interest", {"symbol": "BTCUSDT", "period": "4h", "limit": 50}),
                             ("/api/futures/long-short", {"symbol": "BTCUSDT"}),
                             ("/api/futures/liquidation-levels", {"symbol": "BTCUSDT"}),
                             ("/api/cvd", {"symbol": "BTCUSDT", "interval": "3h", "limit": 100}),
                             ("/api/orderbook/walls", {"symbol": "BTCUSDT", "range_pct": 3})):
            r = client.get(path, params=params)
            assert r.status_code == 200, (path, r.text)
            body = r.json()
            assert body["source"] == "synthetic" and body["symbol"] == "BTCUSDT", path
        assert len(client.get("/api/cvd", params={"symbol": "BTCUSDT", "limit": 100}).json()["rows"]) == 100
        assert client.get("/api/futures/open-interest", params={"period": "3h"}).status_code == 422
        assert client.get("/api/orderbook/walls", params={"market": "options"}).status_code == 422
        assert client.get("/api/cvd", params={"interval": "2d"}).status_code == 422
    config.get_settings.cache_clear()
