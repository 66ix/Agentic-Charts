"""Kimi Cooked v5.7.4: the vendored port still reproduces its own tables, and the service turns its result into
what the chart draws."""

import csv
import gzip
import os
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("DATA_SOURCE", "synthetic")
os.environ.setdefault("LLM_PROVIDER", "none")

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.kimi import Inputs, KimiCooked  # noqa: E402
from app.kimi_service import bar_closed, closed_only, compute, summarize  # noqa: E402
from app.llm import rule_intent  # noqa: E402
from app.main import app  # noqa: E402
from app.schemas import Candle  # noqa: E402

SAMPLE = Path(__file__).parent / "data" / "kimi_BTCUSDT_1h.csv.gz"  # the 6,600 candles shipped with the port


@pytest.fixture(scope="module")
def sample() -> list[Candle]:
    with gzip.open(SAMPLE, "rt") as f:
        return [Candle(time=int(float(r["time"])), open=float(r["open"]), high=float(r["high"]), low=float(r["low"]),
                       close=float(r["close"]), volume=float(r["volume"])) for r in csv.DictReader(f)]


def test_port_reproduces_its_tables(sample):
    """The numbers the port prints on its own sample (`run_example.py --mine`): the lines marked agentic-charts
    in kimi_v574.py must not change any of them."""
    t = np.array([c.time for c in sample], dtype="int64") * 1000
    o, h, l, c, v = (np.array([getattr(x, k) for x in sample]) for k in ("open", "high", "low", "close", "volume"))
    res = KimiCooked(60, Inputs.users_chart()).run(t, o, h, l, c, v)
    s = res.stats_table()
    assert [(s[k]["wins"], s[k]["n"]) for k in ("DIV +/-", "U / Dn", "Early ?", "Random")] == [
        (6, 30), (3, 13), (39, 135), (373, 1188)]
    vt = res.verify_table()
    assert vt["Evaluated"] == 6579
    assert round(vt["Traj Acc %"], 2) == 56.84 and round(vt["Cover % (tgt 75)"], 2) == 73.37
    assert vt["Next candle / right / n"][2] == 469


def test_compute_builds_the_drawing(sample):
    k = compute("BTCUSDT", "1h", sample[-3000:], "binance")
    assert k.bars == 3000 and k.last_closed == sample[-1].time and k.version == "5.7.4"
    times = {c.time for c in sample}

    # S/R: at most maxSR (4) per side; broken levels end on the candle that broke them, others extend right.
    for side in ("support", "resistance"):
        assert len([lv for lv in k.levels if lv.side == side]) <= 4
    for lv in k.levels:
        assert lv.zone_low < lv.price < lv.zone_high and lv.time_start in times
        assert (lv.state == "broken") == (lv.time_end is not None)
        assert lv.odds is None or 0 <= lv.odds <= 100
        if lv.time_end is not None:
            assert lv.time_end > lv.time_start

    # Signals sit on real candles, carry the script's labels and are capped like the chart's labels.
    assert k.signals
    assert {s.text for s in k.signals} <= {"B+", "B-", "U", "Dn", "B+?", "B-?"}
    assert all(s.time in times and s.confirm_time >= s.time for s in k.signals)
    assert len([s for s in k.signals if s.type != "Early"]) <= 15 and len([s for s in k.signals if s.type == "Early"]) <= 15

    f = k.forecast
    assert f is not None and f.start_time == sample[-1].time and f.step == 3600
    assert len(f.path) == len(f.band_high) == len(f.band_low) == len(f.texture) == f.horizon + 1
    assert f.path[0] == sample[-1].close and f.texture[-1] == pytest.approx(f.path[-1])
    assert all(lo <= y <= hi for lo, y, hi in zip(f.band_low, f.path, f.band_high))
    assert f.range_low == f.band_low[-1] and f.range_high == f.band_high[-1]

    if k.fib:
        assert k.fib.swing_low < k.fib.swing_high and k.fib.pocket_low < k.fib.pocket_high
        assert [x.ratio for x in k.fib.levels][:7] == [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]

    labels = [r.label for r in k.stats]
    assert labels[:3] == ["DIV +/-", "U / Dn", "Early ?"] and labels[-1] == "Exp/Trade"
    assert k.verify[0].label == "Evaluated"

    facts = summarize(k)
    assert facts["indicator"] == "Kimi Cooked v5.7.4 on BTCUSDT 1h" and "forecast" in facts
    assert all(lv["side"] in ("support", "resistance") for lv in facts["levels"])


