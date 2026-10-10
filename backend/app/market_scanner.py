"""Market-wide setup scanner: the best long and short trade plans across the top USDT pairs by 24h volume.

The watchlist scan (scanner.py) ranks the user's own coins. This one takes the top N USDT spot pairs by 24h quote
volume (stablecoin and fiat pairs and leveraged tokens left out), builds a long and a short plan for each with the
chart agent's own detectors and trade-plan builder (ta_agent.py, trade_plan.py), and ranks the setups by

* reward-to-risk at T1,
* how far the entry is from price (in ATR: an entry at market beats a limit order three ATR away),
* multi-timeframe agreement: the trend on the scan's timeframe and the next two up, against the setup's direction,
* the track record (track_record.py): how that setup type did on that coin and timeframe in the backtest.

Only plans entered at a zone count as setups (not market entries beyond a swing), and T1 must be at least MIN_RR.
Track records are backtests, so they are only run for the best TRACK_TOP setups per side, then those are re-ranked.

Two more lists come out of the same scan, for spot traders (buying coins outright, no shorts or leverage):

* Spot buys (`spot_rank`): the long setups whose entry is a support or demand zone that overlaps a zone on a higher
  timeframe, or where at least half the timeframes trend up, ranked with a bonus for that confluence. Each keeps its
  buy zone (the plan's zone), targets and invalidation (the plan's stop).
* Grid coins (`grid_candidate`): coins that have been ranging on the scan's timeframe, which is what a Spot Grid bot
  needs: most closes inside a 6-60% band, price crossing the middle of it often, little net drift, and price not
  sitting at an edge.

Binance limits: one 24h ticker call for the universe (cached), then candles for the coin's timeframe and its two
higher ones, at most MARKET_SCAN_CONCURRENCY coins at a time, through MarketData's candle cache and SQLite store;
only one scan runs at a time. Without Binance the scan runs on the fallback coin list with synthetic demo candles
and says so. The last result per timeframe is kept (MARKET_SCAN_STORE). MARKET_SCAN_SCHEDULE runs scans on a timer,
and MARKET_SCAN_NOTIFY_TOP sends the best setups of each timed run to Telegram / Discord.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from typing import TYPE_CHECKING, Literal, Optional

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from .alerts import read_store, store_path, write_store
from .config import Settings, get_settings
from .jobs import jobs
from .market_data import FALLBACK_SYMBOLS, INTERVAL_SECONDS, MarketData, candles_to_df
from .pricefmt import _fmt
from .scanner import change_24h
from .schemas import INTERVALS, AnalysisIntent, GridCoin, Interval, MarketSetup, SetupAgreement, TrackRecord
from .ta_agent import analyze, atr, ema, higher_timeframes
from .track_record import TrackRecordService
from .trade_plan import build_plan, plan_overlays

if TYPE_CHECKING:
    from .alerts import AlertService

log = logging.getLogger(__name__)

PLAN_INTENT = AnalysisIntent(features=["support_resistance", "supply_demand"], max_zones=2)
CANDLES = 300
MIN_CANDLES = 60
MIN_RR = 1.0
KEEP = 25            # setups kept per side
TRACK_TOP = 8        # setups per side that get a track record before the final ranking
UNIVERSE_TTL = 600.0
CHECK_SECONDS = 30.0
MIN_EVERY_MINUTES = 5.0
WEIGHTS = {"rr": 0.3, "near": 0.3, "agree": 0.2, "track": 0.2}
SPOT_ZONES = ("support", "demand")
SPOT_HTF_BONUS = 0.1
GRID_BARS = 120          # window the range is measured on
GRID_MIN_WIDTH, GRID_MAX_WIDTH = 6.0, 60.0
GRID_MIN_INSIDE = 0.85   # closes inside the range
GRID_MIN_CROSSINGS = 4
GRID_MAX_EFFICIENCY = 0.3
GRID_EDGE_PCT = 10.0     # price within this % of an edge is about to leave the range

# Base assets that are stablecoins or fiat: their USDT pairs barely move.
STABLE_BASES = frozenset({
    "USDC", "FDUSD", "TUSD", "BUSD", "USDP", "PAX", "DAI", "PYUSD", "USDE", "USDS", "USD1", "RLUSD", "BFUSD", "XUSD",
    "AEUR", "EURI", "EUR", "GBP", "AUD", "TRY", "BRL", "UST", "USTC", "SUSD", "FRAX", "LUSD", "GUSD", "USDD", "USDJ",
    "VAI", "IDRT", "BIDR", "UAH", "NGN", "RUB", "ZAR", "PLN", "RON", "ARS", "JPY", "MXN", "COP", "CZK",
})
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")
_SCHEDULE_ITEM = re.compile(r"^\s*([0-9]+[mhdwM])\s*[=:]\s*([0-9]+(?:\.[0-9]+)?)\s*$")


class MarketScanResult(BaseModel):
    """One scan of one timeframe. Mirrors MarketScanResult in frontend/lib/types.ts."""

    interval: Interval
    generated_at: int = Field(..., description="UNIX milliseconds")
    seconds: float = 0.0
    trigger: Literal["manual", "timer", "agent"] = "manual"
    universe: int = Field(0, description="Coins asked for")
    scanned: int = Field(0, description="Coins whose candles loaded")
    universe_source: Literal["binance", "fallback"] = "binance"
    data_source: str = "binance"
    longs: list[MarketSetup] = Field(default_factory=list)
    shorts: list[MarketSetup] = Field(default_factory=list)
    spot_buys: list[MarketSetup] = Field(default_factory=list, description="Longs at higher-timeframe demand")
    grid_coins: list[GridCoin] = Field(default_factory=list, description="Coins ranging well enough for a grid bot")
    notes: list[str] = Field(default_factory=list)

    def best(self, n: int, direction: Optional[str] = None) -> list[MarketSetup]:
        rows = (self.longs if direction != "short" else []) + (self.shorts if direction != "long" else [])
        return sorted(rows, key=lambda r: -r.score)[:n]

    def best_spot(self, n: int) -> list[MarketSetup]:
        return sorted(self.spot_buys, key=lambda r: -(r.spot_score or 0))[:n]


# ---------------------------------------------------------------- universe --


def is_leveraged(base: str, bases: set[str]) -> bool:
    """BTCUP, ETHDOWN, BNBBULL: a leveraged-token suffix on a coin that trades on its own. (JUP and SYRUP stay.)"""
    return any(base.endswith(s) and len(base) > len(s) + 1 and base[: -len(s)] in bases for s in LEVERAGED_SUFFIXES)


def pick_universe(tickers: list[dict], n: int) -> list[dict]:
    """24h tickers → the top `n` USDT pairs by quote volume: [{symbol, quote_volume, change_pct}]."""
    usdt = [t for t in tickers if isinstance(t, dict) and str(t.get("symbol", "")).endswith("USDT")]
    bases = {t["symbol"][:-4] for t in usdt}
    out = []
    for t in usdt:
        sym = t["symbol"]
        base = sym[:-4]
        if not sym.isascii() or not sym.isalnum() or base in STABLE_BASES or is_leveraged(base, bases):
            continue
        try:
            qv = float(t.get("quoteVolume") or 0)
            last, open_ = float(t.get("lastPrice") or 0), float(t.get("openPrice") or 0)
        except (TypeError, ValueError):
            continue
        if qv <= 0 or last <= 0:
            continue
        out.append({"symbol": sym, "quote_volume": qv,
                    "change_pct": round((last / open_ - 1) * 100, 2) if open_ > 0 else None})
    out.sort(key=lambda r: -r["quote_volume"])
    return out[:n]


def parse_schedule(text: str) -> dict[str, float]:
    """"15m=10, 4h=60" → {"15m": 10.0, "4h": 60.0} minutes; bad items are skipped, intervals under
    MIN_EVERY_MINUTES are raised to it."""
    out: dict[str, float] = {}
    for item in filter(str.strip, (text or "").split(",")):
        m = _SCHEDULE_ITEM.match(item)
        if not m or m.group(1) not in INTERVALS:
            log.warning("MARKET_SCAN_SCHEDULE: ignoring %r (use timeframe=minutes, e.g. 4h=60)", item)
            continue
        out[m.group(1)] = max(MIN_EVERY_MINUTES, float(m.group(2)))
    return out


# --------------------------------------------------------------- one coin --


def trend_of(df: pd.DataFrame) -> Literal["up", "down", "range"]:
    """EMA 20 against EMA 50 with the slow EMA's slope, the same rule ta_agent.analyze uses."""
    if len(df) < 30:
        return "range"
    a = float(atr(df).iloc[-1]) or 1e-12
    fast, slow = ema(df["close"], 20), ema(df["close"], 50)
    slope = (slow.iloc[-1] - slow.iloc[-10]) / a if len(df) > 10 else 0
    if fast.iloc[-1] > slow.iloc[-1] and slope > 0.2:
        return "up"
    if fast.iloc[-1] < slow.iloc[-1] and slope < -0.2:
        return "down"
    return "range"


