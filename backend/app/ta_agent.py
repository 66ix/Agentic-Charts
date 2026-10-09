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


# ------------------------------------------------------- a price asked about

PRICE_NEAR_ATR = 0.3   # a zone this close to the asked price counts as "at" it
VISIT_ATR = 0.15       # a candle whose range comes this close to the price visited it
REACT_ATR = 0.5        # after a visit, the first close this far away says which way it went


def price_check(df: pd.DataFrame, price: float, tf: str, frames: dict[str, pd.DataFrame] | None = None) -> dict:
    """What sits at a price the user asked about ("what's at 7.15?"): the zone there on this timeframe and the ones
    above, whether it is support or resistance now, how price reacted on its visits, and where price goes next if it
    breaks. Pure: no I/O."""
    df = df.reset_index(drop=True)
    atr_s = atr(df)
    atr_v = float(atr_s.iloc[-1])
    last = float(df["close"].iloc[-1])
    h, lo_, c = (df[k].to_numpy(dtype=float) for k in ("high", "low", "close"))
    role = "resistance" if price > last else "support"
    out: dict = {"price": price, "timeframe": TF_LABEL.get(tf, tf), "role_now": role,
                 "distance_pct": round((price / last - 1) * 100, 2),
                 "distance_atr": round(abs(price - last) / atr_v, 2) if atr_v else None}

    highs, lows = find_swings(df, atr_v)
    zones = cluster_levels(highs + lows, atr_v, last, len(df)) + supply_demand_zones(df, atr_s)
    pad = PRICE_NEAR_ATR * atr_v
    here = [z for z in zones if z.price_low - pad <= price <= z.price_high + pad]
    if here:
        z = max(here, key=lambda z: (z.price_low <= price <= z.price_high, z.score))
        row = {"kind": z.kind, "low": z.price_low, "high": z.price_high,
               "touches" if z.kind in ("support", "resistance") else "tests":
                   z.touches if z.kind in ("support", "resistance") else z.tests}
        rec = zone_record(df, z, atr_v)
        if rec["tests"]:
            row["held"], row["tests_resolved"] = rec["held"], rec["tests"]
        out["zone"] = row
    else:
        out["zone"] = None
    out["higher_timeframes"] = [TF_LABEL.get(t, t) for t, f in (frames or {}).items()
                                if any(z.price_low - pad <= price <= z.price_high + pad for z in htf_zones(f))]

    # Visits: runs of candles whose range reached the price. Each is judged by the first close REACT_ATR away after it.
    tol, react = VISIT_ATR * atr_v, REACT_ATR * atr_v
    touched = (lo_ <= price + tol) & (h >= price - tol)
    rejected = crossed = 0
    last_visit = None
    i, n = 1, len(df)
    while i < n:
        if not touched[i]:
            i += 1
            continue
        start = i
        while i < n and touched[i]:
            i += 1
        last_visit = start
        from_above = c[start - 1] > price
        for j in range(start, n):
            if abs(c[j] - price) >= react:
                if (c[j] > price) == from_above:
                    rejected += 1
                else:
                    crossed += 1
                break
    out["visits"] = {"bounced": rejected, "crossed": crossed,
                     "last_bars_ago": n - 1 - last_visit if last_visit is not None else None, "bars_checked": n}

    # If it breaks: the next zone beyond it, or the data's extreme when nothing is there.
    up = role == "resistance"
    edge = (out["zone"]["high"] if up else out["zone"]["low"]) if out["zone"] else price
    beyond = [z for z in zones if (z.price_low > edge if up else z.price_high < edge)]
    if beyond:
        nz = min(beyond, key=lambda z: z.price_low if up else -z.price_high)
        out["if_breaks"] = {"direction": "up" if up else "down", "next_kind": nz.kind, "next_low": nz.price_low,
                            "next_high": nz.price_high}
    else:
        extreme = float(h.max() if up else lo_.min())
        out["if_breaks"] = {"direction": "up" if up else "down", "next_kind": None,
                            "open_to": extreme if (extreme > edge if up else extreme < edge) else None}
    return out


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
        for e in position.get("earn", []):
            lines.append(f"You {'also ' if position.get('spot') else ''}have {e['qty']:g} {e['asset']} in Simple Earn "
                         f"{e['product']}"
                         + (f" at {e['apr_pct']:g}% APR" if e.get("apr_pct") is not None else "") + ".")
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
            lines.append(f"In Simple Earn: ${holdings['earn_value_usd']:,.0f} across " + "; ".join(
                f"{e['qty']:g} {e['asset']} ({e['product']}" + (f", {e['apr_pct']:g}% APR)" if e.get("apr_pct")
                                                                  is not None else ")") for e in holdings["earn"][:8])
                         + ".")
        for p in holdings.get("futures", []):
            lines.append(f"Futures {p['symbol']} {p['side']} {p['qty']:g} at {_fmt(p['entry_price'])}"
                         + (f", PnL {p['unrealized_pnl']:+,.2f}" if p.get("unrealized_pnl") is not None else "") + ".")
    return lines


