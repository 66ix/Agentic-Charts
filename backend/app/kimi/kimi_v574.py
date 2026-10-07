"""
Kimi Cooked - Elite Edition v5.7.4 -- Python port (forecast engine + signal engine).

One bar-by-bar loop that follows the Pine script's own order of operations on CLOSED candles
(TradingView's history calculation; v5.7.4 makes the live chart decide signals on closes too):

    adaptive ATR / regime / structure  ->  pivots + divergences (regular, hidden, MACD, MFI)
    ->  pivot gap  ->  S/R engine  ->  early "?" divergences  ->  confluence score
    ->  resolve open signals  ->  register new signals + random-entry controls
    ->  PATH VERIFY sweep (scoring, conformal band, level odds, out-of-sample score, fit + gate)
    ->  record this bar's forecast

PORTED (same formulas and constants as the script):
  * dynamic ATR, ATR percentile, regime filter (efficiency ratio + choppiness), spike/noise pause
  * adaptive structure: effPivotLen, avgPivotGap, effLookback, effDivWindow, horizon (effDrawSteps)
  * RSI (variable length) + EMA smoothing, adaptive RSI levels, MACD histogram, MFI
  * regular / hidden divergences with the v5.7.1 pivot-RSI fix, MACD/MFI divergence recency,
    early divergence warnings, effDivThresh, separation rules
  * S/R levels: push / evict-weakest / touch + decay / mitigation / expiry / retest / near flags
  * HTF anchor (linreg slope of the auto-picked higher timeframe + second anchor, last
    COMPLETED HTF bar), quick drift projection
  * confluence factors w0 w1 w4 w5 w6 w7 w9, adaptive factor weights, top-% tiers
  * signal outcome tracking: fixed-R bracket (auto-scaled by timeframe), costs, expiry,
    loss-checked-first, random controls every 5th bar, the decayed "vs random" baseline,
    Exp/Trade - i.e. the Signal Stats table
  * forecast path: S/R magnets (taper, momentum damping, strength weights), learned gains,
    exhaustion next-candle tilt (with v5.7.3 lowest-timeframe gate and z < -2 auto-off)
  * PATH VERIFY: Traj Acc, Skill vs RW, Tgt Hit, MATE, Cover, Call %, Miss up/dn, decayed
    over Metric Window; skewed conformal band per horizon / side / daily-trend state; level
    odds (rch) and f_reach; out-of-sample shadows; thinned decayed OLS with intercept; the
    joint Wald gate with gain shrink (1 - 5/W)

NOT PORTED (their effects are absent here):
  * chart patterns and harmonics: no "Pat BO" / "Harmonics" rows, no pattern/harmonic magnets,
    confluence factors w2 / w3 are always off, their signals do not enter Long/Short/Conf rows
  * HTF divergence factor (w8, computed inside request.security on the higher timeframe)
  * session filters / session multipliers (defaults are off in the script)
  * drawings, labels, alerts
Because w2/w3/w8 are always off, confluence scores (and so the Conf top/rest split) can
differ from the chart. Everything else is meant to match closed-candle values exactly,
apart from the start of history (bar 0 = the first candle you pass in, as on the chart).

agentic-charts: chart patterns + harmonics (patterns_v574.py: detection, registry, break-outs / failures,
lifecycle, w2 / w3, the "Pat BO" and "Harmonics" rows, their Long / Short / Conf entries and forecast magnets)
and the session filter + multipliers (sessions_v574.py) are now ported and hooked into the loop below at the
script's own points (lines marked agentic-charts). The HTF divergence factor w8 is still off. With
Inputs(showChartPatterns=False, showHarmonics=False) the engine gives the original port's numbers.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

import numpy as np

from .patterns_v574 import HARM_NAMES, ChartPatterns, Harmonics   # agentic-charts
from .sessions_v574 import session_arrays                        # agentic-charts

__version__ = "5.7.4"


# ════════════════════════════════════════════════════════════════════════════════════════
# Inputs (defaults = the script's defaults; same names as the Pine variables)
# ════════════════════════════════════════════════════════════════════════════════════════
@dataclass
class Inputs:
    # core / oscillator
    lookback: int = 30
    maxSR: int = 4
    pivotLen: int = 5
    rsiPeriod: int = 9            # only used when autoStructure is off
    rsiOB: int = 60
    rsiOS: int = 35
    rsiAdapt: bool = True
    rsiSmooth: int = 3
    divThresh: float = 2.0
    divWindow: int = 50           # only used before the structure engine warms up / when it is off
    minDivBars: int = 0
    minDivGapATR: float = 0.5
    showEarlyDiv: bool = True     # NOTE: in the script this also gates Early signal registration
    # volatility
    atrPeriod: int = 14
    autoAtr: bool = True
    autoAtrMin: int = 5
    autoAtrMax: int = 50
    zoneWidth: float = 0.15
    autoZoneWidth: bool = True
    maxNoiseIdx: float = 0.8
    mitBuffer: float = 0.10
    autoMitBuf: bool = True
    # trend / HTF anchor
    regLen: int = 33
    trendMA: int = 200
    htfChoice: str = "Auto"       # "Auto" or minutes as string ("15","60","240") / "D" / "W" / "M"
    anchorSMAFilter: bool = False
    strictHtf: bool = True
    useHtf2Anchor: bool = True
    # auto structure
    autoStructure: bool = True
    anchorSwings: float = 4.0
    lookbackSwings: float = 8.0
    # forecast
    autoParams: bool = True
    forecastSteps: int = 20
    magnetMaxDist: float = 2.0
    magnetStrength: float = 0.25
    # verification
    verifyOn: bool = True
    vTolMult: float = 0.25        # Node Tolerance (x ATR)
    vWin: int = 500               # Metric Window (evaluations)
    # forecast refinements
    fcCover: float = 0.80         # Target Band Coverage
    fcCfGamma: float = 0.02       # Band Learning Rate
    fcTrLen: int = 50             # Daily Trend SMA Length
    fcSkewOn: bool = True
    fcNcOn: bool = True           # Next-Candle Exhaustion Call
    fcNcThr: float = 0.5          # Next-Candle Call Selectivity
    fcNcMinTf: int = 15           # Next-Candle Call: Lowest Timeframe (minutes)  (v5.7.3)
    # confluence
    confMinAlert: int = 50
    confUseVol: bool = True
    confVolMult: float = 1.2
    confVolLen: int = 20
    adaptWeights: bool = True
    # S/R retest
    srRetestOn: bool = True
    srRetestZone: float = 0.25
    srRetestAway: int = 5
    # stats
    statWinATR: float = 1.0
    statLossATR: float = 1.0
    statMaxBars: int = 48
    statCostPct: float = 0.15
    # display flags that change logic
    srExpire: bool = True
    mitigateBroken: bool = True
    showDivergence: bool = True
    showHiddenDiv: bool = True
    # regime
    useRegime: bool = True
    regimeLen: int = 20
    trendThresh: float = 0.50
    chopThresh: float = 0.30
    regimeConfirm: bool = True
    spikeMult: float = 2.5
    spikePctMin: float = 50.0
    # drawing budget inputs (only used for the forecast-steps line budget, as in the script)
    harmMaxSets: int = 3
    patMaxLabels: int = 5
    patBreakout: bool = True
    showFib: bool = True
    fibShowExt: bool = True
    fcTexture: bool = True
    showBands: bool = True
    # agentic-charts: the inputs of the modules ported in patterns_v574.py / sessions_v574.py (script defaults).
    # showChartPatterns=False and showHarmonics=False give the original port's numbers back.
    confWindow: int = 10              # Pattern Recency Window (bars) - confluence w2 / w3
    showChartPatterns: bool = True
    patTolATR: float = 0.75
    autoPatTol: bool = True
    patMinDepthATR: float = 1.0
    flagPoleATR: float = 3.0
    patExtend: int = 10
    patBoVolReq: bool = True
    patBoBufATR: float = 0.0
    autoPatBoBuf: bool = True
    patFailClean: bool = True
    patDupATR: float = 0.75
    patExpMult: float = 3.0
    patMaxTgtPct: float = 30.0
    showHarmonics: bool = True
    harmGartley: bool = True
    harmBat: bool = True
    harmBfly: bool = True
    harmCrab: bool = True
    harmDeepCrab: bool = True
    harmAltBat: bool = True
    harmShark: bool = True
    harmFiveZero: bool = True
    harm3Drives: bool = True
    harmABCD: bool = True
    harmTol: float = 0.05
    harmTolD: float = 0.025
    przConfluence: bool = True
    przDivConfirm: bool = True
    useCrossInv: bool = True
    harmMinXA: float = 1.0
    harmShowPRZ: bool = True
    harmExtend: int = 15
    harmExpBars: int = 150
    fibShowPocket: bool = True        # also gates the PRZ golden-pocket tier
    useSessFilter: bool = False
    sessTz: str = "UTC"
    sess1On: bool = True
    sess1: str = "0000-0800"
    sess2On: bool = True
    sess2: str = "1200-2100"
    useSessionParams: bool = False
    asiaZoneMult: float = 1.3
    asiaDivMult: float = 1.2
    ldnNyZoneMult: float = 0.85
    ldnNyDivMult: float = 0.85

    @staticmethod
    def users_chart() -> "Inputs":
        """The settings on the user's chart when this port was written (2 Oct 2026)."""
        return Inputs(rsiPeriod=6, vTolMult=0.05, fcCover=0.75)


