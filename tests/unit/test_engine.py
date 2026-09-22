"""DAG execution, retries and the base-runner path.

These are the things the brief says Dagster's own tests won't verify for us: *our* integration
of it. Most tests use the real ``SubprocessRunner`` (the real base-runner path); a few use a
scripted fake runner where the point is retry/ordering logic rather than subprocess behaviour.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from booth_pipeline.engine import compile_job, effective_retry, execute
from booth_pipeline.model import ModelError, PipelineSpec, RetryPolicy
from booth_pipeline.runners.base import Cancellation, TaskFailed, TaskInvocation
from booth_pipeline.runners.registry import RunnerRegistry
from booth_pipeline.runners.subprocess_runner import SubprocessRunner

from .helpers import ListRecorder, linear, spec, task

REAL = RunnerRegistry([SubprocessRunner()])


def run(s: PipelineSpec, registry: RunnerRegistry = REAL, job_retry: RetryPolicy | None = None, cancel: Cancellation | None = None):
    rec = ListRecorder()
    res = execute(s, run_id="r1", registry=registry, recorder=rec, cancel=cancel or Cancellation(), job_retry=job_retry)
    return res, rec


class Scripted:
    """A runner whose behaviour per (task, attempt) is scripted; records the order of calls."""

    id = "base"

    def __init__(self, script: dict[str, list[Any]] | None = None) -> None:
        self.script = script or {}
        self.calls: list[tuple[str, int]] = []
        self.inputs: dict[str, dict[str, Any]] = {}

    def run(self, inv: TaskInvocation, log: Any, cancel: Cancellation) -> Any:
        self.calls.append((inv.task_key, inv.attempt))
        self.inputs[inv.task_key] = inv.inputs
        steps = self.script.get(inv.task_key, [])
        step = steps[inv.attempt - 1] if inv.attempt - 1 < len(steps) else "ok"
        if step == "fail":
            raise TaskFailed(f"{inv.task_key} failed on attempt {inv.attempt}")
        return f"out:{inv.task_key}"


# ---- the real base-runner path ---------------------------------------------------------------


def test_source_transform_sink_runs_in_order_and_passes_data_downstream():
    s = spec(
        task("extract", "source", source="def run(ctx):\n    return [1, 2, 3]\n"),
        task("clean", "transform", ["extract"], source="def run(ctx):\n    return [x * 10 for x in ctx.inputs['extract']]\n"),
        task("load", "sink", ["clean"], source="def run(ctx):\n    print('loaded', ctx.inputs['clean'])\n    return 'done'\n"),
    )
    res, rec = run(s)
    assert res.success, rec.events
    assert [e[1] for e in rec.events if e[0] == "started"] == ["extract", "clean", "load"]
    assert rec.log_text("load") == ["loaded [10, 20, 30]"]


def test_a_sql_task_queries_upstream_output_and_a_downstream_python_task_sees_its_rows():
    """ADR 0064: the base runner's language dispatch is a real registry, not a hardcoded
    Python-only check. A SQL task's direct upstream is queryable as a table named after its task
    key (only because its output is a JSON list of rows); the SQL task's own result becomes JSON
    rows the same way a Python task's return value does, so a downstream Python task sees it
    exactly like any other upstream output."""
    s = spec(
        task("extract", "source", source="def run(ctx):\n    return [{'n': 1}, {'n': 2}, {'n': 3}]\n"),
        {
            "key": "agg",
            "kind": "transform",
            "dependsOn": ["extract"],
            "code": {"type": "catalog", "entryId": "e", "version": "1", "language": "sql", "source": "SELECT sum(n) AS total FROM extract"},
        },
        task("load", "sink", ["agg"], source="def run(ctx):\n    print('total', ctx.inputs['agg'])\n"),
    )
    res, rec = run(s)
    assert res.success, rec.events
    assert rec.log_text("load") == ["total [{'total': 6}]"]


def test_an_upstream_output_that_is_not_a_list_is_simply_not_a_table_for_a_sql_task():
    """Not every upstream output is tabular (a scalar, a dict, None) — a SQL task referencing one
    that isn't gets DuckDB's own "table does not exist" error, not a crash elsewhere."""
    s = spec(
        task("extract", "source", source="def run(ctx):\n    return 42\n"),
        {
            "key": "agg",
            "kind": "transform",
            "dependsOn": ["extract"],
            "code": {"type": "catalog", "entryId": "e", "version": "1", "language": "sql", "source": "SELECT * FROM extract"},
        },
    )
    res, rec = run(s)
    assert not res.success
    assert "agg" in res.error


def test_params_and_context_are_available_to_task_code():
    s = spec(
        task(
            "a",
            "source",
            source="def run(ctx):\n    print(ctx.task_key, ctx.kind, ctx.params['n'], ctx.attempt, ctx.run_id)\n",
            params={"n": 7},
        )
    )
    res, rec = run(s)
    assert res.success
    assert rec.log_text("a") == ["a source 7 1 r1"]


def test_plain_script_without_run_function_is_executed():
    res, rec = run(spec(task("a", "source", source="print('top level')\n")))
    assert res.success
    assert rec.log_text("a") == ["top level"]


def test_stdout_and_stderr_are_captured_on_separate_streams():
    res, rec = run(spec(task("a", "source", source="import sys\nprint('out')\nprint('err', file=sys.stderr)\n")))
    assert res.success
    assert {(s, m) for _k, _a, s, m in rec.lines} == {("stdout", "out"), ("stderr", "err")}


def test_a_failing_task_fails_the_run_and_downstream_never_runs():
    s = spec(
        task("a", "source"),
        task("b", "transform", ["a"], source="raise RuntimeError('kaboom')\n"),
        task("c", "sink", ["b"]),
    )
    res, rec = run(s)
    assert not res.success and not res.canceled
    assert "b" in (res.error or "")
    assert rec.statuses("b") == ["started", "failed"]
    assert rec.statuses("c") == []  # never started
    assert any("RuntimeError: kaboom" in m for m in rec.log_text("b"))  # the traceback is in the log


def test_unrelated_branch_does_not_run_after_a_failure_it_depends_on_but_siblings_upstream_do():
    s = spec(
        task("a", "source"),
        task("bad", "transform", ["a"], source="raise SystemExit(4)\n"),
        task("ok", "transform", ["a"]),
        task("end", "sink", ["bad", "ok"]),
    )
    res, rec = run(s)
    assert not res.success
    assert rec.statuses("bad") == ["started", "failed"]
    assert rec.statuses("end") == []


def test_unserialisable_output_is_a_task_failure_not_a_crash():
    res, rec = run(spec(task("a", "source", source="def run(ctx):\n    return {1, 2}\n")))
    assert not res.success
    assert any("not JSON-serialisable" in m for m in rec.log_text("a"))


def test_task_environment_is_scrubbed_of_service_secrets(monkeypatch):
    monkeypatch.setenv("BOOTH_PIPELINE_DATABASE_DSN", "postgresql://user:hunter2@db/x")
    monkeypatch.setenv("SOME_TOKEN", "abc")
    src = "import os\nprint(sorted(k for k in os.environ if 'BOOTH_PIPELINE' in k or k == 'SOME_TOKEN'))\nprint(os.environ.get('BOOTH_RUN_ID'))\n"
    res, rec = run(spec(task("a", "source", source=src)))
    assert res.success
    assert rec.log_text("a") == ["[]", "r1"]


def test_timeout_kills_the_task():
    s = spec(task("a", "source", source="import time\ntime.sleep(30)\n", timeoutSeconds=1))
    t0 = time.monotonic()
    res, rec = run(s)
    assert not res.success
    assert time.monotonic() - t0 < 15
    assert any("timed out after 1s" in (e[3] or "") for e in rec.events if e[0] == "failed")


def test_cancellation_stops_a_running_task_and_the_rest_of_the_run():
    import threading

    cancel = Cancellation()
    s = spec(task("a", "source", source="import time\nprint('start', flush=True)\ntime.sleep(30)\n"), task("b", "sink", ["a"]))
    threading.Timer(1.5, cancel.cancel).start()
    t0 = time.monotonic()
    res, rec = run(s, cancel=cancel)
    assert res.canceled and not res.success
    assert time.monotonic() - t0 < 15
    assert rec.statuses("a")[-1] == "canceled"
    assert rec.statuses("b") == []


# ---- retries (Dagster's policy, wired by us) --------------------------------------------------


def test_task_retry_override_reruns_until_success():
    sc = Scripted({"flaky": ["fail", "fail"]})
    s = spec(task("flaky", "source", retry={"maxRetries": 3}))
    res, rec = run(s, RunnerRegistry([sc]))
    assert res.success
    assert sc.calls == [("flaky", 1), ("flaky", 2), ("flaky", 3)]
    assert rec.statuses("flaky") == ["started", "retrying", "started", "retrying", "started", "succeeded"]
    assert any(e[0] == "system" and "retrying (attempt 2 of 4)" in e[1] for e in rec.events)


def test_retries_are_exhausted_then_the_task_fails_for_good():
    sc = Scripted({"flaky": ["fail"] * 10})
    res, rec = run(spec(task("flaky", "source", retry={"maxRetries": 2})), RunnerRegistry([sc]))
    assert not res.success
    assert sc.calls == [("flaky", 1), ("flaky", 2), ("flaky", 3)]  # 1 try + 2 retries, no more
    assert rec.statuses("flaky")[-1] == "failed"


def test_no_retry_policy_means_exactly_one_attempt():
    sc = Scripted({"a": ["fail"] * 5})
    res, _ = run(spec(task("a", "source")), RunnerRegistry([sc]))
    assert not res.success
    assert sc.calls == [("a", 1)]


def test_job_default_retry_applies_to_tasks_without_an_override_and_the_override_wins():
    sc = Scripted({"plain": ["fail"] * 9, "special": ["fail"] * 9})
    s = spec(
        task("plain", "source"),
        task("special", "source", retry={"maxRetries": 0}),  # explicit override: no retries
    )
    res, _ = run(s, RunnerRegistry([sc]), job_retry=RetryPolicy(maxRetries=2))
    assert not res.success
    assert [c for c in sc.calls if c[0] == "plain"] == [("plain", 1), ("plain", 2), ("plain", 3)]
    assert [c for c in sc.calls if c[0] == "special"] == [("special", 1)]


def test_retry_delay_is_honoured():
    sc = Scripted({"a": ["fail"]})
    t0 = time.monotonic()
    res, _ = run(spec(task("a", "source", retry={"maxRetries": 1, "delaySeconds": 1})), RunnerRegistry([sc]))
    assert res.success
    assert time.monotonic() - t0 >= 1.0


def test_retry_state_does_not_leak_between_tasks():
    sc = Scripted({"a": ["fail"], "b": []})
    res, _ = run(spec(task("a", "source", retry={"maxRetries": 1}), task("b", "transform", ["a"])), RunnerRegistry([sc]))
    assert res.success
    assert sc.calls == [("a", 1), ("a", 2), ("b", 1)]


def test_effective_retry_precedence():
    t_override = spec(task("a", "source", retry={"maxRetries": 1})).tasks[0]
    t_plain = spec(task("a", "source")).tasks[0]
    job = RetryPolicy(maxRetries=5)
    assert effective_retry(t_override, job).max_retries == 1
    assert effective_retry(t_plain, job).max_retries == 5
    assert effective_retry(t_plain, None) is None


# ---- ordering & data flow ---------------------------------------------------------------------


def test_only_direct_dependencies_are_passed_as_inputs():
    sc = Scripted()
    s = spec(task("a", "source"), task("b", "transform", ["a"]), task("c", "sink", ["b"]))
    res, _ = run(s, RunnerRegistry([sc]))
    assert res.success
    assert sc.inputs == {"a": {}, "b": {"a": "out:a"}, "c": {"b": "out:b"}}


def test_diamond_join_receives_every_upstream_output():
    sc = Scripted()
    s = spec(
        task("a", "source"),
        task("b", "transform", ["a"]),
        task("c", "transform", ["a"]),
        task("d", "sink", ["b", "c"]),
    )
    res, _ = run(s, RunnerRegistry([sc]))
    assert res.success
    assert sc.inputs["d"] == {"b": "out:b", "c": "out:c"}
    assert sc.calls.index(("a", 1)) < sc.calls.index(("b", 1)) < sc.calls.index(("d", 1))


def test_task_keys_that_collide_with_dagster_reserved_names_are_fine():
    # Dagster reserves names like "context" and "config"; ops are prefixed so any valid key works
    sc = Scripted()
    s = spec(task("context", "source"), task("config", "transform", ["context"]), task("input", "sink", ["config"]))
    res, _ = run(s, RunnerRegistry([sc]))
    assert res.success and len(sc.calls) == 3


def test_compile_time_cycle_is_a_model_error():
    s = spec(task("a", "transform", ["b"]), task("b", "transform", ["a"]))
    with pytest.raises(ModelError):
        compile_job(s, run_id="r", registry=REAL, recorder=ListRecorder(), cancel=Cancellation())


def test_the_linear_pipeline_compiles_to_the_expected_dagster_graph():
    job = compile_job(linear(), run_id="r", registry=REAL, recorder=ListRecorder(), cancel=Cancellation())
    assert {n.name for n in job.graph.nodes} == {"task_extract", "task_clean", "task_load"}  # Dagster does not promise node order


def test_op_compute_functions_are_generators_or_dagster_ignores_retry_policies():
    """Regression pin for the subtle one (see engine.py, fact 1): a non-generator compute fn
    makes Dagster skip its retry boundary entirely. The behavioural retry tests above would also
    catch it; this names the cause so the failure message points at the fix."""
    import inspect

    job = compile_job(linear(), run_id="r", registry=REAL, recorder=ListRecorder(), cancel=Cancellation(),
                      job_retry=RetryPolicy(maxRetries=1))
    for node in job.graph.nodes:
        assert inspect.isgeneratorfunction(node.definition.compute_fn), node.name
        assert node.definition.retry_policy is not None
