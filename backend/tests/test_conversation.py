"""Follow-up requests: adding, removing, user-given levels and alerts (rule parser, no LLM)."""

import os

os.environ["DATA_SOURCE"] = "synthetic"
os.environ["LLM_PROVIDER"] = "none"

from fastapi.testclient import TestClient  # noqa: E402

from app.llm import rule_intent  # noqa: E402
from app.main import app  # noqa: E402
from app.schemas import AnalysisIntent  # noqa: E402


def test_rule_parser_actions():
    prev = AnalysisIntent(features=["supply_demand"], timeframe="4h")
    i = rule_intent("remove the trendline", prev)
    assert i.remove == ["trendlines"] and i.features == [] and i.keep_existing

    i = rule_intent("set entry at 24.2, stop at 23.8 and target at 27")
    assert [(c.label, c.price) for c in i.custom_levels] == [("Entry", 24.2), ("Stop", 23.8), ("Target", 27.0)]
    assert i.features == []

    i = rule_intent("box 24 to 25.5 and alert me if price enters it")
    assert i.custom_levels[0].price_low == 24 and i.custom_levels[0].price_high == 25.5
    assert i.alert_targets == ["new"]

    assert rule_intent("alert me at 65k").alert_prices == [65000.0]
    assert rule_intent("show 4h support and resistance").alert_prices == []

    i = rule_intent("same on daily", prev)
    assert i.features == ["supply_demand"] and i.timeframe == "1d" and not i.keep_existing

    i = rule_intent("also show swings", prev)
    assert i.features == ["swings"] and i.keep_existing


def _ask(client, prompt, overlays=(), history=(), previous=None):
    r = client.post("/api/agent/analyze", json={
        "symbol": "INJUSDT", "interval": "4h", "prompt": prompt, "overlays": list(overlays),
        "history": list(history), "previous_intent": previous})
    assert r.status_code == 200, r.text
    return r.json()


def test_conversation_flow():
    with TestClient(app) as client:
        first = _ask(client, "Full analysis: zones, windows, trendlines")
        kinds = {o["kind"] for o in first["overlays"]}
        assert "trendline" in kinds or "window_high" in kinds
        ids = [o["id"] for o in first["overlays"]]
        assert len(ids) == len(set(ids))

        history = [{"role": "user", "text": "Full analysis"}, {"role": "agent", "text": first["summary"]}]
        second = _ask(client, "remove the window levels and draw a line at 7.5", first["overlays"], history,
                      first["intent"])
        kinds2 = [o["kind"] for o in second["overlays"]]
        assert "window_high" not in kinds2 and "custom_level" in kinds2
        kept = {o["id"] for o in second["overlays"]} & set(ids)
        assert kept and all(o["kind"] not in ("window_high", "window_low")
                            for o in first["overlays"] if o["id"] in kept)
        assert "Removed" in second["summary"] and "7.5" in second["summary"]

        third = _ask(client, "alert me if price hits my level", second["overlays"], history, second["intent"])
        assert third["alerts"] == [{"kind": "cross", "price": 7.5, "price_low": None, "price_high": None,
                                    "label": "Level 7.5"}]
        assert len(third["overlays"]) == len(second["overlays"])

        fresh = _ask(client, "show 4h support and resistance", third["overlays"])
        assert any(o["kind"] == "custom_level" for o in fresh["overlays"])  # user levels survive a new analysis
        assert not any(o["kind"] == "trendline" for o in fresh["overlays"])

        # Questions that draw nothing new keep what's on the chart.
        asked = _ask(client, "what does kimi say?", fresh["overlays"])
        assert {o["id"] for o in asked["overlays"]} == {o["id"] for o in fresh["overlays"]}

        cleared = _ask(client, "clear the chart", fresh["overlays"])
        assert cleared["overlays"] == [] and "Removed" in cleared["summary"]


def test_intent_schema_is_strict_compatible():
    """OpenAI strict mode rejects a schema unless every property is required, at every level."""
    from app.llm import INTENT_SCHEMA

    def check(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                assert node.get("additionalProperties") is False
                assert sorted(node["required"]) == sorted(node["properties"]), node["properties"].keys()
            for v in node.values():
                check(v)
        elif isinstance(node, list):
            for v in node:
                check(v)

    check(INTENT_SCHEMA)


def test_ratio_chart_uses_the_client_candles():
    """A ratio chart (ETH priced in BTC) has no Binance pair: the agent analyses the candles the browser sent."""
    import math

    candles = [{"time": 1_700_000_000 + i * 14400, "open": 0.05 + 0.002 * math.sin(i / 5),
                "high": 0.0505 + 0.002 * math.sin(i / 5), "low": 0.0495 + 0.002 * math.sin(i / 5),
                "close": 0.05 + 0.002 * math.sin((i + 1) / 5), "volume": 1.0} for i in range(300)]
    with TestClient(app) as client:
        r = client.post("/api/agent/analyze", json={"symbol": "ETHUSDT/BTCUSDT", "interval": "4h",
                                                     "prompt": "key levels", "candles": candles})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["symbol"] == "ETHUSDT/BTCUSDT" and body["navigate"] is None
        assert body["overlays"] and all(0.04 < (o.get("price") or o.get("price_low")) < 0.06
                                        for o in body["overlays"] if o["type"] in ("box", "horizontal_line"))

        r = client.post("/api/agent/analyze", json={"symbol": "INDEX:TOTAL2", "interval": "1d", "prompt": "levels"})
        assert r.status_code == 422 and "loading" in r.json()["detail"]
