"""Tool-calling planner: the model can look before it draws.

Instead of filling one plan in a single shot, the model gets read-only tools that run the same
deterministic detectors (`look_at_chart` on any coin/timeframe, `scan_watchlist`, `market_context`)
and finishes by calling `draw_on_chart` with the usual AnalysisIntent. Prices still only come from the
detectors. Any failure (provider error, no tool support, step budget spent without a plan) falls back to the
single-shot planner, with whatever the tools already found kept as research.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx
from pydantic import ValidationError

from .llm import INTENT_SCHEMA, INTENT_SYSTEM, ChartContext, LLMClient
from .schemas import ALL_FEATURES, INTERVALS, SCAN_FILTERS, AnalysisIntent, ChatTurn, norm_symbol
from .symbols import QUOTES, find_symbol

log = logging.getLogger(__name__)

FINAL_TOOL = "draw_on_chart"
MAX_RESULT_CHARS = 3000

TOOLS: list[dict[str, Any]] = [
    {
        "name": "look_at_chart",
        "description": "Run the detectors on any coin and timeframe and read the results: zones with their distance "
                       "from price in ATR, higher-timeframe confluence, structure breaks, RSI and divergences, "
                       "sweeps, patterns, and the latest EMA, MACD, Bollinger, Stoch RSI, VWAP, Parabolic SAR and "
                       "ATR values. Nothing is drawn. Use it to check another timeframe or coin before "
                       "deciding what to show.",
        "parameters": {
            "type": "object", "additionalProperties": False, "required": ["symbol", "timeframe", "features"],
            "properties": {
                "symbol": {"type": "string", "description": "USDT pair, e.g. ETHUSDT"},
                "timeframe": {"type": "string", "enum": list(INTERVALS)},
                "features": {"type": "array", "items": {"type": "string", "enum": list(ALL_FEATURES)}},
            },
        },
    },
    {
        "name": "scan_watchlist",
        "description": "Run the detectors across the user's watchlist and rank the coins by a filter. Returns each "
                       "coin's price, 24h change, trend, RSI, nearest zone and signals.",
        "parameters": {
            "type": "object", "additionalProperties": False, "required": ["timeframe", "filter"],
            "properties": {
                "timeframe": {"type": "string", "enum": list(INTERVALS)},
                "filter": {"type": "string", "enum": list(SCAN_FILTERS)},
            },
        },
    },
    {
        "name": "scan_market",
        "description": "Rank the best long and short trade setups across the top coins by 24h volume (not just the "
                       "watchlist) on a timeframe: entry, stop, T1, reward-to-risk, distance from price, how many "
                       "timeframes agree and each setup's backtested track record. Use it for 'best setups right "
                       "now', 'scan the market for longs', 'what's moving that I could trade?'.",
        "parameters": {
            "type": "object", "additionalProperties": False, "required": ["timeframe", "direction"],
            "properties": {
                "timeframe": {"type": "string", "enum": list(INTERVALS)},
                "direction": {"type": "string", "enum": ["long", "short", "any"]},
            },
        },
    },
    {
        "name": "market_context",
        "description": "24h change, futures funding (now and 24h average), open interest and its 24h change, long/short "
                       "ratio, 24h spot CVD, the nearest order-book walls, estimated liquidation clusters, and "
                       "high-impact economic events in the next 24h, for one coin; plus the whole market (total "
                       "market cap, 24h volume, liquidations, open interest, Fear & Greed, BTC dominance).",
        "parameters": {
            "type": "object", "additionalProperties": False, "required": ["symbol"],
            "properties": {"symbol": {"type": "string", "description": "USDT pair, e.g. BTCUSDT"}},
        },
    },
    {
        "name": "read_kimi",
        "description": "Read the user's own indicator, Kimi Cooked v5.7.4, on any coin and timeframe: its S/R levels "
                       "with the % chance price reaches each within the forecast window, the Fib ladder with odds, "
                       "its latest signals (B+/B- divergence, U/Dn trend, B+?/B-? early warnings) and how they "
                       "resolved, its chart patterns (double/triple tops and bottoms, head and shoulders, triangles, "
                       "wedges, flags: break-out level, invalidation, measured target) and harmonic patterns "
                       "(Gartley, Bat, Butterfly, Crab, Shark, 5-0, AB=CD...: PRZ, TP1/TP2, invalidation), its "
                       "forecast (direction, range, next-candle call) and its signal stats. Use it whenever the user "
                       "mentions Kimi.",
        "parameters": {
            "type": "object", "additionalProperties": False, "required": ["symbol", "timeframe"],
            "properties": {
                "symbol": {"type": "string", "description": "USDT pair, e.g. ETHUSDT"},
                "timeframe": {"type": "string", "enum": list(INTERVALS)},
            },
        },
    },
    {
        "name": FINAL_TOOL,
        "description": "Final step, call exactly once: the plan for what to draw, remove, alert on, which chart to "
                       "switch to, which indicators to toggle, whether to scan the watchlist or build a trade plan.",
        "parameters": INTENT_SCHEMA,
    },
]

LOOP_SYSTEM = (
    "You are the planning step of a crypto charting agent. {chart} You can call look_at_chart, scan_watchlist, "
    "scan_market, read_kimi and market_context to gather facts (at most {steps} calls in total), then you must call "
    "draw_on_chart exactly once with the plan for what to show. A question that isn't about a chart (today's date, "
    "FOMC or CPI results, coin upgrades, what something means) needs no looking: call draw_on_chart straight away "
    "with general_question true, no features and keep_existing true. Only look first when the answer depends on "
    "something you can't see yet: another timeframe ('does the daily agree?'), another coin ('compare with ETH'), "
    "several coins, funding/open interest, or the user's Kimi Cooked indicator on another coin or timeframe. For a "
    "simple request call draw_on_chart straight away.\n\nHow to fill draw_on_chart: " + INTENT_SYSTEM
)


@dataclass
class Toolbox:
    """The read-only tools, bound to this request's market data."""

    look: Callable[[str, str, list[str]], Awaitable[dict]]
    scan: Callable[[str, str], Awaitable[list[dict]]]
    context: Callable[[str], Awaitable[dict]]
    kimi: Callable[[str, str], Awaitable[dict]] | None = None
    market_scan: Callable[[str, str], Awaitable[dict]] | None = None


