"""The agent desk: when it runs, how it sizes, how calls are scored on candles, what it learns, and a full run on demo
candles with a paper wallet, notifications and the API."""

import asyncio
import os

import pandas as pd
import pytest

from app.agent_desk import AgentDesk, DeskSettings, due_bars, stale
from app.config import Settings
from app.db import Database
from app.desk_calls import (ENTRY_BARS, HOLD_BARS, DeskCall, expected_r, kelly, score_call, size_for)
from app.desk_learning import (MIN_TP_SAMPLES, calibration, estimate, random_odds, take_profit, verdict)
from app.market_data import INTERVAL_SECONDS, candles_to_df, synthetic_klines
from app.paper import PaperService
from app.schemas import TrackRecord

H = 3600
T0 = 1_760_000_400 - 1_760_000_400 % (4 * H)  # a 4h boundary


def _call(**kw) -> DeskCall:
    base = dict(id="c1", symbol="INJUSDT", interval="1h", created_at=T0, bar_time=T0 - H, price_at_call=10.5,
                entry=10.0, zone_low=9.8, zone_high=10.0, stop=9.7, tp=11.0, tp_level=11.0, rr=3.33, risk_pct=3.0,
                confidence=0.5, expected_r=0.5, kelly=0.3, kind="demand", bucket="1h|demand|fresh|htf|with",
                setup="H1 demand (fresh)", expires_at=T0 + ENTRY_BARS["1h"] * H, hold_seconds=HOLD_BARS["1h"] * H)
    base.update(kw)
    return DeskCall(**base)


def _df(rows, step=300, start=T0):
    """Candles from (open, high, low, close) rows, `step` seconds apart from `start`."""
    return pd.DataFrame([{"time": start + i * step, "open": o, "high": h, "low": lo, "close": c, "volume": 1.0}
                         for i, (o, h, lo, c) in enumerate(rows)])


# --------------------------------------------------------------------- scheduling --


def test_due_bars_waits_for_the_close_and_runs_each_candle_once():
    now = T0 + 20  # 20 s after a 4h/1h close: too fresh, the previous candles are the latest closed ones
    assert dict(due_bars(["1h", "4h"], {}, now)) == {"1h": T0 - 2 * H, "4h": T0 - 8 * H}
    now = T0 + 60
    due = dict(due_bars(["1h", "4h", "1d"], {}, now))
    assert due["1h"] == T0 - H and due["4h"] == T0 - 4 * H
    assert due_bars(["1h"], {"1h": T0 - H}, now) == []  # already looked at
    assert not stale("1h", T0 - H, T0 + 60) and stale("1h", T0 - H, T0 + 40 * 60)
    assert not stale("1d", T0 - 86400, T0 + 3600)


# ------------------------------------------------------------------------- sizing --


def test_sizing_scales_with_confidence_and_respects_the_cash():
    assert kelly(0.5, 2.0) == pytest.approx(0.25)
    assert expected_r(0.5, 2.0, 2.0) == pytest.approx(0.5 - 0.1)  # fees: 0.2% of a 2% risk = 0.1R
    k_lo, pct_lo, n_lo, _ = size_for(0.45, 2.0, 1000, 1000)
    k_hi, pct_hi, n_hi, _ = size_for(0.65, 2.0, 1000, 1000)
    assert 0 < pct_lo < pct_hi <= 15 and n_lo < n_hi
    assert size_for(0.3, 1.5, 1000, 1000)[2] is None              # no edge: nothing placed
    k, pct, n, note = size_for(0.9, 3.0, 1000, 40)
    assert pct == 15 and n == 40 and "Trimmed" in note             # capped, then trimmed to the free cash
    assert size_for(0.6, 2.0, 1000, 5)[2] is None                  # under Binance's minimum order


# ------------------------------------------------------------------------ scoring --


def test_waiting_then_expired_when_the_buy_never_fills():
    c = _call()
    df = _df([(10.5, 10.6, 10.2, 10.4)] * 10)
    assert score_call(c, df, "binance", "5m", T0 + 3000)["status"] == "waiting"
    late = score_call(c, df, "binance", "5m", c.expires_at + 1)
    assert late["status"] == "expired" and late["closed_at"] == c.expires_at


