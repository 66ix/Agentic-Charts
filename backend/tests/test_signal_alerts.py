"""Signal alerts: each detector on constructed candles, candle-close handling on a fake stream, and the API."""

import asyncio
import json
import os

os.environ["DATA_SOURCE"] = "synthetic"
os.environ["LLM_PROVIDER"] = "none"

import httpx  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.alerts import AlertService  # noqa: E402
from app.config import Settings  # noqa: E402
from app.main import app  # noqa: E402
from app.schemas import Candle, KimiSignal  # noqa: E402
from app.signal_alerts import (SIGNAL_IDS, SIGNALS, Frame, SignalAlertPatch, SignalAlertService, detect,  # noqa: E402
                               scan_history)

T0, STEP = 1_700_000_000, 3600
TOKEN = "123456:SECRET-telegram-token"


def bars(closes, last=None) -> pd.DataFrame:
    """Hourly candles from closes (open = previous close, 0.3 wicks); `last` overrides the final candle."""
    c = np.asarray(closes, dtype=float)
    o = np.concatenate([c[:1], c[:-1]])
    df = pd.DataFrame({"time": T0 + np.arange(len(c)) * STEP, "open": o, "high": np.maximum(o, c) + 0.3,
                       "low": np.minimum(o, c) - 0.3, "close": c, "volume": np.full(len(c), 100.0)})
    for k, v in (last or {}).items():
        df.loc[len(df) - 1, k] = v
    return df


def wave(n, base=100.0, amp=1.0, period=8):
    return list(base + amp * np.sin(np.arange(n) * 2 * np.pi / period))


def mirror(df, k=200.0):
    m = df.copy()
    m["open"], m["close"] = k - df["open"], k - df["close"]
    m["high"], m["low"] = k - df["low"], k - df["high"]
    return m


def sweep_low_df():
    closes = list(np.linspace(100, 106, 60)) + [104, 101, 98, 96, 95.3] + list(np.linspace(96.5, 104, 10)) + [102]
    return bars(closes, {"open": 103.5, "low": 94.0, "high": 103.8, "close": 102.0})


def demand_df():
    return bars(wave(80) + [100.2, 100.1, 100.3, 106.0, 108.0])


def bos_df():
    return bars(wave(60, amp=2) + [101, 103, 106, 104, 101, 99, 100, 102, 104, 107])


def rsi_df():
    return bars(wave(80) + list(100 + np.cumsum(np.full(15, 0.8))))


def divergence_df():
    return bars(wave(50, amp=1.5) + [97, 94, 91, 90.0] + list(np.linspace(92, 98, 8))
                + list(np.linspace(97.5, 89.2, 16)) + list(np.linspace(90.5, 96, 8)))


def last_time(df) -> int:
    return int(df["time"].iloc[-1])


# ---------------------------------------------------------------- detectors --


def test_every_signal_has_a_plain_name():
    assert set(SIGNALS) == set(SIGNAL_IDS) and all(SIGNALS[s].name and SIGNALS[s].description for s in SIGNAL_IDS)


def test_sweeps_fire_on_the_sweeping_candle_only():
    df = sweep_low_df()
    hit = detect("sweep_low", Frame(df))
    assert hit and hit.time == last_time(df) and "swept the low at 95.00" in hit.text
    assert "closed back above" in hit.text
    assert detect("sweep_low", Frame(df.iloc[:-1])) is None  # the candle before: nothing yet
    assert detect("sweep_high", Frame(df)) is None
    high = detect("sweep_high", Frame(mirror(df)))  # the mirror image is a sweep of a high
    assert high and high.time == last_time(df) and "closed back below" in high.text


def test_new_zone_forms_with_the_candle_after_the_impulse():
    df = demand_df()
    hit = detect("new_demand", Frame(df))
    assert hit and hit.time == last_time(df) and hit.text.startswith("new demand zone 99.80–100.30")
    assert detect("new_demand", Frame(df.iloc[:-1])) is None
    assert detect("new_supply", Frame(df)) is None
    assert detect("new_supply", Frame(mirror(df))) is not None


def test_structure_break():
    df = bos_df()
    hit = detect("bos_bull", Frame(df))
    assert hit and "closed above the swing high" in hit.text and hit.time == last_time(df)
    assert detect("bos_bear", Frame(df)) is None
    assert detect("bos_bear", Frame(mirror(df))) is not None


