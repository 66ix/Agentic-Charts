"""Trade manager: advice on closed candles for a live trade (targets, breakeven, trail, structure, stop)."""

import asyncio

import numpy as np
import pandas as pd
import pytest

from app.trade_manager import ManagedTrade, NewManagedTrade, TradePatch, TradeManager, review

H = 3600
T0 = 1_700_000_000 - 1_700_000_000 % H


def frame(closes, spread=0.6):
    """Candles from closes: each opens at the previous close, wicks `spread` beyond the body."""
    closes = np.asarray(closes, dtype=float)
    opens = np.r_[closes[0], closes[:-1]]
    return pd.DataFrame({
        "time": T0 + np.arange(len(closes)) * H,
        "open": opens, "close": closes,
        "high": np.maximum(opens, closes) + spread, "low": np.minimum(opens, closes) - spread,
        "volume": np.full(len(closes), 100.0),
    })


def trade(df, i_open, **kw):
    new = NewManagedTrade(**{"symbol": "INJUSDT", "interval": "1h", "direction": "long", "entry": 100.0,
                             "stop": 95.0, "targets": [105.0, 110.0, 115.0], **kw})
    return ManagedTrade(**new.model_dump(exclude={"opened_at"}), id="t1",
                        opened_at=int(df["time"].iat[i_open]), initial_stop=new.stop)


# 40 quiet bars around 100, then the trade's bars.
BASE = list(100 + np.sin(np.arange(40) / 3))


def test_target_then_breakeven_then_trail_then_stop():
    up = [101, 102, 104, 106.5, 107, 106, 104.5, 103.5, 104.2, 105.5, 107, 108.5, 109, 108, 107.5, 108.2]
    df = frame(BASE + up)
    t = trade(df, 40)
    advice = review(t, df)
    kinds = [a.kind for a in advice]
    assert kinds[0] == "target"
    t1 = advice[0]
    assert t1.take_pct == 33 and t1.suggested_stop == 100.0 and "breakeven" in t1.text
    # Advice is never repeated on a second pass over the same candles.
    assert review(t, df) == []
    assert t.status == "open" and t.targets_hit == 1 and t.max_r > 1

    # The pullback low (~103) became a confirmed higher low: trail under it, above breakeven.
    trails = [a for a in advice if a.kind == "trail"]
    assert trails, kinds
    assert 101 < trails[-1].suggested_stop < 104
    # Each trail improves on breakeven and on the trail before it, even before the user moves anything.
    stops = [a.suggested_stop for a in trails]
    assert all(x > 100 for x in stops) and stops == sorted(stops)
    assert t.stop == 95.0  # not followed automatically

    # The user moves the stop to the trail; then price falls through it.
    t.stop = trails[-1].suggested_stop
    df2 = frame(BASE + up + [106, 104, 101])
    out = review(t, df2)
    assert out[-1].kind == "stop" and t.status == "stopped"
    assert t.exit_r == pytest.approx((t.stop - 100) / 5, abs=0.01) and t.exit_r > 0


def test_follow_advice_moves_the_tracked_stop():
    df = frame(BASE + [101, 102, 104, 106.5, 107])
    t = trade(df, 40, follow_advice=True)
    review(t, df)
    assert t.stop == 100.0 and t.advice[0].status == "done"


def test_stop_wins_when_one_candle_hits_both():
    df = frame(BASE + [101, 100], spread=6)  # second candle spans 94..107
    t = trade(df, 40)
    out = review(t, df)
    assert [a.kind for a in out] == ["stop"] and t.status == "stopped" and t.exit_r == -1


def test_last_target_closes_the_trade():
    df = frame(BASE + [101, 103, 106, 109, 112, 116])
    t = trade(df, 40, trail="off")
    out = review(t, df)
    assert [a.kind for a in out] == ["target", "target", "done"]
    assert t.status == "done" and t.exit_r == 3


def test_structure_break_against_a_long():
    # Up-trend with a higher low, then a close below that higher low.
    up = [102, 104, 107, 110, 108, 105, 103, 105, 108, 111, 114, 116, 113, 110, 108, 109, 111, 110, 107, 104, 101]
    df = frame(BASE + up, spread=0.3)
    t = trade(df, 40, targets=[120.0], trail="off")
    kinds = [a.kind for a in review(t, df)]
    assert "structure" in kinds


def test_short_mirror():
    down = [99, 98, 96, 94.5, 93, 94, 95.5, 96, 95, 93.5, 92, 91.5]
    df = frame(BASE + down)
    t = trade(df, 40, direction="short", entry=100.0, stop=105.0, targets=[95.0, 90.0])
    out = review(t, df)
    assert out[0].kind == "target" and out[0].suggested_stop == 100.0 and out[0].take_pct == 50


def test_validation():
    with pytest.raises(ValueError, match="stop below"):
        NewManagedTrade(symbol="INJUSDT", direction="long", entry=100, stop=101)
    with pytest.raises(ValueError, match="above the entry"):
        NewManagedTrade(symbol="INJUSDT", direction="long", entry=100, stop=95, targets=[99])


def test_patch_stop_must_stay_on_the_right_side():
    df = frame(BASE + [101, 102])
    t = trade(df, 40)
    review(t, df)
    tm = TradeManager.__new__(TradeManager)
    tm._trades, tm._path = {t.id: t}, None
    with pytest.raises(ValueError, match="below the price"):
        asyncio.run(tm.update(t.id, TradePatch(stop=150)))
    asyncio.run(tm.update(t.id, TradePatch(stop=99)))
    assert t.stop == 99


def test_api_manages_a_journal_entry():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        price = c.get("/api/klines", params={"symbol": "INJUSDT", "interval": "1h", "limit": 10}).json()
        last = price["candles"][-1]["close"]
        j = c.post("/api/journal", json={"symbol": "INJUSDT", "interval": "1h", "direction": "long",
                                         "entry": last, "stop": last * 0.9, "targets": [last * 1.2],
                                         "entry_type": "market"}).json()["entry"]
        r = c.post("/api/trades/managed", json={"journal_id": j["id"], "interval": "15m"})
        assert r.status_code == 200, r.text
        t = r.json()
        assert t["source"] == "journal" and t["interval"] == "15m" and t["status"] == "open"
        # Adding the same journal entry again returns the same managed trade.
        assert c.post("/api/trades/managed", json={"journal_id": j["id"]}).json()["id"] == t["id"]
        assert len(c.get("/api/trades/managed").json()["trades"]) == 1
        r = c.patch(f"/api/trades/managed/{t['id']}", json={"close_price": last})
        assert r.json()["status"] == "closed" and r.json()["exit_r"] == 0
        assert c.post("/api/trades/managed", json={"journal_id": "nope"}).status_code == 404
        assert c.post("/api/trades/managed", json={"symbol": "INJUSDT", "direction": "long",
                                                   "entry": 1, "stop": 2}).status_code == 422
        assert c.delete(f"/api/trades/managed/{t['id']}").json() == {"ok": True}
