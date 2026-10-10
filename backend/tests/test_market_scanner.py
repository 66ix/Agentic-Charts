"""The market-wide setup scanner: universe selection, ranking, the concurrency cap and Binance path (mocked), the
demo fallback, the timer with notifications, the API and the agent's "best setups right now"."""

import asyncio
import json
import os

os.environ["DATA_SOURCE"] = "synthetic"
os.environ["LLM_PROVIDER"] = "none"

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import config  # noqa: E402
from app.agent import run_analysis  # noqa: E402
from app.config import Settings  # noqa: E402
from app.llm import LLMClient  # noqa: E402
from app.main import app  # noqa: E402
from app.market_data import FALLBACK_SYMBOLS, INTERVAL_SECONDS, MarketData, synthetic_klines  # noqa: E402
from app.market_scanner import (  # noqa: E402
    MarketScanner,
    agreement,
    is_leveraged,
    parse_schedule,
    pick_universe,
    score_setup,
    track_score,
)
from app.schemas import AnalyzeRequest, TrackRecord  # noqa: E402


def _ticker(symbol, qv, last=1.0, open_=1.0):
    return {"symbol": symbol, "quoteVolume": str(qv), "lastPrice": str(last), "openPrice": str(open_)}


class StubTrack:
    """Instant track records, so scans test the ranking rather than the backtests."""

    def __init__(self, avg_r=0.5, status="ok"):
        self.calls = []
        self.avg_r, self.status = avg_r, status

    async def for_plan(self, symbol, interval, plan, timeout=None):
        self.calls.append((symbol, interval, plan.direction))
        return TrackRecord(symbol=symbol, interval=interval, status=self.status, trades=25, avg_r=self.avg_r,
                           summary=f"stub record for {symbol}")


def _settings(**kw):
    return Settings(**{"market_scan_store": "memory", "market_scan_top": 100, **kw})


def test_universe_skips_stablecoins_leveraged_tokens_and_dead_pairs():
    tickers = [
        _ticker("BTCUSDT", 9e9, 101, 100), _ticker("ETHUSDT", 5e9), _ticker("USDCUSDT", 8e9),
        _ticker("FDUSDUSDT", 7e9), _ticker("EURUSDT", 1e8), _ticker("BTCUPUSDT", 6e9), _ticker("ETHDOWNUSDT", 6e9),
        _ticker("JUPUSDT", 2e8), _ticker("SYRUPUSDT", 1e8), _ticker("ETHBTC", 9e9), _ticker("DEADUSDT", 0),
        _ticker("SOLUSDT", 3e9), {"symbol": "BADUSDT", "quoteVolume": "x"}, "junk",
    ]
    rows = pick_universe(tickers, 10)
    assert [r["symbol"] for r in rows] == ["BTCUSDT", "ETHUSDT", "SOLUSDT", "JUPUSDT", "SYRUPUSDT"]
    assert rows[0]["change_pct"] == 1.0 and rows[0]["quote_volume"] == 9e9
    assert [r["symbol"] for r in pick_universe(tickers, 2)] == ["BTCUSDT", "ETHUSDT"]
    assert is_leveraged("BNBBULL", {"BNB"}) and not is_leveraged("JUP", {"J"}) and not is_leveraged("SYRUP", {"BTC"})


def test_schedule_parsing():
    assert parse_schedule("15m=10, 4h:60, junk, 7x=3, 1h=1") == {"15m": 10.0, "4h": 60.0, "1h": 5.0}
    assert parse_schedule("") == {}


def test_ranking_parts():
    a = agreement("long", {"4h": "up", "1d": "range", "1w": "down"})
    assert (a.aligned, a.total) == (1.5, 3)
    assert agreement("short", {"4h": "down"}).aligned == 1.0
    assert track_score(None) == 0.5
    assert track_score(TrackRecord(symbol="X", interval="4h", status="ok", avg_r=1.5)) == 1.0
    assert track_score(TrackRecord(symbol="X", interval="4h", status="ok", avg_r=-0.5)) == 0.25
    assert track_score(TrackRecord(symbol="X", interval="4h", status="small_sample", avg_r=1.0)) == 0.75
    assert track_score(TrackRecord(symbol="X", interval="4h", status="too_few_trades")) == 0.5
    full = agreement("long", {"4h": "up", "1d": "up"})
    good = TrackRecord(symbol="X", interval="4h", status="ok", avg_r=0.8)
    # better R:R, closer entry, more agreement and a better record each rank higher
    assert score_setup(3, 0.2, full, good) > score_setup(1.5, 0.2, full, good)
    assert score_setup(3, 0.2, full, good) > score_setup(3, 2.5, full, good)
    assert score_setup(3, 0.2, full, good) > score_setup(3, 0.2, a, good)
    assert score_setup(3, 0.2, full, good) > score_setup(3, 0.2, full, None)


def _scan(scanner, *args, **kw):
    async def go():
        try:
            return await scanner.run(*args, **kw)
        finally:
            await scanner.market.close()

    return asyncio.run(go())


