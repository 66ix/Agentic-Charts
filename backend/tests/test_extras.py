"""Coaching from your own trades, what changed since you last looked, and which desk setups work right now."""

import asyncio
import os

import numpy as np
import pandas as pd

from app.changes import changes, zone_changes
from app.coach import Trade, findings, overview
from app.desk_calls import DeskCall
from app.desk_learning import working_now
from app.market_data import candles_to_df, synthetic_klines

H = 3600
T0 = 1_759_000_000 - 1_759_000_000 % H


def _candles(symbol, prices):
    """Hourly candles following `prices` (one per hour from T0 - 8 days)."""
    rows = []
    for i, p in enumerate(prices):
        rows.append({"time": T0 - 8 * 86400 + i * H, "open": p, "high": p * 1.002, "low": p * 0.998, "close": p,
                     "volume": 1.0})
    return pd.DataFrame(rows)


def test_coach_finds_early_sells_and_losers_held_long():
    # Price climbs steadily: every sell is followed by more upside.
    prices = list(np.linspace(100, 160, 30 * 24))
    df = _candles("INJUSDT", prices)
    px = lambda t: float(df.loc[df["time"] <= t, "close"].iloc[-1])  # noqa: E731
    trades = []
    for k in range(8):
        o = T0 + k * 36 * H
        c = o + 6 * H
        trades.append(Trade("INJUSDT", o, c, px(o), px(c), pnl=10.0, fees=1.0))
    for k in range(4):  # losers held 30h, winners 6h
        o = T0 + (300 + k * 40) * H
        trades.append(Trade("INJUSDT", o, o + 30 * H, px(o), px(o) * 0.97, pnl=-5.0, fees=1.0))
    out = {f["code"]: f for f in findings(sorted(trades, key=lambda t: t.opened_at), {"INJUSDT": df}, [])}
    assert out["sold_early"]["tone"] == "warn" and out["sold_early"]["numbers"]["share"] > 0.5
    assert out["hold_losers"]["numbers"]["hold_loss_h"] == 30.0 and out["hold_losers"]["numbers"]["hold_win_h"] == 6.0
    assert "payoff" in out
    ov = overview(trades)
    assert ov["trades"] == 12 and ov["wins"] == 8 and ov["pnl"] == 60.0
    assert findings(trades[:3], {"INJUSDT": df}, []) == []  # too few trades to say anything


def test_coach_compares_buys_inside_the_desks_zones():
    prices = [100.0] * (30 * 24)
    df = _candles("INJUSDT", prices)
    trades = [Trade("INJUSDT", T0 + k * 10 * H, T0 + k * 10 * H + 5 * H, 100.0, 104.0 if k < 4 else 98.0,
                    pnl=4.0 if k < 4 else -2.0, fees=0.1) for k in range(8)]
    zones = [{"symbol": "INJUSDT", "low": 99.0, "high": 101.0, "from": T0 - H, "until": T0 + 35 * H}]
    out = {f["code"]: f for f in findings(trades, {"INJUSDT": df}, zones)}
    z = out["desk_zones"]
    assert z["numbers"]["count"] == 4 and z["numbers"]["avg_pct"] > z["numbers"]["others_pct"] and z["tone"] == "good"


def test_zone_changes_finds_broken_and_new_zones():
    cs = synthetic_klines("INJUSDT", "1h", 500, end=1_760_000_000)
    df = candles_to_df(cs)
    since = int(df["time"].iloc[-60])
    zc = zone_changes(df, since, "1h")
    assert set(zc) == {"broke", "tested", "new", "structure"}
    for z in zc["broke"]:
        assert z["direction"] in ("up", "down") and z["low"] < z["high"]
    assert zone_changes(df.iloc[:50], since, "1h")["broke"] == []  # too little history: nothing claimed


def test_changes_summary_on_demo_data():
    os.environ["DATA_SOURCE"] = "synthetic"
    from app.market_data import MarketData

    async def go():
        m = MarketData()
        try:
            return await changes(m, "INJUSDT", "1h", int(__import__("time").time()) - 12 * H)
        finally:
            await m.close()

    out = asyncio.run(go())
    assert out["lines"][0].startswith("Price ") and out["away"] == "12 hours" and "zones" in out


def _call(i, bucket, hit, rr=2.0, days_ago=3):
    t = T0 - days_ago * 86400
    return DeskCall(id=f"w{bucket}{i}", symbol="INJUSDT", interval="4h", created_at=t, bar_time=t, price_at_call=10.5,
                    entry=10.0, zone_low=9.8, zone_high=10.0, stop=9.0, tp=10.0 + rr, tp_level=10.0 + rr, rr=rr,
                    risk_pct=10.0, confidence=0.4, expected_r=0.1, kelly=0.1, bucket=bucket, setup=bucket,
                    expires_at=t + 86400, hold_seconds=86400, status="tp" if hit else "invalidated", level_hit=hit,
                    closed_at=t + 3600, fill_price=10.0, data_source="binance")


def test_working_now_ranks_setups_against_random_odds():
    good = [_call(i, "4h|demand|fresh|htf|with", i < 6) for i in range(8)]       # 6/8 at 2R: random gives 2.7
    bad = [_call(i, "4h|support|-|nohtf|against", i < 1) for i in range(8)]      # 1/8
    old = [_call(i, "4h|supply|x", True, days_ago=60) for i in range(8)]          # outside the window
    few = [_call(i, "4h|demand|tested|nohtf|mixed", True) for i in range(3)]     # too few to judge
    rows = working_now(good + bad + old + few, T0)
    assert [r["bucket"] for r in rows] == ["4h|demand|fresh|htf|with", "4h|support|-|nohtf|against"]
    assert rows[0]["verdict"] == "working" and rows[0]["lift"] > 2 and rows[1]["verdict"] == "not working"


def test_api_coach_and_changes():
    os.environ["DATA_SOURCE"] = "synthetic"
    os.environ["LLM_PROVIDER"] = "none"
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        rep = client.get("/api/coach").json()
        assert rep["findings"] == [] and "needs at least" in rep["note"]
        assert client.post("/api/changes", json={"symbol": "INJUSDT", "interval": "1h", "since": 0}).status_code == 422
        ok = client.post("/api/changes", json={"symbol": "INJUSDT", "interval": "4h",
                                               "since": int(__import__("time").time()) - 2 * 86400})
        assert ok.status_code == 200 and ok.json()["lines"]
        ans = client.post("/api/agent/analyze", json={"symbol": "INJUSDT", "interval": "4h",
                                                      "prompt": "how am I trading?"}).json()
        assert "coaching needs at least" in ans["summary"]
        ans = client.post("/api/agent/analyze", json={"symbol": "INJUSDT", "interval": "4h",
                                                      "prompt": "what's working right now?"}).json()
        assert "what is working" in ans["summary"]
