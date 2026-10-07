"""Kimi Cooked v5.7.4: the ported chart-pattern, harmonic and session-filter modules, on constructed pivots and
candles (the script's own rules: tolerances, spacing, born-broken gate, break-out / failure lines, best-fit
harmonic ratios, PRZ tiers and lifecycle), and what the service and the agent make of them."""

import csv
import gzip
import os
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("DATA_SOURCE", "synthetic")
os.environ.setdefault("LLM_PROVIDER", "none")

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from app.kimi import Inputs, KimiCooked  # noqa: E402
from app.kimi.patterns_v574 import ChartPatterns, Harmonics  # noqa: E402
from app.kimi.sessions_v574 import in_session, session_arrays  # noqa: E402
from app.kimi_service import LABELS, TYPE_NAMES, compute, summarize  # noqa: E402
from app.schemas import Candle  # noqa: E402
from app.ta_agent import _kimi_lines  # noqa: E402

SAMPLE = Path(__file__).parent / "data" / "kimi_BTCUSDT_1h.csv.gz"
K = 300.0   # mirror axis: price -> K - price (and low <-> high) turns a bullish setup into its bearish twin
PLEN = 5


def mirror(piv):
    return [(b, K - px, -t) for b, px, t in piv]


def detect(piv, close, p=None, atr=1.0, eff_lb=100, i=None, cp=None, pause=False):
    """One f_detectChartPatterns call on the candle that confirms the newest pivot (vol regime 1, 4h scale)."""
    cp = cp or ChartPatterns(p or Inputs())
    i = piv[-1][0] + PLEN if i is None else i
    cp.detect(i, close, atr, 1.0, 1.0, PLEN, eff_lb, piv, True, pause)
    return cp


# ------------------------------------------------------------------ chart patterns: detection
# (name(s) drawn, pivots as (bar, price, type: -1 low / 1 high), close on the confirming candle)
BULLISH = [
    ({"Double Bottom"}, [(10, 100.0, -1), (20, 105.0, 1), (30, 100.2, -1)], 102.0),
    ({"Triple Bottom"}, [(10, 100.0, -1), (15, 105.0, 1), (20, 100.3, -1), (25, 106.5, 1), (30, 100.1, -1)], 103.0),
    ({"Inv H&S"}, [(10, 100.0, -1), (15, 105.0, 1), (20, 95.0, -1), (25, 106.2, 1), (30, 100.4, -1)], 102.5),
    # a flat top over rising lows; the two equal highs are a double top as well, and the script draws both
    ({"Ascending Triangle", "Double Top"}, [(10, 110.0, 1), (15, 100.0, -1), (25, 110.0, 1), (30, 104.0, -1)], 107.0),
    ({"Falling Wedge"}, [(10, 120.0, 1), (15, 100.0, -1), (25, 110.0, 1), (30, 98.0, -1)], 101.0),
    ({"Bull Flag"}, [(2, 90.0, -1), (10, 110.0, 1), (15, 104.0, -1), (25, 109.0, 1), (30, 103.0, -1)], 106.0),
]
TWIN = {"Double Bottom": "Double Top", "Triple Bottom": "Triple Top", "Inv H&S": "Head & Shoulders",
        "Ascending Triangle": "Descending Triangle", "Falling Wedge": "Rising Wedge", "Bull Flag": "Bear Flag",
        "Double Top": "Double Bottom"}


@pytest.mark.parametrize("names,piv,close", BULLISH, ids=[sorted(n)[0] for n, _, _ in BULLISH])
def test_chart_patterns_and_their_bearish_twins(names, piv, close):
    cp = detect(piv, close)
    assert {x.name for x in cp.drawn} == names
    main = next(x for x in cp.drawn if x.name in TWIN and x.name not in ("Double Top",) and x.dir > 0)
    assert main.state == "watching" and main.registered and len(main.lines) >= 2
    tw = detect(mirror(piv), K - close)
    assert {x.name for x in tw.drawn} == {TWIN[n] for n in names}
    twin = next(x for x in tw.drawn if x.name == TWIN[main.name])
    assert twin.dir == -1 and twin.state == "watching"
    # the twin's break-out and invalidation lines are the mirror image of the original's
    assert twin.brk_a * 40 + twin.brk_b == pytest.approx(K - (main.brk_a * 40 + main.brk_b))
    assert twin.inv_lvl == pytest.approx(K - main.inv_lvl) and twin.depth == pytest.approx(main.depth)


