"""Server-side price alerts, so they fire with every browser tab closed.

Alerts live in a small JSON file (ALERTS_STORE). For each symbol with armed
alerts the service holds one 1m subscription on the StreamHub (sharing the
upstream Binance stream with any open charts) and checks every kline update's
close against the alerts. A fired alert is disarmed (or, with `repeat`, stays
armed for the next crossing, at most one fire per 5 minutes), saved, pushed to
Telegram / Discord if configured, and broadcast to `/ws/alerts` listeners for
the in-app toast. Alerts with `expires_at` disarm themselves once it passes,
checked on every price and by a periodic task. Prices from the synthetic
fallback feed are ignored unless DATA_SOURCE=synthetic, so a Binance outage
never sends a notification for a made-up price.

The service also owns `/ws/alerts` and the alert history (ALERT_HISTORY_STORE):
every fire, whether a price alert, a signal alert (signal_alerts.py) or a brief
(brief.py), is appended to one log that the panel's History tab reads. The other
services broadcast through `broadcast` and add their own snapshot to every new
listener with `add_snapshot_provider`.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import contextlib
import json
import logging
import math
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Optional

import httpx
from pydantic import BaseModel, Field

from .notify import ChannelStats, Notice, Outbox, app_link
from .config import Settings, get_settings
from .jobs import jobs
from .schemas import AlertSpec, PriceAlert

if TYPE_CHECKING:
    from .stream_hub import StreamHub

log = logging.getLogger(__name__)

INTERVAL = "1m"
MAX_SNAPSHOTS = 50  # chart snapshots of fires kept for the History tab
MAX_ALERTS = 200
LISTENER_QUEUE_SIZE = 64
REPEAT_COOLDOWN_MS = 5 * 60_000  # a repeating alert fires at most this often
REPEAT_BAND = 0.003  # a repeating cross alert re-arms only once price is this far (0.3%) from its level
EXPIRY_CHECK_SECONDS = 30.0
MAX_HISTORY = 500
Side = Literal["above", "below", "inside"]
HistoryKind = Literal["price", "signal", "brief", "trade", "desk"]


# ------------------------------------------------------------- evaluation --


def _side(a: PriceAlert, price: float) -> Side:
    if a.kind == "zone":
        if a.price_low is not None and a.price_high is not None and a.price_low <= price <= a.price_high:
            return "inside"
        return "above" if price > (a.price_high if a.price_high is not None else math.inf) else "below"
    return "above" if price >= (a.price if a.price is not None else 0) else "below"


def evaluate(alert: PriceAlert, price: float, now_ms: Optional[int] = None) -> tuple[PriceAlert, bool]:
    """Check one armed alert against a new price → (alert, fired). Fires on the transition only:
    a cross alert when price moves to the other side of the level, a zone alert when price moves
    into the zone (or jumps straight through it). The first observation only records the side.
    A `repeat` alert stays armed after firing; a crossing within REPEAT_COOLDOWN_MS of its last fire
    only moves `last_side`, and after a fire a repeating cross alert keeps its side until price is REPEAT_BAND
    away from the level, so a price hovering on the level cannot fire it again and again. An alert past `expires_at` is disarmed and
    marked expired instead. Returns the same object when nothing changed."""
    if not alert.armed or not math.isfinite(price):
        return alert, False
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    if alert.expires_at is not None and now >= alert.expires_at:
        return alert.model_copy(update={"armed": False, "expired": True}), False
    side = _side(alert, price)
    prev = alert.last_side
    if (alert.repeat and alert.kind == "cross" and alert.triggered_at is not None and prev and side != prev
            and alert.price and abs(price - alert.price) < REPEAT_BAND * alert.price):
        side = prev  # still on the level since the last fire: not a new crossing
    fired = False
    if prev and prev != side:
        fired = alert.kind == "cross" or side == "inside" or prev != "inside"  # above→below skips over the zone
    if fired and alert.repeat and alert.triggered_at is not None and now - alert.triggered_at < REPEAT_COOLDOWN_MS:
        return alert.model_copy(update={"last_side": side}), False
    if fired:
        return alert.model_copy(update={
            "armed": alert.repeat, "last_side": side, "triggered_price": price, "triggered_at": now,
            "fire_count": alert.fire_count + 1,
        }), True
    return (alert if side == prev else alert.model_copy(update={"last_side": side})), False


def fmt_price(p: float) -> str:
    """The frontend's precision (2/4/5/8 decimals by magnitude), trimmed to at least two decimals."""
    digits = 2 if p >= 1000 else 4 if p >= 1 else 5 if p >= 0.01 else 8
    s = f"{p:,.{digits}f}"
    if digits > 2:
        whole, frac = s.split(".")
        s = f"{whole}.{frac.rstrip('0').ljust(2, '0')}"
    return s


