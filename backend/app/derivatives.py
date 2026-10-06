"""Live derivatives metrics from Binance USD-M futures (free, no API key).

* Open interest: the top N USDT perpetuals by 24h quote volume, summed in USD
  from `/futures/data/openInterestHist` (1h buckets, so we also get the change
  versus 24 hours ago).
* Liquidations: the `!forceOrder@arr` stream, kept as a rolling 24h window and
  saved to disk so a restart does not reset the total. Binance pushes at most
  one liquidation per symbol per second on this stream, so the total is a
  floor, not an exact sum; the header says so. The latest events of each
  symbol are also kept with their price, for the per-coin market data panel.

Both cover Binance only, not every exchange.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
import websockets

from .config import Settings, get_settings

log = logging.getLogger(__name__)

DAY = 24 * 3600
# Latest liquidations kept per symbol (with price) for the market data panel: enough for a "recent" list
# without letting a busy day grow the saved file without bound.
SYMBOL_EVENTS_CAP = 200


@dataclass
class OpenInterest:
    usd: float
    change_pct: float | None
    symbols: int


@dataclass
class Liquidations:
    usd: float
    longs_usd: float
    shorts_usd: float
    since: float  # UNIX seconds of the oldest data we could have seen
    count: int


@dataclass
class LiquidationEvent:
    symbol: str
    time: float  # UNIX seconds
    price: float
    qty: float
    side: str  # 'long' (a long was closed) | 'short'

    @property
    def usd(self) -> float:
        return self.price * self.qty


def parse_force_order_events(msg: dict | list) -> list[LiquidationEvent]:
    """`!forceOrder@arr` payload → liquidation events with symbol and price. A SELL order closes a long."""
    out = []
    for ev in msg if isinstance(msg, list) else [msg]:
        o = ev.get("o") if isinstance(ev, dict) else None
        if not o:
            continue
        price = float(o.get("ap") or 0) or float(o.get("p") or 0)
        qty = float(o.get("z") or 0) or float(o.get("q") or 0)
        if price <= 0 or qty <= 0:
            continue
        side = "long" if o.get("S") == "SELL" else "short"
        t = int(o.get("T") or ev.get("E") or time.time() * 1000) / 1000
        out.append(LiquidationEvent(str(o.get("s") or ""), t, price, qty, side))
    return out


def parse_force_order(msg: dict | list) -> list[tuple[float, float, str]]:
    """`!forceOrder@arr` payload → [(time_s, notional_usd, 'long'|'short')], the header's rolling total."""
    return [(e.time, e.usd, e.side) for e in parse_force_order_events(msg)]


def top_perpetuals(tickers: list[dict], n: int) -> list[str]:
    perps = [t for t in tickers if t.get("symbol", "").endswith("USDT") and "_" not in t["symbol"]]
    perps.sort(key=lambda t: float(t.get("quoteVolume") or 0), reverse=True)
    return [t["symbol"] for t in perps[:n]]


def sum_open_interest(histories: list[list[dict]]) -> OpenInterest:
    """Each history is one symbol's 1h openInterestHist rows. Sums latest and ~24h-ago USD values."""
    now_total, then_total, used = 0.0, 0.0, 0
    for rows in histories:
        if not rows:
            continue
        rows = sorted(rows, key=lambda r: int(r["timestamp"]))
        now_total += float(rows[-1]["sumOpenInterestValue"])
        if len(rows) >= 25:
            then_total += float(rows[0]["sumOpenInterestValue"])
        else:  # newly listed: count it as unchanged rather than as a 100% jump
            then_total += float(rows[-1]["sumOpenInterestValue"])
        used += 1
    change = round((now_total - then_total) / then_total * 100, 2) if then_total else None
    return OpenInterest(usd=now_total, change_pct=change, symbols=used)


def parse_symbol_snapshot(premium: dict | None, oi_rows: list[dict] | None) -> dict | None:
    """premiumIndex + 1h openInterestHist rows → {funding_rate_pct, oi_usd, oi_change_24h_pct}."""
    out: dict = {}
    if isinstance(premium, dict) and premium.get("lastFundingRate") not in (None, ""):
        out["funding_rate_pct"] = round(float(premium["lastFundingRate"]) * 100, 4)
    if isinstance(oi_rows, list) and oi_rows:
        rows = sorted(oi_rows, key=lambda r: int(r["timestamp"]))
        now = float(rows[-1]["sumOpenInterestValue"])
        out["oi_usd"] = round(now)
        if len(rows) >= 25 and float(rows[0]["sumOpenInterestValue"]) > 0:
            out["oi_change_24h_pct"] = round((now / float(rows[0]["sumOpenInterestValue"]) - 1) * 100, 2)
    return out or None


