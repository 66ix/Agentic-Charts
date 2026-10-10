"""Next steps under an answer, built from what it found: "Alert me if 7.53–7.57 breaks" when price sits on a demand
zone, "Plan the Kimi B+ long" right after one printed, "Where do I trim INJ?" for a coin in profit near resistance.
Pure: facts in, at most MAX suggestions out, best first. Spot mode never offers a short. Each prompt is phrased so the
rule parser reads it the intended way (evals/intents.jsonl has a line for each)."""

from __future__ import annotations

from typing import Optional

from .pricefmt import _fmt
from .schemas import AnalysisIntent, Suggestion

MAX = 4
NEAR_ATR = 0.3  # a zone this close (in ATR) counts as "price is at it"


def _coin(symbol: str) -> str:
    for q in ("USDT", "USDC", "FDUSD"):
        if symbol.endswith(q) and len(symbol) > len(q):
            return symbol[: -len(q)]
    return symbol


def next_steps(facts: dict, intent: Optional[AnalysisIntent], symbol: str, interval: str,
               spot_only: bool = True) -> list[Suggestion]:
    coin = _coin(symbol)
    out: list[tuple[float, Suggestion]] = []

    def add(score: float, label: str, prompt: str, reason: str) -> None:
        if all(s.label != label for _, s in out):
            out.append((score, Suggestion(label=label, prompt=prompt, reason=reason)))

    # Price at a buy zone: an alert on it breaking.
    for kind in ("demand", "support"):
        for z in (facts.get(kind) or [])[:1]:
            if z.get("inside") or (z.get("distance_atr") is not None and z["distance_atr"] <= NEAR_ATR):
                lo, hi = _fmt(z["low"]), _fmt(z["high"])
                add(0.95, f"Alert me if {lo}–{hi} breaks", f"Alert me if {coin} breaks below {lo}",
                    f"Price is at the {kind} zone {lo}–{hi}.")
    # A fresh Kimi signal.
    sigs = (facts.get("kimi") or {}).get("recent_signals") or []
    if sigs:
        s = sigs[-1]
        if s.get("bars_ago") is not None and s["bars_ago"] <= 3 and (s.get("direction") == "long" or not spot_only):
            add(0.9, f"Plan the Kimi {s['label']} {s['direction']}", f"Give me a {s['direction']} plan on {coin}",
                f"Kimi printed {s['label']} {s['bars_ago']} candle(s) ago.")
    # Holding in profit near resistance or a take-profit level.
    spot = (facts.get("your_position") or {}).get("spot") or {}
    if (spot.get("pnl_pct") or 0) > 0:
        last = facts.get("last_price") or 0
        near = [z for z in facts.get("resistance") or [] if last and z["low"] <= last * 1.05]
        near += [r for r in facts.get("take_profit") or [] if last and r["low"] <= last * 1.05]
        if near:
            add(0.85, f"Where do I trim {coin}?", f"Where should I take profit on {coin}?",
                f"You hold {coin} up {spot['pnl_pct']:g}% and resistance is within 5%.")
    # A plan on screen: what would make it wrong.
    if facts.get("plan") and not facts.get("plan_in_focus"):
        add(0.8, "What would invalidate this?", "What would invalidate this?", "Where the plan is wrong.")
    # A higher timeframe that disagrees.
    trend = facts.get("trend")
    for tf, row in (facts.get("higher_timeframes") or {}).items():
        t = row.get("trend")
        if trend in ("up", "down") and t in ("up", "down") and t != trend:
            add(0.65, f"Why does the {tf} disagree?", f"Does the {tf} agree?",
                f"The {tf} trends {t} while this chart trends {trend}.")
            break
    if (facts.get("momentum") or {}).get("divergence"):
        add(0.6, "Show swings and the divergence", f"Show swings and RSI divergence on {coin}",
            f"RSI has a divergence: {facts['momentum']['divergence']}.")
    if (facts.get("volume") or {}).get("unusual"):
        add(0.55, f"Any news on {coin}?", f"Any news on {coin}?", "Volume is unusually high.")
    for e in (facts.get("upcoming_events") or [])[:1]:
        if e.get("in_hours") is not None and e["in_hours"] <= 24:
            add(0.5, f"{e['title']}: what could it do to {coin}?", f"What events are coming up for {coin}?",
                f"{e['title']} in {e['in_hours']:.0f}h.")
    for c in ((facts.get("agent_desk") or {}).get("running") or [])[:1]:
        add(0.45, f"How is the desk's {c['interval']} call doing?", f"How is the desk's {coin} {c['interval']} call doing?",
            "The desk has a call running on this coin.")
    if spot_only:
        out = [(sc, s) for sc, s in out if "short" not in s.prompt.lower()]
    out.sort(key=lambda x: -x[0])
    return [s for _, s in out[:MAX]]
