"""Open interest and liquidation handling, with the network mocked."""

import asyncio
import json
import time

import httpx

from app.config import Settings
from app.derivatives import (
    SYMBOL_EVENTS_CAP,
    DerivativesService,
    LiquidationEvent,
    LiquidationTracker,
    parse_force_order,
    parse_force_order_events,
    sum_open_interest,
    top_perpetuals,
)
from app.market_metrics import MarketMetricsService


def _force(symbol, side, price, qty, t_ms):
    return {"e": "forceOrder", "E": t_ms, "o": {"s": symbol, "S": side, "o": "LIMIT", "f": "IOC", "q": qty,
                                                 "p": price, "ap": price, "X": "FILLED", "l": qty, "z": qty,
                                                 "T": t_ms}}


def test_parse_force_order_sides_and_notional():
    now = int(time.time() * 1000)
    events = parse_force_order(_force("BTCUSDT", "SELL", "60000", "0.5", now))
    assert events == [(now / 1000, 30000.0, "long")]
    events = parse_force_order([_force("ETHUSDT", "BUY", "3000", "2", now), {"junk": 1}])
    assert events[0][1:] == (6000.0, "short") and len(events) == 1


def test_liquidation_window_and_persistence(tmp_path):
    store = tmp_path / "liq.json"
    now = time.time()
    t = LiquidationTracker(str(store))
    t.add([(now - 2 * 86400, 1e6, "long"), (now - 60, 2e6, "long"), (now - 30, 5e5, "short")])
    snap = t.snapshot(now)
    assert snap.usd == 2.5e6 and snap.longs_usd == 2e6 and snap.shorts_usd == 5e5 and snap.count == 2
    t.save()
    again = LiquidationTracker(str(store))
    assert again.snapshot(now).usd == 2.5e6 and again.since == t.since


def test_force_order_events_keep_symbol_and_price():
    now = int(time.time() * 1000)
    [ev] = parse_force_order_events(_force("SOLUSDT", "SELL", "150", "10", now))
    assert (ev.symbol, ev.price, ev.qty, ev.side, ev.usd, ev.time) == ("SOLUSDT", 150.0, 10.0, "long", 1500.0,
                                                                       now / 1000)
    assert parse_force_order_events({"junk": 1}) == []


def test_per_symbol_liquidations_and_header_total(tmp_path):
    store = tmp_path / "liq.json"
    now = time.time()
    t = LiquidationTracker(str(store))
    t.add_events([LiquidationEvent("BTCUSDT", now - 2 * 86400, 60000, 1, "long"),
                  LiquidationEvent("BTCUSDT", now - 120, 61000, 0.5, "long"),
                  LiquidationEvent("BTCUSDT", now - 60, 62000, 0.25, "short"),
                  LiquidationEvent("ETHUSDT", now - 30, 3000, 2, "long")])
    snap = t.snapshot(now)
    assert snap.count == 3 and snap.usd == 30500 + 15500 + 6000 and snap.longs_usd == 36500
    recent = t.recent("BTCUSDT", now=now)
    assert [r["price"] for r in recent] == [62000, 61000]  # newest first, the 2-day-old one dropped
    assert recent[0] == {"time": int(now - 60), "price": 62000, "qty": 0.25, "usd": 15500, "side": "short"}
    assert t.recent("BTCUSDT", limit=1, now=now)[0]["price"] == 62000 and t.recent("XRPUSDT") == []

    t.save()
    again = LiquidationTracker(str(store))
    assert again.snapshot(now).usd == snap.usd
    assert [r["price"] for r in again.recent("BTCUSDT", now=now)] == [62000, 61000]
    assert again.recent("ETHUSDT", now=now)[0]["usd"] == 6000


def test_per_symbol_events_are_capped():
    t = LiquidationTracker(None)
    now = time.time()
    t.add_events([LiquidationEvent("BTCUSDT", now - 1000 + i, 100 + i, 1, "long")
                  for i in range(SYMBOL_EVENTS_CAP + 50)])
    recent = t.recent("BTCUSDT", limit=10_000, now=now)
    assert len(recent) == SYMBOL_EVENTS_CAP and recent[0]["price"] == 100 + SYMBOL_EVENTS_CAP + 49
    assert t.snapshot(now).count == SYMBOL_EVENTS_CAP + 50  # the header total is not capped


def test_old_liquidation_files_still_load(tmp_path):
    store = tmp_path / "liq.json"
    now = time.time()
    store.write_text(json.dumps({"since": now - 3600, "events": [[now - 100, 1e6, "long"], [now - 50, 2e5, "short"]]}))
    t = LiquidationTracker(str(store))
    assert t.snapshot(now).usd == 1.2e6 and t.since == now - 3600 and t.recent("BTCUSDT") == []
    # A corrupt per-symbol section is skipped without losing the totals.
    store.write_text(json.dumps({"since": now - 3600, "events": [[now - 100, 1e6, "long"]],
                                 "symbols": {"BTCUSDT": [["bad"]], "ETHUSDT": [[now - 10, 3000, 1, "short"]]}}))
    t = LiquidationTracker(str(store))
    assert t.snapshot(now).usd == 1e6 and t.recent("BTCUSDT") == [] and t.recent("ETHUSDT")[0]["price"] == 3000


