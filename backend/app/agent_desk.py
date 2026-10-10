"""The agent desk: spot buy calls the agent makes on its own, at every 1h, 4h and 1d candle close, on the coins you
follow; scored as the candles come in, traded in a paper wallet of its own, and learned from.

On each closed candle of a timeframe (CLOSE_DELAY after the close, so Binance has the final bar) every coin is
analysed with the chart agent's detectors (desk_calls.candidates): each support or demand zone at or below price
becomes a possible call with a limit buy at the top of the zone, an invalidation just below it and a take-profit at
the next resistance. desk_learning.py gives each its confidence (from the coin's backtest and the desk's own past
calls) and, once there is enough history, a learned take-profit. The call with the best expected R is made when its
confidence is at least the minimum, its expected R after fees is positive and the coin has no call still running on
that timeframe. A zone recorded in the last 2 x ENTRY_BARS candles, or still waiting, isn't recorded again, except a
watched zone skipped for capacity or a running call, which is called once there is room while it still waits.

Each call gets a paper position in the desk's own wallet (AGENT_WALLET_CASH, 1000 USDT by default; paper.py rules):
sized so it risks a Kelly-scaled share of the wallet at its invalidation, after fees (desk_calls.size_for). The limit
buy, the take-profit sell and the stop-sell go in together as one group: a sell reached before the buy fills waits and only counts candles after
the fill, and whichever sell fills first cancels the other. The group is cancelled when the call expires unfilled, and
a held position is sold at market when the hold window runs out. The wallet is replayed on 1m candles and the call
is scored on 5m-1h candles, so the two can differ by a candle.

Every other new buy zone it looked at is watched rather than called (`shadow`): no paper trade, no message, but it
is scored the same way and learned from, so the desk keeps learning about setups it doesn't trade yet, including ones
whose backtest shows no edge. Zones recorded in the last 2 x ENTRY_BARS candles are skipped.

Scoring runs every SCORE_SECONDS on the calls still running (desk_calls.score_call). New calls, fills and results go
to Telegram / Discord (each can be turned off) and to the alert history.

Storage: the calls and settings live in SQLite (db.py). A candle more than STALE_FRACTION of its timeframe old (after a
restart, say) is skipped rather than called late; "Run now" calls the latest candle whatever its age.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from .config import Settings, get_settings
from .db import Database
from .desk_calls import (ACTIVE, CLOSED, DEFAULT_TIMEFRAMES, DESK_TIMEFRAMES, ENTRY_BARS, MIN_EXPECTED_R, SCORE_RES, Candidate, DeskCall,
                         call_text, candidates, expected_r, kelly, new_call, outcome_text, score_call, size_for)
from .desk_learning import (RECENT_DAYS, bucket_table, calibration, estimate, learnable, take_profit, verdict,
                            working_now)
from .chart_render import snapshot
from .jobs import jobs
from .notify import Notice, app_link
from .pricefmt import _fmt as fmt_price
from .kimi_service import closed_only
from .market_data import INTERVAL_SECONDS, MarketData, candles_to_df
from .paper import NewPaperOrder, PaperService
from .scanner import DEFAULT_WATCHLIST
from .schemas import TrackRecord, norm_symbol
from .ta_agent import ZONE_BARS
from .ta_agent import TF_LABEL, higher_timeframes

if TYPE_CHECKING:
    from .alerts import AlertService
    from .track_record import TrackRecordService

log = logging.getLogger(__name__)

CHECK_SECONDS = 30.0
SCORE_SECONDS = 120.0
CLOSE_DELAY = 45.0
STALE_FRACTION = 0.25
CANDLES = ZONE_BARS
MIN_CANDLES = 120
CONCURRENCY = 3
TRACK_TIMEOUT = 30.0
PROMOTABLE = ("capacity", "running call")  # watched for these reasons, a still-waiting zone can be called later
MAX_SYMBOLS = 40
KEEP_WATCHED_DAYS = 365   # watched zones older than this count for ~6% at a 90-day half-life: dropped once a day
PRUNE_SECONDS = 86400.0

SCHEMA = [
    """CREATE TABLE desk_calls (
        id TEXT PRIMARY KEY,
        symbol TEXT NOT NULL,
        interval TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        closed_at INTEGER,
        bucket TEXT NOT NULL,
        data_source TEXT NOT NULL,
        body TEXT NOT NULL
    );
    CREATE INDEX desk_calls_status ON desk_calls (status);
    CREATE INDEX desk_calls_symbol ON desk_calls (symbol, interval, created_at);
    CREATE TABLE desk_kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
]


