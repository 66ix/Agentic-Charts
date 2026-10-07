"""Trade post-mortems (facts, lessons, template, LLM guard), the weekly review and its schedule, and the API."""

import asyncio
import dataclasses
import os
import time
from datetime import datetime, timezone
from types import SimpleNamespace

os.environ["DATA_SOURCE"] = "synthetic"
os.environ["LLM_PROVIDER"] = "none"

import pandas as pd  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.journal import (ImportedTrade, JournalEntry, JournalEvaluation, JournalService, ManualClose,  # noqa: E402
                         NewJournalEntry, PostMortem, entry_from_plan, evaluate_entry)
from app.main import app  # noqa: E402
from app.market_data import candles_to_df, synthetic_klines  # noqa: E402
from app.postmortem import (PostMortemService, ReviewSettings, entry_context, grounded, kimi_context,  # noqa: E402
                            lessons_for, render_postmortem, split_llm, trade_facts, weekly_due_slot, weekly_review)
from app.schemas import PlanTarget, TradePlan  # noqa: E402

T0 = 1_760_000_000 - 1_760_000_000 % 60


def candles(rows, start=T0, step=60):
    return pd.DataFrame([{"time": start + i * step, "open": o, "high": h, "low": lo, "close": c, "volume": 1.0}
                         for i, (o, h, lo, c) in enumerate(rows)])


def entry(**kw):
    base = dict(id="e1", symbol="SOLUSDT", interval="4h", direction="long", entry=100.0, stop=98.0,
                targets=[102.0, 104.0], taken_at=T0, created_at=T0, updated_at=1.0, setup="H4 demand")
    base.update(kw)
    return JournalEntry(**base)


PENDING, FILL = (101, 101.2, 100.5, 100.8), (100.8, 101, 99.8, 100.2)  # the limit at 100 fills in the 2nd candle
# Stopped at 98, then price runs to T1 anyway.
STOP_THEN_T1 = [PENDING, FILL, (100.2, 100.9, 99.5, 99.7), (99.7, 99.8, 97.9, 98.2), (98.2, 99.5, 98.1, 99.4),
                (99.4, 101.0, 99.3, 100.9), (100.9, 102.5, 100.8, 102.3)]
# T1 at +1R, then all the way back to the stop.
T1_THEN_STOP = [PENDING, FILL, (100.2, 102.5, 100.1, 102.2), (102.2, 102.3, 97.5, 97.8)]
# Closed by hand at 101, then both targets trade.
CLOSED_EARLY = [PENDING, FILL, (100.2, 101.5, 100.1, 101.2), (101.2, 101.4, 100.9, 101.0), (101.0, 104.5, 100.9, 104.2)]
# A market buy at 100.5 above the zone 97.5–99, straight to the stop.
CHASED = [(100.5, 100.6, 99.9, 100.2), (100.2, 100.3, 99.0, 99.2), (99.2, 99.3, 97.8, 98.0), (98.0, 98.5, 97.5, 98.1)]


def closed(e, rows):
    df = candles(rows)
    ev = evaluate_entry(e, df, now=T0 + 3600)
    assert ev.status == "closed"
    return df, ev


# ------------------------------------------------------------ facts + lessons --


