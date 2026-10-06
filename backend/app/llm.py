"""LLM layer: natural-language command → structured AnalysisIntent, and facts → prose.

The LLM never invents prices. It only (1) decides *which* detectors to run and on
which timeframe, via a JSON-schema-constrained response, and (2) phrases the
detector's findings. Both steps fall back to deterministic code if the provider
is unset, unreachable, slow, or returns invalid JSON.

Providers (LLM_PROVIDER):
  ollama    — local, `format=<json schema>` structured outputs (default)
  openai    — any OpenAI-compatible /chat/completions with `response_format=json_schema`
  anthropic — Messages API with a forced tool call for structured output
  none      — rules only
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import httpx
from pydantic import ValidationError

from .config import Settings, get_settings
from .schemas import ALL_FEATURES, INTERVALS, AnalysisIntent

log = logging.getLogger(__name__)

# Strict-mode compatible schema (every key required, no extra keys) so the same
# document works for Ollama, OpenAI strict json_schema and Anthropic tools.
INTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["features", "timeframe", "window_timeframes", "max_zones", "answer_hint"],
    "properties": {
        "features": {
            "type": "array",
            "items": {"type": "string", "enum": list(ALL_FEATURES)},
            "description": "Detectors to run. support_resistance = clustered S/R boxes; supply_demand = "
                           "base/impulse zones; swings = swing high/low markers with HH/HL/LH/LL; "
                           "window_levels = previous higher-timeframe candle high/low lines; "
                           "trendlines = lines through recent swings.",
        },
        "timeframe": {
            "type": ["string", "null"],
            "enum": [*INTERVALS, None],
            "description": "Timeframe the user wants analysed (H4 → 4h, daily → 1d). null if unspecified.",
        },
        "window_timeframes": {
            "type": "array",
            "items": {"type": "string", "enum": list(INTERVALS)},
            "description": "Higher timeframes whose previous-candle high/low to draw. Default [\"4h\",\"1d\"].",
        },
        "max_zones": {"type": "integer", "minimum": 1, "maximum": 6,
                      "description": "Zones per side of price. Default 2; 1 if the user asks for 'the' zone."},
        "answer_hint": {"type": "string", "description": "One-line restatement of the user's question."},
    },
}

INTENT_SYSTEM = (
    "You translate a trader's chart request into a JSON analysis plan for a crypto charting app. "
    "Pick only the detectors needed to answer the request; if the request is vague ('analyse this', "
    "'key levels') use support_resistance and window_levels. 'Supply zone' or 'demand zone' means "
    "supply_demand. 'Resistance high' or 'window high' means window_levels. Map timeframes: "
    "M1→1m, M5→5m, M15→15m, M30→30m, H1→1h, H3→3h, H4→4h, D/D1/daily→1d, W/weekly→1w, M/monthly→1M. "
    "Respond with JSON only."
)

NARRATE_SYSTEM = (
    "You are a concise crypto market-structure analyst inside a charting app. Answer the user's request "
    "in at most 4 short sentences using ONLY the numbers in the FACTS JSON; never invent prices or "
    "indicators. Refer to zones by their price range. The levels are already drawn on the chart. "
    "No disclaimers, no markdown."
)

_TF_PATTERNS: list[tuple[str, str]] = [
    (r"\b(m1|1m|1\s*min(ute)?)\b", "1m"),
    (r"\b(m5|5m|5\s*min(ute)?s?)\b", "5m"),
    (r"\b(m15|15m|15\s*min(ute)?s?)\b", "15m"),
    (r"\b(m30|30m|30\s*min(ute)?s?)\b", "30m"),
    (r"\b(h1|1h|1\s*hour|hourly)\b", "1h"),
    (r"\b(h3|3h|3\s*hours?)\b", "3h"),
    (r"\b(h4|4h|4\s*hours?)\b", "4h"),
    (r"\b(d1|1d|daily|day)\b", "1d"),
    (r"\b(w1|1w|weekly|week)\b", "1w"),
    (r"\b(monthly|month|mn)\b", "1M"),
]


def rule_intent(prompt: str) -> AnalysisIntent:
    """Keyword parser used when no LLM is available."""
    p = prompt.lower()
    feats: list[str] = []
    if re.search(r"\b(all|everything|full|complete)\b", p):
        feats = list(ALL_FEATURES)
    if re.search(r"support|resistance|\blevels?\b|s/r|\bkey\b|\bsr\b", p):
        feats.append("support_resistance")
    if re.search(r"supply|demand|order ?block|\bob\b|imbalance", p):
        feats.append("supply_demand")
    if re.search(r"swing|pivot|structure|\bhh\b|\bhl\b|\blh\b|\bll\b|bos|choch", p):
        feats.append("swings")
    if re.search(r"window|previous (candle|bar|day|week)|prior (candle|bar|day)|\b(high|low)s?\b", p):
        feats.append("window_levels")
    if re.search(r"trend ?lines?|channel|wedge|triangle", p):
        feats.append("trendlines")
    if not feats:
        feats = ["support_resistance", "window_levels"]

    tfs = [tf for pat, tf in _TF_PATTERNS if re.search(pat, p)]
    timeframe = tfs[0] if tfs else None
    windows = [tf for tf in tfs if tf in ("1h", "4h", "1d", "1w")] or ["4h", "1d"]
    single = bool(re.search(r"\b(the|current|nearest|key)\b.*\b(zone|level|high|low)\b(?!s)", p))
    return AnalysisIntent(features=feats, timeframe=timeframe, window_timeframes=windows,
                          max_zones=1 if single else 2, answer_hint=prompt.strip()[:200])


class LLMClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.s = settings or get_settings()
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.s.llm_timeout, connect=3.0))
        self._down_until = 0.0  # circuit breaker: skip the LLM briefly after a connection failure

    def _available(self) -> bool:
        return self.provider != "none" and time.monotonic() >= self._down_until

    def _trip(self, exc: Exception) -> None:
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            self._down_until = time.monotonic() + 30

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def provider(self) -> str:
        p = self.s.llm_provider
        if p == "openai" and not self.s.openai_api_key and "api.openai.com" in self.s.openai_base_url:
            return "none"
        if p == "anthropic" and not self.s.anthropic_api_key:
            return "none"
        return p if p in ("ollama", "openai", "anthropic") else "none"

    @property
    def model(self) -> str:
        return {"ollama": self.s.ollama_model, "openai": self.s.openai_model,
                "anthropic": self.s.anthropic_model}.get(self.provider, "")

    # ----------------------------------------------------------- public
    async def parse_intent(self, prompt: str) -> tuple[AnalysisIntent, str]:
        if not prompt.strip():
            return AnalysisIntent(answer_hint="Auto-detect key levels"), "default"
        if self._available():
            try:
                raw = await self._structured(INTENT_SYSTEM, prompt, INTENT_SCHEMA, "analysis_plan")
                intent = AnalysisIntent.model_validate(raw)
                return intent, f"{self.provider}:{self.model}"
            except (httpx.HTTPError, ValidationError, ValueError, KeyError, TypeError) as exc:
                log.warning("LLM intent parsing failed (%s: %s); using rule parser", type(exc).__name__, exc)
                self._trip(exc)
        return rule_intent(prompt), "rules"

    async def narrate(self, prompt: str, facts: dict, fallback: str) -> tuple[str, str]:
        if not self._available() or not prompt.strip():
            return fallback, "template"
        user = f"REQUEST: {prompt}\n\nFACTS: {json.dumps(facts, default=float)}"
        try:
            text = (await self._text(NARRATE_SYSTEM, user)).strip()
            if text:
                return text, f"{self.provider}:{self.model}"
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            log.warning("LLM narration failed (%s: %s); using template", type(exc).__name__, exc)
            self._trip(exc)
        return fallback, "template"

    # -------------------------------------------------------- providers
    async def _structured(self, system: str, user: str, schema: dict, name: str) -> dict:
        if self.provider == "ollama":
            r = await self._client.post(f"{self.s.ollama_url}/api/chat", json={
                "model": self.s.ollama_model, "stream": False, "format": schema,
                "options": {"temperature": 0},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            })
            r.raise_for_status()
            return json.loads(r.json()["message"]["content"])

        if self.provider == "openai":
            r = await self._client.post(f"{self.s.openai_base_url}/chat/completions", headers=self._openai_headers(),
                                        json={
                "model": self.s.openai_model, "temperature": 0,
                "response_format": {"type": "json_schema",
                                    "json_schema": {"name": name, "schema": schema, "strict": True}},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            })
            r.raise_for_status()
            return json.loads(r.json()["choices"][0]["message"]["content"])

        if self.provider == "anthropic":
            r = await self._client.post("https://api.anthropic.com/v1/messages", headers=self._anthropic_headers(),
                                        json={
                "model": self.s.anthropic_model, "max_tokens": 512, "system": system,
                "tools": [{"name": name, "description": "Return the analysis plan.", "input_schema": schema}],
                "tool_choice": {"type": "tool", "name": name},
                "messages": [{"role": "user", "content": user}],
            })
            r.raise_for_status()
            for block in r.json()["content"]:
                if block.get("type") == "tool_use":
                    return block["input"]
            raise ValueError("No tool_use block in Anthropic response")

        raise ValueError("No LLM provider configured")

    async def _text(self, system: str, user: str) -> str:
        if self.provider == "ollama":
            r = await self._client.post(f"{self.s.ollama_url}/api/chat", json={
                "model": self.s.ollama_model, "stream": False, "options": {"temperature": 0.2},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            })
            r.raise_for_status()
            return r.json()["message"]["content"]
        if self.provider == "openai":
            r = await self._client.post(f"{self.s.openai_base_url}/chat/completions", headers=self._openai_headers(),
                                        json={
                "model": self.s.openai_model, "temperature": 0.2,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            })
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        if self.provider == "anthropic":
            r = await self._client.post("https://api.anthropic.com/v1/messages", headers=self._anthropic_headers(),
                                        json={
                "model": self.s.anthropic_model, "max_tokens": 400, "system": system,
                "messages": [{"role": "user", "content": user}],
            })
            r.raise_for_status()
            return "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")
        raise ValueError("No LLM provider configured")

    def _openai_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.s.openai_api_key}"} if self.s.openai_api_key else {}

    def _anthropic_headers(self) -> dict[str, str]:
        return {"x-api-key": self.s.anthropic_api_key, "anthropic-version": "2023-06-01"}
