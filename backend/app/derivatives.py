"""Live derivatives metrics from Binance USD-M futures (free, no API key).

* Open interest: the top N USDT perpetuals by 24h quote volume, summed in USD
  from `/futures/data/openInterestHist` (1h buckets, so we also get the change
  versus 24 hours ago).
* Liquidations: the `!forceOrder@arr` stream, kept as a rolling 24h window and
  saved to disk so a restart does not reset the total. Binance pushes at most
  one liquidation per symbol per second on this stream, so the total is a
  floor, not an exact sum; the header says so.

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


def parse_force_order(msg: dict | list) -> list[tuple[float, float, str]]:
    """`!forceOrder@arr` payload → [(time_s, notional_usd, 'long'|'short')]. A SELL order closes a long."""
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
        out.append((int(o.get("T") or ev.get("E") or time.time() * 1000) / 1000, price * qty, side))
    return out


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


class LiquidationTracker:
    """Rolling 24h window of liquidation notionals, persisted to a small JSON file."""

    def __init__(self, store: str | None = None) -> None:
        self._events: deque[tuple[float, float, str]] = deque()
        self._store = Path(store) if store else None
        self.since = time.time()
        self._load()

    def add(self, events: list[tuple[float, float, str]]) -> None:
        self._events.extend(events)

    def _prune(self, now: float) -> None:
        while self._events and self._events[0][0] < now - DAY:
            self._events.popleft()

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

    def save(self) -> None:
        if not self._store:
            return
        self._prune(time.time())
        try:
            self._store.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._store.with_suffix(".tmp")
            tmp.write_text(json.dumps({"since": self.since, "events": list(self._events)}))
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

    # --------------------------------------------------------- liquidations
    def liquidation_snapshot(self) -> Liquidations | None:
        if not self.enabled:
            return None
        snap = self.liquidations.snapshot()
        # Without a connection and without saved history there is nothing real to show.
        if not self.stream_connected and snap.count == 0:
            return None
        return snap

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
                        self.liquidations.add(parse_force_order(json.loads(raw)))
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
