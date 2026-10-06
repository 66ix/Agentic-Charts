"""Backtest a setup: run the chart agent's own detectors over history and score every trade they would have given.

"How did longs from fresh H4 demand do on SOL over the last year?" The same functions that draw zones on the chart
(`supply_demand_zones`, `cluster_levels`, `liquidity_sweeps`, ...) are re-run on a rolling window as the candles
unfold, so the backtest trades exactly what the agent would have shown at the time:

* No lookahead. At candle i only candles up to i exist. Detectors run every `STEP` candles on the last `WINDOW`
  candles up to that point (ATR is causal too), and their zones become tradable from the next candle. Appending
  newer candles never changes an earlier trade (there is a test for that).
* Zone setups (demand/supply, support/resistance) trade the first touch of a zone found before the touch, with a
  limit order at the near edge and the stop beyond the far edge plus a buffer. A zone the agent would draw is armed
  when found (supply/demand only while untested, "fresh"), and is used up by its first touch whether or not a trade
  was taken. Sweep setups enter at the close of the candle that swept a swing level and closed back inside, with
  the stop beyond its wick.
* One trade at a time. Exits are checked candle by candle: stop first when the stop and target share a candle, and
  a time exit at the close after `max_hold_bars`. On the candle a limit order fills only the stop counts.
* R is measured against the actual entry-to-stop distance and is after fees (`fee_pct` per side).

Kimi Cooked setups instead replay the indicator's own resolved signals (entry, result and R as Kimi scores them,
with its own exit rules and costs), like its Signal Stats table.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Optional, Sequence

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, field_validator

from .kimi import KimiCooked
from .kimi_service import HISTORY_DAYS, LABELS, TYPE_NAMES, closed_only, kimi_inputs
from .market_data import DERIVED_INTERVALS, INTERVAL_SECONDS, MAX_KLINES, candles_to_df
from .patterns import liquidity_sweeps
from .schemas import Candle, Interval, norm_symbol
from .ta_agent import TF_LABEL, Swing, atr, cluster_levels, find_swings, pick_nearest, supply_demand_zones

if TYPE_CHECKING:
    from .kimi_service import KimiService
    from .market_data import MarketData

log = logging.getLogger(__name__)

WINDOW = 400   # candles the detectors see at each step
STEP = 5       # re-detect every this many candles
WARMUP = 60    # first candle that can trade (detectors need some history)
PER_SIDE = 2   # zones per side of price, as the agent draws them (AnalysisIntent.max_zones default)
MIN_TARGET_R = 0.8  # "next_level" skips levels closer than this, like trade_plan.py
SMALL_SAMPLE = 20

Setup = Literal["demand_long", "supply_short", "support_long", "resistance_short", "sweep_long", "sweep_short",
                "kimi_long", "kimi_short", "kimi_any"]
SETUP_NAMES: dict[str, str] = {
    "demand_long": "Long from fresh demand",
    "supply_short": "Short from fresh supply",
    "support_long": "Long at support",
    "resistance_short": "Short at resistance",
    "sweep_long": "Long after a sweep of a low",
    "sweep_short": "Short after a sweep of a high",
    "kimi_long": "Kimi Cooked long signals",
    "kimi_short": "Kimi Cooked short signals",
    "kimi_any": "All Kimi Cooked signals",
}
TARGET_R = {"1.5R": 1.5, "2R": 2.0, "3R": 3.0}

_RUNS = asyncio.Semaphore(2)  # backtests are CPU-bound: a couple at a time keeps the streams responsive


# ------------------------------------------------------------------ models --


class BacktestRequest(BaseModel):
    symbol: str = Field(..., min_length=2, max_length=20)
    interval: Interval = "4h"
    setup: Setup
    bars: int = Field(2000, ge=100, le=5000, description="Closed candles to test on, newest last")
    stop_buffer_atr: float = Field(0.25, ge=0, le=5, description="Stop beyond the zone or wick by this many ATRs")
    target: Literal["1.5R", "2R", "3R", "next_level"] = "2R"
    max_hold_bars: int = Field(50, ge=1, le=2000, description="Exit at the close after this many candles")
    fee_pct: float = Field(0.1, ge=0, le=1, description="Fee per side, % of notional")

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, v: str) -> str:
        s = norm_symbol(v)
        if not s.isalnum() or not 5 <= len(s) <= 20:
            raise ValueError("invalid symbol")
        return s


class BacktestTrade(BaseModel):
    direction: Literal["long", "short"]
    entry_time: int
    entry: float
    stop: float
    target: float
    exit_time: int
    exit: float
    r: float = Field(..., description="After fees")
    result: Literal["win", "loss", "timeout"]
    bars_held: int
    basis: str = Field("", description="What the trade was built on, e.g. 'H4 demand 23.9–24.2'")


class BacktestStats(BaseModel):
    count: int = 0
    wins: int = 0
    losses: int = 0
    timeouts: int = 0
    win_rate: Optional[float] = Field(None, description="wins / trades (timeouts count as trades)")
    avg_r: Optional[float] = None
    total_r: float = 0.0
    expectancy: Optional[float] = Field(None, description="Average R per trade: share of positive trades × their "
                                                         "average + share of the rest × theirs")
    profit_factor: Optional[float] = Field(None, description="Sum of winning R / sum of losing R")
    max_drawdown_r: float = 0.0
    avg_bars_held: Optional[float] = None
    avg_win_r: Optional[float] = None
    avg_loss_r: Optional[float] = None
    best_r: Optional[float] = None
    worst_r: Optional[float] = None


class EquityPoint(BaseModel):
    time: int
    r_cum: float


class BacktestResult(BaseModel):
    symbol: str
    interval: str
    setup: str
    setup_name: str
    bars: int
    from_time: int
    to_time: int
    data_source: str
    target: str
    trades: list[BacktestTrade]
    stats: BacktestStats
    equity: list[EquityPoint]
    notes: list[str] = Field(default_factory=list)
    seconds: float = 0.0


# ------------------------------------------------------------------ engine --


@dataclass
class _Armed:
    key: tuple
    lo: float
    hi: float
    label: str


@dataclass
class _Open:
    direction: str
    idx: int
    entry: float
    stop: float
    target: float
    basis: str


@dataclass
class _State:
    armed: dict[tuple, _Armed] = field(default_factory=dict)
    used: list[tuple[float, float, int]] = field(default_factory=list)  # (lo, hi, candle) of zones already touched
    swing_highs: list[tuple[int, int, float]] = field(default_factory=list)  # (index, time, price)
    swing_lows: list[tuple[int, int, float]] = field(default_factory=list)
    level_prices: dict[str, list[float]] = field(default_factory=lambda: {"long": [], "short": []})


def _fmt(p: float) -> str:
    if p >= 1000:
        return f"{p:,.2f}"
    return f"{p:.4f}".rstrip("0").rstrip(".") if p >= 1 else f"{p:.8f}".rstrip("0").rstrip(".")


def _overlap(a_lo: float, a_hi: float, b_lo: float, b_hi: float) -> float:
    inter = min(a_hi, b_hi) - max(a_lo, b_lo)
    span = min(a_hi - a_lo, b_hi - b_lo)
    return max(0.0, inter) / span if span > 0 else 0.0


def _r(direction: str, entry: float, stop: float, exit_px: float, fee_pct: float) -> float:
    risk = abs(entry - stop)
    sign = 1.0 if direction == "long" else -1.0
    return ((exit_px - entry) * sign - (entry + exit_px) * fee_pct / 100) / risk


def backtest_frame(df: pd.DataFrame, setup: str, interval: str = "4h", stop_buffer_atr: float = 0.25,
                   target: str = "2R", max_hold_bars: int = 50, fee_pct: float = 0.1) -> tuple[list[BacktestTrade],
                                                                                              list[str]]:
    """Walk closed candles (time/open/high/low/close/volume, oldest first) and return the setup's trades plus notes.
    Pure and deterministic. Detector setups only; Kimi setups go through `kimi_trades`."""
    if setup not in SETUP_NAMES or setup.startswith("kimi"):
        raise ValueError(f"Unknown detector setup {setup!r}")
    df = df.reset_index(drop=True)
    n = len(df)
    t = df["time"].to_numpy(dtype=np.int64)
    o, h, l, c = (df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    atr_s = atr(df)
    a = atr_s.to_numpy(dtype=float)
    long = setup.endswith("_long")
    side = "long" if long else "short"
    sign = 1.0 if long else -1.0
    family = setup.rsplit("_", 1)[0]  # demand / supply / support / resistance / sweep
    tfl = TF_LABEL.get(interval, interval.upper())
    need_levels = target == "next_level"

    st = _State()
    trades: list[BacktestTrade] = []
    notes: list[str] = []
    pos: Optional[_Open] = None
    fallback_targets = 0

    def target_for(entry: float, stop: float) -> tuple[float, str]:
        nonlocal fallback_targets
        risk = abs(entry - stop)
        if target in TARGET_R:
            return entry + sign * TARGET_R[target] * risk, ""
        far = [p for p in st.level_prices[side] if (p - entry) * sign >= MIN_TARGET_R * risk]
        if far:
            return (min(far) if long else max(far)), ""
        fallback_targets += 1
        return entry + sign * 2.0 * risk, " (no level beyond 0.8R, target 2R)"

    def close(j: int, px: float, result: str) -> None:
        nonlocal pos
        assert pos is not None
        trades.append(BacktestTrade(
            direction=pos.direction, entry_time=int(t[pos.idx]), entry=round(pos.entry, 10),
            stop=round(pos.stop, 10), target=round(pos.target, 10), exit_time=int(t[j]), exit=round(px, 10),
            r=round(_r(pos.direction, pos.entry, pos.stop, px, fee_pct), 4), result=result,  # type: ignore[arg-type]
            bars_held=j - pos.idx, basis=pos.basis))
        pos = None

    def stop_hit(j: int) -> Optional[float]:
        """Exit price if candle j reaches the open trade's stop (at the open when it gapped through)."""
        assert pos is not None
        if (l[j] <= pos.stop) if long else (h[j] >= pos.stop):
            return min(o[j], pos.stop) if long else max(o[j], pos.stop)
        return None

    def recompute(i: int) -> None:
        lo_i = max(0, i - WINDOW + 1)
        win = df.iloc[lo_i:i + 1].reset_index(drop=True)
        atr_w = atr_s.iloc[lo_i:i + 1].reset_index(drop=True)
        atr_v = float(a[i])
        last = float(c[i])
        if atr_v <= 0 or not math.isfinite(atr_v):
            return
        highs: list[Swing] = []
        lows: list[Swing] = []
        if family in ("support", "resistance", "sweep") or need_levels:
            highs, lows = find_swings(win, atr_v)
            st.swing_highs = [(lo_i + s.idx, s.time, s.price) for s in highs]
            st.swing_lows = [(lo_i + s.idx, s.time, s.price) for s in lows]
        clusters = (cluster_levels(highs + lows, atr_v, last, len(win))
                    if (highs or lows) and (family in ("support", "resistance") or need_levels) else [])
        if need_levels:
            st.level_prices["long"] = [z.price_low for z in clusters] + [s.price for s in highs]
            st.level_prices["short"] = [z.price_high for z in clusters] + [s.price for s in lows]
        zones = []
        if family in ("demand", "supply"):
            sd = [z for z in supply_demand_zones(win, atr_w) if z.tests == 0]  # fresh: untouched since it formed
            zones = [z for z in pick_nearest(sd, last, PER_SIDE) if z.kind == family]
        elif family in ("support", "resistance"):
            zones = [z for z in pick_nearest(clusters, last, PER_SIDE) if z.kind == family]
        for z in zones:
            if (z.price_high >= last) if long else (z.price_low <= last):
                continue  # only zones price still has to come back to
            key = (family, z.first_time)
            if key in st.armed or any(_overlap(z.price_low, z.price_high, u_lo, u_hi) > 0.5
                                      for u_lo, u_hi, u_at in st.used if i - u_at < WINDOW):
                continue
            label = f"{tfl} {family} {_fmt(z.price_low)}–{_fmt(z.price_high)}"
            st.armed[key] = _Armed(key, z.price_low, z.price_high, label)

    def zone_entry(j: int, can_trade: bool) -> None:
        nonlocal pos
        touched = [z for z in st.armed.values() if ((l[j] <= z.hi) if long else (h[j] >= z.lo))]
        if not touched:
            return
        for z in touched:
            del st.armed[z.key]
            st.used.append((z.lo, z.hi, j))
        if not can_trade:
            return
        z = max(touched, key=lambda z: z.hi) if long else min(touched, key=lambda z: z.lo)  # the one reached first
        edge = z.hi if long else z.lo
        entry = (min(o[j], edge) if long else max(o[j], edge))  # a gap through the edge fills at the open
        buf = stop_buffer_atr * float(a[j - 1])
        stop = z.lo - buf if long else z.hi + buf
        if (entry - stop) * sign <= 0:
            return
        tgt, extra = target_for(entry, stop)
        pos = _Open(side, j, entry, stop, tgt, z.label + extra)
        px = stop_hit(j)  # the fill candle can stop out (stop first); its high may have come before the fill
        if px is not None:
            close(j, px, "loss")

    def sweep_entry(j: int, can_trade: bool) -> None:
        nonlocal pos
        levels = st.swing_lows if long else st.swing_highs
        cands = [s for s in levels if s[0] < j and ((l[j] < s[2] < c[j]) if long else (c[j] < s[2] < h[j]))]
        if not cands:
            return
        base = min(s[0] for s in levels if s[0] < j)
        sl = df.iloc[base:j + 1].reset_index(drop=True)
        swings = [Swing(idx - base, tm, px, "low" if long else "high", 0.0) for idx, tm, px in levels
                  if base <= idx < j]
        found = liquidity_sweeps(sl, [] if long else swings, swings if long else [], lookback=1, max_results=2)
        found = [s for s in found if s["direction"] == ("bullish" if long else "bearish")]
        if not found or not can_trade:
            return
        sw = found[0]
        entry = float(c[j])
        buf = stop_buffer_atr * float(a[j])
        stop = (float(l[j]) - buf) if long else (float(h[j]) + buf)
        if (entry - stop) * sign <= 0:
            return
        tgt, extra = target_for(entry, stop)
        basis = f"{tfl} swept {'low' if long else 'high'} {_fmt(sw['level'])}" + extra
        pos = _Open(side, j, entry, stop, tgt, basis)

    for j in range(WARMUP - 1, n):
        if j >= WARMUP:
            was_free = pos is None
            if pos is not None and pos.idx < j:
                px = stop_hit(j)
                if px is not None:
                    close(j, px, "loss")
                elif (h[j] >= pos.target) if long else (l[j] <= pos.target):
                    close(j, pos.target, "win")
                elif j - pos.idx >= max_hold_bars:
                    close(j, float(c[j]), "timeout")
            if family == "sweep":
                sweep_entry(j, was_free)
            else:
                zone_entry(j, was_free)
        if (j - (WARMUP - 1)) % STEP == 0:
            recompute(j)

    if pos is not None:
        notes.append("One trade was still open at the last candle and is left out.")
    if fallback_targets:
        notes.append(f"{fallback_targets} trade{'s' if fallback_targets != 1 else ''} had no level at least "
                     f"{MIN_TARGET_R}R away, so {'their' if fallback_targets != 1 else 'its'} target is 2R.")
    return trades, notes