# ════════════════════════════════════════════════════════════════════════════════════════
# Pine-equivalent helpers
# ════════════════════════════════════════════════════════════════════════════════════════
def _rnd(x: float) -> int:          # math.round (half away from zero for positives)
    return int(math.floor(x + 0.5))


def _sma(x, n):
    x = np.asarray(x, float)
    out = np.full(len(x), np.nan)
    if n <= 0 or len(x) < n:
        return out
    ok = ~np.isnan(x)
    c = np.insert(np.cumsum(np.where(ok, x, 0.0)), 0, 0.0)
    k = np.insert(np.cumsum(ok.astype(int)), 0, 0)
    idx = np.arange(n, len(x) + 1)
    s = c[idx] - c[idx - n]
    m = k[idx] - k[idx - n]
    out[n - 1:] = np.where(m == n, s / n, np.nan)
    return out


def _stdev(x, n):                   # ta.stdev (population)
    m = _sma(x, n)
    m2 = _sma(np.asarray(x, float) ** 2, n)
    return np.sqrt(np.maximum(m2 - m * m, 0.0))


def _ema(x, n):                     # ta.ema: seeded with the first non-na value
    a = 2.0 / (n + 1)
    out = np.full(len(x), np.nan)
    prev = np.nan
    for i, v in enumerate(x):
        if np.isnan(v):
            out[i] = prev
            continue
        prev = v if np.isnan(prev) else a * v + (1 - a) * prev
        out[i] = prev
    return out


def _rma(x, n):                     # ta.rma: SMA seed, then Wilder
    out = np.full(len(x), np.nan)
    s = _sma(x, n)
    prev = np.nan
    for i in range(len(x)):
        if np.isnan(prev):
            if not np.isnan(s[i]):
                prev = s[i]
        else:
            prev = prev + (x[i] - prev) / n
        out[i] = prev
    return out


def _highest(x, n):
    from numpy.lib.stride_tricks import sliding_window_view
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        out[n - 1:] = np.max(sliding_window_view(x, n), axis=1)
    return out


def _lowest(x, n):
    from numpy.lib.stride_tricks import sliding_window_view
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        out[n - 1:] = np.min(sliding_window_view(x, n), axis=1)
    return out


def _percentrank(x, n):             # ta.percentrank: share of the previous n values <= current
    out = np.full(len(x), np.nan)
    for i in range(n, len(x)):
        w = x[i - n:i]
        if np.isnan(x[i]) or np.isnan(w).any():
            continue
        out[i] = np.sum(w <= x[i]) / n * 100.0
    return out


def _mfi(h, l, c, v, n=14):         # ta.mfi(hlc3, 14)
    hlc3 = (h + l + c) / 3.0
    ch = np.concatenate([[np.nan], np.diff(hlc3)])
    up = np.where(np.isnan(ch), np.nan, np.where(ch <= 0, 0.0, hlc3) * v)
    dn = np.where(np.isnan(ch), np.nan, np.where(ch >= 0, 0.0, hlc3) * v)
    su = _sma(up, n) * n
    sd = _sma(dn, n) * n
    with np.errstate(divide="ignore", invalid="ignore"):
        return 100.0 - 100.0 / (1.0 + su / np.where(sd == 0, np.nan, sd))


# ── higher-timeframe helpers (chart bars -> HTF buckets, UTC like TradingView crypto) ──
_HTF_LADDER = [5, 15, 30, 60, 120, 240, 480, 1440, 10080, 43200]   # minutes; 10080 = W, 43200 = M
_HTF_NEXT = {5: 15, 15: 60, 30: 120, 60: 240, 120: 480, 240: 1440, 480: 1440, 1440: 10080, 10080: 43200, 43200: 43200}


