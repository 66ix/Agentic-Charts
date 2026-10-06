import json
from dataclasses import dataclass

import numpy as np
import pandas as pd
import pytest

from app import indicators, patterns
from app.market_data import candles_to_df, synthetic_klines
from app.patterns import (
    detect_double,
    detect_range,
    detect_triangle,
    fair_value_gaps,
    liquidity_sweeps,
    order_blocks,
)
from app.ta_agent import atr, find_swings

T0, STEP = 1_700_000_000, 3600
K = 40.0  # mirror axis: price → K - price turns every bullish setup into its bearish twin


@dataclass
class S:
    idx: int
    time: int
    price: float
    kind: str
    prominence: float = 1.0
    structure: str = ""


def make_df(closes, highs=None, lows=None, opens=None, volume=None) -> pd.DataFrame:
    c = np.asarray(closes, dtype=float)
    o = np.asarray(opens, dtype=float) if opens is not None else np.concatenate([c[:1], c[:-1]])
    h = np.asarray(highs, dtype=float) if highs is not None else np.maximum(o, c) + 0.1
    l = np.asarray(lows, dtype=float) if lows is not None else np.minimum(o, c) - 0.1
    v = np.asarray(volume, dtype=float) if volume is not None else np.full(len(c), 100.0)
    return pd.DataFrame({"time": T0 + np.arange(len(c)) * STEP, "open": o, "high": h, "low": l, "close": c,
                         "volume": v})


def from_rows(rows) -> pd.DataFrame:
    """Bars from explicit (open, high, low, close) tuples."""
    o, h, l, c = (list(x) for x in zip(*rows))
    return make_df(c, highs=h, lows=l, opens=o)


def swing(df, idx, kind, price=None):
    p = price if price is not None else float(df["high" if kind == "high" else "low"].iloc[idx])
    return S(idx, int(df["time"].iloc[idx]), p, kind)


def mirror(df, highs, lows):
    m = df.copy()
    m["open"], m["close"] = K - df["open"], K - df["close"]
    m["high"], m["low"] = K - df["low"], K - df["high"]
    flip = lambda ss, kind: [S(s.idx, s.time, K - s.price, kind) for s in ss]  # noqa: E731
    return m, flip(lows, "high"), flip(highs, "low")


FLAT = (10.0, 10.2, 9.8, 10.0)


# ------------------------------------------------------- liquidity sweeps


def _sweep_market():
    rows = [FLAT] * 40
    rows[5] = (10.0, 11.0, 9.9, 10.5)  # swing high 11.0
    rows[8] = (10.0, 10.1, 9.0, 9.5)  # swing low 9.0
    rows[35] = (10.0, 11.3, 9.9, 10.7)  # wicks above 11.0, closes back below
    rows[37] = (10.0, 10.1, 8.7, 9.6)  # wicks below 9.0, closes back above
    df = from_rows(rows)
    return df, [swing(df, 5, "high")], [swing(df, 8, "low")]


def test_liquidity_sweeps_both_sides_newest_first():
    df, highs, lows = _sweep_market()
    sw = liquidity_sweeps(df, highs, lows)
    assert sw == [
        {"side": "sell_side", "direction": "bullish", "level": 9.0, "swing_time": int(df.time[8]),
         "time": int(df.time[37]), "extreme": 8.7, "close": 9.6},
        {"side": "buy_side", "direction": "bearish", "level": 11.0, "swing_time": int(df.time[5]),
         "time": int(df.time[35]), "extreme": 11.3, "close": 10.7},
    ]


def test_liquidity_sweep_counts_each_level_once_and_reports_the_furthest():
    df, highs, lows = _sweep_market()
    df.loc[36, ["high", "close"]] = [11.2, 10.6]  # a second wick through 11.0
    df.loc[15, "high"] = 10.8
    highs = highs + [swing(df, 15, "high")]  # a lower swing high swept by the same bar
    buy = [s for s in liquidity_sweeps(df, highs, lows) if s["side"] == "buy_side"]
    assert [(s["level"], s["time"]) for s in buy] == [(11.0, int(df.time[35]))]


