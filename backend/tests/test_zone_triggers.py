"""Zone trigger alerts: touches, each confirmation, once per touch and the cooldown on constructed 5m candles, the
detected higher-timeframe zone, the signal-alert service, the API and the chart agent."""

import asyncio
import os

os.environ["DATA_SOURCE"] = "synthetic"
os.environ["LLM_PROVIDER"] = "none"

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.alerts import AlertService  # noqa: E402
from app.config import Settings  # noqa: E402
from app.llm import rule_intent  # noqa: E402
from app.main import app  # noqa: E402
from app.schemas import Candle, TriggerZone, ZoneTriggerIntent, ZoneTriggerSpec  # noqa: E402
from app.signal_alerts import SignalAlertService  # noqa: E402
from app.zone_triggers import (ZoneBand, describe_trigger, find_zone, scan_triggers, spec_from_intent,  # noqa: E402
                               touch_start, trigger_hit)

T0, STEP = 1_700_000_000, 300
ZONE = ZoneBand(98.8, 100.2, "long", "H4 demand")

# After an up-wave around 106, price breaks down (a bearish BOS), makes a lower high at 103.00, taps the demand
# zone (low 99.00) and closes back above the lower high on the last candle: a bullish CHoCH. The candle after the
# low (row 9) is a bullish engulfing.
DIP = [(106.0, 106.2, 104.4, 104.5), (104.5, 104.6, 102.4, 102.5), (102.5, 102.6, 100.9, 101.0),
       (101.0, 101.2, 100.5, 101.1), (101.1, 103.0, 101.0, 102.8), (102.8, 102.9, 101.0, 101.1),
       (101.1, 101.2, 99.9, 100.0), (100.0, 100.1, 99.1, 99.4), (99.4, 99.6, 99.0, 99.3),
       (99.3, 100.9, 99.2, 100.8), (100.8, 102.0, 100.6, 101.9), (101.9, 103.6, 101.8, 103.4)]
# The same dip, but a swing low at 99.00, a bounce, and a last candle that wicks to 98.80 and closes back at 100.40.
SWEEP = DIP[:6] + [(101.1, 101.2, 99.9, 100.0), (100.0, 100.1, 99.0, 99.3), (99.3, 100.6, 99.2, 100.5),
                   (100.5, 101.4, 100.4, 101.3), (101.3, 101.6, 100.6, 100.8), (100.8, 100.9, 98.8, 100.4)]


def bars(closes) -> pd.DataFrame:
    c = np.asarray(closes, dtype=float)
    o = np.concatenate([c[:1], c[:-1]])
    return pd.DataFrame({"time": T0 + np.arange(len(c)) * STEP, "open": o, "high": np.maximum(o, c) + 0.3,
                         "low": np.minimum(o, c) - 0.3, "close": c, "volume": np.full(len(c), 100.0)})


def wave(n, base=106.0, amp=1.0, period=8):
    return list(base + amp * np.sin(np.arange(n) * 2 * np.pi / period))


def with_rows(df, rows) -> pd.DataFrame:
    t = int(df["time"].iloc[-1])
    extra = pd.DataFrame([{"time": t + (i + 1) * STEP, "open": o, "high": h, "low": lo, "close": c, "volume": 100.0}
                          for i, (o, h, lo, c) in enumerate(rows)])
    return pd.concat([df, extra], ignore_index=True)


def dip_df(rows=DIP) -> pd.DataFrame:
    return with_rows(bars(wave(60)), rows)


def mirror(df, k=200.0):
    m = df.copy()
    m["open"], m["close"] = k - df["open"], k - df["close"]
    m["high"], m["low"] = k - df["low"], k - df["high"]
    return m


def at(df, i) -> int:
    return int(df["time"].iloc[i])


# ---------------------------------------------------------------- detection --


def test_choch_fires_on_the_breaking_candle():
    df = dip_df()
    assert touch_start(df, ZONE) == 66  # the first candle that reached into the zone
    hit = trigger_hit(df, ZONE, "choch", tf="5m")
    assert hit and hit.time == at(df, -1) and hit.confirm == "choch" and hit.price == 103.4
    assert hit.text.startswith("M5 bullish CHoCH (closed above the swing high 103.00) after touching H4 demand "
                               "98.80–100.20, close 103.40. Suggested stop ")
    assert hit.text.endswith("under the M5 swing low 99.00.")
    assert 98.8 < hit.stop < 99.0 and hit.touch == at(df, 66)
    assert trigger_hit(df.iloc[:-1], ZONE, "choch", tf="5m") is None  # the candle before: nothing yet
    assert [h.time for h in scan_triggers(df, ZONE, "choch", 40)] == [hit.time]


