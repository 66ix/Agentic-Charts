"""An exit plan for a coin you hold: where to sell it in pieces, how much at each level, and where the idea of holding
it is wrong.

Rungs are the resistance, supply and window highs above price (take_profits: this timeframe and the daily), at least
one 4h ATR apart and within +35%; with nothing above, price is in open space and the rungs are spaced by the daily
ATR. Your average entry, when it is above price, is a rung too (break-even). The sizes follow a profile:

* quarters: 25% at each of the first three rungs, 25% left to run;
* thirds: a third at each of the first three;
* cost_out: the first rung sells enough to get your money back, the rest is split over the higher rungs (a first
  rung under your average would sell everything at a loss, so that falls back to quarters).

Rungs worth less than MIN_RUNG_USDT are merged into the next. Coins locked in Simple Earn can't be sold before their
term ends, so they aren't in the sizes. The invalidation is the nearest daily support or demand at least one daily
ATR under price: a daily close below its low says the reason for holding has gone.
"""

from __future__ import annotations

import json
import math
import time
from typing import Literal, Optional

import pandas as pd
from pydantic import BaseModel, Field

from .pricefmt import _fmt
from .schemas import AnalysisIntent
from .ta_agent import analyze, atr, htf_zones

Profile = Literal["quarters", "thirds", "cost_out"]
MAX_TAKE_PROFITS = 4
MAX_GAIN = 0.35          # rungs at most this far above price (your average excepted)
MIN_RUNG_USDT = 10.0     # Binance's minimum order is about $5; smaller rungs are merged
SPOT_KINDS = ("support", "demand")


def take_profits(levels: list, last: float, frames: dict, tfl: str) -> list[dict]:
    """Where to sell coins held: resistance, supply and window highs above price on this timeframe and the next one
    up, nearest first, one per price area."""
    rows = [{"low": lv.low, "high": lv.high, "label": lv.label} for lv in levels
            if lv.kind in ("resistance", "supply", "window_high") and lv.low > last * 1.002]
    for tf, df in list(frames.items())[:1]:
        for z in htf_zones(df):
            if z.kind in ("resistance", "supply") and z.price_low > last * 1.002:
                rows.append({"low": z.price_low, "high": z.price_high, "label": f"{tf.upper()} {z.kind}"})
    rows.sort(key=lambda r: r["low"])
    out: list[dict] = []
    for r in rows:
        if out and r["low"] <= out[-1]["high"]:  # overlaps the previous one: same area
            out[-1]["label"] += f" + {r['label']}"
            continue
        out.append({**r, "gain_pct": round((r["low"] / last - 1) * 100, 2)})
        if len(out) == MAX_TAKE_PROFITS:
            break
    return out


class ExitRung(BaseModel):
    price: float
    low: float
    high: float
    label: str
    sell_qty: float
    sell_pct: float = Field(..., description="Share of the coins held")
    usdt: float
    gain_from_now_pct: float
    pnl_vs_entry_pct: Optional[float] = None
    realizes_usd: Optional[float] = Field(None, description="Gain (or loss) of this rung against your average")
    done: bool = False


class Invalidation(BaseModel):
    price: float
    label: str
    rule: str = "daily close below"


class ExitPlan(BaseModel):
    symbol: str
    profile: Profile
    qty: float
    sellable_qty: float
    avg_entry: Optional[float] = None
    price: float
    rungs: list[ExitRung]
    runner_qty: float = Field(0.0, description="Coins the plan leaves to run")
    invalidation: Optional[Invalidation] = None
    notes: list[str] = Field(default_factory=list)
    created_at: int = 0
    armed: list[str] = Field(default_factory=list, description="Ids of the price alerts armed for it")


def _round_qty(q: float) -> float:
    """Down to 6 significant digits: never more coins than you hold."""
    if q <= 0:
        return 0.0
    d = 5 - math.floor(math.log10(q))
    return math.floor(q * 10 ** d) / 10 ** d


