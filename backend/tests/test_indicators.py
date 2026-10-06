from dataclasses import dataclass

import numpy as np
import pandas as pd
import pytest

from app.indicators import (
    anchored_vwap,
    macd,
    rsi,
    rsi_divergence,
    structure_breaks,
    volume_profile,
    volume_stats,
)

T0, STEP = 1_700_000_000, 3600


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


def swing(df, idx, kind, price=None):
    p = price if price is not None else float(df["high" if kind == "high" else "low"].iloc[idx])
    return S(idx, int(df["time"].iloc[idx]), p, kind)


# ------------------------------------------------------------------- RSI


def test_rsi_monotonic_series_hits_the_bounds():
    up = rsi(pd.Series(np.arange(1.0, 41.0)))
    down = rsi(pd.Series(np.arange(40.0, 0.0, -1.0)))
    assert up.iloc[:14].isna().all() and not up.iloc[14:].isna().any()
    assert (up.iloc[14:] == 100.0).all()
    assert (down.iloc[14:] == 0.0).all()
    assert (rsi(pd.Series(np.full(30, 5.0))).iloc[14:] == 50.0).all()


def test_rsi_matches_hand_computed_wilder_values():
    # Changes: +1, -0.5, +1, -0.5, +1 with period 3.
    # Seed: avg gain 2/3, avg loss 1/6 → RS 4 → 80.
    # Then gain (2/3*2+0)/3 = 4/9, loss (1/6*2+0.5)/3 = 5/18 → RS 1.6 → 61.538…
    # Then gain (4/9*2+1)/3 = 17/27, loss (5/18*2)/3 = 5/27 → RS 3.4 → 77.272…
    r = rsi(pd.Series([10, 11, 10.5, 11.5, 11, 12]), period=3)
    assert r.iloc[:3].isna().all()
    assert r.iloc[3:].tolist() == pytest.approx([80.0, 100 - 100 / 2.6, 100 - 100 / 4.4])


def test_rsi_matches_wilder_reference_series():
    # Classic 14-period worked example (StockCharts ChartSchool closes). Their
    # table rounds the averages (first RSI 70.53); unrounded: gains sum 3.34,
    # losses 1.40 → RS 2.3857 → 70.46, then a -0.28 change → 66.25.
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28,
              46.28, 46.00, 46.03, 46.41, 46.22, 45.64, 46.21]
    r = rsi(pd.Series(closes))
    assert r.iloc[14:].tolist() == pytest.approx([70.46, 66.25, 66.48, 69.35, 66.29, 57.92, 62.88], abs=0.01)


def test_rsi_short_series_is_all_nan():
    assert rsi(pd.Series([1.0, 2.0, 3.0])).isna().all()


# ------------------------------------------------------------------ MACD


def test_macd_components_are_consistent():
    close = pd.Series(np.linspace(10, 20, 80))
    line, sig, hist = macd(close)
    ema = lambda s, p: s.ewm(span=p, adjust=False).mean()  # noqa: E731
    assert np.allclose(line, ema(close, 12) - ema(close, 26))
    assert np.allclose(sig, ema(line, 9))
    assert np.allclose(hist, line - sig)
    assert line.iloc[-1] > 0
    flat_line, flat_sig, flat_hist = macd(pd.Series(np.full(50, 3.0)))
    assert np.allclose(flat_line, 0) and np.allclose(flat_sig, 0) and np.allclose(flat_hist, 0)


# ------------------------------------------------------------ divergence


def _div_setup(p1, p2, r1, r2, kind="high", i1=70, i2=90, n=100):
    df = make_df(np.full(n, 10.0))
    r = pd.Series(np.full(n, 50.0))
    r.iloc[i1], r.iloc[i2] = r1, r2
    sw = [swing(df, i1, kind, p1), swing(df, i2, kind, p2)]
    return (df, r, sw, []) if kind == "high" else (df, r, [], sw)


@pytest.mark.parametrize("p1,p2,r1,r2,kind,expected", [
    (10.0, 11.0, 75.0, 65.0, "high", ("bearish", "regular")),
    (11.0, 10.5, 60.0, 68.0, "high", ("bearish", "hidden")),
    (9.0, 8.5, 25.0, 32.0, "low", ("bullish", "regular")),
    (8.5, 9.0, 35.0, 28.0, "low", ("bullish", "hidden")),
])
def test_rsi_divergence_types(p1, p2, r1, r2, kind, expected):
    df, r, highs, lows = _div_setup(p1, p2, r1, r2, kind)
    d = rsi_divergence(df, r, highs, lows)
    assert d is not None and (d["type"], d["kind"]) == expected
    assert (d["price1"], d["price2"], d["rsi1"], d["rsi2"]) == (p1, p2, r1, r2)
    assert (d["time1"], d["time2"]) == (int(df.time[70]), int(df.time[90]))


def test_rsi_divergence_prefers_the_most_recent_pair():
    df, r, highs, _ = _div_setup(10.0, 11.0, 75.0, 65.0, "high", i1=70, i2=88)
    r.iloc[75], r.iloc[95] = 25.0, 32.0
    lows = [swing(df, 75, "low", 9.0), swing(df, 95, "low", 8.5)]
    assert rsi_divergence(df, r, highs, lows)["type"] == "bullish"


