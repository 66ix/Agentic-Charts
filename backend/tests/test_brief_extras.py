"""The brief's market mood, coin notes and Binance holdings; the weekly level check; the agent reading holdings."""

import asyncio
import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

os.environ["DATA_SOURCE"] = "synthetic"
os.environ["LLM_PROVIDER"] = "none"

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.agent import holding_facts, portfolio_facts  # noqa: E402
from app.alerts import AlertService  # noqa: E402
from app.brief import BriefSections, BriefService, BriefSettings, holdings_lines, market_line  # noqa: E402
from app.level_review import LevelLog, LoggedLevel, judge  # noqa: E402
from app.schemas import BoxOverlay, Candle, MarketMetrics, Metric  # noqa: E402
from app.ta_agent import _position_lines  # noqa: E402
from tests.test_brief import WEBHOOK, FakeDerivs, FakeHub, FakeMarket, _settings, ts  # noqa: E402

UTC = ZoneInfo("UTC")

POSITIONS = {"manual": {
    "spot": [{"asset": "INJ", "symbol": "INJUSDT", "qty": 120, "own_qty": 120, "avg_entry": 7.2, "price": 7.8,
              "value": 936.0, "unrealized_pnl": 72.0, "untracked_qty": 0},
             {"asset": "DUST", "symbol": "DUSTUSDT", "qty": 1, "own_qty": 1, "avg_entry": None, "price": 1.0,
              "value": 1.0, "unrealized_pnl": None, "untracked_qty": 1}],
    "futures": [{"symbol": "BTCUSDT", "side": "long", "qty": 0.01, "entry_price": 60000.0, "mark_price": 62000.0,
                 "leverage": 10, "liquidation_price": 54500.0, "unrealized_pnl": 20.0}],
    "cash": [{"asset": "USDT", "qty": 500.0}]}}


class FakeMetrics:
    async def get(self):
        return MarketMetrics(updated_at=datetime.now(timezone.utc), metrics=[
            Metric(key="fear_greed", label="Fear & Greed", value=31, display="31/100 · Fear", source="live"),
            Metric(key="btc_dominance", label="BTC Dominance", value=57.3, display="57.30%", change_pct=0.12,
                   source="live"),
            Metric(key="market_cap", label="Market Cap", value=2.9e12, display="$2.90T", source="mock")])


def test_market_line_uses_live_values_only():
    line = market_line(asyncio.run(FakeMetrics().get()))
    assert line == "Market: Fear & Greed 31/100 · Fear · BTC Dominance 57.30% (+0.12%)"
    assert market_line(MarketMetrics(updated_at=datetime.now(timezone.utc), metrics=[])) is None


def test_holdings_lines_skip_dust_and_show_entry_and_futures():
    lines = holdings_lines(POSITIONS)
    assert lines[0] == "Your holdings: $936 in 1 coin"
    assert lines[1] == "INJ 120 · $936 · avg 7.20, +8.3% (+$72)"
    assert lines[2].startswith("BTCUSDT long 0.01 @ 60") and "10x" in lines[2] and "liq 54" in lines[2]
    assert not any("DUST" in x for x in lines)
    assert holdings_lines({"manual": {"spot": [], "futures": []}}) == []


def test_build_has_market_holdings_notes_and_held_coins():
    async def holdings():
        return POSITIONS

    async def go():
        s = _settings(brief_store="memory")
        alerts = AlertService(FakeHub(), s)
        b = BriefService(FakeMarket(), None, FakeDerivs(), alerts, s, metrics=FakeMetrics(),
                         holdings_provider=holdings)
        b.update_settings(BriefSettings(symbols=["BTCUSDT"], include_holdings=True))
        b.set_notes({"injusdt": "Only add below 7", "BTCUSDT": "  "})
        assert b.notes == {"INJUSDT": "Only add below 7"}
        brief = await b.build()
        assert brief.symbols == ["BTCUSDT", "INJUSDT"]  # the held coin is covered too
        assert "Market: Fear & Greed 31/100" in brief.text
        assert "Your holdings: $936 in 1 coin" in brief.text
        assert "Your note: Only add below 7" in brief.text
        quiet = await b.build(sections=BriefSections(market=False, holdings=False, notes=False))
        assert "Market:" not in quiet.text and "Your holdings" not in quiet.text and "Your note" not in quiet.text
        await alerts.close()

    asyncio.run(go())


# ------------------------------------------------------------ level review --


def _c(t, o, h, low, c):
    return Candle(time=t, open=o, high=h, low=low, close=c)


def _lv(side="floor", price=110.0, kind="support"):
    return LoggedLevel(id="x", symbol="INJUSDT", interval="1h", kind=kind, low=100.0, high=102.0, side=side,
                       drawn_at=0, price=price)


def test_judge_held_broke_untested():
    held = judge(_lv(), [_c(1, 110, 111, 104, 105), _c(2, 105, 106, 101, 103), _c(3, 103, 112, 102, 111)])
    assert held["outcome"] == "held" and held["touched_at"] == 2 and held["bounce_pct"] == round((112 / 102 - 1) * 100, 2)
    broke = judge(_lv(), [_c(1, 105, 106, 101, 103), _c(2, 103, 104, 98, 99)])
    assert broke["outcome"] == "broke" and broke["broke_at"] == 2 and broke["close"] == 99
    untested = judge(_lv(), [_c(1, 110, 111, 105, 108)])
    assert untested["outcome"] == "untested" and untested["distance_pct"] > 0
    ceiling = judge(_lv("ceiling", 95.0, "resistance"), [_c(1, 96, 101, 95, 99), _c(2, 99, 100, 90, 91)])
    assert ceiling["outcome"] == "held" and ceiling["bounce_pct"] == 10.0
    inside = judge(_lv(price=101.0), [_c(1, 101, 103, 99.5, 99.9)])
    assert inside["outcome"] == "broke"  # drawn with price inside: touched at once


