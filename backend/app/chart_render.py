"""Server-side chart snapshots for alert cards: the last candles with the zone, levels and the candle that fired, in
the app's dark palette. Pillow, no browser. Built from the same closed candles the signal judged, stamped "as of
<close>", with a DEMO watermark for synthetic data. Deterministic (same input, same bytes) and cached."""

from __future__ import annotations

import asyncio
import hashlib
import io
import time
from collections import OrderedDict
from typing import Optional

import pandas as pd
from PIL import Image, ImageDraw, ImageFont

from .pricefmt import _fmt as fmt_price

BG, GRID, TEXT, MUTE = (11, 14, 20), (31, 38, 51), (226, 232, 240), (100, 116, 139)
UP, DOWN = (34, 197, 94), (239, 68, 68)
BARS = 120
CACHE_SIZE = 32
_cache: "OrderedDict[str, bytes]" = OrderedDict()
_sem = asyncio.Semaphore(1)

Box = tuple[float, float, tuple[int, int, int, int], str]          # low, high, rgba, label
Line = tuple[float, tuple[int, int, int], bool, str]                 # price, rgb, dashed, label
Marker = tuple[int, float, str, tuple[int, int, int]]                # time, price, "up" | "down", rgb


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # older Pillow
        return ImageFont.load_default()


def render(df: pd.DataFrame, boxes: list[Box] = (), lines: list[Line] = (), markers: list[Marker] = (),  # type: ignore[assignment]
           title: str = "", demo: bool = False, width: int = 800, height: int = 420) -> bytes:
    """PNG bytes of the last BARS candles of `df` with the boxes, lines and markers."""
    df = df.tail(BARS).reset_index(drop=True)
    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img, "RGBA")
    f12, f11 = _font(12), _font(11)
    left, right, top, bottom = 8, width - 78, 26, height - 18
    prices = list(df["low"]) + list(df["high"]) + [b[0] for b in boxes] + [b[1] for b in boxes] + [ln[0] for ln in lines]
    lo, hi = min(prices), max(prices)
    pad = (hi - lo) * 0.06 or hi * 0.01 or 1.0
    lo, hi = lo - pad, hi + pad

    def y(p: float) -> float:
        return top + (hi - p) / (hi - lo) * (bottom - top)

    n = max(len(df), 1)
    step = (right - left) / n
    for i in range(5):  # grid and price axis
        p = lo + (hi - lo) * i / 4
        d.line([(left, y(p)), (right, y(p))], fill=GRID, width=1)
        d.text((right + 6, y(p) - 7), fmt_price(p), fill=MUTE, font=f11)
    for b_lo, b_hi, rgba, label in boxes:
        d.rectangle([left, y(b_hi), right, y(b_lo)], fill=rgba, outline=rgba[:3] + (200,))
        if label:
            d.text((left + 4, y(b_hi) + 2), label, fill=TEXT, font=f11)
    for i, r in df.iterrows():
        x = left + step * (i + 0.5)
        col = UP if r["close"] >= r["open"] else DOWN
        d.line([(x, y(r["high"])), (x, y(r["low"]))], fill=col, width=1)
        y0, y1 = sorted((y(r["open"]), y(r["close"])))
        d.rectangle([x - max(step * 0.35, 0.5), y0, x + max(step * 0.35, 0.5), max(y1, y0 + 1)], fill=col)
    for price, rgb, dashed, label in lines:
        yy = y(price)
        if dashed:
            x = left
            while x < right:
                d.line([(x, yy), (min(x + 6, right), yy)], fill=rgb, width=1)
                x += 10
        else:
            d.line([(left, yy), (right, yy)], fill=rgb, width=1)
        d.text((right - 4 - d.textlength(label, font=f11), yy - 13), label, fill=rgb, font=f11)
    times = list(df["time"])
    for t, price, way, rgb in markers:
        if t not in times:
            continue
        x = left + step * (times.index(t) + 0.5)
        yy = y(price)
        tri = [(x, yy + 4), (x - 5, yy + 13), (x + 5, yy + 13)] if way == "up" else [(x, yy - 4), (x - 5, yy - 13), (x + 5, yy - 13)]
        d.polygon(tri, fill=rgb)
    last = float(df["close"].iloc[-1])
    d.rectangle([right + 2, y(last) - 8, width - 2, y(last) + 8], fill=UP if last >= df["open"].iloc[-1] else DOWN)
    d.text((right + 6, y(last) - 7), fmt_price(last), fill=BG, font=f11)
    close_t = int(times[-1]) + (int(times[-1] - times[-2]) if len(times) > 1 else 0)
    stamp = time.strftime("%d %b %H:%M UTC", time.gmtime(close_t))
    d.text((left, 6), title, fill=TEXT, font=f12)
    d.text((right - d.textlength(f"as of {stamp} close", font=f11), 7), f"as of {stamp} close", fill=MUTE, font=f11)
    if demo:
        d.text((width / 2 - 40, height / 2 - 10), "DEMO DATA", fill=(250, 204, 21, 110), font=_font(20))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def _key(df: pd.DataFrame, *parts) -> str:
    tail = df.tail(BARS)
    h = hashlib.sha256(repr((tail.to_numpy().tobytes(), parts)).encode()).hexdigest()
    return h


async def snapshot(df: pd.DataFrame, **kw) -> Optional[bytes]:
    """render() in a thread, one at a time, cached; None when it fails (the card goes out without a picture)."""
    key = _key(df, sorted(kw.items(), key=lambda kv: kv[0]))
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    async with _sem:
        try:
            png = await asyncio.to_thread(render, df, **kw)
        except Exception:
            return None
    _cache[key] = png
    while len(_cache) > CACHE_SIZE:
        _cache.popitem(last=False)
    return png