def test_double_bottom_registry_and_label():
    cp = detect([(10, 100.0, -1), (20, 105.0, 1), (30, 100.2, -1)], 102.0)
    (db,) = cp.drawn
    # invalidation = the lower bottom (flat), break-out = the peak between (flat), depth = peak - bottom
    assert (db.inv_lvl, db.inv_sl, db.brk_a, db.brk_b, db.depth) == (100.0, 0.0, 0.0, 105.0, pytest.approx(5.0))
    assert db.label_bar == 20 and db.label_y == pytest.approx(100.0 - 0.3)     # below the lows by 0.3 ATR
    assert db.text == "2B" and db.lines[2].x2 == 35 + 10                      # lines run patExtend bars ahead
    assert db.exp_bar == 35 + 3 * (35 - 10)                                   # 3 x the formation width


@pytest.mark.parametrize("piv,close,why", [
    ([(10, 100.0, -1), (20, 105.0, 1), (30, 101.0, -1)], 102.0, "bottoms 1 ATR apart > 0.75 ATR tolerance"),
    ([(10, 100.0, -1), (15, 105.0, 1), (19, 100.2, -1)], 102.0, "closer than 2 pivot lengths"),
    ([(10, 100.0, -1), (20, 100.8, 1), (30, 100.2, -1)], 100.5, "shallower than the min depth (1 ATR)"),
    ([(10, 100.0, -1), (20, 105.0, 1), (30, 100.2, -1)], 105.5, "born broken: already closed above the neckline"),
    ([(10, 100.0, -1), (20, 105.0, 1), (130, 100.2, -1)], 102.0, "wider than effLookback"),
])
def test_double_bottom_gates(piv, close, why):
    assert not detect(piv, close).drawn, why


def test_no_patterns_while_entries_are_paused_or_switched_off():
    piv = [(10, 100.0, -1), (20, 105.0, 1), (30, 100.2, -1)]
    assert not detect(piv, 102.0, pause=True).drawn
    assert not detect(piv, 102.0, p=Inputs(showChartPatterns=False)).drawn


def test_head_and_shoulders_registers_the_clamped_sloped_neckline():
    piv = mirror([(10, 100.0, -1), (15, 105.0, 1), (20, 95.0, -1), (25, 106.2, 1), (30, 100.4, -1)])
    (hs,) = [x for x in detect(piv, K - 102.5).drawn if x.name == "Head & Shoulders"]
    nsl = ((K - 106.2) - (K - 105.0)) / 10
    assert hs.brk_a == pytest.approx(nsl) and hs.brk_b == pytest.approx((K - 105.0) - nsl * 15)
    assert hs.inv_lvl == K - 95.0 and hs.label_bar == 20 and hs.text == "H&S"


# ------------------------------------------------------------------ chart patterns: lifecycle
def life(cp, i, close, prev, spike=True, ranging=False, sess_ok=True):
    return cp.lifecycle(i, close, prev, 1.0, 1.0, spike, False, ranging, sess_ok)


def db_pattern(**kw):
    return detect([(10, 100.0, -1), (20, 105.0, 1), (30, 100.2, -1)], 102.0, p=Inputs(**kw))


def test_breakout_needs_a_close_through_the_line_on_a_volume_spike():
    cp = db_pattern()
    assert life(cp, 36, 104.0, 102.0) == []
    assert life(cp, 37, 105.5, 104.0, spike=False) == []          # "Breakouts Require Volume Spike"
    assert life(cp, 38, 105.8, 105.5) == []                       # no longer a cross
    assert cp.drawn[0].state == "watching"

    cp = db_pattern()
    life(cp, 36, 104.0, 102.0)
    (bo,) = life(cp, 37, 105.5, 104.0)
    assert bo.state == "breakout" and bo.end_bar == 37 and bo.text == "2B ▲" and not cp.registry
    assert bo.breakout == {"bar": 37, "price": 105.0, "target": pytest.approx(110.0), "capped": False}
    assert cp.bos[-1]["target"] == pytest.approx(110.0) and cp.bos[-1]["end_bar"] == 47
    assert (cp.last_tgt, cp.last_tgt_bar) == (pytest.approx(110.0), 37)    # the forecast magnet