def agreement(direction: str, trends: dict[str, str]) -> SetupAgreement:
    want = "up" if direction == "long" else "down"
    aligned = sum(1.0 if t == want else 0.5 if t == "range" else 0.0 for t in trends.values())
    return SetupAgreement(frames=dict(trends), aligned=aligned, total=len(trends))  # type: ignore[arg-type]


def track_score(t: Optional[TrackRecord]) -> float:
    """0..1 from the backtest's average R (+1R or better = 1, −1R or worse = 0); 0.5 when there is no usable record,
    pulled halfway back to 0.5 on a small sample."""
    if t is None or t.status not in ("ok", "small_sample") or t.avg_r is None:
        return 0.5
    s = 0.5 + max(-1.0, min(1.0, t.avg_r)) / 2
    return 0.5 + (s - 0.5) * 0.5 if t.status == "small_sample" else s


def score_setup(rr: float, distance_atr: float, agree: SetupAgreement, track: Optional[TrackRecord]) -> float:
    parts = {"rr": min(rr, 4.0) / 4.0, "near": 1.0 / (1.0 + distance_atr),
             "agree": agree.aligned / agree.total if agree.total else 0.5, "track": track_score(track)}
    return round(sum(WEIGHTS[k] * v for k, v in parts.items()), 4)


