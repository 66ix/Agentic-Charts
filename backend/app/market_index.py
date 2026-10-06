"""TOTAL / TOTAL2 / TOTAL3-style market-cap indexes, built from Binance candles.

Each bar is Σ price × circulating supply over the top ~20 coins that have a Binance USDT pair (stablecoins,
wrapped and gold tokens left out): TOTAL is all of them, TOTAL2 leaves out BTC, TOTAL3 leaves out BTC and ETH.
Circulating supply comes from CoinGecko's free markets endpoint (cached for 6 hours) with a static table as the
fallback, and today's supply is applied to every bar, so this is an approximation of the TradingView indexes, not
a copy: it covers fewer coins and no stablecoins, so its level is lower and its moves slightly larger.

Bars are aligned by time. A coin missing any bar in the window is left out of the whole series, so the index never
jumps when a coin starts or stops contributing. Highs and lows use the market-cap-weighted average wick of the
coins (Σ of the coins' highs would overstate them: they do not all peak in the same minute). Volume is in USD.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx
import numpy as np

from .config import get_settings
from .market_data import INTERVAL_SECONDS, MarketData

log = logging.getLogger(__name__)

COINGECKO_MARKETS = "https://api.coingecko.com/api/v3/coins/markets"
INDEXES: dict[str, set[str]] = {"TOTAL": set(), "TOTAL2": {"BTC"}, "TOTAL3": {"BTC", "ETH"}}
TOP_N = 20
SUPPLY_TTL, SUPPLY_RETRY = 6 * 3600.0, 600.0
# Not "crypto market cap" in the TradingView sense, or not a coin of its own.
EXCLUDE = {
    "USDT", "USDC", "DAI", "FDUSD", "USDE", "TUSD", "PYUSD", "USDS", "USD1", "BUSD", "USDD", "RLUSD", "USDTB", "USD0",
    "GHO", "FRAX", "LUSD", "EURC", "EURI", "AEUR", "XUSD", "USDF", "BFUSD", "USDG", "SUSDE", "SUSDS",
    "XAUT", "PAXG",
    "WBTC", "WETH", "STETH", "WSTETH", "WEETH", "CBBTC", "WBETH", "RETH", "METH", "BETH", "JITOSOL", "MSOL",
    "BNSOL", "LBTC", "SOLVBTC", "CLBTC", "BUIDL", "BSC-USD", "WBT", "BGB", "LEO",
}
# Approximate circulating supply, used only when CoinGecko cannot be reached (largest coins first).
STATIC_SUPPLY: dict[str, float] = {
    "BTC": 19.94e6, "ETH": 120.7e6, "XRP": 60e9, "BNB": 138e6, "SOL": 550e6, "DOGE": 151e9, "TRX": 95e9,
    "ADA": 36.2e9, "LINK": 690e6, "AVAX": 425e6, "XLM": 31.5e9, "SUI": 3.6e9, "HBAR": 42.5e9, "BCH": 19.9e6,
    "LTC": 76.5e6, "TON": 2.5e9, "SHIB": 589e12, "DOT": 1.6e9, "UNI": 630e6, "PEPE": 420.69e12, "NEAR": 1.25e9,
    "APT": 680e6, "ICP": 540e6, "ETC": 153e6, "AAVE": 15.2e6, "INJ": 97e6, "ARB": 5e9, "OP": 1.7e9,
}


def build_index(series: dict[str, list[tuple[int, float, float, float, float, float]]], supply: dict[str, float],
                limit: int) -> tuple[list[dict], list[str]]:
    """Coin → bars (time, open, high, low, close, volume) and coin → supply → (index candles, coins used).

    The time grid is the newest `limit` times; newest times that fewer than 80% of coins have yet (a bar that
    just opened) are dropped. Coins without every grid time are left out."""
    coins = [c for c in series if series[c] and supply.get(c, 0) > 0]
    if not coins:
        return [], []
    counts: dict[int, int] = {}
    for c in coins:
        for b in series[c]:
            counts[b[0]] = counts.get(b[0], 0) + 1
    grid = sorted(counts)
    while grid and counts[grid[-1]] < 0.8 * len(coins):
        grid.pop()
    grid = grid[-limit:]
    if not grid:
        return [], []
    by_time = {c: {b[0]: b for b in series[c]} for c in coins}
    used = [c for c in coins if all(t in by_time[c] for t in grid)]
    if not used:
        return [], []
    arr = np.array([[by_time[c][t][1:] for t in grid] for c in used], dtype=float)  # coin × time × OHLCV
    s = np.array([supply[c] for c in used], dtype=float)[:, None]
    o, h, lo, cl, v = (arr[:, :, k] for k in range(5))
    caps = cl * s
    w = caps / caps.sum(axis=0)
    top, bottom = np.maximum(o, cl), np.minimum(o, cl)
    up = (w * np.where(top > 0, h / np.where(top > 0, top, 1) - 1, 0)).sum(axis=0)
    down = (w * np.where(bottom > 0, 1 - lo / np.where(bottom > 0, bottom, 1), 0)).sum(axis=0)
    io, ic = (o * s).sum(axis=0), caps.sum(axis=0)
    ih, il = np.maximum(io, ic) * (1 + up), np.minimum(io, ic) * (1 - down)
    iv = (v * cl).sum(axis=0)
    candles = [{"time": int(t), "open": round(float(a), 2), "high": round(float(b), 2), "low": round(float(c), 2),
                "close": round(float(d), 2), "volume": round(float(e), 2)}
               for t, a, b, c, d, e in zip(grid, io, ih, il, ic, iv)]
    return candles, used


class MarketIndexService:
    """Builds TOTAL, TOTAL2 and TOTAL3 candles on request; supply is cached for 6 hours, results for 15 seconds."""

    def __init__(self, market: MarketData) -> None:
        self.market = market
        self.s = get_settings()
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0),
                                         headers={"Accept": "application/json", "User-Agent": "agentic-charts/1.0"})
        self._supply: tuple[float, list[tuple[str, float]], str] | None = None  # (expires, ranked coins, source)
        self._cache: dict[tuple, tuple[float, dict]] = {}
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        await self._client.aclose()

    async def _ranked_supply(self) -> tuple[list[tuple[str, float]], str]:
        """[(ticker, circulating supply)] largest market cap first, and "coingecko" or "static"."""
        async with self._lock:
            if self._supply and self._supply[0] > time.monotonic():
                return self._supply[1], self._supply[2]
            ranked: list[tuple[str, float]] = []
            if self.s.data_source != "synthetic":
                try:
                    r = await self._client.get(COINGECKO_MARKETS, params={
                        "vs_currency": "usd", "order": "market_cap_desc", "per_page": 50, "page": 1})
                    r.raise_for_status()
                    for row in r.json():
                        tk = str(row.get("symbol") or "").upper()
                        supply = float(row.get("circulating_supply") or 0)
                        if tk and supply > 0 and tk not in EXCLUDE and tk not in dict(ranked):
                            ranked.append((tk, supply))
                except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
                    log.warning("CoinGecko markets unavailable (%s); using the static supply table", exc)
            if ranked:
                self._supply = (time.monotonic() + SUPPLY_TTL, ranked, "coingecko")
            else:
                self._supply = (time.monotonic() + SUPPLY_RETRY, list(STATIC_SUPPLY.items()), "static")
            return self._supply[1], self._supply[2]

    async def klines(self, name: str, interval: str, limit: int = 500) -> dict:
        """{symbol, interval, source, candles: [{time, open, high, low, close, volume}], note, coins,
        supply_source} — shaped like /api/klines. `name` is TOTAL, TOTAL2 or TOTAL3."""
        name = name.upper()
        if name not in INDEXES:
            raise ValueError("name must be TOTAL, TOTAL2 or TOTAL3")
        if interval not in INTERVAL_SECONDS:
            raise ValueError(f"Unsupported interval {interval!r}")
        limit = max(10, min(limit, 1500))
        key = (name, interval, limit)
        hit = self._cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]

        ranked, supply_source = await self._ranked_supply()
        listed = set(await self.market.list_symbols())
        universe = [(tk, s) for tk, s in ranked if f"{tk}USDT" in listed][:TOP_N]
        members = [(tk, s) for tk, s in universe if tk not in INDEXES[name]]
        sem = asyncio.Semaphore(6)

        async def load(tk: str) -> tuple[str, list[tuple], str] | None:
            async with sem:
                try:
                    candles, source = await self.market.get_klines(f"{tk}USDT", interval, limit + 2)
                except Exception as exc:
                    log.info("Index %s: %sUSDT skipped (%s)", name, tk, exc)
                    return None
            return tk, [(c.time, c.open, c.high, c.low, c.close, c.volume) for c in candles], source

        loaded = [x for x in await asyncio.gather(*(load(tk) for tk, _ in members)) if x]
        # Never mix live and demo prices in one series: keep whichever source most coins came from.
        live = [x for x in loaded if x[2] == "binance"]
        demo = [x for x in loaded if x[2] != "binance"]
        chosen, source = (live, "binance") if live and len(live) >= len(demo) else (demo, "synthetic")
        candles, used = build_index({tk: bars for tk, bars, _ in chosen}, dict(members), limit)
        note = (f"Approximation: Σ price × circulating supply of the top {len(universe)} coins with a Binance USDT "
                f"pair{' except ' + ' and '.join(sorted(INDEXES[name])) if INDEXES[name] else ''}, "
                f"{len(used)} of them with full history here. Stablecoins are not included, so the level is below "
                "TradingView's " + name + ".")
        if supply_source == "static":
            note += " Supply figures are a built-in table (CoinGecko was unreachable)."
        if source != "binance":
            note += " Demo data: Binance is unreachable."
        out = {"symbol": name, "interval": interval, "source": source if candles else "unavailable",
               "candles": candles, "note": note, "coins": used, "supply_source": supply_source}
        ttl = min(15.0, INTERVAL_SECONDS[interval] / 4) if candles and source == "binance" else 5.0
        self._cache[key] = (time.monotonic() + ttl, out)
        if len(self._cache) > 64:
            self._cache.pop(next(iter(self._cache)))
        return out
