"""OHLCV access: Binance REST with resampling, plus a deterministic synthetic feed.

`DATA_SOURCE=auto` tries Binance first and falls back to the synthetic feed when
Binance is unreachable (geo-blocked, offline, rate limited). Every response says
which source produced it so the UI can flag demo data.

Closed Binance bars of native intervals are also kept in SQLite (`CANDLE_CACHE`), so
after a restart only the bars opened since the last cached one are downloaded.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import random
import time
from dataclasses import dataclass

import httpx
import numpy as np
import pandas as pd

from .candle_store import CandleStore
from .config import get_settings
from .schemas import Candle

log = logging.getLogger(__name__)

INTERVAL_SECONDS: dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "3h": 10800,
    "4h": 14400,
    "6h": 21600,
    "8h": 28800,
    "12h": 43200,
    "1d": 86400,
    "1w": 604800,
    "1M": 2592000,  # nominal 30d, only used for synthetic data / extrapolation
}

# Intervals Binance does not serve natively: (base interval, bars per bucket).
DERIVED_INTERVALS: dict[str, tuple[str, int]] = {"3h": ("1h", 3)}

BINANCE_MAX_LIMIT = 1000
# Largest history one call returns: the REST endpoint allows 1500, Kimi Cooked asks for up to 5000.
MAX_KLINES = 5000
# Longest get_range: a bit over 13 months of 1m bars.
MAX_RANGE_BARS = 570_000

FALLBACK_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "INJUSDT", "DOGEUSDT", "ADAUSDT",
    "AVAXUSDT", "LINKUSDT", "DOTUSDT", "TONUSDT", "SUIUSDT", "APTUSDT", "ARBUSDT", "OPUSDT",
    "NEARUSDT", "TIAUSDT", "SEIUSDT", "LTCUSDT",
]

RETRY_JITTER = (0.2, 0.8)  # seconds before retrying a request that hit a network error or a 5xx
DEMOTE_AFTER = 3           # failed requests in a row before Binance is treated as down (sooner if ping fails too)
SYMBOLS_TTL = 3600.0
SYMBOLS_RETRY = 60.0       # a failed symbol-list lookup is tried again this soon

# Rough anchor prices so synthetic charts look plausible per symbol.
_SYNTH_ANCHORS = {"BTC": 65000.0, "ETH": 3200.0, "SOL": 150.0, "BNB": 580.0, "XRP": 0.6, "INJ": 7.8, "DOGE": 0.15}


class MarketDataError(RuntimeError):
    pass


class BinanceRejected(MarketDataError):
    """Binance answered 400 (e.g. an unknown or delisted symbol): Binance is up, only this request is bad."""


class BinanceRateLimited(MarketDataError):
    """Binance answered 429/418: it is up but wants us to wait. Not a reason to serve demo candles."""


@dataclass
class _Cached:
    expires: float
    candles: list[Candle]
    source: str


class MarketData:
    """Async OHLCV provider with a short TTL cache and Binance/synthetic failover."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._client: httpx.AsyncClient | None = None
        self._cache: dict[tuple[str, str, int], _Cached] = {}
        self._binance_down_until = 0.0
        self._paused_until = 0.0  # Retry-After of the last 429/418
        self._fails = 0           # failed Binance requests in a row
        self._symbols: tuple[float, list[str]] | None = None
        self._good_symbols: list[str] | None = None
        # Switched to the fallback endpoints once the primary answers 451/403 (geo-block).
        self.rest_url = self.settings.binance_rest_url
        self.ws_url = self.settings.binance_ws_url
        self._store = self._open_store()

    def _open_store(self) -> CandleStore | None:
        if not self.settings.candle_cache or self.settings.data_source == "synthetic":
            return None
        try:
            return CandleStore(self.settings.candle_cache_path)
        except Exception as exc:  # unwritable directory, corrupt file: just fetch everything
            log.warning("Candle cache %s unusable (%s); fetching full history from Binance",
                        self.settings.candle_cache_path, exc)
            return None

    # ------------------------------------------------------------ lifecycle
    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._store is not None:
            self._store.close()
            self._store = None

    # -------------------------------------------------------------- public
    def binance_usable(self) -> bool:
        mode = self.settings.data_source
        if mode == "synthetic":
            return False
        if mode == "binance":
            return True
        return time.monotonic() >= self._binance_down_until

    def mark_binance_down(self, seconds: float = 60.0) -> None:
        if self.settings.data_source == "auto":
            self._binance_down_until = time.monotonic() + seconds
        self._fails = 0

    async def _failed(self, exc: Exception) -> None:
        """One Binance request failed. Rejections and rate limits say Binance is up; anything else demotes it
        when /ping fails too, or after DEMOTE_AFTER failures in a row: one blip must not send every chart to demo
        data for a minute."""
        if isinstance(exc, (BinanceRejected, BinanceRateLimited)):
            return
        self._fails += 1
        if self._fails >= DEMOTE_AFTER:
            self.mark_binance_down()
            return
        try:
            resp = await self.client.get(f"{self.rest_url}/api/v3/ping", timeout=3.0)
            up = resp.status_code == 200
        except Exception:
            up = False
        if not up:
            self.mark_binance_down()

    async def get_klines(self, symbol: str, interval: str, limit: int = 500) -> tuple[list[Candle], str]:
        """Return (candles oldest→newest, source) where source is 'binance' or 'synthetic'."""
        if interval not in INTERVAL_SECONDS:
            raise MarketDataError(f"Unsupported interval {interval!r}")
        limit = max(1, min(limit, MAX_KLINES))
        key = (symbol, interval, limit)
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached and cached.expires > now:
            return cached.candles, cached.source

        candles: list[Candle] | None = None
        source = "synthetic"
        if self.binance_usable():
            try:
                candles = await self._binance_klines(symbol, interval, limit)
                source = "binance"
                self._fails = 0
            except BinanceRateLimited:
                raise
            except Exception as exc:  # network, HTTP 4xx/5xx, bad payload
                if self.settings.data_source == "binance":
                    raise MarketDataError(f"Binance klines failed: {exc}") from exc
                log.warning("Binance request failed (%s); serving synthetic data", exc)
                await self._failed(exc)
        if candles is None:
            candles = synthetic_klines(symbol, interval, limit)

        ttl = min(5.0, INTERVAL_SECONDS[interval] / 4)
        self._cache[key] = _Cached(now + ttl, candles, source)
        if len(self._cache) > 256:
            self._cache.pop(next(iter(self._cache)))
        return candles, source

    async def get_range(self, symbol: str, interval: str, start: int, end: int | None = None) -> tuple[pd.DataFrame, str]:
        """Every bar of a native interval from `start` to `end` (UNIX s; None = now, including the open bar)
        → (DataFrame with time/open/high/low/close/volume, source). For grid bots, the journal and backtests,
        which need long stretches of 1m bars: closed bars are cached in SQLite, so only new ones are fetched,
        and no Candle objects are built (a month of 1m is 43,200 bars)."""
        if interval not in INTERVAL_SECONDS or interval in DERIVED_INTERVALS:
            raise MarketDataError(f"Unsupported range interval {interval!r}")
        step = INTERVAL_SECONDS[interval]
        now = int(time.time())
        end = now if end is None else min(end, now)
        start = start - start % step
        if end < start:
            raise MarketDataError("Range ends before it starts")
        bars = (end - start) // step + 1
        if bars > MAX_RANGE_BARS:
            raise MarketDataError(f"Range too long: {bars:,} {interval} bars (at most {MAX_RANGE_BARS:,})")
        if self.binance_usable():
            try:
                df = _rows_df(await self._binance_range(symbol, interval, start, end))
                self._fails = 0
                return df, "binance"
            except BinanceRateLimited:
                raise
            except Exception as exc:
                if self.settings.data_source == "binance":
                    raise MarketDataError(f"Binance klines failed: {exc}") from exc
                log.warning("Binance request failed for a range (%s); serving synthetic data", exc)
                await self._failed(exc)
        candles = synthetic_klines(symbol, interval, (now - start) // step + 1, end=now)
        df = candles_to_df(candles)
        return df[(df["time"] >= start) & (df["time"] <= end)].reset_index(drop=True), "synthetic"

    async def _binance_range(self, symbol: str, interval: str, start: int, end: int) -> list[tuple]:
        step = INTERVAL_SECONDS[interval]
        cached: list[tuple] = []
        if self._store is not None:
            try:
                cached = await asyncio.to_thread(self._store.load_range, symbol, interval, start, end)
            except Exception as exc:
                log.warning("Range cache read failed (%s); fetching from Binance", exc)
        # Spans the cache does not cover: before its first bar, holes inside it, and after its last bar.
        spans: list[tuple[int, int]] = []
        cursor = start
        for row in cached:
            if row[0] > cursor:
                spans.append((cursor, row[0] - step))
            cursor = row[0] + step
        if cursor <= end:
            spans.append((cursor, end))
        fetched: list[tuple] = []
        for lo, hi in spans:
            fetched += await self._binance_span(symbol, interval, lo, hi)
        if fetched and self._store is not None:
            closed = [r for r in fetched if r[0] + step <= time.time()]
            try:
                await asyncio.to_thread(self._store.save_range, symbol, interval, closed)
            except Exception as exc:
                log.warning("Range cache write failed: %s", exc)
        rows = {r[0]: r for r in cached}
        rows.update({r[0]: r for r in fetched})
        return [rows[t] for t in sorted(rows) if start <= t <= end]

    async def _binance_span(self, symbol: str, interval: str, lo: int, hi: int) -> list[tuple]:
        """Bars opening in [lo, hi] as tuples, paging forward 1000 at a time."""
        out: list[tuple] = []
        start_ms, end_ms = lo * 1000, hi * 1000
        while start_ms <= end_ms:
            rows = await self._kline_rows({"symbol": symbol, "interval": interval, "startTime": start_ms,
                                           "endTime": end_ms, "limit": BINANCE_MAX_LIMIT})
            if not rows:
                break
            out += [(int(r[0]) // 1000, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
                    for r in rows]
            start_ms = int(rows[-1][0]) + 1
            if len(rows) < BINANCE_MAX_LIMIT:
                break
        return out

    async def list_symbols(self) -> list[str]:
        """Every USDT spot pair trading on Binance, cached for an hour. When the lookup fails: the last good list
        (or the built-in one), looked up again in a minute."""
        if self._symbols and self._symbols[0] > time.monotonic():
            return self._symbols[1]
        if self.binance_usable():
            try:
                resp = await self._binance_get("/api/v3/exchangeInfo", params={"permissions": "SPOT"})
                resp.raise_for_status()
                symbols = sorted(
                    s["symbol"] for s in resp.json()["symbols"]
                    if s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT"
                )
                self._good_symbols = symbols
                self._symbols = (time.monotonic() + SYMBOLS_TTL, symbols)
                return symbols
            except Exception as exc:
                log.warning("exchangeInfo failed (%s); using the %s symbol list", exc,
                            "last good" if self._good_symbols else "built-in")
                await self._failed(exc)
        symbols = self._good_symbols or FALLBACK_SYMBOLS
        self._symbols = (time.monotonic() + SYMBOLS_RETRY, symbols)
        return symbols

    # ------------------------------------------------------------ binance
    async def _binance_get(self, path: str, params: dict | None = None) -> httpx.Response:
        """GET from Binance: one retry after a network error or a 5xx; a 429/418 pauses every request for its
        Retry-After and raises BinanceRateLimited."""
        wait = self._paused_until - time.monotonic()
        if wait > 0:
            raise BinanceRateLimited(f"Binance rate limit: paused for another {math.ceil(wait)}s")
        try:
            resp = await self.client.get(f"{self.rest_url}{path}", params=params)
            retry = resp.status_code >= 500
        except httpx.TransportError:
            retry = True
        if retry:
            await asyncio.sleep(random.uniform(*RETRY_JITTER))
            resp = await self.client.get(f"{self.rest_url}{path}", params=params)
        if resp.status_code in (418, 429):
            try:
                pause = float(resp.headers.get("Retry-After", 60))
            except ValueError:
                pause = 60.0
            self._paused_until = time.monotonic() + max(1.0, pause)
            log.warning("Binance rate limit (%s); pausing requests for %.0fs", resp.status_code, pause)
            raise BinanceRateLimited(f"Binance rate limit: paused for {pause:.0f}s")
        fallback = self.settings.binance_fallback_rest_url
        if resp.status_code in (403, 451) and fallback and self.rest_url != fallback:
            log.warning("Binance %s returned %s (region block); switching to %s", self.rest_url,
                        resp.status_code, fallback)
            self.rest_url, self.ws_url = fallback, self.settings.binance_fallback_ws_url
            resp = await self.client.get(f"{self.rest_url}{path}", params=params)
        return resp

    async def _binance_klines(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        if interval in DERIVED_INTERVALS:
            base, factor = DERIVED_INTERVALS[interval]
            raw = await self._binance_cached(symbol, base, limit * factor + factor)
            return resample(raw, INTERVAL_SECONDS[interval])[-limit:]
        return await self._binance_cached(symbol, interval, limit)

    async def _binance_cached(self, symbol: str, interval: str, total: int) -> list[Candle]:
        """The newest `total` bars of a native interval: cached closed bars plus only what Binance added since."""
        if self._store is None:
            return await self._binance_paginated(symbol, interval, total)
        step = INTERVAL_SECONDS[interval]
        cached = contiguous_tail(await self._store_load(symbol, interval, total), step)
        if cached:
            last = cached[-1].time
            missing = max(0, int(time.time()) - last) // step  # bars opened after the last cached one
            if missing <= total and len(cached) + missing >= total:
                fresh = await self._binance_forward(symbol, interval, last, missing + 1)
                if fresh:
                    merged = contiguous_tail([c for c in cached if c.time < fresh[0].time] + fresh, step)
                    if len(merged) >= total:
                        # Not the newest bar (it may still be open), nor the closed one we already had.
                        await self._store_save(symbol, interval, [c for c in fresh[:-1] if c.time > last])
                        return merged[-total:]
        candles = await self._binance_paginated(symbol, interval, total)
        await self._store_save(symbol, interval, candles[:-1])
        return candles

    async def _store_load(self, symbol: str, interval: str, total: int) -> list[Candle]:
        store = self._store
        if store is None:
            return []
        try:
            return await asyncio.to_thread(store.load, symbol, interval, total)
        except Exception as exc:  # a broken cache must never break a request
            log.warning("Candle cache read failed (%s); fetching from Binance", exc)
            return []

    async def _store_save(self, symbol: str, interval: str, candles: list[Candle]) -> None:
        store = self._store
        if store is None:
            return
        try:
            await asyncio.to_thread(store.save, symbol, interval, candles)
        except Exception as exc:
            log.warning("Candle cache write failed: %s", exc)

    async def _binance_forward(self, symbol: str, interval: str, start: int, expected: int) -> list[Candle] | None:
        """Bars opening at or after `start` (UNIX s) up to the newest, oldest→newest. `expected` is the
        estimated count; None when the bars did not fit in the pages that estimate allows."""
        out: list[Candle] = []
        start_ms = start * 1000
        for _ in range(expected // BINANCE_MAX_LIMIT + 2):
            # One more than expected, so a short page tells us we have reached the newest bar.
            limit = min(BINANCE_MAX_LIMIT, max(expected - len(out), 0) + 1)
            rows = await self._kline_rows({"symbol": symbol, "interval": interval, "startTime": start_ms,
                                           "limit": limit})
            out += [parse_binance_kline(r) for r in rows]
            if len(rows) < limit:
                return out
            start_ms = rows[-1][0] + 1
        return None

    async def _kline_rows(self, params: dict[str, str | int]) -> list[list]:
        resp = await self._binance_get("/api/v3/klines", params=params)
        if resp.status_code == 400:
            raise BinanceRejected(f"Binance rejected request: {resp.text[:200]}")
        resp.raise_for_status()
        return resp.json()

    async def _binance_paginated(self, symbol: str, interval: str, total: int) -> list[Candle]:
        out: list[Candle] = []
        end_time: int | None = None
        while len(out) < total:
            params: dict[str, str | int] = {
                "symbol": symbol, "interval": interval, "limit": min(BINANCE_MAX_LIMIT, total - len(out)),
            }
            if end_time is not None:
                params["endTime"] = end_time
            rows = await self._kline_rows(params)
            if not rows:
                break
            batch = [parse_binance_kline(r) for r in rows]
            out = batch + out
            end_time = rows[0][0] - 1
            if len(rows) < params["limit"]:
                break
        return out[-total:]


def parse_binance_kline(row: list) -> Candle:
    """Binance REST kline row → Candle."""
    return Candle(
        time=int(row[0]) // 1000,
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
        volume=float(row[5]),
    )


def contiguous_tail(candles: list[Candle], step: int) -> list[Candle]:
    """The newest run of `candles` with no bar missing (1.5 steps of slack: months vary in length).

    Keeps a cache with a hole (e.g. a long absence followed by a short request) from being served
    as history. A gap in Binance's own data only costs full fetches until it leaves the window."""
    for i in range(len(candles) - 1, 0, -1):
        if candles[i].time - candles[i - 1].time > step * 1.5:
            return candles[i:]
    return candles


def resample(candles: list[Candle], bucket_seconds: int) -> list[Candle]:
    """Aggregate candles into UTC-aligned buckets of `bucket_seconds`."""
    if not candles:
        return []
    df = candles_to_df(candles)
    df["bucket"] = (df["time"] // bucket_seconds) * bucket_seconds
    agg = df.groupby("bucket", sort=True).agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), volume=("volume", "sum"),
    )
    return [
        Candle(time=int(t), open=r.open, high=r.high, low=r.low, close=r.close, volume=r.volume)
        for t, r in agg.iterrows()
    ]


def _rows_df(rows: list[tuple]) -> pd.DataFrame:
    """(time, open, high, low, close, volume) tuples → the same frame as candles_to_df."""
    arr = np.asarray(rows, dtype=float).reshape(-1, 6)
    return pd.DataFrame({"time": arr[:, 0].astype(np.int64), "open": arr[:, 1], "high": arr[:, 2],
                         "low": arr[:, 3], "close": arr[:, 4], "volume": arr[:, 5]})


def candles_to_df(candles: list[Candle]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "time": np.fromiter((c.time for c in candles), dtype=np.int64, count=len(candles)),
            "open": np.fromiter((c.open for c in candles), dtype=float, count=len(candles)),
            "high": np.fromiter((c.high for c in candles), dtype=float, count=len(candles)),
            "low": np.fromiter((c.low for c in candles), dtype=float, count=len(candles)),
            "close": np.fromiter((c.close for c in candles), dtype=float, count=len(candles)),
            "volume": np.fromiter((c.volume for c in candles), dtype=float, count=len(candles)),
        }
    )


# --------------------------------------------------------------- synthetic


def _seed(*parts: str) -> int:
    return int.from_bytes(hashlib.sha256("|".join(parts).encode()).digest()[:4], "little")


def synthetic_anchor(symbol: str) -> float:
    base = symbol.upper().removesuffix("USDT").removesuffix("USDC").removesuffix("BUSD")
    return _SYNTH_ANCHORS.get(base, 1.0 + (_seed(base) % 9000) / 100.0)


def synthetic_klines(symbol: str, interval: str, limit: int, end: int | None = None) -> list[Candle]:
    """Deterministic, realistic-looking OHLCV: regime-switching random walk with
    mean reversion, so it produces genuine swings and ranges for the detector."""
    step = INTERVAL_SECONDS[interval]
    end = end if end is not None else int(time.time())
    last_open = end - end % step
    rng = np.random.default_rng(_seed(symbol, interval))
    anchor = synthetic_anchor(symbol)
    vol = 0.004 * math.sqrt(step / 3600)  # per-bar volatility scales with sqrt(time)

    # Always simulate the same fixed-length history and slice it, so requests
    # with different limits (REST vs stream seed) agree on every price.
    n = max(limit, 1500)
    regime = np.repeat(rng.choice([-1.0, 0.0, 0.0, 1.0], size=n // 40 + 1), 40)[:n]
    rets = rng.normal(0.0, vol, n) + regime * vol * 0.25
    log_p = np.empty(n)
    acc = 0.0
    for i in range(n):
        acc = acc * 0.985 + rets[i]  # pull back toward the anchor
        log_p[i] = acc
    # Every interval ends at the anchor price, so synthetic timeframes roughly agree.
    closes = anchor * np.exp(log_p - log_p[-1])
    opens = np.concatenate([[closes[0] * (1 - vol)], closes[:-1]])
    wick = np.abs(rng.normal(0, vol * 0.6, (2, n)))
    highs = np.maximum(opens, closes) * (1 + wick[0])
    lows = np.minimum(opens, closes) * (1 - wick[1])
    volume = rng.lognormal(10, 0.5, n) * (1 + np.abs(rets) / vol)

    times = last_open - step * np.arange(n - 1, -1, -1)
    sl = slice(n - limit, n)
    opens, highs, lows, closes, volume, times = (a[sl] for a in (opens, highs, lows, closes, volume, times))
    return [
        Candle(time=int(t), open=round(float(o), 6), high=round(float(h), 6), low=round(float(l), 6),
               close=round(float(c), 6), volume=round(float(v), 2))
        for t, o, h, l, c, v in zip(times, opens, highs, lows, closes, volume)
    ]
