"""Deterministic technical-analysis engine.

Turns OHLCV into chart primitives without any LLM involvement, so every price
the agent draws is grounded in the data:

* swing highs / lows      — `scipy.signal.find_peaks` with ATR-scaled prominence
* support / resistance    — hierarchical clustering (`scipy.cluster.hierarchy`)
                            of swing prices into zones, scored by touches,
                            recency and prominence
* supply / demand         — base-then-impulse detection, filtered to zones price
                            has not closed through since they formed
* window highs / lows     — high/low of the last *completed* higher-timeframe bar
* trendlines              — lines through the two latest swing highs / lows
* market structure        — HH / HL / LH / LL labels on swing points
* HTF confluence          — zones re-detected on the next two timeframes up;
                            overlapping zones score higher (`mark_confluence`)
* sweeps, FVGs, order blocks, ranges, triangles, double tops/bottoms
                          — `patterns.py`
* RSI, divergences, structure breaks, volume profile
                          — `indicators.py`

Trade plans built from these levels live in `trade_plan.py`.

All functions are pure and operate on a DataFrame with columns
time, open, high, low, close, volume (oldest → newest).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Literal

import numpy as np
import pandas as pd
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.signal import find_peaks

from .pricefmt import _fmt, price_decimals  # noqa: F401 (re-exported for agent.py)
from .indicators import rsi, rsi_divergence, structure_breaks, volume_profile, volume_stats
from .patterns import detect_double, detect_range, detect_triangle, fair_value_gaps, liquidity_sweeps, order_blocks
from .schemas import (
    AnalysisIntent,
    AnalysisStats,
    BoxOverlay,
    HorizontalLineOverlay,
    MarkerOverlay,
    TrendlineOverlay,
)
from .session_levels import level_lines
from .trade_plan import Level

TF_LABEL = {
    "1m": "M1", "5m": "M5", "15m": "M15", "30m": "M30", "1h": "H1", "3h": "H3",
    "4h": "H4", "1d": "D1", "1w": "W1", "1M": "MN",
}

RED, GREEN, ORANGE, TEAL, SLATE = "#ef4444", "#22c55e", "#f97316", "#14b8a6", "#94a3b8"


def rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r}, {g}, {b}, {alpha})"


# ------------------------------------------------------------- indicators


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's Average True Range."""
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=1).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def parabolic_sar(df: pd.DataFrame, step: float = 0.02, max_step: float = 0.2) -> pd.Series:
    """Classic Wilder Parabolic SAR."""
    high, low = df["high"].to_numpy(), df["low"].to_numpy()
    n = len(df)
    sar = np.full(n, np.nan)
    if n < 2:
        return pd.Series(sar, index=df.index)
    up = high[1] >= high[0]
    af = step
    ep = high[0] if up else low[0]
    sar[0] = low[0] if up else high[0]
    for i in range(1, n):
        prev = sar[i - 1]
        cur = prev + af * (ep - prev)
        if up:
            cur = min(cur, low[i - 1], low[i - 2] if i > 1 else low[i - 1])
            if low[i] < cur:
                up, cur, ep, af = False, ep, low[i], step
            elif high[i] > ep:
                ep, af = high[i], min(af + step, max_step)
        else:
            cur = max(cur, high[i - 1], high[i - 2] if i > 1 else high[i - 1])
            if high[i] > cur:
                up, cur, ep, af = True, ep, high[i], step
            elif low[i] < ep:
                ep, af = low[i], min(af + step, max_step)
        sar[i] = cur
    return pd.Series(sar, index=df.index)


# ------------------------------------------------------------------ swings


@dataclass
class Swing:
    idx: int
    time: int
    price: float
    kind: Literal["high", "low"]
    prominence: float
    structure: str = ""  # HH / LH / HL / LL


