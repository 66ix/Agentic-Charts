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
  *    /api/alerts*              price alerts, signal alerts and the alert history
  *    /api/brief*               the scheduled market brief
  *    /api/market-scan*         best long / short setups across the top coins by volume
  GET  /api/levels/sessions      session (Asia/London/NY), previous day/week/month and opening-range levels
  GET  /api/sessions/clock       which sessions are open now, and when each next opens or closes
  GET  /api/orderbook/heatmap    resting order-book liquidity over time (sampled while someone polls it)
WebSocket
  /ws/klines?symbol=INJUSDT&interval=4h   live candle updates
  /ws/alerts                              alert snapshots, fires and history items
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import Body, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import StreamingResponse

from .agent import run_analysis
from .alerts import AlertPatch, AlertService
from .backtest import BacktestRequest, run_backtest
from .brief import BriefService, BriefSettings, NoChannelError
from .config import get_settings
from .derivatives import DerivativesService
from .gridbot import GridBotCreate, GridBotPatch, GridBotService, GridSimulateRequest, error_text
from .grid_planner import GridBacktestRequest, GridPlanRequest, backtest_grid, plan_grid
from .dip_ladder import LadderRequest, plan_ladder
from .sell_check import sell_scan
from .top_down import ladder as walk_ladder
from .top_down import walk
from .binance_account import BinanceAccount, BinanceApiError, BinanceKeyError
from .binance_import import BinanceImportService, ClassifyRequest, ImportSettings
from .journal import JournalPatch, JournalService, NewJournalEntry, entry_json
from .dca import DcaRequest, plan_dca
from .holdings_watch import HoldingsWatch, WatchSettings
from .paper import NewPaperOrder, PaperService
from .events import EventsService
from .futures_data import FUTURES_PERIODS, FuturesDataService
from .kimi_service import KimiService
from .llm import LLMClient
from .postmortem import PostMortemService, ReviewSettings
from .market_data import INTERVAL_SECONDS, MarketData, MarketDataError, candles_to_df
from .market_index import MarketIndexService
from .market_metrics import MarketMetricsService
from .model_choice import ModelChoice, ModelChooser
from .market_scanner import MarketScanner
from .orderbook_heatmap import OrderbookHeatmapService
from .ratelimit import RateLimitMiddleware
from .scanner import DEFAULT_WATCHLIST, WatchlistCache, tickers
from .session_levels import SessionLevelsService, session_clock
from .schemas import AnalysisIntent
from .schemas import norm_symbol as schemas_norm
from .screenshot import SCHEMA as SHOT_SCHEMA
from .screenshot import SYSTEM as SHOT_SYSTEM
from .screenshot import ScreenshotRead, ScreenshotRequest, clean, compare, describe, split_image, to_overlays
from .symbols import find_symbol
from .ta_agent import analyze as analyze_chart
from .schemas import (INTERVALS, AnalyzeRequest, AnalyzeResponse, CreateAlertsRequest, KimiResponse, MarketMetrics,
                      ScanResult, ZoneTriggerSpec)
from .metric_alerts import CreateMetricAlertsRequest, MetricAlertService
from .signal_alerts import SIGNALS, CreateSignalAlertsRequest, SignalAlertPatch, SignalAlertService
from .stream_hub import StreamHub
from .trade_manager import NewManagedTrade, TradeManager, TradePatch, from_journal
from .track_record import TrackRecordService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("agentic-charts")


