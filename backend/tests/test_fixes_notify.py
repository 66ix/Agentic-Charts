"""Notify fixes: timed scans send spot setups from live data once, a newly added trade catches up quietly, jobs stay
'error' after a failing pass, and Discord never pings @everyone."""

import asyncio
import dataclasses

from app.alerts import build_channels
from app.config import Settings
from app.market_scanner import MarketScanner
from app.market_scanner import MarketScanResult


def _scanner(**kw):
    s = dataclasses.replace(Settings(), data_source="auto", market_scan_notify_top=3, **kw)
    return MarketScanner.__new__(MarketScanner), s


def _result(source, rows):
    return MarketScanResult.model_construct(interval="4h", scanned=10, data_source=source, longs=rows, shorts=[],
                                            spot_buys=rows, grid_coins=[], notes=[])


class Row:
    def __init__(self, sym, entry, source="binance"):
        self.symbol, self.entry, self.direction, self.data_source = sym, entry, "long", source
        self.stop, self.target, self.rr, self.distance_pct = entry * 0.9, entry * 1.2, 2.0, 1.0
        self.score = self.spot_score = 1.0
        self.track_record = None

        class Agree:
            aligned, total = 2, 3
        self.agreement = Agree()


def _make(**kw):
    sc, s = _scanner(**kw)
    sc.s, sc._sent = s, {}
    return sc


def test_a_timed_scan_sends_live_setups_once():
    sc = _make()
    res = _result("binance", [Row("AAAUSDT", 10.0), Row("BBBUSDT", 5.0)])
    text = sc.to_send(res, 3, now=1000)
    assert text and "AAAUSDT" in text and "BBBUSDT" in text
    assert sc.to_send(res, 3, now=2000) is None  # same setups: not sent again
    res2 = _result("binance", [Row("AAAUSDT", 10.0), Row("CCCUSDT", 2.0)])
    text = sc.to_send(res2, 3, now=3000)
    assert "CCCUSDT" in text and "AAAUSDT" not in text
    assert sc.to_send(res, 3, now=1000 + 13 * 3600)  # after the resend window


def test_demo_and_mixed_scans_are_never_sent():
    sc = _make()
    assert sc.to_send(_result("synthetic", [Row("AAAUSDT", 10.0, "synthetic")]), 3) is None
    assert sc.to_send(_result("mixed", [Row("AAAUSDT", 10.0)]), 3) is None


def test_discord_never_pings_everyone():
    s = dataclasses.replace(Settings(), discord_webhook_url="https://discord.com/api/webhooks/1/abcdefghij",
                            telegram_bot_token="", telegram_chat_id="")
    [dc] = build_channels(s)
    assert dc.body("@everyone look")["allowed_mentions"] == {"parse": []}


def test_a_trade_added_late_catches_up_with_one_message():
    from app.market_data import candles_to_df  # noqa: F401
    from app.schemas import Candle
    from app.trade_manager import TradeManager
    from tests.test_trade_manager import BASE, frame, trade

    up = [101, 102, 104, 106.5, 107, 106, 104.5, 103.5, 104.2, 105.5, 107, 108.5, 109, 108, 107.5, 108.2, 108.3]
    df = frame(BASE + up)
    t = trade(df, 40)

    class Market:
        async def get_klines(self, symbol, interval, limit):
            return [Candle(**r) for r in df.to_dict("records")], "binance"

    sent = []

    class Alerts:
        def record(self, *a, **kw):
            pass

        def broadcast(self, msg):
            pass

        def notify(self, text):
            sent.append(text)

    tm = TradeManager.__new__(TradeManager)
    tm.market, tm.alerts, tm.s = Market(), Alerts(), Settings(data_source="auto")
    advice = asyncio.run(tm.check(t, catch_up=True))
    assert len(advice) >= 2 and all(a.status == "done" for a in advice)
    assert len(sent) == 1 and "caught up" in sent[0] and "best stop now" in sent[0]
