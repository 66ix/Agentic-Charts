"""Track record of a trade plan: how the same kind of setup did on that coin and timeframe in the past.

A plan built on fresh H4 demand on INJ is matched to the "long from fresh demand" backtest (backtest.py) on INJ 4h
and summed up in one line: "fresh 4h demand longs on INJ: 14 trades, 57% win, +0.60R avg, last 1 year". The backtest
is the same one the Backtest tab runs (so "Open in Backtest" shows the same trades): about a year of closed candles
(capped at MAX_BARS), the stop 0.25 ATR beyond the zone like the plan's, and the exit at the next opposing level at
least 0.8R away, like the plan's first target.

What the backtest can't express is said in the notes rather than hidden: a plan on a zone that was already tested is
compared with first touches of untested zones, and higher-timeframe confluence is not filtered on. Order blocks, the
user's own zones and market entries beyond a swing have no matching setup. With too little history or too few trades
the record says so instead of quoting a win rate.

Results are cached per (symbol, interval, setup) for TTL_MIN to TTL_MAX depending on the timeframe, and concurrent
asks for the same record share one run, so plans and the market scanner stay fast.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Optional

from .backtest import SETUP_NAMES, BacktestRequest, BacktestResult, run_backtest
from .market_data import INTERVAL_SECONDS
from .schemas import TrackRecord, TradePlan

if TYPE_CHECKING:
    from .market_data import MarketData

log = logging.getLogger(__name__)

YEAR = 365 * 86400
MAX_BARS = 3000      # about a second of CPU per backtest
MIN_HISTORY = 500    # at least this many candles are asked for, even on daily and weekly charts
MIN_BARS = 300       # fewer closed candles than this is too little history to judge
MIN_TRADES = 8       # below this no win rate is quoted
SMALL_SAMPLE = 20    # below this the numbers are flagged as a small sample
TARGET = "next_level"
TTL_MIN, TTL_MAX = 1800.0, 6 * 3600.0
RETRY_TTL = 60.0     # failed runs are retried after this
DEMO_TTL = 300.0     # demo results while Binance is briefly down are not kept for long
MAX_CACHE = 512

# (zone kind, direction) → backtest setup, and how the record names it.
ZONE_SETUPS: dict[tuple[str, str], str] = {
    ("demand", "long"): "demand_long",
    ("supply", "short"): "supply_short",
    ("support", "long"): "support_long",
    ("resistance", "short"): "resistance_short",
}
SETUP_PHRASES: dict[str, str] = {
    "demand_long": "fresh {tf} demand longs",
    "supply_short": "fresh {tf} supply shorts",
    "support_long": "{tf} support longs",
    "resistance_short": "{tf} resistance shorts",
}
UNMATCHED: dict[str, str] = {
    "ob_bullish": "an order block", "ob_bearish": "an order block", "custom_zone": "your own zone",
    "swing": "a market entry beyond the last swing",
}


def base_asset(symbol: str) -> str:
    for quote in ("USDT", "USDC", "FDUSD", "BTC", "ETH"):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[: -len(quote)]
    return symbol


def history_bars(interval: str) -> int:
    """Candles to backtest on: about a year, at least MIN_HISTORY, at most MAX_BARS."""
    return max(MIN_HISTORY, min(MAX_BARS, YEAR // INTERVAL_SECONDS.get(interval, 3600)))


def ttl_for(interval: str) -> float:
    """A record only changes as candles close: ten candles, kept between half an hour and six hours."""
    return max(TTL_MIN, min(TTL_MAX, 10.0 * INTERVAL_SECONDS.get(interval, 3600)))


def period_text(seconds: float) -> str:
    days = seconds / 86400
    if days >= 330:
        years = round(days / 365, 1)
        return f"last {years:g} year{'s' if years != 1 else ''}"
    if days >= 60:
        return f"last {round(days / 30.4)} months"
    if days >= 2:
        return f"last {round(days)} days"
    return f"last {max(1, round(seconds / 3600))} hours"


def setup_label(setup: str, symbol: str, interval: str) -> str:
    return f"{SETUP_PHRASES.get(setup, SETUP_NAMES.get(setup, setup)).format(tf=interval)} on {base_asset(symbol)}"


def match_setup(plan: TradePlan) -> tuple[Optional[str], list[str]]:
    """The backtest setup matching a plan's basis → (setup or None, notes on how close the match is)."""
    kind = plan.zone_kind or ""
    setup = ZONE_SETUPS.get((kind, plan.direction))
    if setup is None:
        return None, [f"No backtest setup matches {UNMATCHED.get(kind, 'this plan')} yet."]
    notes: list[str] = []
    if kind in ("demand", "supply") and plan.zone_fresh is False:
        notes.append(f"This {kind} zone was already tested; the record counts first touches of untested zones.")
    if plan.zone_htf:
        notes.append(f"The zone lines up with {'/'.join(plan.zone_htf)}; the record counts every {kind} zone, with or "
                     f"without higher-timeframe confluence.")
    return setup, notes