def test_any_takes_the_first_confirmation_and_spends_the_touch():
    df = dip_df()
    hits = scan_triggers(df, ZONE, "any", 40, tf="5m")
    assert [(h.time, h.confirm) for h in hits] == [(at(df, 69), "engulfing")]
    assert "M5 bullish engulfing candle inside H4 demand 98.80–100.20, close 100.80" in hits[0].text
    # The CHoCH three candles later is in the same touch: no second fire, whatever the cooldown.
    assert trigger_hit(df, ZONE, "choch", last_fire=at(df, 69), cooldown_s=0) is None
    # A fire before this touch started does not block it; a cooldown that has not run out does.
    assert trigger_hit(df, ZONE, "choch", last_fire=at(df, 60), cooldown_s=0) is not None
    assert trigger_hit(df, ZONE, "choch", last_fire=at(df, 60), cooldown_s=3600) is None


def test_sweep_of_the_ltf_low():
    df = dip_df(SWEEP)
    hit = trigger_hit(df, ZONE, "sweep", tf="5m")
    assert hit and hit.confirm == "sweep" and hit.time == at(df, -1)
    assert hit.text.startswith("M5 sweep of the low 99.00 (wick to 98.80, closed back above) inside H4 demand")
    assert hit.stop < 98.8  # under the sweep's wick
    assert trigger_hit(df, ZONE, "choch") is None


def test_a_close_through_the_far_side_ends_the_touch():
    df = dip_df(DIP[:9] + [(99.3, 99.4, 98.2, 98.5)] + DIP[10:])
    assert scan_triggers(df, ZONE, "any", 40) == []
    assert touch_start(df, ZONE) is None


def test_far_from_the_zone_or_long_after_the_touch_never_fires():
    df = dip_df()
    away = ZoneBand(90.0, 91.0, "long", "far")  # never touched
    assert trigger_hit(df, away, "any") is None
    later = with_rows(df, [(103.4, 103.6, 103.2, 103.5)] * 13)  # 13 candles without a touch: the touch is over
    assert touch_start(later, ZONE) is None


def test_supply_mirrors_demand():
    df = mirror(dip_df())
    zone = ZoneBand(99.8, 101.2, "short", "H4 supply")
    hit = trigger_hit(df, zone, "choch", tf="5m")
    assert hit and hit.text.startswith("M5 bearish CHoCH (closed below the swing low 97.00) after touching H4 supply")
    assert "over the M5 swing high 101.00" in hit.text and 101.0 < hit.stop < 101.2
    assert trigger_hit(df, ZoneBand(99.8, 101.2, "long", "wrong way"), "choch") is None


def test_find_zone_on_the_higher_timeframe():
    htf = bars(wave(80, base=100.0) + [100.2, 100.1, 100.3, 106.0, 108.0])
    z = find_zone(htf, "demand", "4h")
    assert z and (z.low, z.high, z.direction, z.label) == (99.8, 100.3, "long", "H4 demand (untested)")
    assert find_zone(htf, "supply", "4h") is None  # the only zone is below price
    assert find_zone(htf, "any", "4h") == z
    s = find_zone(mirror(htf), "supply", "4h")
    assert s and s.direction == "short" and (s.low, s.high) == (99.7, 100.2)
    assert find_zone(htf.iloc[:20], "demand", "4h") is None


def test_agent_spec_and_wording():
    spec = spec_from_intent(ZoneTriggerIntent(timeframe="1m", confirm="choch", zone_kind="demand",
                                              zone_timeframe="4h"), "INJUSDT", "1h")
    assert (spec.interval, spec.zone.source, spec.zone.timeframe, spec.zone.kind) == ("1m", "detected", "4h", "demand")
    assert describe_trigger(spec, ZoneBand(23.9, 24.2, "long", "H4 demand")) == \
        "M1 CHoCH / BOS inside the nearest fresh H4 demand (now 23.90–24.20)"
    assert describe_trigger(spec) == "M1 CHoCH / BOS inside the nearest fresh H4 demand (none on that side of price " \
                                     "right now)"
    # No zone timeframe: the chart's, unless it is not above the trigger's.
    assert spec_from_intent(ZoneTriggerIntent(timeframe="5m"), "SOL", "1d").zone.timeframe == "1d"
    assert spec_from_intent(ZoneTriggerIntent(timeframe="15m"), "SOL", "5m").zone.timeframe == "4h"
    intent = rule_intent("alert me when 1m shows a CHoCH inside the 4h demand")
    assert intent.zone_trigger == ZoneTriggerIntent(timeframe="1m", confirm="choch", zone_kind="demand",
                                                    zone_timeframe="4h")
    assert not intent.alert_targets and intent.timeframe is None