def test_demo_fallback_scan_ranks_both_sides():
    track = StubTrack()
    scanner = MarketScanner(MarketData(), track, settings=_settings())
    res = _scan(scanner, "4h")
    assert res.universe_source == "fallback" and res.data_source == "synthetic"
    assert res.universe == len(FALLBACK_SYMBOLS) and res.scanned == res.universe
    assert any("synthetic" in n.lower() for n in res.notes)
    assert res.longs and res.shorts
    for side, rows in (("long", res.longs), ("short", res.shorts)):
        assert all(r.direction == side for r in rows)
        assert [r.score for r in rows] == sorted((r.score for r in rows), reverse=True)
        for r in rows:
            assert r.rr >= 1.0 and r.plan.zone_kind not in (None, "swing")
            assert (r.entry <= r.last_price) if side == "long" else (r.entry >= r.last_price)
            assert (r.stop < r.entry < r.target) if side == "long" else (r.target < r.entry < r.stop)
            assert r.agreement.total == 3 and set(r.agreement.frames) == {"4h", "1d", "1w"}
            assert {o.kind for o in r.overlays} >= {"plan_entry", "plan_stop", "plan_target"}
    # only the best few per side (and of the spot buys) get a (costly) track record
    sides = min(8, len(res.longs)) + min(8, len(res.shorts))
    assert sides <= len(track.calls) <= sides + min(8, len(res.spot_buys))
    assert res.longs[0].track_record and res.longs[0].plan.track_record.summary.startswith("stub record")
    assert scanner.latest("4h") is res
    best = res.best(3)
    assert len(best) == 3 and best[0].score == max(r.score for r in res.longs + res.shorts)
    assert all(r.direction == "short" for r in res.best(5, "short"))


def test_concurrent_runs_share_one_scan():
    scanner = MarketScanner(MarketData(), StubTrack(), settings=_settings())

    async def go():
        try:
            return await asyncio.gather(scanner.run("1h"), scanner.run("1h"))
        finally:
            await scanner.market.close()

    a, b = asyncio.run(go())
    assert a is b


class FakeBinance:
    """24h tickers and klines, with the peak number of requests in flight."""

    def __init__(self, tickers):
        self.tickers = tickers
        self.series = {}
        self.active = self.peak = 0
        self.kline_symbols = set()

    async def __call__(self, req: httpx.Request) -> httpx.Response:
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.005)
            q = req.url.params
            if req.url.path.endswith("/ticker/24hr"):
                assert q.get("type") == "MINI"
                return httpx.Response(200, json=self.tickers)
            if req.url.path.endswith("/klines"):
                self.kline_symbols.add(q["symbol"])
                key = (q["symbol"], q["interval"])
                if key not in self.series:
                    self.series[key] = synthetic_klines(q["symbol"], q["interval"], 1500)
                step = INTERVAL_SECONDS[q["interval"]]
                rows = [[c.time * 1000, str(c.open), str(c.high), str(c.low), str(c.close), str(c.volume),
                         (c.time + step) * 1000 - 1, "0", 1, "0", "0", "0"] for c in self.series[key]]
                return httpx.Response(200, json=rows[-int(q["limit"]):])
            return httpx.Response(404)
        finally:
            self.active -= 1


@pytest.fixture
def binance_mode(monkeypatch):
    monkeypatch.setenv("DATA_SOURCE", "binance")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def test_live_scan_uses_the_volume_leaders_within_the_concurrency_cap(binance_mode):
    names = ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK"]
    tickers = [_ticker(f"{b}USDT", 1e9 - i * 1e7, 1.02, 1.0) for i, b in enumerate(names)]
    tickers += [_ticker("USDCUSDT", 2e9), _ticker("BTCUPUSDT", 2e9)]
    fake = FakeBinance(tickers)
    md = MarketData()
    md._client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    scanner = MarketScanner(md, StubTrack(), settings=_settings(market_scan_top=6, market_scan_concurrency=2))
    res = _scan(scanner, "4h")
    assert res.universe_source == "binance" and res.data_source == "binance" and res.universe == 6
    assert fake.kline_symbols == {f"{b}USDT" for b in names[:6]}
    assert fake.peak <= 2 * 3  # two coins at a time, each loading its timeframe and the two above it
    assert not any("synthetic" in n.lower() for n in res.notes)
    rows = res.longs + res.shorts
    assert rows and all(r.data_source == "binance" and r.quote_volume and r.change_pct == 2.0 for r in rows)


