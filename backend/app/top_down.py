"""Top-down S/R walk: draw the valid levels on the daily first, then step down to 4H, 1H and 15m.

A trader reads a chart from the top: the daily levels matter most, the 4H ones refine them, the 1H and 15m ones
time the entry. `walk` runs the same detectors the chart agent uses (ta_agent.py) on each timeframe of the ladder,
oldest timeframe first, and keeps only the levels worth drawing:

  * Support / resistance: built from at least MIN_TOUCHES swing points, score at least MIN_SR_SCORE, and respected
    lately (at most one close through it in the last RESPECT_BARS bars).
  * Supply / demand: still valid (no close beyond its far edge since it formed, as the detector already checks),
    tested at most MAX_TESTS times, and left with a strong move (impulse of MIN_IMPULSE ATRs or more).
  * Both: within MAX_DISTANCE_ATR ATRs and MAX_DISTANCE_PCT of price, so a level from a year ago far from price
    doesn't count.

A level that overlaps one already drawn on a higher timeframe is not drawn again; the step says it confirms that
level instead. A timeframe with nothing new and valid is skipped with the reason, and the walk carries on to the
next one, so the client only stops on timeframes that add something. The client plays the steps one by one
(switching the chart's timeframe and drawing each step's levels), and higher-timeframe levels stay visible on the
lower timeframes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Literal

import pandas as pd
from pydantic import BaseModel, Field

from .market_data import MarketData, candles_to_df
from .pricefmt import _fmt
from .schemas import BoxOverlay
from .ta_agent import ZONE_BARS
from .ta_agent import GREEN, ORANGE, RED, TEAL, TF_LABEL, Zone, atr, cluster_levels, find_swings, rgba, \
    supply_demand_zones

log = logging.getLogger(__name__)

LADDER: tuple[str, ...] = ("1w", "1d", "4h", "1h", "15m")
DEFAULT_START, DEFAULT_END = "1d", "15m"
CANDLES = ZONE_BARS
MIN_CANDLES = 60
MIN_TOUCHES = 2
MIN_SR_SCORE = 0.45
RESPECT_BARS = 50
MAX_TESTS = 1
MIN_IMPULSE = 2.0
MAX_DISTANCE_ATR = 10.0
MAX_DISTANCE_PCT = 25.0
PER_SIDE = 2
OVERLAP = 0.5  # share of the thinner zone that must overlap a higher-timeframe zone to count as the same level

COLORS = {"support": GREEN, "resistance": RED, "demand": TEAL, "supply": ORANGE}


class WalkZone(BaseModel):
    kind: str
    low: float
    high: float
    label: str
    why: str = Field(..., description="Why it counts: '3 touches', 'fresh, 2.6 ATR move'")
    distance_pct: float = Field(..., description="Signed: + above price, − below, 0 inside")
    score: float
    confirmed_by: list[str] = Field(default_factory=list, description="Lower timeframes with a level in the same place")


class WalkStep(BaseModel):
    interval: str
    label: str = Field(..., description="'D1', 'H4'")
    status: Literal["drawn", "skipped", "failed"]
    zones: list[WalkZone] = Field(default_factory=list)
    overlays: list[BoxOverlay] = Field(default_factory=list)
    reason: str = Field("", description="Why the step was skipped, or what it confirms")
    summary: str = ""
    data_source: str = "binance"


class TopDownResult(BaseModel):
    """Mirrors TopDownResult in frontend/lib/types.ts."""

    symbol: str
    last_price: float
    steps: list[WalkStep]
    generated_at: int = Field(..., description="UNIX milliseconds")
    data_source: str = "binance"
    notes: list[str] = Field(default_factory=list)

    @property
    def drawn(self) -> list[WalkStep]:
        return [s for s in self.steps if s.status == "drawn"]


def ladder(start: str = DEFAULT_START, end: str = DEFAULT_END) -> list[str]:
    """The timeframes from `start` down to `end`, e.g. 1d → 15m: ["1d", "4h", "1h", "15m"]. Unknown ends fall back
    to the defaults; a reversed pair is swapped."""
    i = LADDER.index(start) if start in LADDER else LADDER.index(DEFAULT_START)
    j = LADDER.index(end) if end in LADDER else LADDER.index(DEFAULT_END)
    if j < i:
        i, j = j, i
    return list(LADDER[i:j + 1])


def _respected(df: pd.DataFrame, z: Zone) -> bool:
    """At most one close through the zone's far side in the last RESPECT_BARS bars."""
    closes = df["close"].tail(RESPECT_BARS)
    through = (closes < z.price_low) if z.kind == "support" else (closes > z.price_high)
    return int(through.sum()) <= 1


