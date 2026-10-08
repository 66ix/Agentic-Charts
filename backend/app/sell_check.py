"""Sell or trim: the spot holder's answer to a short signal.

Spot traders can't short, but they can sell or trim what they hold. `check` reads one coin on its timeframe and on
the daily and flags it when:

  * Lost support (sell): the last LOST_CONFIRM closes are below a support or demand zone that price closed above in
    the LOOKBACK bars before them. The broken zone is now overhead, so the suggestion is to sell or trim on a retest
    of it from below, and the next support is where price may head.
  * At resistance (trim): price is inside a resistance or supply zone, or within NEAR_ATR ATRs under it. A rejection
    candle (long upper wick, or a red close), RSI at or above RSI_HOT, or the daily zone in the same place make it
    stronger. The suggestion is to trim into the zone.

Every price comes from the detectors in ta_agent.py. A coin with neither is not flagged.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal, Optional

import pandas as pd
from .indicators import rsi as rsi_series
from .market_data import MarketData, candles_to_df
from .pricefmt import _fmt
from .schemas import SellSignal
from .scanner import change_24h
from .ta_agent import TF_LABEL, Zone, atr, cluster_levels, find_swings, supply_demand_zones

log = logging.getLogger(__name__)

TIMEFRAMES = ("1h", "4h", "1d")
DEFAULT_TF = "4h"
CANDLES = 300
MIN_CANDLES = 60
LOOKBACK = 8
LOST_CONFIRM = 2
MAX_LOST_ATR = 3.0  # a support broken further back than this is old news, not a fresh sell signal
NEAR_ATR = 0.5
RSI_HOT = 70.0
MIN_TOUCHES = 2


def _zones(df: pd.DataFrame) -> tuple[list[Zone], float]:
    """S/R zones with MIN_TOUCHES+ touches and supply/demand zones on one frame, and its ATR."""
    df = df.reset_index(drop=True)
    atr_s = atr(df)
    atr_v = float(atr_s.iloc[-1])
    last = float(df["close"].iloc[-1])
    highs, lows = find_swings(df, atr_v)
    sr = [z for z in cluster_levels(highs + lows, atr_v, last, len(df)) if z.touches >= MIN_TOUCHES]
    return sr + supply_demand_zones(df, atr_s), atr_v


def _lost(df: pd.DataFrame, zones: list[Zone], atr_v: float, fresh: bool = False) -> Optional[Zone]:
    """The strongest zone price closed above in the LOOKBACK bars before the last LOST_CONFIRM, and below since.
    `fresh`: only a zone lost on the last candle, i.e. the close before the last LOST_CONFIRM was still above it (the
    signal alert fires once, when the break is confirmed)."""
    closes = df["close"].reset_index(drop=True)
    if len(closes) < LOOKBACK + LOST_CONFIRM + 20:
        return None
    recent = closes.iloc[-LOST_CONFIRM:]
    before = closes.iloc[-(LOOKBACK + LOST_CONFIRM):-LOST_CONFIRM]
    held = closes.iloc[-(LOOKBACK + LOST_CONFIRM + 20):-LOST_CONFIRM]  # it acted as support: most closes above it
    last = float(closes.iloc[-1])
    hits = [z for z in zones
            if (recent < z.price_low).all() and float(before.max()) >= z.price_low
            and float((held >= z.price_low).mean()) >= 0.6 and z.price_low - last <= MAX_LOST_ATR * atr_v
            and (not fresh or float(closes.iloc[-(LOST_CONFIRM + 1)]) >= z.price_low)]
    return max(hits, key=lambda z: z.score, default=None)


def _overhead(zones: list[Zone], last: float, atr_v: float) -> Optional[Zone]:
    """The nearest resistance or supply zone price is inside or just under."""
    near = [z for z in zones if z.kind in ("resistance", "supply") and z.price_high >= last
            and z.price_low - last <= NEAR_ATR * atr_v]
    return min(near, key=lambda z: max(0.0, z.price_low - last), default=None)


def _rejection(df: pd.DataFrame, z: Zone, rsi_v: Optional[float]) -> tuple[bool, bool]:
    """(rejected, hot) for the last candle under resistance or supply `z`: a rejection is a candle that reached into
    the zone and left a long upper wick; hot is RSI at or above RSI_HOT."""
    bar = df.iloc[-1]
    rng = float(bar["high"] - bar["low"]) or 1e-12
    wick = float(bar["high"] - max(bar["open"], bar["close"])) / rng
    rejected = float(bar["high"]) >= z.price_low and wick >= 0.5
    return rejected, rsi_v is not None and rsi_v >= RSI_HOT


def _support_below(zones: list[Zone], last: float) -> Optional[float]:
    below = [z.price_high for z in zones if z.kind in ("support", "demand") and z.price_high < last * 0.998]
    return max(below, default=None)


def check(symbol: str, interval: str, df: pd.DataFrame, daily: Optional[pd.DataFrame], source: str) -> Optional[
        SellSignal]:
    """One coin: a sell or trim signal, or None when it holds up. CPU-bound."""
    if len(df) < MIN_CANDLES:
        return None
    df = df.reset_index(drop=True)
    tfl = TF_LABEL.get(interval, interval.upper())
    last = float(df["close"].iloc[-1])
    zones, atr_v = _zones(df)
    frames: list[tuple[str, list[Zone], pd.DataFrame, float]] = [(tfl, zones, df, atr_v)]
    if daily is not None and interval != "1d" and len(daily) >= MIN_CANDLES:
        dz, datr = _zones(daily)
        frames.insert(0, ("D1", dz, daily.reset_index(drop=True), datr))
    every = [z for _, zs, _, _ in frames for z in zs]
    r = rsi_series(df["close"]).iloc[-1]
    rsi_v = round(float(r), 1) if pd.notna(r) else None
    support = _support_below(every, last)
    drop = round((support / last - 1) * 100, 2) if support else None
    base = dict(symbol=symbol, interval=interval, last_price=last, change_pct=change_24h(df), rsi=rsi_v,
                support_below=support, drop_pct=drop, data_source=source)

    # Lost support, higher timeframe first. The daily is checked on its own closes.
    for label, zs, frame, a in frames:
        z = _lost(frame, zs, a)
        if z is None:
            continue
        score = 0.7 + (0.2 if label == "D1" else 0) + min(z.touches, 5) * 0.02
        was = "demand" if z.kind in ("supply", "demand") else "support"
        up = (z.price_low / last - 1) * 100
        reason = (f"Lost {label} {was} {_fmt(z.price_low)}–{_fmt(z.price_high)}: closed below it "
                  f"{LOST_CONFIRM} times. Sell or trim on a retest from below ({up:+.1f}%)")
        reason += f"; next support {_fmt(support)} ({drop:+.1f}%)." if support else "."
        return SellSignal(**base, action="sell", reason=reason, sell_low=z.price_low, sell_high=z.price_high,
                          zone=f"{label} {was}", score=round(score, 3))

    # At resistance, the daily zone first when both are there.
    for label, zs, _, a in frames:
        z = _overhead(zs, last, a)
        if z is None:
            continue
        rejected, hot = _rejection(df, z, rsi_v)
        if not (rejected or hot):
            continue  # sitting under resistance alone is not a reason to sell
        why = [w for w, on in (("a rejection wick", rejected), (f"RSI at {rsi_v}", hot)) if on]
        where = "inside" if z.price_low <= last else "just under"
        score = 0.35 + (0.15 if label == "D1" else 0) + (0.15 if rejected else 0) + (0.1 if hot else 0)
        reason = (f"Price is {where} {label} {z.kind} {_fmt(z.price_low)}–{_fmt(z.price_high)} with "
                  f"{' and '.join(why)}. Trim into the zone")
        reason += f"; next support {_fmt(support)} ({drop:+.1f}%)." if support else "."
        return SellSignal(**base, action="trim", reason=reason, sell_low=z.price_low, sell_high=z.price_high,
                          zone=f"{label} {z.kind}", score=round(score, 3))
    return None


def alert_check(df: pd.DataFrame, signal: str) -> Optional[tuple[Zone, str]]:
    """The signal alerts' version of `check`, on one frame of closed candles: `lost_support` when a support or demand
    zone was lost on the last candle, `at_resistance` when the last candle is at resistance or supply with a rejection
    wick or hot RSI. Returns the zone and the text, or None. CPU-bound."""
    if len(df) < MIN_CANDLES:
        return None
    df = df.reset_index(drop=True)
    zones, atr_v = _zones(df)
    last = float(df["close"].iloc[-1])
    support = _support_below(zones, last)
    nxt = f"; next support {_fmt(support)} ({(support / last - 1) * 100:+.1f}%)." if support else "."
    if signal == "lost_support":
        z = _lost(df, zones, atr_v, fresh=True)
        if z is None:
            return None
        was = "demand" if z.kind in ("supply", "demand") else "support"
        return z, (f"lost {was} {_fmt(z.price_low)}–{_fmt(z.price_high)}: closed below it {LOST_CONFIRM} times "
                   f"(close {_fmt(last)}). Sell or trim on a retest from below "
                   f"({(z.price_low / last - 1) * 100:+.1f}%){nxt}")
    if signal == "at_resistance":
        z = _overhead(zones, last, atr_v)
        if z is None:
            return None
        r = rsi_series(df["close"]).iloc[-1]
        rsi_v = round(float(r), 1) if pd.notna(r) else None
        rejected, hot = _rejection(df, z, rsi_v)
        if not (rejected or hot):
            return None
        why = " and ".join(w for w, on in (("a rejection wick", rejected), (f"RSI at {rsi_v}", hot)) if on)
        where = "inside" if z.price_low <= last else "just under"
        return z, (f"{where} {z.kind} {_fmt(z.price_low)}–{_fmt(z.price_high)} with {why} (close {_fmt(last)}). "
                   f"Trim into the zone{nxt}")
    raise ValueError(f"Unknown sell signal {signal!r}")


def timeframe_for(tf: str) -> str:
    return tf if tf in TIMEFRAMES else DEFAULT_TF


async def sell_scan(market: MarketData, symbols: list[str], interval: str) -> list[SellSignal]:
    """Sell or trim signals for `symbols` on `interval` (with the daily), strongest first."""
    interval = timeframe_for(interval)
    sem = asyncio.Semaphore(6)

    async def one(sym: str) -> Optional[SellSignal]:
        async with sem:
            try:
                candles, source = await market.get_klines(sym, interval, CANDLES)
                daily = None
                if interval != "1d":
                    daily = candles_to_df((await market.get_klines(sym, "1d", CANDLES))[0])
            except Exception as exc:  # unknown symbol, network
                log.info("Sell check skipped %s: %s", sym, exc)
                return None
        return await asyncio.to_thread(check, sym, interval, candles_to_df(candles), daily, source)

    rows = await asyncio.gather(*(one(s) for s in symbols[:40]))
    return sorted((r for r in rows if r is not None), key=lambda r: -r.score)


def sell_facts(rows: list[SellSignal], checked: int) -> dict:
    return {"checked": checked, "flagged": [r.model_dump(include={"symbol", "action", "reason", "sell_low", "sell_high",
                                                                  "support_below", "drop_pct", "rsi"}) for r in rows[:6]]}


def describe_sells(rows: list[SellSignal], checked: int, interval: str) -> str:
    if not rows:
        return (f"None of the {checked} coins checked is losing support or sitting at resistance on "
                f"{TF_LABEL.get(interval, interval)}; nothing to sell or trim right now.")
    parts = [f"{r.symbol} ({r.action}): {r.reason}" for r in rows[:3]]
    return " ".join(parts)
