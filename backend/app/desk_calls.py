"""The agent desk's calls: spot buy ideas it makes on its own, how big a paper position each gets, and how each is
scored as the candles come in. Pure functions; the service around them is agent_desk.py.

A call is a long only (spot: no shorts, no leverage): a buy zone from the chart agent's own detectors, a limit buy at
the top of the zone (where price reaches it first), an invalidation just below it (the plan's stop: where the idea is
wrong) and a take-profit at the next resistance or supply above (the plan's T1, optionally a learned share of the way
there). Its confidence is the chance that, once bought, price reaches the take-profit before the invalidation within
the hold window (HOLD_BARS); desk_learning.py estimates it from the backtest and the desk's own past calls.

Scoring replays each call on finer candles than its timeframe (SCORE_RES) with the trade journal's rules
(journal.evaluate_entry: only candles after the call count, the stop first when both are inside one candle, fees on
both sides), so a call's result can never use a price from before it was made:

  waiting      the limit buy hasn't filled yet
  expired      it didn't fill within ENTRY_BARS candles of its timeframe
  open         bought; neither level reached yet
  tp           take-profit reached first (a hit)
  invalidated  invalidation reached first (a miss)
  timed_out    neither within HOLD_BARS candles: closed at that candle's close (a miss for the confidence, its R counts)

R is after fees, against the entry-to-invalidation distance.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Literal, Optional

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from .journal import JournalEntry, ManualClose, evaluate_entry
from .market_data import INTERVAL_SECONDS
from .pricefmt import _fmt
from .schemas import AnalysisIntent, TradePlan
from .ta_agent import TF_LABEL, analyze, atr, ema
from .trade_plan import RESIST_KINDS, Level, build_plan

DESK_TIMEFRAMES = ("1h", "4h", "1d")
ENTRY_BARS = {"1h": 24, "4h": 18, "1d": 10}    # the limit buy waits this many candles
HOLD_BARS = {"1h": 72, "4h": 60, "1d": 45}     # then the position has this many to reach a level
SCORE_RES = {"1h": "5m", "4h": "15m", "1d": "1h"}
FEE_PCT = 0.1           # per side, Binance spot without BNB
MAX_DIST_ATR = 4.0      # buy zones further below price than this aren't called
MIN_RR = 1.0            # take-profit at least 1R away
MIN_EXPECTED_R = 0.1    # a call needs this much expected R after fees
SIZE_SCALE = 0.25       # paper position = Kelly fraction x this, as a share of the wallet ...
MAX_POSITION_PCT = 15.0  # ... capped here
MIN_NOTIONAL = 10.0     # Binance's minimum order is about this, in USDT
DESK_INTENT = AnalysisIntent(features=["support_resistance", "supply_demand"], max_zones=3)
SPOT_KINDS = ("support", "demand")

Status = Literal["waiting", "expired", "open", "tp", "invalidated", "timed_out", "cancelled"]
ACTIVE: tuple[str, ...] = ("waiting", "open")
CLOSED: tuple[str, ...] = ("tp", "invalidated", "timed_out")


class DeskCall(BaseModel):
    """One call. Mirrors DeskCall in frontend/lib/desk.ts."""

    id: str
    symbol: str
    interval: str
    shadow: bool = Field(False, description="A zone the desk watched but didn't call: scored and learned from, never "
                                            "traded or announced")
    created_at: int = Field(..., description="UNIX s: when it was made, just after its candle closed")
    bar_time: int = Field(..., description="Open time of the closed candle it was made on")
    data_source: str = "binance"
    status: Status = "waiting"
    price_at_call: float
    entry: float = Field(..., description="Limit buy: the top of the buy zone, or the price when it is inside")
    zone_low: float
    zone_high: float
    stop: float = Field(..., description="Invalidation: where the idea is wrong")
    tp: float = Field(..., description="Take-profit")
    tp_label: str = ""
    tp_level: float = Field(..., description="The resistance or supply the take-profit is set against")
    tp_fraction: float = Field(1.0, description="Share of the way to tp_level the take-profit sits (learned)")
    rr: float = Field(..., description="Reward-to-risk at the take-profit")
    risk_pct: float
    confidence: float = Field(..., description="Chance of the take-profit before the invalidation, once bought")
    confidence_basis: str = ""
    expected_r: float = Field(..., description="confidence x rr - (1 - confidence) - fees, in R")
    kelly: float
    size_pct: float = Field(0.0, description="Paper position as % of the wallet")
    notional: Optional[float] = Field(None, description="Paper USDT; None when nothing was placed")
    paper_group: Optional[str] = None
    paper_note: str = ""
    setup: str = Field("", description="e.g. 'H4 demand (fresh, D1 behind)'")
    kind: str = "demand"
    bucket: str = Field("", description="Learning group: timeframe, kind, freshness, HTF, trend agreement")
    features: dict = Field(default_factory=dict)
    expires_at: int
    hold_seconds: int
    filled_at: Optional[int] = None
    fill_price: Optional[float] = None
    closed_at: Optional[int] = None
    exit_price: Optional[float] = None
    r: Optional[float] = Field(None, description="After fees")
    mfe_r: Optional[float] = None
    mae_r: Optional[float] = None
    # What the learning uses, measured apart from the take-profit so a learned take-profit can't bias it: did price
    # reach the full level (tp_level) before the invalidation within the hold window, and how far it got (in R).
    level_hit: Optional[bool] = Field(None, description="None until it is known")
    reach_r: Optional[float] = Field(None, description="Furthest move up before the invalidation or the window's end")
    reach_final: bool = False
    open_r: Optional[float] = None
    notes: list[str] = Field(default_factory=list)
    updated_at: int = 0

    @property
    def hit(self) -> Optional[bool]:
        return None if self.status not in CLOSED else self.status == "tp"


# ------------------------------------------------------------------ candidates --


@dataclass
class Candidate:
    """A possible call on one buy zone, before the confidence is known."""

    plan: TradePlan
    zone: Level
    last: float
    atr: float
    features: dict
    bucket: str
    setup: str
    tp_level: float
    tp_label: str
    notes: list[str] = field(default_factory=list)


def trend_of(df: pd.DataFrame) -> str:
    """EMA 20 against EMA 50 with the slow EMA's slope (the rule ta_agent.analyze uses)."""
    if len(df) < 60:
        return "range"
    a = float(atr(df).iloc[-1]) or 1e-12
    fast, slow = ema(df["close"], 20), ema(df["close"], 50)
    slope = (slow.iloc[-1] - slow.iloc[-10]) / a
    if fast.iloc[-1] > slow.iloc[-1] and slope > 0.2:
        return "up"
    if fast.iloc[-1] < slow.iloc[-1] and slope < -0.2:
        return "down"
    return "range"