def test_timer_runs_due_scans_and_sends_the_best(monkeypatch):
    sent = []

    class FakeAlerts:
        channels = ["telegram"]
        channel_status = {"telegram": True, "discord": False}

        async def send_text(self, text):
            sent.append(text)
            return {"telegram": True}

    scanner = MarketScanner(MarketData(), StubTrack(), FakeAlerts(),
                            settings=_settings(market_scan_schedule="4h=60", market_scan_notify_top=3))

    async def go():
        try:
            assert scanner.due() == ["4h"]
            ran = await scanner.tick()
            return ran, scanner.due(), scanner.due(now=scanner.latest("4h").generated_at / 1000 + 3601)
        finally:
            await scanner.market.close()

    ran, due_now, due_later = asyncio.run(go())
    assert ran == ["4h"] and due_now == [] and due_later == ["4h"]
    assert scanner.latest("4h").trigger == "timer"
    assert len(sent) == 1 and sent[0].startswith("Market scan 4h: best setups") and "DEMO DATA" in sent[0]
    assert len([ln for ln in sent[0].splitlines() if ln.startswith(("LONG", "SHORT"))]) == 3
    status = scanner.status("4h")
    assert status["schedule"] == {"4h": 60.0} and status["notify_top"] == 3 and status["result"]["interval"] == "4h"


def test_results_survive_a_restart(tmp_path):
    store = str(tmp_path / "scan.json")
    first = MarketScanner(MarketData(), StubTrack(), settings=_settings(market_scan_store=store))
    res = _scan(first, "1d")
    again = MarketScanner(MarketData(), StubTrack(), settings=_settings(market_scan_store=store))
    kept = again.latest("1d")
    assert kept is not None and kept.generated_at == res.generated_at
    assert [r.symbol for r in kept.longs] == [r.symbol for r in res.longs]


def test_api_status_and_run():
    with TestClient(app) as client:
        app.state.market_scanner.track = StubTrack()
        r = client.get("/api/market-scan", params={"interval": "1d"})
        assert r.status_code == 200 and r.json()["interval"] == "1d" and "schedule" in r.json()
        r = client.post("/api/market-scan/run", params={"interval": "1d", "top": 5})
        assert r.status_code == 200
        body = r.json()
        assert body["result"]["universe"] == 5 and body["result"]["data_source"] == "synthetic"
        row = (body["result"]["longs"] + body["result"]["shorts"])[0]
        assert {"symbol", "entry", "stop", "target", "rr", "distance_pct", "agreement", "track_record", "plan",
                "overlays"} <= set(row)
        assert client.get("/api/market-scan", params={"interval": "1d"}).json()["result"] is not None
        assert client.get("/api/market-scan", params={"interval": "2d"}).status_code == 422


def test_agent_answers_best_setups_from_the_scanner():
    scanner = MarketScanner(MarketData(), StubTrack(), settings=_settings())
    req = AnalyzeRequest(symbol="INJUSDT", interval="4h", prompt="scan the market for shorts on the daily",
                         spot_only=False)
    llm = LLMClient()

    async def go():
        try:
            return await run_analysis(req, scanner.market, llm, scanner=scanner)
        finally:
            await scanner.market.close()
            await llm.close()

    res = asyncio.run(go())
    assert res.intent.scan_market and res.intent.timeframe == "1d" and res.navigate is None
    assert res.setups and all(s.direction == "short" and s.interval == "1d" for s in res.setups)
    assert res.summary.count("Best 1d setups across") == 1 and "stub record" in res.summary
    assert res.plan is None and res.scan == []


def test_tool_loop_can_scan_the_market_first():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append(body)
        if "tools" not in body:  # narration gets the scan in its facts
            assert '"market_scan"' in body["messages"][0]["content"]
            return httpx.Response(200, json={"content": [{"type": "text", "text": "Top long: X."}]})
        assert "scan_market" in {t["name"] for t in body["tools"]}
        if sum(1 for b in seen if "tools" in b) == 1:
            return httpx.Response(200, json={"content": [{"type": "tool_use", "id": "t1", "name": "scan_market",
                                                          "input": {"timeframe": "5m", "direction": "long"}}]})
        result = body["messages"][-1]["content"][0]["content"]
        assert '"setups"' in result and '"direction":"short"' not in result
        plan = {"features": [], "timeframe": "5m", "window_timeframes": ["4h", "1d"], "max_zones": 2,
                "answer_hint": "", "custom_levels": [], "remove": [], "keep_existing": True, "alert_prices": [],
                "alert_targets": [], "symbol": None, "switch_chart": False, "scan_watchlist": False,
                "scan_filter": "bullish", "scan_market": True, "trade_plan": None, "indicators_on": [],
                "indicators_off": []}
        return httpx.Response(200, json={"content": [{"type": "tool_use", "id": "t2", "name": "draw_on_chart",
                                                      "input": plan}]})

    llm = LLMClient(Settings(llm_provider="anthropic", anthropic_api_key="k"))
    llm._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    scanner = MarketScanner(MarketData(), StubTrack(), settings=_settings())
    req = AnalyzeRequest(symbol="INJUSDT", interval="4h", prompt="best 5m longs right now?")

    async def go():
        try:
            return await run_analysis(req, scanner.market, llm, scanner=scanner)
        finally:
            await scanner.market.close()
            await llm.close()

    res = asyncio.run(go())
    assert res.steps == [f"Scanned the top {len(FALLBACK_SYMBOLS)} coins for 5m setups"]
    assert res.summary == "Top long: X." and res.setups and all(s.direction == "long" for s in res.setups)
