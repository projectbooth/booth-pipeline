"""The cron scheduler: a small polling loop, safe to run in every replica.

Every ``interval`` seconds it (1) claims jobs whose schedule is due, and (2) sweeps runs whose
worker died. Claiming is atomic in the store (``claim_due_jobs`` advances ``next_run_at`` in the
same step it returns the job), so with several replicas each due fire is started exactly once —
there is no leader election because none is needed.

Missed fires are **coalesced**: after downtime a job fires once, not once per missed interval,
because the claim advances ``next_run_at`` from *now*. A nightly report that missed three nights
should run once when the service returns, not three times in a row.

Dagster ships its own scheduler daemon; we do not use it. It watches code locations, and our
pipelines are rows in a database compiled per run (docs/decisions/0003) — driving Dagster's daemon
would mean generating and hot-reloading code locations on every save.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime

from .runs import RunManager
from .service import PipelineService
from .store.base import Store

log = logging.getLogger(__name__)

CLAIM_BATCH = 100


class Scheduler:
    def __init__(self, store: Store, service: PipelineService, manager: RunManager, interval_seconds: int) -> None:
        self._store = store
        self._service = service
        self._manager = manager
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self, now: datetime | None = None) -> list[str]:
        """One pass. Returns the ids of runs it started (for tests and logs)."""
        now = now or datetime.now(UTC)
        started: list[str] = []
        for job in self._store.claim_due_jobs(now, CLAIM_BATCH):
            run = self._service.start_scheduled(job)
            if run:
                started.append(run.id)
        self._manager.sweep()
        return started

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - one bad tick (a database blip) must not end scheduling for good
                log.exception("scheduler tick failed; will retry")
            self._stop.wait(self._interval)
