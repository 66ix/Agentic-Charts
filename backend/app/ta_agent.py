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

import re
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
    IndicatorLengths,
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


def _last(series: pd.Series) -> float | None:
    v = float(series.iloc[-1]) if len(series) else float("nan")
    return None if np.isnan(v) else v


def _price(v: float | None) -> float | None:
    return None if v is None else float(f"{v:.6g}")


def _cross_bars_ago(diff: pd.Series, lookback: int = 50) -> tuple[str, int] | None:
    """The latest sign change of `diff` (a - b) in the last `lookback` bars: ("bullish" | "bearish", bars ago)."""
    sign = np.sign(diff.dropna().to_numpy()[-(lookback + 1):])
    for i in range(len(sign) - 1, 0, -1):
        if sign[i] != sign[i - 1] and sign[i] != 0:
            return ("bullish" if sign[i] > 0 else "bearish"), len(sign) - 1 - i
    return None


def indicator_snapshot(df: pd.DataFrame, lengths: IndicatorLengths | None = None) -> dict:
    """The latest value of every chart indicator, whether or not it is switched on, with the chart's own lengths
    (frontend/lib/indicators.ts; the defaults are EMA 20/50, RSI 14, MACD 12/26/9, Bollinger 20/2, ATR 14 and
    Stoch RSI 14/14/3/3): VWAP is the daily session below 1d and anchored at the first bar above, like the chart."""
    ln = lengths or IndicatorLengths()
    close = df["close"]
    last = float(close.iloc[-1])
    out: dict = {"lengths": ln.model_dump()}

    ef, es = ema(close, ln.ema_fast), ema(close, ln.ema_slow)
    out["ema_fast"], out["ema_slow"] = _price(_last(ef)), _price(_last(es))
    out["price_vs_ema"] = ("above both" if last > max(out["ema_fast"], out["ema_slow"]) else
                           "below both" if last < min(out["ema_fast"], out["ema_slow"]) else "between")
    if (x := _cross_bars_ago(ef - es)):
        out["ema_cross"] = {"type": "golden" if x[0] == "bullish" else "death", "bars_ago": x[1]}

    rsi_s = rsi(close, ln.rsi)
    if (rv := _last(rsi_s)) is not None:
        out["rsi"] = round(rv, 1)

    m = ln.macd
    if len(df) >= max(m.fast, m.slow) + m.signal:
        line = ema(close, m.fast) - ema(close, m.slow)
        signal = ema(line, m.signal)
        hist = line - signal
        out["macd"] = {"macd": _price(_last(line)), "signal": _price(_last(signal)), "hist": _price(_last(hist)),
                       "hist_rising": bool(hist.iloc[-1] > hist.iloc[-2])}
        if (x := _cross_bars_ago(hist)):
            out["macd"]["last_cross"] = {"direction": x[0], "bars_ago": x[1]}

    mid = close.rolling(ln.bb.length).mean()
    sd = close.rolling(ln.bb.length).std(ddof=0)
    if (mv := _last(mid)) is not None:
        up, lo = mv + ln.bb.mult * float(sd.iloc[-1]), mv - ln.bb.mult * float(sd.iloc[-1])
        out["bollinger"] = {"upper": _price(up), "mid": _price(mv), "lower": _price(lo),
                            "pct_b": round((last - lo) / (up - lo), 2) if up > lo else None,
                            "width_pct": round((up - lo) / mv * 100, 2) if mv else None}

    st = ln.stoch_rsi
    srsi = rsi_s if st.rsiLength == ln.rsi else rsi(close, st.rsiLength)
    lo_r, hi_r = srsi.rolling(st.stochLength).min(), srsi.rolling(st.stochLength).max()
    raw = ((srsi - lo_r) / (hi_r - lo_r).replace(0, np.nan) * 100).fillna(0).where(lo_r.notna())
    k = raw.rolling(st.k).mean()
    d = k.rolling(st.d).mean()
    if (kv := _last(k)) is not None and (dv := _last(d)) is not None:
        out["stoch_rsi"] = {"k": round(kv, 1), "d": round(dv, 1),
                            "zone": "overbought" if kv >= 80 else "oversold" if kv <= 20 else "neutral"}

    times = df["time"].to_numpy()
    intraday = len(times) > 1 and float(np.median(np.diff(times))) < 86400
    tp = (df["high"] + df["low"] + close) / 3
    pv, vol = tp * df["volume"], df["volume"]
    if intraday:
        day = pd.Series(times // 86400, index=df.index)
        pv, vol = pv.groupby(day).cumsum(), vol.groupby(day).cumsum()
    else:
        pv, vol = pv.cumsum(), vol.cumsum()
    vw = float(pv.iloc[-1] / vol.iloc[-1]) if float(vol.iloc[-1]) > 0 else float(tp.iloc[-1])
    out["vwap"] = {"value": _price(vw), "price": "above" if last > vw else "below",
                   "anchor": "UTC day" if intraday else "first bar"}

    if (sar := _last(parabolic_sar(df))) is not None:
        out["psar"] = {"value": _price(sar), "trend": "up" if sar < last else "down"}
    if (av := _last(atr(df, ln.atr))) is not None:
        out["atr"] = _price(av)
    return out


def htf_readings(frames: dict[str, pd.DataFrame], lengths: IndicatorLengths | None = None) -> dict:
    """The higher timeframes in brief, so "does the daily agree?" needs no second question: trend from the EMAs,
    RSI, MACD histogram, Stoch RSI zone and price against the EMAs and VWAP on each."""
    out: dict = {}
    for tf, df in frames.items():
        if len(df) < 60:
            continue
        snap = indicator_snapshot(df.reset_index(drop=True), lengths)
        fast, slow, where = snap["ema_fast"], snap["ema_slow"], snap["price_vs_ema"]
        row: dict = {"trend": "up" if fast > slow and where == "above both" else
                     "down" if fast < slow and where == "below both" else "mixed",
                     "price_vs_ema": where, "rsi": snap.get("rsi"), "price_vs_vwap": snap["vwap"]["price"]}
        if (m := snap.get("macd")):
            row["macd_hist"] = m["hist"]
            row["macd_momentum"] = ("rising" if m["hist_rising"] else "falling")
        if (st := snap.get("stoch_rsi")):
            row["stoch_rsi_zone"] = st["zone"]
        out[TF_LABEL.get(tf, tf)] = row
    return out


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


HOLD_ATR = 1.0    # a test held when price then closes this many ATRs away from the zone, on its side
BREAK_ATR = 0.25  # and failed when it closes this far through the zone first
AWAY_ATR = 0.3    # price must have closed this far away before touching again counts as a new test


def zone_record(df: pd.DataFrame, z: Zone, atr_v: float) -> dict:
    """How `z` held up after it formed: each time price came back to it from its current side (from above for
    support/demand, from below for resistance/supply) is a test; it held when price then closed HOLD_ATR away on
    that side before closing BREAK_ATR through the zone, else it broke. A test still open at the last candle is
    left out. → {"held", "broke", "tests"}."""
    times = df["time"].to_numpy(dtype=np.int64)
    start = int(np.searchsorted(times, z.first_time, "right"))
    if z.kind in ("supply", "demand"):
        start = max(start, z.last_idx + 1)  # after the impulse that made it
    below = z.kind in ("support", "demand")  # price sits above it
    h, lo_, c = (df[k].to_numpy(dtype=float) for k in ("high", "low", "close"))
    zl, zh = z.price_low, z.price_high
    held = broke = 0
    away = False
    testing = False
    for i in range(start, len(df)):
        if testing:
            if (below and c[i] >= zh + HOLD_ATR * atr_v) or (not below and c[i] <= zl - HOLD_ATR * atr_v):
                held, testing, away = held + 1, False, True
            elif (below and c[i] < zl - BREAK_ATR * atr_v) or (not below and c[i] > zh + BREAK_ATR * atr_v):
                broke, testing, away = broke + 1, False, False
            continue
        if not away:
            away = c[i] >= zh + AWAY_ATR * atr_v if below else c[i] <= zl - AWAY_ATR * atr_v
            continue
        if (below and lo_[i] <= zh) or (not below and h[i] >= zl):
            testing = True
    return {"held": held, "broke": broke, "tests": held + broke}


def _zone_fact(z: Zone, last: float, atr_v: float, count_key: str, count: int) -> dict:
    inside = z.price_low <= last <= z.price_high
    edge = z.price_low if z.price_low > last else z.price_high
    out = {"low": z.price_low, "high": z.price_high, count_key: count,
           "distance_atr": 0.0 if inside else round(abs(edge - last) / atr_v, 2), "inside": inside}
    if z.meta.get("htf"):
        out["htf_confluence"] = z.meta["htf"]
    if z.meta.get("record", {}).get("tests"):
        out["held"], out["tests_resolved"] = z.meta["record"]["held"], z.meta["record"]["tests"]
    return out


def _zone_label(tfl: str, z: Zone, suffix: str = "") -> str:
    htf = z.meta.get("htf")
    rec = z.meta.get("record")
    tail = f" · {rec['held']}/{rec['tests']} held" if rec and rec.get("tests") else ""
    return f"{tfl} {z.kind.title()}{suffix}" + (f" + {'/'.join(htf)}" if htf else "") + tail


def analyze(
    df: pd.DataFrame,
    intent: AnalysisIntent,
    tf: str,
    higher_tf: dict[str, pd.DataFrame] | None = None,
    confluence: dict[str, pd.DataFrame] | None = None,
    lengths: IndicatorLengths | None = None,
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
    # Every chart indicator's latest value, whether or not the user has it on the chart.
    facts["indicators"] = indicator_snapshot(df, lengths)
    breaks = structure_breaks(df, highs, lows)
    bias = None
    if breaks:
        b = breaks[-1]
        bias = b["direction"]
        facts["last_structure_break"] = {"type": b["type"], "direction": b["direction"], "level": b["level"],
                                         "bars_ago": len(df) - 1 - b["idx"]}
    facts["volume"] = volume_stats(df)
    if facts["volume"]["last_vs_avg"] >= 3 or facts["volume"]["recent_vs_avg"] >= 2:
        facts["volume"]["unusual"] = True  # 3x the average on the last bar, or 2x over the last 5

    if "support_resistance" in feats:
        zones = cluster_levels(highs + lows, atr_v, last, len(df))
        mark_confluence(zones, htf)
        sr_zones = pick_nearest(zones, last, intent.max_zones)
        for z in sr_zones:
            z.meta["record"] = zone_record(df, z, atr_v)
            color = RED if z.kind == "resistance" else GREEN
            label = _zone_label(tfl, z)
            overlays.append(BoxOverlay(
                label=label, kind=z.kind, price_high=z.price_high, price_low=z.price_low,
                color=rgba(color, 0.22), border_color=rgba(color, 0.7), time_start=z.first_time,
                strength=round(min(z.score, 1.0), 2),
            ))
            levels.append(Level(z.kind, z.price_low, z.price_high, label, z.score, htf=tuple(z.meta.get("htf") or ())))
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
            z.meta["record"] = zone_record(df, z, atr_v)
            color = ORANGE if z.kind == "supply" else TEAL
            label = _zone_label(tfl, z, " (fresh)" if z.tests == 0 else "")
            overlays.append(BoxOverlay(
                label=label, kind=z.kind, price_high=z.price_high, price_low=z.price_low, color=rgba(color, 0.18),
                border_color=rgba(color, 0.65), time_start=z.first_time, strength=round(min(z.score, 1.0), 2),
            ))
            levels.append(Level(z.kind, z.price_low, z.price_high, label, z.score, tests=z.tests,
                                htf=tuple(z.meta.get("htf") or ())))
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


def _held(z: dict) -> str:
    t = z.get("tests_resolved")
    return f", held {z['held']} of {t} test{'s' if t != 1 else ''}" if t else ""


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
    out += _kimi_pattern_lines(k)
    return out


def _kimi_pattern_lines(k: dict) -> list[str]:
    """Kimi's chart patterns still in play (or just broken out) and its newest harmonic."""
    out = []
    for p in reversed(k.get("chart_patterns", [])):
        if p["state"] == "watching":
            side = "above" if p["direction"] == "bullish" else "below"
            out.append(f"{p['pattern']} ({p['direction']}, formed {p['formed_bars_ago']} bars ago): breaks out on a "
                       f"close {side} {_fmt(p['breakout_level'])}, invalid past {_fmt(p['invalidation'])}.")
        elif p["state"] == "breakout" and p.get("target") is not None:
            out.append(f"{p['pattern']} broke out at {_fmt(p['broke_out_at'])}, measured-move target "
                       f"{_fmt(p['target'])}.")
        if len(out) == 2:
            break
    if k.get("harmonics"):
        h = k["harmonics"][-1]
        out.append(f"Harmonic: {h['direction']} {h['pattern']} ({h['state']}), D {_fmt(h['d'])} "
                   f"{h['completed_bars_ago']} bars ago, PRZ {_fmt(h['prz'][0])}–{_fmt(h['prz'][1])} "
                   f"(confluence {h['prz_confluence']}), TP1 {_fmt(h['tp1'])}, TP2 {_fmt(h['tp2'])}, "
                   f"invalid past {_fmt(h['invalidation'])}.")
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


def market_mood(overview: dict | None) -> str | None:
    """'Market mood: Fear & Greed 31/100 · Fear, BTC dominance 57.30% (+0.12% 24h).' from the header bar's live
    values; None when neither is live."""
    bits = []
    for key, name in (("fear_greed", "Fear & Greed"), ("btc_dominance", "BTC dominance")):
        row = (overview or {}).get(key)
        if row:
            chg = f" ({row['change_pct']:+g}% 24h)" if row.get("change_pct") is not None else ""
            bits.append(f"{name} {row['display']}{chg}")
    return f"Market mood: {', '.join(bits)}." if bits else None


def _spot_text(r: dict) -> str:
    out = f"{r['qty']:g} {r['coin']}" + (f" (${r['value_usd']:,.0f})" if r.get("value_usd") else "")
    if r.get("avg_entry"):
        out += f", average entry {_fmt(r['avg_entry'])}"
        if r.get("pnl_pct") is not None:
            out += f", {r['pnl_pct']:+.1f}%"
    return out


def _position_lines(position: dict | None, holdings: dict | None) -> list[str]:
    """'You hold 120 INJ ($936), average entry 7.21, +8.3%.' and the whole portfolio when asked."""
    lines = []
    if position:
        if position.get("spot"):
            lines.append(f"You hold {_spot_text(position['spot'])}.")
        for e in position.get("in_earn", []):
            apr = f" at {e['apr_pct']}% APR" if e.get("apr_pct") is not None else ""
            lines.append(f"You also have {e['qty']:g} in {e['product']} Earn{apr}.")
        for p in position.get("futures", []):
            line = f"Your futures {p['side']}: {p['qty']:g} at {_fmt(p['entry_price'])}"
            line += f", {p['leverage']}x" if p.get("leverage") else ""
            line += f", PnL {p['unrealized_pnl']:+,.2f}" if p.get("unrealized_pnl") is not None else ""
            line += f", liquidation {_fmt(p['liquidation_price'])}" if p.get("liquidation_price") else ""
            lines.append(line + ".")
    if holdings:
        spot = holdings.get("spot", [])
        lines.append(f"Your holdings: ${holdings['spot_value_usd']:,.0f} in {len(spot)} coins"
                     + (f" plus ${holdings['stablecoins_usd']:,.0f} in stablecoins" if holdings.get("stablecoins_usd")
                        else "") + (": " + "; ".join(_spot_text(r) for r in spot[:8]) if spot else "") + ".")
        if holdings.get("earn"):
            lines.append(f"In Simple Earn: ${holdings['earn_value_usd']:,.0f}, " + "; ".join(
                f"{e['qty']:g} {e['asset']} {e['product']}" + (f" at {e['apr_pct']}% APR" if e.get("apr_pct") is not None
                                                               else "") for e in holdings["earn"][:6]) + ".")
        for p in holdings.get("futures", []):
            lines.append(f"Futures {p['symbol']} {p['side']} {p['qty']:g} at {_fmt(p['entry_price'])}"
                         + (f", PnL {p['unrealized_pnl']:+,.2f}" if p.get("unrealized_pnl") is not None else "") + ".")
    return lines


TF_WORDS = {"M1": "1-minute", "M5": "5-minute", "M15": "15-minute", "M30": "30-minute", "H1": "1-hour",
            "H3": "3-hour", "H4": "4-hour", "D1": "daily", "W1": "weekly", "MN": "monthly"}
TREND_WORDS = {"up": "trending up", "down": "trending down"}
NUMBER_WORDS = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")


def _coin(symbol: str) -> str:
    for q in ("USDT", "USDC", "FDUSD", "BUSD", "BTC", "ETH"):
        if symbol.endswith(q) and len(symbol) > len(q):
            return symbol[: -len(q)]
    return symbol


def _n(n: int) -> str:
    return NUMBER_WORDS[n] if 0 <= n < len(NUMBER_WORDS) else str(n)


def _tf_word(tf: str) -> str:
    return TF_WORDS.get(tf, tf)


def _tf_list(tfs: list[str]) -> str:
    words = [_tf_word(t) for t in tfs]
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def _pct_away(z: dict, last: float) -> float:
    edge = z["low"] if z["low"] > last else z["high"]
    return abs(edge - last) / last * 100 if last else 0.0


def _distance_words(z: dict, last: float, side: str) -> str:
    """'and price is trading inside it' / 'just above price' / 'about 2.1% below price'."""
    if z.get("inside"):
        return "and price is trading inside it right now"
    atr_away = z.get("distance_atr")
    if atr_away is not None and atr_away < 0.5:
        return f"just {side} price"
    return f"about {_pct_away(z, last):.1f}% {side} price"


def _record_words(z: dict) -> str:
    """How the zone held up when it was tested, in words; empty when it hasn't been tested yet."""
    t, h = z.get("tests_resolved") or 0, z.get("held") or 0
    if not t:
        return ""
    num = (lambda n: str(n)) if t > 10 else _n
    if h == t:
        return "it held the only time it was tested" if t == 1 else f"it held all {num(t)} times it was tested"
    if h == 0:
        return "it gave way the last time it was tested" if t == 1 else f"it gave way all {num(t)} times it was tested"
    return f"it held {num(h)} of the {num(t)} times it was tested"


def _zone_sentence(opening: str, kind: str, z: dict, last: float, side: str) -> str:
    """'Overhead, resistance sits at 7.11–7.19, just above price, and it lines up with the daily. It held all two
    times it was tested.'"""
    s = f"{opening}{kind} sits at {_fmt(z['low'])}–{_fmt(z['high'])}, {_distance_words(z, last, side)}"
    if z.get("htf_confluence"):
        s += f", and it lines up with the {_tf_list(z['htf_confluence'])}"
    s += "."
    rec = _record_words(z)
    return s + (f" {rec[0].upper()}{rec[1:]}." if rec else "")


def _overlaps(a: dict, b: dict) -> bool:
    return a["low"] <= b["high"] and b["low"] <= a["high"]


def _bars_ago(n: int) -> str:
    return "on the last candle" if n <= 1 else f"{_n(n) if n <= 10 else n} candles ago"


def _price_check_lines(checks: list[dict], tf: str) -> list[str]:
    """Answers 'what's at 7.15?': which zone the price is in or near, how it has behaved, what's next if it breaks."""
    out = []
    for c in checks:
        p = _fmt(c["price"])
        z = c.get("zone")
        if not z:
            s = f"Nothing I detected on the {_tf_word(tf)} sits at {p}, so on its own it isn't a level."
            if c.get("closest"):
                n = c["closest"]
                s += f" The closest level is {n['kind']} at {_fmt(n['low'])}–{_fmt(n['high'])}."
            out.append(s)
            continue
        where = "inside" if c["relation"] == "inside" else f"right by ({c['relation']})"
        role = "resistance" if c["side"] == "above" else "support"
        s = (f"{p} is {where} the {z['kind']} zone at {_fmt(z['low'])}–{_fmt(z['high'])}, "
             f"{c['pct_from_price']:.1f}% {c['side']} the current price, so it acts as {role} for now.")
        rec = _record_words(z)
        if rec:
            s += f" {rec[0].upper()}{rec[1:]}."
        nxt = c.get("next_if_broken")
        if nxt:
            s += (f" If it breaks, the next {nxt['kind']} {'up' if c['side'] == 'above' else 'down'} is "
                  f"{_fmt(nxt['low'])}–{_fmt(nxt['high'])}.")
        elif c.get("next_if_broken") is None and c["side"] in ("above", "below"):
            s += f" If it breaks there is no other detected level {'above' if c['side'] == 'above' else 'below'} it."
        out.append(s)
    return out


def _takeaway(facts: dict, res: dict | None, sup: dict | None) -> str | None:
    """One sentence on what the picture means: what to watch at the nearest zone, or where the cleaner entry is."""
    trend = facts["trend"]
    if res and (res.get("inside") or (res.get("distance_atr") or 9) < 0.5):
        return (f"It's pressing into resistance, so a close above {_fmt(res['high'])} would be the sign buyers are "
                f"taking over, while a rejection there keeps sellers in control.")
    if sup and (sup.get("inside") or (sup.get("distance_atr") or 9) < 0.5):
        return (f"It's sitting right on support, so the question is whether {_fmt(sup['low'])} holds; a close below "
                f"it opens the way lower.")
    if trend == "down" and sup:
        return (f"With the trend down, the better buy is a reaction at support around {_fmt(sup['low'])}–"
                f"{_fmt(sup['high'])} rather than chasing here.")
    if trend == "up" and sup:
        return (f"With the trend up, a pullback that holds support around {_fmt(sup['low'])}–{_fmt(sup['high'])} "
                f"would be a cleaner entry than buying up here.")
    return None


PRICE_IN_PROMPT = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\d+)(?![\w%.])")
ZONE_KEYS = ("resistance", "support", "supply", "demand")


def prices_in_prompt(prompt: str, last: float) -> list[float]:
    """Prices the user typed ('what's at 7.1561?'): numbers within half and double the last price, at most two.
    Timeframes ('4h'), percentages and counts far from the price are left out."""
    out: list[float] = []
    for m in PRICE_IN_PROMPT.finditer(prompt or ""):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if last and 0.5 * last <= v <= 2 * last and v not in out:
            out.append(v)
    return out[:2]


def price_check(prompt: str, facts: dict) -> list[dict]:
    """For each price in the prompt: the detected zone it sits in (or the nearest one within an ATR), which side of
    the current price it is on, how that zone held up and the next zone beyond it if it breaks."""
    last, atr_v = facts.get("last_price"), facts.get("atr")
    if not last or not atr_v:
        return []
    zones = [{**z, "kind": k} for k in ZONE_KEYS for z in facts.get(k) or []]
    out = []
    for p in prices_in_prompt(prompt, last):
        side = "above" if p > last else "below"
        row: dict = {"price": p, "side": side, "pct_from_price": round(abs(p - last) / last * 100, 2) if last else 0.0}
        inside = [z for z in zones if z["low"] <= p <= z["high"]]
        near = sorted(zones, key=lambda z: min(abs(z["low"] - p), abs(z["high"] - p)))
        z = inside[0] if inside else (near[0] if near and min(abs(near[0]["low"] - p), abs(near[0]["high"] - p))
                                      <= atr_v else None)
        if z is None:
            row["zone"] = None
            row["closest"] = {k: near[0][k] for k in ("kind", "low", "high")} if near else None
            out.append(row)
            continue
        row["relation"] = "inside" if inside else ("above it" if p > z["high"] else "below it")
        row["zone"] = {k: z[k] for k in ("kind", "low", "high", "held", "tests_resolved", "htf_confluence")
                       if k in z}
        beyond = [o for o in zones if (o["low"] > z["high"] if side == "above" else o["high"] < z["low"])]
        beyond.sort(key=lambda o: o["low"] if side == "above" else -o["high"])
        row["next_if_broken"] = {k: beyond[0][k] for k in ("kind", "low", "high")} if beyond else None
        out.append(row)
    return out


def describe(facts: dict, symbol: str) -> str:
    """The answer in plain, conversational English, used when no LLM is configured (or it fails). It leads with
    the question about a price when there is one, then what matters for a decision: the trend, the nearest zones
    and how they held up, the last structure break and divergence. Everything else is drawn on the chart and
    left out unless the user asked for it."""
    tf = facts["timeframe"]
    last = facts["last_price"]
    coin = _coin(symbol)
    trend = TREND_WORDS.get(facts["trend"], "moving sideways")
    lines = []
    if facts.get("price_check"):
        lines.append(f"{coin} is at {_fmt(last)} on the {_tf_word(tf)}.")
        lines += _price_check_lines(facts["price_check"], tf)
    else:
        lines.append(f"{coin} is {trend} on the {_tf_word(tf)} and trading at {_fmt(last)}.")
    lines += facts.get("navigation", [])
    res, sup = (facts.get("resistance") or [None])[0], (facts.get("support") or [None])[0]
    checked = [c["zone"] for c in facts.get("price_check", []) if c.get("zone")]
    if res and not any(_overlaps(res, c) for c in checked):
        lines.append(_zone_sentence("Overhead, ", "resistance", res, last, "above"))
    if sup and not any(_overlaps(sup, c) for c in checked):
        lines.append(_zone_sentence("Below, " if res else "", "support", sup, last, "below").replace(
            "support sits", "support sits" if res else "Support sits"))
    if "supply" in facts and not facts["supply"]:
        lines.append(f"There's no unmitigated {_tf_word(tf)} supply above price right now.")
    if "demand" in facts and not facts["demand"]:
        lines.append(f"There's no unmitigated {_tf_word(tf)} demand below price right now.")
    extra = 0
    for key, side in (("supply", "above"), ("demand", "below")):
        if facts.get(key):
            z = facts[key][0]
            if any(_overlaps(z, o) for o in [r for r in (res, sup) if r] + checked):
                continue
            fresh = "fresh " if z["tests"] == 0 else ""
            extra += 1
            s = f"{'There is also' if extra == 1 else 'And there is'} {fresh}{key} at {_fmt(z['low'])}–{_fmt(z['high'])}, {_distance_words(z, last, side)}"
            if z.get("htf_confluence"):
                s += f", lining up with the {_tf_list(z['htf_confluence'])}"
            lines.append(s + ".")
    if not (res or sup or facts.get("supply") or facts.get("demand")):
        for w in facts.get("windows", []):
            lines.append(f"The {_tf_word(w['tf'])} window runs from {_fmt(w['low'])} to {_fmt(w['high'])}.")
    if facts.get("structure"):
        lines.append("Recent structure: " + " → ".join(facts["structure"]) + ".")
    br = facts.get("last_structure_break")
    if br and (facts.get("structure") is not None or br["bars_ago"] <= 20):
        who = "Sellers" if br["direction"] == "bearish" else "Buyers"
        lines.append(f"{who} broke structure through {_fmt(br['level'])} {_bars_ago(br['bars_ago'])} "
                     f"(a {br['direction']} {br['type']}).")
    mom = facts.get("momentum") or {}
    if mom.get("divergence"):
        lines.append(f"RSI is at {mom.get('rsi')} with a {re.sub(r' [(].*', '', mom['divergence'])} divergence.")
    elif mom.get("rsi") is not None and (mom["rsi"] >= 70 or mom["rsi"] <= 30):
        lines.append(f"RSI is {'overbought' if mom['rsi'] >= 70 else 'oversold'} at {mom['rsi']}.")
    if not facts.get("price_check") and (take := _takeaway(facts, res, sup)):
        lines.append(take)
    for sw in facts.get("sweeps", [])[:2]:
        lines.append(f"Price swept the {sw['direction']} side at {_fmt(sw['level'])} {_bars_ago(sw['bars_ago'])}.")
    if "sweeps" in facts and not facts["sweeps"]:
        lines.append("No liquidity sweeps in the last 30 candles.")
    for g in facts.get("fvg", [])[:2]:
        lines.append(f"There's an unfilled {g['direction']} fair value gap at {_fmt(g['low'])}–{_fmt(g['high'])}.")
    for o in facts.get("order_blocks", [])[:2]:
        lines.append(f"There's a {o['direction']} order block at {_fmt(o['low'])}–{_fmt(o['high'])}.")
    if facts.get("patterns"):
        lines.append("Patterns: " + "; ".join(facts["patterns"]) + ".")
    elif "patterns" in facts:
        lines.append("No range, triangle, wedge or double top/bottom right now.")
    if facts.get("volume_profile"):
        vp = facts["volume_profile"]
        lines.append(f"Most volume traded around {_fmt(vp['poc'])}, with the value area from {_fmt(vp['val'])} to "
                     f"{_fmt(vp['vah'])}.")
    # Funding and open interest only when asked (futures_context); spot traders don't need them on every answer.
    if facts.get("futures_context"):
        lines += _futures_lines(facts["futures_context"])
    if (facts.get("session_levels") or {}).get("at"):
        lines += level_lines({"at": facts["session_levels"]["at"]})
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
        lines.append(f"The {p['direction']} plan comes from {p['basis']}: enter around {_fmt(p['entry'])}, stop at "
                     f"{_fmt(p['stop'])}, and take profit at {tgts}.")
        lines += p.get("notes", [])
        if (p.get("track_record") or {}).get("summary"):
            lines.append(f"Track record: {p['track_record']['summary']}.")
    elif "plan" in facts:
        lines.append("No clean trade plan here: no zone or swing to put a stop behind.")
    if any(k in facts for k in ("scan", "market_scan", "spot_buys", "grid_coins")) and (
            mood := market_mood(facts.get("market_overview"))):
        lines.append(mood)
    if facts.get("scan"):
        best = facts["scan"][:3]
        lines.append("Watchlist: " + "; ".join(f"{r['symbol']} " + (", ".join(r["signals"][:2]) or r["trend"])
                                               for r in best) + ".")
    elif "scan" in facts:
        lines.append("Couldn't scan the watchlist.")
    if "market_scan" in facts:
        ms = facts["market_scan"]
        if ms.get("setups"):
            demo = " (demo data)" if ms.get("data_source") == "synthetic" else ""
            lines.append(f"Best {ms['timeframe']} setups across {ms['coins']} coins{demo}: " + "; ".join(
                f"{s['direction']} {s['symbol']} entry {_fmt(s['entry'])}, stop {_fmt(s['stop'])}, T1 "
                f"{_fmt(s['target'])} ({s['rr']}R, {s['distance_pct']}% away)"
                + (f", {s['track_record']}" if s.get("track_record") else "") for s in ms["setups"][:3]) + ".")
        else:
            lines.append(ms.get("note") or f"No {ms['timeframe']} setups across the market right now.")
    if facts.get("kimi"):
        lines += _kimi_lines(facts["kimi"])
    if (facts.get("volume") or {}).get("unusual"):
        v = facts["volume"]
        lines.append(f"Volume is unusually heavy: the last candle traded {v['last_vs_avg']}x its average and the "
                     f"last five {v['recent_vs_avg']}x.")
    if facts.get("your_note_on_this_coin"):
        lines.append(f"Your note on this coin: {facts['your_note_on_this_coin']}")
    lines += _position_lines(facts.get("your_position"), facts.get("your_holdings"))
    lines += facts.get("actions", [])
    if len(lines) == 1:
        lines.append("No qualifying levels found for this request.")
    return " ".join(lines)
