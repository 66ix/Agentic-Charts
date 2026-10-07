"""Trigger alerts: a lower-timeframe confirmation inside a higher-timeframe zone ("alert me when 1m shows a CHoCH
inside the 4h demand").

The zone is fixed (an AI zone or rectangle picked on the chart, a trade plan's zone) or found by the detectors on a
higher timeframe (the nearest fresh 4h demand), looked up again whenever that timeframe closes. On every closed 1m,
5m or 15m candle the signal-alert service (signal_alerts.py) asks `trigger_hit` whether that candle confirmed a live
touch of the zone:

* A touch starts with the first candle whose range reaches into the zone and stays live while price keeps coming
  back within TOUCH_GAP candles. A close through the far side (below a demand zone) ends it; a later reclaim starts
  a new touch.
* The confirmation points the zone's way (up from demand / support, down from supply / resistance): `choch` is a
  close through the last LTF swing (CHoCH or BOS, `indicators.structure_breaks`), `sweep` a wick through an LTF
  swing that closes back (`patterns.liquidity_sweeps`), `engulfing` a candle whose body engulfs the previous,
  opposite one; `any` takes the first of them. The close must be within NEAR_ATR LTF ATRs of the zone.
* One fire per touch (an earlier fire in the same touch blocks the rest), and at most one per `cooldown`.

The suggested stop sits under the touch's lowest low (over its highest high for a short) by STOP_BUFFER_ATR; every
price in the message is a candle's or the zone's. Detection is pure; `scan_triggers` replays it bar by bar for the
preview, each bar seeing only the candles up to it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from .alerts import fmt_price
from .indicators import structure_breaks
from .kimi_service import closed_only
from .market_data import INTERVAL_SECONDS, candles_to_df
from .patterns import liquidity_sweeps
from .schemas import Confirmation, TriggerZone, ZoneTriggerIntent, ZoneTriggerSpec
from .ta_agent import TF_LABEL, atr, cluster_levels, find_swings, supply_demand_zones

if TYPE_CHECKING:
    from .market_data import MarketData

TOUCH_GAP = 12          # candles away from the zone before a touch is over
NEAR_ATR = 3.0          # the confirming close must be this close to the zone (LTF ATRs)
STOP_BUFFER_ATR = 0.1   # the suggested stop's distance beyond the touch's extreme
MIN_BARS = 40           # fewer LTF candles and the swing detectors have nothing to work with
MIN_ENGULF_ATR = 0.3    # an engulfing body smaller than this is noise
HTF_BARS = 300          # higher-timeframe candles the zone detectors see
WINDOW = 300            # LTF candles each check sees, like the other signal alerts

CONFIRM_NAMES = {"choch": "CHoCH / BOS", "sweep": "Liquidity sweep", "engulfing": "Engulfing close",
                 "any": "Any confirmation"}
CONFIRM_PHRASES = {"choch": "CHoCH / BOS", "sweep": "liquidity sweep", "engulfing": "engulfing close",
                   "any": "confirmation"}
LONG_KINDS = {"demand", "support"}


class ZoneTrigger(BaseModel):
    """What a stored trigger alert watches (SignalAlert.trigger). `zone_low` / `zone_high` / `zone_label` are the
    zone it watches now: the fixed one, or the detected one as of the last higher-timeframe close."""

    zone: TriggerZone
    confirm: Confirmation = "any"
    cooldown_min: int = Field(60, ge=0, le=1440)
    zone_low: Optional[float] = None
    zone_high: Optional[float] = None
    zone_label: str = ""


@dataclass(frozen=True)
class ZoneBand:
    low: float
    high: float
    direction: str  # long | short
    label: str      # e.g. "H4 demand"


@dataclass(frozen=True)
class TriggerHit:
    time: int     # open time of the confirming candle, UNIX seconds
    price: float  # its close
    text: str     # plain English, without the "BTCUSDT 5m: " prefix
    key: str
    stop: float   # suggested stop beyond the touch's extreme
    confirm: str  # which confirmation fired: choch | sweep | engulfing
    touch: int    # open time of the first candle of the touch


def _r6(x: float) -> float:
    return float(f"{x:.6g}")


def band_text(z: ZoneBand) -> str:
    return f"{z.label} {fmt_price(z.low)}–{fmt_price(z.high)}"


# -------------------------------------------------------------- detection --


def touch_start(df: pd.DataFrame, zone: ZoneBand, gap: int = TOUCH_GAP) -> Optional[int]:
    """Index of the first candle of the touch that is live at the last candle, or None. See the module docstring."""
    l, h, c = (df[k].to_numpy(dtype=float) for k in ("low", "high", "close"))
    n = len(c)
    overlap = (l <= zone.high) & (h >= zone.low)
    broken = np.flatnonzero(c < zone.low if zone.direction == "long" else c > zone.high)
    after = int(broken[-1]) + 1 if broken.size else 0
    idx = np.flatnonzero(overlap[after:]) + after
    if not idx.size or n - 1 - int(idx[-1]) > gap:
        return None
    k = len(idx) - 1
    while k > 0 and idx[k] - idx[k - 1] <= gap:
        k -= 1
    return int(idx[k])


def _confirmation(df: pd.DataFrame, atr_v: float, long: bool, which: str, tfl: str) -> Optional[tuple[str, str]]:
    """Did the last candle confirm the zone's direction? → (confirmation id, what happened), first match wins."""
    n = len(df)
    t = int(df["time"].iloc[-1])
    direction = "bullish" if long else "bearish"
    highs: list = []
    lows: list = []
    if which in ("choch", "sweep", "any"):
        highs, lows = find_swings(df, atr_v)
    if which in ("choch", "any"):
        hits = [b for b in structure_breaks(df, highs, lows) if b["idx"] == n - 1 and b["direction"] == direction]
        if hits:
            b = hits[-1]
            where = "above the swing high" if long else "below the swing low"
            return "choch", f"{tfl} {direction} {b['type']} (closed {where} {fmt_price(b['level'])})"
    if which in ("sweep", "any"):
        for sw in liquidity_sweeps(df, highs, lows):
            if sw["time"] == t and sw["direction"] == direction:
                side = "low" if long else "high"
                back = "above" if long else "below"
                return "sweep", (f"{tfl} sweep of the {side} {fmt_price(sw['level'])} (wick to "
                                 f"{fmt_price(sw['extreme'])}, closed back {back})")
    if which in ("engulfing", "any") and n >= 2:
        o0, c0 = float(df["open"].iloc[-2]), float(df["close"].iloc[-2])
        o1, c1 = float(df["open"].iloc[-1]), float(df["close"].iloc[-1])
        if abs(c1 - o1) >= MIN_ENGULF_ATR * atr_v:
            bull = c0 < o0 and c1 > o1 and c1 > o0 and o1 <= c0
            bear = c0 > o0 and c1 < o1 and c1 < o0 and o1 >= c0
            if bull if long else bear:
                return "engulfing", f"{tfl} {direction} engulfing candle"
    return None