class LiquidationTracker:
    """Rolling 24h window of liquidation notionals, persisted to a small JSON file.

    Next to the all-market totals it keeps the latest SYMBOL_EVENTS_CAP events of each symbol with price and
    size, saved under a separate "symbols" key so files written before it existed still load (and older code
    simply ignores the key)."""

    def __init__(self, store: str | None = None) -> None:
        self._events: deque[tuple[float, float, str]] = deque()
        # symbol → (time_s, price, qty, side), oldest first.
        self._by_symbol: dict[str, deque[tuple[float, float, float, str]]] = {}
        self._store = Path(store) if store else None
        self.since = time.time()
        self._load()

    def add(self, events: list[tuple[float, float, str]]) -> None:
        self._events.extend(events)

    def add_events(self, events: list[LiquidationEvent]) -> None:
        """Stream events: counted in the 24h total and kept per symbol."""
        self.add([(e.time, e.usd, e.side) for e in events])
        for e in events:
            if e.symbol:
                q = self._by_symbol.setdefault(e.symbol, deque(maxlen=SYMBOL_EVENTS_CAP))
                q.append((e.time, e.price, e.qty, e.side))

    def recent(self, symbol: str, limit: int = 50, now: float | None = None) -> list[dict]:
        """The latest liquidations of one symbol in the last 24h, newest first."""
        cutoff = (now or time.time()) - DAY
        rows = [e for e in self._by_symbol.get(symbol, ()) if e[0] >= cutoff]
        return [{"time": int(t), "price": p, "qty": q, "usd": round(p * q, 2), "side": side}
                for t, p, q, side in reversed(rows[-limit:])]

    def _prune(self, now: float) -> None:
        while self._events and self._events[0][0] < now - DAY:
            self._events.popleft()
        for sym in list(self._by_symbol):
            q = self._by_symbol[sym]
            while q and q[0][0] < now - DAY:
                q.popleft()
            if not q:
                del self._by_symbol[sym]

    def snapshot(self, now: float | None = None) -> Liquidations:
        now = now or time.time()
        self._prune(now)
        longs = sum(n for _, n, s in self._events if s == "long")
        shorts = sum(n for _, n, s in self._events if s == "short")
        return Liquidations(usd=longs + shorts, longs_usd=longs, shorts_usd=shorts,
                            since=max(self.since, now - DAY), count=len(self._events))

    def _load(self) -> None:
        if not self._store or not self._store.exists():
            return
        try:
            data = json.loads(self._store.read_text())
            cutoff = time.time() - DAY
            self.since = float(data.get("since", self.since))
            self._events.extend(tuple(e) for e in data.get("events", []) if e[0] >= cutoff)  # type: ignore[misc]
        except (OSError, ValueError, TypeError, IndexError) as exc:
            log.warning("Could not read %s (%s); starting liquidations from zero", self._store, exc)
            return
        symbols = data.get("symbols") if isinstance(data, dict) else None
        if not isinstance(symbols, dict):  # written before per-symbol events existed
            return
        for sym, rows in symbols.items():
            try:
                kept = [(float(t), float(p), float(q), str(side)) for t, p, q, side in rows if float(t) >= cutoff]
            except (TypeError, ValueError):
                continue
            if kept:
                self._by_symbol[str(sym)] = deque(kept[-SYMBOL_EVENTS_CAP:], maxlen=SYMBOL_EVENTS_CAP)

    def save(self) -> None:
        if not self._store:
            return
        self._prune(time.time())
        try:
            self._store.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._store.with_suffix(".tmp")
            tmp.write_text(json.dumps({"since": self.since, "events": list(self._events),
                                       "symbols": {k: list(v) for k, v in self._by_symbol.items()}}))
            tmp.replace(self._store)
        except OSError as exc:
            log.warning("Could not save liquidations to %s: %s", self._store, exc)


