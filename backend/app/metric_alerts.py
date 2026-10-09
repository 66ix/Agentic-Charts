"""Alerts on the market header bar: "tell me when Fear & Greed drops below 25", "when BTC dominance moves 1 point",
"when total market cap is above $3T".

An alert watches one header metric (market_metrics.py) with one condition:

  above / below   the metric's live value is above / below `value`
  moves           it has moved `value` away from where it was when the alert was set, either way: in points for
                  Fear & Greed and BTC dominance (57.3% → 58.3% is 1 point), in percent for the dollar metrics

Every CHECK_SECONDS the cached header values are read (the service refreshes them at most once per its TTL) and
each armed alert whose condition holds fires once and disarms; one set while its condition already holds fires on
the next check. Only live values count: a metric that falls back to its mocked value never fires anything.

A fire goes to the alert history (kind "signal", symbol "MARKET"), `/ws/alerts` listeners and Telegram / Discord
through AlertService, like the other alerts.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from typing import Optional

from pydantic import BaseModel, Field

from .alerts import AlertService, read_store, store_path, write_store
from .jobs import jobs
from .market_metrics import MarketMetricsService, fmt_usd
from .schemas import MetricAlertSpec

log = logging.getLogger(__name__)

CHECK_SECONDS = 60.0
NAMES: dict[str, str] = {"fear_greed": "Fear & Greed", "btc_dominance": "BTC dominance", "market_cap": "Market cap",
                         "volume_24h": "24h volume", "open_interest": "Open interest",
                         "liquidations": "Liquidations"}
POINTS = ("fear_greed", "btc_dominance")  # "moves" is in points for these, in percent for the dollar metrics


def fmt_value(metric: str, v: float) -> str:
    if metric == "fear_greed":
        return f"{v:.0f}"
    if metric == "btc_dominance":
        return f"{v:.2f}%"
    return fmt_usd(v)


class MetricAlert(MetricAlertSpec):
    id: str
    created_at: int = Field(..., description="UNIX milliseconds")
    armed: bool = True
    base_value: Optional[float] = Field(None, description="The metric when a moves alert was set")
    triggered_at: Optional[int] = None
    triggered_value: Optional[float] = None


class CreateMetricAlertsRequest(BaseModel):
    alerts: list[MetricAlertSpec] = Field(..., min_length=1, max_length=10)


def describe(a: MetricAlertSpec) -> str:
    """'Fear & Greed below 25', 'BTC dominance moves 1 point', 'Market cap moves 5%'."""
    name = NAMES[a.metric]
    if a.condition == "moves":
        unit = (" point" + ("s" if a.value != 1 else "")) if a.metric in POINTS else "%"
        return f"{name} moves {a.value:g}{unit}"
    return f"{name} {a.condition} {fmt_value(a.metric, a.value)}"


def holds(a: MetricAlert, value: float) -> bool:
    if a.condition == "above":
        return value > a.value
    if a.condition == "below":
        return value < a.value
    if a.base_value is None or a.base_value <= 0:
        return False
    moved = value - a.base_value if a.metric in POINTS else (value / a.base_value - 1) * 100
    return abs(moved) >= a.value


class MetricAlertService:
    def __init__(self, metrics: MarketMetricsService, alerts: AlertService, store: str = "memory") -> None:
        self.metrics = metrics
        self.alerts = alerts
        self._path = store_path(store)
        data = read_store(self._path) or {}
        self._items: dict[str, MetricAlert] = {}
        for raw in data.get("alerts", []):
            with contextlib.suppress(Exception):
                a = MetricAlert.model_validate(raw)
                self._items[a.id] = a
        self._task: asyncio.Task | None = None
        alerts.add_snapshot_provider(self.snapshot)

    def start(self) -> None:
        if self._task is None:
            jobs.declare("metric_alerts", "Market alerts", CHECK_SECONDS)
            self._task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def list(self) -> list[MetricAlert]:
        return sorted(self._items.values(), key=lambda a: -a.created_at)

    def snapshot(self) -> dict:
        return {"type": "metric_snapshot", "alerts": [a.model_dump() for a in self.list()]}

    async def _live(self) -> dict[str, float]:
        res = await self.metrics.get()
        return {m.key: m.value for m in res.metrics if m.source == "live"}

    async def add(self, specs: list[MetricAlertSpec]) -> list[MetricAlert]:
        live: dict[str, float] = {}
        if any(s.condition == "moves" for s in specs):
            with contextlib.suppress(Exception):
                live = await self._live()
        out = []
        for s in specs:
            if s.condition == "moves" and s.metric not in live:
                raise ValueError(f"{NAMES[s.metric]} has no live value right now, so a move can't be measured "
                                 "from it; try again later or set an above/below level")
            a = MetricAlert(**s.model_dump(), id=uuid.uuid4().hex[:12], created_at=int(time.time() * 1000),
                            base_value=live.get(s.metric) if s.condition == "moves" else None)
            self._items[a.id] = a
            out.append(a)
        self._changed()
        return out

    def remove(self, alert_id: str) -> bool:
        if self._items.pop(alert_id, None) is None:
            return False
        self._changed()
        return True

    async def check(self, live: Optional[dict[str, float]] = None) -> list[MetricAlert]:
        """Fire every armed alert whose condition holds on the live values → the alerts that fired."""
        armed = [a for a in self._items.values() if a.armed]
        if not armed:
            return []
        if live is None:
            try:
                live = await self._live()
            except Exception as exc:  # next check
                log.info("Market alerts: no metrics (%s)", exc)
                return []
        fired = []
        for a in armed:
            v = live.get(a.metric)
            if v is None or not holds(a, v):
                continue
            a.armed, a.triggered_at, a.triggered_value = False, int(time.time() * 1000), v
            title = f"Market alert: {describe(a)}"
            text = f"{title}. {NAMES[a.metric]} is now {fmt_value(a.metric, v)}" + (
                f" (was {fmt_value(a.metric, a.base_value)} when set)" if a.base_value is not None else "") + "." + (
                f" Note: {a.note}" if a.note else "")
            self.alerts.record("signal", "MARKET", title, text, alert_id=a.id)
            self.alerts.notify(text)
            fired.append(a)
        if fired:
            self._changed()
        return fired

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(CHECK_SECONDS)
            try:
                await self.check()
                jobs.ok("metric_alerts")
            except Exception as exc:
                jobs.fail("metric_alerts", exc)
                log.exception("Market alert check failed")

    def _changed(self) -> None:
        write_store(self._path, {"alerts": [a.model_dump() for a in self._items.values()]})
        self.alerts.broadcast(self.snapshot())
