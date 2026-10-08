"""Chart screenshots: a vision model reads a pasted chart image (the coin, the timeframe, the support and resistance
lines, boxes, trendlines and patterns drawn on it, with prices read off the price axis) and they are drawn on the
app's own chart. When the coin is found, the app's detectors run on the real candles too, so you can see which of
the drawn levels the app agrees with.

Times are only known when the model can read the dates at both ends of the time axis; then a drawing's left/right
position (0 = left edge of the plot, 1 = right edge) becomes a time. Without them boxes span the chart and
trendlines are left out.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .pricefmt import _fmt
from .schemas import INTERVALS, BoxOverlay, HorizontalLineOverlay, TrendlineOverlay
from .ta_agent import rgba

MAX_IMAGE_B64 = 7_000_000      # ~5 MB image
MEDIA_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")
MATCH_PCT = 0.6                # a read level within this % of one the detectors found counts as the same

SUPPORT = "#22c55e"
RESISTANCE = "#ef4444"
LEVEL = "#eab308"
DEMAND = "#14b8a6"
SUPPLY = "#f97316"
PATTERN = "#a78bfa"

SYSTEM = """You read trading chart screenshots. Report only what is visible in the image.
- symbol: the pair or coin in the chart title or legend, as Binance spot writes it (e.g. BTCUSDT, INJUSDT), or null.
- interval: the chart timeframe as one of 1m 3m 5m 15m 30m 1h 2h 4h 6h 8h 12h 1d 3d 1w 1M, or null.
- time_left / time_right: the dates (ISO 8601, UTC, e.g. 2026-09-14T00:00) at the left and right edges of the
  candle area, read from the time axis; null when you cannot tell.
- levels: horizontal lines drawn on the chart (support, resistance, or other), with the price read off the price
  axis. Read prices carefully: use the axis labels and interpolate between them.
- zones: rectangles / boxes (demand, supply or other zones) with their top and bottom prices, and x_start / x_end
  as fractions of the candle area's width (0 = left edge, 1 = right edge); x_end null when the box runs to the
  right edge.
- lines: trendlines and pattern legs, each from (x1, price1) to (x2, price2) with x as fractions of the width.
- patterns: chart patterns that are drawn or clearly visible (e.g. ascending triangle, head and shoulders, bull
  flag, double bottom), with direction, the key price (breakout level or neckline), the target if shown, and x of
  where it is.
