"""The persistence interface.

Two implementations (``memory.py`` for tests/dev, ``postgres.py`` for real) satisfy it, and one
contract suite (``tests/unit/test_store_contract.py``) runs against both — so they cannot drift.
Every method that takes a ``workspace`` scopes to it: a record in another workspace is
indistinguishable from a missing one (multi-workspace tenancy from v0, ADR 0008).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from ..model import PipelineSpec, TaskConfig
from ..records import (
    RUN_TERMINAL,
    TASK_PENDING,
    TASK_SKIPPED,
    EventTriggerState,
    LogLine,
    Page,
    Pipeline,
    PipelineDraft,
    PipelineVersion,
    Run,
    TaskDraft,
    TaskEntity,
    TaskRun,
    TaskSecret,
    TaskVersionRecord,
)
from ..triggers import TriggerEvent


def initial_task_status(run: Run) -> str:
    """A run created already finished (a dropped scheduled fire, recorded as skipped) has tasks that
    will never run: they start skipped, not pending."""
    return TASK_SKIPPED if run.status in RUN_TERMINAL else TASK_PENDING


class Conflict(Exception):
    """A uniqueness rule was violated (e.g. a pipeline name already used in the workspace)."""

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.field = field


class Busy(Exception):
    """An exclusive run was refused because the pipeline already has an active run."""


class InUse(Exception):
    """The record cannot be removed because something still depends on it."""


class Store(Protocol):
    # ---- pipelines ----
    # Scheduling/triggering/ownership live directly on Pipeline (ADR 0071 retires the separate Job
    # entity these used to belong to) — `create_pipeline`/`update_pipeline` below only cover
    # name/description, the same fields they always did; a pipeline's schedule fields are set via
    # `update_schedule`, mirroring how a version is its own separate `add_version` call.
    def create_pipeline(self, workspace: str, name: str, description: str, by: str) -> Pipeline: ...
    def get_pipeline(self, workspace: str, pipeline_id: str) -> Pipeline | None: ...
    def list_pipelines(self, workspace: str, q: str, limit: int, offset: int, projects: frozenset[str] | None = None) -> Page:
        """``projects``: only pipelines in one of these projects (ADR 0124's ``?project=``); None: all."""
        ...

    def set_pipeline_project(self, workspace: str, pipeline_id: str, project: str | None) -> Pipeline | None: ...
    def update_pipeline(self, workspace: str, pipeline_id: str, name: str, description: str) -> Pipeline | None: ...
    def delete_pipeline(self, workspace: str, pipeline_id: str) -> bool:
        """False if absent."""
        ...

    def update_schedule(self, pipeline: Pipeline) -> Pipeline | None:
        """Persist `pipeline`'s schedule/allowConcurrentRuns/nextRunAt/owner/roleCeiling/
        pinnedVersion fields — everything folded on from the retired Job entity, plus
        pinnedVersion (ADR 0071's second "Open question, ruled 2026-09-24"). None if the pipeline
        is absent."""
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

    def get_pipeline_draft(self, workspace: str, pipeline_id: str) -> PipelineDraft | None:
        """None if the pipeline is absent, or if nothing has ever been saved onto it (neither a
        plain Save nor a "Save as new version" — ADR 0073)."""
        ...

    def save_pipeline_draft(self, workspace: str, pipeline_id: str, spec: PipelineSpec, by: str) -> PipelineDraft | None:
        """Overwrite the draft in place. Called by plain Save directly, and by ``add_version`` too
        (a "Save as new version" also updates the draft, so it is never behind the latest
        immutable version — ADR 0073). None if the pipeline is absent."""
        ...

    # ---- tasks (ADR 0071) ----
    # The direct counterpart of pipelines/versions above: a standalone, versioned, reusable
    # resource, referenced by `(id, version)` from any number of pipelines.
    def create_task(self, workspace: str, name: str, description: str, by: str) -> TaskEntity: ...
    def get_task(self, workspace: str, task_id: str) -> TaskEntity | None: ...
    def list_tasks(self, workspace: str, q: str, limit: int, offset: int, projects: frozenset[str] | None = None) -> Page: ...
    def set_task_project(self, workspace: str, task_id: str, project: str | None) -> TaskEntity | None: ...
    def update_task(self, workspace: str, task_id: str, name: str, description: str) -> TaskEntity | None: ...
    def delete_task(self, workspace: str, task_id: str) -> bool:
        """False if absent. Raises ``InUse`` while any pipeline version still references it."""
        ...

    def add_task_version(self, workspace: str, task_id: str, config: TaskConfig, notes: str, by: str) -> TaskVersionRecord | None:
        """Append the next immutable version (1, 2, ...). None if the task is absent."""
        ...

    def get_task_version(self, workspace: str, task_id: str, version: int | None) -> TaskVersionRecord | None:
        """``None`` means the latest."""
        ...

    def list_task_versions(self, workspace: str, task_id: str, limit: int, offset: int) -> Page:
        """Newest first."""
        ...

    def get_task_draft(self, workspace: str, task_id: str) -> TaskDraft | None:
        """None if the task is absent, or if nothing has ever been saved onto it (ADR 0073)."""
        ...

    def save_task_draft(self, workspace: str, task_id: str, config: TaskConfig, by: str) -> TaskDraft | None:
        """Overwrite the draft in place. Called by plain Save directly, and by ``add_task_version``
        too, so the draft is never behind the latest immutable version. None if absent."""
        ...

    # ---- scheduling ----
    def claim_due_pipelines(self, now: datetime, limit: int) -> list[Pipeline]:
        """Across all workspaces: pipelines whose schedule is enabled and ``next_run_at <= now``.

        Each returned pipeline's ``next_run_at`` has *already been advanced* to its next fire time
        in the same atomic step, so with several replicas each due fire is claimed exactly once.
        A write (row locks in the SQL store) — the scheduler calls this only when it actually
        believes something is due, never on a fixed cadence (ADR 0065; see ``list_upcoming``).
        """
        ...

    def list_upcoming(self, limit: int) -> list[Pipeline]:
        """Across all workspaces: pipelines whose schedule is enabled and has a ``next_run_at``,
        the soonest first. A plain read — no claiming, no locking, ``next_run_at`` untouched.

        This is the scheduler's in-memory index (ADR 0065): called on a coarser cadence than a
        claim, so a fleet of replicas can each know "what's coming up" without every one of them
        running a locking write query every few seconds. Bounded staleness — a pipeline created or
        rescheduled by another replica is visible within one refresh, not instantly.
        """
        ...

    # ---- event triggers (ADR 0123; the rules are triggers.py's, applied under the state row's lock) ----
    def ingest_trigger_event(self, event: TriggerEvent, seq: int | None, now: datetime) -> dict[str, str] | None:
        """One event off the bus, in one transaction. ``seq`` (its JetStream stream sequence) is
        recorded; one already recorded is a redelivery and returns None, changing nothing. Otherwise
        returns ``{pipeline_id: outcome}`` for every enabled event trigger in the event's workspace
        that matches it (``triggers.ingest``'s outcomes)."""
        ...

    def claim_due_event_triggers(self, now: datetime, limit: int) -> list[tuple[Pipeline, dict[str, Any]]]:
        """Across all workspaces: event triggers whose ``fire_at`` has come, claimed exactly once
        across replicas (row locks, SKIP LOCKED). Returns those that should start a run now, each with
        the run's ``trigger_events``; one whose pipeline is busy is queued or dropped instead."""
        ...

    def requeue_event_trigger(self, pipeline_id: str, fired: dict[str, Any], now: datetime) -> None:
        """A claimed fire whose run couldn't be created (the pipeline became busy in between)."""
        ...

    def next_event_fire_at(self) -> datetime | None:
        """The soonest pending event-trigger fire, for the scheduler's index. A plain read."""
        ...

    def get_event_trigger_state(self, workspace: str, pipeline_id: str) -> EventTriggerState | None: ...

    def event_triggers_missed_events(self, now: datetime) -> int:
        """The bus may have lost events: every enabled event trigger fires once. Returns how many."""
        ...

    def trigger_bus(self) -> tuple[datetime, datetime] | None:
        """The shared consumer's record: ``(created_at, last_contact_at)``, or None before it exists."""
        ...

    def record_trigger_bus_contact(self, now: datetime) -> None: ...

    def prune_seen_trigger_events(self, before: datetime) -> int: ...

    # ---- runs ----
    def create_run(self, run: Run, task_keys: list[str], exclusive: bool = False) -> Run:
        """Creates the run and one pending task run per key (skipped, for a run created finished).

        ``exclusive``: atomically refuse (``Busy``) if the pipeline already has an active run. The
        check and the insert happen under one lock on the pipeline, so two simultaneous triggers
        cannot both succeed — a plain "count, then insert" would let them.
        """
        ...

    def get_run(self, workspace: str, run_id: str) -> Run | None: ...
    def list_runs(self, workspace: str, pipeline_id: str | None, status: str | None, limit: int, offset: int) -> Page:
        """Newest first."""
        ...

    def count_active_runs(self, workspace: str, pipeline_id: str) -> int: ...
    def mark_running(self, run_id: str, worker_id: str, now: datetime) -> None: ...
    def heartbeat(self, run_id: str, now: datetime) -> None: ...
    def finish_run(self, run_id: str, status: str, error: str | None, now: datetime) -> bool:
        """Set a terminal status. No-op (False) if the run is already terminal — first writer wins.
        In the same step, releases a queue_one follow-up owed to the run's pipeline (ADR 0123)."""
        ...

    def request_cancel(self, workspace: str, run_id: str) -> Run | None: ...
    def is_cancel_requested(self, run_id: str) -> bool: ...
    def stale_runs(self, heartbeat_before: datetime, queued_before: datetime) -> list[Run]:
        """Runs whose worker is gone: *running* with no heartbeat since ``heartbeat_before``, or
        *queued* since before ``queued_before`` (the process died between creating the run and
        starting it). Without this a crashed worker would leave a run "active" forever — and with
        it the pipeline, since a non-concurrent pipeline refuses to start while a run is active."""
        ...

    def list_task_runs(self, run_id: str) -> list[TaskRun]: ...
    def update_task_run(self, task_run: TaskRun) -> None: ...

    # ---- task secrets (ADR 0128) ----
    def list_task_secrets(self, workspace: str) -> list[TaskSecret]:
        """By name."""
        ...

    def get_task_secret(self, workspace: str, name: str) -> TaskSecret | None: ...
    def put_task_secret(self, secret: TaskSecret) -> None:
        """Creates or replaces."""
        ...

    def delete_task_secret(self, workspace: str, name: str) -> bool: ...
    def task_secrets_sealed_with_other_keys(self, key_id: str) -> list[TaskSecret]:
        """Every workspace's secrets not sealed with ``key_id``: what a key rotation re-seals."""
        ...

    # ---- logs ----
    def append_logs(self, lines: list[LogLine]) -> None: ...
    def list_logs(self, run_id: str, task_key: str | None, after_seq: int, limit: int, exclude_streams: tuple[str, ...] = ()) -> list[LogLine]:
        """Lines with ``seq > after_seq`` in order. ``task_key`` filters to one task's lines."""
        ...

    # ---- lifecycle ----
    def ping(self) -> None:
        """Raises if the backing store is unreachable. Backs the health check."""
        ...