def _shares(profile: Profile, n: int) -> list[float]:
    """Share of the sellable coins at each of `n` rungs."""
    if n <= 0:
        return []
    if profile == "thirds":
        k = min(3, n)
        return [1 / k] * k + [0.0] * (n - k)
    k = min(3, n)
    return [0.75 / k] * k + [0.0] * (n - k)  # quarters: 25% each over three rungs, a quarter to run


def build_exit_plan(symbol: str, qty: float, sellable: float, avg_entry: Optional[float], df4: pd.DataFrame,
                    df1: pd.DataFrame, profile: Profile = "quarters", now: Optional[float] = None) -> ExitPlan:
    """The exit plan (pure, CPU-bound): `df4` 4h and `df1` daily candles, closed ones."""
    df4, df1 = df4.reset_index(drop=True), df1.reset_index(drop=True)
    last = float(df4["close"].iloc[-1])
    a4 = float(atr(df4).iloc[-1])
    a1 = float(atr(df1).iloc[-1]) if len(df1) > 15 else a4 * 4
    notes: list[str] = []
    res = analyze(df4, AnalysisIntent(features=["support_resistance", "supply_demand", "window_levels"]), "4h", None,
                  {"1d": df1} if len(df1) >= 30 else {})
    rows = take_profits(res.levels, last, {"1d": df1} if len(df1) >= 30 else {}, "H4")
    levels: list[dict] = []
    floor = last + 0.5 * a4
    for r in rows:
        if r["low"] < floor or r["low"] > last * (1 + MAX_GAIN):
            continue
        levels.append({"price": r["low"], "low": r["low"], "high": r["high"], "label": r["label"]})
        floor = r["low"] + a4
    if not levels:
        levels = [{"price": last + k * 2 * a1, "low": last + k * 2 * a1, "high": last + k * 2 * a1,
                   "label": f"{2 * k} daily ATR up"} for k in (1, 2, 3)]
        notes.append("No resistance above within reach: price is in open space, so the rungs are spaced by the "
                     "daily ATR.")
    if avg_entry and avg_entry > last and all(abs(lv["price"] - avg_entry) > a4 for lv in levels):
        levels.append({"price": avg_entry, "low": avg_entry, "high": avg_entry, "label": "your average (break-even)"})
        levels.sort(key=lambda lv: lv["price"])
    if avg_entry and avg_entry > last:
        notes.append(f"Under water: price is {(1 - last / avg_entry) * 100:.1f}% below your average "
                     f"{_fmt(avg_entry)}; the rungs under it trim into resistance at a loss.")

    n = len(levels)
    if profile == "cost_out":
        first = levels[0]["price"]
        if not avg_entry or first < avg_entry:
            notes.append("Money back first needs a first rung above your average; using quarters instead.")
            profile = "quarters"
            shares = _shares("quarters", n)
            qtys = [sellable * sh for sh in shares]
        else:
            back = min(sellable, qty * avg_entry / first)
            rest = sellable - back
            qtys = [back] + ([rest / (n - 1)] * (n - 1) if n > 1 else [])
    else:
        qtys = [sellable * sh for sh in _shares(profile, n)]

    # Merge rungs too small to sell into the next one up (the last one into the one before it).
    merged: list[tuple[dict, float]] = []
    carry = 0.0
    for lv, q in zip(levels, qtys):
        q += carry
        carry = 0.0
        if q * lv["price"] < MIN_RUNG_USDT and q > 0:
            carry = q
            continue
        merged.append((lv, q))
    if carry and merged:
        lv, q = merged[-1]
        merged[-1] = (lv, q + carry)

    rungs = []
    for lv, q in merged:
        q = _round_qty(q)
        if q <= 0:
            continue
        p = lv["price"]
        rungs.append(ExitRung(
            price=p, low=lv["low"], high=lv["high"], label=lv["label"], sell_qty=q,
            sell_pct=round(q / qty * 100, 1) if qty else 0.0, usdt=round(q * p, 2),
            gain_from_now_pct=round((p / last - 1) * 100, 2),
            pnl_vs_entry_pct=round((p / avg_entry - 1) * 100, 2) if avg_entry else None,
            realizes_usd=round(q * (p - avg_entry), 2) if avg_entry else None))
    runner = _round_qty(max(0.0, sellable - sum(r.sell_qty for r in rungs)))
    if sellable < qty:
        notes.append(f"{_round_qty(qty - sellable):g} of your {qty:g} are locked in Simple Earn and left out until "
                     "the term ends.")

    inv = None
    from .sell_check import _zones  # imported here: sell_check pulls in the scanner

    if len(df1) >= 30:
        zones, _ = _zones(df1)
        below = [z for z in zones if z.kind in SPOT_KINDS and z.price_high <= last - a1]
        if below:
            z = max(below, key=lambda z: z.price_high)
            inv = Invalidation(price=z.price_low, label=f"D1 {z.kind} {_fmt(z.price_low)}–{_fmt(z.price_high)}")
        else:
            notes.append("No daily support at least one daily ATR below price: no line where holding is wrong yet.")
    return ExitPlan(symbol=symbol, profile=profile, qty=qty, sellable_qty=sellable, avg_entry=avg_entry, price=last,
                    rungs=rungs, runner_qty=runner, invalidation=inv, notes=notes,
                    created_at=int(time.time() if now is None else now))


