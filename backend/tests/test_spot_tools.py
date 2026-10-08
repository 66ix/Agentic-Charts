"""Spot tools: spot mode, spot buys and grid coins in the market scan, the top-down walk, the dip-buy ladder, take
profit levels, and plain-language questions (with the LLM and the feeds mocked)."""

import asyncio
import json
import os
import time

os.environ["DATA_SOURCE"] = "synthetic"
os.environ["LLM_PROVIDER"] = "none"

import httpx  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import general  # noqa: E402
from app.agent import run_analysis, take_profits  # noqa: E402
from app.config import Settings  # noqa: E402
from app.dip_ladder import LadderRequest, backtest_ladder, build_ladder  # noqa: E402
from app.llm import LLMClient, rule_intent  # noqa: E402
from app.main import app  # noqa: E402
from app.market_data import MarketData  # noqa: E402
from app.market_scanner import grid_candidate, spot_score  # noqa: E402
from app.schemas import AnalyzeRequest, MarketSetup, SetupAgreement, TradePlan  # noqa: E402
from app.sell_check import check as sell_check  # noqa: E402
from app.top_down import build_step, ladder  # noqa: E402
from app.trade_plan import Level  # noqa: E402

H4 = 4 * 3600


def _df(closes, start=1_700_000_000, step=H4, wick=0.004):
    c = np.asarray(closes, dtype=float)
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"time": start + np.arange(len(c)) * step, "open": o,
                         "high": np.maximum(o, c) * (1 + wick), "low": np.minimum(o, c) * (1 - wick), "close": c,
                         "volume": np.full(len(c), 1000.0)})


def _wave(n=400, mid=100.0, amp=0.08, period=24, drift=0.0):
    t = np.arange(n)
    return mid * (1 + amp * np.sin(2 * np.pi * t / period)) * (1 + drift) ** t


def _run(prompt, llm=None, events=None, **kw):
    async def go():
        md, lm = MarketData(), llm or LLMClient()
        try:
            req = AnalyzeRequest(symbol="INJUSDT", interval="4h", prompt=prompt, **kw)
            return await run_analysis(req, md, lm, events=events)
        finally:
            await md.close()
            await lm.close()
    return asyncio.run(go())


# ------------------------------------------------------------- the parser


def test_rule_parser_understands_the_new_shortcuts():
    cases = {
        "Top-down S/R walk": {"top_down": True},
        "Best spot buys": {"scan_market": True, "scan_kind": "spot_buys"},
        "Best grid bot coins": {"scan_market": True, "scan_kind": "grid_coins", "grid_plan": False},
        "Suggest a grid bot for this coin": {"grid_plan": True, "scan_market": False},
        "Where do I take profit?": {"take_profit": True},
        "Plan a dip-buy ladder": {"dip_ladder": True},
        "what day is today?": {"general_question": True, "features": []},
        "What were the results of the latest FOMC meeting?": {"general_question": True},
        "what coins are expecting a big upgrade or just had an upgrade?": {"general_question": True},
        "which coins are expecting a big upgrade or just had one?": {"general_question": True, "scan_watchlist": False},
        "Which of my coins are near demand?": {"general_question": False, "scan_watchlist": True},
        "key levels": {"general_question": False},
        "what is funding like?": {"general_question": False},
        "any news on INJ?": {"general_question": False},
        "What should I sell or trim?": {"sell_check": True, "trade_plan": None, "scan_watchlist": False},
        "which of my coins should I sell?": {"sell_check": True, "scan_watchlist": False},
        "should I sell INJ?": {"sell_check": True, "trade_plan": None},
        "give me a short setup": {"sell_check": False, "trade_plan": "short"},
    }
    for prompt, want in cases.items():
        got = rule_intent(prompt, chart_symbol="INJUSDT").model_dump()
        for k, v in want.items():
            assert got[k] == v, (prompt, k, got[k])
    tp = rule_intent("where should I sell?")
    assert tp.take_profit and {"support_resistance", "supply_demand"} <= set(tp.features)


# ------------------------------------------------------------- spot mode


def test_spot_mode_turns_a_short_plan_into_a_note_and_auto_into_a_long():
    res = _run("give me a short setup")
    assert res.plan is None and "Spot mode is on" in res.summary
    res = _run("give me a short setup", spot_only=False)
    assert res.intent.trade_plan == "short" and "Spot mode" not in res.summary
    res = _run("what's the trade here?")
    assert res.intent.trade_plan == "long"


