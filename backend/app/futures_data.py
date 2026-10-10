"""Per-coin futures and order-flow data for the market data panel and the chat agent.

All of it comes from free, keyless Binance endpoints:

* Funding: `/fapi/v1/premiumIndex` (the rate for the next settlement, mark price) and `/fapi/v1/fundingRate`
  (settled history).
* Open interest and long/short ratios: `/futures/data/openInterestHist`, `globalLongShortAccountRatio` and
  `topLongShortPositionRatio` (Binance keeps 30 days of these).
* CVD: spot klines' taker-buy base volume (kline field 9), so buy = taker buys, sell = volume − buys.
* Order-book walls: spot `/api/v3/depth` (or futures `/fapi/v1/depth`) bucketed into ~0.1% price bands.
* Liquidation levels: an *estimate*, not an exchange heatmap. Recent 1h bars are treated as places where
  positions were opened (weighted by volume and by rising open interest), a mix of common leverages gives each
  one a liquidation price, levels price has already traded through are dropped, and what is left is bucketed.
  The actual liquidations come from the `!forceOrder@arr` stream that `derivatives.py` already runs.

Binance futures blocks US IPs (HTTP 451/403) and has no mirror, so from there every futures endpoint answers
`source: "unavailable"` with a note instead of failing; spot-based data (CVD, spot walls) keeps working through
the spot fallback hosts. When Binance cannot be reached at all, or `DATA_SOURCE=synthetic`, numeric series fall
back to deterministic demo data marked `source: "synthetic"`. Liquidation prints are never made up.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import numpy as np
from scipy.signal import find_peaks

from .config import Settings, get_settings
from .perp_map import PerpMap
from .derivatives import DerivativesService
from .market_data import DERIVED_INTERVALS, INTERVAL_SECONDS, BINANCE_MAX_LIMIT, MarketData, synthetic_klines

log = logging.getLogger(__name__)

DAY = 86400
# Periods the /futures/data endpoints accept.
FUTURES_PERIODS = ("5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d")
# Leverage mix assumed for the liquidation estimate: (leverage, share of positions).
LEVERAGES: tuple[tuple[int, float], ...] = ((10, 0.35), (25, 0.30), (50, 0.20), (100, 0.15))
# Maintenance margin rate: Binance's lowest tiers are 0.4–1%, so one middle value is close enough for an estimate.
MMR = 0.005
LIQ_LOOKBACK_HOURS = 168
LIQ_HALF_LIFE_HOURS = 48.0  # older positions are more likely to have been closed already

REGION_NOTE = ("Binance futures is not available from this server's region (Binance blocks US IPs). Run the backend "
               "from a non-US connection to see live futures data.")
DEMO_NOTE = "Binance did not answer, so this is demo data"
TTL_FUNDING, TTL_SLOW, TTL_WALLS, TTL_LIQ, TTL_CVD, TTL_RETRY = 60.0, 300.0, 10.0, 300.0, 20.0, 30.0


class FuturesBlocked(Exception):
    """Binance futures answered 451/403: geo-blocked."""


class NotListed(Exception):
    """Binance rejected the symbol (no such perpetual or spot pair)."""


# --------------------------------------------------------------------------------------------- pure helpers


def _sig(x: float | None, digits: int = 4) -> float | None:
    """Round to `digits` significant figures (compact numbers for the agent)."""
    if x is None or not math.isfinite(x) or x == 0:
        return x
    return round(x, digits - 1 - int(math.floor(math.log10(abs(x)))))


async def _gather(*aws: Awaitable[Any]) -> list[Any]:
    """Run requests together; once all have finished, raise the first failure (so none is left unawaited)."""
    results = await asyncio.gather(*aws, return_exceptions=True)
    for r in results:
        if isinstance(r, BaseException):
            raise r
    return list(results)


def _seed(*parts: object) -> int:
    return int.from_bytes(hashlib.sha256("|".join(map(str, parts)).encode()).digest()[:4], "little")


def funding_interval_hours(times: list[int]) -> float:
    """Hours between settlements, from the history (most perpetuals 8h, some 4h or 1h)."""
    gaps = [b - a for a, b in zip(times, times[1:]) if b > a]
    if not gaps:
        return 8.0
    return max(1.0, round(float(np.median(gaps)) / 3600))


def parse_funding(premium: dict, history: list[dict], now: float | None = None) -> dict:
    """premiumIndex + fundingRate rows → current rate (percent per settlement), annualised, history, 24h average.

    Binance calls the rate for the coming settlement `lastFundingRate`; it is what longs pay shorts at
    `nextFundingTime` if it stays where it is."""
    now = now or time.time()
    rows = sorted(({"time": int(h["fundingTime"]) // 1000, "rate": round(float(h["fundingRate"]) * 100, 6)}
                   for h in history), key=lambda r: r["time"])
    interval_h = funding_interval_hours([r["time"] for r in rows])
    rate = float(premium.get("lastFundingRate") or 0) * 100
    day = [r["rate"] for r in rows if r["time"] > now - DAY]
    return {
        "current": {
            "rate": round(rate, 6),
            "annualized": round(rate * 24 / interval_h * 365, 2),
            "next_funding_time": int(premium.get("nextFundingTime") or 0) // 1000 or None,
            "mark_price": float(premium.get("markPrice") or 0) or None,
            "index_price": float(premium.get("indexPrice") or 0) or None,
            "interval_hours": interval_h,
        },
        "avg_24h": round(sum(day) / len(day), 6) if day else None,
        "history": rows,
    }


def _change_24h(rows: list[dict], key: str, pct: bool = True) -> float | None:
    """Latest value of `key` vs the newest row at least 24h older (percent, or absolute when pct=False)."""
    if len(rows) < 2:
        return None
    last = rows[-1]
    ref = next((r for r in reversed(rows) if r["time"] <= last["time"] - DAY), None)
    if ref is None:
        return None
    if not pct:
        return round(last[key] - ref[key], 4)
    return round((last[key] / ref[key] - 1) * 100, 2) if ref[key] else None


def parse_open_interest(rows: list[dict]) -> dict:
    """openInterestHist rows → {rows: [{time, oi, oi_usd}], oi, oi_usd, change_24h_pct}."""
    out = sorted(({"time": int(r["timestamp"]) // 1000, "oi": float(r["sumOpenInterest"]),
                   "oi_usd": float(r["sumOpenInterestValue"])} for r in rows), key=lambda r: r["time"])
    last = out[-1] if out else None
    return {"rows": out, "oi": last["oi"] if last else None, "oi_usd": last["oi_usd"] if last else None,
            "change_24h_pct": _change_24h(out, "oi_usd")}


def parse_ratio_rows(rows: list[dict]) -> list[dict]:
    """Long/short ratio rows (account or position) → [{time, long_pct, short_pct, ratio}]."""
    return sorted(({"time": int(r["timestamp"]) // 1000, "long_pct": round(float(r["longAccount"]) * 100, 2),
                    "short_pct": round(float(r["shortAccount"]) * 100, 2), "ratio": float(r["longShortRatio"])}
                   for r in rows), key=lambda r: r["time"])


def cvd_rows(raw: list[list], bucket_seconds: int | None = None) -> list[dict]:
    """Binance kline rows → [{time, buy, sell, delta, cvd, close, delta_usd}] with taker-buy volume as buys.

    `bucket_seconds` merges rows into larger UTC-aligned bars (the 3h chart is built from 1h)."""
    bars: dict[int, list[float]] = {}
    for r in raw:
        t = int(r[0]) // 1000
        if bucket_seconds:
            t -= t % bucket_seconds
        vol, buy = float(r[5]), float(r[9])
        quote, buy_quote = float(r[7]), float(r[10])
        b = bars.setdefault(t, [0.0, 0.0, 0.0, 0.0, 0.0])
        b[0] += buy
        b[1] += vol - buy
        b[2] += buy_quote - (quote - buy_quote)
        b[3] = float(r[4])
    out, acc = [], 0.0
    for t in sorted(bars):
        buy, sell, delta_usd, close, _ = bars[t]
        acc += buy - sell
        out.append({"time": t, "buy": buy, "sell": sell, "delta": buy - sell, "cvd": acc, "close": close,
                    "delta_usd": delta_usd})
    return out


def cvd_totals(rows: list[dict]) -> dict:
    buy = sum(r["buy"] for r in rows)
    sell = sum(r["sell"] for r in rows)
    return {"buy": buy, "sell": sell, "delta": buy - sell, "delta_usd": sum(r["delta_usd"] for r in rows),
            "buy_pct": round(buy / (buy + sell) * 100, 2) if buy + sell else None}


def find_walls(bids: list[tuple[float, float]], asks: list[tuple[float, float]], range_pct: float = 5.0,
               bucket_pct: float = 0.1, factor: float = 3.0, top: int = 5) -> dict:
    """Order book → the biggest resting liquidity near price.

    Levels within ±range_pct of the mid are grouped into buckets of bucket_pct of price; a bucket is a wall when
    its notional is at least `factor` × the median bucket. Each wall reports the price of its largest level
    (where the order actually sits), its total size and its strength relative to the largest wall."""
    if not bids or not asks:
        return {"mid": None, "bids": [], "asks": [], "median_usd": None, "covered_pct": {"bids": 0.0, "asks": 0.0}}
    mid = (max(p for p, _ in bids) + min(p for p, _ in asks)) / 2
    size = mid * bucket_pct / 100
    lo, hi = mid * (1 - range_pct / 100), mid * (1 + range_pct / 100)

    def bucketize(levels: list[tuple[float, float]]) -> list[dict]:
        buckets: dict[int, dict] = {}
        for p, q in levels:
            if not lo <= p <= hi or q <= 0:
                continue
            b = buckets.setdefault(math.floor(p / size), {"usd": 0.0, "qty": 0.0, "price": p, "top": 0.0})
            b["usd"] += p * q
            b["qty"] += q
            if p * q > b["top"]:
                b["price"], b["top"] = p, p * q
        return list(buckets.values())

    bid_b, ask_b = bucketize(bids), bucketize(asks)
    every = [b["usd"] for b in bid_b + ask_b]
    median = float(np.median(every)) if every else 0.0
    picked = {side: sorted((b for b in bs if median and b["usd"] >= factor * median), key=lambda b: -b["usd"])[:top]
              for side, bs in (("bids", bid_b), ("asks", ask_b))}
    biggest = max((b["usd"] for bs in picked.values() for b in bs), default=0.0)
    out: dict[str, Any] = {"mid": mid, "median_usd": round(median, 2)}
    for side, bs in picked.items():
        out[side] = [{"price": b["price"], "qty": round(b["qty"], 8), "usd": round(b["usd"], 2),
                      "strength": round(b["usd"] / biggest, 3) if biggest else 0.0,
                      "distance_pct": round((b["price"] / mid - 1) * 100, 3)} for b in bs]
    in_bids = [p for p, _ in bids if p >= lo]
    in_asks = [p for p, _ in asks if p <= hi]
    out["covered_pct"] = {"bids": round((1 - min(in_bids) / mid) * 100, 2) if in_bids else 0.0,
                          "asks": round((max(in_asks) / mid - 1) * 100, 2) if in_asks else 0.0}
    return out


def estimate_liquidation_clusters(bars: list[tuple[int, float, float, float, float, float]], price: float,
                                  oi_usd: dict[int, float] | None = None, bucket_pct: float = 0.25,
                                  top: int = 4, half_life_hours: float = LIQ_HALF_LIFE_HOURS) -> list[dict]:
    """Where leveraged positions opened over `bars` (time, open, high, low, close, volume) would be liquidated.

    Each bar's typical price is an entry. Its weight is its USD volume, blended half and half with the open
    interest it added when `oi_usd` (time → OI in USD) is given, and halved every `half_life_hours` of age.
    Half is treated as longs and half as shorts, spread over LEVERAGES:
    long liq ≈ p × (1 − 1/lev + MMR), short liq ≈ p × (1 + 1/lev − MMR). A level that any later bar traded
    through is gone (liquidated or closed), and so is anything on the wrong side of the current price.
    The surviving weight is bucketed (bucket_pct of price); each side's strongest peaks, widened over neighbours
    holding at least 40% of the peak, are returned with weight 0..1 relative to the strongest cluster."""
    if not bars or price <= 0:
        return []
    times = np.array([b[0] for b in bars], dtype=np.int64)
    highs = np.array([b[2] for b in bars], dtype=float)
    lows = np.array([b[3] for b in bars], dtype=float)
    closes = np.array([b[4] for b in bars], dtype=float)
    typical = (highs + lows + closes) / 3
    vol_usd = np.array([b[5] for b in bars], dtype=float) * typical
    weights = vol_usd / vol_usd.sum() if vol_usd.sum() > 0 else np.full(len(bars), 1 / len(bars))
    if oi_usd:
        added = np.zeros(len(bars))
        for i, t in enumerate(times):
            prev = oi_usd.get(int(t) - 3600)
            cur = oi_usd.get(int(t))
            if prev is not None and cur is not None:
                added[i] = max(cur - prev, 0.0)
        if added.sum() > 0:
            weights = 0.5 * weights + 0.5 * added / added.sum()
    weights = weights * 0.5 ** ((times[-1] - times) / (half_life_hours * 3600))
    # Lowest low / highest high after each bar: a level inside that range has been hit since.
    after_low = np.append(np.minimum.accumulate(lows[::-1])[::-1][1:], np.inf)
    after_high = np.append(np.maximum.accumulate(highs[::-1])[::-1][1:], -np.inf)

    size = price * bucket_pct / 100
    hist: dict[str, dict[int, np.ndarray]] = {"long": {}, "short": {}}
    for i in range(len(bars)):
        p = typical[i]
        for j, (lev, share) in enumerate(LEVERAGES):
            w = weights[i] * share * 0.5
            long_liq = p * (1 - 1 / lev + MMR)
            if long_liq < min(after_low[i], price):
                hist["long"].setdefault(math.floor(long_liq / size), np.zeros(len(LEVERAGES)))[j] += w
            short_liq = p * (1 + 1 / lev - MMR)
            if short_liq > max(after_high[i], price):
                hist["short"].setdefault(math.floor(short_liq / size), np.zeros(len(LEVERAGES)))[j] += w

    clusters: list[dict] = []
    for side, buckets in hist.items():
        if not buckets:
            continue
        first, last = min(buckets), max(buckets)
        dense = np.zeros((last - first + 1, len(LEVERAGES)))
        for k, v in buckets.items():
            dense[k - first] = v
        total = dense.sum(axis=1)
        padded = np.concatenate([[0.0], total, [0.0]])  # so a peak at either end still counts
        peaks, _ = find_peaks(padded)
        taken: list[tuple[int, int]] = []

        def free(k: int) -> bool:
            return not any(lo <= k <= hi for lo, hi in taken)

        for pk in sorted(peaks - 1, key=lambda k: -total[k]):
            if len(taken) == top:
                break
            if not free(pk):
                continue  # inside a stronger neighbour's range
            a = b = pk
            while a > 0 and total[a - 1] >= 0.4 * total[pk] and free(a - 1):
                a -= 1
            while b < len(total) - 1 and total[b + 1] >= 0.4 * total[pk] and free(b + 1):
                b += 1
            taken.append((a, b))
            by_lev = dense[a:b + 1].sum(axis=0)
            clusters.append({"side": side, "price_low": (first + a) * size, "price_high": (first + b + 1) * size,
                             "raw": float(total[a:b + 1].sum()),
                             "leverage_hint": f"{LEVERAGES[int(np.argmax(by_lev))][0]}x"})
    strongest = max((c["raw"] for c in clusters), default=0.0)
    out = []
    for c in sorted(clusters, key=lambda c: -c["price_high"]):
        near = c["price_low"] if c["side"] == "short" else c["price_high"]
        out.append({"price_low": c["price_low"], "price_high": c["price_high"], "side": c["side"],
                    "weight": round(c["raw"] / strongest, 3) if strongest else 0.0,
                    "leverage_hint": c["leverage_hint"], "distance_pct": round((near / price - 1) * 100, 2)})
    return out


# --------------------------------------------------------------------------------------------- synthetic data


def _synthetic_series(symbol: str, kind: str, n: int, step: int, base: float, vol: float,
                      lo: float | None = None, hi: float | None = None) -> list[tuple[int, float]]:
    """Deterministic mean-reverting series for demo mode, aligned to `step`. Always simulates the same length and
    slices it, so requests for different lengths agree on every value (like synthetic_klines)."""
    end = int(time.time()) // step * step
    total = max(n, 1000)
    rng = np.random.default_rng(_seed(symbol, kind, step))
    x, out = 0.0, []
    for i in range(total):
        x = x * 0.93 + rng.normal(0, vol)
        v = base * (1 + x) if lo is None else min(max(base + x, lo), hi if hi is not None else math.inf)
        out.append((end - (total - 1 - i) * step, v))
    return out[-n:]


def synthetic_funding(symbol: str, limit: int) -> dict:
    pts = _synthetic_series(symbol, "funding", limit + 1, 8 * 3600, 0.008, 0.006, -0.05, 0.08)
    price = synthetic_klines(symbol, "1m", 1)[-1].close
    history = [{"fundingTime": t * 1000, "fundingRate": v / 100} for t, v in pts[:-1]]
    premium = {"lastFundingRate": pts[-1][1] / 100, "nextFundingTime": (pts[-1][0] + 8 * 3600) * 1000,
               "markPrice": price, "indexPrice": price}
    return parse_funding(premium, history)


def synthetic_open_interest(symbol: str, period: str, limit: int) -> dict:
    price = synthetic_klines(symbol, "1m", 1)[-1].close
    base_usd = 2e9 if symbol.startswith("BTC") else 8e8 if symbol.startswith("ETH") else 6e7
    pts = _synthetic_series(symbol, "oi", limit, INTERVAL_SECONDS[period], base_usd, 0.01)
    return parse_open_interest([{"timestamp": t * 1000, "sumOpenInterest": v / price, "sumOpenInterestValue": v}
                                for t, v in pts])


def synthetic_ratio(symbol: str, kind: str, period: str, limit: int) -> list[dict]:
    pts = _synthetic_series(symbol, kind, limit, INTERVAL_SECONDS[period], 0.0, 0.02, -0.25, 0.25)
    rows = []
    for t, x in pts:
        long_ = 0.55 + x if kind == "accounts" else 0.5 + x * 0.6
        rows.append({"timestamp": t * 1000, "longAccount": long_, "shortAccount": 1 - long_,
                     "longShortRatio": long_ / (1 - long_)})
    return parse_ratio_rows(rows)


def synthetic_cvd_raw(symbol: str, interval: str, limit: int) -> list[list]:
    """Kline-shaped rows from the synthetic candles, with a taker-buy share that leans with each bar's body."""
    candles = synthetic_klines(symbol, interval, max(limit, 1500))  # fixed length: every limit agrees
    noise = np.random.default_rng(_seed(symbol, interval, "cvd")).normal(0, 0.04, len(candles))
    rows = []
    for c, eps in zip(candles[-limit:], noise[-limit:]):
        body = (c.close - c.open) / (c.high - c.low) if c.high > c.low else 0.0
        share = float(np.clip(0.5 + 0.35 * body + eps, 0.05, 0.95))
        rows.append([c.time * 1000, c.open, c.high, c.low, c.close, c.volume, 0, c.volume * c.close, 0,
                     c.volume * share, c.volume * share * c.close, 0])
    return rows


