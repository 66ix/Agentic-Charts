"""Did the agent's levels hold? A log of the zones the agent draws, and a weekly review of how price treated them.

Every support, resistance, demand, supply and order-block box the agent draws is logged with the coin, timeframe,
time and the price when it was drawn (the same zone drawn again within a week is logged once). The review reads
the candles since each zone was drawn and sorts it:

  held       price came into the zone and no candle has closed through it since; `bounce_pct` is how far price
             then moved away from it (from the top of a floor, the bottom of a ceiling)
  broke      price came in and a candle closed through the far side
  untested   price hasn't reached it yet

A zone below price when drawn is a floor (it should hold price up), one above is a ceiling; a zone drawn with price
inside it counts as touched at once and is judged by its kind. Candles come from the same market data the chart uses,
on the zone's timeframe, or a coarser one when the week holds more candles than one request returns.

The review goes out with the brief's channels (brief.py) once a week, and GET /api/levels/review shows it any time.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .alerts import fmt_price, read_store, store_path, write_store
from .market_data import INTERVAL_SECONDS, MAX_KLINES, MarketData
from .ta_agent import TF_LABEL

log = logging.getLogger(__name__)

FLOORS = ("support", "demand", "ob_bullish")
CEILINGS = ("resistance", "supply", "ob_bearish")
KIND_NAMES = {"support": "support", "resistance": "resistance", "demand": "demand", "supply": "supply",
              "ob_bullish": "bullish order block", "ob_bearish": "bearish order block"}
KEEP_DAYS = 35
DEDUP_DAYS = 7
MAX_LEVELS = 1000
COARSER = ("15m", "1h", "4h", "1d")

Outcome = Literal["held", "broke", "untested"]


class LoggedLevel(BaseModel):
    id: str
    symbol: str
    interval: str
    kind: str
    low: float
    high: float
    side: Literal["floor", "ceiling"]
    drawn_at: int = Field(..., description="UNIX seconds")
    price: float = Field(..., description="Price when drawn")
    source: str = "binance"


def judge(level: LoggedLevel, candles: list[Any]) -> dict:
    """How price treated `level` in `candles` (oldest → newest, all after it was drawn)."""
    floor = level.side == "floor"
    inside = level.low <= level.price <= level.high
    touched_at: Optional[int] = level.drawn_at if inside else None
    after: list[Any] = []
    for c in candles:
        if touched_at is None and (c.low <= level.high if floor else c.high >= level.low):
            touched_at = c.time
        if touched_at is not None:
            if (c.close < level.low) if floor else (c.close > level.high):
                return {"outcome": "broke", "touched_at": touched_at, "broke_at": c.time, "close": c.close}
            after.append(c)
    if touched_at is None:
        last = candles[-1].close if candles else level.price
        away = (last / level.high - 1) * 100 if floor else (level.low / last - 1) * 100
        return {"outcome": "untested", "distance_pct": round(away, 2)}
    if floor:
        bounce = (max(c.high for c in after) / level.high - 1) * 100 if after else 0.0
    else:
        bounce = (1 - min(c.low for c in after) / level.low) * 100 if after else 0.0
    return {"outcome": "held", "touched_at": touched_at, "bounce_pct": round(max(bounce, 0.0), 2)}


def _interval_for(interval: str, since: float, now: float) -> str:
    """`interval`, or the first coarser one whose candles since `since` fit one request."""
    for iv in (interval, *COARSER):
        if iv in INTERVAL_SECONDS and INTERVAL_SECONDS[iv] >= INTERVAL_SECONDS.get(interval, 0) and \
                (now - since) / INTERVAL_SECONDS[iv] + 2 <= MAX_KLINES:
            return iv
    return "1d"


def render_review(review: dict) -> str:
    """The review as a plain-text message."""
    days = review["days"]
    head = f"Weekly level check — the last {days} days"
    if not review["levels"]:
        return f"{head}\nThe agent drew no support, resistance, demand or supply zones in that time."
    t = review["totals"]
    lines = [head, f"{t['drawn']} zone{'s' if t['drawn'] != 1 else ''} on {len(review['coins'])} coin{'s' if len(review['coins']) != 1 else ''}: "
                   f"{t['tested']} reached, {t['held']} held, {t['broke']} broke, {t['untested']} not reached yet."]
    if t["tested"]:
        lines.append(f"Held {t['held_pct']:.0f}% of the zones price reached.")
    for kind, k in review["by_kind"].items():
        if k["tested"]:
            lines.append(f"  {KIND_NAMES.get(kind, kind).capitalize()}: {k['held']}/{k['tested']} held")
    if review.get("demo"):
        lines.append("Demo data: some zones were judged on synthetic candles, not the live market.")
    blocks = ["\n".join(lines)]
    for coin in review["coins"]:
        rows = [coin["symbol"]]
        for r in coin["levels"]:
            zone = f"{TF_LABEL.get(r['interval'], r['interval'])} {KIND_NAMES.get(r['kind'], r['kind'])} " \
                   f"{fmt_price(r['low'])}–{fmt_price(r['high'])}"
            o = r["result"]
            if o["outcome"] == "held":
                rows.append(f"  {zone}: held, moved {o['bounce_pct']:.2f}% away")
            elif o["outcome"] == "broke":
                rows.append(f"  {zone}: broke (closed at {fmt_price(o['close'])})")
            else:
                rows.append(f"  {zone}: not reached ({abs(o['distance_pct']):.2f}% away)")
        blocks.append("\n".join(rows))
    return "\n\n".join(blocks)


class LevelLog:
    def __init__(self, market: MarketData, store: str = "memory") -> None:
        self.market = market
        self._path = store_path(store)
        data = read_store(self._path) or {}
        self._levels: list[LoggedLevel] = []
        for raw in data.get("levels", []):
            try:
                self._levels.append(LoggedLevel.model_validate(raw))
            except ValueError:
                continue

    def levels(self) -> list[LoggedLevel]:
        return list(self._levels)

    def record(self, symbol: str, interval: str, overlays: list, price: float, source: str,
               now: Optional[float] = None) -> int:
        """Log the zone boxes in `overlays` → how many were new."""
        ts = int(time.time() if now is None else now)
        self._levels = [lv for lv in self._levels if ts - lv.drawn_at <= KEEP_DAYS * 86400]
        added = 0
        for o in overlays:
            kind = getattr(o, "kind", None)
            if getattr(o, "type", None) != "box" or kind not in FLOORS + CEILINGS or price <= 0:
                continue
            low, high = float(o.price_low), float(o.price_high)
            if any(lv.symbol == symbol and lv.interval == interval and lv.kind == kind
                   and abs(lv.low - low) <= 1e-9 * max(low, 1) and abs(lv.high - high) <= 1e-9 * max(high, 1)
                   and ts - lv.drawn_at <= DEDUP_DAYS * 86400 for lv in self._levels):
                continue
            side = "floor" if price > high else "ceiling" if price < low else (
                "floor" if kind in FLOORS else "ceiling")
            self._levels.append(LoggedLevel(id=uuid.uuid4().hex[:12], symbol=symbol, interval=interval, kind=kind,
                                            low=low, high=high, side=side, drawn_at=ts, price=price, source=source))
            added += 1
        if added:
            self._levels = self._levels[-MAX_LEVELS:]
            write_store(self._path, {"levels": [lv.model_dump() for lv in self._levels]})
        return added

    async def review(self, days: int = 7, now: Optional[float] = None) -> dict:
        """The zones drawn in the last `days` days and how each one did, grouped by coin."""
        ts = time.time() if now is None else now
        recent = [lv for lv in self._levels if ts - lv.drawn_at <= days * 86400]
        groups: dict[tuple[str, str], list[LoggedLevel]] = {}
        for lv in recent:
            groups.setdefault((lv.symbol, lv.interval), []).append(lv)
        sem = asyncio.Semaphore(4)
        results: dict[str, dict] = {}
        demo = False

        async def one(symbol: str, interval: str, levels: list[LoggedLevel]) -> None:
            nonlocal demo
            since = min(lv.drawn_at for lv in levels)
            iv = _interval_for(interval, since, ts)
            step = INTERVAL_SECONDS[iv]
            async with sem:
                try:
                    candles, source = await self.market.get_klines(symbol, iv, int((ts - since) / step) + 3)
                except Exception as exc:
                    log.info("Level review: %s %s unavailable: %s", symbol, iv, exc)
                    return
            demo = demo or source == "synthetic"
            for lv in levels:
                # candles that opened at or after the bar the zone was drawn on
                start = lv.drawn_at - lv.drawn_at % step
                results[lv.id] = judge(lv, [c for c in candles if c.time >= start + step])

        await asyncio.gather(*(one(s, i, lvs) for (s, i), lvs in groups.items()))
        rows = [{**lv.model_dump(), "result": results[lv.id]} for lv in recent if lv.id in results]
        totals = {"drawn": len(rows), "held": 0, "broke": 0, "untested": 0}
        by_kind: dict[str, dict] = {}
        for r in rows:
            o = r["result"]["outcome"]
            totals[o] += 1
            k = by_kind.setdefault(r["kind"], {"tested": 0, "held": 0})
            if o != "untested":
                k["tested"] += 1
                k["held"] += o == "held"
        totals["tested"] = totals["held"] + totals["broke"]
        totals["held_pct"] = round(totals["held"] / totals["tested"] * 100, 1) if totals["tested"] else None
        coins: dict[str, list[dict]] = {}
        for r in sorted(rows, key=lambda r: (r["symbol"], r["drawn_at"])):
            coins.setdefault(r["symbol"], []).append(r)
        out = {"days": days, "levels": rows, "totals": totals, "by_kind": by_kind, "demo": demo,
               "coins": [{"symbol": s, "levels": lv} for s, lv in coins.items()]}
        out["text"] = render_review(out)
        return out
