"""Server-side price alerts, so they fire with every browser tab closed.

Alerts live in a small JSON file (ALERTS_STORE). For each symbol with armed
alerts the service holds one 1m subscription on the StreamHub (sharing the
upstream Binance stream with any open charts) and checks every kline update's
close against the alerts. A fired alert is disarmed, saved, pushed to Telegram /
Discord if configured, and broadcast to `/ws/alerts` listeners for the in-app
toast. Prices from the synthetic fallback feed are ignored unless
DATA_SOURCE=synthetic, so a Binance outage never sends a notification for a
made-up price.
"""

from __future__ import annotations

import asyncio
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

from .config import Settings, get_settings
from .schemas import AlertSpec, PriceAlert

if TYPE_CHECKING:
    from .stream_hub import StreamHub

log = logging.getLogger(__name__)

INTERVAL = "1m"
MAX_ALERTS = 200
LISTENER_QUEUE_SIZE = 64
Side = Literal["above", "below", "inside"]


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
    Returns the same object when nothing changed. Port of `evaluateAlert` in frontend/lib/alerts.ts."""
    if not alert.armed or not math.isfinite(price):
        return alert, False
    side = _side(alert, price)
    prev = alert.last_side
    fired = False
    if prev and prev != side:
        fired = alert.kind == "cross" or side == "inside" or prev != "inside"  # above→below skips over the zone
    if fired:
        return alert.model_copy(update={
            "armed": False, "last_side": side, "triggered_price": price,
            "triggered_at": now_ms if now_ms is not None else int(time.time() * 1000),
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
    return f"{alert.symbol}: price {what}{label} at {fmt_price(price)}"


def _valid_spec(spec: AlertSpec) -> AlertSpec:
    def ok(p: Optional[float]) -> bool:
        return p is not None and math.isfinite(p) and p > 0

    if spec.kind == "cross":
        if not ok(spec.price):
            raise ValueError("a cross alert needs a positive price")
        return spec
    if not (ok(spec.price_low) and ok(spec.price_high)):
        raise ValueError("a zone alert needs positive price_low and price_high")
    if spec.price_low > spec.price_high:  # type: ignore[operator]
        return spec.model_copy(update={"price_low": spec.price_high, "price_high": spec.price_low})
    return spec


# --------------------------------------------------------------- channels --


@dataclass(frozen=True)
class Channel:
    """A notification target: POST `body(text)` as JSON to `url`. The URL holds the secret."""

    name: str
    url: str
    body: Callable[[str], dict]
    secrets: tuple[str, ...]

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
        out.append(Channel("discord", url, lambda text: {"content": text[:2000]},
                           (url, token) if len(token) >= 8 else (url,)))
    return out


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


# ---------------------------------------------------------------- service --


class AlertService:
    def __init__(self, hub: StreamHub, settings: Settings | None = None,
                 client: httpx.AsyncClient | None = None) -> None:
        self.hub = hub
        self.s = settings or get_settings()
        store = self.s.alerts_store.strip()
        self._store = None if store.lower() in ("memory", "none", "off") else Path(store)
        self._alerts: dict[str, PriceAlert] = {}
        self._watches: dict[str, tuple[asyncio.Queue, asyncio.Task]] = {}
        self._listeners: set[asyncio.Queue] = set()
        self._tasks: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()
        self._closed = False
        self.channels = build_channels(self.s)
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
        await self._sync()

    async def close(self) -> None:
        self._closed = True
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
        a = a.model_copy(update={"armed": True, "last_side": None, "triggered_at": None, "triggered_price": None})
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

    async def send_test(self) -> dict[str, bool]:
        return await send_all(self._client, self.channels,
                              "Agentic Charts: test notification. Price alerts from this backend will arrive here.")

    # --------------------------------------------------------- listeners
    def subscribe(self) -> asyncio.Queue:
        """A queue of `snapshot` / `fired` messages, starting with the current snapshot."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=LISTENER_QUEUE_SIZE)
        queue.put_nowait(self._snapshot())
        self._listeners.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._listeners.discard(queue)

    def _snapshot(self) -> dict:
        return {"type": "snapshot", "alerts": [a.model_dump() for a in self._alerts.values()]}

    def _broadcast(self, msg: dict) -> None:
        for q in list(self._listeners):
            if q.full():  # slow consumer: drop its oldest message rather than block
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            q.put_nowait(msg)

    def _changed(self) -> None:
        self._broadcast(self._snapshot())

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

    def on_price(self, symbol: str, price: float, source: str = "binance") -> list[PriceAlert]:
        """Evaluate every armed alert on `symbol` against `price`; returns the ones that fired."""
        fired: list[PriceAlert] = []
        changed = False
        for a in list(self._alerts.values()):
            if a.symbol != symbol or not a.armed:
                continue
            new, did_fire = evaluate(a, price)
            if new is not a:
                self._alerts[a.id] = new
                changed = True
            if did_fire:
                fired.append(new)
        if not changed:
            return fired
        self._save()
        for a in fired:
            log.info("Alert fired: %s", describe_fire(a, price))
            self._broadcast({"type": "fired", "alert": a.model_dump(), "price": price})
        self._changed()
        if fired:
            if self.channels:
                lines = [describe_fire(a, price) for a in fired]
                if source == "synthetic":
                    lines.append("(synthetic demo data)")
                self._spawn(send_all(self._client, self.channels, "\n".join(lines)))
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
        if not self._store:
            return
        try:
            self._store.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._store.with_suffix(".tmp")
            tmp.write_text(json.dumps({"alerts": [a.model_dump() for a in self._alerts.values()]}))
            tmp.replace(self._store)
        except OSError as exc:
            log.warning("Could not save alerts to %s: %s", self._store, exc)
