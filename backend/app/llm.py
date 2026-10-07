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
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import ValidationError

from .config import Settings, get_settings
from .pricefmt import round_facts
from .schemas import (
    ALL_FEATURES,
    FULL_FEATURES,
    INDICATORS,
    INTERVALS,
    SCAN_FILTERS,
    TARGETS,
    AnalysisIntent,
    ChatTurn,
    CustomLevel,
)
from .symbols import find_symbol

log = logging.getLogger(__name__)

# Strict-mode compatible schema (every key required, no extra keys) so the same
# document works for Ollama, OpenAI strict json_schema and Anthropic tools.
INTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["features", "timeframe", "window_timeframes", "max_zones", "answer_hint", "custom_levels",
                 "remove", "keep_existing", "alert_prices", "alert_targets", "symbol", "switch_chart",
                 "scan_watchlist", "scan_filter", "trade_plan", "indicators_on", "indicators_off"],
    "properties": {
        "features": {
            "type": "array",
            "items": {"type": "string", "enum": list(ALL_FEATURES)},
            "description": "Detectors to run. support_resistance = clustered S/R boxes; supply_demand = "
                           "base/impulse zones; swings = swing high/low markers with HH/HL/LH/LL; "
                           "window_levels = previous higher-timeframe candle high/low lines; "
                           "trendlines = lines through recent swings; liquidity_sweeps = wicks through a swing "
                           "that closed back inside (stop hunts); fvg = unfilled fair value gaps / imbalances; "
                           "order_blocks = last opposite candle before a break of structure; patterns = ranges, "
                           "triangles, wedges, double tops/bottoms; volume_profile = POC and value area.",
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
                           "'alert me on these levels' → new if this request draws levels, else all; "
                           "'alert me on the plan' → plan; 'alert me at the entry' → entry; stop; targets).",
        },
        "symbol": {
            "type": ["string", "null"],
            "description": "Coin to look at as a Binance USDT pair (ETH → ETHUSDT, solana → SOLUSDT) when the user "
                           "names one. null = the coin already on the chart.",
        },
        "switch_chart": {
            "type": "boolean",
            "description": "true when the user wants the chart itself moved: 'switch to daily', 'open ETH', 'show me "
                           "the SOL 1h chart'. Naming another coin always moves the chart. false for 'H4 supply' asked "
                           "on the current chart (the higher-timeframe zones are drawn on this chart).",
        },
        "scan_watchlist": {
            "type": "boolean",
            "description": "true for questions about several coins: 'which of my coins are near demand?', "
                           "'scan my watchlist', 'anything oversold?'.",
        },
        "scan_filter": {"type": "string", "enum": list(SCAN_FILTERS),
                        "description": "What the scan ranks by; 'any' when it is not a scan."},
        "trade_plan": {
            "type": ["string", "null"],
            "enum": ["long", "short", "auto", None],
            "description": "Build an entry/stop/targets plan from the detected zones ('give me a long setup' → long, "
                           "'what's the trade here?' → auto). null otherwise.",
        },
        "indicators_on": {"type": "array", "items": {"type": "string", "enum": list(INDICATORS)},
                          "description": "Chart indicators to show ('add RSI' → rsi; 'show my Kimi' or 'turn on "
                                         "Kimi Cooked' → kimi, the user's own indicator)."},
        "indicators_off": {"type": "array", "items": {"type": "string", "enum": list(INDICATORS)},
                           "description": "Chart indicators to hide."},
    },
}