def _distance_pct(z: Zone, last: float) -> float:
    if z.price_low <= last <= z.price_high:
        return 0.0
    edge = z.price_low if z.price_low > last else z.price_high
    return round((edge - last) / last * 100, 2)


def valid_zones(df: pd.DataFrame) -> tuple[list[tuple[Zone, str]], dict]:
    """Detected zones on one timeframe that pass the walk's rules → ([(zone, why)], counts for the skip reason).
    CPU-bound."""
    df = df.reset_index(drop=True)
    atr_s = atr(df)
    atr_v = float(atr_s.iloc[-1])
    last = float(df["close"].iloc[-1])
    highs, lows = find_swings(df, atr_v)
    sr = cluster_levels(highs + lows, atr_v, last, len(df))
    sd = supply_demand_zones(df, atr_s)
    counts = {"sr": len(sr), "sd": len(sd), "far": 0}

    def near(z: Zone) -> bool:
        edge = z.price_low if z.price_low > last else z.price_high if z.price_high < last else last
        ok = abs(edge - last) <= MAX_DISTANCE_ATR * atr_v and abs(edge - last) / last * 100 <= MAX_DISTANCE_PCT
        if not ok:
            counts["far"] += 1
        return ok

    out: list[tuple[Zone, str]] = []
    for z in sr:
        if z.touches >= MIN_TOUCHES and z.score >= MIN_SR_SCORE and near(z) and _respected(df, z):
            out.append((z, f"{z.touches} touches"))
    for z in sd:
        impulse = float(z.meta.get("impulse_atr") or 0)
        if z.tests <= MAX_TESTS and impulse >= MIN_IMPULSE and near(z):
            fresh = "fresh" if z.tests == 0 else "tested once"
            out.append((z, f"{fresh}, left with a {impulse:g} ATR move"))
    # Strongest first, at most PER_SIDE above and below price, and no two of the same timeframe on top of each other.
    out.sort(key=lambda zw: -zw[0].score)
    kept: list[tuple[Zone, str]] = []
    above = below = 0
    for z, why in out:
        if any(z.overlap_ratio(k) > OVERLAP for k, _ in kept):
            continue
        is_above = z.mid >= last
        if (above if is_above else below) >= PER_SIDE:
            continue
        kept.append((z, why))
        above += is_above
        below += not is_above
    return kept, counts


def _skip_reason(tfl: str, counts: dict) -> str:
    if counts["sr"] + counts["sd"] == 0:
        return f"No support, resistance, supply or demand found on {tfl}."
    far = f" ({counts['far']} more are too far from price to matter now)" if counts["far"] else ""
    return (f"Nothing valid on {tfl}: none of its {counts['sr']} S/R zones has {MIN_TOUCHES}+ touches and held lately, "
            f"and none of its {counts['sd']} supply/demand zones is fresh with a strong move{far}.")


def build_step(tf: str, df: pd.DataFrame, higher: list[tuple[str, WalkZone]], source: str) -> WalkStep:
    """One timeframe of the walk. `higher` holds the zones already drawn above it (timeframe label, zone); the ones
    this timeframe confirms get its label in `confirmed_by`."""
    tfl = TF_LABEL.get(tf, tf.upper())
    if len(df) < MIN_CANDLES:
        return WalkStep(interval=tf, label=tfl, status="failed", data_source=source,
                        reason=f"Not enough {tfl} history ({len(df)} candles).")
    last = float(df["close"].iloc[-1])
    zones, counts = valid_zones(df)
    if not zones:
        return WalkStep(interval=tf, label=tfl, status="skipped", reason=_skip_reason(tfl, counts), data_source=source)

    new: list[WalkZone] = []
    overlays: list[BoxOverlay] = []
    confirms: list[str] = []
    for z, why in zones:
        same = next((hz for _, hz in higher
                     if Zone(hz.low, hz.high, hz.kind, 0, 0, 0, 0).overlap_ratio(z) > OVERLAP), None)
        if same is not None:
            if tfl not in same.confirmed_by:
                same.confirmed_by.append(tfl)
            confirms.append(f"{same.label} {_fmt(same.low)}–{_fmt(same.high)}")
            continue
        label = f"{tfl} {z.kind.title()}"
        wz = WalkZone(kind=z.kind, low=z.price_low, high=z.price_high, label=label, why=why,
                      distance_pct=_distance_pct(z, last), score=round(min(z.score, 1.0), 2))
        new.append(wz)
        color = COLORS.get(z.kind, GREEN)
        overlays.append(BoxOverlay(
            id=f"td-{tf}-{len(overlays)}", label=f"{label} ({why})", kind=z.kind, price_low=z.price_low,
            price_high=z.price_high, color=rgba(color, 0.2), border_color=rgba(color, 0.75),
            time_start=z.first_time, strength=wz.score))
    confirm_text = f" It confirms {', '.join(confirms)}." if confirms else ""
    if not new:
        return WalkStep(interval=tf, label=tfl, status="skipped", data_source=source,
                        reason=f"{tfl} only repeats the higher-timeframe levels.{confirm_text}")
    parts = [f"{z.kind} {_fmt(z.low)}–{_fmt(z.high)} ({z.why})" for z in sorted(new, key=lambda z: -z.high)]
    return WalkStep(interval=tf, label=tfl, status="drawn", zones=new, overlays=overlays, data_source=source,
                    reason=confirm_text.strip(), summary=f"{tfl}: " + "; ".join(parts) + "." + confirm_text)


