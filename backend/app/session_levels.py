"""Session and period levels: the Asia, London and New York session highs and lows, the previous day, week and
month high and low, and the opening range, all computed from candles.

Sessions are fixed local-time windows, converted to UTC date by date so London and New York stay right across
daylight saving. The DST rules are written out below (`utc_offset`), so no time-zone database is needed:

  Asia      09:00-18:00 Tokyo     00:00-09:00 UTC all year (Japan has no DST)
  London    08:00-16:30 London    07:00-15:30 UTC in British Summer Time, 08:00-16:30 in winter
  New York  09:30-16:00 New York  13:30-20:00 UTC in daylight time, 14:30-21:00 in winter

Only weekday sessions count (Monday to Friday, local date). Day, week and month are UTC like Binance's candles:
the day starts at 00:00 UTC, the week on Monday 00:00 UTC, the month on the 1st.

Data: session highs and lows come from 30m candles (every window edge is on the hour or half hour), the period
levels from 4h candles (whole UTC days, weeks and months are made of 4h bars, and the synthetic 1w/1M bars are not
calendar-aligned) and the opening range from 30m, 15m or 5m candles, whichever is the largest that divides N.
Each level also says when price first traded through it after its window closed (`*_taken_at`), so the chart
can stop drawing levels that are spent.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import math
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from .market_data import INTERVAL_SECONDS, MarketData, candles_to_df
from .pricefmt import _fmt

log = logging.getLogger(__name__)

DAY = 86400
WEEK = 7 * DAY
TTL = 15.0  # seconds a computed set is reused (the chart polls every 30 s)
OR_MINUTES = (5, 240)  # allowed opening-range lengths


@dataclass(frozen=True)
class Session:
    key: str
    name: str
    label: str  # short chart label
    zone: str  # "tokyo" | "london" | "new_york", see utc_offset
    start: tuple[int, int]  # local (hour, minute)
    end: tuple[int, int]


SESSIONS: dict[str, Session] = {
    "asia": Session("asia", "Asia", "AS", "tokyo", (9, 0), (18, 0)),
    "london": Session("london", "London", "LON", "london", (8, 0), (16, 30)),
    "ny": Session("ny", "New York", "NY", "new_york", (9, 30), (16, 0)),
}
PERIODS: dict[str, tuple[str, str]] = {"day": ("PD", "previous day"), "week": ("PW", "previous week"),
                                       "month": ("PM", "previous month")}


# --------------------------------------------------------------------------------------------- time zones


def _sunday(year: int, month: int, nth: int) -> date:
    """The nth Sunday of a month (nth = -1: the last one)."""
    days = [d for d in range(1, calendar.monthrange(year, month)[1] + 1) if date(year, month, d).weekday() == 6]
    return date(year, month, days[nth])


def _utc(d: date, hour: int = 0, minute: int = 0) -> int:
    return int(datetime(d.year, d.month, d.day, hour, minute, tzinfo=timezone.utc).timestamp())


def utc_offset(zone: str, ts: int) -> int:
    """Seconds to add to UTC for local time in `zone` at UNIX time `ts`.

    London: BST (+1h) from the last Sunday of March 01:00 UTC to the last Sunday of October 01:00 UTC.
    New York: EDT (-4h) from the second Sunday of March 02:00 local (07:00 UTC) to the first Sunday of November
    02:00 local (06:00 UTC), EST (-5h) otherwise. Tokyo: +9h all year."""
    if zone == "tokyo":
        return 9 * 3600
    year = datetime.fromtimestamp(ts, timezone.utc).year
    if zone == "london":
        summer = _utc(_sunday(year, 3, -1), 1) <= ts < _utc(_sunday(year, 10, -1), 1)
        return 3600 if summer else 0
    if zone == "new_york":
        summer = _utc(_sunday(year, 3, 1), 7) <= ts < _utc(_sunday(year, 11, 0), 6)
        return -4 * 3600 if summer else -5 * 3600
    raise ValueError(f"Unknown zone {zone!r}")


def local_to_utc(d: date, hour: int, minute: int, zone: str) -> int:
    """UNIX time of local `hour:minute` on date `d` in `zone` (sessions never start inside a DST change)."""
    naive = _utc(d, hour, minute)
    std = {"tokyo": 9 * 3600, "london": 0, "new_york": -5 * 3600}[zone]
    return naive - utc_offset(zone, naive - std)


def session_windows(key: str, start: int, end: int) -> list[tuple[int, int]]:
    """(open, close) in UTC of every weekday `key` session that overlaps [start, end], oldest first."""
    s = SESSIONS[key]
    out = []
    d = datetime.fromtimestamp(start, timezone.utc).date() - timedelta(days=1)
    last = datetime.fromtimestamp(end, timezone.utc).date() + timedelta(days=1)
    while d <= last:
        if d.weekday() < 5:
            o = local_to_utc(d, *s.start, s.zone)
            c = local_to_utc(d, *s.end, s.zone)
            if c > start and o <= end:
                out.append((o, c))
        d += timedelta(days=1)
    return out


def recent_sessions(key: str, now: int, n: int = 2) -> list[tuple[int, int]]:
    """The last `n` windows of a session that have opened by `now`, newest first (the first may be in progress)."""
    wins = [w for w in session_windows(key, now - 10 * DAY, now) if w[0] <= now]
    return wins[::-1][:n]


def session_clock(now: int) -> list[dict]:
    """Each session's state at `now`: open (with when it closes) or closed (with when it next opens), in UTC
    seconds and whole minutes from now. Weekends skip to Monday's session."""
    out = []
    for key, s in SESSIONS.items():
        wins = [w for w in session_windows(key, now - DAY, now + 5 * DAY) if w[1] > now]
        if not wins:
            continue
        o, c = wins[0]
        live = o <= now
        out.append({"key": key, "name": s.name, "open": live, "opens_at": o, "closes_at": c,
                    "minutes": round(((c if live else o) - now) / 60)})
    return out


