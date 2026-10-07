"""Import the user's Binance fills and holdings with a read-only key, and tell their own trading apart from bots.

What it does
  * Fetches spot fills (/api/v3/myTrades, paged by trade id so a re-import only asks for new ones) for the user's
    symbols: the ones they list, their grid bots' pairs and every coin in the spot wallet. Fetches USD-M futures
    fills (/fapi/v1/userTrades) for every futures symbol with realized PnL or an open position. Each fill is stored
    once (key "spot:BTCUSDT:<trade id>"), so re-imports never double up.
  * Classifies every fill as "manual" (the user's own), "bot" (with the tracked grid bot's id when it matches one)
    or "unknown", and keeps the reason on the record. The user can override any classification.
  * Rebuilds round trips (flat → position → flat) from the fills of one kind, and keeps the journal in step with
    the user's own closed round trips: only "manual" trades go into the journal; bot fills feed the grid bot's
    "real vs simulated" comparison instead.
  * Lists open positions: spot holdings with their average entry from the fills, open USD-M positions, and the
    bots' holdings separately.

How bot activity shows up on Binance, and what the classification relies on
  Documented by Binance:
  * GET /sapi/v1/asset/wallet/balance returns one row per wallet (Spot, Funding, Cross Margin, Isolated Margin,
    USDⓈ-M Futures, COIN-M Futures, Earn, Options, Trading Bots, Copy Trading...) with its value. Trading-bot funds
    are reported in their own "Trading Bots" wallet, not in the spot balance; the endpoint only gives the wallet's
    total value, not the coins in it.
  * Every order has a clientOrderId (allOrders returns it; myTrades does not, so orders are fetched for the order
    ids the fills point at). An API client may set it; when it does not, Binance generates a random one.
  * There is no public endpoint for Spot Grid bots (their settings, orders or fills). The /sapi/v1/algo/spot and
    /sapi/v1/algo/futures endpoints cover TWAP and volume-participation algo orders only.
  Inferred (from what responses look like in practice, not from Binance's documentation):
  * Orders placed by hand carry a clientOrderId with the front end's prefix: "web_" (website), "ios_", "and_"
    (Android), "electron_" (desktop app). Those fills are marked manual.
  * Because bot funds sit in the Trading Bots wallet, the bot's own orders are normally not part of the spot
    account's myTrades at all. When fills on the spot account do match a tracked grid bot (same pair, inside the
    bot's run, on one of its grid lines and with its quantity per order) they are marked as that bot's.
  * Orders placed through the API without an app prefix (random ids, broker "x-" ids) come from a program: one of
    the user's own scripts or a third-party bot. They are marked unknown until the user says what they are.
  Order of the signals: the user's override, then an app clientOrderId (manual), then a match to a tracked grid
  bot (bot), then any other clientOrderId (unknown). A fill whose order details Binance did not return is unknown.

Round trips: spot positions are long only. A sell with no imported buy before it (coins bought before the
imported history, deposits) is left out and noted. Futures positions are signed, per position side in hedge mode;
a fill that flips the position closes one round trip and opens the next. A position below DUST of its peak counts
as flat. Spot PnL = sell proceeds − buy cost − fees in the quote asset (fees taken in the coin show up as a
smaller position); futures PnL = Binance's realizedPnl − commissions. Fees paid in BNB are not converted and are
noted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

import httpx
import numpy as np
from pydantic import BaseModel, Field, ValidationError, field_validator

from .binance_account import BinanceAccount, BinanceApiError, BinanceKeyError, write_private
from .config import Settings, get_settings
from .gridbot import RECENT_FILLS, GridBot, GridBotResult, GridBotService, split_symbol
from .journal import ImportedTrade, JournalService, NewJournalEntry
from .scanner import tickers

log = logging.getLogger(__name__)

DAY_MS = 86_400_000
STABLES = {"USDT", "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "EUR", "TRY"}
MAX_SYMBOLS = 40
MAX_FILLS = 20_000
MAX_PAGES = 10
PAGE = 1000
FUTURES_WINDOW_MS = 7 * DAY_MS - 1  # userTrades allows at most 7 days between startTime and endTime
DUST = 0.005           # a position under 0.5% of its peak is flat
LINE_TOL = 0.1         # a fill within 10% of a grid step of a line is on that line
QTY_TOL = 0.03         # and within 3% of the bot's quantity per order has its size
APP_PREFIXES = ("web_", "ios_", "and_", "android_", "electron_")  # inferred, see the module docstring
AUTO_MINUTES = (0, 5, 15, 30, 60, 240, 1440)
POSITIONS_TTL = 30.0
API_NOTE = ("Binance has no public API for Spot Grid bots, so the bot's own orders and fills can't be read. The "
            "real fills here are fills on your spot account that match this bot (its pair, while it ran, on its "
            "grid lines with its order size). Bots trade from the separate Trading Bots wallet, so usually none of "
            "their fills show up on the spot account.")

Kind = Literal["manual", "bot", "unknown"]
Market = Literal["spot", "futures"]


# ---------------------------------------------------------------- models --


class AccountFill(BaseModel):
    """One fill on the user's Binance account, with its classification."""

    key: str = Field(..., description="'spot:BTCUSDT:<trade id>' or 'futures:BTCUSDT:<trade id>'")
    market: Market
    symbol: str
    trade_id: int
    order_id: int
    time: int = Field(..., description="UNIX s")
    time_ms: int
    side: Literal["buy", "sell"]
    price: float
    qty: float
    quote_qty: float
    commission: float = 0.0
    commission_asset: str = ""
    maker: bool = False
    realized_pnl: Optional[float] = Field(None, description="Futures: Binance's realized PnL of this fill")
    position_side: str = Field("BOTH", description="Futures hedge mode: LONG / SHORT; BOTH in one-way mode")
    client_order_id: Optional[str] = None
    kind: Kind = "unknown"
    bot_id: Optional[str] = None
    reason: str = ""
    overridden: bool = False