def agree_level(ratio: float) -> str:
    return "with" if ratio >= 0.67 else "against" if ratio < 0.34 else "mixed"


def bucket_of(interval: str, kind: str, fresh: Optional[bool], htf: bool, agree: str) -> str:
    """The group a call learns with: 4h|demand|fresh|htf|with."""
    return "|".join((interval, kind, "fresh" if fresh else "tested" if fresh is False else "-",
                     "htf" if htf else "nohtf", agree))


def describe_setup(interval: str, kind: str, fresh: Optional[bool], htf: list[str], agree: str) -> str:
    bits = []
    if fresh is True:
        bits.append("fresh")
    elif fresh is False:
        bits.append("tested")
    if htf:
        bits.append(f"{'/'.join(htf)} behind")
    bits.append({"with": "trend with it", "against": "trend against it", "mixed": "trend mixed"}[agree])
    return f"{TF_LABEL.get(interval, interval)} {kind} ({', '.join(bits)})"


def candidates(symbol: str, interval: str, df: pd.DataFrame, frames: dict[str, pd.DataFrame]) -> list[Candidate]:
    """Every buy zone worth a call on this candle: support and demand at or below price (within MAX_DIST_ATR), each
    with the plan the trade-plan builder makes on it. CPU-bound: call it in a thread."""
    df = df.reset_index(drop=True)
    res = analyze(df, DESK_INTENT, interval, None, frames)
    last, atr_v = res.stats.last_price, res.stats.atr
    if atr_v <= 0 or last <= 0:
        return []
    trends = [res.stats.trend] + [trend_of(f.reset_index(drop=True)) for f in frames.values()]
    ratio = sum(1.0 if t == "up" else 0.5 if t == "range" else 0.0 for t in trends) / len(trends)
    agree = agree_level(ratio)
    resist = [lv for lv in res.levels if lv.kind in RESIST_KINDS and lv.kind not in SPOT_KINDS]
    rsi = (res.facts.get("momentum") or {}).get("rsi")
    out: list[Candidate] = []
    for zone in res.levels:
        if zone.kind not in SPOT_KINDS or zone.low > last + 0.1 * atr_v:
            continue
        dist = max(0.0, last - zone.high) / atr_v
        if dist > MAX_DIST_ATR:
            continue
        plan = build_plan("long", last, atr_v, res.stats.trend, [zone] + resist, res.swing_lows, res.swing_highs,
                          res.bias)
        if plan is None or plan.zone_kind != zone.kind or not plan.targets or plan.targets[0].rr < MIN_RR:
            continue
        fresh = None if zone.tests is None else zone.tests == 0
        htf = list(zone.htf)
        bucket = bucket_of(interval, zone.kind, fresh, bool(htf), agree)
        t1 = plan.targets[0]
        features = {"kind": zone.kind, "fresh": fresh, "htf": htf, "agree": agree, "agree_ratio": round(ratio, 2),
                    "trends": trends, "trend": res.stats.trend, "bias": res.bias, "distance_atr": round(dist, 2),
                    "rr": t1.rr, "risk_pct": plan.risk_pct, "rsi": rsi, "zone_score": round(zone.score, 3),
                    "atr_pct": round(atr_v / last * 100, 3)}
        out.append(Candidate(plan=plan, zone=zone, last=last, atr=atr_v, features=features, bucket=bucket,
                             setup=describe_setup(interval, zone.kind, fresh, htf, agree), tp_level=t1.price,
                             tp_label=t1.label, notes=list(plan.notes)))
    return out


