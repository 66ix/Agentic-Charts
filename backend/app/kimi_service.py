"""Kimi Cooked v5.7.4 on the app's candles: runs Trick's own Python port (app/kimi) and turns its result into
what the chart draws (S/R zones, Fib ladder, signal labels, forecast path and band) and the two tables.

The engine works on closed candles only, like the script since v5.7.4, so the forming candle is dropped and a
result is cached until the next candle closes. Daily history is passed in for intraday charts so the
higher-timeframe anchor and the daily trend state of the band are right from the first candle (see
app/kimi/PORT_README.md, "Getting candles").
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

from .alerts import fmt_price
from .config import get_settings
from .kimi import Inputs, KimiCooked, Result, __version__
from .market_data import DERIVED_INTERVALS, INTERVAL_SECONDS, MarketData
from .schemas import (Candle, KimiFib, KimiFibLevel, KimiForecast, KimiLevel, KimiNextCandle, KimiResponse, KimiRow,
                      KimiSignal)

log = logging.getLogger(__name__)

MAX_LABELS = 15        # the script's "Max Divergence Labels on Chart" default; Early "?" labels get the same cap
FIB_EXTEND = 25        # "Right Extension (bars)"
HISTORY_DAYS = 1000    # daily candles fed to the higher-timeframe requests of intraday charts
NOTES = [
    "Not in the Python port yet: chart patterns and harmonics (no Pat BO or Harmonics rows), the HTF divergence "
    "factor and session filters. Confluence scores run a little lower than on TradingView, so Conf top/rest, Long "
    "and Short can differ; DIV, U/Dn, Early and Random are unaffected.",
    "Stats cover the candles loaded here. TradingView's depend on how many candles your chart loaded.",
]
TYPE_NAMES = {0: "DIV", 1: "U/Dn", 2: "Early"}
LABELS = {(0, 1): "B+", (0, -1): "B-", (1, 1): "U", (1, -1): "Dn", (2, 1): "B+?", (2, -1): "B-?"}


def kimi_inputs() -> Inputs:
    """Trick's chart settings when the port was written (Node Tolerance 0.05, Coverage 0.75); the rest are the
    script's defaults."""
    return Inputs.users_chart()


def bar_closed(open_time: int, interval: str, now: float) -> bool:
    if interval == "1M":
        t = datetime.fromtimestamp(open_time, tz=timezone.utc)
        nxt = t.replace(year=t.year + (t.month == 12), month=t.month % 12 + 1)
        return nxt.timestamp() <= now
    return open_time + INTERVAL_SECONDS[interval] <= now


def closed_only(candles: list[Candle], interval: str, now: float | None = None) -> list[Candle]:
    now = time.time() if now is None else now
    return candles[:-1] if candles and not bar_closed(candles[-1].time, interval, now) else candles