def test_stopped_then_target_says_the_stop_was_too_tight():
    e = entry(zone_low=99.0, zone_high=100.5)
    df, ev = closed(e, STOP_THEN_T1)
    f = trade_facts(e, ev, df)
    assert f["zone"] == {"low": 99.0, "high": 100.5, "label": "H4 demand", "from": "plan", "position": "inside",
                         "depth_pct": 33, "stop_beyond": True}
    assert f["excursion"] == {"mfe_r": 0.45, "mae_r": -1.0, "mfe_price": 100.9, "mae_price": 98.0, "heat_first": False}
    t1, t2 = f["targets"]
    assert (t1["hit"], t1["short_by_r"], t1["reached_after_exit_h"]) == (False, 0.55, 0.05)
    assert t2["hit"] is False and "reached_after_exit_h" not in t2
    assert f["result"]["closed_by"] == "stop" and f["after_exit"]["best_price"] == 102.5
    lessons = lessons_for(f)
    assert [c for c, _ in lessons] == ["stop_too_tight"]
    assert lessons[0][1].startswith("The stop at 98.00 was hit, then price reached T1 102.00 3 min later")
    text = render_postmortem(f)
    assert text.startswith("Long SOLUSDT (H4 demand, H4) closed as a loss (-1.10R): filled at 100.00, then stopped "
                           "at 98.00, 2 min after the fill.")
    assert "33% deep into the H4 demand 99.00–100.50" in text and "reached 3 min after the exit" in text
    assert grounded(text + " ".join(t for _, t in lessons), f)  # the template only uses numbers from the facts


def test_gave_back_and_closed_early_and_chased():
    e = entry(targets=[102.0, 106.0], manage="none", risk_usd=100.0)
    df, ev = closed(e, T1_THEN_STOP)
    f = trade_facts(e, ev, df)
    assert [c for c, _ in lessons_for(f)] == ["gave_back"]
    assert "closed at breakeven (-0.10R, −$10.00)" in render_postmortem(f)

    e = entry(manual_close=ManualClose(time=T0 + 240, price=101.0))
    df, ev = closed(e, CLOSED_EARLY)
    f = trade_facts(e, ev, df)
    lessons = lessons_for(f)
    assert [c for c, _ in lessons] == ["closed_early", "target_too_far"]
    assert "T1 102.00 was reached under a minute later" in lessons[0][1]

    e = entry(zone_low=97.5, zone_high=99.0, entry_type="market")
    df, ev = closed(e, CHASED)
    ctx = {"timeframe": "H4", "trend": "down", "rsi": 41.2, "support": {"low": 96.5, "high": 97.2}}
    kimi = {"last_signal": {"label": "B-", "direction": "short", "price": 101.2, "bars_before": 2}}
    f = trade_facts(e, ev, df, ctx, kimi)
    assert f["zone"]["position"] == "outside" and f["zone"]["chased"] and f["zone"]["distance_r"] == 0.75
    assert [c for c, _ in lessons_for(f)] == ["chased_entry", "against_trend"]  # at most two
    text = render_postmortem(f)
    assert "1.49% above the H4 demand 97.50–99.00" in text
    assert ("At entry the H4 trend was down with RSI 41.2, the agent's nearest support was 96.50–97.20, Kimi's last "
            "label was B- (short) at 101.20, 2 bars earlier.") in text


def test_short_clean_win_and_open_trades():
    e = entry(direction="short", entry=100.0, stop=102.0, targets=[98.0])
    rows = [(99.0, 99.5, 98.8, 99.2), (99.2, 100.2, 99.1, 99.9), (99.9, 100.1, 97.5, 97.8)]
    df, ev = closed(e, rows)
    f = trade_facts(e, ev, df)
    assert ev.outcome == "win" and f["targets"][0]["hit"] and f["excursion"]["mfe_price"] == 98.0
    assert lessons_for(f)[0][0] == "clean"
    with pytest.raises(ValueError):
        trade_facts(e, evaluate_entry(e, candles(rows[:2]), now=T0 + 3600), candles(rows[:2]))


