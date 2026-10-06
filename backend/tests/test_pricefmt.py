"""Prices in answers are rounded like the exchange shows them, and the LLM sees rounded facts."""

from app.pricefmt import _fmt, price_decimals, round_facts


def test_price_decimals_follow_binance_steps():
    assert price_decimals(65123.4567) == 2  # BTC: 0.01
    assert price_decimals(3200.5) == 2
    assert price_decimals(150.12345) == 2
    assert price_decimals(7.82713) == 4  # INJ
    assert price_decimals(0.15234567) == 5  # DOGE
    assert price_decimals(0.0000123456) == 8  # capped
    assert price_decimals(0) == 2


def test_fmt_rounds_and_uses_the_price_for_distances():
    assert _fmt(65123.4567) == "65,123.46"
    assert _fmt(7.827134) == "7.8271"
    assert _fmt(0.090334, 7.8271) == "0.0903"  # ATR on INJ: the price's step, not the ATR's
    assert _fmt(25.4, strip=True) == "25.4"
    assert _fmt(65000.0, strip=True) == "65,000"


def test_round_facts_rounds_prices_per_coin():
    facts = {
        "last_price": 7.827134, "atr": 0.0903341, "rsi": 71.23456, "distance_atr": 2.781234, "bars": 18,
        "support": [{"low": 7.534812, "high": 7.576234, "touches": 2}],
        "scan": [{"symbol": "BTCUSDT", "price": 65123.4567, "low": 64000.123456}],
        "kimi": {"forecast": {"range": [7.61234, 8.01234]}},
    }
    out = round_facts(facts)
    assert out["atr"] == 0.0903 and out["last_price"] == 7.8271
    assert out["rsi"] == 71.23 and out["distance_atr"] == 2.781 and out["bars"] == 18
    assert out["support"][0] == {"low": 7.5348, "high": 7.5762, "touches": 2}
    assert out["scan"][0]["low"] == 64000.12  # rounded with BTC's own price
    assert out["kimi"]["forecast"]["range"] == [7.6123, 8.0123]
