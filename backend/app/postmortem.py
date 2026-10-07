"""Trade post-mortems and the weekly review.

When a journal trade closes (stop, target, breakeven or by hand) it gets a short review stored on the entry: how the
fill sat against its zone, the best and worst excursion in R (and which came first), what happened at each target
(hit, missed by how much, reached after the exit), what the agent's levels and Kimi Cooked said when the trade was
taken, and one or two concrete lessons.

Every number comes from the data: `trade_facts` works them out in Python from the journal's evaluation and the same
candles it replays on, and `lessons_for` picks the lessons with fixed rules. The configured LLM, when there is one,
only rewrites the text; its answer is used only if every number in it matches a number in the facts (prices to
their printed precision), otherwise the deterministic template is kept. "At entry" context is rebuilt from candles
that had closed by `taken_at`: the agent's zones and trend on the trade's timeframe, and Kimi's last label that was
tradable before the entry (Kimi levels count when they were active at the entry; their pivots are confirmed a few
bars after they print, so treat those as approximate).

The weekly review sums the closed trades of the last N days (win rate, average R, best and worst setup types and
coins, lessons that keep coming back) and can be sent on a schedule to Telegram / Discord, like the brief. Its
settings and the scheduler's memory live in JOURNAL_REVIEW_STORE.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, field_validator

from .alerts import AlertService, fmt_price, read_store, store_path, write_store
from .brief import GRACE_SECONDS, NoChannelError, _TIME, _tz
from .config import Settings, get_settings
from .journal import JournalEntry, JournalEvaluation, JournalService, PostMortem, journal_stats
from .market_data import DERIVED_INTERVALS, INTERVAL_SECONDS, MarketDataError
from .scanner import SCAN_INTENT
from .ta_agent import TF_LABEL, analyze

if TYPE_CHECKING:
    from .kimi_service import KimiService
    from .llm import LLMClient
    from .market_data import MarketData

log = logging.getLogger(__name__)

CONTEXT_BARS = 300          # candles before the entry the agent's levels are rebuilt from
KIMI_MAX_BARS = 30          # a Kimi label more than this many bars before the entry is not "at entry"
AFTER_EXIT_MIN_H = 4.0      # how long after the exit to look for "reached the target anyway"
AFTER_EXIT_MAX_H = 48.0
SWEEP_SECONDS = 300.0       # background check for closed trades without a post-mortem
CHECK_SECONDS = 30.0        # weekly review scheduler
KIMI_TIMEOUT = 60.0
MAX_LESSONS = 2

# Lesson codes → the name the weekly review uses for a recurring one.
LESSON_NAMES = {
    "stop_too_tight": "stop too tight (stopped, then the target)",
    "closed_early": "closed by hand before the plan played out",
    "gave_back": "gave back open profit",
    "target_too_far": "target just out of reach",
    "chased_entry": "entry outside the zone",
    "against_trend": "against the trend at entry",
    "against_kimi": "against Kimi's last label",
    "no_reaction": "stopped with no reaction from the zone",
    "early_entry": "deep drawdown before it worked",
    "breakeven_saved": "breakeven stop protected the trade",
    "clean": "clean trade, as planned",
    "normal_loss": "normal loss, plan followed",
}


# ------------------------------------------------------------------- facts --


def _r6(x: float) -> float:
    return float(f"{x:.6g}")


def _hours(seconds: float) -> float:
    return round(seconds / 3600, 2)


def _ago(hours: float) -> str:
    if hours * 60 < 1:
        return "under a minute"
    return f"{round(hours * 60)} min" if hours < 1 else f"{hours:g}h" if hours < 48 else f"{hours / 24:.1f} days"


def _zone_at_entry(e: JournalEntry, fill: float, ctx: Optional[dict]) -> Optional[dict]:
    """The zone the trade was taken from: the plan's, else the agent's nearest zone on the trade's side."""
    if e.zone_low is not None and e.zone_high is not None:
        return {"low": e.zone_low, "high": e.zone_high, "label": e.setup, "from": "plan"}
    if not ctx:
        return None
    kinds = ("support", "demand") if e.direction == "long" else ("resistance", "supply")
    zones = [(k, z) for k in kinds for z in ctx.get("zones", {}).get(k, [])]
    if not zones:
        return None
    k, z = min(zones, key=lambda kz: 0.0 if kz[1]["low"] <= fill <= kz[1]["high"]
               else min(abs(fill - kz[1]["low"]), abs(fill - kz[1]["high"])))
    return {"low": z["low"], "high": z["high"], "label": f"{ctx['timeframe']} {k}", "from": "agent"}


