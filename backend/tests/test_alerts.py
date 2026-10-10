"""Server-side price alerts: transition logic, the service on a fake stream, notifiers and the API."""

import asyncio
import json
import logging
import os
import time

os.environ["DATA_SOURCE"] = "synthetic"
os.environ["LLM_PROVIDER"] = "none"

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import config  # noqa: E402
from app.alerts import (AlertHistory, AlertPatch, AlertService, apply_patch, build_channels,  # noqa: E402
                        describe_fire, evaluate, fmt_price, send_all, split_message)
from app.config import Settings  # noqa: E402
from app.main import app  # noqa: E402
from app.schemas import AlertSpec, PriceAlert  # noqa: E402

TOKEN = "123456:SECRET-telegram-token"
WEBHOOK = "https://discord.com/api/webhooks/987/SECRET-discord-token"


def _cross(price=10.0, **kw):
    return PriceAlert(id="c", symbol="INJUSDT", kind="cross", price=price, created_at=0, **kw)


def _zone(lo=10.0, hi=12.0, **kw):
    return PriceAlert(id="z", symbol="INJUSDT", kind="zone", price_low=lo, price_high=hi, created_at=0, **kw)


# ------------------------------------------------------------- evaluate() --


def test_first_observation_only_records_the_side():
    a, fired = evaluate(_cross(), 9.0)
    assert not fired and a.armed and a.last_side == "below" and a.triggered_at is None
    z, fired = evaluate(_zone(), 11.0)
    assert not fired and z.armed and z.last_side == "inside"
    same, fired = evaluate(a, 9.5)
    assert same is a and not fired


def test_cross_fires_both_directions():
    a, fired = evaluate(_cross(last_side="below"), 10.5, now_ms=1234)
    assert fired and not a.armed and a.last_side == "above"
    assert a.triggered_price == 10.5 and a.triggered_at == 1234
    a, fired = evaluate(_cross(last_side="above"), 9.99)
    assert fired and a.last_side == "below" and a.triggered_at > 0
    a, fired = evaluate(_cross(last_side="below"), 10.0)  # touching the level counts as above
    assert fired


def test_zone_fires_on_entry_and_jump_through_not_on_exit():
    z, fired = evaluate(_zone(last_side="below"), 10.0)
    assert fired and z.last_side == "inside"
    z, fired = evaluate(_zone(last_side="above"), 12.0)
    assert fired and z.last_side == "inside"
    z, fired = evaluate(_zone(last_side="below"), 12.5)  # gapped straight through
    assert fired and z.last_side == "above" and z.triggered_price == 12.5
    z, fired = evaluate(_zone(last_side="above"), 9.0)
    assert fired and z.last_side == "below"
    z, fired = evaluate(_zone(last_side="inside"), 12.5)  # leaving does not fire
    assert not fired and z.armed and z.last_side == "above"


def test_disarmed_and_bad_prices_are_ignored():
    a = _cross(last_side="below", armed=False)
    assert evaluate(a, 11.0) == (a, False)
    b = _cross(last_side="below")
    assert evaluate(b, float("nan")) == (b, False)


def test_message_format():
    z, _ = evaluate(_zone(24.1, 24.6, label="H4 Demand", last_side="below"), 24.32)
    assert describe_fire(z, 24.32) == "INJUSDT: price entered 24.10–24.60 (H4 Demand) at 24.32"
    z, _ = evaluate(_zone(24.1, 24.6, last_side="above"), 23.9)
    assert describe_fire(z, 23.9) == "INJUSDT: price moved down through 24.10–24.60 at 23.90"
    c, _ = evaluate(_cross(65000, label="Price 65k", last_side="below"), 65010.5)
    assert describe_fire(c, 65010.5) == "INJUSDT: price crossed above 65,000.00 (Price 65k) at 65,010.50"
    assert fmt_price(0.000123) == "0.000123" and fmt_price(1.23456) == "1.2346"


# ------------------------------------------------------------- service --


