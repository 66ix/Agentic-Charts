"""Kimi + Agent: the agent's correction to Kimi Cooked's forecast line and its filter on Kimi's signals, each learned
from Kimi's own record on the chart and switched on only when it has proven itself on data it wasn't fitted on.

The ported engine (app/kimi) is not touched, so plain Kimi still matches TradingView and stays the yardstick.

Features (all causal: bar i uses candles up to i only), each scaled to about [-1, 1]:
  htf_trend   EMA 80 against EMA 200 in ATRs: the trend a timeframe about four times higher would show
  trend       EMA 20 against EMA 50 in ATRs
  rsi         (RSI 14 - 50) / 50
  range_pos   where the close sits in the last 100 candles' high-low range
  momentum    the last 10 candles' move in ATRs (per square root of a candle)

Forecast. For each forecast Kimi made, once its horizon H has passed, Kimi's error is
  (actual move to bar + H  -  Kimi's projected move) / (ATR / price x sqrt H)
i.e. in volatility units. A decayed ridge regression learns that error from the features at the forecast's bar.
Overlapping forecasts share most of their path, so only every H-th forecast is used, for fitting and for judging,
like the engine's own thinned regression. Judging is walk-forward: each forecast is corrected with the fit from the
forecasts that had finished before it was made, and the corrected end is compared with Kimi's own end. The correction
is switched on (`active`) when, on at least MIN_EVALS such forecasts, it cut the average end error by at least
MIN_GAIN and a paired t-test of the two errors gives at least MIN_T. Then the line drawn is Kimi's path plus the
correction ramped in over the horizon, the band moved with it; otherwise it is Kimi's own path.

Signals. Each resolved Kimi signal's realised R is learned from the features at its bar, turned to the signal's
direction (a long sees the features as they are, a short sees them flipped) plus one column per signal type. Walk-
forward, a signal is "take" when the fit from signals resolved before it predicts R > 0, else "skip". The filter is
switched on when, on at least MIN_SIGNALS judged signals, the ones it would take made money on average, more than
taking them all, and beat the ones it would skip by a clear margin (Welch's t at least MIN_T).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

FEATURES = ("htf_trend", "trend", "rsi", "range_pos", "momentum")
RIDGE = 2.0             # ridge penalty, in units of decayed forecasts
DECAY = 0.98            # per thinned forecast (half-life about 35 of them)
MIN_TRAIN = 20          # thinned forecasts a fit needs before it corrects anything
MIN_EVALS = 30          # walk-forward forecasts needed to judge it
MIN_GAIN = 0.02         # at least 2% less average end error than Kimi alone
MIN_T = 1.65            # one-sided 95% on the paired errors
MAX_NUDGE_VOL = 1.0     # the correction is capped at this many volatility units
SIG_RIDGE = 4.0
SIG_DECAY = 0.99
MIN_SIG_TRAIN = 15
MIN_SIGNALS = 25


# ----------------------------------------------------------------------------- features --


def features(o: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray) -> np.ndarray:
    """(n, len(FEATURES)) feature rows, each causal and scaled to about [-1, 1]; NaN where history is too short."""
    close = pd.Series(c, dtype=float)
    hi, lo = pd.Series(h, dtype=float), pd.Series(l, dtype=float)
    prev = close.shift(1)
    tr = pd.concat([hi - lo, (hi - prev).abs(), (lo - prev).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()

    def ema(n: int) -> pd.Series:
        return close.ewm(span=n, adjust=False, min_periods=n).mean()

    clip = lambda s, k: (s / k).clip(-1.0, 1.0)  # noqa: E731
    htf = clip((ema(80) - ema(200)) / atr, 3.0)
    trend = clip((ema(20) - ema(50)) / atr, 3.0)
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rsi = 100 - 100 / (1 + up / down.replace(0, np.nan))
    rsi = ((rsi.fillna(100.0) - 50) / 50).where(up.notna())
    hh, ll = hi.rolling(100, min_periods=50).max(), lo.rolling(100, min_periods=50).min()
    half = (hh - ll) / 2
    pos = ((close - (hh + ll) / 2) / half.replace(0, np.nan)).clip(-1.0, 1.0)
    mom = clip((close - close.shift(10)) / (atr * math.sqrt(10)), 3.0)
    return np.column_stack([htf, trend, rsi, pos, mom]).astype(float)


# ---------------------------------------------------------------------------- the fit --


class Ridge:
    """Decayed ridge regression with an intercept: each `add` first fades what came before by `decay`."""

    def __init__(self, k: int, ridge: float, decay: float) -> None:
        self.k, self.ridge, self.decay = k, ridge, decay
        self.xx = np.zeros((k + 1, k + 1))
        self.xy = np.zeros(k + 1)
        self.n = 0.0
        self.count = 0

    def add(self, x: np.ndarray, y: float) -> None:
        v = np.concatenate([[1.0], x])
        self.xx = self.xx * self.decay + np.outer(v, v)
        self.xy = self.xy * self.decay + v * y
        self.n = self.n * self.decay + 1.0
        self.count += 1

    def coef(self) -> np.ndarray:
        pen = np.eye(self.k + 1) * self.ridge
        pen[0, 0] = 1e-9  # the intercept isn't shrunk
        return np.linalg.solve(self.xx + pen, self.xy)

    def predict(self, x: np.ndarray) -> float:
        return float(self.coef() @ np.concatenate([[1.0], x]))


@dataclass
class ForecastFix:
    active: bool
    reason: str
    nudge_vol: float = 0.0          # the correction now, in volatility units
    nudge_pct: float = 0.0          # ... as a % move of price by the horizon
    evals: int = 0
    kimi_err: Optional[float] = None    # average end error, % of price
    agent_err: Optional[float] = None
    kimi_dir: Optional[float] = None    # of the forecasts that called a direction, the share that was right
    agent_dir: Optional[float] = None   # ... and of Kimi + Agent, on the same forecasts
    kimi_called: Optional[float] = None  # share of forecasts that called a direction (the rest end flat)
    t: Optional[float] = None
    weights: dict = field(default_factory=dict)
    now: dict = field(default_factory=dict)


FLAT_END = 1e-4  # a forecast ending within 0.01% of where it started calls no direction (as the headline says)


def _vol(atr_i: float, born: float, H: int) -> float:
    return atr_i / born * math.sqrt(H) if born > 0 and atr_i > 0 and H > 0 else 0.0


def forecast_fix(records: list[dict], feats: np.ndarray, atr: np.ndarray, last_bar: int) -> ForecastFix:
    """The correction to Kimi's latest forecast and its walk-forward record (see the module docstring). `records`
    are the engine's forecast records, oldest first (`bar`, `H`, `born`, `y`, and `out` once finished)."""
    done = [r for r in records if r.get("out") is not None]
    if not done:
        return ForecastFix(False, "No finished forecasts yet.")
    thinned, last_kept = [], -10**9
    for r in done:  # one forecast per horizon: overlapping ones share most of their path
        if r["bar"] - last_kept >= r["H"]:
            thinned.append(r)
            last_kept = r["bar"]
    fit = Ridge(len(FEATURES), RIDGE, DECAY)
    pending = sorted(thinned, key=lambda r: r["bar"] + r["H"])  # by the bar their outcome is known
    k = 0
    errs_k, errs_a, dir_k, dir_a = [], [], [], []
    for r in thinned:
        while k < len(pending) and pending[k]["bar"] + pending[k]["H"] <= r["bar"]:
            p = pending[k]
            x, vol = feats[p["bar"]], _vol(atr[p["bar"]], p["born"], p["H"])
            if vol > 0 and not np.isnan(x).any():
                kimi_end = (p["y"][-1] - p["born"]) / p["born"]
                real_end = (p["out"]["close_at_H"] - p["born"]) / p["born"]
                fit.add(x, (real_end - kimi_end) / vol)
            k += 1
        x, vol = feats[r["bar"]], _vol(atr[r["bar"]], r["born"], r["H"])
        if fit.count < MIN_TRAIN or vol <= 0 or np.isnan(x).any():
            continue
        nudge = max(-MAX_NUDGE_VOL, min(MAX_NUDGE_VOL, fit.predict(x))) * vol
        kimi_end = (r["y"][-1] - r["born"]) / r["born"]
        real_end = (r["out"]["close_at_H"] - r["born"]) / r["born"]
        errs_k.append(abs(real_end - kimi_end))
        errs_a.append(abs(real_end - kimi_end - nudge))
        called = abs(kimi_end) > FLAT_END  # a flat line (learned gains off) calls nothing: not 'wrong'
        dir_k.append(np.sign(kimi_end) == np.sign(real_end) if called else None)
        dir_a.append(np.sign(kimi_end + nudge) == np.sign(real_end) if called else None)
    # The fit as it stands now, from every finished forecast.
    while k < len(pending):
        p = pending[k]
        x, vol = feats[p["bar"]], _vol(atr[p["bar"]], p["born"], p["H"])
        if vol > 0 and not np.isnan(x).any():
            fit.add(x, ((p["out"]["close_at_H"] - p["born"]) - (p["y"][-1] - p["born"])) / p["born"] / vol)
        k += 1
    out = ForecastFix(False, "", evals=len(errs_k))
    if fit.count >= MIN_TRAIN:
        coef = fit.coef()
        out.weights = {"intercept": round(float(coef[0]), 4), **{f: round(float(b), 4) for f, b in zip(FEATURES, coef[1:])}}
    x_now = feats[last_bar]
    if not np.isnan(x_now).any():
        out.now = {f: round(float(v), 3) for f, v in zip(FEATURES, x_now)}
    if len(errs_k) < MIN_EVALS:
        out.reason = f"Learning: {len(errs_k)}/{MIN_EVALS} forecasts judged so far."
        return out
    ek, ea = np.array(errs_k), np.array(errs_a)
    diff = ek - ea
    sd = float(diff.std(ddof=1)) if len(diff) > 1 else 0.0
    t = float(diff.mean() / (sd / math.sqrt(len(diff)))) if sd > 0 else 0.0
    out.kimi_err, out.agent_err = round(float(ek.mean()) * 100, 3), round(float(ea.mean()) * 100, 3)
    called_k = [d for d in dir_k if d is not None]
    called_a = [d for d in dir_a if d is not None]
    out.kimi_called = round(len(called_k) / len(dir_k), 3)
    if called_k:
        out.kimi_dir, out.agent_dir = round(float(np.mean(called_k)), 3), round(float(np.mean(called_a)), 3)
    out.t = round(t, 2)
    gain = 1 - ea.mean() / ek.mean() if ek.mean() > 0 else 0.0
    out.active = gain >= MIN_GAIN and t >= MIN_T
    if out.active and fit.count >= MIN_TRAIN and not np.isnan(x_now).any():
        out.nudge_vol = round(max(-MAX_NUDGE_VOL, min(MAX_NUDGE_VOL, fit.predict(x_now))), 4)
    out.reason = (f"On {len(errs_k)} forecasts it wasn't fitted on, the correction cut the average end error from "
                  f"{out.kimi_err:.2f}% to {out.agent_err:.2f}% of price (t {t:+.1f})"
                  + ("." if out.active else f"; it needs {MIN_GAIN * 100:.0f}% less error and t {MIN_T:g} to be used."))
    return out