def record_from_result(res: BacktestResult, label: str) -> TrackRecord:
    """Summarise a backtest as a track record, refusing to quote numbers on thin data."""
    s = res.stats
    span = period_text(res.to_time - res.from_time + INTERVAL_SECONDS.get(res.interval, 0))
    demo = res.data_source == "synthetic"
    common = dict(setup=res.setup, label=label, symbol=res.symbol, interval=res.interval, trades=s.count,
                  wins=s.wins, bars=res.bars, from_time=res.from_time, to_time=res.to_time, period=span,
                  target=res.target, data_source=res.data_source)
    notes = [f"Backtest of \"{res.setup_name}\" on the {span} ({res.bars:,} closed {res.interval} candles): first "
             f"touch of each zone, stop 0.25 ATR beyond it, exit at the next opposing level at least 0.8R away "
             f"(else 2R) or after 50 candles, 0.1% fees per side."]
    if demo:
        notes.append("Computed on synthetic demo data, not real prices.")
    tail = " (demo data)" if demo else ""
    if res.bars < MIN_BARS:
        return TrackRecord(status="short_history", notes=notes, **common,
                           summary=f"{label}: only {res.bars} candles of history, too little to judge{tail}")
    if s.count < MIN_TRADES:
        n = f"only {s.count} trade{'s' if s.count != 1 else ''}" if s.count else "no trades"
        return TrackRecord(status="too_few_trades", notes=notes, **common,
                           summary=f"{label}: {n} in the {span}, too few to judge{tail}")
    status = "small_sample" if s.count < SMALL_SAMPLE else "ok"
    summary = (f"{label}: {s.count} trades, {round((s.win_rate or 0) * 100)}% win, {s.avg_r or 0:+.2f}R avg, {span}"
               + (" (small sample)" if status == "small_sample" else "") + tail)
    return TrackRecord(status=status, win_rate=s.win_rate, avg_r=s.avg_r, total_r=s.total_r,
                       profit_factor=s.profit_factor, max_drawdown_r=s.max_drawdown_r, summary=summary, notes=notes,
                       **common)


class TrackRecordService:
    """Track records cached per (symbol, interval, setup); one run per key at a time."""

    def __init__(self, market: "MarketData") -> None:
        self.market = market
        self._cache: dict[tuple[str, str, str], tuple[float, TrackRecord]] = {}
        self._inflight: dict[tuple[str, str, str], asyncio.Task] = {}

    async def get(self, symbol: str, interval: str, setup: str) -> TrackRecord:
        key = (symbol, interval, setup)
        hit = self._cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1].model_copy(deep=True)
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._compute(symbol, interval, setup), name=f"track:{symbol}:{interval}")
            self._inflight[key] = task
            task.add_done_callback(lambda _t: self._inflight.pop(key, None))
        # Shielded: a caller that stops waiting (a timeout) leaves the run going, and the next ask finds it cached.
        return (await asyncio.shield(task)).model_copy(deep=True)

    async def for_plan(self, symbol: str, interval: str, plan: TradePlan,
                       timeout: Optional[float] = None) -> TrackRecord:
        setup, notes = match_setup(plan)
        if setup is None:
            return TrackRecord(symbol=symbol, interval=interval, status="no_match", notes=notes,
                               summary=notes[0].rstrip("."))
        try:
            rec = await asyncio.wait_for(self.get(symbol, interval, setup), timeout)
        except asyncio.TimeoutError:
            label = setup_label(setup, symbol, interval)
            return TrackRecord(setup=setup, label=label, symbol=symbol, interval=interval, status="unavailable",
                               summary=f"{label}: still being backtested, ask again in a moment")
        rec.notes = notes + rec.notes
        return rec

    async def _compute(self, symbol: str, interval: str, setup: str) -> TrackRecord:
        label = setup_label(setup, symbol, interval)
        req = BacktestRequest(symbol=symbol, interval=interval, setup=setup, bars=history_bars(interval),
                              target=TARGET)
        ttl = ttl_for(interval)
        try:
            rec = record_from_result(await run_backtest(self.market, None, req), label)
            if rec.data_source == "synthetic" and self.market.settings.data_source != "synthetic":
                ttl = DEMO_TTL
        except ValueError as exc:  # fewer than 100 closed candles: a new listing
            log.info("Track record %s %s %s: %s", symbol, interval, setup, exc)
            rec = TrackRecord(setup=setup, label=label, symbol=symbol, interval=interval, status="short_history",
                              summary=f"{label}: too little history to judge")
        except Exception as exc:  # network, Binance errors: the plan goes out without it
            log.warning("Track record %s %s %s failed: %s", symbol, interval, setup, exc)
            rec = TrackRecord(setup=setup, label=label, symbol=symbol, interval=interval, status="unavailable",
                              summary=f"{label}: history unavailable right now")
            ttl = RETRY_TTL
        self._cache[(symbol, interval, setup)] = (time.monotonic() + ttl, rec)
        while len(self._cache) > MAX_CACHE:
            self._cache.pop(next(iter(self._cache)))
        return rec