INTENT_SYSTEM = (
    "You translate a trader's chart request into a JSON analysis plan for a crypto charting app. "
    "Pick only the detectors needed to answer the request; if the request is vague ('analyse this', "
    "'key levels') use support_resistance and window_levels. 'Supply zone' or 'demand zone' means "
    "supply_demand. 'Resistance high' or 'window high' means window_levels. Map timeframes: "
    "M1→1m, M5→5m, M15→15m, M30→30m, H1→1h, H3→3h, H4→4h, D/D1/daily→1d, W/weekly→1w, M/monthly→1M. "
    "The user may be following up on the conversation: use it and the overlays already on the chart to resolve "
    "'that', 'it' or 'same on daily' / 'same on ETH' (reuse the previous plan's detectors). 'Order block' means "
    "order_blocks, 'imbalance' or 'FVG' means fvg, 'stop hunt' or 'liquidity grab' means liquidity_sweeps, "
    "'triangle', 'wedge', 'range' or 'double top' means patterns. If the request only draws given prices, removes "
    "overlays, sets alerts, switches the chart, toggles indicators or scans the watchlist, features may be empty. "
    "'Kimi' or 'Kimi Cooked' is the user's own indicator: 'show Kimi' → indicators_on kimi; a question about what "
    "Kimi says needs no detectors (its facts are read separately) and keeps the chart as it is (keep_existing true). "
    "Keep what is on the chart (keep_existing true) whenever the request doesn't ask for a new analysis. "
    "Respond with JSON only."
)

NARRATE_SYSTEM = (
    "You are a concise crypto market-structure analyst inside a charting app. Answer the user's request "
    "in at most 4 short sentences using ONLY the numbers in the FACTS JSON; never invent prices or "
    "indicators. Quote prices exactly as FACTS gives them (they are rounded to the coin's price step) and give ATR "
    "with its percent of price (atr_pct). Refer to zones by their price range. The levels are already drawn on the "
    "chart. "
    "If FACTS lists actions (levels you drew, overlays removed, alerts set, chart switched), confirm them briefly. "
    "Lead with what matters for a decision: where price sits relative to the nearest zones (distance in ATR), "
    "higher-timeframe confluence, structure breaks, divergences, sweeps, and funding/open interest when given. "
    "For a trade plan give entry, stop, targets and reward-to-risk. For a scan name the best few coins and why. "
    "FACTS.kimi is the user's own indicator, Kimi Cooked: name it, and give its levels with odds_pct (the chance "
    "price reaches that level within the forecast window), its latest signals and its forecast when they answer "
    "the question; mention its chart_patterns (a watching pattern's break-out level and invalidation, a break-out's "
    "target) and harmonics (PRZ, TP1/TP2, invalidation) when it has any. "
    "FACTS.futures_context has funding, open interest, the long/short ratio, 24h spot CVD, the nearest order-book "
    "walls and estimated liquidation clusters (call them estimates); use what the question needs. "
    "FACTS.upcoming_events lists high-impact economic events by hours from now: with a trade plan, warn about any "
    "inside it; an empty list means nothing high-impact is scheduled. FACTS.headlines are recent news titles. "
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
    (r"\b(sweeps?|stop hunts?|liquidity grabs?)\b", "sweeps"),
    (r"\b(fvgs?|fair value gaps?|imbalances?|gaps?)\b", "fvg"),
    (r"\b(order ?blocks?|obs?)\b", "order_blocks"),
    (r"\b(patterns?|triangles?|wedges?|ranges?|double (?:tops?|bottoms?)|necklines?)\b", "patterns"),
    (r"\b(volume profile|poc|value area|vah|val)\b", "volume_profile"),
    (r"\b(plan|setup|entry|stop|targets?|trade)\b", "plan"),
    (r"\bentry\b", "entry"),
    (r"\bstop(?! hunts?)(?:[ -]?loss)?\b|\bsl\b", "stop"),
    (r"\b(targets?|tps?|take[ -]?profits?)\b", "targets"),
]
_PLAN_PARTS = ("entry", "stop", "targets")