def test_breakout_gates_regime_and_session():
    cp = db_pattern()
    assert life(cp, 37, 105.5, 104.0, ranging=True) == [] and cp.drawn[0].state == "watching"
    cp = db_pattern(patBoVolReq=False)
    assert life(cp, 37, 105.5, 104.0, spike=False)[0].state == "breakout"
    # outside the session window it still breaks out on the chart but registers no signal / magnet
    cp = db_pattern()
    assert life(cp, 37, 105.5, 104.0, sess_ok=False) == []
    assert cp.drawn[0].state == "breakout" and cp.last_tgt is None


def test_breakout_target_is_capped_at_a_share_of_price():
    cp = db_pattern(patMaxTgtPct=2.0)
    (bo,) = life(cp, 37, 105.5, 104.0)
    assert bo.breakout["target"] == pytest.approx(105.0 * 1.02) and bo.breakout["capped"]


def test_failure_and_expiry():
    cp = db_pattern()
    life(cp, 36, 99.9, 102.0)
    assert cp.drawn[0].state == "failed" and cp.drawn[0].text == "2B ✕" and not cp.registry
    cp = db_pattern()
    exp = cp.drawn[0].exp_bar
    life(cp, exp, 103.0, 103.0)
    assert cp.drawn                                              # still there on its expiry bar
    life(cp, exp + 1, 103.0, 103.0)
    assert not cp.drawn and not cp.registry                      # stale: deleted


def test_wedge_fails_only_through_its_sloping_line():
    """v5.7.2: a new low INSIDE a falling wedge (under its last swing low, above the falling lower line) is what a
    falling wedge does on its way to the apex; only a close through the line kills it."""
    cp = detect([(10, 120.0, 1), (15, 100.0, -1), (25, 110.0, 1), (30, 98.0, -1)], 101.0)
    (fw,) = cp.drawn
    line_at = lambda i: fw.inv_lvl + fw.inv_sl * (i - fw.bar)  # noqa: E731
    assert fw.inv_sl == pytest.approx(-4.0 / 30) and line_at(36) == pytest.approx(97.2)
    life(cp, 36, 97.5, 99.0)                                     # below the 98 low, above the line
    assert fw.state == "watching"
    life(cp, 37, 97.0, 97.5)                                     # through the line (97.07)
    assert fw.state == "failed"


def test_duplicate_pattern_is_superseded_and_the_cap_unregisters():
    cp = db_pattern()
    first = cp.drawn[0]
    piv = [(20, 105.0, 1), (30, 100.2, -1), (40, 105.2, 1), (50, 100.1, -1)]   # the first low has left the history
    detect(piv, 100.15, cp=cp)
    bottoms = [x for x in cp.drawn if x.name == "Double Bottom"]
    assert first not in cp.drawn and len(bottoms) == 1 and bottoms[0].inv_lvl == 100.1

    cp = ChartPatterns(Inputs(patMaxLabels=1, patDupATR=0.0))
    detect([(10, 100.0, -1), (20, 105.0, 1), (30, 100.2, -1)], 102.0, cp=cp)
    old = cp.drawn[0]
    detect(piv, 100.15, cp=cp)
    assert old not in cp.drawn and old not in cp.registry and len(cp.drawn) == 1


# ------------------------------------------------------------------ harmonics
def xabcd(X, A, B, C, D, leg=10):
    t = (-1, 1, -1, 1, -1) if X < A else (1, -1, 1, -1, 1)
    return [(k * leg, float(px), ty) for k, (px, ty) in enumerate(zip((X, A, B, C, D), t))]


def harm(piv, p=None, i=None, sup=None, res=None, div=None, hm=None, atr=1.0):
    hm = hm or Harmonics(p or Inputs())
    i = piv[-1][0] + PLEN if i is None else i
    hm.detect(i, PLEN, piv[-1][1] if piv[-1][2] == -1 else None, piv[-1][1] if piv[-1][2] == 1 else None, piv, atr,
              False, sup, res, 0.15, 0.1, 100, div or {1: False, -1: False})
    return hm


