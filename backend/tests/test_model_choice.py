"""Settings → AI model: switching the agent's model at runtime and scoring models on the eval set."""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from app.config import get_settings
from app.llm import LLMClient
from app.model_choice import ModelChoice, ModelChooser
from evals.run import load_cases


def _settings(tmp_path=None, **kw):
    store = str(tmp_path / "choice.json") if tmp_path else "memory"
    return replace(get_settings(), llm_provider="ollama", ollama_model="llama3.1:8b", llm_choice_store=store, **kw)


def _ollama(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_choice_switches_model_and_goes_back(tmp_path):
    s = _settings(tmp_path)
    llm = LLMClient(s)
    chooser = ModelChooser(llm, s)
    assert (llm.provider, llm.model) == ("ollama", "llama3.1:8b")

    out = chooser.choose(ModelChoice(provider="ollama", model="qwen2.5:7b"))
    assert out["model"] == "qwen2.5:7b" and out["custom"]
    assert llm.model == "qwen2.5:7b"

    # Kept across restarts.
    llm2 = LLMClient(s)
    ModelChooser(llm2, s)
    assert llm2.model == "qwen2.5:7b"

    chooser.choose(None)
    assert llm.model == "llama3.1:8b" and not chooser.current()["custom"]


def test_cloud_provider_needs_a_key():
    s = _settings(anthropic_api_key="")
    chooser = ModelChooser(LLMClient(s), s)
    with pytest.raises(ValueError, match="API key"):
        chooser.choose(ModelChoice(provider="anthropic", model="claude-sonnet-5-5"))


def test_requests_use_the_chosen_model():
    s = _settings()
    llm = LLMClient(s)
    llm.use("ollama", "qwen2.5:7b")
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content)["model"])
        return httpx.Response(200, json={"message": {"content": "Hello"}})

    llm._client = _ollama(handler)
    text, engine = asyncio.run(llm.narrate("levels?", {"price": 1}, "fallback"))
    assert text == "Hello" and engine == "ollama:qwen2.5:7b" and seen == ["qwen2.5:7b"]


def test_ollama_models_lists_installed_with_gpu_share():
    s = _settings()
    chooser = ModelChooser(LLMClient(s), s)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [
                {"name": "qwen2.5:7b", "size": 4_700_000_000,
                 "details": {"parameter_size": "7.6B", "quantization_level": "Q4_K_M"}}]})
        return httpx.Response(200, json={"models": [{"name": "qwen2.5:7b", "size": 6e9, "size_vram": 4.5e9}]})

    chooser._http = _ollama(handler)
    out = asyncio.run(chooser.ollama_models())
    assert out["error"] is None
    assert out["installed"] == [{"model": "qwen2.5:7b", "size_gb": 4.7, "params": "7.6B", "quant": "Q4_K_M",
                                 "gpu_share": 0.75}]
    assert next(m for m in out["suggested"] if m["model"] == "qwen2.5:7b")["installed"]


def test_ollama_down_is_reported_not_raised():
    s = _settings()
    chooser = ModelChooser(LLMClient(s), s)

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    chooser._http = _ollama(handler)
    out = asyncio.run(chooser.ollama_models())
    assert out["installed"] == [] and "isn't reachable" in out["error"]


def test_eval_scores_a_model(monkeypatch):
    """A model that returns the expected plan for the first case passes it and fails the rest."""
    s = _settings()
    chooser = ModelChooser(LLMClient(s), s)
    case = load_cases()[0]
    plan = {"features": case["expect"]["features"], "timeframe": case["expect"]["timeframe"],
            "switch_chart": False, "max_zones": 1}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        prompt = json.loads(req.content)["messages"][-1]["content"]
        body = plan if prompt.endswith(case["prompt"]) else {"features": []}
        return httpx.Response(200, json={"message": {"content": json.dumps(body)}})

    real_init = LLMClient.__init__

    def init(self, settings=None):
        real_init(self, settings)
        self._client = _ollama(handler)

    monkeypatch.setattr(LLMClient, "__init__", init)
    chooser._http = _ollama(handler)

    async def go():
        run = chooser.start_eval(ModelChoice(provider="ollama", model="qwen2.5:7b"), limit=3)
        with pytest.raises(RuntimeError, match="already running"):
            chooser.start_eval(ModelChoice(provider="ollama", model="x"))
        await chooser._task
        return run

    run = asyncio.run(go())
    assert run.status == "done" and run.total == 3 and run.done == 3
    assert run.passed == 1 and run.cases[0].ok and not run.cases[1].ok
    assert run.avg_seconds is not None
    assert chooser.runs()[0].id == run.id


def test_eval_stops_when_the_model_does_not_answer(monkeypatch):
    s = _settings()
    chooser = ModelChooser(LLMClient(s), s)

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "model 'nope' not found"})

    real_init = LLMClient.__init__

    def init(self, settings=None):
        real_init(self, settings)
        self._client = _ollama(handler)

    monkeypatch.setattr(LLMClient, "__init__", init)

    async def go():
        run = chooser.start_eval(ModelChoice(provider="ollama", model="nope"))
        await chooser._task
        return run

    run = asyncio.run(go())
    assert run.status == "failed" and "didn't answer" in run.error and run.done == 1


def test_api_routes():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        r = c.get("/api/llm/models")
        assert r.status_code == 200 and "ollama" in r.json() and "providers" in r.json()
        r = c.put("/api/llm/model", json={"provider": "ollama", "model": "qwen2.5:7b"})
        assert r.status_code == 200 and r.json()["model"] == "qwen2.5:7b"
        assert c.get("/api/health").json()["llm"]["model"] == "qwen2.5:7b"
        assert c.put("/api/llm/model", json={"provider": None}).json()["custom"] is False
        assert c.put("/api/llm/model", json={"provider": "nope", "model": "x"}).status_code == 422
        assert c.get("/api/llm/evals/missing").status_code == 404
        assert c.get("/api/llm/evals").json() == {"runs": []}