def trigger_hit(df: pd.DataFrame, zone: ZoneBand, confirm: str, last_fire: Optional[int] = None,
                cooldown_s: int = 0, tf: str = "") -> Optional[TriggerHit]:
    """Did the last candle of `df` (closed LTF candles, oldest first) confirm a live touch of `zone`? `last_fire`
    is the open time of the candle this alert last fired on: a fire earlier in the same touch, or within
    `cooldown_s` seconds, blocks this one."""
    df = df.reset_index(drop=True)
    n = len(df)
    if n < MIN_BARS:
        return None
    s = touch_start(df, zone)
    if s is None:
        return None
    times = df["time"].to_numpy()
    t = int(times[-1])
    if last_fire is not None and (last_fire >= int(times[s]) or t - last_fire < cooldown_s):
        return None
    long = zone.direction == "long"
    atr_v = float(atr(df).iloc[-1])
    close = float(df["close"].iloc[-1])
    away = max(0.0, close - zone.high) if long else max(0.0, zone.low - close)
    if atr_v <= 0 or away > NEAR_ATR * atr_v:
        return None
    tfl = TF_LABEL.get(tf, tf.upper())
    got = _confirmation(df, atr_v, long, confirm, tfl)
    if got is None:
        return None
    which, what = got
    extreme = float(df["low"].iloc[s:].min()) if long else float(df["high"].iloc[s:].max())
    stop = _r6(extreme - STOP_BUFFER_ATR * atr_v if long else extreme + STOP_BUFFER_ATR * atr_v)
    swing = f"{tfl} swing low {fmt_price(extreme)}" if long else f"{tfl} swing high {fmt_price(extreme)}"
    inside = float(df["low"].iloc[-1]) <= zone.high and float(df["high"].iloc[-1]) >= zone.low
    text = (f"{what} {'inside' if inside else 'after touching'} {band_text(zone)}, close {fmt_price(close)}. "
            f"Suggested stop {fmt_price(stop)}, {'under' if long else 'over'} the {swing}.")
    return TriggerHit(t, close, text, f"trigger:{int(times[s])}:{t}", stop, which, int(times[s]))


def scan_triggers(df: pd.DataFrame, zone: ZoneBand, confirm: str, bars: int, cooldown_s: int = 0, tf: str = "",
                  window: int = WINDOW) -> list[TriggerHit]:
    """Every candle among the last `bars` of `df` where the trigger would have fired, each judged on the `window`
    candles up to it with the earlier fires remembered, as the live check would have. Oldest first."""
    df = df.reset_index(drop=True)
    n = len(df)
    hits: list[TriggerHit] = []
    for k in range(max(MIN_BARS - 1, n - bars), n):
        hit = trigger_hit(df.iloc[max(0, k + 1 - window):k + 1], zone, confirm,
                          hits[-1].time if hits else None, cooldown_s, tf)
        if hit:
            hits.append(hit)
    return hits


