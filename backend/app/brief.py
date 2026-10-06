"""The brief: a scheduled Telegram / Discord message on the watchlist, e.g. every morning at 08:00, or at the open
of each session.

For each coin it says where price is (24h change and change since the previous brief), the trend and RSI, the
nearest zone and how far it is, Kimi Cooked's latest signal in the last 24 hours and its forecast headline, funding
and the 24h open-interest change, and the key levels (nearest support and resistance, yesterday's daily high and
low). An overview line picks out the biggest movers and the coins sitting in a zone, and a "Today" section lists the
day's economic events when an events provider is wired in (`events_provider`). Everything comes from the same
engines the chart uses (scanner / ta_agent, kimi_service, derivatives); nothing is invented, and synthetic demo
candles or demo calendar entries are labelled as such.

Settings and the scheduler's memory live in BRIEF_STORE. The scheduler checks every 30 s and sends each configured
local time once per day; the last sent slot is saved before sending, so a restart never sends a slot twice. A slot
missed by more than an hour (server down) is skipped rather than sent late. Each send is added to the alert history
as kind "brief".
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator

from .alerts import AlertService, fmt_price, read_store, split_message, store_path, write_store
from .config import Settings, get_settings
from .kimi_service import closed_only, summarize as kimi_summarize
from .market_data import INTERVAL_SECONDS, MarketData, candles_to_df
from .scanner import DEFAULT_WATCHLIST, SCAN_INTENT, summarize as scan_summarize
from .schemas import Interval, ScanResult, norm_symbol
from .ta_agent import TF_LABEL, analyze

if TYPE_CHECKING:
    from .derivatives import DerivativesService
    from .kimi_service import KimiService

log = logging.getLogger(__name__)

EventsProvider = Callable[[], Awaitable[list[dict]]]

CHECK_SECONDS = 30.0
GRACE_SECONDS = 3600.0     # a slot missed by more than this (server down) is skipped, not sent late
COIN_TIMEOUT = 90.0        # Kimi's first run on a coin downloads 5,000 candles
EVENTS_TIMEOUT = 10.0
MAX_EVENTS = 12
_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
DEMO_SOURCES = ("demo", "mock", "synthetic", "sample")


def _tz(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        if name.upper() == "UTC":  # no tz database on this host (Windows without the tzdata package)
            return timezone.utc
        raise


# --------------------------------------------------------------- settings --


class BriefSections(BaseModel):
    zones: bool = True
    kimi: bool = True
    derivatives: bool = True
    events: bool = True
    levels: bool = True


class BriefSettings(BaseModel):
    """Mirrors BriefSettings in frontend/lib/alerts.ts."""

    enabled: bool = False
    times: list[str] = Field(default_factory=lambda: ["08:00"], min_length=1, max_length=8,
                             description="Local send times, HH:MM")
    timezone: str = Field("UTC", description="IANA zone, e.g. Europe/London")
    symbols: list[str] = Field(default_factory=list, max_length=40,
                               description="Coins to cover; empty = the default watchlist")
    interval: Interval = "4h"
    sections: BriefSections = Field(default_factory=BriefSections)

    @field_validator("times")
    @classmethod
    def _times(cls, v: list[str]) -> list[str]:
        out: set[str] = set()
        for t in v:
            m = _TIME.match(t.strip())
            if not m:
                raise ValueError(f"time {t!r} must look like 08:00")
            out.add(f"{int(m.group(1)):02d}:{m.group(2)}")
        return sorted(out)

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, v: str) -> str:
        v = v.strip() or "UTC"
        try:
            _tz(v)
        except Exception as exc:
            raise ValueError(f"unknown time zone {v!r}") from exc
        return v

    @field_validator("symbols")
    @classmethod
    def _symbols(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for raw in v:
            s = norm_symbol(raw)
            if not (s.isalnum() and 5 <= len(s) <= 20):
                raise ValueError(f"invalid symbol {raw!r}")
            if s not in out:
                out.append(s)
        return out


class BriefResult(BaseModel):
    text: str
    messages: list[str] = Field(..., description="The text split into the messages a channel receives")
    generated_at: datetime
    symbols: list[str]
    interval: str
    prices: dict[str, float] = Field(default_factory=dict, description="Each coin's price in this brief")
    data_source: str = Field("binance", description="'synthetic' when any coin ran on demo candles")


def due_slot(cfg: BriefSettings, now: float, last_slot: float, grace: float = GRACE_SECONDS) -> Optional[float]:
    """The newest configured send time (UNIX s) that has passed, is newer than `last_slot` and at most `grace`
    seconds old, or None. Times are local to `cfg.timezone`, so the brief follows daylight saving."""
    tz = _tz(cfg.timezone)
    local = datetime.fromtimestamp(now, tz)
    best: Optional[float] = None
    for back in (0, 1):  # yesterday too: a 23:59 slot checked just after midnight
        d = (local - timedelta(days=back)).date()
        for hhmm in cfg.times:
            h, m = (int(x) for x in hhmm.split(":"))
            slot = datetime(d.year, d.month, d.day, h, m, tzinfo=tz).timestamp()
            if last_slot < slot <= now and now - slot <= grace and (best is None or slot > best):
                best = slot
    return best


# ---------------------------------------------------------------- content --


@dataclass
class CoinBrief:
    symbol: str
    price: Optional[float] = None
    change_24h: Optional[float] = None
    since_last: Optional[float] = None
    trend: Optional[str] = None
    rsi: Optional[float] = None
    zone: Optional[ScanResult] = None
    support: Optional[dict] = None
    resistance: Optional[dict] = None
    d1_high: Optional[float] = None
    d1_low: Optional[float] = None
    kimi_signal: Optional[dict] = None   # {label, direction, price, hours_ago}
    kimi_headline: Optional[str] = None
    funding_pct: Optional[float] = None
    oi_change_pct: Optional[float] = None
    source: str = "binance"
    error: Optional[str] = None
    notes: list[str] = field(default_factory=list)


def _pct(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:+.2f}%"


def _band(lo: float, hi: float) -> str:
    return f"{fmt_price(lo)}–{fmt_price(hi)}"


def _zone_line(c: CoinBrief, tfl: str) -> Optional[str]:
    z = c.zone
    if z is None or z.nearest_kind is None or z.nearest_low is None or z.nearest_high is None:
        return None
    band = _band(z.nearest_low, z.nearest_high)
    if z.distance_pct == 0:
        return f"Zone: inside {tfl} {z.nearest_kind} {band}"
    where = "above" if (z.distance_pct or 0) > 0 else "below"
    return f"Zone: {tfl} {z.nearest_kind} {band}, {abs(z.distance_pct or 0):.2f}% {where}"


def _coin_block(c: CoinBrief, tfl: str, sections: BriefSections) -> str:
    if c.error or c.price is None:
        return f"{c.symbol}: no data ({c.error or 'unavailable'})"
    head = f"{c.symbol} {fmt_price(c.price)} ({_pct(c.change_24h)} 24h"
    head += f", {_pct(c.since_last)} since last brief)" if c.since_last is not None else ")"
    if c.source == "synthetic":
        head += " [demo data]"
    lines = [head]
    trend = f"Trend {c.trend}" if c.trend else ""
    rsi = f"RSI {c.rsi:.1f}" if c.rsi is not None else ""
    if trend or rsi:
        lines.append(" · ".join(x for x in (trend, rsi) if x))
    if sections.zones and (zl := _zone_line(c, tfl)):
        lines.append(zl)
    if sections.kimi and (c.kimi_signal or c.kimi_headline):
        parts = []
        if c.kimi_signal:
            k = c.kimi_signal
            parts.append(f"{k['label']} ({k['direction']}) {k['hours_ago']:.0f}h ago at {fmt_price(k['price'])}")
        else:
            parts.append("no signal in the last 24h")
        if c.kimi_headline:
            parts.append(f"forecast {c.kimi_headline}")
        lines.append("Kimi: " + " · ".join(parts))
    if sections.derivatives and (c.funding_pct is not None or c.oi_change_pct is not None):
        parts = []
        if c.funding_pct is not None:
            parts.append(f"Funding {c.funding_pct:.4f}%")
        if c.oi_change_pct is not None:
            parts.append(f"OI {c.oi_change_pct:+.2f}% 24h")
        lines.append(" · ".join(parts))
    if sections.levels:
        parts = []
        if c.support:
            parts.append(f"support {_band(c.support['low'], c.support['high'])}")
        if c.resistance:
            parts.append(f"resistance {_band(c.resistance['low'], c.resistance['high'])}")
        if c.d1_high is not None and c.d1_low is not None:
            parts.append(f"D1 high {fmt_price(c.d1_high)} / low {fmt_price(c.d1_low)}")
        if parts:
            lines.append("Levels: " + " · ".join(parts))
    lines += c.notes
    return "\n".join([lines[0]] + [f"  {x}" for x in lines[1:]])


def _event_lines(events: list[dict], tz: tzinfo, today: datetime) -> tuple[list[str], bool]:
    """Today's events (local date) as lines, and whether any is demo data."""
    rows: list[tuple[bool, float, str]] = []
    demo = False
    for e in events:
        try:
            t = float(e["time"])
            title = str(e.get("title") or "").strip()
        except (KeyError, TypeError, ValueError):
            continue
        local = datetime.fromtimestamp(t, tz)
        if not title or local.date() != today.date():
            continue
        demo = demo or bool(e.get("demo") or e.get("mock")) or str(e.get("source", "")).lower() in DEMO_SOURCES
        impact = str(e.get("impact") or "").strip().lower()
        country = str(e.get("country") or "").strip()
        line = f"{local:%H:%M} " + (f"{country} " if country else "") + title
        line += f" ({impact} impact)" if impact else ""
        rows.append((impact in ("", "low", "none", "holiday"), t, line))
    if len(rows) > MAX_EVENTS:  # keep the important ones
        rows = sorted(rows)[:MAX_EVENTS]
    return [line for _, _, line in sorted(rows, key=lambda r: r[1])], demo


