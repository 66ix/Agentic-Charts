import asyncio

from app.market_data import BinanceRejected, MarketData


def test_invalid_symbol_does_not_mark_binance_down(monkeypatch):
    md = MarketData()

    async def rejected(*a, **k):
        raise BinanceRejected('Binance rejected request: {"code":-1121,"msg":"Invalid symbol."}')

    monkeypatch.setattr(md, "_binance_klines", rejected)
    candles, source = asyncio.run(md.get_klines("ETHWUSDT", "1h", 30))
    assert source == "synthetic" and candles
    assert md.binance_usable()