def describe_exit_plan(plan: ExitPlan) -> str:
    coin = plan.symbol.removesuffix("USDT")
    parts = [f"{r.label.split(' (')[0]} {_fmt(r.price)}: sell {r.sell_pct:g}% ≈ {r.sell_qty:g} {coin} ≈ ${r.usdt:,.0f}"
             + (f", {r.pnl_vs_entry_pct:+g}% on your {_fmt(plan.avg_entry)} entry" if r.pnl_vs_entry_pct is not None
                else "") for r in plan.rungs]
    out = f"Exit plan for your {plan.qty:g} {coin}: " + "; ".join(parts) + "."
    if plan.runner_qty:
        out += f" {plan.runner_qty:g} {coin} left to run."
    if plan.invalidation:
        out += f" Wrong on a daily close below {_fmt(plan.invalidation.price)} ({plan.invalidation.label})."
    return out + (" " + " ".join(plan.notes) if plan.notes else "")


def plan_json(plan: ExitPlan) -> str:
    return json.dumps(plan.model_dump())


# ------------------------------------------------------------------ service --


async def plan_from_market(market, symbol: str, qty: float, sellable: float, avg_entry: Optional[float],
                           profile: Profile = "quarters") -> ExitPlan:
    """build_exit_plan on the closed 4h and daily candles. ValueError when there is too little history or the
    holding is dust."""
    import asyncio

    from .kimi_service import closed_only
    from .market_data import candles_to_df
    from .ta_agent import ZONE_BARS

    (c4, _), (c1, _) = await asyncio.gather(market.get_klines(symbol, "4h", ZONE_BARS + 1),
                                            market.get_klines(symbol, "1d", 400))
    df4 = candles_to_df(closed_only(c4, "4h"))
    df1 = candles_to_df(closed_only(c1, "1d"))
    if len(df4) < 60:
        raise ValueError(f"Not enough candles for {symbol}")
    if qty * float(df4["close"].iloc[-1]) < MIN_RUNG_USDT:
        raise ValueError(f"Your {symbol} is worth under ${MIN_RUNG_USDT:g}: dust, no plan")
    return await asyncio.to_thread(build_exit_plan, symbol, qty, sellable, avg_entry, df4, df1, profile)

SCHEMA = ["""CREATE TABLE exit_plans (symbol TEXT PRIMARY KEY, body TEXT NOT NULL, updated_at INTEGER NOT NULL)"""]
DONE_TOL = 0.005  # a sell of yours within 0.5% of a rung's price, after the plan was made, marks the rung done


