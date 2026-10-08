"""How a coin moves against BTC: is it leading, lagging or just following?

From daily candles of the coin and BTCUSDT, aligned on their open times:

* change today and over 7 days, the coin's and BTC's, and the gap between them (positive: the coin is stronger),
* the correlation of daily returns over the last 30 days (1 moves in lockstep with BTC, 0 unrelated),
* beta over the same days (1.5: a 1% BTC move comes with about 1.5% in the coin).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

WINDOW = 30


def _pct(a: float, b: float) -> float | None:
    return round((a / b - 1) * 100, 2) if b else None


def vs_btc(coin: pd.DataFrame, btc: pd.DataFrame, window: int = WINDOW) -> dict | None:
    """The coin against BTC from daily frames (oldest → newest, the last bar may still be forming); None when
    they share fewer than 10 days."""
    joined = pd.merge(coin[["time", "close"]], btc[["time", "close"]], on="time", suffixes=("", "_btc"))
    if len(joined) < 10:
        return None
    c, b = joined["close"].to_numpy(float), joined["close_btc"].to_numpy(float)
    out: dict = {"days": min(window, len(joined) - 1)}
    for label, back in (("today", 1), ("7d", 7)):
        if len(joined) > back:
            mine, theirs = _pct(c[-1], c[-1 - back]), _pct(b[-1], b[-1 - back])
            out[f"change_{label}_pct"] = mine
            out[f"btc_change_{label}_pct"] = theirs
            if mine is not None and theirs is not None:
                out[f"vs_btc_{label}_pct"] = round(mine - theirs, 2)
    rc, rb = np.diff(c)[-window:] / c[:-1][-window:], np.diff(b)[-window:] / b[:-1][-window:]
    if len(rc) >= 10 and rc.std() > 0 and rb.std() > 0:
        corr = float(np.corrcoef(rc, rb)[0, 1])
        out["correlation_30d"] = round(corr, 2)
        out["beta_30d"] = round(float(np.cov(rc, rb)[0, 1] / rb.var(ddof=1)), 2)
        out["follows_btc"] = "closely" if corr >= 0.8 else "loosely" if corr >= 0.5 else "barely"
    gap = out.get("vs_btc_7d_pct")
    if gap is not None:
        out["strength"] = "stronger than BTC" if gap > 2 else "weaker than BTC" if gap < -2 else "in line with BTC"
    return out
