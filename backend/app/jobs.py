"""What the background jobs are doing, for the status panel (GET /api/status).

Each loop declares itself once (`declare`) with a name, a label and how often it runs, then reports every run
(`ok`, with an optional short detail) or failure (`fail`). The status says when each job last ran, its last error and
whether it is overdue: a job that hasn't reported for OVERDUE_FACTOR times its period (after a start-up grace) has
probably stopped. A module-level registry, so any service can report without being handed one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

OVERDUE_FACTOR = 3.0
STARTUP_GRACE = 120.0


@dataclass
class Job:
    name: str
    label: str
    every: float  # seconds between runs
    declared_at: float
    enabled: bool = True
    last_ok: Optional[float] = None
    last_fail: Optional[float] = None
    last_error: Optional[str] = None
    detail: str = ""
    runs: int = 0
    failures: int = 0


class Jobs:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def declare(self, name: str, label: str, every: float, enabled: bool = True) -> None:
        job = self._jobs.get(name)
        if job is None:
            self._jobs[name] = Job(name, label, every, time.time(), enabled)
        else:
            job.label, job.every, job.enabled = label, every, enabled

    def set_enabled(self, name: str, enabled: bool) -> None:
        if name in self._jobs:
            self._jobs[name].enabled = enabled

    def ok(self, name: str, detail: str = "") -> None:
        job = self._jobs.get(name)
        if job is None:
            return
        job.last_ok, job.runs = time.time(), job.runs + 1
        if detail:
            job.detail = detail

    def fail(self, name: str, exc: BaseException | str) -> None:
        job = self._jobs.get(name)
        if job is None:
            return
        job.last_fail, job.failures = time.time(), job.failures + 1
        job.last_error = (str(exc) or type(exc).__name__)[:300] if isinstance(exc, BaseException) else exc[:300]

    def status(self, now: Optional[float] = None) -> list[dict]:
        now = time.time() if now is None else now
        out = []
        for j in sorted(self._jobs.values(), key=lambda j: j.label):
            last = max(filter(None, (j.last_ok, j.last_fail)), default=None)
            overdue = j.enabled and now - (last or j.declared_at) > max(STARTUP_GRACE, OVERDUE_FACTOR * j.every)
            failing = j.last_fail is not None and (j.last_ok is None or j.last_fail > j.last_ok)
            state = "off" if not j.enabled else "error" if failing else "overdue" if overdue else (
                "ok" if j.last_ok else "waiting")
            out.append({"name": j.name, "label": j.label, "every_seconds": j.every, "enabled": j.enabled,
                        "state": state, "last_ok": j.last_ok, "last_fail": j.last_fail, "last_error": j.last_error,
                        "detail": j.detail, "runs": j.runs, "failures": j.failures})
        return out

    def clear(self) -> None:
        self._jobs.clear()


jobs = Jobs()