# X, A, B, C, D built from each pattern's ratios (AB/XA, BC/AB, AD/XA, CD/BC)
HARMONICS = [
    ("Gartley", (100, 110, 103.82, 107.63924, 102.14)),       # 0.618, 0.618, 0.786, 1.44
    ("Bat", (100, 110, 105.5, 109.1, 101.14)),                 # 0.45, 0.8, 0.886, 2.21
    ("Butterfly", (100, 110, 102.14, 109.10396, 95.5)),       # 0.786, 0.886, 1.45, 1.95
    ("Crab", (100, 110, 105, 109.43, 93.82)),                  # 0.5, 0.886, 1.618, 3.52
    ("Deep Crab", (100, 110, 101.14, 106.456, 93.82)),        # 0.886, 0.6, 1.618, 2.38
    ("Alt Bat", (100, 110, 106.18, 109.5645, 98.7)),          # 0.382, 0.886, 1.13, 3.21
    ("Shark", (100, 110, 104, 111.2, 99)),                     # 0.6, 1.2, 1.1, 1.69
    ("5-0", (100, 110, 97, 123, 110)),                         # 1.3, 2.0, D at A, CD = 0.5 BC
    ("Three Drives", (100, 110, 96, 105.8, 92.08)),           # 1.4, 0.7, -, 1.4
    ("AB=CD", (100, 110, 106, 108.472, 104.472)),             # BC 0.618, CD = AB
]


@pytest.mark.parametrize("name,pts", HARMONICS, ids=[n for n, _ in HARMONICS])
def test_harmonic_best_fit_and_bearish_mirror(name, pts):
    (s,) = harm(xabcd(*pts)).sets
    assert s.name == name and s.dir == 1 and s.state == 0 and s.bar == 45
    assert [b for b, _ in s.points] == [0, 10, 20, 30, 40] and s.points[-1][1] == pts[-1]
    assert s.prz_low < pts[-1] < s.prz_high and s.tp1 > pts[-1] and s.tp2 > s.tp1
    (m,) = harm(xabcd(*(K - x for x in pts))).sets
    assert m.name == name and m.dir == -1
    assert (m.tp1, m.inv) == (pytest.approx(K - s.tp1), pytest.approx(K - s.inv))


def test_gartley_levels():
    (s,) = harm(xabcd(100, 110, 103.82, 107.63924, 102.14)).sets
    ad = 110 - 102.14
    assert s.inv == 100.0                                         # X (D completes inside XA)
    assert (s.tp1, s.tp2, s.tp_basis) == (pytest.approx(102.14 + 0.382 * ad), pytest.approx(102.14 + 0.618 * ad), "A→D")
    assert (s.prz_low, s.prz_high) == (pytest.approx(102.04), pytest.approx(102.24))   # ideal D = actual D, +-0.1 ATR
    assert s.exp_bar == 45 + 150 and s.end_x == 45 + 15 and s.text == "Gart ▲"


def test_extension_patterns_and_deep_5_0():
    (b,) = harm(xabcd(100, 110, 102.14, 109.10396, 95.5)).sets
    assert b.inv == pytest.approx(95.5 - 0.1)                     # Butterfly: beyond D by the break buffer, not X
    assert (b.prz_low, b.prz_high) == (pytest.approx(95.4), pytest.approx(110 - 1.272 * 10 + 0.1))   # ideal D 1.272 XA
    (f,) = harm(xabcd(100, 110, 97, 123, 110)).sets
    assert f.tp_basis == "C→D" and f.tp1 == pytest.approx(110 + 0.382 * 13)   # D at A: TPs off the C->D leg


def test_prz_tiers():
    piv = xabcd(100, 110, 103.82, 107.63924, 102.14)
    (s,) = harm(piv, sup=102.1, div={1: True, -1: False}).sets   # S/R at D + a divergence on the D candle
    assert s.tier == 2 and s.text == "Gart ▲ ★2"
    (s,) = harm(piv, p=Inputs(przConfluence=False), sup=102.1, div={1: True, -1: False}).sets
    assert s.tier == 0