def coin_setups(symbol: str, interval: str, df: pd.DataFrame, frames: dict[str, pd.DataFrame], source: str,
                change_pct: Optional[float] = None, quote_volume: Optional[float] = None) -> list[MarketSetup]:
    """The long and the short setup on one coin (either may be missing), scored without a track record yet.
    CPU-bound: call it in a thread."""
    df = df.reset_index(drop=True)
    res = analyze(df, PLAN_INTENT, interval, None, frames)
    last, atr_v = res.stats.last_price, res.stats.atr
    trends = {interval: res.stats.trend, **{tf: trend_of(f.reset_index(drop=True)) for tf, f in frames.items()}}
    last_time = int(df["time"].iloc[-1])
    out: list[MarketSetup] = []
    for side in ("long", "short"):
        plan = build_plan(side, last, atr_v, res.stats.trend, res.levels, res.swing_lows, res.swing_highs, res.bias)
        if plan is None or plan.zone_kind in (None, "swing") or not plan.targets or plan.targets[0].rr < MIN_RR:
            continue
        dist = abs(plan.entry - last)
        agree = agreement(side, trends)
        rr = plan.targets[0].rr
        overlays = plan_overlays(plan, last_time)
        for i, ov in enumerate(overlays):
            ov.id = f"scan-{symbol}-{i}"
        out.append(MarketSetup(
            symbol=symbol, interval=interval, direction=side, last_price=last,  # type: ignore[arg-type]
            change_pct=change_pct if change_pct is not None else change_24h(df), quote_volume=quote_volume,
            entry=plan.entry, stop=plan.stop, target=plan.targets[0].price, rr=rr, risk_pct=plan.risk_pct,
            distance_pct=round(dist / last * 100, 2) if last else 0.0, distance_atr=round(dist / atr_v, 2),
            basis=plan.basis, agreement=agree, score=score_setup(rr, dist / atr_v, agree, None), plan=plan,
            overlays=overlays, data_source=source))
    return out


def spot_score(s: MarketSetup) -> Optional[float]:
    """Rank among spot buys, or None when the setup isn't one: a long at a support or demand zone with
    higher-timeframe confluence or at least half the timeframes trending up."""
    if s.direction != "long" or s.plan.zone_kind not in SPOT_ZONES:
        return None
    htf = bool(s.plan.zone_htf)
    agree = s.agreement.aligned / s.agreement.total if s.agreement.total else 0.5
    if not htf and agree < 0.5:
        return None
    return round(min(1.0, s.score + (SPOT_HTF_BONUS if htf else 0.0)), 4)


