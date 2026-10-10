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


def test_a_stop_too_tight_for_the_fees_is_widened():
    from app.desk_calls import MAX_FEE_R, fee_r, widen_for_fees
    from app.schemas import PlanTarget, TradePlan

    tight = TradePlan(direction="long", entry=2499.0, stop=2492.0, risk_pct=0.28,
                      targets=[PlanTarget(price=2550.0, label="T1", rr=7.3)])
    wide = widen_for_fees(tight)
    assert fee_r(wide.risk_pct) <= MAX_FEE_R + 1e-9 and wide.stop < tight.stop
    assert wide.targets[0].rr < tight.targets[0].rr and any("fees" in n for n in wide.notes)
    roomy = tight.model_copy(update={"stop": 2400.0, "risk_pct": 3.96})
    assert widen_for_fees(roomy) is roomy


def test_weekly_candles_open_on_monday():
    import datetime

    from app.agent_desk import bar_open, due_bars

    t = bar_open("1w", 1_760_100_000)
    assert datetime.datetime.fromtimestamp(t, datetime.UTC).weekday() == 0
    [(tf, closed)] = due_bars(["1w"], {}, 1_760_100_000)
    assert tf == "1w" and closed == t - 7 * 86400


def test_watched_zones_say_why_they_were_not_called():
    end = T0 + 30
    desk = _desk(FakeMarket(end))
    desk.settings = desk.settings.model_copy(update={"min_confidence": 0.9})
    asyncio.run(desk.run_interval("4h", now=end + 60, force=True))
    watched = desk.calls(watched=True)
    assert watched and all(w.skip_reason in ("confidence", "expected R", "reward:risk") for w in watched)


def test_new_calls_are_broadcast_so_open_apps_refresh():
    end = T0 + 30
    desk = _desk(FakeMarket(end))
    seen = []
    desk.alerts.broadcast = lambda msg: seen.append(msg)
    made = asyncio.run(desk.run_interval("4h", now=end + 60, force=True))
    assert made and any(m.get("type") == "desk_scored" and set(m["changed"]) >= {c.id for c in made} for m in seen)


class FakeSignals:
    def __init__(self):
        self.made, self.ctx = {}, {}

    def register_context(self, prefix, fn):
        self.ctx[prefix] = fn

    async def add_trigger(self, spec, owner=None):
        from types import SimpleNamespace
        a = SimpleNamespace(spec=spec, owner=owner)
        self.made[owner] = a
        return a

    async def remove_owned(self, owner):
        return int(self.made.pop(owner, None) is not None)


def test_new_calls_arm_a_confirmation_trigger_in_their_zone():
    end = T0 + 30
    desk = _desk(FakeMarket(end))
    sig = FakeSignals()
    desk.attach_signals(sig)
    made = asyncio.run(desk.run_interval("4h", now=end + 60, force=True))
    assert made and set(sig.made) == {f"desk:{c.id}" for c in made if not c.shadow}
    c = made[0]
    t = sig.made[f"desk:{c.id}"]
    assert t.spec.interval == "15m" and t.spec.zone.price_low == c.zone_low and t.spec.zone.direction == "long"
    line = sig.ctx["desk:"](t)
    assert "buy zone" in line and "still waiting" in line and "%" not in line  # no desk odds on the ping
    # A call that ends takes its trigger with it.
    desk._calls[c.id] = c.model_copy(update={"status": "expired"})
    asyncio.run(desk._disarm_trigger(desk._calls[c.id]))
    assert f"desk:{c.id}" not in sig.made


def test_no_triggers_when_switched_off():
    end = T0 + 30
    desk = _desk(FakeMarket(end), arm_triggers=False)
    sig = FakeSignals()
    desk.attach_signals(sig)
    assert asyncio.run(desk.run_interval("4h", now=end + 60, force=True)) and not sig.made
