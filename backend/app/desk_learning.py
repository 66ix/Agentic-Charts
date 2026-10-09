"""What the agent desk learns from its own calls: how likely a buy zone is to work, where to put the take-profit,
and whether its confidence numbers can be trusted. Pure functions over the desk's call records (desk_calls.py).

Confidence: the chance that price reaches the full level above (the resistance or supply the take-profit is set
against) before the invalidation, within the hold window. It starts from the odds on a random walk, 1 / (1 + R) for a
level R risk-units away (a 3R target is reached first a quarter of the time with no edge at all), times the setup's
learned edge (`lift`): how many more hits its calls got than random odds would give. So a far target is never
called as likely as a near one, and a setup with no evidence has no edge, makes no call, and only starts calling once
its backtest or its live record shows one.

The edge is built up level by level, each level's observed hits over the hits random odds would have given, borrowing
K_EXPECTED expected hits from the level above it, so a level with little evidence stays close to its parent and one
with plenty speaks for itself:

  no edge (BASE_LIFT)
  -> this timeframe and zone kind, across every coin the desk has called ("H4 demand")
  -> this setup: freshness, a higher-timeframe zone behind it, trend agreement ("fresh, D1 behind, trend with it")
  -> this coin: the backtest of the same setup on its own history (each trade counts BACKTEST_WEIGHT of a live
     call, at most BACKTEST_CAP trades) plus the desk's live calls on the coin

Live calls fade with age (half-life HALF_LIFE_DAYS), so a market that changes character shows up within weeks.
Only calls and backtests on real prices count, unless the whole app runs on demo data.

Take-profit placement: every call also records how far price got before the invalidation or the end of its window
(`reach_r`). For a setup with at least MIN_TP_SAMPLES finished calls, the take-profit can sit a share of the way to the
level (TP_FRACTIONS) when that earned more on those calls than the full level would have: e.g. price stalling just
under resistance. The confidence for that take-profit is the level's confidence scaled by how much more often the
nearer target was reached.

Calibration: closed calls grouped by their stated confidence against how often they reached the take-profit, and the
Brier score against always quoting the overall hit rate. Calls the desk said were 60% should work about 60% of the
time; if they don't, the numbers are not to be trusted yet and the panel says so.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .desk_calls import CLOSED, DeskCall, expected_r
from .schemas import TrackRecord
from .ta_agent import TF_LABEL

BASE_LIFT = 1.0
K_EXPECTED = 3.0
HALF_LIFE_DAYS = 90.0
BACKTEST_WEIGHT = 0.5
BACKTEST_CAP = 40
MAX_P, MIN_P = 0.9, 0.02
MAX_LIFT = 3.0
TP_FRACTIONS = (0.7, 0.8, 0.9, 1.0)
MIN_TP_SAMPLES = 15
TP_MIN_GAIN = 0.05       # R per call a nearer take-profit must add over the full level
CAL_EDGES = (0.0, 0.25, 0.35, 0.45, 0.55, 0.65, 1.0)
MIN_CAL_CALLS = 20


def random_odds(rr: float) -> float:
    """Chance a random walk reaches +rr before -1: 1 / (1 + rr)."""
    return 1.0 / (1.0 + rr) if rr > 0 else 1.0


@dataclass
class Level:
    name: str
    n: float          # calls (weighted)
    hits: float       # hits (weighted)
    expected: float   # hits random odds would have given
    lift: float       # the edge after this level

    def as_dict(self) -> dict:
        return {"level": self.name, "calls": round(self.n, 1), "hits": round(self.hits, 1),
                "expected": round(self.expected, 2), "lift": round(self.lift, 3)}


@dataclass
class Estimate:
    p: float
    lift: float
    odds: float
    basis: str
    levels: list[Level] = field(default_factory=list)


def weight(call: DeskCall, now: float) -> float:
    age_days = max(0.0, now - (call.closed_at or call.created_at)) / 86400
    return 0.5 ** (age_days / HALF_LIFE_DAYS)


def learnable(calls: Iterable[DeskCall], demo_ok: bool = False) -> list[DeskCall]:
    """Calls whose level outcome is known, on real prices (or demo prices when the app runs on demo data)."""
    return [c for c in calls if c.level_hit is not None and (demo_ok or c.data_source == "binance")]


def level_r(c: DeskCall) -> float:
    """Distance from the fill (or entry) to the full level, in R."""
    fill = c.fill_price or c.entry
    risk = fill - c.stop
    return (c.tp_level - fill) / risk if risk > 0 else 0.0


def _count(calls: list[DeskCall], now: float) -> tuple[float, float, float]:
    """(weighted calls, weighted hits, weighted hits random odds would have given)."""
    n = hits = exp = 0.0
    for c in calls:
        w = weight(c, now)
        n += w
        hits += w if c.level_hit else 0.0
        exp += w * random_odds(level_r(c))
    return n, hits, exp


def _shrink(hits: float, exp: float, parent: float) -> float:
    return min(MAX_LIFT, (hits + K_EXPECTED * parent) / (exp + K_EXPECTED))


def _backtest(track: Optional[TrackRecord], demo_ok: bool) -> tuple[float, float, float]:
    """A coin's backtest of the same setup as pseudo-calls: (n, hits, expected hits). The backtest's average target
    distance comes from its win rate and average R: avg_r = w x rr - (1 - w)."""
    if track is None or track.status not in ("ok", "small_sample", "too_few_trades") or not track.trades:
        return 0.0, 0.0, 0.0
    if track.data_source == "synthetic" and not demo_ok:
        return 0.0, 0.0, 0.0
    w = track.wins / track.trades if track.trades else 0.0
    rr = (track.avg_r + 1 - w) / w if w > 0 and track.avg_r is not None else 1.5
    trades = min(track.trades, BACKTEST_CAP) * BACKTEST_WEIGHT
    # Fees pull avg_r down, so the implied distance can come out too short; the backtest's targets are >= 0.8R.
    return trades, trades * w, trades * random_odds(max(rr, 0.8))


def estimate(symbol: str, interval: str, kind: str, bucket: str, rr_level: float, track: Optional[TrackRecord],
             history: list[DeskCall], now: float, demo_ok: bool = False) -> Estimate:
    """Confidence that price reaches a level `rr_level` R away before the invalidation (see the module docstring),
    with the chain that produced it."""
    tfl = TF_LABEL.get(interval, interval)
    odds = random_odds(rr_level)
    same_kind = [c for c in history if c.interval == interval and c.kind == kind]
    in_bucket = [c for c in same_kind if c.bucket == bucket]
    on_coin = [c for c in same_kind if c.symbol == symbol]
    levels: list[Level] = []
    lift = BASE_LIFT
    for name, rows in ((f"{tfl} {kind}, every coin", same_kind), ("this setup", in_bucket)):
        n, hits, exp = _count(rows, now)
        lift = _shrink(hits, exp, lift)
        levels.append(Level(name, n, hits, exp, lift))
    bn, bh, be = _backtest(track, demo_ok)
    ln, lh, le = _count(on_coin, now)
    lift = _shrink(bh + lh, be + le, lift)
    base = symbol.removesuffix("USDT")
    levels.append(Level(f"{base}: backtest + live", bn + ln, bh + lh, be + le, lift))
    p = max(MIN_P, min(MAX_P, odds * lift))

    bits = [f"random odds for a {rr_level:.1f}R level are {odds * 100:.0f}%"]
    if bn:
        bits.append(f"the backtest on {base} reached it {track.wins}/{track.trades} times "  # type: ignore[union-attr]
                    f"({bh / be:.1f}x random)" if be else "")
    for name, rows in ((f"the desk's {tfl} {kind} calls", same_kind), ("this setup", in_bucket)):
        if rows and (name.startswith("the desk") or len(rows) != len(same_kind)):
            n, hits, exp = _count(rows, now)
            bits.append(f"{name}: {hits:.0f}/{n:.0f} ({hits / exp:.1f}x random)" if exp else f"{name}: {hits:.0f}/{n:.0f}")
    if len(bits) == 1:
        bits.append("no backtest or past calls yet, so no edge is assumed")
    return Estimate(p, lift, odds, "Edge: " + "; ".join(b for b in bits if b) + f" → {lift:.2f}x.", levels)


# --------------------------------------------------------------------- take-profit --


def reach_rows(history: list[DeskCall], interval: str, kind: str) -> list[DeskCall]:
    return [c for c in history if c.interval == interval and c.kind == kind and c.reach_final and c.reach_r is not None
            and level_r(c) > 0]


def reach_rate(rows: list[DeskCall], fraction: float, now: float) -> float:
    """Weighted share of calls that got `fraction` of the way to their level."""
    n = hits = 0.0
    for c in rows:
        w = weight(c, now)
        n += w
        hits += w if (c.reach_r or 0.0) >= fraction * level_r(c) - 1e-9 else 0.0
    return hits / n if n else 0.0


@dataclass
class TakeProfit:
    fraction: float
    price: float
    p: float
    note: str = ""
    table: list[dict] = field(default_factory=list)


def take_profit(entry: float, stop: float, tp_level: float, p_level: float, risk_pct: float, interval: str,
                kind: str, history: list[DeskCall], now: float) -> TakeProfit:
    """Where to put the take-profit on the way to `tp_level`, and its confidence (see the module docstring)."""
    risk = entry - stop
    full = TakeProfit(1.0, tp_level, p_level)
    rows = reach_rows(history, interval, kind)
    if len(rows) < MIN_TP_SAMPLES or risk <= 0:
        return full
    base_rate = reach_rate(rows, 1.0, now)
    lr = (tp_level - entry) / risk
    table, best = [], None
    for f in TP_FRACTIONS:
        rate = reach_rate(rows, f, now)
        # The level's own confidence, scaled by how much more often the nearer target was reached on past calls.
        p = p_level if f == 1.0 else min(MAX_P, p_level * (rate / base_rate)) if base_rate > 0 else rate
        rr = f * lr
        er = expected_r(p, rr, risk_pct)
        table.append({"fraction": f, "reached": round(rate, 3), "p": round(p, 3), "rr": round(rr, 2),
                      "expected_r": round(er, 3)})
        if rr >= 1.0 and (best is None or er > best[1]):
            best = (f, er, p)
    full_er = next(r["expected_r"] for r in table if r["fraction"] == 1.0)
    if best is None or best[0] == 1.0 or best[1] - full_er < TP_MIN_GAIN:
        return TakeProfit(1.0, tp_level, p_level, table=table)
    f, er, p = best
    price = entry + f * (tp_level - entry)
    note = (f"Take-profit at {f * 100:.0f}% of the way to the level: on {len(rows)} past "
            f"{TF_LABEL.get(interval, interval)} {kind} calls price got that far {reach_rate(rows, f, now) * 100:.0f}% "
            f"of the time against {base_rate * 100:.0f}% for the full level ({er:+.2f}R vs {full_er:+.2f}R a call).")
    return TakeProfit(f, price, p, note, table)


# -------------------------------------------------------------------- calibration --


def calibration(calls: list[DeskCall], demo_ok: bool = False) -> dict:
    """Stated confidence against what happened, on closed calls (tp counts as a hit; invalidated or timed out as a
    miss)."""
    rows = [c for c in calls if c.status in CLOSED and (demo_ok or c.data_source == "binance")]
    out: dict = {"calls": len(rows), "bins": [], "brier": None, "brier_base": None, "hit_rate": None,
                 "mean_confidence": None, "enough": len(rows) >= MIN_CAL_CALLS}
    if not rows:
        return out
    hits = [1.0 if c.status == "tp" else 0.0 for c in rows]
    rate = sum(hits) / len(rows)
    out["hit_rate"] = round(rate, 4)
    out["mean_confidence"] = round(sum(c.confidence for c in rows) / len(rows), 4)
    out["brier"] = round(sum((c.confidence - h) ** 2 for c, h in zip(rows, hits)) / len(rows), 4)
    out["brier_base"] = round(sum((rate - h) ** 2 for h in hits) / len(rows), 4)
    for lo, hi in zip(CAL_EDGES, CAL_EDGES[1:]):
        group = [(c, h) for c, h in zip(rows, hits) if lo <= c.confidence < hi or (hi == 1.0 and c.confidence == 1.0)]
        if group:
            out["bins"].append({"from": lo, "to": hi, "calls": len(group),
                                "predicted": round(sum(c.confidence for c, _ in group) / len(group), 4),
                                "actual": round(sum(h for _, h in group) / len(group), 4)})
    return out


def verdict(cal: dict) -> str:
    """One line on whether the confidence numbers can be trusted yet."""
    if not cal["calls"]:
        return "No closed calls yet."
    if not cal["enough"]:
        return f"{cal['calls']} closed call{'s' if cal['calls'] != 1 else ''}: too few to judge the confidence yet."
    gap = (cal["mean_confidence"] - cal["hit_rate"]) * 100
    skill = cal["brier_base"] - cal["brier"]
    side = ("about right" if abs(gap) < 5 else f"{abs(gap):.0f} points too {'high' if gap > 0 else 'low'}")
    return (f"On {cal['calls']} closed calls the confidence averaged {cal['mean_confidence'] * 100:.0f}% and "
            f"{cal['hit_rate'] * 100:.0f}% reached the take-profit: {side}. "
            + ("It sorts good calls from bad better than a flat guess." if skill > 0.002 else
               "It doesn't yet sort good calls from bad better than a flat guess."))


# ------------------------------------------------------------------------ summary --


def bucket_table(calls: list[DeskCall], now: float, demo_ok: bool = False) -> list[dict]:
    """Per setup: closed calls, hit rate, average R, and the level's learned confidence, most calls first."""
    groups: dict[str, list[DeskCall]] = {}
    for c in calls:
        if c.status in CLOSED and (demo_ok or c.data_source == "binance"):
            groups.setdefault(c.bucket, []).append(c)
    out = []
    for b, rows in groups.items():
        _, hits, exp = _count(learnable(rows, True), now)
        rs = [c.r for c in rows if c.r is not None and math.isfinite(c.r)]
        out.append({"bucket": b, "setup": rows[-1].setup, "calls": len(rows),
                    "tp": sum(1 for c in rows if c.status == "tp"),
                    "invalidated": sum(1 for c in rows if c.status == "invalidated"),
                    "timed_out": sum(1 for c in rows if c.status == "timed_out"),
                    "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
                    "total_r": round(sum(rs), 3) if rs else None,
                    "lift": round(_shrink(hits, exp, BASE_LIFT), 3)})
    return sorted(out, key=lambda r: (-r["calls"], r["bucket"]))


