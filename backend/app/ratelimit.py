"""Per-client rate limits, so a public URL can't run up LLM costs or hammer Binance.

A token bucket per (client, rule): the agent endpoint gets a tight budget (it may call an LLM several
times), everything else under /api a loose one. AGENT_DAILY_LIMIT optionally caps agent requests per UTC
day across all clients. Behind a reverse proxy set TRUST_PROXY=1 so the client is taken from
X-Forwarded-For instead of the proxy's address. WebSockets are not limited here.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

from starlette.types import ASGIApp, Receive, Scope, Send


def parse_rate(spec: str) -> tuple[float, float] | None:
    """'20/minute' → (capacity 20, refill 20/60 per second). '0' or 'off' disables."""
    m = re.fullmatch(r"\s*(\d+)\s*/\s*(s|sec|second|m|min|minute|h|hour|d|day)\s*", spec or "")
    if not m or int(m.group(1)) == 0:
        return None
    seconds = {"s": 1, "sec": 1, "second": 1, "m": 60, "min": 60, "minute": 60, "h": 3600, "hour": 3600,
               "d": 86400, "day": 86400}[m.group(2)]
    n = float(m.group(1))
    return n, n / seconds


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    def __init__(self, capacity: float, refill_per_s: float, max_clients: int = 10_000) -> None:
        self.capacity, self.refill = capacity, refill_per_s
        self._buckets: dict[str, _Bucket] = {}
        self._max = max_clients

    def take(self, key: str, now: float | None = None) -> float:
        """0 when allowed, else seconds until a token is available."""
        now = time.monotonic() if now is None else now
        b = self._buckets.get(key)
        if b is None:
            if len(self._buckets) >= self._max:  # drop the stalest client rather than grow without bound
                self._buckets.pop(min(self._buckets, key=lambda k: self._buckets[k].updated))
            b = self._buckets[key] = _Bucket(self.capacity, now)
        b.tokens = min(self.capacity, b.tokens + (now - b.updated) * self.refill)
        b.updated = now
        if b.tokens >= 1:
            b.tokens -= 1
            return 0.0
        return (1 - b.tokens) / self.refill


class DailyCap:
    def __init__(self, limit: int) -> None:
        self.limit, self.day, self.count = limit, "", 0

    def take(self) -> bool:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if day != self.day:
            self.day, self.count = day, 0
        if self.count >= self.limit:
            return False
        self.count += 1
        return True


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp, agent_rate: str = "20/minute", api_rate: str = "300/minute",
                 agent_daily: int = 0, trust_proxy: int = 0) -> None:
        self.app = app
        agent, api = parse_rate(agent_rate), parse_rate(api_rate)
        self.agent = RateLimiter(*agent) if agent else None
        self.api = RateLimiter(*api) if api else None
        self.daily = DailyCap(agent_daily) if agent_daily > 0 else None
        self.trust_proxy = int(trust_proxy)  # number of reverse proxies in front of the API

    def _client(self, scope: Scope) -> str:
        if self.trust_proxy:
            # Each proxy appends the address it saw, so count from the right: entries further left
            # come from the client and can be forged.
            hops = [h.strip() for name, value in scope.get("headers", []) if name == b"x-forwarded-for"
                    for h in value.decode("latin-1").split(",") if h.strip()]
            if hops:
                return hops[-min(self.trust_proxy, len(hops))]
        client = scope.get("client")
        return client[0] if client else "unknown"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/") or scope["method"] == "OPTIONS":
            await self.app(scope, receive, send)
            return
        client = self._client(scope)
        is_agent = scope["path"] == "/api/agent/analyze"
        limiter = self.agent if is_agent else self.api
        wait = limiter.take(client) if limiter else 0.0
        if wait:
            await _reject(send, f"Too many requests; try again in {wait:.0f}s.", wait)
            return
        if is_agent and self.daily and not self.daily.take():
            await _reject(send, "The agent's daily request budget is used up; it resets at 00:00 UTC.", 3600)
            return
        await self.app(scope, receive, send)


async def _reject(send: Send, detail: str, retry_after: float) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send({"type": "http.response.start", "status": 429, "headers": [
        (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
        (b"retry-after", str(max(1, int(retry_after + 0.999))).encode())]})
    await send({"type": "http.response.body", "body": body})
