"""Spot pair → Binance USD-M perpetual. Most perpetuals share the spot name (BTCUSDT), but low-priced coins trade as
1000PEPEUSDT, 1000SHIBUSDT, 1000000MOGUSDT or 1MBABYDOGEUSDT: one contract is 1,000 (or 1,000,000) coins, so prices
are that many times the spot price and quantities that many times fewer. The map comes from /fapi/v1/exchangeInfo
(perpetual, trading, USDT-quoted), cached for PERP_TTL; without it every symbol maps to itself."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Awaitable, Callable

import httpx

log = logging.getLogger(__name__)

PERP_TTL = 6 * 3600.0
PERP_RETRY = 600.0
_PREFIX = re.compile(r"^(1000000|1000|1M)(?=[A-Z])")
_MULT = {"1000": 1_000.0, "1000000": 1_000_000.0, "1M": 1_000_000.0}


def build_map(info: dict) -> dict[str, tuple[str, float]]:
    """exchangeInfo → {spot symbol: (perpetual symbol, coins per contract unit)} for the prefixed perpetuals."""
    out: dict[str, tuple[str, float]] = {}
    for s in info.get("symbols", []):
        if s.get("contractType") != "PERPETUAL" or s.get("status") != "TRADING" or s.get("quoteAsset") != "USDT":
            continue
        base = str(s.get("baseAsset", ""))
        m = _PREFIX.match(base)
        if not m:
            continue
        spot = f"{base[m.end():]}USDT"
        if spot not in out or _MULT[m.group(1)] < out[spot][1]:
            out[spot] = (s["symbol"], _MULT[m.group(1)])
    return out


class PerpMap:
    def __init__(self, fetch: Callable[[], Awaitable[Any]]) -> None:
        """`fetch` returns /fapi/v1/exchangeInfo as JSON; errors other than network trouble (a region block)
        propagate to the caller."""
        self._fetch = fetch
        self._map: dict[str, tuple[str, float]] = {}
        self._expires = 0.0
        self._lock = asyncio.Lock()

    async def resolve(self, symbol: str) -> tuple[str, float]:
        """(perpetual symbol, multiplier): ('1000PEPEUSDT', 1000.0) for PEPEUSDT, (symbol, 1.0) otherwise."""
        symbol = symbol.upper()
        if time.monotonic() >= self._expires:
            async with self._lock:
                if time.monotonic() >= self._expires:
                    await self._load()
        return self._map.get(symbol, (symbol, 1.0))

    def cached(self, symbol: str) -> tuple[str, float]:
        """resolve() from what is already loaded, without a request (for sync callers)."""
        return self._map.get(symbol.upper(), (symbol.upper(), 1.0))

    async def _load(self) -> None:
        try:
            self._map = build_map(await self._fetch())
            self._expires = time.monotonic() + PERP_TTL
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            log.info("Perpetuals list unavailable (%s); using spot names", exc)
            self._expires = time.monotonic() + PERP_RETRY
