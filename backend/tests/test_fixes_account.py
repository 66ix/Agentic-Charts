"""Account fixes: coins in Simple Earn stay held, the average entry uses USD-quoted fills with their fees, BNB fees
are priced, and a paper 'Sell all' or a cancelled plan buy closes the plan's stop and targets."""

import asyncio

import numpy as np
import pandas as pd

from app.binance_import import AccountFill, BinanceImportService, _fee, average_entry, build_round_trips
from app.holdings_watch import HoldingsWatch
from app.paper import PaperOrder, build_wallet

T0 = 1_700_000_040


def _fill(i, side, price, qty, symbol="INJUSDT", fee=0.0, fee_asset="", t=T0):
    return AccountFill(key=f"spot:{symbol}:{i}", market="spot", symbol=symbol, trade_id=i, order_id=i, time=t + i,
                       time_ms=(t + i) * 1000, side=side, price=price, qty=qty, quote_qty=price * qty,
                       commission=fee, commission_asset=fee_asset, kind="manual")


def _positions(spot_value, earn_qty, earn_value):
    return {"manual": {"spot": [{"key": "holding:spot:INJ", "asset": "INJ", "symbol": "INJUSDT", "own_qty": 0.1,
                                 "value": spot_value}],
                       "earn": [{"asset": "INJ", "symbol": "INJUSDT", "product": "flexible", "qty": earn_qty,
                                 "value": earn_value},
                                {"asset": "USDT", "symbol": "USDTUSDT", "product": "flexible", "qty": 500,
                                 "value": 500}],
                       "futures": []},
            "bots": {}}


def test_a_coin_moved_to_earn_stays_watched():
    coins, _ = HoldingsWatch.coins(_positions(1.0, 50, 400.0), 10.0, False)
    assert [(c["symbol"], c["value"], c["source"]) for c in coins] == [("INJUSDT", 401.0, "spot + earn")]


def test_a_coin_moved_to_earn_keeps_its_open_key():
    class Acc:
        def status(self):
            return {"configured": True}

    svc = BinanceImportService.__new__(BinanceImportService)
    svc.account = Acc()

    async def positions(refresh=False):
        return _positions(1.0, 50, 400.0)

    svc.positions = positions
    assert asyncio.run(svc.open_position_keys()) == {"holding:spot:INJ"}


def test_average_entry_leaves_out_other_quotes_and_counts_fees():
    fills = [_fill(1, "buy", 20.0, 10, fee=0.2, fee_asset="USDT"),
             _fill(2, "buy", 0.0003, 10, symbol="INJBTC")]
    qty, avg = average_entry(fills)
    assert qty == 10 and avg == (200 + 0.2) / 10


def test_a_priced_bnb_fee_counts_in_the_pnl():
    buy = _fill(1, "buy", 20.0, 10, fee=0.01, fee_asset="BNB")
    sell = _fill(2, "sell", 22.0, 10, fee=0.01, fee_asset="BNB")
    [trip] = build_round_trips([buy, sell])
    assert trip.realized_pnl == 20 and any("BNB" in n for n in trip.notes)
    priced = [f.model_copy(update={"commission_quote": f.commission * 600}) for f in (buy, sell)]
    assert _fee(priced[0]) == (6.0, 0.0, False)
    [trip] = build_round_trips(priced)
    assert trip.realized_pnl == 8 and not trip.notes


def test_import_prices_bnb_fees_at_the_fill_hour():
    class Market:
        async def get_range(self, symbol, interval, start, end):
            assert symbol == "BNBUSDT" and interval == "1h"
            return pd.DataFrame({"time": [start], "open": [600.0], "high": [600.0], "low": [600.0],
                                 "close": [600.0], "volume": [1.0]}), "binance"

    svc = BinanceImportService.__new__(BinanceImportService)
    svc.market = Market()
    f = _fill(1, "buy", 20.0, 10, fee=0.01, fee_asset="BNB")
    svc._fills = {f.key: f}
    notes: list[str] = []
    asyncio.run(svc._price_fees(notes))
    assert svc._fills[f.key].commission_quote == 6.0 and not notes


# ------------------------------------------------------------------- paper --


def _candles(rows):
    a = np.asarray(rows, dtype=float)
    return pd.DataFrame({"time": T0 + np.arange(len(a)) * 60, "open": a[:, 0], "high": a[:, 1], "low": a[:, 2],
                         "close": a[:, 3], "volume": np.full(len(a), 1.0)})


def _order(i, side, typ="limit", price=None, placed=T0, **kw):
    return PaperOrder(id=f"o{i}", symbol="XUSDT", side=side, type=typ, price=price, placed_at=placed, **kw)


def _result(w, oid):
    return next(o["result"] for o in w.orders if o["id"] == oid)


def test_sell_all_closes_the_plan_so_its_stop_never_sells_a_later_buy():
    df = _candles([(10, 10, 10, 10), (10, 10, 10, 10), (10, 10, 10, 10), (8, 8, 7, 7.5)])
    orders = [_order(1, "buy", "market", qty=5, group="plan", market_price=10.0),
              _order(2, "sell", "stop", price=8, group="plan"),
              _order(3, "sell", "market", placed=T0 + 60, market_price=10.0),  # Sell all
              _order(4, "buy", "market", qty=3, placed=T0 + 120, market_price=10.0)]  # a new manual buy
    w = build_wallet(1000.0, 0.1, T0, orders, {"XUSDT": df}, now=T0 + 10_000)
    assert _result(w, "o3")["fill_qty"] == 5
    assert _result(w, "o2")["status"] == "cancelled" and _result(w, "o2")["reason"] == "position closed"
    assert w.holdings[0].qty == 3


def test_a_plan_whose_buy_was_cancelled_drops_its_stop():
    df = _candles([(10, 10, 10, 10), (10, 10, 10, 10), (8, 8, 7, 7.5)])
    orders = [_order(1, "buy", price=5, qty=5, group="plan", cancelled_at=T0 + 30),
              _order(2, "sell", "stop", price=8, group="plan"),
              _order(3, "buy", "market", qty=3, placed=T0 + 60, market_price=10.0)]
    w = build_wallet(1000.0, 0.1, T0, orders, {"XUSDT": df}, now=T0 + 10_000)
    assert _result(w, "o2")["status"] == "cancelled" and w.holdings[0].qty == 3