def _dur(minutes: int) -> str:
    h, m = divmod(max(minutes, 0), 60)
    return f"{h}h {m}m" if h and m else f"{h}h" if h else f"{m}m"


def clock_facts(now: int) -> dict:
    """For the agent: {"London": "open, closes in 3h 10m", "New York": "opens in 40m", ...}."""
    return {r["name"]: (f"open, closes in {_dur(r['minutes'])}" if r["open"] else f"opens in {_dur(r['minutes'])}")
            for r in session_clock(now)}


def period_window(kind: str, now: int, back: int = 1) -> tuple[int, int]:
    """UTC (open, close) of the day / week / month `back` periods before the current one."""
    today = datetime.fromtimestamp(now, timezone.utc).date()
    if kind == "day":
        o = _utc(today) - back * DAY
        return o, o + DAY
    if kind == "week":
        o = _utc(today - timedelta(days=today.weekday())) - back * WEEK
        return o, o + WEEK
    if kind == "month":
        y, m = today.year, today.month - back
        while m < 1:
            y, m = y - 1, m + 12
        o = _utc(date(y, m, 1))
        y2, m2 = (y + 1, 1) if m == 12 else (y, m + 1)
        return o, _utc(date(y2, m2, 1))
    raise ValueError(f"Unknown period {kind!r}")


# --------------------------------------------------------------------------------------------- pure levels


@dataclass
class Bars:
    """time / high / low arrays of a candle frame (bar open times, oldest first)."""
    time: np.ndarray
    high: np.ndarray
    low: np.ndarray

    @classmethod
    def of(cls, df: pd.DataFrame | None) -> "Bars":
        if df is None or df.empty:
            return cls(np.array([], dtype=np.int64), np.array([]), np.array([]))
        return cls(df["time"].to_numpy(np.int64), df["high"].to_numpy(float), df["low"].to_numpy(float))

    def join(self, finer: "Bars") -> "Bars":
        """These bars up to where `finer` starts, then `finer` (for long look-backs with recent detail)."""
        if not len(finer.time):
            return self
        keep = self.time < finer.time[0]
        return Bars(np.concatenate([self.time[keep], finer.time]), np.concatenate([self.high[keep], finer.high]),
                    np.concatenate([self.low[keep], finer.low]))


def window_high_low(bars: Bars, open_: int, close: int) -> tuple[float, float] | None:
    """High and low of the bars that open inside [open_, close)."""
    m = (bars.time >= open_) & (bars.time < close)
    if not m.any():
        return None
    return float(bars.high[m].max()), float(bars.low[m].min())


def first_cross(bars: Bars, after: int, price: float, side: str) -> int | None:
    """Open time of the first bar opening at or after `after` that traded beyond `price` (above a high, below a
    low); None while the level holds."""
    m = bars.time >= after
    hit = (bars.high > price) if side == "high" else (bars.low < price)
    idx = np.flatnonzero(m & hit)
    return int(bars.time[idx[0]]) if len(idx) else None


