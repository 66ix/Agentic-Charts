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

Feature = Literal["support_resistance", "supply_demand", "swings", "window_levels", "trendlines",
                  "liquidity_sweeps", "fvg", "order_blocks", "patterns", "volume_profile"]
ALL_FEATURES: tuple[str, ...] = ("support_resistance", "supply_demand", "swings", "window_levels", "trendlines",
                                 "liquidity_sweeps", "fvg", "order_blocks", "patterns", "volume_profile")
# "Full analysis": the structural picture without cluttering the chart with every detector.
FULL_FEATURES: tuple[str, ...] = ("support_resistance", "supply_demand", "swings", "window_levels", "trendlines",
                                  "liquidity_sweeps")

# Overlay groups the user can refer to ("remove the trendline", "alert me on the supply zone").
Target = Literal["all", "support", "resistance", "supply", "demand", "window", "swings", "trendlines", "custom",
                 "sweeps", "fvg", "order_blocks", "patterns", "volume_profile", "plan", "new"]
TARGETS: tuple[str, ...] = ("all", "support", "resistance", "supply", "demand", "window", "swings", "trendlines",
                            "custom", "sweeps", "fvg", "order_blocks", "patterns", "volume_profile", "plan", "new")
TARGET_KINDS: dict[str, frozenset[str]] = {
    "support": frozenset({"support"}),
    "resistance": frozenset({"resistance"}),
    "supply": frozenset({"supply"}),
    "demand": frozenset({"demand"}),
    "window": frozenset({"window_high", "window_low"}),
    "swings": frozenset({"swing_high", "swing_low"}),
    "trendlines": frozenset({"trendline"}),
    "custom": frozenset({"custom_level", "custom_zone"}),
    "sweeps": frozenset({"sweep"}),
    "fvg": frozenset({"fvg_bullish", "fvg_bearish"}),
    "order_blocks": frozenset({"ob_bullish", "ob_bearish"}),
    "patterns": frozenset({"pattern_range", "pattern_line", "pattern_neckline", "pattern_point"}),
    "volume_profile": frozenset({"poc", "vah", "val"}),
    "plan": frozenset({"plan_entry", "plan_stop", "plan_target", "plan_risk", "plan_reward"}),
}
# Overlay kinds each detector produces; a re-run replaces the old ones.
FEATURE_KINDS: dict[str, frozenset[str]] = {
    "support_resistance": TARGET_KINDS["support"] | TARGET_KINDS["resistance"],
    "supply_demand": TARGET_KINDS["supply"] | TARGET_KINDS["demand"],
    "swings": TARGET_KINDS["swings"],
    "window_levels": TARGET_KINDS["window"],
    "trendlines": TARGET_KINDS["trendlines"],
    "liquidity_sweeps": TARGET_KINDS["sweeps"],
    "fvg": TARGET_KINDS["fvg"],
    "order_blocks": TARGET_KINDS["order_blocks"],
    "patterns": TARGET_KINDS["patterns"],
    "volume_profile": TARGET_KINDS["volume_profile"],
}

# Chart indicators the agent can switch on or off (mirrors IndicatorState in frontend/lib/types.ts).
IndicatorName = Literal["rsi", "macd", "vwap", "ema20", "ema50", "psar", "volume"]
INDICATORS: tuple[str, ...] = ("rsi", "macd", "vwap", "ema20", "ema50", "psar", "volume")

# What a watchlist scan looks for.
ScanFilter = Literal["any", "near_support", "near_resistance", "bullish", "bearish", "oversold", "overbought",
                     "breakout"]
SCAN_FILTERS: tuple[str, ...] = ("any", "near_support", "near_resistance", "bullish", "bearish", "oversold",
                                 "overbought", "breakout")


def norm_symbol(v: str) -> str:
    """'eth', 'ETH/USDT', 'eth-usdt' → 'ETHUSDT'."""
    s = v.replace("/", "").replace("-", "").replace("$", "").strip().upper()
    return s if s.endswith(("USDT", "USDC", "FDUSD", "BTC", "ETH")) and len(s) > 4 else f"{s}USDT"


