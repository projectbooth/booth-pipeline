"""The execution engine: compile a saved ``PipelineSpec`` into a Dagster job and run it.

Dagster (ADR 0010) owns everything about *orchestration*: dependency ordering, per-task retry
policies with delay/backoff, and failure propagation (a failed task's downstream never runs).
This module owns the translation and nothing else — it deliberately does not reimplement any of
that. What a Task actually *executes* is delegated to its ``Runner`` (``runners/``), which is how
"base runner by default, Spark only when selected" (ADR 0006) stays a per-task choice.

Three integration facts worth knowing, each pinned by a test in ``tests/unit/test_engine.py``
because Dagster's own suite will not catch us getting them wrong:

1. **Compute functions must be generators.** Dagster applies a retry policy only while it
   *iterates* a step's result. A hand-built ``OpDefinition`` whose compute function simply raises
   or returns has its ``RetryPolicy`` silently ignored — the step fails on the first attempt and
   nothing says why. Ours ``yield Output(...)``.
2. **Op and input names are prefixed** (``task_<key>`` / ``in_<key>``) so no user-chosen key can
   collide with a name Dagster reserves.
3. **Execution is serial in v0** (Dagster's in-process executor): independent branches run one
   after another in dependency order, not in parallel. Correct, just not maximally fast. Parallel
   execution needs the multiprocess executor and a persistent Dagster instance — deliberately
   left out of v0 (docs/decisions/0003).

Dagster is used as an execution library, not a system of record: run history, task state and
logs live in our own store (``store/``), where the API, retention and workspace scoping are ours.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from dagster import (
    Any as DagsterAny,
)
from dagster import (
    Backoff,
    DagsterInstance,
    DependencyDefinition,
    Failure,
    GraphDefinition,
    In,
    JobDefinition,
    OpDefinition,
    Out,
    Output,
    RetryPolicy,
)

from .model import PipelineSpec, Task, topological_order
from .model import RetryPolicy as SpecRetry
from .runners.base import Cancellation, TaskCanceled, TaskInvocation
from .runners.registry import RunnerRegistry
from .workload import AccessProvider, MintNotConfigured, MintRefused

log = logging.getLogger(__name__)

RESULT = "result"


def op_name(key: str) -> str:
    return f"task_{key}"


def input_name(key: str) -> str:
    return f"in_{key}"


class RunRecorder(Protocol):
    """Everything the engine reports while a run executes. The service backs it with the store;
    tests back it with a list."""

    def task_started(self, key: str, attempt: int) -> None: ...

    def task_attempt_finished(self, key: str, attempt: int, status: str, error: str | None) -> None:
        """``status`` is succeeded, failed (final), retrying (will run again) or canceled."""
        ...

    def task_log(self, key: str, attempt: int) -> Any:
        """A ``TaskLog`` for one attempt's stdout/stderr/system lines."""
        ...

    def system(self, message: str) -> None: ...


@dataclass(frozen=True)
class EngineResult:
    success: bool
    canceled: bool
    error: str | None


def effective_retry(task: Task, job_default: SpecRetry | None) -> SpecRetry | None:
    """A task's own retry override wins outright; otherwise the job's default; otherwise none."""
    return task.retry if task.retry is not None else job_default


def _dagster_retry(policy: SpecRetry | None) -> RetryPolicy | None:
    if policy is None or policy.max_retries == 0:
        return None
    backoff = {"fixed": None, "linear": Backoff.LINEAR, "exponential": Backoff.EXPONENTIAL}[policy.backoff]
    return RetryPolicy(max_retries=policy.max_retries, delay=policy.delay_seconds, backoff=backoff)


