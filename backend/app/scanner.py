"""Watchlist scans and tickers: the same detectors run across many coins, ranked by what was asked
("near demand", "oversold", "breaking out")."""

from __future__ import annotations

import asyncio
import json
import logging
import time

import pandas as pd

from .market_data import INTERVAL_SECONDS, MarketData, candles_to_df
from .schemas import AnalysisIntent, ScanResult
from .ta_agent import TF_LABEL, AnalysisResult, _fmt, analyze

log = logging.getLogger(__name__)

DEFAULT_WATCHLIST = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "INJUSDT", "DOGEUSDT", "LINKUSDT"]
SCAN_INTENT = AnalysisIntent(features=["support_resistance", "supply_demand", "liquidity_sweeps"], max_zones=1)
SUPPORT_KINDS = ("support", "demand")
RESIST_KINDS = ("resistance", "supply")


def change_24h(df: pd.DataFrame) -> float | None:
    """% change of the last close against the price 24 hours before it: the close of the last candle that closed by
    then (on 1d that is yesterday's close, not its open), or the first open when the candles start later."""
    if df.empty:
        return None
    last = df.iloc[-1]
    times = df["time"].to_numpy()
    step = int(times[1] - times[0]) if len(times) > 1 else 86400
    cutoff = int(last["time"]) + step - 86400  # 24 h before the last candle's close
    before = df[df["time"] + step <= cutoff]
    base = float(before["close"].iloc[-1]) if len(before) else float(df["open"].iloc[0])
    return round((float(last["close"]) / base - 1) * 100, 2) if base > 0 else None


def summarize(res: AnalysisResult, df: pd.DataFrame, symbol: str, interval: str, source: str) -> ScanResult:
    """One coin's scan row: nearest zone either side, and the signals worth knowing."""
    f = res.facts
    tfl = TF_LABEL.get(interval, interval)
    last = res.stats.last_price
    signals: list[str] = []
    zones = [(k, z) for k in ("support", "demand", "resistance", "supply") for z in f.get(k, [])]
    nearest = min(zones, key=lambda kz: kz[1]["distance_atr"], default=None)
    for k, z in zones:
        if z["inside"]:
            signals.append(f"Inside {tfl} {k} {_fmt(z['low'])}–{_fmt(z['high'])}")
        elif z["distance_atr"] <= 1.0:
            signals.append(f"{z['distance_atr']} ATR from {tfl} {k}")
        if z.get("htf_confluence") and (z["inside"] or z["distance_atr"] <= 1.5):
            signals.append(f"{tfl} {k} lines up with {'/'.join(z['htf_confluence'])}")
    br = f.get("last_structure_break")
    if br and br["bars_ago"] <= 10:
        signals.append(f"{br['direction'].title()} {br['type']} through {_fmt(br['level'])} {br['bars_ago']} bars ago")
    for sw in f.get("sweeps", [])[:1]:
        if sw["bars_ago"] <= 10:
            signals.append(f"Swept {'low' if sw['direction'] == 'bullish' else 'high'} {_fmt(sw['level'])}")
    mom = f.get("momentum") or {}
    rsi = mom.get("rsi")
    if mom.get("divergence"):
        signals.append(f"RSI {mom['divergence']} divergence")
    if rsi is not None and (rsi >= 70 or rsi <= 30):
        signals.append(f"RSI {rsi} ({'overbought' if rsi >= 70 else 'oversold'})")
    vol = (f.get("volume") or {}).get("last_vs_avg")
    if vol and vol >= 2:
        signals.append(f"Volume {vol}x average")

    dist_pct = None
    if nearest:
        k, z = nearest
        dist_pct = 0.0 if z["inside"] else round(((z["low"] if z["low"] > last else z["high"]) / last - 1) * 100, 2)
    return ScanResult(symbol=symbol, interval=interval, last_price=last, change_pct=change_24h(df),
                      trend=res.stats.trend, rsi=rsi, nearest_kind=nearest[0] if nearest else None,
                      nearest_low=nearest[1]["low"] if nearest else None,
                      nearest_high=nearest[1]["high"] if nearest else None, distance_pct=dist_pct,
                      signals=signals, data_source=source, volume_ratio=vol,
                      unusual_volume=bool((f.get("volume") or {}).get("unusual")))


def _closest(f: dict, kinds: tuple[str, ...]) -> float:
    d = [z["distance_atr"] for k in kinds for z in f.get(k, [])]
    return min(d, default=10.0)


def score(r: ScanResult, f: dict, filt: str) -> float:
    rsi = r.rsi if r.rsi is not None else 50.0
    br = f.get("last_structure_break") or {}
    recent_break = br.get("bars_ago", 99) <= 10
    sweeps = [s for s in f.get("sweeps", []) if s["bars_ago"] <= 10]
    if filt == "near_support":
        return 1 / (1 + _closest(f, SUPPORT_KINDS)) + (0.15 if br.get("direction") == "bullish" else 0)
    if filt == "near_resistance":
        return 1 / (1 + _closest(f, RESIST_KINDS)) + (0.15 if br.get("direction") == "bearish" else 0)
    if filt == "oversold":
        return (50 - rsi) / 50
    if filt == "overbought":
        return (rsi - 50) / 50
    if filt in ("bullish", "bearish"):
        up = filt == "bullish"
        return (0.4 * (r.trend == ("up" if up else "down")) + 0.3 * (br.get("direction") == filt)
                + 0.2 * any(s["direction"] == filt for s in sweeps) + 0.1 * ((rsi > 50) if up else (rsi < 50)))
    if filt == "volume":  # "which coins have unusual volume?"
        v = f.get("volume") or {}
        return max(v.get("last_vs_avg") or 1.0, (v.get("recent_vs_avg") or 1.0) * 1.5)
    if filt == "breakout":
        vol = (f.get("volume") or {}).get("last_vs_avg") or 1.0
        return (1 - br["bars_ago"] / 11 if recent_break else 0) + min(vol, 3) / 10
    return 1 / (1 + min(_closest(f, SUPPORT_KINDS), _closest(f, RESIST_KINDS))) + 0.1 * len(r.signals)


