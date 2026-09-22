"""The pipeline domain model: what a saved Pipeline *is*.

Terminology is fixed by ADR 0010 and the brief — do not rename:

* **Pipeline** — a DAG definition, drawn on the canvas, versioned. Each save is an immutable
  ``PipelineVersion`` holding a ``PipelineSpec``.
* **Task** — one node of that DAG: a source, transform or sink, with its own code reference,
  target runner and retry override.
* **Job** — a schedulable/triggerable instance of a pipeline (see ``jobs.py``/``service.py``).

The spec is the single artifact everything else derives from: the canvas edits it, ``engine``
compiles it to a Dagster job, and a Run records which version of it ran. It is therefore
self-contained on purpose — code is *snapshotted into it* at save time (see ``CodeRef``) so a run
never has to call another module and a version's behaviour can never change after it is saved.

JSON on the wire is camelCase, like every other Booth module's API.
"""

from __future__ import annotations

import hashlib
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

# Bounds. Each exists so one request cannot make the service hold or execute something unbounded.
MAX_TASKS = 200
MAX_INLINE_SOURCE_BYTES = 256 * 1024
MAX_SPEC_SOURCE_BYTES = 8 * 1024 * 1024  # all snapshotted source in one version, catalog + inline
MAX_PARAMS_BYTES = 64 * 1024
MAX_RETRIES = 10
MAX_RETRY_DELAY_SECONDS = 3600
MAX_TIMEOUT_SECONDS = 24 * 3600
DEFAULT_TIMEOUT_SECONDS = 3600

# A task key becomes a Dagster op name and an upstream-input name in user code, so it is a
# restricted identifier: lowercase, starts with a letter, no spaces.
TASK_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

Kind = Literal["source", "transform", "sink"]
Backoff = Literal["fixed", "linear", "exponential"]

# The one runner every install has. Any other id is only valid if a runner with that id is
# registered and available (runners/registry.py) — ADR 0006: Spark is never a default.
BASE_RUNNER = "base"

# The highest role a run's workload token may carry. Never "owner": nothing a pipeline does needs
# workspace administration, and a token that can't administer can't be turned into one.
ROLE_CEILINGS = ("viewer", "editor")


class Wire(BaseModel):
    """Base for everything crossing the API: camelCase JSON, unknown fields rejected.

    Rejecting unknown fields turns a typo (``maxRetrys``) into a 422 instead of a silently
    default retry policy — a quiet misconfiguration is worse than a loud one here.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class ModelError(ValueError):
    """A spec violates a rule. ``field`` is a dotted path the UI can attach the message to."""

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.field = field


def sha256_text(text: str) -> str:
    """Hash over the UTF-8 bytes — the same bytes booth-catalog stores, byte for byte."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RetryPolicy(Wire):
    """A task's retry override (or a job's default). Maps onto Dagster's ``RetryPolicy``."""

    max_retries: int = Field(0, ge=0, le=MAX_RETRIES)
    delay_seconds: float = Field(0, ge=0, le=MAX_RETRY_DELAY_SECONDS)
    backoff: Backoff = "fixed"


class Position(Wire):
    """Where a node sits on the canvas. Purely presentational; never affects execution."""

    x: float = 0
    y: float = 0


class CatalogCode(Wire):
    """Code from booth-catalog's code catalog — the v0 code-catalog reference shape (ADR 0010).

    Deliberately just ``{entryId, version}``. At authoring time ``version`` may be the alias
    ``"latest"``; at save time the service resolves it to the concrete label and fills
    ``name``/``sha256``/``source`` from the catalog, so what is stored is a *snapshot*:

    * a run never calls the catalog (it works with the catalog down or uninstalled), and
    * "latest" is pinned at save time, not silently re-resolved at 3am by the scheduler.

    This relies on the guarantees booth-catalog recorded in its docs/decisions/0006 (versions are
    immutable; ``latest`` returns the concrete label; a deleted entry is 404). See our own
    docs/decisions/0001.
    """

    type: Literal["catalog"] = "catalog"
    entry_id: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=64)
    # Filled by the service on save; ignored (and overwritten) if a client sends them.
    name: str | None = None
    language: str | None = None  # advisory label from the catalog; checked against the runner at save
    sha256: str | None = None
    source: str | None = None

    @property
    def resolved(self) -> bool:
        return self.source is not None and self.sha256 is not None and self.version != "latest"


class InlineCode(Wire):
    """Code the user wrote in the builder ("the user's own workspace" — see docs/decisions/0001).

    This is what makes the base runner work with zero other modules installed: no catalog, no
    storage, no anything.
    """

    type: Literal["inline"] = "inline"
    source: str = Field(min_length=1)
    sha256: str | None = None

    @field_validator("source")
    @classmethod
    def _source_ok(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("source is required")
        if "\x00" in v:
            raise ValueError("source must not contain NUL bytes")
        if len(v.encode("utf-8")) > MAX_INLINE_SOURCE_BYTES:
            raise ValueError(f"inline source is limited to {MAX_INLINE_SOURCE_BYTES} bytes")
        return v


Code = Annotated[CatalogCode | InlineCode, Field(discriminator="type")]


class Task(Wire):
    """One node in the DAG."""

    key: str
    name: str = Field(default="", max_length=200)
    kind: Kind
    code: Code
    runner: str = BASE_RUNNER
    retry: RetryPolicy | None = None
    depends_on: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = Field(DEFAULT_TIMEOUT_SECONDS, ge=1, le=MAX_TIMEOUT_SECONDS)
    # Opt-in, per task, to a short-lived platform token (ADR 0056) so ctx.storage / ctx.catalog work.
    # Off by default: a task that doesn't need booth-storage/booth-catalog never triggers a mint, so
    # it can't be blocked by (or leak) an identity it doesn't use. See docs/decisions/0007.
    platform_access: bool = False
    position: Position = Field(default_factory=Position)

    @field_validator("key")
    @classmethod
    def _key_ok(cls, v: str) -> str:
        if not TASK_KEY_RE.match(v):
            raise ValueError(
                "must be lowercase letters, digits and underscores, starting with a letter (max 63)"
            )
        return v

    @field_validator("params")
    @classmethod
    def _params_ok(cls, v: dict[str, Any]) -> dict[str, Any]:
        import json

        try:
            size = len(json.dumps(v))
        except (TypeError, ValueError) as e:
            raise ValueError(f"params must be JSON-serialisable: {e}") from e
        if size > MAX_PARAMS_BYTES:
            raise ValueError(f"params are limited to {MAX_PARAMS_BYTES} bytes")
        return v

    @property
    def display_name(self) -> str:
        return self.name or self.key


class PipelineSpec(Wire):
    """The whole DAG. Edges are each task's ``dependsOn`` — an edge exists iff a task names
    another as a dependency, so there is no separate edge list that could disagree with it."""

    tasks: list[Task] = Field(default_factory=list, max_length=MAX_TASKS)

    def by_key(self) -> dict[str, Task]:
        return {t.key: t for t in self.tasks}

    def downstream(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {t.key: [] for t in self.tasks}
        for t in self.tasks:
            for d in t.depends_on:
                if d in out:
                    out[d].append(t.key)
        return out

    def total_source_bytes(self) -> int:
        n = 0
        for t in self.tasks:
            src = t.code.source
            if src:
                n += len(src.encode("utf-8"))
        return n


def topological_order(spec: PipelineSpec) -> list[str]:
    """Task keys in a valid execution order (Kahn's algorithm, ties broken by declaration order
    so the order — and therefore run logs — is deterministic). Raises ``ModelError`` on a cycle."""
    indeg = {t.key: len(set(t.depends_on)) for t in spec.tasks}
    down = spec.downstream()
    ready = [t.key for t in spec.tasks if indeg[t.key] == 0]
    order: list[str] = []
    while ready:
        k = ready.pop(0)
        order.append(k)
        for nxt in down[k]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                ready.append(nxt)
    if len(order) != len(spec.tasks):
        stuck = sorted(set(indeg) - set(order))
        raise ModelError(f"the tasks form a cycle: {', '.join(stuck)}", "tasks")
    return order


def validate_structure(spec: PipelineSpec, available_runners: set[str] | None = None) -> None:
    """Enforce the DAG rules the brief states: source -> transform(s) -> sink.

    Raises ``ModelError`` naming the first problem and the field it is about
    (``tasks[2].dependsOn``), so the canvas can mark the exact node. Pure: no I/O, so it runs
    identically for the live validation endpoint, at save time, and in tests.
    """
    if not spec.tasks:
        raise ModelError("a pipeline needs at least one task", "tasks")

    seen: dict[str, int] = {}
    for i, t in enumerate(spec.tasks):
        if t.key in seen:
            raise ModelError(f"duplicate task key {t.key!r}", f"tasks[{i}].key")
        seen[t.key] = i

    for i, t in enumerate(spec.tasks):
        f = f"tasks[{i}]"
        if len(set(t.depends_on)) != len(t.depends_on):
            raise ModelError(f"{t.key!r} lists the same dependency twice", f"{f}.dependsOn")
        for d in t.depends_on:
            if d == t.key:
                raise ModelError(f"{t.key!r} cannot depend on itself", f"{f}.dependsOn")
            if d not in seen:
                raise ModelError(f"{t.key!r} depends on {d!r}, which is not a task in this pipeline", f"{f}.dependsOn")
        if t.kind == "source" and t.depends_on:
            raise ModelError(f"source {t.key!r} cannot have dependencies — a source is where data comes from", f"{f}.dependsOn")
        if t.kind in ("transform", "sink") and not t.depends_on:
            raise ModelError(f"{t.kind} {t.key!r} needs at least one upstream task", f"{f}.dependsOn")
        if available_runners is not None and t.runner not in available_runners:
            raise ModelError(
                f"runner {t.runner!r} is not available; available: {', '.join(sorted(available_runners))}",
                f"{f}.runner",
            )

    down = spec.downstream()
    for i, t in enumerate(spec.tasks):
        if t.kind == "sink" and down[t.key]:
            raise ModelError(f"sink {t.key!r} cannot have downstream tasks — a sink is where data ends up", f"tasks[{i}].kind")

    topological_order(spec)  # cycle check

    if spec.total_source_bytes() > MAX_SPEC_SOURCE_BYTES:
        raise ModelError(f"the pipeline's code totals more than {MAX_SPEC_SOURCE_BYTES} bytes", "tasks")


# ---- Job-level config ----------------------------------------------------------------------


class Schedule(Wire):
    """A basic cron schedule (5 fields, minute granularity) in an IANA timezone."""

    cron: str = Field(min_length=1, max_length=100)
    timezone: str = "UTC"
    enabled: bool = True

    @model_validator(mode="after")
    def _valid(self) -> Schedule:
        from .schedule import validate_schedule

        validate_schedule(self.cron, self.timezone)
        return self