- notes: one short sentence on anything that matters for reading the chart (e.g. "log scale", "levels unlabeled").
Leave lists empty when there is nothing of that kind. Never invent drawings that are not in the image."""

_NUM = {"type": "number"}
_NUM_NULL = {"type": ["number", "null"]}
_STR_NULL = {"type": ["string", "null"]}


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


SCHEMA = _obj({
    "symbol": _STR_NULL,
    "interval": _STR_NULL,
    "time_left": _STR_NULL,
    "time_right": _STR_NULL,
    "levels": {"type": "array", "items": _obj({
        "price": _NUM, "kind": {"type": "string", "enum": ["support", "resistance", "level"]},
        "label": {"type": "string"}})},
    "zones": {"type": "array", "items": _obj({
        "price_high": _NUM, "price_low": _NUM, "kind": {"type": "string", "enum": ["demand", "supply", "zone"]},
        "label": {"type": "string"}, "x_start": _NUM_NULL, "x_end": _NUM_NULL})},
    "lines": {"type": "array", "items": _obj({
        "x1": _NUM, "price1": _NUM, "x2": _NUM, "price2": _NUM, "label": {"type": "string"}})},
    "patterns": {"type": "array", "items": _obj({
        "name": {"type": "string"}, "direction": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
        "key_price": _NUM_NULL, "target": _NUM_NULL, "x": _NUM_NULL})},
    "notes": {"type": "string"},
})


class ReadLevel(BaseModel):
    price: float
    kind: Literal["support", "resistance", "level"] = "level"
    label: str = ""


class ReadZone(BaseModel):
    price_high: float
    price_low: float
    kind: Literal["demand", "supply", "zone"] = "zone"
    label: str = ""
    x_start: Optional[float] = None
    x_end: Optional[float] = None


class ReadLine(BaseModel):
    x1: float
    price1: float
    x2: float
    price2: float
    label: str = ""


class ReadPattern(BaseModel):
    name: str
    direction: Literal["bullish", "bearish", "neutral"] = "neutral"
    key_price: Optional[float] = None
    target: Optional[float] = None
    x: Optional[float] = None


class ScreenshotRead(BaseModel):
    """What the model read off the image."""
    symbol: Optional[str] = None
    interval: Optional[str] = None
    time_left: Optional[str] = None
    time_right: Optional[str] = None
    levels: list[ReadLevel] = Field(default_factory=list)
    zones: list[ReadZone] = Field(default_factory=list)
    lines: list[ReadLine] = Field(default_factory=list)
    patterns: list[ReadPattern] = Field(default_factory=list)
    notes: str = ""


class ScreenshotRequest(BaseModel):
    image: str = Field(..., description="data:image/...;base64,... or bare base64", max_length=MAX_IMAGE_B64 + 100)
    symbol: Optional[str] = Field(None, description="The chart open now, used when the image shows no coin")
    interval: Optional[str] = None


def split_image(data: str) -> tuple[str, str]:
    """A data URL (or bare base64 PNG) → (media type, base64)."""
    m = re.match(r"^data:(image/[a-z+]+);base64,(.*)$", data.strip(), flags=re.S)
    media, b64 = (m.group(1), m.group(2)) if m else ("image/png", data.strip())
    if media == "image/jpg":
        media = "image/jpeg"
    if media not in MEDIA_TYPES:
        raise ValueError("The image must be a PNG, JPEG, WebP or GIF")
    b64 = re.sub(r"\s+", "", b64)
    if not b64 or len(b64) > MAX_IMAGE_B64 or not re.fullmatch(r"[A-Za-z0-9+/]+=*", b64):
        raise ValueError("The image is empty, too large (5 MB at most) or not base64")
    return media, b64


def _when(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def clean(read: ScreenshotRead, price_range: tuple[float, float] | None = None) -> tuple[ScreenshotRead, int]:
    """Drops impossible prices: non-positive ones and, when the real candles are known, any more than 2x outside
    their range (a misread axis). Returns the cleaned read and how many drawings were dropped."""
    lo, hi = price_range if price_range else (0.0, float("inf"))

    def ok(*prices: Optional[float]) -> bool:
        return all(p is None or (p > 0 and lo / 2 <= p <= hi * 2) for p in prices)

    out = read.model_copy(update={
        "levels": [x for x in read.levels if ok(x.price)],
        "zones": [x for x in read.zones if ok(x.price_high, x.price_low)],
        "lines": [x for x in read.lines if ok(x.price1, x.price2)],
        "patterns": [x for x in read.patterns if ok(x.key_price, x.target)],
    })
    if out.interval not in INTERVALS:
        out.interval = None
    dropped = sum(len(getattr(read, k)) - len(getattr(out, k)) for k in ("levels", "zones", "lines", "patterns"))
    return out, dropped


def to_overlays(read: ScreenshotRead) -> list:
    """The drawings as chart overlays (kind "screenshot", ids "shot:…")."""
    t0, t1 = _when(read.time_left), _when(read.time_right)
    span = (t0, t1) if t0 is not None and t1 is not None and t1 > t0 else None

    def at(x: Optional[float]) -> Optional[int]:
        if span is None or x is None:
            return None
        return int(span[0] + max(0.0, min(1.2, x)) * (span[1] - span[0]))

    out: list = []
    for i, lv in enumerate(read.levels):
        color = SUPPORT if lv.kind == "support" else RESISTANCE if lv.kind == "resistance" else LEVEL
        name = lv.label.strip() or lv.kind.capitalize()
        out.append(HorizontalLineOverlay(id=f"shot:level:{i}", price=lv.price, label=f"{name} (screenshot)",
                                         color=color, kind="screenshot", line_style="dashed", line_width=2))
    for i, z in enumerate(read.zones):
        color = DEMAND if z.kind == "demand" else SUPPLY if z.kind == "supply" else LEVEL
        name = z.label.strip() or ("Demand" if z.kind == "demand" else "Supply" if z.kind == "supply" else "Zone")
        out.append(BoxOverlay(id=f"shot:zone:{i}", price_high=z.price_high, price_low=z.price_low,
                              label=f"{name} (screenshot)", color=rgba(color, 0.15), border_color=rgba(color, 0.8), kind="screenshot",
                              time_start=at(z.x_start), time_end=at(z.x_end)))
    if span is not None:
        for i, ln in enumerate(read.lines):
            a, b = at(ln.x1), at(ln.x2)
            if a is None or b is None or a == b:
                continue
            (a, p1), (b, p2) = sorted(((a, ln.price1), (b, ln.price2)))
            out.append(TrendlineOverlay(id=f"shot:line:{i}", time1=a, price1=p1, time2=b, price2=p2,
                                        label=ln.label.strip() or "Trendline (screenshot)", color=PATTERN,
                                        kind="screenshot", line_style="solid"))
    for i, pt in enumerate(read.patterns):
        arrow = "▲" if pt.direction == "bullish" else "▼" if pt.direction == "bearish" else ""
        if pt.key_price is not None:
            out.append(HorizontalLineOverlay(id=f"shot:pattern:{i}", price=pt.key_price,
                                             label=f"{pt.name} {arrow} key level (screenshot)".replace("  ", " "),
                                             color=PATTERN, kind="screenshot", line_style="dashed",
                                             time_start=at(pt.x)))
        if pt.target is not None:
            out.append(HorizontalLineOverlay(id=f"shot:target:{i}", price=pt.target,
                                             label=f"{pt.name} target (screenshot)", color=PATTERN,
                                             kind="screenshot", line_style="dotted", time_start=at(pt.x)))
    return out


def _detected_prices(overlays: list) -> list[tuple[float, float]]:
    """(low, high) of each level or zone the detectors drew."""
    rows = []
    for o in overlays:
        d = o if isinstance(o, dict) else o.model_dump()
        if d.get("type") == "horizontal_line":
            rows.append((d["price"], d["price"]))
        elif d.get("type") == "box":
            rows.append((d["price_low"], d["price_high"]))
    return rows


def compare(read: ScreenshotRead, detected: list) -> tuple[int, list[float]]:
    """How many of the read levels and zones the app's detectors also found (within MATCH_PCT), and the prices of
    the ones they did not."""
    zones = _detected_prices(detected)
    prices = [lv.price for lv in read.levels] + [(z.price_high + z.price_low) / 2 for z in read.zones]
    hit, miss = 0, []
    for p in prices:
        tol = p * MATCH_PCT / 100
        if any(lo - tol <= p <= hi + tol for lo, hi in zones):
            hit += 1
        else:
            miss.append(p)
    return hit, miss


def describe(read: ScreenshotRead, symbol: Optional[str], interval: Optional[str], matched: Optional[tuple[int, list]],
             dropped: int) -> str:
    fmt = _fmt
    parts = []
    what = []
    for n, word in ((len(read.levels), "level"), (len(read.zones), "box"), (len(read.lines), "trendline"),
                    (len(read.patterns), "pattern")):
        if n:
            what.append(f"{n} {word}{'' if n == 1 else 'es' if word == 'box' else 's'}")
    where = f"{symbol} {interval or ''}".strip() if symbol else "the chart"
    if what:
        parts.append(f"Read {', '.join(what)} from the screenshot of {where} and drew them as dashed lines.")
    else:
        parts.append(f"Found no drawn levels, boxes or patterns in the screenshot of {where}.")
    for pt in read.patterns:
        bits = [f"{pt.name} ({pt.direction})"]
        if pt.key_price is not None:
            bits.append(f"key level {fmt(pt.key_price)}")
        if pt.target is not None:
            bits.append(f"target {fmt(pt.target)}")
        parts.append(", ".join(bits) + ".")
    if matched is not None and (read.levels or read.zones):
        hit, miss = matched
        total = len(read.levels) + len(read.zones)
        line = f"{hit} of {total} match levels the app finds on the real candles"
        if miss:
            line += f"; not found by the app: {', '.join(fmt(p) for p in miss[:6])}"
        parts.append(line + ".")
    if read.lines and not (read.time_left and read.time_right):
        parts.append("Trendlines were left out because the dates on the time axis could not be read.")
    if dropped:
        parts.append(f"{dropped} drawing{'' if dropped == 1 else 's'} had prices far from where this coin trades "
                     "and were skipped (probably a misread axis).")
    if read.notes.strip():
        parts.append(read.notes.strip())
    return " ".join(parts)
