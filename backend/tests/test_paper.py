"""Spot paper trading: the wallet replay on constructed 1m candles, and the API."""

import os

os.environ["DATA_SOURCE"] = "synthetic"
os.environ["LLM_PROVIDER"] = "none"

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.paper import NewPaperOrder, PaperOrder, build_wallet  # noqa: E402

T0 = 1_700_000_040  # a minute boundary
FEE = 0.1


def candles(rows):
    """1m candles from (open, high, low, close) rows, starting at T0."""
    a = np.asarray(rows, dtype=float)
    return pd.DataFrame({"time": T0 + np.arange(len(a)) * 60, "open": a[:, 0], "high": a[:, 1], "low": a[:, 2],
                         "close": a[:, 3], "volume": np.full(len(a), 1.0)})


def order(i, side, typ="limit", price=None, placed=T0, **kw):
    return PaperOrder(id=f"o{i}", symbol="XUSDT", side=side, type=typ, price=price, placed_at=placed, **kw)


def wallet(orders, df, cash=1000.0, fee=FEE):
    return build_wallet(cash, fee, T0, orders, {"XUSDT": df}, now=T0 + 10_000)


def result(w, oid):
    return next(o["result"] for o in w.orders if o["id"] == oid)


def test_limit_buy_fills_on_touch_and_sets_cash_aside_until_then():
    df = candles([(10, 10.2, 9.9, 10), (10, 10.1, 9.6, 9.7), (9.7, 10.5, 9.7, 10.4)])
    w = wallet([order(1, "buy", price=9.5, quote=500)], df)
    assert result(w, "o1")["status"] == "pending"
    assert w.reserved == 500 and w.cash == pytest.approx(500) and w.equity == pytest.approx(1000)

    w = wallet([order(1, "buy", price=9.7, quote=500)], df)
    r = result(w, "o1")
    assert r["status"] == "filled" and r["fill_price"] == 9.7 and r["filled_at"] == T0 + 60
    h = w.holdings[0]
    assert h.qty * 9.7 * (1 + FEE / 100) == pytest.approx(500)
    assert h.avg_cost == pytest.approx(9.7 * 1.001)
    assert h.unrealized_pnl == pytest.approx(h.qty * 10.4 - 500)
    assert w.equity == pytest.approx(500 + h.qty * 10.4)


def test_a_gap_down_fills_a_buy_at_the_open_and_a_stop_at_the_open():
    df = candles([(10, 10, 10, 10), (9, 9.1, 8.8, 9), (9, 9, 7, 7.2)])
    w = wallet([order(1, "buy", price=9.5, qty=10), order(2, "sell", "stop", price=8, group="g")], df)
    assert result(w, "o1")["fill_price"] == 9  # opened below the limit
    stop = result(w, "o2")
    assert stop["status"] == "filled" and stop["fill_price"] == 8 and stop["fill_qty"] == 10
    assert stop["pnl"] == pytest.approx(10 * 8 * 0.999 - 10 * 9 * 1.001)
    assert w.holdings[0].qty == 0 and w.stats.sells == 1 and w.stats.wins == 0


def test_a_sell_never_uses_coins_bought_in_the_same_candle_and_waits_for_them():
    # The buy and the target are both inside candle 1; the target can only fill on candle 2.
    df = candles([(10, 10, 10, 10), (10, 11.2, 9.4, 10.5), (10.5, 10.9, 10.4, 10.8), (10.8, 11.3, 10.7, 11.1)])
    w = wallet([order(1, "buy", price=9.5, qty=10), order(2, "sell", price=11, group="g")], df)
    assert result(w, "o1")["filled_at"] == T0 + 60
    tp = result(w, "o2")
    assert tp["status"] == "filled" and tp["filled_at"] == T0 + 180 and tp["fill_price"] == 11
    assert w.realized_pnl == pytest.approx(10 * 11 * 0.999 - 10 * 9.5 * 1.001)
    assert w.stats.win_rate == 100.0


def test_a_stop_closing_the_position_cancels_the_groups_targets():
    df = candles([(10, 10, 10, 10), (10, 10.1, 9.5, 9.6), (9.6, 9.7, 8.9, 9.0), (9.0, 12, 9.0, 11.9)])
    orders = [order(1, "buy", price=9.6, qty=5, group="g"), order(2, "sell", "stop", price=9.0, group="g"),
              order(3, "sell", price=11.5, group="g")]
    w = wallet(orders, df)
    assert result(w, "o2")["status"] == "filled"
    t = result(w, "o3")
    assert t["status"] == "cancelled" and t["reason"] == "position closed"


