"""Close-confirmed level alerts: the pure check and the API."""

import os

os.environ["DATA_SOURCE"] = "synthetic"
os.environ["LLM_PROVIDER"] = "none"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.signal_alerts import LevelClose, level_close_hit  # noqa: E402


def test_fires_on_the_crossing_close_only():
    lvl = LevelClose(price_low=10, price_high=10, side="above")
    assert level_close_hit(lvl, [9.5, 10.2], 1) is not None
    assert level_close_hit(lvl, [10.1, 10.2], 2) is None  # already above: not a new close beyond
    assert level_close_hit(lvl, [9.5, 9.9], 3) is None  # a wick above with a close below doesn't count


def test_needs_n_closes_in_a_row():
    lvl = LevelClose(price_low=10, price_high=10.5, side="below", closes=2, label="range low")
    assert level_close_hit(lvl, [10.2, 9.8], 1) is None  # too few closes to judge
    assert level_close_hit(lvl, [10.2, 9.8, 10.1], 2) is None
    hit = level_close_hit(lvl, [10.2, 9.8, 9.7], 3)
    assert hit is not None and "range low" in hit.text and "2 closes" in hit.text


def test_inside():
    lvl = LevelClose(price_low=10, price_high=11, side="inside")
    assert level_close_hit(lvl, [12, 10.5], 1) is not None
    assert level_close_hit(lvl, [10.4, 10.5], 1) is None


def test_api(monkeypatch):
    from app import config
    config.get_settings.cache_clear()
    with TestClient(app) as client:
        body = {"symbol": "btcusdt", "interval": "1d", "price_low": 50000, "price_high": 50000, "side": "below"}
        r = client.post("/api/level-alerts", json=body)
        assert r.status_code == 200, r.text
        a = r.json()["alert"]
        assert a["signal"] == "level_close" and a["symbol"] == "BTCUSDT" and a["level"]["side"] == "below"
        again = client.post("/api/level-alerts", json=body).json()["alert"]
        assert again["id"] == a["id"]  # same level replaces, doesn't duplicate
        assert client.post("/api/level-alerts", json={**body, "interval": "7m"}).status_code == 422
        assert client.delete(f"/api/signal-alerts/{a['id']}").status_code == 200
