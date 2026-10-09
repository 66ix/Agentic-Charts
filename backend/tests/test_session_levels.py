"""Session (Asia / London / New York), previous day / week / month and opening-range levels (network mocked)."""

import asyncio
import os
from datetime import date, datetime, timezone

import httpx
import numpy as np
import pandas as pd
import pytest

from app import config
from app.market_data import INTERVAL_SECONDS, MarketData
from app.session_levels import (
    SessionLevelsService,
    clamp_or_minutes,
    compute_levels,
    level_facts,
    level_lines,
    local_to_utc,
    or_resolution,
    period_window,
    recent_sessions,
    session_windows,
    shows,
    utc_offset,
)

H = 3600


def ts(y, m, d, hh=0, mm=0) -> int:
    return int(datetime(y, m, d, hh, mm, tzinfo=timezone.utc).timestamp())


@pytest.fixture
def auto_mode(monkeypatch):
    monkeypatch.setenv("DATA_SOURCE", "auto")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


# ------------------------------------------------------------------------------------------ time zones


def test_dst_rules_switch_on_the_right_days():
    # 2026: UK summer time 29 Mar 01:00 UTC → 25 Oct 01:00 UTC; US daylight time 8 Mar 07:00 UTC → 1 Nov 06:00 UTC.
    assert utc_offset("london", ts(2026, 3, 29, 0, 59)) == 0 and utc_offset("london", ts(2026, 3, 29, 1)) == H
    assert utc_offset("london", ts(2026, 10, 25, 0, 59)) == H and utc_offset("london", ts(2026, 10, 25, 1)) == 0
    ny = lambda *a: utc_offset("new_york", ts(*a)) // H  # noqa: E731
    assert ny(2026, 3, 8, 6, 59) == -5 and ny(2026, 3, 8, 7) == -4
    assert ny(2026, 11, 1, 5, 59) == -4 and ny(2026, 11, 1, 6) == -5
    assert utc_offset("tokyo", ts(2026, 7, 1)) == 9 * H


def test_dst_rules_match_the_tz_database():
    zoneinfo = pytest.importorskip("zoneinfo")
    try:
        zones = {"london": zoneinfo.ZoneInfo("Europe/London"), "new_york": zoneinfo.ZoneInfo("America/New_York"),
                 "tokyo": zoneinfo.ZoneInfo("Asia/Tokyo")}
    except zoneinfo.ZoneInfoNotFoundError:
        pytest.skip("no tz database here")
    for t in range(ts(2024, 1, 1), ts(2029, 1, 1), 5 * H + 17 * 60):
        for name, tz in zones.items():
            want = int(datetime.fromtimestamp(t, tz).utcoffset().total_seconds())
            assert utc_offset(name, t) == want, (name, datetime.fromtimestamp(t, timezone.utc))


def test_session_windows_follow_local_time_and_skip_weekends():
    # Fri 27 Mar 2026: the US is on daylight time, the UK not yet. Mon 30 Mar: both are.
    assert local_to_utc(date(2026, 3, 27), 8, 0, "london") == ts(2026, 3, 27, 8)
    assert local_to_utc(date(2026, 3, 30), 8, 0, "london") == ts(2026, 3, 30, 7)
    assert local_to_utc(date(2026, 3, 27), 9, 30, "new_york") == ts(2026, 3, 27, 13, 30)
    assert local_to_utc(date(2026, 1, 9), 9, 30, "new_york") == ts(2026, 1, 9, 14, 30)
    wins = session_windows("london", ts(2026, 3, 27), ts(2026, 3, 31))
    assert wins == [(ts(2026, 3, 27, 8), ts(2026, 3, 27, 16, 30)), (ts(2026, 3, 30, 7), ts(2026, 3, 30, 15, 30))]
    assert session_windows("asia", ts(2026, 3, 30), ts(2026, 3, 30, 1)) == [(ts(2026, 3, 30), ts(2026, 3, 30, 9))]
    ny = session_windows("ny", ts(2026, 11, 2), ts(2026, 11, 3))  # first Monday back on standard time
    assert ny[0] == (ts(2026, 11, 2, 14, 30), ts(2026, 11, 2, 21))


