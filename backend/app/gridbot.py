"""Binance Spot Grid bot tracker: replay a grid bot on 1-minute candles and report what Binance shows.

The user runs a Spot Grid bot on Binance and enters its settings here (range, number of grids, arithmetic or
geometric, investment, how long it has run). `simulate` replays the bot over every 1m bar since it started and
returns the numbers on Binance's bot card: matched trades (total and 24h), grid profit, floating PnL, total PnL,
APR, the per-grid profit range, holdings, open orders and the latest fills. `GridBotService` keeps saved bots in a
JSON file (GRIDBOTS_STORE) and caches each bot's result until the next 1m bar opens.

Checked against Binance's own pages ("What Is Spot Grid Trading and How Does It Work?" and "Binance Spot Grid
Trading Parameters", Binance FAQ, read October 2026):
  - N grids = N+1 price lines. Arithmetic: d = (upper - lower) / N between lines. Geometric: each line is the one
    below times r = (upper / lower) ^ (1 / N). Grids 2-500.
  - Sell orders above the price, buy orders below; the bot market-buys the base the sell orders need.
  - Every order has the same base quantity ("Qty Per Order"): the largest quantity the investment covers after
    the buy orders' quote and the base for the sell orders (and the fees) are set aside.
  - Profit/Grid after fees = (1 - c) * sell / buy - 1 - c, with c the fee rate (0.1% by default). So each matched
    pair earns qty * (sell * (1 - c) - buy * (1 + c)): fees on both legs, each on its fill's quote value.
  - Grid profit: realized profit of matched buy/sell pairs, fees deducted. Total profit = grid profit + floating
    (unrealized) PnL, and floating PnL = open buy orders' quote + base for the sell orders at the last price
    + fee reserves - investment. Here total PnL = quote + base * last price - investment, which is the same
    thing, and floating = total - grid.
  - Annualized yield = profit / investment * 525,600 / runtime in minutes. Binance's FAQ applies it to total
    profit; the bot card also shows a grid APR, so both are returned (`grid_apr_pct`, `total_apr_pct`).
  - The trigger price starts the bot when the last price rises above or falls below it; take profit sits above
    the range and stop loss below it; "Sell all base on stop" market-sells the base when the bot stops.
Assumed (Binance does not document it):
  - The line closest to the start price gets no order (ties go to the lower line), lines are rounded to the
    nearest tick and the quantity rounded down to the lot step.
  - The fee reserve is the initial market buy's fee only, so Qty Per Order = investment / (sum of buy-line prices
    + start price * sell orders * (1 + c)). Binance keeps a small base reserve for later fees too, so its quantity
    can be a hair smaller; enter Binance's "Qty per order" (`qty_per_order`) to use it exactly.
  - A sell that was placed at creation is matched with the line below it (its profit counts as one grid; the gap
    between the actual buy price and that line shows in floating PnL).
  - Fees are taken in quote at the fill price (Binance takes buy fees in base or BNB; same value at fill time).
  - Fills: a buy fills when price reaches its line (touch), a sell likewise. 1m bars are walked open -> nearer
    extreme -> farther extreme -> close (high first when the open is closer to the high; a tie goes low first),
    and every fill happens at its line price. A counter-order placed during one leg can only fill on a later leg.
    Within a minute the real path may cross a line more often than four points show, so a choppy minute can hide
    a fill: that is the main reason the app can differ slightly from Binance.

Speed: the whole grid's state is one number, the index of the line without an order (buys below it, sells above
it). A move down to price p fills every buy between p and that line, a move up every sell, so each path point is
two precomputed `searchsorted` lookups and a comparison. A month of 1m bars (172,800 path points) takes ~20 ms.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Optional, Protocol

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from .config import Settings, get_settings
from .market_data import MAX_RANGE_BARS

log = logging.getLogger(__name__)

INTERVAL = "1m"
BAR = 60
DAY = 86_400
MINUTES_PER_YEAR = 525_600
MIN_GRIDS, MAX_GRIDS = 2, 500
DEFAULT_FEE = 0.001
BNB_FEE_FACTOR = 0.75  # paying fees in BNB takes 25% off: 0.1% -> 0.075%
RECENT_FILLS = 200
MAX_BOTS = 50
FILTERS_TTL = 24 * 3600.0  # exchangeInfo tick/step sizes rarely change
FILTERS_RETRY = 300.0  # after a failed exchangeInfo call
MAX_RUNTIME_DAYS = MAX_RANGE_BARS // 1440
EQUITY_POINTS = 400

GridType = Literal["arithmetic", "geometric"]
Side = Literal["buy", "sell"]


# ---------------------------------------------------------------- models --


class SymbolFilters(BaseModel):
    """Binance's PRICE_FILTER.tickSize, LOT_SIZE.stepSize and (MIN_)NOTIONAL.minNotional for one pair."""

    tick_size: float = Field(..., gt=0)
    step_size: float = Field(..., gt=0)
    min_notional: float = Field(0.0, ge=0)
    source: Literal["binance", "estimated"] = "estimated"


class BinanceShows(BaseModel):
    """Numbers the user copied from the bot on Binance, to see how close the simulation is."""

    matched_trades: Optional[int] = Field(None, ge=0)
    grid_profit: Optional[float] = None
    total_pnl: Optional[float] = None
    captured_at: Optional[int] = Field(None, description="When they were read off Binance (UNIX s); default: saved")


