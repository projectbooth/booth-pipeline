"""Request bodies for the HTTP API (what a client sends; responses are built in ``api.py``)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .model import PipelineSpec, TaskConfig, Trigger, Wire

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


class PipelineScheduleUpdate(Wire):
    """Sets a pipeline's own trigger and unattended-run settings directly (ADR 0071 folds these
    onto Pipeline; there is no more separate Job to hold them)."""

    schedule: Trigger | None = None
    allow_concurrent_runs: bool = False
    # The most a run's platform token (for tasks that opt in) may be granted; capped again by the
    # owner's live role. Never "owner".
    role_ceiling: Literal["viewer", "editor"] = "editor"


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
