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
from collections.abc import Awaitable, Callable
from typing import Any

import pandas as pd

from . import general
from .agent_loop import LoopResult, Toolbox, plan_with_tools
from .config import get_settings
from .derivatives import DerivativesService
from .dip_ladder import LadderRequest, LadderResult, describe_ladder, ladder_facts, plan_ladder
from .events import EventsService
from .futures_data import FuturesDataService
from .grid_planner import GridPlanRequest, describe_plan, plan_grid
from .grid_planner import plan_facts as grid_plan_facts
from .gridbot import GridBotService
from .kimi_service import KimiService, summarize
from .llm import ChartContext, LLMClient, rule_route
from .market_data import FALLBACK_SYMBOLS, INTERVAL_SECONDS, MarketData, candles_to_df
from .market_metrics import MarketMetricsService, overview_facts
from .market_scanner import MarketScanner, MarketScanResult
from .scanner import DEFAULT_WATCHLIST, scan, tickers
from .sell_check import describe_sells, sell_facts, sell_scan, timeframe_for
from .pricefmt import round_facts
from .relative import vs_btc
from .session_levels import SessionLevelsService, clock_facts, level_facts
from .schemas import (
    FEATURE_KINDS,
    TARGET_KINDS,
    AlertSpec,
    MetricAlertSpec,
    AnalysisIntent,
    AnalyzeRequest,
    AnalyzeResponse,
    Focus,
    IndicatorLengths,
    BoxOverlay,
    HorizontalLineOverlay,
    MarketSetup,
    Navigate,
    PastAnswer,
    SellWatch,
    ZoneTriggerSpec,
    is_custom_symbol,
)
from .ta_agent import (GREEN, ORANGE, TEAL, ZONE_BARS, _fmt, analyze, atr, describe, find_swings, higher_timeframes,
                       htf_readings, htf_zones, price_check, rgba)
from .exit_plan import describe_exit_plan, plan_from_market, take_profits
from .focus import focus_line, plan_in_focus
from .suggest import next_steps
from .focus import usable as focus_usable
from .top_down import TopDownResult, describe_walk, walk, walk_facts
from .trade_plan import build_plan, plan_overlays
from .level_review import LevelLog
from .metric_alerts import MetricAlertService
from .metric_alerts import describe as describe_metric_alert
from .zone_triggers import describe_trigger, detect_now, spec_from_intent

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
    """Facts trimmed for a tool result: no overlays, no bar timestamps, and the indicator readings last so a long
    result is clipped there rather than in the zones."""
    out = {k: v for k, v in facts.items() if k not in ("last_bar_time", "indicators")}
    if "indicators" in facts:
        out["indicators"] = facts["indicators"]
    return out


def make_toolbox(market: MarketData, derivatives: DerivativesService | None, watchlist: list[str],
                 kimi: KimiService | None = None, futures: FuturesDataService | None = None,
                 events: EventsService | None = None, scanner: MarketScanner | None = None,
                 spot_only: bool = False, metrics: MarketMetricsService | None = None,
                 lengths: IndicatorLengths | None = None) -> Toolbox:
    async def look(symbol: str, tf: str, features: list[str]) -> dict:
        candles, source = await market.get_klines(symbol, tf, 400)
        frames = await _confluence_frames(market, symbol, tf, features)
        res = await asyncio.to_thread(analyze, candles_to_df(candles), AnalysisIntent(features=features), tf, None,
                                      frames, lengths)
        return {"symbol": symbol, "data_source": source, **_compact(res.facts)}

    async def scan_tool(tf: str, filt: str) -> list[dict]:
        rows = await scan(market, watchlist or DEFAULT_WATCHLIST, tf, filt)
        return [r.model_dump(exclude={"interval", "data_source"}) for r in rows[:10]]

    async def context(symbol: str) -> dict:
        tick, deriv, fut, upcoming, overview = await asyncio.gather(
            tickers(market, [symbol]), derivatives.symbol_snapshot(symbol) if derivatives else _none(),
            _guarded(futures.futures_context(symbol), "futures context") if futures else _none(),
            _upcoming(events, 24), _market_overview(metrics))
        out: dict = {"symbol": symbol}
        if tick:
            out.update(price=tick[0]["price"], change_24h_pct=tick[0]["change_pct"])
        out["futures"] = fut or deriv or "unavailable"
        if upcoming is not None:
            out["upcoming_events"] = upcoming
        if overview:
            out["whole_market"] = overview
        return out

    async def read_kimi(symbol: str, tf: str) -> dict:
        if kimi is None:
            return {"error": "Kimi Cooked is not available"}
        return {"data_source": (k := await kimi.get(symbol, tf)).data_source, **summarize(k)}

    async def market_scan_tool(tf: str, direction: str) -> dict:
        if scanner is None:
            return {"error": "the market scanner is not available"}
        try:
            res = await asyncio.wait_for(scanner.fresh_or_run(tf, scan_max_age(tf)), MARKET_SCAN_WAIT)
        except asyncio.TimeoutError:
            return {"error": "the market scan is still running; its results will be ready in a minute"}
        except Exception as exc:  # network: the planner carries on without it
            return {"error": f"the market scan failed: {exc}"}
        if spot_only:  # shorts are of no use to a spot trader: longs, and the spot buys among them
            return {**market_scan_facts(res, "long", 6), "spot_buys": spot_facts(res, 5)}
        return market_scan_facts(res, None if direction == "any" else direction, 8)

    return Toolbox(look=look, scan=scan_tool, context=context, kimi=read_kimi if kimi else None,
                   market_scan=market_scan_tool if scanner else None)


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


async def _market_overview(metrics: MarketMetricsService | None) -> dict | None:
    """The market header bar: total market cap, 24h volume, liquidations, open interest, Fear & Greed and BTC
    dominance (cached by the service, so this is usually free)."""
    if metrics is None:
        return None
    res = await _guarded(metrics.get(), "market overview", 5.0)
    return overview_facts(res) if res else None


