import os

os.environ["DATA_SOURCE"] = "synthetic"
os.environ["LLM_PROVIDER"] = "none"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def test_endpoints_offline():
    with TestClient(app) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        k = client.get("/api/klines", params={"symbol": "INJ/USDT", "interval": "3h", "limit": 200}).json()
        assert k["symbol"] == "INJUSDT" and len(k["candles"]) == 200 and k["source"] == "synthetic"

        r = client.post("/api/agent/analyze", json={
            "symbol": "INJUSDT", "interval": "1h",
            "prompt": "Identify the current H4 supply zone and key resistance high"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["analysis_interval"] == "4h"
        assert body["engine"]["intent"] == "rules"
        assert body["overlays"] and body["summary"]
        assert {o["type"] for o in body["overlays"]} <= {"box", "horizontal_line", "marker", "trendline"}

        assert client.get("/api/klines", params={"interval": "2d"}).status_code == 422

        with client.websocket_connect("/ws/klines?symbol=INJUSDT&interval=1m") as ws:
            msgs = [ws.receive_json() for _ in range(2)]
            assert msgs[0]["type"] == "status" and msgs[0]["source"] == "synthetic"
            assert msgs[1]["type"] == "kline" and msgs[1]["candle"]["close"] > 0
