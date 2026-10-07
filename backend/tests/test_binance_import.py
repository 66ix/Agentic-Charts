"""Read-only Binance key, signed requests, fill import, bot vs manual classification, journal sync and positions,
with Binance mocked at the HTTP layer (including the signature check)."""

import asyncio
import hashlib
import hmac
import json
import os
import stat
import time
from urllib.parse import parse_qsl

import httpx
import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.binance_account import (BinanceAccount, BinanceApiError, BinanceKeyError, KeyStore, check_restrictions,
                                 sign_query)
from app.binance_import import (AccountFill, BinanceImportService, ClassifyRequest, ImportSettings, Override,
                                build_round_trips, classify, journal_entry, match_bot, parse_futures_fill,
                                parse_spot_fill, BotRef)
from app.config import Settings
from app.gridbot import GridBotCreate, GridBotParams, GridBotService
from app.journal import JournalService

KEY = "vmPUZE6mv9SD5VNHk4HlWFsOr6aKE2zvsw0MuIgwCIPy6utIco14y7Ju91duEh8A"
SECRET = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
READ_ONLY = {"ipRestrict": False, "createTime": 1698645219000, "enableReading": True, "enableWithdrawals": False,
             "enableInternalTransfer": False, "permitsUniversalTransfer": False, "enableVanillaOptions": False,
             "enableFutures": False, "enableMargin": False, "enableSpotAndMarginTrading": False,
             "enablePortfolioMarginTrading": False, "enableFixApiTrade": False, "enableFixReadOnly": False}
NOW_MS = int(time.time() * 1000)


# --------------------------------------------------------------- signing --


def test_signature_matches_binance_docs_example():
    # The HMAC SHA256 example from Binance's "SIGNED endpoint security" docs.
    params = {"symbol": "LTCBTC", "side": "BUY", "type": "LIMIT", "timeInForce": "GTC", "quantity": 1, "price": 0.1,
              "recvWindow": 5000, "timestamp": 1499827319559}
    q = sign_query(params, SECRET)
    assert q == ("symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1&recvWindow=5000"
                 "&timestamp=1499827319559&signature=c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71")
    assert "fromId" not in sign_query({"symbol": "X", "fromId": None}, SECRET)  # None values are left out


def test_check_restrictions():
    assert check_restrictions(READ_ONLY) == []
    bad = check_restrictions({**READ_ONLY, "enableSpotAndMarginTrading": True, "enableWithdrawals": True})
    assert bad == ["allows withdrawals", "allows spot and margin trading"]
    assert check_restrictions({**READ_ONLY, "enableReading": False}) == ["does not have reading enabled"]
    assert check_restrictions({"enableReading": True}) == ["has no permission report from Binance"]
    assert check_restrictions([]) == ["has no permission report from Binance"]


def test_key_store_file_is_private_and_env_wins(tmp_path):
    path = tmp_path / "sub" / "key.json"
    store = KeyStore(Settings(binance_api_key="", binance_api_secret=""), path=str(path))
    assert store.load() is None
    store.save(KEY, SECRET)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    loaded = store.load()
    assert loaded.key == KEY and loaded.secret == SECRET and loaded.source == "file"
    assert store.delete() and store.load() is None and not store.delete()
    env = KeyStore(Settings(binance_api_key="envkey1234567890", binance_api_secret="envsecret1234567"),
                   path=str(path))
    assert env.load().source == "env"
    with pytest.raises(BinanceKeyError, match="BINANCE_API_KEY"):
        env.save(KEY, SECRET)


# ------------------------------------------------------------ mock Binance --


def _spot_trade(tid, oid, side, price, qty, t_ms, commission=0.0, asset="USDT"):
    return {"symbol": "", "id": tid, "orderId": oid, "price": str(price), "qty": str(qty),
            "quoteQty": str(price * qty), "commission": str(commission), "commissionAsset": asset, "time": t_ms,
            "isBuyer": side == "buy", "isMaker": True, "isBestMatch": True}