_SMALL = {1: "once", 2: "twice"}
SENTENCES = {"short": 2, "normal": 6}  # sentences of analysis per answer length ("detailed" keeps them all)


def _times(n: int) -> str:
    return _SMALL.get(n, f"{n} times")


def _base(symbol: str) -> str:
    for q in ("USDT", "USDC", "FDUSD", "BUSD"):
        if symbol.endswith(q) and len(symbol) > len(q):
            return symbol[: -len(q)]
    return symbol


def _away(z: dict, side: str) -> str:
    """'price is inside it' / 'right next to price' / '0.96 ATR below price'."""
    if z.get("inside"):
        return "price is trading inside it"
    d = z.get("distance_atr")
    if d is None:
        return ""
    if d < 0.3:
        return "right next to price"
    return f"{d:g} ATR {side} price"


def _record(z: dict, count_key: str) -> str:
    """How a zone has done, after "it": "has held every one of 3 tests", "is fresh: price hasn't been back"."""
    t = z.get("tests_resolved")
    held = z.get("held", 0)
    if t:
        if held == t:
            return f"has held {'its only test' if t == 1 else 'both tests' if t == 2 else f'all {t} tests'}"
        if held == 0:
            return f"has broken {'on its only test' if t == 1 else f'on all {t} tests'}"
        return f"has held {held} of {t} tests"
    if count_key == "tests":
        return "is fresh: price hasn't been back to it" if z.get("tests") == 0 else \
            f"has been tested {_times(z['tests'])}"
    return f"has been touched {_times(z['touches'])}"


def _article(label: str) -> str:
    """'an H4', 'a D1': M and H are read "em" and "aitch"."""
    return "an" if label[:1] in "AEFHILMNORSX" else "a"


def _confluence(z: dict) -> str:
    htf = z.get("htf_confluence")
    return f", and it lines up with {_article(htf[0])} {'/'.join(htf)} zone" if htf else ""


def _zone_sentence(kind: str, z: dict, tf: str) -> str:
    above = kind in ("resistance", "supply")
    side = "above" if above else "below"
    rng = f"{_fmt(z['low'])}–{_fmt(z['high'])}"
    name = {"resistance": "resistance", "support": "support", "supply": "supply zone", "demand": "demand zone"}[kind]
    where = _away(z, side)
    record = _record(z, "touches" if kind in ("resistance", "support") else "tests")
    return f"The nearest {tf} {name} is {rng}" + (f", {where}" if where else "") + f"; it {record}" \
        + _confluence(z) + "."


