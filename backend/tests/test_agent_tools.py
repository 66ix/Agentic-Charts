"""The tool-calling planner and the new agent abilities (navigation, scans, trade plans, indicators),
with the LLM mocked at the HTTP layer."""

import asyncio
import json
import os

os.environ["DATA_SOURCE"] = "synthetic"
os.environ["LLM_PROVIDER"] = "none"

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.agent import run_analysis  # noqa: E402
from app.config import Settings  # noqa: E402
from app.llm import LLMClient, rule_intent  # noqa: E402
from app.main import app  # noqa: E402
from app.market_data import MarketData  # noqa: E402
from app.schemas import AnalyzeRequest  # noqa: E402

WATCH = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "INJUSDT"]


def _plan(**kw):
    base = {"features": [], "timeframe": None, "window_timeframes": ["4h", "1d"], "max_zones": 2, "answer_hint": "",
            "custom_levels": [], "remove": [], "keep_existing": False, "alert_prices": [], "alert_targets": [],
            "symbol": None, "switch_chart": False, "scan_watchlist": False, "scan_filter": "any",
            "trade_plan": None, "indicators_on": [], "indicators_off": []}
    return {**base, **kw}


def _llm(provider: str, handler) -> LLMClient:
    s = Settings(llm_provider=provider, anthropic_api_key="k", openai_api_key="k")
    llm = LLMClient(s)
    llm._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return llm


def _run(llm, prompt, **kw):
    req = AnalyzeRequest(symbol="INJUSDT", interval="4h", prompt=prompt, watchlist=WATCH, **kw)
    md = MarketData()
    try:
        return asyncio.run(run_analysis(req, md, llm))
    finally:
        asyncio.run(md.close())


def test_anthropic_tool_loop_looks_then_draws():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append(body)
        if "tools" not in body:  # narration
            return httpx.Response(200, json={"content": [{"type": "text", "text": "Daily agrees."}]})
        n = sum(1 for b in seen if "tools" in b)
        if n == 1:
            assert body["tool_choice"] == {"type": "any"}
            return httpx.Response(200, json={"content": [
                {"type": "tool_use", "id": "t1", "name": "look_at_chart",
                 "input": {"symbol": "INJUSDT", "timeframe": "1d", "features": ["support_resistance"]}}]})
        # The tool result went back as a user turn with a tool_result block.
        last = body["messages"][-1]
        assert last["role"] == "user" and last["content"][0]["type"] == "tool_result"
        assert '"timeframe":"D1"' in last["content"][0]["content"]
        return httpx.Response(200, json={"content": [
            {"type": "tool_use", "id": "t2", "name": "draw_on_chart",
             "input": _plan(features=["supply_demand"], keep_existing=False)}]})

    res = _run(_llm("anthropic", handler), "is the 4h demand backed by the daily?")
    assert res.engine["intent"].endswith("(tools)")
    assert res.steps == ["Looked at INJUSDT on 1d"]
    assert res.summary == "Daily agrees."
    assert {o.kind for o in res.overlays} <= {"supply", "demand"}


def test_openai_tool_loop_messages_and_strict_tools():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        calls.append(body)
        if "tools" not in body:
            return httpx.Response(200, json={"choices": [{"message": {"content": "Switched to ETH."}}]})
        assert all(t["function"]["strict"] for t in body["tools"])
        if len([c for c in calls if "tools" in c]) == 1:
            return httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "market_context",
                                                              "arguments": '{"symbol": "ETHUSDT"}'}}]}}]})
        tool_msg = body["messages"][-1]
        assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "c1"
        return httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "c2", "type": "function", "function": {"name": "draw_on_chart", "arguments": json.dumps(
                _plan(symbol="ETHUSDT", timeframe="1d", switch_chart=True, features=["support_resistance"]))}}]}}]})

    res = _run(_llm("openai", handler), "open ETH daily and show key levels")
    assert res.navigate and (res.navigate.symbol, res.navigate.interval) == ("ETHUSDT", "1d")
    assert res.symbol == "ETHUSDT" and res.interval == "1d"
    assert res.steps == ["Checked funding and open interest for ETHUSDT"]


def test_ollama_without_tool_call_falls_back_to_single_shot():
    contexts = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        contexts.append(body["options"].get("num_ctx"))
        if "tools" in body:
            return httpx.Response(200, json={"message": {"role": "assistant", "content": "I think..."}})
        if "format" in body:
            return httpx.Response(200, json={"message": {"content": json.dumps(_plan(indicators_on=["rsi"]))}})
        return httpx.Response(200, json={"message": {"content": "RSI is on."}})

    res = _run(_llm("ollama", handler), "add rsi")
    assert res.engine["intent"] == "ollama:llama3.1:8b"
    assert res.indicators == {"rsi": True}
    # Tool loop, single-shot plan and narration all ask for a window the tool schemas fit in.
    assert len(contexts) == 3 and set(contexts) == {8192}


def test_navigation_scan_plan_and_indicators_with_rules():
    with TestClient(app) as client:
        def ask(prompt, **kw):
            r = client.post("/api/agent/analyze", json={"symbol": "INJUSDT", "interval": "4h", "prompt": prompt,
                                                        "watchlist": WATCH, **kw})
            assert r.status_code == 200, r.text
            return r.json()

        j = ask("switch to daily")
        assert j["navigate"] == {"symbol": "INJUSDT", "interval": "1d"} and j["interval"] == "1d"
        j = ask("show me ETH supply and demand")
        assert j["navigate"] == {"symbol": "ETHUSDT", "interval": "4h"} and j["symbol"] == "ETHUSDT"
        assert {o["kind"] for o in j["overlays"]} <= {"supply", "demand"}
        j = ask("show me the H4 supply zone", interval="1h")
        assert j["navigate"] is None and j["analysis_interval"] == "4h" and j["interval"] == "1h"

        j = ask("which of my coins are near demand?")
        assert {r["symbol"] for r in j["scan"]} == set(WATCH)
        assert [r["score"] for r in j["scan"]] == sorted((r["score"] for r in j["scan"]), reverse=True)

        j = ask("give me a long setup on 1h", interval="1h", symbol="BTCUSDT")
        plan = j["plan"]
        assert plan["direction"] == "long" and plan["stop"] < plan["entry"]
        assert all(t["price"] > plan["entry"] and t["rr"] > 0 for t in plan["targets"])
        kinds = [o["kind"] for o in j["overlays"]]
        assert "plan_entry" in kinds and "plan_stop" in kinds and "plan_target" in kinds

        j = ask("add RSI and MACD, hide the volume bars")
        assert j["indicators"] == {"rsi": True, "macd": True, "volume": False}

        t = client.get("/api/tickers", params={"symbols": "BTCUSDT,ETHUSDT"}).json()["tickers"]
        assert [x["symbol"] for x in t] == ["BTCUSDT", "ETHUSDT"] and t[0]["price"] > 0
        rows = client.get("/api/watchlist/scan", params={"symbols": "SOLUSDT,BTCUSDT", "interval": "1h"}).json()
        assert [r["symbol"] for r in rows] == ["SOLUSDT", "BTCUSDT"]
        assert client.get("/api/tickers", params={"symbols": ""}).status_code == 422


def test_rule_parser_symbols_are_not_everyday_words():
    assert rule_intent("show near support").symbol is None
    assert rule_intent("what about NEAR?").symbol == "NEARUSDT"
    assert rule_intent("same on solana").symbol == "SOLUSDT"
    assert rule_intent("op chart please").symbol is None
    assert rule_intent("eth/usdt daily").symbol == "ETHUSDT"