@asynccontextmanager
async def lifespan(app: FastAPI):
    market = MarketData()
    app.state.market = market
    app.state.llm = LLMClient()
    app.state.models = ModelChooser(app.state.llm)  # model picked in the app's settings, and model evals
    app.state.derivatives = DerivativesService()
    app.state.derivatives.start()
    app.state.metrics = MarketMetricsService(app.state.derivatives)
    app.state.hub = StreamHub(market)
    app.state.watchlist = WatchlistCache(market)
    app.state.kimi = KimiService(market)
    app.state.alerts = AlertService(app.state.hub)
    await app.state.alerts.start()
    app.state.gridbots = GridBotService(market)  # grid bot tracker: saved bots, results cached per 1m bar
    app.state.journal = JournalService(market)  # trade journal: entries tracked on 1m candles
    app.state.paper = PaperService(market)  # spot paper wallet, replayed on 1m candles
    # Read-only Binance account: fills → journal, bot vs manual, positions (binance_account.py, binance_import.py).
    app.state.binance = BinanceImportService(BinanceAccount(), app.state.journal, app.state.gridbots, market)
    app.state.binance.start()
    # Market data panel (futures, order flow, estimated liquidation levels), calendar + news, market-cap indexes.
    app.state.futures = FuturesDataService(market, app.state.derivatives)
    app.state.events = EventsService()
    app.state.indexes = MarketIndexService(market)
    # Signal alerts and the scheduled brief (signal_alerts.py, brief.py); both notify through app.state.alerts.
    # The brief lists today's high-impact economic events from the calendar.
    app.state.signal_alerts = SignalAlertService(app.state.hub, market, app.state.kimi, app.state.alerts)
    # Sell-or-trim alerts kept in step with the coins held on Binance (holdings_watch.py).
    app.state.holdings_watch = HoldingsWatch(app.state.binance, app.state.signal_alerts)
    app.state.holdings_watch.start()
    await app.state.signal_alerts.start()
    # Alerts on the header bar: Fear & Greed, BTC dominance, market cap... (metric_alerts.py).
    app.state.metric_alerts = MetricAlertService(app.state.metrics, app.state.alerts,
                                                 get_settings().metric_alerts_store)
    app.state.metric_alerts.start()
    app.state.brief = BriefService(market, app.state.kimi, app.state.derivatives, app.state.alerts,
                                   events_provider=app.state.events.upcoming_events)
    app.state.brief.start()
    # Trade manager: live trades it watches on closed candles, advising through the same channels.
    app.state.trades = TradeManager(market, app.state.alerts)
    app.state.trades.open_positions = app.state.binance.open_position_keys  # closes positions sold on Binance
    app.state.trades.start()
    # Market-wide setup scanner (market_scanner.py) with plan track records (track_record.py), shared with the agent.
    app.state.market_scanner = MarketScanner(market, TrackRecordService(market), app.state.alerts)
    app.state.market_scanner.start()
    # Session/period levels (session_levels.py) and the order-book heatmap (orderbook_heatmap.py).
    app.state.session_levels = SessionLevelsService(market)
    app.state.heatmap = OrderbookHeatmapService(market, app.state.hub)
    # Post-mortems of closed journal trades and the weekly trade review (postmortem.py).
    app.state.postmortems = PostMortemService(app.state.journal, market, app.state.llm, app.state.alerts,
                                              kimi=app.state.kimi)
    app.state.postmortems.start()
    log.info("Data source: %s | LLM provider: %s %s", market.settings.data_source,
             app.state.llm.provider, app.state.llm.model)
    yield
    await app.state.postmortems.close()
    await app.state.market_scanner.close()
    await app.state.brief.close()
    await app.state.models.close()
    await app.state.trades.close()
    await app.state.heatmap.close()
    await app.state.holdings_watch.close()
    await app.state.binance.close()
    await app.state.binance.account.close()
    await app.state.signal_alerts.close()
    await app.state.metric_alerts.close()
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
                   allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"], allow_headers=["*"])
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
        st = request.app.state
        return await run_analysis(req, st.market, st.llm, st.derivatives, st.kimi,
                                  getattr(st, "futures", None), getattr(st, "events", None),
                                  getattr(st, "market_scanner", None), levels=getattr(st, "session_levels", None),
                                  gridbots=getattr(st, "gridbots", None), metrics=getattr(st, "metrics", None),
                                  metric_alerts=getattr(st, "metric_alerts", None))
    except MarketDataError as exc:
        raise HTTPException(502, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/agent/analyze/stream")
async def agent_analyze_stream(req: AnalyzeRequest, request: Request) -> StreamingResponse:
    """`/api/agent/analyze` as server-sent events (not gzipped, so each arrives at once): {"type":"result","response"}
    with the drawings, plan and the rest as soon as they are ready (summary still empty), {"type":"delta","text"}
    pieces of the summary as the model writes it, then {"type":"done","response"} with everything, or
    {"type":"error","status","detail"}."""
    st = request.app.state
    queue: asyncio.Queue[dict | None] = asyncio.Queue()

    async def on_result(res: AnalyzeResponse) -> None:
        await queue.put({"type": "result", "response": res.model_dump(mode="json")})

    async def on_delta(text: str) -> None:
        await queue.put({"type": "delta", "text": text})

    async def work() -> None:
        try:
            res = await run_analysis(req, st.market, st.llm, st.derivatives, st.kimi,
                                     getattr(st, "futures", None), getattr(st, "events", None),
                                     getattr(st, "market_scanner", None), levels=getattr(st, "session_levels", None),
                                     gridbots=getattr(st, "gridbots", None), metrics=getattr(st, "metrics", None),
                                     metric_alerts=getattr(st, "metric_alerts", None),
                                     on_result=on_result, on_delta=on_delta)
            await queue.put({"type": "done", "response": res.model_dump(mode="json")})
        except MarketDataError as exc:
            await queue.put({"type": "error", "status": 502, "detail": str(exc)})
        except ValueError as exc:
            await queue.put({"type": "error", "status": 422, "detail": str(exc)})
        except Exception as exc:  # noqa: BLE001 - the client gets an error line instead of a cut-off stream
            log.exception("Streamed analysis failed")
            await queue.put({"type": "error", "status": 500, "detail": f"Analysis failed: {type(exc).__name__}"})
        finally:
            await queue.put(None)

    async def lines():
        task = asyncio.create_task(work())
        try:
            while (event := await queue.get()) is not None:
                yield f"data: {json.dumps(event, default=float)}\n\n"
        finally:
            task.cancel()

    return StreamingResponse(lines(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# ------------------------------- signal alerts, history and the brief --
# (ahead of the price-alert routes so DELETE /api/alerts/history is not read as an alert id)
#   PATCH  /api/alerts/{id}               edit price / zone / label / note / repeat / expires_at
#   GET    /api/alerts/history?limit=     every fire (price, signal, brief), newest first
#   DELETE /api/alerts/history            clear it
#   GET    /api/metric-alerts             {alerts}: alerts on the header bar (Fear & Greed, BTC dominance...)
#   POST   /api/metric-alerts             {alerts: [{metric, condition: above|below|moves, value, note?}]}
#   DELETE /api/metric-alerts/{id}
#   GET    /api/signal-alerts             {alerts, signals: [{id, name, description}]}
#   POST   /api/signal-alerts             {symbols, interval, signal, repeat?, note?} → one alert per symbol
#   PATCH  /api/signal-alerts/{id}        {armed?, repeat?, note?}
#   DELETE /api/signal-alerts/{id}
#   GET    /api/signal-alerts/preview     symbol, interval, signal, bars → when it would have fired
#   GET    /api/brief/settings            {settings, channels, last_sent_at, default_symbols}
#   PUT    /api/brief/settings            BriefSettings → the same shape
#   GET    /api/brief/preview             {text, messages, generated_at, ...}; symbols/interval override
#   POST   /api/brief/send                sends it now; 400 without a channel
#   WS     /ws/alerts                     also {type:"signal_snapshot"|"signal_fired"|"history"}


@app.patch("/api/alerts/{alert_id}")
async def update_alert(alert_id: str, patch: AlertPatch, request: Request) -> dict:
    try:
        alert = await request.app.state.alerts.update(alert_id, patch)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if alert is None:
        raise HTTPException(404, "Alert not found")
    return {"alert": alert.model_dump()}


@app.get("/api/alerts/history")
async def alert_history(request: Request, limit: int = Query(100, ge=1, le=500)) -> dict:
    return {"items": request.app.state.alerts.history.list(limit)}


@app.delete("/api/alerts/history")
async def clear_alert_history(request: Request) -> dict:
    return {"removed": request.app.state.alerts.history.clear()}


@app.get("/api/metric-alerts")
async def list_metric_alerts(request: Request) -> dict:
    return {"alerts": [a.model_dump() for a in request.app.state.metric_alerts.list()]}


@app.post("/api/metric-alerts")
async def create_metric_alerts(req: CreateMetricAlertsRequest, request: Request) -> dict:
    try:
        created = await request.app.state.metric_alerts.add(req.alerts)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"alerts": [a.model_dump() for a in created]}


@app.delete("/api/metric-alerts/{alert_id}")
async def delete_metric_alert(alert_id: str, request: Request) -> dict:
    if not request.app.state.metric_alerts.remove(alert_id):
        raise HTTPException(404, "No such market alert")
    return {"ok": True}


@app.get("/api/signal-alerts")
async def list_signal_alerts(request: Request) -> dict:
    service: SignalAlertService = request.app.state.signal_alerts
    return {"alerts": [a.model_dump() for a in service.list()],
            "signals": [{"id": k, "name": v.name, "description": v.description} for k, v in SIGNALS.items()]}


@app.post("/api/signal-alerts")
async def create_signal_alerts(req: CreateSignalAlertsRequest, request: Request) -> dict:
    try:
        created = await request.app.state.signal_alerts.add(req.symbols, req.interval, req.signal, req.repeat,
                                                            req.note)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"alerts": [a.model_dump() for a in created]}


@app.get("/api/signal-alerts/preview")
async def preview_signal_alert(request: Request, symbol: str = Query(...), interval: str = Query("4h"),
                               signal: str = Query(...), bars: int = Query(300, ge=10, le=1000)) -> dict:
    try:
        return await request.app.state.signal_alerts.preview(_norm_symbol(symbol), _check_interval(interval),
                                                             signal, bars)
    except MarketDataError as exc:
        raise HTTPException(502, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


# Zone trigger alerts (zone_triggers.py) are signal alerts with signal "zone_trigger": listed, edited and deleted
# with the routes above.
#   POST   /api/zone-triggers             ZoneTriggerSpec → {alert}
#   POST   /api/zone-triggers/preview     ZoneTriggerSpec, ?bars= → {zone, hits: [{time, price, text, stop}], note?}


@app.post("/api/zone-triggers")
async def create_zone_trigger(spec: ZoneTriggerSpec, request: Request) -> dict:
    try:
        alert = await request.app.state.signal_alerts.add_trigger(spec)
    except MarketDataError as exc:
        raise HTTPException(502, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"alert": alert.model_dump()}


@app.post("/api/zone-triggers/preview")
async def preview_zone_trigger(spec: ZoneTriggerSpec, request: Request,
                               bars: int = Query(300, ge=10, le=1000)) -> dict:
    try:
        return await request.app.state.signal_alerts.preview_trigger(spec, bars)
    except MarketDataError as exc:
        raise HTTPException(502, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.patch("/api/signal-alerts/{alert_id}")
async def update_signal_alert(alert_id: str, patch: SignalAlertPatch, request: Request) -> dict:
    alert = await request.app.state.signal_alerts.update(alert_id, patch)
    if alert is None:
        raise HTTPException(404, "Signal alert not found")
    return {"alert": alert.model_dump()}


@app.delete("/api/signal-alerts/{alert_id}")
async def delete_signal_alert(alert_id: str, request: Request) -> dict:
    if not await request.app.state.signal_alerts.remove(alert_id):
        raise HTTPException(404, "Signal alert not found")
    return {"ok": True}


@app.get("/api/brief/settings")
async def brief_settings(request: Request) -> dict:
    return request.app.state.brief.status()


@app.put("/api/brief/settings")
async def save_brief_settings(settings_in: BriefSettings, request: Request) -> dict:
    service: BriefService = request.app.state.brief
    service.update_settings(settings_in)
    return service.status()


@app.get("/api/brief/preview")
async def brief_preview(request: Request, symbols: str | None = Query(None, description="Comma-separated override"),
                        interval: str | None = Query(None)) -> dict:
    try:
        brief = await request.app.state.brief.build(_symbol_list(symbols) if symbols else None,
                                                    _check_interval(interval) if interval else None)
    except MarketDataError as exc:
        raise HTTPException(502, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return brief.model_dump(mode="json")


@app.post("/api/brief/send")
async def brief_send(request: Request, symbols: str | None = Query(None), interval: str | None = Query(None)) -> dict:
    try:
        return await request.app.state.brief.send(_symbol_list(symbols) if symbols else None,
                                                  _check_interval(interval) if interval else None)
    except NoChannelError as exc:
        raise HTTPException(400, str(exc)) from exc
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


# --------------------------------------------------------------- grid bots --
#   POST   /api/gridbot/simulate          GridBotParams (+ binance?) → GridBotResult, nothing saved
#   GET    /api/gridbots                  {bots: [GridBot]}
#   POST   /api/gridbots                  {name?, params, binance?} → {bot, result}
#   PATCH  /api/gridbots/{id}             {name?, params? (partial), binance? (null clears)} → {bot, result}
#   DELETE /api/gridbots/{id}             {ok}
#   GET    /api/gridbots/{id}/result      GridBotResult (recomputed at most once per 1m bar)
# Bodies are validated here rather than by FastAPI so a bad setting comes back as one readable 422 message.


def _gridbot_body(model, body: dict):
    try:
        return model.model_validate(body)
    except ValueError as exc:  # pydantic's ValidationError is a ValueError
        raise HTTPException(422, error_text(exc)) from None


async def _gridbot_run(coro):
    try:
        return await coro
    except MarketDataError as exc:
        raise HTTPException(502, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, error_text(exc)) from None


@app.post("/api/gridbot/simulate")
async def gridbot_simulate(request: Request, body: dict = Body(...)) -> dict:
    req: GridSimulateRequest = _gridbot_body(GridSimulateRequest, body)
    result = await _gridbot_run(request.app.state.gridbots.simulate(req, req.binance))
    return result.model_dump()


# ------------------------------------------------------------------ trade manager


@app.get("/api/trades/managed")
async def trades_list(request: Request) -> dict:
    return {"trades": [t.model_dump(exclude={"keys"}) for t in request.app.state.trades.list()]}


@app.post("/api/trades/managed")
async def trades_add(request: Request, body: dict = Body(...)) -> dict:
    """A trade to manage: its fields, or {"journal_id", "interval"?} to manage a journal entry."""
    st = request.app.state
    try:
        if body.get("journal_id"):
            entry = st.journal.get(body["journal_id"])
            if entry is None:
                raise HTTPException(404, "Journal entry not found")
            new = from_journal(entry, body.get("interval"))
        else:
            new = NewManagedTrade.model_validate(body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return (await st.trades.add(new)).model_dump(exclude={"keys"})


@app.patch("/api/trades/managed/{trade_id}")
async def trades_update(trade_id: str, request: Request, body: dict = Body(...)) -> dict:
    try:
        t = await request.app.state.trades.update(trade_id, TradePatch.model_validate(body))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if t is None:
        raise HTTPException(404, "Managed trade not found")
    return t.model_dump(exclude={"keys"})


@app.delete("/api/trades/managed/{trade_id}")
async def trades_delete(trade_id: str, request: Request) -> dict:
    if not request.app.state.trades.remove(trade_id):
        raise HTTPException(404, "Managed trade not found")
    return {"ok": True}


# ------------------------------------------------------------------ AI model (Settings → AI model)


@app.get("/api/llm/models")
async def llm_models(request: Request) -> dict:
    models = request.app.state.models
    return {**models.current(), "ollama": await models.ollama_models()}


@app.put("/api/llm/model")
async def llm_choose(request: Request, body: dict = Body(...)) -> dict:
    """{"provider", "model"} switches the agent's model; {"provider": null} goes back to the .env one."""
    try:
        choice = ModelChoice.model_validate(body) if body.get("provider") else None
        return request.app.state.models.choose(choice)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/llm/evals")
async def llm_evals(request: Request) -> dict:
    return {"runs": [r.model_dump(exclude={"cases"}) for r in request.app.state.models.runs()]}


@app.post("/api/llm/evals")
async def llm_eval_start(request: Request, body: dict = Body(...)) -> dict:
    try:
        limit = body.pop("limit", None)
        run = request.app.state.models.start_eval(ModelChoice.model_validate(body), limit)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return run.model_dump()


@app.get("/api/llm/evals/{run_id}")
async def llm_eval_get(run_id: str, request: Request) -> dict:
    run = request.app.state.models.run(run_id)
    if run is None:
        raise HTTPException(404, "Eval run not found")
    return run.model_dump()


# Grid bot planner (grid_planner.py):
#   POST   /api/gridbot/plan              {symbol, investment?, timeframe?, grid_type?, fee_rate?, ...} → GridPlan
#   POST   /api/gridbot/backtest          {symbol, lower, upper, grids, grid_type, investment, days, compare_grids}


@app.post("/api/gridbot/plan")
async def gridbot_plan(request: Request, body: dict = Body(...)) -> dict:
    req: GridPlanRequest = _gridbot_body(GridPlanRequest, body)
    return (await _gridbot_run(plan_grid(request.app.state.gridbots, req))).model_dump()


@app.post("/api/gridbot/backtest")
async def gridbot_backtest(request: Request, body: dict = Body(...)) -> dict:
    req: GridBacktestRequest = _gridbot_body(GridBacktestRequest, body)
    return (await _gridbot_run(backtest_grid(request.app.state.gridbots, req))).model_dump()


# Spot tools:
#   POST   /api/top-down                  {symbol, start?, end?} → TopDownResult (top_down.py)
#   POST   /api/dip-ladder                {symbol, budget?, rungs?, timeframe?, days?, fee_rate?} → LadderResult
#   POST   /api/sell-check                {symbols, interval?} → [SellSignal] (sell_check.py)


@app.post("/api/top-down")
async def top_down_walk(request: Request, body: dict = Body(...)) -> dict:
    symbol = _norm_symbol(str(body.get("symbol") or ""))
    tfs = walk_ladder(str(body.get("start") or "1d"), str(body.get("end") or "15m"))
    try:
        return (await walk(request.app.state.market, symbol, tfs[0], tfs[-1])).model_dump()
    except MarketDataError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/dip-ladder")
async def dip_ladder(request: Request, body: dict = Body(...)) -> dict:
    req: LadderRequest = _gridbot_body(LadderRequest, body)
    try:
        return (await plan_ladder(request.app.state.market, req)).model_dump()
    except MarketDataError as exc:
        raise HTTPException(502, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/dca")
async def dca_plan(req: DcaRequest, request: Request) -> dict:
    """Lump sum vs weekly / daily DCA vs buying the dips on one coin over the last `days` (dca.py)."""
    req = req.model_copy(update={"symbol": _norm_symbol(schemas_norm(req.symbol))})
    try:
        df, source = await request.app.state.market.get_range(req.symbol, "1d",
                                                              int(time.time()) - (req.days + 40) * 86400)
        return plan_dca(df, req, source).model_dump()
    except MarketDataError as exc:
        raise HTTPException(502, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/replay/levels")
async def replay_levels(request: Request, symbol: str = Query(...), interval: str = Query("4h"),
                        time_: int = Query(..., alias="time", description="Last candle's open time, UNIX s")) -> dict:
    """Replay mode: the support/resistance and supply/demand zones the detectors would have drawn with only the
    candles up to `time`, so you can step forward and see how price treated them."""
    sym, iv = _norm_symbol(symbol), _check_interval(interval)
    step = INTERVAL_SECONDS[iv]
    try:
        df, source = await request.app.state.market.get_range(sym, iv, time_ - 400 * step, time_ + step - 1)
    except MarketDataError as exc:
        raise HTTPException(422 if "Unsupported" in str(exc) else 502, str(exc)) from exc
    df = df[df["time"] <= time_].reset_index(drop=True)
    if len(df) < 60:
        raise HTTPException(422, "Not enough candles before this point")
    intent = AnalysisIntent(features=["support_resistance", "supply_demand"], window_timeframes=[], max_zones=3)
    res = await asyncio.to_thread(analyze_chart, df, intent, iv)
    return {"symbol": sym, "interval": iv, "time": int(df["time"].iloc[-1]), "source": source,
            "overlays": [o.model_dump() for o in res.overlays]}


@app.post("/api/screenshot")
async def read_screenshot(req: ScreenshotRequest, request: Request) -> dict:
    """A pasted chart screenshot → the coin and timeframe it shows, its drawn levels, boxes, trendlines and
    patterns as overlays, and how they compare with the levels the app's detectors find on the real candles."""
    st = request.app.state
    try:
        image = split_image(req.image)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    llm: LLMClient = st.llm
    if not llm.available():
        raise HTTPException(503, "Reading a screenshot needs an AI model that can see images: set LLM_PROVIDER to "
                                 "anthropic or openai, or use ollama with a vision model such as llama3.2-vision.")
    hint = (f"The chart open in the app is {req.symbol} {req.interval or ''}; use it only when the image shows no "
            "coin or timeframe." if req.symbol else "")
    got = await llm.read_image(SHOT_SYSTEM, hint or "Read this chart.", image, SHOT_SCHEMA, "chart_screenshot")
    if got is None:
        raise HTTPException(502, "The AI model could not read the image. It may not support images.")
    fields, engine = got
    try:
        raw = ScreenshotRead.model_validate(fields)
    except ValueError as exc:  # pydantic's ValidationError
        raise HTTPException(502, "The AI model's answer about the image was malformed") from exc
    fallback = None
    if req.symbol:
        try:
            fallback = _norm_symbol(schemas_norm(req.symbol))
        except HTTPException:
            fallback = None
    read_sym = find_symbol(raw.symbol) if raw.symbol else None
    if raw.symbol and not read_sym:
        cleaned = re.sub(r"[^A-Za-z0-9]", "", raw.symbol).upper()
        read_sym = cleaned if 5 <= len(cleaned) <= 20 and cleaned.endswith(("USDT", "USDC", "FDUSD")) else None
    interval = raw.interval if raw.interval in INTERVALS else (req.interval if req.interval in INTERVALS else "4h")
    symbol, df, source, detected = None, None, None, []
    for sym in dict.fromkeys(x for x in (read_sym, fallback) if x):
        try:
            candles, source = await st.market.get_klines(sym, interval, 300)
        except MarketDataError:
            continue
        if len(candles) >= 60:
            symbol, df = sym, candles_to_df(candles)
            break
    if df is not None:
        intent = AnalysisIntent(features=["support_resistance", "supply_demand"], window_timeframes=[], max_zones=4)
        detected = (await asyncio.to_thread(analyze_chart, df, intent, interval)).overlays
        read, dropped = clean(raw, (float(df["low"].min()), float(df["high"].max())))
    else:
        read, dropped = clean(raw)
    matched = compare(read, detected) if df is not None else None
    summary = describe(read, symbol or read_sym or raw.symbol, interval, matched, dropped)
    if read_sym and symbol and symbol != read_sym:
        summary += f" Could not load {read_sym}, so this is compared with {symbol}."
    return {"symbol": symbol, "interval": interval, "read": read.model_dump(),
            "overlays": [o.model_dump() for o in to_overlays(read)],
            "detected": [o.model_dump() for o in detected], "summary": summary, "engine": engine,
            "data_source": source}


@app.post("/api/sell-check")
async def sell_check(request: Request, body: dict = Body(...)) -> list[dict]:
    raw = body.get("symbols") or DEFAULT_WATCHLIST
    if not isinstance(raw, list):
        raise HTTPException(422, "symbols must be a list")
    symbols = [s for s in (_norm_symbol(str(x)) for x in raw[:40]) if s]
    rows = await sell_scan(request.app.state.market, symbols, str(body.get("interval") or "4h"))
    return [r.model_dump() for r in rows]


@app.get("/api/gridbots")
async def gridbots_list(request: Request) -> dict:
    return {"bots": [b.model_dump() for b in request.app.state.gridbots.list()]}


@app.post("/api/gridbots")
async def gridbots_create(request: Request, body: dict = Body(...)) -> dict:
    req: GridBotCreate = _gridbot_body(GridBotCreate, body)
    bot, result = await _gridbot_run(request.app.state.gridbots.create(req))
    return {"bot": bot.model_dump(), "result": result.model_dump()}


@app.patch("/api/gridbots/{bot_id}")
async def gridbots_update(bot_id: str, request: Request, body: dict = Body(...)) -> dict:
    patch: GridBotPatch = _gridbot_body(GridBotPatch, body)
    out = await _gridbot_run(request.app.state.gridbots.update(bot_id, patch))
    if out is None:
        raise HTTPException(404, "Grid bot not found")
    return {"bot": out[0].model_dump(), "result": out[1].model_dump()}


@app.delete("/api/gridbots/{bot_id}")
async def gridbots_delete(bot_id: str, request: Request) -> dict:
    if not request.app.state.gridbots.delete(bot_id):
        raise HTTPException(404, "Grid bot not found")
    return {"ok": True}


@app.get("/api/gridbots/{bot_id}/result")
async def gridbots_result(bot_id: str, request: Request) -> dict:
    result = await _gridbot_run(request.app.state.gridbots.result(bot_id))
    if result is None:
        raise HTTPException(404, "Grid bot not found")
    return result.model_dump()


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


# ------------------------------------------------------------- paper trading --
#   GET    /api/paper                     the wallet: cash, holdings, PnL, orders with what happened to them
#   POST   /api/paper/orders              {orders: [NewPaperOrder, ...]} placed together → {orders, wallet}
#   DELETE /api/paper/orders/{id}         cancels a pending order → {wallet}
#   POST   /api/paper/reset               {start_cash?, fee_pct?} → empty wallet


@app.get("/api/paper")
async def paper_wallet(request: Request) -> dict:
    return (await request.app.state.paper.wallet()).model_dump()


@app.post("/api/paper/orders")
async def paper_place(request: Request, body: dict = Body(...)) -> dict:
    service: PaperService = request.app.state.paper
    try:
        rows = body.get("orders")
        if not isinstance(rows, list) or not 1 <= len(rows) <= 12:
            raise ValueError("Send 1 to 12 orders as {orders: [...]}")
        made = await service.place([NewPaperOrder.model_validate(r) for r in rows])
    except MarketDataError as exc:
        raise HTTPException(502, str(exc)) from exc
    except ValueError as exc:  # includes pydantic's ValidationError
        raise HTTPException(422, str(exc)) from exc
    return {"orders": [o.model_dump() for o in made], "wallet": (await service.wallet()).model_dump()}


@app.delete("/api/paper/orders/{order_id}")
async def paper_cancel(order_id: str, request: Request) -> dict:
    service: PaperService = request.app.state.paper
    if service.cancel(order_id) is None:
        raise HTTPException(404, "Order not found")
    return {"wallet": (await service.wallet()).model_dump()}


@app.post("/api/paper/reset")
async def paper_reset(request: Request, body: dict = Body(default={})) -> dict:
    service: PaperService = request.app.state.paper
    try:
        service.reset(float(body.get("start_cash", 10_000)), float(body.get("fee_pct", 0.1)))
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"wallet": (await service.wallet()).model_dump()}


# ------------------------------------------------------ trade journal + backtests --
#   GET    /api/journal                   entries, each with its evaluation (status, fills, exits, R, PnL)
#   GET    /api/journal/stats             ?symbol=&setup=&direction= → win rate, R, breakdowns, equity curve
#   POST   /api/journal                   NewJournalEntry → {entry}
#   PATCH  /api/journal/{id}              {notes?, tags?, setup?, cancel?, close?: {price?, time?}} → {entry}
#   DELETE /api/journal/{id}
#   POST   /api/journal/{id}/postmortem   (re)writes a closed trade's post-mortem → {entry}
#   GET    /api/journal/review?days=7     weekly review: stats, best/worst setups and coins, recurring lessons, text
#   GET/PUT /api/journal/review/settings  weekly review schedule {enabled, weekday, time, timezone, days}
#   POST   /api/journal/review/send       sends the review now; 400 without a channel
#   POST   /api/backtest                  BacktestRequest → trades, stats, equity, notes


@app.get("/api/journal")
async def list_journal(request: Request) -> dict:
    service: JournalService = request.app.state.journal
    rows = await service.rows()
    request.app.state.postmortems.schedule_missing(rows)  # closed trades without a review get one in the background
    return {"entries": [entry_json(e, ev) for e, ev in rows]}


@app.get("/api/journal/stats")
async def journal_stats(request: Request, symbol: str | None = Query(None), setup: str | None = Query(None),
                        direction: str | None = Query(None, pattern="^(long|short)$")) -> dict:
    service: JournalService = request.app.state.journal
    return await service.stats(_norm_symbol(symbol) if symbol else None, setup or None, direction)


@app.post("/api/journal")
async def create_journal_entry(req: NewJournalEntry, request: Request) -> dict:
    try:
        entry, ev = await request.app.state.journal.add(req)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    request.app.state.postmortems.schedule_missing([(entry, ev)])
    return {"entry": entry_json(entry, ev)}


@app.patch("/api/journal/{entry_id}")
async def update_journal_entry(entry_id: str, patch: JournalPatch, request: Request) -> dict:
    try:
        res = await request.app.state.journal.update(entry_id, patch)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if res is None:
        raise HTTPException(404, "Trade not found")
    request.app.state.postmortems.schedule_missing([res])
    return {"entry": entry_json(*res)}


@app.post("/api/journal/{entry_id}/postmortem")
async def journal_postmortem(entry_id: str, request: Request) -> dict:
    journal: JournalService = request.app.state.journal
    try:
        entry = await request.app.state.postmortems.generate(entry_id)
    except MarketDataError as exc:
        raise HTTPException(502, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if entry is None:
        raise HTTPException(404, "Trade not found")
    return {"entry": entry_json(entry, await journal.evaluate(entry))}


@app.get("/api/journal/review")
async def journal_review(request: Request, days: int | None = Query(None, ge=1, le=31)) -> dict:
    return await request.app.state.postmortems.weekly(days)


@app.get("/api/journal/review/settings")
async def journal_review_settings(request: Request) -> dict:
    return request.app.state.postmortems.status()


@app.put("/api/journal/review/settings")
async def save_journal_review_settings(settings_in: ReviewSettings, request: Request) -> dict:
    request.app.state.postmortems.update_settings(settings_in)
    return request.app.state.postmortems.status()


@app.post("/api/journal/review/send")
async def send_journal_review(request: Request, days: int | None = Query(None, ge=1, le=31)) -> dict:
    try:
        return await request.app.state.postmortems.send_weekly(days)
    except NoChannelError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/journal/{entry_id}")
async def delete_journal_entry(entry_id: str, request: Request) -> dict:
    if not await request.app.state.journal.remove(entry_id):
        raise HTTPException(404, "Trade not found")
    return {"ok": True}


@app.post("/api/backtest")
async def backtest(req: BacktestRequest, request: Request) -> dict:
    try:
        return (await run_backtest(request.app.state.market, request.app.state.kimi, req)).model_dump()
    except MarketDataError as exc:
        raise HTTPException(502, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


# ------------------------------------------------ Binance account (read-only key) --
#   GET    /api/binance/key                  configured, source (env/file), last 4 chars, permissions, problems
#   PUT    /api/binance/key                  {api_key, api_secret} → checked with Binance, saved only if read-only
#   POST   /api/binance/key/test             ask Binance again what the key may do
#   DELETE /api/binance/key
#   GET    /api/binance/import               settings, last import, fill counts by kind
#   PUT    /api/binance/import/settings      {auto_minutes, symbols, futures, lookback_days}
#   POST   /api/binance/import               {symbols?} → new fills, journal added/updated/removed, notes
#   GET    /api/binance/fills                ?symbol=&kind=manual|bot|unknown&market=spot|futures&limit=
#   GET    /api/binance/trades               ?kind= → round trips rebuilt from the fills
#   POST   /api/binance/classify             {keys, kind (null clears), bot_id?} → re-classify, journal re-synced
#   GET    /api/binance/positions            ?refresh= → own spot holdings + futures positions, bots' separately
#   GET    /api/binance/gridbots/{id}/compare  a tracked grid bot's real fills next to the simulated ones


async def _binance_run(coro, key_status: int = 400):
    try:
        return await coro
    except BinanceKeyError as exc:
        raise HTTPException(key_status, str(exc)) from exc
    except (BinanceApiError, httpx.HTTPError) as exc:
        raise HTTPException(502, f"Binance: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/binance/key")
async def binance_key(request: Request) -> dict:
    return request.app.state.binance.account.status()


@app.put("/api/binance/key")
async def binance_set_key(request: Request, body: dict = Body(...)) -> dict:
    key, secret = body.get("api_key"), body.get("api_secret")
    if not isinstance(key, str) or not isinstance(secret, str):
        raise HTTPException(422, "Give api_key and api_secret")
    return await _binance_run(request.app.state.binance.account.set_key(key, secret), key_status=422)


@app.post("/api/binance/key/test")
async def binance_test_key(request: Request) -> dict:
    return await _binance_run(request.app.state.binance.account.test())


@app.delete("/api/binance/key")
async def binance_remove_key(request: Request) -> dict:
    return await _binance_run(request.app.state.binance.account.remove_key())


@app.get("/api/binance/import")
async def binance_import_status(request: Request) -> dict:
    return request.app.state.binance.status()


@app.put("/api/binance/import/settings")
async def binance_import_settings(new: ImportSettings, request: Request) -> dict:
    return request.app.state.binance.update_settings(new)


@app.post("/api/binance/import")
async def binance_import(request: Request, body: dict | None = Body(None)) -> dict:
    raw = (body or {}).get("symbols") or []
    symbols = [_norm_symbol(s) for s in raw[:40] if isinstance(s, str) and s.strip()]
    return await _binance_run(request.app.state.binance.run(symbols))


@app.get("/api/binance/fills")
async def binance_fills(request: Request, symbol: str | None = Query(None),
                        kind: str | None = Query(None, pattern="^(manual|bot|unknown)$"),
                        market: str | None = Query(None, pattern="^(spot|futures)$"),
                        limit: int = Query(500, ge=1, le=5000)) -> dict:
    rows = request.app.state.binance.fills(_norm_symbol(symbol) if symbol else None, kind, market, limit)
    return {"fills": [f.model_dump() for f in rows]}


@app.get("/api/binance/trades")
async def binance_trades(request: Request, kind: str | None = Query(None, pattern="^(manual|bot|unknown)$")) -> dict:
    return {"trades": [t.model_dump() for t in request.app.state.binance.trades(kind)]}


@app.post("/api/binance/classify")
async def binance_classify(req: ClassifyRequest, request: Request) -> dict:
    return await _binance_run(request.app.state.binance.set_classification(req))


@app.get("/api/binance/positions")
async def binance_positions(request: Request, refresh: bool = Query(False)) -> dict:
    return await _binance_run(request.app.state.binance.positions(refresh))


@app.get("/api/holdings-watch")
async def holdings_watch_status(request: Request) -> dict:
    return request.app.state.holdings_watch.status()


@app.put("/api/holdings-watch")
async def holdings_watch_update(new: WatchSettings, request: Request) -> dict:
    """Turns the holdings watch on or off, or changes its timeframe; syncs the alerts at once."""
    return await request.app.state.holdings_watch.update(new)


@app.post("/api/holdings-watch/run")
async def holdings_watch_run(request: Request) -> dict:
    await request.app.state.holdings_watch.run()
    return request.app.state.holdings_watch.status()


@app.get("/api/binance/gridbots/{bot_id}/compare")
async def binance_gridbot_compare(bot_id: str, request: Request) -> dict:
    out = await _gridbot_run(request.app.state.binance.compare_bot(bot_id))
    if out is None:
        raise HTTPException(404, "Grid bot not found")
    return out


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


# ------------------------------------------------------------ market-wide setup scanner --
#   GET  /api/market-scan?interval=4h            {interval, result (last scan or null), running, schedule, next_run,
#                                                 top, notify_top, channels}
#   POST /api/market-scan/run?interval=4h&top=   scans now (or joins the scan already running) → the same shape


@app.get("/api/market-scan")
async def market_scan_status(request: Request, interval: str = Query("4h")) -> dict:
    return request.app.state.market_scanner.status(_check_interval(interval))


@app.post("/api/market-scan/run")
async def market_scan_run(request: Request, interval: str = Query("4h"),
                          top: int | None = Query(None, ge=5, le=300)) -> dict:
    scanner: MarketScanner = request.app.state.market_scanner
    iv = _check_interval(interval)
    try:
        await scanner.run(iv, top)
    except MarketDataError as exc:
        raise HTTPException(502, str(exc)) from exc
    return scanner.status(iv)
# ------------------------------------------------------ session and period levels, order-book heatmap --
#   GET /api/levels/sessions?symbol=BTCUSDT&interval=1h&or_minutes=30
#       {source, price, sessions: [{key, name, label, which, open, close, live, high, low, high_taken_at,
#        low_taken_at}], periods: [...same, day/week/month], opening_ranges: [{key, label, open, close, until, live,
#        high, low}]}; levels that don't belong on `interval` (sessions on D and above, ...) are left out
#   GET /api/orderbook/heatmap?symbol=BTCUSDT&step=60&since=1700000000
#       {source, note, bin_size, cadence, step, started_at, collecting, price, walls,
#        columns: [[time, mid, first_bin, [notional per bin]]]}; polling it keeps the symbol sampled


@app.get("/api/sessions/clock")
async def sessions_clock() -> dict:
    """Asia, London and New York: open now (and when each closes) or when each next opens, for the header countdown."""
    now = int(time.time())
    return {"now": now, "sessions": session_clock(now)}


@app.get("/api/levels/sessions")
async def session_levels(request: Request, symbol: str = Query("BTCUSDT"), interval: str | None = Query(None),
                         or_minutes: int = Query(30, ge=5, le=240)) -> dict:
    try:
        return await request.app.state.session_levels.get(_norm_symbol(symbol),
                                                          _check_interval(interval) if interval else None, or_minutes)
    except MarketDataError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/api/orderbook/heatmap")
async def orderbook_heatmap(request: Request, symbol: str = Query("BTCUSDT"),
                            step: int = Query(0, ge=0, le=86400, description="Column width in seconds"),
                            since: int | None = Query(None, ge=0, description="Columns from this time on")) -> dict:
    return await request.app.state.heatmap.get(_norm_symbol(symbol), step or None, since)