class FakeHub:
    def __init__(self):
        self.queues: dict[str, asyncio.Queue] = {}
        self.unsubscribed: list[str] = []

    async def subscribe(self, symbol, interval):
        assert interval == "1m"
        self.queues[symbol] = asyncio.Queue()
        return self.queues[symbol]

    async def unsubscribe(self, symbol, interval, queue):
        assert self.queues.get(symbol) is queue
        del self.queues[symbol]
        self.unsubscribed.append(symbol)

    def push(self, symbol, price, source="binance"):
        self.queues[symbol].put_nowait({"type": "kline", "symbol": symbol, "interval": "1m", "source": source,
                                        "closed": False, "candle": {"time": 0, "open": price, "high": price,
                                                                    "low": price, "close": price, "volume": 1.0}})


async def _next(queue, kind, timeout=2.0):
    while True:
        msg = await asyncio.wait_for(queue.get(), timeout)
        if msg["type"] == kind:
            return msg


async def _until(cond, timeout=2.0):
    for _ in range(int(timeout / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met")


def _settings(**kw):
    base = dict(alerts_store="memory", data_source="auto", telegram_bot_token="", telegram_chat_id="",
                discord_webhook_url="")
    return Settings(**{**base, **kw})


def test_service_fires_on_stream_and_persists(tmp_path):
    store = tmp_path / "alerts.json"

    async def go():
        hub = FakeHub()
        svc = AlertService(hub, _settings(alerts_store=str(store)))
        await svc.start()
        assert hub.queues == {}
        events = svc.subscribe()
        assert (await events.get()) == {"type": "snapshot", "alerts": []}

        a, z = await svc.add("injusdt", [AlertSpec(kind="cross", price=10, label="Ten"),
                                         AlertSpec(kind="zone", price_low=12, price_high=11)])
        assert a.symbol == "INJUSDT" and (z.price_low, z.price_high) == (11, 12)
        assert list(hub.queues) == ["INJUSDT"]  # one subscription per symbol
        assert len((await _next(events, "snapshot"))["alerts"]) == 2

        hub.push("INJUSDT", 9.5)  # first observation: below both
        hub.push("INJUSDT", 30.0, source="synthetic")  # fallback feed in auto mode: ignored
        hub.push("INJUSDT", 10.5)  # crosses 10
        fired = await _next(events, "fired")
        assert fired["alert"]["id"] == a.id and fired["price"] == 10.5 and fired["alert"]["armed"] is False

        saved = {x["id"]: x for x in json.loads(store.read_text())["alerts"]}
        assert saved[a.id]["armed"] is False and saved[a.id]["triggered_price"] == 10.5
        assert saved[z.id]["armed"] is True and saved[z.id]["last_side"] == "below"

        hub.push("INJUSDT", 11.5)  # enters the zone: last armed alert on the symbol
        fired = await _next(events, "fired")
        assert fired["alert"]["id"] == z.id
        await _until(lambda: hub.unsubscribed == ["INJUSDT"])

        assert (await svc.rearm(a.id)).armed and list(hub.queues) == ["INJUSDT"]
        assert await svc.clear_triggered() == 1 and [x.id for x in svc.list()] == [a.id]
        assert await svc.remove(a.id) and not await svc.remove(a.id)
        await _until(lambda: hub.unsubscribed == ["INJUSDT", "INJUSDT"])
        await svc.close()

        # A restart picks the saved alerts up again and re-subscribes for armed ones.
        await AlertService(hub, _settings(alerts_store=str(store))).close()
        svc2 = AlertService(hub, _settings(alerts_store=str(store)))
        assert svc2.list() == []
        b = (await svc2.add("BTCUSDT", [AlertSpec(kind="cross", price=60000)]))[0]
        await svc2.close()
        svc3 = AlertService(hub, _settings(alerts_store=str(store)))
        await svc3.start()
        assert [x.id for x in svc3.list()] == [b.id] and list(hub.queues) == ["BTCUSDT"]
        await svc3.close()
        assert hub.queues == {}

    asyncio.run(go())


def test_service_rejects_bad_specs_and_caps_count():
    async def go():
        svc = AlertService(FakeHub(), _settings())
        with pytest.raises(ValueError):
            await svc.add("INJUSDT", [AlertSpec(kind="cross")])
        with pytest.raises(ValueError):
            await svc.add("INJUSDT", [AlertSpec(kind="zone", price_low=1)])
        await svc.add("INJUSDT", [AlertSpec(kind="cross", price=1 + i) for i in range(200)])
        with pytest.raises(ValueError):
            await svc.add("INJUSDT", [AlertSpec(kind="cross", price=5)])
        oldest = svc.list()[0]
        svc._alerts[oldest.id] = oldest.model_copy(update={"armed": False})  # a triggered one makes room
        await svc.add("INJUSDT", [AlertSpec(kind="cross", price=5)])
        assert len(svc.list()) == 200 and oldest.id not in {a.id for a in svc.list()}
        await svc.close()

    asyncio.run(go())


# ------------------------------------------------------------- notifiers --


def test_notifiers_send_expected_payloads():
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"ok": True}) if req.url.host == "api.telegram.org" else httpx.Response(204)

    async def go():
        hub = FakeHub()
        svc = AlertService(hub, _settings(telegram_bot_token=TOKEN, telegram_chat_id="42", discord_webhook_url=WEBHOOK),
                           client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        assert svc.channel_status == {"telegram": True, "discord": True}
        await svc.add("INJUSDT", [AlertSpec(kind="zone", price_low=24.1, price_high=24.6, label="H4 Demand")])
        hub.push("INJUSDT", 23.0)
        hub.push("INJUSDT", 24.32)
        await _until(lambda: len(seen) == 2)
        assert await svc.send_test() == {"telegram": True, "discord": True}
        await svc.close()

    asyncio.run(go())
    by_host = {r.url.host: r for r in seen[:2]}
    tg, dc = by_host["api.telegram.org"], by_host["discord.com"]
    text = "INJUSDT: price entered 24.10–24.60 (H4 Demand) at 24.32"
    assert tg.method == "POST" and str(tg.url) == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    body = json.loads(tg.content)  # a card: a bold title, the text, the price
    assert body["chat_id"] == "42" and body["parse_mode"] == "HTML"
    assert body["text"].startswith("<b>INJUSDT price alert: H4 Demand</b>") and text in body["text"]
    card = json.loads(dc.content)
    assert str(dc.url) == WEBHOOK and card["allowed_mentions"] == {"parse": []}
    assert card["embeds"][0]["description"] == text and card["embeds"][0]["color"] == 0x64748B


def test_notifier_failures_are_logged_without_secrets(caplog):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "api.telegram.org":
            return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
        raise httpx.ConnectError(f"cannot reach {req.url}")

    caplog.set_level(logging.INFO)
    channels = build_channels(_settings(telegram_bot_token=TOKEN, telegram_chat_id="42", discord_webhook_url=WEBHOOK))
    svc = AlertService(FakeHub(), _settings(telegram_bot_token=TOKEN, telegram_chat_id="42",
                                            discord_webhook_url=WEBHOOK))  # installs the httpx log filter

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await send_all(client, channels, "hello")

    assert asyncio.run(go()) == {"telegram": False, "discord": False}
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert "Unauthorized" in logs and "ConnectError" in logs
    assert "SECRET" not in logs and "***" in logs
    asyncio.run(svc.close())


# ------------------------------------------------------------------- API --


@pytest.fixture
def alerts_store(tmp_path, monkeypatch):
    store = tmp_path / "alerts.json"
    monkeypatch.setenv("ALERTS_STORE", str(store))
    monkeypatch.setenv("DATA_SOURCE", "synthetic")
    monkeypatch.setenv("LLM_PROVIDER", "none")
    config.get_settings.cache_clear()
    yield store
    config.get_settings.cache_clear()


def test_api_round_trip(alerts_store):
    with TestClient(app) as client:
        assert client.get("/api/alerts").json() == {"alerts": [], "channels": {"telegram": False, "discord": False}}
        with client.websocket_connect("/ws/alerts") as ws:
            assert ws.receive_json() == {"type": "snapshot", "alerts": []}
            r = client.post("/api/alerts", json={"symbol": "inj/usdt", "alerts": [
                {"kind": "cross", "price": 5, "label": "Five"},
                {"kind": "zone", "price_low": 2, "price_high": 1}]})
            assert r.status_code == 200, r.text
            cross, zone = r.json()["alerts"]
            assert cross["symbol"] == "INJUSDT" and cross["armed"] and cross["created_at"] > 0
            assert (zone["price_low"], zone["price_high"]) == (1, 2)
            snap = ws.receive_json()
            while not (snap["type"] == "snapshot" and len(snap["alerts"]) == 2):
                snap = ws.receive_json()
            ws.send_json({"type": "ping"})
            while ws.receive_json()["type"] != "pong":
                pass

            # Drive a fire through the service on the app's event loop.
            service = client.app.state.alerts
            client.portal.call(service.on_price, "INJUSDT", 1.0)
            client.portal.call(service.on_price, "INJUSDT", 1e6)
            while (msg := ws.receive_json())["type"] != "fired":
                pass
            assert msg["alert"]["id"] in (cross["id"], zone["id"]) and msg["alert"]["armed"] is False

        assert client.post("/api/alerts", json={"symbol": "INJUSDT", "alerts": [{"kind": "cross"}]}).status_code == 422
        assert client.post("/api/alerts", json={"symbol": "!!", "alerts": [{"kind": "cross", "price": 1}]}
                           ).status_code == 422
        assert client.post("/api/alerts", json={"symbol": "INJUSDT", "alerts": []}).status_code == 422
        assert client.post("/api/alerts/test").status_code == 400
        assert client.post("/api/alerts/nope/rearm").status_code == 404
        assert client.delete("/api/alerts/nope").status_code == 404

        fired_ids = {a["id"] for a in client.get("/api/alerts").json()["alerts"] if not a["armed"]}
        assert fired_ids
        rearmed = client.post(f"/api/alerts/{next(iter(fired_ids))}/rearm").json()["alert"]
        assert rearmed["armed"] and rearmed["triggered_at"] is None
        triggered_left = len(fired_ids) - 1
        assert client.post("/api/alerts/clear-triggered").json() == {"removed": triggered_left}
        assert client.delete(f"/api/alerts/{rearmed['id']}").json() == {"ok": True}

        preflight = client.options(f"/api/alerts/{rearmed['id']}", headers={
            "Origin": "http://localhost:3000", "Access-Control-Request-Method": "DELETE"})
        assert preflight.status_code == 200

        remaining = client.get("/api/alerts").json()["alerts"]

    saved = json.loads(alerts_store.read_text())["alerts"]
    assert [a["id"] for a in saved] == [a["id"] for a in remaining]
    with TestClient(app) as client:  # survives a restart
        assert [a["id"] for a in client.get("/api/alerts").json()["alerts"]] == [a["id"] for a in remaining]


# ------------------------------------------ repeat, expiry, editing, history --


def test_repeat_stays_armed_with_a_cooldown():
    a = _cross(last_side="below", repeat=True)
    a, fired = evaluate(a, 10.5, now_ms=1_000_000)
    assert fired and a.armed and a.fire_count == 1 and a.triggered_at == 1_000_000
    a, fired = evaluate(a, 9.0, now_ms=1_060_000)  # back below, inside the 5 min cooldown
    assert not fired and a.last_side == "below" and a.fire_count == 1
    a, fired = evaluate(a, 10.5, now_ms=1_250_000)  # still inside it
    assert not fired and a.last_side == "above"
    a, fired = evaluate(a, 9.0, now_ms=1_700_000)  # cooldown over
    assert fired and a.armed and a.fire_count == 2 and a.last_side == "below"


def test_expiry_disarms_without_firing():
    a = _cross(last_side="below", expires_at=1_000_000)
    a, fired = evaluate(a, 10.5, now_ms=1_000_001)
    assert not fired and not a.armed and a.expired and a.triggered_at is None
    b = _cross(last_side="below", expires_at=2_000_000)
    b, fired = evaluate(b, 10.5, now_ms=1_999_000)
    assert fired and b.expired is False


def test_apply_patch_validates_and_resets_the_side():
    a = _cross(10.0, last_side="below", label="Ten")
    moved = apply_patch(a, AlertPatch(price=12.0, note="watch this"))
    assert moved.price == 12.0 and moved.last_side is None and moved.note == "watch this" and moved.label == "Ten"
    same = apply_patch(a, AlertPatch(label="Renamed"))
    assert same.last_side == "below" and same.label == "Renamed" and same.price == 10.0
    z = apply_patch(_zone(10, 12), AlertPatch(price_low=14, price_high=13))
    assert (z.price_low, z.price_high) == (13, 14) and z.last_side is None
    with pytest.raises(ValueError):
        apply_patch(a, AlertPatch(price=0))
    with pytest.raises(ValueError):
        apply_patch(a, AlertPatch(price_low=1, price_high=2))  # a cross alert has no zone
    with pytest.raises(ValueError):
        apply_patch(a, AlertPatch(expires_at=1))  # already past
    expired = _cross(last_side="below", armed=False, expired=True, expires_at=1)
    again = apply_patch(expired, AlertPatch(expires_at=None))
    assert again.armed and not again.expired and again.expires_at is None


def test_split_message_respects_the_limit():
    text = "A" * 30 + "\n\n" + "B" * 30 + "\n" + "C" * 30
    parts = split_message(text, 40)
    assert all(len(p) <= 40 for p in parts)
    assert "".join(p.replace("\n", "") for p in parts) == text.replace("\n", "")
    assert split_message("x" * 95, 40) == ["x" * 40, "x" * 40, "x" * 15]
    assert split_message("short", 40) == ["short"] and split_message("  ", 40) == []


def test_history_keeps_the_newest_items(tmp_path):
    store = tmp_path / "history.json"
    h = AlertHistory(str(store))
    for i in range(520):
        h.add("price", "INJUSDT", "Alert", f"fire {i}", price=float(i))
    assert len(h.list(1000)) == 500 and h.list(2)[0]["text"] == "fire 519"
    assert AlertHistory(str(store)).list(1)[0]["text"] == "fire 519"  # survives a restart
    assert h.clear() == 500 and AlertHistory(str(store)).list(10) == []


def test_service_records_history_and_expires(tmp_path):
    async def go():
        hub = FakeHub()
        svc = AlertService(hub, _settings(alert_history_store=str(tmp_path / "h.json")))
        events = svc.subscribe()
        now = int(time.time() * 1000)
        a, = await svc.add("INJUSDT", [AlertSpec(kind="cross", price=10, label="Ten", repeat=True, note="watch")])
        b, = await svc.add("INJUSDT", [AlertSpec(kind="cross", price=20, expires_at=now + 50)])
        hub.push("INJUSDT", 9.0)
        hub.push("INJUSDT", 10.5)
        item = (await _next(events, "history"))["item"]
        assert item["kind"] == "price" and item["alert_id"] == a.id and item["price"] == 10.5
        assert svc.history.list(5)[0]["text"].startswith("INJUSDT: price crossed above 10.00 (Ten) at 10.50 — watch")
        await _until(lambda: svc._alerts[a.id].armed and svc._alerts[a.id].fire_count == 1)  # repeat stays armed
        assert list(hub.queues) == ["INJUSDT"]

        expired = svc.expire_due(now + 100)
        assert [x.id for x in expired] == [b.id] and svc._alerts[b.id].expired
        patched = await svc.update(a.id, AlertPatch(price=11.0, note="moved"))
        assert patched.price == 11.0 and patched.last_side is None and patched.note == "moved"
        assert await svc.update("nope", AlertPatch(note="x")) is None
        await svc.close()

    asyncio.run(go())


def test_api_patch_history_and_expiry(alerts_store):
    with TestClient(app) as client:
        now = int(time.time() * 1000)
        created = client.post("/api/alerts", json={"symbol": "INJUSDT", "alerts": [
            {"kind": "cross", "price": 5, "label": "Five", "repeat": True, "note": "keep an eye"}]}).json()["alerts"]
        a = created[0]
        assert a["repeat"] is True and a["note"] == "keep an eye" and a["fire_count"] == 0 and a["expired"] is False

        r = client.patch(f"/api/alerts/{a['id']}", json={"price": 6, "label": "Six", "expires_at": now + 3_600_000})
        assert r.status_code == 200 and r.json()["alert"]["price"] == 6 and r.json()["alert"]["label"] == "Six"
        assert client.patch(f"/api/alerts/{a['id']}", json={"price": 0}).status_code == 422
        assert client.patch(f"/api/alerts/{a['id']}", json={"expires_at": now - 1}).status_code == 422
        assert client.patch("/api/alerts/nope", json={"label": "x"}).status_code == 404

        service = client.app.state.alerts
        client.portal.call(service.on_price, "INJUSDT", 1.0)
        client.portal.call(service.on_price, "INJUSDT", 9.0)
        items = client.get("/api/alerts/history").json()["items"]
        assert items and items[0]["kind"] == "price" and items[0]["alert_id"] == a["id"]
        assert client.get("/api/alerts/history", params={"limit": 1}).json()["items"] == items[:1]
        assert client.delete("/api/alerts/history").json() == {"removed": len(items)}
        assert client.get("/api/alerts/history").json()["items"] == []
