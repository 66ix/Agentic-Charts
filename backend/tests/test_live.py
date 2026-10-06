"""Live checks against the real APIs. Skipped unless LIVE_TESTS=1.

    LIVE_TESTS=1 pytest tests/test_live.py -v

Binance futures is blocked in some regions (HTTP 451 from US IPs, including
GitHub's runners); those checks skip with the reason instead of failing.
"""

import asyncio
import json
import os

import httpx
import pytest
import websockets

from app.config import Settings, get_settings
from app.derivatives import DerivativesService, parse_force_order
from app.market_data import MarketData
from app.market_metrics import MarketMetricsService

pytestmark = pytest.mark.skipif(os.getenv("LIVE_TESTS") != "1", reason="set LIVE_TESTS=1 to hit real APIs")


def _skip_if_blocked(exc: BaseException) -> None:
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (403, 451):
        pytest.skip(f"region-blocked: {exc.response.status_code} from {exc.request.url.host}")


def test_coingecko_and_fear_greed():
    svc = MarketMetricsService()

    async def go():
        try:
            return await svc._coingecko(), await svc._fear_greed()
        finally:
            await svc.close()

    gecko, fng = asyncio.run(go())
    m = {x.key: x for x in svc._build(gecko, fng).metrics}
    assert m["market_cap"].source == "live" and m["market_cap"].value > 1e11
    assert 0 < m["btc_dominance"].value < 100
    assert m["fear_greed"].source == "live" and 0 <= m["fear_greed"].value <= 100


def test_binance_spot_klines(monkeypatch):
    monkeypatch.setenv("DATA_SOURCE", "binance")
    get_settings.cache_clear()
    md = MarketData()

    async def go():
        try:
            return await md.get_klines("BTCUSDT", "3h", 300), md.rest_url
        finally:
            await md.close()

    (candles, source), url = asyncio.run(go())
    get_settings.cache_clear()
    assert source == "binance" and len(candles) == 300, url
    assert all(c.time % 10800 == 0 for c in candles)
    assert candles[-1].close > 1000


def test_binance_spot_websocket():
    md = MarketData()

    async def go():
        await md.get_klines("BTCUSDT", "1m", 10)  # picks the reachable endpoint
        async with websockets.connect(f"{md.ws_url}/btcusdt@kline_1m", open_timeout=10) as ws:
            return json.loads(await asyncio.wait_for(ws.recv(), 20))

    msg = asyncio.run(go())
    assert msg["k"]["s"] == "BTCUSDT"


def test_binance_futures_open_interest(tmp_path):
    svc = DerivativesService(Settings(data_source="auto", liquidations_store=str(tmp_path / "l.json")))

    async def go():
        try:
            return await svc.open_interest()
        finally:
            await svc.close()

    try:
        oi = asyncio.run(go())
    except httpx.HTTPStatusError as exc:
        _skip_if_blocked(exc)
        raise
    assert oi.usd > 1e9 and oi.symbols >= 10
    assert oi.change_pct is None or -50 < oi.change_pct < 50


def test_binance_liquidation_stream():
    url = f"{Settings().binance_futures_ws_url}/!forceOrder@arr"

    async def go():
        try:
            async with websockets.connect(url, open_timeout=10) as ws:
                return json.loads(await asyncio.wait_for(ws.recv(), 120))
        except websockets.exceptions.InvalidHandshake as exc:
            pytest.skip(f"region-blocked: {exc}")

    msg = asyncio.run(go())
    events = parse_force_order(msg)
    assert events and events[0][1] > 0
