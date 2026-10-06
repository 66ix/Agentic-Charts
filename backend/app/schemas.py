"""Pydantic models shared by the REST API, the WebSocket stream and the TA agent.

The overlay models are the contract with the frontend: every primitive the agent
can draw is described here and mirrored in `frontend/lib/types.ts`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator

# Timeframes the UI exposes. Binance has no native 3h interval, so it is
# resampled from 1h (see market_data.py).
Interval = Literal["1m", "5m", "15m", "30m", "1h", "3h", "4h", "1d", "1w", "1M"]
INTERVALS: tuple[str, ...] = ("1m", "5m", "15m", "30m", "1h", "3h", "4h", "1d", "1w", "1M")

LineStyle = Literal["solid", "dashed", "dotted"]


class Candle(BaseModel):
    time: int = Field(..., description="Bar open time, UNIX seconds (UTC)")
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


# ---------------------------------------------------------------- overlays --


class _OverlayBase(BaseModel):
    id: Optional[str] = None
    label: str = ""
    color: str = "#94a3b8"
    kind: Optional[str] = Field(None, description="Semantic tag, e.g. resistance, demand, window_high")
    strength: Optional[float] = Field(None, ge=0, le=1, description="0..1 confidence score")


class HorizontalLineOverlay(_OverlayBase):
    type: Literal["horizontal_line"] = "horizontal_line"
    price: float
    line_style: LineStyle = "solid"
    line_width: int = Field(1, ge=1, le=4)
    time_start: Optional[int] = Field(None, description="Draw as a ray starting at this time; null = full width")


class BoxOverlay(_OverlayBase):
    type: Literal["box"] = "box"
    price_high: float
    price_low: float
    border_color: Optional[str] = None
    time_start: Optional[int] = Field(None, description="Left edge; null = left edge of the chart")
    time_end: Optional[int] = Field(None, description="Right edge; null = extend to the right edge")

    @model_validator(mode="after")
    def _order_prices(self) -> "BoxOverlay":
        if self.price_low > self.price_high:
            self.price_low, self.price_high = self.price_high, self.price_low
        return self


class MarkerOverlay(_OverlayBase):
    type: Literal["marker"] = "marker"
    time: int
    price: float
    position: Literal["above", "below"] = "above"
    shape: Literal["arrowUp", "arrowDown", "circle", "square"] = "circle"


class TrendlineOverlay(_OverlayBase):
    type: Literal["trendline"] = "trendline"
    time1: int
    price1: float
    time2: int
    price2: float
    extend_right: bool = False
    line_style: LineStyle = "solid"


Overlay = Annotated[
    Union[HorizontalLineOverlay, BoxOverlay, MarkerOverlay, TrendlineOverlay],
    Field(discriminator="type"),
]


# ------------------------------------------------------------- agent I/O --

Feature = Literal["support_resistance", "supply_demand", "swings", "window_levels", "trendlines"]
ALL_FEATURES: tuple[str, ...] = ("support_resistance", "supply_demand", "swings", "window_levels", "trendlines")


class AnalysisIntent(BaseModel):
    """What the user asked for, normalised. Produced by the LLM or the rule parser."""

    features: list[Feature] = Field(default_factory=lambda: ["support_resistance", "window_levels"])
    timeframe: Optional[Interval] = Field(None, description="Timeframe to analyse; null = the chart's timeframe")
    window_timeframes: list[Interval] = Field(default_factory=lambda: ["4h", "1d"])
    max_zones: int = Field(2, ge=1, le=6, description="Max zones per side (above/below price)")
    answer_hint: str = Field("", description="Short restatement of the question")

    @field_validator("features")
    @classmethod
    def _dedupe(cls, v: list[str]) -> list[str]:
        seen: list[str] = []
        for f in v:
            if f not in seen:
                seen.append(f)
        return seen or ["support_resistance"]


class AnalyzeRequest(BaseModel):
    symbol: str = Field("INJUSDT", min_length=2, max_length=20)
    interval: Interval = "4h"
    prompt: str = Field("", max_length=2000)
    limit: int = Field(500, ge=100, le=1000)
    candles: Optional[list[Candle]] = Field(
        None, description="Optional OHLCV the client already has; skips the server-side fetch"
    )

    @field_validator("symbol")
    @classmethod
    def _norm_symbol(cls, v: str) -> str:
        return v.replace("/", "").replace("-", "").upper()


class AnalysisStats(BaseModel):
    last_price: float
    atr: float
    trend: Literal["up", "down", "range"]
    ema_fast: float
    ema_slow: float
    swing_highs: int
    swing_lows: int


class AnalyzeResponse(BaseModel):
    symbol: str
    interval: str
    analysis_interval: str
    overlays: list[Overlay]
    summary: str
    intent: AnalysisIntent
    stats: AnalysisStats
    engine: dict[str, str]
    data_source: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------- metrics --


class Metric(BaseModel):
    key: str
    label: str
    value: float
    display: str
    change_pct: Optional[float] = None
    source: Literal["live", "mock"] = "mock"


class MarketMetrics(BaseModel):
    metrics: list[Metric]
    updated_at: datetime
