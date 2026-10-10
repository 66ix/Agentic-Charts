"""Agent fixes: long chat turns, question-only follow-ups, amounts read as coins, tool-call symbols and the
narrator's coin and demo-data flag."""

import asyncio
import json
import os

os.environ["DATA_SOURCE"] = "synthetic"
os.environ["LLM_PROVIDER"] = "none"

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.agent import run_analysis  # noqa: E402
from app.agent_loop import Toolbox, _run_tool, plan_with_tools  # noqa: E402
from app.config import Settings  # noqa: E402
from app.llm import ChartContext, LLMClient, ToolCall, ToolTurn, rule_intent  # noqa: E402
from app.main import app  # noqa: E402
from app.market_data import MarketData, synthetic_klines  # noqa: E402
from app.schemas import AnalysisIntent, AnalyzeRequest  # noqa: E402
from app.symbols import find_symbol  # noqa: E402

PLAN_KINDS = {"plan_entry", "plan_stop", "plan_target"}


# ------------------------------------------------------------- long chat turns --

def test_a_long_earlier_answer_is_clipped_not_rejected():
    req = AnalyzeRequest(prompt="and now?", history=[{"role": "agent", "text": "x" * 5000}])
    assert len(req.history[0].text) == 2000
    with TestClient(app) as client:
        r = client.post("/api/agent/analyze", json={
            "symbol": "INJUSDT", "interval": "4h", "prompt": "key levels",
            "history": [{"role": "user", "text": "plan"}, {"role": "agent", "text": "word " * 1000}]})
        assert r.status_code == 200, r.text


# ------------------------------------------------------ question-only follow-ups --

QUESTIONS = ["What would invalidate this?", "Does the daily agree?", "Is RSI overbought here?", "why?",
             "is that a good entry?", "How far is the stop?"]


def test_questions_about_the_plan_draw_nothing_and_keep_the_chart():
    prev = rule_intent("Give me a long setup")
    for q in QUESTIONS:
        i = rule_intent(q, prev, chart_symbol="INJUSDT")
        assert (i.features, i.keep_existing, i.timeframe, i.trade_plan) == ([], True, None, None), q
        assert not i.has_actions and i.answer_hint == q
    # Asking for something still draws it; another coin, the macro and Kimi keep their own paths.
    assert rule_intent("does the daily agree with the 4h demand?", prev).features == ["supply_demand"]
    assert rule_intent("why is SOL pumping?", prev, chart_symbol="INJUSDT").symbol == "SOLUSDT"
    assert rule_intent("why is inflation rising?", prev).general_question
    i = rule_intent("Same on daily", prev)
    assert i.trade_plan == "long" and i.timeframe == "1d"


def test_follow_up_chips_leave_the_plan_on_the_chart():
    with TestClient(app) as client:
        def ask(prompt, overlays=(), previous=None):
            r = client.post("/api/agent/analyze", json={"symbol": "INJUSDT", "interval": "4h", "prompt": prompt,
                                                        "overlays": list(overlays), "previous_intent": previous})
            assert r.status_code == 200, r.text
            return r.json()

        first = ask("Give me a long setup")
        assert first["plan"] and PLAN_KINDS <= {o["kind"] for o in first["overlays"]}
        for q in ("What would invalidate this?", "Does the daily agree?", "Is RSI overbought here?"):
            res = ask(q, first["overlays"], first["intent"])
            assert {o["id"] for o in res["overlays"]} == {o["id"] for o in first["overlays"]}, q
            assert res["navigate"] is None and res["analysis_interval"] == "4h"


# ----------------------------------------------------------- amounts as coins --

def test_an_amount_in_usdt_is_not_a_coin():
    assert find_symbol("buy INJ with 500usdt") == "INJUSDT"
    assert find_symbol("buy 200USDT of INJ") == "INJUSDT"
    assert find_symbol("put 50usdc into sol") == "SOLUSDT"
    assert find_symbol("show 1000PEPEUSDT") == "1000PEPEUSDT" and find_symbol("1inch/usdt daily") == "1INCHUSDT"
    assert find_symbol("eth/usdt daily") == "ETHUSDT"
    assert rule_intent("buy INJ with 500usdt", chart_symbol="SOLUSDT").symbol == "INJUSDT"


def test_a_typed_pair_binance_does_not_list_falls_back_to_the_names():
    listed = frozenset({"INJUSDT", "SOLUSDT", "ETHUSDT"})
    assert find_symbol("XYZUSDT daily", listed=listed) is None
    assert find_symbol("XYZUSDT or solana", listed=listed) == "SOLUSDT"
    assert find_symbol("INJUSDT daily", listed=listed) == "INJUSDT"
    assert find_symbol("ETHUSDC daily", listed=listed) == "ETHUSDC"  # the list only has USDT pairs
    assert find_symbol("XYZUSDT daily") == "XYZUSDT"  # no live list: taken as typed
    assert rule_intent("open XYZUSDT", listed=listed).symbol is None


# --------------------------------------------------------- tool-call symbols --

def _box(seen: list):
    async def look(symbol, tf, features):
        seen.append(("look", symbol, tf))
        return {"symbol": symbol}

    async def scan(tf, filt):
        return []

    async def context(symbol):
        seen.append(("context", symbol))
        return {"symbol": symbol}

    async def kimi(symbol, tf):
        seen.append(("kimi", symbol, tf))
        return {}

    return Toolbox(look=look, scan=scan, context=context, kimi=kimi)


