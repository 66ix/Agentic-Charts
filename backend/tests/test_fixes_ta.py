"""TA fixes: zone tests count visits, broken trendlines are marked, 24h change is 24 hours, closed candles in the
sell check, a stricter higher-timeframe confluence, zones independent of the candle count, and 1000x perpetuals."""

import asyncio

import numpy as np
import pandas as pd

from app.perp_map import PerpMap, build_map
from app.scanner import change_24h
from app.ta_agent import Swing, Zone, _trendline_break, _visits, cluster_levels, find_swings, mark_confluence


def test_a_long_visit_counts_as_one_test():
    assert _visits(np.array([False, True, True, True, False, True, False])) == 2
    assert _visits(np.array([True, True])) == 1 and _visits(np.array([], dtype=bool)) == 0


def _df(closes):
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame({"time": np.arange(len(c)) * 3600, "open": c, "high": c + 0.2, "low": c - 0.2, "close": c,
                         "volume": 1.0})


def test_a_broken_trendline_says_so():
    a, b = Swing(0, 0, 110.0, "high", 1.0), Swing(10, 36000, 105.0, "high", 1.0)  # falls 0.5 a bar
    held = _df([100.0] * 20)
    assert _trendline_break(held, a, b, 1.0, resistance=True) is None
    broke = _df([100.0] * 15 + [104.0, 104.0, 100.0, 100.0, 100.0])  # bar 15: line at 102.5, close 104
    assert _trendline_break(broke, a, b, 1.0, resistance=True) == 4


def test_change_24h_is_24_hours_on_daily_candles():
    d = pd.DataFrame({"time": [0, 86400, 172800], "open": [10, 11, 12], "close": [11, 12, 13]})
    assert change_24h(d) == round((13 / 12 - 1) * 100, 2)


def _z(lo, hi, kind):
    return Zone(lo, hi, kind, 2, 0, 0, 0.5)


def test_confluence_needs_real_overlap_and_the_same_side():
    zones = [_z(100, 102, "demand"), _z(110, 112, "support"), _z(120, 122, "resistance")]
    mark_confluence(zones, {"1d": [_z(101.8, 105, "support"),   # 0.2 of 2: too thin
                                   _z(110.5, 111.5, "resistance"),  # other side
                                   _z(121, 125, "supply")]})     # half: lines up
    assert [z.meta.get("htf") for z in zones] == [None, None, ["D1"]]


def test_zones_do_not_depend_on_how_many_candles_were_loaded():
    rng = np.random.default_rng(3)
    c = 100 + np.cumsum(rng.normal(0, 1, 600))
    df = pd.DataFrame({"time": np.arange(600) * 3600, "open": c, "high": c + 0.5, "low": c - 0.5, "close": c,
                       "volume": 1.0})

    def near(d):
        d = d.reset_index(drop=True)
        highs, lows = find_swings(d, 1.0)
        recent = [s for s in highs + lows if s.idx >= len(d) - 200]
        return sorted(round(s.price, 6) for s in recent)

    assert near(df.tail(500)) == near(df.tail(300))
    zs500 = cluster_levels(*[sum(find_swings(df.tail(500).reset_index(drop=True), 1.0), [])], 1.0, c[-1], 500)
    assert zs500


def test_1000x_perpetuals_are_mapped_and_scaled():
    info = {"symbols": [
        {"symbol": "1000PEPEUSDT", "baseAsset": "1000PEPE", "quoteAsset": "USDT", "contractType": "PERPETUAL",
         "status": "TRADING"},
        {"symbol": "1MBABYDOGEUSDT", "baseAsset": "1MBABYDOGE", "quoteAsset": "USDT", "contractType": "PERPETUAL",
         "status": "TRADING"},
        {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "contractType": "PERPETUAL",
         "status": "TRADING"}]}
    assert build_map(info) == {"PEPEUSDT": ("1000PEPEUSDT", 1000.0), "BABYDOGEUSDT": ("1MBABYDOGEUSDT", 1e6)}

    async def fetch():
        return info

    pm = PerpMap(fetch)
    assert asyncio.run(pm.resolve("pepeusdt")) == ("1000PEPEUSDT", 1000.0)
    assert pm.cached("BTCUSDT") == ("BTCUSDT", 1.0)
