"""Agent orchestration: prompt → intent → data → TA detectors → merge with the chart → narrated answer.

Each request carries the conversation so far and the AI overlays already on the
chart, so follow-ups work: "also show swings" adds, "remove the trendline"
removes, "same on daily" reuses the previous plan, "line at 25.4" draws the
user's own level, and "alert me if it enters the supply zone" returns alerts
for the client to arm.
"""

from __future__ import annotations

import asyncio
import uuid

from .llm import LLMClient
from .market_data import MarketData, candles_to_df
from .schemas import (
    FEATURE_KINDS,
    TARGET_KINDS,
    AlertSpec,
    AnalysisIntent,
    AnalyzeRequest,
    AnalyzeResponse,
    BoxOverlay,
    HorizontalLineOverlay,
)
from .ta_agent import _fmt, analyze, describe, rgba

CUSTOM_COLOR = "#a78bfa"
MAX_ALERTS = 10


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
                id=_new_id(), kind="custom_zone", label=c.label or f"Zone {_fmt(c.price_low)}–{_fmt(c.price_high)}",
                price_low=c.price_low, price_high=c.price_high, color=rgba(CUSTOM_COLOR, 0.16),
                border_color=rgba(CUSTOM_COLOR, 0.75),
            ))
        else:
            out.append(HorizontalLineOverlay(
                id=_new_id(), kind="custom_level", label=c.label or f"Level {_fmt(c.price)}", price=c.price,
                color=CUSTOM_COLOR, line_style="dashed", line_width=2,
            ))
    return out


def merge_overlays(existing: list, new: list, intent: AnalysisIntent) -> tuple[list, int]:
    """What stays on the chart: (overlays, number removed)."""
    if intent.keep_existing:
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


def build_alerts(intent: AnalysisIntent, overlays: list, new: list) -> list[AlertSpec]:
    alerts = [AlertSpec(kind="cross", price=p, label=f"Price {_fmt(p)}") for p in intent.alert_prices]
    if intent.alert_targets:
        pool = new if intent.alert_targets == ["new"] else overlays
        kinds = None if "new" in intent.alert_targets else _kinds(intent.alert_targets)
        for o in pool:
            if not _matches(o, kinds):
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
                         else f"Drew {o.label} at {_fmt(o.price_low)}–{_fmt(o.price_high)}.")
        else:
            lines.append(f"Drew a level at {_fmt(o.price)}." if o.label.startswith("Level")
                         else f"Drew {o.label} at {_fmt(o.price)}.")
    if intent.remove:
        lines.append(f"Removed {removed} overlay{'s' if removed != 1 else ''}." if removed
                     else "Nothing matching was on the chart to remove.")
    if alerts:
        names = ", ".join(a.label for a in alerts[:4]) + (" and more" if len(alerts) > 4 else "")
        lines.append(f"Set {len(alerts)} alert{'s' if len(alerts) != 1 else ''}: {names}.")
    elif intent.alert_prices or intent.alert_targets:
        lines.append("Found no matching level to alert on; ask me to find it first.")
    return lines


async def run_analysis(req: AnalyzeRequest, market: MarketData, llm: LLMClient) -> AnalyzeResponse:
    intent, intent_engine = await llm.parse_intent(req.prompt, req.history, req.overlays, req.previous_intent)
    tf = intent.timeframe or req.interval

    if req.candles and tf == req.interval and len(req.candles) >= 30:
        candles, source = req.candles, "client"
    else:
        candles, source = await market.get_klines(req.symbol, tf, req.limit)

    higher: dict = {}
    if "window_levels" in intent.features:
        results = await asyncio.gather(
            *(market.get_klines(req.symbol, wtf, 5) for wtf in intent.window_timeframes), return_exceptions=True
        )
        for wtf, res in zip(intent.window_timeframes, results):
            if not isinstance(res, BaseException):
                higher[wtf] = candles_to_df(res[0])

    df = candles_to_df(candles)
    # Detection is CPU-bound (SciPy); keep the event loop free for streams.
    result = await asyncio.to_thread(analyze, df, intent, tf, higher)

    for ov in result.overlays:
        ov.id = _new_id()
    custom = custom_overlays(intent)
    new = result.overlays + custom
    overlays, removed = merge_overlays(req.overlays, new, intent)
    alerts = build_alerts(intent, overlays, new)

    facts = dict(result.facts)
    actions = _action_lines(intent, custom, removed, alerts)
    if actions:
        facts["actions"] = actions
    fallback = describe(facts, req.symbol)
    summary, narrate_engine = await llm.narrate(req.prompt, facts, fallback, req.history)

    return AnalyzeResponse(
        symbol=req.symbol,
        interval=req.interval,
        analysis_interval=tf,
        overlays=overlays,
        summary=summary,
        intent=intent,
        stats=result.stats,
        engine={"intent": intent_engine, "summary": narrate_engine, "detector": "scipy"},
        data_source=source,
        alerts=alerts,
    )
