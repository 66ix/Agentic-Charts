"""Streamed answers: the endpoint's events, and the summary streamed from each LLM provider (mocked over HTTP)."""

import asyncio
import json
import os

os.environ["DATA_SOURCE"] = "synthetic"
os.environ["LLM_PROVIDER"] = "none"

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.llm import LLMClient  # noqa: E402
from app.main import app  # noqa: E402


def _events(body: str) -> list[dict]:
    return [json.loads(line[5:]) for line in body.splitlines() if line.startswith("data:")]


def test_stream_endpoint_sends_result_then_summary_then_done():
    with TestClient(app) as client:
        r = client.post("/api/agent/analyze/stream", json={"symbol": "BTCUSDT", "interval": "4h",
                                                            "prompt": "draw support and resistance"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    ev = _events(r.text)
    kinds = [e["type"] for e in ev]
    assert kinds[0] == "result" and kinds[-1] == "done" and "delta" in kinds
    assert ev[0]["response"]["summary"] == "" and ev[0]["response"]["overlays"]
    done = ev[-1]["response"]
    assert "".join(e["text"] for e in ev if e["type"] == "delta") == done["summary"] != ""
    assert done["engine"]["summary"] == "template" and done["overlays"] == ev[0]["response"]["overlays"]


def _narrate(provider: str, body: str) -> tuple[list[str], tuple[str, str]]:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, text=body)

    llm = LLMClient(Settings(llm_provider=provider, anthropic_api_key="k", openai_api_key="k"))
    llm._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    got: list[str] = []

    async def on_delta(t: str) -> None:
        got.append(t)

    out = asyncio.run(llm.narrate_stream("levels?", {"a": 1}, "fallback", None, on_delta))
    return got, out


def test_narrate_stream_reads_each_provider():
    ollama = "\n".join(json.dumps({"message": {"content": c}}) for c in ("Sup", "port ", "holds.")) + "\n"
    openai = "".join(f"data: {json.dumps({'choices': [{'delta': {'content': c}}]})}\n\n"
                     for c in ("Sup", "port ", "holds.")) + "data: [DONE]\n\n"
    anthropic = ("event: message_start\ndata: {\"type\":\"message_start\"}\n\n" +
                 "".join(f"data: {json.dumps({'type': 'content_block_delta', 'delta': {'text': c}})}\n\n"
                         for c in ("Sup", "port ", "holds.")))
    for provider, body in (("ollama", ollama), ("openai", openai), ("anthropic", anthropic)):
        got, (text, engine) = _narrate(provider, body)
        assert got == ["Sup", "port ", "holds."], provider
        assert text == "Support holds." and engine.startswith(provider)


def test_narrate_stream_falls_back_to_the_template_when_nothing_comes():
    got, (text, engine) = _narrate("openai", "data: [DONE]\n\n")
    assert got == ["fallback"] and (text, engine) == ("fallback", "template")
