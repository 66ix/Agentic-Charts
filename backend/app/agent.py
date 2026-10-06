"""Agent orchestration: prompt → intent → data → TA detectors → narrated answer."""

from __future__ import annotations

import asyncio

from .llm import LLMClient
from .market_data import MarketData, candles_to_df
from .schemas import AnalyzeRequest, AnalyzeResponse
from .ta_agent import analyze, describe


async def run_analysis(req: AnalyzeRequest, market: MarketData, llm: LLMClient) -> AnalyzeResponse:
    intent, intent_engine = await llm.parse_intent(req.prompt)
    tf = intent.timeframe or req.interval

    if req.candles and tf == req.interval and len(req.candles) >= 30:
        candles, source = req.candles, "client"
    else:
        candles, source = await market.get_klines(req.symbol, tf, req.limit)

    higher: dict = {}
    if "window_levels" in intent.features:
        results = await asyncio.gather(
            *(market.get_klines(req.symbol, wtf, 5) for wtf in intent.window_timeframes), return_exceptions=True
        )
        for wtf, res in zip(intent.window_timeframes, results):
            if not isinstance(res, BaseException):
                higher[wtf] = candles_to_df(res[0])

    df = candles_to_df(candles)
    # Detection is CPU-bound (SciPy); keep the event loop free for streams.
    result = await asyncio.to_thread(analyze, df, intent, tf, higher)

    fallback = describe(result.facts, req.symbol)
    summary, narrate_engine = await llm.narrate(req.prompt, result.facts, fallback)

    return AnalyzeResponse(
        symbol=req.symbol,
        interval=req.interval,
        analysis_interval=tf,
        overlays=result.overlays,
        summary=summary,
        intent=intent,
        stats=result.stats,
        engine={"intent": intent_engine, "summary": narrate_engine, "detector": "scipy"},
        data_source=source,
    )
