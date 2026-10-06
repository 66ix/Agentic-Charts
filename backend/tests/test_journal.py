"""Trade journal: deterministic replay on 1m candles, the service (cache, demo-data fallback) and the API."""

import asyncio
import dataclasses
import os
import time

os.environ["DATA_SOURCE"] = "synthetic"
os.environ["LLM_PROVIDER"] = "none"

import pandas as pd  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.journal import (JournalEntry, JournalEvaluation, JournalPatch, JournalService, ManualClose,  # noqa: E402
                         NewJournalEntry, entry_from_plan, evaluate_entry, journal_stats, plan_setup_name,
                         resolution_for)
from app.main import app  # noqa: E402
from app.schemas import PlanTarget, TradePlan  # noqa: E402

T0 = 1_760_000_000 - 1_760_000_000 % 60


def candles(rows, start=T0, step=60):
    """(open, high, low, close) rows → a 1m frame starting at `start`."""
    return pd.DataFrame([{"time": start + i * step, "open": o, "high": h, "low": lo, "close": c, "volume": 1.0}
                         for i, (o, h, lo, c) in enumerate(rows)])


def entry(**kw):
    base = dict(id="e1", symbol="SOLUSDT", interval="4h", direction="long", entry=100.0, stop=98.0,
                targets=[102.0, 104.0], taken_at=T0, created_at=T0, updated_at=1.0)
    base.update(kw)
    return JournalEntry(**base)


# ------------------------------------------------------------ evaluate_entry --

LIFECYCLE = [
    (101.0, 101.5, 100.5, 101.0),  # pending: never reaches 100
    (101.0, 101.2, 99.8, 100.4),   # limit fills at 100 inside the candle
    (100.4, 102.5, 100.2, 102.2),  # T1 102 → half closed, stop to breakeven
    (102.2, 103.0, 99.9, 100.1),   # back to 100: breakeven stop
]


def test_pending_fill_t1_then_breakeven():
    e = entry(risk_usd=100.0)
    ev = evaluate_entry(e, candles(LIFECYCLE[:1]), now=T0 + 120)
    assert ev.status == "pending" and ev.filled_at is None and ev.last_price == 101.0

    ev = evaluate_entry(e, candles(LIFECYCLE[:2]), now=T0 + 180)
    assert ev.status == "open" and ev.filled_at == T0 + 60 and ev.fill_price == 100.0
    assert ev.exits == [] and ev.remaining == 1 and ev.current_stop == 98.0
    assert ev.open_r == pytest.approx(0.2)  # last 100.4, risk 2

    ev = evaluate_entry(e, candles(LIFECYCLE[:3]), now=T0 + 240)
    assert ev.status == "open" and [x.kind for x in ev.exits] == ["T1"]
    assert ev.exits[0].fraction == 0.5 and ev.exits[0].r == 1.0 and ev.remaining == 0.5
    assert ev.current_stop == 100.0  # moved to the fill price after T1
    assert ev.open_r == pytest.approx(0.5 * (102.2 - 100) / 2)

    ev = evaluate_entry(e, candles(LIFECYCLE), now=T0 + 300)
    assert ev.status == "closed" and ev.closed_at == T0 + 180
    assert [(x.kind, x.price, x.fraction, x.r) for x in ev.exits] == [("T1", 102.0, 0.5, 1.0),
                                                                      ("breakeven", 100.0, 0.5, 0.0)]
    fees = 0.5 * (100 + 102) * 0.001 / 2 + 0.5 * (100 + 100) * 0.001 / 2
    assert ev.gross_r == 0.5 and ev.fees_r == pytest.approx(fees, abs=1e-4)
    assert ev.realized_r == pytest.approx(0.5 - fees, abs=1e-4) and ev.outcome == "win"
    assert ev.pnl_usd == pytest.approx(ev.realized_r * 100, abs=0.01)
    assert ev.mfe_r == pytest.approx(1.25) and ev.mae_r == pytest.approx(-0.1)


def test_no_breakeven_management_keeps_the_stop():
    ev = evaluate_entry(entry(manage="none"), candles(LIFECYCLE), now=T0 + 300)
    assert ev.status == "open" and ev.current_stop == 98.0 and [x.kind for x in ev.exits] == ["T1"]


def test_stop_and_target_in_one_candle_counts_the_stop():
    rows = [(101.0, 101.0, 99.9, 100.5),   # fills at 100
            (100.0, 103.5, 97.5, 101.0)]   # both 98 and 103 inside this candle
    ev = evaluate_entry(entry(targets=[103.0]), candles(rows), now=T0 + 200)
    assert ev.status == "closed" and [(x.kind, x.price) for x in ev.exits] == [("stop", 98.0)]
    assert ev.exits[0].r == -1.0 and ev.realized_r < -1.0 and ev.outcome == "loss"
    assert any("counted the stop first" in n for n in ev.notes)