def _bucket_ids(t_ms: np.ndarray, htf_min: int) -> np.ndarray:
    if htf_min == 43200:            # calendar month
        days = (t_ms // 86400000).astype("int64")
        d64 = days.astype("datetime64[D]")
        return (d64.astype("datetime64[M]").astype("int64"))
    if htf_min == 10080:            # weeks start Monday 00:00 UTC
        return (t_ms // 86400000 + 3) // 7
    return t_ms // (htf_min * 60000)


def _htf_map(chart_t: np.ndarray, src_t: np.ndarray, src_c: np.ndarray, htf_min: int):
    """HTF close series built from (src_t, src_c) - a longer history than the chart when available,
    like request.security, which always sees the higher timeframe's full history - and, for every
    chart bar, the index of the last COMPLETED HTF bar (the one before the bar's own bucket)."""
    sb = _bucket_ids(src_t, htf_min)
    chg = np.nonzero(np.diff(sb))[0]
    last_idx = np.append(chg, len(sb) - 1)
    hc = src_c[last_idx]
    hb = sb[last_idx]                                   # bucket id of each HTF bar (increasing)
    cb = _bucket_ids(chart_t, htf_min)
    ref = np.searchsorted(hb, cb, side="left") - 1      # last HTF bar strictly before the chart bar's bucket
    return ref, hc


def _linreg_slope_series(y: np.ndarray, L: int) -> np.ndarray:
    """ta.linreg(y, L, 0) - ta.linreg(y, L, 1) = OLS slope over the last L values."""
    out = np.full(len(y), np.nan)
    if len(y) < L:
        return out
    xs = np.arange(L) - (L - 1) / 2.0
    den = (xs * xs).sum()
    from numpy.lib.stride_tricks import sliding_window_view
    W = sliding_window_view(y, L)
    out[L - 1:] = (W - W.mean(axis=1, keepdims=True)) @ xs / den
    return out


# ════════════════════════════════════════════════════════════════════════════════════════
# Engine
# ════════════════════════════════════════════════════════════════════════════════════════
SIG_NAMES = {0: "DIV", 1: "U/Dn", 2: "Early", 15: "Random long", 16: "Random short"}
# agentic-charts: the pattern break-out and the ten harmonic signal types (types 5-14 follow hPatNm)
SIG_NAMES.update({3: "Pat BO", **{5 + k: nm for k, nm in enumerate(HARM_NAMES)}})


@dataclass
class Signal:
    bar: int
    time_ms: int
    dir: int
    typ: int
    entry: float
    conf: int
    tier: int
    mask: int
    tp: float
    sl: float
    exp: int
    result: int = 0          # 1 win, 2 loss, 3 expiry
    r: float = float("nan")  # realised R (costs charged once)
    res_bar: int = -1
    p_random: float = float("nan")   # decayed random-entry win rate used as the baseline
    # agentic-charts: where the script puts the label - the pivot candle and price for DIV / U/Dn,
    # the signal candle and the armed extreme for Early "?".
    pivot_bar: int = -1
    price: float = float("nan")


class KimiCooked:
    def __init__(self, tf_minutes: float, inputs: Optional[Inputs] = None):
        self.tf = float(tf_minutes)
        self.p = inputs or Inputs()

    # ────────────────────────────────────────────────────────────────────────────────
    def run(self, t_ms, o, h, l, c, v, history: Optional[dict] = None) -> "Result":
        """Candles oldest first (t_ms = open time in ms UTC). Bar 0 is the first candle passed, as
        bar 0 is the first loaded candle on the chart.
        history: optional dict(t=..., c=...) of the same symbol going further back (the chart's
        timeframe or finer is best; a coarser one, e.g. daily, is used only for daily-and-up requests). It feeds only the higher-timeframe requests - the HTF anchor and the
        daily trend state of the band - which on TradingView always see the full higher-timeframe
        history. Without it they are built from the candles passed, so on a short intraday window
        (e.g. 6,000 x 15m = 62 days) the 50-day trend SMA and the weekly anchor start late."""
        p = self.p
        t_ms = np.asarray(t_ms, dtype="int64")
        o, h, l, c, v = (np.asarray(a, float) for a in (o, h, l, c, v))
        N = len(c)
        tf = self.tf
        R = Result(self, t_ms, o, h, l, c, v)

        # ── vectorised per-bar series (identical on every bar) ──
        pc = np.concatenate([[np.nan], c[:-1]])
        tr = np.where(np.isnan(pc), h - l, np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc))))
        chg = c - pc
        rv_now = _stdev(100.0 * chg / np.where(np.isnan(pc), c, pc), 20)
        rv_avg = _sma(rv_now, 50)
        with np.errstate(divide="ignore", invalid="ignore"):
            rv_reg = rv_now / np.maximum(np.where(np.isnan(rv_avg), rv_now, rv_avg), 1e-10)
        rv_reg = np.where(np.isnan(rv_reg), 1.0, rv_reg)
        atr_fixed = _rma(tr, p.atrPeriod)
        atr_pct = _percentrank(atr_fixed, 252)
        rv_blend = rv_reg * (0.75 + 0.005 * np.where(np.isnan(atr_pct), 50.0, atr_pct))
        if p.autoAtr:
            eff_atr_p = np.array([max(p.autoAtrMin, min(p.autoAtrMax, _rnd(p.atrPeriod / max(0.5, min(2.0, math.sqrt(max(x, 0.1))))))) for x in rv_blend])
        else:
            eff_atr_p = np.full(N, p.atrPeriod)
        atr = np.empty(N)
        atr[0] = tr[0]
        for i in range(1, N):
            atr[i] = atr[i - 1] + (tr[i] - atr[i - 1]) / max(eff_atr_p[i], 1)
        atr_sma20 = _sma(atr, 20)
        e12, e26 = _ema(c, 12), _ema(c, 26)
        macd = e12 - e26
        macd_h = macd - _ema(macd, 9)
        mfi = _mfi(h, l, c, v, 14)
        # regime
        er_num = np.abs(c - np.concatenate([np.full(p.regimeLen, np.nan), c[:-p.regimeLen]]))
        er_den = _sma(np.abs(chg), p.regimeLen) * p.regimeLen
        with np.errstate(divide="ignore", invalid="ignore"):
            er = np.where(er_den > 0, er_num / np.where(er_den > 0, er_den, 1.0), 0.0)
        er = np.where(np.isnan(er), 0.0, er)
        ci_sum = _sma(tr, 14) * 14
        ci_rng = _highest(h, 14) - _lowest(l, 14)
        with np.errstate(divide="ignore", invalid="ignore"):
            chop = np.where(ci_rng > 0, 100.0 * np.log10(ci_sum / np.where(ci_rng > 0, ci_rng, 1.0)) / math.log10(14), 50.0)
        chop = np.where(np.isnan(chop), 50.0, chop)
        trending = p.useRegime & (er > p.trendThresh) & ((chop < 61.8) | (not p.regimeConfirm))
        ranging = p.useRegime & (er < p.chopThresh) & ((chop > 38.2) | (not p.regimeConfirm))
        spike = p.useRegime & (atr > atr_sma20 * p.spikeMult) & ~np.isnan(atr_sma20) & ((p.spikePctMin <= 0) | (np.where(np.isnan(atr_pct), 0.0, atr_pct) >= p.spikePctMin))
        tf_scale = max(0.06, min(math.sqrt(tf / 240.0), 2.0))
        micro = tf < 15.0
        with np.errstate(divide="ignore", invalid="ignore"):
            vol_reg = atr / np.maximum(np.where(np.isnan(atr_sma20), atr, atr_sma20), 1e-10)
        vol_reg = np.where(np.isnan(vol_reg), 1.0, vol_reg)
        sd_chg = _stdev(chg, 20)
        with np.errstate(divide="ignore", invalid="ignore"):
            noise = np.where(atr > 0, np.where(np.isnan(sd_chg), atr * 0.5, sd_chg) / np.where(atr > 0, atr, 1.0), 0.5)
        chop_filter = (p.maxNoiseIdx <= 0) | (noise <= p.maxNoiseIdx)
        pause = spike | ~chop_filter
        sig_bar = np.where(np.isnan(sd_chg), atr * 0.5, sd_chg)
        rng = h - l
        clv = np.where(rng > 0, ((c - l) - (h - c)) / np.where(rng > 0, rng, 1.0), 0.0)
        vol_sma = _sma(v, p.confVolLen)
        vol_spike = ~np.isnan(vol_sma) & (vol_sma > 0) & (v >= vol_sma * p.confVolMult)
        vol_high = p.confUseVol & vol_spike
        avg_bar_chg = _sma(chg, 5)
        eff_zone = (p.zoneWidth * np.clip(np.sqrt(np.maximum(vol_reg, 0.1)), 0.5, 2.0)) if p.autoZoneWidth else np.full(N, p.zoneWidth)
        eff_mitbuf = (p.mitBuffer * np.clip(vol_reg, 0.5, 2.0)) if p.autoMitBuf else np.full(N, p.mitBuffer)
        vol_adj = np.clip(np.sqrt(np.maximum(vol_reg, 0.1)), 0.85, 1.25) if p.autoStructure else np.ones(N)
        div_thr = (p.divThresh * (1.0 if tf >= 15.0 else 1.0 + 0.5 * math.sqrt((15.0 - tf) / 14.0)) * np.clip(vol_reg, 0.8, 1.5)) if p.autoParams else np.full(N, p.divThresh)
        dist_mult = (0.5 + 0.5 * tf_scale) + ((15.0 - tf) / 15.0 * 2.0 if micro else 0.0)
        mag_dist = (2.0 * dist_mult * (1.0 + (vol_reg - 1.0) * 0.15)) if p.autoParams else np.full(N, p.magnetMaxDist)
        mag_str = (0.25 * max(0.05, 1.0 + (1.0 - tf_scale) * 1.5) * np.maximum(0.5, 1.0 + (1.0 - vol_reg) * 0.2)) if p.autoParams else np.full(N, p.magnetStrength)
        mag_str = np.clip(mag_str, 0.0, 1.0)
        eff_stat_win = p.statWinATR * max(0.5, tf_scale) if p.autoParams else p.statWinATR
        eff_stat_loss = p.statLossATR * max(0.5, tf_scale) if p.autoParams else p.statLossATR
        eff_mitigate = p.mitigateBroken and not micro
        # agentic-charts: session filter / session multipliers (sessOk, sessZoneMult, sessDivMult)
        sess_ok, sess_zone, sess_div = session_arrays(t_ms, tf, p)
        div_thr = div_thr * sess_div
        eff_zone = eff_zone * sess_zone
        # forecast-steps line budget (rendering clamp; it also caps the scored horizon in the script)
        lines_per_step = 1 + (1 if p.fcTexture else 0) + (2 if p.showBands else 0)
        reserved = p.harmMaxSets * 7 + p.patMaxLabels * 6 + p.maxSR * 2 + (20 if p.patBreakout else 0) + ((9 if p.fibShowExt else 7) if p.showFib else 0) + 20
        fc_budget = max(5, int((450 - reserved) / lines_per_step))
        # exhaustion streak (next-candle call)
        streak = np.zeros(N)
        for i in range(1, N):
            if c[i] > c[i - 1]:
                streak[i] = streak[i - 1] + 1 if streak[i - 1] >= 0 else 1
            elif c[i] < c[i - 1]:
                streak[i] = streak[i - 1] - 1 if streak[i - 1] <= 0 else -1
        ex_score = -(np.clip(streak, -5, 5) / 3.0 + clv)
        ex_thr = 1.0 + 0.8 * p.fcNcThr
        ex_call = np.where(p.fcNcOn & (np.abs(ex_score) >= ex_thr), np.sign(ex_score), 0.0).astype(int)
        ex_tf_ok = tf * 60 >= p.fcNcMinTf * 60
        # daily trend state for the skewed band (last COMPLETED daily bar: close - SMA(fcTrLen))
        if history is not None:
            src_t = np.asarray(history["t"], dtype="int64"); src_c = np.asarray(history["c"], float)
            keep = src_t < t_ms[-1] + 1
            src_t, src_c = src_t[keep], src_c[keep]
            # make sure the chart's own candles are included at the end (history may stop earlier)
            if len(src_t) == 0 or src_t[-1] < t_ms[-1]:
                m = t_ms > (src_t[-1] if len(src_t) else -1)
                src_t = np.concatenate([src_t, t_ms[m]]); src_c = np.concatenate([src_c, c[m]])
        else:
            src_t, src_c = t_ms, c
        if len(src_t) > 1:
            g = np.diff(src_t); g = g[g > 0]
            src_tf = float(np.median(g)) / 60000.0 if len(g) else tf
        else:
            src_tf = tf
        # a coarser history cannot build a finer HTF: those rungs come from the chart's own candles
        pick = lambda rung: (src_t, src_c) if src_tf <= rung else (t_ms, c)
        tr_state = np.zeros(N, dtype=int)
        if p.fcSkewOn:
            if tf >= 1440:
                gap = c - _sma(c, p.fcTrLen)
                prev = np.concatenate([[np.nan], gap[:-1]])
                tr_state = (np.nan_to_num(prev) > 0).astype(int)
            else:
                ref, dc = _htf_map(t_ms, *pick(1440), 1440)
                dgap = dc - _sma(dc, p.fcTrLen)
                ok = ref >= 0
                vals = np.full(N, np.nan)
                vals[ok] = dgap[ref[ok]]
                tr_state = (np.nan_to_num(vals) > 0).astype(int)
        # HTF slope series for every ladder rung above the chart
        htf_slope: Dict[int, np.ndarray] = {}
        for rung in _HTF_LADDER:
            if rung > tf:
                ref, hc = _htf_map(t_ms, *pick(rung), rung)
                sl = _linreg_slope_series(hc, p.regLen)
                vals = np.full(N, np.nan)
                ok = ref >= 0
                vals[ok] = sl[ref[ok]]
                htf_slope[rung] = vals
        fixed_htf = None if p.htfChoice == "Auto" else {"D": 1440, "W": 10080, "M": 43200}.get(p.htfChoice, None) or int(p.htfChoice)

        # ── state ──
        avg_gap: Optional[float] = None
        last_gap_bar: Optional[int] = None
        avg_g = avg_l = None
        rsi_ema = np.nan
        a_rsi = 2.0 / (p.rsiSmooth + 1.0)
        smooth = np.full(N, np.nan)
        lr_now = None
        lr_prev = None
        lr_slope = None
        LAST = {1: dict(p=None, b=None, r=None, m=None, f=None), -1: dict(p=None, b=None, r=None, m=None, f=None)}
        last_div_dir = 0; last_div_bar = None; last_hdiv_dir = 0; last_hdiv_bar = None
        last_macd_bar = None; last_macd_dir = 0; last_mfi_bar = None; last_mfi_dir = 0
        EARLY = {1: dict(ref=None, armed=False, fired=False, arm=None), -1: dict(ref=None, armed=False, fired=False, arm=None)}
        # S/R: per side list of [price, bar, active, away, strength, touches, broken_bar]
        # (agentic-charts: broken_bar = the candle that mitigated the level, -1 while it holds)
        LEV: Dict[int, List[list]] = {1: [], -1: []}
        piv_hist: List[tuple] = []          # (bar, price, type) for the Fib swing, last 50
        # confluence weights
        base_w = np.array([12.0, 8.0, 8.0, 8.0, 18.0, 12.0, 8.0, 8.0, 12.0, 10.0])
        fw = base_w / base_w.sum() * 100.0
        f_wins = np.zeros(10); f_loss = np.zeros(10)
        conf_hist: List[int] = []
        # signal ledger
        open_sigs: List[Signal] = []
        st_wins = np.zeros(22, dtype=int); st_loss = np.zeros(22, dtype=int)
        exp_w = np.zeros(22); exp_v = np.zeros(22)
        st_r = np.zeros(8)    # [sigR, sigN, ctrlR, ctrlN, ctrlLongW, ctrlLongN, ctrlShortW, ctrlShortN]
        # next-candle call record
        ex_hit = 0.0; ex_n = 0.0
        # forecast verification state
        cfW = np.full(204, 1.30)
        rch = np.zeros(41)
        mzS = np.zeros(18)
        mzXX = np.zeros((4, 4)); mzXy = np.zeros(4)
        vf: List[dict] = []                  # open forecasts (insertion order)
        V = dict(evals=0, W=0.0, traj=0.0, mate=0.0, hits=0.0, flat=0.0, node_in=0.0, node_n=0.0,
                 calls=0.0, traj_n=0.0, tgt_n=0.0, miss_u=0.0, miss_d=0.0)
        oDk = 1.0 - 1.0 / max(p.vWin, 10)
        rec_out = R.forecasts
        H_arr = np.zeros(N, dtype=int)
        gains_arr = np.zeros((N, 3))
        tilt_arr = np.zeros(N)
        # agentic-charts: chart patterns + harmonics (patterns_v574.py) and their confluence recency bars
        CP = ChartPatterns(p)
        HM = Harmonics(p)
        last_pat_bar = {1: None, -1: None}      # lastBullChartPatBar / lastBearChartPatBar
        last_harm_bar = {1: None, -1: None}     # lastHarmBullBar / lastHarmBearBar

        for i in range(N):
            # ===== adaptive structure (uses the pivot gap as it stood before this bar) =====
            struct_ok = p.autoStructure and avg_gap is not None
            swing_gap = avg_gap if avg_gap is not None else 10.0
            gtr = (p.pivotLen * 2.5) / max(avg_gap, 1.0) if struct_ok else 1.0
            er_adj = (1.3 if er[i] < p.chopThresh else 0.9 if er[i] > p.trendThresh else 1.0) if p.useRegime else 1.0
            va = vol_adj[i]
            plen = max(2, min(15, _rnd(p.pivotLen * max(0.67, min(1.5, gtr)) * er_adj * va))) if p.autoStructure else p.pivotLen
            lb_base = _rnd(max(p.lookback, plen * 3) * 15.0 / tf) if micro else max(p.lookback, plen * 3)
            eff_lb = max(20, min(400, _rnd(avg_gap * p.lookbackSwings * va))) if struct_ok else lb_base
            rsi_p = max(5, min(21, plen * 2)) if p.autoStructure else p.rsiPeriod
            reg_len = max(12, min(100, plen * 6)) if p.autoStructure else p.regLen
            div_win = max(20, min(200, _rnd(avg_gap * 4.5))) if struct_ok else p.divWindow
            stat_exp = max(10, min(300, _rnd(avg_gap * 4.0))) if struct_ok else p.statMaxBars
            fc_steps = max(5, min(50, _rnd(avg_gap * 1.8))) if struct_ok else p.forecastSteps
            H = min(fc_steps, fc_budget)
            H_arr[i] = H
            min_div_bars = plen * 2 if p.minDivBars <= 0 else p.minDivBars
            # ===== RSI (f_rsiVar) + ta.ema smoothing =====
            ch_ = chg[i]
            gain = ch_ if (not np.isnan(ch_) and ch_ > 0) else 0.0
            loss = -ch_ if (not np.isnan(ch_) and ch_ < 0) else 0.0
            al = 1.0 / max(rsi_p, 1)
            avg_g = gain if avg_g is None else avg_g + al * (gain - avg_g)
            avg_l = loss if avg_l is None else avg_l + al * (loss - avg_l)
            raw_rsi = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)
            rsi_ema = raw_rsi if np.isnan(rsi_ema) else a_rsi * raw_rsi + (1 - a_rsi) * rsi_ema
            smooth[i] = rsi_ema
            if p.rsiAdapt and i >= 99:
                w100 = smooth[i - 99:i + 1]
                hH, hL = w100.max(), w100.min()
                dyn_ob = min(95, max(55, hL + (hH - hL) * 0.80))
                dyn_os = max(5, min(45, hL + (hH - hL) * 0.20))
            elif p.rsiAdapt:
                dyn_ob = dyn_os = np.nan
            else:
                dyn_ob, dyn_os = float(p.rsiOB), float(p.rsiOS)
            # ===== adaptive regression (f_smaVar) + slope =====
            lr_prev = lr_now
            lr_now = c[i] if lr_now is None else lr_now + (1.0 / reg_len) * (c[i] - lr_now)
            raw_sl = (lr_now - lr_prev) if lr_prev is not None else np.nan
            if not np.isnan(raw_sl):
                lr_slope = raw_sl if lr_slope is None else 0.5 * raw_sl + 0.5 * lr_slope   # ta.ema(.,3)
            # ===== HTF anchor (adaptive pick) =====
            if fixed_htf is not None and fixed_htf > tf:
                htf = fixed_htf
            elif p.autoStructure:
                tgt = tf * max(swing_gap * p.anchorSwings * va, 5.0)
                htf = None
                for x in (5, 15, 30, 60, 120, 240, 480, 1440, 10080):
                    if tgt <= x and tf < x:
                        htf = x
                        break
                if htf is None:
                    htf = 43200 if tf < 40000 else None
            else:   # autoHTF (fixed ladder when Auto Structure is off)
                htf = 15 if tf <= 1 else 60 if tf <= 5 else 120 if tf <= 15 else 240 if tf <= 30 else 480 if tf <= 60 else 1440 if tf <= 240 else 10080 if tf <= 1440 else 43200
            h2 = _HTF_NEXT.get(htf, htf) if htf is not None else None
            s1 = htf_slope[htf][i] if htf in htf_slope else np.nan
            h2avail = p.useHtf2Anchor and h2 is not None and h2 != htf
            s2 = htf_slope[h2][i] if (h2avail and h2 in htf_slope) else np.nan
            htf_bull = (not np.isnan(s1)) and s1 > 0 and ((not h2avail) or ((not np.isnan(s2)) and s2 > 0))
            htf_bear = (not np.isnan(s1)) and s1 < 0 and ((not h2avail) or ((not np.isnan(s2)) and s2 < 0))
            htf_dir = 0.0 if np.isnan(s1) else float(np.sign(s1))
            raw_drift = (lr_slope if lr_slope is not None else 0.0) * 0.30 + (0.0 if np.isnan(avg_bar_chg[i]) else avg_bar_chg[i]) * 0.35 + htf_dir * atr[i] * 0.06
            step_drift = max(-atr[i] * 0.35, min(atr[i] * 0.35, raw_drift))
            # ===== next-candle exhaustion call record (v5.7.0 / v5.7.3) =====
            if i >= 1 and ex_call[i - 1] != 0 and c[i] != c[i - 1]:
                ex_hit = ex_hit * 0.998 + (1.0 if np.sign(c[i] - c[i - 1]) == ex_call[i - 1] else 0.0)
                ex_n = ex_n * 0.998 + 1.0
            ex_p = ex_hit / ex_n if ex_n > 0 else 0.5
            ex_off = ex_n >= 100.0 and (ex_p - 0.5) / math.sqrt(0.25 / ex_n) < -2.0
            nc_tilt = ex_call[i] * 0.1 * sig_bar[i] if (ex_tf_ok and ex_call[i] != 0 and not ex_off) else 0.0
            tilt_arr[i] = nc_tilt
            # ===== pivots (closed candle) =====
            conf_lo = conf_hi = None
            if i >= 2 * plen:
                cl_, ch2 = l[i - plen], h[i - plen]
                if (l[i - 2 * plen:i + 1] >= cl_).all():
                    conf_lo = cl_
                if (h[i - 2 * plen:i + 1] <= ch2).all():
                    conf_hi = ch2
            pv_rsi = smooth[i - plen] if i >= plen else np.nan
            pv_macd = macd_h[i - plen] if i >= plen else np.nan
            pv_mfi = mfi[i - plen] if i >= plen else np.nan
            # ===== f_runDivergenceFlags =====
            now = {1: dict(reg=False, hid=False, macd=False, mfi=False), -1: dict(reg=False, hid=False, macd=False, mfi=False)}
            pend = {1: None, -1: None}
            sep = {1: False, -1: False}
            for dd, pvt in ((1, conf_lo), (-1, conf_hi)):
                if pvt is None:
                    continue
                L = LAST[dd]
                pB = i - plen
                sepok = (L["b"] is None or pB - L["b"] >= min_div_bars) and (p.minDivGapATR <= 0 or L["p"] is None or abs(pvt - L["p"]) >= atr[i] * p.minDivGapATR)
                sep[dd] = sepok
                if sepok and L["p"] is not None and L["r"] is not None and not np.isnan(pv_rsi):
                    if (pvt - L["p"]) * dd < 0 and (pv_rsi - L["r"]) * dd > div_thr[i]:
                        now[dd]["reg"] = bool(sess_ok[i]) and (not pause[i]) and (not trending[i])   # agentic-charts: sessOk
                    elif (pvt - L["p"]) * dd > 0 and (pv_rsi - L["r"]) * dd < -div_thr[i]:
                        now[dd]["hid"] = bool(sess_ok[i]) and (not pause[i]) and (not ranging[i])
                if sepok and not pause[i] and L["m"] is not None and not np.isnan(pv_macd) and (pvt - L["p"]) * dd < 0 and (pv_macd - L["m"]) * dd > 0:
                    now[dd]["macd"] = True
                if sepok and not pause[i] and L["f"] is not None and not np.isnan(pv_mfi) and (pvt - L["p"]) * dd < 0 and (pv_mfi - L["f"]) * dd > 0:
                    now[dd]["mfi"] = True
                if now[dd]["reg"]:
                    last_div_dir = dd; last_div_bar = i - plen; pend[dd] = 0
                if now[dd]["hid"]:
                    last_hdiv_dir = dd; last_hdiv_bar = i - plen
                    if pend[dd] is None:
                        pend[dd] = 1
                if now[dd]["macd"]:
                    last_macd_bar = i - plen; last_macd_dir = dd
                if now[dd]["mfi"]:
                    last_mfi_bar = i - plen; last_mfi_dir = dd
            for dd, pvt in ((1, conf_lo), (-1, conf_hi)):
                if pvt is not None and sep[dd]:
                    L = LAST[dd]
                    L["p"] = pvt; L["b"] = i - plen
                    if not np.isnan(pv_rsi): L["r"] = pv_rsi
                    if not np.isnan(pv_macd): L["m"] = pv_macd
                    if not np.isnan(pv_mfi): L["f"] = pv_mfi
            div_recent = last_div_bar is not None and i - last_div_bar < div_win
            hdiv_recent = last_hdiv_bar is not None and i - last_hdiv_bar < div_win
            dbr = {1: (last_div_dir == 1 and div_recent) or (last_hdiv_dir == 1 and hdiv_recent),
                   -1: (last_div_dir == -1 and div_recent) or (last_hdiv_dir == -1 and hdiv_recent)}
            macd_rec = {dd: last_macd_bar is not None and i - last_macd_bar < div_win and last_macd_dir == dd for dd in (1, -1)}
            mfi_rec = {dd: last_mfi_bar is not None and i - last_mfi_bar < div_win and last_mfi_dir == dd for dd in (1, -1)}
            multi_osc = {dd: (now[dd]["reg"] or now[dd]["hid"] or dbr[dd]) and (macd_rec[dd] or mfi_rec[dd]) for dd in (1, -1)}
            if conf_lo is not None:
                piv_hist.append((i - plen, conf_lo, -1))
            if conf_hi is not None:
                piv_hist.append((i - plen, conf_hi, 1))
            if conf_lo is not None or conf_hi is not None:
                pb = i - plen
                if last_gap_bar is not None and pb > last_gap_bar:
                    g = pb - last_gap_bar
                    avg_gap = g if avg_gap is None else avg_gap * 0.75 + g * 0.25
                last_gap_bar = pb
            while len(piv_hist) > 50:
                piv_hist.pop(0)
            # ===== agentic-charts: f_detectChartPatterns (after the pivot history, before S/R, as in the script) =====
            pat_now = CP.detect(i, c[i], atr[i], vol_reg[i], tf_scale, plen, eff_lb, piv_hist,
                                conf_lo is not None or conf_hi is not None, bool(pause[i]))
            for dd, hit in zip((1, -1), pat_now):
                if hit:
                    last_pat_bar[dd] = i
            # ===== f_updateSR =====
            for dd, px in ((1, conf_lo), (-1, conf_hi)):
                if px is None:
                    continue
                Ls = LEV[dd]
                if len(Ls) >= p.maxSR:
                    wk, ws = -1, 999999.0
                    for k, x in enumerate(Ls):
                        if x[2]:
                            sc = x[4] + x[5] * 0.5 - (i - x[1]) * 0.02
                            if sc < ws:
                                ws, wk = sc, k
                    Ls.pop(wk if wk >= 0 else 0)
                Ls.append([px, i - plen, True, 0, 1.0, 0, -1])
            retest = {1: False, -1: False}
            for dd in (1, -1):
                Ls = LEV[dd]
                for x in Ls:
                    if x[2]:
                        if abs(c[i] - x[0]) <= atr[i] * eff_zone[i]:
                            x[5] += 1
                            x[4] = min(x[4] + 0.3, 6.0)
                        x[4] *= 0.998
                if eff_mitigate:
                    for x in Ls:
                        if x[2] and (x[0] - c[i]) * dd > atr[i] * eff_mitbuf[i]:
                            x[2] = False
                            x[6] = i
                if p.srRetestOn and sess_ok[i] and not pause[i]:   # agentic-charts: sessOk
                    tdist = atr[i] * p.srRetestZone
                    for x in Ls:
                        act = (x[2] if eff_mitigate else True) and ((not p.srExpire) or i - x[1] <= eff_lb)
                        if act:
                            if (c[i] - x[0]) * dd > tdist:
                                x[3] += 1
                            else:
                                if x[3] >= p.srRetestAway and (c[i] - x[0]) * dd > 0:
                                    retest[dd] = True
                                x[3] = 0
            near = {1: False, -1: False}
            nearest = {1: None, -1: None}
            for dd in (1, -1):
                for x in LEV[dd]:
                    act = (x[2] if eff_mitigate else True) and ((not p.srExpire) or i - x[1] <= eff_lb)
                    if act and (x[0] - c[i]) * dd < 0:
                        if abs(c[i] - x[0]) <= atr[i] * 2.0:
                            near[dd] = True
                        if nearest[dd] is None or (x[0] > nearest[dd] if dd > 0 else x[0] < nearest[dd]):
                            nearest[dd] = x[0]
            # ===== f_checkEarlyDivergence =====
            early_now = {1: False, -1: False}   # agentic-charts: earlyBullNow / earlyBearNow (PRZ divergence tier)
            for dd in (1, -1):
                if not p.showEarlyDiv:
                    break
                L = LAST[dd]
                E = EARLY[dd]
                if L["p"] is not None and L["r"] is not None and L["b"] is not None and i - L["b"] <= div_win:
                    if E["ref"] is None or E["ref"] != L["b"]:
                        E.update(armed=False, fired=False, arm=None, ref=L["b"])
                    beyond = l[i] < L["p"] if dd > 0 else h[i] > L["p"]
                    if beyond:
                        E["armed"] = True
                        ext = l[i] if dd > 0 else h[i]
                        E["arm"] = ext if E["arm"] is None else (min(E["arm"], ext) if dd > 0 else max(E["arm"], ext))
                    rsi_ok = smooth[i] > L["r"] + div_thr[i] if dd > 0 else smooth[i] < L["r"] - div_thr[i]
                    if E["armed"] and not E["fired"] and not now[dd]["reg"] and rsi_ok:
                        E["fired"] = True
                        early_now[dd] = bool(sess_ok[i]) and (not pause[i]) and (not trending[i])   # agentic-charts: sessOk
                        if early_now[dd] and pend[dd] is None:
                            pend[dd] = 2
            # ===== confluence (agentic-charts: w2 chart pattern / w3 harmonic recency ported; w8 HTF div still off) =====
            rsi_x = {1: (not np.isnan(dyn_os)) and smooth[i] < dyn_os, -1: (not np.isnan(dyn_ob)) and smooth[i] > dyn_ob}
            pat_rec = {dd: last_pat_bar[dd] is not None and i - last_pat_bar[dd] <= p.confWindow for dd in (1, -1)}
            harm_rec = {dd: last_harm_bar[dd] is not None and i - last_harm_bar[dd] <= p.confWindow for dd in (1, -1)}
            fac = {}
            for dd in (1, -1):
                fac[dd] = [near[dd], retest[dd], pat_rec[dd], harm_rec[dd], htf_bull if dd > 0 else htf_bear, rsi_x[dd],
                           step_drift > 0 if dd > 0 else step_drift < 0, bool(vol_high[i]), False, multi_osc[dd]]
            conf_w = {dd: int(sum(fw[k] for k in range(10) if fac[dd][k])) for dd in (1, -1)}
            mask = {dd: sum((1 << k) for k in range(10) if fac[dd][k]) for dd in (1, -1)}
            # ===== f_resolveSignalStats (newest first) =====
            for s in reversed(list(open_sigs)):
                if i <= s.bar:
                    continue
                tp_need = s.tp if not np.isnan(s.tp) else atr[i] * eff_stat_win
                sl_need = s.sl if not np.isnan(s.sl) else atr[i] * eff_stat_loss
                cst = s.entry * 2 * p.statCostPct / 100.0
                fav = h[i] - s.entry if s.dir > 0 else s.entry - l[i]
                adv = s.entry - l[i] if s.dir > 0 else h[i] - s.entry
                res = 2 if adv >= sl_need else 1 if fav >= tp_need + cst else 3 if i - s.bar >= s.exp else 0
                if res == 0:
                    continue
                s.result = res; s.res_bar = i
                if sl_need > 0:
                    rr = (tp_need if res == 1 else (-sl_need - cst) if res == 2 else s.dir * (c[i] - s.entry) - cst) / sl_need
                    s.r = rr
                    ro = 0 if s.typ < 15 else 2
                    st_r[ro] += rr; st_r[ro + 1] += 1
                if res < 3:
                    ck = 4 if s.dir > 0 else 6
                    p0 = (st_r[ck] + 1) / (st_r[ck + 1] + 2)
                    if s.typ >= 15:
                        (st_wins if res == 1 else st_loss)[s.typ] += 1
                        st_r[ck] = st_r[ck] * 0.995 + (1 if res == 1 else 0)
                        st_r[ck + 1] = st_r[ck + 1] * 0.995 + 1
                    else:
                        s.p_random = p0
                        slots = (s.typ, 18 if s.dir > 0 else 19, 20 if s.tier > 0 else 21 if s.tier == 0 else 17)
                        for sI in slots:
                            (st_wins if res == 1 else st_loss)[sI] += 1
                            exp_w[sI] += p0
                            exp_v[sI] += p0 * (1 - p0)
                        if p.adaptWeights:
                            for f in range(10):
                                if (s.mask >> f) & 1:
                                    if res == 1:
                                        f_wins[f] += 1
                                    else:
                                        f_loss[f] += 1
                nR = int(st_r[1])
                if s.typ < 15 and nR > 0 and nR % 50 == 0 and p.adaptWeights:
                    wr = (f_wins + 2.0) / (f_wins + f_loss + 4.0)
                    nw = base_w * (0.5 + wr)
                    fw = nw / nw.sum() * 100.0
                open_sigs.remove(s)
            # ===== register signals + random controls =====
            def register(dd, typ, conf, msk, at=None):
                tier = -1
                if typ < 15:
                    nH = len(conf_hist)
                    nLt = sum(1.0 if x < conf else 0.5 if x == conf else 0.0 for x in conf_hist)
                    tier = -1 if nH < 20 else (1 if nLt / nH >= 1.0 - p.confMinAlert / 100.0 else 0)
                    conf_hist.append(conf)
                    if nH >= 200:
                        conf_hist.pop(0)
                sgl = Signal(bar=i, time_ms=int(t_ms[i]), dir=dd, typ=typ, entry=c[i], conf=conf, tier=tier, mask=msk,
                             tp=atr[i] * eff_stat_win, sl=atr[i] * eff_stat_loss, exp=stat_exp)
                if typ < 2:      # agentic-charts: label position
                    sgl.pivot_bar, sgl.price = i - plen, float(conf_lo if dd > 0 else conf_hi)
                elif typ == 2:
                    sgl.pivot_bar, sgl.price = i, float(EARLY[dd]["arm"] if EARLY[dd]["arm"] is not None else c[i])
                elif at is not None:     # agentic-charts: harmonic D / pattern break-out level
                    sgl.pivot_bar, sgl.price = at
                open_sigs.append(sgl)
                R.signals.append(sgl)
            # ===== agentic-charts: f_detectHarmonics (after the resolver, before the divergence registrations) =====
            HM.lifecycle(i, c[i], h[i], l[i], atr[i], eff_mitbuf[i])
            div_at = {dd: now[dd]["reg"] or now[dd]["hid"] or early_now[dd] for dd in (1, -1)}
            for hs in HM.detect(i, plen, conf_lo, conf_hi, piv_hist, atr[i], bool(pause[i]), nearest[1], nearest[-1],
                                eff_zone[i], eff_mitbuf[i], eff_lb, div_at):
                if sess_ok[i] and not trending[i]:      # pauseEntries already gates detection
                    register(hs.dir, 5 + hs.best, conf_w[hs.dir] + hs.tier * 5, mask[hs.dir], (hs.points[4][0], hs.points[4][1]))
                    last_harm_bar[hs.dir] = i
            for dd in (1, -1):
                if pend[dd] is not None:
                    register(dd, pend[dd], conf_w[dd], mask[dd])
            if i % 5 == 0 and sess_ok[i] and not pause[i]:   # agentic-charts: sessOk
                register(1 if i % 10 == 0 else -1, 15 if i % 10 == 0 else 16, 0, 0)
            # ===== agentic-charts: f_updatePatternLifecycle (break-outs register "Pat BO") =====
            for pat in CP.lifecycle(i, c[i], c[i - 1] if i >= 1 else None, atr[i], vol_reg[i], bool(vol_spike[i]),
                                    bool(pause[i]), bool(ranging[i]), bool(sess_ok[i])):
                register(pat.dir, 3, conf_w[pat.dir], mask[pat.dir], (i, pat.breakout["price"]))
            # ===== PATH VERIFY sweep =====
            tot = dict(evals=0, traj=0.0, mate=0.0, hits=0, flat=0.0, node_in=0, node_n=0, calls=0, traj_n=0, tgt_n=0, miss_u=0, miss_d=0)
            if p.verifyOn and vf:
                for r in reversed(list(vf)):
                    Hr = r["H"]
                    if i - r["bar"] < Hr:
                        continue
                    born, tol, sgB, stB = r["born"], r["tol"], r["sig"], r["st"]
                    nrm = abs(born) if born != 0 else 1.0
                    hits = calls = ins = 0
                    sum_abs = sum_flat = 0.0
                    win_hi, win_lo = h[i], l[i]
                    for k in range(1, Hr + 1):
                        node, up, lo = r["y"][k - 1], r["up"][k - 1], r["lo"][k - 1]
                        act = c[i - (Hr - k)]
                        diff = act - node
                        if abs(node - born) > tol:
                            calls += 1
                            if np.sign(act - born) == np.sign(node - born) or abs(diff) <= tol:
                                hits += 1
                        sum_abs += abs(diff) / abs(node) if node != 0 else 0.0
                        sum_flat += abs(act - born) / abs(born) if born != 0 else 0.0
                        if lo <= act <= up:
                            ins += 1
                        elif act > up:
                            tot["miss_u"] += 1
                        else:
                            tot["miss_d"] += 1
                        if sgB > 0:
                            cfB = stB * 102 + min(k, 50)
                            sU = diff / (sgB * math.sqrt(k))
                            qS = 0.5 + 0.5 * p.fcCover
                            wU, wD = cfW[cfB], cfW[cfB + 51]
                            cfW[cfB] = max(0.05, min(12.0, wU + p.fcCfGamma * qS if sU > wU else wU - p.fcCfGamma * (1.0 - qS)))
                            cfW[cfB + 51] = max(0.05, min(12.0, wD + p.fcCfGamma * qS if -sU > wD else wD - p.fcCfGamma * (1.0 - qS)))
                        win_hi = max(win_hi, h[i - (Hr - k)])
                        win_lo = min(win_lo, l[i - (Hr - k)])
                    tot["evals"] += 1
                    aF = (c[i] - born) / nrm
                    shA = 0.0
                    for ka in range(3):
                        shJ = r["sh"][ka]
                        shA += shJ
                        mzS[12 + ka] = mzS[12 + ka] * oDk + abs(aF) - abs(aF - shJ)
                    mzS[16] = mzS[16] * oDk + abs(aF) - abs(aF - shA)
                    mzS[15] = mzS[15] * oDk + abs(aF)
                    tot["calls"] += calls
                    if calls > 0:
                        tot["traj_n"] += 1
                        tot["traj"] += hits * 100.0 / calls
                    tot["mate"] += sum_abs / Hr * 100.0
                    tot["flat"] += sum_flat / Hr * 100.0
                    if abs(r["y"][-1] - born) > tol:
                        tot["tgt_n"] += 1
                        tot["hits"] += 1 if ((win_hi >= r["tgt"]) if r["dirF"] > 0 else (win_lo <= r["tgt"])) else 0
                    if sgB > 0:
                        sqR = sgB * math.sqrt(Hr)
                        for j in range(20):
                            kj = 0.25 * (j + 1)
                            rch[j] = rch[j] * 0.9995 + (1.0 if (win_hi - born) / sqR >= kj else 0.0)
                            rch[20 + j] = rch[20 + j] * 0.9995 + (1.0 if (born - win_lo) / sqR >= kj else 0.0)
                        rch[40] = rch[40] * 0.9995 + 1.0
                    tot["node_in"] += ins
                    tot["node_n"] += Hr
                    xS, xH, cLv = r["rS"], r["rH"], r["cM"] / nrm
                    if xS != 0.0 or xH != 0.0 or cLv != 0.0:
                        mzS[11] += 1.0
                        if mzS[11] >= float(Hr):
                            mzS[11] = 0.0
                            aEnd = (c[i] - born) / nrm
                            xv = np.array([1.0, xS, xH, cLv])
                            mzXy = mzXy * 0.999 + xv * aEnd
                            mzXX = mzXX * 0.999 + np.outer(xv, xv)
                            mzS[9] = mzS[9] * 0.999 + aEnd * aEnd
                            mzS[10] = mzS[10] * 0.999 + 1.0
                            nEf = mzS[10]
                            if nEf > 30.0:
                                mInv = np.linalg.inv(mzXX + np.eye(4) * 1e-10)
                                bv = mInv @ mzXy
                                rss = mzS[9] - float(bv @ mzXy)
                                sg2 = max(rss, 0.0) / max(nEf - 4.0, 1.0)
                                xy0 = mzXy[0]
                                wJ = (mzS[9] - rss - xy0 * xy0 / mzXX[0, 0]) / sg2 if sg2 > 0.0 else 0.0
                                shr = 1.0 - 5.0 / wJ if (mzS[16] > 0.0 and wJ > 5.0) else 0.0
                                mzS[17] = wJ
                                for ka in range(1, 4):
                                    bA = bv[ka]
                                    seA = math.sqrt(max(sg2 * mInv[ka, ka], 0.0))
                                    mzS[2 + ka] = bA
                                    mzS[5 + ka] = bA / seA if seA > 0 else 0.0
                                    mzS[ka - 1] = max(-1.5, min(1.5, bA)) * shr
                    # finished: attach the outcome to the stored forecast and drop it
                    r["out"] = dict(close_at_H=c[i], traj_hits=hits, traj_calls=calls, inside=ins, mate=sum_abs / Hr * 100.0,
                                    flat=sum_flat / Hr * 100.0, win_hi=win_hi, win_lo=win_lo)
                    vf.remove(r)
                if tot["evals"] > 0:
                    V["evals"] += tot["evals"]
                    vDk = (1.0 - 1.0 / max(p.vWin, 10)) ** tot["evals"]
                    V["W"] = V["W"] * vDk + tot["evals"]
                    for key in ("traj", "mate", "flat", "node_in", "node_n", "calls", "traj_n", "tgt_n", "miss_u", "miss_d"):
                        V[key] = V[key] * vDk + tot[key]
                    V["hits"] = V["hits"] * vDk + tot["hits"]
            gains_arr[i] = mzS[0:3]
            # ===== record this bar's forecast (f_fcstPath + f_verifyRecord) =====
            if p.verifyOn and not np.isnan(avg_bar_chg[i]) and i >= 1:
                rS = (c[i] - c[i - 3]) / c[i - 3] if i >= 3 and c[i - 3] > 0 else 0.0
                rH = (c[i] - c[i - H]) / c[i - H] if i >= H and c[i - H] > 0 else 0.0
                pre_win = atr[i] * mag_dist[i] * 2.0
                mwin = atr[i] * mag_dist[i] * 1.5
                pool_side = {1: [], -1: []}
                for dd in (1, -1):
                    for x in LEV[dd]:
                        active = x[2] if eff_mitigate else True
                        if active and i - x[1] <= eff_lb and abs(x[0] - c[i]) < pre_win:
                            pool_side[dd].append((x[0], x[4]))
                # agentic-charts: live harmonic TP1s (7.667 pre-solved -> 2.0x) and the last pattern break-out
                # target (4.333 -> 1.5x) join the pool, supports after supports and resistances after resistances
                for hs in HM.sets:
                    if hs.state in (0, 4) and abs(hs.tp1 - c[i]) < pre_win:
                        pool_side[-1 if hs.tp1 > c[i] else 1].append((hs.tp1, 7.667))
                if CP.last_tgt is not None and i - CP.last_tgt_bar <= eff_lb and abs(CP.last_tgt - c[i]) < pre_win:
                    pool_side[-1 if CP.last_tgt > c[i] else 1].append((CP.last_tgt, 4.333))
                pool = [(lv, min(1.0 + (st - 1.0) * 0.15, 2.0)) for dd in (1, -1) for lv, st in pool_side[dd]]
                mom = abs(lr_slope if lr_slope is not None else 0.0) / atr[i] if atr[i] > 0 else 0.5
                gS, gH, gL = mzS[0], mzS[1], mzS[2]
                floor_ = c[i] * 0.05
                prevY = c[i]
                cum = 0.0
                ys = np.empty(H); dU = np.empty(H); dD = np.empty(H)
                for k in range(1, H + 1):
                    dfl = 0.0
                    for lv, wgt in pool:
                        dist = prevY - lv
                        if abs(dist) < mwin:
                            prox = 0.0 if mwin <= 0 else max(0.0, 1.0 - abs(dist) / mwin)
                            taper = min(abs(dist) / (0.25 * mwin), 1.0) if mwin > 0 else 0.0
                            force = atr[i] * prox * taper * mag_str[i] * 2.5
                            if np.sign(dist) == np.sign(step_drift):
                                force *= 1.0 - min(mom * 0.6, 0.85)
                            dfl += (0.0 if dist == 0 else (-1.0 if dist > 0 else 1.0) * force) * wgt
                    cum += dfl
                    cfB = tr_state[i] * 102 + min(k, 50)
                    sq = sig_bar[i] * math.sqrt(k)
                    bUp = sq * cfW[cfB]
                    bDn = sq * cfW[cfB + 51]
                    ramp = k / H
                    disp = (gS * rS + gH * rH) * c[i] * ramp + gL * cum + nc_tilt
                    ys[k - 1] = max(c[i] + max(-bDn, min(bUp, disp)), floor_)
                    dU[k - 1] = bUp; dD[k - 1] = bDn
                    prevY += dfl
                finalY = ys[-1]
                tgt = nearest[-1] if (finalY > c[i] and nearest[-1] is not None) else nearest[1] if (finalY < c[i] and nearest[1] is not None) else finalY
                rec = dict(bar=i, time_ms=int(t_ms[i]), H=H, born=c[i], y=ys, up=ys + dU, lo=np.maximum(ys - dD, floor_ * 0.5),
                           tgt=tgt, dirF=1.0 if finalY > c[i] else -1.0, tol=p.vTolMult * atr[i], sig=sig_bar[i], st=int(tr_state[i]),
                           rS=rS, rH=rH, cM=cum,
                           sh=np.array([max(-1.5, min(1.5, mzS[3])) * rS, max(-1.5, min(1.5, mzS[4])) * rH,
                                        max(-1.5, min(1.5, mzS[5])) * (cum / c[i] if c[i] > 0 else 0.0)]),
                           gains=(gS, gH, gL), tilt=nc_tilt, out=None)
                vf.append(rec)
                rec_out.append(rec)

        # ── final state ──
        R.final = dict(V=V, mzS=mzS.copy(), cfW=cfW.copy(), rch=rch.copy(), ex_hit=ex_hit, ex_n=ex_n,
                       ex_call_last=int(ex_call[-1]) if ex_tf_ok else 0, ex_tf_ok=ex_tf_ok,
                       ex_off=ex_n >= 100.0 and ((ex_hit / ex_n if ex_n else 0.5) - 0.5) / math.sqrt(0.25 / ex_n) < -2.0 if ex_n > 0 else False,
                       st_wins=st_wins.copy(), st_loss=st_loss.copy(), exp_w=exp_w.copy(), exp_v=exp_v.copy(), st_r=st_r.copy(),
                       factor_weights=fw.copy(), atr=atr[-1], sig=sig_bar[-1], H=H_arr[-1], close=c[-1],
                       eff_stat_loss=eff_stat_loss, levels={dd: [list(x) for x in LEV[dd]] for dd in (1, -1)},
                       eff_mitigate=eff_mitigate, eff_lb=eff_lb, piv_hist=list(piv_hist),
                       eff_zone=float(eff_zone[-1]), vol_reg=float(vol_reg[-1]),   # agentic-charts: for drawing
                       patterns=list(CP.drawn), breakouts=list(CP.bos), harmonics=list(HM.sets))
        R.H = H_arr
        R.gains = gains_arr
        R.tilt = tilt_arr
        R.atr = atr
        R.sig = sig_bar
        R.ex_call = ex_call
        return R