def apply_fix(fix: ForecastFix, path: list[float], band_high: list[float], band_low: list[float], atr_last: float,
              H: int) -> tuple[list[float], list[float], list[float], float]:
    """Kimi's path and band moved by the correction, ramped in over the horizon → (path, high, low, nudge %)."""
    born = path[0]
    end_shift = fix.nudge_vol * _vol(atr_last, born, H) * born if fix.active else 0.0
    n = len(path) - 1
    shift = [end_shift * (k / n) if n else 0.0 for k in range(len(path))]
    floor = born * 0.05
    p = [max(y + s, floor) for y, s in zip(path, shift)]
    hi = [y + s for y, s in zip(band_high, shift)]
    lo = [max(y + s, floor * 0.5) for y, s in zip(band_low, shift)]
    return p, hi, lo, (end_shift / born * 100 if born else 0.0)


# ---------------------------------------------------------------------------- signals --


@dataclass
class SignalFilter:
    active: bool
    reason: str
    verdicts: dict = field(default_factory=dict)   # (bar, typ, dir) -> ("take" | "skip", predicted R)
    judged: int = 0
    all_r: Optional[float] = None
    take_r: Optional[float] = None
    skip_r: Optional[float] = None
    took: int = 0
    take_win: Optional[float] = None
    all_win: Optional[float] = None
    t: Optional[float] = None


