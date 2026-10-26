"""The runner interface — the seam between "what a task is" and "where its code executes".

A **runner** takes one task's code plus its inputs and returns its JSON output, streaming log
lines as it goes. Dagster owns *when* a task runs, its dependencies and its retries
(``engine.py``); a runner owns only *where and how the code executes*. That split is what lets a
Task pick its runner per ADR 0010/0006: ``base`` everywhere, and (later) something else — Spark,
a Kubernetes Job — when the user explicitly selects it and it is installed.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


class TaskFailed(Exception):
    """The task's code failed (non-zero exit, exception, unserialisable output, timeout).

    Retryable: Dagster's retry policy sees any exception, so this is what drives a task's retries.
    """


class TaskCanceled(Exception):
    """The run was canceled while this task was executing. Never retried."""


class TaskSkipped(Exception):
    """The task called ``ctx.skip(reason)``: nothing to do this time. Not a failure, never retried;
    its downstream tasks don't run and the run ends ``skipped``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class TaskAccess:
    """What a task that opted in to platform access is handed (ADR 0056/0057).

    A short-lived, role-ceilinged bearer token and where to send it - never the credential that
    mints tokens, which only the API/scheduler pod holds. ``refresh`` (set in the API/scheduler pod,
    never sent to the runner) returns a fresh token: a token lives ~10 minutes and a task can run
    for hours, so whoever holds the task open keeps pushing new ones (runners/remote.py).
    """

    workspace: str
    token: str
    # Base URLs the task's clients call (booth-core's gateway by default; see docs/decisions/0007).
    storage_url: str
    catalog_url: str
    refresh: Callable[[], str] | None = field(default=None, compare=False, repr=False)
    # The role core actually granted (the mint response's `role`, possibly below the ceiling).
    # What the credential sidecar asks for follows it: the broker refuses `readwrite` to a viewer.
    role: str = ""


@dataclass(frozen=True)
class TaskInvocation:
    run_id: str
    task_key: str
    kind: str
    attempt: int  # 1-based
    source: str
    params: dict[str, Any]
    # Upstream task key -> that task's JSON output. Only direct dependencies appear.
    inputs: dict[str, Any]
    timeout_seconds: int
    access: TaskAccess | None = None
    # What language `source` is in (ADR 0064) — where the base runner's language dispatch reads
    # from (runners/languages.py). Defaults to "python": every task before this ADR was Python,
    # and InlineCode (the builder's old free-text path) still always is.
    language: str = "python"
    # ADR 0115: the task's own Python requirements, installed into its pod before it starts. Only a
    # task Job installs them (runners/kubejob.py); the runner service then puts them on the path.
    requirements: tuple[str, ...] = ()
    # ADR 0119: for a Spark task, what the task pod's Spark client needs (the task's Spark options,
    # plus where booth-spark is and how to wait for it, added by runners/spark.py). None otherwise.
    spark: dict[str, Any] | None = None
    # ADR 0123: how the run started, ``ctx.trigger`` ({type, events, count, missedEvents}); None is a
    # caller from before it existed, seen by the task as a manual run with no events.
    trigger: dict[str, Any] | None = None
    # (ctx.trigger.scheduledFor is part of `trigger`: a scheduled run's slot, ISO 8601 UTC.)
    # ADR 0128: the task's declared secrets, environment variable -> value, for the task's own process
    # only: never written to invocation.json, never given to the pip init container, never in a repr.
    env: dict[str, str] = field(default_factory=dict, repr=False)


class TaskLog(Protocol):
    """Where one task attempt's output lines go. ``stream`` is stdout, stderr or system."""

    def line(self, stream: str, message: str) -> None: ...


@dataclass
class Cancellation:
    """A run-scoped cooperative cancel flag, checked by runners while a task executes."""

    _event: threading.Event = field(default_factory=threading.Event)

    def cancel(self) -> None:
        self._event.set()

    @property
    def canceled(self) -> bool:
        return self._event.is_set()


class Runner(Protocol):
    id: str

    def run(self, inv: TaskInvocation, log: TaskLog, cancel: Cancellation) -> Any:
        """Execute and return the task's JSON-serialisable output (``None`` if it returns nothing).

        Raises ``TaskFailed``, ``TaskCanceled`` or ``TaskSkipped``; any other exception is a runner bug and is
        treated as a failure too.
        """
        ...