def render_brief(now: datetime, tz_name: str, interval: str, coins: list[CoinBrief], sections: BriefSections,
                 events: Optional[list[dict]] = None) -> str:
    """The brief's plain text. `now` is local to `tz_name`."""
    tfl = TF_LABEL.get(interval, interval)
    out = [f"Market brief — {now:%a %d %b %Y, %H:%M} ({tz_name})",
           f"{len(coins)} coin{'s' if len(coins) != 1 else ''} on {tfl}"]
    if any(c.source == "synthetic" for c in coins):
        out.append("Demo data: prices marked [demo data] come from synthetic candles, not the live market.")
    ok = [c for c in coins if c.price is not None and not c.error]
    movers = sorted((c for c in ok if c.change_24h is not None), key=lambda c: -abs(c.change_24h or 0))[:3]
    overview = []
    if movers:
        overview.append("Biggest 24h moves: " + ", ".join(f"{c.symbol} {_pct(c.change_24h)}" for c in movers))
    in_zone = [c for c in ok if c.zone and c.zone.distance_pct == 0 and c.zone.nearest_kind]
    if sections.zones and in_zone:
        overview.append("In a zone: " + ", ".join(f"{c.symbol} ({c.zone.nearest_kind})" for c in in_zone
                                                  if c.zone))
    blocks = ["\n".join(out + overview)] + [_coin_block(c, tfl, sections) for c in coins]
    if sections.events and events:
        lines, demo = _event_lines(events, now.tzinfo or timezone.utc, now)
        if lines:
            title = "Today" + (" (demo calendar, not real events)" if demo else "")
            blocks.append("\n".join([title] + [f"  {x}" for x in lines]))
    return "\n\n".join(blocks)


