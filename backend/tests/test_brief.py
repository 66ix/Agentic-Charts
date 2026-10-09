"""The brief: slot scheduling across time zones and restarts, the text, splitting, sending and the API."""

import asyncio
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

os.environ["DATA_SOURCE"] = "synthetic"
os.environ["LLM_PROVIDER"] = "none"

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.alerts import AlertService  # noqa: E402
from app.brief import (BriefSections, BriefService, BriefSettings, CoinBrief, NoChannelError, due_slot,  # noqa: E402
                       render_brief)
from app.config import Settings  # noqa: E402
from app.main import app  # noqa: E402
from app.market_data import synthetic_klines  # noqa: E402
from app.schemas import ScanResult  # noqa: E402

LONDON = ZoneInfo("Europe/London")
WEBHOOK = "https://discord.com/api/webhooks/987/SECRET-discord-token"


def ts(*args, tz=LONDON) -> float:
    return datetime(*args, tzinfo=tz).timestamp()


# ------------------------------------------------------------- scheduling --


def test_due_slot_local_times_and_grace():
    cfg = BriefSettings(enabled=True, times=["13:30", "8:00"], timezone="Europe/London")
    assert cfg.times == ["08:00", "13:30"]
    at_8 = ts(2026, 10, 6, 8, 0)
    assert due_slot(cfg, at_8 - 60, 0) is None
    assert due_slot(cfg, at_8 + 20, 0) == at_8
    assert due_slot(cfg, at_8 + 20, at_8) is None  # already sent
    assert due_slot(cfg, ts(2026, 10, 6, 13, 31), at_8) == ts(2026, 10, 6, 13, 30)
    assert due_slot(cfg, at_8 + 2 * 3600, 0) is None  # missed by two hours: skipped, not sent late
    # Summer time: 08:00 London is 07:00 UTC in October and 08:00 UTC in December.
    assert datetime.fromtimestamp(at_8, ZoneInfo("UTC")).hour == 7
    dec = BriefSettings(times=["08:00"], timezone="Europe/London")
    assert datetime.fromtimestamp(due_slot(dec, ts(2026, 12, 1, 8, 5), 0), ZoneInfo("UTC")).hour == 8
    late = BriefSettings(times=["23:59"], timezone="UTC")
    assert due_slot(late, ts(2026, 10, 7, 0, 0, 30, tz=ZoneInfo("UTC")), 0) == ts(2026, 10, 6, 23, 59,
                                                                                     tz=ZoneInfo("UTC"))


def test_settings_validation():
    for bad in ({"times": ["25:00"]}, {"times": []}, {"timezone": "Mars/Base"}, {"symbols": ["!!"]},
                {"interval": "2h"}):
        with pytest.raises(ValueError):
            BriefSettings(**bad)
    s = BriefSettings(symbols=["eth", "BTC/USDT", "ETHUSDT"], timezone="")
    assert s.symbols == ["ETHUSDT", "BTCUSDT"] and s.timezone == "UTC" and s.sections.kimi


# ---------------------------------------------------------------- content --


def _coin(sym, price=100.0, **kw):
    zone = ScanResult(symbol=sym, interval="4h", last_price=price, trend="up", nearest_kind="demand",
                      nearest_low=price * 0.99, nearest_high=price * 1.0, distance_pct=0.0)
    return CoinBrief(sym, price=price, change_24h=2.5, since_last=-1.0, trend="up", rsi=55.0, zone=zone,
                     support={"low": 95.0, "high": 96.0}, resistance={"low": 110.0, "high": 111.0}, d1_high=105.0,
                     d1_low=97.0, kimi_signal={"label": "B+", "direction": "long", "price": 99.0, "hours_ago": 5},
                     kimi_headline="▲ Proj: 104.00", funding_pct=0.01, oi_change_pct=3.2, **kw)