class CustomLevel(BaseModel):
    """A level the user stated explicitly, e.g. "draw a line at 25.40" or "box 24 to 25"."""

    kind: Literal["line", "zone"] = "line"
    price: Optional[float] = Field(None, gt=0)
    price_low: Optional[float] = Field(None, gt=0)
    price_high: Optional[float] = Field(None, gt=0)
    label: str = ""

    @model_validator(mode="after")
    def _complete(self) -> "CustomLevel":
        if self.kind == "line" and self.price is None:
            if self.price_low is None and self.price_high is None:
                raise ValueError("a line needs a price")
            self.price = self.price_low or self.price_high
        if self.kind == "zone":
            if self.price_low is None or self.price_high is None:
                raise ValueError("a zone needs price_low and price_high")
            if self.price_low > self.price_high:
                self.price_low, self.price_high = self.price_high, self.price_low
        return self


class AnalysisIntent(BaseModel):
    """What the user asked for, normalised. Produced by the LLM or the rule parser."""

    features: list[Feature] = Field(default_factory=lambda: ["support_resistance", "window_levels"])
    timeframe: Optional[Interval] = Field(None, description="Timeframe to analyse; null = the chart's timeframe")
    window_timeframes: list[Interval] = Field(default_factory=lambda: ["4h", "1d"])
    max_zones: int = Field(2, ge=1, le=6, description="Max zones per side (above/below price)")
    answer_hint: str = Field("", description="Short restatement of the question")
    custom_levels: list[CustomLevel] = Field(default_factory=list, max_length=10)
    remove: list[Target] = Field(default_factory=list, description="Overlay groups to take off the chart")
    keep_existing: bool = Field(False, description="Add to the overlays already drawn instead of replacing them")
    alert_prices: list[float] = Field(default_factory=list, max_length=10)
    alert_targets: list[Target] = Field(default_factory=list, description="Overlay groups to set alerts on")
    symbol: Optional[str] = Field(None, description="Coin to look at; null = the chart's symbol")
    switch_chart: bool = Field(False, description="Move the chart to the analysed symbol and timeframe")
    scan_watchlist: bool = Field(False, description="Scan every watchlist symbol instead of one chart")
    scan_filter: ScanFilter = "any"
    trade_plan: Optional[Literal["long", "short", "auto"]] = Field(None, description="Build a trade plan")
    indicators_on: list[IndicatorName] = Field(default_factory=list)
    indicators_off: list[IndicatorName] = Field(default_factory=list)

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, v: Optional[str]) -> Optional[str]:
        if not v or not v.strip():
            return None
        s = norm_symbol(v)
        return s if s.isalnum() and 5 <= len(s) <= 20 else None

    @field_validator("features", "remove", "alert_targets", "indicators_on", "indicators_off")
    @classmethod
    def _dedupe(cls, v: list[str]) -> list[str]:
        seen: list[str] = []
        for f in v:
            if f not in seen:
                seen.append(f)
        return seen

    @field_validator("alert_prices")
    @classmethod
    def _positive(cls, v: list[float]) -> list[float]:
        return [p for p in v if p > 0]

    @property
    def has_actions(self) -> bool:
        return bool(self.custom_levels or self.remove or self.alert_prices or self.alert_targets or self.symbol
                    or self.switch_chart or self.scan_watchlist or self.trade_plan or self.indicators_on
                    or self.indicators_off)

    @model_validator(mode="after")
    def _default_features(self) -> "AnalysisIntent":
        # A request with nothing to draw, remove or alert on is a plain "analyse this".
        if not self.features and not self.has_actions:
            self.features = ["support_resistance"]
        return self


class ChatTurn(BaseModel):
    role: Literal["user", "agent"]
    text: str = Field("", max_length=2000)


class AlertSpec(BaseModel):
    """A price alert the client should arm. `cross` fires when price crosses `price`;
    `zone` fires when price moves into [price_low, price_high]."""

    kind: Literal["cross", "zone"]
    price: Optional[float] = None
    price_low: Optional[float] = None
    price_high: Optional[float] = None
    label: str = ""


class Navigate(BaseModel):
    """Where the chart should go after this answer."""

    symbol: str
    interval: Interval


class PlanTarget(BaseModel):
    price: float
    label: str
    rr: float = Field(..., description="Reward-to-risk at this target")


