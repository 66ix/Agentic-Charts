"""Agent orchestration: prompt → plan → data → TA detectors → merge with the chart → narrated answer.

Each request carries the conversation so far, the AI overlays already on the chart and the user's
watchlist, so follow-ups work: "also show swings" adds, "remove the trendline" removes, "same on daily"
reuses the previous plan, "line at 25.4" draws the user's own level, "alert me if it enters the supply
zone" returns alerts for the client to arm, "open ETH daily" moves the chart, "which of my coins are near
demand?" scans the watchlist and "give me a long setup" builds a trade plan from the detected zones.

With an LLM configured the plan comes from a tool-calling loop (agent_loop.py) that can look at other
timeframes and coins first; otherwise from the single-shot planner or the rule parser.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid

from .agent_loop import Toolbox, plan_with_tools
from .config import get_settings
from .derivatives import DerivativesService
from .events import EventsService
from .futures_data import FuturesDataService
from .grid_planner import GridPlanRequest, describe_plan, plan_facts, plan_grid
from .gridbot import GridBotService
from .kimi_service import KimiService, summarize
from .llm import ChartContext, LLMClient
from .market_data import MarketData, candles_to_df
from .scanner import DEFAULT_WATCHLIST, scan, tickers
from .schemas import (
    FEATURE_KINDS,
    TARGET_KINDS,
    AlertSpec,
    AnalysisIntent,
    AnalyzeRequest,
    AnalyzeResponse,
    BoxOverlay,
    HorizontalLineOverlay,
    Navigate,
    is_custom_symbol,
)
from .ta_agent import _fmt, analyze, describe, higher_timeframes, rgba
from .trade_plan import build_plan, plan_overlays

CUSTOM_COLOR = "#a78bfa"
INDICATOR_NAMES = {"kimi": "Kimi Cooked", "ema20": "EMA 20", "ema50": "EMA 50", "psar": "Parabolic SAR",
                   "volume": "volume"}
MAX_ALERTS = 10


log = logging.getLogger(__name__)


def _new_id() -> str:
    return f"ai-{uuid.uuid4().hex[:8]}"


def _kinds(targets: list[str]) -> frozenset[str] | None:
    """Overlay kinds for target groups; None means everything."""
    if "all" in targets:
        return None
    out: frozenset[str] = frozenset()
    for t in targets:
        out |= TARGET_KINDS.get(t, frozenset())
    return out


def _matches(overlay, kinds: frozenset[str] | None) -> bool:
    return kinds is None or (overlay.kind or "") in kinds


def custom_overlays(intent: AnalysisIntent) -> list:
    out: list = []
    for c in intent.custom_levels:
        if c.kind == "zone":
            out.append(BoxOverlay(
                id=_new_id(), kind="custom_zone", label=c.label or f"Zone {_fmt(c.price_low, strip=True)}–{_fmt(c.price_high, strip=True)}",
                price_low=c.price_low, price_high=c.price_high, color=rgba(CUSTOM_COLOR, 0.16),
                border_color=rgba(CUSTOM_COLOR, 0.75),
            ))
        else:
            out.append(HorizontalLineOverlay(
                id=_new_id(), kind="custom_level", label=c.label or f"Level {_fmt(c.price, strip=True)}", price=c.price,
                color=CUSTOM_COLOR, line_style="dashed", line_width=2,
            ))
    return out


def merge_overlays(existing: list, new: list, intent: AnalysisIntent) -> tuple[list, int]:
    """What stays on the chart: (overlays, number removed).

    An answer that draws nothing new (a question about Kimi, a scan, alerts, an indicator toggle) keeps everything
    on the chart; only a fresh analysis replaces the earlier detector output.
    """
    if intent.keep_existing or not intent.features:
        kept = list(existing)
    else:  # a fresh analysis replaces detector output but keeps levels the user asked for by price
        kept = [o for o in existing if (o.kind or "") in TARGET_KINDS["custom"]]
    rerun = frozenset().union(*(FEATURE_KINDS[f] for f in intent.features)) if intent.features else frozenset()
    kept = [o for o in kept if (o.kind or "") not in rerun]
    before = len(kept)
    if intent.remove:
        kinds = _kinds(intent.remove)
        kept = [o for o in kept if not _matches(o, kinds)]
    return kept + new, before - len(kept)


NO_ALERT_KINDS = {"plan_risk", "plan_reward", "pattern_point", "swing_high", "swing_low"}


def build_alerts(intent: AnalysisIntent, overlays: list, new: list) -> list[AlertSpec]:
    alerts = [AlertSpec(kind="cross", price=p, label=f"Price {_fmt(p, strip=True)}") for p in intent.alert_prices]
    if intent.alert_targets:
        pool = new if intent.alert_targets == ["new"] else overlays
        kinds = None if "new" in intent.alert_targets else _kinds(intent.alert_targets)
        for o in pool:
            if not _matches(o, kinds) or (o.kind or "") in NO_ALERT_KINDS:
                continue
            if o.type == "box":
                alerts.append(AlertSpec(kind="zone", price_low=o.price_low, price_high=o.price_high, label=o.label))
            elif o.type == "horizontal_line":
                alerts.append(AlertSpec(kind="cross", price=o.price, label=o.label))
    return alerts[:MAX_ALERTS]


def _action_lines(intent: AnalysisIntent, custom: list, removed: int, alerts: list[AlertSpec]) -> list[str]:
    lines = []
    for o in custom:
        if o.type == "box":
            lines.append(f"Drew {o.label}." if o.label.startswith("Zone")
                         else f"Drew {o.label} at {_fmt(o.price_low, strip=True)}–{_fmt(o.price_high, strip=True)}.")
        else:
            lines.append(f"Drew a level at {_fmt(o.price, strip=True)}." if o.label.startswith("Level")
                         else f"Drew {o.label} at {_fmt(o.price, strip=True)}.")
    if intent.remove:
        lines.append(f"Removed {removed} overlay{'s' if removed != 1 else ''}." if removed
                     else "Nothing matching was on the chart to remove.")
    if alerts:
        names = ", ".join(a.label for a in alerts[:4]) + (" and more" if len(alerts) > 4 else "")
        lines.append(f"Set {len(alerts)} alert{'s' if len(alerts) != 1 else ''}: {names}.")
    elif intent.alert_prices or intent.alert_targets:
        lines.append("Found no matching level to alert on; ask me to find it first.")
    return lines


def _compact(facts: dict) -> dict:
    """Facts trimmed for a tool result: no overlays, no bar timestamps."""
    return {k: v for k, v in facts.items() if k not in ("last_bar_time",)}


def make_toolbox(market: MarketData, derivatives: DerivativesService | None, watchlist: list[str],
                 kimi: KimiService | None = None, futures: FuturesDataService | None = None,
                 events: EventsService | None = None) -> Toolbox:
    async def look(symbol: str, tf: str, features: list[str]) -> dict:
        candles, source = await market.get_klines(symbol, tf, 400)
        frames = await _confluence_frames(market, symbol, tf, features)
        res = await asyncio.to_thread(analyze, candles_to_df(candles), AnalysisIntent(features=features), tf, None,
                                      frames)
        return {"symbol": symbol, "data_source": source, **_compact(res.facts)}

    async def scan_tool(tf: str, filt: str) -> list[dict]:
        rows = await scan(market, watchlist or DEFAULT_WATCHLIST, tf, filt)
        return [r.model_dump(exclude={"interval", "data_source"}) for r in rows[:10]]

    async def context(symbol: str) -> dict:
        tick, deriv, fut, upcoming = await asyncio.gather(
            tickers(market, [symbol]), derivatives.symbol_snapshot(symbol) if derivatives else _none(),
            _guarded(futures.futures_context(symbol), "futures context") if futures else _none(),
            _upcoming(events, 24))
        out: dict = {"symbol": symbol}
        if tick:
            out.update(price=tick[0]["price"], change_24h_pct=tick[0]["change_pct"])
        out["futures"] = fut or deriv or "unavailable"
        if upcoming is not None:
            out["upcoming_events"] = upcoming
        return out

    async def read_kimi(symbol: str, tf: str) -> dict:
        if kimi is None:
            return {"error": "Kimi Cooked is not available"}
        return {"data_source": (k := await kimi.get(symbol, tf)).data_source, **summarize(k)}

    return Toolbox(look=look, scan=scan_tool, context=context, kimi=read_kimi if kimi else None)


async def _none() -> None:
    return None


async def _empty() -> list:
    return []


async def _guarded(coro, what: str, seconds: float = 8.0):
    """Extra context the answer can go out without: None when it fails or is slow."""
    try:
        return await asyncio.wait_for(coro, seconds)
    except Exception as exc:
        log.info("Skipped %s: %s", what, exc)
        return None


FUTURES_WORDS = re.compile(r"\b(funding|open interest|oi|long[ /-]?short|l/s|liquidat\w*|order ?book|walls?|cvd|"
                           r"order ?flow|delta|positioning|crowded|sentiment|squeeze)\b", re.I)
EVENT_WORDS = re.compile(r"\b(news|events?|calendar|cpi|fomc|fed|nfp|payrolls|macro|data release|"
                         r"anything coming|coming up)\b", re.I)
NEWS_WORDS = re.compile(r"\b(news|headlines?|why is .* (up|down|pumping|dumping)|what happened)\b", re.I)
EVENT_HOURS = 48  # how far ahead a trade plan looks for high-impact events


async def _upcoming(events: EventsService | None, hours: int) -> list[dict] | None:
    """High-impact economic events in the next `hours`, small enough to put in the facts; None when the
    calendar can't be read (so the answer never says "nothing coming up" when it simply doesn't know)."""
    if events is None:
        return None
    cal = await _guarded(events.calendar(days=(hours + 23) // 24, impact="high"), "economic calendar")
    if not cal or cal.get("source") == "unavailable":
        return None
    now = time.time()
    return [{"in_hours": round((e["time"] - now) / 3600, 1), "title": e["title"], "country": e["country"],
             "impact": e["impact"], "forecast": e.get("forecast"), "previous": e.get("previous")}
            for e in cal["events"] if now <= e["time"] <= now + hours * 3600][:6]


async def _headlines(events: EventsService | None, symbol: str) -> list[dict]:
    if events is None:
        return []
    rows = await _guarded(events.headlines(symbol, 24, 6), "news") or []
    now = time.time()
    return [{"hours_ago": round((now - h["time"]) / 3600, 1), "title": h["title"], "source": h["source"]}
            for h in rows]


def _event_note(upcoming: list[dict] | None) -> str | None:
    """A trade-plan warning for the first high-impact event coming up."""
    if not upcoming:
        return None
    e = upcoming[0]
    when = "within the hour" if e["in_hours"] < 1 else f"in {e['in_hours']:.0f}h"
    more = f" (+{len(upcoming) - 1} more in {EVENT_HOURS}h)" if len(upcoming) > 1 else ""
    return f"Heads-up: {e['country']} {e['title']} {when}{more}. High-impact news can run through stops."


async def _confluence_frames(market: MarketData, symbol: str, tf: str, features: list[str]) -> dict:
    """Higher-timeframe frames for zone confluence, when zones are being detected."""
    if not {"support_resistance", "supply_demand"} & set(features):
        return {}
    htfs = higher_timeframes(tf)
    results = await asyncio.gather(*(market.get_klines(symbol, h, 300) for h in htfs), return_exceptions=True)
    return {h: candles_to_df(r[0]) for h, r in zip(htfs, results)
            if not isinstance(r, BaseException) and len(r[0]) >= 60}


KIMI_WORDS = re.compile(r"\bkimi\b", re.I)


async def _kimi_facts(kimi: KimiService | None, symbol: str, tf: str) -> dict | None:
    if kimi is None:
        return None
    try:
        return summarize(await kimi.get(symbol, tf))
    except Exception as exc:  # the answer goes out without it
        log.warning("Kimi Cooked failed for %s %s: %s", symbol, tf, exc)
        return {"error": f"Kimi Cooked could not run: {exc}"}


async def _grid_plan(gridbots: GridBotService | None, symbol: str):
    """A grid bot plan for "plan a grid bot on INJ" (grid_planner.py); None when it cannot be made."""
    if gridbots is None:
        return None
    try:
        return await plan_grid(gridbots, GridPlanRequest(symbol=symbol))
    except Exception as exc:  # the answer goes out without it and says so
        log.warning("Grid plan for %s failed: %s", symbol, exc)
        return None


async def run_analysis(req: AnalyzeRequest, market: MarketData, llm: LLMClient,
                       derivatives: DerivativesService | None = None, kimi: KimiService | None = None,
                       futures: FuturesDataService | None = None,
                       events: EventsService | None = None,
                       gridbots: GridBotService | None = None) -> AnalyzeResponse:
    settings = get_settings()
    chart = ChartContext(req.symbol, req.interval, req.watchlist)
    steps: list[str] = []
    research: list[dict] = []
    loop = None
    if req.prompt.strip() and llm.available() and settings.agent_mode == "tools":
        box = make_toolbox(market, derivatives, req.watchlist, kimi, futures, events)
        loop = await plan_with_tools(llm, req.prompt, req.history, req.overlays, req.previous_intent, chart, box,
                                     settings.agent_max_steps)
    if loop:
        intent, intent_engine, steps, research = loop.intent, loop.engine, loop.steps, loop.research
    else:
        intent, intent_engine = await llm.parse_intent(req.prompt, req.history, req.overlays, req.previous_intent,
                                                       chart)

    # Where to look, and whether the chart moves there. Another coin always moves the chart; another
    # timeframe only when the user asked to switch ("H4 supply" on a 1h chart is drawn on the 1h chart).
    symbol = intent.symbol or req.symbol
    tf = intent.timeframe or req.interval
    # A ratio or index chart is built in the browser: analyse the candles it sent, on its own timeframe only.
    custom_chart = is_custom_symbol(symbol)
    if custom_chart:
        tf = req.interval
    moving = symbol != req.symbol or (intent.switch_chart and not custom_chart)
    chart_interval = tf if moving else req.interval
    navigate = Navigate(symbol=symbol, interval=chart_interval) if (symbol, chart_interval) != (
        req.symbol, req.interval) else None
    existing = [] if navigate else req.overlays

    features = list(intent.features)
    if intent.trade_plan and not {"support_resistance", "supply_demand"} & set(features):
        features += ["support_resistance", "supply_demand"]
    run_intent = intent.model_copy(update={"features": features})

    async def candles_for_chart():
        if req.candles and symbol == req.symbol and tf == req.interval and len(req.candles) >= 30:
            return req.candles, "client"
        if custom_chart:
            raise ValueError("This chart is still loading; ask again once its candles are on screen.")
        return await market.get_klines(symbol, tf, req.limit)

    async def windows() -> dict:
        if "window_levels" not in features or custom_chart:
            return {}
        res = await asyncio.gather(*(market.get_klines(symbol, w, 5) for w in intent.window_timeframes),
                                   return_exceptions=True)
        return {w: candles_to_df(r[0]) for w, r in zip(intent.window_timeframes, res) if not isinstance(r, BaseException)}

    async def scan_rows():
        if not intent.scan_watchlist:
            return []
        return await scan(market, req.watchlist or DEFAULT_WATCHLIST, tf, intent.scan_filter)

    want_deriv = derivatives is not None and bool(req.prompt.strip()) and not custom_chart
    # The user's own indicator: read it when they name it or switch it on.
    want_kimi = ("kimi" in intent.indicators_on or bool(KIMI_WORDS.search(req.prompt))) and not custom_chart
    # Futures and order flow when asked about them or building a plan; the calendar for plans and "any news?".
    want_futures = futures is not None and not custom_chart and (
        bool(intent.trade_plan) or bool(FUTURES_WORDS.search(req.prompt)))
    want_events = bool(intent.trade_plan) or bool(EVENT_WORDS.search(req.prompt))
    want_news = bool(NEWS_WORDS.search(req.prompt)) and not custom_chart
    want_grid = intent.grid_plan and not custom_chart
    (candles, source), higher, frames, rows, deriv, kimi_facts, fut, upcoming, headlines, grid = await asyncio.gather(
        candles_for_chart(), windows(),
        _confluence_frames(market, symbol, tf, [] if custom_chart else features), scan_rows(),
        derivatives.symbol_snapshot(symbol) if want_deriv else _none(),
        _kimi_facts(kimi, symbol, tf) if want_kimi else _none(),
        _guarded(futures.futures_context(symbol), "futures context") if want_futures else _none(),
        _upcoming(events, EVENT_HOURS) if want_events else _none(),
        _headlines(events, symbol) if want_news else _empty(),
        _grid_plan(gridbots, symbol) if want_grid else _none())

    df = candles_to_df(candles)
    # Detection is CPU-bound (SciPy); keep the event loop free for streams.
    result = await asyncio.to_thread(analyze, df, run_intent, tf, higher, frames)
    facts = dict(result.facts)

    for ov in result.overlays:
        ov.id = _new_id()
    plan = None
    plan_ovs: list = []
    if intent.trade_plan:
        plan = build_plan(intent.trade_plan, result.stats.last_price, result.stats.atr, result.stats.trend,
                          result.levels, result.swing_lows, result.swing_highs, result.bias)
        if plan and (note := _event_note(upcoming)):
            plan.notes.append(note)
        facts["plan"] = plan.model_dump() if plan else None
        if plan:
            plan_ovs = plan_overlays(plan, int(df["time"].iloc[-1]))
            for ov in plan_ovs:
                ov.id = _new_id()
    custom = custom_overlays(intent)
    new = result.overlays + plan_ovs + custom
    if plan_ovs:  # a new plan replaces the previous one
        existing = [o for o in existing if (o.kind or "") not in TARGET_KINDS["plan"]]
    overlays, removed = merge_overlays(existing, new, run_intent)
    alerts = build_alerts(intent, overlays, new)

    if deriv:
        facts["derivatives"] = deriv
    if kimi_facts:
        facts["kimi"] = kimi_facts
    if fut:
        facts["futures_context"] = fut
    if upcoming is not None:
        facts["upcoming_events"] = upcoming  # [] says "nothing high-impact coming", which is worth saying too
    if headlines:
        facts["headlines"] = headlines
    if want_grid:
        facts["grid_plan"] = plan_facts(grid) if grid else None
    if intent.scan_watchlist:
        facts["scan"] = [r.model_dump(include={"symbol", "last_price", "change_pct", "trend", "rsi", "signals",
                                               "nearest_kind", "distance_pct"}) for r in rows[:6]]
    if research:
        facts["research"] = [{"step": s, "result": r["result"]} for s, r in zip(steps, research)]
    nav = []
    if navigate:
        nav.append(f"Switched the chart to {navigate.symbol} {navigate.interval}.")
    toggles = {**{k: True for k in intent.indicators_on}, **{k: False for k in intent.indicators_off}}
    actions = _action_lines(intent, custom, removed, alerts)
    show_kimi = want_kimi and "kimi" not in toggles  # asked about it: show it on the chart too, quietly
    if toggles:
        on = [INDICATOR_NAMES.get(k, k.upper()) for k, v in toggles.items() if v]
        off = [INDICATOR_NAMES.get(k, k.upper()) for k, v in toggles.items() if not v]
        actions.append(" ".join(filter(None, [f"Turned on {', '.join(on)}." if on else "",
                                              f"Turned off {', '.join(off)}." if off else ""])))
    if nav:
        facts["navigation"] = nav
    if actions:
        facts["actions"] = actions
    narrate_facts = {k: v for k, v in facts.items() if k != "last_bar_time"}
    fallback = describe(narrate_facts, symbol)
    if want_grid:  # the grid plan leads the answer; the chart summary follows
        fallback = (describe_plan(grid) if grid else "Couldn't plan a grid bot for this coin right now.") + " " + \
            fallback
    summary, narrate_engine = await llm.narrate(req.prompt, narrate_facts, fallback, req.history)

    return AnalyzeResponse(
        symbol=symbol,
        interval=chart_interval,
        analysis_interval=tf,
        overlays=overlays,
        summary=summary,
        intent=intent,
        stats=result.stats,
        engine={"intent": intent_engine, "summary": narrate_engine, "detector": "scipy"},
        data_source=source,
        alerts=alerts,
        navigate=navigate,
        indicators={**toggles, **({"kimi": True} if show_kimi else {})},
        scan=rows[:10],
        plan=plan,
        grid_plan=grid.model_dump() if grid else None,
        steps=steps,
    )