# ---------------------------------------------------------------------- sizing --


def fee_r(risk_pct: float) -> float:
    """Both sides' fees in R."""
    return 2 * FEE_PCT / risk_pct if risk_pct > 0 else 0.0


def expected_r(p: float, rr: float, risk_pct: float) -> float:
    return p * rr - (1 - p) - fee_r(risk_pct)


def kelly(p: float, rr: float) -> float:
    """Kelly fraction for a bet that wins rr or loses 1: p - (1 - p) / rr (negative = no edge)."""
    return p - (1 - p) / rr if rr > 0 else -1.0


def size_for(p: float, rr: float, equity: float, free_cash: float) -> tuple[float, float, Optional[float], str]:
    """Paper position for a call → (kelly, size % of the wallet, notional USDT or None, note). The more confident
    the call and the better its reward-to-risk, the bigger: Kelly x SIZE_SCALE of the wallet, at most
    MAX_POSITION_PCT, and never more than the free cash."""
    k = kelly(p, rr)
    pct = max(0.0, min(MAX_POSITION_PCT, k * SIZE_SCALE * 100))
    if pct <= 0 or equity <= 0:
        return k, 0.0, None, "No edge after the confidence: nothing placed."
    want = equity * pct / 100
    note = ""
    if want > free_cash:
        want, note = free_cash, f"Trimmed to the free cash ({free_cash:,.2f} USDT)."
    if want < MIN_NOTIONAL:
        return k, pct, None, "Not enough free cash in the agent's wallet: nothing placed."
    return k, round(pct, 2), round(want, 2), note


# --------------------------------------------------------------------- scoring --


def _entry(call: DeskCall, close: Optional[ManualClose] = None) -> JournalEntry:
    return JournalEntry(id=call.id, symbol=call.symbol, interval=call.interval, direction="long", entry=call.entry,
                        stop=call.stop, targets=[call.tp], setup=call.setup[:60] or "desk", source="agent_plan",
                        entry_type="limit", fee_pct=FEE_PCT, manage="none", taken_at=call.created_at,
                        created_at=call.created_at, manual_close=close)