def test_partial_targets_market_orders_and_cancels():
    df = candles([(10, 10, 10, 10), (10, 10.6, 9.9, 10.5), (10.5, 11.2, 10.4, 11.1), (11, 11, 11, 11)])
    orders = [order(1, "buy", "market", qty=4, market_price=10.0, placed=T0 + 5),
              order(2, "sell", price=10.5, qty=2, group="g"), order(3, "sell", price=11, qty=2, group="g"),
              order(4, "buy", price=5, quote=100, cancelled_at=T0 + 70)]
    w = wallet(orders, df)
    assert result(w, "o1")["fill_price"] == 10.0 and result(w, "o1")["filled_at"] == T0 + 5
    assert result(w, "o2")["filled_at"] == T0 + 60 and result(w, "o3")["filled_at"] == T0 + 120
    assert result(w, "o4")["status"] == "cancelled" and w.reserved == 0
    assert w.holdings[0].qty == pytest.approx(0) and w.stats.sells == 2
    fees = 4 * 10 * 0.001 + 2 * 10.5 * 0.001 + 2 * 11 * 0.001
    assert w.cash == pytest.approx(1000 - 40 + 21 + 22 - fees + 0)  # buy cost includes its fee in `fees`
    assert w.stats.fees == pytest.approx(fees)


def test_orders_validate():
    with pytest.raises(ValueError):
        NewPaperOrder(symbol="btc", side="buy", type="limit", quote=10)  # no price
    with pytest.raises(ValueError):
        NewPaperOrder(symbol="btc", side="buy", type="stop", price=1, qty=1)  # stop buys are not a thing here
    with pytest.raises(ValueError):
        NewPaperOrder(symbol="btc", side="buy", type="market")  # no size
    assert NewPaperOrder(symbol="eth", side="sell", type="limit", price=5).symbol == "ETHUSDT"


def test_api_places_cancels_refuses_too_big_and_resets():
    with TestClient(app) as client:
        r = client.post("/api/paper/reset", json={"start_cash": 1000})
        assert r.status_code == 200 and r.json()["wallet"]["equity"] == 1000
        r = client.post("/api/paper/orders", json={"orders": [
            {"symbol": "BTCUSDT", "side": "buy", "type": "limit", "price": 1, "quote": 600},
            {"symbol": "BTCUSDT", "side": "sell", "type": "stop", "price": 0.5}]})
        assert r.status_code == 200, r.text
        made = r.json()["orders"]
        assert made[0]["group"] and made[0]["group"] == made[1]["group"]
        assert r.json()["wallet"]["reserved"] == 600
        too_big = client.post("/api/paper/orders", json={"orders": [
            {"symbol": "ETHUSDT", "side": "buy", "type": "limit", "price": 1, "quote": 500}]})
        assert too_big.status_code == 422 and "Not enough paper cash" in too_big.text
        r = client.delete(f"/api/paper/orders/{made[0]['id']}")
        assert r.status_code == 200 and r.json()["wallet"]["reserved"] == 0
        bad = client.post("/api/paper/orders", json={"orders": [{"symbol": "BTCUSDT", "side": "buy", "type": "stop",
                                                                  "price": 1, "qty": 1}]})
        assert bad.status_code == 422
        r = client.post("/api/paper/orders", json={"orders": [
            {"symbol": "BTCUSDT", "side": "buy", "type": "market", "quote": 100}]})
        assert r.status_code == 200 and r.json()["orders"][0]["market_price"] > 0
        assert client.get("/api/paper").json()["holdings"][0]["symbol"] == "BTCUSDT"
        assert client.post("/api/paper/reset", json={}).json()["wallet"]["orders"] == []


def test_a_plans_stop_only_sells_what_the_plan_bought():
    df = candles([(10, 10, 10, 10), (10, 10.1, 9.4, 9.6), (9.6, 9.7, 8.9, 9.0), (9.0, 9.1, 8.9, 9.0)])
    orders = [order(1, "buy", "market", qty=3, market_price=10.0, placed=T0 + 1),  # held outside the plan
              order(2, "buy", price=9.5, qty=2, group="p", placed=T0 + 2),
              order(3, "sell", "stop", price=9.0, group="p", placed=T0 + 2),
              order(4, "sell", price=11, group="p", placed=T0 + 2)]
    w = wallet(orders, df)
    stop = result(w, "o3")
    assert stop["status"] == "filled" and stop["fill_qty"] == pytest.approx(2)
    assert result(w, "o4")["status"] == "cancelled" and w.holdings[0].qty == pytest.approx(3)
