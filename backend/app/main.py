"""Agentic Charts API.

REST
  GET  /api/health               service + provider status
  GET  /api/symbols              tradable USDT spot pairs
  GET  /api/klines               historical OHLCV
  GET  /api/market/metrics       global market header metrics
  GET  /api/tickers              last price + 24h change for a list of symbols
  GET  /api/watchlist/scan       nearest zone and signals per watchlist symbol
  GET  /api/indicators/kimi      Kimi Cooked v5.7.4: levels, signals, forecast and its two tables
  POST /api/agent/analyze        prompt → structured chart overlays
WebSocket
  /ws/klines?symbol=INJUSDT&interval=4h   live candle updates
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from .agent import run_analysis
from .alerts import AlertService
from .config import get_settings
from .derivatives import DerivativesService
from .events import EventsService
from .futures_data import FUTURES_PERIODS, FuturesDataService
from .kimi_service import KimiService
from .llm import LLMClient
from .market_data import MarketData, MarketDataError
from .market_index import MarketIndexService
from .market_metrics import MarketMetricsService
from .ratelimit import RateLimitMiddleware
from .scanner import WatchlistCache, tickers
from .schemas import (INTERVALS, AnalyzeRequest, AnalyzeResponse, CreateAlertsRequest, KimiResponse, MarketMetrics,
                      ScanResult)
from .stream_hub import StreamHub

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("agentic-charts")


@asynccontextmanager
async def lifespan(app: FastAPI):
    market = MarketData()
    app.state.market = market
    app.state.llm = LLMClient()
    app.state.derivatives = DerivativesService()
    app.state.derivatives.start()
    app.state.metrics = MarketMetricsService(app.state.derivatives)
    app.state.hub = StreamHub(market)
    app.state.watchlist = WatchlistCache(market)
    app.state.kimi = KimiService(market)
    app.state.alerts = AlertService(app.state.hub)
    await app.state.alerts.start()
    # Market data panel (futures, order flow, estimated liquidation levels), calendar + news, market-cap indexes.
    app.state.futures = FuturesDataService(market, app.state.derivatives)
    app.state.events = EventsService()
    app.state.indexes = MarketIndexService(market)
    log.info("Data source: %s | LLM provider: %s %s", market.settings.data_source,
             app.state.llm.provider, app.state.llm.model)
    yield
    await asyncio.gather(app.state.futures.close(), app.state.events.close(), app.state.indexes.close())
    await app.state.alerts.close()
    await app.state.hub.shutdown()
    await asyncio.gather(market.close(), app.state.llm.close(), app.state.metrics.close(),
                         app.state.derivatives.close())


settings = get_settings()
app = FastAPI(title="Agentic Charts API", version="1.0.0", lifespan=lifespan)
# Added first = innermost, so CORS headers also reach 429 responses and the browser can read them.
app.add_middleware(RateLimitMiddleware, agent_rate=settings.agent_rate_limit, api_rate=settings.api_rate_limit,
                   agent_daily=settings.agent_daily_limit, trust_proxy=settings.trust_proxy)
app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_credentials=False,
                   allow_methods=["GET", "POST", "DELETE"], allow_headers=["*"])
app.add_middleware(GZipMiddleware, minimum_size=2048)


def _norm_symbol(symbol: str) -> str:
    s = symbol.replace("/", "").replace("-", "").upper()
    if not s.isalnum() or not 2 <= len(s) <= 20:
        raise HTTPException(422, "Invalid symbol")
    return s


def _check_interval(interval: str) -> str:
    if interval not in INTERVALS:
        raise HTTPException(422, f"interval must be one of {', '.join(INTERVALS)}")
    return interval


@app.get("/api/health")
async def health(request: Request) -> dict:
    st = request.app.state
    return {
        "status": "ok",
        "data_source": st.market.settings.data_source,
        "binance_reachable": st.market.binance_usable(),
        "llm": {"provider": st.llm.provider, "model": st.llm.model},
        "streams": st.hub.stats(),
        "liquidation_stream": st.derivatives.stream_connected,
    }


@app.get("/api/symbols")
async def symbols(request: Request) -> dict:
    return {"symbols": await request.app.state.market.list_symbols()}


@app.get("/api/klines")
async def klines(
    request: Request,
    symbol: str = Query("INJUSDT"),
    interval: str = Query("4h"),
    limit: int = Query(500, ge=10, le=1500),
    since: int | None = Query(None, ge=0, description="Only return candles with time >= since (UNIX seconds)"),
) -> dict:
    sym, iv = _norm_symbol(symbol), _check_interval(interval)
    try:
        candles, source = await request.app.state.market.get_klines(sym, iv, limit)
    except MarketDataError as exc:
        raise HTTPException(502, str(exc)) from exc
    if since is not None:  # the client already has older bars; the candle cache keeps the fetch above cheap
        candles = [c for c in candles if c.time >= since]
    return {"symbol": sym, "interval": iv, "source": source, "candles": [c.model_dump() for c in candles]}


@app.get("/api/market/metrics", response_model=MarketMetrics)
async def market_metrics(request: Request) -> MarketMetrics:
    return await request.app.state.metrics.get()


def _symbol_list(symbols: str) -> list[str]:
    out = [_norm_symbol(s) for s in symbols.split(",") if s.strip()]
    if not out or len(out) > 40:
        raise HTTPException(422, "Give 1 to 40 comma-separated symbols")
    return list(dict.fromkeys(out))


@app.get("/api/tickers")
async def get_tickers(request: Request, symbols: str = Query(..., description="Comma-separated")) -> dict:
    return {"tickers": await tickers(request.app.state.market, _symbol_list(symbols))}


@app.get("/api/watchlist/scan", response_model=list[ScanResult])
async def watchlist_scan(request: Request, symbols: str = Query(...), interval: str = Query("4h")) -> list[ScanResult]:
    return await request.app.state.watchlist.get(_symbol_list(symbols), _check_interval(interval))


@app.get("/api/indicators/kimi", response_model=KimiResponse)
async def kimi_cooked(request: Request, symbol: str = Query("INJUSDT"), interval: str = Query("4h")) -> KimiResponse:
    try:
        return await request.app.state.kimi.get(_norm_symbol(symbol), _check_interval(interval))
    except MarketDataError as exc:
        raise HTTPException(502, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/agent/analyze", response_model=AnalyzeResponse)
async def agent_analyze(req: AnalyzeRequest, request: Request) -> AnalyzeResponse:
    try:
        return await run_analysis(req, request.app.state.market, request.app.state.llm, request.app.state.derivatives,
                                  request.app.state.kimi)
    except MarketDataError as exc:
        raise HTTPException(502, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


# ------------------------------------------------------------ price alerts --
#   GET    /api/alerts                    alerts + which notification channels are configured
#   POST   /api/alerts                    {symbol, alerts: [AlertSpec]} → created alerts
#   DELETE /api/alerts/{id}
#   POST   /api/alerts/{id}/rearm
#   POST   /api/alerts/clear-triggered
#   POST   /api/alerts/test               test message to Telegram / Discord
#   WS     /ws/alerts                     {type:"snapshot", alerts} on connect and change, {type:"fired", alert, price}


@app.get("/api/alerts")
async def list_alerts(request: Request) -> dict:
    service: AlertService = request.app.state.alerts
    return {"alerts": [a.model_dump() for a in service.list()], "channels": service.channel_status}


@app.post("/api/alerts")
async def create_alerts(req: CreateAlertsRequest, request: Request) -> dict:
    try:
        created = await request.app.state.alerts.add(_norm_symbol(req.symbol), req.alerts)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"alerts": [a.model_dump() for a in created]}


@app.delete("/api/alerts/{alert_id}")
async def delete_alert(alert_id: str, request: Request) -> dict:
    if not await request.app.state.alerts.remove(alert_id):
        raise HTTPException(404, "Alert not found")
    return {"ok": True}


@app.post("/api/alerts/clear-triggered")
async def clear_triggered_alerts(request: Request) -> dict:
    return {"removed": await request.app.state.alerts.clear_triggered()}


@app.post("/api/alerts/test")
async def test_alert_channels(request: Request) -> dict:
    service: AlertService = request.app.state.alerts
    if not service.channels:
        raise HTTPException(400, "No notification channels configured; set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID "
                                 "or DISCORD_WEBHOOK_URL on the backend")
    return {"results": await service.send_test()}


@app.post("/api/alerts/{alert_id}/rearm")
async def rearm_alert(alert_id: str, request: Request) -> dict:
    alert = await request.app.state.alerts.rearm(alert_id)
    if alert is None:
        raise HTTPException(404, "Alert not found")
    return {"alert": alert.model_dump()}


@app.websocket("/ws/alerts")
async def ws_alerts(ws: WebSocket) -> None:
    await ws.accept()
    service: AlertService = ws.app.state.alerts
    queue = service.subscribe()

    async def pump() -> None:
        while True:
            await ws.send_json(await queue.get())

    async def drain() -> None:
        while True:
            msg = await ws.receive_json()
            if isinstance(msg, dict) and msg.get("type") == "ping":
                await ws.send_json({"type": "pong"})

    sender, receiver = asyncio.create_task(pump()), asyncio.create_task(drain())
    try:
        await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        for t in (sender, receiver):
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, Exception):
                await t
        service.unsubscribe(queue)


@app.websocket("/ws/klines")
async def ws_klines(ws: WebSocket, symbol: str = "INJUSDT", interval: str = "4h") -> None:
    try:
        sym, iv = _norm_symbol(symbol), _check_interval(interval)
    except HTTPException as exc:
        await ws.close(code=1008, reason=str(exc.detail))
        return
    await ws.accept()
    hub: StreamHub = ws.app.state.hub
    queue = await hub.subscribe(sym, iv)

    async def pump() -> None:
        while True:
            await ws.send_json(await queue.get())

    async def drain() -> None:
        # Consume client frames (pings) so disconnects are noticed promptly.
        while True:
            msg = await ws.receive_json()
            if isinstance(msg, dict) and msg.get("type") == "ping":
                await ws.send_json({"type": "pong"})

    sender, receiver = asyncio.create_task(pump()), asyncio.create_task(drain())
    try:
        await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        for t in (sender, receiver):
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, Exception):
                await t
        await hub.unsubscribe(sym, iv, queue)



# ------------------------------------------- market data panel, calendar + news, market-cap indexes --
# Every response carries `source`: "binance" (live), "synthetic" (demo data) or "unavailable" (with a `note`);
# calendar and news use "live", "stale" (last good copy) or "unavailable". See futures_data.py, events.py and
# market_index.py.
#   GET /api/futures/funding?symbol=BTCUSDT&limit=100               current rate, annualised, history, 24h average
#   GET /api/futures/open-interest?symbol=BTCUSDT&period=1h&limit=200
#   GET /api/futures/long-short?symbol=BTCUSDT&period=1h&limit=200  account ratio + top-trader position ratio
#   GET /api/futures/liquidation-levels?symbol=BTCUSDT               estimated clusters + recent real liquidations
#   GET /api/cvd?symbol=BTCUSDT&interval=1h&limit=500                spot taker buy/sell volume and its running sum
#   GET /api/orderbook/walls?symbol=BTCUSDT&range_pct=5&market=spot  big resting orders near price
#   GET /api/calendar?days=7&impact=high&past_days=0                 economic events (Forex Factory)
#   GET /api/news?symbol=BTCUSDT&limit=30                            RSS headlines tagged with coins
#   GET /api/index/klines?name=TOTAL2&interval=4h&limit=500          market-cap index candles (top-20 approximation)


def _futures_period(period: str) -> str:
    if period not in FUTURES_PERIODS:
        raise HTTPException(422, f"period must be one of {', '.join(FUTURES_PERIODS)}")
    return period


@app.get("/api/futures/funding")
async def futures_funding(request: Request, symbol: str = Query("BTCUSDT"),
                          limit: int = Query(100, ge=1, le=1000)) -> dict:
    return await request.app.state.futures.funding(_norm_symbol(symbol), limit)


@app.get("/api/futures/open-interest")
async def futures_open_interest(request: Request, symbol: str = Query("BTCUSDT"), period: str = Query("1h"),
                                limit: int = Query(200, ge=2, le=500)) -> dict:
    return await request.app.state.futures.open_interest(_norm_symbol(symbol), _futures_period(period), limit)


@app.get("/api/futures/long-short")
async def futures_long_short(request: Request, symbol: str = Query("BTCUSDT"), period: str = Query("1h"),
                             limit: int = Query(200, ge=2, le=500)) -> dict:
    return await request.app.state.futures.long_short(_norm_symbol(symbol), _futures_period(period), limit)


@app.get("/api/futures/liquidation-levels")
async def futures_liquidation_levels(request: Request, symbol: str = Query("BTCUSDT")) -> dict:
    return await request.app.state.futures.liquidation_levels(_norm_symbol(symbol))


@app.get("/api/cvd")
async def cvd(request: Request, symbol: str = Query("BTCUSDT"), interval: str = Query("1h"),
              limit: int = Query(500, ge=2, le=1500)) -> dict:
    return await request.app.state.futures.cvd(_norm_symbol(symbol), _check_interval(interval), limit)


@app.get("/api/orderbook/walls")
async def orderbook_walls(request: Request, symbol: str = Query("BTCUSDT"),
                          range_pct: float = Query(5.0, ge=0.5, le=20.0),
                          market: str = Query("spot", pattern="^(spot|futures)$")) -> dict:
    return await request.app.state.futures.walls(_norm_symbol(symbol), range_pct, market)


@app.get("/api/calendar")
async def economic_calendar(request: Request, days: int = Query(7, ge=0, le=14),
                            impact: str = Query("high", pattern="^(high|medium|all)$"),
                            past_days: int = Query(0, ge=0, le=7)) -> dict:
    return await request.app.state.events.calendar(days, impact, past_days)


@app.get("/api/news")
async def crypto_news(request: Request, symbol: str | None = Query(None),
                      limit: int = Query(30, ge=1, le=200)) -> dict:
    return await request.app.state.events.news(_norm_symbol(symbol) if symbol else None, limit)


@app.get("/api/index/klines")
async def index_klines(request: Request, name: str = Query("TOTAL", description="TOTAL, TOTAL2 or TOTAL3"),
                       interval: str = Query("4h"), limit: int = Query(500, ge=10, le=1500)) -> dict:
    try:
        return await request.app.state.indexes.klines(name, _check_interval(interval), limit)
    except MarketDataError as exc:
        raise HTTPException(502, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