def test_harmonic_gates():
    piv = xabcd(100, 110, 103.82, 107.63924, 102.14)
    assert not harm(piv, i=46).sets                               # D is not the pivot confirming now
    hm = harm(piv)
    hm.detect(45, PLEN, 102.14, None, piv, 1.0, False, None, None, 0.15, 0.1, 100, {})
    assert len(hm.sets) == 1                                      # one evaluation per D
    assert not harm(piv, atr=20.0).sets                           # XA under the 1-ATR minimum
    assert not harm(piv, p=Inputs(harmGartley=False)).sets
    # an older same-type pivot more extreme than the one kept would anchor the wrong swing: abort
    worse = piv[:3] + [(25, 108.0, 1)] + piv[3:]
    assert not harm(worse).sets
    milder = piv[:3] + [(25, 107.0, 1)] + piv[3:]
    assert harm(milder).sets[0].name == "Gartley"


def test_harmonic_lifecycle():
    piv = xabcd(100, 110, 103.82, 107.63924, 102.14)
    hm = harm(piv)
    hm.lifecycle(46, 104.0, 104.5, 103.5, 1.0, 0.1)
    assert hm.sets[0].state == 0
    hm.lifecycle(47, 105.0, 105.2, 104.0, 1.0, 0.1)              # high reaches TP1
    assert hm.sets[0].state == 2 and not hm.sets[0].box and hm.sets[0].text.endswith("✓")

    hm = harm(piv)
    hm.lifecycle(46, 99.9, 102.0, 99.5, 1.0, 0.1)                # close below X
    assert hm.sets[0].state == 1 and hm.sets[0].text.endswith("✕")

    hm = harm(piv)
    hm.lifecycle(45 + 151, 103.0, 103.5, 102.5, 1.0, 0.1)
    assert hm.sets[0].state == 3 and hm.sets[0].text.endswith("⋯")

    hm = harm(piv, sup=101.0)                                     # the support standing at D
    hm.lifecycle(46, 100.85, 101.5, 100.5, 1.0, 0.1)             # closes through it (-0.1 ATR buffer)
    assert hm.sets[0].state == 4 and hm.sets[0].box
    hm.lifecycle(47, 104.0, 105.5, 100.9, 1.0, 0.1)              # still live: TP1 still counts
    assert hm.sets[0].state == 2 and hm.sets[0].text == "Gart ▲ ⚠ ✓"


def test_only_three_harmonics_stay_on_the_chart():
    hm = Harmonics(Inputs())
    for k in range(4):
        piv = [(b + 100 * k, px, t) for b, px, t in xabcd(100, 110, 103.82, 107.63924, 102.14)]
        harm(piv, hm=hm)
    assert len(hm.sets) == 3 and hm.sets[0].bar == 145


# ------------------------------------------------------------------ engine, on constructed candles
def zigzag(pivots, wick=0.3, spikes=()):
    """4h candles running straight between swing points (bar, pivot price). Every candle has a wick; the swing
    candles' wick reaches the pivot price exactly, so each swing is one clean pivot at that price."""
    cp = []
    for k, (b, px) in enumerate(pivots):
        nb = pivots[k + 1][1] if k + 1 < len(pivots) else pivots[k - 1][1]
        cp.append((b, px + 2 * wick if px < nb else px - 2 * wick))
    n = cp[-1][0] + 1
    c = np.empty(n)
    for (b0, p0), (b1, p1) in zip(cp, cp[1:]):
        c[b0:b1 + 1] = np.linspace(p0, p1, b1 - b0 + 1)
    o = np.concatenate([c[:1], c[:-1]])
    h, l = np.maximum(o, c) + wick, np.minimum(o, c) - wick
    for k, (b, px) in enumerate(pivots[:-1]):
        if px < cp[k][1]:
            l[b] = px
        else:
            h[b] = px
    v = np.full(n, 100.0)
    v[list(spikes)] = 300.0
    t = (1_700_000_000 + np.arange(n) * 14400) * 1000
    return t, o, h, l, c, v


def warm(n=8, leg=12, lo=101.0, hi=105.0):
    return [(k * leg, lo if k % 2 == 0 else hi) for k in range(n)]   # ends on a high at bar 84


def run(pts, spikes=(), **kw):
    # regime filter off: a zigzag range is "ranging", which would block every break-out on the default settings
    return KimiCooked(240, Inputs(useRegime=False, **kw)).run(*zigzag(pts, spikes=spikes))


