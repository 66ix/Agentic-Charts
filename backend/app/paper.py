"""Spot paper trading: a pretend USDT wallet that buys and sells coins on real prices.

The wallet stores only what the trader decided: a starting balance and a list of orders (market, limit, or a stop
that sells). Everything else, which orders filled, at what price, what is held, the average cost and the PnL, is
worked out again from 1m candles every time, the same way the trade journal is, so editing or cancelling an order
simply re-plays the wallet and it never drifts from the market. The rules are conservative:

* A market order fills when it is placed, at the last price the app saw then (stored on the order).
* A limit or stop order only sees candles that open at or after the time it was placed, up to the candle it was
  cancelled in. A limit buy fills when a candle trades down to its price, at that price (at the open when the candle opened
  below it, as a resting order would). A limit sell fills when a candle trades up to its price, a stop sell when a
  candle trades down to its price (at the open when it gapped through).
* A sell only sells what was held before its candle, so a buy and a sell can't both fill in one candle off the same
  coins. A sell with no quantity sells everything held; one for more than is held sells what is held. A sell that
  is reached while nothing is held waits for the next buy to fill.
* Orders placed together (a plan's buy, stop and targets, a ladder's rungs and take profit) share a `group`. A group
  with buys only sells what its own buys bought, so a plan's stop never sells coins held for another plan. When a
  sell leaves the group holding nothing and none of its buys is still waiting, its other pending sells are cancelled
  ("position closed"), so a stop and its targets act like an OCO order.
* Fees (`fee_pct`, 0.1% like Binance spot) are charged on every fill in USDT.
* Cash for a buy is set aside when it is placed (the order is refused when there isn't enough), and given back if
  it is cancelled.

Only spot: there is no shorting and no leverage. While Binance is unreachable the last real result is kept rather
than replaced by synthetic demo prices.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Optional

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, field_validator, model_validator

from .alerts import read_store, store_path, write_store
from .config import Settings, get_settings
from .journal import resolution_for
from .market_data import MarketDataError
from .schemas import norm_symbol

if TYPE_CHECKING:
    from .market_data import MarketData

log = logging.getLogger(__name__)

DEFAULT_CASH = 10_000.0
DEFAULT_FEE = 0.1  # % per fill, Binance spot without BNB
MAX_ORDERS = 3000
EPS = 1e-12

Side = Literal["buy", "sell"]
OrderType = Literal["market", "limit", "stop"]
OrderStatus = Literal["pending", "filled", "cancelled"]
Source = Literal["manual", "plan", "ladder", "agent"]


# ----------------------------------------------------------------- models --


class NewPaperOrder(BaseModel):
    """An order as the trader places it. Mirrors NewPaperOrder in frontend/lib/paper.ts."""

    symbol: str
    side: Side
    type: OrderType = "limit"
    price: Optional[float] = Field(None, gt=0, description="Limit or stop price; market orders use the last price")
    qty: Optional[float] = Field(None, gt=0, description="Coins; a sell without it sells everything held")
    quote: Optional[float] = Field(None, gt=0, description="Buys: USDT to spend, fees included, instead of qty")
    group: Optional[str] = Field(None, max_length=64, description="Orders placed together (plan, ladder)")
    source: Source = "manual"
    note: str = Field("", max_length=300)

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, v: str) -> str:
        return norm_symbol(v)

    @model_validator(mode="after")
    def _check(self) -> "NewPaperOrder":
        if self.type != "market" and self.price is None:
            raise ValueError(f"A {self.type} order needs a price")
        if self.type == "stop" and self.side != "sell":
            raise ValueError("Stop orders sell; spot paper trading has no stop buys")
        if self.side == "buy" and self.qty is None and self.quote is None:
            raise ValueError("A buy needs qty (coins) or quote (USDT to spend)")
        if self.side == "sell" and self.quote is not None:
            raise ValueError("A sell takes qty (coins), or nothing to sell everything held")
        return self


class PaperOrder(NewPaperOrder):
    id: str
    placed_at: int = Field(..., description="UNIX seconds")
    cancelled_at: Optional[int] = None
    market_price: Optional[float] = Field(None, description="Market orders: the price it filled at")


class OrderResult(BaseModel):
    """What happened to an order in the replay."""

    status: OrderStatus
    filled_at: Optional[int] = None
    fill_price: Optional[float] = None
    fill_qty: Optional[float] = None
    fee: float = 0.0
    pnl: Optional[float] = Field(None, description="Sells: proceeds after fees minus the average cost of what sold")
    reason: str = ""


class Holding(BaseModel):
    symbol: str
    qty: float
    avg_cost: float = Field(..., description="Average price paid per coin, fees included")
    last_price: Optional[float] = None
    value: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    unrealized_pct: Optional[float] = None
    realized_pnl: float = 0.0


class WalletStats(BaseModel):
    sells: int = 0
    wins: int = 0
    win_rate: Optional[float] = None
    best: Optional[float] = None
    worst: Optional[float] = None
    fees: float = 0.0


class Wallet(BaseModel):
    """The replayed wallet, as the API returns it. Mirrors PaperWallet in frontend/lib/paper.ts."""

    start_cash: float
    fee_pct: float
    created_at: int
    cash: float = Field(..., description="USDT free to spend")
    reserved: float = Field(..., description="USDT set aside for pending buys")
    holdings_value: float
    equity: float = Field(..., description="Cash + reserved + holdings at the last price")
    return_pct: float
    realized_pnl: float
    unrealized_pnl: float
    holdings: list[Holding]
    orders: list[dict] = Field(default_factory=list, description="Newest first: the order with its `result`")
    stats: WalletStats
    data_source: str = "binance"
    error: Optional[str] = None
    evaluated_at: int


# ----------------------------------------------------------------- replay --


@dataclass
class _Book:
    """One coin's position while replaying."""

    held: float = 0.0
    avg_cost: float = 0.0
    realized: float = 0.0
    fills: list[tuple[int, float]] = field(default_factory=list)  # (time, signed qty)

    def held_before(self, t: int, strict: bool) -> float:
        return max(0.0, sum(q for ft, q in self.fills if (ft < t if strict else ft <= t)))