def trade_facts(e: JournalEntry, ev: JournalEvaluation, df: pd.DataFrame, at_entry: Optional[dict] = None,
                kimi: Optional[dict] = None) -> dict:
    """Everything a post-mortem says, worked out from the evaluation and the candles it replayed on (time/open/
    high/low/close, any range covering the trade; candles after the exit are used for "after the exit")."""
    if ev.status != "closed" or ev.fill_price is None or ev.filled_at is None or ev.closed_at is None:
        raise ValueError("Only closed trades get a post-mortem")
    long = e.direction == "long"
    sign = 1.0 if long else -1.0
    fill = ev.fill_price
    risk = abs(e.entry - e.stop)

    def r_of(px: float) -> float:
        return (px - fill) * sign / risk

    def at_r(r: float) -> float:
        return _r6(fill + sign * r * risk)

    t = df["time"].to_numpy(dtype=np.int64)
    h, lo = df["high"].to_numpy(dtype=float), df["low"].to_numpy(dtype=float)
    step = INTERVAL_SECONDS.get(ev.resolution, 60)
    last_exit = ev.exits[-1]
    in_trade = (t >= ev.filled_at) & (t <= ev.closed_at)

    facts: dict = {
        "trade": {"symbol": e.symbol, "timeframe": TF_LABEL.get(e.interval, e.interval), "direction": e.direction,
                  "setup": e.setup, "entry_type": e.entry_type, "entry": e.entry, "stop": e.stop,
                  "targets": list(e.targets), "taken_at": e.taken_at},
        "result": {"outcome": ev.outcome, "realized_r": round(ev.realized_r, 2), "fees_r": round(ev.fees_r, 2),
                   "pnl_usd": ev.pnl_usd, "closed_by": last_exit.kind, "fill_price": fill,
                   "hours_to_fill": _hours(ev.filled_at - e.taken_at),
                   "hours_in_trade": _hours(ev.closed_at - ev.filled_at),
                   "exits": [{"kind": x.kind, "price": _r6(x.price), "r": round(x.r, 2),
                              "hours_after_fill": _hours(x.time - ev.filled_at)} for x in ev.exits]},
        "data_source": ev.data_source,
    }

    # ---- excursions, and which extreme came first
    mfe, mae = ev.mfe_r or 0.0, ev.mae_r or 0.0
    exc = {"mfe_r": round(mfe, 2), "mae_r": round(mae, 2), "mfe_price": at_r(mfe), "mae_price": at_r(mae),
           "heat_first": None}
    # The fill candle's favourable extreme may have printed before the fill, so it only counts for the drawdown.
    fav_idx = np.flatnonzero(in_trade & (t > ev.filled_at))
    adv_idx = np.flatnonzero(in_trade)
    if len(fav_idx) and mae <= -0.05 and mfe >= 0.05:
        fav, adv = (h, lo) if long else (lo, h)
        i_fav = fav_idx[int(np.argmax(fav[fav_idx]) if long else np.argmin(fav[fav_idx]))]
        i_adv = adv_idx[int(np.argmin(adv[adv_idx]) if long else np.argmax(adv[adv_idx]))]
        exc["heat_first"] = bool(i_adv < i_fav)
    facts["excursion"] = exc

    # ---- after the exit: did price go on to the targets (or back to the stop)?
    window_h = min(max(_hours(ev.closed_at - ev.filled_at) * 2, AFTER_EXIT_MIN_H), AFTER_EXIT_MAX_H)
    # A stop or target candle may have gone on after the exit, but which part came first is unknown: it is left out.
    manual = last_exit.kind == "manual"
    after = ((t >= ev.closed_at) if manual else (t > ev.closed_at)) & (t <= ev.closed_at + window_h * 3600)
    after_info = None
    if after.any():
        a_fav = h[after].max() if long else lo[after].min()
        a_adv = lo[after].min() if long else h[after].max()
        seen_h = _hours(int(t[after][-1]) + step - ev.closed_at)
        after_info = {"hours_watched": min(seen_h, window_h), "best_r": round(r_of(a_fav), 2),
                      "best_price": _r6(a_fav), "worst_r": round(r_of(a_adv), 2), "worst_price": _r6(a_adv)}
    facts["after_exit"] = after_info

    # ---- each target
    hit_kinds = {x.kind: x for x in ev.exits}
    targets = []
    for i, tp in enumerate(e.targets):
        name = f"T{i + 1}"
        row: dict = {"name": name, "price": tp, "planned_r": round(abs(tp - e.entry) / risk, 2), "hit": name in hit_kinds}
        if row["hit"]:
            row["hours_after_fill"] = _hours(hit_kinds[name].time - ev.filled_at)
        else:
            row["short_by_r"] = round(max(0.0, r_of(tp) - mfe), 2)
            if after.any():
                reach = (h >= tp) if long else (lo <= tp)
                j = np.flatnonzero(reach & after)
                if len(j):
                    row["reached_after_exit_h"] = _hours(int(t[j[0]]) - ev.closed_at)
        targets.append(row)
    facts["targets"] = targets

    # ---- the fill against its zone
    zone = _zone_at_entry(e, fill, at_entry)
    if zone:
        lo_z, hi_z = zone["low"], zone["high"]
        if lo_z <= fill <= hi_z:
            depth = (hi_z - fill) / (hi_z - lo_z) if long else (fill - lo_z) / (hi_z - lo_z)
            zone.update(position="inside", depth_pct=round(depth * 100) if hi_z > lo_z else 0)
        else:
            edge = hi_z if fill > hi_z else lo_z
            chased = (fill > hi_z) if long else (fill < lo_z)
            zone.update(position="outside", chased=chased, distance_pct=round(abs(fill - edge) / fill * 100, 2),
                        distance_r=round(abs(fill - edge) / risk, 2))
        zone["stop_beyond"] = (e.stop < lo_z) if long else (e.stop > hi_z)
    facts["zone"] = zone
    facts["at_entry"] = at_entry and {k: v for k, v in at_entry.items() if k != "zones"}
    facts["kimi"] = kimi
    return facts