async def _listed(market: MarketData) -> frozenset[str] | None:
    """Binance's live USDT pairs, or None when only the built-in fallback list is at hand (synthetic data, or
    exchangeInfo failed): that list is too short to rule a coin out."""
    symbols = await _guarded(market.list_symbols(), "symbol list", 5.0)
    return frozenset(symbols) if symbols and symbols is not FALLBACK_SYMBOLS else None


async def _vs_btc(market: MarketData, symbol: str) -> dict | None:
    """The coin against BTC on daily candles (relative.py): change vs BTC today and over 7 days, 30-day correlation
    and beta. None for BTC itself, pairs not quoted in a dollar stablecoin, or when the candles can't be had."""
    if symbol.startswith("BTC") or not symbol.endswith(("USDT", "USDC", "FDUSD")):
        return None
    try:
        (coin, _), (btc, _) = await asyncio.wait_for(asyncio.gather(
            market.get_klines(symbol, "1d", 60), market.get_klines("BTCUSDT", "1d", 60)), 8.0)
    except Exception as exc:  # the answer goes out without it
        log.info("Skipped vs BTC for %s: %s", symbol, exc)
        return None
    return vs_btc(candles_to_df(coin), candles_to_df(btc))


HOLDINGS_WORDS = re.compile(r"\b(?:my|our)\s+(?:holdings?|portfolio|bags?|positions?|spot coins|coins|account|"
                            r"balances?|wallet)\b|\bwhat (?:do )?i (?:hold|own)\b|\bi'?m holding\b", re.I)


async def _positions(binance: Any) -> dict | None:
    """The user's Binance holdings and positions (binance_import.positions, cached 30 s), or None without a
    read-only key or on any error."""
    if binance is None or not binance.account.status().get("configured"):
        return None
    try:
        return await asyncio.wait_for(binance.positions(), 10.0)
    except Exception as exc:  # the answer goes out without it
        log.info("Skipped Binance holdings: %s", exc)
        return None


def _spot_row(r: dict) -> dict:
    out = {"coin": r["asset"], "qty": r["own_qty"], "value_usd": r.get("value"), "price": r.get("price")}
    if r.get("avg_entry"):
        out["avg_entry"] = r["avg_entry"]
        if r.get("price"):
            out["pnl_pct"] = round((r["price"] / r["avg_entry"] - 1) * 100, 2)
        if r.get("unrealized_pnl") is not None:
            out["unrealized_pnl_usd"] = r["unrealized_pnl"]
    if r.get("untracked_qty"):
        out["qty_without_known_entry"] = r["untracked_qty"]
    return out


def _futures_row(p: dict) -> dict:
    return {k: p.get(k) for k in ("symbol", "side", "qty", "entry_price", "mark_price", "leverage",
                                  "liquidation_price", "unrealized_pnl") if p.get(k) is not None}


def held_symbols(pos: dict, min_value: float = 10.0) -> list[str]:
    """The coins you hold worth `min_value` or more (spot and Simple Earn), biggest first."""
    return [r["symbol"] for r in pos.get("manual", {}).get("holdings", [])
            if r.get("value") is not None and r["value"] >= min_value][:40]


def _held_row(h: dict) -> dict:
    """A merged holding (binance_import.merge_holdings) for the narrator: how much, where, cost and gain."""
    out = {"coin": h["asset"], "qty": h["qty"], "value_usd": h.get("value"), "price": h.get("price")}
    if h.get("earn_qty"):
        out["in_spot"], out["in_earn"] = h["spot_qty"], h["earn_qty"]
    if h.get("locked_qty"):
        when = time.strftime("%d %b", time.gmtime(h["redeem_at"])) if h.get("redeem_at") else "later"
        out["locked"] = f"{h['locked_qty']:g} locked in Earn until {when}: can't be sold before then"
    if h.get("rewards_qty"):
        out["earn_rewards_qty"] = h["rewards_qty"]
    if h.get("avg_entry"):
        out["avg_entry"] = h["avg_entry"]
        if h.get("price"):
            out["pnl_pct"] = round((h["price"] / h["avg_entry"] - 1) * 100, 2)
        if h.get("unrealized_pnl") is not None:
            out["unrealized_pnl_usd"] = h["unrealized_pnl"]
            if h.get("rewards_qty"):
                out["pnl_note"] = "includes Earn rewards, which cost nothing"
    return out


def holding_facts(pos: dict, symbol: str) -> dict | None:
    """What the user holds of `symbol` on Binance: their spot holding (with average entry from imported fills)
    and any USD-M position on it."""
    base = re.sub(r"(?:USDT|USDC|FDUSD|BUSD)$", "", symbol)
    manual = pos.get("manual", {})
    spot = next((r for r in manual.get("spot", []) if r["asset"] == base and (r.get("value") or 0) >= 5), None)
    fut = [p for p in manual.get("futures", []) if p["symbol"] == symbol]
    earn = [r for r in manual.get("earn", []) if r["asset"] == base]
    held = next((h for h in manual.get("holdings", []) if h["asset"] == base and (h.get("value") or 0) >= 5), None)
    if not spot and not fut and not earn and not held:
        return None
    out: dict = {}
    if held:
        out["held"] = _held_row(held)  # spot and Earn together
    if spot:
        out["spot"] = _spot_row(spot)
    if fut:
        out["futures"] = [_futures_row(p) for p in fut]
    if earn:
        out["earn"] = [_earn_row(r) for r in earn]
    return out


def _earn_row(r: dict) -> dict:
    """A Simple Earn position for the narrator: coins earning yield, not in the spot wallet."""
    return {k: r.get(k) for k in ("asset", "product", "qty", "value", "apr_pct", "redeem_at") if r.get(k) is not None}


