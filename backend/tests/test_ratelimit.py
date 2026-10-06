import asyncio

from app.ratelimit import DailyCap, RateLimiter, RateLimitMiddleware, parse_rate


def test_parse_rate():
    assert parse_rate("20/minute") == (20.0, 20 / 60)
    assert parse_rate("5/s") == (5.0, 5.0)
    assert parse_rate("0") is None and parse_rate("off") is None and parse_rate("0/minute") is None


def test_token_bucket_refills():
    rl = RateLimiter(2, 1.0)
    assert rl.take("a", now=0) == 0 and rl.take("a", now=0) == 0
    assert rl.take("a", now=0) > 0  # empty
    assert rl.take("b", now=0) == 0  # other clients unaffected
    assert rl.take("a", now=1.01) == 0  # one token back after a second


def test_daily_cap():
    cap = DailyCap(2)
    assert cap.take() and cap.take() and not cap.take()


def _call(mw, path="/api/agent/analyze", ip="1.2.3.4", xff=None):
    sent = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw.app = app
    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    scope = {"type": "http", "path": path, "method": "POST", "client": (ip, 1), "headers": headers}

    async def send(msg):
        sent.append(msg)

    asyncio.run(mw(scope, None, send))
    return sent[0]["status"], dict(sent[0]["headers"])


def test_middleware_limits_agent_and_trusts_proxy_only_when_told():
    mw = RateLimitMiddleware(None, agent_rate="2/minute", api_rate="100/minute")
    assert _call(mw)[0] == 200 and _call(mw)[0] == 200
    status, headers = _call(mw)
    assert status == 429 and int(headers[b"retry-after"]) >= 1
    assert _call(mw, path="/api/klines")[0] == 200  # separate, looser budget
    assert _call(mw, xff="9.9.9.9")[0] == 429  # header ignored without TRUST_PROXY

    proxied = RateLimitMiddleware(None, agent_rate="1/minute", trust_proxy=1)
    assert _call(proxied, xff="5.5.5.5")[0] == 200
    assert _call(proxied, xff="10.0.0.1, 6.6.6.6")[0] == 200
    assert _call(proxied, xff="5.5.5.5")[0] == 429
    # A forged left-hand entry does not buy a fresh budget: the proxy's own entry (rightmost) counts.
    assert _call(proxied, xff="7.7.7.7, 5.5.5.5")[0] == 429

    two_hops = RateLimitMiddleware(None, agent_rate="1/minute", trust_proxy=2)
    assert _call(two_hops, xff="1.1.1.1, 8.8.8.8, 172.16.0.2")[0] == 200
    assert _call(two_hops, xff="2.2.2.2, 8.8.8.8, 172.16.0.3")[0] == 429  # same client behind a CDN
