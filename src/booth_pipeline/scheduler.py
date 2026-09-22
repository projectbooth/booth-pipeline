"""The scheduler: an index-driven loop, safe to run in every replica.

Two different things touch the database, at two different cadences (ADR 0065):

* **Claiming** (`claim_due_jobs`) is a locking write — it advances `next_run_at` atomically so
  with several replicas each due fire is started exactly once. The loop calls this **only when it
  already believes a job is due**, never on a fixed cadence, so a job's `interval` can go well
  below a minute without every replica hammering the database with a claim query every few
  seconds.
* **Indexing** (`list_upcoming`) is a cheap, lock-free read — "what's coming up, across the whole
  fleet." The loop refreshes this on a coarser cadence (`interval_seconds`, the constructor's one
  knob) and sleeps until either the index's earliest known fire time, or the next scheduled
  refresh, whichever comes first — so a newly created or rescheduled job (possibly from a
  *different* replica) is picked up within one refresh, not never.

`tick()` is the direct, synchronous "claim what's due right now and sweep" entrypoint — unchanged
in shape from before this redesign, still the one thing tests call directly with an explicit
``now``. The loop above it is what changed: it used to call `tick()` unconditionally on a fixed
interval; now it calls it only when the index says to.

Missed fires still **coalesce**: after downtime a job fires once, not once per missed interval,
because the claim advances ``next_run_at`` from *now*. A nightly report that missed three nights
should run once when the service returns, not three times in a row. This lives in the store's
``claim_due_jobs`` and is untouched by any of the above.

Dagster ships its own scheduler daemon; we do not use it. It watches code locations, and our
pipelines are rows in a database compiled per run (docs/decisions/0003) — driving Dagster's daemon
would mean generating and hot-reloading code locations on every save.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime

from .runs import RunManager
from .service import PipelineService
from .store.base import Store

log = logging.getLogger(__name__)

CLAIM_BATCH = 100
INDEX_SCAN_LIMIT = 200
# However far off the index's next known fire is, never sleep longer than this before checking
# again — it doubles as the index's own refresh cadence. Never sleep for less than this either,
# so a burst of near-simultaneous fires doesn't spin the loop.
MIN_SLEEP_SECONDS = 0.2


class Scheduler:
    def __init__(self, store: Store, service: PipelineService, manager: RunManager, interval_seconds: int) -> None:
        self._store = store
        self._service = service
        self._manager = manager
        self._index_refresh_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Loop-only state (unused by direct tick() calls): the index's earliest known fire time,
        # and when (monotonic) it was last refreshed from the database.
        self._next_known_fire: datetime | None = None
        self._index_loaded_at = 0.0

    def tick(self, now: datetime | None = None) -> list[str]:
        """One claim-and-run pass, unconditional. Returns the ids of runs it started (for tests
        and logs). This is the write path — see the module docstring for when the loop calls it."""
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

    def _refresh_index(self, now: datetime) -> None:
        upcoming = self._store.list_upcoming(INDEX_SCAN_LIMIT)
        self._next_known_fire = upcoming[0].next_run_at if upcoming else None
        self._index_loaded_at = time.monotonic()
        if len(upcoming) >= INDEX_SCAN_LIMIT:
            # More jobs are due soon than one scan covers — sweeping through them at
            # INDEX_SCAN_LIMIT-per-tick will still make progress (tick() claims CLAIM_BATCH at a
            # time and the loop re-indexes after every tick), just not in one pass. Worth knowing
            # about, not worth failing over.
            log.warning("scheduler index is full (%d jobs); more may be due than one refresh sees", INDEX_SCAN_LIMIT)

    def _sleep_seconds(self, now: datetime) -> float:
        # The index's own deadline is an ABSOLUTE monotonic instant (when it was last loaded,
        # plus the refresh cadence) — not "now plus the cadence," which would silently push the
        # deadline later every time this is called between refreshes rather than counting down to
        # a fixed point.
        seconds_to_refresh = (self._index_loaded_at + self._index_refresh_seconds) - time.monotonic()
        candidates = [seconds_to_refresh]
        if self._next_known_fire is not None:
            candidates.append((self._next_known_fire - now).total_seconds())
        return max(MIN_SLEEP_SECONDS, min(candidates))

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = datetime.now(UTC)
            if time.monotonic() - self._index_loaded_at >= self._index_refresh_seconds or self._index_loaded_at == 0.0:
                try:
                    self._refresh_index(now)
                except Exception:  # noqa: BLE001 - a bad refresh (a database blip) must not end scheduling for good
                    log.exception("scheduler index refresh failed; will retry")

            if self._stop.wait(self._sleep_seconds(now)):
                break

            now = datetime.now(UTC)
            if self._next_known_fire is not None and now >= self._next_known_fire:
                try:
                    self.tick(now)
                except Exception:  # noqa: BLE001 - one bad tick must not end scheduling for good
                    log.exception("scheduler tick failed; will retry")
                # The claim just advanced things (and may have started a run whose worker also
                # sweeps): re-index immediately rather than waiting out the rest of the cadence,
                # so a job firing every few seconds isn't throttled to the refresh interval.
                try:
                    self._refresh_index(datetime.now(UTC))
                except Exception:  # noqa: BLE001
                    log.exception("scheduler index refresh failed; will retry")