def entry_context(df: pd.DataFrame, interval: str) -> Optional[dict]:
    """The agent's read of the chart from candles closed by the entry: trend, RSI, last structure break and the
    zones (support/demand below, resistance/supply above)."""
    if len(df) < 60:
        return None
    res = analyze(df.reset_index(drop=True), SCAN_INTENT, interval)
    f = res.facts
    out: dict = {"timeframe": TF_LABEL.get(interval, interval), "trend": res.stats.trend,
                 "rsi": (f.get("momentum") or {}).get("rsi"), "price": _r6(float(df["close"].iloc[-1]))}
    br = f.get("last_structure_break")
    if br:
        out["last_break"] = f"{br['direction']} {br['type']} at {fmt_price(br['level'])}, {br['bars_ago']} bars before"
        out["last_break_level"] = br["level"]
    zones = {k: [{"low": _r6(z["low"]), "high": _r6(z["high"])} for z in f.get(k, [])[:2]]
             for k in ("support", "demand", "resistance", "supply")}
    out["zones"] = zones
    below = [z for k in ("support", "demand") for z in zones[k]]
    above = [z for k in ("resistance", "supply") for z in zones[k]]
    out["support"] = max(below, key=lambda z: z["high"], default=None)
    out["resistance"] = min(above, key=lambda z: z["low"], default=None)
    return out