def test_limit_fill_candle_counts_the_stop_but_not_the_target():
    rows = [(101.0, 104.5, 97.0, 99.0)]  # fills at 100, but the low 97 also takes out the stop
    ev = evaluate_entry(entry(), candles(rows), now=T0 + 100)
    assert ev.status == "closed" and [x.kind for x in ev.exits] == ["stop"]
    assert ev.mfe_r == 0 and ev.mae_r == -1.0


def test_market_entry_fills_at_first_open_after_logging():
    rows = [(99.0, 99.0, 90.0, 99.0),       # before taken_at: ignored, even though it is below the stop
            (100.5, 101.0, 100.0, 100.8),   # first candle after logging: fill at its open
            (100.8, 104.2, 100.6, 104.0)]   # one target, hit
    e = entry(entry_type="market", targets=[104.0], taken_at=T0 + 60)
    ev = evaluate_entry(e, candles(rows), now=T0 + 200)
    assert ev.filled_at == T0 + 60 and ev.fill_price == 100.5
    assert ev.status == "closed" and ev.exits[0].kind == "T1"
    assert ev.exits[0].r == pytest.approx((104 - 100.5) / 2)  # R against the planned risk |entry - stop|
    assert any("Market order" in n for n in ev.notes)


def test_limit_gap_fills_at_the_open():
    rows = [(99.5, 99.8, 99.0, 99.6)]
    ev = evaluate_entry(entry(), candles(rows), now=T0 + 100)
    assert ev.fill_price == 99.5 and any("opened past your limit" in n for n in ev.notes)


def test_short_mirrors_long():
    rows = [(99.0, 100.2, 98.8, 99.6),   # fills at 100
            (99.6, 99.7, 95.9, 96.0),    # T1 96 and T2 ... no: T2 94 not yet
            (96.0, 96.1, 93.5, 94.0)]    # T2 94
    e = entry(direction="short", entry=100.0, stop=102.0, targets=[94.0, 96.0])
    assert e.targets == [96.0, 94.0]  # nearest first
    ev = evaluate_entry(e, candles(rows), now=T0 + 200)
    assert ev.status == "closed" and [x.kind for x in ev.exits] == ["T1", "T2"]
    assert ev.gross_r == pytest.approx(0.5 * 2 + 0.5 * 3)


def test_manual_close_ignores_later_candles():
    rows = [(101.0, 101.0, 99.9, 100.5), (100.5, 101.5, 100.2, 101.0), (101.0, 101.0, 97.0, 97.5)]
    e = entry(manual_close=ManualClose(time=T0 + 120, price=101.0))
    ev = evaluate_entry(e, candles(rows), now=T0 + 400)
    assert ev.status == "closed" and [(x.kind, x.price, x.fraction) for x in ev.exits] == [("manual", 101.0, 1.0)]
    assert ev.exits[0].r == 0.5 and ev.closed_at == T0 + 120

    early = entry(manual_close=ManualClose(time=T0, price=101.0))  # before any candle could fill it
    assert evaluate_entry(early, candles(rows), now=T0 + 400).status == "cancelled"

    # Closed during the candle that filled it: it fills, and that candle's stop does not count.
    same = entry(manual_close=ManualClose(time=T0 + 150, price=100.5))
    late = [(101.0, 101.2, 100.6, 101.0), (101.0, 101.5, 100.3, 101.0), (100.5, 100.6, 97.0, 97.5)]
    ev = evaluate_entry(same, candles(late), now=T0 + 400)
    assert ev.filled_at == T0 + 120 and ev.fill_price == 100.0
    assert [(x.kind, x.price) for x in ev.exits] == [("manual", 100.5)]


def test_cancelled_entry():
    ev = evaluate_entry(entry(cancelled=True), candles(LIFECYCLE), now=T0 + 400)
    assert ev.status == "cancelled" and ev.exits == []


def test_entry_validation():
    with pytest.raises(ValueError):
        NewJournalEntry(symbol="SOL", direction="long", entry=100, stop=101, targets=[105])
    with pytest.raises(ValueError):
        NewJournalEntry(symbol="SOL", direction="short", entry=100, stop=101, targets=[105])
    ok = NewJournalEntry(symbol="sol/usdt", direction="long", entry=100, stop=99, targets=[104, 102, 102],
                         tags=[" a ", "a", "b"])
    assert ok.symbol == "SOLUSDT" and ok.targets == [102, 104] and ok.tags == ["a", "b"]


