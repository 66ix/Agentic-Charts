"""Order-book heatmap: resting liquidity over time, drawn behind the candles Bookmap-style.

While someone has the heatmap on for a symbol, its spot order book (`/api/v3/depth`, HEATMAP_DEPTH_LIMIT levels)
is sampled every HEATMAP_INTERVAL_SECONDS. Each snapshot is bucketed into fixed price bins (the bin size is picked
from the book's reach on the first snapshot and kept, so the history lines up) as resting notional per bin, within
±HEATMAP_RANGE_PCT of the mid, and kept in a rolling in-memory history of HEATMAP_HISTORY_MINUTES per symbol.

The chart polls GET /api/orderbook/heatmap. Every poll counts as "someone is viewing": a symbol nobody has polled
for IDLE_SECONDS stops being sampled, its history is dropped KEEP_SECONDS later, and at most MAX_SYMBOLS are
tracked at once (the one viewed longest ago goes first). There is no history from before the first poll: Binance
only serves the book as it is now.

When Binance cannot be reached (or `DATA_SOURCE=synthetic`) the samples come from `DemoBook`, a plausible synthetic
book around the chart's price (the stream hub's last price), with a 45-minute backfill so there is something to
see at once, marked `source: "synthetic"`. With `DATA_SOURCE=binance` a failure answers `source: "unavailable"`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import NamedTuple

import httpx
import numpy as np

from .config import Settings, get_settings
from .futures_data import NotListed, _seed
from .market_data import MarketData, synthetic_klines

log = logging.getLogger(__name__)

IDLE_SECONDS = 45.0  # stop sampling a symbol nobody has polled for this long (the chart polls every cadence)
KEEP_SECONDS = 15 * 60.0  # drop a stopped symbol's history this long after its last poll
MAX_SYMBOLS = 8
TARGET_BINS = 120  # bins across the book's reach when a symbol's bin size is picked
MAX_COLUMNS = 2000
DEMO_BACKFILL_SECONDS = 45 * 60
WALL_FACTOR = 4.0  # a bin is a wall at this many times the median bin


class Sample(NamedTuple):
    time: int
    mid: float
    first: int  # index of the first bin; bin i covers [i × bin_size, (i + 1) × bin_size)
    values: np.ndarray  # resting notional (quote currency) per bin from `first`


# --------------------------------------------------------------------------------------------- pure helpers


def nice_step(x: float) -> float:
    """The smallest 1, 2, 2.5 or 5 × 10^k that is at least x."""
    if not x > 0 or not math.isfinite(x):
        return 1.0
    e = 10.0 ** math.floor(math.log10(x))
    for m in (1, 2, 2.5, 5, 10):
        if m * e >= x * (1 - 1e-9):
            return float(f"{m * e:.12g}")
    return 10 * e


def pick_bin_size(bids: list[tuple[float, float]], asks: list[tuple[float, float]], mid: float,
                  range_pct: float) -> float:
    """Bin size for a symbol's whole history: the book's reach (capped at ±range_pct) over TARGET_BINS, rounded to
    a nice number and never finer than 0.01% of the price."""
    lo, hi = mid * (1 - range_pct / 100), mid * (1 + range_pct / 100)
    if bids:
        lo = max(lo, min(p for p, _ in bids))
    if asks:
        hi = min(hi, max(p for p, _ in asks))
    span = max(hi - lo, mid * 0.002)
    return nice_step(max(span / TARGET_BINS, mid * 0.0001))


def bucket_book(bids: list[tuple[float, float]], asks: list[tuple[float, float]], bin_size: float, mid: float,
                range_pct: float) -> tuple[int, np.ndarray]:
    """Resting notional (price × quantity) per price bin within ±range_pct of `mid`: (first bin, values)."""
    lo, hi = mid * (1 - range_pct / 100), mid * (1 + range_pct / 100)
    levels = [(p, q) for p, q in (*bids, *asks) if lo <= p <= hi and q > 0]
    if not levels:
        return 0, np.zeros(0, dtype=np.float32)
    prices = np.array([p for p, _ in levels])
    usd = prices * np.array([q for _, q in levels])
    idx = np.floor(prices / bin_size + 1e-9).astype(np.int64)
    first = int(idx.min())
    return first, np.bincount(idx - first, weights=usd).astype(np.float32)


def heatmap_columns(samples: list[Sample], step: int, since: int | None = None) -> list[list]:
    """Samples averaged into columns `step` seconds wide: [[time, mid, first_bin, [notional per bin, ...]], ...].
    A bin a sample did not reach does not pull the average down. `since` keeps the column holding it and later."""
    start = since - since % step if since is not None else None
    groups: dict[int, list[Sample]] = {}
    for s in samples:
        t = s.time - s.time % step
        if start is None or t >= start:
            groups.setdefault(t, []).append(s)
    out = []
    for t in sorted(groups):
        g = [s for s in groups[t] if len(s.values)]
        if not g:
            continue
        lo = min(s.first for s in g)
        hi = max(s.first + len(s.values) for s in g)
        acc, cnt = np.zeros(hi - lo), np.zeros(hi - lo)
        for s in g:
            a = s.first - lo
            acc[a:a + len(s.values)] += s.values
            cnt[a:a + len(s.values)] += 1
        vals = acc / np.maximum(cnt, 1)
        nz = np.flatnonzero(vals >= 0.5)
        if not len(nz):
            continue
        a, b = int(nz[0]), int(nz[-1]) + 1
        mid = sum(s.mid for s in g) / len(g)
        out.append([t, float(f"{mid:.10g}"), lo + a, [int(round(v)) for v in vals[a:b]]])
    return out


def walls_from_sample(s: Sample, bin_size: float, factor: float = WALL_FACTOR, top: int = 3) -> list[dict]:
    """The biggest bins of a snapshot on each side of the mid, when at least `factor` × the median bin:
    [{side: "bid"|"ask", price (bin centre), usd}]."""
    vals = s.values
    nz = vals[vals > 0]
    if not len(nz):
        return []
    med = float(np.median(nz))
    centers = (s.first + np.arange(len(vals)) + 0.5) * bin_size
    out = []
    for side, mask in (("bid", centers < s.mid), ("ask", centers > s.mid)):
        idx = np.flatnonzero(mask & (vals >= factor * med))
        for i in sorted(idx, key=lambda i: -vals[i])[:top]:
            out.append({"side": side, "price": float(f"{centers[i]:.10g}"), "usd": int(round(float(vals[i])))})
    return out


# --------------------------------------------------------------------------------------------- demo book


class DemoBook:
    """Plausible synthetic depth around a moving mid: thin near the touch and thicker further out, noise per price
    level that drifts over a couple of minutes, and walls that appear, sit for a few minutes to half an hour and
    are then pulled, or eaten when price trades through them."""

    def __init__(self, symbol: str, mid: float, range_pct: float) -> None:
        self.rng = np.random.default_rng(_seed(symbol, "heatmap", int(time.time())))
        self.tick = mid * 0.0002
        self.levels = max(20, int(range_pct / 100 / 0.0002))  # per side
        self.usd = 30_000.0 if symbol.startswith(("BTC", "ETH")) else 4_000.0  # typical notional per level
        self.range_pct = range_pct
        self.noise: dict[int, float] = {}
        self.walls: list[dict] = []
        self.last_t: float | None = None

    def book(self, mid: float, t: float) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        rng = self.rng
        dt = 10.0 if self.last_t is None else max(0.0, t - self.last_t)
        self.last_t = t
        self.walls = [w for w in self.walls
                      if w["until"] > t and (w["price"] < mid if w["side"] == "bid" else w["price"] > mid)]
        if len(self.walls) < 10 and rng.random() < 1 - math.exp(-dt / 40):  # about one new wall every 40 s
            side = "bid" if rng.random() < 0.5 else "ask"
            dist = rng.uniform(0.08, self.range_pct * 0.8) / 100
            self.walls.append({"side": side, "price": mid * (1 - dist if side == "bid" else 1 + dist),
                               "usd": self.usd * rng.uniform(20, 120), "until": t + rng.uniform(90, 1800)})
        k0 = round(mid / self.tick)
        decay = math.exp(-dt / 120)
        bids: list[tuple[float, float]] = []
        asks: list[tuple[float, float]] = []
        shocks = rng.normal(size=(self.levels, 2))
        for i in range(1, self.levels + 1):
            for j, (key, out) in enumerate(((k0 - i, bids), (k0 + i, asks))):
                z = self.noise.get(key)
                z = shocks[i - 1, j] if z is None else z * decay + shocks[i - 1, j] * math.sqrt(1 - decay * decay)
                self.noise[key] = z
                p = key * self.tick
                out.append((p, self.usd * (0.3 + 1.7 * i / self.levels) * math.exp(0.6 * z) / p))
        for w in self.walls:
            side = bids if w["side"] == "bid" else asks
            j = abs(round(w["price"] / self.tick) - k0) - 1
            if 0 <= j < len(side):
                p, q = side[j]
                side[j] = (p, q + w["usd"] / p)
        if len(self.noise) > 8 * self.levels:
            self.noise = {k: v for k, v in self.noise.items() if abs(k - k0) <= 2 * self.levels}
        return bids, asks


# --------------------------------------------------------------------------------------------- service


@dataclass
class _Track:
    symbol: str
    samples: deque
    bin_size: float | None = None
    source: str = "connecting"  # of the latest attempt: binance | synthetic | unavailable
    data_source: str | None = None  # of the samples held (a switch between live and demo starts over)
    note: str | None = None
    last_view: float = 0.0
    task: asyncio.Task | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    demo: DemoBook | None = None


class OrderbookHeatmapService:
    def __init__(self, market: MarketData, hub=None, settings: Settings | None = None,
                 idle_seconds: float = IDLE_SECONDS) -> None:
        self.market = market
        self.hub = hub  # StreamHub: its last price centres the demo book on what the chart shows
        self.s = settings or get_settings()
        self.cadence = max(0.05, self.s.heatmap_interval_seconds)
        self.history = max(60, self.s.heatmap_history_minutes * 60)
        self.range_pct = max(0.2, min(self.s.heatmap_range_pct, 20.0))
        self.idle = idle_seconds
        self._tracks: dict[str, _Track] = {}

    async def get(self, symbol: str, step: int | None = None, since: int | None = None) -> dict:
        """{symbol, source, note, bin_size, cadence, step, started_at, collecting, price, columns: [[time, mid,
        first_bin, [notional per bin]]], walls: [{side, price, usd}]}. Starts sampling `symbol` if it isn't yet;
        the first call waits (up to 10 s) for the first snapshot."""
        self._prune()
        tr = self._tracks.get(symbol)
        if tr is None:
            if len(self._tracks) >= MAX_SYMBOLS:
                oldest = min(self._tracks.values(), key=lambda t: t.last_view)
                self._drop(oldest.symbol)
            tr = _Track(symbol, deque(maxlen=int(self.history / self.cadence) + 2))
            self._tracks[symbol] = tr
        tr.last_view = time.monotonic()
        if tr.task is None or tr.task.done():
            tr.task = asyncio.create_task(self._run(tr), name=f"heatmap:{symbol}")
        if not tr.ready.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(tr.ready.wait(), 10)
        step = int(max(self.cadence, step or 0, 1))
        cutoff = time.time() - self.history
        samples = [s for s in tr.samples if s.time >= cutoff]
        latest = samples[-1] if samples else None
        return {
            "symbol": symbol, "source": "unavailable" if tr.source == "connecting" else tr.source, "note": tr.note,
            "bin_size": tr.bin_size, "cadence": self.cadence, "step": step,
            "started_at": samples[0].time if samples else None, "collecting": not tr.task.done(),
            "price": latest.mid if latest else None,
            "columns": heatmap_columns(samples, step, since)[-MAX_COLUMNS:],
            "walls": walls_from_sample(latest, tr.bin_size) if latest and tr.bin_size else [],
        }

    def stats(self) -> list[dict]:
        return [{"symbol": t.symbol, "collecting": bool(t.task and not t.task.done()), "samples": len(t.samples),
                 "source": t.source} for t in self._tracks.values()]

    async def close(self) -> None:
        tasks = [t.task for t in self._tracks.values() if t.task]
        self._tracks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # ----------------------------------------------------------------- internals
    def _drop(self, symbol: str) -> None:
        tr = self._tracks.pop(symbol, None)
        if tr and tr.task:
            tr.task.cancel()

    def _prune(self) -> None:
        now = time.monotonic()
        for sym in [s for s, t in self._tracks.items()
                    if (t.task is None or t.task.done()) and now - t.last_view > KEEP_SECONDS]:
            self._drop(sym)

    async def _run(self, tr: _Track) -> None:
        """Samples every cadence until nobody has polled for `idle` seconds."""
        try:
            while time.monotonic() - tr.last_view <= self.idle:
                started = time.monotonic()
                try:
                    await self._sample(tr)
                except Exception as exc:  # never let one bad snapshot end the collector
                    log.warning("Heatmap sample for %s failed: %s", tr.symbol, exc)
                tr.ready.set()
                await asyncio.sleep(max(0.0, self.cadence - (time.monotonic() - started)))
        except asyncio.CancelledError:
            pass

    async def _fetch(self, symbol: str) -> tuple[list, list]:
        resp = await self.market._binance_get("/api/v3/depth", {"symbol": symbol,
                                                                 "limit": self.s.heatmap_depth_limit})
        if resp.status_code == 400:
            raise NotListed(resp.text[:200])
        resp.raise_for_status()
        book = resp.json()
        return ([(float(p), float(q)) for p, q in book["bids"]], [(float(p), float(q)) for p, q in book["asks"]])

    async def _sample(self, tr: _Track) -> None:
        now = time.time()
        live: tuple[list, list] | None = None
        if self.market.binance_usable():
            try:
                live = await self._fetch(tr.symbol)
            except NotListed:
                tr.source, tr.note = "unavailable", f"Binance has no spot order book for {tr.symbol}"
                return
            except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as exc:
                log.info("Heatmap depth for %s unavailable: %s", tr.symbol, exc)
                if self.s.data_source == "binance":
                    tr.source, tr.note = "unavailable", "Binance order book did not answer; retrying"
                    return
            note = None if live else "Binance did not answer, so this is a demo order book"
        else:
            note = ("Demo order book (synthetic feed)" if self.s.data_source == "synthetic"
                    else "Binance did not answer, so this is a demo order book")
        source = "binance" if live else "synthetic"
        if tr.data_source and tr.data_source != source:  # live and demo samples don't mix
            tr.samples.clear()
            tr.bin_size, tr.demo = None, None
        if live:
            bids, asks = live
            if not bids or not asks:
                tr.source, tr.note = "unavailable", "Binance returned an empty order book"
                return
            mid = (max(p for p, _ in bids) + min(p for p, _ in asks)) / 2
        else:
            mid = await self._demo_mid(tr.symbol)
            if tr.demo is None:
                tr.demo = DemoBook(tr.symbol, mid, self.range_pct)
                if not tr.samples:
                    self._backfill(tr, mid, now)
            bids, asks = tr.demo.book(mid, now)
        if tr.bin_size is None:
            tr.bin_size = pick_bin_size(bids, asks, mid, self.range_pct)
        first, values = bucket_book(bids, asks, tr.bin_size, mid, self.range_pct)
        tr.samples.append(Sample(int(now), mid, first, values))
        tr.source, tr.data_source, tr.note = source, source, note

    async def _demo_mid(self, symbol: str) -> float:
        price = self.hub.last_price(symbol) if self.hub is not None and hasattr(self.hub, "last_price") else None
        if price:
            return float(price)
        candles, _ = await self.market.get_klines(symbol, "1m", 1)
        return candles[-1].close

    def _backfill(self, tr: _Track, mid: float, now: float) -> None:
        """Demo history for the last DEMO_BACKFILL_SECONDS, following the synthetic 1m closes (scaled to end at
        `mid`), so the demo heatmap shows walls coming and going at once."""
        span = min(DEMO_BACKFILL_SECONDS, self.history)
        closes = [c.close for c in synthetic_klines(tr.symbol, "1m", int(span // 60) + 2)]
        scale = mid / closes[-1]
        start = now - span
        bin_size = None
        t = start
        while t < now - self.cadence / 2:
            k = (t - start) / 60
            i = min(int(k), len(closes) - 2)
            m = (closes[i] + (closes[i + 1] - closes[i]) * (k - i)) * scale
            bids, asks = tr.demo.book(m, t)  # type: ignore[union-attr]
            bin_size = bin_size or pick_bin_size(bids, asks, m, self.range_pct)
            first, values = bucket_book(bids, asks, bin_size, m, self.range_pct)
            tr.samples.append(Sample(int(t), m, first, values))
            t += self.cadence
        tr.bin_size = bin_size