def portfolio_facts(pos: dict) -> dict:
    """All the user's own holdings and positions, biggest first."""
    manual = pos.get("manual", {})
    spot = sorted((r for r in manual.get("spot", []) if (r.get("value") or 0) >= 5), key=lambda r: -(r["value"] or 0))
    cash = sum(c["qty"] for c in manual.get("cash", []))
    out: dict = {"spot_value_usd": round(sum(r["value"] for r in spot), 2), "stablecoins_usd": round(cash, 2),
                 "spot": [_spot_row(r) for r in spot[:20]]}
    held = [h for h in manual.get("holdings", []) if (h.get("value") or 0) >= 5]
    if held:  # every coin owned, spot and Earn together
        out["coins_value_usd"] = round(sum(h["value"] for h in held), 2)
        out["coins"] = [_held_row(h) for h in held[:20]]
    if manual.get("futures"):
        out["futures"] = [_futures_row(p) for p in manual["futures"][:10]]
    earn = [r for r in manual.get("earn", []) if (r.get("value") or 0) >= 1]
    if earn:
        out["earn_value_usd"] = round(sum(r["value"] for r in earn), 2)
        out["earn"] = [_earn_row(r) for r in earn[:20]]
    return out


def past_answer_facts(prev: PastAnswer, price_now: float, now: float | None = None) -> dict | None:
    """The agent's last answer on this coin (from the client's chat history) and how price moved since."""
    age_h = ((now if now is not None else time.time()) * 1000 - prev.time) / 3_600_000
    if age_h < 0.05 or not prev.summary.strip():  # the answer the user is following up on right now
        return None
    out: dict = {"when": f"{age_h:.0f} hours ago" if age_h < 48 else f"{age_h / 24:.0f} days ago",
                 "question": prev.prompt[:200], "answer": prev.summary[:500]}
    if prev.interval:
        out["timeframe"] = prev.interval
    if prev.price:
        out.update(price_then=prev.price, price_now=price_now,
                   change_since_pct=round((price_now / prev.price - 1) * 100, 2))
    return out


async def _chart_cvd(futures: FuturesDataService | None, symbol: str, tf: str) -> dict | None:
    """Spot CVD on the chart's own timeframe (the chart's CVD pane): the latest bar's and last 20 bars' taker
    delta, and so which way the running sum has moved over those 20 bars."""
    if futures is None:
        return None
    res = await _guarded(futures.cvd(symbol, tf, 100), "chart CVD", 5.0)
    rows = (res or {}).get("rows") or []
    if len(rows) < 2:
        return None
    last20 = rows[-20:]
    buy, sell = sum(r["buy"] for r in last20), sum(r["sell"] for r in last20)
    out = {"timeframe": tf, "delta_last_bar": float(f"{rows[-1]['delta']:.4g}"),
           "delta_last_20_bars": float(f"{buy - sell:.4g}"),
           "buy_pct_last_20_bars": round(buy / (buy + sell) * 100, 1) if buy + sell else None,
           "cvd_last_20_bars": "rising" if buy > sell else "falling" if buy < sell else "flat",
           "units": "coins (taker buy minus sell volume)"}
    if res.get("source") != "binance":
        out["source"] = res.get("source")
    return out


# Left out of the "numbers used" view: tool transcripts and what the answer did rather than read.
USED_SKIP = frozenset({"research", "actions", "navigation", "spot_note", "trading_style", "symbol", "coin",
                       "data_source"})
SHOW_VERBS = re.compile(r"\b(?:show|add|turn on|switch on|enable|display|plot|put|overlay|bring up|pull up|"
                        r"draw|toggle|apply|equip|load)\b", re.I)
FUTURES_WORDS = re.compile(r"\b(funding|open interest|oi|long[ /-]?short|l/s|liquidat\w*|order ?book|walls?|cvd|"
                           r"order ?flow|delta|positioning|crowded|sentiment|squeeze|buyers?|sellers?|buying pressure|"
                           r"selling pressure|money flow|more money)\b", re.I)
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
# "What's working right now?" → the desk's setups over the last 30 days; "how am I trading?" → coaching.
WORKING_WORDS = re.compile(r"\b(what(?:'s| is) working|working (?:right )?now|which setups?|best setups? (?:lately|now|"
                           r"this (?:week|month))|desk(?:'s)? (?:record|results?|doing))\b", re.I)
COACH_WORDS = re.compile(r"\b(coach|my (?:trading|trades|habits|mistakes)|how am i (?:trading|doing)|what am i doing "
                         r"wrong|am i (?:selling|buying) too)\b", re.I)

# A price in the question ("what's at 7.1561?", "is 25.4 support?"); not one followed by a timeframe, percent or
# multiplier, and only within PRICE_RANGE of the last price so "top 10 coins" or "2025" isn't read as one.
_PRICE = re.compile(r"(?<!\btop )(?<!\blast )(?<!\bnext )(?<![\w.])\$?(\d[\d,]*(?:\.\d+)?)(k?)"
                    r"(?![\w.%]|\s*(?:x|%|m|h|d|w|min|mins|minutes?|hours?|days?|weeks?|months?|candles?|bars?|"
                    r"coins?|pairs?|tokens?|setups?|trades?|times?)\b)", re.I)
PRICE_RANGE = (0.5, 2.0)


def asked_price(prompt: str, intent: AnalysisIntent, last: float) -> float | None:
    """The price a question is about, when it asks about one rather than drawing a level or setting an alert there."""
    if not last or intent.trade_plan or intent.scan_watchlist or intent.scan_market:
        return None
    given = {c.price for c in intent.custom_levels} | {c.price_low for c in intent.custom_levels} | \
        {c.price_high for c in intent.custom_levels} | set(intent.alert_prices)
    for num, k in _PRICE.findall(prompt):
        try:
            p = float(num.replace(",", "")) * (1000 if k else 1)
        except ValueError:
            continue
        if PRICE_RANGE[0] * last <= p <= PRICE_RANGE[1] * last and p not in given:
            return p
    return None

# Market-wide scans (market_scanner.py): results younger than one candle (5 to 30 minutes) are reused; a new scan
# is waited for this long, then the answer says it is still running (it finishes in the background and is kept).
MARKET_SCAN_WAIT = 45.0
TRACK_RECORD_WAIT = 15.0
SCAN_DIRECTIONS = {"bullish": "long", "near_support": "long", "oversold": "long", "bearish": "short",
                   "near_resistance": "short", "overbought": "short"}