class DerivativesService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.s = settings or get_settings()
        self.enabled = self.s.derivatives_enabled and self.s.data_source != "synthetic"
        self.liquidations = LiquidationTracker(self.s.liquidations_store if self.enabled else None)
        self.stream_connected = False
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0),
                                         headers={"User-Agent": "agentic-charts/1.0"})
        self._oi_cache: tuple[float, OpenInterest] | None = None
        self._symbol_cache: dict[str, tuple[float, dict | None]] = {}
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self.enabled and self._task is None:
            self._task = asyncio.create_task(self._run_stream())

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self.liquidations.save()
        await self._client.aclose()

    # ------------------------------------------------------- open interest
    async def open_interest(self) -> OpenInterest | None:
        if not self.enabled:
            return None
        if self._oi_cache and self._oi_cache[0] > time.monotonic():
            return self._oi_cache[1]
        base = self.s.binance_futures_rest_url
        r = await self._client.get(f"{base}/fapi/v1/ticker/24hr")
        r.raise_for_status()
        symbols = top_perpetuals(r.json(), self.s.oi_top_symbols)
        sem = asyncio.Semaphore(8)

        async def hist(sym: str) -> list[dict]:
            async with sem:
                resp = await self._client.get(f"{base}/futures/data/openInterestHist",
                                              params={"symbol": sym, "period": "1h", "limit": 25})
                resp.raise_for_status()
                return resp.json()

        results = await asyncio.gather(*(hist(s) for s in symbols), return_exceptions=True)
        histories = [x for x in results if isinstance(x, list)]
        if not histories:
            raise RuntimeError("no open interest data returned")
        oi = sum_open_interest(histories)
        self._oi_cache = (time.monotonic() + 300, oi)  # 1h buckets: no point refreshing faster
        return oi

    # ----------------------------------------------------------- per symbol
    async def symbol_snapshot(self, symbol: str) -> dict | None:
        """Funding rate and 24h open-interest change for one perpetual, or None when unavailable
        (spot-only coin, Binance futures blocked, or derivatives off). Cached for a minute."""
        if not self.enabled:
            return None
        cached = self._symbol_cache.get(symbol)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        base = self.s.binance_futures_rest_url
        out: dict | None = None
        try:
            prem, hist = await asyncio.gather(
                self._client.get(f"{base}/fapi/v1/premiumIndex", params={"symbol": symbol}, timeout=4.0),
                self._client.get(f"{base}/futures/data/openInterestHist",
                                 params={"symbol": symbol, "period": "1h", "limit": 25}, timeout=4.0))
            out = parse_symbol_snapshot(prem.json() if prem.status_code == 200 else None,
                                        hist.json() if hist.status_code == 200 else None)
        except (httpx.HTTPError, ValueError) as exc:
            log.info("Derivatives for %s unavailable: %s", symbol, exc)
        self._symbol_cache[symbol] = (time.monotonic() + 60, out)
        return out

    # --------------------------------------------------------- liquidations
    def liquidation_snapshot(self) -> Liquidations | None:
        if not self.enabled:
            return None
        snap = self.liquidations.snapshot()
        # Without a connection and without saved history there is nothing real to show.
        if not self.stream_connected and snap.count == 0:
            return None
        return snap

    def recent_liquidations(self, symbol: str, limit: int = 50) -> list[dict] | None:
        """Latest liquidations of one symbol (newest first), or None when the stream is off and nothing is saved."""
        if not self.enabled:
            return None
        rows = self.liquidations.recent(symbol, limit)
        if not rows and not self.stream_connected:
            return None
        return rows

    async def _run_stream(self) -> None:
        url = f"{self.s.binance_futures_ws_url}/!forceOrder@arr"
        backoff = 2.0
        last_save = time.monotonic()
        while True:
            try:
                async with websockets.connect(url, open_timeout=10, ping_interval=20, ping_timeout=20) as ws:
                    self.stream_connected = True
                    backoff = 2.0
                    log.info("Liquidation stream connected")
                    async for raw in ws:
                        self.liquidations.add_events(parse_force_order_events(json.loads(raw)))
                        if time.monotonic() - last_save > 60:
                            self.liquidations.save()
                            last_save = time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Liquidation stream error: %s; retrying in %.0fs", exc, backoff)
            self.stream_connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300.0)


def since_label(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %d %H:%M UTC")