def describe_fire(alert: PriceAlert, price: float) -> str:
    """e.g. "INJUSDT: price entered 24.10–24.60 (H4 Demand) at 24.32"."""
    if alert.kind == "zone" and alert.price_low is not None and alert.price_high is not None:
        zone = f"{fmt_price(alert.price_low)}–{fmt_price(alert.price_high)}"
        what = (f"entered {zone}" if alert.last_side == "inside"
                else f"moved {'up' if alert.last_side == 'above' else 'down'} through {zone}")
    else:
        what = f"crossed {'above' if alert.last_side == 'above' else 'below'} {fmt_price(alert.price or 0)}"
    label = f" ({alert.label})" if alert.label else ""
    note = f" — {alert.note}" if alert.note else ""
    return f"{alert.symbol}: price {what}{label} at {fmt_price(price)}{note}"


def _check_expiry(expires_at: Optional[int], now_ms: Optional[int] = None) -> None:
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    if expires_at is not None and expires_at <= now:
        raise ValueError("the expiry time has already passed")


def _valid_spec(spec: AlertSpec, now_ms: Optional[int] = None, check_expiry: bool = True) -> AlertSpec:
    def ok(p: Optional[float]) -> bool:
        return p is not None and math.isfinite(p) and p > 0

    if check_expiry:
        _check_expiry(spec.expires_at, now_ms)
    if spec.kind == "cross":
        if not ok(spec.price):
            raise ValueError("a cross alert needs a positive price")
        return spec
    if not (ok(spec.price_low) and ok(spec.price_high)):
        raise ValueError("a zone alert needs positive price_low and price_high")
    if spec.price_low > spec.price_high:  # type: ignore[operator]
        return spec.model_copy(update={"price_low": spec.price_high, "price_high": spec.price_low})
    return spec


class AlertPatch(BaseModel):
    """PATCH /api/alerts/{id}: only the fields sent change. `expires_at: null` removes the expiry; a null
    label, note or repeat is ignored."""

    price: Optional[float] = None
    price_low: Optional[float] = None
    price_high: Optional[float] = None
    label: Optional[str] = Field(None, max_length=200)
    note: Optional[str] = Field(None, max_length=500)
    repeat: Optional[bool] = None
    expires_at: Optional[int] = Field(None, description="UNIX milliseconds, or null for no expiry")


def apply_patch(alert: PriceAlert, patch: AlertPatch, now_ms: Optional[int] = None) -> PriceAlert:
    """The edited alert, validated like a new one. Moving a level forgets which side price was on, so the edit
    itself never fires it; a new expiry (or none) on an expired alert arms it again."""
    given = patch.model_dump(include=patch.model_fields_set)
    for key in ("label", "note", "repeat"):
        if key in given and given[key] is None:
            del given[key]
    levels = ("price",) if alert.kind == "cross" else ("price_low", "price_high")
    if any(k in given for k in ("price", "price_low", "price_high") if k not in levels):
        raise ValueError("a level alert takes `price`" if alert.kind == "cross"
                         else "a zone alert takes `price_low` and `price_high`")
    if "expires_at" in given:
        _check_expiry(given["expires_at"], now_ms)
    spec = _valid_spec(AlertSpec.model_validate({**alert.model_dump(include=set(AlertSpec.model_fields)), **given}),
                       check_expiry=False)
    update = spec.model_dump()
    if any(getattr(spec, k) != getattr(alert, k) for k in levels):
        update["last_side"] = None
    if alert.expired and "expires_at" in given:
        update.update(armed=True, expired=False, last_side=None)
    return alert.model_copy(update=update)