def level_reach(call: DeskCall, df: pd.DataFrame, filled_at: int, fill_price: float, step: int,
                now: float) -> dict:
    """Did price reach the full level before the invalidation within the hold window, and how far up it got (R from
    the fill) → {level_hit, reach_r, reach_final}. The fill's own candle counts for the invalidation but not for the
    move up (its high may have come before the fill); a candle with both counts as the invalidation first."""
    t = df["time"].to_numpy(dtype=np.int64)
    h, lo = df["high"].to_numpy(dtype=float), df["low"].to_numpy(dtype=float)
    hold_until = filled_at + call.hold_seconds
    inside = np.nonzero((t >= filled_at) & (t < hold_until))[0]
    risk = fill_price - call.stop
    if not len(inside) or risk <= 0:
        return {"level_hit": None, "reach_r": None, "reach_final": False}
    first = int(inside[0])
    end = int(inside[-1]) + 1
    stops = np.nonzero(lo[first:end] <= call.stop)[0]
    i_stop = first + int(stops[0]) if len(stops) else None
    ups = np.nonzero(h[first + 1:end] >= call.tp_level)[0]
    i_level = first + 1 + int(ups[0]) if len(ups) else None
    upto = i_stop if i_stop is not None else end
    highs = h[first + 1:upto]
    reach = round(max(0.0, (float(highs.max()) - fill_price) / risk), 4) if len(highs) else 0.0
    window_over = now >= hold_until and t[end - 1] + step >= hold_until  # every candle of the window is in
    if i_level is not None and (i_stop is None or i_level < i_stop):
        hit: Optional[bool] = True
    elif i_stop is not None or window_over:
        hit = False
    else:
        hit = None
    return {"level_hit": hit, "reach_r": reach, "reach_final": bool(i_stop is not None or window_over)}


def score_call(call: DeskCall, df: pd.DataFrame, source: str, resolution: str, now: float) -> dict:
    """Where the call stands on these candles (time/open/high/low/close at `resolution`, from its creation on)
    → the fields to update. Pure and deterministic."""
    step = INTERVAL_SECONDS[resolution]
    ev = evaluate_entry(_entry(call), df, source, resolution, now)
    if ev.filled_at is None or ev.filled_at >= call.expires_at:
        if now >= call.expires_at:
            return {"status": "expired", "closed_at": call.expires_at, "filled_at": None, "fill_price": None}
        return {"status": "waiting"}
    out: dict = {"filled_at": ev.filled_at, "fill_price": ev.fill_price, "mfe_r": ev.mfe_r, "mae_r": ev.mae_r,
                 **level_reach(call, df, ev.filled_at, ev.fill_price or call.entry, step, now)}
    hold_until = ev.filled_at + call.hold_seconds
    if ev.status == "closed" and ev.closed_at is not None and ev.closed_at < hold_until:
        last = ev.exits[-1]
        out.update(status="tp" if last.kind == "T1" else "invalidated", closed_at=last.time, exit_price=last.price,
                   r=ev.realized_r)
        return out
    if now < hold_until:
        out.update(status="open", r=None)
        out["open_r"] = round(ev.open_r - fee_r(call.risk_pct) / 2, 4)
        return out
    # Held for the whole window without reaching a level: out at the close of the candle the window ends in.
    t = df["time"].to_numpy(dtype=np.int64)
    done = np.nonzero((t >= call.created_at) & (t + step <= hold_until))[0]
    if not len(done):
        out.update(status="open", r=None)
        return out
    px = float(df["close"].iloc[int(done[-1])])
    ev2 = evaluate_entry(_entry(call, ManualClose(time=hold_until, price=px)), df, source, resolution, now)
    out.update(status="timed_out", closed_at=hold_until, exit_price=px, r=ev2.realized_r, mfe_r=ev2.mfe_r,
               mae_r=ev2.mae_r)
    return out


