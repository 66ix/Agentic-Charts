"""DCA planner on constructed daily candles."""

import os

os.environ["DATA_SOURCE"] = "synthetic"

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.dca import DcaRequest, plan_dca  # noqa: E402
from app.main import app  # noqa: E402


def daily(closes):
    c = np.asarray(closes, dtype=float)
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"time": 1_700_000_000 + np.arange(len(c)) * 86400, "open": o, "high": np.maximum(o, c),
                         "low": np.minimum(o, c), "close": c, "volume": 1.0})


def test_every_strategy_invests_the_budget_and_spreading_wins_in_a_fall():
    df = daily(np.r_[np.full(40, 100.0), np.linspace(100, 50, 60), np.linspace(50, 70, 40)])
    r = plan_dca(df, DcaRequest(symbol="XUSDT", budget=1000, days=100, fee_pct=0.1))
    by = {s.id: s for s in r.strategies}
    assert all(abs(s.invested - 1000) < 1e-6 for s in r.strategies)
    assert by["lump"].return_pct < by["weekly"].return_pct and by["lump"].return_pct < by["dips"].return_pct
    assert by["dips"].buys < by["weekly"].buys and by["daily"].buys == 100
    assert r.best != "lump" and "spreading the buys out helped" in r.summary
    assert by["lump"].max_drawdown_pct < -20


def test_lump_sum_wins_a_steady_rise():
    r = plan_dca(daily(np.linspace(50, 150, 200)), DcaRequest(symbol="XUSDT", days=150))
    assert r.best == "lump" and r.buy_hold_pct > 0


def test_endpoint():
    with TestClient(app) as client:
        r = client.post("/api/dca", json={"symbol": "btc", "budget": 500, "days": 90})
        assert r.status_code == 200, r.text
        assert r.json()["symbol"] == "BTCUSDT" and len(r.json()["strategies"]) == 4


def test_replay_levels_only_use_candles_up_to_the_point():
    import time as _t

    with TestClient(app) as client:
        t = (int(_t.time()) // 14400 - 100) * 14400
        r = client.get("/api/replay/levels", params={"symbol": "BTCUSDT", "interval": "4h", "time": t})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["time"] <= t and body["overlays"]
        assert all((o.get("time_start") or 0) <= t for o in body["overlays"])
