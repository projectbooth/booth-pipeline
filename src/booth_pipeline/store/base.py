"""The persistence interface.

Two implementations (``memory.py`` for tests/dev, ``postgres.py`` for real) satisfy it, and one
contract suite (``tests/unit/test_store_contract.py``) runs against both — so they cannot drift.
Every method that takes a ``workspace`` scopes to it: a record in another workspace is
indistinguishable from a missing one (multi-workspace tenancy from v0, ADR 0008).
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from ..model import PipelineSpec
from ..records import Job, LogLine, Page, Pipeline, PipelineVersion, Run, TaskRun


class Conflict(Exception):
    """A uniqueness rule was violated (e.g. a pipeline name already used in the workspace)."""

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.field = field


class Busy(Exception):
    """An exclusive run was refused because the job already has an active run."""


class InUse(Exception):
    """The record cannot be removed because something still depends on it."""


class Store(Protocol):
    # ---- pipelines ----
    def create_pipeline(self, workspace: str, name: str, description: str, by: str) -> Pipeline: ...
    def get_pipeline(self, workspace: str, pipeline_id: str) -> Pipeline | None: ...
    def list_pipelines(self, workspace: str, q: str, limit: int, offset: int) -> Page: ...
    def update_pipeline(self, workspace: str, pipeline_id: str, name: str, description: str) -> Pipeline | None: ...
    def delete_pipeline(self, workspace: str, pipeline_id: str) -> bool:
        """False if absent. Raises ``InUse`` while any job still references it."""
        ...

    def add_version(self, workspace: str, pipeline_id: str, spec: PipelineSpec, notes: str, by: str) -> PipelineVersion | None:
        """Append the next immutable version (1, 2, ...). None if the pipeline is absent."""
        ...

    def get_version(self, workspace: str, pipeline_id: str, version: int | None) -> PipelineVersion | None:
        """``None`` means the latest."""
        ...

    def list_versions(self, workspace: str, pipeline_id: str, limit: int, offset: int) -> Page:
        """Newest first."""
        ...

    # ---- jobs ----
    def create_job(self, job: Job) -> Job: ...
    def get_job(self, workspace: str, job_id: str) -> Job | None: ...
    def list_jobs(self, workspace: str, pipeline_id: str | None, q: str, limit: int, offset: int) -> Page: ...
    def update_job(self, job: Job) -> Job | None: ...
    def delete_job(self, workspace: str, job_id: str) -> bool:
        """Also removes the job's runs, task runs and logs."""
        ...

    def claim_due_jobs(self, now: datetime, limit: int) -> list[Job]:
        """Across all workspaces: jobs whose schedule is enabled and ``next_run_at <= now``.

        Each returned job's ``next_run_at`` has *already been advanced* to its next fire time
        in the same atomic step, so with several replicas each due fire is claimed exactly once.
        A write (row locks in the SQL store) — the scheduler calls this only when it actually
        believes something is due, never on a fixed cadence (ADR 0065; see ``list_upcoming``).
        """
        ...

    def list_upcoming(self, limit: int) -> list[Job]:
        """Across all workspaces: jobs whose schedule is enabled and has a ``next_run_at``, the
        soonest first. A plain read — no claiming, no locking, ``next_run_at`` untouched.

        This is the scheduler's in-memory index (ADR 0065): called on a coarser cadence than a
        claim, so a fleet of replicas can each know "what's coming up" without every one of them
        running a locking write query every few seconds. Bounded staleness — a job created or
        rescheduled by another replica is visible within one refresh, not instantly.
        """
        ...

    # ---- runs ----
    def create_run(self, run: Run, task_keys: list[str], exclusive: bool = False) -> Run:
        """Creates the run and one pending task run per key.

        ``exclusive``: atomically refuse (``Busy``) if the job already has an active run. The check
        and the insert happen under one lock on the job, so two simultaneous triggers cannot both
        succeed — a plain "count, then insert" would let them.
        """
        ...

    def get_run(self, workspace: str, run_id: str) -> Run | None: ...
    def list_runs(self, workspace: str, job_id: str | None, status: str | None, limit: int, offset: int) -> Page:
        """Newest first."""
        ...

    def count_active_runs(self, workspace: str, job_id: str) -> int: ...
    def mark_running(self, run_id: str, worker_id: str, now: datetime) -> None: ...
    def heartbeat(self, run_id: str, now: datetime) -> None: ...
    def finish_run(self, run_id: str, status: str, error: str | None, now: datetime) -> bool:
        """Set a terminal status. No-op (False) if the run is already terminal — first writer wins."""
        ...

    def request_cancel(self, workspace: str, run_id: str) -> Run | None: ...
    def is_cancel_requested(self, run_id: str) -> bool: ...
    def stale_runs(self, heartbeat_before: datetime, queued_before: datetime) -> list[Run]:
        """Runs whose worker is gone: *running* with no heartbeat since ``heartbeat_before``, or
        *queued* since before ``queued_before`` (the process died between creating the run and
        starting it). Without this a crashed worker would leave a run "active" forever — and with
        it the job, since a non-concurrent job refuses to start while a run is active."""
        ...

    def list_task_runs(self, run_id: str) -> list[TaskRun]: ...
    def update_task_run(self, task_run: TaskRun) -> None: ...

    # ---- logs ----
    def append_logs(self, lines: list[LogLine]) -> None: ...
    def list_logs(self, run_id: str, task_key: str | None, after_seq: int, limit: int) -> list[LogLine]:
        """Lines with ``seq > after_seq`` in order. ``task_key`` filters to one task's lines."""
        ...

    # ---- lifecycle ----
    def ping(self) -> None:
        """Raises if the backing store is unreachable. Backs the health check."""
        ...