def test_fill_then_take_profit_records_r_and_the_level():
    c = _call()
    rows = [(10.5, 10.5, 10.1, 10.2), (10.2, 10.25, 9.9, 10.0), (10.0, 10.6, 9.95, 10.5), (10.5, 11.1, 10.4, 11.0),
            (11.0, 11.4, 10.9, 11.3)]
    out = score_call(c, _df(rows), "binance", "5m", T0 + 3600)
    assert out["status"] == "tp" and out["fill_price"] == 10.0 and out["exit_price"] == 11.0
    assert out["r"] == pytest.approx((1.0 - 0.001 * 21) / 0.3, abs=1e-3)  # +1/0.3 R less fees on 10 and 11
    assert out["level_hit"] is True and out["reach_r"] >= 3.3


def test_fill_then_invalidation_is_a_miss_even_when_both_are_in_one_candle():
    c = _call()
    rows = [(10.5, 10.5, 9.95, 10.0), (10.0, 11.2, 9.6, 10.0)]  # second candle has the take-profit and the stop
    out = score_call(c, _df(rows), "binance", "5m", T0 + 3600)
    assert out["status"] == "invalidated" and out["r"] < -1
    assert out["level_hit"] is False and out["reach_final"] is True


def test_time_out_closes_at_the_window_and_counts_as_a_miss():
    c = _call(hold_seconds=3600)
    rows = [(10.5, 10.5, 9.95, 10.0)] + [(10.2, 10.4, 10.1, 10.3)] * 20
    out = score_call(c, _df(rows), "binance", "5m", T0 + 3 * 3600)
    assert out["status"] == "timed_out" and out["exit_price"] == 10.3 and out["r"] > 0
    assert out["level_hit"] is False and out["reach_final"] is True
    assert score_call(c, _df(rows[:5]), "binance", "5m", T0 + 25 * 60)["status"] == "open"


# ----------------------------------------------------------------------- learning --


def _closed(n, hits, bucket="1h|demand|fresh|htf|with", symbol="INJUSDT", reach=None, **kw):
    out = []
    for i in range(n):
        hit = i < hits
        out.append(_call(id=f"h{bucket}{symbol}{i}", symbol=symbol, bucket=bucket, status="tp" if hit else "invalidated",
                         level_hit=hit, closed_at=T0, fill_price=10.0, reach_final=True,
                         reach_r=(reach[i] if reach else (3.4 if hit else 0.5)), r=3.0 if hit else -1.05, **kw))
    return out


def test_estimate_starts_at_random_odds_and_follows_the_evidence():
    b = "1h|demand|fresh|htf|with"
    first = estimate("INJUSDT", "1h", "demand", b, 3.0, None, [], T0)
    assert first.p == pytest.approx(random_odds(3.0)) == pytest.approx(0.25) and first.lift == 1.0
    assert "no edge is assumed" in first.basis
    # Calls 3.33R from the fill: random odds 23%. 21 of 30 hit is far better than that; 3 of 30 is worse.
    good = estimate("INJUSDT", "1h", "demand", b, 3.0, None, _closed(30, 21), T0)
    bad = estimate("INJUSDT", "1h", "demand", b, 3.0, None, _closed(30, 3), T0)
    assert bad.p < 0.25 < good.p and good.lift > 1.5 and "21/30" in good.basis
    # A nearer level is always likelier than a farther one with the same edge.
    near = estimate("INJUSDT", "1h", "demand", b, 1.5, None, _closed(30, 21), T0)
    assert near.p > good.p
    # A setup with no calls of its own leans on the timeframe's calls; the coin's backtest moves it further.
    other = estimate("ETHUSDT", "1h", "demand", "1h|demand|tested|nohtf|against", 3.0, None, _closed(30, 21), T0)
    assert 0.25 < other.p < good.p
    track = TrackRecord(symbol="ETHUSDT", interval="1h", status="ok", trades=40, wins=8, avg_r=-0.4,
                        data_source="binance")
    with_bt = estimate("ETHUSDT", "1h", "demand", "1h|demand|tested|nohtf|against", 3.0, track, _closed(30, 21), T0)
    assert with_bt.p < other.p and "8/40" in with_bt.basis
    demo = TrackRecord(symbol="ETHUSDT", interval="1h", status="ok", trades=40, wins=40, avg_r=1.5,
                       data_source="synthetic")
    assert estimate("ETHUSDT", "1h", "demand", "x", 3.0, demo, [], T0).lift == 1.0  # demo backtests don't count
    assert estimate("ETHUSDT", "1h", "demand", "x", 3.0, demo, [], T0, demo_ok=True).lift > 1.0
    # Old calls count less than new ones.
    old = _closed(30, 21)
    for c in old:
        c.closed_at = T0 - 400 * 86400
    assert estimate("INJUSDT", "1h", "demand", b, 3.0, None, old, T0).p < good.p