def _make_op(
    task: Task,
    run_id: str,
    retry: SpecRetry | None,
    registry: RunnerRegistry,
    recorder: RunRecorder,
    cancel: Cancellation,
    access: AccessProvider | None,
    reasons: dict[str, str],
) -> OpDefinition:
    max_retries = retry.max_retries if retry else 0

    # `context` is deliberately unannotated: Dagster inspects the annotation and rejects anything
    # that is not one of its own context types (an `Any` fails at definition time).
    def compute(context, inputs):
        attempt = context.retry_number + 1
        upstream = {dep: inputs[input_name(dep)] for dep in task.depends_on}
        recorder.task_started(task.key, attempt)
        tlog = recorder.task_log(task.key, attempt)
        try:
            # Only a task that opted in mints anything (ADR 0056, docs/decisions/0007): a task that
            # never touches storage or the catalog must not be blocked by an identity it doesn't use.
            grant = None
            if task.platform_access:
                if access is None:
                    raise MintNotConfigured("workload identity is not configured for this run")
                grant = access.grant()
            inv = TaskInvocation(
                run_id=run_id,
                task_key=task.key,
                kind=task.kind,
                attempt=attempt,
                source=task.code.source or "",
                params=task.params,
                inputs=upstream,
                timeout_seconds=task.timeout_seconds,
                access=grant,
            )
            output = registry.get(task.runner).run(inv, tlog, cancel)
        except TaskCanceled:
            recorder.task_attempt_finished(task.key, attempt, "canceled", "canceled")
            # allow_retries=False: a canceled task must not be re-run by its retry policy.
            raise Failure(description="canceled", allow_retries=False) from None
        except (MintRefused, MintNotConfigured) as e:
            # An expected outcome, not a crash: core said this run may not have an identity (its
            # owner has not signed in recently enough, ADR 0058). Say so plainly, and do NOT retry —
            # the answer will not change in seconds; the owner has to act.
            message = f"not given platform access: {e}"
            reasons[task.key] = message
            recorder.task_attempt_finished(task.key, attempt, "failed", message)
            raise Failure(description=message, allow_retries=False) from None
        except Exception as e:  # noqa: BLE001 - any failure, incl. a runner bug, is a task failure
            will_retry = attempt <= max_retries and not cancel.canceled
            message = str(e) or e.__class__.__name__
            reasons[task.key] = message
            recorder.task_attempt_finished(task.key, attempt, "retrying" if will_retry else "failed", message)
            if will_retry and retry:
                recorder.system(
                    f"task {task.key!r} attempt {attempt} failed ({message}); retrying "
                    f"(attempt {attempt + 1} of {max_retries + 1})"
                )
            raise  # re-raised so Dagster's retry policy sees it (fact 1 above)
        recorder.task_attempt_finished(task.key, attempt, "succeeded", None)
        yield Output(output, output_name=RESULT)

    return OpDefinition(
        compute_fn=compute,
        name=op_name(task.key),
        ins={input_name(dep): In(dagster_type=DagsterAny) for dep in task.depends_on},
        outs={RESULT: Out(dagster_type=DagsterAny)},
        retry_policy=_dagster_retry(retry),
        description=task.display_name,
    )


def compile_job(
    spec: PipelineSpec,
    *,
    run_id: str,
    registry: RunnerRegistry,
    recorder: RunRecorder,
    cancel: Cancellation,
    job_retry: SpecRetry | None = None,
    name: str = "pipeline",
    access: AccessProvider | None = None,
    reasons: dict[str, str] | None = None,
) -> JobDefinition:
    """Compile the spec into a Dagster ``JobDefinition``.

    Called both at save time (to prove the graph compiles — a structural error surfaces when the
    user saves, not at 3am) and at run time (with a real recorder). Raises ``ModelError`` for a
    cycle via ``topological_order``.
    """
    order = topological_order(spec)
    by_key = spec.by_key()
    ops = [
        _make_op(by_key[k], run_id, effective_retry(by_key[k], job_retry), registry, recorder, cancel, access, reasons if reasons is not None else {})
        for k in order
    ]
    deps: dict[str, dict[str, DependencyDefinition]] = {}
    for k in order:
        t = by_key[k]
        if t.depends_on:
            deps[op_name(k)] = {input_name(d): DependencyDefinition(op_name(d), RESULT) for d in t.depends_on}
    graph = GraphDefinition(name=_safe(name), node_defs=ops, dependencies=deps)
    return graph.to_job(name=_safe(name))


def _safe(name: str) -> str:
    cleaned = "".join(c if c.isalnum() or c == "_" else "_" for c in name)
    return cleaned if cleaned and not cleaned[0].isdigit() else f"p_{cleaned}"


def execute(
    spec: PipelineSpec,
    *,
    run_id: str,
    registry: RunnerRegistry,
    recorder: RunRecorder,
    cancel: Cancellation,
    job_retry: SpecRetry | None = None,
    access: AccessProvider | None = None,
) -> EngineResult:
    """Run the pipeline to completion (blocking). Never raises for a task failure — that is a
    result, not an exception."""
    reasons: dict[str, str] = {}
    job = compile_job(
        spec, run_id=run_id, registry=registry, recorder=recorder, cancel=cancel, job_retry=job_retry,
        name=f"run_{run_id}", access=access, reasons=reasons,
    )
    # A fresh in-memory instance per run: nothing is shared between runs, nothing persists in
    # Dagster (our store is the record). Dagster's own console logging is quieted to warnings —
    # the user-facing log is the one we record, and Dagster's debug output is not it.
    # `with`: the instance is backed by in-memory SQLite, whose connections may only be closed on
    # the thread that made them. Left to the garbage collector it is finalised on some other
    # thread, and every run would spray "SQLite objects created in a thread" errors into the log.
    with DagsterInstance.ephemeral() as instance:
        result = job.execute_in_process(
            instance=instance,
            raise_on_error=False,
            run_config={"loggers": {"console": {"config": {"log_level": "WARNING"}}}},
        )
    if result.success:
        return EngineResult(success=True, canceled=False, error=None)
    if cancel.canceled:
        return EngineResult(success=False, canceled=True, error="canceled")
    failed = sorted({e.step_key.removeprefix("task_") for e in result.all_events if e.is_step_failure and e.step_key})
    # Carry each failed task's reason into the run's own status, so "why did it fail" is visible
    # without opening the log (notably: "not given platform access: the owner hasn't signed in...").
    detail = "; ".join(f"{k}: {reasons[k]}" if k in reasons else k for k in failed)
    return EngineResult(success=False, canceled=False, error=f"task(s) failed: {detail}" if failed else "run failed")