def test_only_closed_candles():
    now = datetime(2026, 10, 6, 7, 30, tzinfo=timezone.utc).timestamp()
    bar = lambda t: Candle(time=int(t), open=1, high=1, low=1, close=1)  # noqa: E731
    hour = int(datetime(2026, 10, 6, 7, tzinfo=timezone.utc).timestamp())
    assert [c.time for c in closed_only([bar(hour - 3600), bar(hour)], "1h", now)] == [hour - 3600]
    assert len(closed_only([bar(hour - 7200), bar(hour - 3600)], "1h", now)) == 2
    sept = datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp()
    octo = datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()
    assert bar_closed(int(sept), "1M", now) and not bar_closed(int(octo), "1M", now)
    dec = datetime(2025, 12, 1, tzinfo=timezone.utc).timestamp()
    assert bar_closed(int(dec), "1M", datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())


def test_rule_parser_toggles_kimi():
    assert rule_intent("show my kimi").indicators_on == ["kimi"]
    assert rule_intent("turn on kimi cooked and RSI").indicators_on == ["kimi", "rsi"]
    assert rule_intent("hide kimi").indicators_off == ["kimi"]
    assert rule_intent("what does kimi say?").indicators_on == []


def test_kimi_endpoint_and_agent_offline():
    with TestClient(app) as client:
        r = client.get("/api/indicators/kimi", params={"symbol": "SOL/USDT", "interval": "4h"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["symbol"] == "SOLUSDT" and body["data_source"] == "synthetic" and body["bars"] > 1000
        assert body["forecast"]["horizon"] >= 5 and body["verify"] and body["stats"]
        # The synthetic feed's newest candle is still forming, so it is left out.
        k = client.get("/api/klines", params={"symbol": "SOLUSDT", "interval": "4h", "limit": 10}).json()["candles"]
        assert body["last_closed"] == k[-2]["time"]
        assert client.get("/api/indicators/kimi", params={"interval": "2d"}).status_code == 422

        r = client.post("/api/agent/analyze", json={"symbol": "SOLUSDT", "interval": "4h",
                                                    "prompt": "show my kimi indicator"})
        assert r.status_code == 200, r.text
        res = r.json()
        assert res["indicators"] == {"kimi": True}
        assert "Kimi Cooked v5.7.4 on SOLUSDT 4h" in res["summary"] and "Turned on Kimi Cooked" in res["summary"]


def test_htf_divergence_factor_w8(sample):
    """f_htfDivState on the 4h bars built from the 1h sample: it fires on its own, and with the factor on, signals
    whose last completed 4h bar had a recent divergence carry bit 8 in their factor mask."""
    from app.kimi.kimi_v574 import _htf_bars, _htf_div_state

    t = np.array([c.time for c in sample], dtype="int64") * 1000
    o, h, l, c, v = (np.array([getattr(x, k) for x in sample]) for k in ("open", "high", "low", "close", "volume"))
    ref, hh, ll, hc = _htf_bars(t, t, h, l, c, 240)
    assert len(hc) == 1651 and (ref[1:] >= ref[:-1]).all() and hh.max() == h.max() and ll.min() == l.min()
    d, age = _htf_div_state(hh, ll, hc, 240.0, Inputs())
    assert (age == 0).sum() > 5 and set(np.unique(d)) <= {-1, 0, 1} and {1, -1} <= set(d[age == 0])
    with_w8 = KimiCooked(60, Inputs(htfChoice="240")).run(t, o, h, l, c, v)
    without = KimiCooked(60, Inputs(htfChoice="240", confUseHtfDiv=False)).run(t, o, h, l, c, v)
    flagged = [s for s in with_w8.signals if s.mask & (1 << 8)]
    assert flagged and not any(s.mask & (1 << 8) for s in without.signals)
    assert [s.bar for s in with_w8.signals] == [s.bar for s in without.signals]  # the factor only moves scores