def kimi_context(k, taken_at: int, interval: str, fill: float) -> Optional[dict]:
    """Kimi Cooked at the entry: its last label tradable before `taken_at`, and the levels active then."""
    step = INTERVAL_SECONDS.get(interval, 3600)
    out: dict = {}
    known = [s for s in k.signals if s.confirm_time <= taken_at]
    if known:
        s = known[-1]
        bars = int((taken_at - s.confirm_time) // step)
        if bars <= KIMI_MAX_BARS:
            out["last_signal"] = {"label": s.text, "direction": s.direction, "price": _r6(s.price), "bars_before": bars}
    active = [lv for lv in k.levels if lv.time_start <= taken_at and (lv.time_end is None or lv.time_end > taken_at)]
    sup = [lv for lv in active if lv.side == "support" and lv.price <= fill]
    res = [lv for lv in active if lv.side == "resistance" and lv.price >= fill]
    if sup:
        lv = max(sup, key=lambda x: x.price)
        out["support"] = {"price": _r6(lv.price), "low": _r6(lv.zone_low), "high": _r6(lv.zone_high)}
    if res:
        lv = min(res, key=lambda x: x.price)
        out["resistance"] = {"price": _r6(lv.price), "low": _r6(lv.zone_low), "high": _r6(lv.zone_high)}
    return out or None


# ----------------------------------------------------------------- lessons --


def lessons_for(f: dict) -> list[tuple[str, str]]:
    """Up to two (code, lesson) pairs, most useful first, each tied to this trade's numbers."""
    tr, res, exc, zone = f["trade"], f["result"], f["excursion"], f.get("zone")
    targets, after = f["targets"], f.get("after_exit")
    long = tr["direction"] == "long"
    outcome, closed_by = res["outcome"], res["closed_by"]
    t1 = targets[0] if targets else None
    out: list[tuple[str, str]] = []

    def add(code: str, text: str) -> None:
        if code not in (c for c, _ in out):
            out.append((code, text))

    if closed_by == "stop" and t1 and not t1["hit"] and t1.get("reached_after_exit_h") is not None:
        where = ""
        if zone and not zone.get("stop_beyond"):
            where = f", inside the zone {fmt_price(zone['low'])}–{fmt_price(zone['high'])}"
        add("stop_too_tight", f"The stop at {fmt_price(tr['stop'])}{where} was hit, then price reached T1 "
            f"{fmt_price(t1['price'])} {_ago(t1['reached_after_exit_h'])} later: the stop sat inside the noise. Put it "
            f"beyond the zone's far edge with a buffer, and size down to keep the same risk.")
    if closed_by == "manual":
        later = [t for t in targets if not t["hit"] and t.get("reached_after_exit_h") is not None]
        if later:
            t = later[0]
            add("closed_early", f"You closed by hand at {fmt_price(res['exits'][-1]['price'])} "
                f"({res['exits'][-1]['r']:+.2f}R); {t['name']} {fmt_price(t['price'])} was reached "
                f"{_ago(t['reached_after_exit_h'])} later. Let the plan's stop and targets work unless the reason for "
                f"the trade is gone.")
    if exc["mfe_r"] >= 1.0 and res["realized_r"] < 0.3:
        add("gave_back", f"Price was {exc['mfe_r']:+.2f}R in your favour (at {fmt_price(exc['mfe_price'])}) but the "
            f"trade closed at {res['realized_r']:+.2f}R. Take part off or trail the stop once it is +1R.")
    if zone and zone["position"] == "outside" and zone.get("chased") and zone["distance_r"] >= 0.25:
        add("chased_entry", f"The fill at {fmt_price(res['fill_price'])} was {zone['distance_pct']}% outside the zone "
            f"{fmt_price(zone['low'])}–{fmt_price(zone['high'])} ({zone['distance_r']}R of room given away). Wait for "
            f"price to come into the zone, or rest a limit at its edge.")
    if t1 and not t1["hit"] and 0 < t1["short_by_r"] <= 0.3:
        add("target_too_far", f"T1 {fmt_price(t1['price'])} was missed by {t1['short_by_r']}R (best "
            f"{fmt_price(exc['mfe_price'])}). Set targets just in front of the level, not on it.")
    ctx = f.get("at_entry") or {}
    against = {"long": "down", "short": "up"}[tr["direction"]]
    if outcome != "win" and ctx.get("trend") == against:
        add("against_trend", f"This {tr['direction']} went against the {ctx['timeframe']} {against}trend at entry. "
            f"Take counter-trend trades only after a lower-timeframe confirmation in the zone, or at half size.")
    sig = (f.get("kimi") or {}).get("last_signal")
    if outcome != "win" and sig and sig["direction"] != tr["direction"]:
        add("against_kimi", f"Kimi's last label before the entry was {sig['label']} at {fmt_price(sig['price'])} "
            f"({sig['bars_before']} bars earlier), pointing the other way. Treat a fresh opposite label as a reason "
            f"to wait.")
    if closed_by == "stop" and exc.get("heat_first") is not False and exc["mfe_r"] < 0.3 \
            and res["hours_in_trade"] * 3600 <= 3 * INTERVAL_SECONDS.get(_tf_value(tr["timeframe"]), 3600):
        add("no_reaction", f"Stopped {_ago(res['hours_in_trade'])} after the fill with no reaction (best "
            f"{exc['mfe_r']:+.2f}R): wait for the zone to react (a sweep or CHoCH on a lower timeframe) before "
            f"entering.")
    if outcome == "win" and exc["mae_r"] <= -0.7:
        add("early_entry", f"It worked, but only after going {exc['mae_r']:+.2f}R against you (to "
            f"{fmt_price(exc['mae_price'])}): the entry was early. A lower-timeframe confirmation in the zone would "
            f"have allowed a tighter stop.")
    if any(x["kind"] == "breakeven" for x in res["exits"]) and len(out) < MAX_LESSONS:
        add("breakeven_saved", "Moving the stop to breakeven after T1 kept the rest of the position from turning "
            "into a loss. Keep doing it on this setup.")
    if not out:
        if outcome == "win":
            add("clean", f"Clean trade: {tr['setup']} worked as planned with {exc['mae_r']:+.2f}R of heat. Keep "
                f"taking this setup as it is.")
        elif outcome == "loss":
            add("normal_loss", "A normal loss: the plan was followed and the stop did its job. Judge this setup on "
                "its stats over many trades, not on this one.")
        else:
            add("normal_loss", "A scratch: nothing to change on one trade.")
    return out[:MAX_LESSONS]


def _tf_value(label: str) -> str:
    return next((k for k, v in TF_LABEL.items() if v == label), label)


# ---------------------------------------------------------------- template --


def _exit_text(x: dict) -> str:
    px = fmt_price(x["price"])
    if x["kind"] == "stop":
        return f"stopped at {px}"
    if x["kind"] == "breakeven":
        return f"stopped at breakeven {px}"
    if x["kind"] == "manual":
        return f"closed by hand at {px}"
    return f"{x['kind']} {px} hit"


def render_postmortem(f: dict) -> str:
    """The deterministic review: a few sentences, every number from `f`."""
    tr, res, exc, zone = f["trade"], f["result"], f["excursion"], f.get("zone")
    word = {"win": "as a win", "loss": "as a loss", "breakeven": "at breakeven"}.get(res["outcome"] or "", "")
    pnl = f", {'+' if res['pnl_usd'] >= 0 else '−'}${abs(res['pnl_usd']):,.2f}" if res.get("pnl_usd") is not None else ""
    lines = [f"{tr['direction'].title()} {tr['symbol']} ({tr['setup']}, {tr['timeframe']}) closed {word} "
             f"({res['realized_r']:+.2f}R{pnl}): filled at {fmt_price(res['fill_price'])}, then "
             f"{', then '.join(_exit_text(x) for x in res['exits'])}, {_ago(res['hours_in_trade'])} after the fill."]
    if zone:
        band = f"{zone['label']} {fmt_price(zone['low'])}–{fmt_price(zone['high'])}"
        if zone["position"] == "inside":
            lines.append(f"The fill was {zone['depth_pct']}% deep into the {band}.")
        else:
            side = "above" if res["fill_price"] > zone["high"] else "below"
            lines.append(f"The fill was {zone['distance_pct']}% {side} the {band}.")
    order = ""
    if exc.get("heat_first") is True:
        order = "; the drawdown came first"
    elif exc.get("heat_first") is False:
        order = "; the move in your favour came first"
    lines.append(f"Best {exc['mfe_r']:+.2f}R ({fmt_price(exc['mfe_price'])}), worst {exc['mae_r']:+.2f}R "
                 f"({fmt_price(exc['mae_price'])}){order}.")
    missed = []
    for t in f["targets"]:
        if t["hit"]:
            continue
        s = f"{t['name']} {fmt_price(t['price'])} ({t['planned_r']}R) missed by {t['short_by_r']}R"
        if t.get("reached_after_exit_h") is not None:
            s += f", reached {_ago(t['reached_after_exit_h'])} after the exit"
        missed.append(s)
    if missed:
        text = "; ".join(missed)
        lines.append(text[0].upper() + text[1:] + ".")
    ctx, kimi = f.get("at_entry"), f.get("kimi")
    parts = []
    if ctx:
        p = f"the {ctx['timeframe']} trend was {ctx['trend']}"
        if ctx.get("rsi") is not None:
            p += f" with RSI {ctx['rsi']}"
        parts.append(p)
        if ctx.get("support"):
            parts.append(f"the agent's nearest support was {fmt_price(ctx['support']['low'])}–"
                         f"{fmt_price(ctx['support']['high'])}")
        if ctx.get("resistance"):
            parts.append(f"resistance {fmt_price(ctx['resistance']['low'])}–{fmt_price(ctx['resistance']['high'])}")
    if kimi and kimi.get("last_signal"):
        s = kimi["last_signal"]
        parts.append(f"Kimi's last label was {s['label']} ({s['direction']}) at {fmt_price(s['price'])}, "
                     f"{s['bars_before']} bars earlier")
    elif kimi and (kimi.get("support") or kimi.get("resistance")):
        lv = kimi.get("support") if tr["direction"] == "long" else kimi.get("resistance")
        if lv:
            parts.append(f"Kimi's nearest {'support' if tr['direction'] == 'long' else 'resistance'} was "
                         f"{fmt_price(lv['price'])}")
    if parts:
        lines.append("At entry " + ", ".join(parts) + ".")
    if f.get("data_source") == "synthetic":
        lines.append("(Tracked on synthetic demo data, not real prices.)")
    return " ".join(lines)


# ------------------------------------------------------------- LLM checks --

POSTMORTEM_SYSTEM = (
    "You review one closed crypto trade for the trader who took it. Use only the numbers in FACTS and the DRAFT; "
    "copy prices exactly as written there and never invent a price, level, time or R value. Write 3 to 5 short, "
    "plain sentences: what happened, how the fill sat against its zone, the best and worst excursion in R, what "
    "happened at each target, and what the agent's levels and Kimi Cooked said at entry when FACTS has them. Then "
    "1 or 2 lessons, each on its own line starting with '- ', concrete and tied to this trade's numbers (the "
    "LESSONS are a good basis). No headings, no other markdown."
)

_NUM = re.compile(r"[-+−]?\d[\d,]*(?:\.\d+)?")


def _leaves(x) -> list[float]:
    if isinstance(x, bool) or x is None:
        return []
    if isinstance(x, (int, float)):
        return [float(x)]
    if isinstance(x, dict):
        return [v for y in x.values() for v in _leaves(y)]
    if isinstance(x, (list, tuple)):
        return [v for y in x for v in _leaves(y)]
    if isinstance(x, str):
        return [float(m.replace(",", "").replace("−", "-")) for m in _NUM.findall(x)]
    return []


def grounded(text: str, facts: dict, extra: str = "") -> bool:
    """True when every number in `text` is in `facts` (or `extra`, the template), rounded to the precision it is
    printed with. Small whole numbers (counts, minutes, percentages) and years are allowed."""
    known = [abs(v) for v in _leaves(facts) + _leaves(extra)]
    for m in _NUM.findall(text):
        raw = m.replace(",", "").replace("−", "-").lstrip("+-")
        x = float(raw)
        if x.is_integer() and (x <= 100 or 2000 <= x <= 2100):
            continue
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        tol = 0.5 * 10 ** -decimals + 1e-9  # a number rounded to how it is printed
        if not any(abs(x - k) <= tol for k in known):
            return False
    return True


def split_llm(text: str) -> tuple[str, list[str]]:
    """LLM text → (summary, lessons): lines starting with '- ' or '• ' are lessons."""
    summary, lessons = [], []
    for line in text.strip().splitlines():
        s = line.strip()
        if s[:2] in ("- ", "• ", "* "):
            lessons.append(s[2:].strip())
        elif s:
            summary.append(s)
    return " ".join(summary), [x for x in lessons if x][:MAX_LESSONS]


# ------------------------------------------------------------ weekly review --


def weekly_review(rows: list[tuple[JournalEntry, JournalEvaluation]], now: Optional[float] = None,
                  days: int = 7) -> dict:
    """The closed trades of the last `days` days: stats, best and worst setup types and coins, recurring lessons
    (from the post-mortems), and the text the scheduled message sends."""
    ts = time.time() if now is None else now
    start = ts - days * 86400
    closed = [(e, ev) for e, ev in rows if ev.status == "closed" and ev.closed_at is not None
              and start <= ev.closed_at <= ts]
    st = journal_stats(closed)
    setups, coins = st["by_setup"], st["by_symbol"]
    best_setup = max(setups, key=lambda g: (g["avg_r"], g["trades"]), default=None)
    worst_setup = min(setups, key=lambda g: (g["avg_r"], -g["trades"]), default=None)
    best_coin = max(coins, key=lambda g: (g["total_r"], g["trades"]), default=None)
    worst_coin = min(coins, key=lambda g: (g["total_r"], -g["trades"]), default=None)
    codes: Counter = Counter()
    example: dict[str, str] = {}
    for e, _ in closed:
        pm = e.postmortem
        if pm is None:
            continue
        for code, lesson in zip(pm.lesson_codes, pm.lessons):
            codes[code] += 1
            example.setdefault(code, lesson)
    recurring = [{"code": c, "name": LESSON_NAMES.get(c, c), "count": n, "example": example.get(c, "")}
                 for c, n in codes.most_common() if n >= 2 and c not in ("clean", "normal_loss", "breakeven_saved")]
    trades = sorted(({"id": e.id, "symbol": e.symbol, "setup": e.setup, "direction": e.direction,
                      "realized_r": ev.realized_r, "outcome": ev.outcome, "closed_at": ev.closed_at,
                      "lesson": e.postmortem.lessons[0] if e.postmortem and e.postmortem.lessons else None}
                     for e, ev in closed), key=lambda x: x["closed_at"] or 0, reverse=True)
    out = {
        "from": int(start), "to": int(ts), "days": days, "closed": st["closed"], "wins": st["wins"],
        "losses": st["losses"], "breakeven": st["breakeven"], "win_rate": st["win_rate"], "avg_r": st["avg_r"],
        "total_r": st["total_r"], "best_r": st["best_r"], "worst_r": st["worst_r"],
        "best_setup": best_setup, "worst_setup": worst_setup if worst_setup is not best_setup else None,
        "best_symbol": best_coin, "worst_symbol": worst_coin if worst_coin is not best_coin else None,
        "recurring": recurring, "trades": trades,
        "missing_postmortems": sum(1 for e, _ in closed if e.postmortem is None),
        "open": sum(1 for _, ev in rows if ev.status == "open"),
        "data_source": "synthetic" if any(ev.data_source == "synthetic" for _, ev in closed) else "binance",
    }
    out["text"] = render_weekly(out)
    return out


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d %b")


def render_weekly(w: dict) -> str:
    head = f"Weekly trade review, {_day(w['from'])} – {_day(w['to'])}"
    if not w["closed"]:
        return f"{head}\nNo trades closed in the last {w['days']} days." + (
            f" {w['open']} still open." if w["open"] else "")
    lines = [head, f"{w['closed']} closed: {w['wins']} won, {w['losses']} lost, {w['breakeven']} breakeven "
                   f"({round((w['win_rate'] or 0) * 100)}% win rate). Average {w['avg_r']:+.2f}R, total "
                   f"{w['total_r']:+.2f}R."]
    if w["best_setup"]:
        s = f"Best setup: {w['best_setup']['key']} ({w['best_setup']['trades']}, avg {w['best_setup']['avg_r']:+.2f}R)"
        if w["worst_setup"]:
            s += f". Worst: {w['worst_setup']['key']} ({w['worst_setup']['trades']}, avg {w['worst_setup']['avg_r']:+.2f}R)"
        lines.append(s + ".")
    if w["best_symbol"]:
        s = f"Best coin: {w['best_symbol']['key']} ({w['best_symbol']['total_r']:+.2f}R)"
        if w["worst_symbol"]:
            s += f". Worst: {w['worst_symbol']['key']} ({w['worst_symbol']['total_r']:+.2f}R)"
        lines.append(s + ".")
    if w["recurring"]:
        lines.append("Keeps coming back: " + "; ".join(f"{r['name']} ({r['count']}x)" for r in w["recurring"][:3]) + ".")
        lines.append(f"e.g. {w['recurring'][0]['example']}")
    if w["open"]:
        lines.append(f"{w['open']} trade{'s' if w['open'] != 1 else ''} still open.")
    if w.get("data_source") == "synthetic":
        lines.append("(Some trades were tracked on synthetic demo data.)")
    return "\n".join(lines)


class ReviewSettings(BaseModel):
    """When the weekly review is sent. Mirrors ReviewSettings in frontend/lib/journal.ts."""

    enabled: bool = False
    weekday: int = Field(6, ge=0, le=6, description="0 = Monday … 6 = Sunday")
    time: str = Field("18:00", description="Local send time, HH:MM")
    timezone: str = Field("UTC", description="IANA zone, e.g. Europe/London")
    days: int = Field(7, ge=1, le=31, description="How many days the review covers")

    @field_validator("time")
    @classmethod
    def _time(cls, v: str) -> str:
        m = _TIME.match(v.strip())
        if not m:
            raise ValueError(f"time {v!r} must look like 18:00")
        return f"{int(m.group(1)):02d}:{m.group(2)}"

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, v: str) -> str:
        v = v.strip() or "UTC"
        try:
            _tz(v)
        except Exception as exc:
            raise ValueError(f"unknown time zone {v!r}") from exc
        return v


