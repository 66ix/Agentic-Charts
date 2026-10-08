"""The agent reads futures data and the economic calendar when they help: plans warn about high-impact events,
"funding?" brings the futures read, and an unreachable calendar never turns into "nothing coming up"."""

import asyncio
import os
import time

os.environ["DATA_SOURCE"] = "synthetic"
os.environ["LLM_PROVIDER"] = "none"

from app.agent import run_analysis  # noqa: E402
from app.llm import LLMClient  # noqa: E402
from app.market_data import MarketData  # noqa: E402
from app.schemas import AnalyzeRequest  # noqa: E402


class FakeEvents:
    def __init__(self, events, source="live"):
        self.events, self.source = events, source

    async def calendar(self, days=7, impact="high", past_days=0):
        return {"events": [] if self.source == "unavailable" else self.events, "source": self.source}

    async def headlines(self, symbol=None, hours=24, limit=8):
        return [{"time": time.time() - 3600, "title": "INJ unlocks next week", "source": "CoinDesk", "url": "https://x"}]


class FakeFutures:
    calls = 0

    async def futures_context(self, symbol):
        FakeFutures.calls += 1
        return {"symbol": symbol, "funding": {"rate_pct": 0.012, "avg_24h_pct": 0.01, "annualized_pct": 13.1,
                                              "interval_hours": 8, "next_in_minutes": 90},
                "long_short": {"ratio": 1.8, "long_pct": 64.3, "change_24h": 0.2, "top_traders_ratio": 1.2},
                "sources": {"funding": "binance"}}

    async def cvd(self, symbol, interval="1h", limit=500):
        rows = [{"time": i, "buy": 60.0, "sell": 40.0, "delta": 20.0, "cvd": 20.0 * (i + 1)} for i in range(30)]
        return {"symbol": symbol, "interval": interval, "rows": rows, "source": "binance"}


def _run(prompt, events=None, futures=None, metrics=None):
    async def go():
        md, llm = MarketData(), LLMClient()
        try:
            req = AnalyzeRequest(symbol="INJUSDT", interval="4h", prompt=prompt)
            return await run_analysis(req, md, llm, None, None, futures, events, metrics=metrics)
        finally:
            await md.close()
            await llm.close()
    return asyncio.run(go())


def test_plan_warns_about_an_upcoming_high_impact_event():
    cpi = {"time": time.time() + 5 * 3600, "title": "CPI m/m", "country": "USD", "impact": "high",
           "forecast": "0.3%", "previous": "0.2%"}
    res = _run("give me a long setup", events=FakeEvents([cpi]))
    assert res.plan is not None
    assert any("CPI m/m" in n and "in 5h" in n for n in res.plan.notes)
    assert "CPI m/m" in res.summary  # the template answer passes the warning on


def test_unreachable_calendar_is_not_reported_as_quiet():
    res = _run("any high impact events coming up?", events=FakeEvents([], source="unavailable"))
    assert "No high-impact" not in res.summary
    res = _run("any high impact events coming up?", events=FakeEvents([]))
    assert "No high-impact economic events" in res.summary


def test_funding_question_reads_the_futures_context_and_news_question_the_headlines():
    FakeFutures.calls = 0
    res = _run("what is funding and long/short like?", futures=FakeFutures())
    assert FakeFutures.calls == 1
    assert "funding 0.012%" in res.summary and "long/short 1.8" in res.summary
    FakeFutures.calls = 0
    res = _run("show 4h support and resistance", futures=FakeFutures(), events=FakeEvents([]))
    assert FakeFutures.calls == 0  # not asked, not fetched
    res = _run("any news on INJ?", events=FakeEvents([]))
    assert "INJ unlocks next week" in res.summary


class FakeMetrics:
    async def get(self):
        from datetime import datetime, timezone

        from app.schemas import MarketMetrics, Metric
        return MarketMetrics(updated_at=datetime.now(timezone.utc), metrics=[
            Metric(key="fear_greed", label="Fear & Greed", value=31, display="31/100 · Fear", source="live"),
            Metric(key="btc_dominance", label="BTC Dominance", value=57.3, display="57.30%", source="mock")])


def test_the_agent_reads_every_indicator_and_the_header_bar_without_them_on_the_chart():
    seen = {}

    async def narrate(prompt, facts, fallback, history=None):
        seen.update(facts)
        return fallback, "test"

    async def go():
        md, llm = MarketData(), LLMClient()
        llm.narrate = narrate
        try:
            req = AnalyzeRequest(symbol="INJUSDT", interval="4h", prompt="what's the MACD and fear and greed?")
            return await run_analysis(req, md, llm, None, None, FakeFutures(), None, metrics=FakeMetrics())
        finally:
            await md.close()
            await llm.close()
    asyncio.run(go())
    ind = seen["indicators"]
    assert {"ema20", "ema50", "macd", "bollinger", "stoch_rsi", "vwap", "psar", "atr", "cvd"} <= set(ind)
    assert ind["cvd"]["cvd_last_20_bars"] == "rising" and ind["cvd"]["buy_pct_last_20_bars"] == 60.0
    assert seen["market_overview"]["fear_greed"]["display"] == "31/100 · Fear"
    assert seen["market_overview"]["unavailable"] == ["BTC Dominance"]  # a mocked value is never quoted