def test_render_brief_sections_and_events():
    now = datetime(2026, 10, 6, 8, 0, tzinfo=LONDON)
    events = [{"time": ts(2026, 10, 6, 13, 30), "title": "CPI", "country": "US", "impact": "high"},
              {"time": ts(2026, 10, 7, 13, 30), "title": "Tomorrow", "country": "US", "impact": "high"},
              {"time": "bad"}]
    text = render_brief(now, "Europe/London", "4h", [_coin("BTCUSDT"), CoinBrief("XYZUSDT", error="unavailable")],
                        BriefSections(), events)
    assert text.startswith("Market brief — Tue 06 Oct 2026, 08:00 (Europe/London)")
    assert "BTCUSDT 100.00 (+2.50% 24h, -1.00% since last brief)" in text
    for part in ("Trend up · RSI 55.0", "Zone: inside H4 demand 99.00–100.00",
                 "Kimi: B+ (long) 5h ago at 99.00 · forecast ▲ Proj: 104.00", "Funding 0.0100% · OI +3.20% 24h",
                 "Levels: support 95.00–96.00 · resistance 110.00–111.00 · D1 high 105.00 / low 97.00",
                 "In a zone: BTCUSDT (demand)", "XYZUSDT: no data (unavailable)",
                 "Today\n  13:30 US CPI (high impact)"):
        assert part in text, part
    assert "Tomorrow" not in text and "demo" not in text.lower()

    quiet = render_brief(now, "Europe/London", "4h", [_coin("BTCUSDT", source="synthetic")],
                         BriefSections(kimi=False, derivatives=False, levels=False, events=False),
                         [{**events[0], "source": "demo"}])
    assert "Kimi" not in quiet and "Funding" not in quiet and "Levels" not in quiet and "Today" not in quiet
    assert "[demo data]" in quiet and "Demo data" in quiet
    demo = render_brief(now, "Europe/London", "4h", [_coin("BTCUSDT")], BriefSections(),
                        [{**events[0], "source": "demo"}])
    assert "Today (demo calendar, not real events)" in demo


# ---------------------------------------------------------------- service --


class FakeMarket:
    async def get_klines(self, symbol, interval, limit):
        return synthetic_klines(symbol, interval, limit), "binance"


class FakeDerivs:
    async def symbol_snapshot(self, symbol):
        return {"funding_rate_pct": 0.0125, "oi_change_24h_pct": -4.5}


class FakeHub:
    async def subscribe(self, symbol, interval):
        return asyncio.Queue()

    async def unsubscribe(self, symbol, interval, queue):
        pass


def _settings(**kw):
    base = dict(alerts_store="memory", alert_history_store="memory", brief_store="memory", data_source="auto",
                telegram_bot_token="", telegram_chat_id="", discord_webhook_url="")
    return Settings(**{**base, **kw})


def _brief(store, client=None, events=None, **kw):
    s = _settings(brief_store=str(store), **kw)
    alerts = AlertService(FakeHub(), s, client=client)
    return alerts, BriefService(FakeMarket(), None, FakeDerivs(), alerts, s, events_provider=events)