# ---------------------------------------------------------------- the call itself --


def new_call(c: Candidate, symbol: str, interval: str, bar_time: int, now: int, source: str, confidence: float,
             basis: str, tp: float, tp_fraction: float, shadow: bool = False) -> DeskCall:
    """A call from a candidate, its confidence and its (possibly learned) take-profit. Sizing is added after."""
    step = INTERVAL_SECONDS[interval]
    plan = c.plan
    risk = plan.entry - plan.stop
    rr = round((tp - plan.entry) / risk, 2) if risk > 0 else 0.0
    return DeskCall(
        id=uuid.uuid4().hex[:12], symbol=symbol, interval=interval, shadow=shadow, created_at=now, bar_time=bar_time,
        data_source=source, price_at_call=c.last, entry=plan.entry, zone_low=float(f"{c.zone.low:.6g}"),
        zone_high=float(f"{c.zone.high:.6g}"),
        stop=plan.stop, tp=float(f"{tp:.6g}"), tp_label=c.tp_label, tp_level=c.tp_level, tp_fraction=tp_fraction,
        rr=rr, risk_pct=plan.risk_pct, confidence=round(confidence, 4), confidence_basis=basis,
        expected_r=round(expected_r(confidence, rr, plan.risk_pct), 3), kelly=round(kelly(confidence, rr), 4),
        setup=c.setup, kind=c.zone.kind, bucket=c.bucket, features=c.features,
        expires_at=bar_time + step + ENTRY_BARS[interval] * step, hold_seconds=HOLD_BARS[interval] * step,
        notes=c.notes, updated_at=now)


def pct(a: float, b: float) -> float:
    return (b / a - 1) * 100 if a else 0.0


def call_text(c: DeskCall) -> str:
    """The Discord / Telegram message for a new call."""
    base = c.symbol.removesuffix("USDT")
    tfl = TF_LABEL.get(c.interval, c.interval)
    zone = f"{_fmt(c.zone_low)}–{_fmt(c.zone_high)}"
    size = (f"Paper: {c.notional:,.2f} USDT ({c.size_pct:g}% of the agent's wallet)." if c.notional
            else f"Paper: {c.paper_note or 'nothing placed.'}")
    return (f"Agent call · {base} {tfl}: buy zone {zone} (limit {_fmt(c.entry)}), take-profit {_fmt(c.tp)} "
            f"({pct(c.entry, c.tp):+.1f}%), wrong below {_fmt(c.stop)} ({pct(c.entry, c.stop):+.1f}%).\n"
            f"{c.setup}. Confidence {c.confidence * 100:.0f}% of the take-profit first ({c.rr:g}R, expected "
            f"{c.expected_r:+.2f}R). {c.confidence_basis}\n{size}")


def outcome_text(c: DeskCall, record: Optional[str] = None) -> str:
    """The message when a call fills, closes or expires."""
    base = c.symbol.removesuffix("USDT")
    head = f"Agent call · {base} {TF_LABEL.get(c.interval, c.interval)}"
    tail = f" {record}" if record else ""
    if c.status == "open":
        return f"{head}: bought at {_fmt(c.fill_price or c.entry)}. Take-profit {_fmt(c.tp)}, wrong below {_fmt(c.stop)}."
    if c.status == "tp":
        return (f"{head}: take-profit {_fmt(c.tp)} reached, {c.r:+.2f}R ({pct(c.fill_price or c.entry, c.tp):+.1f}%). "
                f"It was called at {c.confidence * 100:.0f}%.{tail}")
    if c.status == "invalidated":
        return (f"{head}: invalidated at {_fmt(c.exit_price or c.stop)}, {c.r:+.2f}R. It was called at "
                f"{c.confidence * 100:.0f}%.{tail}")
    if c.status == "timed_out":
        return (f"{head}: neither level within the hold window; closed at {_fmt(c.exit_price or 0)}, "
                f"{(c.r or 0):+.2f}R.{tail}")
    if c.status == "expired":
        return f"{head}: the buy at {_fmt(c.entry)} didn't fill in time; the call expired."
    return f"{head}: {c.status}."

