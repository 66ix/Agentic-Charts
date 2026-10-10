"""Live kline fan-out.

One upstream Binance WebSocket per (symbol, interval) is shared by every browser
connected to that stream; it opens with the first subscriber and closes with the
last. Derived intervals (3h) subscribe to the base interval and aggregate.
When Binance is unreachable and DATA_SOURCE allows it, the stream switches to a
synthetic tick generator and tells clients via a `status` message, and goes back to
Binance once it answers again.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field

import numpy as np
import websockets

from .market_data import DERIVED_INTERVALS, INTERVAL_SECONDS, MarketData
from .schemas import Candle

log = logging.getLogger(__name__)

QUEUE_SIZE = 256
SYNTHETIC_RECHECK = 30.0   # seconds between looks at whether Binance is back while streaming synthetic candles
CONNECT_FAILS = 3          # failed connects in a row before a stream that never connected streams synthetic
WS_DOWN_FOR = 60.0         # how long such a stream stays synthetic before trying Binance again


@dataclass
class _Stream:
    symbol: str
    interval: str
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    task: asyncio.Task | None = None
    source: str = "connecting"
    ws_retry_at: float = 0.0  # a stream whose WebSocket won't connect stays synthetic until then


def parse_ws_kline(k: dict) -> tuple[Candle, bool]:
    """Binance `<symbol>@kline_<interval>` payload's `k` object → (Candle, is_closed)."""
    return (
        Candle(time=int(k["t"]) // 1000, open=float(k["o"]), high=float(k["h"]), low=float(k["l"]),
               close=float(k["c"]), volume=float(k["v"])),
        bool(k["x"]),
    )


