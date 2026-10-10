"""Notifications as cards, delivered reliably.

A Notice is what happened (a price alert, a signal, a desk call, trade advice) with its levels as fields, a side
(buy: green, sell: red, info: grey), an optional chart image and a link back into the app. Discord gets an embed
(several notices queued within BATCH_WAIT go out as one post of up to 10 embeds), Telegram an HTML message (or a
photo with a caption). Plain texts (the brief, a scan) still go out as text.

Every channel has its own queue and worker, so a burst at a candle close goes out in order: a 429 waits for the
channel's retry-after and tries again (up to RETRIES times), a 5xx backs off 1, 2, 4 s, and anything else is a failure.
Each channel keeps delivery stats for the status panel.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Optional, Union
from urllib.parse import urlencode

import httpx

if TYPE_CHECKING:
    from .alerts import Channel

log = logging.getLogger(__name__)

Side = Literal["buy", "sell", "info"]
COLORS = {"buy": 0x22C55E, "sell": 0xEF4444, "info": 0x64748B}
BATCH_WAIT = 2.0          # seconds a Discord worker waits to put more notices in the same post
MAX_EMBEDS = 10
MAX_POST_CHARS = 6000
RETRIES = 3
BACKOFF = (1.0, 2.0, 4.0)
MAX_RETRY_AFTER = 60.0


@dataclass
class Notice:
    kind: str                   # price | signal | trigger | desk | trade | metric
    title: str
    text: str                   # the plain-text version (the old message)
    symbol: str = ""
    interval: str = ""
    description: str = ""
    fields: list[tuple[str, str, bool]] = field(default_factory=list)  # (name, value, inline)
    side: Side = "info"
    url: str = ""
    image: Optional[bytes] = None
    demo: bool = False


def app_link(base: str, symbol: str, interval: str = "", focus: str = "") -> str:
    """A link that opens the app on this chart (and tab item), or "" when PUBLIC_APP_URL isn't set."""
    if not base:
        return ""
    q = {"symbol": symbol, **({"tf": interval} if interval else {}), **({"focus": focus} if focus else {})}
    return f"{base.rstrip('/')}/?{urlencode(q)}"


