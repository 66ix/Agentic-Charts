"""Price-action pattern detectors.

Pure, deterministic functions over an OHLCV DataFrame with columns
time, open, high, low, close, volume (oldest → newest, index 0..n-1):

* liquidity sweeps    — wicks through a swing level that close back inside
* fair value gaps     — 3-candle imbalances, shrunk by later partial fills
* order blocks        — last opposite candle before a break of structure
* trading range       — longest recent window that is tight and two-sided
* triangles / wedges  — converging regression lines through the latest swings
* double top / bottom — two matching swings with a neckline between them

Results are plain JSON-friendly dicts and only describe what is still current.
Every price is a bar's OHLC value or arithmetic on such values (a line fitted
through swing prices, a gap edge trimmed by a later wick), never invented.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

import numpy as np
import pandas as pd

from .indicators import structure_breaks

if TYPE_CHECKING:
    from .ta_agent import Swing


def _ohlc(df: pd.DataFrame) -> tuple[np.ndarray, ...]:
    return tuple(df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))


def _by_idx(swings: Sequence[Swing]) -> list[Swing]:
    return sorted(swings, key=lambda s: s.idx)


def _distance(lo: float, hi: float, price: float) -> float:
    """Distance from `price` to the band [lo, hi]; 0 when inside it."""
    return 0.0 if lo <= price <= hi else min(abs(price - lo), abs(price - hi))


# -------------------------------------------------------- liquidity sweeps


def liquidity_sweeps(
    df: pd.DataFrame, highs: Sequence[Swing], lows: Sequence[Swing], lookback: int = 30, max_results: int = 4
) -> list[dict]:
    """Wicks through a prior swing level that close back on the original side.

    A swing high's liquidity is swept by the first later bar to trade above it,
    if that bar closes back below it (buy-side swept → bearish); a first bar
    that closes beyond the level is a breakout and spends the level. Swing lows
    mirror this (sell-side swept → bullish). A sweep is dropped once a later
    close goes back through the level (the rejection failed). Only sweeps
    within the last `lookback` bars are kept; one bar sweeping several levels
    on the same side is reported once, at the furthest level. Newest first.
    """
    _, h, l, c = _ohlc(df)
    times = df["time"].to_numpy()
    n = len(df)
    start = max(0, n - lookback)
    best: dict[tuple[int, str], dict] = {}
    for swings, buy_side in ((highs, True), (lows, False)):
        for s in swings:
            beyond = h[s.idx + 1:] > s.price if buy_side else l[s.idx + 1:] < s.price
            hit = np.flatnonzero(beyond)
            if not hit.size:
                continue
            j = s.idx + 1 + int(hit[0])
            if j < start:
                continue
            # The sweep bar must close back inside, and no later close may reclaim the level.
            if (c[j:] >= s.price).any() if buy_side else (c[j:] <= s.price).any():
                continue
            side = "buy_side" if buy_side else "sell_side"
            prev = best.get((j, side))
            if prev is not None and (s.price <= prev["level"] if buy_side else s.price >= prev["level"]):
                continue
            best[(j, side)] = {
                "side": side, "direction": "bearish" if buy_side else "bullish", "level": float(s.price),
                "swing_time": int(s.time), "time": int(times[j]), "extreme": float(h[j] if buy_side else l[j]),
                "close": float(c[j]),
            }
    return sorted(best.values(), key=lambda e: (-e["time"], e["side"]))[:max_results]


# --------------------------------------------------------- fair value gaps


def fair_value_gaps(
    df: pd.DataFrame, atr_value: float, min_size_atr: float = 0.2, max_results: int = 6
) -> list[dict]:
    """Unfilled 3-candle imbalances, nearest to the last close first.

    Bullish: low[i+1] > high[i-1], gap (high[i-1], low[i+1]). Bearish:
    high[i+1] < low[i-1], gap (high[i+1], low[i-1]). Gaps under
    `min_size_atr` ATRs are ignored. Bars from i+2 on eat into the gap from
    the side price returns from (above a bullish gap, below a bearish one): a
    gap traded all the way through is dropped, a partly filled one is reported
    as its untouched remainder with `filled_pct` of the original gap.
    """
    _, h, l, c = _ohlc(df)
    times = df["time"].to_numpy()
    n = len(df)
    if n < 3:
        return []
    # Lowest low / highest high from bar k to the end.
    low_from = np.minimum.accumulate(l[::-1])[::-1]
    high_from = np.maximum.accumulate(h[::-1])[::-1]
    min_size = max(min_size_atr * atr_value, 0.0)
    last = float(c[-1])
    found: list[tuple[float, int, dict]] = []
    for i in range(1, n - 1):
        if l[i + 1] > h[i - 1]:
            lo, hi, bullish = h[i - 1], l[i + 1], True
        elif h[i + 1] < l[i - 1]:
            lo, hi, bullish = h[i + 1], l[i - 1], False
        else:
            continue
        size = hi - lo
        if size < min_size:
            continue
        r_lo, r_hi = lo, hi
        if i + 2 < n:
            if bullish:
                r_hi = min(hi, low_from[i + 2])
            else:
                r_lo = max(lo, high_from[i + 2])
            if r_hi <= r_lo:
                continue
        found.append((_distance(r_lo, r_hi, last), -i, {
            "direction": "bullish" if bullish else "bearish", "price_low": float(r_lo), "price_high": float(r_hi),
            "time": int(times[i]), "filled_pct": round(float(1 - (r_hi - r_lo) / size), 3),
        }))
    found.sort(key=lambda f: (f[0], f[1]))
    return [f[2] for f in found[:max_results]]


# ------------------------------------------------------------ order blocks


def order_blocks(
    df: pd.DataFrame,
    highs: Sequence[Swing],
    lows: Sequence[Swing],
    atr_series: pd.Series,
    max_results: int = 4,
    min_move_atr: float = 1.0,
) -> list[dict]:
    """Order blocks behind each break of structure, nearest to the last close first.

    For a bullish break (close above the latest swing high, see
    `structure_breaks`) the move's origin is the lowest low between the swing
    and the break; the OB is the last bearish candle at or before that low and
    after the swing, zone = its low..high. The move from the OB's far edge to
    the breaking close must be at least `min_move_atr` ATRs. Bearish OBs mirror
    this. An OB is dropped once a later close goes through it (below a bullish
    OB's low / above a bearish OB's high). `tests` counts bars after the break
    whose wick reaches back into the zone.
    """
    o, h, l, c = _ohlc(df)
    times = df["time"].to_numpy()
    a = atr_series.to_numpy(dtype=float)
    n = len(df)
    if n == 0:
        return []
    last = float(c[-1])
    seen: set[int] = set()
    found: list[tuple[float, int, dict]] = []
    for br in structure_breaks(df, highs, lows):
        k, bullish = br["idx"], br["direction"] == "bullish"
        s_idx = int(np.searchsorted(times, br["swing_time"]))
        if k - s_idx < 2:
            continue
        seg = slice(s_idx + 1, k + 1)
        origin = s_idx + 1 + int(np.argmin(l[seg]) if bullish else np.argmax(h[seg]))
        j = next((x for x in range(origin, s_idx, -1) if (c[x] < o[x] if bullish else c[x] > o[x])), None)
        if j is None or j in seen:
            continue
        move = c[k] - l[j] if bullish else h[j] - c[k]
        if move < min_move_atr * a[j]:
            continue
        later = c[j + 1:]
        if (later < l[j]).any() if bullish else (later > h[j]).any():
            continue
        seen.add(j)
        tests = int((l[k + 1:] <= h[j]).sum() if bullish else (h[k + 1:] >= l[j]).sum())
        found.append((_distance(l[j], h[j], last), -j, {
            "direction": "bullish" if bullish else "bearish", "price_low": float(l[j]), "price_high": float(h[j]),
            "time": int(times[j]), "tests": tests, "broken_level": float(br["level"]),
        }))
    found.sort(key=lambda f: (f[0], f[1]))
    return [f[2] for f in found[:max_results]]


# ------------------------------------------------------------------- range


def _episodes(mask: np.ndarray) -> int:
    """Number of separate runs of True (re-entries into a band)."""
    if not mask.size:
        return 0
    return int(mask[0]) + int((mask[1:] & ~mask[:-1]).sum())


def detect_range(
    df: pd.DataFrame, atr_value: float, min_bars: int = 20, max_span_atr: float = 4.0, band: float = 0.3
) -> dict | None:
    """Is price currently ranging?

    Finds the longest window ending at the last bar, at least `min_bars` long,
    whose high–low span is at most `max_span_atr` ATRs and which touched both
    edges at least twice. A touch is a separate visit of the highs into the top
    `band` fraction of the span (lows into the bottom fraction for the floor);
    a run of consecutive bars inside the band counts once. Leading bars of the
    trend that ran into the range are then trimmed while each one sticks more
    than 0.25 ATR outside the box of the rest and the rest still qualifies, so
    the box hugs the range rather than its approach.
    """
    n = len(df)
    if n < min_bars or atr_value <= 0:
        return None
    _, h, l, _ = _ohlc(df)
    times = df["time"].to_numpy()

    def box(s: int) -> tuple[float, float, int, int] | None:
        top, bot = float(h[s:].max()), float(l[s:].min())
        span = top - bot
        if n - s < min_bars or not 0 < span <= max_span_atr * atr_value:
            return None
        t_hi = _episodes(h[s:] >= top - band * span)
        t_lo = _episodes(l[s:] <= bot + band * span)
        return (top, bot, t_hi, t_lo) if t_hi >= 2 and t_lo >= 2 else None

    # The span only widens as the window grows backwards, so the windows that
    # fit the cap are the shortest `w_max`.
    fits = (np.maximum.accumulate(h[::-1]) - np.minimum.accumulate(l[::-1])) <= max_span_atr * atr_value
    w_max = n if fits.all() else int(np.argmin(fits))
    start = next((n - w for w in range(w_max, min_bars - 1, -1) if box(n - w)), None)
    if start is None:
        return None
    found = box(start)
    tol = 0.25 * atr_value
    while (nxt := box(start + 1)) is not None and (h[start] > nxt[0] + tol or l[start] < nxt[1] - tol):
        start, found = start + 1, nxt
    top, bot, t_hi, t_lo = found
    return {"price_low": bot, "price_high": top, "time_start": int(times[start]), "time_end": int(times[-1]),
            "bars": n - start, "touches_high": t_hi, "touches_low": t_lo}


# ------------------------------------------------------ triangles / wedges


_TRIANGLES = {
    (0, 1): "ascending_triangle", (-1, 0): "descending_triangle", (-1, 1): "symmetrical_triangle",
    (1, 1): "rising_wedge", (-1, -1): "falling_wedge",
}


def detect_triangle(
    df: pd.DataFrame,
    highs: Sequence[Swing],
    lows: Sequence[Swing],
    atr_value: float,
    lookback: int = 120,
    flat_slope_atr: float = 0.02,
    fit_tol_atr: float = 0.6,
    max_break_age: int = 10,
) -> dict | None:
    """Triangle or wedge from lines fitted through the latest swings.

    Fits a least-squares line through the last 3 (at least 2) swing highs and
    the last 3 swing lows inside the last `lookback` bars. Both sets must cover
    overlapping stretches, each swing must sit within `fit_tol_atr` ATRs of
    its line, closes must stay inside the lines (same tolerance) up to the last
    swing used, and the lines must converge without having crossed by the last
    bar. Slopes in ATR per bar below `flat_slope_atr` count as flat; the
    (upper, lower) slope signs pick the type.

    `broken` is the side of the first close outside the lines after the last
    swing used ("up" / "down"), or None; a break older than `max_break_age`
    bars means the pattern has played out and None is returned. Line endpoints
    are the fitted prices at the first and last swing of each set.
    """
    n = len(df)
    if atr_value <= 0:
        return None
    hs = [s for s in _by_idx(highs) if s.idx >= n - lookback][-3:]
    ls = [s for s in _by_idx(lows) if s.idx >= n - lookback][-3:]
    if len(hs) < 2 or len(ls) < 2 or max(hs[0].idx, ls[0].idx) >= min(hs[-1].idx, ls[-1].idx):
        return None
    tol = fit_tol_atr * atr_value
    lines = []
    for pts in (hs, ls):
        x = np.array([s.idx for s in pts], dtype=float)
        y = np.array([s.price for s in pts], dtype=float)
        slope, icpt = np.polyfit(x, y, 1)
        if np.abs(y - (slope * x + icpt)).max() > tol:
            return None
        lines.append((float(slope), float(icpt)))
    (su, iu), (sl, il) = lines
    start, pattern_end = min(hs[0].idx, ls[0].idx), max(hs[-1].idx, ls[-1].idx)
    gap0 = (su - sl) * start + iu - il
    gap1 = (su - sl) * (n - 1) + iu - il
    if not gap0 > gap1 > 0:
        return None

    def trend(slope: float) -> int:
        return 0 if abs(slope) / atr_value < flat_slope_atr else (1 if slope > 0 else -1)

    kind = _TRIANGLES.get((trend(su), trend(sl)))
    if kind is None:
        return None

    _, _, _, c = _ohlc(df)
    x = np.arange(n, dtype=float)
    upper, lower = su * x + iu, sl * x + il
    body = slice(start, pattern_end + 1)
    if (c[body] - upper[body]).max() > tol or (lower[body] - c[body]).max() > tol:
        return None
    after = np.arange(pattern_end + 1, n)
    outside = after[(c[after] > upper[after]) | (c[after] < lower[after])]
    broken = None
    if outside.size:
        b = int(outside[0])
        if b < n - 1 - max_break_age:
            return None
        broken = "up" if c[b] > upper[b] else "down"

    def endpoints(pts: list[Swing], slope: float, icpt: float) -> dict:
        p, q = pts[0], pts[-1]
        return {"time1": int(p.time), "price1": float(slope * p.idx + icpt),
                "time2": int(q.time), "price2": float(slope * q.idx + icpt)}

    return {"type": kind, "upper": endpoints(hs, su, iu), "lower": endpoints(ls, sl, il), "broken": broken}


# ---------------------------------------------------- double top / bottom


def detect_double(
    df: pd.DataFrame,
    highs: Sequence[Swing],
    lows: Sequence[Swing],
    atr_value: float,
    tol_atr: float = 0.5,
    min_bars_apart: int = 5,
    depth_atr: float = 1.5,
) -> dict | None:
    """Double top / bottom on the two latest swing highs / lows (the more recent wins).

    Top: the two latest swing highs within `tol_atr` ATRs of each other, at
    least `min_bars_apart` bars apart, with the lowest low between them at
    least `depth_atr` ATRs below the lower top; that low is the neckline.
    `confirmed` once a close after the second top is below the neckline. A
    close above both tops after the second one invalidates the pattern.
    Bottoms mirror this.
    """
    if atr_value <= 0:
        return None
    _, h, l, c = _ohlc(df)
    times = df["time"].to_numpy()
    found: list[tuple[int, dict]] = []
    for swings, top in ((highs, True), (lows, False)):
        ss = _by_idx(swings)
        if len(ss) < 2:
            continue
        a, b = ss[-2], ss[-1]
        if abs(a.price - b.price) > tol_atr * atr_value or b.idx - a.idx < min_bars_apart:
            continue
        between = slice(a.idx + 1, b.idx)
        after = c[b.idx + 1:]
        if top:
            m = a.idx + 1 + int(np.argmin(l[between]))
            neck = l[m]
            if min(a.price, b.price) - neck < depth_atr * atr_value or (after > max(a.price, b.price)).any():
                continue
            confirmed = bool((after < neck).any())
        else:
            m = a.idx + 1 + int(np.argmax(h[between]))
            neck = h[m]
            if neck - max(a.price, b.price) < depth_atr * atr_value or (after < min(a.price, b.price)).any():
                continue
            confirmed = bool((after > neck).any())
        found.append((b.idx, {
            "type": "double_top" if top else "double_bottom", "price1": float(a.price), "time1": int(a.time),
            "price2": float(b.price), "time2": int(b.time), "neckline": float(neck), "neck_time": int(times[m]),
            "confirmed": confirmed,
        }))
    return max(found, key=lambda f: f[0])[1] if found else None