def _level_pair(bars: Bars, taken_bars: Bars, open_: int, close: int, now: int) -> dict | None:
    hl = window_high_low(bars, open_, close)
    if hl is None:
        return None
    high, low = hl
    live = now < close
    return {"open": open_, "close": close, "live": live, "high": high, "low": low,
            "high_taken_at": None if live else first_cross(taken_bars, close, high, "high"),
            "low_taken_at": None if live else first_cross(taken_bars, close, low, "low")}


def or_resolution(minutes: int) -> str:
    """Largest candle interval that divides an opening range of `minutes` (multiples of 5)."""
    for iv, m in (("30m", 30), ("15m", 15), ("5m", 5)):
        if minutes % m == 0:
            return iv
    return "5m"


def clamp_or_minutes(minutes: int) -> int:
    lo, hi = OR_MINUTES
    return max(lo, min(hi, int(round(minutes / 5)) * 5))


def or_anchors(now: int) -> list[tuple[str, int, int]]:
    """(key, open, close) of the latest UTC day and the latest window of each session: what an opening range
    hangs off."""
    day = period_window("day", now, 0)
    out = [("day", day[0], day[1])]
    for key in SESSIONS:
        wins = recent_sessions(key, now, 1)
        if wins:
            out.append((key, *wins[0]))
    return out


def shows(group: str, key: str, interval: str | None) -> bool:
    """Whether a level belongs on a chart of `interval`: sessions and opening ranges only intraday, the previous
    day below D, the previous week below W, the previous month below M."""
    if interval is None:
        return True
    secs = INTERVAL_SECONDS.get(interval, 0)
    if group in ("session", "opening_range"):
        return secs < DAY
    return {"day": secs < DAY, "week": secs < WEEK, "month": interval != "1M"}.get(key, True)


def compute_levels(fine: pd.DataFrame, coarse: pd.DataFrame, or_df: pd.DataFrame | None, or_minutes: int,
                   now: int, interval: str | None = None) -> dict:
    """{sessions, periods, opening_ranges} from 30m candles (`fine`), 4h candles (`coarse`) and the opening-range
    candles (`or_df`, None = use `fine`).

    sessions: the latest and the previous window of each session, {key, name, label, which: "latest"|"previous",
    open, close, live, high, low, high_taken_at, low_taken_at}. periods: previous day / week / month, same fields.
    opening_ranges: per anchor (UTC day or session) {key, name, label, open, close (= open + N min), until (the
    anchor's close), live, high, low}."""
    f, c = Bars.of(fine), Bars.of(coarse)
    taken = c.join(f)
    sessions = []
    for key, s in SESSIONS.items():
        if not shows("session", key, interval):
            continue
        for which, (o, cl) in zip(("latest", "previous"), recent_sessions(key, now, 2)):
            lv = _level_pair(f, taken, o, cl, now)
            if lv:
                sessions.append({"key": key, "name": s.name, "label": s.label, "which": which, **lv})
    periods = []
    for key, (label, name) in PERIODS.items():
        if not shows("period", key, interval):
            continue
        lv = _level_pair(c, taken, *period_window(key, now, 1), now)
        if lv:
            periods.append({"key": key, "name": name, "label": label, **lv})
    ranges = []
    if shows("opening_range", "", interval):
        o_bars = Bars.of(or_df) if or_df is not None else f
        for key, o, until in or_anchors(now):
            close = o + or_minutes * 60
            hl = window_high_low(o_bars, o, min(close, until))
            if hl is None:
                continue
            s = SESSIONS.get(key)
            ranges.append({"key": key, "name": f"{s.name if s else 'Day'} opening range",
                           "label": f"{s.label if s else 'Day'} OR", "open": o, "close": close, "until": until,
                           "live": now < close, "high": hl[0], "low": hl[1]})
    return {"sessions": sessions, "periods": periods, "opening_ranges": ranges}


# --------------------------------------------------------------------------------------------- agent facts


def _level_rows(data: dict) -> list[dict]:
    """Every level as {name, label, price, taken, live}."""
    rows = []
    for s in data.get("sessions", []):
        prefix = "" if s["which"] == "latest" else "previous "
        when = " (session in progress)" if s["live"] else ""
        for side in ("high", "low"):
            rows.append({"name": f"{prefix}{s['name']} session {side}{when}",
                         "label": f"{'' if s['which'] == 'latest' else 'p'}{s['label']} {side[0].upper()}",
                         "price": s[side], "taken": s[f"{side}_taken_at"] is not None, "live": s["live"]})
    for p in data.get("periods", []):
        for side in ("high", "low"):
            rows.append({"name": f"{p['name']} {side}", "label": f"{p['label']}{side[0].upper()}", "price": p[side],
                         "taken": p[f"{side}_taken_at"] is not None, "live": False})
    seen = set()
    for r in data.get("opening_ranges", []):
        if (r["open"], r["high"], r["low"]) in seen:  # Asia opens with the UTC day: one range, not two
            continue
        seen.add((r["open"], r["high"], r["low"]))
        for side in ("high", "low"):
            rows.append({"name": f"{r['name']} {side}" + (" (forming)" if r["live"] else ""),
                         "label": f"{r['label']} {side[0].upper()}", "price": r[side], "taken": False,
                         "live": r["live"]})
    return rows


