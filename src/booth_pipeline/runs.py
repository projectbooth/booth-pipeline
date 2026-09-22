"""Running a Run: the recorder that persists what happens, and the manager that owns the workers.

``StoreRecorder`` is the engine's ``RunRecorder`` backed by the store: it keeps each task's state
current (so the UI shows live progress) and streams log lines into ``run_logs`` — buffered and
capped, because task output is arbitrary and unbounded. ``RunManager`` owns the worker threads,
per-run heartbeats and cancellation, and finalises runs — including runs whose worker died.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from . import engine
from .config import Config
from .records import (
    RUN_CANCELED,
    RUN_FAILED,
    RUN_QUEUED,
    RUN_SUCCEEDED,
    TASK_CANCELED,
    TASK_FAILED,
    TASK_PENDING,
    TASK_RETRYING,
    TASK_RUNNING,
    TASK_SKIPPED,
    TASK_SUCCEEDED,
    LogLine,
    Run,
    TaskRun,
)
from .runners.base import Cancellation
from .runners.registry import RunnerRegistry
from .store.base import Store
from .workload import AccessProvider, WorkloadMinter

log = logging.getLogger(__name__)

FLUSH_INTERVAL = 0.5
FLUSH_BATCH = 200


def _now() -> datetime:
    return datetime.now(UTC)


class StoreRecorder:
    """Thread-safe: the engine calls it from the run's thread while the base runner's stdout and
    stderr reader threads call ``TaskLog.line`` concurrently."""

    def __init__(self, store: Store, run_id: str, max_lines: int, flush_interval: float = FLUSH_INTERVAL) -> None:
        self._store = store
        self._run_id = run_id
        self._max = max_lines
        self._lock = threading.Lock()
        self._seq = 0
        self._buf: list[LogLine] = []
        self._dropped = 0
        self._truncated_noted = False
        self._tasks: dict[str, TaskRun] = {t.task_key: t for t in store.list_task_runs(run_id)}
        self._stop = threading.Event()
        self._interval = flush_interval
        self._flusher = threading.Thread(target=self._flush_loop, name=f"logflush-{run_id[:8]}", daemon=True)
        self._flusher.start()

    # ---- logging ----
    def _emit(self, task_key: str | None, attempt: int, stream: str, message: str) -> None:
        with self._lock:
            if stream != "system" and self._seq >= self._max:
                self._dropped += 1
                if not self._truncated_noted:
                    self._truncated_noted = True
                    self._seq += 1
                    self._buf.append(LogLine(self._run_id, self._seq, _now(), None, 0, "system", f"log truncated: a run keeps at most {self._max} lines; further task output is discarded"))
                return
            self._seq += 1
            self._buf.append(LogLine(self._run_id, self._seq, _now(), task_key, attempt, stream, message))
            full = len(self._buf) >= FLUSH_BATCH
        if full:
            self.flush()

    def flush(self) -> None:
        with self._lock:
            batch, self._buf = self._buf, []
        if batch:
            try:
                self._store.append_logs(batch)
            except Exception:  # noqa: BLE001 - losing a batch of log lines must never fail the run
                log.exception("run %s: could not persist %d log lines", self._run_id, len(batch))

    def _flush_loop(self) -> None:
        while not self._stop.wait(self._interval):
            self.flush()

    def close(self) -> None:
        self._stop.set()
        self._flusher.join(timeout=5)
        self.flush()

    def system(self, message: str) -> None:
        self._emit(None, 0, "system", message)

    def task_log(self, key: str, attempt: int):
        rec = self

        class _TaskLog:
            def line(self, stream: str, message: str) -> None:
                rec._emit(key, attempt, stream, message)

        return _TaskLog()

    # ---- task state ----
    def _save(self, t: TaskRun) -> None:
        try:
            self._store.update_task_run(t)
        except Exception:  # noqa: BLE001
            log.exception("run %s: could not persist state of task %s", self._run_id, t.task_key)

    def task_started(self, key: str, attempt: int) -> None:
        with self._lock:
            t = self._tasks[key]
            t.status, t.attempts, t.error, t.finished_at = TASK_RUNNING, attempt, None, None
            t.started_at = t.started_at or _now()
        self._save(t)
        self._emit(key, attempt, "system", f"task {key!r} started (attempt {attempt})")

    def task_attempt_finished(self, key: str, attempt: int, status: str, error: str | None) -> None:
        state = {"succeeded": TASK_SUCCEEDED, "failed": TASK_FAILED, "retrying": TASK_RETRYING, "canceled": TASK_CANCELED}[status]
        with self._lock:
            t = self._tasks[key]
            t.status, t.error = state, error
            if state != TASK_RETRYING:
                t.finished_at = _now()
        self._save(t)
        self._emit(key, attempt, "system", f"task {key!r} {status}" + (f": {error}" if error else ""))

    def settle_unfinished(self, run_status: str) -> None:
        """Called when the run ends: tasks that never ran are *skipped* (an upstream failed) or
        *canceled*; a task caught mid-flight (worker lost) is failed. Nothing stays "pending"."""
        with self._lock:
            todo = [t for t in self._tasks.values() if t.status in (TASK_PENDING, TASK_RUNNING, TASK_RETRYING)]
            for t in todo:
                if t.status == TASK_PENDING:
                    t.status = TASK_CANCELED if run_status == RUN_CANCELED else TASK_SKIPPED
                else:
                    t.status = TASK_CANCELED if run_status == RUN_CANCELED else TASK_FAILED
                    t.error = t.error or "the run ended while this task was in progress"
                t.finished_at = _now()
        for t in todo:
            self._save(t)


class RunManager:
    def __init__(self, store: Store, registry: RunnerRegistry, cfg: Config, worker_id: str, minter: WorkloadMinter | None = None) -> None:
        self._store = store
        self._registry = registry
        self._cfg = cfg
        self._minter = minter
        self.worker_id = worker_id
        self._pool = ThreadPoolExecutor(max_workers=cfg.max_workers, thread_name_prefix="run")
        self._cancels: dict[str, Cancellation] = {}
        self._lock = threading.Lock()

    def submit(self, run: Run) -> None:
        self._pool.submit(self._execute, run)

    def cancel(self, run_id: str) -> None:
        """Signal a run this process is executing. A run on another replica sees the store's
        ``cancel_requested`` flag through its own watcher instead."""
        with self._lock:
            c = self._cancels.get(run_id)
        if c:
            c.cancel()

    def shutdown(self) -> None:
        with self._lock:
            for c in self._cancels.values():
                c.cancel()
        self._pool.shutdown(wait=True, cancel_futures=True)

    # ---- one run ----
    def _execute(self, run: Run) -> None:
        store, now = self._store, _now()
        try:
            self._execute_inner(run)
        except Exception as e:  # noqa: BLE001 - a bug here must not leave the run "active" forever
            log.exception("run %s: unexpected failure", run.id)
            store.finish_run(run.id, RUN_FAILED, f"internal error: {e.__class__.__name__}", now)

    def _execute_inner(self, run: Run) -> None:
        store = self._store
        current = store.get_run(run.workspace, run.id)
        if current is None:
            return  # its job was deleted while it waited in the queue
        if current.status != RUN_QUEUED:
            return  # already finished (e.g. swept as never-started) — do not resurrect it
        if current.cancel_requested:
            store.finish_run(run.id, RUN_CANCELED, "canceled before it started", _now())
            return

        version = store.get_version(run.workspace, run.pipeline_id, run.pipeline_version)
        job = store.get_job(run.workspace, run.job_id)
        if version is None or job is None:
            store.finish_run(run.id, RUN_FAILED, "the pipeline version or job no longer exists", _now())
            return

        store.mark_running(run.id, self.worker_id, _now())
        recorder = StoreRecorder(store, run.id, self._cfg.max_log_lines_per_run)
        cancel = Cancellation()
        with self._lock:
            self._cancels[run.id] = cancel
        stop = threading.Event()
        watcher = threading.Thread(target=self._watch, args=(run.id, cancel, stop), name=f"watch-{run.id[:8]}", daemon=True)
        watcher.start()

        started = time.monotonic()
        recorder.system(f"run started: pipeline version {run.pipeline_version}, {len(version.spec.tasks)} task(s), triggered by {run.triggered_by} ({run.trigger})")
        try:
            # `sub` names the RUN, never a person (ADR 0056); core uses the owner only to look up the
            # role that caps the token. Nothing is minted here: only a task that opted in mints.
            access = AccessProvider(
                self._minter,
                workspace=run.workspace,
                subject=f"run:{run.id}",
                role_ceiling=job.role_ceiling,
                owner=run.owner_sub,
                storage_url=self._cfg.storage_base,
                catalog_url=self._cfg.catalog_base,
            )
            result = engine.execute(
                version.spec, run_id=run.id, registry=self._registry, recorder=recorder, cancel=cancel, job_retry=job.retry, access=access
            )
            status = RUN_SUCCEEDED if result.success else (RUN_CANCELED if result.canceled else RUN_FAILED)
            error = None if result.success else result.error
        except Exception as e:  # noqa: BLE001 - engine/compile failure is a failed run, not a crashed worker
            log.exception("run %s: engine error", run.id)
            status, error = RUN_FAILED, f"engine error: {e}"
        finally:
            stop.set()
            watcher.join(timeout=5)
            with self._lock:
                self._cancels.pop(run.id, None)

        recorder.settle_unfinished(status)
        recorder.system(f"run {status} after {time.monotonic() - started:.1f}s" + (f": {error}" if error else ""))
        recorder.close()  # flush every log line BEFORE the terminal status is visible
        store.finish_run(run.id, status, error, _now())

    def _watch(self, run_id: str, cancel: Cancellation, stop: threading.Event) -> None:
        """Heartbeat (so a dead worker is noticed) and poll for a cancel requested on any replica."""
        last_beat = 0.0
        while not stop.wait(1.0):
            try:
                if time.monotonic() - last_beat >= self._cfg.heartbeat_seconds:
                    self._store.heartbeat(run_id, _now())
                    last_beat = time.monotonic()
                if self._store.is_cancel_requested(run_id):
                    cancel.cancel()
            except Exception:  # noqa: BLE001 - a store blip must not kill the watcher
                log.exception("run %s: heartbeat/cancel poll failed", run_id)

    # ---- recovery ----
    def sweep(self) -> int:
        """Fail runs whose worker is gone. Safe to call from every replica: ``finish_run`` is
        first-writer-wins, so each stale run is failed exactly once."""
        now = _now()
        stale = self._store.stale_runs(
            now - timedelta(seconds=self._cfg.stale_after_seconds), now - timedelta(seconds=self._cfg.queued_timeout_seconds)
        )
        failed = 0
        for r in stale:
            reason = "worker lost: no heartbeat" if r.status != RUN_QUEUED else "never started (the worker that accepted it went away)"
            if self._store.finish_run(r.id, RUN_FAILED, reason, now):
                failed += 1
                log.warning("run %s failed by sweep: %s", r.id, reason)
                for t in self._store.list_task_runs(r.id):
                    if t.status in (TASK_PENDING, TASK_RUNNING, TASK_RETRYING):
                        t.status = TASK_SKIPPED if t.status == TASK_PENDING else TASK_FAILED
                        t.error = t.error or reason
                        t.finished_at = now
                        self._store.update_task_run(t)
                self._store.append_logs([LogLine(r.id, _next_seq(self._store, r.id), now, None, 0, "system", f"run failed: {reason}")])
        return failed


def _next_seq(store: Store, run_id: str) -> int:
    """A sweeping replica appends after the dead worker's last line. There is no live writer to
    race (the worker is presumed dead), and (run_id, seq) is a primary key, so a wrong guess is a
    dropped line rather than corruption."""
    tail = 0
    while True:
        page = store.list_logs(run_id, None, tail, 1000)
        if not page:
            return tail + 1
        tail = page[-1].seq