# -------------------------------------------------------------------- kimi --


def kimi_trades(signals: Sequence[Any], times: np.ndarray, closes: np.ndarray, direction: Optional[str],
                cost_pct: float) -> tuple[list[BacktestTrade], int]:
    """Kimi Cooked's resolved signals as trades, scored the way Kimi scores them (its target, stop, expiry and
    `cost_pct` per side). `signals` are engine Signal records (bar, dir, typ, entry, tp, sl, result, r, res_bar,
    conf, tier). Returns (trades, signals still open)."""
    trades: list[BacktestTrade] = []
    still_open = 0
    for s in signals:
        if s.typ >= 15:  # the random-entry controls, not signals
            continue
        d = "long" if s.dir > 0 else "short"
        if direction and d != direction:
            continue
        if s.result == 0:
            still_open += 1
            continue
        if any(not math.isfinite(float(x)) for x in (s.r, s.sl, s.tp, s.entry)) or s.res_bar < 0:
            continue
        sign = 1.0 if s.dir > 0 else -1.0
        entry = float(s.entry)
        cost = entry * 2 * cost_pct / 100
        stop = entry - sign * float(s.sl)
        tgt = entry + sign * (float(s.tp) + cost)
        exit_px = tgt if s.result == 1 else stop if s.result == 2 else float(closes[s.res_bar])
        tier = {1: "top", 0: "rest", -1: "warm-up"}.get(int(s.tier), "rest")
        trades.append(BacktestTrade(
            direction=d, entry_time=int(times[s.bar]), entry=entry, stop=stop, target=tgt,
            exit_time=int(times[s.res_bar]), exit=exit_px, r=round(float(s.r), 4),
            result={1: "win", 2: "loss"}.get(int(s.result), "timeout"),  # type: ignore[arg-type]
            bars_held=int(s.res_bar - s.bar),
            basis=f"Kimi {LABELS.get((s.typ, s.dir), '?')} ({TYPE_NAMES.get(s.typ, '?')}, confluence {s.conf}, "
                  f"{tier} tier)"))
    trades.sort(key=lambda x: (x.exit_time, x.entry_time))
    return trades, still_open