def _break_sentence(br: dict) -> str:
    kind = "change of character" if br["type"] == "CHoCH" else "break of structure"
    who = "sellers" if br["direction"] == "bearish" else "buyers"
    ago = "on the last candle" if br["bars_ago"] == 0 else f"{br['bars_ago']} candle{'s' if br['bars_ago'] != 1 else ''} ago"
    lead = "Structure flipped" if br["type"] == "CHoCH" else "Structure broke"
    return f"{lead} {br['direction']} {ago} (a {kind} through {_fmt(br['level'])}), so {who} have the upper hand."


def _momentum_sentence(mom: dict) -> str | None:
    r = mom.get("rsi")
    if mom.get("divergence"):
        name, _, prices = mom["divergence"].partition(" (")
        return f"RSI is {r} with a {name} divergence" + (f" ({prices}" if prices else "") + "."
    if r is not None and r >= 70:
        return f"RSI is {r}, overbought."
    if r is not None and r <= 30:
        return f"RSI is {r}, oversold."
    return None


def _futures_sentence(funding: float | None, oi: float | None, extra: list[str] | None = None) -> str | None:
    bits = []
    if funding is not None:
        bits.append(f"funding is {funding}%")
    if oi is not None:
        trend = "positions are being closed" if oi <= -5 else "new positions are opening" if oi >= 5 else ""
        bits.append(f"open interest is {'down' if oi < 0 else 'up'} {abs(oi):g}% in 24h"
                    + (f" ({trend})" if trend else ""))
    bits += extra or []
    if not bits:
        return None
    return "On futures, " + (", ".join(bits[:-1]) + " and " + bits[-1] if len(bits) > 1 else bits[0]) + "."


def _futures_lines(f: dict) -> list[str]:
    extra = []
    if (ls := f.get("long_short")) and ls.get("ratio") is not None:
        extra.append(f"the long/short ratio is {ls['ratio']}")
    if (cv := f.get("cvd_24h")) and cv.get("direction"):
        extra.append(f"24h spot flow is {cv['direction']} ({cv['buy_pct']}% taker buys)")
    fu, oi = f.get("funding") or {}, f.get("open_interest") or {}
    out = [s] if (s := _futures_sentence(fu.get("rate_pct"), oi.get("change_24h_pct"), extra)) else []
    liq = f.get("est_liquidations") or {}
    near = [f"{side} at {_fmt(c['price_low'])}–{_fmt(c['price_high'])}" for side, c in
            (("above", liq.get("above")), ("below", liq.get("below"))) if c]
    if near:
        out.append("Estimated liquidation clusters sit " + " and ".join(near) + ".")
    return out


