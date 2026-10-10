"""Rules first: clear requests are planned by the rules without the model; the model plans what the rules are
unsure of; tool calls can't fall back to demo data; the Ollama tool path handles a local model's quirks."""

import asyncio
import dataclasses
import json
import os

os.environ["DATA_SOURCE"] = "synthetic"
os.environ["LLM_PROVIDER"] = "none"

import httpx  # noqa: E402

from app.agent import run_analysis  # noqa: E402
from app.agent_loop import MAX_RESULT_CHARS, Toolbox, _clip, _run_tool  # noqa: E402
from app.config import Settings  # noqa: E402
from app.llm import ChartContext, LLMClient, rule_intent, rule_route, text_tool_calls  # noqa: E402
from app.market_data import MarketData  # noqa: E402
from app.schemas import AnalyzeRequest  # noqa: E402

# AgentPanel.tsx's SUGGESTIONS, SHORTCUTS and FOLLOW_UPS: one tap, so they must be instant.
TAPS = ["Identify the current H4 supply zone and key resistance high", "Which of my coins are near demand?",
        "Give me a long setup", "Best 4h setups across the market", "What does Kimi say?",
        "Find order blocks, FVGs and liquidity sweeps", "Open BTC daily and show key levels",
        "Top-down S/R walk", "Best spot buys", "Suggest a grid bot for this coin", "Best grid bot coins",
        "Where do I take profit?", "Plan a dip-buy ladder", "What should I sell or trim?",
        "Same on daily", "Does the daily agree?", "Give me a trade plan", "Alert me on these levels"]
UNSURE = ["compare INJ and SOL", "is INJ better than SOL?", "what about it?", "what is staking?",
          "Will the 7.5 support hold if BTC dumps?", "is this demand safe to buy?", "should I buy SOL?"]


def test_one_tap_requests_are_confident_and_judgements_are_not():
    prev = rule_intent("Give me a long setup")
    known = {"INJ", "SOL", "BTC"}
    for t in TAPS:
        r = rule_route(t, prev, known, "INJUSDT")
        assert r.confident and r.reason.startswith("explicit"), (t, r.reason)
    for t in UNSURE:
        r = rule_route(t, prev, known, "INJUSDT")
        assert not r.confident and r.reason.startswith("unsure"), (t, r.reason)


class NoModel(LLMClient):
    """A model that is 'available' but must never be asked to plan; narration falls back to the template."""

    def available(self) -> bool:
        return True

    async def tool_turn(self, *a, **kw):
        raise AssertionError("the fast path must not call the tool planner")

    async def parse_intent(self, *a, **kw):
        raise AssertionError("the fast path must not call the planner")

    async def narrate(self, prompt, facts, fallback, history=None, detail="normal"):
        return fallback, "template"


def test_the_fast_path_skips_the_model():
    llm = NoModel(dataclasses.replace(Settings(), llm_provider="ollama", agent_router="rules_first"))
    md = MarketData()

    async def go():
        try:
            return await run_analysis(AnalyzeRequest(symbol="INJUSDT", interval="4h", prompt="Give me a long setup"),
                                      md, llm)
        finally:
            await md.close()
            await llm.close()

    res = asyncio.run(go())
    assert res.engine["intent"] == "rules (fast path)" and res.engine["route"] == "explicit: trade_plan"
    assert res.plan is not None


def test_a_tool_result_on_demo_data_is_an_error_on_a_live_chart():
    calls = []

    async def look(sym, tf, feats):
        calls.append(sym)
        return {"symbol": sym, "data_source": "synthetic"}

    box = Toolbox(look=look, scan=None, context=None)  # type: ignore[arg-type]
    live = ChartContext("INJUSDT", "4h", listed=frozenset({"INJUSDT", "BTCUSDT"}))
    res, step = asyncio.run(_run_tool(box, "look_at_chart", {"symbol": "XYZ", "timeframe": "4h"}, live))
    assert "unknown symbol" in res["error"] and not calls  # never looked up
    res, step = asyncio.run(_run_tool(box, "look_at_chart", {"symbol": "BTC/USDT", "timeframe": "4h"}, live))
    assert calls == ["BTCUSDT"] and res == {"error": "no live data for BTCUSDT"} and step == ""
    demo = ChartContext("INJUSDT", "4h")  # the app runs on demo data: demo results are all there is
    res, _ = asyncio.run(_run_tool(box, "look_at_chart", {"symbol": "INJ", "timeframe": "4h"}, demo))
    assert res["data_source"] == "synthetic"


def test_clip_keeps_valid_json():
    big = {"zones": [{"low": i, "high": i + 1, "note": "x" * 50} for i in range(400)], "symbol": "INJUSDT"}
    text = _clip(big)
    assert len(text) <= MAX_RESULT_CHARS
    out = json.loads(text)
    assert out["symbol"] == "INJUSDT" and 0 < len(out["zones"]) < 400


def test_text_tool_calls():
    calls = text_tool_calls('Sure.\n<tool_call>{"name": "draw_on_chart", "arguments": {"features": []}}</tool_call>',
                            {"draw_on_chart"})
    assert [(c.name, c.args) for c in calls] == [("draw_on_chart", {"features": []})]
    assert text_tool_calls('{"name": "look_at_chart", "arguments": "{\\"symbol\\": \\"INJUSDT\\"}"}',
                           {"look_at_chart"})[0].args == {"symbol": "INJUSDT"}
    assert text_tool_calls('{"name": "rm_rf", "arguments": {}}', {"look_at_chart"}) == []


def test_ollama_forced_tool_is_the_only_one_offered():
    sent = []

    def handler(req):
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"message": {"content": '<tool_call>{"name": "draw_on_chart", '
                                                                '"arguments": {"features": ["swings"]}}</tool_call>'}})

    llm = LLMClient(dataclasses.replace(Settings(), llm_provider="ollama"))
    llm._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tools = [{"name": "look_at_chart", "description": "", "parameters": {}},
             {"name": "draw_on_chart", "description": "", "parameters": {}}]
    turn = asyncio.run(llm.tool_turn("sys", [{"role": "user", "text": "hi"}], tools, force="draw_on_chart"))
    assert [t["function"]["name"] for t in sent[0]["tools"]] == ["draw_on_chart"]
    assert turn.calls[0].name == "draw_on_chart" and turn.calls[0].args == {"features": ["swings"]}


def test_evals_route_counts():
    from evals.run import run_route

    r = run_route()
    assert r["fast"] >= r["cases"] * 0.8 and r["fast_right"] == r["fast"]