# ---------------------------------------------------------------- service --


class NoChannelError(ValueError):
    """Raised by `send` when neither Telegram nor Discord is configured."""


class BriefService:
    def __init__(self, market: MarketData, kimi: Optional[KimiService], derivatives: Optional[DerivativesService],
                 alerts: AlertService, settings: Settings | None = None,
                 events_provider: Optional[EventsProvider] = None, check_seconds: float = CHECK_SECONDS) -> None:
        self.market = market
        self.kimi = kimi
        self.derivatives = derivatives
        self.alerts = alerts
        self.s = settings or get_settings()
        # async () -> [{time (UNIX s), title, country, impact}]; None = no "Today" section. Set by whoever owns the
        # economic calendar, e.g. `app.state.brief.events_provider = calendar.today`.
        self.events_provider = events_provider
        self.check_seconds = check_seconds
        self._path = store_path(self.s.brief_store)
        data = read_store(self._path) or {}
        try:
            self._settings = BriefSettings.model_validate(data.get("settings") or {})
        except ValueError as exc:
            log.warning("Invalid brief settings in %s (%s); using defaults", self._path, exc)
            self._settings = BriefSettings()
        self._last_slot = float(data.get("last_slot") or 0.0)
        self._last_prices: dict[str, float] = {k: float(v) for k, v in (data.get("last_prices") or {}).items()
                                               if isinstance(v, (int, float))}
        self._last_sent_at: Optional[int] = data.get("last_sent_at")
        self._task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()

    # ----------------------------------------------------------- settings
    @property
    def settings(self) -> BriefSettings:
        return self._settings

    def status(self) -> dict:
        """Settings plus what the panel shows next to them."""
        return {"settings": self._settings.model_dump(), "channels": self.alerts.channel_status,
                "last_sent_at": self._last_sent_at, "default_symbols": DEFAULT_WATCHLIST}

    def update_settings(self, new: BriefSettings, now: Optional[float] = None) -> BriefSettings:
        """Save new settings. Send times that already passed today do not fire at once; the next one does."""
        self._settings = new
        self._last_slot = max(self._last_slot, time.time() if now is None else now)
        self._save()
        return new

    # ------------------------------------------------------------ content
    async def build(self, symbols: Optional[list[str]] = None, interval: Optional[str] = None,
                    now: Optional[float] = None, sections: Optional[BriefSections] = None) -> BriefResult:
        """Build the brief now (nothing is sent) for `symbols` (default: the saved ones, else the default
        watchlist) on `interval` (default: the saved one)."""
        cfg = self._settings
        syms = [norm_symbol(s) for s in (symbols or cfg.symbols or DEFAULT_WATCHLIST)][:40]
        iv = interval or cfg.interval
        if iv not in INTERVAL_SECONDS:
            raise ValueError(f"unsupported interval {iv!r}")
        secs = sections or cfg.sections
        ts = time.time() if now is None else now
        local = datetime.fromtimestamp(ts, _tz(cfg.timezone))
        sem = asyncio.Semaphore(4)

        async def one(sym: str) -> CoinBrief:
            async with sem:
                try:
                    return await asyncio.wait_for(self._coin(sym, iv, secs), COIN_TIMEOUT)
                except Exception as exc:
                    log.info("Brief: %s failed: %s", sym, exc)
                    return CoinBrief(sym, error="data unavailable")

        coins, events = await asyncio.gather(asyncio.gather(*(one(s) for s in syms)), self._events(secs))
        text = render_brief(local, cfg.timezone, iv, list(coins), secs, events)
        limit = min((c.limit for c in self.alerts.channels), default=4000)
        return BriefResult(text=text, messages=split_message(text, limit),
                           generated_at=datetime.fromtimestamp(ts, timezone.utc), symbols=syms, interval=iv,
                           prices={c.symbol: c.price for c in coins if c.price is not None and not c.error},
                           data_source="synthetic" if any(c.source == "synthetic" for c in coins) else "binance")

    async def _coin(self, sym: str, interval: str, secs: BriefSections) -> CoinBrief:
        candles, source = await self.market.get_klines(sym, interval, 300)
        if len(candles) < 60:
            return CoinBrief(sym, error="not enough history")
        df = candles_to_df(candles)
        res = await asyncio.to_thread(analyze, df, SCAN_INTENT, interval)
        row = scan_summarize(res, df, sym, interval, source)
        c = CoinBrief(sym, price=row.last_price, change_24h=row.change_pct, trend=row.trend, rsi=row.rsi, zone=row,
                      source=source)
        prev = self._last_prices.get(sym)
        if prev:
            c.since_last = round((row.last_price / prev - 1) * 100, 2)
        sup, res_ = res.facts.get("support") or [], res.facts.get("resistance") or []
        c.support, c.resistance = (sup[0] if sup else None), (res_[0] if res_ else None)

        async def daily() -> None:
            if not secs.levels:
                return
            day, day_source = await self.market.get_klines(sym, "1d", 3)
            done = closed_only(day, "1d")
            if done and day_source == source:
                c.d1_high, c.d1_low = done[-1].high, done[-1].low

        async def kimi() -> None:
            if not secs.kimi or self.kimi is None:
                return
            k = await self.kimi.get(sym, interval)
            facts = kimi_summarize(k)
            step = INTERVAL_SECONDS[interval]
            recent = [s for s in facts.get("recent_signals", []) if s["bars_ago"] * step <= 86400]
            if recent:
                s = recent[-1]
                c.kimi_signal = {"label": s["label"], "direction": s["direction"], "price": s["price"],
                                 "hours_ago": s["bars_ago"] * step / 3600}
            if facts.get("forecast"):
                c.kimi_headline = facts["forecast"]["headline"]

        async def derivs() -> None:
            if not secs.derivatives or self.derivatives is None:
                return
            snap = await self.derivatives.symbol_snapshot(sym)
            if snap:
                c.funding_pct = snap.get("funding_rate_pct")
                c.oi_change_pct = snap.get("oi_change_24h_pct")

        for name, result in zip(("levels", "kimi", "derivatives"),
                                await asyncio.gather(daily(), kimi(), derivs(), return_exceptions=True)):
            if isinstance(result, BaseException):
                log.info("Brief: %s %s unavailable: %s", sym, name, result)
        return c

    async def _events(self, secs: BriefSections) -> Optional[list[dict]]:
        if not secs.events or self.events_provider is None:
            return None
        try:
            events = await asyncio.wait_for(self.events_provider(), EVENTS_TIMEOUT)
        except Exception as exc:
            log.info("Brief: economic events unavailable: %s", exc)
            return None
        return [e for e in events if isinstance(e, dict)] if isinstance(events, list) else None

    # ------------------------------------------------------------ sending
    async def send(self, symbols: Optional[list[str]] = None, interval: Optional[str] = None,
                   now: Optional[float] = None) -> dict:
        """Build the brief and send it to Telegram / Discord now → {text, messages, generated_at, results}.
        Raises NoChannelError when no channel is configured."""
        if not self.alerts.channels:
            raise NoChannelError("No notification channel is configured. Set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID "
                                 "or DISCORD_WEBHOOK_URL on the backend to receive the brief.")
        async with self._send_lock:
            brief = await self.build(symbols, interval, now)
            results = await self.alerts.send_text(brief.text)
            ts = time.time() if now is None else now
            self._last_sent_at = int(ts * 1000)
            if brief.data_source != "synthetic" or self.s.data_source == "synthetic":  # never mix demo and real
                self._last_prices.update(brief.prices)
            self._save()
            self.alerts.record("brief", "", "Market brief", brief.text, time_ms=self._last_sent_at)
        return {**brief.model_dump(mode="json"), "results": results}

    # ---------------------------------------------------------- scheduler
    async def tick(self, now: Optional[float] = None) -> bool:
        """Send the brief if a configured time is due → whether it was sent."""
        cfg = self._settings
        ts = time.time() if now is None else now
        if not cfg.enabled:
            return False
        slot = due_slot(cfg, ts, self._last_slot)
        if slot is None:
            return False
        self._last_slot = slot
        self._save()  # before sending: a crash or restart mid-send never sends this slot twice
        if not self.alerts.channels:
            log.warning("Brief due at %s but no Telegram / Discord channel is configured", cfg.times)
            return False
        try:
            await self.send(now=ts)
        except Exception:
            log.exception("Scheduled brief failed")
            return False
        return True

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="brief:scheduler")

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.check_seconds)
            try:
                await self.tick()
            except Exception:
                log.exception("Brief scheduler check failed")

    def _save(self) -> None:
        write_store(self._path, {"settings": self._settings.model_dump(), "last_slot": self._last_slot,
                                 "last_prices": self._last_prices, "last_sent_at": self._last_sent_at})
