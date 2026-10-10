"""follow-the-thread: a follow-up knows what 'this' is (the plan in focus), and next-step chips come from the
answer's facts."""

import os
import time

os.environ["DATA_SOURCE"] = "synthetic"
os.environ["LLM_PROVIDER"] = "none"

import pandas as pd  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.focus import plan_in_focus, usable  # noqa: E402
from app.llm import rule_intent  # noqa: E402
from app.main import app  # noqa: E402
from app.schemas import Focus, PlanTarget, TradePlan  # noqa: E402
from app.suggest import next_steps  # noqa: E402
from app.ta_agent import describe  # noqa: E402

PLAN_KINDS = {"plan_entry", "plan_stop", "plan_target"}


def _ask(client, prompt, overlays=(), previous=None, focus=None, symbol="INJUSDT"):
    r = client.post("/api/agent/analyze", json={"symbol": symbol, "interval": "4h", "prompt": prompt,
                                                "overlays": list(overlays), "previous_intent": previous,
                                                "focus": focus})
    assert r.status_code == 200, r.text
    return r.json()


def test_what_would_invalidate_this_answers_about_the_plan_in_focus():
    with TestClient(app) as client:
        first = _ask(client, "Give me a long setup")
        plan = first["plan"]
        assert plan and PLAN_KINDS <= {o["kind"] for o in first["overlays"]}
        focus = {"kind": "plan", "symbol": "INJUSDT", "interval": "4h", "at": int(time.time()) - 60, "plan": plan}
        res = _ask(client, "What would invalidate this?", first["overlays"], first["intent"], focus)
        assert PLAN_KINDS <= {o["kind"] for o in res["overlays"]}
        assert res["question_only"] is True
        pf = res["facts"]["plan_in_focus"]
        assert abs(pf["invalidation"]["stop"] - plan["stop"]) < 1e-3 * plan["stop"]  # rounded for display
        assert "stop" in res["summary"] and "wrong" in res["summary"]
        # Another coin's plan, or one from two days ago, isn't "this".
        for stale in ({**focus, "symbol": "SOLUSDT"}, {**focus, "at": int(time.time()) - 2 * 86400}):
            res = _ask(client, "What would invalidate this?", first["overlays"], first["intent"], stale)
            assert "plan_in_focus" not in res["facts"]
        assert first["suggestions"] and len(first["suggestions"]) <= 4


def _plan(direction="long"):
    if direction == "long":
        return TradePlan(direction="long", entry=100.0, stop=95.0, targets=[PlanTarget(price=110.0, label="T1", rr=2)],
                         risk_pct=5.0, basis="H4 demand")
    return TradePlan(direction="short", entry=100.0, stop=105.0, targets=[PlanTarget(price=90.0, label="T1", rr=2)],
                     risk_pct=5.0)


def _df(rows, t0=1000):
    return pd.DataFrame([{"time": t0 + i * 3600, "open": o, "high": h, "low": low, "close": c, "volume": 1.0}
                         for i, (o, h, low, c) in enumerate(rows)])


def test_plan_in_focus_tracks_fill_stop_and_structure():
    df = _df([(103, 104, 102, 103), (102, 102, 99, 100), (100, 111, 99.5, 110)])
    pf = plan_in_focus(_plan(), 999, df, 110.0, 2.0, [97.0, 99.0, 101.0], [], {"D1": {"trend": "up"}})
    assert pf["filled_since"] and pf["t1_hit_since"] and not pf["stop_hit_since"]
    assert pf["invalidation"]["structure_level"] == 99.0  # the highest swing low between stop and entry
    assert pf["htf_verdict"] == "agrees"
    pf = plan_in_focus(_plan(), 999, df, 110.0, 2.0, [90.0], [], {"D1": {"trend": "down"}})
    assert pf["invalidation"]["structure_level"] is None and pf["htf_verdict"] == "disagrees"
    # Candles before the plan was given don't count.
    assert not plan_in_focus(_plan(), 10_000, df, 110.0, 2.0, [], [], None)["filled_since"]


def test_focus_expires_and_belongs_to_its_coin():
    f = Focus(kind="plan", symbol="injusdt", at=int(time.time()) - 100, plan=_plan())
    assert f.symbol == "INJUSDT" and usable(f, "INJUSDT") and not usable(f, "SOLUSDT")
    assert not usable(f.model_copy(update={"at": int(time.time()) - 90_000}), "INJUSDT")


def _facts(**kw):
    base = {"timeframe": "H4", "last_price": 100.0, "trend": "up", "atr": 2.0, "support": [], "resistance": [],
            "indicators": {"rsi": 50.2, "macd": {"hist": 0.12, "hist_rising": True}, "ema_fast": 99.0,
                           "ema_slow": 97.0, "price_vs_ema": "above both"}}
    return {**base, **kw}


def test_describe_reads_out_the_indicator_asked_about():
    assert "RSI is 50.2" in describe(_facts(), "INJUSDT", "short", "is RSI overbought here?")
    assert "MACD histogram is 0.12" in describe(_facts(), "INJUSDT", "short", "what's the macd doing?")
    assert "No resistance above" in describe(_facts(), "INJUSDT", "detailed", "levels")


def test_chips_come_from_the_facts():
    f = _facts(demand=[{"low": 99.0, "high": 100.5, "inside": True, "distance_atr": 0.0}],
               kimi={"recent_signals": [{"label": "B-", "direction": "short", "bars_ago": 1}]},
               higher_timeframes={"D1": {"trend": "down"}}, plan={"direction": "long"})
    steps = next_steps(f, None, "INJUSDT", "4h", spot_only=True)
    assert steps[0].label.startswith("Alert me if 99") and len(steps) <= 4
    assert not any("short" in s.prompt.lower() for s in steps)
    assert any(s.label == "Why does the D1 disagree?" for s in steps)
    # Each chip's prompt does what its label says.
    by_label = {s.label: s.prompt for s in steps}
    assert rule_intent(by_label[steps[0].label], chart_symbol="INJUSDT").alert_prices == [99.0]
    assert rule_intent(by_label["What would invalidate this?"], chart_symbol="INJUSDT").keep_existing
    longs = next_steps(_facts(kimi={"recent_signals": [{"label": "B+", "direction": "long", "bars_ago": 0}]}),
                       None, "INJUSDT", "4h")
    assert longs[0].label == "Plan the Kimi B+ long"
    assert rule_intent(longs[0].prompt, chart_symbol="INJUSDT").trade_plan == "long"
