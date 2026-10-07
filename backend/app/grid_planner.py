"""Grid bot planner: suggest a Spot Grid bot for a coin and test it on past candles.

`plan_grid` proposes a lower and upper price, a number of grids and arithmetic vs geometric from the chart itself,
and says why, in the words a trader would use ("range = H4 demand low to H4 supply high; 38 grids keeps profit per
grid at 0.62–0.71% after fees"):

  * Range. The ATR and the support/resistance and supply/demand zones come from ta_agent's detectors (the same
    ones the chart agent draws), on the planning timeframe with daily confluence. The lower price is the low of the
    best support or demand zone below price, the upper price the high of the best resistance or supply zone above
    it, each between MIN_EDGE_ATR and MAX_EDGE_ATR ATRs away (a zone hugging price leaves the bot no room; one far
    away leaves most of the grid idle). "Best" = the detector's score, plus higher-timeframe confluence, minus
    distance. Without a zone in that band the edge falls back to the 30-day low/high, then to 3 ATRs.
  * Grid type. Geometric (the same % per grid) once the upper price is 25%+ above the lower, else arithmetic
    (the same price step); on a narrow range the two are nearly the same and arithmetic reads easier.
  * Grids. Each grid should be wide enough to clear the fees with a margin (net profit per grid >= MIN_NET_PCT
    after both fills' fees) and fine enough to trade often: about GRID_ATR_FRACTION of an ATR. The finer of the two
    that still clears the fees wins, then it is capped so each order stays above Binance's minimum order size.

`backtest_grid` replays a plan (or any edited grid) with the tracker's own simulator (gridbot.simulate) over the
last 7/30/90 days of 1m candles: grid profit, matched trades, APR, max drawdown, time in range and the value
curve, and the same numbers for a few other grid counts, so the trade-off between more fills and thinner grids
is visible. Past fills are no promise: the notes say what the test cannot know (fills inside one minute,
a range that held in the past breaking next week).
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Literal, Optional

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, field_validator, model_validator

from .gridbot import (BAR, BNB_FEE_FACTOR, DAY, DEFAULT_FEE, INTERVAL, MAX_GRIDS, MIN_GRIDS, GridBotParams,
                      GridBotResult,
                      GridBotService, GridType, SymbolFilters, estimate_filters, grid_lines, profit_per_grid_pct,
                      simulate, split_symbol)
from .market_data import candles_to_df
from .pricefmt import _fmt
from .ta_agent import TF_LABEL, Zone, atr, cluster_levels, find_swings, htf_zones, mark_confluence, \
    supply_demand_zones

PLAN_TIMEFRAMES = ("1h", "4h", "1d")
PLAN_BARS = 400
MIN_EDGE_ATR = 1.5      # an edge closer than this to price leaves the bot no room to trade
MAX_EDGE_ATR = 12.0     # further than this most of the grid sits idle
FALLBACK_ATR = 3.0      # edge distance when no zone or recent extreme fits
GEOMETRIC_RATIO = 1.25  # upper / lower above this → geometric
MIN_NET_PCT = 0.3       # net profit per grid after both fees, %
GRID_ATR_FRACTION = 0.3  # a grid about this fraction of the ATR fills often without being eaten by fees
MIN_ORDER_USDT = 5.0    # Binance's usual minimum order (NOTIONAL filter) when exchangeInfo is unavailable
ORDER_MARGIN = 1.2      # keep each order this much above the minimum (the lowest line's order is the smallest)
BACKTEST_DAYS = (7, 30, 90)
MAX_COMPARE = 6

ZONE_KINDS_LOW = ("support", "demand")
ZONE_KINDS_HIGH = ("resistance", "supply")


# ---------------------------------------------------------------- models --


def _norm_symbol(v: str) -> str:
    s = v.replace("/", "").replace("-", "").replace(" ", "").upper()
    if not s.isalnum() or not 2 <= len(s) <= 20:
        raise ValueError("symbol must look like BTCUSDT")
    return s


class GridPlanRequest(BaseModel):
    symbol: str
    investment: float = Field(1000.0, gt=0, description="Investment in the quote asset")
    timeframe: Literal["1h", "4h", "1d"] = Field("4h", description="Timeframe the zones and ATR come from")
    grid_type: Optional[GridType] = Field(None, description="None = let the planner choose")
    fee_rate: float = Field(DEFAULT_FEE, ge=0, le=0.01)
    bnb_discount: bool = False
    min_net_pct: float = Field(MIN_NET_PCT, ge=0.05, le=5, description="Least profit per grid after fees, %")

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, v: str) -> str:
        return _norm_symbol(v)


class PlanZone(BaseModel):
    kind: str
    low: float
    high: float
    label: str
    score: float
    distance_atr: float


class PlanEdge(BaseModel):
    price: float
    basis: str = Field(..., description="e.g. 'H4 demand low', '30-day high', '3 ATR below price'")
    distance_atr: float
    distance_pct: float


class GridPlan(BaseModel):
    symbol: str
    base_asset: str
    quote_asset: str
    timeframe: str
    data_source: str
    last_price: float
    atr: float
    atr_pct: float
    lower: PlanEdge
    upper: PlanEdge
    grids: int
    grid_type: GridType
    investment: float
    fee_rate: float = Field(..., description="Fee per fill as entered, 0.001 = 0.1%")
    bnb_discount: bool
    fee_per_fill: float = Field(..., description="Effective fee per fill (BNB discount applied)")
    profit_per_grid_min_pct: float
    profit_per_grid_max_pct: float
    order_value: float = Field(..., description="About what each order is worth in the quote asset")
    position_pct: float = Field(..., description="Where the price sits in the range: 0 = lower, 100 = upper")
    reasoning: list[str]
    warnings: list[str] = Field(default_factory=list)
    zones: list[PlanZone] = Field(default_factory=list, description="Zones the range was chosen from")
    compare_grids: list[int] = Field(default_factory=list, description="Other grid counts worth testing")
    filters: SymbolFilters

    def params(self, **extra) -> GridBotParams:
        """The plan as tracker settings (give start_time or runtime in `extra` to save it)."""
        return GridBotParams(symbol=self.symbol, lower=self.lower.price, upper=self.upper.price, grids=self.grids,
                             grid_type=self.grid_type, investment=self.investment,
                             fee_rate=self.fee_rate, bnb_discount=self.bnb_discount, **extra)


class GridBacktestRequest(BaseModel):
    """A grid to replay over the last `days` days. The lines, fees and investment work as in the tracker."""

    symbol: str
    lower: float = Field(..., gt=0)
    upper: float = Field(..., gt=0)
    grids: int = Field(..., ge=MIN_GRIDS, le=MAX_GRIDS)
    grid_type: GridType = "arithmetic"
    investment: float = Field(..., gt=0)
    fee_rate: float = Field(DEFAULT_FEE, ge=0, le=0.01)
    bnb_discount: bool = False
    days: int = Field(30, ge=1, le=90)
    compare_grids: list[int] = Field(default_factory=list, max_length=MAX_COMPARE)

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, v: str) -> str:
        return _norm_symbol(v)

    @field_validator("compare_grids")
    @classmethod
    def _grids(cls, v: list[int]) -> list[int]:
        return sorted({g for g in v if MIN_GRIDS <= g <= MAX_GRIDS})

    @model_validator(mode="after")
    def _range(self) -> "GridBacktestRequest":
        if self.lower >= self.upper:
            raise ValueError("The lower price must be below the upper price")
        return self

    def params(self, start: int, grids: Optional[int] = None) -> GridBotParams:
        return GridBotParams(symbol=self.symbol, lower=self.lower, upper=self.upper, grids=grids or self.grids,
                             grid_type=self.grid_type, investment=self.investment, fee_rate=self.fee_rate,
                             bnb_discount=self.bnb_discount, start_time=start)


class GridBacktestRow(BaseModel):
    grids: int
    profit_per_grid_min_pct: float
    profit_per_grid_max_pct: float
    matched_trades: int
    grid_profit: float
    grid_apr_pct: float
    total_pnl: float
    total_pnl_pct: float
    max_drawdown_pct: float
    chosen: bool = False


class GridBacktestResult(BaseModel):
    symbol: str
    quote_asset: str
    days: int
    start_time: int
    end_time: int
    data_source: str
    grid_profit: float
    grid_profit_pct: float
    matched_trades: int
    grid_apr_pct: float
    total_pnl: float
    total_pnl_pct: float
    total_apr_pct: float
    max_drawdown_pct: float
    time_in_range_pct: float
    hold_return_pct: float = Field(..., description="Buying the coin with the investment and holding it instead")
    start_price: float
    last_price: float
    equity: list[tuple[int, float]]
    alternatives: list[GridBacktestRow]
    result: GridBotResult
    notes: list[str] = Field(default_factory=list)


# -------------------------------------------------------------- planning --


def _zone_rank(z: Zone, dist_atr: float) -> float:
    """Detector score, a bonus per confirming higher timeframe, and a small penalty for distance."""
    return z.score + 0.1 * len(z.meta.get("htf", [])) - 0.03 * dist_atr


def _pick_edge(zones: list[Zone], side: Literal["low", "high"], last: float, atr_v: float, tfl: str,
               recent: float, recent_label: str) -> tuple[float, str, Optional[Zone]]:
    """The lower (side 'low') or upper edge: (price, basis, zone used)."""
    below = side == "low"
    kinds = ZONE_KINDS_LOW if below else ZONE_KINDS_HIGH
    best: tuple[float, Zone] | None = None
    for z in zones:
        if z.kind not in kinds:
            continue
        edge = z.price_low if below else z.price_high
        dist = (last - edge) / atr_v if below else (edge - last) / atr_v
        if not MIN_EDGE_ATR <= dist <= MAX_EDGE_ATR:
            continue
        rank = _zone_rank(z, dist)
        if best is None or rank > best[0]:
            best = (rank, z)
    if best is not None:
        z = best[1]
        htf = z.meta.get("htf")
        name = f"{tfl} {z.kind} {'low' if below else 'high'}" + (f" (+{'/'.join(htf)})" if htf else "")
        return (z.price_low if below else z.price_high), name, z
    dist = (last - recent) / atr_v if below else (recent - last) / atr_v
    if MIN_EDGE_ATR <= dist <= MAX_EDGE_ATR:
        return recent, recent_label, None
    edge = last - FALLBACK_ATR * atr_v if below else last + FALLBACK_ATR * atr_v
    return edge, f"{FALLBACK_ATR:g} ATR {'below' if below else 'above'} price", None


def _grid_count(lower: float, upper: float, grid_type: GridType, step_pct: float) -> int:
    """Grids that make each grid about `step_pct` % wide (at the top of the range for arithmetic grids, where the
    same price step is the smallest %)."""
    if grid_type == "geometric":
        n = math.log(upper / lower) / math.log(1 + step_pct / 100)
    else:
        n = (upper - lower) / (upper * step_pct / 100)
    return int(max(MIN_GRIDS, min(MAX_GRIDS, math.floor(n))))


def compare_counts(grids: int) -> list[int]:
    """A few grid counts around the plan's, for the history test: half, two thirds, 1.5x and double."""
    out = {max(MIN_GRIDS, min(MAX_GRIDS, round(grids * f))) for f in (0.5, 0.67, 1.5, 2.0)}
    out.discard(grids)
    return sorted(out)[:MAX_COMPARE]