def test_liquidity_sweep_negative_cases():
    df, highs, lows = _sweep_market()
    breakout = df.copy()
    breakout.loc[35, "close"] = 11.2  # closes beyond the level: a breakout, not a sweep
    assert all(s["side"] != "buy_side" for s in liquidity_sweeps(breakout, highs, lows))
    reclaimed = df.copy()
    reclaimed.loc[38, ["high", "close"]] = [11.6, 11.5]  # a later close reclaims the swept level
    assert all(s["side"] != "buy_side" for s in liquidity_sweeps(reclaimed, highs, lows))
    # Outside the lookback window.
    assert [s["side"] for s in liquidity_sweeps(df, highs, lows, lookback=3)] == ["sell_side"]
    assert liquidity_sweeps(df, [], []) == []


# -------------------------------------------------------- fair value gaps


def _fvg_market():
    rows = [FLAT] * 10 + [
        (10.0, 10.1, 9.9, 10.0),  # 10: candle before the gap (high 10.1)
        (10.05, 11.0, 10.0, 10.9),  # 11: displacement candle
        (10.9, 11.2, 10.6, 11.1),  # 12: candle after the gap (low 10.6)
    ] + [(11.1, 11.2, 11.0, 11.1)] * 7
    return from_rows(rows)


def test_fair_value_gap_bullish_unfilled():
    df = _fvg_market()
    gaps = fair_value_gaps(df, atr_value=1.0)
    assert gaps == [{"direction": "bullish", "price_low": 10.1, "price_high": 10.6, "time": int(df.time[11]),
                     "filled_pct": 0.0}]


def test_fair_value_gap_partial_and_full_fill():
    df = _fvg_market()
    df.loc[16, "low"] = 10.35  # trades half way into the gap
    (g,) = fair_value_gaps(df, atr_value=1.0)
    assert (g["price_low"], g["price_high"], g["filled_pct"]) == (10.1, 10.35, 0.5)
    df.loc[17, "low"] = 10.05  # trades through the whole gap
    assert fair_value_gaps(df, atr_value=1.0) == []


def test_fair_value_gap_bearish_mirror_and_size_filter():
    df, _, _ = mirror(_fvg_market(), [], [])
    (g,) = fair_value_gaps(df, atr_value=1.0)
    assert g["direction"] == "bearish"
    assert (g["price_low"], g["price_high"]) == pytest.approx((K - 10.6, K - 10.1))
    assert fair_value_gaps(df, atr_value=5.0) == []  # 0.5 gap < 0.2 * 5 ATR


def test_fair_value_gaps_nearest_first():
    df = _fvg_market()
    rows = list(zip(df.open, df.high, df.low, df.close)) + [
        (11.1, 11.2, 11.0, 11.1), (11.1, 12.0, 11.1, 11.9), (11.9, 12.2, 11.6, 12.1)] + [(12.1, 12.2, 12.0, 12.1)] * 3
    gaps = fair_value_gaps(from_rows(rows), atr_value=1.0)
    assert [(g["price_low"], g["price_high"]) for g in gaps] == [(11.2, 11.6), (10.1, 10.6)]
    assert len(fair_value_gaps(from_rows(rows), atr_value=1.0, max_results=1)) == 1


# ------------------------------------------------------------ order blocks


def _ob_market():
    rows = [(10.0, 10.1, 9.9, 10.0)] * 5 + [
        (10.0, 10.5, 9.95, 10.3),  # 5: swing high 10.5
        (10.3, 10.35, 10.0, 10.05),
        (10.05, 10.1, 9.8, 9.85),
        (9.85, 9.9, 9.6, 9.65),
        (9.65, 9.85, 9.4, 9.5),  # 9: last bearish candle at the low → the OB
        (9.5, 9.95, 9.45, 9.9),
        (9.9, 10.35, 9.85, 10.3),
        (10.3, 10.8, 10.25, 10.7),  # 12: closes above 10.5 → bullish break
        (10.7, 11.1, 10.6, 11.0),
        (11.0, 11.2, 10.9, 11.1),  # 14
        (11.1, 11.15, 9.8, 10.5),  # 15: wicks back into the OB
        (10.5, 10.9, 10.4, 10.8),
        (10.8, 11.0, 10.7, 10.9),
    ]
    df = from_rows(rows)
    return df, [swing(df, 5, "high")], [swing(df, 9, "low")]