# --------------------------------------------------------------- channels --


@dataclass(frozen=True)
class Channel:
    """A notification target: POST `body(text)` as JSON to `url`. The URL holds the secret."""

    name: str
    url: str
    body: Callable[[str], dict]
    secrets: tuple[str, ...]
    limit: int = 4000  # characters per message; longer texts go out in several (send_long)

    async def send(self, client: httpx.AsyncClient, text: str) -> None:
        r = await client.post(self.url, json=self.body(text))
        r.raise_for_status()

    def redact(self, text: str) -> str:
        for s in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(s, "***")
        return text


def build_channels(s: Settings) -> list[Channel]:
    out: list[Channel] = []
    if s.telegram_bot_token and s.telegram_chat_id:
        chat_id = s.telegram_chat_id
        out.append(Channel(
            "telegram", f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage",
            lambda text: {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True},
            (s.telegram_bot_token,),
        ))
    if s.discord_webhook_url:
        url = s.discord_webhook_url
        token = url.rstrip("/").rsplit("/", 1)[-1]  # .../webhooks/<id>/<token>
        out.append(Channel("discord", url, lambda text: {"content": text[:2000], "allowed_mentions": {"parse": []}},  # never @everyone
                           (url, token) if len(token) >= 8 else (url,), limit=2000))
    return out


