"""What changed on a chart since you last looked at it (POST /api/changes): price, the agent's zones, structure,
Kimi's signals, the desk's calls and the alerts that fired, each only if something happened.

Zones are compared by running the same detectors on the candles up to `since` and on the candles now:
  broke   a zone that was there then and a candle has since closed through its far side
  tested  a zone that was there then that price came into without closing through
  new     a zone now that overlaps nothing that was there then
Times are UNIX seconds; `since` is when you last looked (the app remembers it per coin and timeframe).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Optional

import numpy as np
import pandas as pd

from .market_data import INTERVAL_SECONDS, candles_to_df
from .pricefmt import _fmt
from .schemas import AnalysisIntent
from .ta_agent import TF_LABEL, analyze

if TYPE_CHECKING:
    from .agent_desk import AgentDesk
    from .alerts import AlertService
    from .kimi_service import KimiService
    from .market_data import MarketData

log = logging.getLogger(__name__)

INTENT = AnalysisIntent(features=["support_resistance", "supply_demand"], max_zones=3)
CANDLES = 500
MIN_BARS = 60
FLOORS = ("support", "demand")


def _ago(seconds: float) -> str:
    h = seconds / 3600
    return f"{seconds / 60:.0f} minutes" if h < 1 else f"{h:.0f} hour{'s' if round(h) != 1 else ''}" if h < 48 else \
        f"{h / 24:.0f} days"


def zone_changes(df: pd.DataFrame, since: int, interval: str) -> dict:
    """Zones then against now, and the latest structure break if it came after `since`. Pure."""
    df = df.reset_index(drop=True)
    t = df["time"].to_numpy(dtype=np.int64)
    then_df = df[t + INTERVAL_SECONDS[interval] <= since]
    out: dict = {"broke": [], "tested": [], "new": [], "structure": None}
    if len(then_df) < MIN_BARS or len(df) < MIN_BARS:
        return out
    then = analyze(then_df, INTENT, interval).levels
    now_res = analyze(df, INTENT, interval)
    after = df[t >= since]
    closes, highs, lows = (after[k].to_numpy(dtype=float) for k in ("close", "high", "low"))
    tfl = TF_LABEL.get(interval, interval)
    for z in then:
        if z.kind not in ("support", "resistance", "demand", "supply") or not len(after):
            continue
        row = {"kind": z.kind, "low": z.low, "high": z.high, "label": f"{tfl} {z.kind} {_fmt(z.low)}–{_fmt(z.high)}"}
        floor = z.kind in FLOORS
        if (closes < z.low).any() if floor else (closes > z.high).any():
            out["broke"].append({**row, "direction": "down" if floor else "up"})
        elif ((lows <= z.high) & (highs >= z.low)).any():
            out["tested"].append(row)
    for z in now_res.levels:
        if z.kind in ("support", "resistance", "demand", "supply") and not any(
                min(z.high, o.high) > max(z.low, o.low) for o in then):
            out["new"].append({"kind": z.kind, "low": z.low, "high": z.high,
                               "label": f"{tfl} {z.kind} {_fmt(z.low)}–{_fmt(z.high)}"})
    br = now_res.facts.get("last_structure_break")
    if br:
        when = int(df["time"].iloc[len(df) - 1 - br["bars_ago"]])
        if when >= since:
            out["structure"] = {**br, "time": when}
    return out


async def changes(market: "MarketData", symbol: str, interval: str, since: int, kimi: Optional["KimiService"] = None,
                  desk: Optional["AgentDesk"] = None, alerts: Optional["AlertService"] = None,
                  now: Optional[float] = None) -> dict:
    now = time.time() if now is None else now
    candles, source = await market.get_klines(symbol, interval, CANDLES)
    df = candles_to_df(candles)
    out: dict[str, Any] = {"symbol": symbol, "interval": interval, "since": since, "away": _ago(now - since),
                           "data_source": source, "lines": []}
    t = df["time"].to_numpy(dtype=np.int64)
    step = INTERVAL_SECONDS[interval]
    before = df[t + step <= since]
    after = df[t >= since - step]
    if not len(before) or not len(after):
        out["note"] = "Not enough history on this timeframe to compare."
        return out
    then_px, now_px = float(before["close"].iloc[-1]), float(df["close"].iloc[-1])
    out["price"] = {"then": then_px, "now": now_px, "change_pct": round((now_px / then_px - 1) * 100, 2),
                    "high": float(after["high"].max()), "low": float(after["low"].min())}
    lines = [f"Price {out['price']['change_pct']:+.2f}% ({_fmt(then_px)} → {_fmt(now_px)}), range "
             f"{_fmt(out['price']['low'])}–{_fmt(out['price']['high'])}."]
    zc = await asyncio.to_thread(zone_changes, df, since, interval)
    out["zones"] = zc
    for z in zc["broke"][:3]:
        lines.append(f"Broke {'below' if z['direction'] == 'down' else 'above'} the {z['label']}.")
    if zc["tested"]:
        lines.append("Tested " + ", ".join(z["label"] for z in zc["tested"][:3]) + " (held).")
    if zc["new"]:
        lines.append("New: " + ", ".join(z["label"] for z in zc["new"][:3]) + ".")
    if zc["structure"]:
        s = zc["structure"]
        lines.append(f"Structure: {s['direction']} {s['type']} through {_fmt(s['level'])}.")

    if kimi is not None:
        try:
            k = await asyncio.wait_for(kimi.get(symbol, interval), 30)
            sigs = [s for s in k.signals if s.confirm_time >= since]
            out["kimi_signals"] = [s.model_dump() for s in sigs]
            if sigs:
                lines.append("Kimi: " + ", ".join(f"{s.text} at {_fmt(s.entry)}" + (f" ({s.result})" if s.result != "open"
                                                                                      else "") for s in sigs[-4:]) + ".")
        except Exception as exc:  # the rest goes out without it
            log.info("Changes: Kimi for %s %s failed: %s", symbol, interval, exc)
    if desk is not None:
        calls = [c for c in desk.calls(symbol=symbol, limit=50)
                 if c.created_at >= since or (c.closed_at or 0) >= since]
        out["desk_calls"] = [c.model_dump() for c in calls]
        for c in calls[:3]:
            tfl = TF_LABEL.get(c.interval, c.interval)
            if c.created_at >= since and c.status in ("waiting", "open"):
                lines.append(f"The desk called a {tfl} buy at {_fmt(c.zone_low)}–{_fmt(c.zone_high)} "
                             f"({c.confidence * 100:.0f}%), {c.status.replace('waiting', 'waiting to fill')}.")
            elif c.status in ("tp", "invalidated", "timed_out"):
                lines.append(f"The desk's {tfl} call {c.status.replace('_', ' ').replace('tp', 'hit its take-profit')} "
                             f"({(c.r or 0):+.2f}R).")
    if alerts is not None:
        fired = [h for h in alerts.history.list(300) if h.get("symbol") == symbol and h["time"] / 1000 >= since
                 and h.get("kind") != "desk"]
        out["alerts"] = fired[:10]
        if fired:
            lines.append(f"{len(fired)} alert{'s' if len(fired) != 1 else ''} fired: "
                         + "; ".join(h["title"] for h in fired[:3]) + ".")
    out["lines"] = lines
    out["quiet"] = len(lines) == 1 and abs(out["price"]["change_pct"]) < 1
    return out