def weekly_due_slot(cfg: ReviewSettings, now: float, last_slot: float, grace: float = GRACE_SECONDS) -> Optional[float]:
    """The latest weekly send time (UNIX s) that has passed, is newer than `last_slot` and at most `grace` old."""
    tz = _tz(cfg.timezone)
    local = datetime.fromtimestamp(now, tz)
    h, m = (int(x) for x in cfg.time.split(":"))
    d = (local - timedelta(days=(local.weekday() - cfg.weekday) % 7)).date()
    slot = datetime(d.year, d.month, d.day, h, m, tzinfo=tz).timestamp()
    if slot > now:
        d -= timedelta(days=7)
        slot = datetime(d.year, d.month, d.day, h, m, tzinfo=tz).timestamp()
    return slot if last_slot < slot <= now and now - slot <= grace else None


# ----------------------------------------------------------------- service --


class PostMortemService:
    """Writes post-mortems for closed journal trades (in the background, or on request) and runs the weekly
    review's schedule."""

    def __init__(self, journal: JournalService, market: "MarketData", llm: Optional["LLMClient"],
                 alerts: Optional[AlertService], kimi: Optional["KimiService"] = None,
                 settings: Optional[Settings] = None, sweep_seconds: float = SWEEP_SECONDS,
                 check_seconds: float = CHECK_SECONDS) -> None:
        self.journal = journal
        self.market = market
        self.llm = llm
        self.alerts = alerts
        self.kimi = kimi
        self.s = settings or get_settings()
        self.sweep_seconds = sweep_seconds
        self.check_seconds = check_seconds
        self._path = store_path(self.s.journal_review_store)
        data = read_store(self._path) or {}
        try:
            self._settings = ReviewSettings.model_validate(data.get("settings") or {})
        except ValueError as exc:
            log.warning("Invalid weekly review settings in %s (%s); using defaults", self._path, exc)
            self._settings = ReviewSettings()
        self._last_slot = float(data.get("last_slot") or 0.0)
        self._last_sent_at: Optional[int] = data.get("last_sent_at")
        self._busy: dict[str, asyncio.Task] = {}
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------- post-mortems
    def _demo_fallback(self, source: str) -> bool:
        return source == "synthetic" and self.s.data_source != "synthetic"

    @staticmethod
    def stale(e: JournalEntry, ev: JournalEvaluation) -> bool:
        """A closed trade whose post-mortem is missing or was written for a different result."""
        if ev.status != "closed":
            return False
        pm = e.postmortem
        return pm is None or pm.closed_at != ev.closed_at or abs(pm.realized_r - ev.realized_r) > 1e-6

    async def generate(self, entry_id: str) -> Optional[JournalEntry]:
        """Write (or rewrite) the post-mortem of a closed trade → the updated entry, None when it does not exist.
        Raises ValueError for a trade that is not closed, MarketDataError when real candles are unavailable."""
        e = self.journal.get(entry_id)
        if e is None:
            return None
        ev = await self.journal.evaluate(e)
        if ev.status != "closed":
            raise ValueError("Only closed trades get a post-mortem")
        df, source = await self.market.get_range(e.symbol, ev.resolution, e.taken_at)
        if self._demo_fallback(source):
            raise MarketDataError("Binance is unreachable; try again when it is back")
        at_entry, kimi = await asyncio.gather(self._entry_context(e), self._kimi(e, ev))
        facts = trade_facts(e, ev, df, at_entry, kimi)
        lessons = lessons_for(facts)
        summary = render_postmortem(facts)
        texts, engine = [t for _, t in lessons], "template"
        written = await self._write(facts, summary, texts)
        if written:
            summary, texts, engine = written
        pm = PostMortem(generated_at=int(time.time()), engine=engine, summary=summary, lessons=texts,
                        lesson_codes=[c for c, _ in lessons], facts=facts, closed_at=ev.closed_at,
                        realized_r=ev.realized_r, data_source=source)
        return self.journal.set_postmortem(e.id, pm)

    async def _write(self, facts: dict, summary: str, lessons: list[str]) -> Optional[tuple[str, list[str], str]]:
        if self.llm is None or not self.llm.available():
            return None
        user = (f"FACTS: {json.dumps(facts, default=float)}\n\nDRAFT: {summary}\n\nLESSONS:\n"
                + "\n".join(f"- {x}" for x in lessons))
        got = await self.llm.write(POSTMORTEM_SYSTEM, user)
        if not got:
            return None
        text, engine = got
        new_summary, new_lessons = split_llm(text)
        if len(new_summary) < 40 or not new_lessons:
            log.info("Post-mortem: the model's answer had no summary or lessons; using the template")
            return None
        if not grounded(new_summary + "\n" + "\n".join(new_lessons), facts, summary + " " + " ".join(lessons)):
            log.info("Post-mortem: the model's answer used numbers not in the facts; using the template")
            return None
        return new_summary, new_lessons, engine

    async def _entry_context(self, e: JournalEntry) -> Optional[dict]:
        iv = "1h" if e.interval in DERIVED_INTERVALS else e.interval
        step = INTERVAL_SECONDS[iv]
        try:
            df, source = await self.market.get_range(e.symbol, iv, e.taken_at - CONTEXT_BARS * step, e.taken_at)
            if self._demo_fallback(source):
                return None
            df = df[df["time"] + step <= e.taken_at]  # only candles closed by the entry
            return await asyncio.to_thread(entry_context, df, iv)
        except Exception as exc:
            log.info("Post-mortem: levels at entry for %s unavailable: %s", e.symbol, exc)
            return None

    async def _kimi(self, e: JournalEntry, ev: JournalEvaluation) -> Optional[dict]:
        if self.kimi is None or ev.fill_price is None:
            return None
        try:
            k = await asyncio.wait_for(self.kimi.get(e.symbol, e.interval), KIMI_TIMEOUT)
            if self._demo_fallback(k.data_source):
                return None
            return kimi_context(k, e.taken_at, e.interval, ev.fill_price)
        except Exception as exc:
            log.info("Post-mortem: Kimi at entry for %s unavailable: %s", e.symbol, exc)
            return None

    def schedule(self, entry_id: str) -> bool:
        """Generate in the background (once at a time per trade) → whether it was queued; failures are logged."""
        if entry_id in self._busy:
            return False

        async def run() -> None:
            try:
                await self.generate(entry_id)
            except (ValueError, MarketDataError) as exc:
                log.info("Post-mortem for %s skipped: %s", entry_id, exc)
            except Exception:
                log.exception("Post-mortem for %s failed", entry_id)
            finally:
                self._busy.pop(entry_id, None)

        self._busy[entry_id] = asyncio.create_task(run(), name=f"postmortem:{entry_id}")
        return True

    def schedule_missing(self, rows: list[tuple[JournalEntry, JournalEvaluation]]) -> int:
        """Queue every closed trade whose post-mortem is missing or stale → how many were queued."""
        return sum(self.schedule(e.id) for e, ev in rows
                   if self.stale(e, ev) and not self._demo_fallback(ev.data_source) and not ev.error)

    async def sweep(self) -> int:
        return self.schedule_missing(await self.journal.rows())

    # -------------------------------------------------------- weekly review
    @property
    def settings(self) -> ReviewSettings:
        return self._settings

    def status(self) -> dict:
        channels = self.alerts.channel_status if self.alerts else {"telegram": False, "discord": False}
        return {"settings": self._settings.model_dump(), "channels": channels, "last_sent_at": self._last_sent_at}

    def update_settings(self, new: ReviewSettings, now: Optional[float] = None) -> ReviewSettings:
        """Save new settings; a send time that already passed this week waits for next week."""
        self._settings = new
        self._last_slot = max(self._last_slot, time.time() if now is None else now)
        self._save()
        return new

    async def weekly(self, days: Optional[int] = None, now: Optional[float] = None) -> dict:
        return weekly_review(await self.journal.rows(), now, days or self._settings.days)

    async def send_weekly(self, days: Optional[int] = None, now: Optional[float] = None) -> dict:
        """Send the weekly review to Telegram / Discord now → the review plus {results}. Raises NoChannelError."""
        if self.alerts is None or not self.alerts.channels:
            raise NoChannelError("No notification channel is configured. Set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID "
                                 "or DISCORD_WEBHOOK_URL on the backend to receive the weekly review.")
        review = await self.weekly(days, now)
        results = await self.alerts.send_text(review["text"])
        self._last_sent_at = int((time.time() if now is None else now) * 1000)
        self._save()
        self.alerts.record("brief", "", "Weekly trade review", review["text"], time_ms=self._last_sent_at)
        return {**review, "results": results}

    async def tick(self, now: Optional[float] = None) -> bool:
        """Send the weekly review if its time is due → whether it was sent."""
        cfg = self._settings
        ts = time.time() if now is None else now
        if not cfg.enabled:
            return False
        slot = weekly_due_slot(cfg, ts, self._last_slot)
        if slot is None:
            return False
        self._last_slot = slot
        self._save()  # before sending: a restart mid-send never sends this week twice
        try:
            await self.send_weekly(now=ts)
        except NoChannelError:
            log.warning("Weekly trade review due but no Telegram / Discord channel is configured")
            return False
        except Exception:
            log.exception("Scheduled weekly trade review failed")
            return False
        return True

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if not self._tasks:
            self._tasks = [asyncio.create_task(self._loop(self.sweep_seconds, self.sweep), name="postmortem:sweep"),
                           asyncio.create_task(self._loop(self.check_seconds, self.tick), name="postmortem:weekly")]

    async def close(self) -> None:
        for t in [*self._tasks, *self._busy.values()]:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks, self._busy = [], {}

    @staticmethod
    async def _loop(seconds: float, fn) -> None:
        while True:
            await asyncio.sleep(seconds)
            try:
                await fn()
            except Exception:
                log.exception("Post-mortem background check failed")

    def _save(self) -> None:
        write_store(self._path, {"settings": self._settings.model_dump(), "last_slot": self._last_slot,
                                 "last_sent_at": self._last_sent_at})