# ------------------------------------------------------------------ service --


class FakeHub:
    def __init__(self):
        self.queues: dict[tuple, asyncio.Queue] = {}

    async def subscribe(self, symbol, interval):
        self.queues[(symbol, interval)] = asyncio.Queue()
        return self.queues[(symbol, interval)]

    async def unsubscribe(self, symbol, interval, queue):
        del self.queues[(symbol, interval)]


class FakeMarket:
    """Serves one DataFrame per interval, every candle closed."""

    def __init__(self, frames, source="binance"):
        self.frames, self.source, self.calls = frames, source, []

    async def get_klines(self, symbol, interval, limit):
        self.calls.append(interval)
        rows = self.frames[interval].tail(limit)
        return [Candle(**{k: r[k] for k in ("time", "open", "high", "low", "close", "volume")})
                for r in rows.to_dict("records")], self.source


def _service(market):
    settings = Settings(alerts_store="memory", alert_history_store="memory", signal_alerts_store="memory",
                        data_source="auto", telegram_bot_token="", telegram_chat_id="", discord_webhook_url="")
    hub = FakeHub()
    alerts = AlertService(hub, settings)
    return hub, alerts, SignalAlertService(hub, market, None, alerts, settings, close_delay=0, retry_delay=0.01)


def test_service_fires_once_per_touch_with_the_stop():
    df = dip_df()

    async def go():
        hub, alerts, svc = _service(FakeMarket({"5m": df}))
        events = alerts.subscribe()
        spec = ZoneTriggerSpec(symbol="injusdt", interval="5m", confirm="choch",
                               zone=TriggerZone(price_low=98.8, price_high=100.2, timeframe="4h", label="H4 demand"))
        a = await svc.add_trigger(spec)
        assert a.signal == "zone_trigger" and a.symbol == "INJUSDT" and list(hub.queues) == [("INJUSDT", "5m")]
        assert a.trigger.zone.direction == "long"  # the zone is below price
        assert (a.trigger.zone_low, a.trigger.zone_high, a.trigger.zone_label) == (98.8, 100.2, "H4 demand")
        again = await svc.add_trigger(spec.model_copy(update={"note": "take it", "cooldown_min": 30}))
        assert again.id == a.id and again.note == "take it" and again.trigger.cooldown_min == 30
        assert len(svc.list()) == 1

        fired = await svc.on_bar_close("INJUSDT", "5m", at(df, -1))
        assert [x.id for x in fired] == [a.id]
        assert fired[0].last_text.startswith("INJUSDT 5m: M5 bullish CHoCH (closed above the swing high 103.00)")
        assert fired[0].last_text.endswith("— take it") and 98.8 < fired[0].last_stop < 99.0
        while (msg := await asyncio.wait_for(events.get(), 3))["type"] != "signal_fired":
            pass
        assert msg["alert"]["last_stop"] == fired[0].last_stop and msg["price"] == 103.4
        while (msg := await asyncio.wait_for(events.get(), 3))["type"] != "history":
            pass
        assert msg["item"]["title"] == "M5 Zone trigger"

        # A later candle in the same touch (another close above the swing) does not fire again.
        more = with_rows(df, [(103.4, 104.4, 103.3, 104.2)])
        svc.market = FakeMarket({"5m": more})
        assert await svc.on_bar_close("INJUSDT", "5m", at(more, -1)) == []
        with pytest.raises(ValueError):
            await svc.add(["INJUSDT"], "5m", "zone_trigger")
        with pytest.raises(ValueError):  # price is inside the zone: which way?
            await svc.add_trigger(spec.model_copy(update={"zone": TriggerZone(price_low=100, price_high=105)}))
        await svc.close()
        await alerts.close()

    asyncio.run(go())