def find_swings(
    df: pd.DataFrame, atr_value: float, distance: int | None = None, prominence_atr: float = 1.0
) -> tuple[list[Swing], list[Swing]]:
    """Swing highs and lows via peak detection with ATR-scaled prominence."""
    n = len(df)
    if n < 5:
        return [], []
    distance = distance or max(3, min(12, n // 60))
    prominence = max(atr_value * prominence_atr, 1e-12)
    times = df["time"].to_numpy()

    def _collect(values: np.ndarray, kind: Literal["high", "low"]) -> list[Swing]:
        signal = values if kind == "high" else -values
        idx, props = find_peaks(signal, distance=distance, prominence=prominence)
        swings = [
            Swing(int(i), int(times[i]), float(values[i]), kind, float(p))
            for i, p in zip(idx, props["prominences"])
        ]
        prev: Swing | None = None
        for s in swings:
            if prev is not None:
                if kind == "high":
                    s.structure = "HH" if s.price > prev.price else "LH"
                else:
                    s.structure = "HL" if s.price > prev.price else "LL"
            prev = s
        return swings

    return _collect(df["high"].to_numpy(), "high"), _collect(df["low"].to_numpy(), "low")


# --------------------------------------------------------- S/R clustering


@dataclass
class Zone:
    price_low: float
    price_high: float
    kind: str  # resistance | support | supply | demand
    touches: int
    first_time: int
    last_idx: int
    score: float
    tests: int = 0
    meta: dict = field(default_factory=dict)

    @property
    def mid(self) -> float:
        return (self.price_low + self.price_high) / 2

    def overlap_ratio(self, other: "Zone") -> float:
        inter = min(self.price_high, other.price_high) - max(self.price_low, other.price_low)
        span = min(self.price_high - self.price_low, other.price_high - other.price_low)
        return max(0.0, inter) / span if span > 0 else 0.0


def cluster_levels(
    swings: Iterable[Swing], atr_value: float, last_price: float, n_bars: int, tolerance_atr: float = 0.6
) -> list[Zone]:
    """Group swing prices that sit within `tolerance_atr` ATRs into S/R zones."""
    swings = list(swings)
    if not swings or atr_value <= 0:
        return []
    prices = np.array([[s.price] for s in swings])
    if len(swings) == 1:
        labels = np.array([1])
    else:
        labels = fcluster(linkage(prices, method="complete"), t=atr_value * tolerance_atr, criterion="distance")

    max_prom = max(s.prominence for s in swings) or 1.0
    zones: list[Zone] = []
    for label in np.unique(labels):
        members = [s for s, lab in zip(swings, labels) if lab == label]
        lo = min(s.price for s in members)
        hi = max(s.price for s in members)
        # Give thin clusters a minimum visual height around their centre.
        min_h = atr_value * 0.3
        if hi - lo < min_h:
            mid = (hi + lo) / 2
            lo, hi = mid - min_h / 2, mid + min_h / 2
        highs = sum(1 for s in members if s.kind == "high")
        last_idx = max(s.idx for s in members)
        touches = len(members)
        recency = last_idx / max(n_bars - 1, 1)
        prom = np.mean([s.prominence for s in members]) / max_prom
        score = 0.45 * min(touches / 4, 1.0) + 0.30 * recency + 0.25 * prom
        if hi < last_price:
            kind = "support"
        elif lo > last_price:
            kind = "resistance"
        else:  # price is inside the zone: classify by which swings built it
            kind = "resistance" if highs >= touches - highs else "support"
        zones.append(Zone(lo, hi, kind, touches, min(s.time for s in members), last_idx, float(score),
                          meta={"swing_highs": highs, "swing_lows": touches - highs}))
    return _merge_adjacent(zones, atr_value * 0.25, atr_value * 1.5, last_price)


def _merge_adjacent(zones: list[Zone], gap: float, max_height: float, last_price: float) -> list[Zone]:
    """Merge S/R zones that overlap or sit within `gap` of each other, so the
    chart shows one band instead of a stack of slivers."""
    merged: list[Zone] = []
    for z in sorted(zones, key=lambda z: z.price_low):
        prev = merged[-1] if merged else None
        if (prev and z.price_low - prev.price_high <= gap
                and max(prev.price_high, z.price_high) - prev.price_low <= max_height):
            touches = prev.touches + z.touches
            prev.price_high = max(prev.price_high, z.price_high)
            prev.first_time = min(prev.first_time, z.first_time)
            prev.last_idx = max(prev.last_idx, z.last_idx)
            prev.score = min(1.0, max(prev.score, z.score) + 0.05)
            prev.touches = touches
            for k in ("swing_highs", "swing_lows"):
                prev.meta[k] = prev.meta.get(k, 0) + z.meta.get(k, 0)
            if prev.price_high < last_price:
                prev.kind = "support"
            elif prev.price_low > last_price:
                prev.kind = "resistance"
            else:
                prev.kind = "resistance" if prev.meta["swing_highs"] >= prev.meta["swing_lows"] else "support"
        else:
            merged.append(z)
    return merged


# ------------------------------------------------------- supply / demand


def supply_demand_zones(
    df: pd.DataFrame, atr_series: pd.Series, impulse_atr: float = 1.6, base_body_atr: float = 0.6, max_base: int = 3
) -> list[Zone]:
    """Find base → impulse formations and keep the ones price hasn't invalidated.

    Demand: small-bodied base followed by a strong bullish move. Zone spans the
    base's lowest low to its highest body top. Supply is the mirror image.
    A zone is invalidated once a later bar closes beyond its far edge.
    """
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    times = df["time"].to_numpy()
    a = atr_series.to_numpy()
    body = c - o
    n = len(df)
    zones: list[Zone] = []
    i = max_base + 1
    while i < n - 1:
        # Impulse = one big candle or two consecutive same-direction candles.
        move1 = body[i]
        move2 = body[i] + body[i + 1] if np.sign(body[i]) == np.sign(body[i + 1]) else move1
        move = move2 if abs(move2) > abs(move1) else move1
        if abs(move) < impulse_atr * a[i - 1] or abs(body[i]) < 0.5 * a[i - 1]:
            i += 1
            continue
        bullish = move > 0
        # Base: up to `max_base` small-bodied candles just before the impulse.
        j = i - 1
        while j >= i - max_base and abs(body[j]) <= base_body_atr * a[j]:
            j -= 1
        base = slice(j + 1, i) if j + 1 < i else slice(i - 1, i)
        if bullish:
            lo = float(l[base].min())
            hi = float(np.maximum(o[base], c[base]).max())
        else:
            hi = float(h[base].max())
            lo = float(np.minimum(o[base], c[base]).min())
        if hi - lo < 0.15 * a[i]:
            pad = (0.15 * a[i] - (hi - lo)) / 2
            lo, hi = lo - pad, hi + pad

        after = slice(i + 1, n)
        closes_after = c[after]
        if bullish:
            invalid = bool((closes_after < lo).any())
            # A "test" is a later bar wicking back into the zone.
            tests = int(((l[after] <= hi) & (l[after] >= lo)).sum())
        else:
            invalid = bool((closes_after > hi).any())
            tests = int(((h[after] >= lo) & (h[after] <= hi)).sum())
        if not invalid:
            strength = min(abs(move) / (a[i - 1] * impulse_atr * 2), 1.0)
            freshness = 1.0 / (1 + tests)
            zones.append(Zone(lo, hi, "demand" if bullish else "supply", 1, int(times[base.start]), i,
                              float(0.6 * strength + 0.4 * freshness), tests=tests,
                              meta={"impulse_atr": round(abs(move) / a[i - 1], 2)}))
        i += 2
    return _dedupe_zones(zones)


def _dedupe_zones(zones: list[Zone]) -> list[Zone]:
    """Merge overlapping zones of the same kind, keeping the higher score."""
    zones = sorted(zones, key=lambda z: -z.score)
    kept: list[Zone] = []
    for z in zones:
        if all(not (k.kind == z.kind and k.overlap_ratio(z) > 0.3) for k in kept):
            kept.append(z)
    return kept


def pick_nearest(zones: list[Zone], last_price: float, per_side: int) -> list[Zone]:
    """Keep the `per_side` strongest-near zones above and below price.

    Ranking blends proximity and score so a strong zone slightly further away
    beats a weak one hugging price.
    """
    above = [z for z in zones if z.mid >= last_price]
    below = [z for z in zones if z.mid < last_price]

    def rank(z: Zone) -> float:
        dist = abs(z.mid - last_price) / last_price
        return z.score - dist * 8

    return sorted(above, key=rank, reverse=True)[:per_side] + sorted(below, key=rank, reverse=True)[:per_side]


# --------------------------------------------------------- window levels


def window_levels(df: pd.DataFrame, tf: str) -> dict[str, float | int] | None:
    """High/low of the last completed bar of a higher-timeframe series."""
    if len(df) < 2:
        return None
    bar = df.iloc[-2]
    return {"high": float(bar["high"]), "low": float(bar["low"]), "time": int(bar["time"]),
            "tf": tf}


# ------------------------------------------------------------- confluence

HTF_LADDER: tuple[str, ...] = ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M")


def higher_timeframes(tf: str, n: int = 2) -> list[str]:
    """The next `n` timeframes up the ladder (3h sits between 1h and 4h)."""
    if tf == "3h":
        return ["4h", "1d"][:n]
    if tf not in HTF_LADDER:
        return []
    i = HTF_LADDER.index(tf)
    return list(HTF_LADDER[i + 1:i + 1 + n])


def htf_zones(df: pd.DataFrame) -> list[Zone]:
    """S/R and supply/demand zones on a higher-timeframe frame, for confluence checks."""
    df = df.reset_index(drop=True)
    if len(df) < 30:
        return []
    atr_s = atr(df)
    a = float(atr_s.iloc[-1])
    highs, lows = find_swings(df, a)
    return cluster_levels(highs + lows, a, float(df["close"].iloc[-1]), len(df)) + supply_demand_zones(df, atr_s)


def mark_confluence(zones: list[Zone], frames: dict[str, list[Zone]]) -> None:
    """Tag zones that overlap a zone on a higher timeframe and raise their score."""
    for z in zones:
        hits = [TF_LABEL.get(tf, tf) for tf, hz in frames.items()
                if any(min(z.price_high, h.price_high) > max(z.price_low, h.price_low) for h in hz)]
        if hits:
            z.meta["htf"] = hits
            z.score = min(1.0, z.score + 0.15 * len(hits))


# --------------------------------------------------------------- engine


@dataclass
class AnalysisResult:
    overlays: list
    stats: AnalysisStats
    facts: dict
    levels: list[Level] = field(default_factory=list)  # everything detected, for trade plans and scans
    swing_highs: list[float] = field(default_factory=list)
    swing_lows: list[float] = field(default_factory=list)
    bias: str | None = None  # direction of the latest structure break


def _zone_fact(z: Zone, last: float, atr_v: float, count_key: str, count: int) -> dict:
    inside = z.price_low <= last <= z.price_high
    edge = z.price_low if z.price_low > last else z.price_high
    out = {"low": z.price_low, "high": z.price_high, count_key: count,
           "distance_atr": 0.0 if inside else round(abs(edge - last) / atr_v, 2), "inside": inside}
    if z.meta.get("htf"):
        out["htf_confluence"] = z.meta["htf"]
    return out


def _zone_label(tfl: str, z: Zone, suffix: str = "") -> str:
    htf = z.meta.get("htf")
    return f"{tfl} {z.kind.title()}{suffix}" + (f" + {'/'.join(htf)}" if htf else "")


def analyze(
    df: pd.DataFrame,
    intent: AnalysisIntent,
    tf: str,
    higher_tf: dict[str, pd.DataFrame] | None = None,
    confluence: dict[str, pd.DataFrame] | None = None,
) -> AnalysisResult:
    """Run the requested detectors and return overlays + stats + facts for narration.

    `higher_tf` holds the frames for window levels; `confluence` the higher-timeframe frames whose zones
    are checked against this timeframe's zones."""
    if len(df) < 30:
        raise ValueError("Need at least 30 candles for analysis")
    df = df.reset_index(drop=True)
    tfl = TF_LABEL.get(tf, tf.upper())
    atr_s = atr(df)
    atr_v = float(atr_s.iloc[-1])
    last = float(df["close"].iloc[-1])
    last_time = int(df["time"].iloc[-1])
    ema_f = ema(df["close"], 20)
    ema_s = ema(df["close"], 50)
    highs, lows = find_swings(df, atr_v)

    slope = (ema_s.iloc[-1] - ema_s.iloc[-10]) / atr_v if len(df) > 10 else 0
    if ema_f.iloc[-1] > ema_s.iloc[-1] and slope > 0.2:
        trend = "up"
    elif ema_f.iloc[-1] < ema_s.iloc[-1] and slope < -0.2:
        trend = "down"
    else:
        trend = "range"

    overlays: list = []
    facts: dict = {"timeframe": tfl, "last_price": last, "trend": trend, "atr": atr_v,
                   "atr_pct": round(atr_v / last * 100, 2) if last else None}
    levels: list[Level] = []
    feats = set(intent.features)
    sr_zones: list[Zone] = []
    htf = {wtf: htf_zones(frame) for wtf, frame in (confluence or {}).items()}

    # Momentum, structure and volume are cheap and always useful context for the answer.
    rsi_s = rsi(df["close"])
    rsi_v = float(rsi_s.iloc[-1]) if not np.isnan(rsi_s.iloc[-1]) else None
    momentum: dict = {"rsi": round(rsi_v, 1) if rsi_v is not None else None}
    div = rsi_divergence(df, rsi_s, highs, lows)
    if div:
        momentum["divergence"] = f"{div['kind']} {div['type']} ({_fmt(div['price1'])} → {_fmt(div['price2'])})"
    facts["momentum"] = momentum
    breaks = structure_breaks(df, highs, lows)
    bias = None
    if breaks:
        b = breaks[-1]
        bias = b["direction"]
        facts["last_structure_break"] = {"type": b["type"], "direction": b["direction"], "level": b["level"],
                                         "bars_ago": len(df) - 1 - b["idx"]}
    facts["volume"] = volume_stats(df)

    if "support_resistance" in feats:
        zones = cluster_levels(highs + lows, atr_v, last, len(df))
        mark_confluence(zones, htf)
        sr_zones = pick_nearest(zones, last, intent.max_zones)
        for z in sr_zones:
            color = RED if z.kind == "resistance" else GREEN
            label = _zone_label(tfl, z)
            overlays.append(BoxOverlay(
                label=label, kind=z.kind, price_high=z.price_high, price_low=z.price_low,
                color=rgba(color, 0.22), border_color=rgba(color, 0.7), time_start=z.first_time,
                strength=round(min(z.score, 1.0), 2),
            ))
            levels.append(Level(z.kind, z.price_low, z.price_high, label, z.score))
        facts["resistance"] = sorted([_zone_fact(z, last, atr_v, "touches", z.touches) for z in sr_zones
                                      if z.kind == "resistance"], key=lambda f: f["low"])
        facts["support"] = sorted([_zone_fact(z, last, atr_v, "touches", z.touches) for z in sr_zones
                                   if z.kind == "support"], key=lambda f: -f["high"])

    if "supply_demand" in feats:
        sd = supply_demand_zones(df, atr_s)
        sd = [z for z in sd if not any(z.overlap_ratio(s) > 0.5 for s in sr_zones)]
        mark_confluence(sd, htf)
        sd = pick_nearest(sd, last, intent.max_zones)
        for z in sd:
            color = ORANGE if z.kind == "supply" else TEAL
            label = _zone_label(tfl, z, " (fresh)" if z.tests == 0 else "")
            overlays.append(BoxOverlay(
                label=label, kind=z.kind, price_high=z.price_high, price_low=z.price_low, color=rgba(color, 0.18),
                border_color=rgba(color, 0.65), time_start=z.first_time, strength=round(min(z.score, 1.0), 2),
            ))
            levels.append(Level(z.kind, z.price_low, z.price_high, label, z.score))
        facts["supply"] = sorted([_zone_fact(z, last, atr_v, "tests", z.tests) for z in sd if z.kind == "supply"],
                                 key=lambda f: f["low"])
        facts["demand"] = sorted([_zone_fact(z, last, atr_v, "tests", z.tests) for z in sd if z.kind == "demand"],
                                 key=lambda f: -f["high"])

    if "window_levels" in feats:
        facts["windows"] = []
        for wtf, wdf in (higher_tf or {}).items():
            lv = window_levels(wdf.reset_index(drop=True), wtf)
            if not lv:
                continue
            wl = TF_LABEL.get(wtf, wtf.upper())
            style = "solid" if wtf in ("1h", "3h", "4h") else "dashed"
            overlays.append(HorizontalLineOverlay(label=f"{wl} window high", kind="window_high", price=lv["high"],
                                                  color=RED, line_style=style, time_start=lv["time"]))
            overlays.append(HorizontalLineOverlay(label=f"{wl} window low", kind="window_low", price=lv["low"],
                                                  color=GREEN, line_style=style, time_start=lv["time"]))
            levels.append(Level("window_high", lv["high"], lv["high"], f"{wl} window high", 0.5))
            levels.append(Level("window_low", lv["low"], lv["low"], f"{wl} window low", 0.5))
            facts["windows"].append({"tf": wl, "high": lv["high"], "low": lv["low"]})

    if "swings" in feats:
        for s in sorted(highs[-6:] + lows[-6:], key=lambda s: s.idx):
            is_high = s.kind == "high"
            overlays.append(MarkerOverlay(
                time=s.time, price=s.price, position="above" if is_high else "below",
                shape="arrowDown" if is_high else "arrowUp", label=s.structure or ("SH" if is_high else "SL"),
                color=RED if is_high else GREEN, kind=f"swing_{s.kind}",
            ))
        facts["structure"] = [s.structure for s in sorted(highs[-3:] + lows[-3:], key=lambda s: s.idx)
                              if s.structure]

    if "trendlines" in feats:
        for pts, color, name in ((highs, RED, "Descending resistance"), (lows, GREEN, "Ascending support")):
            if len(pts) < 2:
                continue
            a, b = pts[-2], pts[-1]
            falling = b.price < a.price
            # Only the "meaningful" direction: lower highs or higher lows.
            if (pts is highs and falling) or (pts is lows and not falling):
                overlays.append(TrendlineOverlay(
                    time1=a.time, price1=a.price, time2=b.time, price2=b.price, extend_right=True,
                    color=color, label=f"{tfl} {name}", kind="trendline",
                ))

    if "liquidity_sweeps" in feats:
        sweeps = liquidity_sweeps(df, highs, lows)[:3]
        for sw in sweeps:
            bearish = sw["direction"] == "bearish"
            color = RED if bearish else GREEN
            overlays.append(HorizontalLineOverlay(
                label=f"{tfl} swept {'high' if bearish else 'low'}", kind="sweep", price=sw["level"], color=color,
                line_style="dotted", time_start=sw["swing_time"]))
            overlays.append(MarkerOverlay(
                time=sw["time"], price=sw["extreme"], position="above" if bearish else "below",
                shape="arrowDown" if bearish else "arrowUp", label="Sweep", color=color, kind="sweep"))
        facts["sweeps"] = [{"direction": sw["direction"], "side": sw["side"], "level": sw["level"],
                            "bars_ago": len(df) - 1 - int(df.index[df["time"] == sw["time"]][0])} for sw in sweeps]

    if "fvg" in feats:
        gaps = fair_value_gaps(df, atr_v)
        gaps = ([g for g in gaps if g["direction"] == "bullish"][:intent.max_zones]
                + [g for g in gaps if g["direction"] == "bearish"][:intent.max_zones])
        for g in gaps:
            color = "#22d3ee" if g["direction"] == "bullish" else "#f472b6"
            kind = f"fvg_{g['direction']}"
            label = f"{tfl} {g['direction'].title()} FVG" + (" (part filled)" if g.get("filled_pct", 0) > 0 else "")
            overlays.append(BoxOverlay(label=label, kind=kind, price_low=g["price_low"], price_high=g["price_high"],
                                       color=rgba(color, 0.14), border_color=rgba(color, 0.55), time_start=g["time"]))
            levels.append(Level(kind, g["price_low"], g["price_high"], label, 0.45))
        facts["fvg"] = [{"direction": g["direction"], "low": g["price_low"], "high": g["price_high"],
                         "filled_pct": g.get("filled_pct", 0)} for g in gaps]

    if "order_blocks" in feats:
        obs = order_blocks(df, highs, lows, atr_s)
        obs = ([o for o in obs if o["direction"] == "bullish"][:intent.max_zones]
               + [o for o in obs if o["direction"] == "bearish"][:intent.max_zones])
        for o in obs:
            color = "#60a5fa" if o["direction"] == "bullish" else "#e879f9"
            kind = f"ob_{o['direction']}"
            label = f"{tfl} {o['direction'].title()} OB"
            overlays.append(BoxOverlay(label=label, kind=kind, price_low=o["price_low"], price_high=o["price_high"],
                                       color=rgba(color, 0.16), border_color=rgba(color, 0.6), time_start=o["time"]))
            levels.append(Level(kind, o["price_low"], o["price_high"], label, 0.6 / (1 + o.get("tests", 0))))
        facts["order_blocks"] = [{"direction": o["direction"], "low": o["price_low"], "high": o["price_high"],
                                  "tests": o.get("tests", 0)} for o in obs]

    if "patterns" in feats:
        found: list[str] = []
        rng = detect_range(df, atr_v, min_bars=30)
        if rng:
            overlays.append(BoxOverlay(label=f"{tfl} Range ({rng['bars']} bars)", kind="pattern_range",
                                       price_low=rng["price_low"], price_high=rng["price_high"],
                                       color=rgba(SLATE, 0.10), border_color=rgba(SLATE, 0.6),
                                       time_start=rng["time_start"], time_end=rng["time_end"]))
            levels.append(Level("support", rng["price_low"], rng["price_low"], "Range low", 0.5))
            levels.append(Level("resistance", rng["price_high"], rng["price_high"], "Range high", 0.5))
            found.append(f"range {_fmt(rng['price_low'])}–{_fmt(rng['price_high'])} for {rng['bars']} bars")
        tri = detect_triangle(df, highs, lows, atr_v)
        if tri:
            name = tri["type"].replace("_", " ")
            for side, color in (("upper", RED), ("lower", GREEN)):
                ln = tri[side]
                overlays.append(TrendlineOverlay(time1=ln["time1"], price1=ln["price1"], time2=ln["time2"],
                                                 price2=ln["price2"], extend_right=True, color=color,
                                                 label=f"{tfl} {name.capitalize()}", kind="pattern_line"))
            found.append(name + (f", broken {tri['broken']}" if tri.get("broken") else ""))
        dbl = detect_double(df, highs, lows, atr_v)
        if dbl:
            top = dbl["type"] == "double_top"
            for n, (t, p) in enumerate(((dbl["time1"], dbl["price1"]), (dbl["time2"], dbl["price2"])), 1):
                overlays.append(MarkerOverlay(time=t, price=p, position="above" if top else "below",
                                              shape="circle", label=f"{'Top' if top else 'Bottom'} {n}",
                                              color=RED if top else GREEN, kind="pattern_point"))
            overlays.append(HorizontalLineOverlay(label="Double top neckline" if top else "Double bottom neckline",
                                                  kind="pattern_neckline", price=dbl["neckline"], color=SLATE,
                                                  line_style="dashed", time_start=dbl["time1"]))
            found.append(f"{dbl['type'].replace('_', ' ')} with neckline {_fmt(dbl['neckline'])}"
                         + (" (confirmed)" if dbl["confirmed"] else " (not confirmed)"))
        facts["patterns"] = found

    if "volume_profile" in feats:
        vp = volume_profile(df)
        if vp:
            for key, color, style, width in (("poc", "#facc15", "solid", 2), ("vah", SLATE, "dashed", 1),
                                             ("val", SLATE, "dashed", 1)):
                overlays.append(HorizontalLineOverlay(label=f"{tfl} {key.upper()}", kind=key, price=vp[key],
                                                      color=color, line_style=style, line_width=width))
                levels.append(Level(key, vp[key], vp[key], key.upper(), 0.5))
            facts["volume_profile"] = vp

    for i, ov in enumerate(overlays):
        ov.id = f"ai-{ov.type}-{i}"
        for name in ("price", "price_high", "price_low", "price1", "price2"):
            if getattr(ov, name, None) is not None:
                setattr(ov, name, float(f"{getattr(ov, name):.6g}"))

    stats = AnalysisStats(
        last_price=last, atr=atr_v, trend=trend, ema_fast=float(ema_f.iloc[-1]), ema_slow=float(ema_s.iloc[-1]),
        swing_highs=len(highs), swing_lows=len(lows),
    )
    facts["last_bar_time"] = last_time
    return AnalysisResult(overlays, stats, facts, levels, [s.price for s in highs], [s.price for s in lows], bias)


def _touches(n: int) -> str:
    return f"{n} touch" if n == 1 else f"{n} touches"


def _where(z: dict) -> str:
    if z.get("inside"):
        return ", price inside"
    return f", {z['distance_atr']} ATR away" if z.get("distance_atr") is not None else ""


def _htf(z: dict) -> str:
    return f", lines up with {'/'.join(z['htf_confluence'])}" if z.get("htf_confluence") else ""


def _kimi_lines(k: dict) -> list[str]:
    if k.get("error"):
        return [k["error"] + "."]
    out = [f"{k['indicator']}:"]
    f = k.get("forecast")
    if f:
        nxt = f" Next candle: {'▲' if f['next_candle'] == 'up' else '▼'}." if f.get("next_candle") else ""
        out.append(f"{f['headline']} over {f['bars']} bars, range {_fmt(f['range'][0])}–{_fmt(f['range'][1])} "
                   f"({f['volatility'].lower()} volatility).{nxt}")
    for lv in k.get("levels", [])[:4]:
        odds = f", {lv['odds_pct']}% to reach" if lv.get("odds_pct") is not None else ""
        out.append(f"{lv['side'].title()} {_fmt(lv['price'])}{odds}.")
    if k.get("recent_signals"):
        s = k["recent_signals"][-1]
        out.append(f"Last signal {s['label']} ({s['direction']}) at {_fmt(s['price'])}, {s['bars_ago']} bars ago, "
                   f"{s['result']}.")
    return out


def _futures_lines(f: dict) -> list[str]:
    bits = []
    if (fu := f.get("funding")) and fu.get("rate_pct") is not None:
        bits.append(f"funding {fu['rate_pct']}%")
    if (oi := f.get("open_interest")) and oi.get("change_24h_pct") is not None:
        bits.append(f"open interest {oi['change_24h_pct']:+}% in 24h")
    if (ls := f.get("long_short")) and ls.get("ratio") is not None:
        bits.append(f"long/short {ls['ratio']}")
    if (cv := f.get("cvd_24h")) and cv.get("direction"):
        bits.append(f"24h spot flow: {cv['direction']} ({cv['buy_pct']}% taker buys)")
    out = ["Futures: " + ", ".join(bits) + "."] if bits else []
    liq = f.get("est_liquidations") or {}
    near = [f"{side} {_fmt(c['price_low'])}–{_fmt(c['price_high'])}" for side, c in
            (("above", liq.get("above")), ("below", liq.get("below"))) if c]
    if near:
        out.append("Estimated liquidation clusters " + ", ".join(near) + ".")
    return out


def describe(facts: dict, symbol: str) -> str:
    """Plain-English summary of the analysis, used when no LLM is configured."""
    tf = facts["timeframe"]
    last = facts["last_price"]
    lines = [f"{symbol} on {tf}: last {_fmt(last)}, trend {facts['trend']} "
             f"(ATR {_fmt(facts['atr'], last)}, {facts['atr'] / last * 100:.2f}% of price)."]
    lines += facts.get("navigation", [])
    if facts.get("resistance"):
        z = facts["resistance"][0]
        lines.append(f"Nearest {tf} resistance {_fmt(z['low'])}–{_fmt(z['high'])} ({_touches(z['touches'])}"
                     f"{_where(z)}{_htf(z)}).")
    if facts.get("support"):
        z = facts["support"][0]
        lines.append(f"Nearest {tf} support {_fmt(z['low'])}–{_fmt(z['high'])} ({_touches(z['touches'])}"
                     f"{_where(z)}{_htf(z)}).")
    if "supply" in facts and not facts["supply"]:
        lines.append(f"No unmitigated {tf} supply zone above price right now.")
    if "demand" in facts and not facts["demand"]:
        lines.append(f"No unmitigated {tf} demand zone below price right now.")
    for key in ("supply", "demand"):
        if facts.get(key):
            z = facts[key][0]
            tested = " (untested" if z["tests"] == 0 else f" (tested {z['tests']}x"
            lines.append(f"Unmitigated {key} {_fmt(z['low'])}–{_fmt(z['high'])}{tested}{_where(z)}{_htf(z)}).")
    for w in facts.get("windows", []):
        lines.append(f"{w['tf']} window: high {_fmt(w['high'])}, low {_fmt(w['low'])}.")
    if facts.get("structure"):
        lines.append("Recent structure: " + " → ".join(facts["structure"]) + ".")
    br = facts.get("last_structure_break")
    if br and (facts.get("structure") is not None or br["bars_ago"] <= 20):
        lines.append(f"Last break: {br['direction']} {br['type']} through {_fmt(br['level'])}, "
                     f"{br['bars_ago']} bars ago.")
    mom = facts.get("momentum") or {}
    if mom.get("divergence"):
        lines.append(f"RSI {mom.get('rsi')} with {mom['divergence']} divergence.")
    elif mom.get("rsi") is not None and (mom["rsi"] >= 70 or mom["rsi"] <= 30):
        lines.append(f"RSI {mom['rsi']} ({'overbought' if mom['rsi'] >= 70 else 'oversold'}).")
    for sw in facts.get("sweeps", [])[:2]:
        lines.append(f"{sw['direction'].title()} sweep of {_fmt(sw['level'])} {sw['bars_ago']} bars ago.")
    if "sweeps" in facts and not facts["sweeps"]:
        lines.append("No liquidity sweeps in the last 30 bars.")
    for g in facts.get("fvg", [])[:2]:
        lines.append(f"Unfilled {g['direction']} FVG {_fmt(g['low'])}–{_fmt(g['high'])}.")
    for o in facts.get("order_blocks", [])[:2]:
        lines.append(f"{o['direction'].title()} order block {_fmt(o['low'])}–{_fmt(o['high'])}.")
    if facts.get("patterns"):
        lines.append("Patterns: " + "; ".join(facts["patterns"]) + ".")
    elif "patterns" in facts:
        lines.append("No range, triangle, wedge or double top/bottom right now.")
    if facts.get("volume_profile"):
        vp = facts["volume_profile"]
        lines.append(f"Volume profile: POC {_fmt(vp['poc'])}, value area {_fmt(vp['val'])}–{_fmt(vp['vah'])}.")
    if facts.get("derivatives") and not facts.get("futures_context"):
        d = facts["derivatives"]
        bits = []
        if d.get("funding_rate_pct") is not None:
            bits.append(f"funding {d['funding_rate_pct']}%")
        if d.get("oi_change_24h_pct") is not None:
            bits.append(f"open interest {d['oi_change_24h_pct']:+}% in 24h")
        if bits:
            lines.append("Futures: " + ", ".join(bits) + ".")
    if facts.get("futures_context"):
        lines += _futures_lines(facts["futures_context"])
    if facts.get("session_levels"):
        lines += level_lines(facts["session_levels"])
    if facts.get("upcoming_events"):
        ev = facts["upcoming_events"]
        lines.append("Coming up: " + "; ".join(f"{e['country']} {e['title']} in {e['in_hours']:.0f}h" for e in ev[:3])
                     + ".")
    elif "upcoming_events" in facts and not facts.get("plan"):
        lines.append("No high-impact economic events in the next 48 hours.")
    if facts.get("headlines"):
        lines.append("Headlines: " + "; ".join(h["title"] for h in facts["headlines"][:3]) + ".")
    if facts.get("plan"):
        p = facts["plan"]
        tgts = ", ".join(f"{_fmt(t['price'])} ({t['rr']}R)" for t in p["targets"])
        lines.append(f"{p['direction'].title()} plan from {p['basis']}: entry {_fmt(p['entry'])}, "
                     f"stop {_fmt(p['stop'])}, targets {tgts}.")
        lines += p.get("notes", [])
    elif "plan" in facts:
        lines.append("No clean trade plan here: no zone or swing to put a stop behind.")
    if facts.get("scan"):
        best = facts["scan"][:3]
        lines.append("Watchlist: " + "; ".join(f"{r['symbol']} " + (", ".join(r["signals"][:2]) or r["trend"])
                                               for r in best) + ".")
    elif "scan" in facts:
        lines.append("Couldn't scan the watchlist.")
    if facts.get("kimi"):
        lines += _kimi_lines(facts["kimi"])
    lines += facts.get("actions", [])
    if len(lines) == 1:
        lines.append("No qualifying levels found for this request.")
    return " ".join(lines)
