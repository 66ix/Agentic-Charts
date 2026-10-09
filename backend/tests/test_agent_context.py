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
    assert "funding is 0.012%" in res.summary and "long/short ratio is 1.8" in res.summary
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

    async def narrate(prompt, facts, fallback, history=None, **kw):
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
    assert {"ema_fast", "ema_slow", "rsi", "macd", "bollinger", "stoch_rsi", "vwap", "psar", "atr", "cvd"} <= set(ind)
    assert ind["cvd"]["cvd_last_20_bars"] == "rising" and ind["cvd"]["buy_pct_last_20_bars"] == 60.0
    assert seen["market_overview"]["fear_greed"]["display"] == "31/100 · Fear"
    assert seen["market_overview"]["unavailable"] == ["BTC Dominance"]  # a mocked value is never quoted


def test_buyers_or_sellers_brings_the_order_flow_read():
    FakeFutures.calls = 0
    res = _run("which side has more money, buyers or sellers?", futures=FakeFutures())
    assert FakeFutures.calls == 1 and res.summary


def _toggles(prompt, indicators_on):
    async def parse(*a, **k):
        from app.schemas import AnalysisIntent
        return AnalysisIntent(indicators_on=indicators_on, keep_existing=True), "test"

    async def go():
        md, llm = MarketData(), LLMClient()
        llm.parse_intent = parse
        try:
            return await run_analysis(AnalyzeRequest(symbol="INJUSDT", interval="4h", prompt=prompt), md, llm)
        finally:
            await md.close()
            await llm.close()
    return asyncio.run(go()).indicators


def test_asking_about_an_indicator_reads_it_without_putting_it_on_the_chart():
    assert _toggles("what's the MACD doing?", ["macd"]) == {}
    assert _toggles("add MACD", ["macd"]) == {"macd": True}


def test_the_answer_carries_the_numbers_it_used():
    res = _run("what's the RSI?")
    assert "indicators" in res.facts and "last_price" in res.facts and "research" not in res.facts


def test_higher_timeframes_are_read_without_asking():
    res = _run("what's the RSI?")
    assert set(res.facts["higher_timeframes"]) == {"D1", "W1"}
    assert {"trend", "rsi", "price_vs_ema"} <= set(res.facts["higher_timeframes"]["D1"])


def test_scans_open_with_the_market_mood():
    from app.ta_agent import market_mood
    assert market_mood({"fear_greed": {"display": "31/100 · Fear", "change_pct": -5.0},
                        "unavailable": ["BTC Dominance"]}) == "Market mood: Fear & Greed 31/100 · Fear (-5% 24h)."
    assert market_mood({"unavailable": ["Fear & Greed"]}) is None
    res = _run("which of my coins are near demand?", metrics=FakeMetrics())
    assert "Market mood: Fear & Greed 31/100 · Fear" in res.summary


def test_vs_btc_correlation_and_strength():
    import numpy as np
    import pandas as pd

    from app.relative import vs_btc
    t = 1_700_006_400 + np.arange(40) * 86400
    btc = pd.DataFrame({"time": t, "close": 60000 * np.cumprod(1 + 0.01 * np.sin(np.arange(40)))})
    coin = pd.DataFrame({"time": t, "close": 100 * np.cumprod(1 + 0.02 * np.sin(np.arange(40)))})
    out = vs_btc(coin, btc)
    assert out["correlation_30d"] == 1.0 and out["beta_30d"] == 2.0 and out["follows_btc"] == "closely"
    assert vs_btc(coin.head(5), btc.head(5)) is None


def test_the_agent_knows_btc_your_note_the_last_answer_and_the_session_clock():
    import time as _t

    from app.schemas import PastAnswer
    seen = {}

    async def narrate(prompt, facts, fallback, history=None, **kw):
        seen.update(facts, detail=kw.get("detail"))
        return fallback, "test"

    async def go():
        md, llm = MarketData(), LLMClient()
        llm.narrate = narrate
        try:
            req = AnalyzeRequest(symbol="INJUSDT", interval="4h", prompt="is INJ just following BTC?", detail="short",
                                 coin_note="waiting for a retest of 20",
                                 previous_answer=PastAnswer(time=int((_t.time() - 3 * 86400) * 1000), prompt="long?",
                                                            summary="Long from 20, stop 19.", price=20.0))
            return await run_analysis(req, md, llm)
        finally:
            await md.close()
            await llm.close()
    res = asyncio.run(go())
    assert {"correlation_30d", "vs_btc_7d_pct"} <= set(seen["vs_btc"])
    assert seen["your_note_on_this_coin"] == "waiting for a retest of 20"
    assert seen["last_time_you_asked"]["when"] == "3 days ago" and "change_since_pct" in seen["last_time_you_asked"]
    assert set(seen["session_clock"]) == {"Asia", "London", "New York"}
    assert seen["detail"] == "short"
    assert "Your note on this coin: waiting for a retest of 20" in res.summary


def test_session_clock_counts_down_to_the_next_open():
    from datetime import datetime, timezone

    from app.session_levels import session_clock
    # Wednesday 2026-10-07 06:20 UTC: Asia is open, London (BST) opens at 07:00, New York (EDT) at 13:30.
    now = int(datetime(2026, 10, 7, 6, 20, tzinfo=timezone.utc).timestamp())
    rows = {r["key"]: r for r in session_clock(now)}
    assert rows["asia"]["open"] and rows["asia"]["minutes"] == 160
    assert not rows["london"]["open"] and rows["london"]["minutes"] == 40
    assert rows["ny"]["minutes"] == 430
    # Saturday: the next London session is Monday's.
    sat = int(datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc).timestamp())
    assert {r["key"]: r for r in session_clock(sat)}["london"]["minutes"] == (2 * 24 - 5) * 60


def test_unusual_volume_scan():
    from app.llm import rule_intent
    intent = rule_intent("which of my coins have unusual volume?")
    assert intent.scan_watchlist and intent.scan_filter == "volume"
    res = _run("which of my coins have unusual volume?")
    assert res.scan and all(hasattr(r, "volume_ratio") for r in res.scan)
    scores = [r.score for r in res.scan]
    assert scores == sorted(scores, reverse=True) and scores[0] >= max(r.volume_ratio or 0 for r in res.scan)