def test_rsi_divergence_negative_cases():
    # RSI confirms the higher high → no divergence.
    df, r, highs, lows = _div_setup(10.0, 11.0, 65.0, 75.0, "high")
    assert rsi_divergence(df, r, highs, lows) is None
    # Second swing too old (more than 40 bars back).
    df, r, highs, lows = _div_setup(10.0, 11.0, 75.0, 65.0, "high", i1=20, i2=50)
    assert rsi_divergence(df, r, highs, lows) is None
    # Swings too far apart.
    df, r, highs, lows = _div_setup(10.0, 11.0, 75.0, 65.0, "high", i1=10, i2=90)
    assert rsi_divergence(df, r, highs, lows) is None
    # NaN RSI (warm-up period) at the first swing.
    df, r, highs, lows = _div_setup(10.0, 11.0, np.nan, 65.0, "high")
    assert rsi_divergence(df, r, highs, lows) is None


# ------------------------------------------------------------- structure


def test_structure_breaks_bos_then_choch():
    closes = [10, 10.5, 10.8, 10, 9.2, 10, 11.2, 11.5, 12, 11, 10.5, 11, 12.5, 12, 11, 10.2, 10.0]
    df = make_df(closes)
    highs = [swing(df, 2, "high", 11.0), swing(df, 8, "high", 12.2)]
    lows = [swing(df, 4, "low", 9.0), swing(df, 10, "low", 10.4)]
    br = structure_breaks(df, highs, lows)
    assert [(b["type"], b["direction"], b["level"], b["idx"]) for b in br] == [
        ("BOS", "bullish", 11.0, 6), ("BOS", "bullish", 12.2, 12), ("CHoCH", "bearish", 10.4, 15)]
    assert br[0]["swing_time"] == int(df.time[2]) and br[0]["time"] == int(df.time[6])


def test_structure_break_ignores_unformed_swings_and_breaks_each_level_once():
    closes = [10, 10.6, 10.2, 9.8, 9.9, 9.7, 10.4, 10.6, 10.7, 10.3, 10.8]
    df = make_df(closes)
    # Swing high at idx 5 priced 10.5: the close of 10.6 at idx 1 came before it formed.
    br = structure_breaks(df, [swing(df, 5, "high", 10.5)], [])
    assert [(b["idx"], b["type"]) for b in br] == [(7, "BOS")]


# ---------------------------------------------------------------- volume


def test_volume_profile_finds_the_heavy_node():
    n = 60
    closes = np.full(n, 10.1)
    highs, lows = np.full(n, 10.2), np.full(n, 10.0)
    vol = np.full(n, 100.0)
    highs[:5], lows[:5], closes[:5], vol[:5] = 12.0, 11.0, 11.5, 5.0
    vp = volume_profile(make_df(closes, highs, lows, opens=closes, volume=vol), bins=40)
    assert 10.0 <= vp["poc"] <= 10.2
    assert vp["val"] == pytest.approx(10.0) and 10.15 <= vp["vah"] <= 10.25
    assert vp["val"] <= vp["poc"] <= vp["vah"]


def test_volume_profile_value_area_holds_70_percent():
    rng = np.random.default_rng(3)
    closes = 10 + np.cumsum(rng.normal(0, 0.1, 200))
    df = make_df(closes, volume=rng.uniform(50, 150, 200))
    vp = volume_profile(df)
    # Recompute volume inside [val, vah] with the same uniform-spread rule.
    h, l, v = df.high.to_numpy(), df.low.to_numpy(), df.volume.to_numpy()
    inside = np.clip(np.minimum(h, vp["vah"]) - np.maximum(l, vp["val"]), 0, None) / (h - l)
    assert (inside * v).sum() >= 0.70 * v.sum() - 1e-6


def test_volume_profile_without_volume_is_none():
    assert volume_profile(make_df([1, 2, 3], volume=[0, 0, 0])) is None
    assert volume_profile(make_df([])) is None


def test_anchored_vwap():
    df = make_df([10, 11, 12, 13], highs=[10, 11, 12, 13], lows=[10, 11, 12, 13], volume=[1, 1, 3, 1])
    vw = anchored_vwap(df, 1)
    assert np.isnan(vw.iloc[0])
    assert vw.iloc[1:].tolist() == pytest.approx([11.0, (11 + 36) / 4, (11 + 36 + 13) / 5])
    assert anchored_vwap(df, -1).iloc[-1] == pytest.approx(13.0)
    assert anchored_vwap(df, 10).isna().all()


def test_volume_stats():
    df = make_df(np.full(61, 10.0), volume=[100.0] * 60 + [300.0])
    assert volume_stats(df) == {"last_vs_avg": 3.0, "recent_vs_avg": 1.4}
    assert volume_stats(make_df(np.full(30, 10.0), volume=np.zeros(30))) == {"last_vs_avg": 1.0,
                                                                              "recent_vs_avg": 1.0}
    assert volume_stats(make_df([10.0])) == {"last_vs_avg": 1.0, "recent_vs_avg": 1.0}