def test_rsi_cross_fires_once_on_the_crossing_candle():
    df = rsi_df()
    hits = scan_history("rsi_overbought", df, 40)
    assert len(hits) == 1 and "RSI crossed above 70" in hits[0].text
    k = int(np.flatnonzero(df["time"] == hits[0].time)[0])
    assert detect("rsi_overbought", Frame(df.iloc[:k + 1])) == hits[0]
    assert detect("rsi_overbought", Frame(df.iloc[:k])) is None
    assert detect("rsi_overbought", Frame(df.iloc[:k + 2])) is None  # still above 70: no new cross
    assert scan_history("rsi_oversold", mirror(df), 40)


def test_divergence_fires_when_it_first_appears():
    df = divergence_df()
    hits = scan_history("rsi_bull_div", df, 60)
    assert len(hits) == 1 and "bullish RSI divergence: lower low 88.90 vs 89.70" in hits[0].text
    k = int(np.flatnonzero(df["time"] == hits[0].time)[0])
    frame, prev = Frame(df.iloc[:k + 1]), Frame(df.iloc[:k])
    assert detect("rsi_bull_div", frame, prev) == hits[0]
    assert detect("rsi_bull_div", Frame(df.iloc[:k + 2]), frame) is None  # same divergence a candle later
    assert scan_history("rsi_bear_div", df, 60) == []
    assert scan_history("rsi_bear_div", mirror(df), 60)


def _kimi(text, confirm_time, direction="long", entry=64210.0):
    return KimiSignal(type="DIV", direction=direction, text=text, time=confirm_time - 3 * STEP,
                      confirm_time=confirm_time, price=entry, entry=entry, confluence=3, tier="top", result="open")


def test_kimi_signals_match_by_label_and_candle():
    t = T0 + 99 * STEP
    sigs = [_kimi("B-", t - STEP, "short"), _kimi("B+", t)]
    frame = Frame(bars(wave(100)))
    hit = detect("kimi_buy", frame, kimi=sigs)
    assert hit and hit.text == "Kimi printed B+ (long) at 64,210.00 — confluence 3" and hit.price == 64210.0
    assert detect("kimi_sell", frame, kimi=sigs) is None  # B- was a candle earlier
    assert detect("kimi_any", frame, kimi=sigs) == hit
    hist = scan_history("kimi_sell", bars(wave(100)), 10, kimi=sigs)
    assert [h.time for h in hist] == [t - STEP]


# ------------------------------------------------------------------ service --


class FakeHub:
    def __init__(self):
        self.queues: dict[tuple, asyncio.Queue] = {}
        self.unsubscribed: list[tuple] = []

    async def subscribe(self, symbol, interval):
        self.queues[(symbol, interval)] = asyncio.Queue()
        return self.queues[(symbol, interval)]

    async def unsubscribe(self, symbol, interval, queue):
        assert self.queues.get((symbol, interval)) is queue
        del self.queues[(symbol, interval)]
        self.unsubscribed.append((symbol, interval))

    def push(self, symbol, interval, t, source="binance", closed=False):
        self.queues[(symbol, interval)].put_nowait({
            "type": "kline", "symbol": symbol, "interval": interval, "source": source, "closed": closed,
            "candle": {"time": t, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}})


class FakeMarket:
    """Serves the candles of `df` up to `upto` (open time); the candle at `upto` is the newest closed one."""

    def __init__(self, df, source="binance"):
        self.df, self.source, self.upto, self.calls = df, source, last_time(df), 0

    async def get_klines(self, symbol, interval, limit):
        self.calls += 1
        rows = self.df[self.df["time"] <= self.upto].tail(limit)
        return [Candle(**{k: r[k] for k in ("time", "open", "high", "low", "close", "volume")})
                for r in rows.to_dict("records")], self.source


class FakeKimi:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), 0

    async def get(self, symbol, interval):
        self.calls += 1
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


class KimiResp:
    def __init__(self, last_closed, signals, data_source="binance"):
        self.last_closed, self.signals, self.data_source = last_closed, signals, data_source


def _settings(**kw):
    base = dict(alerts_store="memory", alert_history_store="memory", signal_alerts_store="memory",
                data_source="auto", telegram_bot_token="", telegram_chat_id="", discord_webhook_url="")
    return Settings(**{**base, **kw})


async def _next(queue, kind, timeout=3.0):
    while True:
        msg = await asyncio.wait_for(queue.get(), timeout)
        if msg["type"] == kind:
            return msg


