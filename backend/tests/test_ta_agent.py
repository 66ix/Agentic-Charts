import numpy as np
import pandas as pd
import pytest

from app.llm import rule_intent
from app.market_data import candles_to_df, resample, synthetic_klines
from app.schemas import AnalysisIntent, BoxOverlay, Candle, HorizontalLineOverlay
from app.ta_agent import analyze, atr, cluster_levels, find_swings, parabolic_sar, supply_demand_zones


def _df(n=400, symbol="INJUSDT", interval="4h"):
    return candles_to_df(synthetic_klines(symbol, interval, n, end=1_760_000_000))


def _range_market():
    """Deterministic oscillation between ~7.5 and ~7.9 with a demand base + impulse."""
    t = np.arange(300)
    mid = 7.7 + 0.2 * np.sin(t / 8)
    close = mid + np.random.default_rng(1).normal(0, 0.01, 300)
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) + 0.02
    low = np.minimum(open_, close) - 0.02
    return pd.DataFrame({"time": 1_700_000_000 + t * 14400, "open": open_, "high": high, "low": low,
                         "close": close, "volume": np.full(300, 1000.0)})


def test_swings_found_at_oscillation_extremes():
    df = _range_market()
    a = float(atr(df).iloc[-1])
    highs, lows = find_swings(df, a, prominence_atr=1.0)
    assert len(highs) >= 4 and len(lows) >= 4
    assert all(h.price > 7.8 for h in highs)
    assert all(l.price < 7.6 for l in lows)


def test_clusters_produce_one_resistance_and_one_support_band():
    df = _range_market()
    a = float(atr(df).iloc[-1])
    highs, lows = find_swings(df, a)
    zones = cluster_levels(highs + lows, a, 7.7, len(df), tolerance_atr=1.5)
    res = [z for z in zones if z.kind == "resistance"]
    sup = [z for z in zones if z.kind == "support"]
    assert res and sup
    top = max(res, key=lambda z: z.touches)
    bot = max(sup, key=lambda z: z.touches)
    assert 7.85 < top.mid < 7.95 and top.touches >= 4
    assert 7.45 < bot.mid < 7.55 and bot.touches >= 4


def test_demand_zone_detected_and_invalidation():
    rows = []
    price = 10.0
    for i in range(40):  # quiet chop
        rows.append((price, price + 0.05, price - 0.05, price + (0.01 if i % 2 else -0.01)))
    base_lo = 9.9
    rows.append((10.0, 10.04, base_lo, 10.01))  # base candle
    rows.append((10.01, 11.2, 10.0, 11.15))  # impulse up
    for _ in range(20):
        rows.append((11.15, 11.25, 11.05, 11.15))
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df["time"] = 1_700_000_000 + df.index * 3600
    df["volume"] = 1.0
    zones = supply_demand_zones(df, atr(df))
    demand = [z for z in zones if z.kind == "demand"]
    assert demand and demand[0].price_low <= base_lo + 1e-9

    df.loc[len(df) - 1, "close"] = 9.5  # close through the zone → invalid
    assert not [z for z in supply_demand_zones(df, atr(df)) if z.kind == "demand" and z.price_low <= 9.95]


def test_analyze_returns_grounded_overlays():
    df = _df()
    hi, lo = df["high"].max(), df["low"].min()
    higher = {"1d": _df(10, interval="1d")}
    intent = AnalysisIntent(features=["support_resistance", "supply_demand", "window_levels", "swings",
                                      "trendlines"], window_timeframes=["1d"], max_zones=2)
    res = analyze(df, intent, "4h", higher)
    assert res.overlays
    for o in res.overlays:
        if isinstance(o, BoxOverlay):
            assert lo - res.stats.atr <= o.price_low <= o.price_high <= hi + res.stats.atr
    boxes = [o for o in res.overlays if isinstance(o, BoxOverlay) and o.kind in ("resistance", "support")]
    assert len([b for b in boxes if b.kind == "resistance"]) <= 2
    assert any(isinstance(o, HorizontalLineOverlay) and o.label == "D1 window high" for o in res.overlays)
    assert len({o.id for o in res.overlays}) == len(res.overlays)


def test_parabolic_sar_flips_sides():
    df = _df(300)
    sar = parabolic_sar(df)
    above = (sar > df["high"]).sum()
    below = (sar < df["low"]).sum()
    assert above > 10 and below > 10


def test_resample_3h_from_1h():
    base = [Candle(time=10800 * 5 + i * 3600, open=i, high=i + 1, low=i - 1, close=i + 0.5, volume=1) for i in range(6)]
    out = resample(base, 10800)
    assert [c.time for c in out] == [54000, 64800]
    assert out[0].open == 0 and out[0].close == 2.5 and out[0].high == 3 and out[0].volume == 3


@pytest.mark.parametrize("prompt,feature,tf", [
    ("Identify the current H4 supply zone and key resistance high", "supply_demand", "4h"),
    ("show daily support and resistance", "support_resistance", "1d"),
    ("mark swing highs and lows on 15m", "swings", "15m"),
    ("draw trendlines", "trendlines", None),
])
def test_rule_intent(prompt, feature, tf):
    intent = rule_intent(prompt)
    assert feature in intent.features
    assert intent.timeframe == tf