_INDICATOR_WORDS: list[tuple[str, str]] = [
    (r"\brsi\b", "rsi"),
    (r"\bmacd\b", "macd"),
    (r"\bvwap\b", "vwap"),
    (r"\b(?:ema ?20|20 ?ema)\b", "ema20"),
    (r"\b(?:ema ?50|50 ?ema)\b", "ema50"),
    (r"\b(?:psar|parabolic(?: sar)?)\b", "psar"),
    (r"\bvolume (?:bars?|histogram)\b", "volume"),
    (r"\bkimi(?: cooked)?(?: indicator| script)?\b", "kimi"),
]
_OFF = r"\b(?:hide|remove|delete|turn off|disable|close|drop|get rid of|take off)\b"
_ON = r"\b(?:show|add|turn on|enable|display|plot|put|overlay|open|bring up|pull up|give me)\b"
_SWITCH = (r"\b(?:switch(?:es|ed)?|change (?:to|the (?:chart|timeframe|tf|symbol|coin))|go (?:to|back to|over to)|"
           r"flip (?:to|over to)|jump (?:to|over to)|open(?! interest)|pull up|bring up|load up|take me to)\b")
_SCAN = (r"\b(?:scan|screen(?:er)?|watch ?list|which (?:of my |of the )?(?:coins?|symbols?|pairs?|ones|tokens?)|"
         r"any (?:of my )?(?:coins?|pairs?|tokens?)|across (?:my|the) (?:coins?|list|watchlist)|all my coins|"
         r"best (?:coins?|setups?) (?:on|in) my)\b")
_PLAN = (r"\b(?:trade plan|trade idea|(?:long|short|trade) setup|setup|plan (?:a|the|my) trade|"
         r"where (?:should|would|do|can) i (?:buy|enter|long|short|sell|get in)|should i (?:long|short|buy|sell)|"
         r"(?:long|short) (?:entry|idea|trade|position)|give me (?:a|an) (?:long|short|entry|trade)|"
         r"what(?:'s| is) the trade)\b")


def _num(token: str) -> float:
    token = token.replace(",", "").lower()
    return float(token[:-1]) * 1000 if token.endswith("k") else float(token)


def _groups(text: str) -> list[str]:
    found = [g for pat, g in _GROUP_WORDS if re.search(pat, text)]
    if any(g in _PLAN_PARTS for g in found):  # "the entry" means that line, not the whole plan
        found = [g for g in found if g != "plan"]
    return found


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


def _clause(p: str, start: int) -> str:
    """Text from `start` to the end of its clause."""
    m = re.compile(_CLAUSE_END).search(p, start)
    return p[start:m.start() if m else len(p)]


def _indicator_toggles(p: str) -> tuple[list[str], list[str], str]:
    """'add RSI and MACD, hide the EMA 20' → (on, off, text with the indicator names blanked).
    Each indicator takes the nearest show/hide verb before it in the same sentence."""
    on: list[str] = []
    off: list[str] = []
    verbs = sorted([(v.start(), "off") for v in re.finditer(_OFF, p)] + [(v.start(), "on") for v in re.finditer(_ON, p)])
    stops = [m.start() for m in re.finditer(r"[.;!?]", p)]
    hits = sorted((m.start(), m.end(), name) for pat, name in _INDICATOR_WORDS for m in re.finditer(pat, p))
    for start, end, name in hits:
        sentence_start = max([s for s in stops if s < start], default=-1)
        before = [kind for pos, kind in verbs if sentence_start < pos < start]
        if not before:
            continue
        (off if before[-1] == "off" else on).append(name)
        p = p[:start] + " " * (end - start) + p[end:]
    return list(dict.fromkeys(on)), list(dict.fromkeys(off)), p


def _scan_filter(p: str) -> str:
    for pat, f in ((r"oversold", "oversold"), (r"overbought", "overbought"),
                   (r"\bbreak(?:ing|out|s)?\b", "breakout"), (r"\b(?:demand|support|bounce|dip)", "near_support"),
                   (r"\b(?:supply|resistance|reject)", "near_resistance"),
                   (r"\b(?:bullish|long|strong|uptrend|pump)", "bullish"),
                   (r"\b(?:bearish|short|weak|downtrend|dump)", "bearish")):
        if re.search(pat, p):
            return f
    return "any"