def test_take_profit_moves_closer_when_price_keeps_stalling_under_the_level():
    # Level 3R away: most calls reach 2.5R (83%) but few reach the full 3R.
    reach = [3.1] * 4 + [2.6] * 12 + [0.4] * 4
    hist = _closed(len(reach), 4, reach=reach, tp_level=10.9)
    for c in hist:
        c.level_hit = c.reach_r >= 2.999
    assert len(hist) >= MIN_TP_SAMPLES
    tp = take_profit(10.0, 9.7, 10.9, 0.3, 3.0, "1h", "demand", hist, T0)  # level 3R away
    assert tp.fraction < 1.0 and 10.0 < tp.price < 10.9 and tp.p > 0.3 and "of the way" in tp.note
    # Too few calls: the full level.
    assert take_profit(10.0, 9.7, 10.9, 0.3, 3.0, "1h", "demand", hist[:5], T0).fraction == 1.0


def test_calibration_and_verdict():
    rows = _closed(30, 18)
    for c in rows:
        c.confidence = 0.6
    cal = calibration(rows)
    assert cal["calls"] == 30 and cal["hit_rate"] == 0.6 and cal["enough"]
    assert cal["bins"] == [{"from": 0.55, "to": 0.65, "calls": 30, "predicted": 0.6, "actual": 0.6}]
    assert "about right" in verdict(cal)
    assert "too few" in verdict(calibration(rows[:3]))


# ------------------------------------------------------------------- the service --


class FakeAlerts:
    channels = ["discord"]
    channel_status = {"telegram": False, "discord": True}

    def __init__(self):
        self.sent, self.records = [], []

    async def send_text(self, text):
        self.sent.append(text)
        return {"discord": True}

    def record(self, kind, symbol, title, text, price=None, **kw):
        self.records.append((kind, symbol, title))

    def broadcast(self, msg):
        pass


class FakeMarket:
    """Demo candles ending at T0 for the analysis; for scoring and the paper wallet, candles the test sets per coin."""

    def __init__(self, end):
        self.end = end
        self.paths: dict[str, list[tuple]] = {}

    async def get_klines(self, symbol, interval, limit):
        return synthetic_klines(symbol, interval, limit, end=self.end), "synthetic"

    async def get_range(self, symbol, interval, start, end=None):
        step = INTERVAL_SECONDS[interval]
        rows = self.paths.get(symbol)
        if not rows:
            return candles_to_df(synthetic_klines(symbol, interval, 50, end=self.end)), "synthetic"
        return _df(rows, step=step, start=start - start % step), "synthetic"


class StubTrack:
    """A backtest showing an edge: 60% of 30 trades reached their level, +0.8R a trade (demo data)."""

    async def for_plan(self, symbol, interval, plan, timeout=None):
        return TrackRecord(symbol=symbol, interval=interval, status="ok", trades=30, wins=18, avg_r=0.8,
                           data_source="synthetic")


def _desk(market, **kw):
    s = Settings(data_source="synthetic", agent_desk=True, agent_paper_store="memory", agent_wallet_cash=1000.0)
    desk = AgentDesk(market, Database("memory"), FakeAlerts(), StubTrack(), PaperService(market, s, "memory", 1000.0),
                     settings=s)
    desk.update_settings(DeskSettings(min_confidence=0.2, symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT", "INJUSDT",
                                                                    "LINKUSDT", "XRPUSDT", "DOGEUSDT", "BNBUSDT"], **kw))
    return desk


