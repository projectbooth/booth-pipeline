"""Request bodies for the HTTP API (what a client sends; responses are built in ``api.py``)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .model import EventTrigger, PipelineSpec, TaskConfig, Trigger, Wire

MAX_NAME = 200
MAX_DESCRIPTION = 4000
MAX_NOTES = 4000


class PipelineCreate(Wire):
    name: str = Field(min_length=1, max_length=MAX_NAME)
    description: str = Field("", max_length=MAX_DESCRIPTION)
    # Optional first version. Omit it to create an empty pipeline and draw it in the builder.
    spec: PipelineSpec | None = None
    notes: str = Field("", max_length=MAX_NOTES)


class PipelineUpdate(Wire):
    name: str = Field(min_length=1, max_length=MAX_NAME)
    description: str = Field("", max_length=MAX_DESCRIPTION)


class VersionCreate(Wire):
    spec: PipelineSpec
    notes: str = Field("", max_length=MAX_NOTES)


class ValidateRequest(Wire):
    spec: PipelineSpec


class PipelineDraftSave(Wire):
    """Plain "Save" (ADR 0073): writes the draft in place, no version created."""

    spec: PipelineSpec


class PipelineScheduleUpdate(Wire):
    """Sets a pipeline's own trigger and unattended-run settings directly (ADR 0071 folds these
    onto Pipeline; there is no more separate Job to hold them)."""

    schedule: Trigger | None = None
    allow_concurrent_runs: bool = False
    # The most a run's platform token (for tasks that opt in) may be granted; capped again by the
    # owner's live role. Never "owner".
    role_ceiling: Literal["viewer", "editor"] = "editor"
    # None: every run tracks the latest saved version (the default). An explicit version pins every
    # scheduled/manual run to it, mirroring the old Job's `pipeline_version` pin (ADR 0071's second
    # "Open question, ruled 2026-09-24").
    pinned_version: int | None = Field(None, ge=1)
    # ADR 0123: beside `schedule`, not instead of it. Left out of the body (an older UI), the saved one
    # is kept; null removes it.
    event_trigger: EventTrigger | None = None


class PipelineOwnerUpdate(Wire):
    """Who the pipeline's scheduled runs act as: ``"me"`` (Take over) or ``svc:<workspace>:<name>``."""

    owner: str = Field(min_length=1, max_length=256)


class TaskCreate(Wire):
    name: str = Field(min_length=1, max_length=MAX_NAME)
    description: str = Field("", max_length=MAX_DESCRIPTION)
    # Optional first version. Omit it to create an empty, unconfigured task and fill it in later.
    config: TaskConfig | None = None
    notes: str = Field("", max_length=MAX_NOTES)


class TaskUpdate(Wire):
    name: str = Field(min_length=1, max_length=MAX_NAME)
    description: str = Field("", max_length=MAX_DESCRIPTION)


class TaskVersionCreate(Wire):
    config: TaskConfig
    notes: str = Field("", max_length=MAX_NOTES)


class TaskDraftSave(Wire):
    """Plain "Save" (ADR 0073): writes the draft in place, no version created."""

    config: TaskConfig