def test_recent_sessions_and_periods():
    mon = ts(2026, 3, 30, 3)  # Monday 03:00 UTC: Asia running, London last ran on Friday
    assert recent_sessions("london", mon) == [(ts(2026, 3, 27, 8), ts(2026, 3, 27, 16, 30)),
                                              (ts(2026, 3, 26, 8), ts(2026, 3, 26, 16, 30))]
    assert recent_sessions("asia", mon)[0] == (ts(2026, 3, 30), ts(2026, 3, 30, 9))
    assert period_window("day", mon) == (ts(2026, 3, 29), ts(2026, 3, 30))
    assert period_window("week", mon) == (ts(2026, 3, 23), ts(2026, 3, 30))
    assert period_window("week", ts(2026, 4, 5, 23)) == (ts(2026, 3, 23), ts(2026, 3, 30))  # Sunday: same week
    assert period_window("month", ts(2026, 1, 15)) == (ts(2025, 12, 1), ts(2026, 1, 1))
    assert period_window("month", ts(2026, 3, 1), 0) == (ts(2026, 3, 1), ts(2026, 4, 1))


def test_opening_range_helpers_and_timeframe_filter():
    assert [or_resolution(m) for m in (30, 60, 15, 45, 5, 20)] == ["30m", "30m", "15m", "15m", "5m", "5m"]
    assert clamp_or_minutes(1) == 5 and clamp_or_minutes(33) == 35 and clamp_or_minutes(999) == 240
    assert shows("session", "london", "4h") and not shows("session", "london", "1d")
    assert not shows("opening_range", "day", "1w") and shows("period", "day", "4h")
    assert not shows("period", "day", "1d") and shows("period", "week", "1d") and not shows("period", "week", "1w")
    assert shows("period", "month", "1w") and not shows("period", "month", "1M") and shows("session", "ny", None)


# ------------------------------------------------------------------------------------------ levels from candles


def _frame(interval: str, start: int, end: int, base: float = 100.0, spikes: dict[int, tuple[float, float]] = {}):
    """Flat bars (high base+1, low base-1) from start to end, with (high, low) overrides at given bar times."""
    step = INTERVAL_SECONDS[interval]
    times = np.arange(start - start % step, end + 1, step)
    hi = np.full(len(times), base + 1.0)
    lo = np.full(len(times), base - 1.0)
    for t, (h, l) in spikes.items():
        i = int(np.searchsorted(times, t))
        hi[i], lo[i] = h, l
    return pd.DataFrame({"time": times, "open": base, "high": hi, "low": lo, "close": base, "volume": 1.0})


def test_compute_levels_sessions_periods_and_taken():
    now = ts(2026, 3, 31, 10)  # Tuesday 10:00 UTC (BST): London live since 07:00
    spikes = {
        ts(2026, 3, 31, 8): (106.0, 99.0),  # today's London high
        ts(2026, 3, 30, 12): (108.0, 99.0),  # Monday London high (previous London session)...
        ts(2026, 3, 31, 9, 30): (108.5, 99.0),  # ...taken out today (after Asia closed at 09:00)
        ts(2026, 3, 30, 15): (103.0, 92.0),  # Monday NY low, and the previous day's low
    }
    fine = _frame("30m", now - 10 * 86400, now, spikes=spikes)
    coarse = _frame("4h", now - 70 * 86400, now, spikes={ts(2026, 3, 30, 12): (108.0, 92.0)})
    out = compute_levels(fine, coarse, None, 30, now, "15m")
    s = {(r["key"], r["which"]): r for r in out["sessions"]}
    lon = s[("london", "latest")]
    assert lon["open"] == ts(2026, 3, 31, 7) and lon["live"] and lon["high"] == 108.5 and lon["high_taken_at"] is None
    prev = s[("london", "previous")]
    assert prev["high"] == 108.0 and prev["high_taken_at"] == ts(2026, 3, 31, 9, 30) and prev["low_taken_at"] is None
    assert s[("ny", "latest")]["low"] == 92.0 and s[("ny", "latest")]["open"] == ts(2026, 3, 30, 13, 30)
    asia = s[("asia", "latest")]
    assert not asia["live"] and asia["high"] == 106.0 and asia["high_taken_at"] == ts(2026, 3, 31, 9, 30)
    p = {r["key"]: r for r in out["periods"]}
    assert p["day"]["open"] == ts(2026, 3, 30) and p["day"]["high"] == 108.0 and p["day"]["low"] == 92.0
    assert p["day"]["high_taken_at"] == ts(2026, 3, 31, 9, 30)
    assert p["week"]["open"] == ts(2026, 3, 23) and p["month"]["open"] == ts(2026, 2, 1)
    ors = {r["key"]: r for r in out["opening_ranges"]}
    assert ors["london"]["open"] == ts(2026, 3, 31, 7) and ors["london"]["close"] == ts(2026, 3, 31, 7, 30)
    assert ors["london"]["until"] == lon["close"] and not ors["london"]["live"]
    assert ors["day"]["until"] == ts(2026, 4, 1)

    daily = compute_levels(fine, coarse, None, 30, now, "1d")
    assert daily["sessions"] == [] and daily["opening_ranges"] == [] and {r["key"] for r in daily["periods"]} == {
        "week", "month"}