def test_build_split_and_scheduled_send_survives_restart(tmp_path):
    sent: list[str] = []

    def handler(req):
        sent.append(json.loads(req.content)["content"])
        return httpx.Response(204)

    store = tmp_path / "brief.json"
    syms = [f"C{i:02d}USDT" for i in range(40)]

    async def events():
        return [{"time": ts(2026, 10, 6, 13, 30), "title": "CPI", "country": "US", "impact": "high"}]

    async def go():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        alerts, b = _brief(store, client, events, discord_webhook_url=WEBHOOK)
        b.update_settings(BriefSettings(enabled=True, times=["08:00"], timezone="Europe/London", symbols=syms),
                          now=ts(2026, 10, 6, 7, 0))
        brief = await b.build(now=ts(2026, 10, 6, 8, 0))
        assert len(brief.messages) > 1 and all(len(m) <= 2000 for m in brief.messages)  # Discord's limit
        assert "Funding 0.0125% · OI -4.50% 24h" in brief.text and "13:30 US CPI" in brief.text
        assert set(brief.prices) == set(syms)

        assert not await b.tick(ts(2026, 10, 6, 7, 59))
        assert await b.tick(ts(2026, 10, 6, 8, 0, 30))
        assert len(sent) == len(brief.messages) and sent[0].startswith("Market brief — Tue 06 Oct 2026, 08:00")
        assert not await b.tick(ts(2026, 10, 6, 8, 1))  # once per slot
        item = alerts.history.list(1)[0]
        assert item["kind"] == "brief" and item["text"].startswith("Market brief")
        await alerts.close()

        # A restart remembers the slot it sent, and the prices for "since last brief".
        alerts2, b2 = _brief(store, httpx.AsyncClient(transport=httpx.MockTransport(handler)), events,
                             discord_webhook_url=WEBHOOK)
        assert not await b2.tick(ts(2026, 10, 6, 8, 2))
        assert b2.settings.symbols == syms
        again = await b2.build(now=ts(2026, 10, 6, 9, 0))
        assert "+0.00% since last brief" in again.text
        await alerts2.close()

    asyncio.run(go())


def test_new_settings_do_not_fire_a_past_slot_and_send_needs_a_channel(tmp_path):
    async def go():
        alerts, b = _brief(tmp_path / "b.json")
        b.update_settings(BriefSettings(enabled=True, times=["08:00"]), now=ts(2026, 10, 6, 8, 10, tz=ZoneInfo("UTC")))
        assert not await b.tick(ts(2026, 10, 6, 8, 11, tz=ZoneInfo("UTC")))
        with pytest.raises(NoChannelError):
            await b.send()
        b.update_settings(BriefSettings(enabled=True, times=["09:00"]), now=ts(2026, 10, 6, 8, 12, tz=ZoneInfo("UTC")))
        assert not await b.tick(ts(2026, 10, 6, 9, 0, 5, tz=ZoneInfo("UTC")))  # due, but nowhere to send it
        assert b._last_slot == ts(2026, 10, 6, 9, 0, tz=ZoneInfo("UTC"))  # and not retried every 30 s
        await alerts.close()

    asyncio.run(go())


# ---------------------------------------------------------------------- API --


def test_api_settings_preview_and_send(monkeypatch):
    monkeypatch.setenv("DATA_SOURCE", "synthetic")
    from app import config
    config.get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            got = client.get("/api/brief/settings").json()
            assert got["settings"]["enabled"] is False and got["settings"]["times"] == ["08:00"]
            assert got["channels"] == {"telegram": False, "discord": False} and got["default_symbols"]
            r = client.put("/api/brief/settings", json={"enabled": True, "times": ["07:30", "13:00"],
                                                        "timezone": "Europe/London", "symbols": ["BTCUSDT"],
                                                        "interval": "1h", "sections": {"kimi": False}})
            assert r.status_code == 200, r.text
            assert r.json()["settings"]["times"] == ["07:30", "13:00"]
            assert r.json()["settings"]["sections"] == {"zones": True, "kimi": False, "derivatives": True,
                                                        "events": True, "levels": True, "market": True, "holdings": True,
                                                        "notes": True, "desk": True}
            assert client.put("/api/brief/settings", json={"timezone": "Nowhere"}).status_code == 422

            p = client.get("/api/brief/preview")
            assert p.status_code == 200, p.text
            body = p.json()
            assert "BTCUSDT" in body["text"] and body["messages"] == [body["text"]]
            assert body["data_source"] == "synthetic" and "[demo data]" in body["text"] and body["generated_at"]
            assert "ETHUSDT" in client.get("/api/brief/preview", params={"symbols": "ETHUSDT"}).json()["text"]

            r = client.post("/api/brief/send")
            assert r.status_code == 400 and "TELEGRAM_BOT_TOKEN" in r.json()["detail"]
    finally:
        config.get_settings.cache_clear()