def kimi_engine_signals(candles: list[Candle], interval: str, history: Optional[list[Candle]] = None) -> list[Any]:
    """Run Kimi Cooked on closed candles → every signal it registered (CPU-bound: call it in a thread)."""
    t = np.array([x.time for x in candles], dtype="int64") * 1000
    cols = [np.array([getattr(x, k) for x in candles], dtype=float) for k in ("open", "high", "low", "close", "volume")]
    hist = None
    if history:
        hist = {"t": np.array([x.time for x in history], dtype="int64") * 1000,
                "c": np.array([x.close for x in history], dtype=float)}
    res = KimiCooked(INTERVAL_SECONDS[interval] / 60.0, kimi_inputs()).run(t, *cols, history=hist)
    return list(res.signals)


# ------------------------------------------------------------------- stats --


def compute_stats(trades: list[BacktestTrade]) -> tuple[BacktestStats, list[EquityPoint]]:
    rs = [x.r for x in trades]
    n = len(rs)
    if not n:
        return BacktestStats(), []
    wins = [x.r for x in trades if x.result == "win"]
    losses = [x.r for x in trades if x.result == "loss"]
    pos_r = [r for r in rs if r > 0]
    neg_r = [r for r in rs if r <= 0]
    gain, loss = sum(pos_r), -sum(neg_r)
    equity, cum, peak, dd = [], 0.0, 0.0, 0.0
    for x in sorted(trades, key=lambda x: (x.exit_time, x.entry_time)):
        cum += x.r
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
        equity.append(EquityPoint(time=x.exit_time, r_cum=round(cum, 4)))
    avg_win = sum(pos_r) / len(pos_r) if pos_r else None
    avg_loss = sum(neg_r) / len(neg_r) if neg_r else None
    stats = BacktestStats(
        count=n, wins=len(wins), losses=len(losses), timeouts=n - len(wins) - len(losses),
        win_rate=round(len(wins) / n, 4), avg_r=round(sum(rs) / n, 4), total_r=round(sum(rs), 4),
        expectancy=round(len(pos_r) / n * (avg_win or 0.0) + len(neg_r) / n * (avg_loss or 0.0), 4),
        profit_factor=round(gain / loss, 4) if loss > 0 else None, max_drawdown_r=round(dd, 4),
        avg_bars_held=round(sum(x.bars_held for x in trades) / n, 2),
        avg_win_r=round(avg_win, 4) if avg_win is not None else None,
        avg_loss_r=round(avg_loss, 4) if avg_loss is not None else None,
        best_r=round(max(rs), 4), worst_r=round(min(rs), 4))
    return stats, equity