class ExitPlanService:
    """Builds, keeps (SQLite) and arms exit plans. `binance` (binance_import) gives the holding and your sells."""

    def __init__(self, market, db, alerts=None, binance=None) -> None:
        self.market, self.db, self.alerts, self.binance = market, db, alerts, binance
        db.migrate("exit_plans", SCHEMA)

    async def _holding(self, symbol: str) -> Optional[dict]:
        if self.binance is None or not self.binance.account.status().get("configured"):
            return None
        try:
            pos = await self.binance.positions()
        except Exception:
            return None
        return next((h for h in pos.get("manual", {}).get("holdings", []) if h["symbol"] == symbol), None)

    async def build(self, symbol: str, profile: Profile = "quarters", qty: Optional[float] = None,
                    avg_entry: Optional[float] = None, save: bool = True) -> ExitPlan:
        """The plan for `symbol`: your Binance holding (spot + Earn, locked coins left out), or `qty` / `avg_entry`
        given by hand. Raises ValueError without a holding or with one worth under MIN_RUNG_USDT."""
        h = await self._holding(symbol)
        held = qty if qty is not None else (h or {}).get("qty")
        if not held or held <= 0:
            raise ValueError(f"No holding of {symbol} to plan an exit for: add a read-only key or give the amount")
        locked = (h or {}).get("locked_qty", 0.0) if qty is None else 0.0
        avg = avg_entry if avg_entry is not None else (h or {}).get("avg_entry")
        plan = await plan_from_market(self.market, symbol, held, max(0.0, held - locked), avg, profile)
        if save:
            old = self.get(symbol)
            if old is not None:
                plan.armed = old.armed  # still armed until re-armed
            self._save(plan)
        return plan

    def _save(self, plan: ExitPlan) -> None:
        self.db.execute("INSERT OR REPLACE INTO exit_plans (symbol, body, updated_at) VALUES (?, ?, ?)",
                        (plan.symbol, plan_json(plan), int(time.time())))

    def get(self, symbol: str) -> Optional[ExitPlan]:
        row = self.db.one("SELECT body FROM exit_plans WHERE symbol = ?", (symbol,))
        return self._with_done(ExitPlan.model_validate_json(row["body"])) if row else None

    def list(self) -> list[ExitPlan]:
        return [self._with_done(ExitPlan.model_validate_json(r["body"]))
                for r in self.db.query("SELECT body FROM exit_plans ORDER BY updated_at DESC")]

    def _with_done(self, plan: ExitPlan) -> ExitPlan:
        """Rungs you sold at (a manual spot sell within DONE_TOL of the price, after the plan was made) are done."""
        fills = getattr(self.binance, "_fills", {}) if self.binance is not None else {}
        sells = [f.price for f in fills.values() if f.market == "spot" and f.side == "sell" and f.kind == "manual"
                 and f.symbol == plan.symbol and f.time >= plan.created_at]
        for r in plan.rungs:
            r.done = any(abs(p / r.price - 1) <= DONE_TOL for p in sells)
        return plan

    async def delete(self, symbol: str) -> bool:
        plan = self.get(symbol)
        if plan is None:
            return False
        await self._disarm(plan)
        self.db.execute("DELETE FROM exit_plans WHERE symbol = ?", (symbol,))
        return True

    async def _disarm(self, plan: ExitPlan) -> None:
        if self.alerts is None:
            return
        for aid in plan.armed:
            await self.alerts.remove(aid)
        plan.armed = []

    async def arm(self, symbol: str) -> ExitPlan:
        """One price alert per rung not done yet, and one at the invalidation (labelled to check the daily close);
        arming again replaces the previous ones."""
        from .schemas import AlertSpec

        plan = self.get(symbol)
        if plan is None:
            raise ValueError(f"No exit plan for {symbol}")
        if self.alerts is None:
            raise ValueError("Alerts are not available")
        await self._disarm(plan)
        coin = symbol.removesuffix("USDT")
        specs = [AlertSpec(kind="cross", price=r.price, repeat=False,
                           label=f"Exit {coin} TP{i + 1}: sell {r.sell_pct:g}% ≈ ${r.usdt:,.0f}")
                 for i, r in enumerate(plan.rungs) if not r.done]
        if plan.invalidation:
            specs.append(AlertSpec(kind="cross", price=plan.invalidation.price, repeat=False,
                                   label=f"Exit {coin}: wrong on a daily close below "
                                         f"{_fmt(plan.invalidation.price)}; check the daily close"))
        made = await self.alerts.add(symbol, specs)
        plan.armed = [a.id for a in made]
        self._save(plan)
        return plan
