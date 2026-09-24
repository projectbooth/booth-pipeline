"""Persisted record shapes — what the store returns and the API serialises.

Plain dataclasses, kept apart from ``model.py``: the model is what a *user authors* and is
validated; these are what the *system records*. Timestamps are timezone-aware UTC throughout.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .model import PipelineSpec, TaskConfig, Trigger

# Run and task-run lifecycle. Terminal states never change again.
RUN_QUEUED, RUN_RUNNING, RUN_SUCCEEDED, RUN_FAILED, RUN_CANCELED = "queued", "running", "succeeded", "failed", "canceled"
RUN_ACTIVE = (RUN_QUEUED, RUN_RUNNING)
RUN_TERMINAL = (RUN_SUCCEEDED, RUN_FAILED, RUN_CANCELED)

TASK_PENDING, TASK_RUNNING, TASK_RETRYING, TASK_SUCCEEDED, TASK_FAILED, TASK_SKIPPED, TASK_CANCELED = (
    "pending",
    "running",
    "retrying",
    "succeeded",
    "failed",
    "skipped",
    "canceled",
)

TRIGGER_MANUAL, TRIGGER_SCHEDULE = "manual", "schedule"


@dataclass
class Pipeline:
    id: str
    workspace: str
    name: str
    description: str
    created_by: str
    created_at: datetime
    updated_at: datetime
    # Derived on read: the highest saved version number, 0 if none has been saved yet.
    latest_version: int = 0
    # Scheduling/triggering/ownership, folded on from the retired Job entity (ADR 0071) — Pipeline
    # is now the only thing that ever runs unattended, so there is no more separate place for
    # these to live.
    schedule: Trigger | None = None
    allow_concurrent_runs: bool = False
    next_run_at: datetime | None = None
    # The `sub` of the user whose live role caps this pipeline's workload tokens when it runs
    # unattended (ADR 0058) — whoever created it, or last saved a schedule onto it. Distinct from
    # `created_by`, which is a display name, not a `sub`.
    owner_sub: str = ""
    # The most a run's token may be granted, whatever the owner holds (least privilege).
    role_ceiling: str = "editor"
    # None: every run (scheduled or manual) uses the latest saved version. Set: every run targets
    # exactly this version, regardless of what is saved on top of it later — mirrors the old Job's
    # `pipeline_version` pin (ADR 0071 "Open question, ruled 2026-09-24", second one).
    pinned_version: int | None = None


@dataclass
class PipelineVersion:
    pipeline_id: str
    version: int
    spec: PipelineSpec
    notes: str
    created_by: str
    created_at: datetime


@dataclass
class TaskEntity:
    """A standalone, reusable Task (ADR 0071) — the direct counterpart of ``Pipeline``: an owned
    resource with its own version history, referenced by ``(id, version)`` from any number of
    pipelines rather than embedded 1:1 in exactly one."""

    id: str
    workspace: str
    name: str
    description: str
    created_by: str
    created_at: datetime
    updated_at: datetime
    # Derived on read: the highest saved version number, 0 if none has been saved yet.
    latest_version: int = 0


@dataclass
class TaskVersionRecord:
    """The direct counterpart of ``PipelineVersion``: one immutable, versioned snapshot of a
    Task's configuration."""

    task_id: str
    version: int
    config: TaskConfig
    notes: str
    created_by: str
    created_at: datetime


@dataclass
class Run:
    id: str
    workspace: str
    pipeline_id: str
    pipeline_version: int
    status: str
    trigger: str
    triggered_by: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    # Which worker owns this run and when it last proved alive — how a crashed worker's runs are
    # noticed and failed instead of staying "running" forever.
    worker_id: str | None = None
    heartbeat_at: datetime | None = None
    cancel_requested: bool = False
    # Whose live role caps this run's workload tokens: the pipeline's owner for a scheduled run,
    # the person who pressed "Run now" for a manual one.
    owner_sub: str = ""


@dataclass
class TaskRun:
    run_id: str
    task_key: str
    status: str = TASK_PENDING
    attempts: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


@dataclass
class LogLine:
    run_id: str
    seq: int  # monotonically increasing per run; the cursor for "give me everything after"
    ts: datetime
    task_key: str | None  # None = a run-level (system) line
    attempt: int
    stream: str  # stdout | stderr | system
    message: str


@dataclass
class Page:
    items: list[Any] = field(default_factory=list)
    total: int = 0
