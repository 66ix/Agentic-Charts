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
from .schemas import ALL_FEATURES, INTERVALS, TARGETS, AnalysisIntent, ChatTurn, CustomLevel

log = logging.getLogger(__name__)

# Strict-mode compatible schema (every key required, no extra keys) so the same
# document works for Ollama, OpenAI strict json_schema and Anthropic tools.
INTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["features", "timeframe", "window_timeframes", "max_zones", "answer_hint", "custom_levels",
                 "remove", "keep_existing", "alert_prices", "alert_targets"],
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
        "custom_levels": {
            "type": "array",
            "description": "Levels the user gave explicit prices for ('line at 25.4', 'box 24 to 25'). "
                           "Never invent prices; empty unless the user typed the numbers.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "price", "price_low", "price_high", "label"],
                "properties": {
                    "kind": {"type": "string", "enum": ["line", "zone"]},
                    "price": {"type": ["number", "null"], "description": "For a line."},
                    "price_low": {"type": ["number", "null"], "description": "For a zone."},
                    "price_high": {"type": ["number", "null"], "description": "For a zone."},
                    "label": {"type": "string", "description": "Short label, e.g. 'Entry' or 'My target'."},
                },
            },
        },
        "remove": {
            "type": "array",
            "items": {"type": "string", "enum": [t for t in TARGETS if t != "new"]},
            "description": "Overlay groups to remove from the chart ('remove the trendline' → trendlines, "
                           "'clear the chart' → all). Empty if nothing should be removed.",
        },
        "keep_existing": {
            "type": "boolean",
            "description": "true when the request adds to, removes from or alerts on what is already drawn "
                           "(follow-ups like 'also show swings', 'remove the trendline', 'line at 25'); "
                           "false when it asks for a fresh analysis that should replace the AI overlays.",
        },
        "alert_prices": {
            "type": "array",
            "items": {"type": "number"},
            "description": "Prices the user wants a price alert at ('alert me at 25.4'). Never invent prices.",
        },
        "alert_targets": {
            "type": "array",
            "items": {"type": "string", "enum": list(TARGETS)},
            "description": "Overlay groups to alert on ('alert me if it enters the supply zone' → supply; "
                           "'alert me on these levels' → new if this request draws levels, else all).",
        },
    },
}

INTENT_SYSTEM = (
    "You translate a trader's chart request into a JSON analysis plan for a crypto charting app. "
    "Pick only the detectors needed to answer the request; if the request is vague ('analyse this', "
    "'key levels') use support_resistance and window_levels. 'Supply zone' or 'demand zone' means "
    "supply_demand. 'Resistance high' or 'window high' means window_levels. Map timeframes: "
    "M1→1m, M5→5m, M15→15m, M30→30m, H1→1h, H3→3h, H4→4h, D/D1/daily→1d, W/weekly→1w, M/monthly→1M. "
    "The user may be following up on the conversation: use it and the overlays already on the chart to resolve "
    "'that', 'it' or 'same on daily' (reuse the previous plan's detectors). If the request only draws given "
    "prices, removes overlays or sets alerts, features may be empty. Respond with JSON only."
)