async def _until(cond, timeout=3.0):
    for _ in range(int(timeout / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met")


def _service(hub, market, kimi=None, client=None, **kw):
    settings = _settings(**kw)
    alerts = AlertService(hub, settings, client=client)
    return alerts, SignalAlertService(hub, market, kimi, alerts, settings, close_delay=0, retry_delay=0.01)


def test_fires_on_candle_close_once_and_notifies(tmp_path):
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"ok": True})

    df = sweep_low_df()
    t = last_time(df)

    async def go():
        hub, market = FakeHub(), FakeMarket(df)
        alerts, svc = _service(hub, market, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                               telegram_bot_token=TOKEN, telegram_chat_id="42",
                               signal_alerts_store=str(tmp_path / "s.json"))
        events = alerts.subscribe()
        assert (await events.get())["type"] == "snapshot"
        assert (await events.get()) == {"type": "signal_snapshot", "alerts": []}
        a, = await svc.add(["injusdt"], "1h", "sweep_low", note="buy the dip?")
        assert a.symbol == "INJUSDT" and a.repeat and list(hub.queues) == [("INJUSDT", "1h")]
        assert len((await _next(events, "signal_snapshot"))["alerts"]) == 1

        hub.push("INJUSDT", "1h", t)            # the candle still forming
        hub.push("INJUSDT", "1h", t + STEP)     # a newer open time: candle t closed
        fired = await _next(events, "signal_fired")
        assert fired["time"] == t and fired["price"] == 102.0
        assert fired["text"].startswith("INJUSDT 1h: swept the low at 95.00") and fired["text"].endswith("buy the dip?")
        hist = (await _next(events, "history"))["item"]
        assert hist["kind"] == "signal" and hist["alert_id"] == a.id and hist["symbol"] == "INJUSDT"
        stored = svc.list()[0]
        assert stored.armed and stored.fire_count == 1 and stored.last_bar == t  # repeat: stays armed

        hub.push("INJUSDT", "1h", t, closed=True)  # a late "closed" flag for the same candle
        assert await svc.on_bar_close("INJUSDT", "1h", t) == []  # judged once
        await _until(lambda: len(seen) == 1)
        await asyncio.sleep(0.05)
        assert len(seen) == 1 and json.loads(seen[0].content)["text"] == fired["text"]
        assert svc.list()[0].fire_count == 1

        saved = json.loads((tmp_path / "s.json").read_text())["alerts"]
        assert saved[0]["last_bar"] == t
        await svc.close()
        await alerts.close()
        assert hub.queues == {}

    asyncio.run(go())


def test_repeat_off_disarms_and_drops_the_stream():
    df = demand_df()
    t = last_time(df)

    async def go():
        hub, market = FakeHub(), FakeMarket(df)
        alerts, svc = _service(hub, market)
        a, = await svc.add(["INJUSDT"], "1h", "new_demand", repeat=False)
        fired = await svc.on_bar_close("INJUSDT", "1h", t)
        assert [x.id for x in fired] == [a.id] and not fired[0].armed
        assert hub.unsubscribed == [("INJUSDT", "1h")]
        # Re-adding the same alert re-arms it instead of creating a duplicate.
        again, = await svc.add(["INJUSDT"], "1h", "new_demand", repeat=True)
        assert again.id == a.id and again.armed and len(svc.list()) == 1
        await svc.close()
        await alerts.close()

    asyncio.run(go())


def test_synthetic_candles_never_fire():
    df = sweep_low_df()
    t = last_time(df)

    async def go():
        hub, market = FakeHub(), FakeMarket(df, source="synthetic")
        alerts, svc = _service(hub, market)
        await svc.add(["INJUSDT"], "1h", "sweep_low")
        hub.push("INJUSDT", "1h", t, source="synthetic")
        hub.push("INJUSDT", "1h", t + STEP, source="synthetic")  # ignored: fallback feed
        await asyncio.sleep(0.05)
        assert market.calls == 0
        assert await svc.on_bar_close("INJUSDT", "1h", t) == []  # and synthetic REST candles are skipped
        assert market.calls == 1 and svc.list()[0].fire_count == 0
        await svc.close()
        await alerts.close()

        # With DATA_SOURCE=synthetic (the offline demo) they do fire.
        hub, market = FakeHub(), FakeMarket(df, source="synthetic")
        alerts, svc = _service(hub, market, data_source="synthetic")
        await svc.add(["INJUSDT"], "1h", "sweep_low")
        assert len(await svc.on_bar_close("INJUSDT", "1h", t)) == 1
        await svc.close()
        await alerts.close()

    asyncio.run(go())