class TradePlan(BaseModel):
    direction: Literal["long", "short"]
    entry: float
    stop: float
    targets: list[PlanTarget]
    basis: str = Field("", description="What the entry is built on, e.g. 'H4 demand 23.9–24.2'")
    risk_pct: float = Field(..., description="Entry-to-stop distance as % of entry")
    notes: list[str] = Field(default_factory=list)


class ScanResult(BaseModel):
    symbol: str
    interval: Interval
    last_price: float
    change_pct: Optional[float] = None
    trend: Literal["up", "down", "range"]
    rsi: Optional[float] = None
    nearest_kind: Optional[str] = None
    nearest_low: Optional[float] = None
    nearest_high: Optional[float] = None
    distance_pct: Optional[float] = Field(None, description="Signed distance to the nearest zone, % of price; 0 inside")
    signals: list[str] = Field(default_factory=list)
    score: float = 0.0
    data_source: str = "binance"


class AnalyzeRequest(BaseModel):
    symbol: str = Field("INJUSDT", min_length=2, max_length=20)
    interval: Interval = "4h"
    prompt: str = Field("", max_length=2000)
    limit: int = Field(500, ge=100, le=1000)
    candles: Optional[list[Candle]] = Field(
        None, description="Optional OHLCV the client already has; skips the server-side fetch"
    )
    history: list[ChatTurn] = Field(default_factory=list, description="Earlier turns, oldest first")
    overlays: list[Overlay] = Field(default_factory=list, description="AI overlays currently on the chart")
    previous_intent: Optional[AnalysisIntent] = None
    watchlist: list[str] = Field(default_factory=list, max_length=40, description="The user's watchlist symbols")

    @field_validator("symbol")
    @classmethod
    def _norm_symbol(cls, v: str) -> str:
        return v.replace("/", "").replace("-", "").upper()

    @field_validator("watchlist")
    @classmethod
    def _norm_watchlist(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for sym in v:
            s = norm_symbol(sym)
            if s.isalnum() and 5 <= len(s) <= 20 and s not in out:
                out.append(s)
        return out

    @field_validator("history")
    @classmethod
    def _recent(cls, v: list[ChatTurn]) -> list[ChatTurn]:
        return v[-10:]

    @field_validator("overlays")
    @classmethod
    def _cap(cls, v: list) -> list:
        return v[-100:]


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
    alerts: list[AlertSpec] = Field(default_factory=list)
    navigate: Optional[Navigate] = Field(None, description="Chart symbol/timeframe to switch to before drawing")
    indicators: dict[str, bool] = Field(default_factory=dict, description="Indicator toggles to apply")
    scan: list[ScanResult] = Field(default_factory=list)
    plan: Optional[TradePlan] = None
    steps: list[str] = Field(default_factory=list, description="What the agent looked at, in order")
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------- metrics --


class Metric(BaseModel):
    key: str
    label: str
    value: float
    display: str
    change_pct: Optional[float] = None
    source: Literal["live", "mock"] = "mock"
    note: Optional[str] = Field(None, description="Tooltip: coverage or why the value is mocked")


class MarketMetrics(BaseModel):
    metrics: list[Metric]
    updated_at: datetime


# ----------------------------------------------------------------- alerts --


class PriceAlert(AlertSpec):
    """An alert stored and evaluated by the backend (alerts.py). Mirrors PriceAlert in `frontend/lib/types.ts`."""

    id: str
    symbol: str
    armed: bool = True
    created_at: int = Field(..., description="UNIX milliseconds")
    triggered_at: Optional[int] = Field(None, description="UNIX milliseconds")
    triggered_price: Optional[float] = None
    last_side: Optional[Literal["above", "below", "inside"]] = Field(
        None, description="Where price was last seen relative to the level, so alerts fire on the transition")


class CreateAlertsRequest(BaseModel):
    symbol: str = Field(..., min_length=2, max_length=20)
    alerts: list[AlertSpec] = Field(..., min_length=1, max_length=50)

    @field_validator("symbol")
    @classmethod
    def _norm_symbol(cls, v: str) -> str:
        return v.replace("/", "").replace("-", "").upper()