def rule_intent(prompt: str, previous: AnalysisIntent | None = None, known_bases: set[str] | None = None,
                chart_symbol: str | None = None) -> AnalysisIntent:
    """Keyword parser used when no LLM is available."""
    p = prompt.lower()
    symbol = find_symbol(prompt, known_bases)
    if symbol == chart_symbol:
        symbol = None

    indicators_on, indicators_off, p_ind = _indicator_toggles(p)

    # Removals: the clause after the verb names what to take off ("remove the trendline and ...").
    remove: list[str] = []
    scan = p_ind
    for m in re.finditer(r"\b(remove|delete|clear|hide|drop|erase|get rid of)\b(.*?)" + _CLAUSE_END, p_ind):
        target = m.group(2)
        groups = [g for g in _groups(target) if g != "custom" or "my" in target or "custom" in target]
        if not groups and not re.sub(r"\b(?:the|a|an|my|that|those|these|indicators?)\b", "", target).strip() and (
                indicators_on or indicators_off):
            continue  # "hide the RSI": the clause was the indicator
        if not groups:
            groups = ["all"] if (m.group(1) == "clear" or previous is None) else [
                g for f in previous.features for g in _FEATURE_GROUPS[f]] or ["all"]
        remove += groups
        scan = scan.replace(m.group(0), " ")
        # "remove the order blocks and the fvg": verbless list items continue the removal.
        pos = m.end()
        while (more := re.match(r"\s*(?:,|and)?\s*(?:the\s+)?([a-z/ ]{2,30}?)\s*(?=\b(?:and|then|but)\b|[,.;!?]|$)",
                                p_ind[pos:])) and more.group(1).strip():
            item = more.group(1)
            if re.search(r"\b(draw|show|add|alert|set|give|find|mark|switch|open|go|put|plot|scan)\b", item) or \
                    not _groups(item):
                break
            remove += _groups(item)
            scan = scan.replace(item, " ")
            pos += more.end()

    # Alerts: "alert me at 25.4", "alert me if it enters the supply zone", "alert me on these levels".
    alert_prices: list[float] = []
    alert_targets: list[str] = []
    alert = re.search(r"\b(alert|notify|ping|tell me|let me know)\b(.*)", p_ind)
    if alert:
        clause = alert.group(2)
        alert_prices = [_num(n) for n in re.findall(r"(?:at|hits?|reach(?:es)?|cross(?:es)?|above|below|to)\s*"
                                                   + _NUM, clause)]
        if not alert_prices:
            alert_targets = [g for g in _groups(clause) if g not in ("custom",) or "my" in clause] or ["new"]
            if "plan" in alert_targets or any(g in _PLAN_PARTS for g in alert_targets):
                alert_targets = [g for g in alert_targets if g != "custom"]
            scan = scan.replace(clause, " " + " ".join(g for g in alert_targets
                                                       if g not in ("all", "plan", *_PLAN_PARTS)) + " ")

    custom, spans = _custom_levels(scan)
    for span in spans:  # "support at 24.1" is a drawing, not a request for the S/R detector
        scan = scan.replace(span, " ")

    trade_plan = None
    if re.search(_PLAN, scan):
        trade_plan = ("long" if re.search(r"\b(?:long|buy|bull)", scan)
                      else "short" if re.search(r"\b(?:short|sell|bear)", scan) else "auto")
        scan = re.sub(_PLAN, " ", scan)

    scan_watchlist = bool(re.search(_SCAN, scan))
    scan_filter = _scan_filter(scan) if scan_watchlist else "any"
    if scan_watchlist:
        scan = re.sub(_SCAN, " ", scan)

    feats: list[str] = []
    if re.search(r"\b(everything|full|complete)\b", scan) or (re.search(r"\ball\b", scan) and not alert):
        feats = list(FULL_FEATURES)
    if re.search(r"support|resistance|\blevels?\b|s/r|\bkey\b|\bsr\b", scan):
        feats.append("support_resistance")
    if re.search(r"supply|demand", scan):
        feats.append("supply_demand")
    if re.search(r"swing|pivot|structure|\bhh\b|\bhl\b|\blh\b|\bll\b|bos|choch", scan):
        feats.append("swings")
    if re.search(r"window|previous (candle|bar|day|week)|prior (candle|bar|day)|\b(high|low)s?\b", scan):
        feats.append("window_levels")
    if re.search(r"trend ?lines?|channel", scan):
        feats.append("trendlines")
    if re.search(r"sweep|stop hunt|liquidity|grab", scan):
        feats.append("liquidity_sweeps")
    if re.search(r"\bfvgs?\b|fair value|imbalance|\bgaps?\b", scan):
        feats.append("fvg")
    if re.search(r"order ?blocks?|\bobs?\b", scan):
        feats.append("order_blocks")
    if re.search(r"pattern|triangle|wedge|double (top|bottom)|\branges?\b|ranging|consolidat|neckline", scan):
        feats.append("patterns")
    if re.search(r"volume profile|\bpoc\b|value area|\bvpvr\b|\bvah\b|\bval\b", scan):
        feats.append("volume_profile")
    if scan_watchlist and not re.search(r"\b(?:this|the) chart\b|\bhere\b", scan):
        feats = []  # "which coins are near demand" ranks the scan; it doesn't ask for zones on this chart
    if alert_targets == ["new"] and not feats and not custom and not trade_plan:
        alert_targets = ["all"]
    if alert_targets == ["new"] and trade_plan and not feats:
        alert_targets = ["plan"]

    tfs = [tf for pat, tf in _TF_PATTERNS if re.search(pat, p)]
    timeframe = tfs[0] if tfs else None
    windows = [tf for tf in tfs if tf in ("1h", "4h", "1d", "1w")] or ["4h", "1d"]
    single = bool(re.search(r"\b(the|current|nearest|key)\b.*\b(zone|level|high|low)\b(?!s)", scan))
    max_zones = 1 if single else 2
    switch_chart = bool(re.search(_SWITCH, p)) or (timeframe is not None and bool(
        re.search(r"\b(?:chart|timeframe|tf)\b", p)) and not feats)

    acting = bool(custom or remove or alert_prices or alert_targets or indicators_on or indicators_off
                  or scan_watchlist or trade_plan)
    navigating = symbol is not None or switch_chart
    # "What does Kimi say?" is read from Kimi's own facts: no detectors, and the chart stays as it is.
    asks_kimi = not feats and bool(re.search(r"\bkimi\b", p))
    follow_up = previous is not None and not feats and not acting and (
        timeframe is not None or symbol is not None
        or re.search(r"\b(same|again|that|it|this|now|instead|redo|refresh)\b", p))
    if follow_up and not (switch_chart and not re.search(r"\b(same|again|redo)\b", p)):
        feats = list(previous.features)
        windows = [tf for tf in tfs if tf in ("1h", "4h", "1d", "1w")] or list(previous.window_timeframes)
        max_zones = previous.max_zones
        trade_plan = previous.trade_plan if re.search(r"\b(same|again|redo)\b", p) else None
    elif not feats and not acting and not navigating and not asks_kimi:
        feats = ["support_resistance", "window_levels"]

    keep = acting or asks_kimi or bool(re.search(r"\b(also|add|plus|too|as well|keep|on top)\b", p))
    if trade_plan and not remove:
        keep = keep and bool(re.search(r"\b(also|add|plus|too|as well|keep|on top)\b", p))
    return AnalysisIntent(features=feats, timeframe=timeframe, window_timeframes=windows, max_zones=max_zones,
                          answer_hint=prompt.strip()[:200], custom_levels=custom, remove=remove,
                          keep_existing=keep, alert_prices=alert_prices[:10], alert_targets=alert_targets,
                          symbol=symbol, switch_chart=switch_chart, scan_watchlist=scan_watchlist,
                          scan_filter=scan_filter, trade_plan=trade_plan, indicators_on=indicators_on,
                          indicators_off=indicators_off)


