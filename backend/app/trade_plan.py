"""Trade plans built only from detected levels: entry at a zone, stop beyond it, targets at the next
opposing levels, reward-to-risk at each target. No price here is invented; risk multiples are used as
targets only when nothing opposes price, and are labelled as such."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .pricefmt import _fmt
from .schemas import BoxOverlay, HorizontalLineOverlay, PlanTarget, TradePlan

SUPPORT_KINDS = {"support", "demand", "window_low", "ob_bullish", "fvg_bullish", "val", "poc", "custom_level",
                 "custom_zone"}
RESIST_KINDS = {"resistance", "supply", "window_high", "ob_bearish", "fvg_bearish", "vah", "poc", "custom_level",
                "custom_zone"}
# Levels worth building an entry on (lines like window lows make weak entries but fine targets).
ENTRY_KINDS = {"support", "demand", "ob_bullish", "resistance", "supply", "ob_bearish", "custom_zone"}

BLUE, RED, GREEN = "#3b82f6", "#ef4444", "#22c55e"


@dataclass
class Level:
    kind: str
    low: float
    high: float
    label: str
    score: float = 0.5
    tests: int | None = None  # supply/demand: returns to the zone since it formed (0 = fresh); None = not tracked
    htf: tuple[str, ...] = ()  # higher timeframes with an overlapping zone ("D1", "W1")

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2


def pick_direction(trend: str, bias: str | None, levels: list[Level], last: float) -> Literal["long", "short"]:
    """Trend and the latest structure break decide; in a range, trade from whichever side price is nearer."""
    if bias == "bullish" or (trend == "up" and bias != "bearish"):
        return "long"
    if bias == "bearish" or (trend == "down" and bias != "bullish"):
        return "short"
    below = [last - lv.high for lv in levels if lv.kind in SUPPORT_KINDS and lv.high <= last]
    above = [lv.low - last for lv in levels if lv.kind in RESIST_KINDS and lv.low >= last]
    return "long" if (min(below, default=float("inf")) <= min(above, default=float("inf"))) else "short"


def build_plan(direction: Literal["long", "short", "auto"], last: float, atr: float, trend: str,
               levels: list[Level], swing_lows: list[float], swing_highs: list[float],
               bias: str | None = None) -> TradePlan | None:
    if atr <= 0 or last <= 0:
        return None
    side = pick_direction(trend, bias, levels, last) if direction == "auto" else direction
    long = side == "long"
    sign = 1 if long else -1
    notes: list[str] = []

    # Entry zone: an entry-grade level on our side of price (or containing it), strongest-near first.
    def dist(lv: Level) -> float:
        edge = lv.high if long else lv.low
        return max(0.0, (last - edge) * sign) / atr

    own = [lv for lv in levels if lv.kind in ENTRY_KINDS and lv.kind in (SUPPORT_KINDS if long else RESIST_KINDS)
           and ((lv.low <= last + 0.1 * atr) if long else (lv.high >= last - 0.1 * atr)) and dist(lv) <= 6]
    zone = max(own, key=lambda lv: lv.score - 0.12 * dist(lv), default=None)
    if zone:
        entry = min(last, zone.high) if long else max(last, zone.low)
        stop = zone.low - 0.25 * atr if long else zone.high + 0.25 * atr
        basis = f"{zone.label} {_fmt(zone.low)}–{_fmt(zone.high)}"
        zone_info: dict = {"zone_kind": zone.kind, "zone_fresh": None if zone.tests is None else zone.tests == 0,
                           "zone_htf": list(zone.htf)}
    else:
        swings = [p for p in swing_lows if p < last] if long else [p for p in swing_highs if p > last]
        if not swings:
            return None
        ref = max(swings) if long else min(swings)
        entry = last
        stop = ref - 0.25 * atr if long else ref + 0.25 * atr
        basis = f"last swing {'low' if long else 'high'} {_fmt(ref)}"
        notes.append("No zone close by, so the entry is at market with the stop beyond the last swing.")
        zone_info = {"zone_kind": "swing"}
    risk = (entry - stop) * sign
    if risk <= 0:
        return None

    # Targets: opposing levels (or swing extremes) beyond both entry and current price, at least 0.8R from
    # entry, one per ~0.3 ATR cluster.
    opp = {(lv.low if long else lv.high): lv.label for lv in levels
           if lv.kind in (RESIST_KINDS if long else SUPPORT_KINDS) and lv is not zone}
    for p in (swing_highs if long else swing_lows):
        opp.setdefault(p, f"swing {'high' if long else 'low'}")
    floor = max(entry, last) if long else min(entry, last)
    prices = sorted(opp.items(), reverse=not long)
    targets: list[PlanTarget] = []
    for price, label in prices:
        if (price - entry) * sign < 0.8 * risk or (price - floor) * sign < 0.2 * atr:
            continue
        if targets and abs(price - targets[-1].price) < 0.3 * atr:
            continue
        targets.append(PlanTarget(price=price, label=f"T{len(targets) + 1} {label}",
                                  rr=round((price - entry) * sign / risk, 2)))
        if len(targets) == 3:
            break
    if not targets:
        mults = [m for m in range(2, 21) if (entry + sign * m * risk - floor) * sign >= 0.2 * atr][:2]
        targets = [PlanTarget(price=entry + sign * m * risk, label=f"T{i + 1} {m}R", rr=float(m))
                   for i, m in enumerate(mults)]
        notes.append("Price is beyond every detected level on that side, so targets are risk multiples.")

    away = abs(entry - last) / atr
    if away > 1.0:
        notes.append(f"Entry is {away:.1f} ATR from price; this is a limit order, wait for price to reach it.")
    if (long and trend == "down") or (not long and trend == "up"):
        notes.append("This is against the current trend.")
    return TradePlan(direction=side, entry=float(f"{entry:.6g}"), stop=float(f"{stop:.6g}"),
                     targets=[t.model_copy(update={"price": float(f"{t.price:.6g}")}) for t in targets], basis=basis,
                     risk_pct=round(risk / entry * 100, 2), notes=notes, **zone_info,
                     zone_low=float(f"{zone.low:.6g}") if zone else None,
                     zone_high=float(f"{zone.high:.6g}") if zone else None)


def plan_overlays(plan: TradePlan, time_start: int) -> list:
    """Entry, stop and target lines plus shaded (unlabelled) risk/reward areas starting at the current bar."""
    last_t = plan.targets[-1].price
    risk_pct = plan.risk_pct
    out: list = [
        BoxOverlay(label="", kind="plan_risk", price_low=min(plan.entry, plan.stop),
                   price_high=max(plan.entry, plan.stop), color="rgba(239, 68, 68, 0.12)", border_color=None,
                   time_start=time_start),
        BoxOverlay(label="", kind="plan_reward", price_low=min(plan.entry, last_t),
                   price_high=max(plan.entry, last_t), color="rgba(34, 197, 94, 0.10)", border_color=None,
                   time_start=time_start),
        HorizontalLineOverlay(label=f"{plan.direction.title()} entry", kind="plan_entry", price=plan.entry,
                              color=BLUE, line_width=2, time_start=time_start),
        HorizontalLineOverlay(label=f"Stop (−{risk_pct}%)", kind="plan_stop", price=plan.stop, color=RED,
                              line_width=2, time_start=time_start),
    ]
    for t in plan.targets:
        out.append(HorizontalLineOverlay(label=f"{t.label.split(' ')[0]} ({t.rr}R)", kind="plan_target",
                                         price=t.price, color=GREEN, line_style="dashed", line_width=2,
                                         time_start=time_start))
    return out
