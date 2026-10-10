"""Desk and Kimi fixes: watched zones skipped for capacity are called once there is room, with the watched copy out
of learning; 'working now' needs significance; Kimi verdicts are per signal; flat forecasts aren't scored."""

import asyncio

import numpy as np

from app.desk_learning import hit_tails, learnable
from app.kimi_agent import forecast_fix, verdict_key
from tests.test_agent_desk import T0, FakeMarket, _desk


def test_a_zone_watched_for_capacity_is_called_once_there_is_room():
    end = T0 + 30
    market = FakeMarket(end)
    desk = _desk(market, max_active=1)
    made = asyncio.run(desk.run_interval("4h", now=end + 60, force=True))
    assert len(made) == 1
    watched = desk.calls(watched=True)
    full = [w for w in watched if w.skip_reason == "capacity"]
    assert full and all(w.symbol != made[0].symbol for w in full)
    assert all(w.skip_reason for w in watched)
    desk._calls[made[0].id] = made[0].model_copy(update={"status": "invalidated", "closed_at": end + 100})

    again = asyncio.run(desk.run_interval("4h", now=end + 120, force=True))
    assert len(again) == 1 and again[0].features.get("from_watched") in {w.id for w in full}
    was = desk._calls[again[0].features["from_watched"]]
    assert was.features["promoted_to"] == again[0].id
    was = was.model_copy(update={"level_hit": True})
    assert learnable([was], demo_ok=True) == []  # learns once, as the call
    # No second watched copy of the zones that are still waiting.
    keys = [(w.symbol, w.zone_low, w.zone_high) for w in desk.calls(watched=True, limit=5000)]
    assert len(keys) == len(set(keys))


def test_hit_tails():
    hi, lo = hit_tails([1 / 3] * 5, 3)
    assert abs(hi - 0.2099) < 1e-3 and lo > 0.9  # 3 hits of 5 at 1/3 odds: chance does it a fifth of the time
    assert hit_tails([0.5] * 12, 12)[0] == 0.5 ** 12


class Sig:
    def __init__(self, bar, typ, d):
        self.bar, self.typ, self.dir = bar, typ, d


def test_two_signals_on_one_candle_keep_their_own_verdicts():
    assert verdict_key(Sig(10, 0, 1)) != verdict_key(Sig(10, 0, -1)) != verdict_key(Sig(10, 1, -1))


def test_flat_forecasts_are_not_scored_for_direction():
    n, H = 400, 5
    rng = np.random.default_rng(1)
    feats = rng.normal(size=(n, 6)) * 0.01
    atr = np.full(n, 1.0)
    records = []
    for b in range(0, n - H, H):
        flat = b % 2 == 0
        end = 100.0 if flat else 101.0
        real = 100.5 if b % 3 else 99.5
        records.append({"bar": b, "H": H, "born": 100.0, "y": [100.0, end], "out": {"close_at_H": real}})
    import app.kimi_agent as ka
    feats = feats[:, :len(ka.FEATURES)] if feats.shape[1] >= len(ka.FEATURES) else np.pad(
        feats, ((0, 0), (0, len(ka.FEATURES) - feats.shape[1])))
    fix = forecast_fix(records, feats, atr, n - 1)
    assert fix.kimi_called is not None and 0.4 < fix.kimi_called < 0.6
    # Kimi calls up on every non-flat forecast; real is up 2/3 of the time.
    assert fix.kimi_dir is not None and 0.55 < fix.kimi_dir < 0.8
