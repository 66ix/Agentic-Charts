"""Tickers: an unknown pair never breaks the batch, answers are reused briefly, and a failed refresh serves the last
real prices before demo candles."""

import asyncio
import dataclasses
import json

import httpx

from app import scanner
from app.market_data import MarketData


def _md(handler) -> MarketData:
    md = MarketData()
    md.settings = dataclasses.replace(md.settings, data_source="auto")
    if md._store is not None:
        md._store.close()
        md._store = None
    md._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return md


def test_unknown_pairs_are_left_out_and_last_prices_stand_in():
    scanner._last_tickers.clear()
    asked, state = [], {"up": True}

    def handler(req):
        if req.url.path == "/api/v3/ticker/24hr":
            asked.append(json.loads(req.url.params["symbols"]))
            if not state["up"]:
                return httpx.Response(503)
            return httpx.Response(200, json=[{"symbol": s, "lastPrice": "10", "openPrice": "8"} for s in asked[-1]])
        return httpx.Response(503)

    md = _md(handler)
    md.usd_pairs = frozenset({"INJUSDT", "SOLUSDT"})

    async def go():
        first = await scanner.tickers(md, ["INJUSDT", "ETHWUSDT", "SOLUSDT"])
        again = await scanner.tickers(md, ["INJUSDT", "SOLUSDT"])  # within TICKER_FRESH: no new call
        state["up"] = False
        scanner._last_tickers.update({k: (v[0] - 10, v[1]) for k, v in scanner._last_tickers.items()})
        stale = await scanner.tickers(md, ["INJUSDT"])
        await md.close()
        return first, again, stale

    first, again, stale = asyncio.run(go())
    assert asked[0] == ["INJUSDT", "SOLUSDT"] and [t["symbol"] for t in first] == ["INJUSDT", "SOLUSDT"]
    assert first[0]["change_pct"] == 25.0 and len(asked) == 2  # 'again' reused, the third call failed
    assert again == first
    assert stale[0]["price"] == 10.0 and stale[0]["stale"] and stale[0]["source"] == "binance"