class GridBotParams(BaseModel):
    """A Spot Grid bot as set up on Binance. Give `runtime` ("12d 3h 45m", as Binance shows it) or `start_time`."""

    symbol: str
    lower: float = Field(..., gt=0, description="Lowest grid line (quote per base)")
    upper: float = Field(..., gt=0, description="Highest grid line")
    grids: int = Field(..., ge=MIN_GRIDS, le=MAX_GRIDS, description="Number of grids (lines = grids + 1)")
    grid_type: GridType = "arithmetic"
    investment: float = Field(..., gt=0, description="Investment in the quote asset (e.g. USDT)")
    runtime: Optional[str] = Field(None, description="How long the bot has run, e.g. '3d 4h 12m'")
    start_time: Optional[int] = Field(None, ge=0, description="When the bot was created (UNIX s); wins over runtime")
    end_time: Optional[int] = Field(None, ge=0, description="When the bot was stopped by hand (UNIX s)")
    fee_rate: float = Field(DEFAULT_FEE, ge=0, le=0.01, description="Fee per fill, 0.001 = 0.1%")
    bnb_discount: bool = Field(False, description="Fees paid in BNB (25% off)")
    trigger_price: Optional[float] = Field(None, gt=0)
    take_profit: Optional[float] = Field(None, gt=0)
    stop_loss: Optional[float] = Field(None, gt=0)
    sell_on_stop: bool = Field(False, description="Sell all base at market when the bot stops")
    qty_per_order: Optional[float] = Field(None, gt=0, description="Binance's Qty Per Order, if known")

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, v: str) -> str:
        s = v.replace("/", "").replace("-", "").replace(" ", "").upper()
        if not s.isalnum() or not 2 <= len(s) <= 20:
            raise ValueError("symbol must look like BTCUSDT")
        return s

    @field_validator("runtime")
    @classmethod
    def _runtime(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        parse_runtime(v)  # raises a readable ValueError
        return v.strip()

    @model_validator(mode="after")
    def _check(self) -> "GridBotParams":
        for name in ("lower", "upper", "investment", "trigger_price", "take_profit", "stop_loss", "qty_per_order"):
            v = getattr(self, name)
            if v is not None and not math.isfinite(v):
                raise ValueError(f"{name} must be a number")
        if self.lower >= self.upper:
            raise ValueError("The lower price must be below the upper price")
        if self.take_profit is not None and self.take_profit <= self.upper:
            raise ValueError("Take profit must be above the upper price (Binance's rule)")
        if self.stop_loss is not None and self.stop_loss >= self.lower:
            raise ValueError("Stop loss must be below the lower price (Binance's rule)")
        if self.start_time is not None and self.end_time is not None and self.end_time <= self.start_time:
            raise ValueError("The stop time must be after the start time")
        return self

    @property
    def fee(self) -> float:
        """Effective fee rate per fill."""
        return self.fee_rate * (BNB_FEE_FACTOR if self.bnb_discount else 1.0)


class GridOrder(BaseModel):
    side: Side
    price: float
    qty: float
    line: int


class GridFill(BaseModel):
    time: int = Field(..., description="Open time of the 1m bar it filled in (UNIX s)")
    side: Side
    price: float
    qty: float
    line: Optional[int] = Field(None, description="Grid line index; None for the initial buy and the stop sell")
    kind: Literal["grid", "initial", "stop"] = "grid"
    profit: Optional[float] = Field(None, description="Grid profit of the matched trade this sell completed")


class GridDay(BaseModel):
    day: int = Field(..., description="UTC midnight, UNIX s")
    matched: int
    grid_profit: float


class CompareRow(BaseModel):
    binance: float
    app: float
    diff: float = Field(..., description="app - binance")
    diff_pct: Optional[float] = Field(None, description="diff as % of Binance's number")


class GridComparison(BaseModel):
    at: int = Field(..., description="Time the app's numbers are taken at (when Binance's were copied)")
    matched_trades: Optional[CompareRow] = None
    grid_profit: Optional[CompareRow] = None
    total_pnl: Optional[CompareRow] = None


class GridBotResult(BaseModel):
    symbol: str
    base_asset: str
    quote_asset: str
    data_source: str = Field(..., description="'binance', or 'synthetic' = demo data, not real prices")
    status: Literal["running", "waiting", "stopped"]
    stop_reason: Optional[Literal["take_profit", "stop_loss", "ended"]] = None
    start_time: int
    triggered_at: Optional[int] = None
    stopped_at: Optional[int] = None
    data_end: int = Field(..., description="Open time of the newest 1m bar used")
    bars: int
    runtime_minutes: int
    runtime_text: str
    grid_type: GridType
    lines: list[float]
    gap_line: Optional[int] = Field(None, description="Index of the line without an order (running bots)")
    qty_per_order: float
    start_price: Optional[float] = Field(None, description="Price the grid was laid out at (None while waiting)")
    last_price: float
    in_range: bool
    position_pct: float = Field(..., description="Where the price sits in the range: 0 = lower, 100 = upper")
    investment: float
    matched_trades: int
    matched_trades_24h: int
    grid_profit: float
    grid_profit_pct: float
    floating_pnl: float
    floating_pnl_pct: float
    total_pnl: float
    total_pnl_pct: float
    grid_apr_pct: float
    total_apr_pct: float
    profit_per_grid_min_pct: float
    profit_per_grid_max_pct: float
    fee_rate: float
    fees_paid: float
    base_held: float
    quote_held: float
    current_value: float
    open_orders: list[GridOrder]
    recent_fills: list[GridFill] = Field(..., description=f"Up to {RECENT_FILLS}, newest first")
    daily: list[GridDay]
    filters: SymbolFilters
    comparison: Optional[GridComparison] = None
    notes: list[str] = Field(default_factory=list)
    max_drawdown_pct: float = Field(0.0, description="Largest fall of the bot's value from a high, % of that high")
    time_in_range_pct: float = Field(0.0, description="Share of 1m closes inside the range while the bot ran")
    equity: Optional[list[tuple[int, float]]] = Field(
        None, description=f"(time, value) at most {EQUITY_POINTS} points; only when asked for (backtests)")


class GridBot(BaseModel):
    id: str
    name: str
    params: GridBotParams
    binance: Optional[BinanceShows] = None
    created_at: int
    updated_at: int


class GridBotCreate(BaseModel):
    name: str = Field("", max_length=80)
    params: GridBotParams
    binance: Optional[BinanceShows] = None


class GridBotPatch(BaseModel):
    """Only the fields sent change. `params` may be partial; `binance: null` clears the comparison."""

    name: Optional[str] = Field(None, max_length=80)
    params: Optional[dict[str, Any]] = None
    binance: Optional[BinanceShows] = None


class GridSimulateRequest(GridBotParams):
    binance: Optional[BinanceShows] = None


# --------------------------------------------------------------- helpers --

_RUNTIME_UNITS = {"w": 7 * DAY, "d": DAY, "h": 3600, "m": 60, "s": 1}
_RUNTIME_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(weeks?|wks?|w|days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)(?![a-z])",
    re.IGNORECASE,
)


