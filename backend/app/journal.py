"""Trade journal: plans the user logs as taken, tracked on real candles.

An entry stores only what the trader decided (direction, entry, stop, targets, size, when). Everything else, from
whether it filled to how much R it made, is worked out again from 1m candles since `taken_at`, so the journal never
drifts from the market and editing an entry simply re-evaluates it. The rules are deterministic and conservative:

* Only candles that open at or after `taken_at` count, so nothing that happened before the trade can fill or stop it.
* Limit orders fill when a candle touches the entry, at the entry price (at the candle's open if it opened past
  the entry, as an exchange would fill a resting order). Market orders fill at the first candle's open.
* The position is split equally across the targets; each target closes its share. With `breakeven_after_t1` the
  stop moves to the fill price after T1, from the next candle on.
* When the stop and a target are both inside one candle the stop is assumed first. On the candle a limit order
  fills, the stop counts but targets do not (the high may have come before the fill).
* A manual close counts candles up to its time; the candle it happened in can still fill the order but not stop it
  or take a target.
* R is measured against the planned risk |entry − stop|. Fees (`fee_pct` per side) are charged on every exit and
  shown in R, so `realized_r` and `pnl_usd` are after fees.

Trades older than the range cache allows on 1m (about 13 months) are tracked on 5m or 15m candles and say so. While
Binance is unreachable the last real evaluation is kept rather than replaced by synthetic demo prices.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Optional

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, field_validator, model_validator

from .config import Settings, get_settings
from .market_data import INTERVAL_SECONDS, MAX_RANGE_BARS, MarketDataError
from .schemas import INTERVALS, TradePlan, norm_symbol

if TYPE_CHECKING:
    from .market_data import MarketData

log = logging.getLogger(__name__)

MAX_ENTRIES = 2000
MAX_TARGETS = 5
BREAKEVEN_BAND_R = 0.1   # closed trades within ±0.1R after fees count as breakeven
RESOLUTIONS = ("1m", "5m", "15m")

Direction = Literal["long", "short"]
Status = Literal["pending", "open", "closed", "cancelled"]
ExitKind = Literal["T1", "T2", "T3", "T4", "T5", "stop", "breakeven", "manual"]


# ------------------------------------------------------------------ models --


class ManualClose(BaseModel):
    time: int = Field(..., description="UNIX seconds")
    price: float = Field(..., gt=0)


class NewJournalEntry(BaseModel):
    """What POST /api/journal takes. Prices are validated against the direction."""

    symbol: str = Field(..., min_length=2, max_length=20)
    interval: str = Field("4h", description="Chart timeframe the trade was taken on")
    direction: Direction
    entry: float = Field(..., gt=0, description="Limit price, or the reference price of a market order")
    stop: float = Field(..., gt=0)
    targets: list[float] = Field(..., min_length=1, max_length=MAX_TARGETS)
    setup: str = Field("manual", max_length=60, description="e.g. 'H4 demand', 'Kimi B+', 'manual'")
    source: Literal["agent_plan", "kimi", "manual"] = "manual"
    notes: str = Field("", max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=10)
    entry_type: Literal["limit", "market"] = "limit"
    size_qty: Optional[float] = Field(None, gt=0, description="Position size in coins")
    risk_usd: Optional[float] = Field(None, gt=0, description="Money lost at the stop, for PnL in dollars")
    fee_pct: float = Field(0.1, ge=0, le=1, description="Fee per side, % of notional")
    manage: Literal["breakeven_after_t1", "none"] = "breakeven_after_t1"
    taken_at: Optional[int] = Field(None, ge=0, description="UNIX seconds; default now")

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

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        return _clean_tags(v)

    @field_validator("setup")
    @classmethod
    def _setup(cls, v: str) -> str:
        return v.strip() or "manual"

    @model_validator(mode="after")
    def _prices(self) -> "NewJournalEntry":
        if not all(math.isfinite(x) for x in (self.entry, self.stop, *self.targets)):
            raise ValueError("prices must be finite numbers")
        long = self.direction == "long"
        if (self.stop >= self.entry) if long else (self.stop <= self.entry):
            raise ValueError(f"a {self.direction} needs its stop {'below' if long else 'above'} the entry")
        bad = [t for t in self.targets if (t <= self.entry if long else t >= self.entry)]
        if bad:
            raise ValueError(f"every target of a {self.direction} must be {'above' if long else 'below'} the entry")
        self.targets = sorted(set(self.targets), reverse=not long)
        return self


class JournalEntry(NewJournalEntry):
    """A stored entry. Mirrors JournalEntry in frontend/lib/journal.ts (minus `evaluation`)."""

    id: str
    taken_at: int
    cancelled: bool = False
    manual_close: Optional[ManualClose] = None
    created_at: int
    updated_at: float = Field(0.0, description="Bumped on every edit; invalidates the cached evaluation")


class ClosePatch(BaseModel):
    price: Optional[float] = Field(None, gt=0, description="Default: the last price")
    time: Optional[int] = Field(None, ge=0, description="UNIX seconds; default now")


class JournalPatch(BaseModel):
    """What PATCH /api/journal/{id} takes. Only the fields given change."""

    notes: Optional[str] = Field(None, max_length=2000)
    tags: Optional[list[str]] = Field(None, max_length=10)
    setup: Optional[str] = Field(None, max_length=60)
    cancel: Optional[bool] = Field(None, description="true cancels an order that has not filled")
    close: Optional[ClosePatch] = Field(None, description="Close what is still open at this price/time")

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        return None if v is None else _clean_tags(v)


class JournalExit(BaseModel):
    time: int
    price: float
    kind: ExitKind
    fraction: float = Field(..., description="Share of the position closed here")
    r: float = Field(..., description="Price move in R for this share, before fees")


class JournalEvaluation(BaseModel):
    status: Status
    filled_at: Optional[int] = None
    fill_price: Optional[float] = None
    exits: list[JournalExit] = Field(default_factory=list)
    closed_at: Optional[int] = None
    remaining: float = Field(1.0, description="Share of the position still open (1 while pending)")
    current_stop: Optional[float] = None
    realized_r: float = Field(0.0, description="Closed share, after fees")
    gross_r: float = Field(0.0, description="Closed share, before fees")
    fees_r: float = 0.0
    open_r: float = Field(0.0, description="Open share marked to the last price, before fees")
    mfe_r: Optional[float] = Field(None, description="Best excursion while in the trade, R (≥ 0)")
    mae_r: Optional[float] = Field(None, description="Worst excursion while in the trade, R (≤ 0)")
    pnl_usd: Optional[float] = Field(None, description="Realized, after fees; needs risk_usd or size_qty")
    open_pnl_usd: Optional[float] = None
    outcome: Optional[Literal["win", "loss", "breakeven"]] = None
    last_price: Optional[float] = None
    data_source: str = ""
    resolution: str = "1m"
    notes: list[str] = Field(default_factory=list)
    error: Optional[str] = None
    evaluated_at: int = 0


def _clean_tags(v: list[str]) -> list[str]:
    out: list[str] = []
    for t in v:
        t = " ".join(str(t).split())[:24]
        if t and t not in out:
            out.append(t)
    return out


# -------------------------------------------------------------- evaluation --


def resolution_for(taken_at: int, now: float) -> str:
    """1m while the range cache can hold the whole trade, else the finest of 5m / 15m that fits."""
    for res in RESOLUTIONS:
        if (now - taken_at) // INTERVAL_SECONDS[res] + 2 <= MAX_RANGE_BARS:
            return res
    raise MarketDataError("This trade is too old to track")


def _first(mask: np.ndarray, start: int) -> Optional[int]:
    """Index of the first True at or after `start`, or None."""
    if start >= len(mask):
        return None
    i = int(np.argmax(mask[start:]))
    return start + i if mask[start + i] else None


def _fmt(p: float) -> str:
    if p >= 1000:
        return f"{p:,.2f}"
    return f"{p:.4f}".rstrip("0").rstrip(".") if p >= 1 else f"{p:.8f}".rstrip("0").rstrip(".")


def _utc(ts: int) -> str:
    return time.strftime("%d %b %H:%M UTC", time.gmtime(ts))


def evaluate_entry(e: JournalEntry, df: pd.DataFrame, data_source: str = "binance", resolution: str = "1m",
                   now: Optional[float] = None) -> JournalEvaluation:
    """Replay one entry over candles (time/open/high/low/close, oldest first; any rows before `taken_at` are
    ignored). Pure and deterministic: the same candles always give the same result."""
    now = time.time() if now is None else now
    step = INTERVAL_SECONDS[resolution]
    out = JournalEvaluation(status="pending", data_source=data_source, resolution=resolution, evaluated_at=int(now))
    if e.cancelled:
        out.status = "cancelled"
        return out
    if resolution != "1m":
        out.notes.append(f"Tracked on {resolution} candles (the trade is older than the 1m history kept), so fills "
                         f"and exits inside one candle are less exact.")
    if data_source == "synthetic":
        out.notes.append("Tracked on synthetic demo data, not real prices.")

    t_all = df["time"].to_numpy(dtype=np.int64)
    keep = t_all >= e.taken_at
    if e.manual_close is not None:
        keep &= t_all < e.manual_close.time
    t = t_all[keep]
    o, h, l, c = (df[k].to_numpy(dtype=float)[keep] for k in ("open", "high", "low", "close"))
    # After a manual close, the candle it happened in can still fill the order (the trader saw it filled) but
    # not stop it or take a target: that part of the candle may have come after the close.
    ex_n = len(t) if e.manual_close is None else int((t + step <= e.manual_close.time).sum())
    if len(df):
        out.last_price = float(df["close"].iloc[-1])
    long = e.direction == "long"
    sign = 1.0 if long else -1.0
    risk = abs(e.entry - e.stop)
    n = len(t)

    # ---- fill
    fill_idx: Optional[int] = None
    if n:
        if e.entry_type == "market":
            fill_idx, fill_px, at_open = 0, float(o[0]), True
        else:
            fill_idx = _first(l <= e.entry if long else h >= e.entry, 0)
            if fill_idx is not None:
                at_open = bool(o[fill_idx] <= e.entry if long else o[fill_idx] >= e.entry)
                fill_px = float(o[fill_idx]) if at_open else e.entry
                if at_open and fill_px != e.entry:
                    out.notes.append(f"The candle opened past your limit, so it filled at the open, {_fmt(fill_px)}.")
    if fill_idx is None:
        if e.manual_close is not None:
            out.status = "cancelled"
            out.notes.append("Closed before the entry filled.")
        return out

    out.status, out.filled_at, out.fill_price = "open", int(t[fill_idx]), fill_px
    if e.entry_type == "market":
        out.notes.append(f"Market order filled at the open of the first candle after it was logged, {_fmt(fill_px)}.")

    # ---- manage the position target by target
    targets = list(e.targets)
    share = 1.0 / len(targets)
    remaining = 1.0
    stop_px = e.stop
    moved = False
    stop_from = fill_idx
    tgt_from = fill_idx if at_open else fill_idx + 1  # a limit fill's candle may have printed its high first
    nxt = 0
    end_idx = n - 1
    exits: list[JournalExit] = []
    stop_mask_cache: dict[float, np.ndarray] = {}

    def r_of(px: float) -> float:
        return (px - fill_px) * sign / risk

    while remaining > 1e-9:
        mask = stop_mask_cache.get(stop_px)
        if mask is None:
            mask = stop_mask_cache[stop_px] = (l[:ex_n] <= stop_px) if long else (h[:ex_n] >= stop_px)
        i_s = _first(mask, stop_from)
        tgt = targets[nxt]
        i_t = _first((h[:ex_n] >= tgt) if long else (l[:ex_n] <= tgt), tgt_from)
        if i_s is None and i_t is None:
            break
        if i_t is None or (i_s is not None and i_s <= i_t):
            gap_ok = i_s > fill_idx or at_open  # the open of a later candle is a real tradable price
            px = (min(float(o[i_s]), stop_px) if long else max(float(o[i_s]), stop_px)) if gap_ok else stop_px
            exits.append(JournalExit(time=int(t[i_s]), price=px, kind="breakeven" if moved else "stop",
                                     fraction=remaining, r=round(r_of(px), 4)))
            if i_s == i_t:
                out.notes.append(f"The stop and T{nxt + 1} were both inside the {_utc(int(t[i_s]))} candle; "
                                 f"counted the stop first.")
            remaining, end_idx = 0.0, i_s
            break
        exits.append(JournalExit(time=int(t[i_t]), price=tgt, kind=f"T{nxt + 1}", fraction=share,  # type: ignore[arg-type]
                                 r=round(r_of(tgt), 4)))
        remaining -= share
        nxt += 1
        tgt_from = i_t  # a bigger move in the same candle can take the next target too
        if nxt == len(targets):
            remaining, end_idx = 0.0, i_t
            break
        if nxt == 1 and e.manage == "breakeven_after_t1":
            stop_px, moved = fill_px, True
            stop_from = max(stop_from, i_t + 1)

    if remaining > 1e-9 and e.manual_close is not None:
        px = e.manual_close.price
        exits.append(JournalExit(time=e.manual_close.time, price=px, kind="manual", fraction=remaining,
                                 r=round(r_of(px), 4)))
        remaining = 0.0
        end_idx = n - 1

    # ---- results
    out.exits = exits
    out.remaining = round(max(remaining, 0.0), 6)
    out.current_stop = stop_px if remaining > 1e-9 else None
    gross = sum(x.fraction * x.r for x in exits)
    fees = sum(x.fraction * (fill_px + x.price) * e.fee_pct / 100 / risk for x in exits)
    out.gross_r, out.fees_r, out.realized_r = round(gross, 4), round(fees, 4), round(gross - fees, 4)
    if remaining > 1e-9:
        out.open_r = round(remaining * r_of(out.last_price if out.last_price is not None else fill_px), 4)
    else:
        out.status = "closed"
        out.closed_at = exits[-1].time
        out.outcome = ("win" if out.realized_r > BREAKEVEN_BAND_R else "loss" if out.realized_r < -BREAKEVEN_BAND_R
                       else "breakeven")

    # Excursions while in the position. The candle of a stop or target exit counts only at the exit price (its
    # other extreme may have come after the exit); exit prices themselves were traded while in the trade.
    level_exit = out.status == "closed" and exits[-1].kind != "manual"
    stop_at = end_idx if level_exit else ex_n
    fav, adv = (h, l) if long else (l, h)
    fav_seg = fav[(fill_idx if at_open else fill_idx + 1):stop_at]
    adv_seg = adv[fill_idx:stop_at]
    pts = [fill_px] + [x.price for x in exits]
    if stop_at > fill_idx:
        pts.append(float(c[fill_idx]))
    if len(fav_seg):
        pts.append(float(fav_seg.max() if long else fav_seg.min()))
    if len(adv_seg):
        pts.append(float(adv_seg.min() if long else adv_seg.max()))
    out.mfe_r = round(max(0.0, max(r_of(p) for p in pts)), 4)
    out.mae_r = round(min(0.0, min(r_of(p) for p in pts)), 4)

    qty = e.size_qty if e.size_qty is not None else (e.risk_usd / risk if e.risk_usd is not None else None)
    if qty is not None:
        out.pnl_usd = round(out.realized_r * qty * risk, 2)
        out.open_pnl_usd = round(out.open_r * qty * risk, 2) if remaining > 1e-9 else 0.0
    return out


# ------------------------------------------------------------------- stats --


def _group(rows: list[tuple[JournalEntry, JournalEvaluation]], key) -> list[dict]:
    groups: dict[str, list[float]] = {}
    for e, ev in rows:
        groups.setdefault(key(e), []).append(ev.realized_r)
    out = []
    for k, rs in groups.items():
        wins = sum(r > BREAKEVEN_BAND_R for r in rs)
        losses = sum(r < -BREAKEVEN_BAND_R for r in rs)
        out.append({"key": k, "trades": len(rs), "wins": wins, "losses": losses,
                    "breakeven": len(rs) - wins - losses, "win_rate": round(wins / len(rs), 4),
                    "avg_r": round(sum(rs) / len(rs), 4), "total_r": round(sum(rs), 4)})
    return sorted(out, key=lambda g: (-g["trades"], -g["total_r"], g["key"]))


def journal_stats(rows: list[tuple[JournalEntry, JournalEvaluation]], symbol: Optional[str] = None,
                  setup: Optional[str] = None, direction: Optional[str] = None) -> dict:
    """Win rate, R and breakdowns over closed trades (R after fees). Cancelled entries are left out entirely."""
    match = [(e, ev) for e, ev in rows
             if (not symbol or e.symbol == symbol) and (not setup or e.setup.lower() == setup.lower())
             and (not direction or e.direction == direction)]
    sel = [(e, ev) for e, ev in match if ev.status != "cancelled"]
    closed = sorted([(e, ev) for e, ev in sel if ev.status == "closed"], key=lambda x: (x[1].closed_at or 0, x[0].id))
    rs = [ev.realized_r for _, ev in closed]
    wins = [r for r in rs if r > BREAKEVEN_BAND_R]
    losses = [r for r in rs if r < -BREAKEVEN_BAND_R]
    n = len(rs)
    gain, loss = sum(r for r in rs if r > 0), -sum(r for r in rs if r < 0)
    win_rate = len(wins) / n if n else None
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = sum(losses) / len(losses) if losses else None
    expectancy = None
    if n:
        expectancy = (len(wins) / n) * (avg_win or 0.0) + (len(losses) / n) * (avg_loss or 0.0)
    equity, cum = [], 0.0
    for _, ev in closed:
        cum += ev.realized_r
        equity.append({"time": ev.closed_at, "r_cum": round(cum, 4)})
    notes = []
    if any(ev.data_source == "synthetic" for _, ev in sel):
        notes.append("Some trades are tracked on synthetic demo data (Binance unreachable).")
    if 0 < n < 20:
        notes.append(f"Only {n} closed trade{'s' if n != 1 else ''}: too few to trust these numbers yet.")

    def rnd(x: Optional[float]) -> Optional[float]:
        return None if x is None else round(x, 4)

    return {
        "count": len(sel), "closed": n, "open": sum(ev.status == "open" for _, ev in sel),
        "pending": sum(ev.status == "pending" for _, ev in sel),
        "cancelled": len(match) - len(sel),
        "wins": len(wins), "losses": len(losses), "breakeven": n - len(wins) - len(losses),
        "win_rate": rnd(win_rate), "avg_r": rnd(sum(rs) / n if n else None), "total_r": round(sum(rs), 4),
        "avg_win_r": rnd(avg_win), "avg_loss_r": rnd(avg_loss), "expectancy": rnd(expectancy),
        "profit_factor": rnd(gain / loss) if loss > 0 else None,
        "best_r": rnd(max(rs)) if rs else None, "worst_r": rnd(min(rs)) if rs else None,
        "open_r": round(sum(ev.open_r for _, ev in sel if ev.status == "open"), 4),
        "by_setup": _group(closed, lambda e: e.setup), "by_symbol": _group(closed, lambda e: e.symbol),
        "by_direction": _group(closed, lambda e: e.direction),
        "equity": equity, "notes": notes,
    }


TF_LABELS = {"M1", "M5", "M15", "M30", "H1", "H3", "H4", "D1", "W1", "MN"}


def plan_setup_name(basis: str) -> str:
    """The setup type of an agent plan's basis, without prices or decorations:
    'H4 Demand (fresh) + D1 23.9–24.2' → 'H4 demand', 'last swing low 23.5' → 'swing low'."""
    words: list[str] = []
    for w in basis.split():
        if w[:1].isdigit() or w[:1] in "(+":
            break
        if w.lower() != "last":
            words.append(w if w in TF_LABELS else w.lower())
    return " ".join(words)[:60] or "agent plan"


def entry_from_plan(plan: TradePlan, symbol: str, interval: str, **extra) -> NewJournalEntry:
    """A journal entry for an agent trade plan (the same fields as planToJournalEntry in lib/journal.ts)."""
    notes = extra.pop("notes", None)
    body = {"symbol": symbol, "interval": interval, "direction": plan.direction, "entry": plan.entry,
            "stop": plan.stop, "targets": [t.price for t in plan.targets], "setup": plan_setup_name(plan.basis),
            "source": "agent_plan", "notes": notes if notes is not None else f"From {plan.basis}"}
    body.update({k: v for k, v in extra.items() if v is not None})
    return NewJournalEntry.model_validate(body)


# ----------------------------------------------------------------- service --


@dataclass
class _Cached:
    version: float
    expires: float
    evaluation: JournalEvaluation


class JournalService:
    """Stores entries in JOURNAL_STORE and evaluates them on 1m candles, cached until the next 1m bar."""

    def __init__(self, market: "MarketData", settings: Optional[Settings] = None) -> None:
        self.market = market
        self.s = settings or get_settings()
        store = self.s.journal_store.strip()
        self._store = None if store.lower() in ("memory", "none", "off") else Path(store)
        self._entries: dict[str, JournalEntry] = {}
        self._evals: dict[str, _Cached] = {}
        self._load()

    # -------------------------------------------------------------- CRUD
    def get(self, entry_id: str) -> Optional[JournalEntry]:
        return self._entries.get(entry_id)

    def list(self) -> list[JournalEntry]:
        return sorted(self._entries.values(), key=lambda e: (-e.taken_at, e.id))

    async def add(self, new: NewJournalEntry) -> tuple[JournalEntry, JournalEvaluation]:
        """Log a trade. `taken_at` defaults to now and may not be in the future."""
        now = time.time()
        taken = int(now) if new.taken_at is None else new.taken_at
        if taken > now + 60:
            raise ValueError("taken_at is in the future")
        if len(self._entries) >= MAX_ENTRIES:
            raise ValueError(f"The journal holds at most {MAX_ENTRIES} trades; delete some first")
        entry = JournalEntry(**new.model_dump(exclude={"taken_at"}), id=uuid.uuid4().hex[:12], taken_at=taken,
                             created_at=int(now), updated_at=now)
        self._entries[entry.id] = entry
        self._save()
        return entry, await self.evaluate(entry)

    async def add_plan(self, plan: TradePlan, symbol: str, interval: str, **extra) -> tuple[JournalEntry,
                                                                                           JournalEvaluation]:
        """Log an agent trade plan as taken (chat: "I took that long", "journal this plan")."""
        return await self.add(entry_from_plan(plan, symbol, interval, **extra))

    async def update(self, entry_id: str, patch: JournalPatch) -> Optional[tuple[JournalEntry, JournalEvaluation]]:
        """Edit notes/tags/setup, cancel an unfilled order, or close what is open. Raises ValueError when the
        change does not fit the trade's state (cancelling a filled trade, closing a closed one)."""
        entry = self._entries.get(entry_id)
        if entry is None:
            return None
        upd: dict = {k: v for k, v in patch.model_dump(include={"notes", "tags", "setup"}).items() if v is not None}
        if "setup" in upd:
            upd["setup"] = upd["setup"].strip() or "manual"
        if patch.cancel is not None or patch.close is not None:
            ev = await self.evaluate(entry)
            if patch.cancel:
                if ev.status in ("open", "closed"):
                    raise ValueError("This trade has already filled; close it instead of cancelling")
                upd["cancelled"] = True
            elif patch.cancel is False:
                upd["cancelled"] = False
            if patch.close is not None:
                if ev.status == "closed" and entry.manual_close is None:
                    raise ValueError("This trade is already closed")
                if ev.status in ("pending", "cancelled"):
                    upd["cancelled"] = True  # closing an order that never filled cancels it
                else:
                    now = int(time.time())
                    at = patch.close.time if patch.close.time is not None else now
                    if at < entry.taken_at or at > now + 60:
                        raise ValueError("The close time must be between taking the trade and now")
                    price = patch.close.price if patch.close.price is not None else ev.last_price
                    if not price:
                        raise ValueError("No price to close at; give one")
                    upd["manual_close"] = ManualClose(time=at, price=price)
        upd["updated_at"] = time.time()
        entry = entry.model_copy(update=upd)
        self._entries[entry_id] = entry
        self._save()
        return entry, await self.evaluate(entry)

    async def remove(self, entry_id: str) -> bool:
        if self._entries.pop(entry_id, None) is None:
            return False
        self._evals.pop(entry_id, None)
        self._save()
        return True

    # --------------------------------------------------------- evaluation
    def _cached(self, e: JournalEntry, now: float) -> Optional[JournalEvaluation]:
        hit = self._evals.get(e.id)
        if hit and hit.version == e.updated_at and hit.expires > now:
            return hit.evaluation
        return None

    def _remember(self, e: JournalEntry, ev: JournalEvaluation, now: float) -> None:
        demo_fallback = ev.data_source == "synthetic" and self.s.data_source != "synthetic"
        final = ev.status == "cancelled" or (ev.status == "closed" and not demo_fallback)
        # Pending/open entries are re-checked when the next 1m candle opens.
        expires = math.inf if final else (now // 60 + 1) * 60 + 1
        self._evals[e.id] = _Cached(e.updated_at, expires, ev)

    async def evaluate(self, entry: JournalEntry) -> JournalEvaluation:
        return (await self.evaluate_many([entry]))[entry.id]

    async def evaluate_many(self, entries: list[JournalEntry]) -> dict[str, JournalEvaluation]:
        """Evaluations for `entries`, fetching one candle range per symbol for everything not cached."""
        now = time.time()
        out: dict[str, JournalEvaluation] = {}
        todo: dict[tuple[str, str], list[JournalEntry]] = {}
        for e in entries:
            hit = self._cached(e, now)
            if hit is not None:
                out[e.id] = hit
            elif e.cancelled or e.taken_at > now:
                ev = JournalEvaluation(status="cancelled" if e.cancelled else "pending", evaluated_at=int(now))
                if e.cancelled:
                    self._remember(e, ev, now)
                out[e.id] = ev
            else:
                try:
                    res = resolution_for(e.taken_at, now)
                except MarketDataError as exc:
                    out[e.id] = JournalEvaluation(status="pending", error=str(exc), evaluated_at=int(now))
                    continue
                todo.setdefault((e.symbol, res), []).append(e)

        async def run(symbol: str, res: str, group: list[JournalEntry]) -> None:
            start = min(e.taken_at for e in group)
            try:
                df, source = await self.market.get_range(symbol, res, start)
            except Exception as exc:  # network trouble must not break the journal
                log.warning("Journal: candles for %s %s failed: %s", symbol, res, exc)
                for e in group:
                    out[e.id] = self._stale(e, f"Could not load candles: {exc}", now)
                return
            for e in group:
                ev = evaluate_entry(e, df, source, res, now)
                prev = self._evals.get(e.id)
                if (source == "synthetic" and self.s.data_source != "synthetic" and prev is not None
                        and prev.version == e.updated_at and prev.evaluation.data_source != "synthetic"):
                    out[e.id] = self._stale(e, "Binance is unreachable; showing the last real evaluation.", now)
                    continue
                self._remember(e, ev, now)
                out[e.id] = ev

        await asyncio.gather(*(run(sym, res, g) for (sym, res), g in todo.items()))
        return out

    def _stale(self, e: JournalEntry, why: str, now: float) -> JournalEvaluation:
        prev = self._evals.get(e.id)
        if prev is not None and prev.version == e.updated_at:
            return prev.evaluation.model_copy(update={"error": why})
        return JournalEvaluation(status="pending", error=why, evaluated_at=int(now))

    async def rows(self) -> list[tuple[JournalEntry, JournalEvaluation]]:
        entries = self.list()
        evals = await self.evaluate_many(entries)
        return [(e, evals[e.id]) for e in entries]

    async def stats(self, symbol: Optional[str] = None, setup: Optional[str] = None,
                    direction: Optional[str] = None) -> dict:
        return journal_stats(await self.rows(), symbol, setup, direction)

    # ------------------------------------------------------- persistence
    def _load(self) -> None:
        if not self._store or not self._store.exists():
            return
        try:
            rows = json.loads(self._store.read_text()).get("entries", [])
        except (OSError, ValueError, AttributeError) as exc:
            log.warning("Could not read %s (%s); starting with an empty journal", self._store, exc)
            return
        for row in rows:
            try:
                e = JournalEntry.model_validate(row)
            except ValueError as exc:
                log.warning("Skipping invalid journal entry: %s", exc)
                continue
            self._entries[e.id] = e
        if self._entries:
            log.info("Loaded %d journal entries", len(self._entries))

    def _save(self) -> None:
        if not self._store:
            return
        try:
            self._store.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._store.with_suffix(".tmp")
            tmp.write_text(json.dumps({"entries": [e.model_dump() for e in self._entries.values()]}))
            tmp.replace(self._store)
        except OSError as exc:
            log.warning("Could not save the journal to %s: %s", self._store, exc)


def entry_json(e: JournalEntry, ev: JournalEvaluation) -> dict:
    """An entry with its evaluation, as the API returns it."""
    return {**e.model_dump(), "evaluation": ev.model_dump()}