@dataclass
class LoopResult:
    """What the planner did. `intent` is None when it looked but never drew: its research is kept and the
    single-shot planner makes the plan."""

    intent: AnalysisIntent | None
    engine: str
    steps: list[str] = field(default_factory=list)
    research: list[dict] = field(default_factory=list)


def _clip(obj: Any) -> str:
    """The tool result as JSON of at most MAX_RESULT_CHARS, still valid: the longest lists lose items from the end
    (and long strings are cut) until it fits."""
    def dump(o: Any) -> str:
        return json.dumps(o, default=float, separators=(",", ":"))

    text = dump(obj)
    if len(text) <= MAX_RESULT_CHARS:
        return text
    obj = json.loads(text)  # a plain copy to trim

    def lists(o: Any) -> list[list]:
        found = []
        if isinstance(o, list):
            found.append(o)
            for x in o:
                found += lists(x)
        elif isinstance(o, dict):
            for x in o.values():
                found += lists(x)
        return found

    while len(text := dump(obj)) > MAX_RESULT_CHARS:
        trimmable = [lst for lst in lists(obj) if len(lst) > 1]
        if not trimmable:
            break
        max(trimmable, key=lambda lst: len(dump(lst))).pop()
    if len(text) > MAX_RESULT_CHARS:  # one huge item: keep the keys, cut the strings
        def cut(o: Any) -> Any:
            if isinstance(o, str):
                return o[:200]
            if isinstance(o, list):
                return [cut(x) for x in o]
            if isinstance(o, dict):
                return {k: cut(v) for k, v in o.items()}
            return o
        text = dump(cut(obj))
        if len(text) > MAX_RESULT_CHARS:
            text = dump({"error": "result too large to show"})
    return text


def _pair(raw: Any, chart: ChartContext) -> tuple[str, str | None]:
    """The coin a tool call names, as a Binance pair ('INJ', 'BTC/USDT', 'solana' → INJUSDT, BTCUSDT, SOLUSDT),
    and an error instead when there is none to look up: looking up a bad pair would give demo candles or fail."""
    text = str(raw or chart.symbol).strip()
    parts = text.upper().split("/")
    ratio = len(parts) == 2 and all(len(p) > 4 and p.endswith(QUOTES) for p in parts)  # ETHUSDT/BTCUSDT, not ETH/USDT
    if ratio or text.upper().startswith("INDEX:"):
        return text, f"{text} is built in the browser and can't be looked up; use a USDT pair like ETHUSDT"
    sym = find_symbol(text) or norm_symbol(text)
    if not sym.isalnum() or (chart.listed and sym.endswith("USDT") and sym not in chart.listed):
        return sym, f"unknown symbol {text}; use a USDT pair like INJUSDT"
    return sym, None