class StreamHub:
    def __init__(self, market: MarketData) -> None:
        self.market = market
        self._streams: dict[tuple[str, str], _Stream] = {}
        self._lock = asyncio.Lock()
        self._last_price: dict[str, float] = {}  # newest close streamed per symbol (the demo heatmap centres on it)

    async def subscribe(self, symbol: str, interval: str) -> asyncio.Queue:
        key = (symbol.upper(), interval)
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        async with self._lock:
            stream = self._streams.get(key)
            if stream is None:
                stream = _Stream(*key)
                self._streams[key] = stream
                stream.task = asyncio.create_task(self._run(stream), name=f"kline:{key}")
            stream.subscribers.add(queue)
            if stream.source != "connecting":
                queue.put_nowait({"type": "status", "source": stream.source})
        return queue

    async def unsubscribe(self, symbol: str, interval: str, queue: asyncio.Queue) -> None:
        key = (symbol.upper(), interval)
        async with self._lock:
            stream = self._streams.get(key)
            if not stream:
                return
            stream.subscribers.discard(queue)
            if not stream.subscribers:
                self._streams.pop(key, None)
                if stream.task:
                    stream.task.cancel()

    async def shutdown(self) -> None:
        async with self._lock:
            tasks = [s.task for s in self._streams.values() if s.task]
            self._streams.clear()
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    def last_price(self, symbol: str) -> float | None:
        """Close of the newest candle streamed for `symbol` on any interval, or None when it isn't streamed."""
        return self._last_price.get(symbol.upper())

    def stats(self) -> list[dict]:
        return [{"symbol": s.symbol, "interval": s.interval, "subscribers": len(s.subscribers), "source": s.source}
                for s in self._streams.values()]

    # ----------------------------------------------------------- internals
    def _publish(self, stream: _Stream, msg: dict) -> None:
        if msg.get("type") == "kline":
            self._last_price[stream.symbol] = msg["candle"]["close"]
        for q in list(stream.subscribers):
            if q.full():  # slow consumer: drop its oldest message rather than block everyone
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            q.put_nowait(msg)

    def _set_source(self, stream: _Stream, source: str, message: str = "") -> None:
        stream.source = source
        self._publish(stream, {"type": "status", "source": source, "message": message})

    async def _run(self, stream: _Stream) -> None:
        try:
            backoff = 1.0
            fails = 0
            while True:
                if not self.market.binance_usable() or time.monotonic() < stream.ws_retry_at:
                    self._set_source(stream, "synthetic", "Binance unreachable; streaming synthetic data")
                    await self._run_synthetic(stream)  # returns once Binance looks usable again
                    continue
                try:
                    await self._run_binance(stream)
                    backoff, fails = 1.0, 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("Binance stream %s %s failed: %s", stream.symbol, stream.interval, exc)
                    if stream.source == "binance":
                        fails = 0
                    fails += 1
                    # A WebSocket that won't connect says nothing about REST: never demote Binance for every caller
                    # here. A stream that never connected streams synthetic for a while after a few tries.
                    if (self.market.settings.data_source == "auto" and stream.source in ("connecting", "synthetic")
                            and fails >= CONNECT_FAILS):
                        stream.ws_retry_at = time.monotonic() + WS_DOWN_FOR
                        fails = 0
                        continue
                    if stream.source == "binance":
                        self._set_source(stream, "reconnecting", str(exc)[:200])
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
        except asyncio.CancelledError:
            pass

    async def _run_binance(self, stream: _Stream) -> None:
        base, factor = DERIVED_INTERVALS.get(stream.interval, (stream.interval, 1))
        bucket_s = INTERVAL_SECONDS[stream.interval]
        # A REST call first: it seeds derived buckets and, if the primary host is region-blocked,
        # switches market.ws_url to the fallback before we connect.
        seed, _ = await self.market.get_klines(stream.symbol, base, factor)
        parts: dict[int, Candle] = {c.time: c for c in seed} if factor > 1 else {}

        url = f"{self.market.ws_url}/{stream.symbol.lower()}@kline_{base}"
        async with websockets.connect(url, open_timeout=8, ping_interval=20, ping_timeout=20,
                                      max_size=2**20) as ws:
            self._set_source(stream, "binance")
            async for raw in ws:
                k = json.loads(raw).get("k")
                if not k:
                    continue
                c, closed = parse_ws_kline(k)
                if factor > 1:
                    bucket = c.time - c.time % bucket_s
                    parts = {t: p for t, p in parts.items() if t - t % bucket_s == bucket}
                    parts[c.time] = c
                    bars = [parts[t] for t in sorted(parts)]
                    c = Candle(time=bucket, open=bars[0].open, high=max(b.high for b in bars),
                               low=min(b.low for b in bars), close=bars[-1].close,
                               volume=sum(b.volume for b in bars))
                    closed = closed and len(bars) == factor
                self._publish(stream, self._kline_msg(stream, c, closed, "binance"))

    def _binance_back(self, stream: _Stream) -> bool:
        return self.market.binance_usable() and time.monotonic() >= stream.ws_retry_at

    async def _run_synthetic(self, stream: _Stream) -> None:
        """Synthetic ticks every second until Binance looks usable again (checked every SYNTHETIC_RECHECK s)."""
        candles, _ = await self.market.get_klines(stream.symbol, stream.interval, 200)
        step = INTERVAL_SECONDS[stream.interval]
        last = candles[-1]
        rng = np.random.default_rng()
        # Per-tick volatility: a bar's typical range spread over ~60 one-second ticks.
        rel = np.median([(c.high - c.low) / c.close for c in candles[-50:]]) / 8 or 1e-4
        checked = time.monotonic()
        while True:
            await asyncio.sleep(1.0)
            if time.monotonic() - checked >= SYNTHETIC_RECHECK:
                checked = time.monotonic()
                if self._binance_back(stream):
                    return
            now = int(time.time())
            bar_open = now - now % step
            price = last.close * float(np.exp(rng.normal(0, rel)))
            if bar_open > last.time:
                self._publish(stream, self._kline_msg(stream, last, True, "synthetic"))
                last = Candle(time=bar_open, open=last.close, high=max(last.close, price),
                              low=min(last.close, price), close=price, volume=0.0)
            else:
                last = Candle(time=last.time, open=last.open, high=max(last.high, price), low=min(last.low, price),
                              close=price, volume=last.volume + float(rng.lognormal(3, 1)))
            self._publish(stream, self._kline_msg(stream, last, False, "synthetic"))

    @staticmethod
    def _kline_msg(stream: _Stream, c: Candle, closed: bool, source: str) -> dict:
        return {"type": "kline", "symbol": stream.symbol, "interval": stream.interval, "source": source,
                "closed": closed, "candle": c.model_dump()}
