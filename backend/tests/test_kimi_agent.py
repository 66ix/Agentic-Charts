"""Kimi + Agent: the forecast correction and the signal filter switch on only when they beat plain Kimi on data they
weren't fitted on, and stay off on noise."""

import math
from types import SimpleNamespace

import numpy as np

from app.kimi_agent import FEATURES, apply_fix, features, forecast_fix, signal_filter
from app.market_data import synthetic_klines


def _records(n=3000, H=10, effect=0.6, seed=1):
    rng = np.random.default_rng(seed)
    feats = rng.uniform(-1, 1, (n, len(FEATURES)))
    atr = np.full(n, 1.0)
    born = 100.0
    vol = atr[0] / born * math.sqrt(H)
    recs = []
    for i in range(n):
        kimi_end = rng.normal(0, vol * 0.3)
        real_end = kimi_end + effect * vol * feats[i, 0] + rng.normal(0, vol * 0.4)
        y = born * (1 + kimi_end * np.arange(1, H + 1) / H)
        out = {"close_at_H": born * (1 + real_end)} if i + H < n else None
        recs.append({"bar": i, "H": H, "born": born, "y": y, "out": out})
    return recs, feats, atr


def test_the_correction_switches_on_when_kimis_error_is_predictable():
    recs, feats, atr = _records(effect=0.6)
    fix = forecast_fix(recs, feats, atr, len(feats) - 1)
    assert fix.active and fix.agent_err < fix.kimi_err and fix.t >= 1.65
    assert fix.weights["htf_trend"] > 0.3 and abs(fix.weights["rsi"]) < 0.2
    assert fix.nudge_vol == round(max(-1.0, min(1.0, fix.weights["intercept"] + sum(
        fix.weights[f] * feats[-1, j] for j, f in enumerate(FEATURES)))), 4)


def test_the_correction_stays_off_on_noise():
    recs, feats, atr = _records(effect=0.0)
    fix = forecast_fix(recs, feats, atr, len(feats) - 1)
    assert not fix.active and fix.nudge_vol == 0.0 and "needs" in fix.reason
    few, f2, a2 = _records(n=150)
    assert "Learning" in forecast_fix(few, f2, a2, 149).reason


def test_apply_fix_ramps_the_correction_in_over_the_horizon():
    recs, feats, atr = _records(effect=0.6)
    fix = forecast_fix(recs, feats, atr, len(feats) - 1)
    fix.nudge_vol = 0.5
    path = [100.0] + [100.0 + k * 0.1 for k in range(1, 11)]
    p, hi, lo, pct = apply_fix(fix, path, [x + 1 for x in path], [x - 1 for x in path], 1.0, 10)
    shift = 0.5 * (1.0 / 100 * math.sqrt(10)) * 100
    assert p[0] == 100.0 and math.isclose(p[-1], path[-1] + shift) and math.isclose(p[5], path[5] + shift / 2)
    assert math.isclose(hi[-1] - p[-1], 1.0) and math.isclose(pct, shift)
    fix.active = False
    assert apply_fix(fix, path, path, path, 1.0, 10)[0] == path


def _signals(n=200, informative=True, seed=2):
    rng = np.random.default_rng(seed)
    feats = rng.uniform(-1, 1, (n * 10, len(FEATURES)))
    sigs = []
    for k in range(n):
        bar = 10 * k + 5
        d = 1 if rng.random() < 0.5 else -1
        good = feats[bar, 1] * d > 0 if informative else rng.random() < 0.45
        win = good if rng.random() < 0.85 else not good
        sigs.append(SimpleNamespace(bar=bar, dir=d, typ=0, result=1 if win else 2, r=1.5 if win else -1.0,
                                    res_bar=bar + 8))
    # The engine's random-entry controls are its baseline, never filtered or learned from.
    sigs += [SimpleNamespace(bar=10 * k + 7, dir=1, typ=15, result=1, r=5.0, res_bar=10 * k + 9) for k in range(50)]
    return sigs, feats


def test_the_signal_filter_learns_which_signals_to_take():
    sigs, feats = _signals(informative=True)
    sf = signal_filter(sigs, feats)
    assert sf.active and sf.take_r > 0 > sf.skip_r and sf.take_r > sf.all_r and sf.t >= 1.65
    taken = [s for s in sigs if sf.verdicts.get(s.bar, ("", 0))[0] == "take"]
    assert taken and all(s.typ == 0 for s in taken)
    aligned = np.mean([feats[s.bar, 1] * s.dir > 0 for s in taken])
    assert aligned > 0.8


def test_the_signal_filter_stays_off_on_noise():
    sigs, feats = _signals(informative=False)
    assert not signal_filter(sigs, feats).active


def test_features_are_causal():
    cs = synthetic_klines("BTCUSDT", "1h", 600, end=1_760_000_000)
    o, h, l, c = (np.array([getattr(x, k) for x in cs]) for k in ("open", "high", "low", "close"))
    full = features(o, h, l, c)
    cut = features(o[:400], h[:400], l[:400], c[:400])
    assert np.allclose(full[:400], cut, equal_nan=True)  # later candles never change an earlier row
    assert np.nanmax(np.abs(full)) <= 1.0 and not np.isnan(full[-1]).any()