def embed(n: Notice, image_name: Optional[str] = None) -> dict:
    title = ("[DEMO DATA] " if n.demo else "") + n.title
    out: dict[str, Any] = {"title": title[:256], "description": (n.description or n.text)[:4096],
                           "color": COLORS["info"] if n.demo else COLORS[n.side],
                           "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if n.url:
        out["url"] = n.url
    if n.fields:
        out["fields"] = [{"name": k[:256], "value": (v or "–")[:1024], "inline": inline} for k, v, inline in n.fields[:25]]
    if n.symbol:
        out["footer"] = {"text": f"{n.symbol} {n.interval}".strip()}
    if image_name:
        out["image"] = {"url": f"attachment://{image_name}"}
    return out


def discord_post(notices: list[Notice]) -> tuple[dict, list[tuple[str, bytes]]]:
    """One Discord post for these notices → (payload, files to attach)."""
    files: list[tuple[str, bytes]] = []
    embeds = []
    for i, n in enumerate(notices[:MAX_EMBEDS]):
        name = None
        if n.image:
            name = f"chart{i}.png"
            files.append((name, n.image))
        embeds.append(embed(n, name))
    return {"embeds": embeds, "allowed_mentions": {"parse": []}}, files


def telegram_html(n: Notice) -> str:
    e = html.escape
    title = ("[DEMO DATA] " if n.demo else "") + n.title
    lines = [f"<b>{e(title)}</b>", e(n.description or n.text)]
    lines += [f"{e(k)}: {e(v)}" for k, v, _ in n.fields]
    if n.url:
        lines.append(f'<a href="{e(n.url, quote=True)}">Open in the app</a>')
    return "\n".join(lines)


def _size(notices: list[Notice]) -> int:
    return sum(len(n.title) + len(n.description or n.text) + sum(len(k) + len(v) for k, v, _ in n.fields)
               for n in notices)


Item = Union[str, Notice]


@dataclass
class _Job:
    item: Item
    done: asyncio.Future


class ChannelStats:
    def __init__(self) -> None:
        self.last_ok: Optional[float] = None
        self.last_fail: Optional[float] = None
        self.last_error: Optional[str] = None
        self._ok: deque[float] = deque()
        self._fail: deque[float] = deque()

    def ok(self) -> None:
        self.last_ok = time.time()
        self._ok.append(self.last_ok)

    def fail(self, error: str) -> None:
        self.last_fail, self.last_error = time.time(), error[:200]
        self._fail.append(self.last_fail)

    def snapshot(self) -> dict:
        cut = time.time() - 86400
        for d in (self._ok, self._fail):
            while d and d[0] < cut:
                d.popleft()
        return {"last_ok": self.last_ok, "last_fail": self.last_fail, "last_error": self.last_error,
                "sent_24h": len(self._ok), "failed_24h": len(self._fail)}


class Outbox:
    """A queue and a worker per channel; `send` resolves once the item was delivered (True) or given up (False)."""

    def __init__(self, client: httpx.AsyncClient, channels: list["Channel"], batch_wait: float = BATCH_WAIT) -> None:
        self.client, self.channels, self.batch_wait = client, channels, batch_wait
        self._queues: dict[str, asyncio.Queue[_Job]] = {}
        self._workers: dict[str, asyncio.Task] = {}
        self.stats: dict[str, ChannelStats] = {c.name: ChannelStats() for c in channels}

    def pending(self) -> int:
        return sum(q.qsize() for q in self._queues.values())

    async def send(self, item: Item) -> dict[str, bool]:
        """Queue `item` on every channel → {channel: delivered}."""
        loop = asyncio.get_running_loop()
        futs = {}
        for ch in self.channels:
            if ch.name not in self._workers or self._workers[ch.name].done():
                self._queues[ch.name] = asyncio.Queue()
                self._workers[ch.name] = loop.create_task(self._work(ch), name=f"notify:{ch.name}")
            fut = loop.create_future()
            parts = [item] if isinstance(item, Notice) else _split(item, ch.limit)
            for i, part in enumerate(parts):
                # Only the last part of a long text resolves the future: in order, so all parts went before it.
                self._queues[ch.name].put_nowait(_Job(part, fut if i == len(parts) - 1 else loop.create_future()))
            futs[ch.name] = fut
        return {name: await f for name, f in futs.items()}

    async def close(self) -> None:
        for t in self._workers.values():
            t.cancel()
        for t in self._workers.values():
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    async def _work(self, ch: "Channel") -> None:
        q = self._queues[ch.name]
        while True:
            job = await q.get()
            batch = [job]
            if ch.name == "discord" and isinstance(job.item, Notice):
                # Notices that arrive together (a candle close) go out as one post.
                deadline = time.monotonic() + self.batch_wait
                while len(batch) < MAX_EMBEDS:
                    if not q.empty() and not isinstance(q._queue[0].item, Notice):  # type: ignore[attr-defined]
                        break
                    left = deadline - time.monotonic()
                    if left <= 0 and q.empty():
                        break
                    try:
                        nxt = await asyncio.wait_for(q.get(), max(left, 0.001))
                    except asyncio.TimeoutError:
                        break
                    batch.append(nxt)
                    if _size([j.item for j in batch]) > MAX_POST_CHARS:  # type: ignore[misc]
                        break
            ok = await self._deliver(ch, [j.item for j in batch])
            for j in batch:
                if not j.done.done():
                    j.done.set_result(ok)

    async def _deliver(self, ch: "Channel", items: list[Item]) -> bool:
        stats = self.stats.setdefault(ch.name, ChannelStats())
        error = ""
        for attempt in range(RETRIES + 1):
            try:
                resp = await self._post(ch, items)
            except httpx.HTTPError as exc:
                error = type(exc).__name__
                if attempt < RETRIES:
                    await asyncio.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
                    continue
                break
            if resp.status_code < 300:
                stats.ok()
                return True
            error = f"HTTP {resp.status_code}"
            if resp.status_code == 429 and attempt < RETRIES:
                await asyncio.sleep(_retry_after(resp))
                continue
            if resp.status_code >= 500 and attempt < RETRIES:
                await asyncio.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
                continue
            break
        stats.fail(error)
        log.warning("Notification via %s failed: %s", ch.name, error)
        return False

    async def _post(self, ch: "Channel", items: list[Item]) -> httpx.Response:
        first = items[0]
        if isinstance(first, str):
            return await self.client.post(ch.url, json=ch.body(first))
        notices = [i for i in items if isinstance(i, Notice)]
        if ch.name == "discord":
            payload, files = discord_post(notices)
            if files:
                return await self.client.post(ch.url, data={"payload_json": json.dumps(payload)},
                                              files={f"files[{i}]": (name, img, "image/png")
                                                     for i, (name, img) in enumerate(files)})
            return await self.client.post(ch.url, json=payload)
        # Telegram (and anything else): one notice, as HTML; with a chart as a photo and the text as its caption.
        n = notices[0]
        body = ch.body("")
        text = telegram_html(n)
        if n.image and ch.url.endswith("/sendMessage"):
            r = await self.client.post(ch.url.removesuffix("/sendMessage") + "/sendPhoto",
                                       data={"chat_id": body.get("chat_id"), "caption": text[:1024], "parse_mode": "HTML"},
                                       files={"photo": ("chart.png", n.image, "image/png")})
            if r.status_code >= 300 or len(text) <= 1024:
                return r
        return await self.client.post(ch.url, json={**body, "text": text[:4000], "parse_mode": "HTML"})


def _retry_after(resp: httpx.Response) -> float:
    """Seconds a 429 asks to wait: Discord's JSON retry_after, Telegram's parameters.retry_after, or the header."""
    wait: Any = None
    try:
        data = resp.json()
        wait = data.get("retry_after") or (data.get("parameters") or {}).get("retry_after")
    except (ValueError, AttributeError):
        pass
    if wait is None:
        wait = resp.headers.get("Retry-After", 1)
    try:
        return min(MAX_RETRY_AFTER, max(0.0, float(wait)))
    except (TypeError, ValueError):
        return 1.0


def _split(text: str, limit: int) -> list[str]:
    from .alerts import split_message

    return split_message(text, limit) or [""]