def test_entry_context_and_kimi_context():
    df = candles_to_df(synthetic_klines("SOLUSDT", "4h", 300))
    ctx = entry_context(df, "4h")
    assert ctx["timeframe"] == "H4" and ctx["trend"] in ("up", "down", "range") and "zones" in ctx
    last = ctx["price"]
    assert ctx["support"] is None or ctx["support"]["low"] <= last * 1.5
    assert entry_context(df.iloc[:30], "4h") is None

    sig = lambda text, d, t: SimpleNamespace(text=text, direction=d, confirm_time=t, price=100.0)  # noqa: E731
    lvl = lambda side, p, start, end: SimpleNamespace(side=side, price=p, zone_low=p - 1, zone_high=p + 1,  # noqa: E731
                                                      time_start=start, time_end=end)
    k = SimpleNamespace(signals=[sig("B+", "long", T0 - 3 * 14400), sig("B-", "short", T0 + 14400)],
                        levels=[lvl("support", 95.0, T0 - 50 * 14400, None), lvl("support", 97.0, T0 - 9e5, T0 - 10),
                                lvl("resistance", 105.0, T0 - 10 * 14400, T0 + 99), lvl("resistance", 103.0, T0 + 1, None)])
    out = kimi_context(k, T0, "4h", 100.0)
    # The B- came after the entry, the 97 support was broken before it and the 103 resistance printed after it.
    assert out == {"last_signal": {"label": "B+", "direction": "long", "price": 100.0, "bars_before": 3},
                   "support": {"price": 95.0, "low": 94.0, "high": 96.0},
                   "resistance": {"price": 105.0, "low": 104.0, "high": 106.0}}
    assert kimi_context(SimpleNamespace(signals=[], levels=[]), T0, "4h", 100.0) is None


def test_llm_text_is_checked_against_the_facts():
    e = entry(zone_low=99.0, zone_high=100.5)
    df, ev = closed(e, STOP_THEN_T1)
    f = trade_facts(e, ev, df)
    good = ("The long filled at 100.00, a third of the way into the zone, and was stopped at 98.00 for -1.10R. "
            "Price only got to 100.90 first.\n- Put the stop below 98.00 with a buffer.\n- Size down.")
    summary, lessons = split_llm(good)
    assert summary.startswith("The long filled") and lessons == ["Put the stop below 98.00 with a buffer.", "Size down."]
    assert grounded(good, f)
    assert not grounded(good.replace("100.90", "101.37"), f)          # an invented price
    assert not grounded("Stop at 97.95 would have held.", f)          # close, but not the printed precision
    assert grounded("It took 3 min and 2 tries in 2025.", f)          # counts and years are fine


class FakeMarket:
    """1m candles from a fixed frame; other intervals have no data (no entry context)."""

    def __init__(self, df, source="binance"):
        self.df, self.source = df, source

    async def get_range(self, symbol, interval, start, end=None):
        if interval != "1m":
            return self.df.iloc[:0], self.source
        return self.df[self.df["time"] >= start - start % 60].reset_index(drop=True), self.source


class FakeLLM:
    def __init__(self, text):
        self.text, self.calls = text, 0

    def available(self):
        return True

    async def write(self, system, user):
        self.calls += 1
        assert "FACTS" in user and "DRAFT" in user
        return (self.text, "fake:model") if self.text else None


class FakeAlerts:
    def __init__(self, channels=True):
        self.channels = ["telegram"] if channels else []
        self.sent, self.recorded = [], []
        self.channel_status = {"telegram": channels, "discord": False}

    async def send_text(self, text):
        self.sent.append(text)
        return {"telegram": True}

    def record(self, kind, symbol, title, text, **kw):
        self.recorded.append((kind, title))
        return {}


def _setup(rows, llm=None, alerts=None, data_source="auto"):
    now = int(time.time()) // 60 * 60
    start = now - 60 * len(rows) - 600
    s = dataclasses.replace(get_settings(), data_source=data_source, journal_store="memory",
                            journal_review_store="memory")
    market = FakeMarket(candles(rows, start=start))
    journal = JournalService(market, s)
    return journal, PostMortemService(journal, market, llm, alerts, settings=s), start