# ----------------------------------------------------------------------- working now --

RECENT_DAYS = 30
MIN_RECENT = 5


def working_now(calls: list[DeskCall], now: float, days: int = RECENT_DAYS, demo_ok: bool = False) -> list[dict]:
    """Setups by how their zones did in the last `days` days (calls and watched zones whose level outcome is known),
    against what random odds would have given, best first. Only setups with MIN_RECENT finished zones."""
    since = now - days * 86400
    groups: dict[str, list[DeskCall]] = {}
    for c in learnable(calls, demo_ok):
        if (c.closed_at or c.created_at) >= since:
            groups.setdefault(c.bucket, []).append(c)
    out = []
    for b, rows in groups.items():
        if len(rows) < MIN_RECENT:
            continue
        hits = sum(1 for c in rows if c.level_hit)
        exp = sum(random_odds(level_r(c)) for c in rows)
        lift = hits / exp if exp else 0.0
        out.append({"bucket": b, "setup": rows[-1].setup, "zones": len(rows), "hits": hits,
                    "expected": round(exp, 2), "lift": round(lift, 2),
                    "verdict": "working" if lift >= 1.2 else "not working" if lift <= 0.8 else "no edge either way"})
    return sorted(out, key=lambda r: (-r["lift"], -r["zones"]))