def reserve_for(o: PaperOrder, fee: float, price: Optional[float] = None) -> float:
    """USDT a buy sets aside when placed."""
    if o.side != "buy":
        return 0.0
    if o.quote is not None:
        return float(o.quote)
    px = price or o.market_price or o.price or 0.0
    return float(o.qty or 0.0) * px * (1 + fee)


def _touch(o: PaperOrder, df: pd.DataFrame, start: int, end: Optional[int]) -> Optional[tuple[int, float]]:
    """The first candle opening in [start, end) that reaches `o`'s price → (its open time, fill price)."""
    if df.empty:
        return None
    times = df["time"].to_numpy(dtype=np.int64)
    a = int(np.searchsorted(times, start, "left"))
    b = len(times) if end is None else int(np.searchsorted(times, end, "left"))
    if a >= b:
        return None
    px = float(o.price or 0.0)
    if o.side == "buy" or o.type == "stop":
        hit = df["low"].to_numpy(dtype=float)[a:b] <= px
    else:
        hit = df["high"].to_numpy(dtype=float)[a:b] >= px
    idx = np.flatnonzero(hit)
    if not len(idx):
        return None
    i = a + int(idx[0])
    op = float(df["open"].iloc[i])
    if o.side == "buy" or o.type == "stop":
        return int(times[i]), min(op, px)  # gapped down through it: filled at the open
    return int(times[i]), max(op, px)