# ------------------------------------------------------------------ zones --


def find_zone(df: pd.DataFrame, kind: str, tf: str, fresh_only: bool = True) -> Optional[ZoneBand]:
    """The nearest zone of `kind` on closed higher-timeframe candles: demand / support at or below the last close,
    supply / resistance at or above it (a zone price is inside counts as nearest); `any` = the nearer of demand
    and supply. Demand and supply zones tested more than once are skipped with `fresh_only`."""
    df = df.reset_index(drop=True)
    if len(df) < 30:
        return None
    atr_s = atr(df)
    atr_v = float(atr_s.iloc[-1])
    last = float(df["close"].iloc[-1])
    tfl = TF_LABEL.get(tf, tf.upper())
    found: list[tuple[float, ZoneBand]] = []

    def add(lo: float, hi: float, k: str, suffix: str = "") -> None:
        long = k in LONG_KINDS
        if (lo > last) if long else (hi < last):
            return  # demand above price / supply below it: the wrong side
        dist = max(0.0, last - hi) if long else max(0.0, lo - last)
        found.append((dist, ZoneBand(_r6(lo), _r6(hi), "long" if long else "short", f"{tfl} {k}{suffix}")))

    if kind in ("demand", "supply", "any"):
        for z in supply_demand_zones(df, atr_s):
            if (kind == "any" or z.kind == kind) and not (fresh_only and z.tests > 1):
                add(z.price_low, z.price_high, z.kind, " (untested)" if z.tests == 0 else "")
    else:
        highs, lows = find_swings(df, atr_v)
        for z in cluster_levels(highs + lows, atr_v, last, len(df)):
            if z.kind == kind:
                add(z.price_low, z.price_high, z.kind)
    return min(found, key=lambda f: f[0])[1] if found else None


def fixed_band(zone: TriggerZone) -> ZoneBand:
    """A fixed zone as a band (its direction must be set)."""
    lo, hi = float(zone.price_low or 0), float(zone.price_high or 0)
    tfl = TF_LABEL.get(zone.timeframe or "", "")
    label = zone.label or (f"{tfl} zone".strip() if tfl else "zone")
    return ZoneBand(lo, hi, zone.direction or "long", label)


async def detect_now(market: MarketData, symbol: str, zone: TriggerZone) -> tuple[Optional[ZoneBand], str, int]:
    """Look up a detected zone on the closed higher-timeframe candles → (zone or None, data source, open time of
    the newest closed candle)."""
    tf = zone.timeframe or "4h"
    candles, source = await market.get_klines(symbol, tf, HTF_BARS + 1)
    closed = closed_only(candles, tf)
    if not closed:
        return None, source, 0
    band = find_zone(candles_to_df(closed), zone.kind, tf, zone.fresh_only)
    return band, source, closed[-1].time


def zone_phrase(zone: TriggerZone) -> str:
    """"the nearest fresh H4 demand", "H4 demand 23.90–24.20"."""
    if zone.source == "detected":
        tfl = TF_LABEL.get(zone.timeframe or "", zone.timeframe or "")
        kind = "demand or supply" if zone.kind == "any" else zone.kind
        fresh = "fresh " if zone.fresh_only and zone.kind in ("demand", "supply", "any") else ""
        return f"the nearest {fresh}{tfl} {kind}"
    return band_text(fixed_band(zone))


def describe_trigger(spec: ZoneTriggerSpec, now: Optional[ZoneBand] = None) -> str:
    """e.g. "M1 CHoCH / BOS inside the nearest fresh H4 demand (now 61,200.00–61,800.00)"."""
    tfl = TF_LABEL.get(spec.interval, spec.interval)
    out = f"{tfl} {CONFIRM_PHRASES[spec.confirm]} inside {zone_phrase(spec.zone)}"
    if spec.zone.source == "detected":
        out += f" (now {fmt_price(now.low)}–{fmt_price(now.high)})" if now else " (none on that side of price right now)"
    return out


def spec_from_intent(zt: ZoneTriggerIntent, symbol: str, chart_tf: str) -> ZoneTriggerSpec:
    """The trigger alert the agent sets up for "alert me when 1m shows a CHoCH inside the 4h demand". Without a
    zone timeframe it uses the chart's, unless that is not above the trigger's (then 4h)."""
    htf = zt.zone_timeframe or chart_tf
    if INTERVAL_SECONDS.get(htf, 0) <= INTERVAL_SECONDS[zt.timeframe]:
        htf = "4h"
    zone = TriggerZone(source="detected", timeframe=htf, kind=zt.zone_kind)  # type: ignore[arg-type]
    return ZoneTriggerSpec(symbol=symbol, interval=zt.timeframe, zone=zone, confirm=zt.confirm)
