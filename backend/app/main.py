"""Agentic Charts API.

REST
  GET  /api/health               service + provider status
  GET  /api/symbols              tradable USDT spot pairs
  GET  /api/klines               historical OHLCV
  GET  /api/market/metrics       global market header metrics
  GET  /api/tickers              last price + 24h change for a list of symbols
  GET  /api/watchlist/scan       nearest zone and signals per watchlist symbol
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
from .config import get_settings
from .derivatives import DerivativesService
from .llm import LLMClient
from .market_data import MarketData, MarketDataError
from .market_metrics import MarketMetricsService
from .ratelimit import RateLimitMiddleware
from .scanner import WatchlistCache, tickers
from .schemas import INTERVALS, AnalyzeRequest, AnalyzeResponse, MarketMetrics, ScanResult
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
    log.info("Data source: %s | LLM provider: %s %s", market.settings.data_source,
             app.state.llm.provider, app.state.llm.model)
    yield
    await app.state.hub.shutdown()
    await asyncio.gather(market.close(), app.state.llm.close(), app.state.metrics.close(),
                         app.state.derivatives.close())


settings = get_settings()
app = FastAPI(title="Agentic Charts API", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_credentials=False,
                   allow_methods=["GET", "POST"], allow_headers=["*"])
app.add_middleware(GZipMiddleware, minimum_size=2048)
app.add_middleware(RateLimitMiddleware, agent_rate=settings.agent_rate_limit, api_rate=settings.api_rate_limit,
                   agent_daily=settings.agent_daily_limit, trust_proxy=settings.trust_proxy)


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


@app.post("/api/agent/analyze", response_model=AnalyzeResponse)
async def agent_analyze(req: AnalyzeRequest, request: Request) -> AnalyzeResponse:
    try:
        return await run_analysis(req, request.app.state.market, request.app.state.llm, request.app.state.derivatives)
    except MarketDataError as exc:
        raise HTTPException(502, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


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