def plan_facts(plan) -> dict:
    """The plan for the narrator: its track record cut to the line to quote and the caveats."""
    out = plan.model_dump(exclude={"zone_kind", "zone_fresh", "zone_htf", "track_record"})
    if tr := plan.track_record:
        out["track_record"] = {"summary": tr.summary, "status": tr.status, "data_source": tr.data_source,
                               "caveats": [n for n in tr.notes if not n.startswith("Backtest of")]}
    return out


def scan_max_age(tf: str) -> float:
    return max(300.0, min(1800.0, float(INTERVAL_SECONDS.get(tf, 1800))))


def market_scan_facts(res: MarketScanResult, direction: str | None, n: int = 6) -> dict:
    """The best setups of a market scan, trimmed for the narrator and the tool loop."""
    rows = res.best(n, direction)
    return {
        "timeframe": res.interval, "coins": res.scanned, "direction": direction or "both",
        "age_minutes": round(max(0.0, time.time() * 1000 - res.generated_at) / 60000, 1),
        "data_source": res.data_source, "notes": res.notes,
        "setups": [{"symbol": r.symbol, "direction": r.direction, "entry": r.entry, "stop": r.stop, "target": r.target,
                    "rr": r.rr, "distance_pct": r.distance_pct, "basis": r.basis,
                    "timeframes_agreeing": f"{r.agreement.aligned:g}/{r.agreement.total}",
                    "track_record": r.track_record.summary if r.track_record else None} for r in rows],
    }


def spot_facts(res: MarketScanResult, n: int = 6) -> list[dict]:
    """The best spot buys of a market scan for the narrator: buy zone, targets and invalidation."""
    out = []
    for r in res.best_spot(n):
        p = r.plan
        out.append({"symbol": r.symbol, "buy_zone": [p.zone_low, p.zone_high] if p.zone_low else [r.entry, r.entry],
                    "buy_at": r.entry, "targets": [t.price for t in p.targets][:3], "invalidation": r.stop,
                    "basis": r.basis, "higher_timeframes": p.zone_htf, "distance_pct": r.distance_pct,
                    "timeframes_agreeing": f"{r.agreement.aligned:g}/{r.agreement.total}",
                    "track_record": r.track_record.summary if r.track_record else None})
    return out


def grid_facts(res: MarketScanResult, n: int = 6) -> list[dict]:
    return [{"symbol": g.symbol, "range": [g.low, g.high], "width_pct": g.width_pct, "crossings": g.crossings,
             "days": g.days, "price_position_pct": g.position_pct, "note": g.note} for g in res.grid_coins[:n]]


TP_COLOR = GREEN


def take_profit_overlays(rows: list[dict]) -> list:
    return [HorizontalLineOverlay(id=_new_id(), kind="plan_target", price=r["low"], color=TP_COLOR,
                                  line_style="dashed", line_width=2,
                                  label=f"TP{i + 1} +{r['gain_pct']:g}% ({r['label']})") for i, r in enumerate(rows)]


def describe_take_profits(rows: list[dict], symbol: str) -> str:
    if not rows:
        return f"No resistance or supply above price on {symbol} right now: price is in open space."
    parts = [f"TP{i + 1} {_fmt(r['low'])} (+{r['gain_pct']:g}%, {r['label']})" for i, r in enumerate(rows)]
    return f"Where to take profit on {symbol}: " + "; ".join(parts) + ". Scaling out across them locks in gains early."


def describe_spot(res: MarketScanResult) -> str:
    rows = res.best_spot(3)
    if not rows:
        return f"No spot buys at higher-timeframe demand on the {res.interval} scan right now."
    parts = [f"{r.symbol} at {_fmt(r.entry)} (targets {', '.join(_fmt(t.price) for t in r.plan.targets[:2])}, wrong "
             f"below {_fmt(r.stop)})" for r in rows]
    return f"Best spot buys on the {res.interval} scan: " + "; ".join(parts) + "."


def describe_grid_coins(res: MarketScanResult) -> str:
    rows = res.grid_coins[:3]
    if not rows:
        return f"No coin has been ranging cleanly enough for a grid bot on the {res.interval} scan."
    return f"Best grid bot coins on {res.interval}: " + "; ".join(f"{g.symbol}: {g.note}" for g in rows)


def sell_overlays(rows: list, symbol: str) -> list:
    """The zone to sell into for the chart's own coin."""
    return [BoxOverlay(id=_new_id(), kind="plan_target", price_low=r.sell_low, price_high=r.sell_high,
                       color=rgba(ORANGE, 0.12), border_color=rgba(ORANGE, 0.85),
                       label=f"{'Sell' if r.action == 'sell' else 'Trim'} zone ({r.zone})")
            for r in rows if r.symbol == symbol][:1]


async def _sells(market: MarketData, symbols: list[str], tf: str) -> list | None:
    try:
        return await sell_scan(market, symbols, tf)
    except Exception as exc:  # the answer goes out without it and says so
        log.warning("Sell check failed: %s", exc)
        return None


SPOT_SHORT_NOTE = ("Spot mode is on, so there is no short plan. For spot that means waiting for a lower buy zone, or "
                   "taking profit on coins already held.")


async def _walk(market: MarketData, symbol: str) -> TopDownResult | None:
    try:
        return await walk(market, symbol)
    except Exception as exc:  # the answer goes out without it and says so
        log.warning("Top-down walk on %s failed: %s", symbol, exc)
        return None


async def _ladder(market: MarketData, symbol: str, tf: str) -> LadderResult | None:
    try:
        return await plan_ladder(market, LadderRequest(symbol=symbol, timeframe=tf if tf in ("1h", "4h", "1d") else
                                                       "4h"))
    except Exception as exc:
        log.warning("Dip ladder on %s failed: %s", symbol, exc)
        return None


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


