"""Persisted record shapes — what the store returns and the API serialises.

Plain dataclasses, kept apart from ``model.py``: the model is what a *user authors* and is
validated; these are what the *system records*. Timestamps are timezone-aware UTC throughout.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .model import EventTrigger, PipelineSpec, TaskConfig, Trigger

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

TRIGGER_MANUAL, TRIGGER_SCHEDULE, TRIGGER_EVENT = "manual", "schedule", "event"


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
    # Who this pipeline's scheduled runs act as (ADR 0058, ADR 0120): a person's `sub`, or a service
    # account `svc:<workspace>:<name>`, whose live role (capped by role_ceiling) core mints for. Set
    # by whoever FIRST saves a schedule, and afterwards only by Take over or a workspace owner
    # choosing a service account (ADR 0119 amendment 3). Distinct from `created_by`, a display name.
    owner_sub: str = ""
    # How the UI names the owner, taken when it was set (empty for owners set before names were kept).
    owner_name: str = ""
    # The most a run's token may be granted, whatever the owner holds (least privilege).
    role_ceiling: str = "editor"
    # None: every run (scheduled or manual) uses the latest saved version. Set: every run targets
    # exactly this version, regardless of what is saved on top of it later — mirrors the old Job's
    # `pipeline_version` pin (ADR 0071 "Open question, ruled 2026-09-24", second one).
    pinned_version: int | None = None
    # Derived on read: whether a mutable draft (ADR 0073) has ever been saved — separate from
    # latest_version above, since a draft can exist without ever having been promoted to a version.
    has_draft: bool = False
    draft_updated_at: datetime | None = None
    # ADR 0123: runs when files are created in booth-storage or rows written through booth-api, beside
    # (not instead of) `schedule`. `event_trigger_since`: when it was last enabled or its match changed;
    # events published before that are not its business.
    event_trigger: EventTrigger | None = None
    event_trigger_since: datetime | None = None
    # ADR 0124: a navigation label from booth-core's project registry, or None. Grants nothing; nothing
    # that decides access, schedules, triggers or run-as reads it.
    project: str | None = None


@dataclass
class EventTriggerState:
    """Where a pipeline's event trigger stands (ADR 0123), kept in the database so a restart neither
    loses a pending fire nor runs it twice. ``triggers.py`` holds the rules; the store applies them
    under a row lock."""

    pipeline_id: str
    workspace: str
    pending_since: datetime | None = None  # the first event of the burst not yet fired
    last_event_at: datetime | None = None  # when the latest matching event was received
    fire_at: datetime | None = None  # when the scheduler starts the run; None = nothing due
    queued: bool = False  # queue_one: run once more when the active run ends
    last_fired_at: datetime | None = None
    events: list[dict[str, Any]] = field(default_factory=list)  # the first MAX_RUN_EVENTS of the burst
    event_count: int = 0  # every matching event of the burst
    missed_events: bool = False  # set by a bus outage longer than the stream keeps events
    ignored_own_runs: int = 0  # written by this pipeline's own runs (the loop guard), all time
    skipped_while_running: int = 0  # dropped under `skip`, all time
    last_event: dict[str, Any] | None = None
    gap_detected_at: datetime | None = None


@dataclass
class PipelineVersion:
    pipeline_id: str
    version: int
    spec: PipelineSpec
    notes: str
    created_by: str
    created_at: datetime


@dataclass
class PipelineDraft:
    """A pipeline's mutable, unversioned "current" state (ADR 0073): what plain "Save" writes to,
    distinct from the immutable ``PipelineVersion`` history. "Save as new version" also writes
    here, so the draft is never behind the latest version — it is always at least as fresh."""

    pipeline_id: str
    spec: PipelineSpec
    updated_by: str
    updated_at: datetime


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
    # Derived on read: whether a mutable draft (ADR 0073) has ever been saved — a task can be
    # "configured" (have real, runnable code) via its draft alone, without any saved version.
    has_draft: bool = False
    draft_updated_at: datetime | None = None
    # ADR 0124: as Pipeline.project.
    project: str | None = None


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
class TaskDraft:
    """A task's mutable, unversioned "current" state (ADR 0073) — the direct counterpart of
    ``PipelineDraft``. What plain "Save" writes to; "Save as new version" also writes here, so the
    draft is never behind the latest version."""

    task_id: str
    config: TaskConfig
    updated_by: str
    updated_at: datetime


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
    owner_name: str = ""
    # ADR 0123: for an event-fired run, what fired it: {events: [...first 100], count, missedEvents}.
    trigger_events: dict[str, Any] | None = None


@dataclass
class TaskRun:
    run_id: str
    task_key: str
    status: str = TASK_PENDING
    attempts: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    # ADR 0119: where the latest attempt ran outside its pod ({kind: spark, id, state, reason}).
    external: dict[str, Any] | None = None
    # What the successful attempt returned, as the run keeps it (runs.kept_output): {"value": ...}, or
    # {"omitted": "tooLarge"|"unstorable", "bytes": n}. None until an attempt succeeds.
    output: dict[str, Any] | None = None


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