def spot_rank(setups: list[MarketSetup], keep: int = KEEP) -> list[MarketSetup]:
    """The spot buys among `setups`, best first, with `spot_score` set."""
    out = []
    for s in setups:
        s.spot_score = spot_score(s)
        if s.spot_score is not None:
            out.append(s)
    return sorted(out, key=lambda s: -(s.spot_score or 0))[:keep]


def grid_candidate(symbol: str, interval: str, df: pd.DataFrame, source: str, change_pct: Optional[float] = None,
                   quote_volume: Optional[float] = None) -> Optional[GridCoin]:
    """`symbol` as a grid coin when its last GRID_BARS candles ranged well enough for a Spot Grid bot, else None."""
    w = df.tail(GRID_BARS).reset_index(drop=True)
    if len(w) < MIN_CANDLES:
        return None
    lo, hi = float(w["low"].quantile(0.05)), float(w["high"].quantile(0.95))
    last = float(w["close"].iloc[-1])
    if lo <= 0 or hi <= lo or last <= 0:
        return None
    width = (hi / lo - 1) * 100
    closes = w["close"].to_numpy(dtype=float)
    inside = float(((closes >= lo) & (closes <= hi)).mean())
    side = np.sign(closes - (lo + hi) / 2)
    side = side[side != 0]
    crossings = int((side[1:] != side[:-1]).sum()) if len(side) > 1 else 0
    moves = float(np.abs(np.diff(closes)).sum())
    eff = abs(closes[-1] - closes[0]) / moves if moves else 1.0
    pos = (last - lo) / (hi - lo) * 100
    if not (GRID_MIN_WIDTH <= width <= GRID_MAX_WIDTH and inside >= GRID_MIN_INSIDE and crossings >= GRID_MIN_CROSSINGS
            and eff <= GRID_MAX_EFFICIENCY and GRID_EDGE_PCT <= pos <= 100 - GRID_EDGE_PCT):
        return None
    days = len(w) * INTERVAL_SECONDS.get(interval, 3600) / 86400
    a = float(atr(w).iloc[-1])
    score = (0.4 * min(crossings / 12, 1.0) + 0.3 * (1 - eff / GRID_MAX_EFFICIENCY) + 0.2 * inside
             + 0.1 * (1 - abs(pos - 50) / 50))
    return GridCoin(
        symbol=symbol, interval=interval, last_price=last, low=lo, high=hi, width_pct=round(width, 2),  # type: ignore[arg-type]
        crossings=crossings, efficiency=round(eff, 3), in_range_pct=round(inside * 100, 1),
        position_pct=round(pos, 1), atr_pct=round(a / last * 100, 2), days=round(days, 1), score=round(score, 4),
        change_pct=change_pct if change_pct is not None else change_24h(df), quote_volume=quote_volume,
        note=f"Ranged {_fmt(lo)}–{_fmt(hi)} ({width:.1f}% wide) for {days:.0f} days, crossing the middle "
             f"{crossings} times; price is {pos:.0f}% up the range.",
        data_source=source)


def coin_scan(symbol: str, interval: str, df: pd.DataFrame, frames: dict[str, pd.DataFrame], source: str,
              change_pct: Optional[float] = None,
              quote_volume: Optional[float] = None) -> tuple[list[MarketSetup], Optional[GridCoin]]:
    """One coin of a market scan: its setups and, if it qualifies, its grid-coin row. CPU-bound."""
    return (coin_setups(symbol, interval, df, frames, source, change_pct, quote_volume),
            grid_candidate(symbol, interval, df, source, change_pct, quote_volume))


# ----------------------------------------------------------------- service --