def synthetic_book(symbol: str, range_pct: float) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """A plausible book around the synthetic price, with a few planted walls that move every minute."""
    price = synthetic_klines(symbol, "1m", 1)[-1].close
    rng = np.random.default_rng(_seed(symbol, "book", int(time.time()) // 60))
    tick = price * 0.0002
    n = int(price * range_pct / 100 / tick)
    base_qty = 25_000 / price
    bids = [(price - tick * (i + 1), float(rng.lognormal(0, 0.6)) * base_qty) for i in range(n)]
    asks = [(price + tick * (i + 1), float(rng.lognormal(0, 0.6)) * base_qty) for i in range(n)]
    for book in (bids, asks):
        for _ in range(4):
            k = int(rng.integers(5, n))
            book[k] = (book[k][0], book[k][1] * float(rng.uniform(25, 90)))
    return bids, asks


# --------------------------------------------------------------------------------------------- service


class FuturesDataService:
    """Funding, open interest, long/short ratios, CVD, order-book walls and estimated liquidation levels.

    Every public method returns a dict with `source`: "binance", "synthetic" (demo data) or "unavailable"
    (with a `note` saying why), and never raises for network trouble."""

    def __init__(self, market: MarketData, derivatives: DerivativesService | None = None,
                 settings: Settings | None = None) -> None:
        self.market = market
        self.derivatives = derivatives
        self.s = settings or get_settings()
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0),
                                         headers={"User-Agent": "agentic-charts/1.0"})
        self._cache: dict[tuple, tuple[float, dict]] = {}
        self._blocked_until = 0.0
        self.perps = PerpMap(lambda: self._fapi("/fapi/v1/exchangeInfo", {}))  # a 451 here blocks like any other

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def futures_live(self) -> bool:
        """Real futures data is wanted (DERIVATIVES on, not the synthetic feed)."""
        return self.s.derivatives_enabled and self.s.data_source != "synthetic"

    # ------------------------------------------------------------------ plumbing
    async def _fapi(self, path: str, params: dict) -> Any:
        if self._blocked_until > time.monotonic():
            raise FuturesBlocked()
        r = await self._client.get(f"{self.s.binance_futures_rest_url}{path}", params=params)
        if r.status_code in (403, 451):
            log.warning("Binance futures %s returned %s (region block); futures data unavailable for 10 min",
                        path, r.status_code)
            self._blocked_until = time.monotonic() + 600
            raise FuturesBlocked()
        if r.status_code == 400:
            raise NotListed(r.text[:200])
        r.raise_for_status()
        return r.json()

    async def _cached(self, key: tuple, ttl: float, live: Callable[[], Awaitable[dict]],
                      demo: Callable[[], dict], use_live: bool, symbol: str, what: str) -> dict:
        """Serve `key` from cache, else `live()` when `use_live`, else `demo()`; map failures to a source+note."""
        hit = self._cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        if not use_live:
            out = {**demo(), "source": "synthetic", "note": "Demo data (synthetic feed)"}
        else:
            try:
                out = {**await live(), "source": "binance"}
            except FuturesBlocked:
                out, ttl = {"source": "unavailable", "blocked": True, "note": REGION_NOTE}, TTL_RETRY * 4
            except NotListed:
                out = {"source": "unavailable", "note": f"Binance has no {what} for {symbol}"}
            except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
                log.info("%s for %s unavailable: %s", what, symbol, exc)
                if self.s.data_source == "binance":
                    out = {"source": "unavailable", "note": f"Binance {what} did not answer; retrying shortly"}
                else:
                    out = {**demo(), "source": "synthetic", "note": DEMO_NOTE}
                ttl = TTL_RETRY
        out = {"symbol": symbol, **out}
        self._cache[key] = (time.monotonic() + ttl, out)
        if len(self._cache) > 512:
            self._cache.pop(next(iter(self._cache)))
        return out

    # ------------------------------------------------------------------ funding / OI / long-short
    async def funding(self, symbol: str, limit: int = 100) -> dict:
        """{symbol, source, current: {rate, annualized, next_funding_time, mark_price, index_price, interval_hours},
        avg_24h, history: [{time, rate}]}. Rates are percent per settlement (0.01 = 0.01%)."""
        limit = max(1, min(limit, 1000))

        async def live() -> dict:
            perp, mult = await self.perps.resolve(symbol)
            prem, hist = await _gather(self._fapi("/fapi/v1/premiumIndex", {"symbol": perp}),
                                       self._fapi("/fapi/v1/fundingRate", {"symbol": perp, "limit": limit}))
            out = parse_funding(prem, hist)
            for k in ("mark_price", "index_price"):  # per contract unit (1000 coins) → per coin
                if out["current"][k]:
                    out["current"][k] = out["current"][k] / mult
            return {**out, **({"perpetual": perp} if perp != symbol else {})}

        return await self._cached(("funding", symbol, limit), TTL_FUNDING, live,
                                  lambda: synthetic_funding(symbol, limit), self.futures_live, symbol, "perpetual")

    async def open_interest(self, symbol: str, period: str = "1h", limit: int = 200) -> dict:
        """{symbol, source, period, rows: [{time, oi, oi_usd}], oi, oi_usd, change_24h_pct}."""
        period = period if period in FUTURES_PERIODS else "1h"
        limit = max(2, min(limit, 500))

        async def live() -> dict:
            perp, mult = await self.perps.resolve(symbol)
            out = parse_open_interest(await self._fapi("/futures/data/openInterestHist",
                                                       {"symbol": perp, "period": period, "limit": limit}))
            if out["change_24h_pct"] is None and out["rows"]:  # the window is shorter than a day
                day = parse_open_interest(await self._fapi("/futures/data/openInterestHist",
                                                           {"symbol": perp, "period": "1h", "limit": 25}))
                out["change_24h_pct"] = day["change_24h_pct"]
            if mult != 1:  # contracts of 1000 coins → coins
                out["rows"] = [{**r, "oi": r["oi"] * mult} for r in out["rows"]]
                out["oi"] = out["oi"] * mult if out["oi"] is not None else None
            return {"period": period, **out, **({"perpetual": perp} if perp != symbol else {})}

        return await self._cached(("oi", symbol, period, limit), TTL_SLOW, live,
                                  lambda: {"period": period, **synthetic_open_interest(symbol, period, limit)},
                                  self.futures_live, symbol, "open interest")

    async def long_short(self, symbol: str, period: str = "1h", limit: int = 200) -> dict:
        """{symbol, source, period, accounts: [{time, long_pct, short_pct, ratio}], top_positions: [...],
        ratio, long_pct, change_24h}. `accounts` is every account's long/short split; `top_positions` the
        position split of the top 20% of traders by margin. change_24h is the account ratio's absolute change."""
        period = period if period in FUTURES_PERIODS else "1h"
        limit = max(2, min(limit, 500))

        def shape(accounts: list[dict], top: list[dict]) -> dict:
            last = accounts[-1] if accounts else None
            return {"period": period, "accounts": accounts, "top_positions": top,
                    "ratio": last["ratio"] if last else None, "long_pct": last["long_pct"] if last else None,
                    "top_ratio": top[-1]["ratio"] if top else None,
                    "change_24h": _change_24h(accounts, "ratio", pct=False)}

        async def live() -> dict:
            params = {"symbol": (await self.perps.resolve(symbol))[0], "period": period, "limit": limit}
            acc, top = await _gather(self._fapi("/futures/data/globalLongShortAccountRatio", params),
                                     self._fapi("/futures/data/topLongShortPositionRatio", params))
            return shape(parse_ratio_rows(acc), parse_ratio_rows(top))

        return await self._cached(
            ("ls", symbol, period, limit), TTL_SLOW, live,
            lambda: shape(synthetic_ratio(symbol, "accounts", period, limit),
                          synthetic_ratio(symbol, "top", period, limit)),
            self.futures_live, symbol, "long/short data")

    # ------------------------------------------------------------------ CVD (spot)
    async def _spot_kline_rows(self, symbol: str, interval: str, total: int) -> list[list]:
        """The newest `total` raw spot kline rows (all 12 fields), paging back 1000 at a time."""
        out: list[list] = []
        end_time: int | None = None
        while len(out) < total:
            params: dict[str, str | int] = {"symbol": symbol, "interval": interval,
                                            "limit": min(BINANCE_MAX_LIMIT, total - len(out))}
            if end_time is not None:
                params["endTime"] = end_time
            rows = await self.market._kline_rows(params)
            if not rows:
                break
            out = rows + out
            end_time = int(rows[0][0]) - 1
            if len(rows) < int(params["limit"]):
                break
        return out[-total:]

    async def cvd(self, symbol: str, interval: str = "1h", limit: int = 500) -> dict:
        """{symbol, interval, source, rows: [{time, buy, sell, delta, cvd}], totals: {buy, sell, delta, delta_usd,
        buy_pct}, last_24h: {...}}. Spot taker volume in the coin's units; CVD starts at 0 on the first bar."""
        if interval not in INTERVAL_SECONDS:
            raise ValueError(f"Unsupported interval {interval!r}")
        limit = max(2, min(limit, 1500))
        derived = DERIVED_INTERVALS.get(interval)
        step = INTERVAL_SECONDS[interval]

        def shape(raw: list[list]) -> dict:
            rows = cvd_rows(raw, step if derived else None)[-limit:]
            day = [r for r in rows if rows and r["time"] > rows[-1]["time"] - DAY]
            return {"interval": interval, "totals": cvd_totals(rows), "last_24h": cvd_totals(day),
                    "rows": [{k: r[k] for k in ("time", "buy", "sell", "delta", "cvd")} for r in rows]}

        async def live() -> dict:
            try:
                if derived:
                    base, factor = derived
                    return shape(await self._spot_kline_rows(symbol, base, limit * factor + factor))
                return shape(await self._spot_kline_rows(symbol, interval, limit))
            except Exception as exc:
                if "Invalid symbol" in str(exc):
                    raise NotListed(str(exc)) from exc
                self.market.mark_binance_down()
                raise httpx.HTTPError(str(exc)) from exc

        return await self._cached(("cvd", symbol, interval, limit), TTL_CVD, live,
                                  lambda: shape(synthetic_cvd_raw(symbol, interval, limit)),
                                  self.market.binance_usable(), symbol, "spot pair")

    # ------------------------------------------------------------------ order-book walls
    async def walls(self, symbol: str, range_pct: float = 5.0, market: str = "spot") -> dict:
        """{symbol, source, market, mid, bids: [{price, qty, usd, strength, distance_pct}], asks: [...],
        median_usd, covered_pct: {bids, asks}, range_pct}. covered_pct says how far the 1,000-level book reaches."""
        range_pct = max(0.5, min(range_pct, 20.0))
        market = "futures" if market == "futures" else "spot"

        def levels(book: dict) -> tuple[list, list]:
            return ([(float(p), float(q)) for p, q in book.get("bids", [])],
                    [(float(p), float(q)) for p, q in book.get("asks", [])])

        async def live() -> dict:
            if market == "futures":
                perp, mult = await self.perps.resolve(symbol)
                book = await self._fapi("/fapi/v1/depth", {"symbol": perp, "limit": 1000})
                if mult != 1:  # per 1000 coins → per coin
                    book = {side: [(float(p) / mult, float(q) * mult) for p, q in book.get(side, [])]
                            for side in ("bids", "asks")}
            else:
                resp = await self.market._binance_get("/api/v3/depth", {"symbol": symbol, "limit": 1000})
                if resp.status_code == 400:
                    raise NotListed(resp.text[:200])
                resp.raise_for_status()
                book = resp.json()
            return {"market": market, "range_pct": range_pct, **find_walls(*levels(book), range_pct=range_pct)}

        use_live = self.futures_live if market == "futures" else self.market.binance_usable()
        return await self._cached(("walls", symbol, range_pct, market), TTL_WALLS, live,
                                  lambda: {"market": market, "range_pct": range_pct,
                                           **find_walls(*synthetic_book(symbol, range_pct), range_pct=range_pct)},
                                  use_live, symbol, "order book" if market == "spot" else "perpetual")

    # ------------------------------------------------------------------ liquidation levels (estimated)
    async def liquidation_levels(self, symbol: str) -> dict:
        """{symbol, source, price, clusters: [{price_low, price_high, side, weight, leverage_hint, distance_pct}],
        oi_weighted, recent: [{time, price, qty, usd, side}], recent_source, note}. Clusters are estimates."""
        key = ("liq", symbol)
        hit = self._cache.get(key)
        if hit and hit[0] > time.monotonic():
            return {**hit[1], **self._recent(symbol)}  # the actual liquidations are always fresh
        try:
            candles, source = await self.market.get_klines(symbol, "1h", LIQ_LOOKBACK_HOURS)
        except Exception as exc:
            log.info("Liquidation estimate for %s: no candles (%s)", symbol, exc)
            return {"symbol": symbol, "source": "unavailable", "clusters": [], "price": None, "oi_weighted": False,
                    "note": f"No price history for {symbol}", **self._recent(symbol)}
        oi_map: dict[int, float] | None = None
        if source == "binance" and self.futures_live:
            oi = await self.open_interest(symbol, "1h", LIQ_LOOKBACK_HOURS + 1)
            if oi["source"] == "binance":
                oi_map = {r["time"]: r["oi_usd"] for r in oi["rows"]}
        bars = [(c.time, c.open, c.high, c.low, c.close, c.volume) for c in candles]
        price = candles[-1].close if candles else 0.0
        clusters = await asyncio.to_thread(estimate_liquidation_clusters, bars, price, oi_map)
        note = (f"Estimated from the last {len(bars)} hourly bars"
                + (" and open-interest changes" if oi_map else "")
                + ", assuming 10–100x leverage. Not exchange data: real positions and leverage are not public.")
        out = {"symbol": symbol, "source": source, "price": price, "clusters": clusters,
               "oi_weighted": bool(oi_map), "lookback_hours": len(bars), "note": note}
        self._cache[key] = (time.monotonic() + (TTL_LIQ if source == "binance" else TTL_RETRY), out)
        return {**out, **self._recent(symbol)}

    def _recent(self, symbol: str) -> dict:
        rows = self.derivatives.recent_liquidations(symbol, 30) if self.derivatives else None
        if rows is None:
            return {"recent": [], "recent_source": "unavailable",
                    "recent_note": REGION_NOTE if self._blocked_until > time.monotonic()
                    else "Live liquidations need the Binance futures stream, which is not connected"}
        return {"recent": rows, "recent_source": "binance",
                "recent_note": "Binance futures liquidations in the last 24h (Binance sends at most one per "
                               "symbol per second)"}

    # ------------------------------------------------------------------ summary for the chat agent
    async def futures_context(self, symbol: str) -> dict:
        """Compact read of one coin's futures and order flow for the agent: funding now and its 24h average,
        open interest and its 24h change, the long/short account ratio and its 24h change, spot CVD over the last
        24h, the nearest bid/ask wall, and the nearest estimated liquidation cluster each side. Small numbers only."""
        funding, oi, ls, cvd, walls, liq = await asyncio.gather(
            self.funding(symbol, 30), self.open_interest(symbol, "1h", 30), self.long_short(symbol, "1h", 30),
            self.cvd(symbol, "1h", 24), self.walls(symbol, 5.0), self.liquidation_levels(symbol))
        out: dict[str, Any] = {"symbol": symbol}
        notes: list[str] = []
        sources = {k: v["source"] for k, v in (("funding", funding), ("open_interest", oi), ("long_short", ls),
                                                ("cvd", cvd), ("walls", walls), ("liquidation_levels", liq))}
        if any(s == "synthetic" for s in sources.values()):
            notes.append("Some values are demo data (synthetic), not live market data")
        for name, part in (("funding", funding), ("open interest", oi), ("long/short", ls)):
            if part["source"] == "unavailable":
                notes.append(f"{name}: {part.get('note', 'unavailable')}")
        if funding["source"] != "unavailable":
            cur = funding["current"]
            nxt = cur.get("next_funding_time")
            out["funding"] = {"rate_pct": _sig(cur["rate"], 3), "avg_24h_pct": _sig(funding.get("avg_24h"), 3),
                              "annualized_pct": _sig(cur["annualized"], 3), "interval_hours": cur["interval_hours"],
                              "next_in_minutes": max(0, int((nxt - time.time()) // 60)) if nxt else None}
        if oi["source"] != "unavailable":
            out["open_interest"] = {"usd": _sig(oi.get("oi_usd"), 3), "change_24h_pct": oi.get("change_24h_pct")}
        if ls["source"] != "unavailable":
            out["long_short"] = {"ratio": _sig(ls.get("ratio"), 3), "long_pct": ls.get("long_pct"),
                                 "change_24h": ls.get("change_24h"), "top_traders_ratio": _sig(ls.get("top_ratio"), 3)}
        if cvd["source"] != "unavailable":
            day = cvd["last_24h"]
            direction = ("buyers" if (day["buy_pct"] or 50) > 51 else "sellers" if (day["buy_pct"] or 50) < 49
                         else "balanced")
            out["cvd_24h"] = {"delta": _sig(day["delta"], 3), "delta_usd": _sig(day["delta_usd"], 3),
                              "buy_pct": day["buy_pct"], "direction": direction}
        if walls["source"] != "unavailable" and walls.get("mid"):
            def nearest(rows: list[dict]) -> dict | None:
                w = min(rows, key=lambda r: abs(r["distance_pct"]), default=None)
                return w and {"price": _sig(w["price"], 6), "usd": _sig(w["usd"], 3),
                              "distance_pct": w["distance_pct"]}
            out["walls"] = {"bid": nearest(walls["bids"]), "ask": nearest(walls["asks"])}
        if liq["source"] != "unavailable":
            def closest(side: str) -> dict | None:
                c = min((c for c in liq["clusters"] if c["side"] == side), key=lambda c: abs(c["distance_pct"]),
                        default=None)
                return c and {"price_low": _sig(c["price_low"], 6), "price_high": _sig(c["price_high"], 6),
                              "weight": c["weight"], "distance_pct": c["distance_pct"]}
            out["est_liquidations"] = {"above": closest("short"), "below": closest("long"),
                                       "note": "estimated, not exchange data"}
        out["sources"] = sources
        if notes:
            out["notes"] = notes
        return out