def parse_runtime(text: str) -> int:
    """Binance's runtime text → seconds: "12d 3h 45m", "3d 4h", "45m", "6h 10m", "2 days 5 hours", "1w 2d"."""
    s = text.strip().lower()
    total = 0.0
    rest = s
    for m in _RUNTIME_RE.finditer(s):
        unit = m.group(2)[0]
        total += float(m.group(1)) * _RUNTIME_UNITS[unit]
        rest = rest.replace(m.group(0), " ", 1)
    if not s or re.sub(r"[\s,]+|\band\b", "", rest):
        raise ValueError(f"Could not read the runtime {text!r}; write it like Binance shows it, e.g. 3d 4h 12m")
    if total < BAR:
        raise ValueError("The runtime must be at least 1 minute")
    return int(total)


def format_runtime(minutes: int) -> str:
    """Minutes → "12d 3h 45m" (Binance's style)."""
    d, rem = divmod(max(0, int(minutes)), 1440)
    h, m = divmod(rem, 60)
    parts = [f"{d}d"] if d else []
    if h or d:
        parts.append(f"{h}h")
    parts.append(f"{m}m")
    return " ".join(parts)


def resolve_start(params: GridBotParams, now: float) -> int:
    """The bot's start as the open time of the 1m bar it was created in."""
    if params.start_time is not None:
        start = params.start_time
    elif params.runtime:
        start = int(now) - parse_runtime(params.runtime)
    else:
        raise ValueError("Give the bot's runtime (e.g. 3d 4h 12m) or its start time")
    start -= start % BAR
    if start > now:
        raise ValueError("The start time is in the future")
    if (int(now) - start) // BAR + 1 > MAX_RANGE_BARS:
        raise ValueError(f"The runtime is too long: the app can replay at most {MAX_RUNTIME_DAYS} days of "
                         "1-minute candles")
    return start


def split_symbol(symbol: str) -> tuple[str, str]:
    for q in ("USDT", "USDC", "FDUSD", "TUSD", "BUSD", "BTC", "ETH", "BNB", "TRY", "EUR"):
        if symbol.endswith(q) and len(symbol) > len(q):
            return symbol[: -len(q)], q
    return symbol, ""


def _decimals(x: float) -> int:
    exp = Decimal(repr(float(x))).normalize().as_tuple().exponent
    return min(12, max(0, -int(exp)))


def floor_step(x: float, step: float) -> float:
    """Round a quantity down to the lot step (with a little slack for float noise)."""
    return round(math.floor(x / step + 1e-9) * step, _decimals(step))


def round_tick(x: float | np.ndarray, tick: float) -> float | np.ndarray:
    """Round a price to the nearest tick."""
    return np.round(np.round(np.asarray(x, dtype=float) / tick) * tick, _decimals(tick))


def estimate_filters(price: float) -> SymbolFilters:
    """Tick and step sizes from the price's magnitude, for when Binance's exchangeInfo is unreachable: about five
    significant digits of price (at most 0.01) and a lot step worth under 0.1 quote. Equal to or finer than
    Binance's for most USDT pairs (BTC: 0.01 / 0.000001 vs Binance's 0.01 / 0.00001), so lines move by less than
    a tick and the quantity by less than one of Binance's lot steps."""
    p = max(price, 1e-12)
    tick = min(0.01, 10.0 ** (math.floor(math.log10(p)) - 4))
    step = min(1.0, 10.0 ** math.floor(math.log10(0.1 / p)))
    return SymbolFilters(tick_size=float(f"{tick:.0e}"), step_size=float(f"{step:.0e}"), min_notional=0.0)


def parse_filters(payload: dict) -> SymbolFilters:
    """Binance /api/v3/exchangeInfo?symbol=X → SymbolFilters."""
    filters = {f.get("filterType"): f for f in payload["symbols"][0]["filters"]}
    notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
    return SymbolFilters(tick_size=float(filters["PRICE_FILTER"]["tickSize"]),
                         step_size=float(filters["LOT_SIZE"]["stepSize"]),
                         min_notional=float(notional.get("minNotional", 0.0)), source="binance")


def grid_lines(lower: float, upper: float, grids: int, grid_type: GridType, tick: float) -> np.ndarray:
    """The grids + 1 price lines from lower to upper, rounded to the tick."""
    i = np.arange(grids + 1, dtype=float)
    if grid_type == "geometric":
        raw = lower * (upper / lower) ** (i / grids)
    else:
        raw = lower + (upper - lower) * i / grids
    lines = np.asarray(round_tick(raw, tick), dtype=float)
    if lines[0] <= 0:
        raise ValueError(f"The lower price is below this coin's price step ({tick:g})")
    if np.any(np.diff(lines) <= 0):
        raise ValueError(f"The grid lines are closer together than this coin's price step ({tick:g}); "
                         "use fewer grids or a wider range")
    return lines


def profit_per_grid_pct(lines: np.ndarray, fee: float) -> tuple[float, float]:
    """Binance's Profit/Grid after fees, (1 - c) * sell / buy - 1 - c, as (min %, max %) over the grids."""
    pct = ((1 - fee) * lines[1:] / lines[:-1] - 1 - fee) * 100
    return float(pct.min()), float(pct.max())