_FEATURE_GROUPS: dict[str, list[str]] = {
    "support_resistance": ["support", "resistance"],
    "supply_demand": ["supply", "demand"],
    "swings": ["swings"],
    "window_levels": ["window"],
    "trendlines": ["trendlines"],
    "liquidity_sweeps": ["sweeps"],
    "fvg": ["fvg"],
    "order_blocks": ["order_blocks"],
    "patterns": ["patterns"],
    "volume_profile": ["volume_profile"],
}


@dataclass
class ChartContext:
    """What the user is looking at: the chart's coin and timeframe, and their watchlist."""

    symbol: str = ""
    interval: str = ""
    watchlist: list[str] = field(default_factory=list)


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class ToolTurn:
    text: str
    calls: list[ToolCall]


def _context_block(history: list[ChatTurn], overlays: list, previous: AnalysisIntent | None,
                   chart: ChartContext | None = None) -> str:
    parts = []
    if chart and chart.symbol:
        parts.append(f"CHART: {chart.symbol} {chart.interval}")
    if chart and chart.watchlist:
        parts.append("WATCHLIST: " + ", ".join(chart.watchlist[:40]))
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
        parts.append("PREVIOUS PLAN: " + previous.model_dump_json(
            include={"features", "timeframe", "max_zones", "symbol", "trade_plan"}))
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
    def available(self) -> bool:
        return self._available()

    def context_block(self, history: list[ChatTurn], overlays: list, previous: AnalysisIntent | None,
                      chart: ChartContext | None) -> str:
        return _context_block(history, overlays, previous, chart)

    async def parse_intent(self, prompt: str, history: list[ChatTurn] | None = None, overlays: list | None = None,
                           previous: AnalysisIntent | None = None,
                           chart: ChartContext | None = None) -> tuple[AnalysisIntent, str]:
        if not prompt.strip():
            return AnalysisIntent(answer_hint="Auto-detect key levels"), "default"
        if self._available():
            context = _context_block(history or [], overlays or [], previous, chart)
            user = f"{context}\n\nNEW REQUEST: {prompt}" if context else prompt
            try:
                raw = await self._structured(INTENT_SYSTEM, user, INTENT_SCHEMA, "analysis_plan")
                intent = AnalysisIntent.model_validate(raw)
                return intent, f"{self.provider}:{self.model}"
            except (httpx.HTTPError, ValidationError, ValueError, KeyError, TypeError) as exc:
                log.warning("LLM intent parsing failed (%s: %s); using rule parser", type(exc).__name__, exc)
                self._trip(exc)
        known = {w.removesuffix("USDT") for w in (chart.watchlist if chart else [])}
        return rule_intent(prompt, previous, known, chart.symbol if chart else None), "rules"

    async def tool_turn(self, system: str, messages: list[dict], tools: list[dict], force: str | None = None) -> ToolTurn:
        """One model turn with tools. `messages` is provider-neutral:
        {"role": "user", "text"} | {"role": "assistant", "text", "calls": [ToolCall]} | {"role": "tool", "id", "name",
        "content"}. `force` = "any" to require some tool call, or a tool name to require that one."""
        if self.provider == "anthropic":
            choice = {"type": "tool", "name": force} if force not in (None, "any") else {"type": "any"} if force else {
                "type": "auto"}
            r = await self._client.post("https://api.anthropic.com/v1/messages", headers=self._anthropic_headers(),
                                        json={
                "model": self.s.anthropic_model, "max_tokens": 1024, "system": system,
                "tools": [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                          for t in tools],
                "tool_choice": choice, "messages": _to_anthropic(messages),
            })
            r.raise_for_status()
            blocks = r.json()["content"]
            return ToolTurn(text="".join(b.get("text", "") for b in blocks if b.get("type") == "text"),
                            calls=[ToolCall(b["id"], b["name"], b.get("input") or {}) for b in blocks
                                   if b.get("type") == "tool_use"])

        if self.provider == "openai":
            choice: Any = ({"type": "function", "function": {"name": force}} if force not in (None, "any")
                           else "required" if force else "auto")
            r = await self._client.post(f"{self.s.openai_base_url}/chat/completions", headers=self._openai_headers(),
                                        json={
                "model": self.s.openai_model, "temperature": 0, "tool_choice": choice,
                "tools": [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                            "parameters": t["parameters"], "strict": True}}
                          for t in tools],
                "messages": [{"role": "system", "content": system}, *_to_openai(messages)],
            })
            r.raise_for_status()
            msg = r.json()["choices"][0]["message"]
            calls = [ToolCall(c["id"], c["function"]["name"], json.loads(c["function"].get("arguments") or "{}"))
                     for c in msg.get("tool_calls") or []]
            return ToolTurn(text=msg.get("content") or "", calls=calls)

        if self.provider == "ollama":
            r = await self._client.post(f"{self.s.ollama_url}/api/chat", json={
                "model": self.s.ollama_model, "stream": False, "options": self._ollama_options(0),
                "tools": [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                            "parameters": t["parameters"]}} for t in tools],
                "messages": [{"role": "system", "content": system}, *_to_ollama(messages)],
            })
            r.raise_for_status()
            msg = r.json()["message"]
            calls = []
            for c in msg.get("tool_calls") or []:
                args = c["function"].get("arguments") or {}
                calls.append(ToolCall(f"call_{uuid.uuid4().hex[:8]}", c["function"]["name"],
                                      json.loads(args) if isinstance(args, str) else args))
            return ToolTurn(text=msg.get("content") or "", calls=calls)

        raise ValueError("No LLM provider configured")

    def note_failure(self, exc: Exception) -> None:
        self._trip(exc)

    async def narrate(self, prompt: str, facts: dict, fallback: str,
                      history: list[ChatTurn] | None = None) -> tuple[str, str]:
        if not self._available() or not prompt.strip():
            return fallback, "template"
        convo = "\n".join(f"{t.role}: {t.text[:400]}" for t in (history or [])[-4:])
        user = (f"CONVERSATION SO FAR:\n{convo}\n\n" if convo else "") + \
            f"REQUEST: {prompt}\n\nFACTS: {json.dumps(round_facts(facts), default=float)}"
        try:
            text = (await self._text(NARRATE_SYSTEM, user)).strip()
            if text:
                return text, f"{self.provider}:{self.model}"
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            log.warning("LLM narration failed (%s: %s); using template", type(exc).__name__, exc)
            self._trip(exc)
        return fallback, "template"

    # -------------------------------------------------------- providers
    def _ollama_options(self, temperature: float) -> dict:
        opts: dict = {"temperature": temperature}
        if self.s.ollama_num_ctx > 0:
            opts["num_ctx"] = self.s.ollama_num_ctx
        return opts

    async def _structured(self, system: str, user: str, schema: dict, name: str) -> dict:
        if self.provider == "ollama":
            r = await self._client.post(f"{self.s.ollama_url}/api/chat", json={
                "model": self.s.ollama_model, "stream": False, "format": schema,
                "options": self._ollama_options(0),
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
                "model": self.s.ollama_model, "stream": False, "options": self._ollama_options(0.2),
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


# ------------------------------------------------- tool-message translation


def _to_anthropic(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["text"]})
        elif m["role"] == "assistant":
            content: list[dict] = [{"type": "text", "text": m["text"]}] if m.get("text") else []
            content += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.args} for c in m.get("calls", [])]
            out.append({"role": "assistant", "content": content})
        else:  # tool results go back as one user turn
            block = {"type": "tool_result", "tool_use_id": m["id"], "content": m["content"]}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
    return out


def _to_openai(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["text"]})
        elif m["role"] == "assistant":
            msg: dict = {"role": "assistant", "content": m.get("text") or None}
            if m.get("calls"):
                msg["tool_calls"] = [{"id": c.id, "type": "function",
                                      "function": {"name": c.name, "arguments": json.dumps(c.args)}}
                                     for c in m["calls"]]
            out.append(msg)
        else:
            out.append({"role": "tool", "tool_call_id": m["id"], "content": m["content"]})
    return out


def _to_ollama(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["text"]})
        elif m["role"] == "assistant":
            out.append({"role": "assistant", "content": m.get("text") or "",
                        "tool_calls": [{"function": {"name": c.name, "arguments": c.args}} for c in m.get("calls", [])]})
        else:
            out.append({"role": "tool", "content": m["content"], "tool_name": m["name"]})
    return out