# ════════════════════════════════════════════════════════════════════════════════════════
# Results: the two tables, level odds, raw records
# ════════════════════════════════════════════════════════════════════════════════════════
class Result:
    def __init__(self, eng: KimiCooked, t_ms, o, h, l, c, v):
        self.eng = eng
        self.t_ms, self.o, self.h, self.l, self.c, self.v = t_ms, o, h, l, c, v
        self.signals: List[Signal] = []
        self.forecasts: List[dict] = []
        self.final: dict = {}

    # ---------------------------------------------------------------- PATH VERIFY table
    def verify_table(self) -> Dict[str, object]:
        p = self.eng.p
        F = self.final
        V, mzS, cfW = F["V"], F["mzS"], F["cfW"]
        n = V["W"]
        out: Dict[str, object] = {"Evaluated": V["evals"]}
        out["Traj Acc %"] = V["traj"] / V["traj_n"] if V["traj_n"] >= 1.0 else None
        out["Skill vs RW %"] = (1.0 - V["mate"] / V["flat"]) * 100.0 if (n > 0 and V["flat"] > 0) else None
        out["Tgt Hit %"] = V["hits"] / V["tgt_n"] * 100.0 if V["tgt_n"] >= 1.0 else None
        out["MATE %"] = V["mate"] / n if n > 0 else None
        out[f"Cover % (tgt {p.fcCover * 100:.0f})"] = V["node_in"] / V["node_n"] * 100.0 if V["node_n"] > 0 else None
        out["Band w D▲ up/dn | D▼ up/dn"] = (round(float(np.mean(cfW[103:153])), 2), round(float(np.mean(cfW[154:204])), 2),
                                            round(float(np.mean(cfW[1:51])), 2), round(float(np.mean(cfW[52:102])), 2))
        out["Gain S/H/Lvl"] = tuple(round(float(x), 2) for x in mzS[0:3])
        out["Gain raw (OLS)"] = tuple(round(float(x), 2) for x in mzS[3:6]) if mzS[10] > 30 else f"learning {int(mzS[10])}/30"
        out["t S/H/Lvl | joint"] = (tuple(round(float(x), 1) for x in mzS[6:9]), round(float(mzS[17]), 1))
        oN = mzS[15]
        out["OOS S/H/Lvl | all %"] = (tuple(round(float(mzS[12 + k] / oN * 100.0), 2) for k in range(3)), round(float(mzS[16] / oN * 100.0), 2)) if oN > 0 else None
        out["Call %"] = V["calls"] / V["node_n"] * 100.0 if V["node_n"] > 0 else None
        out["Miss up/dn %"] = (V["miss_u"] / V["node_n"] * 100.0, V["miss_d"] / V["node_n"] * 100.0) if V["node_n"] > 0 else None
        ex_n, ex_hit = F["ex_n"], F["ex_hit"]
        call = "off" if not p.fcNcOn else (f"off <{p.fcNcMinTf}m" if not F["ex_tf_ok"] else "off here" if F["ex_off"] else {1: "▲", -1: "▼", 0: "-"}[F["ex_call_last"]])
        out["Next candle / right / n"] = (call, ex_hit / ex_n * 100.0 if ex_n >= 1 else None, round(ex_n))
        return out

    # ---------------------------------------------------------------- Signal Stats table
    def stats_table(self) -> Dict[str, object]:
        p = self.eng.p
        F = self.final
        W, Lo, eW, eV, sR = F["st_wins"], F["st_loss"], F["exp_w"], F["exp_v"], F["st_r"]
        nc = W[15] + Lo[15] + W[16] + Lo[16]

        def cell(a, b):
            w = int(W[a:b + 1].sum()); n = int(W[a:b + 1].sum() + Lo[a:b + 1].sum())
            e = float(eW[a:b + 1].sum()); v = float(eV[a:b + 1].sum())
            z = (w - e) / math.sqrt(v + n * e * (1 - e / n) / nc) if (e > 0 and nc > 0 and n > 0) else 0.0
            return dict(win_pct=w * 100.0 / n if n else None, wins=w, n=n,
                        edge_pts=(w - e) * 100.0 / n if (n and e > 0) else None, z=z)
        rows = {"DIV +/-": cell(0, 0), "U / Dn": cell(1, 1), "Early ?": cell(2, 2),
                "Pat BO": cell(3, 3), "Harmonics": cell(5, 14),          # agentic-charts: ported rows
                f"Conf top {p.confMinAlert}%": cell(20, 20), "Conf rest": cell(21, 21),
                "Long": cell(18, 18), "Short": cell(19, 19), "Random": cell(15, 16)}
        fee_r = F["close"] * 2.0 * p.statCostPct / 100.0 / (F["atr"] * F["eff_stat_loss"]) if F["atr"] * F["eff_stat_loss"] > 0 else None
        rows["Exp/Trade"] = dict(signals_R=sR[0] / sR[1] if sR[1] > 0 else None,
                                 random_R=sR[2] / sR[3] if sR[3] > 0 else None, fees_R=fee_r)
        return rows

    # ---------------------------------------------------------------- level odds (f_reach)
    def level_odds(self, level: float, lean: float = 0.0) -> float:
        """Chance (0..1) price reaches `level` within the forecast window, as the script prints
        on the last bar. `lean` = the forecast line's endpoint minus price (0 for a flat line)."""
        F = self.final
        rch = F["rch"]
        close = F["close"]
        sq = F["sig"] * math.sqrt(float(F["H"]))
        up = level >= close
        k = max(level - close - 0.5 * lean if up else close - level + 0.5 * lean, 0.0) / sq if sq > 0 else 0.0
        off = 0 if up else 20
        n = rch[40]
        x = k / 0.25
        j = min(int(math.floor(x)), 19)
        lo = n if j == 0 else rch[off + j - 1]
        hi = rch[off + j]
        if n < 50.0:
            return 2.0 - 2.0 / (1.0 + math.exp(-1.702 * k))
        return hi / n if x >= 20.0 else (lo + (hi - lo) * (x - j)) / n

    def last_forecast(self) -> Optional[dict]:
        """The forecast drawn on the last candle: path, band, range, and odds for active S/R levels."""
        if not self.forecasts:
            return None
        r = self.forecasts[-1]
        lean = r["y"][-1] - r["born"]
        F = self.final
        lv = []
        for dd, name in ((-1, "R"), (1, "S")):
            for x in F["levels"][dd]:
                if x[2] and ((x[0] > r["born"]) if dd < 0 else (x[0] < r["born"])):
                    lv.append((name, float(x[0]), round(self.level_odds(x[0], lean) * 100.0)))
        # Fib ladder (f_fibSwing: highest pivot high / lowest pivot low inside effLookback, last 50 pivots)
        fib = []
        swing = None
        if self.eng.p.showFib:
            hiP = loP = None; hiB = loB = -1
            for (pb, px, ty) in F["piv_hist"]:
                if pb >= r["bar"] - F["eff_lb"]:
                    if ty == 1 and (hiP is None or px > hiP):
                        hiP, hiB = px, pb
                    if ty == -1 and (loP is None or px < loP):
                        loP, loB = px, pb
            if hiP is not None and loP is not None:
                dn = hiB < loB
                swing = dict(high=float(hiP), high_bar=hiB, low=float(loP), low_bar=loB, down=dn)   # agentic-charts
                rngF = hiP - loP
                ratios = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0] + ([1.272, 1.618] if self.eng.p.fibShowExt else [])
                for q in ratios:
                    px = hiP - q * rngF if dn else loP + q * rngF
                    if px > 0:
                        fib.append((q, float(px), round(self.level_odds(px, lean) * 100.0)))
        return dict(bar=r["bar"], born=r["born"], H=r["H"], path=r["y"], band_hi=r["up"], band_lo=r["lo"],
                    final=r["y"][-1], range=(r["lo"][-1], r["up"][-1]), gains=r["gains"], next_candle_tilt=r["tilt"],
                    level_odds=lv, fib_odds=fib, fib_swing=swing)

    def signals_frame(self):
        """All registered signals (not the random controls) as a pandas DataFrame, if pandas is installed."""
        import pandas as pd
        rows = []
        for s in self.signals:
            if s.typ >= 15:
                continue
            rows.append(dict(time=pd.to_datetime(s.time_ms, unit="ms", utc=True), bar=s.bar, type=SIG_NAMES.get(s.typ, s.typ),
                             dir="long" if s.dir > 0 else "short", entry=s.entry, conf_score=s.conf,
                             tier={1: "top", 0: "rest", -1: "warm-up"}[s.tier], tp=s.tp, sl=s.sl, expiry_bars=s.exp,
                             result={0: "open", 1: "win", 2: "loss", 3: "expiry"}[s.result], R=s.r,
                             resolved_bar=s.res_bar, random_win_rate_then=s.p_random))
        return pd.DataFrame(rows)

    # ---------------------------------------------------------------- pretty print
    def print_tables(self):
        def f(x, nd=2):
            if x is None:
                return "-"
            if isinstance(x, float):
                return f"{x:.{nd}f}"
            if isinstance(x, tuple):
                return " / ".join(f(y, nd) for y in x)
            return str(x)
        print(f"PATH VERIFY  (v{__version__} port)")
        for k, x in self.verify_table().items():
            if k == "Next candle / right / n":
                call, pct, n = x
                print(f"  {k:28s} {call} / {('-' if pct is None else f'{pct:.1f}%')} / {n}")
            else:
                print(f"  {k:28s} {f(x)}")
        print("Signal Stats  (win% (W/N), edge vs random entries)")
        for k, x in self.stats_table().items():
            if k == "Exp/Trade":
                print(f"  {k:12s} {f(x['signals_R'])}R | rnd {f(x['random_R'])}R | fees {f(x['fees_R'], 1)}R")
            elif x["n"]:
                e = "" if x["edge_pts"] is None else f" {x['edge_pts']:+.0f}pt (z {x['z']:+.1f})"
                print(f"  {k:12s} {x['win_pct']:.0f}% ({x['wins']}/{x['n']}){e}")
            else:
                print(f"  {k:12s} -")