def level_facts(data: dict, price: float, atr: float | None = None, near: int = 2) -> dict:
    """Compact read for the chart agent: the levels price is at now (within 0.08% or a tenth of an ATR, whichever
    is wider; nearest first), the nearest untaken ones above and below with their distance, and the sessions in
    progress."""
    if not price or price <= 0:
        return {}
    tol = max(price * 0.0008, (atr or 0) * 0.1)
    rows = _level_rows(data)
    at = [r["name"] for r in sorted(rows, key=lambda r: abs(r["price"] - price)) if abs(r["price"] - price) <= tol]
    fresh = [r for r in rows if not r["taken"] and abs(r["price"] - price) > tol]

    def side(rs: list[dict]) -> list[dict]:
        return [{"level": r["name"], "price": round(r["price"], 10),
                 "distance_pct": round((r["price"] / price - 1) * 100, 2)} for r in rs[:near]]

    out: dict = {
        "at": at[:4],
        "above": side(sorted((r for r in fresh if r["price"] > price), key=lambda r: r["price"])),
        "below": side(sorted((r for r in fresh if r["price"] < price), key=lambda r: -r["price"])),
        "sessions_in_progress": sorted({s["name"] for s in data.get("sessions", []) if s["live"]}),
    }
    if data.get("source") == "synthetic":
        out["note"] = "demo data (synthetic candles)"
    return out


def level_lines(f: dict) -> list[str]:
    """Plain-English sentences for `describe` (no-LLM answers)."""
    out = []
    if f.get("at"):
        out.append("Price is at the " + " and the ".join(f["at"][:3]) + ".")
    near = [f"the {r['level']} at {_fmt(r['price'])} ({r['distance_pct']:+.2f}%)" for r in
            (f.get("above", [])[:1] + f.get("below", [])[:1])]
    if near:
        out.append("The closest session levels are " + " and ".join(near) + ".")
    return out


# --------------------------------------------------------------------------------------------- service


class SessionLevelsService:
    """Fetches the candles and computes the levels per (symbol, interval, opening-range length), cached for TTL
    seconds. The result carries `source`: "binance" or "synthetic" (demo data)."""

    def __init__(self, market: MarketData) -> None:
        self.market = market
        self._cache: dict[tuple, tuple[float, dict]] = {}

    async def get(self, symbol: str, interval: str | None = None, or_minutes: int = 30,
                  now: int | None = None) -> dict:
        or_minutes = clamp_or_minutes(or_minutes)
        key = (symbol, interval, or_minutes)
        hit = self._cache.get(key)
        if hit and hit[0] > time.monotonic() and now is None:
            return hit[1]
        now = now or int(time.time())
        res = or_resolution(or_minutes)
        # Enough 30m bars to reach the previous session over a weekend; 4h bars back past the previous month.
        jobs = [self.market.get_klines(symbol, "30m", 480), self.market.get_klines(symbol, "4h", 420)]
        if res != "30m":
            earliest = min(o for _, o, _ in or_anchors(now))
            bars = min(1500, math.ceil((now - earliest) / INTERVAL_SECONDS[res]) + 2)
            jobs.append(self.market.get_klines(symbol, res, bars))
        results = await asyncio.gather(*jobs)
        frames = [candles_to_df(c) for c, _ in results]
        sources = {s for _, s in results}
        data = await asyncio.to_thread(compute_levels, frames[0], frames[1], frames[2] if len(frames) > 2 else None,
                                       or_minutes, now, interval)
        source = "binance" if sources == {"binance"} else "synthetic"
        out = {"symbol": symbol, "interval": interval, "source": source, "or_minutes": or_minutes,
               "price": float(results[0][0][-1].close) if results[0][0] else None, "generated_at": now, **data}
        if source == "synthetic":
            out["note"] = "Demo data: computed from synthetic candles"
        self._cache[key] = (time.monotonic() + TTL, out)
        if len(self._cache) > 256:
            self._cache.pop(next(iter(self._cache)))
        return out
