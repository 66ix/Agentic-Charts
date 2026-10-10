"""Picking the agent's model from the app, and scoring models on the prompt eval set.

The provider and model in .env are the default. Settings → AI model can switch to another one at runtime
(saved in LLM_CHOICE_STORE, so it survives restarts) and run evals/intents.jsonl against any model to compare
them on real numbers: how many plans it gets right, how long it takes, and, for Ollama, whether it fits in
the GPU's memory or spills onto the CPU (which is what makes a local model slow).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Literal, Optional

import httpx
from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .llm import LLMClient

log = logging.getLogger(__name__)

Provider = Literal["ollama", "openai", "anthropic", "google"]
MAX_HISTORY = 50

# Local models worth trying on an 8-12 GB GPU, with the tag to `ollama pull`. Sizes are the Q4 downloads.
SUGGESTED_OLLAMA = [
    {"model": "qwen2.5:7b", "size_gb": 4.7, "note": "Strong at JSON plans and tool calls for its size"},
    {"model": "llama3.1:8b", "size_gb": 4.9, "note": "The app's default"},
    {"model": "qwen3:8b", "size_gb": 5.2, "note": "Newer Qwen; good tool calling"},
    {"model": "mistral-nemo:12b", "size_gb": 7.1, "note": "Bigger; just fits 8 GB with a small context"},
    {"model": "qwen2.5:14b", "size_gb": 9.0, "note": "Better plans, but spills past 8 GB onto the CPU"},
    {"model": "gemma3:4b", "size_gb": 3.3, "note": "Fast and small; weaker at multi-step requests"},
]


class ModelChoice(BaseModel):
    provider: Provider
    model: str = Field(..., min_length=1, max_length=120)


class EvalCaseResult(BaseModel):
    prompt: str
    ok: bool
    errors: list[str] = []
    seconds: float


class EvalRun(BaseModel):
    id: str
    provider: str
    model: str
    status: Literal["running", "done", "failed"] = "running"
    started_at: float
    finished_at: Optional[float] = None
    total: int
    done: int = 0
    passed: int = 0
    avg_seconds: Optional[float] = None
    # Ollama only: share of the loaded model that sat in GPU memory (1.0 = all of it).
    gpu_share: Optional[float] = None
    size_gb: Optional[float] = None
    error: Optional[str] = None
    cases: list[EvalCaseResult] = []


class ModelChooser:
    """The current choice, what's installed, and eval runs (one at a time)."""

    def __init__(self, llm: LLMClient, settings: Settings | None = None) -> None:
        self.s = settings or get_settings()
        self.llm = llm
        store = self.s.llm_choice_store.strip()
        self._store = None if store.lower() in ("memory", "none", "off") else Path(store)
        self._runs: list[EvalRun] = []
        self._task: asyncio.Task | None = None
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(5.0))
        self._load()

    async def close(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        await self._http.aclose()

    # ------------------------------------------------------------ choice
    def current(self) -> dict:
        s = self.s
        return {
            "provider": self.llm.provider,
            "model": self.llm.model,
            "custom": self.llm.choice is not None,
            "default": {"provider": s.llm_provider, "model": {"ollama": s.ollama_model, "openai": s.openai_model,
                                                             "google": s.google_model,
                                                             "anthropic": s.anthropic_model}.get(s.llm_provider, "")},
            # Which providers have what they need (a key, or a URL) to be picked.
            "providers": {"ollama": True, "openai": bool(s.openai_api_key) or "api.openai.com" not in s.openai_base_url,
                          "anthropic": bool(s.anthropic_api_key), "google": bool(s.google_api_key)},
            # Who plans requests: the rules when they are sure ("rules_first") or the model every time.
            "router": self.llm.router, "router_default": s.agent_router,
        }

    def set_router(self, router: str | None) -> dict:
        if router not in (None, "rules_first", "llm_first"):
            raise ValueError("router must be rules_first or llm_first")
        self.llm.router_choice = router
        self._save()
        return self.current()

    def choose(self, choice: ModelChoice | None) -> dict:
        if choice and not self.current()["providers"][choice.provider]:
            raise ValueError(f"{choice.provider} needs an API key in the backend's .env first")
        self.llm.use(choice.provider if choice else None, choice.model if choice else None)
        self._save()
        return self.current()

    async def ollama_models(self) -> dict:
        """Installed Ollama models (with size and quantisation), what's loaded now, and suggestions."""
        installed, loaded, error = [], {}, None
        try:
            r = await self._http.get(f"{self.s.ollama_url}/api/tags")
            r.raise_for_status()
            for m in r.json().get("models", []):
                d = m.get("details") or {}
                installed.append({"model": m["name"], "size_gb": round(m.get("size", 0) / 1e9, 1),
                                  "params": d.get("parameter_size"), "quant": d.get("quantization_level")})
            loaded = await self._loaded()
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            error = f"Ollama isn't reachable at {self.s.ollama_url} ({type(exc).__name__})"
        names = {m["model"] for m in installed}
        for m in installed:
            m["gpu_share"] = loaded.get(m["model"])
        return {"installed": installed, "error": error,
                "suggested": [{**m, "installed": m["model"] in names} for m in SUGGESTED_OLLAMA]}

    async def _loaded(self) -> dict[str, float]:
        """Ollama's /api/ps: per loaded model, the share of it held in GPU memory."""
        r = await self._http.get(f"{self.s.ollama_url}/api/ps")
        r.raise_for_status()
        return {m["name"]: round(m.get("size_vram", 0) / m["size"], 2) for m in r.json().get("models", [])
                if m.get("size")}

    # ------------------------------------------------------------- evals
    def runs(self) -> list[EvalRun]:
        return list(reversed(self._runs))

    def run(self, run_id: str) -> EvalRun | None:
        return next((r for r in self._runs if r.id == run_id), None)

    def start_eval(self, choice: ModelChoice, limit: int | None = None) -> EvalRun:
        if self._task and not self._task.done():
            raise RuntimeError("An eval is already running; wait for it to finish")
        if not self.current()["providers"][choice.provider]:
            raise ValueError(f"{choice.provider} needs an API key in the backend's .env first")
        from evals.run import load_cases  # evals/ sits next to app/, so this is imported on demand

        cases = [c for c in load_cases()][: limit or None]
        run = EvalRun(id=uuid.uuid4().hex[:10], provider=choice.provider, model=choice.model,
                      started_at=time.time(), total=len(cases))
        self._runs.append(run)
        self._runs = self._runs[-MAX_HISTORY:]
        self._task = asyncio.create_task(self._eval(run, cases))
        return run

    async def _eval(self, run: EvalRun, cases: list[dict]) -> None:
        from evals.run import eval_case

        # Its own client, so the eval neither uses nor trips the circuit breaker of the one answering the chart.
        llm = LLMClient(replace(self.s, llm_timeout=max(self.s.llm_timeout, 60)))
        llm.use(run.provider, run.model)
        try:
            for case in cases:
                t0 = time.monotonic()
                errs = await eval_case(llm, case)
                secs = round(time.monotonic() - t0, 2)
                run.cases.append(EvalCaseResult(prompt=case["prompt"], ok=not errs, errors=errs, seconds=secs))
                run.done += 1
                run.passed += not errs
                if errs and errs[0].startswith("LLM unavailable") and run.done == 1:
                    raise RuntimeError(f"{run.provider}:{run.model} didn't answer; is it installed and running?")
            run.avg_seconds = round(sum(c.seconds for c in run.cases) / max(1, len(run.cases)), 2)
            if run.provider == "ollama":
                try:
                    run.gpu_share = (await self._loaded()).get(run.model)
                except (httpx.HTTPError, ValueError, KeyError):
                    pass
            run.status = "done"
        except asyncio.CancelledError:
            run.status, run.error = "failed", "stopped"
            raise
        except Exception as exc:  # noqa: BLE001 - any failure ends this run, shown in the app
            log.warning("Model eval failed: %s", exc)
            run.status, run.error = "failed", str(exc)
        finally:
            run.finished_at = time.time()
            await llm.close()
            self._save()

    # ------------------------------------------------------- persistence
    def _load(self) -> None:
        if not self._store or not self._store.exists():
            return
        try:
            data = json.loads(self._store.read_text())
            if data.get("choice"):
                c = ModelChoice.model_validate(data["choice"])
                self.llm.use(c.provider, c.model)
            if data.get("router") in ("rules_first", "llm_first"):
                self.llm.router_choice = data["router"]
            # A run cut off by a restart can't finish.
            self._runs = [EvalRun.model_validate(r) for r in data.get("runs", [])]
            for r in self._runs:
                if r.status == "running":
                    r.status, r.error = "failed", "the backend restarted"
        except (OSError, ValueError) as exc:
            log.warning("Could not read %s (%s); using the .env model", self._store, exc)

    def _save(self) -> None:
        if not self._store:
            return
        choice = self.llm.choice
        data = {"choice": {"provider": choice[0], "model": choice[1]} if choice else None,
                "router": self.llm.router_choice,
                "runs": [r.model_dump() for r in self._runs if r.status != "running"]}
        try:
            self._store.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._store.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(self._store)
        except OSError as exc:
            log.warning("Could not save the model choice to %s: %s", self._store, exc)