def test_waits_for_the_closed_candle_and_kimi():
    df = bars(wave(100))
    t = last_time(df)
    stale, fresh = KimiResp(t - STEP, []), KimiResp(t, [_kimi("B+", t)])

    async def go():
        hub, market = FakeHub(), FakeMarket(df)
        market.upto = t - STEP  # the feed has not served the new candle yet
        kimi = FakeKimi([stale, fresh])
        alerts, svc = _service(hub, market, kimi)
        a, = await svc.add(["BTCUSDT"], "1h", "kimi_buy")
        task = asyncio.create_task(svc.on_bar_close("BTCUSDT", "1h", t))
        await _until(lambda: market.calls >= 2)
        market.upto = t
        fired = await task
        assert [x.id for x in fired] == [a.id] and kimi.calls == 2
        assert fired[0].last_text == "BTCUSDT 1h: Kimi printed B+ (long) at 64,210.00 — confluence 3"
        await svc.close()
        await alerts.close()

    asyncio.run(go())


def test_crud_validation_and_cap():
    async def go():
        hub = FakeHub()
        alerts, svc = _service(hub, FakeMarket(bars(wave(100))))
        with pytest.raises(ValueError):
            await svc.add(["BTCUSDT"], "1h", "moon")
        with pytest.raises(ValueError):
            await svc.add(["BTCUSDT"], "2h", "sweep_low")
        with pytest.raises(ValueError):
            await svc.add(["!!"], "1h", "sweep_low")
        made = await svc.add(["btc", "ETH/USDT", "BTCUSDT"], "4h", "rsi_oversold")
        assert [a.symbol for a in made] == ["BTCUSDT", "ETHUSDT"]
        off = await svc.update(made[0].id, SignalAlertPatch(armed=False, note=" later "))
        assert not off.armed and off.note == "later" and off.repeat
        assert sorted(hub.queues) == [("ETHUSDT", "4h")]
        assert await svc.update("nope", SignalAlertPatch(armed=True)) is None
        assert await svc.remove(made[1].id) and not await svc.remove(made[1].id)
        assert hub.queues == {}
        for i in range(4):
            await svc.add([f"C{i:03d}USDT" for i in range(i * 40, i * 40 + 40)], "1h", "sweep_low")
        await svc.add([f"D{i:03d}USDT" for i in range(39)], "1h", "sweep_low")
        with pytest.raises(ValueError):
            await svc.add(["ZZZUSDT"], "1h", "sweep_low")
        assert len(svc.list()) == 200
        await svc.close()
        await alerts.close()

    asyncio.run(go())


# ---------------------------------------------------------------------- API --


def test_api_round_trip(monkeypatch):
    monkeypatch.setenv("DATA_SOURCE", "synthetic")
    from app import config
    config.get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            body = client.get("/api/signal-alerts").json()
            assert body["alerts"] == [] and {s["id"] for s in body["signals"]} == set(SIGNAL_IDS)
            with client.websocket_connect("/ws/alerts") as ws:
                assert ws.receive_json()["type"] == "snapshot"
                assert ws.receive_json() == {"type": "signal_snapshot", "alerts": []}
                r = client.post("/api/signal-alerts", json={"symbols": ["BTCUSDT", "ethusdt"], "interval": "4h",
                                                            "signal": "sweep_low", "note": "n"})
                assert r.status_code == 200, r.text
                made = r.json()["alerts"]
                assert [a["symbol"] for a in made] == ["BTCUSDT", "ETHUSDT"] and made[0]["note"] == "n"
                while (msg := ws.receive_json())["type"] != "signal_snapshot":
                    pass
                assert len(msg["alerts"]) == 2

            assert client.post("/api/signal-alerts", json={"symbols": ["BTCUSDT"], "signal": "nope"}).status_code == 422
            assert client.post("/api/signal-alerts", json={"symbols": [], "signal": "sweep_low"}).status_code == 422
            r = client.patch(f"/api/signal-alerts/{made[0]['id']}", json={"armed": False})
            assert r.status_code == 200 and r.json()["alert"]["armed"] is False
            assert client.patch("/api/signal-alerts/nope", json={"armed": False}).status_code == 404
            assert client.delete(f"/api/signal-alerts/{made[1]['id']}").json() == {"ok": True}
            assert client.delete(f"/api/signal-alerts/{made[1]['id']}").status_code == 404

            p = client.get("/api/signal-alerts/preview", params={"symbol": "BTCUSDT", "interval": "4h",
                                                                 "signal": "sweep_low", "bars": 200})
            assert p.status_code == 200, p.text
            prev = p.json()
            assert prev["data_source"] == "synthetic" and prev["name"] == "Sweep of a low"
            assert "Demo data" in prev["note"]
            times = [h["time"] for h in prev["hits"]]
            assert times == sorted(times, reverse=True) and all(h["text"] for h in prev["hits"])
            assert client.get("/api/signal-alerts/preview", params={"symbol": "BTCUSDT", "signal": "x"}
                              ).status_code == 422
    finally:
        config.get_settings.cache_clear()