class MarketScanner:
    def __init__(self, market: MarketData, track: Optional[TrackRecordService] = None,
                 alerts: Optional["AlertService"] = None, settings: Optional[Settings] = None,
                 check_seconds: float = CHECK_SECONDS) -> None:
        self.market = market
        self.track = track or TrackRecordService(market)
        self.alerts = alerts
        self.s = settings or get_settings()
        self.schedule = parse_schedule(self.s.market_scan_schedule)
        self.check_seconds = check_seconds
        self._path = store_path(self.s.market_scan_store)
        self._results: dict[str, MarketScanResult] = {}
        for iv, raw in ((read_store(self._path) or {}).get("results") or {}).items():
            try:
                self._results[iv] = MarketScanResult.model_validate(raw)
            except ValueError as exc:
                log.warning("Ignoring a stored market scan for %s: %s", iv, exc)
        self._running: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()  # one scan at a time, whatever the timeframe: Binance weight limits
        self._universe: Optional[tuple[float, int, list[dict], str]] = None
        self._task: Optional[asyncio.Task] = None
        self._sent: dict[str, float] = {}  # setups sent by timed scans → when

    # ------------------------------------------------------------- reads
    def latest(self, interval: str) -> Optional[MarketScanResult]:
        return self._results.get(interval)

    def status(self, interval: str) -> dict:
        """The last result for `interval` plus what the panel shows around it."""
        now_ms = int(time.time() * 1000)
        last = {tf: r.generated_at for tf, r in self._results.items()}
        # A scheduled timeframe that never ran is scanned at the timer's next check.
        nxt = {tf: last[tf] + int(m * 60000) if tf in last else now_ms + int(self.check_seconds * 1000)
               for tf, m in self.schedule.items()}
        result = self._results.get(interval)
        return {"interval": interval, "result": result.model_dump(mode="json") if result else None,
                "running": sorted(self._running), "schedule": self.schedule, "next_run": nxt,
                "top": self.s.market_scan_top, "notify_top": self.s.market_scan_notify_top,
                "channels": self.alerts.channel_status if self.alerts else {}}

    async def fresh_or_run(self, interval: str, max_age: float, trigger: str = "agent") -> MarketScanResult:
        """The last result when it is younger than `max_age` seconds, else a new scan."""
        r = self._results.get(interval)
        if r and time.time() * 1000 - r.generated_at <= max_age * 1000:
            return r
        return await self.run(interval, trigger=trigger)

    # -------------------------------------------------------------- scan
    async def run(self, interval: str, top: Optional[int] = None, trigger: str = "manual") -> MarketScanResult:
        """Scan `interval` now. A scan of the same timeframe already running is shared rather than repeated."""
        if interval not in INTERVAL_SECONDS:
            raise ValueError(f"unsupported interval {interval!r}")
        task = self._running.get(interval)
        if task is None:
            task = asyncio.create_task(self._scan(interval, top or self.s.market_scan_top, trigger),
                                       name=f"market-scan:{interval}")
            self._running[interval] = task
            task.add_done_callback(lambda _t: self._running.pop(interval, None))
        return await asyncio.shield(task)  # a client that gives up leaves the scan to finish and be kept

    async def universe(self, n: int) -> tuple[list[dict], str]:
        """The top `n` coins → (rows, "binance" | "fallback")."""
        hit = self._universe
        if hit and hit[0] > time.monotonic() and hit[1] >= n:
            return hit[2][:n], hit[3]
        rows: list[dict] = []
        source = "fallback"
        if self.market.binance_usable():
            try:
                resp = await self.market._binance_get("/api/v3/ticker/24hr", params={"type": "MINI"})
                resp.raise_for_status()
                rows = pick_universe(resp.json(), n)
                source = "binance"
            except Exception as exc:  # network, geo-block, bad payload
                log.warning("Market scan: 24h tickers failed (%s); using the fallback coin list", exc)
        if not rows:
            rows = [{"symbol": s, "quote_volume": None, "change_pct": None} for s in FALLBACK_SYMBOLS[:n]]
            source = "fallback"
        self._universe = (time.monotonic() + (UNIVERSE_TTL if source == "binance" else 60.0), n, rows, source)
        return rows, source

    async def _scan(self, interval: str, top: int, trigger: str) -> MarketScanResult:
        async with self._lock:
            started = time.perf_counter()
            coins, universe_source = await self.universe(top)
            htfs = higher_timeframes(interval)
            sem = asyncio.Semaphore(self.s.market_scan_concurrency)
            sources: set[str] = set()

            async def one(c: dict) -> Optional[tuple[list[MarketSetup], Optional[GridCoin]]]:
                sym = c["symbol"]
                async with sem:
                    try:
                        candles, source = await self.market.get_klines(sym, interval, CANDLES)
                        higher = await asyncio.gather(*(self.market.get_klines(sym, h, CANDLES) for h in htfs),
                                                      return_exceptions=True)
                    except Exception as exc:  # unknown symbol, network
                        log.info("Market scan skipped %s: %s", sym, exc)
                        return None
                if len(candles) < MIN_CANDLES:
                    return None
                frames = {h: candles_to_df(r[0]) for h, r in zip(htfs, higher)
                          if not isinstance(r, BaseException) and len(r[0]) >= MIN_CANDLES and r[1] == source}
                sources.add(source)
                try:
                    return await asyncio.to_thread(coin_scan, sym, interval, candles_to_df(candles), frames, source,
                                                   c.get("change_pct"), c.get("quote_volume"))
                except Exception as exc:  # a detector choking on odd data must not sink the scan
                    log.info("Market scan: %s failed: %s", sym, exc)
                    return None

            found = [r for r in await asyncio.gather(*(one(c) for c in coins)) if r is not None]
            demo_dropped = 0
            if "binance" in sources and "synthetic" in sources:
                # Some coins fell back to demo candles mid-scan: leave them out rather than rank fake setups.
                live = [r for r in found if not any(s.data_source == "synthetic" for s in r[0])
                        and (r[1] is None or r[1].data_source != "synthetic")]
                demo_dropped = len(found) - len(live)
                found = live
                sources.discard("synthetic")
            setups = [s for rows, _ in found for s in rows]
            grid = sorted((g for _, g in found if g is not None), key=lambda g: -g.score)[:KEEP]
            longs = sorted((s for s in setups if s.direction == "long"), key=lambda s: -s.score)[:KEEP]
            shorts = sorted((s for s in setups if s.direction == "short"), key=lambda s: -s.score)[:KEEP]
            spot = spot_rank(setups)
            tracked = {id(s): s for s in longs[:TRACK_TOP] + shorts[:TRACK_TOP] + spot[:TRACK_TOP]}
            await self._add_track_records(list(tracked.values()))
            longs.sort(key=lambda s: -s.score)
            shorts.sort(key=lambda s: -s.score)
            spot = spot_rank(spot)

            notes: list[str] = []
            data_source = "synthetic" if sources == {"synthetic"} else "mixed" if len(sources) > 1 else (
                next(iter(sources)) if sources else "binance")
            if universe_source == "fallback":
                notes.append(f"Binance's 24h volume list is unavailable, so the scan covers {len(coins)} fallback "
                             f"coins instead of the top {top} by volume.")
            if "synthetic" in sources:
                notes.append("Synthetic demo data: these setups are not real prices.")
            if demo_dropped:
                many = demo_dropped != 1
                notes.append(f"{demo_dropped} coin{'s' if many else ''} fell back to demo candles and "
                             f"{'were' if many else 'was'} left out.")
            if len(found) < len(coins):
                notes.append(f"{len(coins) - len(found)} of {len(coins)} coins could not be loaded and were skipped.")
            result = MarketScanResult(
                interval=interval, generated_at=int(time.time() * 1000),  # type: ignore[arg-type]
                seconds=round(time.perf_counter() - started, 1), trigger=trigger,  # type: ignore[arg-type]
                universe=len(coins), scanned=len(found), universe_source=universe_source,  # type: ignore[arg-type]
                data_source=data_source, longs=longs, shorts=shorts, spot_buys=spot, grid_coins=grid, notes=notes)
            self._results[interval] = result
            self._save()
            log.info("Market scan %s: %d coins, %d longs, %d shorts, %d spot buys, %d grid coins in %.1fs", interval,
                     len(found), len(longs), len(shorts), len(spot), len(grid), result.seconds)
            return result

    async def _add_track_records(self, rows: list[MarketSetup]) -> None:
        async def one(row: MarketSetup) -> None:
            row.track_record = await self.track.for_plan(row.symbol, row.interval, row.plan)
            row.plan.track_record = row.track_record
            row.score = score_setup(row.rr, row.distance_atr, row.agreement, row.track_record)

        for row, res in zip(rows, await asyncio.gather(*(one(r) for r in rows), return_exceptions=True)):
            if isinstance(res, BaseException):
                log.info("Market scan: no track record for %s %s: %s", row.symbol, row.direction, res)

    # ---------------------------------------------------------- notifying
    def picks(self, result: MarketScanResult, n: int, side: Optional[str] = None) -> list[MarketSetup]:
        """The setups a timed scan sends: spot buys by default (MARKET_SCAN_NOTIFY_SIDE), longs, or both sides."""
        side = side or self.s.market_scan_notify_side
        if side == "both":
            return result.best(n)
        if side == "long":
            return result.best(n, "long")
        return result.best_spot(n)

    def message(self, result: MarketScanResult, n: int, rows: Optional[list[MarketSetup]] = None) -> str:
        """The best `n` setups of a scan (or `rows`) as a plain-text message for Telegram / Discord."""
        lines = [f"Market scan {result.interval}: best setups across {result.scanned} coins"
                 + (" (DEMO DATA)" if result.data_source == "synthetic" else "")]
        for s in self.picks(result, n) if rows is None else rows:
            agree = f"{s.agreement.aligned:g}/{s.agreement.total} timeframes agree" if s.agreement.total else ""
            tr = f" | {s.track_record.summary}" if s.track_record and s.track_record.summary else ""
            demo = " (demo data)" if s.data_source == "synthetic" and result.data_source != "synthetic" else ""
            lines.append(f"{s.direction.upper()} {s.symbol}: entry {_fmt(s.entry)}, stop {_fmt(s.stop)}, T1 "
                         f"{_fmt(s.target)} ({s.rr:g}R), {s.distance_pct:g}% away, {agree}{tr}{demo}")
        if len(lines) == 1:
            lines.append("No setups right now.")
        return "\n".join(lines)

    def to_send(self, result: MarketScanResult, n: int, now: Optional[float] = None) -> Optional[str]:
        """The message a timed scan sends, or None: nothing from demo or mixed data (unless DATA_SOURCE=synthetic),
        and no setup (coin and entry) sent in the last MARKET_SCAN_RESEND_HOURS."""
        now = time.time() if now is None else now
        if result.data_source != "binance" and not (result.data_source == "synthetic"
                                                    and self.s.data_source == "synthetic"):
            return None
        window = self.s.market_scan_resend_hours * 3600
        self._sent = {k: t for k, t in self._sent.items() if now - t < window}
        rows = []
        for s in self.picks(result, n):
            key = f"{result.interval}:{s.direction}:{s.symbol}:{s.entry:.6g}"
            if key not in self._sent:
                rows.append(s)
        if not rows:
            return None
        for s in rows:
            self._sent[f"{result.interval}:{s.direction}:{s.symbol}:{s.entry:.6g}"] = now
        return self.message(result, n, rows)

    # ---------------------------------------------------------- scheduler
    def due(self, now: Optional[float] = None) -> list[str]:
        ts = (time.time() if now is None else now) * 1000
        return [tf for tf, minutes in self.schedule.items() if tf not in self._running
                and (tf not in self._results or ts - self._results[tf].generated_at >= minutes * 60000)]

    async def tick(self, now: Optional[float] = None) -> list[str]:
        """Run every scheduled timeframe that is due → the ones that ran."""
        ran = []
        for tf in self.due(now):
            try:
                result = await self.run(tf, trigger="timer")
            except Exception:
                log.exception("Timed market scan of %s failed", tf)
                continue
            ran.append(tf)
            n = self.s.market_scan_notify_top
            if n > 0 and self.alerts is not None and self.alerts.channels:
                text = self.to_send(result, n, now)
                if text:
                    await self.alerts.send_text(text)
        return ran

    def start(self) -> None:
        if self.schedule and self._task is None:
            jobs.declare("market_scan", "Timed market scans", self.check_seconds)
            self._task = asyncio.create_task(self._run_timer(), name="market-scan:timer")

    async def close(self) -> None:
        tasks = [t for t in (self._task, *self._running.values()) if t is not None]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._task = None

    async def _run_timer(self) -> None:
        while True:
            await asyncio.sleep(self.check_seconds)
            try:
                ran = await self.tick()
                jobs.ok("market_scan", f"scanned {', '.join(ran)}" if ran else "")
            except Exception as exc:
                jobs.fail("market_scan", exc)
                log.exception("Market scan timer check failed")

    def _save(self) -> None:
        write_store(self._path, {"results": {k: v.model_dump(mode="json") for k, v in self._results.items()}})