def _price_sentences(q: dict, last: float) -> list[str]:
    """The answer to "what's at 7.15?" in a few plain sentences."""
    p, tf = _fmt(q["price"]), q["timeframe"]
    where = "above" if q["price"] > last else "below"
    out = []
    z = q.get("zone")
    away = f"{abs(q['distance_pct']):g}% {where} price"
    htf = q.get("higher_timeframes")
    if z:
        kind, acts = z["kind"], q["role_now"]
        name = kind if kind in ("support", "resistance") else f"{kind} zone"
        sr = kind in ("support", "resistance")
        count = z.get("touches") if sr else z.get("tests")
        if z.get("tests_resolved"):
            bits = [f"held {z['held']} of {z['tests_resolved']} tests"]
        else:
            bits = [f"touched {_times(count)}" if sr else "still fresh" if count == 0 else f"tested {_times(count)}"]
        same_side = kind in (acts, {"resistance": "supply", "support": "demand"}[acts])
        flip = "" if same_side else f" (it formed as {kind}, but price is now on the other side of it)"
        out.append(f"{p} is {acts} on the {tf}, {away}: it sits in the {tf} {name} {_fmt(z['low'])}–{_fmt(z['high'])}"
                   f"{flip}, {', '.join(bits)}" + (f", and {_article(htf[0])} {'/'.join(htf)} zone covers it too." if htf else "."))
    else:
        out.append(f"There's no {tf} zone at {p} ({away}), so on this timeframe it's only minor {q['role_now']}"
                   + (f", though {_article(htf[0])} {'/'.join(htf)} zone covers it." if htf else "."))
    v = q.get("visits") or {}
    b, c, bars = v.get("bounced", 0), v.get("crossed", 0), v.get("bars_checked", 0)
    if b + c == 0:
        out.append(f"Price hasn't traded there in the last {bars} candles.")
    else:
        ago = f", most recently {v['last_bars_ago']} candles ago" if v.get("last_bars_ago") else ""
        if b and c:
            verdict = ("it has mostly turned price away" if b > c else "price has mostly cut through it" if c > b
                       else "it has been a coin flip")
            out.append(f"In the last {bars} candles price reached it {_times(b + c)}: it bounced {_times(b)} and "
                       f"crossed {_times(c)}{ago}, so {verdict}.")
        else:
            out.append(f"In the last {bars} candles price reached it {_times(b + c)} and "
                       f"{'bounced' if b else 'crossed'} every time{ago}.")
    br = q.get("if_breaks") or {}
    move = "a close above it" if br.get("direction") == "up" else "a close below it"
    if br.get("next_kind"):
        nk = br["next_kind"] + ("" if br["next_kind"] in ("support", "resistance") else " zone")
        out.append(f"If it breaks, {move} opens the way to the next {nk} at {_fmt(br['next_low'])}–"
                   f"{_fmt(br['next_high'])}.")
    elif br.get("open_to") is not None:
        out.append(f"If it breaks, there's no zone beyond it until the {'high' if br['direction'] == 'up' else 'low'} "
                   f"of the loaded candles at {_fmt(br['open_to'])}.")
    else:
        out.append(f"If it breaks, price is in open space with nothing on this timeframe "
                   f"{'above' if br.get('direction') == 'up' else 'below'}.")
    return out


_TOPIC_WORDS = (
    ("supply_demand", r"\b(supply|demand)\b"),
    ("windows", r"\b(window|high of|low of|range high|range low|resistance high|support low)\b"),
    ("support_resistance", r"\b(support|resistance|levels?)\b"),
    ("structure", r"\b(structure|choch|bos|break|trend)\b"),
    ("momentum", r"\b(rsi|momentum|divergence|overbought|oversold)\b"),
    ("futures", r"\b(funding|open interest|oi|futures|long/short)\b"),
)


