"""Holdings watch: sell-or-trim alerts on the coins you actually hold, kept in step with your Binance account.

When it is on, every CHECK_EVERY seconds (and when settings change) it reads your own spot holdings through the
read-only key (binance_import.positions: coins in the spot wallet and Simple Earn worth MIN_VALUE or more, without the ones you
marked as a bot's) and makes the signal alerts `lost_support` and `at_resistance` on the chosen timeframe exactly
those coins: a coin you buy gets them, a coin you sell loses them. The alerts it adds carry owner="holdings", so
alerts you made yourself are never touched or duplicated.

Grid bots: Binance's API does not show what is inside the Trading Bots wallet (only its total value) or the bots'
open orders, so the coins your bots trade can't be read from the account. With `include_bots` the coins of the
grid bots you track in the Grid bots tab are watched too, and so are coins whose imported fills were matched to a
bot.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING, Literal, Optional

from pydantic import BaseModel, Field

from .alerts import read_store, store_path, write_store
from .config import Settings, get_settings
from .jobs import jobs

if TYPE_CHECKING:
    from .binance_import import BinanceImportService
    from .signal_alerts import SignalAlertService

log = logging.getLogger(__name__)

OWNER = "holdings"
SIGNALS = ["lost_support", "at_resistance"]
CHECK_EVERY = 600.0
MAX_COINS = 40
STABLE_ASSETS = {"USDT", "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "EUR", "TRY"}
BOT_NOTE = ("Binance's API shows only the Trading Bots wallet's total, not the coins inside it or the bots' open "
            "orders. Bots you track in the Grid bots tab are watched by their coin instead.")


class WatchSettings(BaseModel):
    enabled: bool = False
    interval: Literal["1h", "4h", "1d"] = "4h"
    include_bots: bool = Field(True, description="Also watch the coins of grid bots tracked in the Grid bots tab")
    min_value: float = Field(10.0, ge=0, le=1_000_000, description="USDT; smaller holdings are dust")


class HoldingsWatch:
    def __init__(self, binance: "BinanceImportService", signals: "SignalAlertService",
                 settings: Optional[Settings] = None) -> None:
        self.binance = binance
        self.signals = signals
        s = settings or get_settings()
        self._path = store_path(s.holdings_watch_store)
        self.settings = WatchSettings.model_validate((read_store(self._path) or {}).get("settings", {}))
        self.last: Optional[dict] = None
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    def start(self) -> None:
        if self._task is None:
            jobs.declare("holdings_watch", "Holdings watch", CHECK_EVERY, self.settings.enabled)
            self._task = asyncio.create_task(self._loop(), name="holdings-watch")

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        await asyncio.sleep(20)
        while True:
            jobs.set_enabled("holdings_watch", self.settings.enabled)
            if self.settings.enabled:
                try:
                    await self.run()
                    jobs.ok("holdings_watch")
                except Exception as exc:
                    jobs.fail("holdings_watch", exc)
            await asyncio.sleep(CHECK_EVERY)

    def status(self) -> dict:
        return {"settings": self.settings.model_dump(), "last": self.last, "signals": SIGNALS, "bot_note": BOT_NOTE}

    async def update(self, new: WatchSettings) -> dict:
        self.settings = new
        write_store(self._path, {"settings": new.model_dump()})
        await self.run()
        return self.status()

    @staticmethod
    def coins(positions: dict, min_value: float, include_bots: bool) -> tuple[list[dict], list[str]]:
        """Your own holdings worth `min_value`+ → [{symbol, value, source}], and notes. Pure."""
        rows: dict[str, dict] = {}
        manual = positions.get("manual", {})
        held: dict[str, dict] = {}  # spot and Simple Earn together: a coin moved to Earn is still yours
        for r in manual.get("spot", []):
            if r.get("own_qty", 0) > 0:
                held[r["symbol"]] = {"symbol": r["symbol"], "value": r.get("value"), "source": "spot"}
        for r in manual.get("earn", []):
            if r.get("asset") in STABLE_ASSETS or r.get("qty", 0) <= 0:
                continue
            row = held.get(r["symbol"])
            if row is None:
                held[r["symbol"]] = {"symbol": r["symbol"], "value": r.get("value"), "source": "earn"}
            else:
                row["value"] = None if row["value"] is None or r.get("value") is None else row["value"] + r["value"]
                row["source"] = "spot + earn"
        for sym, row in held.items():
            if row["value"] is not None:
                row["value"] = round(row["value"], 2)
                if row["value"] < min_value:
                    continue
            rows[sym] = row
        if include_bots:
            bots = positions.get("bots", {})
            for r in bots.get("tracked", []) + bots.get("spot", []):
                sym = r.get("symbol")
                if sym and sym not in rows:
                    rows[sym] = {"symbol": sym, "value": r.get("value"), "source": "grid bot"}
        out = sorted(rows.values(), key=lambda r: -(r["value"] or 0))
        notes = []
        if len(out) > MAX_COINS:
            notes.append(f"Watching the {MAX_COINS} biggest of {len(out)} coins.")
        return out[:MAX_COINS], notes

    async def run(self) -> dict:
        """One sync now. Off → the watch's alerts are removed."""
        async with self._lock:
            st = self.settings
            now = int(time.time())
            if not st.enabled:
                res = await self.signals.sync_owned(OWNER, [], st.interval, SIGNALS)
                self.last = {"at": now, "coins": [], "added": [], "removed": res["removed"], "error": None,
                             "notes": []}
                return self.last
            if not self.binance.account.status().get("configured"):
                self.last = {"at": now, "coins": [], "added": [], "removed": [], "notes": [],
                             "error": "Add a read-only Binance key in the Account tab first."}
                return self.last
            try:
                pos = await self.binance.positions(refresh=True)
            except Exception as exc:  # keep the alerts as they are when the account can't be read
                log.info("Holdings watch: positions failed: %s", exc)
                self.last = {**(self.last or {"coins": [], "added": [], "removed": [], "notes": []}),
                             "at": now, "error": f"Could not read your holdings: {exc}"}
                return self.last
            coins, notes = self.coins(pos, st.min_value, st.include_bots)
            res = await self.signals.sync_owned(OWNER, [c["symbol"] for c in coins], st.interval, SIGNALS,
                                                note="Holdings watch: a coin you hold")
            if pos.get("bots", {}).get("wallet"):
                notes.append(BOT_NOTE)
            self.last = {"at": now, "coins": coins, "added": res["added"], "removed": res["removed"],
                         "error": None, "notes": notes}
            return self.last