def test_engine_double_bottom_breaks_out_and_registers_pat_bo():
    pts = warm() + [(96, 96.0), (108, 106.0), (120, 96.2), (144, 116.0)]
    res = run(pts, spikes=range(126, 136))
    db = [x for x in res.final["patterns"] if x.name == "Double Bottom" and x.bar == 125]
    assert db and db[0].state == "breakout"
    bo = db[0].breakout
    sig = [s for s in res.signals if s.typ == 3]
    assert [(s.bar, s.dir, s.price) for s in sig] == [(bo["bar"], 1, bo["price"])]
    assert bo["price"] == 106.0 and bo["target"] == pytest.approx(106.0 + (106.0 - 96.0))
    assert res.final["breakouts"][-1]["target"] == bo["target"]

    off = run(pts, spikes=range(126, 136), showChartPatterns=False, showHarmonics=False)
    assert not off.final["patterns"] and not any(s.typ == 3 or 5 <= s.typ < 15 for s in off.signals)


def test_engine_gartley_registers_a_harmonic_signal():
    X, A = 100.0, 110.0
    B, D = A - 0.618 * (A - X), A - 0.786 * (A - X)
    C = B + 0.618 * (A - B)
    pts = warm() + [(96, X), (108, A), (120, B), (132, C), (144, D), (164, 112.0)]
    res = run(pts)
    (g,) = [s for s in res.final["harmonics"] if s.name == "Gartley"]
    assert [b for b, _ in g.points] == [96, 108, 120, 132, 144] and g.points[-1][1] == pytest.approx(D)
    assert g.state == 2                                           # the rally afterwards reaches TP1
    (sig,) = [s for s in res.signals if 5 <= s.typ < 15]
    assert sig.typ == 5 and sig.dir == 1 and sig.bar == g.bar and sig.pivot_bar == 144
    assert res.stats_table()["Harmonics"]["n"] <= 1               # resolved Gartley signals land in "Harmonics"


# ------------------------------------------------------------------ sessions
def ms(y, mo, d, hh, mm=0):
    return int(datetime(y, mo, d, hh, mm, tzinfo=timezone.utc).timestamp() * 1000)


def test_in_session():
    t = np.array([ms(2026, 10, 7, 3), ms(2026, 10, 7, 8), ms(2026, 10, 7, 23), ms(2026, 10, 8, 1)])
    assert in_session(t, "0000-0800", "UTC").tolist() == [True, False, False, True]
    assert in_session(t, "2200-0200", "UTC").tolist() == [False, False, True, True]
    assert in_session(t, "0000-0800", "GMT+3").tolist() == [True, False, True, True]   # 06 / 11 / 02 / 04 local
    # 7 Oct 2026 is a Wednesday (Pine day 4)
    assert in_session(t, "0000-0800:4", "UTC").tolist() == [True, False, False, False]


def test_session_arrays():
    t = np.array([ms(2026, 10, 7, h) for h in (3, 10, 13, 22)])
    ok, zone, div = session_arrays(t, 60, Inputs())
    assert ok.all() and (zone == 1).all() and (div == 1).all()                 # both off by default
    ok, zone, div = session_arrays(t, 60, Inputs(useSessFilter=True, useSessionParams=True))
    assert ok.tolist() == [True, False, True, False]
    assert zone.tolist() == [1.3, 1.0, 0.85, 1.0] and div.tolist() == [1.2, 1.0, 0.85, 1.0]
    assert session_arrays(t, 1440, Inputs(useSessFilter=True))[0].all()      # no sessions on daily charts


@pytest.fixture(scope="module")
def sample() -> list[Candle]:
    with gzip.open(SAMPLE, "rt") as f:
        return [Candle(time=int(float(r["time"])), open=float(r["open"]), high=float(r["high"]), low=float(r["low"]),
                       close=float(r["close"]), volume=float(r["volume"])) for r in csv.DictReader(f)]


def _cols(candles):
    t = np.array([c.time for c in candles], dtype="int64") * 1000
    return (t, *(np.array([getattr(x, k) for x in candles]) for k in ("open", "high", "low", "close", "volume")))


def test_session_filter_on_the_engine(sample):
    cols = _cols(sample[-1500:])
    base = KimiCooked(60, Inputs.users_chart()).run(*cols)
    closed = KimiCooked(60, Inputs(useSessFilter=True, sess1On=False, sess2On=False)).run(*cols)
    assert base.signals and not closed.signals                     # no window open: nothing registers
    asia = KimiCooked(60, Inputs(useSessFilter=True, sess2On=False)).run(*cols)
    hours = {datetime.fromtimestamp(s.time_ms / 1000, tz=timezone.utc).hour for s in asia.signals}
    assert asia.signals and hours <= set(range(0, 8))


