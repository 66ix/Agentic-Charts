"""Global market header metrics.

Every metric comes from a free, keyless API:
  * CoinGecko /global    → market cap (+24h %), 24h volume, BTC dominance
  * alternative.me /fng  → Fear & Greed index (+ change vs yesterday)
  * Binance USD-M futures → open interest and 24h liquidations (see derivatives.py)
When a source is unreachable the metric falls back to a mocked value (with
gentle jitter so the header feels alive). Each metric is tagged
`source: live | mock` and the UI marks mocked values.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timezone

import httpx

from .config import get_settings
from .derivatives import DerivativesService, Liquidations, OpenInterest, since_label
from .schemas import MarketMetrics, Metric

log = logging.getLogger(__name__)

COINGECKO_GLOBAL = "https://api.coingecko.com/api/v3/global"
FEAR_GREED = "https://api.alternative.me/fng/?limit=2"

# Baseline values used when a live source is unavailable.
MOCK_BASE = {
    "market_cap": (2.93e12, 0.85),
    "volume_24h": (66.74e9, 59.89),
    "liquidations": (165e6, 211.70),
    "open_interest": (367.42e9, 2.84),
    "fear_greed": (68, None),
    "btc_dominance": (57.3, 0.12),
}


def fmt_usd(v: float) -> str:
    for div, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"${v / div:.2f}{suffix}"
    return f"${v:,.2f}"


def fng_label(v: float) -> str:
    if v < 25:
        return "Extreme Fear"
    if v < 45:
        return "Fear"
    if v <= 55:
        return "Neutral"
    if v <= 75:
        return "Greed"
    return "Extreme Greed"


class MarketMetricsService:
    def __init__(self, derivatives: DerivativesService | None = None) -> None:
        self.derivatives = derivatives
        self.ttl = get_settings().metrics_cache_seconds
        self._cache: tuple[float, MarketMetrics] | None = None
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(6.0, connect=4.0),
                                         headers={"Accept": "application/json", "User-Agent": "agentic-charts/1.0"})
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        await self._client.aclose()

    async def get(self) -> MarketMetrics:
        async with self._lock:
            if self._cache and self._cache[0] > time.monotonic():
                return self._cache[1]
            gecko, fng, oi = await asyncio.gather(self._coingecko(), self._fear_greed(), self._open_interest(),
                                                   return_exceptions=True)
            for name, res in (("CoinGecko", gecko), ("Fear & Greed", fng), ("Open interest", oi)):
                if isinstance(res, BaseException):
                    log.warning("%s unavailable: %s", name, res)
            liq = self.derivatives.liquidation_snapshot() if self.derivatives else None
            metrics = self._build(gecko if isinstance(gecko, dict) else None,
                                  fng if isinstance(fng, list) else None,
                                  oi if isinstance(oi, OpenInterest) else None, liq)
            self._cache = (time.monotonic() + self.ttl, metrics)
            return metrics

    async def _coingecko(self) -> dict:
        r = await self._client.get(COINGECKO_GLOBAL)
        r.raise_for_status()
        return r.json()["data"]

    async def _open_interest(self) -> OpenInterest | None:
        return await self.derivatives.open_interest() if self.derivatives else None

    async def _fear_greed(self) -> list:
        r = await self._client.get(FEAR_GREED)
        r.raise_for_status()
        return r.json()["data"]

    @staticmethod
    def _mock(key: str, label: str, display_fn, jitter: float = 0.004, note: str | None = None) -> Metric:
        base, chg = MOCK_BASE[key]
        v = base * (1 + random.uniform(-jitter, jitter))
        c = None if chg is None else round(chg + random.uniform(-0.05, 0.05) * abs(chg), 2)
        return Metric(key=key, label=label, value=v, display=display_fn(v), change_pct=c, source="mock",
                      note=note or "Mocked: live source unreachable")

    def _build(self, gecko: dict | None, fng: list | None, oi: OpenInterest | None = None,
               liq: Liquidations | None = None) -> MarketMetrics:
        out: list[Metric] = []
        if gecko:
            mc = float(gecko["total_market_cap"]["usd"])
            out.append(Metric(key="market_cap", label="Market Cap", value=mc, display=fmt_usd(mc),
                              change_pct=round(float(gecko.get("market_cap_change_percentage_24h_usd", 0)), 2),
                              source="live"))
            vol = float(gecko["total_volume"]["usd"])
            out.append(Metric(key="volume_24h", label="24h Vol", value=vol, display=fmt_usd(vol), source="live"))
        else:
            out.append(self._mock("market_cap", "Market Cap", fmt_usd))
            out.append(self._mock("volume_24h", "24h Vol", fmt_usd))

        if liq:
            out.append(Metric(
                key="liquidations", label="Liquidations", value=liq.usd, display=fmt_usd(liq.usd), source="live",
                note=(f"Binance futures since {since_label(liq.since)}. Longs {fmt_usd(liq.longs_usd)}, "
                      f"shorts {fmt_usd(liq.shorts_usd)}. Binance sends at most one liquidation per symbol per "
                      "second, so this is a lower bound."),
            ))
        else:
            out.append(self._mock("liquidations", "Liquidations", fmt_usd, jitter=0.03,
                                  note="Mocked: Binance futures liquidation stream unreachable"))
        if oi:
            out.append(Metric(key="open_interest", label="Open Interest", value=oi.usd, display=fmt_usd(oi.usd),
                              change_pct=oi.change_pct, source="live",
                              note=f"Binance futures, top {oi.symbols} USDT perpetuals; change vs 24h ago"))
        else:
            out.append(self._mock("open_interest", "Open Interest", fmt_usd,
                                  note="Mocked: Binance futures API unreachable"))

        if fng:
            now_v = float(fng[0]["value"])
            prev_v = float(fng[1]["value"]) if len(fng) > 1 else None
            out.append(Metric(key="fear_greed", label="Fear & Greed", value=now_v,
                              display=f"{now_v:.0f}/100 · {fng[0].get('value_classification') or fng_label(now_v)}",
                              change_pct=None if not prev_v else round((now_v - prev_v) / prev_v * 100, 2),
                              source="live"))
        else:
            m = self._mock("fear_greed", "Fear & Greed", lambda v: "", jitter=0.0)
            m.display = f"{m.value:.0f}/100 · {fng_label(m.value)}"
            out.append(m)

        if gecko:
            dom = float(gecko["market_cap_percentage"]["btc"])
            out.append(Metric(key="btc_dominance", label="BTC Dominance", value=dom, display=f"{dom:.2f}%",
                              source="live"))
        else:
            out.append(self._mock("btc_dominance", "BTC Dominance", lambda v: f"{v:.2f}%", jitter=0.001))
        return MarketMetrics(metrics=out, updated_at=datetime.now(timezone.utc))