def build_plan(df: pd.DataFrame, req: GridPlanRequest, filters: SymbolFilters, source: str,
               daily: Optional[pd.DataFrame] = None) -> GridPlan:
    """The plan for one coin from its candles on the planning timeframe (`df`, oldest first) and, for zone
    confluence, its daily candles. Pure: no I/O. Raises ValueError when there are too few candles."""
    df = df.reset_index(drop=True)
    if len(df) < 60:
        raise ValueError("Need at least 60 candles to plan a grid")
    tfl = TF_LABEL.get(req.timeframe, req.timeframe.upper())
    atr_s = atr(df)
    atr_v = float(atr_s.iloc[-1])
    last = float(df["close"].iloc[-1])
    if atr_v <= 0 or last <= 0:
        raise ValueError("The price has not moved; no range to plan a grid in")

    # Zones from the same detectors the chart agent uses, with daily confluence.
    highs, lows = find_swings(df, atr_v)
    zones = cluster_levels(highs + lows, atr_v, last, len(df)) + supply_demand_zones(df, atr_s)
    if daily is not None and req.timeframe != "1d" and len(daily) >= 30:
        mark_confluence(zones, {"1d": htf_zones(daily)})

    step = int(df["time"].iloc[1] - df["time"].iloc[0])
    t_end = int(df["time"].iloc[-1]) + step
    days = max(1, min(30, (t_end - int(df["time"].iloc[0])) // DAY))
    recent = df[df["time"] >= t_end - days * DAY]
    lo_px, lo_basis, lo_zone = _pick_edge(zones, "low", last, atr_v, tfl, float(recent["low"].min()),
                                          f"{days}-day low")
    hi_px, hi_basis, hi_zone = _pick_edge(zones, "high", last, atr_v, tfl, float(recent["high"].max()),
                                          f"{days}-day high")
    tick = filters.tick_size
    lower = float(np.floor(lo_px / tick) * tick)
    upper = float(np.ceil(hi_px / tick) * tick)
    lower = round(lower, 12)
    upper = round(upper, 12)
    if lower <= 0:
        lower = tick
    warnings: list[str] = []

    # Grid type and count.
    ratio = upper / lower
    fee = req.fee_rate * (BNB_FEE_FACTOR if req.bnb_discount else 1.0)
    if req.grid_type:
        grid_type: GridType = req.grid_type
        type_why = f"{grid_type} grids, as asked"
    elif ratio >= GEOMETRIC_RATIO:
        grid_type = "geometric"
        type_why = (f"geometric grids: the upper price is {(ratio - 1) * 100:.0f}% above the lower, so equal % steps "
                    "keep the profit per grid the same across the range")
    else:
        grid_type = "arithmetic"
        type_why = (f"arithmetic grids: on a {(ratio - 1) * 100:.0f}% range equal price steps differ little in %, "
                    "and round steps are easier to read")
    fee_floor = req.min_net_pct + 2 * fee * 100  # gross step that nets min_net after both fills' fees
    atr_step = GRID_ATR_FRACTION * atr_v / last * 100
    step_pct = max(fee_floor, atr_step)
    grids = _grid_count(lower, upper, grid_type, step_pct)
    step_why = (f"a grid of about {GRID_ATR_FRACTION:g} ATR ({atr_step:.2f}%) fills often" if atr_step >= fee_floor
                else f"the {tfl} ATR is only {atr_v / last * 100:.2f}%, so grids are widened to clear the fees")

    # Each order must stay above Binance's minimum order; the smallest order sits on the lowest line.
    min_order = (filters.min_notional or MIN_ORDER_USDT) * ORDER_MARGIN
    order_cap = max(MIN_GRIDS, int(req.investment / min_order) - 1)
    if grids > order_cap:
        warnings.append(f"{grids} grids would make each order smaller than about {min_order:,.2f} "
                        f"{split_symbol(req.symbol)[1] or 'quote'}; capped at {order_cap}. Invest more for finer grids.")
        grids = order_cap
    # Lines closer than one tick cannot be placed: fewer grids until they fit.
    while grids > MIN_GRIDS:
        try:
            lines = grid_lines(lower, upper, grids, grid_type, tick)
            break
        except ValueError:
            grids = max(MIN_GRIDS, int(grids * 0.8))
    else:
        lines = grid_lines(lower, upper, grids, grid_type, tick)
    ppg_min, ppg_max = profit_per_grid_pct(lines, fee)
    if ppg_min <= 0:
        warnings.append("The fees eat the profit of the narrowest grids; widen the range or use fewer grids.")

    base, quote = split_symbol(req.symbol)
    q = quote or "quote"
    position = (last - lower) / (upper - lower) * 100
    lo_dist, hi_dist = (last - lower) / atr_v, (upper - last) / atr_v
    reasoning = [
        f"Range = {lo_basis} {_fmt(lower)} to {hi_basis} {_fmt(upper)}: {(ratio - 1) * 100:.1f}% wide, "
        f"{lo_dist:.1f} ATR below and {hi_dist:.1f} ATR above the price {_fmt(last)} "
        f"({tfl} ATR {_fmt(atr_v, last)}, {atr_v / last * 100:.2f}% of price).",
        f"{grids} grids keeps profit per grid at {ppg_min:.2f}–{ppg_max:.2f}% after fees "
        f"({fee * 100:.3g}% per fill): {step_why}.",
        type_why[0].upper() + type_why[1:] + ".",
        f"About {req.investment / (grids + 1):,.2f} {q} per order on {grids + 1} lines. The price sits "
        f"{position:.0f}% up the range, so about {100 - position:.0f}% of the lines start as sells and the bot "
        f"first buys the {base or 'base'} for them.",
    ]
    if lo_zone is None or hi_zone is None:
        missing = " and ".join(s for s, z in (("below", lo_zone), ("above", hi_zone)) if z is None)
        reasoning.append(f"No {tfl} zone {missing} price within {MIN_EDGE_ATR:g}–{MAX_EDGE_ATR:g} ATR, so that edge "
                         "uses the recent extreme or a fixed ATR distance.")
    if position < 20 or position > 80:
        warnings.append(f"The price is near the {'bottom' if position < 50 else 'top'} of the range: a move "
                        f"{'down' if position < 50 else 'up'} soon takes it out of the grid.")
    if source == "synthetic":
        warnings.append("Demo data: Binance was unreachable, so this plan comes from synthetic prices.")

    used = [z for z in (lo_zone, hi_zone) if z is not None]
    nearby = sorted((z for z in zones if abs(z.mid - last) / atr_v <= MAX_EDGE_ATR),
                    key=lambda z: -_zone_rank(z, abs(z.mid - last) / atr_v))[:6]
    for z in used:
        if z not in nearby:
            nearby.append(z)
    plan_zones = [PlanZone(kind=z.kind, low=float(f"{z.price_low:.6g}"), high=float(f"{z.price_high:.6g}"),
                           label=f"{tfl} {z.kind.title()}" + (f" + {'/'.join(z.meta['htf'])}" if z.meta.get("htf")
                                                              else ""),
                           score=round(min(z.score, 1.0), 2), distance_atr=round(abs(z.mid - last) / atr_v, 2))
                  for z in sorted(nearby, key=lambda z: -z.mid)]
    return GridPlan(
        symbol=req.symbol, base_asset=base, quote_asset=quote, timeframe=req.timeframe, data_source=source,
        last_price=last, atr=atr_v, atr_pct=round(atr_v / last * 100, 3),
        lower=PlanEdge(price=lower, basis=lo_basis, distance_atr=round(lo_dist, 2),
                       distance_pct=round((last - lower) / last * 100, 2)),
        upper=PlanEdge(price=upper, basis=hi_basis, distance_atr=round(hi_dist, 2),
                       distance_pct=round((upper - last) / last * 100, 2)),
        grids=grids, grid_type=grid_type, investment=req.investment, fee_rate=req.fee_rate, bnb_discount=req.bnb_discount,
        fee_per_fill=fee,
        profit_per_grid_min_pct=round(ppg_min, 4), profit_per_grid_max_pct=round(ppg_max, 4),
        order_value=round(req.investment / (grids + 1), 2), position_pct=round(position, 1), reasoning=reasoning,
        warnings=warnings, zones=plan_zones, compare_grids=compare_counts(grids), filters=filters,
    )


async def plan_grid(svc: GridBotService, req: GridPlanRequest) -> GridPlan:
    """Fetch the coin's candles (planning timeframe + daily) and its tick/step sizes, then plan.
    Raises ValueError for a pair Binance does not list and MarketDataError when candles cannot be fetched."""
    market = svc.market
    filters = await svc.symbol_filters(req.symbol)
    want_daily = req.timeframe != "1d"
    got = await asyncio.gather(market.get_klines(req.symbol, req.timeframe, PLAN_BARS),  # type: ignore[attr-defined]
                               *([market.get_klines(req.symbol, "1d", 300)] if want_daily else []),  # type: ignore
                               return_exceptions=True)
    if isinstance(got[0], BaseException):
        raise got[0]
    candles, source = got[0]
    df = candles_to_df(candles)
    daily = candles_to_df(got[1][0]) if want_daily and not isinstance(got[1], BaseException) else None
    if filters is None:
        filters = estimate_filters(float(df["close"].iloc[-1]))
    return await asyncio.to_thread(build_plan, df, req, filters, source, daily)


def describe_plan(plan: GridPlan) -> str:
    """The plan in a few sentences, for the chat agent's answer when no LLM narrates."""
    q = plan.quote_asset or "quote"
    text = (f"Grid bot plan for {plan.symbol}: {_fmt(plan.lower.price)}–{_fmt(plan.upper.price)}, {plan.grids} "
            f"{plan.grid_type} grids, {plan.investment:,.0f} {q}. " + " ".join(plan.reasoning[:3]))
    if plan.warnings:
        text += " " + " ".join(plan.warnings)
    return text + " Open the Grid bots tab to test it on history or track it."


def plan_facts(plan: GridPlan) -> dict:
    """The plan for the chat agent's FACTS (no filters or zone lists)."""
    return {"symbol": plan.symbol, "lower": plan.lower.price, "lower_basis": plan.lower.basis,
            "upper": plan.upper.price, "upper_basis": plan.upper.basis, "grids": plan.grids,
            "grid_type": plan.grid_type, "investment": plan.investment,
            "profit_per_grid_pct": [plan.profit_per_grid_min_pct, plan.profit_per_grid_max_pct],
            "reasoning": plan.reasoning, "warnings": plan.warnings}


# -------------------------------------------------------------- backtest --


def _row(r: GridBotResult, grids: int, chosen: bool) -> GridBacktestRow:
    return GridBacktestRow(grids=grids, profit_per_grid_min_pct=r.profit_per_grid_min_pct,
                           profit_per_grid_max_pct=r.profit_per_grid_max_pct, matched_trades=r.matched_trades,
                           grid_profit=r.grid_profit, grid_apr_pct=r.grid_apr_pct, total_pnl=r.total_pnl,
                           total_pnl_pct=r.total_pnl_pct, max_drawdown_pct=r.max_drawdown_pct, chosen=chosen)


def run_backtest(df: pd.DataFrame, req: GridBacktestRequest, filters: SymbolFilters, source: str,
                 start: int, now: float) -> GridBacktestResult:
    """Replay the grid and its alternatives over `df` (1m bars from `start`). Pure: no I/O."""
    main = simulate(df, req.params(start), filters, now=now, source=source, curve=True)
    rows = [_row(main, req.grids, True)]
    notes: list[str] = []
    for g in req.compare_grids:
        if g == req.grids:
            continue
        try:
            rows.append(_row(simulate(df, req.params(start, g), filters, now=now, source=source), g, False))
        except ValueError as exc:
            notes.append(f"{g} grids: {exc}")
    rows.sort(key=lambda r: r.grids)
    first = float(df["open"].iloc[0])
    last = main.last_price
    if df["time"].iloc[0] > start + 2 * DAY:
        notes.append("The coin has less history than the period asked for; the test starts at its first candle.")
    notes.append("A past test shows how this grid would have traded, not how it will: fills inside one minute are "
                 "approximated and a range that held before can break.")
    notes += main.notes
    return GridBacktestResult(
        symbol=req.symbol, quote_asset=main.quote_asset, days=req.days, start_time=main.start_time,
        end_time=main.data_end + BAR, data_source=source, grid_profit=main.grid_profit,
        grid_profit_pct=main.grid_profit_pct, matched_trades=main.matched_trades, grid_apr_pct=main.grid_apr_pct,
        total_pnl=main.total_pnl, total_pnl_pct=main.total_pnl_pct, total_apr_pct=main.total_apr_pct,
        max_drawdown_pct=main.max_drawdown_pct, time_in_range_pct=main.time_in_range_pct,
        hold_return_pct=round((last / first - 1) * 100, 4) if first else 0.0, start_price=first, last_price=last,
        equity=main.equity or [], alternatives=rows, result=main.model_copy(update={"equity": None}), notes=notes,
    )


async def backtest_grid(svc: GridBotService, req: GridBacktestRequest, now: Optional[float] = None) -> GridBacktestResult:
    """Fetch the last `days` of 1m candles and replay the grid on them (ValueError / MarketDataError as above)."""
    now = time.time() if now is None else now
    start = int(now) - req.days * DAY
    start -= start % BAR
    filters = await svc.symbol_filters(req.symbol)
    df, source = await svc.market.get_range(req.symbol, INTERVAL, start)
    if df.empty:
        raise ValueError("No 1-minute candles for this period")
    if filters is None:
        filters = estimate_filters(req.lower)
    first = int(df["time"].iloc[0])
    return await asyncio.to_thread(run_backtest, df, req, filters, source, max(start, first), now)
