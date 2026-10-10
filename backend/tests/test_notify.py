"""Alert cards: Discord embeds (with the chart attached) and Telegram HTML, a queue per channel that waits out a 429,
batches a burst into one post and counts failures; the chart renderer."""

import asyncio
import json

import httpx

from app import notify
from app.alerts import Channel, build_channels
from app.chart_render import render, snapshot
from app.config import Settings
from app.market_data import candles_to_df, synthetic_klines
from app.notify import Notice, Outbox, app_link, discord_post, telegram_html

import dataclasses

WEBHOOK = "https://discord.com/api/webhooks/1/abcdefghij"


def _channels(**kw):
    s = dataclasses.replace(Settings(), telegram_bot_token=kw.get("tg", ""), telegram_chat_id="42",
                            discord_webhook_url=kw.get("dc", ""))
    return build_channels(s)


def _notice(**kw):
    base = dict(kind="signal", title="INJUSDT H4: Lost support", text="INJUSDT 4h: lost support", symbol="INJUSDT",
                interval="4h", side="sell", fields=[("Close", "7.40", True), ("Long", "x" * 2000, False)])
    return Notice(**{**base, **kw})


def test_discord_embed_colour_fields_and_no_mentions():
    payload, files = discord_post([_notice()])
    e = payload["embeds"][0]
    assert e["color"] == 0xEF4444 and payload["allowed_mentions"] == {"parse": []} and not files
    assert len(e["fields"][1]["value"]) == 1024
    demo, _ = discord_post([_notice(demo=True)])
    assert demo["embeds"][0]["title"].startswith("[DEMO DATA]") and demo["embeds"][0]["color"] == 0x64748B
    with_img, files = discord_post([_notice(image=b"\x89PNG..")])
    assert with_img["embeds"][0]["image"]["url"] == "attachment://chart0.png" and files[0][0] == "chart0.png"


def test_telegram_escapes_and_links():
    t = telegram_html(_notice(text="a <b> c", description="", url="http://box:3000/?symbol=INJUSDT"))
    assert "a &lt;b&gt; c" in t and '<a href="http://box:3000/?symbol=INJUSDT">' in t


def test_app_link_only_with_a_public_url():
    assert app_link("", "INJUSDT", "4h") == ""
    assert app_link("http://box:3000/", "INJUSDT", "4h", "desk:abc") == \
        "http://box:3000/?symbol=INJUSDT&tf=4h&focus=desk%3Aabc"


def _run(handler, channels, items, batch_wait=0.05):
    async def go():
        box = Outbox(httpx.AsyncClient(transport=httpx.MockTransport(handler)), channels, batch_wait=batch_wait)
        try:
            return await asyncio.gather(*(box.send(i) for i in items)), box.stats
        finally:
            await box.close()
    return asyncio.run(go())


def test_a_429_waits_and_retries_in_order(monkeypatch):
    seen = []

    def handler(req):
        seen.append(req)
        if len(seen) == 1:
            return httpx.Response(429, json={"retry_after": 0.01})
        return httpx.Response(204)

    results, stats = _run(handler, _channels(dc=WEBHOOK), ["first", "second"])
    assert results == [{"discord": True}, {"discord": True}]
    assert [json.loads(r.content)["content"] for r in seen] == ["first", "first", "second"]
    assert stats["discord"].snapshot()["sent_24h"] == 2


def test_a_burst_of_notices_is_one_discord_post_and_has_the_chart():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(204)

    results, _ = _run(handler, _channels(dc=WEBHOOK), [_notice(), _notice(image=b"\x89PNGdata"), _notice()])
    assert all(r == {"discord": True} for r in results) and len(seen) == 1
    body = seen[0].content
    assert b"payload_json" in body and b"\x89PNGdata" in body and body.count(b'"title"') == 3


def test_a_dead_webhook_is_a_counted_failure(monkeypatch):
    monkeypatch.setattr(notify, "BACKOFF", (0.0, 0.0, 0.0))
    results, stats = _run(lambda req: httpx.Response(404), _channels(dc=WEBHOOK), ["hello"])
    snap = stats["discord"].snapshot()
    assert results == [{"discord": False}] and snap["failed_24h"] == 1 and snap["last_error"] == "HTTP 404"


def test_render_is_a_png_of_the_size_and_cached():
    df = candles_to_df(synthetic_klines("INJUSDT", "4h", 150))
    png = render(df, lines=[(float(df["close"].iloc[-1]) * 1.05, (34, 197, 94), True, "TP")], title="t",
                 width=640, height=320)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and int.from_bytes(png[16:20], "big") == 640
    assert png == render(df, lines=[(float(df["close"].iloc[-1]) * 1.05, (34, 197, 94), True, "TP")], title="t",
                         width=640, height=320)
    a = asyncio.run(snapshot(df, title="x"))
    assert asyncio.run(snapshot(df, title="x")) is a