def test_opening_range_uses_finer_candles_and_can_be_forming():
    now = ts(2026, 3, 31, 7, 10)  # ten minutes into London
    fine = _frame("30m", now - 10 * 86400, now)
    five = _frame("5m", now - 86400, now, spikes={ts(2026, 3, 31, 7, 5): (101.5, 98.2)})
    out = compute_levels(fine, fine, five, 15, now, "1m")
    lon = next(r for r in out["opening_ranges"] if r["key"] == "london")
    assert lon["live"] and lon["close"] == ts(2026, 3, 31, 7, 15) and (lon["high"], lon["low"]) == (101.5, 98.2)


def test_level_facts_say_where_price_is():
    data = {"source": "binance", "sessions": [
        {"key": "london", "name": "London", "label": "LON", "which": "latest", "open": 0, "close": 1, "live": True,
         "high": 100.05, "low": 97.0, "high_taken_at": None, "low_taken_at": None}],
        "periods": [{"key": "day", "name": "previous day", "label": "PD", "open": 0, "close": 1, "live": False,
                     "high": 102.0, "low": 95.0, "high_taken_at": None, "low_taken_at": 5}],
        "opening_ranges": []}
    f = level_facts(data, 100.0)
    assert f["at"] == ["London session high (session in progress)"]
    assert f["above"][0]["level"] == "previous day high" and f["above"][0]["distance_pct"] == 2.0
    assert f["below"] == [{"level": "London session low (session in progress)", "price": 97.0, "distance_pct": -3.0}]
    assert f["sessions_in_progress"] == ["London"]  # the taken previous-day low is not offered as a level
    text = " ".join(level_lines(f))
    assert "Price is at the London session high" in text and "previous day high at 102" in text


# ------------------------------------------------------------------------------------------ service


def _kline_handler(calls: list):
    def handler(req: httpx.Request) -> httpx.Response:
        iv = req.url.params["interval"]
        calls.append(iv)
        step = INTERVAL_SECONDS[iv]
        n = int(req.url.params["limit"])
        end = int(req.url.params.get("endTime", 0)) // 1000 or int(datetime.now(timezone.utc).timestamp())
        last = end - end % step
        rows = [[(last - (n - 1 - i) * step) * 1000, "100", "101", "99", "100", "5", 0, "500", 1, "2", "200", "0"]
                for i in range(n)]
        return httpx.Response(200, json=rows)
    return handler


def test_service_live_and_cached(auto_mode):
    calls: list = []
    md = MarketData()
    md._client = httpx.AsyncClient(transport=httpx.MockTransport(_kline_handler(calls)))
    svc = SessionLevelsService(md)

    async def go():
        try:
            a = await svc.get("BTCUSDT", "15m", 15)
            b = await svc.get("BTCUSDT", "15m", 15)
            return a, b
        finally:
            await md.close()

    a, b = asyncio.run(go())
    assert a is b and a["source"] == "binance" and "note" not in a and a["or_minutes"] == 15
    assert {"30m", "4h", "15m"} <= set(calls)
    assert a["periods"] and a["opening_ranges"] and all(r["high"] == 101 for r in a["periods"])


def test_service_falls_back_to_demo_data(auto_mode):
    md = MarketData()
    md._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    svc = SessionLevelsService(md)

    async def go():
        try:
            return await svc.get("ETHUSDT", "1h")
        finally:
            await md.close()

    out = asyncio.run(go())
    assert out["source"] == "synthetic" and "Demo" in out["note"] and out["sessions"] and out["periods"]


def test_endpoint_offline():
    os.environ["DATA_SOURCE"] = "synthetic"
    os.environ["LLM_PROVIDER"] = "none"  # the rule-based summary, which spells the facts out
    config.get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r = client.get("/api/levels/sessions", params={"symbol": "btc/usdt", "interval": "5m", "or_minutes": 60})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["symbol"] == "BTCUSDT" and body["source"] == "synthetic" and body["or_minutes"] == 60
        assert body["sessions"] and body["periods"] and body["opening_ranges"]
        assert client.get("/api/levels/sessions", params={"interval": "1d"}).json()["sessions"] == []
        assert client.get("/api/levels/sessions", params={"interval": "2d"}).status_code == 422
        assert client.get("/api/levels/sessions", params={"or_minutes": 1}).status_code == 422
        summary = client.post("/api/agent/analyze", json={"symbol": "BTCUSDT", "interval": "1h",
                                                          "prompt": "where is price?"}).json()["summary"]
        assert "session" in summary.lower()
    config.get_settings.cache_clear()
