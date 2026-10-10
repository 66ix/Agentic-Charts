"""The plan in focus: what "this" is when the user asks "what would invalidate this?" or "is it still valid?" under
a plan the agent gave. From the plan and the candles since it was given: how far entry and stop are, whether it
filled, stopped or reached T1 since, where it is wrong (the stop, and the structure between stop and entry), and
whether the higher timeframes agree with its direction."""

from __future__ import annotations

import time
from typing import Optional

import numpy as np
import pandas as pd

from .schemas import Focus, TradePlan

FOCUS_MAX_AGE = 86400  # a focus older than a day is stale


def usable(focus: Optional[Focus], symbol: str, now: Optional[float] = None) -> bool:
    """The focus is on this coin and recent enough to be what "this" means."""
    if focus is None or focus.symbol != symbol:
        return False
    now = time.time() if now is None else now
    return 0 <= now - focus.at <= FOCUS_MAX_AGE


def _since(df: pd.DataFrame, at: int, plan: TradePlan) -> dict:
    """Filled, stopped and T1 since `at`, from the candles that opened after it (in order: no stop before a fill)."""
    after = df[df["time"] > at]
    long = plan.direction == "long"
    out = {"filled_since": False, "stop_hit_since": False, "t1_hit_since": False, "candles_since": int(len(after))}
    if after.empty:
        return out
    lo, hi = after["low"].to_numpy(), after["high"].to_numpy()
    fill = np.flatnonzero(lo <= plan.entry if long else hi >= plan.entry)
    if not fill.size:
        return out
    out["filled_since"] = True
    rest = slice(int(fill[0]), None)
    out["stop_hit_since"] = bool((lo[rest] <= plan.stop).any() if long else (hi[rest] >= plan.stop).any())
    if plan.targets:
        t1 = plan.targets[0].price
        out["t1_hit_since"] = bool((hi[rest] >= t1).any() if long else (lo[rest] <= t1).any())
    return out


def _structure(plan: TradePlan, swing_lows: list[float], swing_highs: list[float]) -> tuple[Optional[float], str]:
    """The structure level between stop and entry that a close through says the idea is wrong first, or None."""
    if plan.direction == "long":
        inside = [p for p in swing_lows if plan.stop < p < plan.entry]
        if inside:
            return max(inside), "the highest swing low between the stop and the entry"
    else:
        inside = [p for p in swing_highs if plan.entry < p < plan.stop]
        if inside:
            return min(inside), "the lowest swing high between the entry and the stop"
    return None, "the stop is the structure: nothing sits between it and the entry"


def _htf_verdict(direction: str, htf: Optional[dict]) -> tuple[dict, str]:
    if not htf:
        return {}, "unknown"
    want = "up" if direction == "long" else "down"
    trends = {tf: row.get("trend") for tf, row in htf.items()}
    agree = [tf for tf, t in trends.items() if t == want]
    against = [tf for tf, t in trends.items() if t in ("up", "down") and t != want]
    verdict = "agrees" if agree and not against else "disagrees" if against and not agree else "mixed"
    return trends, verdict


def plan_in_focus(plan: TradePlan, at: int, df: pd.DataFrame, last: float, atr: float, swing_lows: list[float],
                  swing_highs: list[float], htf: Optional[dict]) -> dict:
    """Facts about the plan the user is asking about (pure)."""
    level, why = _structure(plan, swing_lows, swing_highs)
    trends, verdict = _htf_verdict(plan.direction, htf)

    def dist(p: float) -> dict:
        return {"pct": round((p / last - 1) * 100, 2) if last else None,
                "atr": round(abs(p - last) / atr, 2) if atr > 0 else None}

    out = {
        "direction": plan.direction, "basis": plan.basis, "entry": plan.entry, "stop": plan.stop,
        "targets": [t.price for t in plan.targets], "risk_pct": plan.risk_pct,
        "entry_distance": dist(plan.entry), "stop_distance": dist(plan.stop),
        **_since(df, at, plan),
        "invalidation": {"stop": plan.stop, "structure_level": level, "why": why},
        "higher_timeframes": trends, "htf_verdict": verdict,
    }
    if plan.track_record and plan.track_record.summary:
        out["track_record"] = plan.track_record.summary
    return out


def focus_line(focus: Focus, now: Optional[float] = None) -> str:
    """One line for the model's context: what the user is looking at."""
    now = time.time() if now is None else now
    ago = max(0, int(now - focus.at))
    when = f"{ago // 3600}h ago" if ago >= 3600 else f"{max(1, ago // 60)}m ago"
    where = f"{focus.symbol} {focus.interval or ''}".strip()
    if focus.kind == "plan" and focus.plan:
        p = focus.plan
        tgts = ", ".join(f"{t.price:g}" for t in p.targets)
        return (f"the {p.direction} plan on {where} given {when}: entry {p.entry:g}, stop {p.stop:g}, targets {tgts}"
                + (f" ({p.basis})" if p.basis else ""))
    if focus.kind == "zone" and focus.zone_low is not None and focus.zone_high is not None:
        return f"the zone {focus.zone_low:g}-{focus.zone_high:g} on {where}" + (f" ({focus.label})" if focus.label else "")
    if focus.kind == "price" and focus.price:
        return f"the price {focus.price:g} on {where}" + (f" ({focus.label})" if focus.label else "")
    if focus.kind == "scan" and focus.symbols:
        return "the scan results: " + ", ".join(focus.symbols)
    return focus.label