def split_message(text: str, limit: int) -> list[str]:
    """Cut `text` into messages of at most `limit` characters, at blank lines where possible, then at line
    breaks, and mid-line only for a line longer than `limit`."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    out: list[str] = []
    cur = ""
    for block in text.split("\n\n"):
        for piece in ([block] if len(block) <= limit else _lines(block, limit)):
            joined = f"{cur}\n\n{piece}" if cur else piece
            if len(joined) <= limit:
                cur = joined
            else:
                out.append(cur)
                cur = piece
    if cur:
        out.append(cur)
    return [m for m in out if m.strip()]


def _lines(block: str, limit: int) -> list[str]:
    """A paragraph longer than `limit` as chunks of whole lines (long lines hard-cut)."""
    chunks: list[str] = []
    cur = ""
    for line in block.split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        joined = f"{cur}\n{line}" if cur else line
        if len(joined) <= limit:
            cur = joined
        else:
            chunks.append(cur)
            cur = line
    if cur:
        chunks.append(cur)
    return chunks


def _error_text(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        detail = ""
        with contextlib.suppress(ValueError, AttributeError):
            body = exc.response.json()
            detail = str(body.get("description") or body.get("message") or "")[:200]
        return f"HTTP {exc.response.status_code}" + (f": {detail}" if detail else "")
    return f"{type(exc).__name__}: {exc}"[:300]


async def send_all(client: httpx.AsyncClient, channels: list[Channel], text: str) -> dict[str, bool]:
    """Send `text` to every channel concurrently → {name: delivered}. Never raises."""

    async def one(ch: Channel) -> bool:
        try:
            await ch.send(client, text)
            return True
        except Exception as exc:
            log.warning("Alert notification via %s failed: %s", ch.name, ch.redact(_error_text(exc)))
            return False

    results = await asyncio.gather(*(one(c) for c in channels))
    return {c.name: ok for c, ok in zip(channels, results)}


async def send_long(client: httpx.AsyncClient, channels: list[Channel], text: str,
                    pause: float = 0.4) -> dict[str, bool]:
    """Like send_all, for texts that may exceed a channel's limit: each channel gets the text split to its own
    limit, in order, with a short pause between parts (Telegram rate-limits bursts). Never raises."""

    async def one(ch: Channel) -> bool:
        try:
            for i, part in enumerate(split_message(text, ch.limit)):
                if i:
                    await asyncio.sleep(pause)
                await ch.send(client, part)
            return True
        except Exception as exc:
            log.warning("Notification via %s failed: %s", ch.name, ch.redact(_error_text(exc)))
            return False

    results = await asyncio.gather(*(one(c) for c in channels))
    return {c.name: ok for c, ok in zip(channels, results)}


class _RedactFilter(logging.Filter):
    """httpx logs every request URL at INFO; the Telegram and Discord URLs contain the secret."""

    def __init__(self, channels: list[Channel]) -> None:
        super().__init__()
        self.channels = channels

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        redacted = msg
        for ch in self.channels:
            redacted = ch.redact(redacted)
        if redacted != msg:
            record.msg, record.args = redacted, ()
        return True


# ------------------------------------------------------------ JSON stores --


def store_path(value: str) -> Optional[Path]:
    """A *_STORE setting → its file, or None for `memory` (kept in memory only, as in tests)."""
    value = value.strip()
    return None if value.lower() in ("", "memory", "none", "off") else Path(value)


def read_store(path: Optional[Path]) -> Optional[dict]:
    if not path or not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        log.warning("Could not read %s (%s); starting empty", path, exc)
        return None
    return data if isinstance(data, dict) else None


def write_store(path: Optional[Path], data: dict) -> None:
    """Atomic write (temp file + rename), so a crash mid-write never leaves a half file."""
    if not path:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(path)
    except OSError as exc:
        log.warning("Could not save %s: %s", path, exc)


class AlertHistory:
    """Every fire (price alert, signal alert, brief), the newest MAX_HISTORY kept, saved to ALERT_HISTORY_STORE.
    Items: {id, time (ms), kind, symbol, title, text, price?, alert_id?}."""

    def __init__(self, store: str) -> None:
        self._path = store_path(store)
        data = read_store(self._path) or {}
        self._items: list[dict] = [i for i in data.get("items", []) if isinstance(i, dict) and "time" in i]
        self._items = self._items[-MAX_HISTORY:]

    def add(self, kind: HistoryKind, symbol: str, title: str, text: str, price: Optional[float] = None,
            alert_id: Optional[str] = None, time_ms: Optional[int] = None) -> dict:
        item: dict = {"id": uuid.uuid4().hex[:12], "time": time_ms or int(time.time() * 1000), "kind": kind,
                      "symbol": symbol, "title": title, "text": text}
        if price is not None:
            item["price"] = price
        if alert_id is not None:
            item["alert_id"] = alert_id
        self._items.append(item)
        del self._items[:-MAX_HISTORY]
        write_store(self._path, {"items": self._items})
        return item

    def mark_image(self, item_id: str) -> bool:
        for it in reversed(self._items):
            if it.get("id") == item_id:
                it["image"] = True
                write_store(self._path, {"items": self._items})
                return True
        return False

    def list(self, limit: int = 100) -> list[dict]:
        """Newest first."""
        return self._items[::-1][:max(0, limit)]

    def clear(self) -> int:
        n = len(self._items)
        self._items = []
        write_store(self._path, {"items": []})
        return n


# ---------------------------------------------------------------- service --


class AlertService:
    def __init__(self, hub: StreamHub, settings: Settings | None = None,
                 client: httpx.AsyncClient | None = None) -> None:
        self.hub = hub
        self.s = settings or get_settings()
        self._store = store_path(self.s.alerts_store)
        self._alerts: dict[str, PriceAlert] = {}
        self._watches: dict[str, tuple[asyncio.Queue, asyncio.Task]] = {}
        self._listeners: set[asyncio.Queue] = set()
        self._snapshot_providers: list[Callable[[], dict]] = []
        self._tasks: set[asyncio.Task] = set()
        self._expiry_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        self.history = AlertHistory(self.s.alert_history_store)
        self.channels = build_channels(self.s)
        self._outbox: Optional[Outbox] = None  # made on first send (it needs the running loop and the client)
        self._images: "OrderedDict[str, bytes]" = OrderedDict()
        hist = store_path(self.s.alert_history_store)
        self._snap_dir = hist.parent / "snapshots" if hist is not None else None
        self._log_filter = _RedactFilter(self.channels)
        logging.getLogger("httpx").addFilter(self._log_filter)
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0),
                                                   headers={"User-Agent": "agentic-charts/1.0"})
        self._load()

    @property
    def channel_status(self) -> dict[str, bool]:
        names = {c.name for c in self.channels}
        return {"telegram": "telegram" in names, "discord": "discord" in names}

    async def start(self) -> None:
        self.expire_due()  # alerts that ran out while the server was down
        await self._sync()
        if self._expiry_task is None:
            jobs.declare("alerts", "Price alerts", EXPIRY_CHECK_SECONDS)
            self._expiry_task = asyncio.create_task(self._expiry_loop(), name="alerts:expiry")

    async def close(self) -> None:
        self._closed = True
        if self._expiry_task:
            self._expiry_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._expiry_task
        async with self._lock:
            watches = list(self._watches.items())
            self._watches.clear()
        for sym, (queue, task) in watches:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            await self.hub.unsubscribe(sym, INTERVAL, queue)
        if self._tasks:  # let in-flight notifications finish briefly
            await asyncio.wait(set(self._tasks), timeout=3)
        if self._outbox is not None:
            await self._outbox.close()
        for t in list(self._tasks):
            t.cancel()
        await self._client.aclose()
        logging.getLogger("httpx").removeFilter(self._log_filter)

    # -------------------------------------------------------------- CRUD
    def list(self) -> list[PriceAlert]:
        return list(self._alerts.values())

    async def add(self, symbol: str, specs: list[AlertSpec]) -> list[PriceAlert]:
        specs = [_valid_spec(s) for s in specs]
        overflow = len(self._alerts) + len(specs) - MAX_ALERTS
        if overflow > 0:  # make room by dropping the oldest triggered alerts
            triggered = sorted((a for a in self._alerts.values() if not a.armed), key=lambda a: a.created_at)
            if overflow > len(triggered):
                raise ValueError(f"At most {MAX_ALERTS} alerts; delete some first")
            for a in triggered[:overflow]:
                del self._alerts[a.id]
        now = int(time.time() * 1000)
        created = [PriceAlert(**s.model_dump(), id=uuid.uuid4().hex[:10], symbol=symbol.upper(), armed=True,
                              created_at=now) for s in specs]
        for a in created:
            self._alerts[a.id] = a
        self._save()
        self._changed()
        await self._sync()
        return created

    async def update(self, alert_id: str, patch: AlertPatch) -> Optional[PriceAlert]:
        """Edit an alert (see apply_patch) → the new alert, or None when it does not exist. Raises ValueError
        for an invalid edit."""
        a = self._alerts.get(alert_id)
        if a is None:
            return None
        a = apply_patch(a, patch)
        self._alerts[alert_id] = a
        self._save()
        self._changed()
        await self._sync()
        return a

    async def remove(self, alert_id: str) -> bool:
        if self._alerts.pop(alert_id, None) is None:
            return False
        self._save()
        self._changed()
        await self._sync()
        return True

    async def rearm(self, alert_id: str) -> Optional[PriceAlert]:
        a = self._alerts.get(alert_id)
        if a is None:
            return None
        update: dict = {"armed": True, "expired": False, "last_side": None, "triggered_at": None,
                        "triggered_price": None}
        if a.expires_at is not None and a.expires_at <= int(time.time() * 1000):
            update["expires_at"] = None  # re-arming an expired alert keeps it until it fires or is deleted
        a = a.model_copy(update=update)
        self._alerts[alert_id] = a
        self._save()
        self._changed()
        await self._sync()
        return a

    async def clear_triggered(self) -> int:
        gone = [k for k, a in self._alerts.items() if not a.armed]
        for k in gone:
            del self._alerts[k]
        if gone:
            self._save()
            self._changed()
        return len(gone)

    def expire_due(self, now_ms: Optional[int] = None) -> list[PriceAlert]:
        """Disarm every armed alert whose `expires_at` has passed → the ones that expired."""
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        expired = [a.model_copy(update={"armed": False, "expired": True}) for a in self._alerts.values()
                   if a.armed and a.expires_at is not None and a.expires_at <= now]
        if not expired:
            return []
        for a in expired:
            self._alerts[a.id] = a
            log.info("Alert expired: %s %s", a.symbol, a.label or a.id)
        self._save()
        self._changed()
        if not self._closed:
            self._spawn(self._sync())
        return expired

    async def _expiry_loop(self) -> None:
        while True:
            await asyncio.sleep(EXPIRY_CHECK_SECONDS)
            try:
                self.expire_due()
                jobs.ok("alerts", f"{len(self._alerts)} alerts, watching {len(self._watches)} coins")
            except Exception as exc:
                jobs.fail("alerts", exc)
                log.exception("Alert expiry check failed")

    # ----------------------------------------------------- notifications
    async def send_test(self) -> dict[str, bool]:
        return await send_all(self._client, self.channels,
                              "Agentic Charts: test notification. Price alerts from this backend will arrive here.")

    @property
    def outbox(self) -> Outbox:
        if self._outbox is None:
            self._outbox = Outbox(self._client, self.channels)
        return self._outbox

    async def send_text(self, text: str) -> dict[str, bool]:
        """Send any text (a signal alert, the brief) to every configured channel, split to each channel's
        message limit and queued in order (notify.Outbox) → {channel: delivered}. Empty when no channel is
        configured."""
        return await self.outbox.send(text) if self.channels else {}

    async def send_notice(self, notice: Notice) -> dict[str, bool]:
        """Send a card (notify.Notice) to every channel → {channel: delivered}."""
        if not self.channels:
            return {}
        if not notice.url and notice.symbol:
            notice.url = app_link(self.s.public_app_url, notice.symbol, notice.interval)
        return await self.outbox.send(notice)

    def notify(self, text: str) -> None:
        """Fire-and-forget send_text, for use from stream callbacks."""
        if self.channels and not self._closed:
            self._spawn(self.send_text(text))

    def notify_notice(self, notice: Notice) -> None:
        """Fire-and-forget send_notice."""
        if self.channels and not self._closed:
            self._spawn(self.send_notice(notice))

    def attach_image(self, history_id: str, png: bytes) -> None:
        """Keep a fire's chart snapshot (the last MAX_SNAPSHOTS) for the History tab."""
        if not self.history.mark_image(history_id):
            return
        self._images[history_id] = png
        while len(self._images) > MAX_SNAPSHOTS:
            old, _ = self._images.popitem(last=False)
            if self._snap_dir is not None:
                with contextlib.suppress(OSError):
                    (self._snap_dir / f"{old}.png").unlink()
        if self._snap_dir is not None:
            with contextlib.suppress(OSError):
                self._snap_dir.mkdir(parents=True, exist_ok=True)
                (self._snap_dir / f"{history_id}.png").write_bytes(png)

    def history_image(self, history_id: str) -> Optional[bytes]:
        if history_id in self._images:
            return self._images[history_id]
        if self._snap_dir is not None and all(c.isalnum() for c in history_id):
            path = self._snap_dir / f"{history_id}.png"
            if path.exists():
                return path.read_bytes()
        return None

    def delivery_stats(self) -> dict[str, dict]:
        """Per channel: last delivered, last failure and its error, sent and failed in the last 24 h."""
        return {c.name: (self._outbox.stats[c.name].snapshot() if self._outbox else ChannelStats().snapshot())
                for c in self.channels}

    def record(self, kind: HistoryKind, symbol: str, title: str, text: str, price: Optional[float] = None,
               alert_id: Optional[str] = None, time_ms: Optional[int] = None) -> dict:
        """Append a fire to the history and push it to every `/ws/alerts` listener as {type: "history", item}."""
        item = self.history.add(kind, symbol, title, text, price=price, alert_id=alert_id, time_ms=time_ms)
        self.broadcast({"type": "history", "item": item})
        return item

    # --------------------------------------------------------- listeners
    def subscribe(self) -> asyncio.Queue:
        """A queue of `/ws/alerts` messages, starting with the price-alert snapshot and every registered
        provider's snapshot (signal alerts)."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=LISTENER_QUEUE_SIZE)
        queue.put_nowait(self._snapshot())
        for provider in self._snapshot_providers:
            try:
                queue.put_nowait(provider())
            except Exception:
                log.exception("Snapshot provider failed")
        self._listeners.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._listeners.discard(queue)

    def add_snapshot_provider(self, provider: Callable[[], dict]) -> None:
        """`provider()` → a message every new `/ws/alerts` listener receives after the price-alert snapshot."""
        self._snapshot_providers.append(provider)

    def _snapshot(self) -> dict:
        return {"type": "snapshot", "alerts": [a.model_dump() for a in self._alerts.values()]}

    def broadcast(self, msg: dict) -> None:
        """Push `msg` to every `/ws/alerts` listener."""
        for q in list(self._listeners):
            if q.full():  # slow consumer: drop its oldest message rather than block
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            q.put_nowait(msg)

    _broadcast = broadcast

    def _changed(self) -> None:
        self.broadcast(self._snapshot())

    # ---------------------------------------------------------- watching
    async def _sync(self) -> None:
        """Hold exactly one hub subscription per symbol that has armed alerts."""
        async with self._lock:
            if self._closed:
                return
            want = {a.symbol for a in self._alerts.values() if a.armed}
            for sym in [s for s in self._watches if s not in want]:
                queue, task = self._watches.pop(sym)
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
                await self.hub.unsubscribe(sym, INTERVAL, queue)
            for sym in sorted(want - self._watches.keys()):
                queue = await self.hub.subscribe(sym, INTERVAL)
                self._watches[sym] = (queue, asyncio.create_task(self._watch(sym, queue), name=f"alerts:{sym}"))

    async def _watch(self, symbol: str, queue: asyncio.Queue) -> None:
        while True:
            msg = await queue.get()
            if msg.get("type") != "kline" or not msg.get("candle"):
                continue
            source = msg.get("source", "")
            if source == "synthetic" and self.s.data_source != "synthetic":
                continue  # fallback feed during a Binance outage: not a real price
            try:
                self.on_price(symbol, float(msg["candle"]["close"]), source)
            except Exception:  # never let one bad message kill the watcher
                log.exception("Alert evaluation failed for %s", symbol)

    def on_price(self, symbol: str, price: float, source: str = "binance",
                 now_ms: Optional[int] = None) -> list[PriceAlert]:
        """Evaluate every armed alert on `symbol` against `price`; returns the ones that fired."""
        fired: list[PriceAlert] = []
        changed = disarmed = False
        for a in list(self._alerts.values()):
            if a.symbol != symbol or not a.armed:
                continue
            new, did_fire = evaluate(a, price, now_ms)
            if new is not a:
                self._alerts[a.id] = new
                changed = True
                disarmed = disarmed or not new.armed
            if did_fire:
                fired.append(new)
        if not changed:
            return fired
        self._save()
        for a in fired:
            text = describe_fire(a, price)
            log.info("Alert fired: %s", text)
            self.broadcast({"type": "fired", "alert": a.model_dump(), "price": price})
            self.record("price", a.symbol, a.label or "Price alert", text, price=price, alert_id=a.id,
                        time_ms=a.triggered_at)
        self._changed()
        for a in fired if self.channels else []:
            text = describe_fire(a, price)
            self.notify_notice(Notice(
                kind="price", title=f"{a.symbol} price alert" + (f": {a.label}" if a.label else ""),
                text=text + (" (synthetic demo data)" if source == "synthetic" else ""), symbol=a.symbol,
                interval=INTERVAL, description=text, side="info", demo=source == "synthetic",
                url=app_link(self.s.public_app_url, a.symbol, "", f"alert:{a.id}"),
                fields=[("Price", fmt_price(price), True)] + ([("Note", a.note, False)] if a.note else [])))
        if disarmed:
            self._spawn(self._sync())  # drop the subscription if nothing is armed on this symbol any more
        return fired

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------- persistence
    def _load(self) -> None:
        if not self._store or not self._store.exists():
            return
        try:
            rows = json.loads(self._store.read_text()).get("alerts", [])
        except (OSError, ValueError, AttributeError) as exc:
            log.warning("Could not read %s (%s); starting with no alerts", self._store, exc)
            return
        for row in rows:
            try:
                a = PriceAlert.model_validate(row)
            except ValueError as exc:
                log.warning("Skipping invalid stored alert: %s", exc)
                continue
            self._alerts[a.id] = a
        if self._alerts:
            log.info("Loaded %d price alerts (%d armed)", len(self._alerts),
                     sum(a.armed for a in self._alerts.values()))

    def _save(self) -> None:
        write_store(self._store, {"alerts": [a.model_dump() for a in self._alerts.values()]})