async def walk(market: MarketData, symbol: str, start: str = DEFAULT_START, end: str = DEFAULT_END) -> TopDownResult:
    """Run the walk on `symbol` from `start` down to `end`."""
    tfs = ladder(start, end)
    results = await asyncio.gather(*(market.get_klines(symbol, tf, CANDLES) for tf in tfs), return_exceptions=True)
    steps: list[WalkStep] = []
    drawn: list[tuple[str, WalkZone]] = []
    sources: set[str] = set()
    last = 0.0
    for tf, res in zip(tfs, results):
        tfl = TF_LABEL.get(tf, tf.upper())
        if isinstance(res, BaseException):
            log.info("Top-down walk: %s %s failed: %s", symbol, tf, res)
            steps.append(WalkStep(interval=tf, label=tfl, status="failed", reason=f"{tfl} candles could not load."))
            continue
        candles, source = res
        sources.add(source)
        df = candles_to_df(candles)
        try:
            step = await asyncio.to_thread(build_step, tf, df, drawn, source)
        except Exception as exc:  # a detector choking on odd data skips the timeframe, not the walk
            log.info("Top-down walk: %s %s detector failed: %s", symbol, tf, exc)
            step = WalkStep(interval=tf, label=tfl, status="failed", reason=f"The {tfl} detectors failed.",
                            data_source=source)
        if len(df):
            last = float(df["close"].iloc[-1])
        drawn += [(step.label, z) for z in step.zones]
        steps.append(step)
    data_source = "synthetic" if sources == {"synthetic"} else "mixed" if len(sources) > 1 else (
        next(iter(sources)) if sources else "binance")
    notes = ["Synthetic demo data: these levels are not real prices."] if "synthetic" in sources else []
    if not any(s.status == "drawn" for s in steps):
        notes.append("No timeframe had a valid level to draw.")
    return TopDownResult(symbol=symbol, last_price=last, steps=steps, generated_at=int(time.time() * 1000),
                         data_source=data_source, notes=notes)


def walk_facts(res: TopDownResult) -> dict:
    """The walk for the narrator."""
    return {
        "last_price": res.last_price,
        "steps": [{"timeframe": s.label, "status": s.status,
                   "levels": [{"kind": z.kind, "low": z.low, "high": z.high, "why": z.why,
                               "distance_pct": z.distance_pct, "confirmed_by": z.confirmed_by} for z in s.zones],
                   "note": s.reason} for s in res.steps],
        "notes": res.notes,
    }


def describe_walk(res: TopDownResult) -> str:
    """The template answer for a walk."""
    lines = []
    for s in res.steps:
        if s.status == "drawn":
            conf = [f"{z.label} is confirmed by {'/'.join(z.confirmed_by)}" for z in s.zones if z.confirmed_by]
            lines.append(s.summary + (" " + "; ".join(conf) + "." if conf else ""))
        else:
            lines.append(f"Skipped {s.label}: {s.reason}")
    drawn = [s.label for s in res.drawn]
    head = (f"Top-down walk on {res.symbol}: drew {', '.join(drawn)}; each timeframe's levels stay visible on the "
            f"lower ones." if drawn else f"Top-down walk on {res.symbol}: no timeframe had a valid level.")
    return " ".join([head, *lines, *res.notes])
