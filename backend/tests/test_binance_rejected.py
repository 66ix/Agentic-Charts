import dataclasses
import asyncio

from app.market_data import BinanceRejected, MarketData


def test_invalid_symbol_does_not_mark_binance_down(monkeypatch):
    md = MarketData()
    monkeypatch.setattr(md, "settings", dataclasses.replace(md.settings, data_source="auto"))

    async def rejected(*a, **k):
        raise BinanceRejected('Binance rejected request: {"code":-1121,"msg":"Invalid symbol."}')

    monkeypatch.setattr(md, "_binance_klines", rejected)
    candles, source = asyncio.run(md.get_klines("ETHWUSDT", "1h", 30))
    assert source == "synthetic" and candles
    assert md.binance_usable()


def test_a_rejected_pair_is_not_requested_again(monkeypatch):
    md = MarketData()
    monkeypatch.setattr(md, "settings", dataclasses.replace(md.settings, data_source="auto"))
    calls = []

    async def rejected(*a, **k):
        calls.append(a)
        raise BinanceRejected("Invalid symbol.")

    monkeypatch.setattr(md, "_binance_klines", rejected)
    asyncio.run(md.get_klines("ETHWUSDT", "1h", 30))
    asyncio.run(md.get_klines("ETHWUSDT", "4h", 30))
    assert len(calls) == 1 and md.binance_usable()