async def _plan_in_focus(market: MarketData, f: Focus, symbol: str, tf: str, df: pd.DataFrame, result,
                         htf: dict | None, custom_chart: bool) -> dict | None:
    """plan_in_focus on the plan's own timeframe: this chart's candles when it is the same, else that timeframe's."""
    assert f.plan is not None
    if not f.interval or f.interval == tf:
        return plan_in_focus(f.plan, f.at, df, result.stats.last_price, result.stats.atr, result.swing_lows,
                             result.swing_highs, htf)
    if custom_chart:
        return None
    try:
        candles, _ = await market.get_klines(symbol, f.interval, ZONE_BARS)
    except Exception:
        return None
    fdf = candles_to_df(candles)
    if len(fdf) < 30:
        return None
    a = float(atr(fdf).iloc[-1])
    highs, lows = find_swings(fdf, a)
    return plan_in_focus(f.plan, f.at, fdf, float(fdf["close"].iloc[-1]), a, [s.price for s in lows],
                         [s.price for s in highs], htf)


async def run_analysis(req: AnalyzeRequest, market: MarketData, llm: LLMClient,
                       derivatives: DerivativesService | None = None, kimi: KimiService | None = None,
                       futures: FuturesDataService | None = None,
                       events: EventsService | None = None,
                       scanner: MarketScanner | None = None,
                       levels: SessionLevelsService | None = None,
                       gridbots: GridBotService | None = None,
                       metrics: MarketMetricsService | None = None,
                       metric_alerts: MetricAlertService | None = None,
                       level_log: LevelLog | None = None,
                       binance: Any = None,
                       desk: Any = None,
                       coach: Any = None,
                       on_result: Callable[[AnalyzeResponse], Awaitable[None]] | None = None,
                       on_delta: Callable[[str], Awaitable[None]] | None = None) -> AnalyzeResponse:
    """The agent's answer to one request. With `on_result` and `on_delta` (the streaming endpoint) the drawings,
    plan and the rest go to `on_result` before the summary is written, and the summary to `on_delta` as it is."""
    settings = get_settings()
    scanner = scanner or MarketScanner(market)
    chart = ChartContext(req.symbol, req.interval, req.watchlist, req.spot_only, await _listed(market),
                         focus_line(req.focus) if focus_usable(req.focus, req.symbol) else "")
    steps: list[str] = []
    research: list[dict] = []
    loop = None
    # Rules first: a request the rule parser is sure of is planned in milliseconds, without the model.
    route = None
    if req.prompt.strip() and llm.available() and llm.router == "rules_first":
        known = {w.removesuffix("USDT") for w in chart.watchlist}
        route = rule_route(req.prompt, req.previous_intent, known, chart.symbol, chart.listed)
        log.info("Route %r: %s", req.prompt[:60], route.reason)
    if route is not None and route.confident:
        loop = LoopResult(route.intent, "rules (fast path)")
    elif req.prompt.strip() and llm.available() and settings.agent_mode == "tools":
        box = make_toolbox(market, derivatives, req.watchlist, kimi, futures, events, scanner, req.spot_only, metrics,
                           req.indicator_settings)
        loop = await plan_with_tools(llm, req.prompt, req.history, req.overlays, req.previous_intent, chart, box,
                                     settings.agent_max_steps)
    if loop and loop.intent is not None:
        intent, intent_engine, steps, research = loop.intent, loop.engine, loop.steps, loop.research
    else:
        if loop:  # the tools looked but never drew: keep what they found
            steps, research = loop.steps, loop.research
        intent, intent_engine = await llm.parse_intent(req.prompt, req.history, req.overlays, req.previous_intent,
                                                       chart)
    # Asking about an indicator reads it; only "add", "show" or "turn on" puts it on the chart (Kimi has its own rule).
    if intent.indicators_on and not SHOW_VERBS.search(req.prompt):
        intent = intent.model_copy(update={"indicators_on": [k for k in intent.indicators_on if k == "kimi"]})
    # Spot only: a short plan becomes a note, "what's the trade?" a long.
    spot_note = None
    if req.spot_only and intent.trade_plan == "short":
        spot_note = SPOT_SHORT_NOTE
        intent = intent.model_copy(update={"trade_plan": None, "keep_existing": True})
    elif req.spot_only and intent.trade_plan == "auto":
        intent = intent.model_copy(update={"trade_plan": "long"})

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
    if intent.take_profit:
        features += [f for f in ("support_resistance", "supply_demand") if f not in features]
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

    scan_direction = "long" if req.spot_only else SCAN_DIRECTIONS.get(intent.scan_filter)

    async def market_scan() -> MarketScanResult | str | None:
        if not intent.scan_market:
            return None
        try:
            return await asyncio.wait_for(scanner.fresh_or_run(tf, scan_max_age(tf)), MARKET_SCAN_WAIT)
        except asyncio.TimeoutError:
            return "still_running"
        except Exception as exc:  # the answer goes out without it
            log.warning("Market scan for the agent failed: %s", exc)
            return "failed"

    want_deriv = derivatives is not None and bool(req.prompt.strip()) and not custom_chart
    # The user's own indicator: read it when they name it or switch it on.
    want_kimi = ("kimi" in intent.indicators_on or bool(KIMI_WORDS.search(req.prompt))) and not custom_chart
    # Futures and order flow when asked about them or building a plan; the calendar for plans and "any news?".
    want_futures = futures is not None and not custom_chart and (
        bool(intent.trade_plan) or bool(FUTURES_WORDS.search(req.prompt)))
    want_events = bool(intent.trade_plan) or bool(EVENT_WORDS.search(req.prompt))
    want_news = bool(NEWS_WORDS.search(req.prompt)) and not custom_chart
    # Session / previous day-week-month / opening-range levels, so the answer can say "price is at the London high".
    want_levels = levels is not None and not custom_chart
    want_grid = intent.grid_plan and not custom_chart
    want_walk = intent.top_down and not custom_chart
    want_ladder = intent.dip_ladder and not custom_chart
    want_general = intent.general_question and bool(req.prompt.strip())
    # "What should I sell?" checks the watchlist, "should I sell INJ?" that coin; a short asked for in spot mode
    # checks this coin, the spot holder's version of a short.
    want_sell = (intent.sell_check or spot_note is not None) and not custom_chart
    base = re.sub(r"(?:USDT|USDC|FDUSD|BUSD|BTC)$", "", symbol) or symbol
    one_coin = bool(intent.symbol or spot_note or re.search(rf"\b{re.escape(base)}\b", req.prompt, re.I))
    held_now = await _positions(binance) if want_sell and not one_coin else None
    held_syms = held_symbols(held_now) if held_now else []
    # "What should I sell?" checks what you hold (spot and Earn); the watchlist without a read-only key.
    sell_symbols = [symbol] if one_coin else (held_syms or req.watchlist or DEFAULT_WATCHLIST)
    sell_tf = timeframe_for(tf)
    extras = asyncio.gather(
        _sells(market, sell_symbols, sell_tf) if want_sell else _none(),
        _walk(market, symbol) if want_walk else _none(),
        _ladder(market, symbol, tf) if want_ladder else _none(),
        general.context(events, req.prompt) if want_general else _none(),
        # The market header bar and the chart's CVD pane, read whether or not the user has them on screen.
        _market_overview(metrics), _chart_cvd(futures, symbol, tf) if not custom_chart else _none(),
        _vs_btc(market, symbol) if not custom_chart else _none(), _positions(binance))
    (candles, source), higher, htf_frames, rows, deriv, kimi_facts, fut, upcoming, headlines, mscan, lvl, grid, extra = await asyncio.gather(
        candles_for_chart(), windows(),
        _confluence_frames(market, symbol, tf, [] if custom_chart else ["support_resistance"]), scan_rows(),
        derivatives.symbol_snapshot(symbol) if want_deriv else _none(),
        _kimi_facts(kimi, symbol, tf) if want_kimi else _none(),
        _guarded(futures.futures_context(symbol), "futures context") if want_futures else _none(),
        _upcoming(events, EVENT_HOURS) if want_events else _none(),
        _headlines(events, symbol) if want_news else _empty(), market_scan(),
        _guarded(levels.get(symbol, tf), "session levels") if want_levels else _none(),
        _grid_plan(gridbots, symbol) if want_grid else _none(), extras)
    sells, walked, ladder, general_ctx, overview, chart_cvd, vs_btc_facts, positions = extra
    if general_ctx is not None and overview:
        general_ctx["market_overview"] = overview
    if general_ctx is not None and positions and HOLDINGS_WORDS.search(req.prompt):
        general_ctx["your_holdings"] = portfolio_facts(positions)

    # The next two timeframes up are always read (htf_readings); their zones only count when zones are drawn.
    frames = htf_frames if {"support_resistance", "supply_demand"} & set(features) else {}
    df = candles_to_df(candles)
    # Detection is CPU-bound (SciPy); keep the event loop free for streams.
    result = await asyncio.to_thread(analyze, df, run_intent, tf, higher, frames, req.indicator_settings)
    # The coin by name, so the narrator never guesses one, and a flag when the candles are demo data. The chart's own
    # candles ("client") are demo data when this server only has synthetic data to give.
    demo = source == "synthetic" or (source == "client" and market.settings.data_source == "synthetic")
    facts = {"symbol": symbol, "coin": symbol if custom_chart else base,
             **({"data_source": "demo (synthetic)"} if demo else {}), **result.facts}

    for ov in result.overlays:
        ov.id = _new_id()
    plan = None
    plan_ovs: list = []
    if intent.trade_plan:
        plan = build_plan(intent.trade_plan, result.stats.last_price, result.stats.atr, result.stats.trend,
                          result.levels, result.swing_lows, result.swing_highs, result.bias)
        if plan and (note := _event_note(upcoming)):
            plan.notes.append(note)
        if plan and not custom_chart:  # how this kind of setup did on this coin and timeframe (track_record.py)
            plan.track_record = await scanner.track.for_plan(symbol, tf, plan, timeout=TRACK_RECORD_WAIT)
        facts["plan"] = plan_facts(plan) if plan else None
        if plan:
            plan_ovs = plan_overlays(plan, int(df["time"].iloc[-1]))
            for ov in plan_ovs:
                ov.id = _new_id()
    tp_rows: list[dict] = []
    exit_plan = None
    if intent.take_profit:
        tp_rows = take_profits(result.levels, result.stats.last_price, frames, tf)
        facts["take_profit"] = tp_rows
        held = next((h for h in ((positions or {}).get("manual") or {}).get("holdings", [])
                     if h["symbol"] == symbol and (h.get("value") or 0) >= 10), None) if not custom_chart else None
        if held:  # a coin you hold: how much to sell where, sized from your holding (exit_plan.py)
            try:
                exit_plan = await plan_from_market(market, symbol, held["qty"],
                                                   max(0.0, held["qty"] - held.get("locked_qty", 0.0)),
                                                   held.get("avg_entry"))
                facts["exit_plan"] = exit_plan.model_dump(exclude={"armed", "created_at"})
            except Exception as exc:  # the plain take-profit levels still answer
                log.info("Exit plan for %s skipped: %s", symbol, exc)
        plan_ovs = plan_ovs + take_profit_overlays(tp_rows)
    if ladder:
        facts["ladder"] = ladder_facts(ladder)
        plan_ovs = plan_ovs + [ov.model_copy(update={"id": _new_id()}) for ov in ladder.overlays]
    if want_sell:
        facts["sell_check"] = sell_facts(sells, len(sell_symbols)) if sells is not None else {
            "error": "The sell check could not run right now."}
        plan_ovs = plan_ovs + sell_overlays(sells or [], symbol)
    if walked:
        facts["top_down"] = walk_facts(walked)
    elif want_walk:
        facts["top_down"] = {"error": "The top-down walk could not run right now."}
    if req.spot_only:
        facts["trading_style"] = "spot only: buys coins outright, no shorts, futures or leverage"
    if spot_note:
        facts["spot_note"] = spot_note
    custom = custom_overlays(intent)
    triggers, trigger_lines = await _trigger_alerts(intent, market, symbol, tf, custom_chart, custom)
    new = result.overlays + plan_ovs + custom
    if plan_ovs:  # a new plan replaces the previous one
        existing = [o for o in existing if (o.kind or "") not in TARGET_KINDS["plan"]]
    if triggers and any(o.kind == "trigger_zone" for o in custom):  # so does a new trigger zone
        existing = [o for o in existing if (o.kind or "") != "trigger_zone"]
    overlays, removed = merge_overlays(existing, new, run_intent)
    alerts = build_alerts(intent, overlays, new)

    if chart_cvd:
        facts["indicators"]["cvd"] = chart_cvd
    if htf_frames and (htf := htf_readings(htf_frames, req.indicator_settings)):
        facts["higher_timeframes"] = htf
    if vs_btc_facts:
        facts["vs_btc"] = vs_btc_facts
    facts["session_clock"] = clock_facts(int(time.time()))
    if req.coin_note and req.coin_note.strip():
        facts["your_note_on_this_coin"] = req.coin_note.strip()
    if positions:  # read-only Binance key set: what the user holds of this coin, and everything when asked
        if not custom_chart and (mine := holding_facts(positions, symbol)):
            facts["your_position"] = mine
        if HOLDINGS_WORDS.search(req.prompt):
            facts["your_holdings"] = portfolio_facts(positions)
    if (asked := asked_price(req.prompt, intent, result.stats.last_price)) is not None:
        facts["price_in_question"] = await asyncio.to_thread(price_check, df, asked, tf, htf_frames)
    if req.focus and not intent.trade_plan and focus_usable(req.focus, symbol):
        # "What would invalidate this?" under a plan: "this" is the plan in focus (focus.py).
        f = req.focus
        if f.kind == "plan" and f.plan is not None:
            pf = await _plan_in_focus(market, f, symbol, tf, df, result, facts.get("higher_timeframes"), custom_chart)
            if pf is not None:
                facts["plan_in_focus"] = pf
        elif f.kind in ("price", "zone") and "price_in_question" not in facts:
            price = f.price or ((f.zone_low + f.zone_high) / 2 if f.zone_low and f.zone_high else None)
            if price:
                facts["price_in_question"] = await asyncio.to_thread(price_check, df, price, tf, htf_frames)
    if desk is not None and not custom_chart and (dk := desk.facts_for(symbol)):
        facts["agent_desk"] = dk  # the calls the desk made on its own on this coin (agent_desk.py)
    if desk is not None and WORKING_WORDS.search(req.prompt):
        summ = desk.summary()
        facts["what_works_now"] = {"days": summ["recent_days"], "setups": summ["working_now"][:8],
                                   "desk_record": {k: summ[k] for k in ("closed", "tp", "total_r")}}
    if coach is not None and COACH_WORDS.search(req.prompt):
        rep = await _guarded(coach.report(), "coaching", 30.0)
        if rep is not None:
            facts["your_trading"] = {"overview": rep["overview"], "note": rep.get("note"),
                                     "habits": [{"title": f["title"], "detail": f["detail"], "tone": f["tone"]}
                                                for f in rep["findings"]]}
    if req.previous_answer and (past := past_answer_facts(req.previous_answer, result.stats.last_price)):
        facts["last_time_you_asked"] = past
    if overview:
        facts["market_overview"] = overview
    if deriv:
        facts["derivatives"] = deriv
    if kimi_facts:
        facts["kimi"] = kimi_facts
    if fut:
        facts["futures_context"] = fut
    if lvl and (lf := level_facts(lvl, result.stats.last_price, result.stats.atr)):
        facts["session_levels"] = lf
    if upcoming is not None:
        facts["upcoming_events"] = upcoming  # [] says "nothing high-impact coming", which is worth saying too
    if headlines:
        facts["headlines"] = headlines
    if want_grid:
        facts["grid_plan"] = grid_plan_facts(grid) if grid else None
    if intent.scan_watchlist:
        facts["scan"] = [r.model_dump(include={"symbol", "last_price", "change_pct", "trend", "rsi", "signals",
                                               "nearest_kind", "distance_pct", "unusual_volume"}) for r in rows[:6]]
    setups: list[MarketSetup] = []
    grid_coins = []
    if isinstance(mscan, MarketScanResult) and intent.scan_kind == "spot_buys":
        facts["spot_buys"] = spot_facts(mscan)
        facts["scan_timeframe"] = mscan.interval
        setups = mscan.best_spot(10)
    elif isinstance(mscan, MarketScanResult) and intent.scan_kind == "grid_coins":
        facts["grid_coins"] = grid_facts(mscan)
        facts["scan_timeframe"] = mscan.interval
        grid_coins = mscan.grid_coins[:10]
    elif isinstance(mscan, MarketScanResult):
        facts["market_scan"] = market_scan_facts(mscan, scan_direction)
        setups = mscan.best(10, scan_direction)
    elif intent.scan_market:
        facts["market_scan"] = {"timeframe": tf, "setups": [], "note": (
            "The market scan is still running; ask again in a minute or open the Scanner tab." if mscan ==
            "still_running" else "The market scan could not run right now.")}
    if research:
        facts["research"] = [{"step": s, "result": r["result"]} for s, r in zip(steps, research)]
    nav = []
    if navigate:
        nav.append(f"Switched the chart to {navigate.symbol} {navigate.interval}.")
    toggles = {**{k: True for k in intent.indicators_on}, **{k: False for k in intent.indicators_off}}
    actions = _action_lines(intent, [o for o in custom if o.kind != "trigger_zone"], removed, alerts) + trigger_lines
    show_kimi = want_kimi and "kimi" not in toggles  # asked about it: show it on the chart too, quietly
    if toggles:
        on = [INDICATOR_NAMES.get(k, k.upper()) for k, v in toggles.items() if v]
        off = [INDICATOR_NAMES.get(k, k.upper()) for k, v in toggles.items() if not v]
        actions.append(" ".join(filter(None, [f"Turned on {', '.join(on)}." if on else "",
                                              f"Turned off {', '.join(off)}." if off else ""])))
    if intent.metric_alerts:
        actions.append(await _set_metric_alerts(intent.metric_alerts, metric_alerts))
    if nav:
        facts["navigation"] = nav
    if actions:
        facts["actions"] = actions
    narrate_facts = {k: v for k, v in facts.items() if k != "last_bar_time"}
    fallback = describe(narrate_facts, symbol, req.detail, req.prompt)
    if want_grid:  # the grid plan leads the answer; the chart summary follows
        fallback = (describe_plan(grid) if grid else "Couldn't plan a grid bot for this coin right now.") + " " + \
            fallback
    lead = []  # answers that lead with their own result, before the chart summary
    if want_walk:
        lead.append(describe_walk(walked) if walked else "The top-down walk could not run right now.")
    if want_ladder:
        lead.append(describe_ladder(ladder) if ladder else "Couldn't plan a dip-buy ladder for this coin right now.")
    if intent.take_profit:
        lead.append(describe_exit_plan(exit_plan) if exit_plan is not None else describe_take_profits(tp_rows, symbol))
    if isinstance(mscan, MarketScanResult) and intent.scan_kind == "spot_buys":
        lead.append(describe_spot(mscan))
    if isinstance(mscan, MarketScanResult) and intent.scan_kind == "grid_coins":
        lead.append(describe_grid_coins(mscan))
    if spot_note:
        lead.append(spot_note)
    if want_sell:
        lead.append(describe_sells(sells, len(sell_symbols), sell_tf) if sells is not None else
                    "The sell check could not run right now.")
    if lead:
        fallback = " ".join(lead) + " " + fallback
    if level_log is not None and not custom_chart:  # for the weekly "did the levels hold?" check
        level_log.record(symbol, tf, overlays, result.stats.last_price, source)
    res = AnalyzeResponse(
        symbol=symbol,
        interval=chart_interval,
        analysis_interval=tf,
        overlays=overlays,
        summary="",
        intent=intent,
        stats=result.stats,
        engine={"intent": intent_engine, "summary": "pending", "detector": "scipy",
                **({"route": route.reason} if route is not None else {})},
        data_source=source,
        alerts=alerts,
        navigate=navigate,
        indicators={**toggles, **({"kimi": True} if show_kimi else {})},
        scan=rows[:10],
        plan=plan,
        setups=setups,
        grid_plan=grid.model_dump() if grid else None,
        steps=steps,
        trigger_alerts=triggers,
        grid_coins=grid_coins,
        top_down=walked.model_dump() if walked else None,
        ladder=ladder.model_dump() if ladder else None,
        sources=[],
        sells=sells or [],
        sell_watch=SellWatch(symbols=sell_symbols[:40], interval=sell_tf) if sells is not None else None,
        facts=round_facts({k: v for k, v in narrate_facts.items() if k not in USED_SKIP}),
        question_only=bool(req.prompt.strip()) and intent.keep_existing and not intent.features
        and not intent.has_actions and not intent.take_profit,
    )
    if not custom_chart:
        res.suggestions = next_steps(narrate_facts, intent, symbol, tf, req.spot_only)
    if on_result is not None:
        await on_result(res)
    sources: list[dict[str, str]] = []
    if want_general and general_ctx is not None:
        # Web-searched answers come back in one piece.
        answered = await llm.answer(req.prompt, general_ctx, req.history, req.spot_only, req.detail)
        if answered:
            summary, narrate_engine, sources = answered
        else:
            summary, narrate_engine = general.template_answer(req.prompt, general_ctx), "template"
        sources = sources or general.sources_from(general_ctx)
        if on_delta is not None:
            await on_delta(summary)
    elif on_delta is not None:
        summary, narrate_engine = await llm.narrate_stream(req.prompt, narrate_facts, fallback, req.history, on_delta,
                                                           detail=req.detail)
    else:
        summary, narrate_engine = await llm.narrate(req.prompt, narrate_facts, fallback, req.history,
                                                    detail=req.detail)
    return res.model_copy(update={"summary": summary, "sources": sources,
                                  "engine": {**res.engine, "summary": narrate_engine}})