def test_order_block_bullish():
    df, highs, lows = _ob_market()
    obs = order_blocks(df, highs, lows, pd.Series(0.3, index=df.index))
    assert obs == [{"direction": "bullish", "price_low": 9.4, "price_high": 9.85, "time": int(df.time[9]),
                    "tests": 1, "broken_level": 10.5}]


def test_order_block_bearish_mirror():
    df, highs, lows = mirror(*_ob_market())
    (ob,) = order_blocks(df, highs, lows, pd.Series(0.3, index=df.index))
    assert ob["direction"] == "bearish" and ob["time"] == int(df.time[9]) and ob["tests"] == 1
    assert (ob["price_low"], ob["price_high"], ob["broken_level"]) == pytest.approx((K - 9.85, K - 9.4, K - 10.5))


def test_order_block_dropped_once_closed_through():
    df, highs, lows = _ob_market()
    df = from_rows(list(zip(df.open, df.high, df.low, df.close)) + [(10.9, 10.95, 9.2, 9.3)])
    obs = order_blocks(df, highs, lows, pd.Series(0.3, index=df.index))
    # The bullish OB is gone; the close below the 9.4 swing low is itself a
    # bearish break whose OB is the last up candle at the high (bar 14).
    assert [(o["direction"], o["price_low"], o["price_high"]) for o in obs] == [("bearish", 10.9, 11.2)]


def test_order_block_needs_a_real_move():
    df, highs, lows = _ob_market()
    assert order_blocks(df, highs, lows, pd.Series(2.0, index=df.index)) == []
    assert order_blocks(df, [], [], pd.Series(0.3, index=df.index)) == []


# ------------------------------------------------------------------ range


def _trend_then_range():
    trend = np.linspace(5.0, 9.5, 40)
    t = np.arange(60)
    rng = 10.0 + 0.4 * np.sin(2 * np.pi * t / 15)
    closes = np.concatenate([trend, rng])
    o = np.concatenate([closes[:1], closes[:-1]])
    return make_df(closes, highs=np.maximum(o, closes) + 0.05, lows=np.minimum(o, closes) - 0.05, opens=o)


def test_range_detected_after_trend():
    df = _trend_then_range()
    r = detect_range(df, atr_value=0.35)
    assert r is not None
    assert 58 <= r["bars"] <= 62
    assert r["price_high"] == df.high.iloc[-60:].max()
    # The bar that gapped up from the trend into the range (low 9.45) is trimmed off.
    assert r["price_low"] == pytest.approx(df.low.iloc[-58:].min())
    assert r["touches_high"] >= 3 and r["touches_low"] >= 3
    assert r["time_end"] == int(df.time.iloc[-1]) and r["time_start"] == int(df.time.iloc[-r["bars"]])


def test_no_range_in_a_trend():
    fast = make_df(np.linspace(10, 20, 100))
    assert detect_range(fast, atr_value=0.3) is None
    # A slow grind fits the span cap but only touches each edge once.
    slow = make_df(np.linspace(10, 11, 100))
    assert detect_range(slow, atr_value=0.3) is None
    assert detect_range(make_df(np.full(10, 1.0)), atr_value=0.3) is None


# --------------------------------------------------------------- triangles

ATR = 0.5