async def scan(market: MarketData, symbols: list[str], interval: str, filt: str = "any",
               limit: int = 300) -> list[ScanResult]:
    """Scan `symbols` on `interval`, best match for `filt` first. Coins that fail to load are skipped."""
    sem = asyncio.Semaphore(6)

    async def one(sym: str) -> ScanResult | None:
        async with sem:
            try:
                candles, source = await market.get_klines(sym, interval, limit)
            except Exception as exc:  # unknown symbol, network
                log.info("Scan skipped %s: %s", sym, exc)
                return None
        if len(candles) < 60:
            return None
        df = candles_to_df(candles)
        res = await asyncio.to_thread(analyze, df, SCAN_INTENT, interval)
        row = summarize(res, df, sym, interval, source)
        row.score = round(score(row, res.facts, filt), 3)
        return row

    rows = [r for r in await asyncio.gather(*(one(s) for s in symbols[:40])) if r is not None]
    return sorted(rows, key=lambda r: -r.score)


class WatchlistCache:
    """Scans for the watchlist sidebar, cached briefly so several tabs don't rescan."""

    def __init__(self, market: MarketData, ttl: float = 60.0) -> None:
        self.market = market
        self.ttl = ttl
        self._cache: dict[tuple, tuple[float, list[ScanResult]]] = {}

    async def get(self, symbols: list[str], interval: str) -> list[ScanResult]:
        key = (tuple(symbols), interval)
        hit = self._cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        rows = await scan(self.market, symbols, interval)
        by_symbol = {r.symbol: r for r in rows}
        ordered = [by_symbol[s] for s in symbols if s in by_symbol]  # keep the user's order
        self._cache[key] = (time.monotonic() + min(self.ttl, INTERVAL_SECONDS.get(interval, 60)), ordered)
        if len(self._cache) > 64:
            self._cache.pop(next(iter(self._cache)))
        return ordered


TICKER_FRESH = 3.0       # seconds a ticker answer is reused (several panels ask at once)
TICKER_KEEP = 15 * 60.0  # how long the last real price stands in for Binance when a refresh fails
_last_tickers: dict[str, tuple[float, dict]] = {}


async def tickers(market: MarketData, symbols: list[str]) -> list[dict]:
    """Last price and 24h change per symbol: one Binance call, or synthetic data as a fallback.

    Pairs Binance doesn't list (from its pair list, or rejected before) are left out of the call, since one unknown
    symbol makes Binance refuse the whole batch. Answers are reused for TICKER_FRESH seconds, and when a refresh
    fails the last real price (up to TICKER_KEEP old, marked stale) is served before any demo candles."""
    symbols = symbols[:40]
    now = time.monotonic()
    listed = getattr(market, "usd_pairs", frozenset())
    rejected = getattr(market, "_rejected", {})
    unknown = {s for s in symbols if (listed and s not in listed) or rejected.get(s, 0) > now}
    ask = [s for s in symbols if s not in unknown]
    order = {s: i for i, s in enumerate(symbols)}
    fresh = {s: row for s in ask if (hit := _last_tickers.get(s)) and now - hit[0] < TICKER_FRESH
             for row in [hit[1]]}
    out: list[dict] = list(fresh.values())
    todo = [s for s in ask if s not in fresh]
    if todo and market.binance_usable():
        try:
            resp = await market.client.get(f"{market.rest_url}/api/v3/ticker/24hr",
                                           params={"symbols": json.dumps(todo, separators=(",", ":")),
                                                   "type": "MINI"})
            if resp.status_code == 200:
                for t in resp.json():
                    last, open_ = float(t["lastPrice"]), float(t["openPrice"])
                    row = {"symbol": t["symbol"], "price": last, "source": "binance",
                           "change_pct": round((last / open_ - 1) * 100, 2) if open_ else None}
                    _last_tickers[t["symbol"]] = (now, row)
                    out.append(row)
                todo = []
            else:
                log.warning("Tickers answered %s; using the last prices or candles", resp.status_code)
        except Exception as exc:
            log.warning("Tickers failed (%s); using the last prices or candles", exc)
    stale = [s for s in todo if (hit := _last_tickers.get(s)) and now - hit[0] < TICKER_KEEP]
    out += [{**_last_tickers[s][1], "stale": True} for s in stale]
    todo = [s for s in todo if s not in stale]
    if len(_last_tickers) > 2000:
        _last_tickers.clear()

    async def from_candles(sym: str) -> dict | None:
        try:
            candles, source = await market.get_klines(sym, "1h", 30)
        except Exception:
            return None
        df = candles_to_df(candles)
        return {"symbol": sym, "price": float(df["close"].iloc[-1]), "change_pct": change_24h(df), "source": source}

    out += [t for t in await asyncio.gather(*(from_candles(s) for s in todo)) if t]
    return sorted(out, key=lambda t: order.get(t["symbol"], 99))
