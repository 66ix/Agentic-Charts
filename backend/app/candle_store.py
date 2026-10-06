"""SQLite cache of closed Binance candles, so a restart only asks Binance for new bars.

One row per (symbol, interval, bar open time). Callers store closed Binance bars
only: the newest bar of a response may still be open, and synthetic data is never
written, so whatever comes back out can be served as real history.

The methods are synchronous; `MarketData` runs them with `asyncio.to_thread`. A read
takes a few milliseconds, but a write can wait on the disk (fsync, WAL checkpoint)
or on another process holding the write lock, and the same event loop drives the
live WebSocket streams. The worker threads share one connection behind a lock.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .schemas import Candle

# Bars kept per (symbol, interval): the largest requests are Kimi Cooked's 5000 bars, and its 1600 bars of 3h
# (4803 bars of 1h).
KEEP_BARS = 5000
# History fetched by time range (grid bots, journal, backtests) lives in its own table and is not trimmed to
# KEEP_BARS: a grid bot that has run for a month needs every 1m bar since it started. About 13 months of 1m.
RANGE_KEEP_BARS = 570_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    symbol   TEXT    NOT NULL,
    interval TEXT    NOT NULL,
    time     INTEGER NOT NULL,
    open     REAL    NOT NULL,
    high     REAL    NOT NULL,
    low      REAL    NOT NULL,
    close    REAL    NOT NULL,
    volume   REAL    NOT NULL,
    PRIMARY KEY (symbol, interval, time)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS range_candles (
    symbol   TEXT    NOT NULL,
    interval TEXT    NOT NULL,
    time     INTEGER NOT NULL,
    open     REAL    NOT NULL,
    high     REAL    NOT NULL,
    low      REAL    NOT NULL,
    close    REAL    NOT NULL,
    volume   REAL    NOT NULL,
    PRIMARY KEY (symbol, interval, time)
) WITHOUT ROWID;
"""


class CandleStore:
    """Raises `sqlite3.Error`/`OSError` when the file is unwritable or corrupt; callers decide how to degrade."""

    def __init__(self, path: str | Path, keep: int = KEEP_BARS) -> None:
        self.path = Path(path)
        self.keep = keep
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, timeout=5.0, check_same_thread=False)
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")  # it is a cache: losing the last commit is fine
            self._db.executescript(_SCHEMA)
            self._db.commit()
        except sqlite3.Error:
            self._db.close()
            raise

    def load(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        """The newest `limit` stored bars, oldest→newest."""
        with self._lock:
            rows = self._db.execute(
                "SELECT time, open, high, low, close, volume FROM candles"
                " WHERE symbol = ? AND interval = ? ORDER BY time DESC LIMIT ?",
                (symbol, interval, limit),
            ).fetchall()
        return [Candle(time=t, open=o, high=h, low=l, close=c, volume=v) for t, o, h, l, c, v in reversed(rows)]

    def save(self, symbol: str, interval: str, candles: list[Candle]) -> None:
        """Insert or overwrite `candles`, then drop all but the newest `keep` bars of the series."""
        if not candles:
            return
        rows = [(symbol, interval, c.time, c.open, c.high, c.low, c.close, c.volume) for c in candles]
        with self._lock, self._db:  # one transaction
            self._db.executemany(
                "INSERT INTO candles (symbol, interval, time, open, high, low, close, volume)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (symbol, interval, time) DO UPDATE SET open = excluded.open, high = excluded.high,"
                " low = excluded.low, close = excluded.close, volume = excluded.volume",
                rows,
            )
            self._db.execute(
                "DELETE FROM candles WHERE symbol = ? AND interval = ? AND time <= (SELECT time FROM candles"
                " WHERE symbol = ? AND interval = ? ORDER BY time DESC LIMIT 1 OFFSET ?)",
                (symbol, interval, symbol, interval, self.keep),
            )

    def load_range(self, symbol: str, interval: str, start: int, end: int) -> list[tuple]:
        """Stored range bars with start <= time <= end as (time, open, high, low, close, volume), oldest first."""
        with self._lock:
            return self._db.execute(
                "SELECT time, open, high, low, close, volume FROM range_candles"
                " WHERE symbol = ? AND interval = ? AND time >= ? AND time <= ? ORDER BY time",
                (symbol, interval, start, end),
            ).fetchall()

    def save_range(self, symbol: str, interval: str, rows: list[tuple]) -> None:
        """Insert closed bars given as (time, open, high, low, close, volume); keeps the newest RANGE_KEEP_BARS."""
        if not rows:
            return
        with self._lock, self._db:
            self._db.executemany(
                "INSERT OR REPLACE INTO range_candles (symbol, interval, time, open, high, low, close, volume)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(symbol, interval, *r) for r in rows],
            )
            self._db.execute(
                "DELETE FROM range_candles WHERE symbol = ? AND interval = ? AND time <= (SELECT time FROM"
                " range_candles WHERE symbol = ? AND interval = ? ORDER BY time DESC LIMIT 1 OFFSET ?)",
                (symbol, interval, symbol, interval, RANGE_KEEP_BARS),
            )

    def latest_time(self, symbol: str, interval: str) -> int | None:
        """Open time of the newest stored bar, or None when the series is empty."""
        with self._lock:
            row = self._db.execute(
                "SELECT MAX(time) FROM candles WHERE symbol = ? AND interval = ?", (symbol, interval)
            ).fetchone()
        return row[0]

    def close(self) -> None:
        with self._lock:
            self._db.close()
