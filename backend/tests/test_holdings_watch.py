"""Holdings watch: which coins it watches, and that it adds and removes only its own alerts."""

import os

os.environ["DATA_SOURCE"] = "synthetic"
os.environ["LLM_PROVIDER"] = "none"

from fastapi.testclient import TestClient  # noqa: E402

from app.holdings_watch import HoldingsWatch  # noqa: E402
from app.main import app  # noqa: E402


def positions(spot, tracked=(), wallet=None):
    return {"manual": {"spot": [{"symbol": s, "own_qty": 1.0, "value": v} for s, v in spot], "futures": [],
                       "cash": []},
            "bots": {"wallet": wallet, "spot": [], "futures": [],
                     "tracked": [{"symbol": s, "value": None} for s in tracked]}}


def test_coins_skip_dust_and_add_tracked_bots_only_when_asked():
    pos = positions([("INJUSDT", 400.0), ("DOGEUSDT", 2.0), ("SOLUSDT", None)], tracked=["ARBUSDT", "INJUSDT"])
    coins, _ = HoldingsWatch.coins(pos, 10, include_bots=True)
    assert [c["symbol"] for c in coins] == ["INJUSDT", "SOLUSDT", "ARBUSDT"]
    assert [c["source"] for c in coins] == ["spot", "spot", "grid bot"]
    coins, _ = HoldingsWatch.coins(pos, 10, include_bots=False)
    assert [c["symbol"] for c in coins] == ["INJUSDT", "SOLUSDT"]


def test_watch_syncs_its_own_alerts_and_leaves_the_users_alone(monkeypatch):
    with TestClient(app) as client:
        st = app.state
        held = {"pos": positions([("INJUSDT", 400.0), ("SOLUSDT", 50.0)], wallet={"wallet": "Trading Bots"})}

        async def fake_positions(refresh=False):
            return held["pos"]

        monkeypatch.setattr(st.binance, "positions", fake_positions)
        monkeypatch.setattr(st.binance.account, "status", lambda: {"configured": True})
        # The user's own alert on SOL is kept and not duplicated.
        mine = client.post("/api/signal-alerts", json={"symbols": ["SOLUSDT"], "interval": "4h",
                                                       "signal": "lost_support"}).json()["alerts"][0]

        r = client.put("/api/holdings-watch", json={"enabled": True, "interval": "4h", "include_bots": False})
        assert r.status_code == 200, r.text
        last = r.json()["last"]
        assert last["added"] == ["INJUSDT", "SOLUSDT"] and any("Trading Bots" in n for n in last["notes"])
        alerts = client.get("/api/signal-alerts").json()["alerts"]
        owned = sorted((a["symbol"], a["signal"]) for a in alerts if a.get("owner") == "holdings")
        assert owned == [("INJUSDT", "at_resistance"), ("INJUSDT", "lost_support"), ("SOLUSDT", "at_resistance")]

        held["pos"] = positions([("SOLUSDT", 50.0)])  # INJ sold
        last = client.post("/api/holdings-watch/run").json()["last"]
        assert last["removed"] == ["INJUSDT"]
        alerts = client.get("/api/signal-alerts").json()["alerts"]
        assert not any(a["symbol"] == "INJUSDT" for a in alerts)
        assert any(a["id"] == mine["id"] for a in alerts)

        client.put("/api/holdings-watch", json={"enabled": False})
        alerts = client.get("/api/signal-alerts").json()["alerts"]
        assert not any(a.get("owner") == "holdings" for a in alerts) and any(a["id"] == mine["id"] for a in alerts)
        client.delete(f"/api/signal-alerts/{mine['id']}")