# ------------------------------------------------------------------ service + agent
def test_sample_tables_and_drawing(sample):
    cols = _cols(sample)
    res = KimiCooked(60, Inputs.users_chart()).run(*cols)
    s = res.stats_table()
    assert list(s)[:5] == ["DIV +/-", "U / Dn", "Early ?", "Pat BO", "Harmonics"]
    # the divergence signals and the random controls don't depend on the new modules
    assert [(s[k]["wins"], s[k]["n"]) for k in ("DIV +/-", "U / Dn", "Early ?", "Random")] == [
        (6, 30), (3, 13), (39, 135), (373, 1188)]
    assert (s["Pat BO"]["wins"], s["Pat BO"]["n"]) == (2, 14) and (s["Harmonics"]["wins"], s["Harmonics"]["n"]) == (4, 16)
    assert (s["Long"]["n"], s["Short"]["n"]) == (104, 104)          # Long / Short now include them, as on the chart

    k = compute("BTCUSDT", "1h", sample, "binance")
    times = {c.time for c in sample}
    assert 0 < len(k.patterns) <= 5 and len(k.breakouts) <= 10 and 0 < len(k.harmonics) <= 3
    for p in k.patterns:
        assert p.time in times and len(p.lines) >= 2 and p.text.startswith(p.text.split()[0])
        assert all(x.time_end >= x.time_start for x in p.lines)
        assert (p.breakout_level is not None) == (p.state == "watching")
        assert (p.target is not None) == (p.state == "breakout")
    for h in k.harmonics:
        assert [x.label for x in h.points] == list("XABCD") and h.time in times
        assert [x.time for x in h.points] == sorted(x.time for x in h.points) and h.prz_low < h.prz_high
        assert h.time_end > h.time and h.state in ("active", "failed", "tp1", "expired", "compromised")
    labels = [r.label for r in k.stats]
    assert labels[3:5] == ["Pat BO", "Harmonics"]
    assert {s.type for s in k.signals} <= {"DIV", "U/Dn", "Early"}
    assert not any("chart patterns" in n for n in k.notes)

    facts = summarize(k)
    assert len(facts["chart_patterns"]) == len(k.patterns) and len(facts["harmonics"]) == len(k.harmonics)
    assert {"Pat BO", "Harmonics"} <= set(facts["signal_stats"])
    text = " ".join(_kimi_lines(facts))
    assert "Harmonic: " in text and k.harmonics[-1].name in text


def test_agent_lines_for_patterns():
    facts = {"indicator": "Kimi Cooked v5.7.4 on BTCUSDT 4h",
             "chart_patterns": [
                 {"pattern": "Double Bottom", "direction": "bullish", "state": "watching", "formed_bars_ago": 3,
                  "breakout_level": 105.0, "invalidation": 100.0},
                 {"pattern": "Bear Flag", "direction": "bearish", "state": "breakout", "formed_bars_ago": 30,
                  "broke_out_at": 98.0, "target": 90.0}],
             "harmonics": [{"pattern": "Bat", "direction": "bullish", "state": "active", "d": 101.14,
                            "completed_bars_ago": 2, "prz": [101.0, 101.3], "tp1": 104.0, "tp2": 106.0,
                            "invalidation": 100.0, "prz_confluence": "1/3"}]}
    text = "\n".join(_kimi_lines(facts))
    assert "Double Bottom (bullish, formed 3 bars ago): breaks out on a close above 105" in text
    assert "Bear Flag broke out at 98" in text and "target 90" in text
    assert "Harmonic: bullish Bat (active)" in text and "TP1 104" in text


def test_backtest_names_the_new_signal_types():
    assert TYPE_NAMES[3] == "Pat BO" and TYPE_NAMES[5] == "Gartley" and TYPE_NAMES[14] == "AB=CD"
    assert LABELS[(3, 1)] == "BO▲" and LABELS[(5, 1)] == "Gart ▲" and LABELS[(11, -1)] == "Shrk ▼"
