"""Signal alerts: "tell me when Kimi prints B+ on BTC 4h", "when RSI diverges on any watchlist coin", "when price
sweeps the H4 low and closes back inside", "when a new fresh demand zone forms", and trigger alerts: "when 1m shows a
CHoCH inside the 4h demand" (signal `zone_trigger`, detection in zone_triggers.py).

A signal alert watches one coin on one timeframe for one detector event. Signals are judged on closed candles
only, so a wick that is still forming never fires one: for every (symbol, interval) with armed alerts the service
holds one StreamHub subscription, and when a kline with a newer open time arrives (or Binance marks the bar closed)
it fetches the closed candles, runs the same detectors the chart agent uses (indicators.py, patterns.py,
ta_agent.py, and Kimi Cooked through the shared KimiService) on the last WINDOW of them, and fires the alerts whose
signal happened on the candle that just closed. An alert never fires twice for the same candle or the same event.

Detection is the pure function `detect`; `scan_history` replays it bar by bar for the preview ("when would this
have fired recently?"), each bar seeing only the candles up to it, exactly as the live check would have.

A trigger alert watches its lower timeframe the same way; its zone is fixed, or looked up again on the higher
timeframe whenever that closes (cached per coin, timeframe and kind until then), and it fires once per touch of
the zone with a cooldown.

A fire goes to the alert history, `/ws/alerts` listeners ({type: "signal_fired"} and a fresh
{type: "signal_snapshot"}) and Telegram / Discord through AlertService. Candles from the synthetic fallback feed
never fire anything unless DATA_SOURCE=synthetic, like price alerts.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING, Literal, Optional, get_args

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from .alerts import AlertService, fmt_price, read_store, store_path, write_store
from .config import Settings, get_settings
from .indicators import rsi, rsi_divergence, structure_breaks
from .kimi_service import closed_only
from .market_data import INTERVAL_SECONDS, MarketData, candles_to_df
from .patterns import liquidity_sweeps
from .schemas import Interval, KimiSignal, TriggerZone, ZoneTriggerSpec, norm_symbol
from .ta_agent import TF_LABEL, atr, find_swings, supply_demand_zones
from .zone_triggers import (TriggerHit, ZoneBand, ZoneTrigger, detect_now, fixed_band, scan_triggers, trigger_hit,
                            zone_phrase)

if TYPE_CHECKING:
    from .kimi_service import KimiService
    from .stream_hub import StreamHub

log = logging.getLogger(__name__)

SignalId = Literal["kimi_buy", "kimi_sell", "kimi_any", "rsi_bull_div", "rsi_bear_div", "sweep_low", "sweep_high",
                   "new_demand", "new_supply", "bos_bull", "bos_bear", "rsi_overbought", "rsi_oversold",
                   "zone_trigger"]
SIGNAL_IDS: tuple[str, ...] = get_args(SignalId)


@dataclass(frozen=True)
class SignalInfo:
    name: str
    description: str


SIGNALS: dict[str, SignalInfo] = {
    "kimi_buy": SignalInfo("Kimi B+ (buy)", "Kimi Cooked prints a B+ divergence buy label"),
    "kimi_sell": SignalInfo("Kimi B- (sell)", "Kimi Cooked prints a B- divergence sell label"),
    "kimi_any": SignalInfo("Any Kimi label", "Kimi Cooked prints any label: B+, B-, U, Dn, B+? or B-?"),
    "rsi_bull_div": SignalInfo("RSI bullish divergence",
                               "Price makes a lower low while RSI makes a higher low"),
    "rsi_bear_div": SignalInfo("RSI bearish divergence",
                               "Price makes a higher high while RSI makes a lower high"),
    "sweep_low": SignalInfo("Sweep of a low", "A wick takes out a swing low and the candle closes back above it"),
    "sweep_high": SignalInfo("Sweep of a high", "A wick takes out a swing high and the candle closes back below it"),
    "new_demand": SignalInfo("New demand zone", "A fresh demand zone forms: a tight base, then a strong move up"),
    "new_supply": SignalInfo("New supply zone", "A fresh supply zone forms: a tight base, then a strong move down"),
    "bos_bull": SignalInfo("Bullish structure break",
                           "A candle closes above the last swing high (break of structure or change of character)"),
    "bos_bear": SignalInfo("Bearish structure break",
                           "A candle closes below the last swing low (break of structure or change of character)"),
    "rsi_overbought": SignalInfo("RSI crosses above 70", "RSI moves up into overbought"),
    "rsi_oversold": SignalInfo("RSI crosses below 30", "RSI moves down into oversold"),
    "zone_trigger": SignalInfo("Zone trigger", "A lower timeframe (1m/5m/15m) confirms inside a higher-timeframe zone: "
                                               "a CHoCH / BOS, a liquidity sweep or an engulfing close"),
}
KIMI_SIGNALS = frozenset({"kimi_buy", "kimi_sell", "kimi_any"})
KIMI_LABELS = {"kimi_buy": {"B+"}, "kimi_sell": {"B-"}}

WINDOW = 300          # closed candles the detectors see, like the watchlist scan
MIN_BARS = 60         # fewer than this and the detectors have nothing to work with
MAX_SIGNAL_ALERTS = 200
CLOSE_DELAY = 2.0     # seconds to let Binance settle the closed candle before fetching it
RETRY_DELAY = 6.0     # longer than MarketData's 5 s klines cache, so a retry sees new data
RETRIES = 4
ZONE_RETRY = 30.0     # seconds before looking up a detected zone again when the higher timeframe lags


# ----------------------------------------------------------------- models --


class SignalAlert(BaseModel):
    """A stored signal alert. Mirrors SignalAlert in frontend/lib/alerts.ts."""

    id: str
    symbol: str
    interval: Interval
    signal: SignalId
    armed: bool = True
    repeat: bool = Field(True, description="Stay armed after firing (the default); off = fire once")
    note: str = Field("", max_length=500)
    created_at: int = Field(..., description="UNIX milliseconds")
    last_fired_at: Optional[int] = Field(None, description="UNIX milliseconds")
    fire_count: int = 0
    last_bar: Optional[int] = Field(None, description="Open time (UNIX seconds) of the candle it last fired on")
    last_text: str = ""
    last_price: Optional[float] = None
    last_key: Optional[str] = Field(None, description="The event it last fired on, so it never fires twice")
    trigger: Optional[ZoneTrigger] = Field(None, description="Signal zone_trigger: the zone and the confirmation")
    last_stop: Optional[float] = Field(None, description="Zone triggers: the suggested stop of the last fire")


class CreateSignalAlertsRequest(BaseModel):
    symbols: list[str] = Field(..., min_length=1, max_length=40)
    interval: Interval = "4h"
    signal: SignalId
    repeat: bool = True
    note: str = Field("", max_length=500)


class SignalAlertPatch(BaseModel):
    armed: Optional[bool] = None
    repeat: Optional[bool] = None
    note: Optional[str] = Field(None, max_length=500)


@dataclass(frozen=True)
class SignalHit:
    time: int     # open time of the candle the signal happened on, UNIX seconds
    price: float  # that candle's close (Kimi: its entry price)
    text: str     # plain English, without the "BTCUSDT 4h: " prefix
    key: str      # identifies the event, e.g. the swing that was swept


# -------------------------------------------------------------- detection --


class Frame:
    """Closed candles ending at the bar being judged, with the detector inputs computed once (and lazily, so a
    signal only pays for what it reads)."""

    def __init__(self, df: pd.DataFrame) -> None:
        self.df = df.reset_index(drop=True)
        self.n = len(self.df)
        self.time = int(self.df["time"].iloc[-1])
        self.close = float(self.df["close"].iloc[-1])

    @cached_property
    def atr_series(self) -> pd.Series:
        return atr(self.df)

    @cached_property
    def swings(self) -> tuple[list, list]:
        return find_swings(self.df, float(self.atr_series.iloc[-1]))

    @cached_property
    def rsi(self) -> np.ndarray:
        return rsi(self.df["close"]).to_numpy(dtype=float)

    def divergence(self, bullish: bool) -> Optional[dict]:
        """The regular divergence on the two latest swing lows (bullish) or highs (bearish), if any."""
        cache = self.__dict__.setdefault("_div", {})
        if bullish not in cache:
            highs, lows = self.swings
            d = rsi_divergence(self.df, pd.Series(self.rsi), [] if bullish else highs, lows if bullish else [])
            cache[bullish] = d if d and d["kind"] == "regular" else None
        return cache[bullish]


def _kimi_hit(signal: str, frame_time: int, kimi: list[KimiSignal]) -> Optional[SignalHit]:
    labels = KIMI_LABELS.get(signal)
    found = [s for s in kimi if s.confirm_time == frame_time and (labels is None or s.text in labels)]
    if not found:
        return None
    s = found[-1]
    return SignalHit(frame_time, float(s.entry),
                     f"Kimi printed {s.text} ({s.direction}) at {fmt_price(s.entry)} — confluence {s.confluence}",
                     f"kimi:{s.text}:{s.time}:{s.confirm_time}")


def detect(signal: str, frame: Frame, prev: Optional[Frame] = None,
           kimi: Optional[list[KimiSignal]] = None) -> Optional[SignalHit]:
    """Did `signal` happen on the last candle of `frame`? `prev` is the same window one candle earlier (needed for
    divergences, which only show once their second swing is confirmed); `kimi` are Kimi Cooked's signals for the
    chart (needed for the kimi_* signals)."""
    if signal in KIMI_SIGNALS:
        return _kimi_hit(signal, frame.time, kimi or [])
    if signal == "zone_trigger":
        return None  # each trigger alert has its own zone: SignalAlertService._trigger_hit
    if frame.n < MIN_BARS:
        return None
    t, close, n = frame.time, frame.close, frame.n
    at = f"at {fmt_price(close)}"

    if signal in ("rsi_overbought", "rsi_oversold"):
        r = frame.rsi
        if n < 2 or np.isnan(r[-1]) or np.isnan(r[-2]):
            return None
        if signal == "rsi_overbought" and r[-2] < 70 <= r[-1]:
            return SignalHit(t, close, f"RSI crossed above 70 ({r[-1]:.1f}) {at}", f"rsi70:{t}")
        if signal == "rsi_oversold" and r[-2] > 30 >= r[-1]:
            return SignalHit(t, close, f"RSI crossed below 30 ({r[-1]:.1f}) {at}", f"rsi30:{t}")
        return None

    if signal in ("sweep_low", "sweep_high"):
        low = signal == "sweep_low"
        highs, lows = frame.swings
        for sw in liquidity_sweeps(frame.df, highs, lows):
            if sw["time"] == t and sw["direction"] == ("bullish" if low else "bearish"):
                what = (f"swept the low at {fmt_price(sw['level'])} (wick to {fmt_price(sw['extreme'])}) and closed "
                        f"back above {at}" if low else
                        f"swept the high at {fmt_price(sw['level'])} (wick to {fmt_price(sw['extreme'])}) and closed "
                        f"back below {at}")
                return SignalHit(t, close, what, f"sweep:{sw['side']}:{sw['swing_time']}")
        return None

    if signal in ("new_demand", "new_supply"):
        kind = "demand" if signal == "new_demand" else "supply"
        # The detector needs the candle after an impulse, so a zone whose impulse began on the previous candle is
        # the newest it can see: it formed with the candle that just closed.
        fresh = [z for z in supply_demand_zones(frame.df, frame.atr_series) if z.kind == kind and z.last_idx >= n - 2]
        if not fresh:
            return None
        z = max(fresh, key=lambda z: z.score)
        return SignalHit(t, close, f"new {kind} zone {fmt_price(z.price_low)}–{fmt_price(z.price_high)} formed "
                                   f"(close {fmt_price(close)})", f"zone:{kind}:{z.first_time}")

    if signal in ("bos_bull", "bos_bear"):
        direction = "bullish" if signal == "bos_bull" else "bearish"
        highs, lows = frame.swings
        hits = [b for b in structure_breaks(frame.df, highs, lows) if b["idx"] == n - 1 and b["direction"] == direction]
        if not hits:
            return None
        b = hits[-1]
        kind = "change of character (CHoCH)" if b["type"] == "CHoCH" else "break of structure (BOS)"
        where = "above the swing high" if direction == "bullish" else "below the swing low"
        return SignalHit(t, close, f"{direction} {kind}: closed {where} {fmt_price(b['level'])} {at}",
                         f"bos:{direction}:{b['swing_time']}")

    if signal in ("rsi_bull_div", "rsi_bear_div"):
        bullish = signal == "rsi_bull_div"
        d = frame.divergence(bullish)
        if d is None:
            return None
        old = prev.divergence(bullish) if prev is not None and prev.n >= MIN_BARS else None
        if old is not None and (old["time1"], old["time2"]) == (d["time1"], d["time2"]):
            return None  # already showing a candle ago
        swing = "lower low" if bullish else "higher high"
        text = (f"{'bullish' if bullish else 'bearish'} RSI divergence: {swing} {fmt_price(d['price2'])} vs "
                f"{fmt_price(d['price1'])} while RSI went {d['rsi1']:.1f} → {d['rsi2']:.1f} "
                f"(close {fmt_price(close)})")
        return SignalHit(t, close, text, f"div:{'bull' if bullish else 'bear'}:{d['time1']}:{d['time2']}")

    raise ValueError(f"Unknown signal {signal!r}")


def scan_history(signal: str, df: pd.DataFrame, bars: int, kimi: Optional[list[KimiSignal]] = None,
                 window: int = WINDOW) -> list[SignalHit]:
    """Every candle among the last `bars` of `df` (closed candles, oldest first) where `signal` would have fired,
    each judged on the `window` candles up to it, as the live check would have. Oldest first."""
    df = df.reset_index(drop=True)
    n = len(df)
    first = max(0, n - bars)
    if signal in KIMI_SIGNALS:
        times = set(df["time"].iloc[first:].astype(int))
        hits = [_kimi_hit(signal, int(s.confirm_time), [s]) for s in (kimi or []) if s.confirm_time in times]
        return sorted((h for h in hits if h), key=lambda h: h.time)
    hits: list[SignalHit] = []
    prev: Optional[Frame] = None
    start = max(first, MIN_BARS - 1)  # the first candle judged needs MIN_BARS candles up to it
    for k in range(start - 1, n):
        frame = Frame(df.iloc[max(0, k + 1 - window):k + 1])
        if k >= start:
            hit = detect(signal, frame, prev)
            if hit and (not hits or hits[-1].key != hit.key):
                hits.append(hit)
        prev = frame
    return hits


def describe(a: SignalAlert) -> str:
    """e.g. "BTCUSDT 4h · Kimi B+ (buy)"."""
    return f"{a.symbol} {a.interval} · {SIGNALS[a.signal].name}"


# ---------------------------------------------------------------- service --


class SignalAlertService:
    """Stores signal alerts and checks them on every candle close. See the module docstring."""

    def __init__(self, hub: StreamHub, market: MarketData, kimi: Optional[KimiService], alerts: AlertService,
                 settings: Settings | None = None, close_delay: float = CLOSE_DELAY,
                 retry_delay: float = RETRY_DELAY) -> None:
        self.hub = hub
        self.market = market
        self.kimi = kimi
        self.alerts = alerts
        self.s = settings or get_settings()
        self.close_delay = close_delay
        self.retry_delay = retry_delay
        self._path = store_path(self.s.signal_alerts_store)
        self._alerts: dict[str, SignalAlert] = {}
        self._watches: dict[tuple[str, str], tuple[asyncio.Queue, asyncio.Task]] = {}
        self._seen: dict[tuple[str, str], int] = {}       # newest open time seen per stream
        self._scheduled: dict[tuple[str, str], int] = {}  # newest closed candle queued for a check per stream
        self._done: dict[tuple[str, str], int] = {}       # newest closed candle checked per stream
        self._tasks: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()
        self._preview_sem = asyncio.Semaphore(1)       # previews are CPU-bound: one at a time
        # Detected trigger zones per (symbol, timeframe, kind, fresh_only) → (look again after, zone or None).
        self._zones: dict[tuple, tuple[float, Optional[ZoneBand]]] = {}
        self._closed = False
        self._load()
        alerts.add_snapshot_provider(self._snapshot)

    async def start(self) -> None:
        await self._sync()

    async def close(self) -> None:
        self._closed = True
        async with self._lock:
            watches = list(self._watches.items())
            self._watches.clear()
        for (sym, iv), (queue, task) in watches:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            await self.hub.unsubscribe(sym, iv, queue)
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=3)

    # -------------------------------------------------------------- CRUD
    def list(self) -> list[SignalAlert]:
        return sorted(self._alerts.values(), key=lambda a: -a.created_at)

    async def add(self, symbols: list[str], interval: str, signal: str, repeat: bool = True,
                  note: str = "") -> list[SignalAlert]:
        """One alert per symbol for `signal` on `interval`. A coin that already has this exact alert gets it back,
        armed again with the new repeat / note, instead of a duplicate. Raises ValueError for bad input."""
        if signal not in SIGNALS:
            raise ValueError(f"signal must be one of {', '.join(SIGNAL_IDS)}")
        if signal == "zone_trigger":
            raise ValueError("create zone triggers with POST /api/zone-triggers")
        if interval not in get_args(Interval):
            raise ValueError(f"interval must be one of {', '.join(get_args(Interval))}")
        syms: list[str] = []
        for raw in symbols:
            s = norm_symbol(raw)
            if not (s.isalnum() and 5 <= len(s) <= 20):
                raise ValueError(f"Invalid symbol {raw!r}")
            if s not in syms:
                syms.append(s)
        if not 1 <= len(syms) <= 40:
            raise ValueError("Give 1 to 40 symbols")
        note = note.strip()[:500]
        existing = {(a.symbol, a.interval, a.signal): a for a in self._alerts.values()}
        new_count = sum((s, interval, signal) not in existing for s in syms)
        if len(self._alerts) + new_count > MAX_SIGNAL_ALERTS:
            raise ValueError(f"At most {MAX_SIGNAL_ALERTS} signal alerts; delete some first")
        now = int(time.time() * 1000)
        out: list[SignalAlert] = []
        for s in syms:
            old = existing.get((s, interval, signal))
            a = (old.model_copy(update={"armed": True, "repeat": repeat, "note": note}) if old else
                 SignalAlert.model_validate({"id": uuid.uuid4().hex[:10], "symbol": s, "interval": interval,
                                             "signal": signal, "repeat": repeat, "note": note, "created_at": now}))
            self._alerts[a.id] = a
            out.append(a)
        self._changed()
        await self._sync()
        return out

    async def update(self, alert_id: str, patch: SignalAlertPatch) -> Optional[SignalAlert]:
        a = self._alerts.get(alert_id)
        if a is None:
            return None
        given = {k: v for k, v in patch.model_dump(include=patch.model_fields_set).items() if v is not None}
        if "note" in given:
            given["note"] = given["note"].strip()
        a = a.model_copy(update=given)
        self._alerts[alert_id] = a
        self._changed()
        await self._sync()
        return a

    async def remove(self, alert_id: str) -> bool:
        if self._alerts.pop(alert_id, None) is None:
            return False
        self._changed()
        await self._sync()
        return True

    # ----------------------------------------------------------- preview
    async def preview(self, symbol: str, interval: str, signal: str, bars: int = 300) -> dict:
        """When `signal` would have fired on the last `bars` closed candles → {symbol, interval, signal, name,
        bars, data_source, hits: [{time, price, text}] newest first, note?}."""
        if signal not in SIGNALS or signal == "zone_trigger":
            raise ValueError(f"signal must be one of {', '.join(SIGNAL_IDS[:-1])}")
        bars = max(10, min(bars, 1000))
        out: dict = {"symbol": symbol, "interval": interval, "signal": signal, "name": SIGNALS[signal].name,
                     "bars": bars}
        kimi: Optional[list[KimiSignal]] = None
        if signal in KIMI_SIGNALS:
            if self.kimi is None:
                raise ValueError("Kimi Cooked is not available on this server")
            k = await self.kimi.get(symbol, interval)
            candles, source = await self.market.get_klines(symbol, interval, bars + 2)
            kimi = k.signals
            out["note"] = "Kimi keeps its 15 most recent labels of each kind, so older ones may be missing."
        else:
            candles, source = await self.market.get_klines(symbol, interval, min(bars + WINDOW + 2, 5000))
        closed = closed_only(candles, interval)
        out["data_source"] = source
        if source == "synthetic":
            demo = "Demo data: these candles are synthetic, not the live market."
            out["note"] = f"{demo} {out['note']}" if out.get("note") else demo
        async with self._preview_sem:
            hits = await asyncio.to_thread(scan_history, signal, candles_to_df(closed), bars, kimi)
        out["hits"] = [{"time": h.time, "price": h.price, "text": h.text} for h in reversed(hits)]
        return out

    # ---------------------------------------------------- trigger alerts
    async def add_trigger(self, spec: ZoneTriggerSpec) -> SignalAlert:
        """A zone trigger alert (zone_triggers.py). The same coin, timeframe, zone and confirmation again re-arms
        the existing alert with the new cooldown, repeat and note instead of adding a duplicate. A fixed zone
        without a direction takes it from where price is (below price = long). Raises ValueError for bad input."""
        sym = spec.symbol
        if not (sym.isalnum() and 5 <= len(sym) <= 20):
            raise ValueError(f"Invalid symbol {sym!r}")
        zone = spec.zone
        if zone.source == "detected":
            if INTERVAL_SECONDS[zone.timeframe or "4h"] <= INTERVAL_SECONDS[spec.interval]:
                raise ValueError("the zone's timeframe must be higher than the trigger's")
        elif zone.direction is None:
            zone = zone.model_copy(update={"direction": await self._side_of(sym, spec.interval, zone)})
        trig = ZoneTrigger(zone=zone, confirm=spec.confirm, cooldown_min=spec.cooldown_min)
        if zone.source == "fixed":
            band = fixed_band(zone)
            trig = trig.model_copy(update={"zone_low": band.low, "zone_high": band.high, "zone_label": band.label})
        same = next((a for a in self._alerts.values() if a.trigger is not None and a.symbol == sym
                     and a.interval == spec.interval and a.trigger.confirm == spec.confirm
                     and a.trigger.zone.model_dump(exclude={"label"}) == zone.model_dump(exclude={"label"})), None)
        if same is None and len(self._alerts) >= MAX_SIGNAL_ALERTS:
            raise ValueError(f"At most {MAX_SIGNAL_ALERTS} signal alerts; delete some first")
        note = spec.note.strip()[:500]
        if same is not None:
            kept = same.trigger if zone.source == "detected" and same.trigger else trig
            a = same.model_copy(update={"armed": True, "repeat": spec.repeat, "note": note,
                                        "trigger": kept.model_copy(update={"cooldown_min": spec.cooldown_min})})
        else:
            a = SignalAlert(id=uuid.uuid4().hex[:10], symbol=sym, interval=spec.interval, signal="zone_trigger",
                            repeat=spec.repeat, note=note, created_at=int(time.time() * 1000), trigger=trig)
        self._alerts[a.id] = a
        self._changed()
        await self._sync()
        return a

    async def _side_of(self, symbol: str, interval: str, zone: TriggerZone) -> str:
        candles, _ = await self.market.get_klines(symbol, interval, 2)
        if not candles:
            raise ValueError("No price for this coin; choose long or short")
        last = candles[-1].close
        if (zone.price_low or 0) <= last <= (zone.price_high or 0):
            raise ValueError("Price is inside this zone: choose long (demand) or short (supply)")
        return "long" if last > (zone.price_high or 0) else "short"

    async def preview_trigger(self, spec: ZoneTriggerSpec, bars: int = 300) -> dict:
        """When the trigger would have fired on the last `bars` closed candles of its timeframe → {symbol,
        interval, name, zone: {low, high, label, direction} | null, bars, data_source, hits: [{time, price, text,
        stop}] newest first, note?}. A detected zone is replayed as it is now."""
        bars = max(10, min(bars, 1000))
        out: dict = {"symbol": spec.symbol, "interval": spec.interval, "name": SIGNALS["zone_trigger"].name,
                     "bars": bars, "zone": None, "hits": []}
        notes: list[str] = []
        zone = spec.zone
        if zone.source == "detected":
            band, source, _ = await detect_now(self.market, spec.symbol, zone)
            if band is None:
                out.update(data_source=source, note=f"No {zone_phrase(zone).removeprefix('the ')} right now.")
                return out
            notes.append("Replayed on the zone the detectors find now; before it formed the alert would have "
                         "watched another one.")
        else:
            if zone.direction is None:
                zone = zone.model_copy(update={"direction": await self._side_of(spec.symbol, spec.interval, zone)})
            band = fixed_band(zone)
        out["zone"] = {"low": band.low, "high": band.high, "label": band.label, "direction": band.direction}
        candles, source = await self.market.get_klines(spec.symbol, spec.interval, min(bars + WINDOW + 2, 5000))
        closed = closed_only(candles, spec.interval)
        out["data_source"] = source
        if source == "synthetic":
            notes.insert(0, "Demo data: these candles are synthetic, not the live market.")
        async with self._preview_sem:
            hits = await asyncio.to_thread(scan_triggers, candles_to_df(closed), band, spec.confirm, bars,
                                           spec.cooldown_min * 60, spec.interval)
        out["hits"] = [{"time": h.time, "price": h.price, "text": h.text, "stop": h.stop} for h in reversed(hits)]
        if notes:
            out["note"] = " ".join(notes)
        return out

    async def _trigger_zone(self, symbol: str, tr: ZoneTrigger) -> tuple[Optional[ZoneBand], bool]:
        """The zone a trigger watches now: fixed, or the detected one, looked up again after each close of its
        timeframe → (zone or None, whether that is a real answer rather than "could not look")."""
        z = tr.zone
        if z.source == "fixed":
            return fixed_band(z), True
        key = (symbol, z.timeframe, z.kind, z.fresh_only)
        cached = self._zones.get(key)
        now = time.time()
        if cached and cached[0] > now:
            return cached[1], True
        try:
            band, source, last_closed = await detect_now(self.market, symbol, z)
        except Exception as exc:
            log.info("Trigger zone %s %s %s unavailable: %s", symbol, z.timeframe, z.kind, exc)
            return (cached[1], True) if cached else (None, False)
        if source == "synthetic" and self.s.data_source != "synthetic":
            return None, False
        nxt = last_closed + 2 * INTERVAL_SECONDS[z.timeframe or "4h"] + self.close_delay
        self._zones[key] = (nxt if nxt > now else now + ZONE_RETRY, band)
        return band, True

    async def _trigger_hit(self, a: SignalAlert, frame: Frame) -> tuple[Optional[TriggerHit], bool]:
        """→ (the trigger's hit on the frame's last candle, whether its detected zone moved or went away)."""
        tr = a.trigger
        if tr is None:
            return None, False
        band, known = await self._trigger_zone(a.symbol, tr)
        now = (band.low, band.high, band.label) if band else (None, None, "")
        moved = known and now != (tr.zone_low, tr.zone_high, tr.zone_label)
        cur = self._alerts.get(a.id)
        if moved and cur is not None and cur.trigger is not None:
            self._alerts[a.id] = cur.model_copy(update={"trigger": cur.trigger.model_copy(update={
                "zone_low": now[0], "zone_high": now[1], "zone_label": now[2]})})
        if band is None:
            return None, moved
        hit = await asyncio.to_thread(trigger_hit, frame.df, band, tr.confirm, a.last_bar, tr.cooldown_min * 60,
                                      a.interval)
        return hit, moved

    # --------------------------------------------------------- listeners
    def _snapshot(self) -> dict:
        return {"type": "signal_snapshot", "alerts": [a.model_dump() for a in self.list()]}

    def _changed(self) -> None:
        self._save()
        self.alerts.broadcast(self._snapshot())

    # ---------------------------------------------------------- watching
    async def _sync(self) -> None:
        """Hold exactly one hub subscription per (symbol, interval) that has armed signal alerts."""
        async with self._lock:
            if self._closed:
                return
            want = {(a.symbol, a.interval) for a in self._alerts.values() if a.armed}
            for key in [k for k in self._watches if k not in want]:
                queue, task = self._watches.pop(key)
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
                await self.hub.unsubscribe(*key, queue)
                self._seen.pop(key, None)
            for key in sorted(want - self._watches.keys()):
                queue = await self.hub.subscribe(*key)
                self._watches[key] = (queue, asyncio.create_task(self._watch(*key, queue), name=f"signals:{key}"))

    async def _watch(self, symbol: str, interval: str, queue: asyncio.Queue) -> None:
        key = (symbol, interval)
        while True:
            msg = await queue.get()
            if msg.get("type") != "kline" or not msg.get("candle"):
                continue
            source = msg.get("source", "")
            if source == "synthetic" and self.s.data_source != "synthetic":
                continue  # fallback feed during a Binance outage: not real candles
            try:
                t = int(msg["candle"]["time"])
            except (KeyError, TypeError, ValueError):
                continue
            seen = self._seen.get(key)
            # Binance marks a candle's last update closed; a newer open time means the previous one closed too.
            closed_bar = t if msg.get("closed") else seen if seen is not None and t > seen else None
            self._seen[key] = max(t, seen or t)
            if closed_bar is not None and self._scheduled.get(key, -1) < closed_bar:
                self._scheduled[key] = closed_bar
                self._spawn(self._after_close(symbol, interval, closed_bar))

    async def _after_close(self, symbol: str, interval: str, bar_time: int) -> None:
        await asyncio.sleep(self.close_delay)
        try:
            await self.on_bar_close(symbol, interval, bar_time)
        except Exception:
            log.exception("Signal check failed for %s %s", symbol, interval)

    async def on_bar_close(self, symbol: str, interval: str, bar_time: int) -> list[SignalAlert]:
        """Judge the armed alerts on (symbol, interval) on the candle that opened at `bar_time` and has just closed
        → the alerts that fired. Each candle is judged once."""
        key = (symbol, interval)
        if self._done.get(key, -1) >= bar_time:
            return []
        self._done[key] = bar_time
        targets = [a for a in self._alerts.values()
                   if a.armed and (a.symbol, a.interval) == key and a.last_bar != bar_time]
        if not targets:
            return []
        frames = await self._frames(symbol, interval, bar_time)
        if frames is None:
            return []
        frame, prev, source = frames
        kimi: Optional[list[KimiSignal]] = None
        if any(a.signal in KIMI_SIGNALS for a in targets):
            kimi = await self._kimi_signals(symbol, interval, bar_time)
        hits: dict[str, Optional[SignalHit]] = {}
        fired: list[SignalAlert] = []
        moved = False
        for a in targets:
            if a.signal in KIMI_SIGNALS and kimi is None:
                continue
            hit: Optional[SignalHit | TriggerHit]
            if a.trigger is not None:
                hit, zone_moved = await self._trigger_hit(a, frame)
                moved = moved or zone_moved
            else:
                if a.signal not in hits:
                    hits[a.signal] = await asyncio.to_thread(detect, a.signal, frame, prev, kimi)
                hit = hits[a.signal]
            current = self._alerts.get(a.id)  # may have been edited or deleted while we computed
            if hit is None or hit.time != bar_time or current is None or not current.armed:
                continue
            if hit.key == current.last_key:
                continue
            fired.append(self._fire(current, hit, source))
        if fired or moved:
            self._changed()
        if fired:
            await self._sync()
        return fired

    def _fire(self, a: SignalAlert, hit: SignalHit | TriggerHit, source: str) -> SignalAlert:
        text = f"{a.symbol} {a.interval}: {hit.text}" + (f" — {a.note}" if a.note else "")
        new = a.model_copy(update={
            "armed": a.repeat, "last_fired_at": int(time.time() * 1000), "fire_count": a.fire_count + 1,
            "last_bar": hit.time, "last_key": hit.key, "last_text": text, "last_price": hit.price,
            "last_stop": getattr(hit, "stop", None),
        })
        self._alerts[a.id] = new
        log.info("Signal alert fired: %s", text)
        self.alerts.broadcast({"type": "signal_fired", "alert": new.model_dump(), "text": text, "price": hit.price,
                               "time": hit.time})
        self.alerts.record("signal", a.symbol, f"{TF_LABEL.get(a.interval, a.interval)} {SIGNALS[a.signal].name}",
                           text, price=hit.price, alert_id=a.id)
        self.alerts.notify(text + (" (synthetic demo data)" if source == "synthetic" else ""))
        return new

    async def _frames(self, symbol: str, interval: str, bar_time: int) -> Optional[tuple[Frame, Frame, str]]:
        """The WINDOW closed candles ending at `bar_time`, and the same window one candle earlier. Retries while
        the feed has not caught up with the close yet."""
        for attempt in range(RETRIES):
            if attempt:
                await asyncio.sleep(self.retry_delay)
            try:
                candles, source = await self.market.get_klines(symbol, interval, WINDOW + 2)
            except Exception as exc:
                log.info("Signal check %s %s: candles unavailable (%s)", symbol, interval, exc)
                continue
            if source == "synthetic" and self.s.data_source != "synthetic":
                log.info("Signal check %s %s skipped: Binance unreachable, synthetic candles", symbol, interval)
                return None
            closed = [c for c in closed_only(candles, interval) if c.time <= bar_time]
            if closed and closed[-1].time == bar_time:
                df = candles_to_df(closed[-(WINDOW + 1):])
                return Frame(df.iloc[-WINDOW:]), Frame(df.iloc[:-1].iloc[-WINDOW:]), source
        log.warning("Signal check %s %s: candle %s never arrived", symbol, interval, bar_time)
        return None

    async def _kimi_signals(self, symbol: str, interval: str, bar_time: int) -> Optional[list[KimiSignal]]:
        if self.kimi is None:
            return None
        for attempt in range(RETRIES):
            if attempt:
                await asyncio.sleep(self.retry_delay)
            try:
                k = await self.kimi.get(symbol, interval)
            except Exception as exc:
                log.info("Kimi for signal alerts on %s %s failed: %s", symbol, interval, exc)
                return None
            if k.data_source == "synthetic" and self.s.data_source != "synthetic":
                return None
            if k.last_closed >= bar_time:  # its cache outlives the close by a few seconds
                return k.signals
        return None

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------- persistence
    def _load(self) -> None:
        data = read_store(self._path) or {}
        for row in data.get("alerts", []):
            try:
                a = SignalAlert.model_validate(row)
            except ValueError as exc:
                log.warning("Skipping invalid stored signal alert: %s", exc)
                continue
            self._alerts[a.id] = a
        if self._alerts:
            log.info("Loaded %d signal alerts (%d armed)", len(self._alerts),
                     sum(a.armed for a in self._alerts.values()))

    def _save(self) -> None:
        write_store(self._path, {"alerts": [a.model_dump() for a in self._alerts.values()]})
