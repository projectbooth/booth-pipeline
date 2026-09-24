"""DAG execution, retries and the base-runner path.

These are the things the brief says Dagster's own tests won't verify for us: *our* integration
of it. Most tests use the real ``SubprocessRunner`` (the real base-runner path); a few use a
scripted fake runner where the point is retry/ordering logic rather than subprocess behaviour.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from booth_pipeline.engine import compile_job, execute
from booth_pipeline.model import ModelError, PipelineSpec, TaskConfig
from booth_pipeline.runners.base import Cancellation, TaskFailed, TaskInvocation
from booth_pipeline.runners.registry import RunnerRegistry
from booth_pipeline.runners.subprocess_runner import SubprocessRunner

from .helpers import ListRecorder, build, node

REAL = RunnerRegistry([SubprocessRunner()])


def run(s: PipelineSpec, configs: dict[str, TaskConfig], registry: RunnerRegistry = REAL, cancel: Cancellation | None = None):
    rec = ListRecorder()
    res = execute(s, configs, run_id="r1", registry=registry, recorder=rec, cancel=cancel or Cancellation())
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
    s, c = build(
        node("extract", source="def run(ctx):\n    return [1, 2, 3]\n"),
        node("clean", ["extract"], source="def run(ctx):\n    return [x * 10 for x in ctx.inputs['extract']]\n"),
        node("load", ["clean"], source="def run(ctx):\n    print('loaded', ctx.inputs['clean'])\n    return 'done'\n"),
    )
    res, rec = run(s, c)
    assert res.success, rec.events
    assert [e[1] for e in rec.events if e[0] == "started"] == ["extract", "clean", "load"]
    assert rec.log_text("load") == ["loaded [10, 20, 30]"]


def test_a_sql_task_queries_upstream_output_and_a_downstream_python_task_sees_its_rows():
    """ADR 0064: the base runner's language dispatch is a real registry, not a hardcoded
    Python-only check. A SQL task's direct upstream is queryable as a table named after its task
    key (only because its output is a JSON list of rows); the SQL task's own result becomes JSON
    rows the same way a Python task's return value does, so a downstream Python task sees it
    exactly like any other upstream output."""
    s, c = build(
        node("extract", source="def run(ctx):\n    return [{'n': 1}, {'n': 2}, {'n': 3}]\n"),
        node("agg", ["extract"], code={"type": "catalog", "entryId": "e", "version": "1", "language": "sql", "source": "SELECT sum(n) AS total FROM extract"}),
        node("load", ["agg"], source="def run(ctx):\n    print('total', ctx.inputs['agg'])\n"),
    )
    res, rec = run(s, c)
    assert res.success, rec.events
    assert rec.log_text("load") == ["total [{'total': 6}]"]


def test_an_upstream_output_that_is_not_a_list_is_simply_not_a_table_for_a_sql_task():
    """Not every upstream output is tabular (a scalar, a dict, None) — a SQL task referencing one
    that isn't gets DuckDB's own "table does not exist" error, not a crash elsewhere."""
    s, c = build(
        node("extract", source="def run(ctx):\n    return 42\n"),
        node("agg", ["extract"], code={"type": "catalog", "entryId": "e", "version": "1", "language": "sql", "source": "SELECT * FROM extract"}),
    )
    res, rec = run(s, c)
    assert not res.success
    assert "agg" in res.error


def test_params_and_context_are_available_to_task_code():
    s, c = build(node("a", source="def run(ctx):\n    print(ctx.task_key, ctx.kind, ctx.params['n'], ctx.attempt, ctx.run_id)\n", params={"n": 7}))
    res, rec = run(s, c)
    assert res.success
    assert rec.log_text("a") == ["a  7 1 r1"]  # ctx.kind is "" — retired by ADR 0071


def test_plain_script_without_run_function_is_executed():
    res, rec = run(*build(node("a", source="print('top level')\n")))
    assert res.success
    assert rec.log_text("a") == ["top level"]


def test_stdout_and_stderr_are_captured_on_separate_streams():
    res, rec = run(*build(node("a", source="import sys\nprint('out')\nprint('err', file=sys.stderr)\n")))
    assert res.success
    assert {(s, m) for _k, _a, s, m in rec.lines} == {("stdout", "out"), ("stderr", "err")}


def test_a_failing_task_fails_the_run_and_downstream_never_runs():
    s, c = build(node("a"), node("b", ["a"], source="raise RuntimeError('kaboom')\n"), node("c", ["b"]))
    res, rec = run(s, c)
    assert not res.success and not res.canceled
    assert "b" in (res.error or "")
    assert rec.statuses("b") == ["started", "failed"]
    assert rec.statuses("c") == []  # never started
    assert any("RuntimeError: kaboom" in m for m in rec.log_text("b"))  # the traceback is in the log


def test_unrelated_branch_does_not_run_after_a_failure_it_depends_on_but_siblings_upstream_do():
    s, c = build(node("a"), node("bad", ["a"], source="raise SystemExit(4)\n"), node("ok", ["a"]), node("end", ["bad", "ok"]))
    res, rec = run(s, c)
    assert not res.success
    assert rec.statuses("bad") == ["started", "failed"]
    assert rec.statuses("end") == []


def test_unserialisable_output_is_a_task_failure_not_a_crash():
    res, rec = run(*build(node("a", source="def run(ctx):\n    return {1, 2}\n")))
    assert not res.success
    assert any("not JSON-serialisable" in m for m in rec.log_text("a"))


def test_task_environment_is_scrubbed_of_service_secrets(monkeypatch):
    monkeypatch.setenv("BOOTH_PIPELINE_DATABASE_DSN", "postgresql://user:hunter2@db/x")
    monkeypatch.setenv("SOME_TOKEN", "abc")
    src = "import os\nprint(sorted(k for k in os.environ if 'BOOTH_PIPELINE' in k or k == 'SOME_TOKEN'))\nprint(os.environ.get('BOOTH_RUN_ID'))\n"
    res, rec = run(*build(node("a", source=src)))
    assert res.success
    assert rec.log_text("a") == ["[]", "r1"]


def test_timeout_kills_the_task():
    s, c = build(node("a", source="import time\ntime.sleep(30)\n", timeoutSeconds=1))
    t0 = time.monotonic()
    res, rec = run(s, c)
    assert not res.success
    assert time.monotonic() - t0 < 15
    assert any("timed out after 1s" in (e[3] or "") for e in rec.events if e[0] == "failed")


def test_cancellation_stops_a_running_task_and_the_rest_of_the_run():
    import threading

    cancel = Cancellation()
    s, c = build(node("a", source="import time\nprint('start', flush=True)\ntime.sleep(30)\n"), node("b", ["a"]))
    threading.Timer(1.5, cancel.cancel).start()
    t0 = time.monotonic()
    res, rec = run(s, c, cancel=cancel)
    assert res.canceled and not res.success
    assert time.monotonic() - t0 < 15
    assert rec.statuses("a")[-1] == "canceled"
    assert rec.statuses("b") == []


# ---- retries (Dagster's policy, wired by us) --------------------------------------------------


def test_task_retry_override_reruns_until_success():
    sc = Scripted({"flaky": ["fail", "fail"]})
    s, c = build(node("flaky", retry={"maxRetries": 3}))
    res, rec = run(s, c, RunnerRegistry([sc]))
    assert res.success
    assert sc.calls == [("flaky", 1), ("flaky", 2), ("flaky", 3)]
    assert rec.statuses("flaky") == ["started", "retrying", "started", "retrying", "started", "succeeded"]
    assert any(e[0] == "system" and "retrying (attempt 2 of 4)" in e[1] for e in rec.events)


def test_retries_are_exhausted_then_the_task_fails_for_good():
    sc = Scripted({"flaky": ["fail"] * 10})
    res, rec = run(*build(node("flaky", retry={"maxRetries": 2})), RunnerRegistry([sc]))
    assert not res.success
    assert sc.calls == [("flaky", 1), ("flaky", 2), ("flaky", 3)]  # 1 try + 2 retries, no more
    assert rec.statuses("flaky")[-1] == "failed"


def test_no_retry_policy_means_exactly_one_attempt():
    sc = Scripted({"a": ["fail"] * 5})
    res, _ = run(*build(node("a")), RunnerRegistry([sc]))
    assert not res.success
    assert sc.calls == [("a", 1)]


def test_a_tasks_own_retry_is_all_there_is_no_pipeline_level_default():
    """ADR 0071: retry lives purely on the task's own config — there is no more job-level default
    a task without its own override could fall back to (Job, which used to hold one, is retired)."""
    sc = Scripted({"plain": ["fail"] * 9})
    res, _ = run(*build(node("plain")), RunnerRegistry([sc]))
    assert not res.success
    assert sc.calls == [("plain", 1)]


def test_retry_delay_is_honoured():
    sc = Scripted({"a": ["fail"]})
    t0 = time.monotonic()
    res, _ = run(*build(node("a", retry={"maxRetries": 1, "delaySeconds": 1})), RunnerRegistry([sc]))
    assert res.success
    assert time.monotonic() - t0 >= 1.0


def test_retry_state_does_not_leak_between_tasks():
    sc = Scripted({"a": ["fail"], "b": []})
    res, _ = run(*build(node("a", retry={"maxRetries": 1}), node("b", ["a"])), RunnerRegistry([sc]))
    assert res.success
    assert sc.calls == [("a", 1), ("a", 2), ("b", 1)]


# ---- ordering & data flow ---------------------------------------------------------------------


def test_only_direct_dependencies_are_passed_as_inputs():
    sc = Scripted()
    s, c = build(node("a"), node("b", ["a"]), node("c", ["b"]))
    res, _ = run(s, c, RunnerRegistry([sc]))
    assert res.success
    assert sc.inputs == {"a": {}, "b": {"a": "out:a"}, "c": {"b": "out:b"}}


def test_diamond_join_receives_every_upstream_output():
    sc = Scripted()
    s, c = build(node("a"), node("b", ["a"]), node("c", ["a"]), node("d", ["b", "c"]))
    res, _ = run(s, c, RunnerRegistry([sc]))
    assert res.success
    assert sc.inputs["d"] == {"b": "out:b", "c": "out:c"}
    assert sc.calls.index(("a", 1)) < sc.calls.index(("b", 1)) < sc.calls.index(("d", 1))


def test_task_keys_that_collide_with_dagster_reserved_names_are_fine():
    # Dagster reserves names like "context" and "config"; ops are prefixed so any valid key works
    sc = Scripted()
    s, c = build(node("context"), node("config", ["context"]), node("input", ["config"]))
    res, _ = run(s, c, RunnerRegistry([sc]))
    assert res.success and len(sc.calls) == 3


def test_compile_time_cycle_is_a_model_error():
    s, c = build(node("a", ["b"]), node("b", ["a"]))
    with pytest.raises(ModelError):
        compile_job(s, c, run_id="r", registry=REAL, recorder=ListRecorder(), cancel=Cancellation())


def test_the_linear_pipeline_compiles_to_the_expected_dagster_graph():
    s, c = build(node("extract"), node("clean", ["extract"]), node("load", ["clean"]))
    job = compile_job(s, c, run_id="r", registry=REAL, recorder=ListRecorder(), cancel=Cancellation())
    assert {n.name for n in job.graph.nodes} == {"task_extract", "task_clean", "task_load"}  # Dagster does not promise node order


def test_op_compute_functions_are_generators_or_dagster_ignores_retry_policies():
    """Regression pin for the subtle one (see engine.py, fact 1): a non-generator compute fn
    makes Dagster skip its retry boundary entirely. The behavioural retry tests above would also
    catch it; this names the cause so the failure message points at the fix."""
    import inspect

    s, c = build(node("extract", retry={"maxRetries": 1}), node("clean", ["extract"], retry={"maxRetries": 1}), node("load", ["clean"], retry={"maxRetries": 1}))
    job = compile_job(s, c, run_id="r", registry=REAL, recorder=ListRecorder(), cancel=Cancellation())
    for op in job.graph.nodes:
        assert inspect.isgeneratorfunction(op.definition.compute_fn), op.name
        assert op.definition.retry_policy is not None
