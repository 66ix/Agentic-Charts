"""Alerts on the market header (metric_alerts.py): parsing, firing once, the endpoints and the agent action."""

import asyncio
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.llm import rule_intent
from app.metric_alerts import MetricAlert, MetricAlertService, describe, holds
from app.schemas import MarketMetrics, Metric, MetricAlertSpec


class FakeAlerts:
    def __init__(self):
        self.recorded, self.sent, self.pushed, self.providers = [], [], [], []

    def record(self, kind, symbol, title, text, **kw):
        self.recorded.append((kind, symbol, title, text))

    def notify(self, text):
        self.sent.append(text)

    def broadcast(self, msg):
        self.pushed.append(msg)

    def add_snapshot_provider(self, p):
        self.providers.append(p)


class FakeMetrics:
    def __init__(self, fg=31.0, dom=57.3, dom_source="live"):
        self.fg, self.dom, self.dom_source = fg, dom, dom_source

    async def get(self):
        return MarketMetrics(updated_at=datetime.now(timezone.utc), metrics=[
            Metric(key="fear_greed", label="Fear & Greed", value=self.fg, display="", source="live"),
            Metric(key="btc_dominance", label="BTC Dominance", value=self.dom, display="", source=self.dom_source),
            Metric(key="market_cap", label="Market Cap", value=2.4e12, display="", source="mock")])


def _spec(**kw):
    return MetricAlertSpec(**kw)


def test_the_rule_parser_reads_header_alerts_and_btc_dominance_is_no_symbol():
    i = rule_intent("alert me when fear & greed drops below 25", known_bases={"BTC"}, chart_symbol="ETHUSDT")
    assert [(a.metric, a.condition, a.value) for a in i.metric_alerts] == [("fear_greed", "below", 25)]
    assert not i.alert_prices and not i.features and i.keep_existing
    i = rule_intent("tell me if btc dominance moves 1%", known_bases={"BTC"}, chart_symbol="ETHUSDT")
    assert [(a.metric, a.condition, a.value) for a in i.metric_alerts] == [("btc_dominance", "moves", 1)]
    assert i.symbol is None
    i = rule_intent("ping me when total market cap is above $3T", known_bases={"BTC"})
    assert [(a.metric, a.value) for a in i.metric_alerts] == [("market_cap", 3e12)]
    assert rule_intent("alert me at 65000").metric_alerts == []


def test_conditions_and_descriptions():
    below = MetricAlert(**_spec(metric="fear_greed", condition="below", value=25).model_dump(), id="a", created_at=0)
    assert holds(below, 24) and not holds(below, 25)
    moves = MetricAlert(**_spec(metric="btc_dominance", condition="moves", value=1).model_dump(), id="b",
                        created_at=0, base_value=57.3)
    assert holds(moves, 56.2) and holds(moves, 58.4) and not holds(moves, 57.9)
    cap = MetricAlert(**_spec(metric="market_cap", condition="moves", value=5).model_dump(), id="c", created_at=0,
                      base_value=2e12)
    assert holds(cap, 2.11e12) and not holds(cap, 2.05e12)
    assert describe(below) == "Fear & Greed below 25"
    assert describe(moves) == "BTC dominance moves 1 point"
    assert describe(cap) == "Market cap moves 5%"


def test_an_alert_fires_once_on_live_values_only():
    async def go():
        alerts, metrics = FakeAlerts(), FakeMetrics(fg=31)
        svc = MetricAlertService(metrics, alerts)
        await svc.add([_spec(metric="fear_greed", condition="below", value=25),
                       _spec(metric="market_cap", condition="above", value=1e12)])  # mocked: never fires
        assert await svc.check() == []
        metrics.fg = 22
        fired = await svc.check()
        assert [a.metric for a in fired] == ["fear_greed"] and fired[0].triggered_value == 22
        assert alerts.recorded[0][:2] == ("signal", "MARKET") and "is now 22" in alerts.sent[0]
        assert await svc.check() == []  # disarmed
        assert svc.snapshot()["type"] == "metric_snapshot"
    asyncio.run(go())


def test_a_move_needs_a_live_starting_value():
    async def go():
        svc = MetricAlertService(FakeMetrics(dom_source="mock"), FakeAlerts())
        try:
            await svc.add([_spec(metric="btc_dominance", condition="moves", value=1)])
        except ValueError as exc:
            assert "no live value" in str(exc)
        else:
            raise AssertionError("expected a ValueError")
        svc = MetricAlertService(FakeMetrics(dom=57.3), FakeAlerts())
        (a,) = await svc.add([_spec(metric="btc_dominance", condition="moves", value=1)])
        assert a.base_value == 57.3
        assert await svc.check({"btc_dominance": 58.4}) == [a]
    asyncio.run(go())


def test_the_endpoints_and_the_agent_set_list_and_delete():
    from app.main import app
    with TestClient(app) as client:
        r = client.post("/api/metric-alerts", json={"alerts": [{"metric": "fear_greed", "condition": "above",
                                                                 "value": 80}]})
        assert r.status_code == 200, r.text
        res = client.post("/api/agent/analyze", json={
            "prompt": "alert me when fear and greed drops below 20", "symbol": "ETHUSDT", "interval": "1h",
            "overlays": []})
        assert res.status_code == 200, res.text
        assert "Set a market alert: Fear & Greed below 20." in res.json()["summary"]
        listed = client.get("/api/metric-alerts").json()["alerts"]
        assert sorted((a["condition"], a["value"]) for a in listed) == [("above", 80), ("below", 20)]
        for a in listed:
            assert client.delete(f"/api/metric-alerts/{a['id']}").status_code == 200
        assert client.get("/api/metric-alerts").json()["alerts"] == []
        assert client.delete("/api/metric-alerts/nope").status_code == 404