def test_resolution_falls_back_for_old_trades():
    now = time.time()
    assert resolution_for(int(now) - 30 * 86400, now) == "1m"
    assert resolution_for(int(now) - 500 * 86400, now) == "5m"
    assert resolution_for(int(now) - 2500 * 86400, now) == "15m"


# -------------------------------------------------------------------- stats --


def _closed(i, r, setup="H4 demand", symbol="SOLUSDT", direction="long"):
    e = entry(id=f"e{i}", setup=setup, symbol=symbol, direction=direction,
              **({"stop": 102.0, "targets": [96.0]} if direction == "short" else {}))
    return e, JournalEvaluation(status="closed", realized_r=r, closed_at=T0 + i * 60, data_source="binance")


def test_stats_arithmetic():
    rows = [_closed(1, 2.0), _closed(2, -1.0, symbol="ETHUSDT"), _closed(3, 0.05, setup="Kimi B+"),
            _closed(4, 1.0, direction="short"), _closed(5, -1.0, setup="Kimi B+"),
            (entry(id="o1"), JournalEvaluation(status="open", open_r=0.4)),
            (entry(id="p1"), JournalEvaluation(status="pending")),
            (entry(id="c1"), JournalEvaluation(status="cancelled"))]
    s = journal_stats(rows)
    assert (s["count"], s["closed"], s["open"], s["pending"], s["cancelled"]) == (7, 5, 1, 1, 1)
    assert (s["wins"], s["losses"], s["breakeven"]) == (2, 2, 1)
    assert s["win_rate"] == pytest.approx(0.4)
    assert s["total_r"] == pytest.approx(1.05) and s["avg_r"] == pytest.approx(0.21)
    assert s["expectancy"] == pytest.approx(0.4 * 1.5 + 0.4 * -1.0)
    assert s["profit_factor"] == pytest.approx(3.05 / 2)
    assert (s["best_r"], s["worst_r"], s["open_r"]) == (2.0, -1.0, 0.4)
    assert [p["r_cum"] for p in s["equity"]] == pytest.approx([2.0, 1.0, 1.05, 2.05, 1.05])
    by_setup = {g["key"]: g for g in s["by_setup"]}
    assert by_setup["H4 demand"]["trades"] == 3 and by_setup["Kimi B+"]["total_r"] == pytest.approx(-0.95)
    assert {g["key"] for g in s["by_symbol"]} == {"SOLUSDT", "ETHUSDT"}
    assert {g["key"]: g["trades"] for g in s["by_direction"]} == {"long": 4, "short": 1}
    assert any("too few" in n for n in s["notes"])

    only = journal_stats(rows, setup="kimi b+")
    assert only["closed"] == 2 and only["total_r"] == pytest.approx(-0.95)
    assert journal_stats(rows, direction="short")["closed"] == 1
    assert journal_stats(rows, symbol="ETHUSDT")["closed"] == 1
    empty = journal_stats([])
    assert empty["count"] == 0 and empty["win_rate"] is None and empty["equity"] == []


def test_plan_helpers():
    assert plan_setup_name("H4 Demand (fresh) + D1 23.9–24.2") == "H4 demand"
    assert plan_setup_name("H4 Support 65,000.00–65,400.00") == "H4 support"
    assert plan_setup_name("last swing low 23.5") == "swing low"
    plan = TradePlan(direction="long", entry=24.2, stop=23.8, targets=[PlanTarget(price=25.0, label="T1 x", rr=2.0)],
                     basis="H4 Demand (fresh) 23.9–24.2", risk_pct=1.65)
    new = entry_from_plan(plan, "INJUSDT", "4h", risk_usd=50)
    assert (new.setup, new.source, new.targets, new.risk_usd) == ("H4 demand", "agent_plan", [25.0], 50)


# ------------------------------------------------------------------ service --


class FakeMarket:
    """get_range from a fixed frame; `source` can be switched to simulate a Binance outage."""

    def __init__(self, df):
        self.df = df
        self.source = "binance"
        self.calls = 0

    async def get_range(self, symbol, interval, start, end=None):
        self.calls += 1
        return self.df[self.df["time"] >= start - start % 60].reset_index(drop=True), self.source


def _service(df, data_source="auto"):
    s = dataclasses.replace(get_settings(), data_source=data_source, journal_store="memory")
    return JournalService(FakeMarket(df), s)


