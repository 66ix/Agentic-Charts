from app.binance_account import parse_earn
from app.binance_import import AccountFill
from app.pnl_calendar import daily_pnl

DAY = 86_400
T0 = 1_759_708_800  # 2025-10-06 00:00 UTC


def _fill(n, side, price, qty, t, market="spot", symbol="INJUSDT", **kw):
    return AccountFill(key=f"{market}:{symbol}:{n}", market=market, symbol=symbol, trade_id=n, order_id=n, time=t,
                       time_ms=t * 1000, side=side, price=price, qty=qty, quote_qty=price * qty, kind="manual", **kw)


def test_spot_sells_realize_against_average_cost_on_their_day():
    fills = [
        _fill(1, "buy", 10.0, 2.0, T0 + 3600),
        _fill(2, "buy", 14.0, 2.0, T0 + 7200),                                   # average cost 12
        _fill(3, "sell", 15.0, 1.0, T0 + DAY + 60, commission=0.15, commission_asset="USDT"),  # +3 - 0.15
        _fill(4, "sell", 11.0, 3.0, T0 + 2 * DAY + 60),                          # -3
    ]
    cal = daily_pnl(fills)
    assert [d["date"] for d in cal["days"]] == ["2025-10-07", "2025-10-08"]
    assert cal["days"][0]["pnl"] == 2.85 and cal["days"][1]["pnl"] == -3.0
    assert cal["total"] == -0.15 and cal["win_days"] == 1 and cal["loss_days"] == 1
    assert cal["best"]["date"] == "2025-10-07" and cal["worst"]["date"] == "2025-10-08"


def test_futures_use_binance_realized_pnl_less_fees_and_days_follow_the_browser_time_zone():
    fills = [_fill(1, "sell", 2000.0, 1.0, T0 + 23 * 3600, market="futures", symbol="ETHUSDT", realized_pnl=0.0,
                   commission=0.8, commission_asset="USDT"),
             _fill(2, "buy", 1900.0, 1.0, T0 + 23 * 3600 + 1800, market="futures", symbol="ETHUSDT",
                   realized_pnl=100.0, commission=0.76, commission_asset="USDT")]
    utc = daily_pnl(fills)
    assert utc["days"] == [{"date": "2025-10-06", "pnl": 98.44, "spot": 0.0, "futures": 98.44, "closes": 1}]
    # UTC+2 (getTimezoneOffset() = -120): 23:00 UTC is 01:00 the next day.
    assert daily_pnl(fills, tz_offset=-120)["days"][0]["date"] == "2025-10-07"


def test_sells_without_an_imported_buy_and_other_quotes_are_noted_not_counted():
    fills = [_fill(1, "sell", 10.0, 1.0, T0), _fill(2, "buy", 0.0001, 5.0, T0, symbol="INJBTC")]
    cal = daily_pnl(fills)
    assert cal["days"] == [] and cal["total"] == 0 and cal["best"] is None
    assert any("no imported buy" in n for n in cal["notes"]) and any("INJBTC" in n for n in cal["notes"])


def test_earn_rows_parse_flexible_and_locked():
    flex = parse_earn("flexible", {"asset": "usdt", "totalAmount": "12.5", "latestAnnualPercentageRate": "0.0431"})
    assert flex == {"product": "flexible", "asset": "USDT", "qty": 12.5, "apr_pct": 4.31, "rewards_total": None,
                    "can_redeem": True}
    locked = parse_earn("locked", {"asset": "BNB", "amount": "1", "APY": "0.2", "duration": "60",
                                   "redeemDate": "1760000000000", "positionId": 5})
    assert locked["duration_days"] == 60 and locked["redeem_at"] == 1_760_000_000 and locked["apr_pct"] == 20
    assert parse_earn("flexible", {"asset": "BTC", "totalAmount": "0"}) is None


def test_pnl_calendar_endpoint_reads_the_imported_fills():
    import os

    os.environ["DATA_SOURCE"] = "synthetic"
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        svc = app.state.binance
        svc._fills = {f.key: f for f in [_fill(1, "buy", 10.0, 1.0, T0), _fill(2, "sell", 12.0, 1.0, T0 + 60)]}
        try:
            body = client.get("/api/binance/pnl-calendar", params={"tz_offset": 0}).json()
        finally:
            svc._fills = {}
    assert body["days"] == [{"date": "2025-10-06", "pnl": 2.0, "spot": 2.0, "futures": 0.0, "closes": 1}]
    assert body["fills"] == 2 and body["currency"] == "USD"
