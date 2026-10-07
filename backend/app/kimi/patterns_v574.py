"""
Kimi Cooked - Elite Edition v5.7.4 -- chart patterns and harmonic patterns (agentic-charts port).

The two modules the original Python port left out, ported from the script's own functions and run from the
engine's bar loop (kimi_v574.py) at the points where the script runs them:

    pivot history  ->  f_detectChartPatterns (+ f_registerPattern)  ->  S/R ... confluence (w2, w3)
    ->  resolve signals  ->  f_detectHarmonics (lifecycle, then detection + "Harmonics" signals)
    ->  divergence signals  ->  random controls  ->  f_updatePatternLifecycle ("Pat BO" signals)
    ->  forecast (live harmonic TP1s and the last breakout target join the magnet pool)

Chart patterns (pivot structure, same tolerances, gates and order as the script): Triple Top / Bottom,
Head & Shoulders and its inverse, Double Top / Bottom, Ascending / Descending Triangle, Falling / Rising Wedge,
Bull / Bear Flag. A pattern is registered with its break-out line and invalidation line; it breaks out on a
close through the break-out line (volume spike required by default), fails on a close through its invalidation
line, and is deleted when it goes stale. Drawing caps are part of the logic (an evicted pattern stops being
tracked), so they are kept: 5 patterns, 10 break-out targets.

Harmonics (XABCD on the last five alternating pivots, D = the pivot that just confirmed): Gartley, Bat,
Butterfly, Crab, Deep Crab, Alt Bat, Shark, 5-0, Three Drives, AB=CD, best fit wins; PRZ box, TP1/TP2,
PRZ cross-confluence tiers (S/R, golden pocket, divergence at D), invalidation / TP1 / expiry / compromised
lifecycle, 3 sets on the chart.

Constants and names are the script's. Pine's `na` is None here.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

MIN_SHOULDER_OFFSET = 0.3
MIN_CHAN_MOVE = 0.2
FLAG_RETRACE_RATIO = 0.65
NEVER = 2147483000          # "no expiry" bar, as the script writes it
MAX_BO_LABELS = 10          # boLbls cap (boLines holds 2 per break-out, capped at 20)

PATTERN_SHORT = {"Triple Top": "3T", "Head & Shoulders": "H&S", "Double Top": "2T", "Triple Bottom": "3B",
                 "Inv H&S": "iH&S", "Double Bottom": "2B", "Ascending Triangle": "AscT", "Falling Wedge": "FW",
                 "Bull Flag": "BullF", "Descending Triangle": "DscT", "Rising Wedge": "RW", "Bear Flag": "BearF"}

# Harmonic rule tables (hPatNm / hK1..4 / hA1..4 / hB1..4 / hAdTgt). Slots = AB, BC, AD, CD; slot kinds:
# 0 none, 1 hit@harmTol, 2 span@harmTol, 3 hit@effHarmTolD, 4 span@effHarmTolD. Order = the script's priority.
HARM_NAMES = ("Gartley", "Bat", "Butterfly", "Crab", "Deep Crab", "Alt Bat", "Shark", "5-0", "Three Drives", "AB=CD")
HARM_SHORT = ("Gart", "Bat", "Bfly", "Crab", "DCrb", "ABat", "Shrk", "5-0", "3Drv", "ABCD")
_HK = ((1, 2, 1, 2, 1, 1, 2, 2, 2, 0),
       (2, 2, 2, 2, 2, 2, 2, 2, 2, 0),
       (3, 3, 4, 3, 3, 3, 4, 0, 0, 0),
       (4, 4, 4, 4, 4, 4, 4, 3, 4, 0))
_HA = ((0.618, 0.382, 0.786, 0.382, 0.886, 0.382, 0.382, 1.130, 1.272, 0.0),
       (0.382, 0.382, 0.382, 0.382, 0.382, 0.382, 1.130, 1.618, 0.618, 0.0),
       (0.786, 0.886, 1.272, 1.618, 1.618, 1.130, 0.886, 0.0, 1.272, 0.0),
       (1.272, 1.618, 1.618, 2.240, 2.240, 2.000, 1.618, 0.500, 1.272, 0.0))
_HB = ((0.0, 0.500, 0.0, 0.618, 0.0, 0.0, 0.886, 1.618, 1.618, 0.0),
       (0.886, 0.886, 0.886, 0.886, 0.886, 0.886, 1.618, 2.240, 0.786, 0.0),
       (0.0, 0.0, 1.618, 0.0, 0.0, 0.0, 1.130, 0.0, 1.618, 0.0),
       (1.618, 2.618, 2.240, 3.618, 3.618, 3.618, 2.240, 0.0, 1.618, 0.0))
_HAD_TGT = (0.786, 0.886, 1.272, 1.618, 1.618, 1.130, 1.000, None, None, None)
HARM_STATES = {0: "active", 1: "failed", 2: "tp1", 3: "expired", 4: "compromised"}


def _f(x: float) -> float:
    return float(x)


@dataclass(eq=False)
class PatLine:
    x1: int
    y1: float
    x2: int
    y2: float
    width: int = 1
    style: str = "solid"      # solid / dashed / dotted


@dataclass(eq=False)
class ChartPattern:
    name: str
    dir: int                  # +1 bullish, -1 bearish
    bar: int                  # the candle it formed on (patFormBar)
    lines: List[PatLine]
    label_bar: int
    label_y: float
    # registry (f_registerPattern); registered = False when the script never tracked it
    registered: bool = False
    inv_lvl: float = math.nan
    inv_sl: float = 0.0
    brk_a: float = 0.0
    brk_b: float = math.nan
    depth: float = math.nan
    exp_bar: int = NEVER
    state: str = "formed"     # formed (never tracked) / watching / breakout / failed
    end_bar: int = -1         # the candle that broke it out or invalidated it
    breakout: Optional[dict] = None   # {bar, price, target, capped}

    @property
    def short(self) -> str:
        return PATTERN_SHORT[self.name]

    @property
    def text(self) -> str:
        """The chart label as the script leaves it: "2B", "2B ▲" after a break-out, "2B ✕" once invalidated."""
        return self.short + {"failed": " ✕", "breakout": " ▲" if self.dir > 0 else " ▼"}.get(self.state, "")


@dataclass(eq=False)
class HarmonicSet:
    name: str
    best: int                 # index into HARM_NAMES (signal type = 5 + best)
    dir: int
    bar: int                  # the candle D confirmed on
    points: List[Tuple[int, float]]   # X, A, B, C, D as (bar, price)
    inv: float
    tp1: float
    tp2: float
    tp_basis: str             # "A→D" or "C→D"
    prz_high: float
    prz_low: float
    tier: int                 # PRZ cross-confluence tiers
    exp_bar: int
    sr: Optional[float]       # opposing S/R level standing at entry (None = none)
    end_x: int                # bar + harmExtend: where the PRZ box, TP lines and labels end
    state: int = 0            # 0 active, 1 failed, 2 TP1 reached, 3 expired, 4 compromised
    end_bar: int = -1
    box: bool = True          # the PRZ box is deleted on failure / expiry / TP1
    compromised: bool = False # went amber at some point (its label keeps the ⚠)
    ratios: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        """The chart label as the script leaves it: "Gart ▲ ★2", then ⚠ / ✕ / ⋯ / ✓ as its lifecycle goes."""
        out = f"{HARM_SHORT[self.best]} {'▲' if self.dir > 0 else '▼'}" + (f" ★{self.tier}" if self.tier > 0 else "")
        out += " ⚠" if self.compromised else ""
        return out + {1: " ✕", 2: " ✓", 3: " ⋯"}.get(self.state, "")


def _pivots(piv, t: int, n: int):
    """getPivots: the newest n pivots of type t (1 = high, -1 = low), newest first."""
    bars, prices = [], []
    for b, px, ty in reversed(piv):
        if ty == t:
            bars.append(b)
            prices.append(px)
            if len(bars) >= n:
                break
    return bars, prices


def _extreme_between(piv, t: int, b_start: int, b_end: int, find_min: bool):
    """f_extremeBetween: lowest (find_min) / highest pivot of type t strictly between two bars (ties: newest)."""
    val, vbar = None, None
    for b, px, ty in reversed(piv):
        if ty == t and b_start < b < b_end:
            if val is None or (px < val if find_min else px > val):
                val, vbar = px, b
    return val, vbar


def fib_swing(piv, bar: int, lookback: int):
    """f_fibSwing: highest pivot high / lowest pivot low with bar >= bar - lookback (-1 bars when none)."""
    hiP = loP = None
    hiB = loB = -1
    for b, px, ty in piv:
        if b >= bar - lookback:
            if ty == 1 and (hiP is None or px > hiP):
                hiP, hiB = px, b
            if ty == -1 and (loP is None or px < loP):
                loP, loB = px, b
    return hiP, hiB, loP, loB


class ChartPatterns:
    """f_detectChartPatterns + f_registerPattern + f_updatePatternLifecycle, with their drawing registry."""

    def __init__(self, p):
        self.p = p
        self.drawn: List[ChartPattern] = []      # patLbls order (oldest first), capped at patMaxLabels
        self.registry: List[ChartPattern] = []   # patLblRef order: patterns still tracked
        self.bos: List[dict] = []                # break-out target drawings, last MAX_BO_LABELS
        self.last_tops = self.last_bots = self.last_bull_chan = self.last_bear_chan = None
        self.last_tgt: Optional[float] = None    # lastPatTgt / lastPatBar (forecast magnet)
        self.last_tgt_bar: Optional[int] = None

    # ---- effective parameters (script: effPatTol, effPatMinDepth, effFlagPole, effPatBoBuf)
    def _tol(self, vol_reg):
        p = self.p
        return p.patTolATR * max(0.5, min(2.0, math.sqrt(max(vol_reg, 0.1)))) if p.autoPatTol else p.patTolATR

    def _min_depth(self, vol_reg, tf_scale):
        p = self.p
        return p.patMinDepthATR * max(0.5, tf_scale) * max(0.5, vol_reg) if p.autoParams else p.patMinDepthATR

    def _pole(self, vol_reg):
        p = self.p
        return p.flagPoleATR * max(0.5, vol_reg) if p.autoParams else p.flagPoleATR

    def _bo_buf(self, vol_reg):
        p = self.p
        return p.patBoBufATR * max(0.5, min(2.0, vol_reg)) if p.autoPatBoBuf else p.patBoBufATR

    @staticmethod
    def born_bad(i: int, close: float, d: int, brk_a: float, brk_b: float) -> bool:
        """f_bornBad: the break-out line is already at/below 1% of price, or price already closed through it."""
        brk_now = brk_a * i + brk_b
        return math.isnan(brk_now) or brk_now <= close * 0.01 or (close > brk_now if d > 0 else close < brk_now)

    @staticmethod
    def ext_y(anchor_a: float, anchor_b: float, ext: float, atr: float, close: float) -> float:
        """f_extY: clamp a projected line end so a steep line can't run off the price scale."""
        lo, hi = min(anchor_a, anchor_b), max(anchor_a, anchor_b)
        pad = max(hi - lo, (close * 0.01 if math.isnan(atr) else atr) * 3.0)
        y_min = max(lo - pad, min(lo, close) * 0.5)
        return min(max(ext, y_min), hi + pad)

    def _draw(self, name, d, i, lines, label_bar, label_y) -> ChartPattern:
        pat = ChartPattern(name=name, dir=d, bar=i, lines=lines, label_bar=int(label_bar), label_y=_f(label_y))
        self.drawn.append(pat)
        return pat

    def _delete(self, pat: ChartPattern):
        """f_deletePatternDrawings + f_unregisterPattern."""
        if pat in self.drawn:
            self.drawn.remove(pat)
        if pat in self.registry:
            self.registry.remove(pat)

    def _register(self, pat, i, close, atr, d, inv_lvl, inv_sl, brk_a, brk_b, dep):
        """f_registerPattern: track it for break-out / failure unless it is born broken; supersede a duplicate."""
        p = self.p
        if not (p.patBreakout or p.patFailClean) or self.born_bad(i, close, d, brk_a, brk_b):
            return
        if p.patDupATR > 0 and not math.isnan(atr):
            for old in reversed(list(self.registry)):
                if old.dir == d and old.name == pat.name and \
                        abs(old.inv_lvl + old.inv_sl * (i - old.bar) - inv_lvl) <= atr * p.patDupATR:
                    self._delete(old)
        width = max(i - pat.lines[0].x1, 1)
        pat.exp_bar = NEVER if p.patExpMult <= 0 else i + int(math.floor(max(width * p.patExpMult, p.patExtend) + 0.5))
        pat.inv_lvl, pat.inv_sl, pat.brk_a, pat.brk_b, pat.depth = inv_lvl, inv_sl, brk_a, brk_b, dep
        pat.registered, pat.state = True, "watching"
        self.registry.append(pat)

    # ------------------------------------------------------------------ detection
    def detect(self, i: int, close: float, atr: float, vol_reg: float, tf_scale: float, plen: int, eff_lb: int,
               piv, pivot_now: bool, pause: bool) -> Tuple[bool, bool]:
        """f_detectChartPatterns on a closed candle; returns (chartPatBullNow, chartPatBearNow)."""
        p = self.p
        bull = bear = False
        if not (p.showChartPatterns and not math.isnan(atr) and pivot_now and not pause):
            return bull, bear
        L = PatLine
        bad = lambda d, a, b: self.born_bad(i, close, d, a, b)          # noqa: E731
        ext = lambda a, b, y: self.ext_y(a, b, y, atr, close)            # noqa: E731
        tol = atr * self._tol(vol_reg)
        depth = atr * self._min_depth(vol_reg, tf_scale)
        hb, hp = _pivots(piv, 1, 3)
        lb, lp = _pivots(piv, -1, 4)
        nH, nL = len(hb), len(lb)
        endX = i + p.patExtend
        new_high_bar = hb[0] if nH > 0 else -1
        new_low_bar = lb[0] if nL > 0 else -1
        padY = atr * 0.3

        # ---- tops: Triple Top, Head & Shoulders, then Double Top
        if nH >= 2 and new_high_bar != (self.last_tops if self.last_tops is not None else -1):
            drew = False
            if nH >= 3:
                b3, b2, b1 = hb[0], hb[1], hb[2]
                H3, H2, H1 = hp[0], hp[1], hp[2]
                trA, tbA = _extreme_between(piv, -1, b1, b2, True)
                trB, tbB = _extreme_between(piv, -1, b2, b3, True)
                top_max, top_min = max(H1, H2, H3), min(H1, H2, H3)
                both = trA is not None and trB is not None
                nsl = (trB - trA) / (tbB - tbA) if both else math.nan
                if both and top_max - top_min <= tol and b2 - b1 >= plen and b3 - b2 >= plen and b2 - b1 <= eff_lb \
                        and b3 - b2 <= eff_lb and top_min - min(trA, trB) >= depth and not bad(-1, 0.0, min(trA, trB)):
                    topY, neckY = (H1 + H2 + H3) / 3.0, min(trA, trB)
                    pat = self._draw("Triple Top", -1, i, [
                        L(b1, H1, tbA, trA), L(tbA, trA, b2, H2), L(b2, H2, tbB, trB), L(tbB, trB, b3, H3),
                        L(b1, topY, endX, topY, 2), L(tbA, neckY, endX, neckY, 1, "dashed")],
                        math.floor((b1 + b3) / 2.0), top_max + padY)
                    self._register(pat, i, close, atr, -1, top_max, 0.0, 0.0, neckY, top_max - neckY)
                    drew = bear = True
                elif both and H2 > H1 + atr * MIN_SHOULDER_OFFSET and H2 > H3 + atr * MIN_SHOULDER_OFFSET \
                        and abs(H1 - H3) <= tol and top_min - min(trA, trB) >= depth and b2 - b1 <= eff_lb \
                        and b3 - b2 <= eff_lb and abs(nsl) <= atr and not bad(-1, nsl, trA - nsl * tbA):
                    # the CLAMPED neckline's implied slope is what gets registered (v5.2.5 P2-19)
                    nk_end = ext(trA, trB, trB + nsl * (endX - tbB))
                    reg_nsl = (nk_end - trA) / (endX - tbA)
                    pat = self._draw("Head & Shoulders", -1, i, [
                        L(b1, H1, tbA, trA), L(tbA, trA, b2, H2), L(b2, H2, tbB, trB), L(tbB, trB, b3, H3),
                        L(tbA, trA, endX, nk_end, 2)], b2, H2 + padY)
                    self._register(pat, i, close, atr, -1, H2, 0.0, reg_nsl, trA - reg_nsl * tbA,
                                   H2 - (reg_nsl * b2 + trA - reg_nsl * tbA))
                    drew = bear = True
            if not drew:
                b2, b1 = hb[0], hb[1]
                H2, H1 = hp[0], hp[1]
                tr, trb = _extreme_between(piv, -1, b1, b2, True)
                if tr is not None and abs(H1 - H2) <= tol and b2 - b1 >= plen * 2 and b2 - b1 <= eff_lb \
                        and min(H1, H2) - tr >= depth and not bad(-1, 0.0, tr):
                    topY = (H1 + H2) / 2.0
                    pat = self._draw("Double Top", -1, i, [
                        L(b1, H1, trb, tr), L(trb, tr, b2, H2), L(b1, topY, endX, topY, 2),
                        L(trb, tr, endX, tr, 1, "dashed")], math.floor((b1 + b2) / 2.0), max(H1, H2) + padY)
                    self._register(pat, i, close, atr, -1, max(H1, H2), 0.0, 0.0, tr, max(H1, H2) - tr)
                    drew = bear = True
            if drew:
                self.last_tops = new_high_bar

        # ---- bottoms: Triple Bottom, Inverse H&S, then Double Bottom
        if nL >= 2 and new_low_bar != (self.last_bots if self.last_bots is not None else -1):
            drew = False
            if nL >= 3:
                b3, b2, b1 = lb[0], lb[1], lb[2]
                L3, L2, L1 = lp[0], lp[1], lp[2]
                pkA, pbA = _extreme_between(piv, 1, b1, b2, False)
                pkB, pbB = _extreme_between(piv, 1, b2, b3, False)
                bot_max, bot_min = max(L1, L2, L3), min(L1, L2, L3)
                both = pkA is not None and pkB is not None
                nsl = (pkB - pkA) / (pbB - pbA) if both else math.nan
                if both and bot_max - bot_min <= tol and b2 - b1 >= plen and b3 - b2 >= plen and b2 - b1 <= eff_lb \
                        and b3 - b2 <= eff_lb and max(pkA, pkB) - bot_max >= depth and not bad(1, 0.0, max(pkA, pkB)):
                    botY, neckY = (L1 + L2 + L3) / 3.0, max(pkA, pkB)
                    pat = self._draw("Triple Bottom", 1, i, [
                        L(b1, L1, pbA, pkA), L(pbA, pkA, b2, L2), L(b2, L2, pbB, pkB), L(pbB, pkB, b3, L3),
                        L(b1, botY, endX, botY, 2), L(pbA, neckY, endX, neckY, 1, "dashed")],
                        math.floor((b1 + b3) / 2.0), bot_min - padY)
                    self._register(pat, i, close, atr, 1, bot_min, 0.0, 0.0, neckY, neckY - bot_min)
                    drew = bull = True
                elif both and L2 < L1 - atr * MIN_SHOULDER_OFFSET and L2 < L3 - atr * MIN_SHOULDER_OFFSET \
                        and abs(L1 - L3) <= tol and max(pkA, pkB) - bot_max >= depth and b2 - b1 <= eff_lb \
                        and b3 - b2 <= eff_lb and abs(nsl) <= atr and not bad(1, nsl, pkA - nsl * pbA):
                    nk_end = ext(pkA, pkB, pkB + nsl * (endX - pbB))
                    reg_nsl = (nk_end - pkA) / (endX - pbA)
                    pat = self._draw("Inv H&S", 1, i, [
                        L(b1, L1, pbA, pkA), L(pbA, pkA, b2, L2), L(b2, L2, pbB, pkB), L(pbB, pkB, b3, L3),
                        L(pbA, pkA, endX, nk_end, 2)], b2, L2 - padY)
                    self._register(pat, i, close, atr, 1, L2, 0.0, reg_nsl, pkA - reg_nsl * pbA,
                                   (reg_nsl * b2 + pkA - reg_nsl * pbA) - L2)
                    drew = bull = True
            if not drew:
                b2, b1 = lb[0], lb[1]
                L2, L1 = lp[0], lp[1]
                pk, pkb = _extreme_between(piv, 1, b1, b2, False)
                if pk is not None and abs(L1 - L2) <= tol and b2 - b1 >= plen * 2 and b2 - b1 <= eff_lb \
                        and pk - max(L1, L2) >= depth and not bad(1, 0.0, pk):
                    botY = (L1 + L2) / 2.0
                    pat = self._draw("Double Bottom", 1, i, [
                        L(b1, L1, pkb, pk), L(pkb, pk, b2, L2), L(b1, botY, endX, botY, 2),
                        L(pkb, pk, endX, pk, 1, "dashed")], math.floor((b1 + b2) / 2.0), min(L1, L2) - padY)
                    self._register(pat, i, close, atr, 1, min(L1, L2), 0.0, 0.0, pk, pk - min(L1, L2))
                    drew = bull = True
            if drew:
                self.last_bots = new_low_bar

        # ---- channels: triangles, wedges, flags (last two highs + last two lows, interleaved)
        if nH >= 2 and nL >= 2:
            bH2, bH1, H2, H1 = hb[0], hb[1], hp[0], hp[1]
            bL2, bL1, L2, L1 = lb[0], lb[1], lp[0], lp[1]
            if bH2 - bH1 >= plen and bL2 - bL1 >= plen and H1 > L1 and H2 > L2 and min(bH2, bL2) > max(bH1, bL1):
                sH = (H2 - H1) / (bH2 - bH1)
                sL = (L2 - L1) / (bL2 - bL1)
                chan_new = max(bH2, bH1, bL2, bL1)
                lbl_bar = math.floor((bH1 + bL2) / 2.0)
                apex_ahead = sH != sL and (L1 - sL * bL1 - H1 + sH * bH1) / (sH - sL) > i
                last_bull = self.last_bull_chan if self.last_bull_chan is not None else -1
                last_bear = self.last_bear_chan if self.last_bear_chan is not None else -1

                if 0.0 <= H1 - H2 <= atr * MIN_CHAN_MOVE and sL > 0 and L2 - L1 > atr * MIN_CHAN_MOVE \
                        and chan_new != last_bull and not bad(1, 0.0, (H1 + H2) / 2.0):
                    topY = (H1 + H2) / 2.0
                    pat = self._draw("Ascending Triangle", 1, i, [
                        L(bH1, topY, endX, topY, 2), L(bL1, L1, endX, ext(L1, L2, L2 + sL * (endX - bL2)), 2)],
                        lbl_bar, min(L1, L2) - padY)
                    self._register(pat, i, close, atr, 1, L2, 0.0, 0.0, topY, topY - min(L1, L2))
                    self.last_bull_chan = chan_new
                    bull = True
                elif sH < 0 and sL < 0 and sH < sL and H1 - L1 >= depth and H2 - L2 >= atr * MIN_CHAN_MOVE \
                        and apex_ahead and chan_new != last_bull and not bad(1, sH, H1 - sH * bH1):
                    hi_end = ext(H1, H2, H2 + sH * (endX - bH2))
                    hi_sl = (hi_end - H1) / (endX - bH1)
                    lo_end = ext(L1, L2, L2 + sL * (endX - bL2))
                    lo_sl = (lo_end - L1) / (endX - bL1)
                    pat = self._draw("Falling Wedge", 1, i, [L(bH1, H1, endX, hi_end, 2), L(bL1, L1, endX, lo_end, 2)],
                                     lbl_bar, min(L1, L2) - padY)
                    self._register(pat, i, close, atr, 1, L1 + lo_sl * (i - bL1), lo_sl, hi_sl, H1 - hi_sl * bH1, H1 - L1)
                    self.last_bull_chan = chan_new
                    bull = True
                elif sH < 0 and sL < 0 and sH >= sL and bL1 > bH1 and chan_new != last_bull:
                    # pole anchor = the lowest retained low before bH1 (v5.2.5 P2-24)
                    pole_l, pole_b = None, None
                    for b, px in zip(lb, lp):
                        if b < bH1 and (pole_l is None or px < pole_l):
                            pole_b, pole_l = b, px
                    if pole_l is not None and H1 - pole_l >= atr * self._pole(vol_reg) \
                            and H1 - L2 <= FLAG_RETRACE_RATIO * (H1 - pole_l) and H1 - L2 >= 0.0 \
                            and not bad(1, sH, H1 - sH * bH1):
                        hi_end = ext(H1, H2, H2 + sH * (endX - bH2))
                        hi_sl = (hi_end - H1) / (endX - bH1)
                        lo_end = ext(L1, L2, L2 + sL * (endX - bL2))
                        lo_sl = (lo_end - L1) / (endX - bL1)
                        pat = self._draw("Bull Flag", 1, i, [
                            L(pole_b, pole_l, bH1, H1, 2), L(bH1, H1, endX, hi_end), L(bL1, L1, endX, lo_end)],
                            lbl_bar, min(L1, L2) - padY)
                        self._register(pat, i, close, atr, 1, L1 + lo_sl * (i - bL1), lo_sl, hi_sl, H1 - hi_sl * bH1,
                                       H1 - pole_l)
                        self.last_bull_chan = chan_new
                        bull = True

                if 0.0 <= L1 - L2 <= atr * MIN_CHAN_MOVE and sH < 0 and H1 - H2 > atr * MIN_CHAN_MOVE \
                        and chan_new != last_bear and not bad(-1, 0.0, (L1 + L2) / 2.0):
                    botY = (L1 + L2) / 2.0
                    pat = self._draw("Descending Triangle", -1, i, [
                        L(bL1, botY, endX, botY, 2), L(bH1, H1, endX, ext(H1, H2, H2 + sH * (endX - bH2)), 2)],
                        lbl_bar, max(H1, H2) + padY)
                    self._register(pat, i, close, atr, -1, H2, 0.0, 0.0, botY, max(H1, H2) - botY)
                    self.last_bear_chan = chan_new
                    bear = True
                elif sH > 0 and sL > 0 and sL > sH and H1 - L1 >= depth and H2 - L2 >= atr * MIN_CHAN_MOVE \
                        and apex_ahead and chan_new != last_bear and not bad(-1, sL, L1 - sL * bL1):
                    hi_end = ext(H1, H2, H2 + sH * (endX - bH2))
                    hi_sl = (hi_end - H1) / (endX - bH1)
                    lo_end = ext(L1, L2, L2 + sL * (endX - bL2))
                    lo_sl = (lo_end - L1) / (endX - bL1)
                    pat = self._draw("Rising Wedge", -1, i, [L(bH1, H1, endX, hi_end, 2), L(bL1, L1, endX, lo_end, 2)],
                                     lbl_bar, max(H1, H2) + padY)
                    self._register(pat, i, close, atr, -1, H1 + hi_sl * (i - bH1), hi_sl, lo_sl, L1 - lo_sl * bL1, H1 - L1)
                    self.last_bear_chan = chan_new
                    bear = True
                elif sH > 0 and sL > 0 and sH >= sL and bH1 > bL1 and chan_new != last_bear:
                    pole_h, pole_b = None, None
                    for b, px in zip(hb, hp):
                        if b < bL1 and (pole_h is None or px > pole_h):
                            pole_b, pole_h = b, px
                    if pole_h is not None and pole_h - L1 >= atr * self._pole(vol_reg) \
                            and H2 - L1 <= FLAG_RETRACE_RATIO * (pole_h - L1) and H2 - L1 >= 0.0 \
                            and not bad(-1, sL, L1 - sL * bL1):
                        hi_end = ext(H1, H2, H2 + sH * (endX - bH2))
                        hi_sl = (hi_end - H1) / (endX - bH1)
                        lo_end = ext(L1, L2, L2 + sL * (endX - bL2))
                        lo_sl = (lo_end - L1) / (endX - bL1)
                        pat = self._draw("Bear Flag", -1, i, [
                            L(pole_b, pole_h, bL1, L1, 2), L(bH1, H1, endX, hi_end), L(bL1, L1, endX, lo_end)],
                            lbl_bar, max(H1, H2) + padY)
                        self._register(pat, i, close, atr, -1, H1 + hi_sl * (i - bH1), hi_sl, lo_sl, L1 - lo_sl * bL1,
                                       pole_h - L1)
                        self.last_bear_chan = chan_new
                        bear = True

        # "Max Patterns On Chart": the oldest drawing goes, and stops being tracked
        while len(self.drawn) > p.patMaxLabels:
            old = self.drawn.pop(0)
            if old in self.registry:
                self.registry.remove(old)
        return bull, bear

    # ------------------------------------------------------------------ lifecycle
    def lifecycle(self, i: int, close: float, prev_close: Optional[float], atr: float, vol_reg: float,
                  vol_spike: bool, pause: bool, ranging: bool, sess_ok: bool) -> List[ChartPattern]:
        """f_updatePatternLifecycle on a closed candle: stale -> deleted, closed through the invalidation line ->
        failed, closed through the break-out line (on a volume spike) -> break-out. Returns the break-outs that
        register a "Pat BO" signal (newest registry entry first, as the script loops)."""
        p = self.p
        signals: List[ChartPattern] = []
        if (p.patBreakout or p.patFailClean) and self.registry and not math.isnan(atr):
            buf = atr * self._bo_buf(vol_reg)
            for pat in reversed(list(self.registry)):
                d = pat.dir
                brk = pat.brk_a * i + pat.brk_b
                inv = pat.inv_lvl + pat.inv_sl * (i - pat.bar)
                stale = i > pat.exp_bar or math.isnan(brk) or brk <= close * 0.01
                failed = close < inv if d > 0 else close > inv
                brk_prev = pat.brk_a * (i - 1) + pat.brk_b
                crossed = prev_close is not None and (close > brk + buf and prev_close <= brk_prev + buf if d > 0
                                                      else close < brk - buf and prev_close >= brk_prev - buf)
                broke = crossed and (not p.patBoVolReq or vol_spike)
                if stale:
                    self._delete(pat)
                elif failed and p.patFailClean:
                    pat.state, pat.end_bar = "failed", i
                    self.registry.remove(pat)
                elif broke and p.patBreakout and not pause and not ranging:
                    dep_use = min(pat.depth, brk * p.patMaxTgtPct / 100.0)
                    tgt = max(brk + d * dep_use, brk * 0.05)
                    pat.state, pat.end_bar = "breakout", i
                    pat.breakout = dict(bar=i, price=float(brk), target=float(tgt), capped=dep_use < pat.depth)
                    self.bos.append(dict(name=pat.name, dir=d, bar=i, price=float(brk), target=float(tgt),
                                         end_bar=i + p.patExtend))
                    if sess_ok:
                        signals.append(pat)
                        self.last_tgt, self.last_tgt_bar = float(tgt), i
                    self.registry.remove(pat)
        while len(self.bos) > MAX_BO_LABELS:
            self.bos.pop(0)
        return signals


