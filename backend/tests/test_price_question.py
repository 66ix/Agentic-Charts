"""'What's at 7.15?': the price is picked out of the question, checked against the zones and the candles, and the
answer (with or without an LLM) leads with it in plain sentences."""

import os

from app.agent import asked_price
from app.market_data import candles_to_df, synthetic_klines
from app.schemas import AnalysisIntent, CustomLevel
from app.ta_agent import describe, price_check


def _df(tf="1h", n=400):
    return candles_to_df(synthetic_klines("INJUSDT", tf, n, end=1_760_000_000))


def test_asked_price_reads_a_price_but_not_timeframes_counts_or_drawn_levels():
    plain = AnalysisIntent()
    assert asked_price("What's at 7.1561? Is it support or resistance?", plain, 7.0) == 7.1561
    assert asked_price("is $7,150 support?", plain, 7000.0) == 7150.0
    assert asked_price("how does 7.2k look", plain, 7000.0) == 7200.0
    assert asked_price("show the 4h and 15m levels", plain, 7.0) is None      # timeframes
    assert asked_price("top 10 coins", plain, 7.0) is None                    # a count, not a price
    assert asked_price("what's at 70?", plain, 7.0) is None                   # too far from price
    assert asked_price("what happens at 2025 lows", plain, 7.0) is None
    drawn = AnalysisIntent(custom_levels=[CustomLevel(kind="line", price=7.15, label="")])
    assert asked_price("line at 7.15", drawn, 7.0) is None                    # drawing it, not asking about it
    assert asked_price("alert me at 7.15", AnalysisIntent(alert_prices=[7.15]), 7.0) is None
    assert asked_price("long setup with a stop at 6.9", AnalysisIntent(trade_plan="long"), 7.0) is None


def test_price_check_finds_the_zone_reactions_and_what_is_beyond():
    df = _df()
    last = float(df["close"].iloc[-1])
    above = price_check(df, last * 1.02, "1h")
    assert above["role_now"] == "resistance" and above["timeframe"] == "H1" and above["distance_pct"] > 0
    v = above["visits"]
    assert v["bounced"] + v["crossed"] >= 0 and v["bars_checked"] == len(df)
    assert above["if_breaks"]["direction"] == "up"
    below = price_check(df, last * 0.98, "1h", {"4h": _df("4h", 300)})
    assert below["role_now"] == "support" and below["if_breaks"]["direction"] == "down"
    assert isinstance(below["higher_timeframes"], list)
    nz = below["if_breaks"]
    if nz["next_kind"]:  # the next zone down sits below the asked price
        assert nz["next_high"] < last * 0.98


FACTS = {
    "timeframe": "H4", "last_price": 7.001, "trend": "down", "atr": 0.2585, "atr_pct": 3.69,
    "resistance": [{"low": 7.1112, "high": 7.1888, "touches": 2, "distance_atr": 0.43, "inside": False,
                    "held": 2, "tests_resolved": 2}],
    "support": [{"low": 6.6752, "high": 6.7528, "touches": 1, "distance_atr": 0.96, "inside": False,
                 "htf_confluence": ["D1"]}],
    "supply": [{"low": 7.351, "high": 7.56, "tests": 0, "distance_atr": 1.35, "inside": False,
                "htf_confluence": ["D1", "W1"]}],
    "demand": [],
    "windows": [{"tf": "H4", "high": 7.199, "low": 6.983}],
    "last_structure_break": {"type": "CHoCH", "direction": "bearish", "level": 7.131, "bars_ago": 6},
    "momentum": {"rsi": 38.4},
    "derivatives": {"funding_rate_pct": 0.0058, "oi_change_24h_pct": -9.35},
}


def test_template_answer_is_prose_and_leads_with_what_was_asked():
    text = describe(FACTS, "INJUSDT", "detailed", "Identify the current H4 supply zone and key resistance high")
    assert text.startswith("INJ is trending down on the H4 at 7.0010")
    # Supply first (asked), then the H4 window high (asked as "resistance high"), then the rest.
    assert text.index("supply zone is 7.3510–7.5600") < text.index("H4 high to beat") < text.index("support is")
    assert "it is fresh: price hasn't been back to it, and it lines up with a D1/W1 zone" in text
    assert "it has held both tests" in text
    assert "Structure flipped bearish 6 candles ago" in text and "sellers have the upper hand" in text
    assert "open interest is down 9.35% in 24h (positions are being closed)" in text
    assert "ATR away" not in text and "touches" not in text  # no fact-dump fragments


def test_short_answers_keep_only_what_was_asked():
    text = describe(FACTS, "INJUSDT", detail="short", prompt="where is supply?")
    assert "supply zone is 7.3510" in text and "support" not in text and "RSI" not in text


def test_price_question_answer_comes_first():
    q = {"price": 7.1561, "timeframe": "M5", "role_now": "resistance", "distance_pct": 2.22, "distance_atr": 5.9,
         "zone": {"kind": "resistance", "low": 7.141, "high": 7.149, "touches": 3, "held": 3, "tests_resolved": 5},
         "higher_timeframes": ["M15", "M30"], "visits": {"bounced": 4, "crossed": 1, "last_bars_ago": 12,
                                                          "bars_checked": 500},
         "if_breaks": {"direction": "up", "next_kind": "supply", "next_low": 7.351, "next_high": 7.56}}
    text = describe({**FACTS, "timeframe": "M5", "price_in_question": q}, "INJUSDT", prompt="What's at 7.1561?")
    first = text.split(". ", 2)[1]
    assert first.startswith("7.1561 is resistance on the M5, 2.22% above price")
    assert "an M15/M30 zone covers it too" in text
    assert "bounced 4 times and crossed once, most recently 12 candles ago, so it has mostly turned price away" in text
    assert "If it breaks, a close above it opens the way to the next supply zone at 7.3510–7.5600." in text


def test_agent_answers_a_price_question_without_an_llm():
    os.environ["DATA_SOURCE"] = "synthetic"
    os.environ["LLM_PROVIDER"] = "none"
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        last = client.get("/api/klines", params={"symbol": "INJUSDT", "interval": "1h", "limit": 50}).json()[
            "candles"][-1]["close"]
        price = round(last * 1.015, 4)
        body = client.post("/api/agent/analyze", json={
            "symbol": "INJUSDT", "interval": "1h",
            "prompt": f"What's at {price}? Is it support or resistance, and what happens if it breaks?"}).json()
    q = body["facts"]["price_in_question"]
    assert q["price"] == price and q["role_now"] == "resistance"
    assert "If it breaks" in body["summary"] and body["engine"]["summary"] == "template"
