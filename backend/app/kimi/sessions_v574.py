"""
Kimi Cooked - Elite Edition v5.7.4 -- "Session Filter / Killzones" (agentic-charts port).

The script's two session windows (defaults: Asia 0000-0800, London/NY 1200-2100, UTC), both features off by
default:
  * useSessFilter: entry signals (divergences, early warnings, S/R retests, harmonics, pattern break-outs and the
    random controls) only register on candles inside an enabled window (sessOk);
  * useSessionParams: inside window 1 / window 2 the S/R zone width and the divergence threshold are multiplied.
Only intraday charts have sessions (timeframe.isintraday); on daily and up sessOk is always true.

A candle is inside a window when its open time is, like `time(timeframe.period, session, tz)` != na.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np

_SESSION = re.compile(r"^\s*(\d{2})(\d{2})-(\d{2})(\d{2})(?::([1-7]+))?\s*$")
_OFFSET = re.compile(r"^(?:UTC|GMT)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$", re.I)


def _tzinfo(name: str):
    """"UTC", "GMT+3", "UTC-05:30" or an IANA name ("Europe/London"); unknown names fall back to UTC."""
    if not name or name.upper() in ("UTC", "GMT", "ETC/UTC"):
        return timezone.utc
    m = _OFFSET.match(name.strip())
    if m:
        sign = 1 if m.group(1) == "+" else -1
        return timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0)))
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - an unknown zone must not stop the indicator
        return timezone.utc


def in_session(t_ms: np.ndarray, session: str, tz: str) -> np.ndarray:
    """True for candles whose open time falls inside `session` ("HHMM-HHMM[:days]", days 1 = Sunday .. 7 =
    Saturday as in Pine; an end before the start wraps past midnight)."""
    m = _SESSION.match(session or "")
    out = np.zeros(len(t_ms), dtype=bool)
    if not m:
        return out
    start = int(m.group(1)) * 60 + int(m.group(2))
    end = int(m.group(3)) * 60 + int(m.group(4))
    days = {int(x) for x in m.group(5)} if m.group(5) else None
    zone = _tzinfo(tz)
    for k, t in enumerate(t_ms):
        local = datetime.fromtimestamp(int(t) / 1000.0, tz=zone)
        minute = local.hour * 60 + local.minute
        if start < end:
            inside, day_ref = start <= minute < end, local
        elif start > end:
            # overnight window: the part after midnight belongs to the session that opened the day before
            inside = minute >= start or minute < end
            day_ref = local if minute >= start else local - timedelta(days=1)
        else:
            inside, day_ref = True, local      # "0000-0000" = the whole day
        if inside and days is not None:
            inside = (day_ref.isoweekday() % 7) + 1 in days
        out[k] = inside
    return out


def session_arrays(t_ms: np.ndarray, tf_minutes: float, p) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(sessOk, sessZoneMult, sessDivMult) per candle for the script's session inputs on `p`."""
    n = len(t_ms)
    intraday = tf_minutes < 1440
    in1: Optional[np.ndarray] = None
    in2: Optional[np.ndarray] = None
    if intraday and (p.useSessFilter or p.useSessionParams):
        in1 = in_session(t_ms, p.sess1, p.sessTz) if p.sess1On else np.zeros(n, dtype=bool)
        in2 = in_session(t_ms, p.sess2, p.sessTz) if p.sess2On else np.zeros(n, dtype=bool)
    sess_ok = np.ones(n, dtype=bool)
    if p.useSessFilter and in1 is not None:
        sess_ok = in1 | in2
    zone_mult = np.ones(n)
    div_mult = np.ones(n)
    if p.useSessionParams and in1 is not None:
        zone_mult = np.where(in1, p.asiaZoneMult, np.where(in2, p.ldnNyZoneMult, 1.0))
        div_mult = np.where(in1, p.asiaDivMult, np.where(in2, p.ldnNyDivMult, 1.0))
    return sess_ok, zone_mult, div_mult