def _setup(direction="long", zone_kind="demand", htf=("D1",), aligned=1.0, total=3, score=0.5):
    plan = TradePlan(direction=direction, entry=10, stop=9, targets=[], risk_pct=10, zone_kind=zone_kind,
                     zone_htf=list(htf))
    return MarketSetup(symbol="XUSDT", interval="4h", direction=direction, last_price=10.5, entry=10, stop=9,
                       target=12, rr=2, risk_pct=10, distance_pct=4.8, distance_atr=1,
                       agreement=SetupAgreement(aligned=aligned, total=total), score=score, plan=plan)


def test_spot_buys_need_demand_with_higher_timeframe_backing_or_an_uptrend():
    assert spot_score(_setup()) == 0.6  # HTF confluence bonus
    assert spot_score(_setup(htf=(), aligned=2, total=3)) == 0.5  # no confluence but the trend is up
    assert spot_score(_setup(htf=(), aligned=0.5, total=3)) is None  # neither
    assert spot_score(_setup(direction="short", zone_kind="supply")) is None
    assert spot_score(_setup(zone_kind="swing")) is None


def test_grid_candidate_wants_a_choppy_range_not_a_trend():
    coin = grid_candidate("XUSDT", "4h", _df(_wave(period=16)), "synthetic")
    assert coin is not None and coin.crossings >= 4 and 6 <= coin.width_pct <= 60 and coin.low < coin.high
    assert "crossing the middle" in coin.note
    assert grid_candidate("XUSDT", "4h", _df(100 * 1.004 ** np.arange(400)), "synthetic") is None
    assert grid_candidate("XUSDT", "4h", _df(_wave(amp=0.005)), "synthetic") is None  # too narrow to pay fees


# ------------------------------------------------------------- top-down walk


def test_ladder_runs_daily_down_to_15m():
    assert ladder() == ["1d", "4h", "1h", "15m"]
    assert ladder("4h", "1h") == ["4h", "1h"]
    assert ladder("15m", "1d") == ["1d", "4h", "1h", "15m"]


def test_walk_step_skips_a_timeframe_with_nothing_valid_and_says_why():
    flat = _df(np.linspace(100, 110, 200), wick=0.0001)  # a straight line: no swings, no bases, no impulses
    step = build_step("4h", flat, [], "synthetic")
    assert step.status == "skipped" and step.overlays == [] and step.reason


def test_walk_step_draws_valid_levels_and_does_not_redraw_higher_ones():
    df = _df(_wave(n=300, period=30))
    first = build_step("1d", df, [], "synthetic")
    assert first.status == "drawn" and first.zones and len(first.overlays) == len(first.zones)
    assert all(o.label.startswith("D1 ") for o in first.overlays)
    again = build_step("4h", df, [("D1", z) for z in first.zones], "synthetic")
    # The same candles find the same levels: nothing new, so the step is skipped and the D1 ones are confirmed.
    assert again.status == "skipped" and "repeats" in again.reason
    assert any("H4" in z.confirmed_by for z in first.zones)


def test_agent_top_down_keeps_the_chart_and_returns_the_steps():
    res = _run("Top-down S/R walk", overlays=[])
    assert res.intent.top_down and res.top_down is not None and res.navigate is None
    assert [s["interval"] for s in res.top_down["steps"]] == ["1d", "4h", "1h", "15m"]
    assert "Top-down walk on INJUSDT" in res.summary


# ------------------------------------------------------------- dip ladder


def test_ladder_buys_below_price_deeper_rungs_bigger_and_sells_above():
    df = _df(_wave(n=300, period=40, amp=0.1))
    plan = build_ladder(df, LadderRequest(symbol="XUSDT", budget=1000, rungs=3), "4h")
    prices = [r.price for r in plan.rungs]
    assert len(prices) == 3 and prices == sorted(prices, reverse=True) and prices[0] < plan.last_price
    assert [r.weight_pct for r in plan.rungs] == sorted(r.weight_pct for r in plan.rungs)
    assert abs(sum(r.amount for r in plan.rungs) - 1000) < 0.1
    assert plan.take_profit > plan.last_price and plan.invalidation < min(prices)
    assert plan.avg_price < plan.last_price


def test_ladder_backtest_trades_a_range_and_never_sees_the_future():
    df = _df(_wave(n=300 + 540, period=40, amp=0.1))
    req = LadderRequest(symbol="XUSDT", budget=1000, rungs=3, days=90)
    bt = backtest_ladder(df, req, "4h")
    assert bt.cycles >= 1 and bt.fills >= bt.cycles and bt.days == 90
    assert bt.curve and bt.curve[-1][1] == bt.final_value
    # Rewriting the last 20 candles can't change anything the test did before them: it never looked ahead.
    future = df.copy()
    future.loc[future.index[-20:], ["open", "high", "low", "close"]] *= 0.5
    other = backtest_ladder(future, req, "4h")
    cutoff = int(df["time"].iloc[-20])
    assert [v for t, v in bt.curve if t < cutoff] == [v for t, v in other.curve if t < cutoff]