NARRATE_SYSTEM = (
    "You are a concise crypto market-structure analyst inside a charting app. Answer the user's request "
    "in at most 4 short sentences using ONLY the numbers in the FACTS JSON; never invent prices or "
    "indicators. Refer to zones by their price range. The levels are already drawn on the chart. "
    "If FACTS lists actions (levels you drew, overlays removed, alerts set), confirm them briefly. "
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


_NUM = r"\$?(\d[\d,]*(?:\.\d+)?k?)(?![\d.]*\s*(?:m|h|d|w|min|mins|minutes?|hours?|days?|weeks?|months?)\b)"
_CLAUSE_END = r"(?:\b(?:and|then|but)\b|[,.;!?]|$)"
_GROUP_WORDS: list[tuple[str, str]] = [
    (r"\b(all|everything|chart|overlays|drawings)\b", "all"),
    (r"\bsupports?\b", "support"),
    (r"\bresistances?\b", "resistance"),
    (r"\bsupply\b", "supply"),
    (r"\bdemand\b", "demand"),
    (r"\bwindows?\b", "window"),
    (r"\b(swings?|markers?|pivots?|structure)\b", "swings"),
    (r"\btrend ?lines?\b", "trendlines"),
    (r"\b(my|custom|manual)\b", "custom"),
]


def _num(token: str) -> float:
    token = token.replace(",", "").lower()
    return float(token[:-1]) * 1000 if token.endswith("k") else float(token)


def _groups(text: str) -> list[str]:
    return [g for pat, g in _GROUP_WORDS if re.search(pat, text)]


def _custom_levels(p: str) -> tuple[list[CustomLevel], list[str]]:
    """Explicit prices in the request → (levels, matched phrases)."""
    out: list[CustomLevel] = []
    spans: list[str] = []
    zone_pats = (r"\b(?:zone|box|range|rectangle|area)\b\D{0,20}?" + _NUM + r"\s*(?:-|–|to|and)\s*" + _NUM,
                 _NUM + r"\s*(?:-|–|to)\s*" + _NUM + r"\s*(?:zone|box|range|area)\b")
    for pat in zone_pats:
        for m in re.finditer(pat, p):
            z = CustomLevel(kind="zone", price_low=_num(m.group(1)), price_high=_num(m.group(2)))
            if not any(o.price_low == z.price_low and o.price_high == z.price_high for o in out):
                out.append(z)
            spans.append(m.group(0))
    line_pat = (r"\b(line|level|ray|mark|horizontal|draw|support|resistance|target|entry|stop|tp|sl)\b"
                r".{0,25}?(?:\bat\b|@)\s*" + _NUM)
    for m in re.finditer(line_pat, p):
        word = m.group(1)
        label = {"tp": "Target", "sl": "Stop"}.get(word, word.title() if word in (
            "support", "resistance", "target", "entry", "stop") else "")
        out.append(CustomLevel(kind="line", price=_num(m.group(2)), label=label))
        spans.append(m.group(0))
    return out[:10], spans


def rule_intent(prompt: str, previous: AnalysisIntent | None = None) -> AnalysisIntent:
    """Keyword parser used when no LLM is available."""
    p = prompt.lower()

    # Removals: the clause after the verb names what to take off ("remove the trendline and ...").
    remove: list[str] = []
    scan = p
    for m in re.finditer(r"\b(remove|delete|clear|hide|drop|erase|get rid of)\b(.*?)" + _CLAUSE_END, p):
        groups = [g for g in _groups(m.group(2)) if g != "custom" or "my" in m.group(2) or "custom" in m.group(2)]
        if not groups:
            groups = ["all"] if (m.group(1) == "clear" or previous is None) else [
                g for f in previous.features for g in _FEATURE_GROUPS[f]] or ["all"]
        remove += groups
        scan = scan.replace(m.group(0), " ")

    # Alerts: "alert me at 25.4", "alert me if it enters the supply zone", "alert me on these levels".
    alert_prices: list[float] = []
    alert_targets: list[str] = []
    alert = re.search(r"\b(alert|notify|ping|tell me|let me know)\b(.*)", p)
    if alert:
        clause = alert.group(2)
        alert_prices = [_num(n) for n in re.findall(r"(?:at|hits?|reach(?:es)?|cross(?:es)?|above|below|to)\s*"
                                                   + _NUM, clause)]
        if not alert_prices:
            alert_targets = [g for g in _groups(clause) if g not in ("custom",) or "my" in clause] or ["new"]
            scan = scan.replace(clause, " " + " ".join(g for g in alert_targets if g != "all") + " ")

    custom, spans = _custom_levels(scan)
    for span in spans:  # "support at 24.1" is a drawing, not a request for the S/R detector
        scan = scan.replace(span, " ")

    feats: list[str] = []
    if re.search(r"\b(everything|full|complete)\b", scan) or (re.search(r"\ball\b", scan) and not alert):
        feats = list(ALL_FEATURES)
    if re.search(r"support|resistance|\blevels?\b|s/r|\bkey\b|\bsr\b", scan):
        feats.append("support_resistance")
    if re.search(r"supply|demand|order ?block|\bob\b|imbalance", scan):
        feats.append("supply_demand")
    if re.search(r"swing|pivot|structure|\bhh\b|\bhl\b|\blh\b|\bll\b|bos|choch", scan):
        feats.append("swings")
    if re.search(r"window|previous (candle|bar|day|week)|prior (candle|bar|day)|\b(high|low)s?\b", scan):
        feats.append("window_levels")
    if re.search(r"trend ?lines?|channel|wedge|triangle", scan):
        feats.append("trendlines")
    if alert_targets == ["new"] and not feats and not custom:
        alert_targets = ["all"]

    tfs = [tf for pat, tf in _TF_PATTERNS if re.search(pat, p)]
    timeframe = tfs[0] if tfs else None
    windows = [tf for tf in tfs if tf in ("1h", "4h", "1d", "1w")] or ["4h", "1d"]
    single = bool(re.search(r"\b(the|current|nearest|key)\b.*\b(zone|level|high|low)\b(?!s)", scan))
    max_zones = 1 if single else 2

    acting = bool(custom or remove or alert_prices or alert_targets)
    follow_up = previous is not None and not feats and not acting and (
        timeframe is not None or re.search(r"\b(same|again|that|it|this|now|instead|redo|refresh)\b", p))
    if follow_up:
        feats = list(previous.features)
        windows = [tf for tf in tfs if tf in ("1h", "4h", "1d", "1w")] or list(previous.window_timeframes)
        max_zones = previous.max_zones
    elif not feats and not acting:
        feats = ["support_resistance", "window_levels"]

    keep = acting or bool(re.search(r"\b(also|add|plus|too|as well|keep|on top)\b", p))
    return AnalysisIntent(features=feats, timeframe=timeframe, window_timeframes=windows, max_zones=max_zones,
                          answer_hint=prompt.strip()[:200], custom_levels=custom, remove=remove,
                          keep_existing=keep, alert_prices=alert_prices[:10], alert_targets=alert_targets)


_FEATURE_GROUPS: dict[str, list[str]] = {
    "support_resistance": ["support", "resistance"],
    "supply_demand": ["supply", "demand"],
    "swings": ["swings"],
    "window_levels": ["window"],
    "trendlines": ["trendlines"],
}


def _context_block(history: list[ChatTurn], overlays: list, previous: AnalysisIntent | None) -> str:
    parts = []
    if history:
        parts.append("CONVERSATION SO FAR:\n" + "\n".join(f"{t.role}: {t.text[:400]}" for t in history[-6:]))
    if overlays:
        rows = []
        for o in overlays[:40]:
            price = getattr(o, "price", None)
            rng = (f"{o.price_low}-{o.price_high}" if getattr(o, "price_high", None) is not None
                   else f"{price}" if price is not None else "")
            rows.append(f"- {o.kind or o.type}: {o.label} {rng}".rstrip())
        parts.append("OVERLAYS ON THE CHART:\n" + "\n".join(rows))
    if previous:
        parts.append("PREVIOUS PLAN: " + previous.model_dump_json(include={"features", "timeframe", "max_zones"}))
    return "\n\n".join(parts)


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
    async def parse_intent(self, prompt: str, history: list[ChatTurn] | None = None, overlays: list | None = None,
                           previous: AnalysisIntent | None = None) -> tuple[AnalysisIntent, str]:
        if not prompt.strip():
            return AnalysisIntent(answer_hint="Auto-detect key levels"), "default"
        if self._available():
            context = _context_block(history or [], overlays or [], previous)
            user = f"{context}\n\nNEW REQUEST: {prompt}" if context else prompt
            try:
                raw = await self._structured(INTENT_SYSTEM, user, INTENT_SCHEMA, "analysis_plan")
                intent = AnalysisIntent.model_validate(raw)
                return intent, f"{self.provider}:{self.model}"
            except (httpx.HTTPError, ValidationError, ValueError, KeyError, TypeError) as exc:
                log.warning("LLM intent parsing failed (%s: %s); using rule parser", type(exc).__name__, exc)
                self._trip(exc)
        return rule_intent(prompt, previous), "rules"

    async def narrate(self, prompt: str, facts: dict, fallback: str,
                      history: list[ChatTurn] | None = None) -> tuple[str, str]:
        if not self._available() or not prompt.strip():
            return fallback, "template"
        convo = "\n".join(f"{t.role}: {t.text[:400]}" for t in (history or [])[-4:])
        user = (f"CONVERSATION SO FAR:\n{convo}\n\n" if convo else "") + \
            f"REQUEST: {prompt}\n\nFACTS: {json.dumps(facts, default=float)}"
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