def test_service_writes_stores_and_rewrites():
    async def run():
        journal, pms, start = _setup(STOP_THEN_T1)
        e, ev = await journal.add(NewJournalEntry(symbol="SOLUSDT", direction="long", entry=100, stop=98,
                                                  targets=[102, 104], taken_at=start, zone_low=99, zone_high=100.5))
        assert ev.status == "closed" and pms.stale(e, ev)
        got = await pms.generate(e.id)
        pm = got.postmortem
        assert pm.engine == "template" and pm.lesson_codes == ["stop_too_tight"] and pm.closed_at == ev.closed_at
        assert "stopped at 98.00" in pm.summary and pm.facts["zone"]["from"] == "plan"
        assert journal.get(e.id).postmortem == pm and not pms.stale(journal.get(e.id), ev)
        assert (await journal.evaluate(journal.get(e.id))) == ev  # storing it is not an edit
        assert await pms.generate("missing") is None

        p, pev = await journal.add(NewJournalEntry(symbol="SOLUSDT", direction="long", entry=90, stop=89,
                                                   targets=[95], taken_at=start))
        with pytest.raises(ValueError):
            await pms.generate(p.id)  # never filled
        assert pms.schedule_missing([(p, pev), (journal.get(e.id), ev)]) == 0

        # The model's text is used when every number checks out, else the template stays.
        pms.llm = FakeLLM("Filled at 100.00 inside the zone and stopped at 98.00 for -1.10R, then price went to "
                          "102.00 anyway.\n- Put the stop under 98.00 with room.")
        pm2 = (await pms.generate(e.id)).postmortem
        assert pm2.engine == "fake:model" and pm2.lessons == ["Put the stop under 98.00 with room."]
        assert pm2.lesson_codes == ["stop_too_tight"]
        pms.llm = FakeLLM("Filled at 100.00 and stopped at 97.13.\n- Use 96.40 next time.")
        assert (await pms.generate(e.id)).postmortem.engine == "template"

    asyncio.run(run())


def test_service_background_and_demo_fallback():
    async def run():
        journal, pms, start = _setup(T1_THEN_STOP)
        e, ev = await journal.add(NewJournalEntry(symbol="SOLUSDT", direction="long", entry=100, stop=98,
                                                  targets=[102, 106], manage="none", taken_at=start))
        assert await pms.sweep() == 1 and await pms.sweep() == 0  # already running
        await asyncio.gather(*pms._busy.values())
        assert journal.get(e.id).postmortem.lesson_codes == ["gave_back"]
        assert await pms.sweep() == 0

        pms.market.source = "synthetic"  # Binance down: no review written on demo prices
        with pytest.raises(Exception, match="unreachable"):
            await pms.generate(e.id)
        await pms.close()

    asyncio.run(run())


# ------------------------------------------------------------- weekly review --


def _row(eid, symbol, setup, r, closed_at, codes=()):
    e = JournalEntry(id=eid, symbol=symbol, interval="4h", direction="long", entry=100, stop=98, targets=[104],
                     setup=setup, taken_at=closed_at - 3600, created_at=closed_at - 3600)
    if codes:
        e = e.model_copy(update={"postmortem": PostMortem(generated_at=closed_at, summary="s",
                                                          lessons=[f"lesson {c}" for c in codes],
                                                          lesson_codes=list(codes), closed_at=closed_at)})
    outcome = "win" if r > 0.1 else "loss" if r < -0.1 else "breakeven"
    return e, JournalEvaluation(status="closed", realized_r=r, outcome=outcome, closed_at=closed_at,
                                data_source="binance")