async def _run_tool(box: Toolbox, name: str, args: dict, chart: ChartContext) -> tuple[Any, str]:
    result, step = await _call_tool(box, name, args, chart)
    # A live chart (Binance's pair list is at hand) never reasons from demo candles a bad lookup fell back to.
    if chart.listed is not None and isinstance(result, dict) and result.get("data_source") == "synthetic":
        return {"error": f"no live data for {result.get('symbol') or args.get('symbol')}"}, ""
    return result, step


async def _call_tool(box: Toolbox, name: str, args: dict, chart: ChartContext) -> tuple[Any, str]:
    if name in ("look_at_chart", "read_kimi", "market_context"):
        sym, error = _pair(args.get("symbol"), chart)
        if error:
            return {"error": error}, ""
    if name == "look_at_chart":
        tf = args.get("timeframe") if args.get("timeframe") in INTERVALS else chart.interval
        feats = [f for f in args.get("features") or [] if f in ALL_FEATURES] or ["support_resistance"]
        return await box.look(sym, tf, feats), f"Looked at {sym} on {tf}"
    if name == "scan_watchlist":
        tf = args.get("timeframe") if args.get("timeframe") in INTERVALS else chart.interval
        filt = args.get("filter") if args.get("filter") in SCAN_FILTERS else "any"
        rows = await box.scan(tf, filt)
        return rows, f"Scanned {len(rows)} watchlist coins on {tf}"
    if name == "scan_market" and box.market_scan is not None:
        tf = args.get("timeframe") if args.get("timeframe") in INTERVALS else chart.interval
        direction = args.get("direction") if args.get("direction") in ("long", "short") else "any"
        res = await box.market_scan(tf, direction)
        return res, f"Scanned the top {res.get('coins', 0)} coins for {tf} setups"
    if name == "read_kimi" and box.kimi is not None:
        tf = args.get("timeframe") if args.get("timeframe") in INTERVALS else chart.interval
        return await box.kimi(sym, tf), f"Read Kimi Cooked on {sym} {tf}"
    if name == "market_context":
        return await box.context(sym), f"Checked futures data, order flow and upcoming events for {sym}"
    return {"error": f"unknown tool {name}"}, ""


async def plan_with_tools(llm: LLMClient, prompt: str, history: list[ChatTurn], overlays: list,
                          previous: AnalysisIntent | None, chart: ChartContext, box: Toolbox,
                          max_steps: int = 4) -> LoopResult | None:
    context = llm.context_block(history, overlays, previous, chart)
    messages: list[dict] = [{"role": "user", "text": f"{context}\n\nNEW REQUEST: {prompt}" if context else prompt}]
    spot = " The user trades spot only: no shorts, futures or leverage." if chart.spot_only else ""
    system = LOOP_SYSTEM.format(chart=f"The user is looking at {chart.symbol} on {chart.interval}.{spot}",
                                steps=max_steps)
    steps: list[str] = []
    research: list[dict] = []
    calls_made = 0
    try:
        for round_no in range(max_steps + 1):
            force = FINAL_TOOL if calls_made >= max_steps else "any"
            turn = await llm.tool_turn(system, messages, TOOLS, force=force)
            if not turn.calls:
                log.info("Planner answered without a tool call; falling back")
                break
            messages.append({"role": "assistant", "text": turn.text, "calls": turn.calls})
            final = next((c for c in turn.calls if c.name == FINAL_TOOL), None)
            if final:
                intent = AnalysisIntent.model_validate(final.args)
                return LoopResult(intent, f"{llm.provider}:{llm.model} (tools)", steps, research)
            for call in turn.calls:
                calls_made += 1
                try:
                    result, step = await _run_tool(box, call.name, call.args, chart)
                except Exception as exc:  # one failed look (too few candles, Binance down) doesn't end the plan
                    log.info("Planner tool %s failed: %s", call.name, exc)
                    result, step = {"error": f"{call.name} failed: {exc}"}, ""
                if step:
                    steps.append(step)
                    research.append({"tool": call.name, "args": call.args, "result": result})
                messages.append({"role": "tool", "id": call.id, "name": call.name, "content": _clip(result)})
            log.debug("Planner round %d: %s", round_no, [c.name for c in turn.calls])
    except (httpx.HTTPError, ValidationError, ValueError, KeyError, TypeError) as exc:
        log.warning("Tool planner failed (%s: %s); using the single-shot planner", type(exc).__name__, exc)
        llm.note_failure(exc)
    # It looked but never drew: keep what it found, and let the single-shot planner make the plan.
    return LoopResult(None, "", steps, research) if research else None
