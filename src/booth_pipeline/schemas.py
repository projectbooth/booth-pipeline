"""Request bodies for the HTTP API (what a client sends; responses are built in ``api.py``)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .model import PipelineSpec, RetryPolicy, Schedule, Wire

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


class JobInput(Wire):
    name: str = Field(min_length=1, max_length=MAX_NAME)
    pipeline_id: str = Field(min_length=1, max_length=64)
    # null = follow the pipeline's latest saved version; an integer pins that version.
    pipeline_version: int | None = Field(None, ge=1)
    schedule: Schedule | None = None
    retry: RetryPolicy | None = None
    allow_concurrent_runs: bool = False
    # The most a run's platform token (for tasks that opt in) may be granted; capped again by the
    # owner's live role. Never "owner".
    role_ceiling: Literal["viewer", "editor"] = "editor"