def simulate_symbol(orders: list[PaperOrder], df: pd.DataFrame, fee: float) -> tuple[dict[str, OrderResult], _Book]:
    """Replays one coin's orders on its candles (oldest first) → each order's result and the position."""
    book = _Book()
    results: dict[str, OrderResult] = {}
    pending = sorted(orders, key=lambda o: (o.placed_at, o.id))
    eligible: dict[str, int] = {o.id: o.placed_at for o in pending}
    waiting: set[str] = set()  # sells reached while nothing was held
    # A group with buys (a plan, a ladder) only sells what its own buys bought, so a plan's stop never sells coins
    # another plan or a manual buy holds.
    owned = {o.group for o in pending if o.group and o.side == "buy"}
    gfills: dict[str, list[tuple[int, float]]] = {g: [] for g in owned}

    def group_held(g: str, t: int, strict: bool) -> float:
        return max(0.0, sum(q for ft, q in gfills[g] if (ft < t if strict else ft <= t)))
    while True:
        best: Optional[tuple[int, int, int, str, float]] = None
        by_id = {}
        for o in pending:
            if o.id in results or o.id in waiting:
                continue
            by_id[o.id] = o
            if o.type == "market":
                if o.cancelled_at is not None and o.cancelled_at <= o.placed_at:
                    continue
                ev = (o.placed_at, float(o.market_price or 0.0))
            else:
                # Candles that open at or after placement, up to the one a cancel happened in.
                ev = _touch(o, df, eligible[o.id], o.cancelled_at)
            if ev is None:
                continue
            key = (ev[0], 0 if o.side == "buy" else 1, o.placed_at, o.id, ev[1])
            if best is None or key < best:
                best = key
        if best is None:
            break
        t, _, _, oid, px = best
        o = by_id[oid]
        if o.side == "buy":
            qty = o.qty if o.qty is not None else float(o.quote) / (px * (1 + fee))
            cost = qty * px
            f = cost * fee
            book.avg_cost = (book.held * book.avg_cost + cost + f) / (book.held + qty)
            book.held += qty
            book.fills.append((t, qty))
            if o.group in gfills:
                gfills[o.group].append((t, qty))
            results[oid] = OrderResult(status="filled", filled_at=t, fill_price=px, fill_qty=qty, fee=f)
            for w in list(waiting):  # a sell that waited for coins can fill from the next candle on
                eligible[w] = t + 1
                waiting.discard(w)
            continue
        strict = o.type != "market"
        have = book.held_before(t, strict)
        mine = o.group in gfills
        if mine:
            have = min(have, group_held(o.group, t, strict))
        if have <= EPS:
            now_held = min(book.held, group_held(o.group, t, False)) if mine else book.held
            if now_held > EPS:  # coins bought in this same candle: it can sell them from the next one
                eligible[oid] = t + 1
            else:
                waiting.add(oid)
            continue
        qty = min(o.qty, have) if o.qty is not None else have
        proceeds = qty * px
        f = proceeds * fee
        pnl = proceeds - f - qty * book.avg_cost
        book.realized += pnl
        book.held = max(0.0, book.held - qty)
        book.fills.append((t, -qty))
        if book.held <= EPS * max(1.0, qty):
            book.held = 0.0
        left = book.held
        if mine:
            gfills[o.group].append((t, -qty))
            left = min(left, group_held(o.group, t, False))
            pending_buys = any(b.group == o.group and b.side == "buy" and b.id not in results
                               and (b.cancelled_at is None or b.cancelled_at > t) for b in pending)
            if pending_buys:
                left = 1.0  # a ladder rung still to fill: its take profit stays
        results[oid] = OrderResult(status="filled", filled_at=t, fill_price=px, fill_qty=qty, fee=f, pnl=pnl,
                                   reason="" if o.qty is None or qty >= o.qty else f"only {qty:g} held")
        if left <= EPS * max(1.0, qty) and o.group:
            for other in pending:
                if (other.group == o.group and other.side == "sell" and other.id not in results
                        and other.cancelled_at is None):
                    results[other.id] = OrderResult(status="cancelled", filled_at=t, reason="position closed")
    for o in pending:
        if o.id not in results:
            if o.cancelled_at is not None:
                results[o.id] = OrderResult(status="cancelled", filled_at=o.cancelled_at, reason="cancelled")
            else:
                results[o.id] = OrderResult(status="pending",
                                            reason="waiting for coins to sell" if o.id in waiting else "")
    return results, book


