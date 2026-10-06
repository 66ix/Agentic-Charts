"""Momentum, volume and market-structure indicators.

Pure, deterministic functions over an OHLCV DataFrame with columns
time, open, high, low, close, volume (oldest → newest, index 0..n-1):

* RSI / MACD            — Wilder RSI (SMA-seeded) and the classic 12/26/9 MACD
* RSI divergence        — regular and hidden, on the two latest swing highs / lows
* BOS / CHoCH           — closes through the latest confirmed swing high / low
* volume profile        — POC and 70% value area from volume spread over each bar's range
* anchored VWAP         — typical-price VWAP from an anchor bar
* volume stats          — last / recent volume relative to its average

Swing arguments are `ta_agent.Swing`-like objects (idx, time, price, kind,
prominence, structure). This module never imports `ta_agent` at runtime, since
`ta_agent` imports it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from .ta_agent import Swing


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _by_idx(swings: Sequence[Swing]) -> list[Swing]:
    return sorted(swings, key=lambda s: s.idx)


# --------------------------------------------------------------- momentum


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI.

    The first average gain / loss is the simple mean of the first `period`
    changes, then each step is smoothed as `(prev * (period - 1) + x) / period`.
    The first `period` values are NaN. No losses in the window reads 100; no
    movement at all reads 50.
    """
    c = close.to_numpy(dtype=float)
    out = np.full(len(c), np.nan)
    if len(c) <= period:
        return pd.Series(out, index=close.index)
    d = np.diff(c)
    gain, loss = np.clip(d, 0, None), np.clip(-d, 0, None)
    ag, al = float(gain[:period].mean()), float(loss[:period].mean())
    out[period] = _rsi_value(ag, al)
    for i in range(period, len(d)):
        ag = (ag * (period - 1) + gain[i]) / period
        al = (al * (period - 1) + loss[i]) / period
        out[i + 1] = _rsi_value(ag, al)
    return pd.Series(out, index=close.index)