def _triangle(upper, lower, n=100, hi_idx=(40, 60, 80), lo_idx=(50, 70, 90)):
    """Bars midway between two lines; swing highs / lows sit exactly on them."""
    x = np.arange(n, dtype=float)
    fu = np.poly1d(np.polyfit(hi_idx, upper, 1))
    fl = np.poly1d(np.polyfit(lo_idx, lower, 1))
    closes = (fu(x) + fl(x)) / 2
    df = make_df(closes, highs=closes + 0.05, lows=closes - 0.05, opens=closes)
    for i, p in zip(hi_idx, upper):
        df.loc[i, "high"] = p
    for i, p in zip(lo_idx, lower):
        df.loc[i, "low"] = p
    return df, [swing(df, i, "high") for i in hi_idx], [swing(df, i, "low") for i in lo_idx], fu, fl


@pytest.mark.parametrize("upper,lower,expected", [
    ((11.0, 11.0, 11.0), (9.0, 9.6, 10.2), "ascending_triangle"),
    ((11.0, 10.4, 9.8), (9.0, 9.0, 9.0), "descending_triangle"),
    ((11.0, 10.7, 10.4), (9.0, 9.3, 9.6), "symmetrical_triangle"),
    ((11.0, 11.4, 11.8), (9.0, 9.8, 10.6), "rising_wedge"),
    ((11.6, 10.8, 10.0), (9.4, 9.0, 8.6), "falling_wedge"),
])
def test_triangle_types(upper, lower, expected):
    df, highs, lows, _, _ = _triangle(upper, lower)
    tri = detect_triangle(df, highs, lows, ATR)
    assert tri is not None and tri["type"] == expected and tri["broken"] is None
    assert (tri["upper"]["time1"], tri["upper"]["time2"]) == (int(df.time[40]), int(df.time[80]))
    assert (tri["lower"]["time1"], tri["lower"]["time2"]) == (int(df.time[50]), int(df.time[90]))
    assert (tri["upper"]["price1"], tri["upper"]["price2"]) == pytest.approx((upper[0], upper[-1]))
    assert (tri["lower"]["price1"], tri["lower"]["price2"]) == pytest.approx((lower[0], lower[-1]))


def test_triangle_breakout_reported_then_stale():
    df, highs, lows, fu, _ = _triangle((11.0, 11.0, 11.0), (9.0, 9.6, 10.2))
    df.loc[96:, "close"] = 11.4
    assert detect_triangle(df, highs, lows, ATR)["broken"] == "up"

    df, highs, lows, fu, _ = _triangle((11.0, 11.4, 11.8), (9.0, 9.8, 10.6), n=120)
    df.loc[95:, "close"] = fu(np.arange(95, 120)) + 0.5  # broke out 24 bars ago
    assert detect_triangle(df, highs, lows, ATR) is None


def test_no_triangle_for_broadening_flat_or_ragged_swings():
    broadening = _triangle((11.0, 11.3, 11.6), (9.0, 8.7, 8.4))
    assert detect_triangle(*broadening[:3], ATR) is None
    rectangle = _triangle((11.0, 11.0, 11.0), (9.0, 9.0, 9.0))
    assert detect_triangle(*rectangle[:3], ATR) is None
    df, highs, lows, _, _ = _triangle((11.0, 11.0, 11.0), (9.0, 9.6, 10.2))
    ragged = highs[:1] + [S(highs[1].idx, highs[1].time, 11.8, "high")] + highs[2:]
    assert detect_triangle(df, ragged, lows, ATR) is None
    assert detect_triangle(df, highs[:1], lows, ATR) is None


# ---------------------------------------------------------- double top/bottom


def _double_top(extra=()):
    pts = [(0, 8.0), (20, 10.0), (30, 8.6), (40, 10.0), (59, 9.0), *extra]
    x, y = zip(*pts)
    df = make_df(np.interp(np.arange(x[-1] + 1), x, y))
    return df, [swing(df, 20, "high"), swing(df, 40, "high")], []


def test_double_top_unconfirmed_then_confirmed():
    df, highs, lows = _double_top()
    d = detect_double(df, highs, lows, ATR)
    assert d == {"type": "double_top", "price1": 10.1, "time1": int(df.time[20]), "price2": 10.1,
                 "time2": int(df.time[40]), "neckline": pytest.approx(8.5), "neck_time": int(df.time[30]),
                 "confirmed": False}
    df, highs, lows = _double_top(extra=[(70, 8.0)])
    assert detect_double(df, highs, lows, ATR)["confirmed"] is True