def test_level_log_records_dedups_and_reviews(tmp_path):
    class Market:
        async def get_klines(self, symbol, interval, limit):
            assert interval == "1h"
            # price falls into the 100-102 support, then rallies
            return [_c(t * 3600, 110, 111, 105, 106) for t in range(1, 3)] + [
                _c(3 * 3600, 106, 107, 101, 103), _c(4 * 3600, 103, 115, 102, 114)], "binance"

    async def go():
        log = LevelLog(Market(), str(tmp_path / "levels.json"))
        ovs = [BoxOverlay(kind="support", price_low=100, price_high=102),
               BoxOverlay(kind="resistance", price_low=130, price_high=132),
               BoxOverlay(kind="fvg_bullish", price_low=90, price_high=91)]
        assert log.record("INJUSDT", "1h", ovs, 110.0, "binance", now=0) == 2
        assert log.record("INJUSDT", "1h", ovs, 110.0, "binance", now=600) == 0  # the same zones again
        assert len(LevelLog(Market(), str(tmp_path / "levels.json")).levels()) == 2  # saved
        review = await log.review(7, now=5 * 3600)
        t = review["totals"]
        assert (t["drawn"], t["held"], t["broke"], t["untested"], t["held_pct"]) == (2, 1, 0, 1, 100.0)
        assert "2 zones on 1 coin: 1 reached, 1 held, 0 broke, 1 not reached yet." in review["text"]
        assert "  H1 support 100.00–102.00: held, moved 12.75% away" in review["text"]
        assert "H1 resistance 130.00–132.00: not reached" in review["text"]
        empty = await LevelLog(Market()).review(7)
        assert "drew no support" in empty["text"]

    asyncio.run(go())


def test_weekly_check_sends_once_on_its_day(tmp_path):
    sent = []

    def handler(req):
        sent.append(json.loads(req.content)["content"])
        return httpx.Response(204)

    class Market:
        async def get_klines(self, symbol, interval, limit):
            return [], "binance"

    async def go():
        s = _settings(brief_store=str(tmp_path / "b.json"), discord_webhook_url=WEBHOOK)
        alerts = AlertService(FakeHub(), s, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        b = BriefService(FakeMarket(), None, FakeDerivs(), alerts, s, levels=LevelLog(Market()))
        b.update_settings(BriefSettings(times=["08:00"], weekly_review=True, review_day=6))  # Sunday
        sunday_8 = ts(2026, 10, 11, 8, 0, 30, tz=UTC)
        assert not await b._weekly(ts(2026, 10, 10, 9, 0, tz=UTC))  # Saturday
        assert not await b._weekly(ts(2026, 10, 11, 7, 59, tz=UTC))  # before the first brief time
        assert await b._weekly(sunday_8)
        assert not await b._weekly(sunday_8 + 60)  # once a week
        assert sent and sent[0].startswith("Weekly level check")
        await alerts.close()

    asyncio.run(go())


# ---------------------------------------------------------------- holdings --


def test_holding_and_portfolio_facts():
    mine = holding_facts(POSITIONS, "INJUSDT")
    assert mine["spot"]["avg_entry"] == 7.2 and mine["spot"]["pnl_pct"] == 8.33 and "futures" not in mine
    assert holding_facts(POSITIONS, "BTCUSDT")["futures"][0]["leverage"] == 10
    assert holding_facts(POSITIONS, "DUSTUSDT") is None  # dust
    assert holding_facts(POSITIONS, "SOLUSDT") is None
    pf = portfolio_facts(POSITIONS)
    assert pf["spot_value_usd"] == 936.0 and pf["stablecoins_usd"] == 500.0 and len(pf["spot"]) == 1
    lines = _position_lines(mine, pf)
    assert lines[0].startswith("You hold 120 INJ ($936), average entry 7.2") and "+8.3%" in lines[0]
    assert any(x.startswith("Your holdings: $936 in 1 coins plus $500 in stablecoins") for x in lines)


def test_the_agent_reads_holdings_when_a_key_is_set():
    from app.main import app

    class FakeBinance:
        account = SimpleNamespace(status=lambda: {"configured": True})

        async def positions(self):
            return POSITIONS

    with TestClient(app) as client:
        real = app.state.binance
        app.state.binance = FakeBinance()
        try:
            res = client.post("/api/agent/analyze", json={"prompt": "how are my holdings doing?", "symbol": "INJUSDT",
                                                          "interval": "1h", "overlays": []})
        finally:
            app.state.binance = real
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["facts"]["your_position"]["spot"]["coin"] == "INJ"
        assert body["facts"]["your_holdings"]["spot_value_usd"] == 936.0
        assert "You hold 120 INJ" in body["summary"]
        r = client.put("/api/brief/notes", json={"notes": {"ethusdt": "watch 3k"}})
        assert r.json()["notes"] == {"ETHUSDT": "watch 3k"}
        assert client.put("/api/brief/notes", json={"notes": [1]}).status_code == 422
        review = client.get("/api/levels/review?days=7").json()
        assert review["totals"]["drawn"] >= 1  # the zones that answer drew were logged
