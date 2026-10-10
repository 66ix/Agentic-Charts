"""Trade manager: watches a live trade and says what to do with it, on Telegram/Discord and in the app.

The app never places or changes orders (the Binance key is read-only); it advises and you act on the exchange. A
managed trade is a position you are in: from the trade journal, from a position the Binance import found, or typed
in. On every closed candle of the trade's management timeframe it checks, in this order:

* **Stop**: the candle traded through the tracked stop → the trade is over (stopped, or out at breakeven/trail).
* **Targets**: a target was reached → take that target's share off; after the first one, move the stop to the entry
  (breakeven) when `breakeven_after_t1` is on.
* **Trail**: once the trade is past T1 or +1R, a newer confirmed swing low (long) or high (short) above the stop is
  a better stop: suggest it, just beyond the swing (`trail="structure"`), or ATR × `atr_mult` from the close
  (`trail="atr"`).
* **Structure**: a change of character against the position (a long's CHoCH down) → the move that justified the
  trade is breaking; suggest closing or tightening to the candle's extreme.

Each piece of advice is given once (a key per event) and kept on the trade with a suggested stop where one applies.
"I moved it" (PATCH stop) updates the tracked stop; `follow_advice` does that by itself, for tracking only.
A trade added from a Binance position is closed here once that position is no longer open on the account.
Swings are judged on the candles up to each bar with a few bars of confirmation, so nothing is suggested from a
swing that only exists in hindsight.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Literal, Optional

import pandas as pd
from pydantic import BaseModel, Field, field_validator, model_validator

from .alerts import AlertService, fmt_price, read_store, store_path, write_store
from .config import Settings, get_settings
from .indicators import structure_breaks
from .jobs import jobs
from .kimi_service import closed_only
from .notify import Notice
from .market_data import INTERVAL_SECONDS, MarketData, MarketDataError, candles_to_df
from .schemas import INTERVALS, norm_symbol
from .ta_agent import atr, find_swings

if TYPE_CHECKING:
    from .journal import JournalEntry

log = logging.getLogger(__name__)

AdviceKind = Literal["stop", "target", "breakeven", "trail", "structure", "done"]
CHECK_SECONDS = 20.0       # how often open trades look for a newly closed candle
POSITIONS_CHECK_SECONDS = 120.0  # how often managed Binance positions are checked for being closed
LOOKBACK_BARS = 300        # candles fetched per check (swings and ATR need history before the entry)
SWING_CONFIRM = 3          # bars after a swing before it can be trailed to
MAX_ADVICE = 100


class Advice(BaseModel):
    id: str
    time: int = Field(..., description="Open time (UNIX s) of the candle that triggered it")
    kind: AdviceKind
    text: str
    price: float = Field(..., description="Close of that candle")
    suggested_stop: Optional[float] = None
    take_pct: Optional[float] = Field(None, description="Share of the position to close, %")
    status: Literal["new", "done", "dismissed"] = "new"


class NewManagedTrade(BaseModel):
    symbol: str = Field(..., min_length=2, max_length=20)
    interval: str = Field("15m", description="Timeframe the trade is managed on (swings, CHoCH)")
    direction: Literal["long", "short"]
    entry: float = Field(..., gt=0)
    stop: float = Field(..., gt=0)
    targets: list[float] = Field(default_factory=list, max_length=6)
    qty: Optional[float] = Field(None, gt=0)
    opened_at: Optional[int] = Field(None, ge=0, description="UNIX s; default now")
    source: Literal["journal", "binance", "manual"] = "manual"
    source_id: Optional[str] = None
    breakeven_after_t1: bool = True
    trail: Literal["structure", "atr", "off"] = "structure"
    atr_mult: float = Field(1.5, gt=0, le=10)
    follow_advice: bool = False
    notes: str = Field("", max_length=1000)

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, v: str) -> str:
        s = norm_symbol(v)
        if not s.isalnum() or not 5 <= len(s) <= 20:
            raise ValueError("invalid symbol")
        return s

    @field_validator("interval")
    @classmethod
    def _interval(cls, v: str) -> str:
        if v not in INTERVALS:
            raise ValueError(f"interval must be one of {', '.join(INTERVALS)}")
        return v

    @model_validator(mode="after")
    def _prices(self) -> "NewManagedTrade":
        if not all(math.isfinite(x) for x in (self.entry, self.stop, *self.targets)):
            raise ValueError("prices must be finite numbers")
        long = self.direction == "long"
        if (self.stop >= self.entry) if long else (self.stop <= self.entry):
            raise ValueError(f"a {self.direction} needs its stop {'below' if long else 'above'} the entry")
        if any((t <= self.entry) if long else (t >= self.entry) for t in self.targets):
            raise ValueError(f"every target of a {self.direction} must be {'above' if long else 'below'} the entry")
        self.targets = sorted(set(self.targets), reverse=not long)
        return self


class ManagedTrade(NewManagedTrade):
    id: str
    opened_at: int
    initial_stop: float
    status: Literal["open", "stopped", "done", "closed"] = "open"
    targets_hit: int = 0
    last_bar: Optional[int] = Field(None, description="Newest candle checked")
    last_price: Optional[float] = None
    r_now: Optional[float] = None
    max_r: float = 0.0
    exit_price: Optional[float] = None
    exit_r: Optional[float] = None
    advised_stop: Optional[float] = Field(None, description="Best stop suggested so far (moved or not)")
    advice: list[Advice] = []
    keys: list[str] = Field(default_factory=list, description="Advice already given (one per event)")
    data_source: str = ""


class TradePatch(BaseModel):
    stop: Optional[float] = Field(None, gt=0, description="Where the stop is now on the exchange")
    targets: Optional[list[float]] = None
    trail: Optional[Literal["structure", "atr", "off"]] = None
    atr_mult: Optional[float] = Field(None, gt=0, le=10)
    breakeven_after_t1: Optional[bool] = None
    follow_advice: Optional[bool] = None
    close_price: Optional[float] = Field(None, gt=0, description="Closed by hand at this price")
    advice_id: Optional[str] = None
    advice_status: Optional[Literal["done", "dismissed"]] = None
    notes: Optional[str] = Field(None, max_length=1000)


# ------------------------------------------------------------------ review --


def r_multiple(t: ManagedTrade, price: float) -> float:
    risk = abs(t.entry - t.initial_stop)
    sign = 1 if t.direction == "long" else -1
    return round(sign * (price - t.entry) / risk, 2) if risk > 0 else 0.0


def _beyond(t: ManagedTrade, a: float, b: float) -> bool:
    """`a` is a better (tighter, more in profit) stop than `b` for this trade."""
    return a > b if t.direction == "long" else a < b


def _target_shares(n: int) -> list[float]:
    """Each target closes an equal share, the last one what is left (so 3 targets → 33, 33, 34)."""
    if n <= 0:
        return []
    base = math.floor(100 / n)
    return [float(base)] * (n - 1) + [float(100 - base * (n - 1))]


def review(t: ManagedTrade, df: pd.DataFrame) -> list[Advice]:
    """Walk the closed candles after `t.last_bar` (or the open), update `t` and return the new advice."""
    if df.empty:
        return []
    out: list[Advice] = []
    long = t.direction == "long"
    times = df["time"].to_numpy()
    start_time = t.last_bar if t.last_bar is not None else t.opened_at - INTERVAL_SECONDS[t.interval]
    start = int((times > start_time).argmax()) if (times > start_time).any() else len(df)
    shares = _target_shares(len(t.targets))
    atr_all = atr(df).bfill().to_numpy()

    def give(i: int, kind: AdviceKind, key: str, text: str, stop: Optional[float] = None,
             take: Optional[float] = None) -> None:
        if key in t.keys:
            return
        t.keys.append(key)
        a = Advice(id=uuid.uuid4().hex[:10], time=int(times[i]), kind=kind, text=text, price=float(df["close"].iat[i]),
                   suggested_stop=stop, take_pct=take)
        out.append(a)
        if stop is not None and (t.advised_stop is None or _beyond(t, stop, t.advised_stop)):
            t.advised_stop = stop
        if t.follow_advice and stop is not None and _beyond(t, stop, t.stop):
            t.stop = stop
            a.status = "done"

    for i in range(start, len(df)):
        if t.status != "open":
            break
        hi, lo, close = (float(df[c].iat[i]) for c in ("high", "low", "close"))
        bar_atr = float(atr_all[i]) if math.isfinite(atr_all[i]) else 0.0
        t.last_bar, t.last_price = int(times[i]), close
        t.max_r = max(t.max_r, r_multiple(t, hi if long else lo))
        t.r_now = r_multiple(t, close)

        # 1. Stop first: a candle that reaches the stop and a target is assumed to have stopped out.
        if (lo <= t.stop) if long else (hi >= t.stop):
            t.status, t.exit_price, t.exit_r = "stopped", t.stop, r_multiple(t, t.stop)
            how = "at breakeven" if abs(t.stop - t.entry) < 1e-12 else f"for {t.exit_r:+.2f}R"
            give(i, "stop", f"stop:{t.stop}", f"Stop {fmt_price(t.stop)} hit: out {how}.")
            break

        # 2. Targets, in order.
        while t.targets_hit < len(t.targets):
            tgt = t.targets[t.targets_hit]
            if not ((hi >= tgt) if long else (lo <= tgt)):
                break
            k = t.targets_hit
            t.targets_hit += 1
            last = t.targets_hit == len(t.targets)
            text = f"T{k + 1} {fmt_price(tgt)} reached ({r_multiple(t, tgt):+.2f}R): take {shares[k]:.0f}% off."
            stop = None
            if k == 0 and t.breakeven_after_t1 and not last and _beyond(t, t.entry, t.stop):
                stop = t.entry
                text += f" Move the stop to breakeven at {fmt_price(t.entry)}."
            if last:
                text = f"Last target T{k + 1} {fmt_price(tgt)} reached ({r_multiple(t, tgt):+.2f}R): close the rest."
                t.status, t.exit_price, t.exit_r = "done", tgt, r_multiple(t, tgt)
            give(i, "done" if last else "target", f"target:{k}", text, stop=stop, take=shares[k])
        if t.status != "open":
            break

        prefix = df.iloc[: i + 1]
        # 3. Structure against the position, on this candle.
        if i >= 20:
            swings_hi, swings_lo = find_swings(prefix, bar_atr or 1e-9)
            breaks = structure_breaks(prefix, swings_hi, swings_lo)
            last_break = breaks[-1] if breaks else None
            if (last_break and last_break["idx"] == i and last_break["type"] == "CHoCH"
                    and last_break["direction"] == ("bearish" if long else "bullish")):
                tight = lo - 0.1 * bar_atr if long else hi + 0.1 * bar_atr
                stop = tight if _beyond(t, tight, t.stop) else None
                word = "down" if long else "up"
                give(i, "structure", f"choch:{last_break['time']}",
                     f"Structure broke {word}: a close {'below' if long else 'above'} the swing at "
                     f"{fmt_price(last_break['level'])} ({t.r_now:+.2f}R now). Consider closing, "
                     + (f"or tighten the stop to {fmt_price(stop)}." if stop else "the stop is already tight."),
                     stop=stop)

            # 4. Trail once the trade has room: past T1, or +1R at its best.
            if t.trail != "off" and (t.targets_hit > 0 or t.max_r >= 1):
                cand = None
                if t.trail == "structure":
                    pool = swings_lo if long else swings_hi
                    ok = [s for s in pool if s.idx <= i - SWING_CONFIRM and s.time >= t.opened_at]
                    if ok:
                        s = ok[-1]
                        cand = s.price - 0.1 * bar_atr if long else s.price + 0.1 * bar_atr
                        key = f"trail:{s.time}"
                else:
                    cand = close - t.atr_mult * bar_atr if long else close + t.atr_mult * bar_atr
                    key = f"trail:atr:{round(cand / max(bar_atr * 0.5, 1e-12))}"
                risk = abs(t.entry - t.initial_stop)
                # Better than the stop and than anything already suggested (e.g. breakeven), by a meaningful step.
                ref = t.advised_stop if t.advised_stop is not None and _beyond(t, t.advised_stop, t.stop) else t.stop
                if (cand is not None and _beyond(t, cand, ref) and abs(cand - ref) > 0.1 * risk
                        and ((cand < close) if long else (cand > close))):
                    give(i, "trail", key, f"Trail the stop to {fmt_price(cand)} "
                         f"({'under the last higher low' if long and t.trail == 'structure' else 'above the last lower high' if t.trail == 'structure' else f'{t.atr_mult:g} ATR from the close'}"
                         f"), locking {r_multiple(t, cand):+.2f}R.", stop=round(cand, 10))
    t.advice = (t.advice + out)[-MAX_ADVICE:]
    return out


def describe(t: ManagedTrade, a: Advice) -> str:
    """One notification line: "INJUSDT long · T1 7.95 reached (+1.50R): take 50% off. Move the stop …"."""
    return f"{t.symbol} {t.direction} ({t.interval}) · {a.text}"


def from_journal(e: "JournalEntry", interval: Optional[str] = None) -> NewManagedTrade:
    if e.stop is None:
        raise ValueError("this journal entry has no stop; add the trade with its stop instead")
    return NewManagedTrade(symbol=e.symbol, interval=interval or e.interval, direction=e.direction, entry=e.entry,
                           stop=e.stop, targets=e.targets, qty=e.size_qty, opened_at=e.taken_at, source="journal",
                           source_id=e.id, breakeven_after_t1=e.manage == "breakeven_after_t1", notes=e.notes)


# ----------------------------------------------------------------- service --


class TradeManager:
    """Stores managed trades, checks the open ones on each new closed candle and sends the advice."""

    def __init__(self, market: MarketData, alerts: AlertService, settings: Settings | None = None,
                 check_seconds: float = CHECK_SECONDS) -> None:
        self.market = market
        self.alerts = alerts
        self.s = settings or get_settings()
        self.check_seconds = check_seconds
        self._path = store_path(self.s.trades_store)
        self._trades: dict[str, ManagedTrade] = {}
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        # Keys of the user's open Binance positions (BinanceImportService.open_position_keys), None when unknown.
        self.open_positions: Optional[Callable[[], Awaitable[Optional[set[str]]]]] = None
        self._positions_checked = 0.0
        self._load()

    def start(self) -> None:
        jobs.declare("trade_manager", "Trade manager", self.check_seconds)
        self._task = asyncio.create_task(self._loop(), name="trade-manager")

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    # -------------------------------------------------------------- CRUD
    def list(self) -> list[ManagedTrade]:
        return sorted(self._trades.values(), key=lambda t: (t.status != "open", -t.opened_at))

    def get(self, trade_id: str) -> Optional[ManagedTrade]:
        return self._trades.get(trade_id)

    async def add(self, new: NewManagedTrade) -> ManagedTrade:
        if new.source_id:
            dup = next((t for t in self._trades.values()
                        if t.source == new.source and t.source_id == new.source_id and t.status == "open"), None)
            if dup:
                return dup
        t = ManagedTrade(**new.model_dump(exclude={"opened_at"}), id=uuid.uuid4().hex[:10],
                         opened_at=new.opened_at or int(time.time()), initial_stop=new.stop)
        self._trades[t.id] = t
        await self.check(t, catch_up=True)  # catch up on the candles since it was opened, quietly
        self._save()
        return t

    async def update(self, trade_id: str, patch: TradePatch) -> Optional[ManagedTrade]:
        t = self._trades.get(trade_id)
        if t is None:
            return None
        if patch.stop is not None:
            long = t.direction == "long"
            if t.status == "open" and t.last_price is not None and (
                    (patch.stop >= t.last_price) if long else (patch.stop <= t.last_price)):
                raise ValueError(f"a {t.direction} stop must be {'below' if long else 'above'} the price "
                                 f"({fmt_price(t.last_price)})")
            t.stop = patch.stop
        if patch.targets is not None:
            NewManagedTrade(**{**t.model_dump(include=set(NewManagedTrade.model_fields)), "targets": patch.targets})
            t.targets = sorted(set(patch.targets), reverse=t.direction == "short")
            t.targets_hit = min(t.targets_hit, len(t.targets))
        for f in ("trail", "atr_mult", "breakeven_after_t1", "follow_advice", "notes"):
            if getattr(patch, f) is not None:
                setattr(t, f, getattr(patch, f))
        if patch.close_price is not None and t.status == "open":
            t.status, t.exit_price, t.exit_r = "closed", patch.close_price, r_multiple(t, patch.close_price)
        if patch.advice_id and patch.advice_status:
            for a in t.advice:
                if a.id == patch.advice_id:
                    a.status = patch.advice_status
                    # "Done" on a stop suggestion means the stop was moved there.
                    if patch.advice_status == "done" and a.suggested_stop is not None and patch.stop is None \
                            and _beyond(t, a.suggested_stop, t.stop):
                        t.stop = a.suggested_stop
        self._save()
        return t

    def remove(self, trade_id: str) -> bool:
        if self._trades.pop(trade_id, None) is None:
            return False
        self._save()
        return True

    # ------------------------------------------------------------ checks
    async def check(self, t: ManagedTrade, catch_up: bool = False) -> list[Advice]:
        """Fetch the trade's candles and review the closed ones it hasn't seen. `catch_up` (a trade just added,
        maybe opened days ago): advice on older candles is kept and marked done without a message each, and one
        summary is sent instead; only advice on the newest closed candle is announced as usual."""
        if t.status != "open":
            return []
        try:
            candles, source = await self.market.get_klines(t.symbol, t.interval, LOOKBACK_BARS)
        except MarketDataError as exc:
            log.warning("Trade manager: no candles for %s %s (%s)", t.symbol, t.interval, exc)
            return []
        if source == "synthetic" and self.s.data_source != "synthetic":
            return []  # demo candles during an outage would give made-up advice
        t.data_source = source
        df = candles_to_df(closed_only(candles, t.interval))
        new = review(t, df)
        latest = int(df["time"].iloc[-1]) if len(df) else None
        old = [a for a in new if catch_up and latest is not None and a.time < latest]
        for a in old:
            a.status = "done"
        if old:
            self._catch_up_summary(t, old, latest)
        for a in new:
            if a not in old:
                self._announce(t, a)
        return new

    def _catch_up_summary(self, t: ManagedTrade, old: list[Advice], latest: int) -> None:
        span = max(0, latest - old[0].time)
        ago = f"{span / 86400:.0f} day{'s' if round(span / 86400) != 1 else ''}" if span >= 86400 else \
            f"{span / 3600:.0f}h"
        kinds = list(dict.fromkeys(a.kind.replace("_", " ") for a in old))
        stops = [a.suggested_stop for a in old if a.suggested_stop is not None]
        best = (max(stops) if t.direction == "long" else min(stops)) if stops else None
        text = (f"{t.symbol} {t.direction}: caught up {ago} of {t.interval} candles: {', '.join(kinds)}"
                + (f"; best stop now {fmt_price(best)}" if best is not None else "") + ".")
        self.alerts.record("trade", t.symbol, f"{t.symbol} {t.direction}: caught up", text, price=old[-1].price)
        self.alerts.broadcast({"type": "trade_advice", "trade_id": t.id, "advice": None, "text": text})
        self.alerts.notify(text)

    async def sync_positions(self) -> bool:
        """Close managed Binance positions that are no longer open on the account. True when one was closed."""
        trades = [t for t in self._trades.values() if t.status == "open" and t.source == "binance" and t.source_id]
        if not trades or self.open_positions is None:
            return False
        keys = await self.open_positions()
        if keys is None:
            return False
        closed = False
        for t in trades:
            if t.source_id in keys:
                continue
            t.status, closed = "closed", True
            if t.last_price is not None:
                t.exit_price, t.exit_r = t.last_price, r_multiple(t, t.last_price)
            text = (f"{t.symbol} {t.direction}: the position is no longer open on Binance, so it is no longer "
                    f"managed" + (f" (last price {fmt_price(t.last_price)}, {t.exit_r:+.2f}R)."
                                  if t.exit_r is not None else "."))
            self.alerts.record("trade", t.symbol, f"{t.symbol} {t.direction}: closed on Binance", text,
                               price=t.last_price)
            self.alerts.broadcast({"type": "trade_advice", "trade_id": t.id, "advice": None, "text": text})
        return closed

    def _announce(self, t: ManagedTrade, a: Advice) -> None:
        text = describe(t, a)
        self.alerts.record("trade", t.symbol, f"{t.symbol} {t.direction}: {a.kind}", text, price=a.price)
        self.alerts.broadcast({"type": "trade_advice", "trade_id": t.id, "advice": a.model_dump(), "text": text})
        if hasattr(self.alerts, "notify_notice"):
            fields = [("Price", fmt_price(a.price), True), ("Stop", fmt_price(t.stop), True)]
            if a.suggested_stop is not None:
                fields.append(("Move stop to", fmt_price(a.suggested_stop), True))
            if a.take_pct:
                fields.append(("Take off", f"{a.take_pct:g}%", True))
            fields.append(("R now", f"{r_multiple(t, a.price):+.2f}R", True))
            self.alerts.notify_notice(Notice(
                kind="trade", title=f"{t.symbol} {t.direction}: {a.kind.replace('_', ' ')}", text=text,
                symbol=t.symbol, interval=t.interval, description=a.text, fields=fields,
                side="sell" if a.kind in ("stop", "structure") else "buy" if a.kind == "target" else "info",
                demo=t.data_source == "synthetic"))
        else:
            self.alerts.notify(text)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.check_seconds)
            now = time.time()
            async with self._lock:
                due = [t for t in self._trades.values() if t.status == "open" and (
                    t.last_bar is None or now >= t.last_bar + 2 * INTERVAL_SECONDS[t.interval])]
                changed = False
                failures: list[str] = []
                for t in due:
                    try:
                        before = (t.last_bar, len(t.advice), t.status)
                        await self.check(t)
                        changed |= before != (t.last_bar, len(t.advice), t.status)
                    except Exception as exc:
                        failures.append(f"{t.symbol} {t.interval}: {exc}")
                        log.exception("Trade manager check failed for %s", t.id)
                if now - self._positions_checked >= POSITIONS_CHECK_SECONDS:
                    self._positions_checked = now
                    try:
                        changed |= await self.sync_positions()
                    except Exception as exc:
                        failures.append(f"Binance position check: {exc}")
                        log.exception("Trade manager: Binance position check failed")
                if changed:
                    self._save()
            if failures:  # ok only for a clean pass, so Status keeps showing the error
                jobs.fail("trade_manager", "; ".join(failures)[:300])
            else:
                jobs.ok("trade_manager", f"{len(due)} trade{'s' if len(due) != 1 else ''} checked" if due else "")

    # ------------------------------------------------------- persistence
    def _load(self) -> None:
        data = read_store(self._path)
        for row in (data or {}).get("trades", []):
            try:
                t = ManagedTrade.model_validate(row)
            except ValueError as exc:
                log.warning("Skipping invalid managed trade: %s", exc)
                continue
            self._trades[t.id] = t

    def _save(self) -> None:
        write_store(self._path, {"trades": [t.model_dump() for t in self._trades.values()]})