def macd(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD line (fast EMA − slow EMA), its signal EMA, and the histogram."""
    line = _ema(close, fast) - _ema(close, slow)
    sig = _ema(line, signal)
    return line, sig, line - sig


def rsi_divergence(
    df: pd.DataFrame,
    rsi_series: pd.Series,
    highs: Sequence[Swing],
    lows: Sequence[Swing],
    max_bars_apart: int = 60,
    recent_bars: int = 40,
) -> dict | None:
    """Most recent RSI divergence on the two latest swing highs or swing lows.

    Highs: higher high + lower RSI high → bearish regular; lower high + higher
    RSI high → bearish hidden. Lows: lower low + higher RSI low → bullish
    regular; higher low + lower RSI low → bullish hidden. The second swing must
    sit within the last `recent_bars` bars and at most `max_bars_apart` bars
    after the first. Ties on recency prefer regular over hidden.
    """
    n = len(df)
    r = rsi_series.to_numpy(dtype=float)
    found: list[tuple[int, bool, dict]] = []
    for swings, is_high in ((highs, True), (lows, False)):
        ss = _by_idx(swings)
        if len(ss) < 2:
            continue
        a, b = ss[-2], ss[-1]
        if b.idx - a.idx > max_bars_apart or b.idx < n - recent_bars or b.idx >= len(r):
            continue
        r1, r2 = r[a.idx], r[b.idx]
        if np.isnan(r1) or np.isnan(r2):
            continue
        if is_high:
            direction = "bearish"
            kind = ("regular" if b.price > a.price and r2 < r1
                    else "hidden" if b.price < a.price and r2 > r1 else None)
        else:
            direction = "bullish"
            kind = ("regular" if b.price < a.price and r2 > r1
                    else "hidden" if b.price > a.price and r2 < r1 else None)
        if kind is None:
            continue
        found.append((b.idx, kind == "regular", {
            "type": direction, "kind": kind, "time1": int(a.time), "price1": float(a.price),
            "time2": int(b.time), "price2": float(b.price), "rsi1": round(float(r1), 2),
            "rsi2": round(float(r2), 2),
        }))
    return max(found, key=lambda f: (f[0], f[1]))[2] if found else None


# -------------------------------------------------------------- structure


def structure_breaks(df: pd.DataFrame, highs: Sequence[Swing], lows: Sequence[Swing]) -> list[dict]:
    """Break of structure (BOS) / change of character (CHoCH), chronologically.

    Walks the bars tracking the latest swing high and low that had already
    formed (swing.idx < bar). A close above that high is a bullish break, a
    close below that low a bearish one; each swing level breaks at most once.
    A break in the same direction as the previous one (or the first break) is a
    BOS, one that flips direction is a CHoCH.
    """
    close = df["close"].to_numpy(dtype=float)
    times = df["time"].to_numpy()
    hs, ls = _by_idx(highs), _by_idx(lows)
    hi_i = lo_i = 0
    last_hi: Swing | None = None
    last_lo: Swing | None = None
    hi_done = lo_done = False
    prev_dir: str | None = None
    out: list[dict] = []
    for i in range(len(close)):
        while hi_i < len(hs) and hs[hi_i].idx < i:
            last_hi, hi_done = hs[hi_i], False
            hi_i += 1
        while lo_i < len(ls) and ls[lo_i].idx < i:
            last_lo, lo_done = ls[lo_i], False
            lo_i += 1
        events: list[tuple[str, Swing]] = []
        if last_hi is not None and not hi_done and close[i] > last_hi.price:
            hi_done = True
            events.append(("bullish", last_hi))
        if last_lo is not None and not lo_done and close[i] < last_lo.price:
            lo_done = True
            events.append(("bearish", last_lo))
        for direction, s in events:
            out.append({
                "type": "BOS" if prev_dir in (None, direction) else "CHoCH", "direction": direction,
                "level": float(s.price), "swing_time": int(s.time), "time": int(times[i]), "idx": i,
            })
            prev_dir = direction
    return out


# ----------------------------------------------------------------- volume


def volume_profile(df: pd.DataFrame, bins: int = 40, value_area: float = 0.70) -> dict | None:
    """Point of control and value area of a fixed-range volume profile.

    Each bar's volume is spread uniformly over its high–low range across
    `bins` equal price bins spanning the frame. POC is the centre of the
    heaviest bin; the value area grows one bin at a time from the POC toward
    the heavier neighbour until it holds `value_area` of the volume, and
    VAH / VAL are its outer bin edges. None when there is no volume.
    """
    if df.empty:
        return None
    h, l = df["high"].to_numpy(dtype=float), df["low"].to_numpy(dtype=float)
    v = np.nan_to_num(df["volume"].to_numpy(dtype=float))
    if v.sum() <= 0:
        return None
    lo, hi = float(l.min()), float(h.max())
    if hi <= lo:
        return {"poc": lo, "vah": lo, "val": lo}
    edges = np.linspace(lo, hi, bins + 1)
    rng = h - l
    overlap = np.clip(np.minimum(h[:, None], edges[None, 1:]) - np.maximum(l[:, None], edges[None, :-1]), 0, None)
    with np.errstate(divide="ignore", invalid="ignore"):
        share = np.where(rng[:, None] > 0, overlap / rng[:, None], 0.0)
    flat = np.flatnonzero(rng <= 0)
    if flat.size:  # zero-range bars put all their volume in the bin holding their price
        share[flat, np.clip(np.searchsorted(edges, h[flat], side="right") - 1, 0, bins - 1)] = 1.0
    profile = (share * v[:, None]).sum(axis=0)

    poc = int(np.argmax(profile))
    lo_b = hi_b = poc
    acc, target = profile[poc], value_area * profile.sum()
    while acc < target and (lo_b > 0 or hi_b < bins - 1):
        up = profile[hi_b + 1] if hi_b < bins - 1 else -1.0
        down = profile[lo_b - 1] if lo_b > 0 else -1.0
        if up >= down:
            hi_b += 1
            acc += up
        else:
            lo_b -= 1
            acc += down
    return {"poc": float((edges[poc] + edges[poc + 1]) / 2), "vah": float(edges[hi_b + 1]),
            "val": float(edges[lo_b])}


def anchored_vwap(df: pd.DataFrame, anchor_idx: int) -> pd.Series:
    """Typical-price VWAP accumulated from `anchor_idx` onward (NaN before it).

    A negative `anchor_idx` counts from the end; an out-of-range one yields all
    NaN. Bars before any volume has traded since the anchor are NaN too.
    """
    n = len(df)
    out = np.full(n, np.nan)
    if anchor_idx < 0:
        anchor_idx += n
    if not 0 <= anchor_idx < n:
        return pd.Series(out, index=df.index)
    sl = slice(anchor_idx, n)
    tp = (df["high"].to_numpy(dtype=float)[sl] + df["low"].to_numpy(dtype=float)[sl]
          + df["close"].to_numpy(dtype=float)[sl]) / 3
    v = np.nan_to_num(df["volume"].to_numpy(dtype=float)[sl])
    cum_v = np.cumsum(v)
    with np.errstate(divide="ignore", invalid="ignore"):
        out[sl] = np.where(cum_v > 0, np.cumsum(tp * v) / cum_v, np.nan)
    return pd.Series(out, index=df.index)


def volume_stats(df: pd.DataFrame, period: int = 20) -> dict:
    """Relative volume: last bar vs the mean of the `period` bars before it, and
    the mean of the last 5 bars vs the mean of the 50 before those. Ratios are
    rounded to 2 dp and read 1.0 when the baseline is empty or has no volume."""
    v = np.nan_to_num(df["volume"].to_numpy(dtype=float))

    def ratio(num: np.ndarray, base: np.ndarray) -> float:
        if not num.size or not base.size or base.mean() <= 0:
            return 1.0
        return round(float(num.mean() / base.mean()), 2)

    return {"last_vs_avg": ratio(v[-1:], v[-1 - period:-1]), "recent_vs_avg": ratio(v[-5:], v[-55:-5])}