# ----------------------------------------------------------------- service --


def _bars_cap(interval: str, bars: int) -> int:
    """Derived intervals (3h) are built from a finer one: keep the base request inside one fetch."""
    if interval in DERIVED_INTERVALS:
        return min(bars, MAX_KLINES // DERIVED_INTERVALS[interval][1] - 50)
    return bars


async def _closed_candles(market: "MarketData", symbol: str, interval: str, bars: int) -> tuple[list[Candle], str]:
    candles, source = await market.get_klines(symbol, interval, min(bars + 1, MAX_KLINES))
    return closed_only(candles, interval)[-bars:], source


async def run_backtest(market: "MarketData", kimi: Optional["KimiService"], req: BacktestRequest) -> BacktestResult:
    """Backtest `req.setup` on the newest `req.bars` closed candles of `req.symbol`/`req.interval`. Raises
    MarketDataError when candles cannot be loaded and ValueError when there are too few."""
    started = time.perf_counter()
    bars = _bars_cap(req.interval, req.bars)
    notes: list[str] = []
    if bars < req.bars:
        notes.append(f"{req.interval} candles are built from 1h ones, so the test covers the last {bars:,} "
                     f"candles instead of {req.bars:,}.")
    candles, source = await _closed_candles(market, req.symbol, req.interval, bars)
    if len(candles) < 100:
        raise ValueError(f"Need at least 100 closed candles, got {len(candles)}")
    times = np.array([x.time for x in candles], dtype=np.int64)

    if req.setup.startswith("kimi"):
        history = None
        if INTERVAL_SECONDS[req.interval] < 86400:  # the higher-timeframe anchor needs daily history, like the chart
            try:
                daily, dsrc = await market.get_klines(req.symbol, "1d", HISTORY_DAYS)
                history = closed_only(daily, "1d") if dsrc == source else None
            except Exception as exc:
                log.warning("Backtest: daily history for Kimi failed (%s); running without it", exc)
        sem = getattr(kimi, "_cpu", None) or _RUNS  # share Kimi's one-run-at-a-time lock with the chart indicator
        async with sem:
            signals = await asyncio.to_thread(kimi_engine_signals, candles, req.interval, history)
        direction = {"kimi_long": "long", "kimi_short": "short"}.get(req.setup)
        cost = kimi_inputs().statCostPct
        trades, still_open = kimi_trades(signals, times, np.array([x.close for x in candles]), direction, cost)
        notes.append(f"These follow Kimi Cooked's own exit rules: its ATR-based target and stop, its expiry, and "
                     f"{cost}% costs per side. The exit rule, max hold and fee settings here are not used.")
        notes.append("Kimi signals can overlap; every resolved signal counts, as in Kimi's Signal Stats table.")
        if still_open:
            notes.append(f"{still_open} signal{'s' if still_open != 1 else ''} still open at the last candle "
                         f"{'are' if still_open != 1 else 'is'} left out.")
    else:
        df = candles_to_df(candles)
        async with _RUNS:
            trades, more = await asyncio.to_thread(backtest_frame, df, req.setup, req.interval, req.stop_buffer_atr,
                                                   req.target, req.max_hold_bars, req.fee_pct)
        notes.append(f"Zones are re-detected every {STEP} candles from the last {WINDOW} candles up to that point, "
                     f"so no trade uses later prices. A zone is traded on its first touch after it was found, one "
                     f"trade at a time; when the stop and target are inside the same candle the stop is counted.")
        notes += more
        notes.append(f"R is after {req.fee_pct:g}% fees per side.")

    stats, equity = compute_stats(trades)
    if source == "synthetic":
        notes.insert(0, "These results come from synthetic demo data, not real prices; they say nothing about the "
                        "real market.")
    if stats.count == 0:
        notes.insert(0, "No trades: the setup never triggered in these candles.")
    elif stats.count < SMALL_SAMPLE:
        notes.insert(0, f"Only {stats.count} trade{'s' if stats.count != 1 else ''}: too small a sample to trust "
                        f"the win rate.")
    return BacktestResult(
        symbol=req.symbol, interval=req.interval, setup=req.setup, setup_name=SETUP_NAMES[req.setup],
        bars=len(candles), from_time=int(times[0]), to_time=int(times[-1]), data_source=source,
        target="kimi" if req.setup.startswith("kimi") else req.target, trades=trades, stats=stats, equity=equity,
        notes=notes, seconds=round(time.perf_counter() - started, 2))


def describe_backtest(res: BacktestResult) -> str:
    """One plain-English paragraph for the chat agent."""
    s = res.stats
    tf = TF_LABEL.get(res.interval, res.interval)

    def day(ts: int) -> str:
        return time.strftime("%d %b %Y", time.gmtime(ts))

    head = f"{res.setup_name} on {res.symbol} {tf}, {res.bars:,} candles ({day(res.from_time)} to {day(res.to_time)})"
    warn = [x for x in res.notes if x.startswith(("Only ", "These results come from synthetic"))]
    if not s.count:
        return " ".join([f"{head}: no trades."] + warn)
    pf = f", profit factor {s.profit_factor:.2f}" if s.profit_factor is not None else ""
    body = (f"{head}: {s.count} trades, {s.wins} wins, {s.losses} losses, {s.timeouts} timed out; win rate "
            f"{(s.win_rate or 0) * 100:.0f}%, average {s.avg_r or 0:+.2f}R, total {s.total_r:+.1f}R{pf}, "
            f"max drawdown {s.max_drawdown_r:.1f}R.")
    return " ".join([body] + warn)