def _pct(x: float, of: float) -> float:
    return x / of * 100 if of else 0.0


def _row(binance: Optional[float], app: float) -> Optional[CompareRow]:
    if binance is None:
        return None
    diff = app - binance
    return CompareRow(binance=binance, app=round(app, 8), diff=round(diff, 8),
                      diff_pct=round(diff / abs(binance) * 100, 4) if binance else None)


def error_text(exc: Exception) -> str:
    """A readable message for a pydantic ValidationError (or any ValueError)."""
    if isinstance(exc, ValidationError):
        msgs = []
        for e in exc.errors():
            msg = str(e.get("msg", "")).removeprefix("Value error, ")
            loc = ".".join(str(p) for p in e.get("loc", ()) if p != "params")
            msgs.append(f"{loc}: {msg}" if loc else msg)
        return "; ".join(msgs) or "Invalid grid bot settings"
    return str(exc)


# -------------------------------------------------------------- simulate --


def simulate(df: pd.DataFrame, params: GridBotParams, filters: SymbolFilters, *, now: Optional[float] = None,
             source: str = "binance", binance: Optional[BinanceShows] = None, curve: bool = False) -> GridBotResult:
    """Replay a Spot Grid bot over 1m bars (`df`: time/open/high/low/close, oldest first, from the start on).

    `now` (UNIX s) sets the runtime and the 24h window; default: the end of the newest bar. `curve` adds the
    bot's value over time (`equity`). Pure: no I/O.
    Raises ValueError when the bot cannot be built (no candles, investment too small for the lot step...)."""
    c = params.fee
    times = df["time"].to_numpy(np.int64)
    start = resolve_start(params, now if now is not None else (int(times[-1]) + BAR if len(times) else time.time()))
    keep = times >= start
    if params.end_time is not None:
        keep &= times < params.end_time
    df = df[keep]
    if df.empty:
        raise ValueError("No 1-minute candles for this bot's period yet")
    t = df["time"].to_numpy(np.int64)
    o, h, lo, cl = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    data_end = int(t[-1])
    now = float(now) if now is not None else data_end + BAR
    if params.end_time is not None:
        now = min(now, float(params.end_time))

    # Path points: open → nearer extreme → farther extreme → close, four per bar.
    high_first = (h - o) < (o - lo)
    pts = np.column_stack([o, np.where(high_first, h, lo), np.where(high_first, lo, h), cl]).ravel()
    n_pts = len(pts)

    lines = grid_lines(params.lower, params.upper, params.grids, params.grid_type, filters.tick_size)
    n = params.grids
    ppg_min, ppg_max = profit_per_grid_pct(lines, c)
    base_asset, quote_asset = split_symbol(params.symbol)
    notes: list[str] = []

    # ---- trigger: the grid is laid out when the price first reaches it (else at the first bar's open).
    first, p0, triggered_at = 1, float(pts[0]), None
    if params.trigger_price is not None and params.trigger_price != pts[0]:
        trig = params.trigger_price
        hit = (pts[1:] >= trig) if trig > pts[0] else (pts[1:] <= trig)
        if not hit.any():
            return _waiting(params, filters, lines, t, cl, start, now, source, ppg_min, ppg_max, binance, df)
        first = int(hit.argmax()) + 1
        p0, triggered_at = float(trig), int(t[first // 4])

    # ---- layout: no order on the line nearest the price, buys below, sells above, one quantity for all.
    gap0 = int(np.argmin(np.abs(lines - p0)))
    n_sell0 = n - gap0
    q, _ = _quantity(params, filters, lines, gap0, n_sell0, p0, notes)
    base0 = n_sell0 * q
    quote0 = params.investment - base0 * p0 * (1 + c)
    fees0 = base0 * p0 * c

    # ---- take profit / stop loss: the first path point at or beyond the price stops the bot there.
    stop_at, stop_price, stop_reason = n_pts, None, None
    for price, reason, beyond in ((params.take_profit, "take_profit", np.greater_equal),
                                  (params.stop_loss, "stop_loss", np.less_equal)):
        if price is None:
            continue
        hit = beyond(pts[first:], price)
        if hit.any() and first + int(hit.argmax()) < stop_at:
            stop_at, stop_price, stop_reason = first + int(hit.argmax()), float(price), reason
    last_v = min(stop_at, n_pts - 1)
    walk = pts[first:last_v + 1].copy()
    if stop_price is not None:
        walk[-1] = stop_price  # the leg ends where the stop triggered

    # Down to p: every buy on lines >= p fills → gap = first line >= p. Up to p: every sell on lines <= p.
    down = np.searchsorted(lines, walk, "left").tolist()
    up = (np.searchsorted(lines, walk, "right") - 1).tolist()
    g = gap0
    fills: list[tuple[int, int, int]] = []  # (path point, line, +1 buy / -1 sell)
    append = fills.append
    for i, (k, j) in enumerate(zip(down, up), start=first):
        if k < g:
            for line in range(g - 1, k - 1, -1):
                append((i, line, 1))
            g = k
        elif j > g:
            for line in range(g + 1, j + 1):
                append((i, line, -1))
            g = j

    # ---- accounting
    if fills:
        fv, fk, fs = (np.fromiter(col, dtype=np.int64, count=len(fills)) for col in zip(*fills))
    else:
        fv = fk = fs = np.zeros(0, dtype=np.int64)
    ft = t[fv // 4]
    fp = lines[fk]
    is_buy, is_sell = fs > 0, fs < 0
    profit_line = np.zeros(n + 1)
    profit_line[1:] = q * (lines[1:] * (1 - c) - lines[:-1] * (1 + c))
    sell_profit = profit_line[fk[is_sell]]
    grid_profit = float(sell_profit.sum())
    buy_quote, sell_quote = float(q * fp[is_buy].sum()), float(q * fp[is_sell].sum())
    quote = quote0 - buy_quote * (1 + c) + sell_quote * (1 - c)
    base = base0 + q * (int(is_buy.sum()) - int(is_sell.sum()))
    base = 0.0 if abs(base) < q * 1e-6 else base
    fees = fees0 + (buy_quote + sell_quote) * c

    status: Literal["running", "stopped"] = "running"
    stopped_at = None
    last_price = float(cl[-1])
    if stop_price is not None:
        status, stopped_at, last_price = "stopped", int(t[stop_at // 4]), stop_price
    elif params.end_time is not None and params.end_time <= data_end + BAR:
        status, stop_reason, stopped_at = "stopped", "ended", int(params.end_time)
    extra_fills: list[GridFill] = []
    if status == "stopped" and params.sell_on_stop and base > 0:
        quote += base * last_price * (1 - c)
        fees += base * last_price * c
        extra_fills.append(GridFill(time=int(stopped_at or data_end), side="sell", price=last_price,
                                    qty=round(base, 10), kind="stop"))
        base = 0.0

    value = quote + base * last_price
    total = value - params.investment
    floating = total - grid_profit
    end_moment = float(stopped_at) if stopped_at is not None else now
    runtime_min = max(1, int((end_moment - start) // BAR))
    year_factor = MINUTES_PER_YEAR / runtime_min

    # ---- report
    open_orders: list[GridOrder] = []
    if status == "running":
        open_orders = [GridOrder(side="buy" if k < g else "sell", price=float(lines[k]), qty=q, line=k)
                       for k in range(n + 1) if k != g]
    initial = GridFill(time=triggered_at if triggered_at is not None else int(t[0]), side="buy", price=p0,
                       qty=base0, kind="initial")
    tail = slice(max(0, len(fills) - RECENT_FILLS), len(fills))
    profit_of = np.zeros(len(fills))
    profit_of[is_sell] = sell_profit
    recent = [GridFill(time=int(ft[i]), side="buy" if fs[i] > 0 else "sell", price=float(fp[i]), qty=q,
                       line=int(fk[i]), profit=round(float(profit_of[i]), 10) if fs[i] < 0 else None)
              for i in range(tail.start, tail.stop)]
    if base0 > 0 and len(fills) < RECENT_FILLS:
        recent.insert(0, initial)
    recent = (recent + extra_fills)[::-1][:RECENT_FILLS]

    max_dd, in_range_pct, equity = _value_path(t, cl, fv, fs, fp, q, c, base0, quote0, first, last_v, stop_price,
                                               params, curve)

    sell_t = ft[is_sell]
    matched_24h = int((sell_t >= now - DAY).sum())
    daily = _daily(start, int(end_moment), sell_t, sell_profit)

    if source == "synthetic":
        notes.append("Demo data: Binance was unreachable, so these numbers come from synthetic prices.")
    if filters.source != "binance":
        notes.append(f"Price step {filters.tick_size:g} and lot step {filters.step_size:g} are estimates "
                     "(Binance's exchange info was unavailable).")
    if params.trigger_price is not None and triggered_at is not None:
        notes.append(f"Started when the price reached the trigger {params.trigger_price:g}.")
    if status == "running" and not params.lower <= last_price <= params.upper:
        side = "above" if last_price > params.upper else "below"
        notes.append(f"The price is {side} the grid range, so the bot is not trading until it comes back.")
    if status == "stopped":
        why = {"take_profit": "take profit", "stop_loss": "stop loss",
               "ended": "you stopping it"}[stop_reason or "ended"]
        notes.append(f"The bot stopped on {why}" + (" and sold its base." if params.sell_on_stop else "."))

    result = GridBotResult(
        symbol=params.symbol, base_asset=base_asset, quote_asset=quote_asset, data_source=source, status=status,
        stop_reason=stop_reason, start_time=start, triggered_at=triggered_at, stopped_at=stopped_at,
        data_end=data_end, bars=len(t), runtime_minutes=runtime_min, runtime_text=format_runtime(runtime_min),
        grid_type=params.grid_type, lines=[float(x) for x in lines], gap_line=g if status == "running" else None,
        qty_per_order=q, start_price=p0, last_price=last_price,
        in_range=params.lower <= last_price <= params.upper,
        position_pct=round(_pct(last_price - params.lower, params.upper - params.lower), 2),
        investment=params.investment, matched_trades=int(is_sell.sum()), matched_trades_24h=matched_24h,
        grid_profit=round(grid_profit, 8), grid_profit_pct=round(_pct(grid_profit, params.investment), 4),
        floating_pnl=round(floating, 8), floating_pnl_pct=round(_pct(floating, params.investment), 4),
        total_pnl=round(total, 8), total_pnl_pct=round(_pct(total, params.investment), 4),
        grid_apr_pct=round(_pct(grid_profit, params.investment) * year_factor, 2),
        total_apr_pct=round(_pct(total, params.investment) * year_factor, 2),
        profit_per_grid_min_pct=round(ppg_min, 4), profit_per_grid_max_pct=round(ppg_max, 4),
        fee_rate=c, fees_paid=round(fees, 8), base_held=round(base, 10), quote_held=round(quote, 8),
        current_value=round(value, 8), open_orders=open_orders, recent_fills=recent, daily=daily,
        filters=filters, notes=notes, max_drawdown_pct=max_dd, time_in_range_pct=in_range_pct, equity=equity,
    )
    if binance is not None:
        result.comparison = _compare(df, params, filters, binance, result, source)
    return result


def _value_path(t: np.ndarray, cl: np.ndarray, fv: np.ndarray, fs: np.ndarray, fp: np.ndarray, q: float, c: float,
                base0: float, quote0: float, first: int, last_v: int, stop_price: Optional[float],
                params: GridBotParams, curve: bool) -> tuple[float, float, Optional[list[tuple[int, float]]]]:
    """The bot's value at each 1m close from the bar it started trading in to the bar it stopped in → (max drawdown
    %, % of those closes inside the range, the value path downsampled to EQUITY_POINTS when `curve`)."""
    b0, b1 = first // 4, last_v // 4
    n = b1 - b0 + 1
    if n <= 0:
        return 0.0, 0.0, [] if curve else None
    bar = fv // 4 - b0
    buy, sell = fs > 0, fs < 0
    d_base = np.bincount(bar, weights=q * fs.astype(float), minlength=n)[:n]
    d_quote = (np.bincount(bar[sell], weights=q * fp[sell] * (1 - c), minlength=n)[:n]
               - np.bincount(bar[buy], weights=q * fp[buy] * (1 + c), minlength=n)[:n])
    price = cl[b0:b1 + 1].astype(float)
    if stop_price is not None:
        price[-1] = stop_price
    value = quote0 + np.cumsum(d_quote) + (base0 + np.cumsum(d_base)) * price
    peak = np.maximum.accumulate(np.maximum(value, params.investment))
    max_dd = float(((peak - value) / peak).max() * 100)
    inside = float(((price >= params.lower) & (price <= params.upper)).mean() * 100)
    equity = None
    if curve:
        idx = np.unique(np.linspace(0, n - 1, min(n, EQUITY_POINTS)).astype(np.int64))
        equity = [(int(t[b0 + i]), round(float(value[i]), 6)) for i in idx]
    return round(max(0.0, max_dd), 4), round(inside, 2), equity


def _quantity(params: GridBotParams, filters: SymbolFilters, lines: np.ndarray, gap: int, n_sell: int, p0: float,
              notes: list[str]) -> tuple[float, float]:
    """Qty per order: Binance's own when given, else the most the investment covers, rounded down to the step."""
    c = params.fee
    denom = float(lines[:gap].sum()) + n_sell * p0 * (1 + c)  # quote needed per unit of quantity
    step = filters.step_size
    if params.qty_per_order is not None:
        q = floor_step(params.qty_per_order, step) or params.qty_per_order
        if q * denom > params.investment * 1.02:
            notes.append(f"Qty per order {q:g} needs about {q * denom:,.2f} {split_symbol(params.symbol)[1]}, more "
                         "than the investment entered.")
        return q, denom
    q = floor_step(params.investment / denom, step)
    if q <= 0:
        raise ValueError(f"The investment is too small: each of the {params.grids} orders would be below the "
                         f"minimum quantity step {step:g}. It needs at least about {step * denom:,.2f}.")
    smallest = q * float(lines[0])
    if filters.min_notional and smallest < filters.min_notional:
        need = math.ceil(filters.min_notional / float(lines[0]) / step) * step * denom
        msg = (f"each order would be worth about {smallest:,.2f}, below Binance's minimum order of "
               f"{filters.min_notional:g} for {params.symbol}. Use at least about {need:,.2f} or fewer grids.")
        if filters.source == "binance":
            raise ValueError("The investment is too small: " + msg)
        notes.append("Possibly too small: " + msg)
    return q, denom


def _daily(start: int, end: int, sell_t: np.ndarray, sell_profit: np.ndarray) -> list[GridDay]:
    """Matched trades and grid profit per UTC day, every day from the start to the end (zeros included)."""
    d0, d1 = start - start % DAY, end - end % DAY
    days = (d1 - d0) // DAY + 1
    idx = ((sell_t - d0) // DAY).astype(np.int64) if len(sell_t) else np.zeros(0, dtype=np.int64)
    idx = np.clip(idx, 0, days - 1)
    counts = np.bincount(idx, minlength=days)
    profit = np.bincount(idx, weights=sell_profit, minlength=days)
    return [GridDay(day=d0 + i * DAY, matched=int(counts[i]), grid_profit=round(float(profit[i]), 8))
            for i in range(days)]


def _waiting(params: GridBotParams, filters: SymbolFilters, lines: np.ndarray, t: np.ndarray, cl: np.ndarray,
             start: int, now: float, source: str, ppg_min: float, ppg_max: float,
             binance: Optional[BinanceShows], df: pd.DataFrame) -> GridBotResult:
    """The trigger price has not been reached: nothing bought, nothing placed."""
    trig = float(params.trigger_price or 0.0)
    gap = int(np.argmin(np.abs(lines - trig)))
    notes: list[str] = []
    q, _ = _quantity(params, filters, lines, gap, params.grids - gap, trig, notes)
    end_moment = min(now, float(params.end_time)) if params.end_time is not None else now
    runtime_min = max(1, int((end_moment - start) // BAR))
    last = float(cl[-1])
    base_asset, quote_asset = split_symbol(params.symbol)
    notes.insert(0, f"Waiting: the price has not reached the trigger {trig:g} yet, so no orders are placed.")
    if source == "synthetic":
        notes.append("Demo data: Binance was unreachable, so these numbers come from synthetic prices.")
    result = GridBotResult(
        symbol=params.symbol, base_asset=base_asset, quote_asset=quote_asset, data_source=source, status="waiting",
        start_time=start, data_end=int(t[-1]), bars=len(t), runtime_minutes=runtime_min,
        runtime_text=format_runtime(runtime_min), grid_type=params.grid_type, lines=[float(x) for x in lines],
        qty_per_order=q, start_price=None, last_price=last, in_range=params.lower <= last <= params.upper,
        position_pct=round(_pct(last - params.lower, params.upper - params.lower), 2),
        investment=params.investment, matched_trades=0, matched_trades_24h=0, grid_profit=0.0, grid_profit_pct=0.0,
        floating_pnl=0.0, floating_pnl_pct=0.0, total_pnl=0.0, total_pnl_pct=0.0, grid_apr_pct=0.0,
        total_apr_pct=0.0, profit_per_grid_min_pct=round(ppg_min, 4), profit_per_grid_max_pct=round(ppg_max, 4),
        fee_rate=params.fee, fees_paid=0.0, base_held=0.0, quote_held=params.investment,
        current_value=params.investment, open_orders=[], recent_fills=[],
        daily=_daily(start, int(end_moment), np.zeros(0, dtype=np.int64), np.zeros(0)), filters=filters, notes=notes,
    )
    if binance is not None:
        result.comparison = _compare(df, params, filters, binance, result, source)
    return result


def _compare(df: pd.DataFrame, params: GridBotParams, filters: SymbolFilters, binance: BinanceShows,
             result: GridBotResult, source: str) -> GridComparison:
    """App vs Binance, with the app's numbers taken when Binance's were copied (they keep moving after)."""
    at = binance.captured_at
    app = result
    if at is not None and at < result.data_end and at >= result.start_time:
        cut = df[df["time"].to_numpy(np.int64) <= at]
        try:
            app = simulate(cut, params.model_copy(update={"start_time": result.start_time, "runtime": None}),
                           filters, now=float(at), source=source)
        except ValueError:  # no candles before that moment: compare with the numbers now
            app = result
    when = int(at) if at is not None else result.data_end + BAR
    return GridComparison(
        at=when,
        matched_trades=_row(None if binance.matched_trades is None else float(binance.matched_trades),
                            float(app.matched_trades)),
        grid_profit=_row(binance.grid_profit, app.grid_profit),
        total_pnl=_row(binance.total_pnl, app.total_pnl),
    )


def describe_result(result: GridBotResult, name: str = "") -> str:
    """One plain-English line for the chat agent, e.g. "BTCUSDT grid 60,000–70,000 (20 arithmetic grids), running
    12d 3h 45m: 34 matched trades (5 in 24h), grid profit +12.40 USDT (+1.24%), total PnL -3.10 USDT (-0.31%)…"."""
    q = result.quote_asset or "quote"
    lo, hi = result.lines[0], result.lines[-1]
    state = {"running": f"running {result.runtime_text}", "waiting": "waiting for its trigger price",
             "stopped": f"stopped after {result.runtime_text}"}[result.status]
    text = (f"{name or result.symbol} grid {lo:,.10g}–{hi:,.10g} ({len(result.lines) - 1} {result.grid_type} grids), "
            f"{state}: {result.matched_trades} matched trades ({result.matched_trades_24h} in 24h), grid profit "
            f"{result.grid_profit:+,.2f} {q} ({result.grid_profit_pct:+.2f}%), floating "
            f"{result.floating_pnl:+,.2f} {q}, "
            f"total PnL {result.total_pnl:+,.2f} {q} ({result.total_pnl_pct:+.2f}%), grid APR "
            f"{result.grid_apr_pct:.1f}%. Price {result.last_price:,.10g} is "
            + ("inside the range." if result.in_range else "outside the range."))
    if result.data_source == "synthetic":
        text += " (Demo data, not real prices.)"
    return text


# --------------------------------------------------------------- service --


class _Market(Protocol):
    async def get_range(self, symbol: str, interval: str, start: int,
                        end: Optional[int] = None) -> tuple[pd.DataFrame, str]: ...

    def binance_usable(self) -> bool: ...


class GridBotService:
    """Saved grid bots (JSON file at GRIDBOTS_STORE, or `memory`) and their results, cached per 1m bar."""

    def __init__(self, market: _Market, settings: Settings | None = None, store: str | None = None) -> None:
        self.market = market
        s = settings or get_settings()
        path = (store if store is not None else s.gridbots_store).strip()
        self._store = None if path.lower() in ("", "memory", "none", "off") else Path(path)
        self._bots: dict[str, GridBot] = {}
        self._results: dict[str, tuple[tuple, GridBotResult]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._filters: dict[str, tuple[float, Optional[SymbolFilters]]] = {}
        self._load()

    # ------------------------------------------------------------ running
    async def simulate(self, params: GridBotParams, binance: Optional[BinanceShows] = None,
                       now: Optional[float] = None) -> GridBotResult:
        """Fetch the bot's 1m candles and the pair's tick/step sizes, then replay it. Nothing is saved.
        Raises ValueError for settings that cannot work and MarketDataError when candles cannot be fetched."""
        now = time.time() if now is None else now
        start = resolve_start(params, now)
        params = params.model_copy(update={"start_time": start})
        filters = await self.symbol_filters(params.symbol)
        df, source = await self.market.get_range(params.symbol, INTERVAL, start, params.end_time)
        if filters is None:
            filters = estimate_filters(params.lower)
        return await asyncio.to_thread(simulate, df, params, filters, now=now, source=source, binance=binance)

    async def simulate_from(self, *, symbol: str, lower: float, upper: float, grids: int, investment: float,
                            runtime: Optional[str] = None, start_time: Optional[int] = None,
                            grid_type: GridType = "arithmetic", **options: Any) -> GridBotResult:
        """Chat-agent entry point: simulate a bot from parsed fields (options: fee_rate, bnb_discount,
        trigger_price, take_profit, stop_loss, sell_on_stop, end_time, qty_per_order). ValueError on bad input."""
        try:
            params = GridBotParams(symbol=symbol, lower=lower, upper=upper, grids=grids, investment=investment,
                                   runtime=runtime, start_time=start_time, grid_type=grid_type, **options)
        except ValidationError as exc:
            raise ValueError(error_text(exc)) from None
        return await self.simulate(params)

    async def symbol_filters(self, symbol: str) -> Optional[SymbolFilters]:
        """Tick/step/min-notional from Binance's exchangeInfo, cached; None when Binance is unreachable.
        Raises ValueError when Binance says the pair does not exist."""
        hit = self._filters.get(symbol)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        found: Optional[SymbolFilters] = None
        ttl = FILTERS_RETRY
        getter = getattr(self.market, "_binance_get", None)
        if getter is not None and self.market.binance_usable():
            try:
                resp = await getter("/api/v3/exchangeInfo", params={"symbol": symbol})
                if resp.status_code == 400:
                    raise LookupError(symbol)
                resp.raise_for_status()
                found, ttl = parse_filters(resp.json()), FILTERS_TTL
            except LookupError:
                raise ValueError(f"Binance has no spot pair {symbol}") from None
            except Exception as exc:  # offline, geo-blocked, odd payload: estimate from the price instead
                log.warning("exchangeInfo for %s failed (%s); estimating tick and lot sizes", symbol, exc)
        self._filters[symbol] = (time.monotonic() + ttl, found)
        return found

    # --------------------------------------------------------------- CRUD
    def list(self) -> list[GridBot]:
        return sorted(self._bots.values(), key=lambda b: b.created_at)

    def get(self, bot_id: str) -> Optional[GridBot]:
        return self._bots.get(bot_id)

    async def create(self, req: GridBotCreate, now: Optional[float] = None) -> tuple[GridBot, GridBotResult]:
        """Save a bot (its runtime pinned to a start time now) after checking it simulates."""
        if len(self._bots) >= MAX_BOTS:
            raise ValueError(f"At most {MAX_BOTS} grid bots; delete one first")
        now = time.time() if now is None else now
        params = self._pin(req.params, now)
        binance = self._stamp(req.binance, now)
        result = await self.simulate(params, binance, now)
        ts = int(now)
        name = req.name.strip() or f"{params.symbol} {params.lower:g}–{params.upper:g}"
        bot = GridBot(id=uuid.uuid4().hex[:10], name=name, params=params, binance=binance, created_at=ts,
                      updated_at=ts)
        self._bots[bot.id] = bot
        self._results[bot.id] = (self._cache_key(bot, now), result)
        self._save()
        return bot, result

    async def update(self, bot_id: str, patch: GridBotPatch,
                     now: Optional[float] = None) -> Optional[tuple[GridBot, GridBotResult]]:
        bot = self._bots.get(bot_id)
        if bot is None:
            return None
        now = time.time() if now is None else now
        changes: dict[str, Any] = {"updated_at": int(now)}
        if patch.name is not None and patch.name.strip():
            changes["name"] = patch.name.strip()
        if patch.params:
            merged = bot.params.model_dump()
            merged.update(patch.params)
            if patch.params.get("runtime") and "start_time" not in patch.params:
                merged["start_time"] = None  # a new runtime means a new start
            elif patch.params.get("start_time") is not None and "runtime" not in patch.params:
                merged["runtime"] = None
            try:
                changes["params"] = self._pin(GridBotParams.model_validate(merged), now)
            except ValidationError as exc:
                raise ValueError(error_text(exc)) from None
        if "binance" in patch.model_fields_set:
            changes["binance"] = self._stamp(patch.binance, now)
        new = bot.model_copy(update=changes)
        result = await self.simulate(new.params, new.binance, now)  # validates before anything is saved
        self._bots[bot_id] = new
        self._results[bot_id] = (self._cache_key(new, now), result)
        self._save()
        return new, result

    def delete(self, bot_id: str) -> bool:
        if self._bots.pop(bot_id, None) is None:
            return False
        self._results.pop(bot_id, None)
        self._locks.pop(bot_id, None)
        self._save()
        return True

    async def result(self, bot_id: str, now: Optional[float] = None) -> Optional[GridBotResult]:
        """The bot's numbers now; recomputed at most once per 1m bar (concurrent calls share the work)."""
        bot = self._bots.get(bot_id)
        if bot is None:
            return None
        now = time.time() if now is None else now
        lock = self._locks.setdefault(bot_id, asyncio.Lock())
        async with lock:
            bot = self._bots.get(bot_id) or bot
            key = self._cache_key(bot, now)
            cached = self._results.get(bot_id)
            if cached and cached[0] == key:
                return cached[1]
            result = await self.simulate(bot.params, bot.binance, now)
            if bot_id in self._bots:
                self._results[bot_id] = (key, result)
            return result

    @staticmethod
    def _cache_key(bot: GridBot, now: float) -> tuple:
        return bot.updated_at, bot.params.model_dump_json(), bot.binance and bot.binance.model_dump_json(), \
            int(now) // BAR

    @staticmethod
    def _pin(params: GridBotParams, now: float) -> GridBotParams:
        """Saved bots keep a fixed start: a runtime is turned into a start time once, when it is entered."""
        return params.model_copy(update={"start_time": resolve_start(params, now)})

    @staticmethod
    def _stamp(binance: Optional[BinanceShows], now: float) -> Optional[BinanceShows]:
        if binance is None or (binance.matched_trades is None and binance.grid_profit is None
                                and binance.total_pnl is None):
            return None
        if binance.captured_at is None:
            return binance.model_copy(update={"captured_at": int(now)})
        return binance

    # -------------------------------------------------------- persistence
    def _load(self) -> None:
        if not self._store or not self._store.exists():
            return
        try:
            rows = json.loads(self._store.read_text()).get("bots", [])
        except (OSError, ValueError, AttributeError) as exc:
            log.warning("Could not read %s (%s); starting with no grid bots", self._store, exc)
            return
        for row in rows:
            try:
                bot = GridBot.model_validate(row)
            except ValueError as exc:
                log.warning("Skipping invalid stored grid bot: %s", exc)
                continue
            self._bots[bot.id] = bot
        if self._bots:
            log.info("Loaded %d grid bots", len(self._bots))

    def _save(self) -> None:
        if not self._store:
            return
        try:
            self._store.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._store.with_suffix(".tmp")
            tmp.write_text(json.dumps({"bots": [b.model_dump() for b in self._bots.values()]}))
            tmp.replace(self._store)
        except OSError as exc:
            log.warning("Could not save grid bots to %s: %s", self._store, exc)
