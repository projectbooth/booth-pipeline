"""Persisted record shapes — what the store returns and the API serialises.

Plain dataclasses, kept apart from ``model.py``: the model is what a *user authors* and is
validated; these are what the *system records*. Timestamps are timezone-aware UTC throughout.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .model import PipelineSpec, RetryPolicy, Schedule

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


@dataclass
class PipelineVersion:
    pipeline_id: str
    version: int
    spec: PipelineSpec
    notes: str
    created_by: str
    created_at: datetime


@dataclass
class Job:
    id: str
    workspace: str
    name: str
    pipeline_id: str
    # None = always run the pipeline's latest saved version; an int pins it.
    pipeline_version: int | None
    schedule: Schedule | None
    # Default retry policy for tasks that don't carry their own override.
    retry: RetryPolicy | None
    allow_concurrent_runs: bool
    created_by: str
    created_at: datetime
    updated_at: datetime
    next_run_at: datetime | None = None
    # The `sub` of the user who created the job: whose live role caps the job's workload tokens
    # when it runs unattended (ADR 0058). Distinct from `created_by`, which is a display name.
    owner_sub: str = ""
    # The most a run's token may be granted, whatever the owner holds (least privilege).
    role_ceiling: str = "editor"


@dataclass
class Run:
    id: str
    workspace: str
    job_id: str
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
    # Whose live role caps this run's workload tokens: the job's owner for a scheduled run, the
    # person who pressed "Run now" for a manual one.
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
