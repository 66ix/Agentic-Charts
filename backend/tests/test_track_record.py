"""Track records on trade plans: matching a plan to its backtest setup, honest summaries on thin data, caching, the
live Binance path (mocked) and the agent's plan answer."""

import asyncio
import os

os.environ["DATA_SOURCE"] = "synthetic"
os.environ["LLM_PROVIDER"] = "none"

import httpx  # noqa: E402
import pytest  # noqa: E402

from app import config, track_record  # noqa: E402
from app.agent import run_analysis  # noqa: E402
from app.backtest import BacktestResult, BacktestStats  # noqa: E402
from app.llm import LLMClient  # noqa: E402
from app.market_data import INTERVAL_SECONDS, MarketData, synthetic_klines  # noqa: E402
from app.schemas import AnalyzeRequest, PlanTarget, TradePlan  # noqa: E402
from app.track_record import (  # noqa: E402
    TrackRecordService,
    history_bars,
    match_setup,
    period_text,
    record_from_result,
    setup_label,
    ttl_for,
)

DAY = 86400


def _plan(direction="long", kind="demand", fresh=True, htf=()):
    return TradePlan(direction=direction, entry=10.0, stop=9.5, targets=[PlanTarget(price=11.0, label="T1 x", rr=2.0)],
                     basis="H4 Demand", risk_pct=5.0, zone_kind=kind, zone_fresh=fresh, zone_htf=list(htf))


def _result(count, wins, bars=2000, source="binance", avg_r=0.6, days=365):
    stats = BacktestStats(count=count, wins=wins, losses=count - wins,
                          win_rate=round(wins / count, 4) if count else None, avg_r=avg_r if count else None,
                          total_r=round(avg_r * count, 4), profit_factor=1.8 if count else None)
    return BacktestResult(symbol="INJUSDT", interval="4h", setup="demand_long", setup_name="Long from fresh demand",
                          bars=bars, from_time=0, to_time=days * DAY - 4 * 3600, data_source=source,
                          target="next_level", trades=[], stats=stats, equity=[])


def test_plan_basis_maps_to_the_backtest_setup():
    assert match_setup(_plan("long", "demand")) == ("demand_long", [])
    assert match_setup(_plan("short", "supply"))[0] == "supply_short"
    assert match_setup(_plan("long", "support", fresh=None))[0] == "support_long"
    assert match_setup(_plan("short", "resistance", fresh=None))[0] == "resistance_short"

    setup, notes = match_setup(_plan("long", "demand", fresh=False, htf=["D1"]))
    assert setup == "demand_long"
    assert "already tested" in notes[0] and "D1" in notes[1]

    for kind in ("ob_bullish", "custom_zone", "swing"):
        setup, notes = match_setup(_plan("long", kind))
        assert setup is None and notes[0].startswith("No backtest setup matches")
    assert setup_label("demand_long", "INJUSDT", "4h") == "fresh 4h demand longs on INJ"
    assert setup_label("resistance_short", "ETHBTC", "1h") == "1h resistance shorts on ETH"


def test_history_window_and_ttl():
    assert history_bars("4h") == 2190  # a year
    assert history_bars("1h") == 3000  # capped
    assert history_bars("1d") == 500   # at least MIN_HISTORY
    assert ttl_for("1m") == 1800 and ttl_for("4h") == 6 * 3600
    assert period_text(365 * DAY) == "last 1 year"
    assert period_text(120 * DAY) == "last 4 months"
    assert period_text(10 * DAY) == "last 10 days"
    assert period_text(3600 * 30) == "last 30 hours"


def test_summary_refuses_numbers_on_thin_data():
    label = "fresh 4h demand longs on INJ"
    ok = record_from_result(_result(25, 14), label)
    assert ok.status == "ok" and ok.trades == 25 and ok.win_rate == 0.56
    assert ok.summary == "fresh 4h demand longs on INJ: 25 trades, 56% win, +0.60R avg, last 1 year"

    small = record_from_result(_result(14, 8), label)
    assert small.status == "small_sample" and small.summary.endswith("(small sample)")
    assert "14 trades, 57% win" in small.summary

    few = record_from_result(_result(3, 2), label)
    assert few.status == "too_few_trades" and few.win_rate is None and few.avg_r is None
    assert few.summary == "fresh 4h demand longs on INJ: only 3 trades in the last 1 year, too few to judge"
    none = record_from_result(_result(0, 0), label)
    assert "only no trades" not in none.summary and "no trades" in none.summary

    short = record_from_result(_result(30, 15, bars=150, days=25), label)
    assert short.status == "short_history" and short.win_rate is None and "150 candles" in short.summary

    demo = record_from_result(_result(25, 14, source="synthetic"), label)
    assert demo.summary.endswith("(demo data)") and any("synthetic" in n for n in demo.notes)


