"""Run retention: a nightly prune of finished runs older than ``execution.retention.days``.

Off unless that is set (everything is kept, as before). When on, once a night at ``hourUtc`` each
replica deletes finished runs (succeeded, failed, canceled or skipped) whose ``finished_at`` is older
than the cutoff; their task runs and log lines go with them (ON DELETE CASCADE). A queued or running
run is never touched, however old.

Bounded so it never gets in the way: each batch is its own short transaction (``store.prune_runs``:
a LIMIT, SKIP LOCKED so replicas pruning at once take different rows and never wait on each other,
and a lock timeout), with a pause between batches and a time budget per night; a backlog bigger than
one night's budget (turning retention on over years of history) is finished on the following nights.
It runs on its own thread, so even a long prune never delays a scheduled fire.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime, timedelta

from .store.base import Store

log = logging.getLogger(__name__)

BATCH = 500  # runs per transaction; a minute pipeline's run has ~25 log lines, so ~12k rows per batch
PAUSE_SECONDS = 0.5
BUDGET_SECONDS = 30 * 60


def next_prune_at(now: datetime, hour_utc: int) -> datetime:
    """The next ``hour_utc``:00 UTC strictly after ``now``."""
    at = now.astimezone(UTC).replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    return at if at > now else at + timedelta(days=1)


class RunPruner:
    def __init__(
        self,
        store: Store,
        days: int,
        hour_utc: int = 8,
        batch: int = BATCH,
        pause_seconds: float = PAUSE_SECONDS,
        budget_seconds: float = BUDGET_SECONDS,
    ) -> None:
        self._store = store
        self.days = days
        self._hour = hour_utc
        self._batch = batch
        self._pause = pause_seconds
        self._budget = budget_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def prune_once(self, now: datetime | None = None) -> int:
        """Delete finished runs older than the cutoff, a batch at a time, within the budget. Returns how
        many runs were deleted."""
        now = now or datetime.now(UTC)
        before = now - timedelta(days=self.days)
        deadline = time.monotonic() + self._budget
        total = 0
        while not self._stop.is_set():
            n = self._store.prune_runs(before, self._batch)
            total += n
            if n < self._batch:
                break
            if time.monotonic() >= deadline:
                log.info("run retention: stopped at this night's budget after %d run(s); the rest goes on the next nights", total)
                break
            self._stop.wait(self._pause)
        if total:
            log.info("run retention: deleted %d finished run(s) older than %d day(s), with their task runs and logs", total, self.days)
        return total

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="run-retention", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = datetime.now(UTC)
            if self._stop.wait((next_prune_at(now, self._hour) - now).total_seconds()):
                return
            try:
                self.prune_once()
            except Exception:  # noqa: BLE001 - a failed prune (a database blip) is retried the next night
                log.exception("run retention: the prune failed; it is tried again tomorrow")
