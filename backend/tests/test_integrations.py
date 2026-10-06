"""Binance / CoinGecko / alternative.me payload handling, with the network mocked."""

import asyncio

import httpx

from app.market_data import MarketData
from app.market_metrics import MarketMetricsService
from app.stream_hub import parse_ws_kline

H = 3600 * 1000


def _kline(open_ms, o, h, l, c, v="10"):
    # Binance REST row: [openTime, o, h, l, c, v, closeTime, quoteVol, trades, takerBase, takerQuote, ignore]
    return [open_ms, str(o), str(h), str(l), str(c), v, open_ms + H - 1, "0", 1, "0", "0", "0"]


def test_binance_pagination_and_3h_resample(monkeypatch):
    monkeypatch.setenv("DATA_SOURCE", "binance")
    from app import config

    config.get_settings.cache_clear()
    start = 1_700_000_000_000 - (1_700_000_000_000 % (3 * H))  # aligned to a 3h bucket
    rows = [_kline(start + i * H, 10 + i, 11 + i, 9 + i, 10.5 + i) for i in range(2400)]
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        q = req.url.params
        calls.append(dict(q))
        assert q["interval"] == "1h"
        end = int(q.get("endTime", 10**18))
        limit = int(q["limit"])
        sel = [r for r in rows if r[0] <= end][-limit:]
        return httpx.Response(200, json=sel)

    md = MarketData()
    md._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    candles, source = asyncio.run(md.get_klines("INJUSDT", "3h", 700))
    config.get_settings.cache_clear()

    assert source == "binance"
    assert len(calls) >= 3  # 2103 hourly bars needs 3 pages of 1000
    assert len(candles) == 700
    assert all(c.time % 10800 == 0 for c in candles)
    times = [c.time for c in candles]
    assert times == sorted(times) and len(set(times)) == len(times)
    last = candles[-1]
    assert last.open == float(rows[-3][1]) and last.close == float(rows[-1][4])
    assert last.high == max(float(r[2]) for r in rows[-3:])


def test_auto_mode_falls_back_to_synthetic(monkeypatch):
    monkeypatch.setenv("DATA_SOURCE", "auto")
    from app import config

    config.get_settings.cache_clear()
    md = MarketData()
    md._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(451, text="geo")))
    candles, source = asyncio.run(md.get_klines("INJUSDT", "4h", 300))
    config.get_settings.cache_clear()
    assert source == "synthetic" and len(candles) == 300
    assert not md.binance_usable()  # circuit stays open for a while


def test_ws_kline_parse():
    c, closed = parse_ws_kline({"t": 1_700_000_000_000, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "100",
                                "x": True})
    assert c.time == 1_700_000_000 and c.high == 2 and closed


def test_metrics_live_payloads():
    gecko = {
        "total_market_cap": {"usd": 2.93e12},
        "total_volume": {"usd": 66.74e9},
        "market_cap_percentage": {"btc": 57.1},
        "market_cap_change_percentage_24h_usd": 0.85,
    }
    fng = [{"value": "68", "value_classification": "Greed"}, {"value": "60"}]
    svc = MarketMetricsService()
    m = {x.key: x for x in svc._build(gecko, fng).metrics}
    assert m["market_cap"].display == "$2.93T" and m["market_cap"].source == "live"
    assert m["fear_greed"].display.startswith("68/100") and m["fear_greed"].change_pct == 13.33
    assert m["btc_dominance"].display == "57.10%"
    assert m["liquidations"].source == "mock"
    asyncio.run(svc.close())


def _llm(handler, provider="ollama"):
    import json as _json  # noqa: F401
    from app.config import Settings
    from app.llm import LLMClient

    s = Settings(llm_provider=provider, openai_api_key="k", anthropic_api_key="k")
    c = LLMClient(s)
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c


def test_llm_structured_intent_ollama_openai_anthropic():
    import json

    plan = {"features": ["supply_demand", "window_levels"], "timeframe": "4h", "window_timeframes": ["4h"],
            "max_zones": 1, "answer_hint": "H4 supply"}

    def ollama(req):
        body = json.loads(req.content)
        assert body["format"]["required"]  # JSON schema passed for structured output
        return httpx.Response(200, json={"message": {"content": json.dumps(plan)}})

    def openai(req):
        body = json.loads(req.content)
        assert body["response_format"]["type"] == "json_schema"
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(plan)}}]})

    def anthropic(req):
        body = json.loads(req.content)
        assert body["tool_choice"]["type"] == "tool"
        return httpx.Response(200, json={"content": [{"type": "tool_use", "name": "analysis_plan", "input": plan}]})

    for provider, handler in (("ollama", ollama), ("openai", openai), ("anthropic", anthropic)):
        intent, engine = asyncio.run(_llm(handler, provider).parse_intent("H4 supply zone?"))
        assert engine.startswith(provider) and intent.features == plan["features"] and intent.max_zones == 1


def test_llm_bad_json_falls_back_to_rules():
    llm = _llm(lambda r: httpx.Response(200, json={"message": {"content": "not json"}}))
    intent, engine = asyncio.run(llm.parse_intent("show daily support"))
    assert engine == "rules" and "support_resistance" in intent.features and intent.timeframe == "1d"


def test_region_block_switches_to_fallback_host(monkeypatch):
    monkeypatch.setenv("DATA_SOURCE", "auto")
    from app import config

    config.get_settings.cache_clear()
    hosts = []

    def handler(req: httpx.Request) -> httpx.Response:
        hosts.append(req.url.host)
        if req.url.host == "api.binance.com":
            return httpx.Response(451, text="restricted location")
        return httpx.Response(200, json=[_kline(1_700_000_000_000 + i * H, 1, 2, 0.5, 1.5) for i in range(50)])

    md = MarketData()
    md._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    candles, source = asyncio.run(md.get_klines("BTCUSDT", "1h", 50))
    config.get_settings.cache_clear()
    assert source == "binance" and len(candles) == 50
    assert hosts == ["api.binance.com", "data-api.binance.vision"]
    assert md.ws_url == "wss://data-stream.binance.vision/ws"