class Override(BaseModel):
    kind: Kind
    bot_id: Optional[str] = None
    at: int = 0


class ImportSettings(BaseModel):
    auto_minutes: int = Field(0, description=f"Re-import every N minutes; one of {AUTO_MINUTES} (0 = off)")
    symbols: list[str] = Field(default_factory=list, max_length=MAX_SYMBOLS,
                               description="Spot pairs to import besides the wallet's coins and grid bot pairs")
    futures: bool = Field(True, description="Import USD-M futures fills and positions too")
    lookback_days: int = Field(30, ge=1, le=180, description="How far back the first futures import goes")

    @field_validator("auto_minutes")
    @classmethod
    def _auto(cls, v: int) -> int:
        if v not in AUTO_MINUTES:
            raise ValueError(f"auto_minutes must be one of {', '.join(map(str, AUTO_MINUTES))}")
        return v

    @field_validator("symbols")
    @classmethod
    def _symbols(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for s in v:
            s = s.replace("/", "").replace("-", "").strip().upper()
            if s.isalnum() and 5 <= len(s) <= 20 and s not in out:
                out.append(s)
        return out


class ClassifyRequest(BaseModel):
    """Override the classification of fills ('spot:BTCUSDT:123'), a spot holding ('holding:spot:BTC') or a futures
    position ('position:futures:BTCUSDT:BOTH'). kind null removes the override."""

    keys: list[str] = Field(..., min_length=1, max_length=2000)
    kind: Optional[Kind] = None
    bot_id: Optional[str] = None


class RoundTrip(BaseModel):
    external_id: str
    market: Market
    symbol: str
    position_side: str
    direction: Literal["long", "short"]
    status: Literal["open", "closed"]
    opened_at: int
    closed_at: Optional[int] = None
    qty: float = Field(..., description="Largest position size, in coins")
    entry_price: float
    exit_price: Optional[float] = None
    realized_pnl: float = Field(..., description="Quote asset, after fees (closed share only)")
    fees: float
    quote_asset: str
    fills: list[str]
    kind: Kind
    bot_id: Optional[str] = None
    notes: list[str] = Field(default_factory=list)


# --------------------------------------------------------------- parsing --


def parse_spot_fill(symbol: str, row: dict) -> AccountFill:
    """One /api/v3/myTrades row."""
    ms = int(row["time"])
    return AccountFill(key=f"spot:{symbol}:{row['id']}", market="spot", symbol=symbol, trade_id=int(row["id"]),
                       order_id=int(row["orderId"]), time=ms // 1000, time_ms=ms,
                       side="buy" if row.get("isBuyer") else "sell", price=float(row["price"]),
                       qty=float(row["qty"]), quote_qty=float(row.get("quoteQty") or 0.0),
                       commission=float(row.get("commission") or 0.0), commission_asset=row.get("commissionAsset", ""),
                       maker=bool(row.get("isMaker")))


def parse_futures_fill(row: dict) -> AccountFill:
    """One /fapi/v1/userTrades row."""
    ms = int(row["time"])
    sym = row["symbol"]
    return AccountFill(key=f"futures:{sym}:{row['id']}", market="futures", symbol=sym, trade_id=int(row["id"]),
                       order_id=int(row["orderId"]), time=ms // 1000, time_ms=ms,
                       side="buy" if str(row.get("side", "")).upper() == "BUY" else "sell",
                       price=float(row["price"]), qty=float(row["qty"]), quote_qty=float(row.get("quoteQty") or 0.0),
                       commission=float(row.get("commission") or 0.0), commission_asset=row.get("commissionAsset", ""),
                       maker=bool(row.get("maker")), realized_pnl=float(row.get("realizedPnl") or 0.0),
                       position_side=str(row.get("positionSide") or "BOTH").upper())


# -------------------------------------------------------- classification --


@dataclass
class BotRef:
    """What a tracked grid bot looks like on the account: its pair, run, lines and order size."""

    id: str
    name: str
    symbol: str
    start: int
    end: Optional[int]
    lines: np.ndarray
    qty: Optional[float]


def bot_ref(bot: GridBot, result: Optional[GridBotResult]) -> BotRef:
    p = bot.params
    if result is not None and result.lines:
        lines = np.asarray(result.lines, dtype=float)
    else:
        i = np.arange(p.grids + 1, dtype=float)
        lines = (p.lower * (p.upper / p.lower) ** (i / p.grids) if p.grid_type == "geometric"
                 else p.lower + (p.upper - p.lower) * i / p.grids)
    qty = result.qty_per_order if result is not None else p.qty_per_order
    return BotRef(bot.id, bot.name, p.symbol, int(p.start_time or 0), p.end_time, lines, qty)


def match_bot(symbol: str, t: int, price: float, order_qty: float, bots: list[BotRef]) -> tuple[BotRef, str] | None:
    """The tracked bot an order belongs to, by pair, time, grid line and order size, with the reason."""
    for b in bots:
        if b.symbol != symbol or t < b.start - 60 or (b.end is not None and t > b.end + 60) or len(b.lines) < 2:
            continue
        k = int(np.argmin(np.abs(b.lines - price)))
        step = float(b.lines[k + 1] - b.lines[k]) if k + 1 < len(b.lines) else float(b.lines[k] - b.lines[k - 1])
        if abs(price - b.lines[k]) > LINE_TOL * step:
            continue
        if b.qty:
            if abs(order_qty - b.qty) > QTY_TOL * b.qty:
                continue
            return b, (f"on grid line {k} ({b.lines[k]:g}) of grid bot '{b.name}' while it ran, with its order "
                       f"size {b.qty:g}")
        return b, f"on grid line {k} ({b.lines[k]:g}) of grid bot '{b.name}' while it ran (its order size is unknown)"
    return None


def classify(fills: list[AccountFill], bots: list[BotRef], overrides: dict[str, Override]) -> list[AccountFill]:
    """Each fill with kind, bot_id and reason set (new objects; the input is left alone). See the module
    docstring for the signals and their order."""
    order_qty: dict[tuple[str, str, int], float] = {}
    for f in fills:
        k = (f.market, f.symbol, f.order_id)
        order_qty[k] = order_qty.get(k, 0.0) + f.qty
    out: list[AccountFill] = []
    for f in fills:
        kind: Kind = "unknown"
        bot_id: Optional[str] = None
        cid = f.client_order_id
        ov = overrides.get(f.key)
        if ov is not None:
            kind, bot_id, reason = ov.kind, ov.bot_id, "You marked it " + (
                f"as grid bot {ov.bot_id}'s" if ov.kind == "bot" and ov.bot_id else ov.kind)
        elif cid and cid.lower().startswith(APP_PREFIXES):
            kind, reason = "manual", f"Placed by hand in the Binance app or website (order id '{cid[:24]}')"
        elif (hit := match_bot(f.symbol, f.time, f.price, order_qty[(f.market, f.symbol, f.order_id)],
                               bots if f.market == "spot" else [])):
            kind, bot_id, reason = "bot", hit[0].id, "Matches " + hit[1]
        elif cid:
            who = "a broker or third-party app" if cid.startswith("x-") else "a program"
            reason = (f"Placed through the API by {who} (order id '{cid[:24]}'): your own bot or script, or a "
                      "third-party tool")
        else:
            reason = "Binance did not return this order's details, so it can't be told apart"
        out.append(f.model_copy(update={"kind": kind, "bot_id": bot_id, "reason": reason,
                                        "overridden": ov is not None}))
    return out


# ------------------------------------------------------------ round trips --


def _quote_of(f: AccountFill) -> str:
    return "USDT" if f.market == "futures" and not split_symbol(f.symbol)[1] else split_symbol(f.symbol)[1]


def _fee(f: AccountFill) -> tuple[float, float, bool]:
    """(fee paid in the quote asset, fee taken in the coin itself (in coins), fee in another asset such as BNB,
    which is not counted)."""
    base, quote = split_symbol(f.symbol)
    a = f.commission_asset.upper()
    if not f.commission:
        return 0.0, 0.0, False
    if a == quote or (f.market == "futures" and a in ("USDT", "USDC")):
        return f.commission, 0.0, False
    if a == base and f.market == "spot":
        return 0.0, f.commission, False
    return 0.0, 0.0, True


class _Trip:
    def __init__(self, f: AccountFill, sign: int) -> None:
        self.first = f
        self.sign = sign
        self.keys: list[str] = []
        self.open_qty = self.open_value = 0.0   # opening fills: coins and quote value
        self.close_qty = self.close_value = 0.0
        self.cost = self.proceeds = 0.0         # spot: quote spent / received
        self.realized = 0.0                     # futures: Binance's realizedPnl
        self.fees = 0.0                         # paid in the quote asset (taken off the PnL)
        self.coin_fees = 0.0                    # taken in the coin, valued at the fill (already in the position)
        self.peak = 0.0
        self.closed_at: Optional[int] = None
        self.notes: set[str] = set()


def _finish(t: _Trip, f0: AccountFill, kind: Kind, bot_id: Optional[str]) -> RoundTrip:
    closed = t.closed_at is not None
    if f0.market == "spot":
        realized = (t.proceeds - t.cost - t.fees) if closed else 0.0
    else:
        realized = t.realized - t.fees
    return RoundTrip(
        external_id=f"binance:{f0.market}:{f0.symbol}:{f0.position_side}:{t.first.trade_id}", market=f0.market,
        symbol=f0.symbol, position_side=f0.position_side, direction="long" if t.sign > 0 else "short",
        status="closed" if closed else "open", opened_at=t.first.time, closed_at=t.closed_at,
        qty=round(t.peak, 12), entry_price=t.open_value / t.open_qty if t.open_qty else t.first.price,
        exit_price=t.close_value / t.close_qty if t.close_qty else None, realized_pnl=round(realized, 8),
        fees=round(t.fees + t.coin_fees, 8), quote_asset=_quote_of(f0), fills=t.keys, kind=kind, bot_id=bot_id,
        notes=sorted(t.notes))


def build_round_trips(fills: list[AccountFill]) -> list[RoundTrip]:
    """Round trips from classified fills, one position per (market, symbol, position side, kind, bot)."""
    groups: dict[tuple, list[AccountFill]] = {}
    for f in fills:
        groups.setdefault((f.market, f.symbol, f.position_side, f.kind, f.bot_id), []).append(f)
    trips: list[RoundTrip] = []
    for (market, _sym, _ps, kind, bot_id), rows in groups.items():
        found: list[RoundTrip] = []
        rows.sort(key=lambda f: (f.time_ms, f.trade_id))
        pos = 0.0
        cur: Optional[_Trip] = None
        orphans = 0
        for f in rows:
            fee_q, fee_base, other_fee = _fee(f)
            delta = (f.qty - fee_base) if f.side == "buy" else -f.qty
            if market == "spot" and delta < 0 and pos <= 0:
                orphans += 1  # coins bought before the imported history (or deposited)
                continue
            if cur is None:
                cur = _Trip(f, 1 if delta > 0 else -1)
            if other_fee:
                cur.notes.add(f"Fees paid in {f.commission_asset} are not included in the PnL.")
            cur.keys.append(f.key)
            cur.coin_fees += fee_base * f.price
            remaining = abs(delta)
            if np.sign(delta) == cur.sign:  # adds to the position
                cur.open_qty += f.qty
                cur.open_value += f.qty * f.price
                cur.cost += f.qty * f.price
                cur.fees += fee_q
                if f.realized_pnl:
                    cur.realized += f.realized_pnl
                pos += delta
                cur.peak = max(cur.peak, abs(pos))
                continue
            close = min(remaining, abs(pos))
            share = close / remaining if remaining else 1.0
            if market == "spot" and close < remaining:
                cur.notes.add("Sold more than the imported buys: the rest came from coins bought earlier.")
            cur.close_qty += close
            cur.close_value += close * f.price
            cur.proceeds += close * f.price
            cur.fees += fee_q * share
            cur.realized += f.realized_pnl or 0.0  # Binance books a fill's realized PnL on the part that closes
            pos += close * (1 if delta > 0 else -1)
            if abs(pos) <= DUST * cur.peak:
                cur.closed_at = f.time
                found.append(_finish(cur, f, kind, bot_id))
                pos, cur = 0.0, None
                rest = remaining - close
                if market == "futures" and rest > 1e-12:  # the fill flipped the position: the rest opens the next
                    cur = _Trip(f, 1 if delta > 0 else -1)
                    cur.keys.append(f.key)
                    cur.open_qty, cur.open_value = rest, rest * f.price
                    cur.fees += fee_q * (1 - share)
                    pos = rest * cur.sign
                    cur.peak = rest
        if cur is not None:
            found.append(_finish(cur, rows[-1], kind, bot_id))
        if orphans and found:
            found[0].notes.append(f"{orphans} earlier sell{'s' if orphans != 1 else ''} of coins bought before the "
                                  "imported history left out.")
        trips += found
    trips.sort(key=lambda t: (t.opened_at, t.external_id))
    return trips


def journal_entry(t: RoundTrip) -> NewJournalEntry:
    """A closed round trip as a journal entry (`imported` carries the fills' result)."""
    return NewJournalEntry(
        symbol=t.symbol, interval="1h", direction=t.direction, entry=t.entry_price, stop=None, targets=[],
        setup=f"Binance {t.market}", source="binance", notes="", fee_pct=0.0, size_qty=t.qty, taken_at=t.opened_at,
        imported=ImportedTrade(external_id=t.external_id, market=t.market, opened_at=t.opened_at,
                               closed_at=t.closed_at, qty=t.qty, entry_price=t.entry_price, exit_price=t.exit_price,
                               realized_pnl=t.realized_pnl, fees=t.fees, fills=len(t.fills),
                               quote_asset=t.quote_asset, notes=t.notes))


# -------------------------------------------------------------- positions --


def average_entry(fills: list[AccountFill]) -> tuple[float, Optional[float]]:
    """(coins held, average cost) from spot fills, oldest first; sells take coins out at the average cost."""
    qty = cost = 0.0
    for f in sorted(fills, key=lambda f: (f.time_ms, f.trade_id)):
        _, fee_base, _ = _fee(f)
        if f.side == "buy":
            qty += f.qty - fee_base
            cost += f.qty * f.price
        elif qty > 0:
            sold = min(f.qty, qty)
            cost -= cost / qty * sold
            qty -= sold
    return qty, (cost / qty if qty > 1e-12 else None)


# ---------------------------------------------------------------- service --


class BinanceImportService:
    """Fills, classifications and import settings (BINANCE_IMPORT_STORE, mode 600), the import itself and the
    optional auto-import loop."""

    def __init__(self, account: BinanceAccount, journal: JournalService, gridbots: GridBotService, market: Any,
                 settings: Settings | None = None, store: str | None = None) -> None:
        self.account = account
        self.journal = journal
        self.gridbots = gridbots
        self.market = market
        s = settings or get_settings()
        path = (store if store is not None else s.binance_import_store).strip()
        self._store = None if path.lower() in ("", "memory", "none", "off") else Path(path)
        self.settings = ImportSettings()
        self._fills: dict[str, AccountFill] = {}
        self._overrides: dict[str, Override] = {}
        self._cursors: dict[str, int] = {}
        self._invalid: set[str] = set()
        self.last: Optional[dict] = None
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._positions: Optional[tuple[float, dict]] = None
        self._load()

    # ----------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            await self.auto_tick()

    async def auto_tick(self, now: Optional[float] = None) -> bool:
        """Run the import when auto-import is on and it is due. True when it ran (or tried to)."""
        now = time.time() if now is None else now
        minutes = self.settings.auto_minutes
        if minutes <= 0 or self._lock.locked() or self.account.store.load() is None:
            return False
        if now - (self.last or {}).get("at", 0) < minutes * 60:
            return False
        try:
            await self.run(auto=True)
        except Exception as exc:  # the next tick tries again; the status shows the error
            log.warning("Binance auto-import failed: %s", exc)
            self.last = {"at": int(now), "auto": True, "error": str(exc)}
            self._save()
        return True

    # -------------------------------------------------------------- status
    def status(self) -> dict:
        kinds = {"manual": 0, "bot": 0, "unknown": 0}
        for f in self._fills.values():
            kinds[f.kind] += 1
        return {"settings": self.settings.model_dump(), "last_import": self.last, "fills": len(self._fills),
                "by_kind": kinds, "running": self._lock.locked(), "key": self.account.status(),
                "auto_minutes_options": list(AUTO_MINUTES), "spot_grid_api": False, "spot_grid_note": API_NOTE}

    def update_settings(self, new: ImportSettings) -> dict:
        self.settings = new
        self._save()
        return self.status()

    def fills(self, symbol: Optional[str] = None, kind: Optional[str] = None, market: Optional[str] = None,
              limit: int = 500) -> list[AccountFill]:
        rows = [f for f in self._fills.values() if (not symbol or f.symbol == symbol) and (not kind or f.kind == kind)
                and (not market or f.market == market)]
        rows.sort(key=lambda f: (-f.time_ms, f.key))
        return rows[:limit]

    def trades(self, kind: Optional[str] = None) -> list[RoundTrip]:
        return [t for t in build_round_trips(list(self._fills.values())) if not kind or t.kind == kind]

    # -------------------------------------------------------------- import
    async def run(self, symbols: Optional[list[str]] = None, auto: bool = False) -> dict:
        """Fetch new fills, classify everything and sync the journal. Raises BinanceKeyError when there is no
        usable key; Binance errors on single symbols are collected in the summary's notes."""
        async with self._lock:
            await self.account.ensure_safe()
            started = time.time()
            notes: list[str] = []
            new: list[AccountFill] = []
            balances: dict[str, float] = {}
            try:
                balances = await self.account.spot_balances()
            except (BinanceApiError, httpx.HTTPError) as exc:
                notes.append(f"Spot balances: {exc}")
            spot_syms = self._spot_symbols(symbols or [], balances)
            for sym in spot_syms:
                try:
                    new += await self._spot_fills(sym)
                except BinanceApiError as exc:
                    if exc.code == -1121:  # not a pair on Binance
                        self._invalid.add(sym)
                    else:
                        notes.append(f"{sym} spot: {exc}")
            if self.settings.futures:
                try:
                    new += await self._futures_fills()
                except (BinanceApiError, httpx.HTTPError) as exc:
                    notes.append(f"USD-M futures unavailable: {exc}")
            await self._client_order_ids(new, notes)
            for f in new:
                self._fills[f.key] = f
            self._trim()
            sync = await self._reclassify()
            self._positions = None
            self.last = {"at": int(started), "auto": auto, "new_fills": len(new), "symbols": len(spot_syms),
                         "journal": sync, "notes": notes, "error": None,
                         "seconds": round(time.time() - started, 1)}
            self._save()
            return self.last

    def _spot_symbols(self, extra: list[str], balances: dict[str, float]) -> list[str]:
        out: list[str] = []
        held = [f"{a}USDT" for a in balances if a not in STABLES]
        bots = [b.params.symbol for b in self.gridbots.list()]
        cursors = [k.split(":", 1)[1] for k in self._cursors if k.startswith("spot:")]
        for s in [*extra, *self.settings.symbols, *bots, *held, *cursors]:
            s = s.upper()
            if s not in out and s not in self._invalid and s.isalnum():
                out.append(s)
        return out[:MAX_SYMBOLS]

    async def _spot_fills(self, symbol: str) -> list[AccountFill]:
        key = f"spot:{symbol}"
        from_id = self._cursors.get(key, -1) + 1
        out: list[AccountFill] = []
        for _ in range(MAX_PAGES):
            rows = await self.account.my_trades(symbol, from_id, PAGE)
            out += [parse_spot_fill(symbol, r) for r in rows]
            if len(rows) < PAGE:
                break
            from_id = int(rows[-1]["id"]) + 1
        if out:
            self._cursors[key] = max(f.trade_id for f in out)
        return [f for f in out if f.key not in self._fills]

    async def _futures_fills(self) -> list[AccountFill]:
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - self.settings.lookback_days * DAY_MS
        syms = {k.split(":", 1)[1] for k in self._cursors if k.startswith("futures:")}
        syms |= await self.account.futures_pnl_symbols(start_ms)
        syms |= {p["symbol"] for p in await self.account.futures_positions()}
        out: list[AccountFill] = []
        for sym in sorted(syms)[:MAX_SYMBOLS]:
            key = f"futures:{sym}"
            rows: list[dict] = []
            if key in self._cursors:
                from_id = self._cursors[key] + 1
                for _ in range(MAX_PAGES):
                    page = await self.account.futures_trades(sym, from_id=from_id, limit=PAGE)
                    rows += page
                    if len(page) < PAGE:
                        break
                    from_id = int(page[-1]["id"]) + 1
            else:  # first import: 7-day windows back to the lookback
                t = start_ms
                while t < now_ms:
                    end = min(t + FUTURES_WINDOW_MS, now_ms)
                    page = await self.account.futures_trades(sym, start_ms=t, end_ms=end, limit=PAGE)
                    rows += page
                    t = int(page[-1]["time"]) + 1 if len(page) >= PAGE else end + 1
            fills = [parse_futures_fill(r) for r in rows]
            if fills:
                self._cursors[key] = max(f.trade_id for f in fills)
            out += [f for f in fills if f.key not in self._fills]
        return out

    async def _client_order_ids(self, new: list[AccountFill], notes: list[str]) -> None:
        """Fill in clientOrderId from allOrders for the orders the new fills belong to."""
        by_sym: dict[tuple[str, str], list[AccountFill]] = {}
        for f in new:
            by_sym.setdefault((f.market, f.symbol), []).append(f)
        for (market, sym), rows in by_sym.items():
            want = {f.order_id for f in rows}
            order_id = min(want)
            ids: dict[int, str] = {}
            try:
                for _ in range(MAX_PAGES):
                    page = await (self.account.all_orders(sym, order_id, PAGE) if market == "spot"
                                  else self.account.futures_orders(sym, order_id, PAGE))
                    for o in page:
                        if o.get("clientOrderId"):
                            ids[int(o["orderId"])] = str(o["clientOrderId"])
                    if len(page) < PAGE or want <= ids.keys():
                        break
                    order_id = int(page[-1]["orderId"]) + 1
            except (BinanceApiError, httpx.HTTPError) as exc:
                notes.append(f"{sym} {market} orders: {exc}")
            for f in rows:
                f.client_order_id = ids.get(f.order_id)

    def _trim(self) -> None:
        if len(self._fills) > MAX_FILLS:
            keep = sorted(self._fills.values(), key=lambda f: -f.time_ms)[:MAX_FILLS]
            self._fills = {f.key: f for f in keep}

    async def _bot_refs(self) -> list[BotRef]:
        refs = []
        for bot in self.gridbots.list():
            try:
                result = await self.gridbots.result(bot.id)
            except Exception as exc:  # classify by the settings' lines alone
                log.info("Grid bot %s result unavailable for classification: %s", bot.id, exc)
                result = None
            refs.append(bot_ref(bot, result))
        return refs

    async def _reclassify(self) -> dict:
        """Classify every stored fill again (bots and overrides change) and sync the journal."""
        bots = await self._bot_refs()
        fills = classify(list(self._fills.values()), bots, self._overrides)
        self._fills = {f.key: f for f in fills}
        manual = [journal_entry(t) for t in build_round_trips(fills) if t.kind == "manual" and t.status == "closed"]
        return self.journal.sync_imported(manual)

    async def set_classification(self, req: ClassifyRequest) -> dict:
        """Override (or with kind null, restore) the classification of fills, holdings or positions."""
        if req.kind == "bot" and req.bot_id and self.gridbots.get(req.bot_id) is None:
            raise ValueError("No tracked grid bot with that id")
        now = int(time.time())
        for k in req.keys:
            if req.kind is None:
                self._overrides.pop(k, None)
            else:
                self._overrides[k] = Override(kind=req.kind, bot_id=req.bot_id if req.kind == "bot" else None, at=now)
        async with self._lock:
            sync = await self._reclassify()
        self._positions = None
        self._save()
        return {"journal": sync, "updated": len(req.keys)}

    # ----------------------------------------------------------- positions
    async def positions(self, refresh: bool = False) -> dict:
        """Open positions: the user's own (spot holdings with average entry, USD-M positions) and the bots'."""
        await self.account.ensure_safe()  # also when cached: a removed or unsafe key shows nothing
        hit = self._positions
        if hit and not refresh and time.monotonic() - hit[0] < POSITIONS_TTL:
            return hit[1]
        notes: list[str] = []
        balances = await self.account.spot_balances()
        wallets: list[dict] = []
        futures: list[dict] = []
        try:
            wallets = await self.account.wallet_balances()
        except (BinanceApiError, httpx.HTTPError) as exc:
            notes.append(f"Wallet balances unavailable: {exc}")
        if self.settings.futures:
            try:
                futures = await self.account.futures_positions()
            except (BinanceApiError, httpx.HTTPError) as exc:
                notes.append(f"USD-M futures unavailable: {exc}")
        fills = list(self._fills.values())
        coins = [a for a in balances if a not in STABLES]
        prices = await self._prices([f"{a}USDT" for a in coins] + [p["symbol"] for p in futures])

        manual_spot, bot_spot, cash = [], [], []
        for asset, qty in sorted(balances.items()):
            if asset in STABLES:
                cash.append({"asset": asset, "qty": qty})
                continue
            sym = f"{asset}USDT"
            mine = [f for f in fills if f.market == "spot" and split_symbol(f.symbol)[0] == asset]
            by_bot: dict[str, float] = {}
            for f in mine:
                if f.kind == "bot" and f.bot_id:
                    by_bot[f.bot_id] = by_bot.get(f.bot_id, 0.0) + (f.qty if f.side == "buy" else -f.qty)
            bot_qty = sum(max(0.0, q) for q in by_bot.values())
            tracked, avg = average_entry([f for f in mine if f.kind == "manual"])
            own = max(0.0, qty - bot_qty)
            price = prices.get(sym)
            key = f"holding:spot:{asset}"
            ov = self._overrides.get(key)
            row = {"key": key, "asset": asset, "symbol": sym, "qty": qty, "own_qty": round(own, 12),
                   "avg_entry": avg, "from_fills_qty": round(min(tracked, own), 12),
                   "untracked_qty": round(max(0.0, own - tracked), 12), "price": price,
                   "value": round(own * price, 2) if price else None,
                   "unrealized_pnl": round(min(tracked, own) * (price - avg), 2) if price and avg else None,
                   "kind": ov.kind if ov else "manual", "bot_id": ov.bot_id if ov else None,
                   "reason": ("You marked it " + ov.kind) if ov else (
                       "In the spot wallet (bots hold their coins in the Trading Bots wallet)"
                       + ("; average entry from your own fills" if avg else "; no own buys imported for it"))}
            for bid, bq in by_bot.items():
                if bq > 0:
                    bot_spot.append({"bot_id": bid, "asset": asset, "symbol": sym, "qty": round(bq, 12),
                                     "reason": "Net of the fills matched to this grid bot"})
            (bot_spot if row["kind"] == "bot" else manual_spot).append(row)

        manual_fut, bot_fut = [], []
        for p in futures:
            sym, side_mode = p["symbol"], str(p.get("positionSide") or "BOTH").upper()
            amt = float(p["positionAmt"])
            key = f"position:futures:{sym}:{side_mode}"
            ov = self._overrides.get(key)
            last = max((f for f in fills if f.market == "futures" and f.symbol == sym and f.position_side == side_mode),
                       key=lambda f: f.time_ms, default=None)
            kind: Kind = ov.kind if ov else ("bot" if last is not None and last.kind == "bot" else "manual")
            reason = ("You marked it " + ov.kind) if ov else (
                "The latest fill on it is a bot's" if kind == "bot" else
                "No bot marker on its fills" if last is not None else "No imported fills; assumed your own")
            row = {"key": key, "symbol": sym, "position_side": side_mode, "side": "long" if amt > 0 else "short",
                   "qty": abs(amt), "entry_price": float(p.get("entryPrice") or 0),
                   "mark_price": float(p.get("markPrice") or 0) or prices.get(sym),
                   "unrealized_pnl": float(p.get("unRealizedProfit") or 0), "leverage": p.get("leverage"),
                   "liquidation_price": float(p.get("liquidationPrice") or 0) or None, "kind": kind,
                   "bot_id": ov.bot_id if ov else (last.bot_id if last is not None and kind == "bot" else None),
                   "reason": reason}
            (bot_fut if kind == "bot" else manual_fut).append(row)

        tracked_bots = []
        for b in self.gridbots.list():
            try:
                r = await self.gridbots.result(b.id)
            except Exception:
                r = None
            tracked_bots.append({"bot_id": b.id, "name": b.name, "symbol": b.params.symbol,
                                 "base_held": r.base_held if r else None, "quote_held": r.quote_held if r else None,
                                 "value": r.current_value if r else None, "simulated": True})
        bot_wallet = next((w for w in wallets if w["wallet"].lower().replace(" ", "") == "tradingbots"), None)
        out = {"manual": {"spot": manual_spot, "futures": manual_fut, "cash": cash},
               "bots": {"wallet": bot_wallet, "spot": bot_spot, "futures": bot_fut, "tracked": tracked_bots},
               "wallets": wallets, "notes": notes, "updated_at": int(time.time())}
        self._positions = (time.monotonic(), out)
        return out

    async def _prices(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        try:
            rows = await tickers(self.market, list(dict.fromkeys(symbols))[:40])
        except Exception as exc:
            log.info("Prices for holdings failed: %s", exc)
            return {}
        return {r["symbol"]: r["price"] for r in rows if r.get("source") == "binance"}

    # ------------------------------------------------- real vs simulated
    async def compare_bot(self, bot_id: str) -> Optional[dict]:
        """A tracked grid bot's real fills (fills matched to it) next to the simulator's, line by line."""
        bot = self.gridbots.get(bot_id)
        if bot is None:
            return None
        result = await self.gridbots.result(bot_id)
        assert result is not None
        real = sorted((f for f in self._fills.values() if f.kind == "bot" and f.bot_id == bot_id),
                      key=lambda f: f.time_ms)
        sim = sorted((f for f in result.recent_fills if f.kind == "grid"), key=lambda f: f.time)
        lines = np.asarray(result.lines, dtype=float)
        tol = float(np.min(np.diff(lines))) / 2 if len(lines) > 1 else 0.0
        sim_from = sim[0].time if len(result.recent_fills) >= RECENT_FILLS and sim else result.start_time
        used: set[int] = set()
        rows = []
        for f in real:
            j = next((i for i, s in enumerate(sim) if i not in used and s.side == f.side
                      and abs(s.price - f.price) <= tol and abs(s.time - f.time) <= 600), None)
            if j is not None:
                used.add(j)
            s = sim[j] if j is not None else None
            rows.append({"time": f.time, "side": f.side, "price": f.price,
                         "real": {"time": f.time, "price": f.price, "qty": f.qty, "key": f.key},
                         "sim": s.model_dump() if s else None})
        for i, s in enumerate(sim):
            if i not in used and s.time >= sim_from:
                rows.append({"time": s.time, "side": s.side, "price": s.price, "real": None, "sim": s.model_dump()})
        rows.sort(key=lambda r: -r["time"])
        both = sum(1 for r in rows if r["real"] and r["sim"])
        real_sells = [f for f in real if f.side == "sell"]
        quote = split_symbol(bot.params.symbol)[1]
        notes = [API_NOTE]
        if not real:
            notes.append("No fills on your spot account match this bot yet.")
        if self.account.store.load() is None:
            notes.append("Add a read-only Binance API key and import to see real fills.")
        return {"bot_id": bot_id, "name": bot.name, "symbol": bot.params.symbol, "quote_asset": quote,
                "real_fills": len(real), "sim_fills": sum(1 for s in sim if s.time >= sim_from), "matched": both,
                "real_only": sum(1 for r in rows if r["real"] and not r["sim"]),
                "sim_only": sum(1 for r in rows if r["sim"] and not r["real"]),
                "real_sells": len(real_sells), "sim_matched_trades": result.matched_trades,
                "real_net_quote": round(sum((f.qty * f.price) * (1 if f.side == "sell" else -1) for f in real)
                                        - sum(_fee(f)[0] for f in real), 8),
                "rows": rows[:400], "since": sim_from, "notes": notes, "spot_grid_api": False}

    # --------------------------------------------------------- persistence
    def _load(self) -> None:
        if not self._store or not self._store.exists():
            return
        try:
            data = json.loads(self._store.read_text())
            self.settings = ImportSettings.model_validate(data.get("settings", {}))
            self._cursors = {str(k): int(v) for k, v in data.get("cursors", {}).items()}
            self._overrides = {k: Override.model_validate(v) for k, v in data.get("overrides", {}).items()}
            self._fills = {f["key"]: AccountFill.model_validate(f) for f in data.get("fills", [])}
            self._invalid = set(data.get("invalid_symbols", []))
            self.last = data.get("last")
        except (OSError, ValueError, ValidationError, AttributeError, KeyError, TypeError) as exc:
            log.warning("Could not read %s (%s); starting with no imported fills", self._store, exc)

    def _save(self) -> None:
        if not self._store:
            return
        data = {"settings": self.settings.model_dump(), "cursors": self._cursors,
                "overrides": {k: v.model_dump() for k, v in self._overrides.items()},
                "fills": [f.model_dump() for f in self._fills.values()], "invalid_symbols": sorted(self._invalid),
                "last": self.last}
        try:
            write_private(self._store, json.dumps(data))  # trade history is private: mode 600 like the key
        except OSError as exc:
            log.warning("Could not save imported fills to %s: %s", self._store, exc)


__all__ = ["AccountFill", "BinanceImportService", "BinanceKeyError", "ClassifyRequest", "ImportSettings",
           "RoundTrip", "build_round_trips", "classify", "journal_entry", "match_bot", "parse_futures_fill",
           "parse_spot_fill"]