def test_recent_liquidations_service(tmp_path):
    svc = DerivativesService(Settings(data_source="auto", liquidations_store=str(tmp_path / "l.json")))
    assert svc.recent_liquidations("BTCUSDT") is None  # no stream and nothing saved: nothing real to show
    svc.stream_connected = True
    assert svc.recent_liquidations("BTCUSDT") == []
    svc.liquidations.add_events([LiquidationEvent("BTCUSDT", time.time() - 5, 60000, 1, "long")])
    assert svc.recent_liquidations("BTCUSDT")[0]["usd"] == 60000
    asyncio.run(svc.close())
    off = DerivativesService(Settings(data_source="synthetic", liquidations_store=str(tmp_path / "l.json")))
    assert off.recent_liquidations("BTCUSDT") is None
    asyncio.run(off.close())


def test_top_perpetuals_skips_delivery_contracts():
    tickers = [{"symbol": "BTCUSDT", "quoteVolume": "9"}, {"symbol": "BTCUSDT_261226", "quoteVolume": "99"},
               {"symbol": "ETHUSDT", "quoteVolume": "5"}, {"symbol": "ETHBTC", "quoteVolume": "50"}]
    assert top_perpetuals(tickers, 5) == ["BTCUSDT", "ETHUSDT"]


def _hist(start_value, end_value, n=25):
    step = (end_value - start_value) / (n - 1)
    return [{"symbol": "X", "sumOpenInterest": "1", "sumOpenInterestValue": str(start_value + i * step),
             "timestamp": 1_700_000_000_000 + i * 3_600_000} for i in range(n)]


def test_sum_open_interest_change():
    oi = sum_open_interest([list(reversed(_hist(100, 110))), _hist(50, 40), _hist(7, 7, n=3)])
    assert oi.usd == 157 and oi.symbols == 3
    assert oi.change_pct == round((157 - 157) / 157 * 100, 2)  # 110+40+7 vs 100+50+7
    oi = sum_open_interest([_hist(100, 120)])
    assert oi.change_pct == 20.0


def test_service_open_interest_over_http(tmp_path):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        if req.url.path == "/fapi/v1/ticker/24hr":
            return httpx.Response(200, json=[{"symbol": "BTCUSDT", "quoteVolume": "10"},
                                             {"symbol": "ETHUSDT", "quoteVolume": "5"}])
        assert req.url.params["period"] == "1h"
        value = 1e10 if req.url.params["symbol"] == "BTCUSDT" else 5e9
        return httpx.Response(200, json=_hist(value, value * 1.1))

    s = Settings(data_source="auto", liquidations_store=str(tmp_path / "l.json"), oi_top_symbols=2)
    svc = DerivativesService(s)
    svc._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    oi = asyncio.run(svc.open_interest())
    assert round(oi.usd) == round(1.65e10) and oi.change_pct == 10.0 and oi.symbols == 2
    asyncio.run(svc.open_interest())  # cached
    assert calls.count("/fapi/v1/ticker/24hr") == 1


def test_header_uses_live_derivatives(tmp_path):
    s = Settings(data_source="auto", liquidations_store=str(tmp_path / "l.json"))
    svc = DerivativesService(s)
    svc.stream_connected = True
    svc.liquidations.add([(time.time() - 10, 1.5e6, "long"), (time.time() - 5, 5e5, "short")])
    oi = sum_open_interest([_hist(3e10, 3.3e10)])
    metrics = MarketMetricsService(svc)
    m = {x.key: x for x in metrics._build(None, None, oi, svc.liquidation_snapshot()).metrics}
    assert m["open_interest"].source == "live" and m["open_interest"].display == "$33.00B"
    assert m["open_interest"].change_pct == 10.0
    assert m["liquidations"].source == "live" and m["liquidations"].display == "$2.00M"
    assert "Longs $1.50M" in m["liquidations"].note
    assert m["market_cap"].source == "mock"  # CoinGecko missing → mocked with a note
    asyncio.run(metrics.close())
    asyncio.run(svc.close())


def test_derivatives_off_in_synthetic_mode(tmp_path):
    svc = DerivativesService(Settings(data_source="synthetic", liquidations_store=str(tmp_path / "l.json")))
    assert asyncio.run(svc.open_interest()) is None and svc.liquidation_snapshot() is None
    asyncio.run(svc.close())