async def _set_metric_alerts(specs: list[MetricAlertSpec], service: MetricAlertService | None) -> str:
    """"Alert me when Fear & Greed drops below 25" → the alert set, and its action line."""
    if service is None:
        return "Market alerts aren't available here."
    try:
        made = await service.add(specs)
    except ValueError as exc:
        return f"Couldn't set the market alert: {exc}."
    return "Set a market alert: " + "; ".join(describe_metric_alert(a) for a in made) + "."


async def _trigger_alerts(intent: AnalysisIntent, market: MarketData, symbol: str, tf: str, custom_chart: bool,
                          drawn: list) -> tuple[list[ZoneTriggerSpec], list[str]]:
    """"Alert me when 1m shows a CHoCH inside the 4h demand" → the trigger alert for the client to arm, and the
    action line naming the zone it watches now. That zone is also drawn (appended to `drawn`)."""
    zt = intent.zone_trigger
    if zt is None:
        return [], []
    if custom_chart:
        return [], ["Trigger alerts need a Binance pair, not a ratio or index chart."]
    spec = spec_from_intent(zt, symbol, tf)
    found = await _guarded(detect_now(market, symbol, spec.zone), "trigger zone")
    band = found[0] if found else None
    if band is not None:
        color = TEAL if band.direction == "long" else ORANGE
        drawn.append(BoxOverlay(id=_new_id(), kind="trigger_zone", label=f"{band.label} (trigger zone)",
                                price_low=band.low, price_high=band.high, color=rgba(color, 0.14),
                                border_color=rgba(color, 0.8)))
    return [spec], [f"Set a trigger alert: {describe_trigger(spec, band)}."]