class MockBinance:
    """Spot + USD-M account endpoints over one in-memory account. Every signed request is checked."""

    def __init__(self, restrictions=None):
        self.restrictions = restrictions or dict(READ_ONLY)
        self.calls: list[tuple[str, dict]] = []
        self.clock_skew_once = False
        t0 = NOW_MS - 3 * 86_400_000
        h = 3_600_000
        self.spot = {
            "BTCUSDT": [  # by hand: buy 1 @ 100 (fee in BTC), sell 0.999 @ 110
                _spot_trade(1, 11, "buy", 100.0, 1.0, t0, 0.001, "BTC"),
                _spot_trade(2, 12, "sell", 110.0, 0.999, t0 + h, 0.10989, "USDT"),
                # then one through the API by some program: unknown
                _spot_trade(3, 13, "buy", 105.0, 0.5, t0 + 2 * h, 0.0525, "USDT"),
            ],
            "INJUSDT": [],  # grid bot fills are added by the test once the bot exists
        }
        self.orders = {11: "web_abc123", 12: "ios_xyz789", 13: "8kFJ2nDks9Qa1LmP0zXvY3"}
        self.balances = [{"asset": "BTC", "free": "0.5", "locked": "0"}, {"asset": "USDT", "free": "250", "locked": "0"},
                         {"asset": "INJ", "free": "3", "locked": "0"}]
        self.futures = {"ETHUSDT": [
            {"symbol": "ETHUSDT", "id": 501, "orderId": 901, "side": "SELL", "price": "2000", "qty": "2",
             "realizedPnl": "0", "quoteQty": "4000", "commission": "1.6", "commissionAsset": "USDT",
             "time": t0 + 4 * h, "positionSide": "BOTH", "buyer": False, "maker": False},
            {"symbol": "ETHUSDT", "id": 502, "orderId": 902, "side": "BUY", "price": "1900", "qty": "2",
             "realizedPnl": "200", "quoteQty": "3800", "commission": "1.52", "commissionAsset": "USDT",
             "time": t0 + 5 * h, "positionSide": "BOTH", "buyer": True, "maker": False},
        ]}
        self.forders = {901: "web_f1", 902: "web_f2"}
        self.positions = [{"symbol": "SOLUSDT", "positionAmt": "-10", "entryPrice": "150", "markPrice": "140",
                           "unRealizedProfit": "100", "leverage": "5", "positionSide": "BOTH",
                           "liquidationPrice": "190"},
                          {"symbol": "BNBUSDT", "positionAmt": "0", "entryPrice": "0", "markPrice": "600",
                           "unRealizedProfit": "0", "leverage": "5", "positionSide": "BOTH"}]

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": NOW_MS})
        raw = req.url.query.decode()
        query, _, sig = raw.rpartition("&signature=")
        params = dict(parse_qsl(query))
        self.calls.append((path, params))
        # The signature is HMAC-SHA256 of the exact query string, keyed with the secret; the key is in the header.
        assert req.headers["X-MBX-APIKEY"] == KEY
        assert sig == hmac.new(SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
        assert int(params["recvWindow"]) == 10_000 and abs(int(params["timestamp"]) - time.time() * 1000) < 60_000
        if self.clock_skew_once:
            self.clock_skew_once = False
            return httpx.Response(400, json={"code": -1021, "msg": "Timestamp for this request is outside of the "
                                                                   "recvWindow."})
        if path == "/sapi/v1/account/apiRestrictions":
            return httpx.Response(200, json=self.restrictions)
        if path == "/api/v3/account":
            return httpx.Response(200, json={"balances": self.balances})
        if path == "/sapi/v1/asset/wallet/balance":
            return httpx.Response(200, json=[{"activate": True, "balance": "300", "walletName": "Spot"},
                                             {"activate": True, "balance": "512.5", "walletName": "Trading Bots"}])
        if path == "/api/v3/myTrades":
            sym = params["symbol"]
            if sym not in self.spot:
                return httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."})
            rows = [r for r in self.spot[sym] if r["id"] >= int(params["fromId"])]
            return httpx.Response(200, json=rows[: int(params["limit"])])
        if path == "/api/v3/allOrders":
            oid = int(params["orderId"])
            return httpx.Response(200, json=[{"orderId": o, "clientOrderId": c} for o, c in sorted(self.orders.items())
                                             if o >= oid])
        if path == "/fapi/v1/income":
            return httpx.Response(200, json=[{"symbol": "ETHUSDT", "incomeType": "REALIZED_PNL", "income": "200"}])
        if path == "/fapi/v2/positionRisk":
            return httpx.Response(200, json=self.positions)
        if path == "/fapi/v1/userTrades":
            rows = self.futures.get(params["symbol"], [])
            if "fromId" in params:
                rows = [r for r in rows if r["id"] >= int(params["fromId"])]
            else:
                assert int(params["endTime"]) - int(params["startTime"]) < 7 * 86_400_000
                rows = [r for r in rows if int(params["startTime"]) <= r["time"] <= int(params["endTime"])]
            return httpx.Response(200, json=rows)
        if path == "/fapi/v1/allOrders":
            oid = int(params["orderId"])
            return httpx.Response(200, json=[{"orderId": o, "clientOrderId": c} for o, c in sorted(self.forders.items())
                                             if o >= oid])
        return httpx.Response(404, json={"code": -1, "msg": f"unexpected {path}"})


def _account(mock: MockBinance, store_path="memory") -> BinanceAccount:
    s = Settings(binance_api_key="", binance_api_secret="")
    return BinanceAccount(s, KeyStore(s, path=store_path), httpx.AsyncClient(transport=httpx.MockTransport(mock.handler)))


# ------------------------------------------------------------- the key --


def test_trading_key_is_refused_and_not_saved():
    mock = MockBinance({**READ_ONLY, "enableSpotAndMarginTrading": True})
    acc = _account(mock)

    async def run():
        with pytest.raises(BinanceKeyError, match="allows spot and margin trading.*read-only"):
            await acc.set_key(KEY, SECRET)
        assert acc.status()["configured"] is False
        with pytest.raises(BinanceKeyError, match="letters and digits"):
            await acc.set_key("short", SECRET)

    asyncio.run(run())


def test_read_only_key_saved_and_rechecked():
    mock = MockBinance()
    acc = _account(mock)

    async def run():
        st = await acc.set_key(f" {KEY} ", SECRET)
        assert st["configured"] and st["ok"] and st["key_last4"] == KEY[-4:] and st["masked"] == "••••" + KEY[-4:]
        assert SECRET not in json.dumps(st) and KEY not in json.dumps(st)
        assert await acc.spot_balances() == {"BTC": 0.5, "USDT": 250.0, "INJ": 3.0}
        n = sum(1 for p, _ in mock.calls if p.endswith("apiRestrictions"))
        await acc.spot_balances()
        assert sum(1 for p, _ in mock.calls if p.endswith("apiRestrictions")) == n  # checked once per hour
        # The key gains trading on Binance: once the check is due again, the app stops using it.
        mock.restrictions["enableWithdrawals"] = True
        acc._check["at"] -= 4000
        with pytest.raises(BinanceKeyError, match="allows withdrawals"):
            await acc.spot_balances()
        assert acc.status()["ok"] is False and acc.status()["problems"] == ["allows withdrawals"]

    asyncio.run(run())


def test_clock_skew_is_retried_once_with_binance_time():
    mock = MockBinance()
    acc = _account(mock)

    async def run():
        await acc.set_key(KEY, SECRET)
        mock.clock_skew_once = True
        assert (await acc.spot_balances())["BTC"] == 0.5

    asyncio.run(run())


def test_unreachable_binance_refuses_to_use_the_key():
    def down(req):
        raise httpx.ConnectError("offline")

    s = Settings(binance_api_key="", binance_api_secret="")
    acc = BinanceAccount(s, KeyStore(s, path="memory"), httpx.AsyncClient(transport=httpx.MockTransport(down)))

    async def run():
        with pytest.raises(BinanceKeyError, match="Could not reach Binance"):
            await acc.set_key(KEY, SECRET)

    asyncio.run(run())


# ------------------------------------------------------- classification --


def _fill(key, side, price, qty, t, cid=None, oid=None, market="spot", symbol="INJUSDT", **kw):
    return AccountFill(key=key, market=market, symbol=symbol, trade_id=int(key.split(":")[-1]),
                       order_id=oid or int(key.split(":")[-1]), time=t, time_ms=t * 1000, side=side, price=price,
                       qty=qty, quote_qty=price * qty, client_order_id=cid, **kw)


def test_classification_order_of_signals():
    bot = BotRef("b1", "INJ grid", "INJUSDT", 1000, None, np.array([10.0, 11.0, 12.0, 13.0]), 2.0)
    fills = [
        _fill("spot:INJUSDT:1", "buy", 11.0, 2.0, 2000, cid="9fj3kd8s7a6s5d4f3g2h1j"),   # on a line, bot qty → bot
        _fill("spot:INJUSDT:2", "buy", 11.0, 2.0, 2000, cid="web_11"),                  # by hand wins over the grid
        _fill("spot:INJUSDT:3", "buy", 11.0, 5.0, 2000, cid="9fj3kd8s7a6s5d4f3g2h1x"),  # wrong size → unknown
        _fill("spot:INJUSDT:4", "buy", 11.0, 2.0, 500, cid="9fj3kd8s7a6s5d4f3g2h1y"),   # before the bot → unknown
        _fill("spot:INJUSDT:5", "sell", 11.5, 2.0, 2000, cid="x-ABCDEF"),               # between lines, broker id
        _fill("spot:INJUSDT:6", "sell", 12.02, 1.0, 2000, oid=60, cid="7s8d9f0g1h2j3k4l5z"),  # a partial fill...
        _fill("spot:INJUSDT:7", "sell", 12.02, 1.0, 2001, oid=60, cid="7s8d9f0g1h2j3k4l5z"),  # ...the order is 2
        _fill("spot:INJUSDT:8", "buy", 10.0, 2.0, 2000),                                # no order details
    ]
    out = {f.key.split(":")[-1]: f for f in classify(fills, [bot], {"spot:INJUSDT:8": Override(kind="manual")})}
    assert out["1"].kind == "bot" and out["1"].bot_id == "b1" and "grid line 1" in out["1"].reason
    assert out["2"].kind == "manual" and "by hand" in out["2"].reason
    assert out["3"].kind == "unknown" and "API" in out["3"].reason
    assert out["4"].kind == "unknown"
    assert out["5"].kind == "unknown" and "broker" in out["5"].reason
    assert out["6"].kind == out["7"].kind == "bot"
    assert out["8"].kind == "manual" and out["8"].overridden and "You marked it" in out["8"].reason
    assert fills[0].kind == "unknown"  # the input is left alone
    assert match_bot("INJUSDT", 2000, 11.0, 2.0, []) is None


# ------------------------------------------------------------ round trips --


def test_spot_round_trip_pnl_with_fees_in_the_coin():
    t0 = 1_700_000_000_000
    rows = [parse_spot_fill("BTCUSDT", r) for r in (
        _spot_trade(1, 1, "buy", 100.0, 1.0, t0, 0.001, "BTC"),
        _spot_trade(2, 2, "sell", 110.0, 0.999, t0 + 1000, 0.10989, "USDT"))]
    rows = [f.model_copy(update={"kind": "manual"}) for f in rows]
    (trip,) = build_round_trips(rows)
    assert trip.status == "closed" and trip.direction == "long" and trip.qty == pytest.approx(0.999)
    assert trip.entry_price == 100 and trip.exit_price == 110
    # 0.999 * 110 - 100 - 0.10989 quote fee (the 0.001 BTC fee is already in the smaller position)
    assert trip.realized_pnl == pytest.approx(0.999 * 110 - 100 - 0.10989)
    assert trip.fees == pytest.approx(0.10989 + 0.1)
    entry = journal_entry(trip)
    assert entry.source == "binance" and entry.stop is None and entry.imported.external_id == trip.external_id


def test_futures_flip_and_orphan_sells():
    t = 1_700_000_000_000
    rows = [parse_futures_fill({"symbol": "ETHUSDT", "id": i, "orderId": i, "side": s, "price": str(p),
                                "qty": str(q), "realizedPnl": str(r), "commission": "1", "commissionAsset": "USDT",
                                "time": t + i, "positionSide": "BOTH"})
            for i, s, p, q, r in ((1, "BUY", 100, 1, 0), (2, "SELL", 110, 3, 10), (3, "BUY", 105, 2, 10))]
    rows = [f.model_copy(update={"kind": "manual"}) for f in rows]
    a, b = build_round_trips(rows)
    assert (a.direction, a.status, a.qty, a.exit_price) == ("long", "closed", 1, 110)
    assert a.realized_pnl == pytest.approx(10 - 1 - 1 / 3)  # Binance's PnL minus both fills' share of commission
    assert (b.direction, b.status, b.qty, b.entry_price) == ("short", "closed", 2, 110)
    assert b.realized_pnl == pytest.approx(10 - 2 / 3 - 1)
    orphan = [parse_spot_fill("XUSDT", _spot_trade(9, 9, "sell", 5.0, 1.0, t)).model_copy(update={"kind": "manual"})]
    assert build_round_trips(orphan) == []


# ------------------------------------------------------------ the service --


class FakeMarket:
    """1m candles oscillating 103–127 (the grid bot's market) and no Binance for prices."""

    async def get_range(self, symbol, interval, start, end=None):
        now = int(time.time())
        end = now if end is None else min(end, now)
        t = np.arange(start - start % 60, end + 1, 60)
        mid = 115 + 12 * np.sin(2 * np.pi * t / 7200)
        return pd.DataFrame({"time": t, "open": mid, "high": mid + 0.3, "low": mid - 0.3, "close": mid,
                             "volume": 1.0}), "binance"

    async def get_klines(self, symbol, interval, limit=500):
        raise RuntimeError("no klines here")

    def binance_usable(self):
        return False


def _service(mock, tmp_path=None):
    market = FakeMarket()
    s = Settings(binance_api_key="", binance_api_secret="", journal_store="memory")
    gridbots = GridBotService(market, store="memory")
    journal = JournalService(market, s)
    store = str(tmp_path / "import.json") if tmp_path else "memory"
    return BinanceImportService(_account(mock), journal, gridbots, market, s, store=store), gridbots, journal


def test_import_classify_journal_positions_and_bot_compare(tmp_path):
    mock = MockBinance()
    svc, gridbots, journal = _service(mock, tmp_path)

    async def run():
        with pytest.raises(BinanceKeyError, match="No Binance API key"):
            await svc.run()
        await svc.account.set_key(KEY, SECRET)
        bot, result = await gridbots.create(GridBotCreate(name="INJ grid", params=GridBotParams(
            symbol="INJUSDT", lower=100, upper=130, grids=3, investment=1000, runtime="2h", fee_rate=0)))
        q = result.qty_per_order
        start_ms = bot.params.start_time * 1000
        mock.spot["INJUSDT"] = [_spot_trade(70, 70, "buy", 110.0, q, start_ms + 600_000),
                                _spot_trade(71, 71, "sell", 120.0, q, start_ms + 1_200_000)]
        mock.orders.update({70: "Zx81kd9aLq0vB3nM5cR7tY", 71: "Pq92ls0bMr1wC4oN6dS8uZ"})

        out = await svc.run(["BTCUSDT"])
        assert out["new_fills"] == 7 and out["error"] is None
        kinds = {f.key: f.kind for f in svc.fills()}
        assert kinds["spot:BTCUSDT:1"] == kinds["spot:BTCUSDT:2"] == "manual"
        assert kinds["spot:BTCUSDT:3"] == "unknown"
        assert kinds["spot:INJUSDT:70"] == kinds["spot:INJUSDT:71"] == "bot"
        assert kinds["futures:ETHUSDT:501"] == "manual"
        # Only the user's own closed round trips reach the journal: the BTC long and the ETH short.
        rows = await journal.rows()
        assert out["journal"] == {"added": 2, "updated": 0, "removed": 0} and len(rows) == 2
        eth = next(ev for e, ev in rows if e.symbol == "ETHUSDT")
        assert eth.status == "closed" and eth.pnl_usd == pytest.approx(200 - 1.6 - 1.52, abs=0.01)
        assert eth.outcome == "win" and eth.exits[0].kind == "exchange"
        stats = await journal.stats()
        assert stats["no_r"] == 2 and stats["closed"] == 0 and stats["pnl_usd"] == pytest.approx(
            sum(ev.pnl_usd for _, ev in rows))

        # Re-import: nothing new, nothing doubled.
        again = await svc.run(["BTCUSDT"])
        assert again["new_fills"] == 0 and len(svc.fills()) == 7 and len(await journal.rows()) == 2
        assert again["journal"] == {"added": 0, "updated": 0, "removed": 0}
        # The futures fills were fetched in 7-day windows the first time, by trade id after that.
        fut = [p for p, prm in mock.calls if p == "/fapi/v1/userTrades" and "fromId" in prm]
        assert fut

        # Mark the API-placed BTC buy as the user's own: it opens a new (still open) round trip, not in the journal.
        res = await svc.set_classification(ClassifyRequest(keys=["spot:BTCUSDT:3"], kind="manual"))
        assert res["journal"]["added"] == 0
        assert next(f for f in svc.fills() if f.key == "spot:BTCUSDT:3").overridden
        trips = svc.trades("manual")
        assert any(t.symbol == "BTCUSDT" and t.status == "open" for t in trips)
        # Mark the ETH fills as a bot's: the journal entry goes away.
        res = await svc.set_classification(ClassifyRequest(keys=["futures:ETHUSDT:501", "futures:ETHUSDT:502"],
                                                           kind="bot"))
        assert res["journal"]["removed"] == 1 and len(await journal.rows()) == 1
        await svc.set_classification(ClassifyRequest(keys=["futures:ETHUSDT:501", "futures:ETHUSDT:502"], kind=None))
        assert len(await journal.rows()) == 2
        # A deleted imported trade stays deleted.
        btc = next(e for e, _ in await journal.rows() if e.symbol == "BTCUSDT")
        await journal.remove(btc.id)
        await svc.run(["BTCUSDT"])
        assert [e.symbol for e, _ in await journal.rows()] == ["ETHUSDT"]

        # Positions: own spot holdings with average entry, futures positions; bots separate.
        pos = await svc.positions(refresh=True)
        spot = {r["asset"]: r for r in pos["manual"]["spot"]}
        assert spot["BTC"]["qty"] == 0.5 and spot["BTC"]["avg_entry"] == pytest.approx(105.0)
        assert spot["BTC"]["from_fills_qty"] == pytest.approx(0.5)
        assert spot["INJ"]["own_qty"] == pytest.approx(3.0)  # the bot's buy was sold again: nothing of it held
        assert pos["manual"]["cash"] == [{"asset": "USDT", "qty": 250.0}]
        (sol,) = pos["manual"]["futures"]
        assert sol["side"] == "short" and sol["qty"] == 10 and sol["unrealized_pnl"] == 100
        assert pos["bots"]["wallet"] == {"wallet": "Trading Bots", "usdt": 512.5, "active": True}
        assert pos["bots"]["tracked"][0]["bot_id"] == bot.id
        await svc.set_classification(ClassifyRequest(keys=["position:futures:SOLUSDT:BOTH"], kind="bot"))
        pos = await svc.positions()
        assert pos["manual"]["futures"] == [] and pos["bots"]["futures"][0]["symbol"] == "SOLUSDT"
        assert pos["bots"]["futures"][0]["overridden"] and not spot["BTC"]["overridden"]
        # What the trade manager watches: own holdings worth $5+ and own futures positions (SOL is a bot's now).
        keys = await svc.open_position_keys()
        assert "position:futures:SOLUSDT:BOTH" not in keys and "holding:spot:USDT" not in keys
        assert all(k.startswith("holding:spot:") for k in keys)
        assert all(spot[k.split(":")[-1]]["value"] is None or spot[k.split(":")[-1]]["value"] >= 5 for k in keys)

        cmp = await svc.compare_bot(bot.id)
        assert cmp["real_fills"] == 2 and cmp["spot_grid_api"] is False and "no public API" in cmp["notes"][0]
        assert cmp["real_net_quote"] == pytest.approx(q * 10)
        assert {r["side"] for r in cmp["rows"] if r["real"]} == {"buy", "sell"}
        assert await svc.compare_bot("nope") is None
        return bot

    asyncio.run(run())
    # The import file is private and survives a restart (fills, cursors, overrides).
    path = tmp_path / "import.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    again, _, _ = _service(MockBinance(), tmp_path)
    assert len(again.fills()) == 7 and again._overrides["spot:BTCUSDT:3"].kind == "manual"
    assert again._cursors["spot:BTCUSDT"] == 3


def test_auto_import_runs_when_due():
    mock = MockBinance()
    svc, _, _ = _service(mock)

    async def run():
        assert await svc.auto_tick() is False  # off
        svc.update_settings(ImportSettings(auto_minutes=15, symbols=["btc/usdt"], futures=False))
        assert svc.settings.symbols == ["BTCUSDT"]
        assert await svc.auto_tick() is False  # no key
        await svc.account.set_key(KEY, SECRET)
        assert await svc.auto_tick() is True and svc.last["auto"] and svc.last["new_fills"] == 3
        assert await svc.auto_tick() is False  # not due yet
        assert await svc.auto_tick(now=time.time() + 15 * 60) is True
        assert not any(p.startswith("/fapi") for p, _ in mock.calls)  # futures switched off
        with pytest.raises(ValueError):
            ImportSettings(auto_minutes=7)

    asyncio.run(run())


def test_futures_unavailable_is_a_note_not_a_failure():
    mock = MockBinance()
    orig = mock.handler

    def blocked(req):
        if req.url.host == "fapi.binance.com" and not req.url.path.endswith("/time"):
            return httpx.Response(451, json={"code": 0, "msg": "Service unavailable from a restricted location"})
        return orig(req)

    mock.handler = blocked
    svc, _, _ = _service(mock)

    async def run():
        await svc.account.set_key(KEY, SECRET)
        out = await svc.run(["BTCUSDT"])
        assert out["new_fills"] == 3 and any("futures unavailable" in n and "region" in n for n in out["notes"])
        pos = await svc.positions()
        assert pos["manual"]["futures"] == [] and any("futures" in n for n in pos["notes"])

    asyncio.run(run())


# ------------------------------------------------------------------- API --


def test_api_never_returns_the_secret():
    from app.main import app

    mock = MockBinance()
    with TestClient(app) as client:
        old = app.state.binance
        svc, gridbots, journal = _service(mock)
        app.state.binance = svc
        try:
            st = client.get("/api/binance/key").json()
            assert st["configured"] is False
            assert client.post("/api/binance/import", json={}).status_code == 400
            mock.restrictions["enableFutures"] = True
            r = client.put("/api/binance/key", json={"api_key": KEY, "api_secret": SECRET})
            assert r.status_code == 422 and "futures trading" in r.json()["detail"]
            mock.restrictions["enableFutures"] = False
            r = client.put("/api/binance/key", json={"api_key": KEY, "api_secret": SECRET})
            assert r.status_code == 200 and r.json()["ok"] and SECRET not in r.text and KEY not in r.text
            assert client.post("/api/binance/key/test").json()["ok"]
            r = client.post("/api/binance/import", json={"symbols": ["BTCUSDT"]})
            assert r.status_code == 200, r.text
            fills = client.get("/api/binance/fills", params={"kind": "unknown"}).json()["fills"]
            assert [f["key"] for f in fills] == ["spot:BTCUSDT:3"] and fills[0]["reason"]
            r = client.post("/api/binance/classify", json={"keys": ["spot:BTCUSDT:3"], "kind": "manual"})
            assert r.status_code == 200
            assert client.post("/api/binance/classify", json={"keys": ["x"], "kind": "bot",
                                                              "bot_id": "nope"}).status_code == 422
            trades = client.get("/api/binance/trades", params={"kind": "manual"}).json()["trades"]
            assert {t["symbol"] for t in trades} == {"BTCUSDT", "ETHUSDT"}
            pos = client.get("/api/binance/positions").json()
            assert pos["manual"]["futures"][0]["symbol"] == "SOLUSDT"
            status = client.get("/api/binance/import").json()
            assert status["fills"] == 5 and status["spot_grid_api"] is False and status["key"]["ok"]
            r = client.put("/api/binance/import/settings", json={"auto_minutes": 30, "symbols": [], "futures": True,
                                                                 "lookback_days": 30})
            assert r.status_code == 200 and r.json()["settings"]["auto_minutes"] == 30
            assert client.get("/api/binance/gridbots/nope/compare").status_code == 404
            assert client.delete("/api/binance/key").json()["configured"] is False
            assert client.get("/api/binance/positions").status_code == 400
        finally:
            app.state.binance = old


def test_binance_errors_carry_status_and_code():
    exc = BinanceApiError("Invalid symbol. (HTTP 400, code -1121)", 400, -1121)
    assert exc.status == 400 and exc.code == -1121