def describe(facts: dict, symbol: str, detail: str = "normal", prompt: str = "") -> str:
    """The answer in plain English, used when no LLM is configured or it fails. Whatever the question asked about
    comes first; `detail` ("short", "normal", "detailed") sets how much of the rest follows."""
    tf = facts["timeframe"]
    last = facts["last_price"]
    asked = {k for k, rx in _TOPIC_WORDS if re.search(rx, prompt, re.I)}
    trend = {"up": "trending up", "down": "trending down"}.get(facts["trend"], "ranging")
    atr_pct = facts.get("atr_pct") or round(facts["atr"] / last * 100, 2)
    head = [f"{_base(symbol)} is {trend} on the {tf} at {_fmt(last)}, moving about {atr_pct:g}% a candle "
            f"(ATR {_fmt(facts['atr'], last)})."]
    lead: list[str] = []      # what the question asked, first
    body: list[tuple[str, str]] = []  # (topic, sentence), in order of importance
    tail: list[str] = list(facts.get("navigation", []))  # always said

    if facts.get("price_in_question"):
        lead += _price_sentences(facts["price_in_question"], last)

    for kind in ("resistance", "support"):
        if facts.get(kind):
            body.append(("support_resistance", _zone_sentence(kind, facts[kind][0], tf)))
    for kind, side in (("supply", "above"), ("demand", "below")):
        if kind in facts and not facts[kind]:
            body.append(("supply_demand", f"There's no unmitigated {tf} {kind} zone {side} price right now."))
        elif facts.get(kind):
            body.append(("supply_demand", _zone_sentence(kind, facts[kind][0], tf)))
    for w in facts.get("windows", []):
        body.append(("windows", f"The last completed {w['tf']} candle ran from {_fmt(w['low'])} to "
                                f"{_fmt(w['high'])}, so {_fmt(w['high'])} is the {w['tf']} high to beat and "
                                f"{_fmt(w['low'])} the low to hold."))
    if facts.get("structure"):
        body.append(("structure", "Recent swings: " + " → ".join(facts["structure"]) + "."))
    br = facts.get("last_structure_break")
    if br and (facts.get("structure") is not None or br["bars_ago"] <= 20):
        body.append(("structure", _break_sentence(br)))
    if m := _momentum_sentence(facts.get("momentum") or {}):
        body.append(("momentum", m))
    for sw in facts.get("sweeps", [])[:2]:
        body.append(("sweeps", f"There was a {sw['direction']} sweep of {_fmt(sw['level'])} {sw['bars_ago']} "
                               f"candles ago."))
    if "sweeps" in facts and not facts["sweeps"]:
        body.append(("sweeps", "No liquidity sweeps in the last 30 candles."))
    for g in facts.get("fvg", [])[:2]:
        body.append(("fvg", f"There's an unfilled {g['direction']} fair value gap at {_fmt(g['low'])}–"
                            f"{_fmt(g['high'])}."))
    for o in facts.get("order_blocks", [])[:2]:
        body.append(("order_blocks", f"A {o['direction']} order block sits at {_fmt(o['low'])}–{_fmt(o['high'])}."))
    if facts.get("patterns"):
        body.append(("patterns", "Patterns in play: " + "; ".join(facts["patterns"]) + "."))
    elif "patterns" in facts:
        body.append(("patterns", "No range, triangle, wedge or double top/bottom right now."))
    if facts.get("volume_profile"):
        vp = facts["volume_profile"]
        body.append(("volume_profile", f"Most volume traded at {_fmt(vp['poc'])} (the POC), with the value area "
                                       f"{_fmt(vp['val'])}–{_fmt(vp['vah'])}."))
    if facts.get("futures_context"):
        body += [("futures", s) for s in _futures_lines(facts["futures_context"])]
    elif d := facts.get("derivatives"):
        if s := _futures_sentence(d.get("funding_rate_pct"), d.get("oi_change_24h_pct")):
            body.append(("futures", s))
    if facts.get("session_levels"):
        body += [("sessions", s) for s in level_lines(facts["session_levels"])]
    if (facts.get("volume") or {}).get("unusual"):
        v = facts["volume"]
        body.append(("volume", f"Volume is unusually high: the last candle traded {v['last_vs_avg']}x its average "
                               f"and the last five {v['recent_vs_avg']}x."))

    # Things the user asked for by name are never trimmed.
    if facts.get("upcoming_events"):
        ev = facts["upcoming_events"]
        tail.append("Coming up: " + "; ".join(f"{e['country']} {e['title']} in {e['in_hours']:.0f}h" for e in ev[:3])
                    + ".")
    elif "upcoming_events" in facts and not facts.get("plan"):
        tail.append("No high-impact economic events in the next 48 hours.")
    if facts.get("headlines"):
        tail.append("Headlines: " + "; ".join(h["title"] for h in facts["headlines"][:3]) + ".")
    if facts.get("plan"):
        p = facts["plan"]
        tgts = ", ".join(f"{_fmt(t['price'])} ({t['rr']}R)" for t in p["targets"])
        lead.append(f"{p['direction'].title()} plan from {p['basis']}: enter at {_fmt(p['entry'])}, stop at "
                    f"{_fmt(p['stop'])}, targets {tgts}.")
        lead += p.get("notes", [])
        if (p.get("track_record") or {}).get("summary"):
            lead.append(f"Track record: {p['track_record']['summary']}.")
    elif "plan" in facts:
        lead.append("There's no clean trade plan here: no zone or swing to put a stop behind.")
    if any(k in facts for k in ("scan", "market_scan", "spot_buys", "grid_coins")) and (
            mood := market_mood(facts.get("market_overview"))):
        lead.append(mood)
    if facts.get("scan"):
        best = facts["scan"][:3]
        lead.append("Watchlist: " + "; ".join(f"{r['symbol']} " + (", ".join(r["signals"][:2]) or r["trend"])
                                              for r in best) + ".")
    elif "scan" in facts:
        lead.append("Couldn't scan the watchlist.")
    if "market_scan" in facts:
        ms = facts["market_scan"]
        if ms.get("setups"):
            demo = " (demo data)" if ms.get("data_source") == "synthetic" else ""
            lead.append(f"Best {ms['timeframe']} setups across {ms['coins']} coins{demo}: " + "; ".join(
                f"{s['direction']} {s['symbol']} entry {_fmt(s['entry'])}, stop {_fmt(s['stop'])}, T1 "
                f"{_fmt(s['target'])} ({s['rr']}R, {s['distance_pct']}% away)"
                + (f", {s['track_record']}" if s.get("track_record") else "") for s in ms["setups"][:3]) + ".")
        else:
            lead.append(ms.get("note") or f"No {ms['timeframe']} setups across the market right now.")
    if facts.get("kimi"):
        lead += _kimi_lines(facts["kimi"])
    if (yt := facts.get("your_trading")) is not None:
        if yt.get("note"):
            lead.append(yt["note"])
        ov = yt.get("overview") or {}
        if ov.get("trades"):
            lead.append(f"Across your {ov['trades']} closed spot trades you won {ov['win_rate']:g}% for "
                        f"{ov['pnl']:+,.2f} USDT after {ov['fees']:,.2f} in fees.")
        lead += [h["detail"] for h in yt.get("habits", [])[:3]]
    if (wn := facts.get("what_works_now")) is not None:
        rows = wn.get("setups") or []
        if not rows:
            lead.append(f"Not enough finished zones in the last {wn['days']} days to say what is working yet.")
        else:
            best, worst = rows[0], rows[-1]
            lead.append(f"Over the last {wn['days']} days the best was {best['setup']}: {best['hits']} of "
                        f"{best['zones']} zones reached their level ({best['lift']:.1f}x random odds).")
            if worst is not best:
                lead.append(f"The worst was {worst['setup']}: {worst['hits']} of {worst['zones']} "
                            f"({worst['lift']:.1f}x).")
    if facts.get("your_note_on_this_coin"):
        tail.append(f"Your note on this coin: {facts['your_note_on_this_coin']}")
    tail += _position_lines(facts.get("your_position"), facts.get("your_holdings"))
    for c in (facts.get("agent_desk") or {}).get("running", [])[:2]:
        tf_l = TF_LABEL.get(c["interval"], c["interval"])
        what = "holding from" if c["status"] == "open" else "waiting to buy at"
        tail.append(f"The desk has a {tf_l} call running: {what} {_fmt(c['entry'])}, take-profit {_fmt(c['tp'])}, "
                    f"wrong below {_fmt(c['stop'])} ({c['confidence'] * 100:.0f}% confidence).")
    tail += facts.get("actions", [])

    # The topics the question named go first, then the rest in order; the answer length trims the rest.
    order = [k for k, _ in _TOPIC_WORDS]
    ranked = [s for t, s in sorted((b for b in body if b[0] in asked), key=lambda b: order.index(b[0]))] + \
        [s for t, s in body if t not in asked]
    if not lead and not ranked and len(tail) == len(facts.get("navigation", [])):
        ranked = ["No qualifying levels found for this request."]
    keep = SENTENCES.get(detail)
    if keep is not None:
        named = sum(1 for t, _ in body if t in asked)
        room = 0 if lead and detail == "short" else keep - (1 if lead else 0)
        ranked = ranked[:max(room, named if detail != "short" else 0)]
    return " ".join(head + lead + ranked + tail)