def test_cached_per_key_and_shared_while_running(monkeypatch):
    calls = []

    async def fake_backtest(market, kimi, req):
        calls.append((req.symbol, req.interval, req.setup, req.bars, req.target))
        await asyncio.sleep(0.05)
        return _result(25, 14).model_copy(update={"symbol": req.symbol, "interval": req.interval})

    monkeypatch.setattr(track_record, "run_backtest", fake_backtest)
    svc = TrackRecordService(MarketData())

    async def go():
        a, b = await asyncio.gather(svc.get("INJUSDT", "4h", "demand_long"), svc.get("INJUSDT", "4h", "demand_long"))
        c = await svc.get("INJUSDT", "4h", "demand_long")
        d = await svc.for_plan("INJUSDT", "1h", _plan(htf=["H4"]))
        return a, b, c, d

    a, b, c, d = asyncio.run(go())
    assert calls == [("INJUSDT", "4h", "demand_long", 2190, "next_level"),
                     ("INJUSDT", "1h", "demand_long", 3000, "next_level")]
    assert a.summary == b.summary == c.summary and a is not c  # callers get copies
    assert d.interval == "1h" and "H4" in d.notes[0]
    c.notes.append("mutated")
    assert "mutated" not in asyncio.run(svc.get("INJUSDT", "4h", "demand_long")).notes


def test_slow_run_times_out_then_lands_in_the_cache(monkeypatch):
    async def slow_backtest(market, kimi, req):
        await asyncio.sleep(0.3)
        return _result(25, 14)

    monkeypatch.setattr(track_record, "run_backtest", slow_backtest)
    svc = TrackRecordService(MarketData())

    async def go():
        first = await svc.for_plan("INJUSDT", "4h", _plan(), timeout=0.05)
        await asyncio.sleep(0.4)
        return first, await svc.for_plan("INJUSDT", "4h", _plan(), timeout=0.05)

    first, second = asyncio.run(go())
    assert first.status == "unavailable" and "still being backtested" in first.summary
    assert second.status == "ok"


def test_failures_and_new_listings(monkeypatch):
    async def boom(market, kimi, req):
        if req.symbol == "NEWUSDT":
            raise ValueError("Need at least 100 closed candles, got 40")
        raise httpx.ConnectError("down")

    monkeypatch.setattr(track_record, "run_backtest", boom)
    svc = TrackRecordService(MarketData())
    new = asyncio.run(svc.get("NEWUSDT", "4h", "demand_long"))
    down = asyncio.run(svc.get("INJUSDT", "4h", "demand_long"))
    assert new.status == "short_history" and "too little history" in new.summary
    assert down.status == "unavailable"
    no_match = asyncio.run(svc.for_plan("INJUSDT", "4h", _plan(kind="swing")))
    assert no_match.status == "no_match" and no_match.setup is None


# ------------------------------------------------------------------ live (mocked) --


class FakeBinance:
    """/api/v3/klines from one fixed synthetic series per (symbol, interval), paged by endTime like Binance."""

    def __init__(self):
        self.series = {}
        self.requests = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests += 1
        q = req.url.params
        if not req.url.path.endswith("/api/v3/klines"):
            return httpx.Response(404)
        key = (q["symbol"], q["interval"])
        if key not in self.series:
            self.series[key] = synthetic_klines(q["symbol"], q["interval"], 3500)
        step = INTERVAL_SECONDS[q["interval"]]
        end = int(q.get("endTime", 10**15)) // 1000
        rows = [[c.time * 1000, str(c.open), str(c.high), str(c.low), str(c.close), str(c.volume),
                 (c.time + step) * 1000 - 1, "0", 1, "0", "0", "0"] for c in self.series[key] if c.time <= end]
        return httpx.Response(200, json=rows[-int(q["limit"]):])


@pytest.fixture
def binance_mode(monkeypatch):
    monkeypatch.setenv("DATA_SOURCE", "binance")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def test_live_track_record_uses_binance_history(binance_mode):
    fake = FakeBinance()
    md = MarketData()
    md._client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    svc = TrackRecordService(md)

    async def go():
        try:
            return await svc.for_plan("INJUSDT", "4h", _plan())
        finally:
            await md.close()

    rec = asyncio.run(go())
    assert rec.data_source == "binance" and "demo" not in rec.summary
    assert rec.bars == 2190 and fake.requests == 3  # 2,191 candles in pages of 1,000
    assert rec.status in ("ok", "small_sample", "too_few_trades")
    assert rec.summary.startswith("fresh 4h demand longs on INJ: ")


# ----------------------------------------------------------------------- agent --


def test_agent_plan_carries_its_track_record():
    req = AnalyzeRequest(symbol="INJUSDT", interval="4h", prompt="give me a long setup")
    md, llm = MarketData(), LLMClient()

    async def go():
        try:
            return await run_analysis(req, md, llm)
        finally:
            await md.close()
            await llm.close()

    res = asyncio.run(go())
    assert res.plan is not None
    tr = res.plan.track_record
    assert tr is not None and tr.symbol == "INJUSDT" and tr.interval == "4h"
    if res.plan.zone_kind in ("demand", "support"):
        assert tr.setup in ("demand_long", "support_long") and tr.data_source == "synthetic"
        assert tr.summary.endswith("(demo data)") or tr.status == "short_history"
    else:
        assert tr.status == "no_match"
    assert f"Track record: {tr.summary}." in res.summary  # the template narrator says it too
