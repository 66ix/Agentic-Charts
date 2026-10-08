"""Prices in answers, rounded the way the exchange shows them.

Binance gives every pair a price step (tick size) of about five significant digits, never finer than 0.01 above
$100: BTC 65,123.45, INJ 7.8271, DOGE 0.15234. Answers use the same rounding, estimated from the price, so the
agent never says "ATR 0.090334".
"""

from __future__ import annotations

import math


def price_decimals(p: float) -> int:
    """Decimals of a coin's price step, estimated from its price the way Binance sets them: about five
    significant digits, and never finer than 0.01 above $100 (BTC 65,123.45, INJ 7.8271, DOGE 0.15234)."""
    if not p or not math.isfinite(p):
        return 2
    tick = min(0.01, 10.0 ** (math.floor(math.log10(abs(p))) - 4))
    return max(0, min(8, -math.floor(math.log10(tick) + 1e-9)))


def _fmt(p: float, ref: float | None = None, strip: bool = False) -> str:
    """A price (or a price distance like ATR when `ref` is the price) rounded to the coin's price step.
    `strip` drops trailing zeros, for prices the user typed ("a line at 25.4")."""
    text = f"{p:,.{price_decimals(ref or p)}f}"
    return text.rstrip("0").rstrip(".") if strip and "." in text else text


# Keys whose values are prices (or price distances) in the facts handed to the LLM.
PRICE_KEYS = frozenset({
    "price", "last_price", "low", "high", "atr", "entry", "stop", "level", "neckline", "poc", "val", "vah",
    "price1", "price2", "price_low", "price_high", "mid", "target", "open", "close", "mark_price", "ema_fast",
    "ema_slow", "range", "upper", "lower",
})


def round_facts(obj, ref: float | None = None):
    """The facts with every price rounded to its coin's price step and other numbers to 4 significant digits, so
    the LLM quotes "ATR 0.0903" rather than "0.090334". A dict with its own `price`/`last_price` (a scan row,
    another coin) rounds with that price."""
    if isinstance(obj, dict):
        own = obj.get("last_price", obj.get("price"))
        r = own if isinstance(own, (int, float)) and not isinstance(own, bool) and own > 0 else ref
        out = {}
        for k, v in obj.items():
            if isinstance(v, float) and k in PRICE_KEYS and r:
                out[k] = round(v, price_decimals(r))
            elif isinstance(v, (list, tuple)) and k in PRICE_KEYS and r:
                out[k] = [round(x, price_decimals(r)) if isinstance(x, float) else round_facts(x, r) for x in v]
            else:
                out[k] = round_facts(v, r)
        return out
    if isinstance(obj, (list, tuple)):
        return [round_facts(x, ref) for x in obj]
    if isinstance(obj, float) and math.isfinite(obj) and obj != 0:
        return float(f"{obj:.4g}") if abs(obj) < 1e4 else round(obj, 1)
    return obj