def test_double_bottom_mirror():
    df, highs, lows = mirror(*_double_top(extra=[(70, 8.0)]))
    d = detect_double(df, highs, lows, ATR)
    assert d["type"] == "double_bottom" and d["confirmed"] is True
    assert (d["price1"], d["price2"], d["neckline"]) == pytest.approx((K - 10.1, K - 10.1, K - 8.5))


def test_double_negative_cases():
    df, highs, lows = _double_top()
    uneven = [highs[0], S(40, highs[1].time, 10.6, "high")]
    assert detect_double(df, uneven, lows, ATR) is None
    assert detect_double(df, highs, lows, atr_value=1.2) is None  # dip of 1.6 < 1.5 ATR
    assert detect_double(df, [highs[0], swing(df, 23, "high", 10.1)], lows, ATR) is None  # 3 bars apart
    df, highs, lows = _double_top(extra=[(70, 11.0)])  # rallies through both tops
    assert detect_double(df, highs, lows, ATR) is None


# ------------------------------------------------------------------ smoke


@pytest.mark.parametrize("symbol,interval", [("INJUSDT", "4h"), ("BTCUSDT", "15m"), ("SOLUSDT", "1d")])
def test_all_detectors_on_synthetic_data(symbol, interval):
    df = candles_to_df(synthetic_klines(symbol, interval, 500, end=1_760_000_000))
    atr_s = atr(df)
    a = float(atr_s.iloc[-1])
    highs, lows = find_swings(df, a)
    r = indicators.rsi(df["close"])
    line, sig, hist = indicators.macd(df["close"])
    out = {
        "rsi": float(r.iloc[-1]), "macd": [float(line.iloc[-1]), float(sig.iloc[-1]), float(hist.iloc[-1])],
        "divergence": indicators.rsi_divergence(df, r, highs, lows),
        "breaks": indicators.structure_breaks(df, highs, lows),
        "profile": indicators.volume_profile(df),
        "vwap": float(indicators.anchored_vwap(df, highs[-1].idx if highs else 0).iloc[-1]),
        "volume": indicators.volume_stats(df),
        "sweeps": patterns.liquidity_sweeps(df, highs, lows),
        "fvgs": patterns.fair_value_gaps(df, a),
        "order_blocks": patterns.order_blocks(df, highs, lows, atr_s),
        "range": patterns.detect_range(df, a),
        "triangle": patterns.detect_triangle(df, highs, lows, a),
        "double": patterns.detect_double(df, highs, lows, a),
    }
    json.dumps(out, allow_nan=False)
    assert 0 <= out["rsi"] <= 100 and out["breaks"] and out["profile"]

    lo, hi = float(df["low"].min()), float(df["high"].max())

    def prices(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, (dict, list)):
                    yield from prices(v)
                elif k.startswith(("price", "level", "neckline", "extreme", "close", "poc", "va")):
                    yield v
        elif isinstance(obj, list):
            for v in obj:
                yield from prices(v)

    assert all(lo - a <= p <= hi + a for p in prices(out))


def test_detectors_tolerate_tiny_frames():
    df = make_df([10.0, 10.5, 10.2])
    s = pd.Series(0.3, index=df.index)
    assert indicators.structure_breaks(df, [], []) == []
    assert indicators.rsi_divergence(df, indicators.rsi(df.close), [], []) is None
    assert patterns.fair_value_gaps(df.iloc[:2], 0.3) == []
    assert patterns.order_blocks(df, [], [], s) == []
    assert patterns.detect_range(df, 0.3) is None
    assert patterns.detect_triangle(df, [], [], 0.3) is None
    assert patterns.detect_double(df, [], [], 0.3) is None
    assert patterns.liquidity_sweeps(df, [], []) == []