def test_service_cache_cancel_close_and_outage():
    now = int(time.time()) // 60 * 60
    df = candles(LIFECYCLE[:2], start=now - 120)  # filled, still open
    svc = _service(df)

    async def run():
        e, ev = await svc.add(NewJournalEntry(symbol="SOLUSDT", direction="long", entry=100, stop=98,
                                              targets=[102, 104], taken_at=now - 120, risk_usd=200))
        assert ev.status == "open" and svc.market.calls == 1
        assert (await svc.evaluate(e)).status == "open" and svc.market.calls == 1  # cached until the next 1m bar

        with pytest.raises(ValueError):
            await svc.update(e.id, JournalPatch(cancel=True))  # already filled

        svc.market.source = "synthetic"  # Binance down: keep the last real evaluation
        svc._evals[e.id].expires = 0
        stale = await svc.evaluate(e)
        assert stale.data_source == "binance" and "unreachable" in (stale.error or "")
        svc.market.source = "binance"

        e2, ev2 = await svc.update(e.id, JournalPatch(notes="took it early", tags=["a"],
                                                      close={"price": 101.0, "time": now - 1}))
        assert e2.notes == "took it early" and e2.manual_close.price == 101.0
        assert ev2.status == "closed" and ev2.exits[-1].kind == "manual" and ev2.pnl_usd == pytest.approx(
            ev2.realized_r * 200, abs=0.01)

        p, pev = await svc.add(NewJournalEntry(symbol="SOLUSDT", direction="long", entry=90, stop=89,
                                               targets=[95], taken_at=now - 120))
        assert pev.status == "pending"
        _, cev = await svc.update(p.id, JournalPatch(cancel=True))
        assert cev.status == "cancelled"
        stats = await svc.stats()
        assert stats["closed"] == 1 and stats["cancelled"] == 1
        assert await svc.remove(p.id) and not await svc.remove(p.id)
        assert await svc.update("missing", JournalPatch(notes="x")) is None

    asyncio.run(run())


def test_service_persists(tmp_path):
    path = tmp_path / "journal.json"
    s = dataclasses.replace(get_settings(), journal_store=str(path))
    df = candles(LIFECYCLE[:1], start=int(time.time()) // 60 * 60 - 60)

    async def run():
        svc = JournalService(FakeMarket(df), s)
        e, _ = await svc.add(NewJournalEntry(symbol="SOLUSDT", direction="long", entry=100, stop=98, targets=[102],
                                             setup="manual", notes="n"))
        again = JournalService(FakeMarket(df), s)
        assert [x.id for x in again.list()] == [e.id] and again.get(e.id).notes == "n"

    asyncio.run(run())


# ---------------------------------------------------------------------- API --


def test_journal_api_on_demo_data():
    with TestClient(app) as client:
        body = {"symbol": "SOLUSDT", "interval": "4h", "direction": "long", "entry": 150.0, "stop": 140.0,
                "targets": [160.0, 170.0], "setup": "H4 demand", "source": "agent_plan", "entry_type": "market",
                "risk_usd": 100, "taken_at": int(time.time()) - 3600}
        r = client.post("/api/journal", json=body)
        assert r.status_code == 200, r.text
        e = r.json()["entry"]
        assert e["id"] and e["evaluation"]["status"] in ("open", "closed")
        assert e["evaluation"]["data_source"] == "synthetic"
        assert any("synthetic" in n for n in e["evaluation"]["notes"])

        bad = client.post("/api/journal", json={**body, "stop": 155.0})
        assert bad.status_code == 422
        future = client.post("/api/journal", json={**body, "taken_at": int(time.time()) + 3600})
        assert future.status_code == 422

        listed = client.get("/api/journal").json()["entries"]
        assert [x["id"] for x in listed] == [e["id"]]

        r = client.patch(f"/api/journal/{e['id']}", json={"notes": "good entry", "tags": ["A+"]})
        assert r.status_code == 200 and r.json()["entry"]["notes"] == "good entry"
        if e["evaluation"]["status"] == "open":
            r = client.patch(f"/api/journal/{e['id']}", json={"close": {}})
            assert r.status_code == 200, r.text
            assert r.json()["entry"]["evaluation"]["exits"][-1]["kind"] in ("manual", "stop", "T1", "T2",
                                                                            "breakeven")
        assert client.patch(f"/api/journal/{e['id']}", json={"cancel": True}).status_code == 409
        assert client.patch("/api/journal/nope", json={"notes": "x"}).status_code == 404

        stats = client.get("/api/journal/stats", params={"symbol": "sol/usdt"}).json()
        assert stats["count"] == 1 and "by_setup" in stats and "equity" in stats
        assert client.get("/api/journal/stats", params={"direction": "up"}).status_code == 422

        assert client.delete(f"/api/journal/{e['id']}").json() == {"ok": True}
        assert client.delete(f"/api/journal/{e['id']}").status_code == 404
        assert client.get("/api/journal").json()["entries"] == []