def verdict_key(s) -> tuple[int, int, int]:
    """A signal's verdict key: two signals can confirm on one candle (a B+? and a B-?, a DIV and a Pat BO)."""
    return int(s.bar), int(s.typ), int(s.dir)


RANDOM_TYPES = 15  # the engine's random-entry controls (types 15 and 16) are its baseline, not signals


def signal_filter(signals: list, feats: np.ndarray, n_types: int = RANDOM_TYPES) -> SignalFilter:
    """Take or skip each Kimi signal by the R the agent expects of it (see the module docstring). `signals` are the
    engine's Signal objects (bar, dir, typ, result 0 open / 1 win / 2 loss / 3 expiry, r, res_bar)."""
    rows = sorted((s for s in signals if s.typ < RANDOM_TYPES and not np.isnan(feats[s.bar]).any()),
                  key=lambda s: s.bar)
    k = len(FEATURES) + n_types

    def x_of(s) -> np.ndarray:
        onehot = np.zeros(n_types)
        onehot[min(s.typ, n_types - 1)] = 1.0
        return np.concatenate([feats[s.bar] * s.dir, onehot])

    fit = Ridge(k, SIG_RIDGE, SIG_DECAY)
    resolved = sorted((s for s in rows if s.result in (1, 2, 3) and not math.isnan(s.r)), key=lambda s: s.res_bar)
    j = 0
    out = SignalFilter(False, "")
    judged: list[tuple[float, float, bool]] = []  # (realised R, predicted R, won)
    for s in rows:
        while j < len(resolved) and resolved[j].res_bar < s.bar:
            fit.add(x_of(resolved[j]), float(resolved[j].r))
            j += 1
        if fit.count < MIN_SIG_TRAIN:
            continue
        pred = fit.predict(x_of(s))
        out.verdicts[verdict_key(s)] = ("take" if pred > 0 else "skip", round(pred, 3))
        if s.result in (1, 2, 3) and not math.isnan(s.r):
            judged.append((float(s.r), pred, s.result == 1))
    out.judged = len(judged)
    if len(judged) < MIN_SIGNALS:
        out.reason = f"Learning: {len(judged)}/{MIN_SIGNALS} signals judged so far."
        return out
    r_all = np.array([x[0] for x in judged])
    take = [x for x in judged if x[1] > 0]
    skip = [x for x in judged if x[1] <= 0]
    out.all_r = round(float(r_all.mean()), 3)
    out.all_win = round(float(np.mean([x[2] for x in judged])), 3)
    out.took = len(take)
    if take:
        out.take_r = round(float(np.mean([x[0] for x in take])), 3)
        out.take_win = round(float(np.mean([x[2] for x in take])), 3)
    if skip:
        out.skip_r = round(float(np.mean([x[0] for x in skip])), 3)
    t = 0.0
    if len(take) >= 2 and len(skip) >= 2:  # Welch's t on taken against skipped R
        a, b = np.array([x[0] for x in take]), np.array([x[0] for x in skip])
        se = math.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b))
        t = float((a.mean() - b.mean()) / se) if se > 0 else 0.0
    out.t = round(t, 2)
    out.active = bool(len(take) >= 10 and skip and out.take_r is not None and out.take_r > 0
                      and out.take_r > out.all_r and t >= MIN_T)
    if not take:
        out.reason = (f"Walk-forward on {len(judged)} signals ({out.all_r:+.2f}R a signal): it would have skipped "
                      f"every one, so every signal is shown as Kimi gives it.")
        return out
    out.reason = (f"Walk-forward on {len(judged)} signals: the {len(take)} it would take averaged "
                  f"{out.take_r:+.2f}R against {out.all_r:+.2f}R for all"
                  + (f", the {len(skip)} it would skip {out.skip_r:+.2f}R (t {t:+.1f})" if skip else "")
                  + ("." if out.active else "; it is used once the signals it takes make money and the gap is "
                     f"clear (t {MIN_T:g}), until then every signal is shown as Kimi gives it."))
    return out