def test_a_run_makes_calls_with_paper_orders_and_scores_them():
    end = T0 + 30
    market = FakeMarket(end)
    desk = _desk(market)

    async def go():
        made = []
        for tf in ("1h", "4h", "1d"):
            made += await desk.run_interval(tf, now=end + 60, force=True)
        return made

    made = asyncio.run(go())
    assert made, "demo candles with a backtest edge should give a call across eight coins and three timeframes"
    for c in made:
        assert c.stop < c.zone_low <= c.entry <= c.zone_high and c.entry < c.tp
        assert 0.2 <= c.confidence <= 0.9 and c.expected_r >= 0.1 and c.data_source == "synthetic"
        assert c.status == "waiting" and c.bucket.startswith(c.interval)
    # One call per coin and timeframe at a time: running again makes none on the same coins.
    again = asyncio.run(desk.run_interval(made[0].interval, now=end + 120, force=True))
    assert all(c.symbol != made[0].symbol for c in again)
    placed = [c for c in made if c.notional]
    assert placed and all(c.paper_group == c.id for c in placed)
    groups = {o.group for o in desk.paper.orders()}
    assert {c.id for c in placed} <= groups
    assert len([o for o in desk.paper.orders() if o.group == placed[0].id]) == 3  # buy, take-profit, stop
    alerts = desk.alerts
    assert any(t.startswith("[demo data] Agent call") for t in alerts.sent)
    assert len([t for t in alerts.sent if "Agent call ·" in t]) == len(made)
    # The other zones it looked at are watched: scored and learned from, never traded or announced.
    watched = desk.calls(watched=True)
    assert watched and all(w.shadow and w.notional is None and w.paper_group is None for w in watched)
    assert not {w.id for w in watched} & {o.group for o in desk.paper.orders()}
    run = desk.last_run["4h"]
    assert run["zones"] == run["calls"] + run["watched"] and run["best"]

    # The first call fills and reaches its take-profit.
    c = made[0]
    p = c.entry
    market.paths[c.symbol] = [(p * 1.01, p * 1.01, p * 0.999, p), (p, c.tp * 1.001, p * 0.999, c.tp)]
    changed = asyncio.run(desk.score(now=c.created_at + 7200))
    after = desk.calls(symbol=c.symbol)
    hit = next(x for x in after if x.id == c.id)
    assert hit.status == "tp" and hit.r > 0 and hit in changed
    assert any("take-profit" in t and "reached" in t for t in alerts.sent)
    s = desk.summary(now=c.created_at + 7200)
    assert s["tp"] == 1 and s["closed"] == 1 and sum(r["calls"] for r in s["setups"]) >= 1
    assert any(line.startswith("Record:") for line in desk.brief_lines(now=c.created_at + 7200))
    facts = desk.facts_for(c.symbol)
    assert facts and facts["recent_results"][0]["status"] == "tp"

    # Watched zones that finished over a year ago are dropped; calls are kept.
    w = desk.calls(watched=True)[0]
    desk._calls[w.id] = w.model_copy(update={"status": "expired", "closed_at": end - 400 * 86400})
    assert asyncio.run(desk.prune(now=end)) == 1 and w.id not in {x.id for x in desk.calls(watched=True, limit=5000)}
    assert desk.db.one("SELECT id FROM desk_calls WHERE id = ?", (w.id,)) is None

    # Calls are kept in the database: a new desk on the same database sees them.
    again = AgentDesk(market, desk.db, None, StubTrack(), desk.paper, settings=desk.s)
    assert {x.id for x in again.calls()} == {x.id for x in desk.calls()}
    assert again.settings.min_confidence == 0.2


def test_api():
    os.environ["DATA_SOURCE"] = "synthetic"
    os.environ["LLM_PROVIDER"] = "none"
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        st = client.get("/api/desk").json()
        assert st["settings"]["timeframes"] == ["1h", "4h", "1d"] and st["summary"]["calls"] == 0
        r = client.put("/api/desk/symbols", json={"symbols": ["inj/usdt", "ETHUSDT", "bad symbol!"]}).json()
        assert r["symbols"] == ["INJUSDT", "ETHUSDT"]
        off = client.put("/api/desk/settings", json={**r["settings"], "follow_watchlist": False}).json()
        assert client.put("/api/desk/symbols", json={"symbols": ["BTCUSDT"]}).json()["symbols"] == ["INJUSDT",
                                                                                                    "ETHUSDT"]
        assert off["settings"]["follow_watchlist"] is False
        assert client.post("/api/desk/run", params={"interval": "15m"}).status_code == 422
        ran = client.post("/api/desk/run", params={"interval": "4h"}).json()
        assert "calls" in ran and ran["status"]["last_run"]["4h"]["coins"] >= 0
        w = client.get("/api/desk/wallet").json()
        assert w["start_cash"] == 1000.0
        assert client.post("/api/desk/wallet/reset", json={"start_cash": 500}).json()["start_cash"] == 500.0
        assert "calls" in client.get("/api/desk/calls", params={"status": "active"}).json()