def test_agent_dip_ladder_draws_buy_lines_and_returns_the_test():
    res = _run("Plan a dip-buy ladder")
    assert res.ladder is not None and res.ladder["plan"]["rungs"]
    kinds = {o.kind for o in res.overlays}
    assert {"plan_entry", "plan_target"} <= kinds
    assert "Dip-buy ladder on INJUSDT" in res.summary


# ------------------------------------------------------------- take profit


def test_take_profits_are_the_zones_above_price_nearest_first():
    levels = [Level("resistance", 110, 112, "H4 Resistance"), Level("supply", 111, 113, "H4 Supply"),
              Level("support", 90, 92, "H4 Support"), Level("resistance", 125, 127, "H4 Resistance")]
    rows = take_profits(levels, 100.0, {}, "4h")
    assert [r["low"] for r in rows] == [110, 125]
    assert rows[0]["gain_pct"] == 10.0 and "Supply" in rows[0]["label"]


def test_agent_take_profit_draws_targets():
    res = _run("Where do I take profit?")
    assert res.intent.take_profit and "take profit" in res.summary.lower()


# ------------------------------------------------------------- general questions


class FakeEvents:
    def __init__(self, items=(), cal=()):
        self.items, self.cal = list(items), list(cal)

    async def calendar(self, days=7, impact="high", past_days=0):
        return {"events": self.cal, "source": "live"}

    async def news(self, symbol=None, limit=30):
        return {"items": self.items, "source": "live"}

    async def headlines(self, symbol=None, hours=24, limit=8):
        return []

    async def upcoming_events(self, hours=24, impact="high"):
        return []


def test_date_question_is_answered_without_touching_the_chart():
    overlay = {"type": "horizontal_line", "price": 25.0, "label": "mine", "color": "#fff", "kind": "custom_level"}
    res = _run("what day is today?", overlays=[overlay])
    today = general.now_facts()
    assert res.intent.general_question and today["weekday"] in res.summary and str(today["date"][:4]) in res.summary
    assert len(res.overlays) == 1 and res.navigate is None


def test_general_question_without_a_model_lists_matching_headlines():
    now = time.time()
    items = [{"time": now - 3600, "title": "Fed holds rates steady, Powell signals patience", "url": "https://x/1",
              "source": "CoinDesk", "coins": [], "summary": ""},
             {"time": now - 7200, "title": "Solana memecoin rally", "url": "https://x/2", "source": "Decrypt",
              "coins": ["SOL"], "summary": ""}]
    res = _run("What were the results of the latest FOMC meeting?", events=FakeEvents(items))
    assert "Fed holds rates steady" in res.summary and "Solana" not in res.summary
    assert res.sources and res.sources[0]["url"] == "https://x/1"


def test_general_question_uses_anthropic_web_search_and_returns_sources():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append(body)
        if any(t.get("name") == "web_search" for t in body.get("tools", [])):
            return httpx.Response(200, json={"stop_reason": "end_turn", "content": [
                {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {"query": "FOMC"}},
                {"type": "web_search_tool_result", "tool_use_id": "s1", "content": [
                    {"type": "web_search_result", "url": "https://fed.example/fomc", "title": "FOMC statement"}]},
                {"type": "text", "text": "The Fed cut rates by 25 bp at its last meeting."}]})
        # The planner: answer with a plan marking a general question.
        return httpx.Response(200, json={"content": [{"type": "tool_use", "id": "t1", "name": "draw_on_chart", "input": {
            "features": [], "keep_existing": True, "general_question": True}}]})

    s = Settings(llm_provider="anthropic", anthropic_api_key="k", anthropic_model="claude-sonnet-5-5")
    llm = LLMClient(s)
    llm._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    res = _run("What did the FOMC decide?", llm=llm, events=FakeEvents())
    assert res.summary == "The Fed cut rates by 25 bp at its last meeting."
    assert res.sources == [{"title": "FOMC statement", "url": "https://fed.example/fomc"}]
    web = next(b for b in seen if any(t.get("name") == "web_search" for t in b.get("tools", [])))
    assert web["tools"][0]["type"] == "web_search_20260209"
    assert "TODAY" not in web["system"] and general.now_facts()["date"] in web["messages"][0]["content"]
    assert "spot only" in web["system"]
    assert "anthropic" in res.engine["summary"] and "web search" in res.engine["summary"]


