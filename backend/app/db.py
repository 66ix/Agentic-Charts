"""SQLite for the app's own records that grow and need queries: the agent desk's calls and what it learns from them.

Older stores stay JSON files (alerts, journal, paper wallet...). New ones go here: one file (AGENT_DB, default
backend/.cache/agent.db; `memory` keeps it in memory, as in tests), one connection shared by worker threads behind a
lock, WAL mode so reads don't wait on writes.

Each module owns its tables and brings its schema as an ordered list of migration scripts (`migrate`): the database
remembers how many of a module's scripts it has run, so a script is never run twice and adding a table or a column
later is one more script at the end of the list. Never edit or reorder a script that has shipped.

The methods are synchronous; call them from async code with `await db.run(fn, ...)` (or `asyncio.to_thread`), as
`MarketData` does with the candle store.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, TypeVar

from .config import get_settings

log = logging.getLogger(__name__)

T = TypeVar("T")


class Database:
    def __init__(self, path: Optional[str] = None) -> None:
        value = (path if path is not None else get_settings().agent_db).strip()
        self.memory = value.lower() in ("", "memory", "none", "off")
        if not self.memory:
            Path(value).parent.mkdir(parents=True, exist_ok=True)
        self.path = ":memory:" if self.memory else value
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if not self.memory:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("CREATE TABLE IF NOT EXISTS _migrations (module TEXT PRIMARY KEY, version INTEGER NOT NULL)")

    def migrate(self, module: str, scripts: list[str]) -> None:
        """Run the scripts of `module` it hasn't run yet, each in its own transaction."""
        with self._lock:
            row = self._conn.execute("SELECT version FROM _migrations WHERE module = ?", (module,)).fetchone()
            done = row["version"] if row else 0
            for i, script in enumerate(scripts[done:], start=done + 1):
                self._conn.execute("BEGIN")
                try:
                    for stmt in filter(str.strip, script.split(";")):
                        self._conn.execute(stmt)
                    self._conn.execute("INSERT INTO _migrations (module, version) VALUES (?, ?) ON CONFLICT(module) "
                                       "DO UPDATE SET version = excluded.version", (module, i))
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Run one statement → rows changed."""
        with self._lock:
            return self._conn.execute(sql, tuple(params)).rowcount

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.executemany(sql, [tuple(r) for r in rows])
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    def one(self, sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(sql, tuple(params)).fetchone()
            return dict(row) if row else None

    async def run(self, fn: Callable[..., T], *args: Any) -> T:
        return await asyncio.to_thread(fn, *args)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