def test_weekly_review():
    now = 1_760_000_000
    day = 86400
    rows = [
        _row("a", "SOLUSDT", "H4 demand", 2.0, now - day, ["clean"]),
        _row("b", "SOLUSDT", "H4 demand", 1.0, now - 2 * day, ["early_entry"]),
        _row("c", "INJUSDT", "Kimi B+", -1.0, now - 3 * day, ["stop_too_tight", "against_trend"]),
        _row("d", "BTCUSDT", "Kimi B+", -1.1, now - 4 * day, ["stop_too_tight"]),
        _row("old", "BTCUSDT", "manual", 5.0, now - 10 * day, ["stop_too_tight"]),
    ]
    rows.append((JournalEntry(id="o", symbol="ETHUSDT", direction="long", entry=1, stop=0.5, targets=[2], taken_at=now,
                              created_at=now), JournalEvaluation(status="open")))
    w = weekly_review(rows, now)
    assert (w["closed"], w["wins"], w["losses"], w["win_rate"], w["total_r"]) == (4, 2, 2, 0.5, 0.9)
    assert w["avg_r"] == pytest.approx(0.225)
    assert w["best_setup"]["key"] == "H4 demand" and w["worst_setup"]["key"] == "Kimi B+"
    assert w["best_symbol"]["key"] == "SOLUSDT" and w["worst_symbol"]["key"] == "BTCUSDT"
    assert [(r["code"], r["count"]) for r in w["recurring"]] == [("stop_too_tight", 2)]
    assert [t["id"] for t in w["trades"]] == ["a", "b", "c", "d"] and w["open"] == 1
    assert w["text"].splitlines()[1:] == [
        "4 closed: 2 won, 2 lost, 0 breakeven (50% win rate). Average +0.23R, total +0.90R.",
        "Best setup: H4 demand (2, avg +1.50R). Worst: Kimi B+ (2, avg -1.05R).",
        "Best coin: SOLUSDT (+3.00R). Worst: BTCUSDT (-1.10R).",
        "Keeps coming back: stop too tight (stopped, then the target) (2x).",
        "e.g. lesson stop_too_tight",
        "1 trade still open.",
    ]
    # A Binance import without a stop: counted with its PnL, kept out of R, the trade list and post-mortems.
    imp = JournalEntry(id="imp", symbol="BTCUSDT", direction="long", entry=100, stop=None, targets=[],
                       taken_at=now - 7200, created_at=now - 7200,
                       imported=ImportedTrade(external_id="x", market="spot", opened_at=now - 7200, closed_at=now - 3600,
                                              qty=1, entry_price=100, exit_price=112.5, realized_pnl=12.5, fills=2))
    imp_ev = JournalEvaluation(status="closed", realized_r=0, outcome="win", closed_at=now - 3600, pnl_usd=12.5,
                               data_source="binance")
    wi = weekly_review(rows + [(imp, imp_ev)], now)
    assert (wi["closed"], wi["total_r"], wi["imported_without_stop"], wi["imported_pnl"]) == (4, 0.9, 1, 12.5)
    assert "imp" not in [t["id"] for t in wi["trades"]]
    assert "1 imported Binance trade without a stop (+$12.50), not in the R numbers." in wi["text"]
    assert not PostMortemService.stale(imp, imp_ev)
    empty = weekly_review(rows[-1:], now)
    assert empty["closed"] == 0 and "No trades closed in the last 7 days. 1 still open." in empty["text"]
    assert weekly_review(rows, now, days=30)["closed"] == 5


def test_weekly_schedule():
    cfg = ReviewSettings(enabled=True, weekday=6, time="18:00", timezone="UTC")
    sunday = datetime(2026, 10, 4, 18, 0, tzinfo=timezone.utc).timestamp()
    assert weekly_due_slot(cfg, sunday + 60, 0) == sunday
    assert weekly_due_slot(cfg, sunday + 60, sunday) is None          # already sent
    assert weekly_due_slot(cfg, sunday - 60, 0) is None                # last week's slot is too old to send late
    assert weekly_due_slot(cfg, sunday + 2 * 3600, 0) is None          # missed by more than the grace period
    assert weekly_due_slot(cfg, sunday + 7 * 86400 + 5, sunday) == sunday + 7 * 86400
    with pytest.raises(ValueError):
        ReviewSettings(time="25:00")
    with pytest.raises(ValueError):
        ReviewSettings(timezone="Mars/Base")

    async def run():
        alerts = FakeAlerts()
        journal, pms, _ = _setup(T1_THEN_STOP, alerts=alerts)
        pms.update_settings(cfg, now=sunday - 3600)
        assert not await pms.tick(sunday - 60)
        assert await pms.tick(sunday + 60) and not await pms.tick(sunday + 120)
        assert alerts.recorded == [("brief", "Weekly trade review")] and alerts.sent[0].startswith("Weekly trade review")
        assert pms.status()["last_sent_at"] == int((sunday + 60) * 1000)
        pms.alerts = FakeAlerts(channels=False)
        from app.brief import NoChannelError
        with pytest.raises(NoChannelError):
            await pms.send_weekly()

    asyncio.run(run())