def build_wallet(start_cash: float, fee_pct: float, created_at: int, orders: list[PaperOrder],
                 frames: dict[str, pd.DataFrame], source: str = "binance", now: Optional[float] = None) -> Wallet:
    """The whole wallet from its orders and each coin's candles. Pure."""
    fee = fee_pct / 100
    now = time.time() if now is None else now
    results: dict[str, OrderResult] = {}
    holdings: list[Holding] = []
    by_symbol: dict[str, list[PaperOrder]] = {}
    for o in orders:
        by_symbol.setdefault(o.symbol, []).append(o)
    cash, reserved, realized, unrealized, value = start_cash, 0.0, 0.0, 0.0, 0.0
    for sym, group in by_symbol.items():
        df = frames.get(sym, pd.DataFrame(columns=["time", "open", "high", "low", "close"]))
        res, book = simulate_symbol(group, df, fee)
        results.update(res)
        last = float(df["close"].iloc[-1]) if len(df) else None
        realized += book.realized
        if book.held > 0 or book.realized:
            h = Holding(symbol=sym, qty=book.held, avg_cost=book.avg_cost, last_price=last, realized_pnl=book.realized)
            if book.held > 0 and last is not None:
                h.value = book.held * last
                h.unrealized_pnl = h.value - book.held * book.avg_cost
                h.unrealized_pct = (last / book.avg_cost - 1) * 100 if book.avg_cost else None
                value += h.value
                unrealized += h.unrealized_pnl
            elif book.held > 0:
                h.value = book.held * book.avg_cost  # no price yet: valued at cost
                value += h.value
            holdings.append(h)
    stats = WalletStats()
    pnls: list[float] = []
    for o in orders:
        r = results[o.id]
        stats.fees += r.fee
        if o.side == "buy":
            if r.status == "filled":
                cash -= (r.fill_qty or 0) * (r.fill_price or 0) + r.fee
            elif r.status == "pending":
                amt = reserve_for(o, fee)
                cash -= amt
                reserved += amt
        elif r.status == "filled":
            cash += (r.fill_qty or 0) * (r.fill_price or 0) - r.fee
            if r.pnl is not None:
                pnls.append(r.pnl)
    if pnls:
        stats.sells = len(pnls)
        stats.wins = sum(p > 0 for p in pnls)
        stats.win_rate = round(stats.wins / len(pnls) * 100, 1)
        stats.best, stats.worst = max(pnls), min(pnls)
    equity = cash + reserved + value
    holdings.sort(key=lambda h: -(h.value or 0))
    rows = [{**o.model_dump(), "result": results[o.id].model_dump()}
            for o in sorted(orders, key=lambda o: o.placed_at, reverse=True)]  # stable: placement order kept
    return Wallet(start_cash=start_cash, fee_pct=fee_pct, created_at=created_at, cash=cash, reserved=reserved,
                  holdings_value=value, equity=equity, return_pct=(equity / start_cash - 1) * 100 if start_cash else 0,
                  realized_pnl=realized, unrealized_pnl=unrealized, holdings=holdings, orders=rows, stats=stats,
                  data_source=source, evaluated_at=int(now))


# ---------------------------------------------------------------- service --


