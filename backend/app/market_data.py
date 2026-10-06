"""OHLCV access: Binance REST with resampling, plus a deterministic synthetic feed.

`DATA_SOURCE=auto` tries Binance first and falls back to the synthetic feed when
Binance is unreachable (geo-blocked, offline, rate limited). Every response says
which source produced it so the UI can flag demo data.
"""

from __future__ import annotations

import hashlib
import logging
import math
import time
from dataclasses import dataclass

import httpx
import numpy as np
import pandas as pd

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

FALLBACK_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "INJUSDT", "DOGEUSDT", "ADAUSDT",
    "AVAXUSDT", "LINKUSDT", "DOTUSDT", "TONUSDT", "SUIUSDT", "APTUSDT", "ARBUSDT", "OPUSDT",
    "NEARUSDT", "TIAUSDT", "SEIUSDT", "LTCUSDT",
]

# Rough anchor prices so synthetic charts look plausible per symbol.
_SYNTH_ANCHORS = {"BTC": 65000.0, "ETH": 3200.0, "SOL": 150.0, "BNB": 580.0, "XRP": 0.6, "INJ": 7.8, "DOGE": 0.15}


class MarketDataError(RuntimeError):
    pass


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
        self._symbols: tuple[float, list[str]] | None = None
        # Switched to the fallback endpoints once the primary answers 451/403 (geo-block).
        self.rest_url = self.settings.binance_rest_url
        self.ws_url = self.settings.binance_ws_url

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

    async def get_klines(self, symbol: str, interval: str, limit: int = 500) -> tuple[list[Candle], str]:
        """Return (candles oldest→newest, source) where source is 'binance' or 'synthetic'."""
        if interval not in INTERVAL_SECONDS:
            raise MarketDataError(f"Unsupported interval {interval!r}")
        limit = max(1, min(limit, 1500))
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
            except Exception as exc:  # network, HTTP 4xx/5xx, bad payload
                if self.settings.data_source == "binance":
                    raise MarketDataError(f"Binance klines failed: {exc}") from exc
                log.warning("Binance unavailable (%s); serving synthetic data", exc)
                self.mark_binance_down()
        if candles is None:
            candles = synthetic_klines(symbol, interval, limit)

        ttl = min(5.0, INTERVAL_SECONDS[interval] / 4)
        self._cache[key] = _Cached(now + ttl, candles, source)
        if len(self._cache) > 256:
            self._cache.pop(next(iter(self._cache)))
        return candles, source

    async def list_symbols(self) -> list[str]:
        if self._symbols and self._symbols[0] > time.monotonic():
            return self._symbols[1]
        symbols = FALLBACK_SYMBOLS
        if self.binance_usable():
            try:
                resp = await self._binance_get("/api/v3/exchangeInfo", params={"permissions": "SPOT"})
                resp.raise_for_status()
                symbols = sorted(
                    s["symbol"] for s in resp.json()["symbols"]
                    if s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT"
                )
            except Exception as exc:
                log.warning("exchangeInfo failed (%s); using fallback symbol list", exc)
                self.mark_binance_down()
        self._symbols = (time.monotonic() + 3600, symbols)
        return symbols

    # ------------------------------------------------------------ binance
    async def _binance_get(self, path: str, params: dict | None = None) -> httpx.Response:
        resp = await self.client.get(f"{self.rest_url}{path}", params=params)
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
            raw = await self._binance_paginated(symbol, base, limit * factor + factor)
            return resample(raw, INTERVAL_SECONDS[interval])[-limit:]
        return await self._binance_paginated(symbol, interval, limit)

    async def _binance_paginated(self, symbol: str, interval: str, total: int) -> list[Candle]:
        out: list[Candle] = []
        end_time: int | None = None
        while len(out) < total:
            params: dict[str, str | int] = {
                "symbol": symbol, "interval": interval, "limit": min(BINANCE_MAX_LIMIT, total - len(out)),
            }
            if end_time is not None:
                params["endTime"] = end_time
            resp = await self._binance_get("/api/v3/klines", params=params)
            if resp.status_code == 400:
                raise MarketDataError(f"Binance rejected request: {resp.text[:200]}")
            resp.raise_for_status()
            rows = resp.json()
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