class Harmonics:
    """f_detectHarmonics: the lifecycle pass over the sets on the chart, then detection on pivot candles."""

    def __init__(self, p):
        self.p = p
        self.sets: List[HarmonicSet] = []        # harmSet* arrays, oldest first, capped at harmMaxSets
        self.last_d_bar: Optional[int] = None    # lastHarmDBar

    def lifecycle(self, i: int, close: float, high: float, low: float, atr: float, eff_mitbuf: float):
        p = self.p
        for s in self.sets:
            if s.state not in (0, 4):
                continue
            failed = close < s.inv if s.dir > 0 else close > s.inv
            hit = high >= s.tp1 if s.dir > 0 else low <= s.tp1
            expired = i > s.exp_bar
            if failed or expired:
                s.state, s.end_bar, s.box = (1 if failed else 3), i, False
            elif hit:
                s.state, s.end_bar, s.box = 2, i, False
            elif s.state == 0 and p.useCrossInv and s.sr is not None:
                # f_opposingSRBroken: the S/R level standing at entry broke the wrong way
                buf = (0.0 if math.isnan(atr) else atr) * eff_mitbuf
                if (close < s.sr - buf) if s.dir > 0 else (close > s.sr + buf):
                    s.state, s.compromised = 4, True

    def detect(self, i: int, plen: int, conf_lo, conf_hi, piv, atr: float, pause: bool,
               nearest_sup: Optional[float], nearest_res: Optional[float], eff_zone: float, eff_mitbuf: float,
               eff_lb: int, div_at: dict) -> List[HarmonicSet]:
        """Completions whose D is the pivot that just confirmed. `div_at[d]` = a regular, hidden or early
        divergence fired this candle on that side (the 4th PRZ tier). Returns the new sets, oldest first."""
        p = self.p
        new: List[HarmonicSet] = []
        if not (p.showHarmonics and not pause and not math.isnan(atr) and (conf_lo is not None or conf_hi is not None)):
            return new
        n = len(piv)
        if n < 5:
            return new
        pre_d = self.last_d_bar if self.last_d_bar is not None else -1   # freshD keys on the pre-bar latch
        enabled = (p.harmGartley, p.harmBat, p.harmBfly, p.harmCrab, p.harmDeepCrab, p.harmAltBat, p.harmShark,
                   p.harmFiveZero, p.harm3Drives, p.harmABCD)
        tol_d = p.harmTol if p.harmTolD <= 0 else p.harmTolD
        for d_run in range(2 if (conf_lo is not None and conf_hi is not None) else 1):
            # newest-first alternating 5-pivot walk, strictly older bars; abort when a skipped same-type
            # pivot is more extreme than the one kept (v5.2.5 P0-5)
            pick: List[int] = []
            prev_t, prev_b = 0, 0
            for k in range(n - 1 - d_run, -1, -1):
                b, px, t = piv[k]
                if not pick:
                    pick.append(k)
                    prev_t, prev_b = t, b
                elif t != prev_t and b < prev_b:
                    pick.append(k)
                    prev_t, prev_b = t, b
                elif b < prev_b:
                    kept = piv[pick[-1]][1]
                    if (prev_t == -1 and px < kept) or (prev_t == 1 and px > kept):
                        pick = []
                        break
                if len(pick) >= 5:
                    break
            if len(pick) < 5:
                continue
            (bD, pD, tD), (bC, pC, _), (bB, pB, _), (bA, pA, _), (bX, pX, _) = (piv[k] for k in pick[:5])
            if not (bD == i - plen and bD != pre_d):
                continue
            self.last_d_bar = bD
            s = 1 if tD == -1 else -1                      # +1 = bullish completion (D is a pivot low)
            legXA, legAB, legBC, legCD, legAD = s * (pA - pX), s * (pA - pB), s * (pC - pB), s * (pC - pD), s * (pA - pD)
            if not (legAB > 0 and legBC > 0 and legCD > 0 and (legXA >= atr * p.harmMinXA or enabled[9])):
                continue
            rAB = legAB / legXA if legXA != 0 else math.nan
            rBC = legBC / legAB
            rCD = legCD / legBC
            rAD = legAD / legXA if legXA != 0 else math.nan
            ratios = (rAB, rBC, rAD, rCD)
            best, best_dev = -1, 999.0
            for q in range(10):
                if not enabled[q]:
                    continue
                ok, dev, cnt = True, 0.0, 0
                for slot in range(4):
                    kind = _HK[slot][q]
                    if kind > 0:
                        r, a, b = ratios[slot], _HA[slot][q], _HB[slot][q]
                        tol = tol_d if kind >= 3 else p.harmTol
                        if kind in (1, 3):
                            ok = ok and abs(r - a) <= tol
                            dev += abs(r - a)
                        else:
                            ok = ok and (a - tol <= r <= b + tol)
                            dev += abs(r - (a + b) / 2.0)
                        cnt += 1
                if q == 9:
                    # AB=CD: BC 0.382-0.886 of AB, CD = AB x 1.0 / 1.272 / 1.618 at 2x the D tolerance
                    tA, tB, tC = 1.0 / rBC, 1.272 / rBC, 1.618 / rBC
                    dA, dB, dC = abs(rCD - tA), abs(rCD - tB), abs(rCD - tC)
                    tgt = tA if (dA <= dB and dA <= dC) else tB if dB <= dC else tC
                    ok = (0.382 - p.harmTol <= rBC <= 0.886 + p.harmTol) and \
                        (dA <= tol_d * 2 or dB <= tol_d * 2 or dC <= tol_d * 2)
                    dev = abs(rBC - (0.382 + 0.886) / 2.0) + abs(rCD - tgt)
                    cnt = 2
                if q != 9:
                    ok = ok and legXA >= atr * p.harmMinXA and (q == 7 or legAD > 0)
                else:
                    ok = ok and legAD > 0
                if ok and cnt > 0:
                    dq = (dev / cnt) * math.sqrt(4.0 / cnt)
                    if dq < best_dev:
                        best, best_dev = q, dq
            if best < 0:
                continue
            ad_tgt = _HAD_TGT[best] if _HAD_TGT[best] is not None else rAD
            ext_pat = best in (2, 3, 4, 5)                # complete beyond X: invalid past D with an ATR buffer
            inv = (pD - atr * eff_mitbuf if s > 0 else pD + atr * eff_mitbuf) if ext_pat else \
                (min(pX, pD) if s > 0 else max(pX, pD))
            tp_leg = legAD if legAD > 0 else legCD        # deep-C 5-0: project off C->D (v5.2.6 M-1)
            ideal_d = pA - s * ad_tgt * legXA
            if math.isnan(ideal_d):
                ideal_d = pD
            tier = 0
            if p.przConfluence:
                win = atr * eff_zone
                if (nearest_sup is not None and abs(pD - nearest_sup) <= win) or \
                        (nearest_res is not None and abs(pD - nearest_res) <= win):
                    tier += 1
                if p.showFib and p.fibShowPocket:
                    hiP, hiB, loP, loB = fib_swing(piv, i, eff_lb)
                    if hiB != -1 and loB != -1:
                        dn, rng = hiB < loB, hiP - loP
                        pk_a = hiP - 0.618 * rng if dn else loP + 0.618 * rng
                        pk_b = hiP - 0.650 * rng if dn else loP + 0.650 * rng
                        if min(pk_a, pk_b) <= pD <= max(pk_a, pk_b):
                            tier += 1
                if p.przDivConfirm and div_at.get(s):
                    tier += 1
            hs = HarmonicSet(name=HARM_NAMES[best], best=best, dir=s, bar=i,
                             points=[(bX, _f(pX)), (bA, _f(pA)), (bB, _f(pB)), (bC, _f(pC)), (bD, _f(pD))],
                             inv=_f(inv), tp1=_f(pD + s * 0.382 * tp_leg), tp2=_f(pD + s * 0.618 * tp_leg),
                             tp_basis="A→D" if legAD > 0 else "C→D",
                             prz_high=_f(max(pD, ideal_d) + atr * 0.1), prz_low=_f(min(pD, ideal_d) - atr * 0.1),
                             tier=tier, exp_bar=NEVER if p.harmExpBars <= 0 else i + p.harmExpBars,
                             sr=nearest_sup if s > 0 else nearest_res, end_x=i + p.harmExtend,
                             box=p.harmShowPRZ, ratios=dict(AB=rAB, BC=rBC, CD=rCD, AD=rAD))
            self.sets.append(hs)
            while len(self.sets) > p.harmMaxSets:
                self.sets.pop(0)
            new.append(hs)
        return new