def test_tool_symbols_are_normalised_and_checked():
    seen: list = []
    box = _box(seen)
    chart = ChartContext("INJUSDT", "4h", listed=frozenset({"INJUSDT", "BTCUSDT", "SOLUSDT"}))

    async def go():
        out = [await _run_tool(box, "look_at_chart", {"symbol": "INJ", "timeframe": "1d"}, chart),
               await _run_tool(box, "market_context", {"symbol": "BTC/USDT"}, chart),
               await _run_tool(box, "read_kimi", {"symbol": "solana", "timeframe": "1h"}, chart),
               await _run_tool(box, "look_at_chart", {"symbol": "XYZ", "timeframe": "1d"}, chart),
               await _run_tool(box, "look_at_chart", {"timeframe": "1d"},
                               ChartContext("ETHUSDT/BTCUSDT", "4h"))]
        return out

    out = asyncio.run(go())
    assert seen == [("look", "INJUSDT", "1d"), ("context", "BTCUSDT"), ("kimi", "SOLUSDT", "1h")]
    assert out[0][1] == "Looked at INJUSDT on 1d"
    assert "unknown symbol XYZ" in out[3][0]["error"] and out[3][1] == ""
    assert "error" in out[4][0]
    # Without Binance's live list (synthetic data) a pair is taken as given.
    seen.clear()
    asyncio.run(_run_tool(box, "look_at_chart", {"symbol": "pendle"}, ChartContext("INJUSDT", "4h")))
    assert seen == [("look", "PENDLEUSDT", "4h")]


class _ToolLLM:
    """Looks at a coin whose candles fail, then draws."""

    provider, model = "fake", "m"

    def __init__(self):
        self.tool_results: list[str] = []

    def context_block(self, *a):
        return ""

    def note_failure(self, exc):
        raise AssertionError(f"the planner gave up: {exc}")

    async def tool_turn(self, system, messages, tools, force=None):
        if messages[-1]["role"] == "tool":
            self.tool_results.append(messages[-1]["content"])
            return ToolTurn("", [ToolCall("2", "draw_on_chart", {"features": ["support_resistance"]})])
        return ToolTurn("", [ToolCall("1", "look_at_chart", {"symbol": "INJUSDT", "timeframe": "1d",
                                                              "features": []})])


def test_a_failing_tool_is_an_error_result_not_the_end_of_the_plan():
    async def look(symbol, tf, features):
        raise RuntimeError("Binance klines failed: HTTP 400")  # MarketDataError in DATA_SOURCE=binance

    async def nothing(*a):
        return {}

    llm = _ToolLLM()
    box = Toolbox(look=look, scan=nothing, context=nothing)
    res = asyncio.run(plan_with_tools(llm, "does the daily agree?", [], [], None, ChartContext("INJUSDT", "4h"),
                                      box))
    assert res is not None and res.intent.features == ["support_resistance"]
    assert res.steps == [] and "Binance klines failed" in json.loads(llm.tool_results[0])["error"]


# ------------------------------------------------- the narrator's coin and source --

def _narrating_llm(seen: list) -> LLMClient:
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if "tools" in body:
            return httpx.Response(200, json={"content": [{"type": "tool_use", "id": "t1", "name": "draw_on_chart",
                                                          "input": AnalysisIntent().model_dump()}]})
        seen.append(body)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "INJ holds."}]})

    llm = LLMClient(Settings(llm_provider="anthropic", anthropic_api_key="k"))
    llm._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return llm


def _analyse(req: AnalyzeRequest, md: MarketData, llm: LLMClient):
    async def go():
        try:
            return await run_analysis(req, md, llm)
        finally:
            await md.close()
            await llm.close()
    return asyncio.run(go())


def _facts_sent(body: dict) -> dict:
    user = body["messages"][-1]["content"]
    return json.loads(user.split("FACTS: ", 1)[1])


def test_the_narrator_is_told_the_coin_and_that_the_data_is_demo():
    seen: list = []
    res = _analyse(AnalyzeRequest(symbol="INJUSDT", interval="4h", prompt="key levels"), MarketData(),
                   _narrating_llm(seen))
    assert res.data_source == "synthetic"
    facts = _facts_sent(seen[0])
    assert facts["symbol"] == "INJUSDT" and facts["coin"] == "INJ" and facts["data_source"] == "demo (synthetic)"
    assert "FACTS.symbol" in seen[0]["system"] and "FACTS.data_source" in seen[0]["system"]
    assert "symbol" not in res.facts and "coin" not in res.facts  # not numbers: left out of "numbers used"


def test_the_charts_own_candles_are_flagged_only_when_the_server_is_on_demo_data():
    candles = [c.model_dump() for c in synthetic_klines("SOLUSDT", "4h", 300)]
    for data_source, flagged in (("synthetic", True), ("auto", False)):
        seen: list = []
        md = MarketData()
        md.settings = Settings(data_source=data_source)
        md.mark_binance_down(3600)  # auto: no network in tests, but the chart's candles may well be live
        _analyse(AnalyzeRequest(symbol="SOLUSDT", interval="4h", prompt="key levels", candles=candles), md,
                 _narrating_llm(seen))
        facts = _facts_sent(seen[0])
        assert facts["coin"] == "SOL" and ("data_source" in facts) is flagged, data_source