def bars_for(interval: str) -> int:
    """Derived intervals are built from a finer one: keep the base request inside the candle cache."""
    bars = get_settings().kimi_bars
    if interval in DERIVED_INTERVALS:
        bars = min(bars, 5000 // DERIVED_INTERVALS[interval][1] - 50)
    return bars


def _arrays(candles: list[Candle]):
    t = np.array([c.time for c in candles], dtype="int64") * 1000
    cols = (np.array([getattr(c, k) for c in candles], dtype=float) for k in ("open", "high", "low", "close", "volume"))
    return (t, *cols)


def _pct(x: float | None, nd: int = 2) -> str:
    return "-" if x is None else f"{x:.{nd}f}"


def _tuple(x, nd: int = 2) -> str:
    if x is None:
        return "-"
    if isinstance(x, str):
        return x
    if isinstance(x, tuple):
        return " / ".join(_tuple(y, nd) for y in x)
    return f"{x:.{nd}f}" if isinstance(x, float) else str(x)


def verify_rows(res: Result) -> list[KimiRow]:
    rows = []
    for label, x in res.verify_table().items():
        if label == "Next candle / right / n":
            call, pct, n = x
            rows.append(KimiRow(label="Next candle / right / n", value=f"{call} / {_pct(pct, 1)}% / {n}"
                                if pct is not None else f"{call} / - / {n}"))
        elif label == "Skill vs RW %":
            rows.append(KimiRow(label=label, value=_tuple(x), tone=None if x is None else "up" if x > 0 else "down"))
        else:
            rows.append(KimiRow(label=label, value=_tuple(x)))
    return rows


def stats_rows(res: Result) -> list[KimiRow]:
    rows = []
    for label, x in res.stats_table().items():
        if label == "Exp/Trade":
            rows.append(KimiRow(label=label, value=f"{_tuple(x['signals_R'])}R | rnd {_tuple(x['random_R'])}R | "
                                                   f"fees {_tuple(x['fees_R'], 1)}R"))
        elif not x["n"]:
            rows.append(KimiRow(label=label, value="-", tone="mute"))
        else:
            edge = "" if x["edge_pts"] is None else f"  {x['edge_pts']:+.0f}pt (z {x['z']:+.1f})"
            tone = None if x["edge_pts"] is None or abs(x["z"]) < 1 else "up" if x["edge_pts"] > 0 else "down"
            rows.append(KimiRow(label=label, value=f"{x['win_pct']:.0f}% ({x['wins']}/{x['n']}){edge}", tone=tone))
    return rows


def _levels(res: Result, times: np.ndarray, odds: dict[float, int]) -> list[KimiLevel]:
    F = res.final
    last = len(times) - 1
    half = F["atr"] * F["eff_zone"]
    out = []
    for dd, side in ((1, "support"), (-1, "resistance")):
        for x in F["levels"][dd]:
            price, born, active, touches, broken = float(x[0]), int(x[1]), bool(x[2]), int(x[5]), int(x[6])
            state = "broken" if not active else "expired" if last - born > F["eff_lb"] else "active"
            out.append(KimiLevel(side=side, price=price, zone_low=price - half, zone_high=price + half,
                                 time_start=int(times[born]), time_end=int(times[broken]) if broken >= 0 else None,
                                 state=state, odds=odds.get(price) if active else None, touches=touches))
    return out


def _fib(res: Result, fc: dict, times: np.ndarray, step: int) -> KimiFib | None:
    sw = fc.get("fib_swing")
    if not sw or not fc["fib_odds"]:
        return None
    hi, lo, rng = sw["high"], sw["low"], sw["high"] - sw["low"]
    pocket = [hi - q * rng if sw["down"] else lo + q * rng for q in (0.618, 0.65)]   # the golden pocket
    return KimiFib(time_start=int(times[min(sw["high_bar"], sw["low_bar"])]),
                   time_end=int(times[-1]) + FIB_EXTEND * step, swing_high=hi, swing_low=lo, down=sw["down"],
                   levels=[KimiFibLevel(ratio=q, price=px, odds=pct) for q, px, pct in fc["fib_odds"]],
                   pocket_low=min(pocket), pocket_high=max(pocket))


def _signals(res: Result, times: np.ndarray) -> list[KimiSignal]:
    sigs = [s for s in res.signals if s.typ < 15]
    keep = [s for s in sigs if s.typ < 2][-MAX_LABELS:] + [s for s in sigs if s.typ == 2][-MAX_LABELS:]
    keep.sort(key=lambda s: (s.pivot_bar, s.bar))
    return [KimiSignal(type=TYPE_NAMES[s.typ], direction="long" if s.dir > 0 else "short",
                       text=LABELS[(s.typ, s.dir)], time=int(times[s.pivot_bar]), confirm_time=int(times[s.bar]),
                       price=s.price, entry=float(s.entry), confluence=int(s.conf),
                       tier={1: "top", 0: "rest", -1: "warm-up"}[s.tier],
                       result={0: "open", 1: "win", 2: "loss", 3: "expiry"}[s.result],
                       r=None if math.isnan(s.r) else round(float(s.r), 2)) for s in keep]


def _forecast(res: Result, fc: dict, times: np.ndarray, c: np.ndarray, step: int) -> KimiForecast:
    F = res.final
    born, H, bar = float(fc["born"]), int(fc["H"]), int(fc["bar"])
    path = [born] + [float(y) for y in fc["path"]]
    hi = [born] + [float(y) for y in fc["band_hi"]]
    lo = [born] + [float(y) for y in fc["band_lo"]]
    # The script's textured scenario path: the last H candles' moves replayed forward, detrended so it adds
    # character without adding direction, ending on the best guess.
    texture = list(path)
    if bar >= H:
        moves = np.diff(c[bar - H:bar + 1])
        cum = np.cumsum(moves)
        drift = cum[-1] / H
        texture = [path[0]] + [path[k] + float(cum[k - 1] - drift * k) for k in range(1, H + 1)]
    final = path[-1]
    n_fit = F["mzS"][10]
    headline = (f"▲ Proj: {fmt_price(final)}" if final > born else f"▼ Proj: {fmt_price(final)}" if final < born
                else "► Flat (no proven edge)" if n_fit > 30 else f"► Flat (learning {int(n_fit)}/30)")
    vol = F["vol_reg"]
    nxt = None
    call = F["ex_call_last"]
    if call != 0 and not F["ex_off"] and res.eng.p.fcNcOn:
        ex_n = F["ex_n"]
        nxt = KimiNextCandle(direction="up" if call > 0 else "down",
                             right_pct=round(F["ex_hit"] / ex_n * 100.0, 1) if ex_n >= 30 else None, calls=round(ex_n))
    return KimiForecast(start_time=int(times[bar]), step=step, horizon=H, path=path, band_high=hi, band_low=lo,
                        texture=texture, final=final, range_low=lo[-1], range_high=hi[-1],
                        pct_change=(final - born) / born * 100.0 if born else 0.0,
                        vol_regime="LOW" if vol < 0.8 else "HIGH" if vol > 1.25 else "NORMAL", headline=headline,
                        next_candle=nxt)


def compute(symbol: str, interval: str, candles: list[Candle], source: str,
            history: list[Candle] | None = None) -> KimiResponse:
    """Runs the engine on closed candles (CPU-bound: call it in a worker thread)."""
    if len(candles) < 60:
        raise ValueError(f"Kimi Cooked needs at least 60 closed candles, got {len(candles)}")
    started = time.perf_counter()
    step = INTERVAL_SECONDS[interval]
    t, o, h, l, c, v = _arrays(candles)
    hist = None
    if history:
        hist = {"t": np.array([x.time for x in history], dtype="int64") * 1000,
                "c": np.array([x.close for x in history], dtype=float)}
    res = KimiCooked(step / 60.0, kimi_inputs()).run(t, o, h, l, c, v, history=hist)
    times = t // 1000
    fc = res.last_forecast()
    odds = {px: pct for _, px, pct in fc["level_odds"]} if fc else {}
    return KimiResponse(
        symbol=symbol, interval=interval, version=__version__, data_source=source, bars=len(candles),
        last_closed=int(times[-1]), levels=_levels(res, times, odds),
        fib=_fib(res, fc, times, step) if fc else None, signals=_signals(res, times),
        forecast=_forecast(res, fc, times, c, step) if fc else None,
        verify=verify_rows(res), stats=stats_rows(res), notes=NOTES,
        seconds=round(time.perf_counter() - started, 2))


def summarize(k: KimiResponse) -> dict:
    """Compact facts for the agent: what the indicator shows right now."""
    last = k.forecast.path[0] if k.forecast else None
    held = [lv for lv in k.levels if lv.state != "broken"]
    out: dict = {
        "indicator": f"Kimi Cooked v{k.version} on {k.symbol} {k.interval}",
        "levels": [{"side": lv.side, "price": lv.price, "odds_pct": lv.odds, "touches": lv.touches,
                    **({"expired": True} if lv.state == "expired" else {})} for lv in held],
        "recent_signals": [{"label": s.text, "type": s.type, "direction": s.direction, "price": s.price,
                            "bars_ago": round((k.last_closed - s.confirm_time) / INTERVAL_SECONDS[k.interval]),
                            "result": s.result} for s in k.signals[-5:]],
    }
    if k.forecast:
        f = k.forecast
        out["forecast"] = {"headline": f.headline, "close": last, "end": round(f.final, 8), "bars": f.horizon,
                           "range": [f.range_low, f.range_high], "volatility": f.vol_regime,
                           **({"next_candle": f.next_candle.direction} if f.next_candle else {})}
    if k.fib:
        out["fib"] = [{"ratio": x.ratio, "price": x.price, "odds_pct": x.odds} for x in k.fib.levels]
    stats = {r.label: r.value for r in k.stats}
    out["signal_stats"] = {key: stats[key] for key in ("DIV +/-", "U / Dn", "Early ?", "Random") if key in stats}
    return out


@dataclass
class _Entry:
    last_closed: int
    response: KimiResponse
    expires: float


class KimiService:
    """One engine run per closed candle per chart; concurrent requests for the same chart share it."""

    def __init__(self, market: MarketData) -> None:
        self.market = market
        self._cache: dict[tuple[str, str], _Entry] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._cpu = asyncio.Semaphore(1)  # the engine is pure Python: one run at a time keeps streams responsive

    async def _inputs(self, symbol: str, interval: str) -> tuple[list[Candle], str, list[Candle] | None]:
        intraday = INTERVAL_SECONDS[interval] < 86400
        chart, daily = await asyncio.gather(
            self.market.get_klines(symbol, interval, bars_for(interval)),
            self.market.get_klines(symbol, "1d", HISTORY_DAYS) if intraday else asyncio.sleep(0, None),
            return_exceptions=True)
        if isinstance(chart, BaseException):
            raise chart
        candles, source = chart
        history = None
        if intraday and not isinstance(daily, BaseException) and daily is not None and daily[1] == source:
            history = closed_only(daily[0], "1d")
        return closed_only(candles, interval), source, history

    async def get(self, symbol: str, interval: str) -> KimiResponse:
        key = (symbol, interval)
        now = time.time()
        hit = self._cache.get(key)
        if hit and hit.expires > now:
            return hit.response
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self._cache.get(key)
            if hit and hit.expires > time.time():
                return hit.response
            candles, source, history = await self._inputs(symbol, interval)
            if hit and candles and hit.last_closed == candles[-1].time and hit.response.data_source == source:
                resp = hit.response  # no new closed candle since the last run
            else:
                async with self._cpu:
                    resp = await asyncio.to_thread(compute, symbol, interval, candles, source, history)
                log.info("Kimi Cooked %s %s: %d candles in %.2fs", symbol, interval, resp.bars, resp.seconds)
            # Re-check for a newly closed candle soon after the forming one is due to close.
            due = (resp.last_closed + 2 * INTERVAL_SECONDS[interval]) if interval != "1M" else now + 3600
            self._cache[key] = _Entry(resp.last_closed, resp, max(time.time() + 5, min(due + 3, time.time() + 3600)))
            if len(self._cache) > 64:
                self._cache.pop(next(iter(self._cache)))
            return resp