def test_service_follows_the_detected_zone():
    htf = bars(wave(80, base=99.4) + [99.6, 99.5, 99.7, 105.4, 107.4])  # an untested demand at 99.20–99.70
    ltf = dip_df()

    async def go():
        market = FakeMarket({"5m": ltf, "4h": htf})
        hub, alerts, svc = _service(market)
        spec = ZoneTriggerSpec(symbol="BTCUSDT", interval="5m", confirm="any",
                               zone=TriggerZone(source="detected", timeframe="4h", kind="demand"))
        a = await svc.add_trigger(spec)
        assert a.trigger.zone_low is None  # looked up on the first candle close
        assert await svc.on_bar_close("BTCUSDT", "5m", at(ltf, 65)) == []  # above the zone: nothing
        assert market.calls.count("4h") == 1
        cur = svc.list()[0]
        assert (cur.trigger.zone_low, cur.trigger.zone_high, cur.trigger.zone_label) == (99.2, 99.7,
                                                                                         "H4 demand (untested)")
        fired = await svc.on_bar_close("BTCUSDT", "5m", at(ltf, 69))
        assert len(fired) == 1 and "engulfing candle inside H4 demand (untested) 99.20–99.70" in fired[0].last_text
        # The zone is cached until the next 4h close (30 s here, where the fake's 4h feed looks stale).
        assert market.calls.count("4h") == 1
        svc._zones.clear()
        market.frames["4h"] = htf.iloc[:-2]  # before the impulse: no demand zone yet
        assert await svc.on_bar_close("BTCUSDT", "5m", at(ltf, -1)) == []
        assert svc.list()[0].trigger.zone_low is None  # no demand zone below price any more
        with pytest.raises(ValueError):  # the zone must be on a higher timeframe
            await svc.add_trigger(spec.model_copy(update={"zone": TriggerZone(source="detected", timeframe="5m")}))
        await svc.close()
        await alerts.close()

    asyncio.run(go())


# ---------------------------------------------------------------------- API --


def test_api_and_agent(monkeypatch):
    monkeypatch.setenv("DATA_SOURCE", "synthetic")
    from app import config
    config.get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            body = {"symbol": "BTCUSDT", "interval": "5m", "confirm": "choch",
                    "zone": {"source": "detected", "timeframe": "4h", "kind": "demand"}}
            r = client.post("/api/zone-triggers", json=body)
            assert r.status_code == 200, r.text
            made = r.json()["alert"]
            assert made["signal"] == "zone_trigger" and made["trigger"]["confirm"] == "choch"
            assert client.post("/api/zone-triggers", json=body).json()["alert"]["id"] == made["id"]
            listed = client.get("/api/signal-alerts").json()["alerts"]
            assert [x["id"] for x in listed if x["signal"] == "zone_trigger"] == [made["id"]]
            bad = {**body, "zone": {"source": "detected", "timeframe": "1m"}}
            assert client.post("/api/zone-triggers", json=bad).status_code == 422
            assert client.post("/api/zone-triggers", json={**body, "zone": {"source": "fixed"}}).status_code == 422
            assert client.post("/api/signal-alerts", json={"symbols": ["BTCUSDT"], "signal": "zone_trigger"}
                               ).status_code == 422

            price = client.get("/api/klines", params={"symbol": "BTCUSDT", "interval": "5m", "limit": 10}
                               ).json()["candles"][-1]["close"]
            fixed = {**body, "zone": {"source": "fixed", "price_low": price * 0.97, "price_high": price * 0.99},
                     "confirm": "any"}
            p = client.post("/api/zone-triggers/preview", params={"bars": 200}, json=fixed)
            assert p.status_code == 200, p.text
            prev = p.json()
            assert prev["zone"]["direction"] == "long" and prev["data_source"] == "synthetic"
            assert "Demo data" in prev["note"] and all("Suggested stop" in h["text"] for h in prev["hits"])
            assert client.delete(f"/api/signal-alerts/{made['id']}").json() == {"ok": True}

            r = client.post("/api/agent/analyze", json={"symbol": "INJUSDT", "interval": "1h",
                                                        "prompt": "alert me when 5m shows a CHoCH inside the 4h demand"})
            assert r.status_code == 200, r.text
            res = r.json()
            assert res["intent"]["zone_trigger"]["timeframe"] == "5m"
            spec, = res["trigger_alerts"]
            assert (spec["symbol"], spec["interval"], spec["confirm"]) == ("INJUSDT", "5m", "choch")
            assert spec["zone"] == {**spec["zone"], "source": "detected", "timeframe": "4h", "kind": "demand"}
            assert "Set a trigger alert: M5 CHoCH / BOS inside the nearest fresh H4 demand" in res["summary"]
            assert res["alerts"] == []
    finally:
        config.get_settings.cache_clear()