class DeskSettings(BaseModel):
    """Mirrors DeskSettings in frontend/lib/desk.ts."""

    enabled: bool = True
    timeframes: list[Literal["15m", "30m", "1h", "4h", "1d", "1w"]] = Field(
        default_factory=lambda: list(DEFAULT_TIMEFRAMES))
    follow_watchlist: bool = Field(True, description="The app keeps `symbols` in step with your active watchlist")
    symbols: list[str] = Field(default_factory=list, max_length=MAX_SYMBOLS, description="Empty = the default list")
    min_confidence: float = Field(0.25, ge=0.1, le=0.9, description="Calls whose take-profit is less likely aren't made")
    min_expected_r: float = Field(MIN_EXPECTED_R, ge=0.0, le=2.0,
                                  description="Calls must expect at least this much R after fees")
    max_active: int = Field(30, ge=1, le=200, description="Calls running at once, across coins and timeframes "
                                                          "(watched zones are not limited)")
    arm_triggers: bool = Field(True, description="Each new call arms a lower-timeframe confirmation alert in its zone")
    notify_new: bool = True
    notify_fills: bool = True
    notify_results: bool = True
    notify_expired: bool = False

    @field_validator("symbols")
    @classmethod
    def _symbols(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for s in v:
            n = norm_symbol(s)
            if n.isalnum() and 5 <= len(n) <= 20 and n not in out:
                out.append(n)
        return out[:MAX_SYMBOLS]

    @field_validator("timeframes")
    @classmethod
    def _timeframes(cls, v: list[str]) -> list[str]:
        return [tf for tf in DESK_TIMEFRAMES if tf in v]


def due_bars(timeframes: list[str], done: dict[str, int], now: float, delay: float = CLOSE_DELAY) -> list[tuple[str, int]]:
    """(timeframe, open time of the candle that just closed) for each timeframe whose latest closed candle hasn't been
    looked at, once `delay` seconds have passed since it closed."""
    out = []
    for tf in timeframes:
        step = INTERVAL_SECONDS[tf]
        current = bar_open(tf, now)
        if now - current < delay:
            current -= step  # the newest close is too fresh: Binance may not have the final bar yet
        closed = current - step
        if done.get(tf, -1) < closed:
            out.append((tf, closed))
    return out


WEEK_OFFSET = 4 * 86400  # UNIX time 0 was a Thursday; Binance's weekly candles open on Monday 00:00 UTC


def bar_open(tf: str, t: float) -> int:
    """Open time of the `tf` candle containing `t` (weekly candles open on Mondays, not on epoch Thursdays)."""
    step = INTERVAL_SECONDS[tf]
    off = WEEK_OFFSET if tf == "1w" else 0
    return int((t - off) // step * step + off)


def stale(tf: str, bar_time: int, now: float) -> bool:
    step = INTERVAL_SECONDS[tf]
    return now - (bar_time + step) > max(STALE_FRACTION * step, 4 * CLOSE_DELAY)


# The lower timeframe whose confirmation candle a call's zone trigger waits for.
TRIGGER_TF = {"15m": "5m", "30m": "5m", "1h": "5m", "4h": "15m", "1d": "15m", "1w": "15m"}


def trigger_owner(call_id: str) -> str:
    return f"desk:{call_id}"


@dataclass
class CoinResult:
    call: Optional[DeskCall] = None
    watched: int = 0
    zones: int = 0
    best: Optional[dict] = None


class AgentDesk:
    def __init__(self, market: MarketData, db: Database, alerts: Optional["AlertService"],
                 track: Optional["TrackRecordService"], paper: Optional[PaperService] = None,
                 settings: Optional[Settings] = None, check_seconds: float = CHECK_SECONDS,
                 score_seconds: float = SCORE_SECONDS) -> None:
        self.market = market
        self.db = db
        self.alerts = alerts
        self.track = track
        self.s = settings or get_settings()
        self.paper = paper or PaperService(market, self.s, store=self.s.agent_paper_store,
                                           start_cash=self.s.agent_wallet_cash)
        self.check_seconds = check_seconds
        self.score_seconds = score_seconds
        self.db.migrate("agent_desk", SCHEMA)
        self.settings = DeskSettings.model_validate(self._kv("settings") or {})
        self._done: dict[str, int] = {k: int(v) for k, v in (self._kv("done") or {}).items()}
        self._calls: dict[str, DeskCall] = {}
        for row in self.db.query("SELECT body FROM desk_calls"):
            try:
                c = DeskCall.model_validate_json(row["body"])
                self._calls[c.id] = c
            except ValueError as exc:
                log.warning("Skipping an unreadable desk call: %s", exc)
        self._lock = asyncio.Lock()       # one timeframe run at a time (Binance weight, CPU)
        self._score_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []
        self._scored_at = 0.0
        self._pruned_at = 0.0
        self.last_run: dict[str, dict] = (self._kv("last_run") or {})
        self.signals = None  # SignalAlertService, set by attach_signals: zone triggers for calls

    # ------------------------------------------------------------------ storage
    def _kv(self, key: str):
        row = self.db.one("SELECT value FROM desk_kv WHERE key = ?", (key,))
        return json.loads(row["value"]) if row else None

    def _set_kv(self, key: str, value) -> None:
        self.db.execute("INSERT INTO desk_kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = "
                        "excluded.value", (key, json.dumps(value)))

    def _save(self, calls: list[DeskCall]) -> None:
        self.db.executemany(
            "INSERT INTO desk_calls (id, symbol, interval, status, created_at, closed_at, bucket, data_source, body) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET status = excluded.status, "
            "closed_at = excluded.closed_at, body = excluded.body",
            [(c.id, c.symbol, c.interval, c.status, c.created_at, c.closed_at, c.bucket, c.data_source,
              c.model_dump_json()) for c in calls])

    # -------------------------------------------------------------------- reads
    @property
    def demo_ok(self) -> bool:
        return self.s.data_source == "synthetic"

    def symbols(self) -> list[str]:
        return self.settings.symbols or list(DEFAULT_WATCHLIST)

    def calls(self, status: Optional[str] = None, symbol: Optional[str] = None, limit: int = 200,
              watched: bool = False) -> list[DeskCall]:
        """Calls (or with `watched`, the zones watched instead), newest first. `status`: active, closed, or one."""
        rows = [c for c in self._calls.values() if c.shadow == watched and (not symbol or c.symbol == symbol) and (
            not status or (c.status in ACTIVE if status == "active" else c.status in CLOSED if status == "closed"
                           else c.status == status))]
        rows.sort(key=lambda c: -c.created_at)
        return rows[:limit]

    def _real(self) -> list[DeskCall]:
        return [c for c in self._calls.values() if self.demo_ok or c.data_source == "binance"]

    def summary(self, now: Optional[float] = None) -> dict:
        """The calls' record, and what the desk learns from: calls and watched zones together."""
        now = time.time() if now is None else now
        tracked = self._real()
        rows = [c for c in tracked if not c.shadow]
        closed = [c for c in rows if c.status in CLOSED]
        rs = [c.r for c in closed if c.r is not None]
        cal = calibration(tracked, self.demo_ok)
        learned = learnable(tracked, self.demo_ok)
        return {"tracked": len(tracked), "watching": sum(c.shadow and c.status in ACTIVE for c in tracked),
                "learned_from": len(learned), "level_hits": sum(bool(c.level_hit) for c in learned),
                "calls": len(rows), "active": sum(c.status in ACTIVE for c in rows),
                "waiting": sum(c.status == "waiting" for c in rows), "open": sum(c.status == "open" for c in rows),
                "closed": len(closed), "tp": sum(c.status == "tp" for c in closed),
                "invalidated": sum(c.status == "invalidated" for c in closed),
                "timed_out": sum(c.status == "timed_out" for c in closed),
                "expired": sum(c.status == "expired" for c in rows),
                "avg_r": round(sum(rs) / len(rs), 3) if rs else None, "total_r": round(sum(rs), 3) if rs else None,
                "calibration": cal, "verdict": verdict(cal), "setups": bucket_table(tracked, now, self.demo_ok),
                "working_now": working_now(tracked, now, RECENT_DAYS, self.demo_ok), "recent_days": RECENT_DAYS}

    def status(self) -> dict:
        now = time.time()
        nxt = {}
        for tf in self.settings.timeframes:
            step = INTERVAL_SECONDS[tf]
            nxt[tf] = bar_open(tf, now) + step + int(CLOSE_DELAY)
        return {"settings": self.settings.model_dump(), "symbols": self.symbols(), "enabled_by_server": self.s.agent_desk,
                "running": self._lock.locked(), "last_run": self.last_run, "next_run": nxt,
                "summary": self.summary(now), "channels": self.alerts.channel_status if self.alerts else {}}

    # ------------------------------------------------------------------- writes
    def update_settings(self, new: DeskSettings) -> dict:
        self.settings = new
        self._set_kv("settings", new.model_dump())
        jobs.set_enabled("agent_desk", self.s.agent_desk and new.enabled)
        return self.status()

    def follow(self, symbols: list[str]) -> dict:
        """The app's active watchlist changed: follow it when following is on."""
        if self.settings.follow_watchlist:
            self.update_settings(self.settings.model_copy(update={
                "symbols": DeskSettings(symbols=symbols).symbols}))
        return self.status()

    # ------------------------------------------------------------- making calls
    async def run_interval(self, interval: str, bar_time: Optional[int] = None, now: Optional[float] = None,
                           force: bool = False) -> list[DeskCall]:
        """Look at every coin on the candle of `interval` that opened at `bar_time` (default: the latest closed one)
        and make the calls worth making → the new calls."""
        if interval not in DESK_TIMEFRAMES:
            raise ValueError(f"the desk calls on {', '.join(DESK_TIMEFRAMES)}")
        async with self._lock:
            now = time.time() if now is None else now
            step = INTERVAL_SECONDS[interval]
            bar = bar_time if bar_time is not None else bar_open(interval, now) - step
            self._done[interval] = max(self._done.get(interval, -1), bar)
            self._set_kv("done", self._done)
            if not force and stale(interval, bar, now):
                log.info("Desk: skipped the %s candle of %s, it closed too long ago", interval, bar)
                return []
            started = time.perf_counter()
            sem = asyncio.Semaphore(CONCURRENCY)
            results: list[CoinResult] = []

            async def one(sym: str) -> None:
                async with sem:
                    try:
                        results.append(await self._coin(sym, interval, bar, now))
                    except Exception as exc:  # one coin's trouble must not stop the rest
                        log.info("Desk: %s %s failed: %s", sym, interval, exc)

            await asyncio.gather(*(one(s) for s in self.symbols()))
            made = [r.call for r in results if r.call is not None]
            best = max((r.best for r in results if r.best), key=lambda b: b["expected_r"], default=None)
            self.last_run[interval] = {
                "at": int(now), "bar_time": bar, "coins": len(results), "zones": sum(r.zones for r in results),
                "calls": len(made), "watched": sum(r.watched for r in results), "best": best,
                "seconds": round(time.perf_counter() - started, 1)}
            self._set_kv("last_run", self.last_run)
        for c in made:
            await self._notify(c, "new")
            await self._arm_trigger(c)
        if made and self.alerts is not None:
            self.alerts.broadcast({"type": "desk_scored", "changed": [c.id for c in made]})
        return made

    # ---------------------------------------------------------- zone triggers
    def attach_signals(self, signals) -> None:
        """Calls arm a confirmation trigger in their zone through `signals` (SignalAlertService)."""
        self.signals = signals
        signals.register_context("desk:", self._trigger_context)

    async def _arm_trigger(self, c: DeskCall) -> None:
        """A lower-timeframe confirmation inside the call's buy zone: the second ping, when price actually reacts
        there. It stays through waiting and open and goes when the call ends (score)."""
        if self.signals is None or c.shadow or not self.settings.arm_triggers:
            return
        from .schemas import TriggerZone, ZoneTriggerSpec

        tf = TF_LABEL.get(c.interval, c.interval)
        spec = ZoneTriggerSpec(symbol=c.symbol, interval=TRIGGER_TF.get(c.interval, "15m"), confirm="any",
                               zone=TriggerZone(source="fixed", price_low=c.zone_low, price_high=c.zone_high,
                                                direction="long", timeframe=c.interval, label=f"Desk {tf} buy zone"),
                               cooldown_min=60, repeat=False)
        try:
            await self.signals.add_trigger(spec, owner=trigger_owner(c.id))
        except ValueError as exc:  # e.g. the alert limit: the call stands without it
            log.info("Desk: no zone trigger for %s: %s", c.symbol, exc)

    async def _disarm_trigger(self, c: DeskCall) -> None:
        if self.signals is not None:
            await self.signals.remove_owned(trigger_owner(c.id))

    def _trigger_context(self, alert) -> str:
        """Where the call stands, for its trigger's fire text. Never the desk's odds: they were measured for the
        limit at the zone top, not for an entry on this confirmation."""
        c = self._calls.get((alert.owner or "").removeprefix("desk:"))
        if c is None:
            return ""
        tf = TF_LABEL.get(c.interval, c.interval)
        where = (f"filled at {fmt_price(c.fill_price or c.entry)}" if c.status == "open"
                 else f"still waiting at {fmt_price(c.entry)}" if c.status == "waiting" else c.status.replace("_", " "))
        return (f"Inside the desk's {tf} buy zone {fmt_price(c.zone_low)}–{fmt_price(c.zone_high)}; desk limit {where}, "
                f"take-profit {fmt_price(c.tp)}, wrong below {fmt_price(c.stop)}")

    async def _coin(self, symbol: str, interval: str, bar: int, now: float) -> "CoinResult":
        """One coin on one candle: the call worth making (if any) and the other new zones, watched."""
        step = INTERVAL_SECONDS[interval]
        candles, source = await self.market.get_klines(symbol, interval, CANDLES + 1)
        if source == "synthetic" and not self.demo_ok:
            return CoinResult()  # demo candles during a Binance outage: never call or learn on fake prices
        live = float(candles[-1].close) if candles else 0.0
        closed = [c for c in candles if c.time <= bar]
        if len(closed) < MIN_CANDLES or live <= 0:
            return CoinResult()
        htfs = higher_timeframes(interval)
        higher = await asyncio.gather(*(self.market.get_klines(symbol, h, CANDLES) for h in htfs),
                                      return_exceptions=True)
        # Closed higher-timeframe candles only: the open D1/W1 bar would move a zone between buckets hour to hour.
        closed_htf = {h: closed_only(r[0], h, now) for h, r in zip(htfs, higher)
                      if not isinstance(r, BaseException) and r[1] == source}
        frames = {h: candles_to_df(c) for h, c in closed_htf.items() if len(c) >= 60}
        cands = await asyncio.to_thread(candidates, symbol, interval, candles_to_df(closed), frames)
        # A zone recorded in the last 2 x ENTRY_BARS candles (or still waiting) isn't recorded again, except a watched
        # zone skipped only for capacity or a running call: that one can still be called while it waits.
        repeat = 2 * ENTRY_BARS[interval] * step
        recent = [c for c in self._calls.values() if c.symbol == symbol and c.interval == interval
                  and (c.created_at >= now - repeat or c.status in ACTIVE)]

        def overlaps(c: Candidate, r: DeskCall) -> bool:
            return min(c.zone.high, r.zone_high) > max(c.zone.low, r.zone_low)

        def promotable(r: DeskCall) -> bool:
            return r.shadow and r.status == "waiting" and r.skip_reason in PROMOTABLE and "promoted_to" not in r.features

        cands = [c for c in cands if live > c.plan.stop and not any(
            overlaps(c, r) for r in recent if not promotable(r))]
        if not cands:
            return CoinResult()
        history = learnable(self._calls.values(), self.demo_ok)
        tracks: dict[str, Optional[TrackRecord]] = {}
        scored: list[tuple[float, bool, Candidate, float, str, float, float, str]] = []
        for c in cands:
            if c.zone.kind not in tracks:
                tracks[c.zone.kind] = await self._track(symbol, interval, c)
            risk = c.plan.entry - c.plan.stop
            rr_level = (c.tp_level - c.plan.entry) / risk if risk > 0 else 0.0
            est = estimate(symbol, interval, c.zone.kind, c.bucket, rr_level, tracks[c.zone.kind], history, now,
                           self.demo_ok)
            tp = take_profit(c.plan.entry, c.plan.stop, c.tp_level, est.p, c.plan.risk_pct, interval, c.zone.kind,
                             history, now)
            rr = (tp.price - c.plan.entry) / risk if risk > 0 else 0.0
            er = expected_r(tp.p, rr, c.plan.risk_pct)
            why = ("confidence" if tp.p < self.settings.min_confidence else
                   "expected R" if er < self.settings.min_expected_r or kelly(tp.p, rr, c.plan.risk_pct) <= 0 else
                   "reward:risk" if rr < 1.0 else "")
            scored.append((er, not why, c, tp.p, est.basis + (f" {tp.note}" if tp.note else ""), tp.price,
                           tp.fraction, why))
        scored.sort(key=lambda x: -x[0])

        def room() -> tuple[bool, bool]:
            """(can call, wallet full). Checked right before each decision, with no await until the call is
            registered: coins run concurrently, so a check made earlier could let two calls past max_active."""
            running = [c for c in self._calls.values() if not c.shadow and c.status in ACTIVE]
            full = len(running) >= self.settings.max_active
            return (not full and not any(c.symbol == symbol and c.interval == interval for c in running)), full

        out = CoinResult(zones=len(scored), best={"symbol": symbol, "interval": interval, "setup": scored[0][2].setup,
                                                  "expected_r": round(scored[0][0], 3)})
        saved: list[DeskCall] = []
        for er, ok, cand, p, basis, tp_price, fraction, why in scored:
            can_call, full = room()
            shadow = not (ok and can_call and out.call is None)
            was = next((r for r in recent if promotable(r) and overlaps(cand, r)), None)
            if shadow and was is not None:
                continue  # still watched as it was: no second copy
            call = new_call(cand, symbol, interval, bar, int(now), source, p, basis, tp_price, fraction, shadow)
            call.features["live_price"] = live
            if shadow:
                call.skip_reason = (why if not ok else "one call per run" if out.call is not None
                                    else "capacity" if full else "running call")
                out.watched += 1
            else:
                if was is not None:  # a watched zone called now: the watched copy leaves learning, the call learns
                    call.features["from_watched"] = was.id
                    was.features["promoted_to"] = call.id
                    saved.append(was)
                self._calls[call.id] = call  # counts against max_active before the await below
                out.call = call
                await self._place(call)
            self._calls[call.id] = call
            saved.append(call)
        await self.db.run(self._save, saved)
        return out

    async def _track(self, symbol: str, interval: str, c: Candidate) -> Optional[TrackRecord]:
        if self.track is None:
            return None
        try:
            return await self.track.for_plan(symbol, interval, c.plan, timeout=TRACK_TIMEOUT)
        except Exception as exc:
            log.info("Desk: no track record for %s %s: %s", symbol, interval, exc)
            return None

    # ------------------------------------------------------------------ paper
    async def _place(self, call: DeskCall) -> None:
        try:
            w = await self.paper.wallet()
            k, pct, notional, note = size_for(call.confidence, call.rr, call.risk_pct, w.equity, w.cash)
            call.kelly, call.size_pct, call.paper_note = round(k, 4), pct, note
            if notional is None:
                return
            await self.paper.place([
                NewPaperOrder(symbol=call.symbol, side="buy", type="limit", price=call.entry, quote=notional,
                              group=call.id, source="agent", note=f"Desk call: {call.setup}"[:300]),
                NewPaperOrder(symbol=call.symbol, side="sell", type="limit", price=call.tp, group=call.id,
                              source="agent", note="Desk take-profit"),
                NewPaperOrder(symbol=call.symbol, side="sell", type="stop", price=call.stop, group=call.id,
                              source="agent", note="Desk invalidation")])
            call.notional, call.paper_group = notional, call.id
        except Exception as exc:  # the call stands without its paper position
            call.paper_note = f"Paper order failed: {exc}"
            log.info("Desk: paper order for %s failed: %s", call.symbol, exc)

    def _group_orders(self, group: str) -> list:
        return [o for o in self.paper.orders() if o.group == group and o.cancelled_at is None]

    async def _paper_follow(self, call: DeskCall) -> None:
        """Keep the paper position in step with the call: cancel it when the call expires, sell when it times out."""
        if not call.paper_group:
            return
        try:
            if call.status == "expired":
                for o in self._group_orders(call.paper_group):
                    self.paper.cancel(o.id)
            elif call.status == "timed_out":
                rows = [o for o in (await self.paper.wallet()).orders if o.get("group") == call.paper_group]
                bought = any(o["side"] == "buy" and o["result"]["status"] == "filled" for o in rows)
                sold = any(o["side"] == "sell" and o["result"]["status"] == "filled" for o in rows)
                for o in self._group_orders(call.paper_group):
                    if o.side == "sell" or not bought:
                        self.paper.cancel(o.id)
                if bought and not sold:  # the wallet still holds it (on 1m candles it may already have exited)
                    await self.paper.place([NewPaperOrder(symbol=call.symbol, side="sell", type="market",
                                                          group=call.paper_group, source="agent",
                                                          note="Desk: hold window over")])
        except Exception as exc:
            call.paper_note = f"Paper follow-up failed: {exc}"
            log.info("Desk: paper follow-up for %s failed: %s", call.symbol, exc)

    async def wallet(self):
        return await self.paper.wallet()

    def reset_wallet(self, start_cash: float) -> None:
        self.paper.reset(start_cash)
        for c in self._calls.values():
            c.paper_group = None

    # ---------------------------------------------------------------- scoring
    async def score(self, now: Optional[float] = None) -> list[DeskCall]:
        """Bring every running call up to date → the ones whose status changed."""
        async with self._score_lock:
            now = time.time() if now is None else now
            todo: dict[tuple[str, str], list[DeskCall]] = {}
            for c in self._calls.values():
                if c.status in ACTIVE or (c.status in CLOSED and not c.reach_final):
                    todo.setdefault((c.symbol, SCORE_RES[c.interval]), []).append(c)
            changed: list[tuple[DeskCall, DeskCall]] = []
            touched: list[DeskCall] = []

            async def run(symbol: str, res: str, group: list[DeskCall]) -> None:
                start = min(c.created_at for c in group)
                try:
                    df, source = await self.market.get_range(symbol, res, start)
                except Exception as exc:
                    log.info("Desk: candles for %s %s failed: %s", symbol, res, exc)
                    return
                if source == "synthetic" and not self.demo_ok:
                    return
                for c in group:
                    upd = score_call(c, df, source, res, now)
                    new = c.model_copy(update={**upd, "updated_at": int(now)})
                    if new.model_dump(exclude={"updated_at"}) != c.model_dump(exclude={"updated_at"}):
                        touched.append(new)
                        if new.status != c.status:
                            changed.append((c, new))

            await asyncio.gather(*(run(s, r, g) for (s, r), g in todo.items()))
            for _, after in changed:
                await self._paper_follow(after)
            for c in touched:
                self._calls[c.id] = c
            if touched:
                await self.db.run(self._save, touched)
            self._scored_at = now
        for before, after in changed:
            await self._notify(after, "update")
            if after.status not in ACTIVE:
                await self._disarm_trigger(after)
        if changed and self.alerts is not None:  # open apps refresh their desk view and chart (no toast)
            self.alerts.broadcast({"type": "desk_scored", "changed": [a.id for _, a in changed]})
        return [a for _, a in changed]

    # ------------------------------------------------------------- messages
    def _record_line(self, c: DeskCall) -> str:
        rows = [x for x in self._real() if x.bucket == c.bucket and x.status in CLOSED and not x.shadow]
        hits = sum(x.status == "tp" for x in rows)
        return f"This setup so far: {hits}/{len(rows)} reached the take-profit." if rows else ""

    async def _notify(self, c: DeskCall, why: Literal["new", "update"]) -> None:
        if c.shadow:
            return
        cfg = self.settings
        if why == "new":
            text, want, title = call_text(c), cfg.notify_new, f"Desk call {TF_LABEL.get(c.interval, c.interval)}"
        else:
            text = outcome_text(c, self._record_line(c) if c.status in CLOSED else None)
            want = (cfg.notify_fills if c.status == "open" else cfg.notify_results if c.status in CLOSED
                    else cfg.notify_expired if c.status == "expired" else False)
            title = {"open": "Desk call filled", "tp": "Desk take-profit", "invalidated": "Desk call invalidated",
                     "timed_out": "Desk call timed out", "expired": "Desk call expired"}.get(c.status, "Desk call")
        if c.data_source == "synthetic":
            text = "[demo data] " + text
        if self.alerts is None:
            return
        item = self.alerts.record("desk", c.symbol, title, text, price=c.fill_price or c.entry)
        self.alerts.broadcast({"type": "desk", "call": c.model_dump()})
        if want and self.alerts.channels:
            try:
                image = await self._snapshot(c) if why == "new" else None
                if image is not None and hasattr(self.alerts, "attach_image"):
                    self.alerts.attach_image(item.get("id", ""), image)
                await self.alerts.send_notice(self._notice(c, title, text, image))
            except Exception as exc:
                log.info("Desk: notification failed: %s", exc)

    def _notice(self, c: DeskCall, title: str, text: str, image: Optional[bytes]) -> Notice:
        """A desk call or its result as a card: the buy zone, take-profit and invalidation with their distances,
        the confidence (once filled) and the paper size."""
        def pct(p: float) -> str:
            return f"{fmt_price(p)} ({(p / c.entry - 1) * 100:+.1f}%)"

        fields = [("Buy zone", f"{fmt_price(c.zone_low)}–{fmt_price(c.zone_high)}", True),
                  ("Take-profit", pct(c.tp), True), ("Invalidation", pct(c.stop), True),
                  ("Confidence if filled", f"{c.confidence * 100:.0f}% (random {100 / (1 + max(c.rr, 0.01)):.0f}%)", True),
                  ("Expected", f"{c.expected_r:+.2f}R after fees", True)]
        if c.notional:
            fields.append(("Paper size", f"${c.notional:,.0f}", True))
        if c.r is not None:
            fields.insert(0, ("Result", f"{c.r:+.2f}R", True))
        side = ("buy" if c.status in ("waiting", "open", "tp") else "sell" if c.status in ("invalidated", "timed_out")
                else "info")
        return Notice(kind="desk", title=f"{title}: {c.symbol}", text=text, symbol=c.symbol, interval=c.interval,
                      description=c.setup, fields=fields, side=side, image=image, demo=c.data_source == "synthetic",
                      url=app_link(self.s.public_app_url, c.symbol, c.interval, f"desk:{c.id}"))

    async def _snapshot(self, c: DeskCall) -> Optional[bytes]:
        """A chart of the call: the closed candles it was made on, its buy zone, take-profit and invalidation."""
        try:
            candles, source = await self.market.get_klines(c.symbol, c.interval, 200)
        except Exception:
            return None
        df = candles_to_df([k for k in candles if k.time <= c.bar_time])
        if len(df) < 20:
            return None
        return await snapshot(df, boxes=[(c.zone_low, c.zone_high, (167, 139, 250, 60), "Desk buy zone")],
                              lines=[(c.tp, (34, 197, 94), True, f"TP {fmt_price(c.tp)}"),
                                     (c.stop, (239, 68, 68), True, f"wrong below {fmt_price(c.stop)}")],
                              title=f"{c.symbol} {TF_LABEL.get(c.interval, c.interval)} · desk call",
                              demo=c.data_source == "synthetic")

    def brief_lines(self, now: Optional[float] = None) -> list[str]:
        """The desk's part of the brief: what it is running, what closed in the last day, its record."""
        now = time.time() if now is None else now
        rows = [c for c in self._real() if not c.shadow]
        if not rows:
            return []
        out = ["Agent desk"]
        for c in sorted((c for c in rows if c.status in ACTIVE), key=lambda c: -c.confidence)[:6]:
            what = "holding" if c.status == "open" else "waiting to buy"
            out.append(f"{c.symbol.removesuffix('USDT')} {TF_LABEL.get(c.interval, c.interval)}: {what} "
                       f"{c.zone_low:g}–{c.zone_high:g}, take-profit {c.tp:g}, {c.confidence * 100:.0f}%")
        day = [c for c in rows if c.status in CLOSED and (c.closed_at or 0) >= now - 86400]
        if day:
            out.append("Last 24h: " + ", ".join(f"{c.symbol.removesuffix('USDT')} {c.status.replace('_', ' ')} "
                                                f"{(c.r or 0):+.2f}R" for c in day[:8]))
        s = self.summary(now)
        if s["closed"]:
            out.append(f"Record: {s['tp']}/{s['closed']} reached the take-profit, {s['total_r']:+.2f}R in all. "
                       f"{s['verdict']}")
        return out

    def facts_for(self, symbol: str) -> Optional[dict]:
        """What the chart agent says about the desk on this coin."""
        rows = [c for c in self.calls(symbol=symbol, limit=20) if self.demo_ok or c.data_source == "binance"]
        if not rows:
            return None
        keep = ("interval", "status", "zone_low", "zone_high", "entry", "tp", "stop", "confidence", "setup", "r",
                "created_at")
        return {"running": [{k: getattr(c, k) for k in keep} for c in rows if c.status in ACTIVE],
                "recent_results": [{k: getattr(c, k) for k in keep} for c in rows if c.status in CLOSED][:5]}

    # ------------------------------------------------------------- scheduling
    async def tick(self, now: Optional[float] = None) -> list[str]:
        """Run the timeframes whose candle just closed, then score → the timeframes that ran."""
        now = time.time() if now is None else now
        ran = []
        if self.s.agent_desk and self.settings.enabled:
            for tf, bar in due_bars(self.settings.timeframes, self._done, now):
                await self.run_interval(tf, bar, now)
                ran.append(tf)
        if ran or now - self._scored_at >= self.score_seconds:
            await self.score(now)
        if now - self._pruned_at >= PRUNE_SECONDS:
            await self.prune(now)
        return ran

    async def prune(self, now: Optional[float] = None) -> int:
        """Drop watched zones that finished more than KEEP_WATCHED_DAYS ago (calls are always kept) → how many."""
        now = time.time() if now is None else now
        self._pruned_at = now
        cutoff = now - KEEP_WATCHED_DAYS * 86400
        old = [c.id for c in self._calls.values() if c.shadow and c.status not in ACTIVE
               and (c.closed_at or c.created_at) < cutoff]
        for cid in old:
            self._calls.pop(cid, None)
        if old:
            await self.db.run(self.db.executemany, "DELETE FROM desk_calls WHERE id = ?", [(cid,) for cid in old])
        return len(old)

    def start(self) -> None:
        if not self._tasks:
            jobs.declare("agent_desk", "Agent desk", self.check_seconds, self.s.agent_desk and self.settings.enabled)
            self._tasks = [asyncio.create_task(self._loop(), name="agent-desk")]

    async def _loop(self) -> None:
        await asyncio.sleep(10)
        while True:
            try:
                ran = await self.tick()
                active = sum(c.status in ACTIVE and not c.shadow for c in self._calls.values())
                jobs.ok("agent_desk", (f"looked at {', '.join(ran)}; " if ran else "") + f"{active} calls running")
            except Exception as exc:
                jobs.fail("agent_desk", exc)
                log.exception("Agent desk check failed")
            await asyncio.sleep(self.check_seconds)

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks = []
