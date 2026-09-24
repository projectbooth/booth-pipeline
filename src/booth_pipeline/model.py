"""The pipeline domain model: what a saved Pipeline *is*, and what a standalone Task *is*.

Terminology is fixed by ADR 0010, restructured by ADR 0071 — do not rename:

* **Task** — a standalone, independently versioned, reusable resource: a unit of code (from
  ``booth-catalog`` or a ``booth-storage`` file, never typed inline — ADR 0063) that runs against
  a chosen engine (``runners/languages.py``'s registry — ADR 0064). ``TaskConfig`` is one
  version's worth of it — the direct counterpart of ``PipelineSpec``, just for a Task instead of a
  Pipeline. A Task has no ``kind`` (source/transform/sink) — that field is retired entirely, not
  merely optional (ADR 0062 made it decorative; ADR 0071 removes it).
* **Pipeline** — a DAG definition, drawn on the canvas, versioned. Each save is an immutable
  ``PipelineVersion`` holding a ``PipelineSpec``. A DAG node (``TaskRef``) is a *reference* to a
  specific ``(task_id, task_version)`` pair plus this pipeline's own wiring (dependencies,
  position) — never an embedded task definition. Pipeline owns scheduling, triggering and run
  history directly; there is no separate Job entity (ADR 0071 retires it).

A spec (``PipelineSpec`` or ``TaskConfig``) is the artifact its own version history is built from.
Code is *snapshotted* into a ``TaskConfig`` at save time (see ``CatalogCode``/``StorageCode``) so a
run never has to call another module and a task version's behaviour can never change after it is
saved. A ``PipelineSpec`` pins the specific task versions it references at save time the same way —
updating a task creates a new task version but never retroactively changes a pipeline version that
still points at the old one.

JSON on the wire is camelCase, like every other Booth module's API.
"""

from __future__ import annotations

import hashlib
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

# Bounds. Each exists so one request cannot make the service hold or execute something unbounded.
MAX_TASKS = 200  # nodes in one pipeline's DAG
MAX_INLINE_SOURCE_BYTES = 256 * 1024
MAX_TASK_SOURCE_BYTES = 8 * 1024 * 1024  # one task version's own snapshotted source (catalog/storage)
MAX_PARAMS_BYTES = 64 * 1024
MAX_RETRIES = 10
MAX_RETRY_DELAY_SECONDS = 3600
MAX_TIMEOUT_SECONDS = 24 * 3600
DEFAULT_TIMEOUT_SECONDS = 3600

# A DAG node's local key becomes a Dagster op name and an upstream-input name in user code, so it
# is a restricted identifier: lowercase, starts with a letter, no spaces.
TASK_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

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
    """A task version's retry policy. Maps onto Dagster's ``RetryPolicy``."""

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


class StorageCode(Wire):
    """Code from booth-storage — a save-time snapshot (ADR 0063), structurally parallel to
    ``CatalogCode``: ``{backendId, path}``.

    Unlike a catalog entry, a storage object has no immutable version to pin to — the
    ``{backendId, path}`` pair *is* the reference, and it stays resolvable to whatever is at that
    path right now. Re-resolution therefore follows the same rule as an already-pinned catalog
    reference: the service reuses whatever it already snapshotted for this exact ``{backendId,
    path}`` in the pipeline's own previous version, rather than re-fetching on every save — so
    saving an unrelated edit still works while storage is unreachable, and a task's code cannot
    change out from under a run just because the file at that path was overwritten later. Picking
    the file again in the builder (a fresh reference) is what pulls in new content.
    """

    type: Literal["storage"] = "storage"
    backend_id: str = Field(min_length=1, max_length=128)
    path: str = Field(min_length=1, max_length=4096)
    # Filled by the service on save; ignored (and overwritten) if a client sends them.
    name: str | None = None  # the path's basename, for display
    language: str | None = None  # inferred from the path's extension at save time
    sha256: str | None = None
    source: str | None = None

    @property
    def resolved(self) -> bool:
        return self.source is not None and self.sha256 is not None


class InlineCode(Wire):
    """Code the user wrote in the builder — the pre-ADR-0063 way of authoring code, kept only for
    back-compat ("the user's own workspace" — see docs/decisions/0001). The builder no longer
    offers a path to create new inline code (ADR 0063): a pipeline saved before that still loads
    and runs exactly as it did, but new tasks pick a catalog or storage reference instead.

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


Code = Annotated[CatalogCode | InlineCode | StorageCode, Field(discriminator="type")]


def code_language(code: Code) -> str:
    """What language a task's code executes as (ADR 0064) — what the base runner's per-language
    dispatch (``runners/languages.py``) reads. ``InlineCode`` has no language field at all: it is
    the builder's old free-text path, which only ever wrote Python, so it is always "python".
    ``CatalogCode``'s ``language`` is an advisory label from the catalog, defaulting the same way
    when the catalog didn't say."""
    if isinstance(code, InlineCode):
        return "python"
    return code.language or "python"


class TaskConfig(Wire):
    """One version's worth of a standalone Task's configuration (ADR 0071) — what actually runs.
    The direct counterpart of ``PipelineSpec``, just for a Task instead of a Pipeline: a Task's
    own version history is independent of any pipeline that references it, which is what makes
    reuse across pipelines possible (today's embedded, 1:1 tasks could never be reused at all).

    Deliberately has no ``key``, ``dependsOn`` or ``position`` — those describe how one particular
    ``TaskRef`` is wired into one particular pipeline's DAG, not anything about the Task itself.
    Also has no ``kind`` (source/transform/sink): ADR 0062 already made it purely decorative;
    ADR 0071 removes the field entirely rather than continuing to carry a label that constrains
    nothing.
    """

    code: Code
    runner: str = BASE_RUNNER
    retry: RetryPolicy | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = Field(DEFAULT_TIMEOUT_SECONDS, ge=1, le=MAX_TIMEOUT_SECONDS)
    # Opt-in to a short-lived platform token (ADR 0056) so ctx.storage / ctx.catalog work. Off by
    # default: a task that doesn't need booth-storage/booth-catalog never triggers a mint, so it
    # can't be blocked by (or leak) an identity it doesn't use. See docs/decisions/0007.
    platform_access: bool = False

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


def validate_task_config(config: TaskConfig, available_runners: set[str] | None = None) -> None:
    """Everything about a ``TaskConfig``'s validity that pydantic's own field constraints (params
    size, timeout bounds, the ``code`` union's own shape) don't already enforce. Pure: no I/O, so
    it runs identically whether called live or at save time — the same shape as
    ``validate_structure`` below, just for a task's own config instead of a pipeline's DAG.

    Runner availability and the task's own snapshotted source size are now checked HERE, at the
    task's own save time — not at the pipeline's, since both are properties of the Task, not of
    any pipeline that happens to reference it (ADR 0071 moves this off the pipeline entirely).
    """
    if available_runners is not None and config.runner not in available_runners:
        raise ModelError(f"runner {config.runner!r} is not available; available: {', '.join(sorted(available_runners))}", "runner")
    src = config.code.source
    if src and len(src.encode("utf-8")) > MAX_TASK_SOURCE_BYTES:
        raise ModelError(f"the task's code is more than {MAX_TASK_SOURCE_BYTES} bytes", "code")


class TaskRef(Wire):
    """One node in a Pipeline's DAG (ADR 0071): a reference to a specific standalone Task's
    ``(task_id, task_version)`` pair, plus this pipeline's own wiring around it.

    Dependency edges and canvas position are properties of the REFERENCE — this pipeline's DAG
    shape — never of the Task resource being referenced, which may be wired in completely
    differently by a different pipeline, or by a later version of this same one.

    ``key`` is the DAG-local identifier (what ``dependsOn`` and ``ctx.inputs[...]`` address),
    deliberately distinct from ``taskId``: the same reusable Task can appear more than once in a
    pipeline, or across different pipelines, so a reference needs its own identity independent of
    which Task it happens to point at right now.

    ``taskVersion`` accepts the alias ``"latest"`` at authoring time, exactly like
    ``CatalogCode.version`` (ADR 0010) and for the same reason: the service pins it to a concrete
    version number at save time, so what is stored is never a floating pointer — updating a task
    creates a new version but never retroactively changes a pipeline version that already points
    at the old one.
    """

    key: str
    task_id: str = Field(min_length=1, max_length=64)
    task_version: int | Literal["latest"] = "latest"
    depends_on: list[str] = Field(default_factory=list)
    position: Position = Field(default_factory=Position)

    @field_validator("key")
    @classmethod
    def _key_ok(cls, v: str) -> str:
        if not TASK_KEY_RE.match(v):
            raise ValueError(
                "must be lowercase letters, digits and underscores, starting with a letter (max 63)"
            )
        return v

    @property
    def resolved(self) -> bool:
        return self.task_version != "latest"


class PipelineSpec(Wire):
    """The whole DAG (ADR 0071): each node is a reference to a standalone Task's specific
    version, never an embedded task definition. Edges are each reference's ``dependsOn`` — an edge
    exists iff a reference names another (by its local ``key``) as a dependency, so there is no
    separate edge list that could disagree with it."""

    tasks: list[TaskRef] = Field(default_factory=list, max_length=MAX_TASKS)

    def by_key(self) -> dict[str, TaskRef]:
        return {t.key: t for t in self.tasks}

    def downstream(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {t.key: [] for t in self.tasks}
        for t in self.tasks:
            for d in t.depends_on:
                if d in out:
                    out[d].append(t.key)
        return out


def topological_order(spec: PipelineSpec) -> list[str]:
    """DAG-node keys in a valid execution order (Kahn's algorithm, ties broken by declaration
    order so the order — and therefore run logs — is deterministic). Raises ``ModelError`` on a
    cycle."""
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


def validate_structure(spec: PipelineSpec) -> None:
    """Enforce the DAG's actual mechanics — no self-loops, no duplicate edges, no cycles, every
    dependency resolves to another node in this same spec.

    Raises ``ModelError`` naming the first problem and the field it is about
    (``tasks[2].dependsOn``), so the canvas can mark the exact node. Pure: no I/O, so it runs
    identically for the live validation endpoint, at save time, and in tests — and deliberately
    cannot check that a referenced task actually exists (that needs a store lookup; the service
    does it at save time) or that its runner is available (that moved to the referenced Task's own
    save-time validation — ``validate_task_config`` above — since ADR 0071 makes both the task's
    own concern, not the pipeline's).
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

    topological_order(spec)  # cycle check


# ---- Pipeline-level config: triggers (ADR 0065, retargeted onto Pipeline by ADR 0071) --------
#
# A pipeline's `schedule` field is a discriminated `Trigger` union so a future trigger type is a
# new member, not a redesign. `cron` is the original mechanism (5-field, minute-granularity,
# unchanged) and `interval` is new, for schedules below cron's one-minute floor. This was a Job's
# field before ADR 0071 retired Job as an entity; the union and its scheduling mechanics are
# unaffected, only which entity owns it changed.

# A run has real overhead (a Dagster job compile, dispatch to the runner, a subprocess start, and
# — if any task opts in — a workload-token mint): measured at ~0.17-0.2s steady-state for a single
# trivial in-process task (see docs/decisions/0009). MIN_INTERVAL_SECONDS is a conservative floor
# well above that, leaving headroom for the runner-pod hop, larger pipelines and real infra
# latency — an honest engineering floor, not an arbitrary policy line.
MIN_INTERVAL_SECONDS = 5
MAX_INTERVAL_SECONDS = 30 * 24 * 3600  # beyond this, cron is the right tool


class CronTrigger(Wire):
    """The original mechanism: a 5-field cron expression (minute granularity) in an IANA timezone."""

    type: Literal["cron"] = "cron"
    cron: str = Field(min_length=1, max_length=100)
    timezone: str = "UTC"
    enabled: bool = True

    @model_validator(mode="after")
    def _valid(self) -> CronTrigger:
        from .schedule import validate_schedule

        validate_schedule(self.cron, self.timezone)
        return self


class IntervalTrigger(Wire):
    """Run every N seconds — for schedules below cron's one-minute floor (ADR 0065). Deliberately
    its own type rather than accepting croniter's 6-field seconds syntax: cron is already the
    thing the friendlier UI exists to hide, and stacking a seconds field onto it would make the
    one case where clarity matters most (very short intervals) more cryptic, not less."""

    type: Literal["interval"] = "interval"
    seconds: int = Field(ge=MIN_INTERVAL_SECONDS, le=MAX_INTERVAL_SECONDS)
    enabled: bool = True


def _coerce_trigger(v: Any) -> Any:
    """Back-compat (ADR 0065): every trigger saved before this existed is a bare
    ``{cron, timezone, enabled}`` object with no ``type`` — the shape ``Schedule`` used to be.
    Inject the discriminator so it keeps loading as a ``CronTrigger``, unchanged, with no
    migration. Anything that already has a ``type`` (or isn't a dict at all) passes through as-is."""
    if isinstance(v, dict) and "type" not in v and "cron" in v:
        return {**v, "type": "cron"}
    return v


Trigger = Annotated[CronTrigger | IntervalTrigger, Field(discriminator="type"), BeforeValidator(_coerce_trigger)]

# Deprecated alias: existing code (and anyone's saved bookmark of the name) that says "Schedule"
# means "the cron-shaped trigger" — kept so `Schedule(cron=..., ...)` still constructs the same
# thing it always did. Prefer `CronTrigger` in new code; the wire shape is unaffected either way.
Schedule = CronTrigger