def test_plan_zone_reaches_the_journal():
    plan = TradePlan(direction="long", entry=24.2, stop=23.8, targets=[PlanTarget(price=25.0, label="T1 x", rr=2.0)],
                     basis="H4 Demand (fresh) 23.9–24.2", risk_pct=1.65, zone_low=23.9, zone_high=24.2)
    new = entry_from_plan(plan, "INJUSDT", "4h")
    assert (new.zone_low, new.zone_high) == (23.9, 24.2)
    with pytest.raises(ValueError):
        NewJournalEntry(symbol="SOLUSDT", direction="long", entry=100, stop=98, targets=[102], zone_low=99)
    swapped = NewJournalEntry(symbol="SOLUSDT", direction="long", entry=100, stop=98, targets=[102], zone_low=100.5,
                              zone_high=99)
    assert (swapped.zone_low, swapped.zone_high) == (99, 100.5)


# ---------------------------------------------------------------------- API --


def test_postmortem_and_review_api_on_demo_data():
    with TestClient(app) as client:
        app.state.postmortems.kimi = None  # keep the test fast
        body = {"symbol": "SOLUSDT", "interval": "4h", "direction": "long", "entry": 150.0, "stop": 140.0,
                "targets": [160.0, 170.0], "setup": "H4 demand", "entry_type": "market", "risk_usd": 100,
                "taken_at": int(time.time()) - 3600, "zone_low": 145.0, "zone_high": 151.0}
        e = client.post("/api/journal", json=body).json()["entry"]
        assert e["zone_low"] == 145.0 and e["postmortem"] is None
        if e["evaluation"]["status"] == "open":
            assert client.post(f"/api/journal/{e['id']}/postmortem").status_code == 409
            assert client.patch(f"/api/journal/{e['id']}", json={"close": {}}).status_code == 200
        r = client.post(f"/api/journal/{e['id']}/postmortem")
        assert r.status_code == 200, r.text
        pm = r.json()["entry"]["postmortem"]
        assert pm["engine"] == "template" and pm["summary"].startswith("Long SOLUSDT") and pm["lessons"]
        assert "synthetic demo data" in pm["summary"]
        assert client.post("/api/journal/nope/postmortem").status_code == 404

        review = client.get("/api/journal/review", params={"days": 7}).json()
        assert review["closed"] == 1 and review["text"].startswith("Weekly trade review")
        assert client.get("/api/journal/review", params={"days": 99}).status_code == 422

        st = client.get("/api/journal/review/settings").json()
        assert st["settings"]["enabled"] is False and st["channels"] == {"telegram": False, "discord": False}
        saved = client.put("/api/journal/review/settings", json={"enabled": True, "weekday": 0, "time": "9:30",
                                                                 "timezone": "Europe/London"}).json()
        assert saved["settings"]["time"] == "09:30" and saved["settings"]["weekday"] == 0
        assert client.put("/api/journal/review/settings", json={"weekday": 7}).status_code == 422
        assert client.post("/api/journal/review/send").status_code == 400  # no channel configured
        assert client.delete(f"/api/journal/{e['id']}").json() == {"ok": True}