class PaperService:
    """The paper wallet: orders saved to PAPER_STORE, the wallet replayed on demand (cached until the next minute)."""

    def __init__(self, market: "MarketData", settings: Optional[Settings] = None) -> None:
        self.market = market
        self.s = settings or get_settings()
        self._store = store_path(self.s.paper_store)
        self.start_cash = DEFAULT_CASH
        self.fee_pct = DEFAULT_FEE
        self.created_at = int(time.time())
        self._orders: dict[str, PaperOrder] = {}
        self._version = 0
        self._cache: Optional[tuple[int, float, Wallet]] = None  # (version, expires, wallet)
        self._last_real: Optional[Wallet] = None
        self._last_real_version = -1
        self._lock = asyncio.Lock()
        self._load()

    # ------------------------------------------------------------ reading
    def orders(self) -> list[PaperOrder]:
        return list(self._orders.values())

    async def wallet(self) -> Wallet:
        now = time.time()
        if self._cache and self._cache[0] == self._version and self._cache[1] > now:
            return self._cache[2]
        frames: dict[str, pd.DataFrame] = {}
        sources: set[str] = set()
        errors: list[str] = []
        by_symbol: dict[str, int] = {}
        for o in self._orders.values():
            by_symbol[o.symbol] = min(by_symbol.get(o.symbol, o.placed_at), o.placed_at)

        async def load(sym: str, since: int) -> None:
            try:
                res = resolution_for(since, now)
                df, src = await self.market.get_range(sym, res, min(since, int(now) - 120))
            except Exception as exc:  # network trouble must not break the wallet
                log.warning("Paper: candles for %s failed: %s", sym, exc)
                errors.append(f"{sym}: {exc}")
                return
            frames[sym], _ = df, sources.add(src)

        await asyncio.gather(*(load(s, t) for s, t in by_symbol.items()))
        source = "synthetic" if "synthetic" in sources else "binance"
        w = build_wallet(self.start_cash, self.fee_pct, self.created_at, self.orders(), frames, source, now)
        demo_fallback = source == "synthetic" and self.s.data_source != "synthetic"
        if (demo_fallback or errors) and self._last_real is not None and self._last_real_version == self._version:
            return self._last_real.model_copy(update={"error": "Binance is unreachable; showing the last real "
                                                               "result."})
        if errors:
            w.error = "Could not load candles for " + "; ".join(errors[:3])
        if not demo_fallback and not errors:
            self._last_real, self._last_real_version = w, self._version
        self._cache = (self._version, (now // 60 + 1) * 60 + 1, w)
        return w

    # ----------------------------------------------------------- writing
    async def _last_price(self, symbol: str) -> float:
        candles, _ = await self.market.get_klines(symbol, "1m", 2)
        if not candles:
            raise MarketDataError(f"No price for {symbol}")
        return float(candles[-1].close)

    async def place(self, new: list[NewPaperOrder]) -> list[PaperOrder]:
        """Places orders together (one group when there are several); refuses them all when the buys need more
        cash than is free."""
        if not new:
            return []
        async with self._lock:
            if len(self._orders) + len(new) > MAX_ORDERS:
                raise ValueError(f"At most {MAX_ORDERS} paper orders; reset the wallet to start again")
            now = int(time.time())
            group = new[0].group or (uuid.uuid4().hex[:10] if len(new) > 1 else None)
            made: list[PaperOrder] = []
            for n in new:
                data = n.model_dump()
                data["group"] = n.group or group
                o = PaperOrder(**data, id=uuid.uuid4().hex[:12], placed_at=now)
                if o.type == "market":
                    o.market_price = await self._last_price(o.symbol)
                made.append(o)
            need = sum(reserve_for(o, self.fee_pct / 100) for o in made)
            if need:
                free = (await self.wallet()).cash
                if need > free + 1e-6:
                    raise ValueError(f"Not enough paper cash: these buys need {need:,.2f} USDT and "
                                     f"{max(free, 0):,.2f} is free")
            for o in made:
                self._orders[o.id] = o
            self._changed()
            return made

    def cancel(self, order_id: str) -> Optional[PaperOrder]:
        o = self._orders.get(order_id)
        if o is None:
            return None
        if o.cancelled_at is None:
            o.cancelled_at = int(time.time())
            self._changed()
        return o

    def reset(self, start_cash: float = DEFAULT_CASH, fee_pct: float = DEFAULT_FEE) -> None:
        if not (0 < start_cash <= 1e9) or not (0 <= fee_pct <= 1):
            raise ValueError("Starting cash must be above 0 and the fee between 0% and 1%")
        self._orders.clear()
        self.start_cash, self.fee_pct, self.created_at = float(start_cash), float(fee_pct), int(time.time())
        self._last_real = None
        self._changed()

    def _changed(self) -> None:
        self._version += 1
        self._save()

    # ------------------------------------------------------- persistence
    def _load(self) -> None:
        data = read_store(self._store)
        if not data:
            return
        try:
            self.start_cash = float(data.get("start_cash", DEFAULT_CASH))
            self.fee_pct = float(data.get("fee_pct", DEFAULT_FEE))
            self.created_at = int(data.get("created_at", self.created_at))
        except (TypeError, ValueError):
            pass
        for row in data.get("orders", []):
            try:
                o = PaperOrder.model_validate(row)
            except ValueError as exc:
                log.warning("Skipping invalid paper order: %s", exc)
                continue
            self._orders[o.id] = o

    def _save(self) -> None:
        write_store(self._store, {"start_cash": self.start_cash, "fee_pct": self.fee_pct,
                                  "created_at": self.created_at,
                                  "orders": [o.model_dump() for o in self._orders.values()]})