# ------------------------------------------------------------- endpoints


def test_top_down_and_dip_ladder_endpoints():
    with TestClient(app) as client:
        r = client.post("/api/top-down", json={"symbol": "INJUSDT"})
        assert r.status_code == 200 and [s["interval"] for s in r.json()["steps"]] == ["1d", "4h", "1h", "15m"]
        r = client.post("/api/dip-ladder", json={"symbol": "INJUSDT", "budget": 500, "rungs": 2})
        assert r.status_code == 200
        body = r.json()
        assert len(body["plan"]["rungs"]) == 2 and body["backtest"]["days"] > 0 and body["overlays"]
        assert client.post("/api/dip-ladder", json={"symbol": "INJUSDT", "rungs": 0}).status_code == 422


def test_market_scan_ranks_spot_buys_and_grid_coins_and_the_agent_answers_from_them():
    from app.market_scanner import MarketScanner
    from tests.test_market_scanner import StubTrack, _settings

    scanner = MarketScanner(MarketData(), StubTrack(), settings=_settings(market_scan_top=20))

    async def go(prompt):
        llm = LLMClient()
        try:
            return await run_analysis(AnalyzeRequest(symbol="INJUSDT", interval="4h", prompt=prompt), scanner.market,
                                      llm, scanner=scanner)
        finally:
            await llm.close()

    try:
        res = asyncio.run(go("Best spot buys"))
        scan = scanner.latest("4h")
        assert scan is not None
        assert all(s.direction == "long" and s.spot_score is not None for s in scan.spot_buys)
        assert [s.symbol for s in res.setups] == [s.symbol for s in scan.best_spot(10)]
        assert "spot buys" in res.summary.lower()
        res = asyncio.run(go("Best grid bot coins"))
        assert [g.symbol for g in res.grid_coins] == [g.symbol for g in scan.grid_coins[:10]]
        assert res.grid_plan is None and "grid" in res.summary.lower()
        res = asyncio.run(go("best 4h setups right now"))
        assert res.setups and all(s.direction == "long" for s in res.setups)  # spot mode: no shorts
    finally:
        asyncio.run(scanner.market.close())


# ------------------------------------------------------------- sell or trim


def test_sell_check_flags_lost_support_and_rejection_at_resistance_but_not_a_healthy_trend():
    broke = _df(list(_wave(200, amp=0.05)) + [97, 95.5, 95.2, 94.0, 93.2])
    lost = sell_check("XUSDT", "4h", broke, None, "t")
    assert lost and lost.action == "sell" and lost.zone == "H4 support" and "Lost H4 support" in lost.reason
    assert lost.sell_low > lost.last_price  # sell into the retest from below

    top = _df(list(_wave(198, amp=0.05)))
    top.loc[top.index[-1], "high"] = top["close"].iloc[-1] * 1.03  # a long upper wick into the zone
    trim = sell_check("XUSDT", "4h", top, None, "t")
    assert trim and trim.action == "trim" and trim.sell_high >= trim.last_price
    assert trim.support_below and trim.support_below < trim.last_price and "rejection wick" in trim.reason

    assert sell_check("XUSDT", "4h", _df(list(np.linspace(80, 100, 200))), None, "t") is None


def test_agent_sell_check_answers_for_the_watchlist_and_a_spot_short_checks_this_coin():
    res = _run("What should I sell or trim?", watchlist=["BTCUSDT", "ETHUSDT", "INJUSDT"])
    assert res.intent.sell_check and res.plan is None and res.navigate is None
    assert all(r.symbol in ("BTCUSDT", "ETHUSDT", "INJUSDT") for r in res.sells)
    assert res.summary  # either the flagged coins or "nothing to sell"
    res = _run("should I sell INJ?", watchlist=["BTCUSDT", "ETHUSDT"])
    assert res.intent.sell_check and all(r.symbol == "INJUSDT" for r in res.sells)
    res = _run("give me a short setup")
    assert "Spot mode is on" in res.summary and all(r.symbol == "INJUSDT" for r in res.sells)
    res = _run("give me a short setup", spot_only=False)
    assert res.sells == []


def test_sell_check_endpoint():
    with TestClient(app) as c:
        r = c.post("/api/sell-check", json={"symbols": ["BTCUSDT", "ETHUSDT"], "interval": "4h"})
        assert r.status_code == 200 and all(x["action"] in ("sell", "trim") for x in r.json())
        assert c.post("/api/sell-check", json={"symbols": "BTC"}).status_code == 422
