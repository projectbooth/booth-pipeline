"""The Spark runner (ADR 0119; docs/design-spark-runner.md) end to end on one machine: the real engine,
the real task-pod Spark client (a subprocess, as in a task pod), and a stand-in booth-spark that RUNS
the submitted PySpark code against a stub pyspark (tests/integration/fixtures/spark/).

What only a cluster shows (a task pod killed mid-run, the gateway path, NetworkPolicy) is in
tests/integration/spark.sh."""

from __future__ import annotations

import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from booth_pipeline.engine import execute
from booth_pipeline.model import ModelError, TaskConfig, validate_task_config
from booth_pipeline.runners.base import Cancellation, TaskFailed
from booth_pipeline.runners.registry import RunnerRegistry
from booth_pipeline.runners.spark import SparkRunner, summary
from booth_pipeline.runners.subprocess_runner import SubprocessRunner
from booth_pipeline.workload import AccessProvider, WorkloadToken

from .helpers import ListRecorder, build, node

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration" / "fixtures" / "spark"))
from spark_standin import StandIn, serve  # noqa: E402


class Minter:
    def __init__(self) -> None:
        self.n = 0

    def mint(self, workspace, subject, role_ceiling, owner, owner_name=""):
        self.n += 1
        return WorkloadToken(f"tok-{workspace}-{self.n}", datetime.now(UTC) + timedelta(minutes=10), "editor")


@pytest.fixture()
def spark():
    standin = StandIn(start_delay=0.2)
    server = serve(standin)
    yield standin, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def runner(url: str, **kw) -> SparkRunner:
    kw.setdefault("poll_seconds", 0.2)
    kw.setdefault("admission_wait_seconds", 3)
    kw.setdefault("retry_min_delay_seconds", 1)
    kw.setdefault("stop_wait_seconds", 5)
    return SparkRunner(SubprocessRunner(), url, **kw)


def spark_node(key: str, source: str, deps=None, **kw) -> dict:
    return node(key, deps, source=source, runner="spark", spark=kw.pop("spark", {}), **kw)


def run(spark_runner: SparkRunner, *nodes, cancel: Cancellation | None = None):
    spec, configs = build(*nodes)
    rec = ListRecorder()
    access = AccessProvider(Minter(), workspace="acme", subject="pipeline:acme:r1", role_ceiling="editor", owner="u-1", storage_url="http://x/modules/storage", catalog_url="http://x/modules/catalog")
    registry = RunnerRegistry([SubprocessRunner(), spark_runner])
    result = execute(spec, configs, run_id="r1", registry=registry, recorder=rec, cancel=cancel or Cancellation(), access=access)
    return result, rec


AGG = """
def run(ctx):
    n = ctx.spark.range(ctx.params["n"]).count()
    print("counted", n, "rows; attempt", ctx.attempt)
    return {"rows": n, "season": ctx.params["season"]}
"""


def test_a_spark_task_runs_its_code_with_ctx_and_hands_its_output_downstream(spark):
    standin, url = spark
    use = "def run(ctx):\n    return ctx.inputs['agg']['rows'] * 2\n"
    result, rec = run(runner(url), spark_node("agg", AGG, params={"n": 21, "season": 2026}), node("use", ["agg"], source=use))
    assert result.success, result.error
    lines = rec.lines
    driver = [m for k, _a, s, m in lines if k == "agg" and s == "spark"]
    assert "counted 21 rows; attempt 1" in driver
    assert not any(m.startswith("BOOTH-PIPELINE-OUTPUT") for m in driver)  # the output line isn't shown
    system = [m for k, _a, s, m in lines if k == "agg" and s == "system"]
    (app,) = standin.apps.values()
    assert system[0] == f"spark: submitted application {app.id} (booth-spark's default resources; data: none; at most 3600s)"
    assert "spark: pending → running" in system and "spark: running → succeeded" in system
    assert not any(m.startswith("BOOTH-SPARK-EVENT") for _k, _a, _s, m in lines)  # events never reach the log as text
    assert app.key == "r1/agg/1" and app.spec["maxDuration"] == "3600s"
    assert app.spec["name"] == "pipeline agg r1 a1"


def test_options_reach_spark_as_its_application_spec(spark):
    standin, url = spark
    opts = {
        "resources": {"driver": {"memory": "1g"}, "executors": {"min": 1, "max": 2, "memory": "2g"}},
        "conf": {"spark.sql.shuffle.partitions": "8"},
        "args": ["--season", "2026"],
        "dataAccess": {"lakehouse": True, "storage": [{"backendId": "seaweedfs-spark", "path": "nfl/raw", "access": "readwrite"}]},
    }
    src = "import sys\nprint('args', sys.argv[1:])\n"
    result, rec = run(runner(url), spark_node("s", src, spark=opts, timeoutSeconds=600))
    assert result.success, result.error
    (app,) = standin.apps.values()
    assert app.spec["resources"] == opts["resources"] and app.spec["conf"] == opts["conf"] and app.spec["args"] == opts["args"]
    assert app.spec["dataAccess"] == {"database": False, "lakehouse": True, "storage": [{"backendId": "seaweedfs-spark", "path": "nfl/raw", "access": "readwrite"}]}
    assert app.spec["maxDuration"] == "600s"
    assert "args ['--season', '2026']" in [m for *_x, s, m in rec.lines if s == "spark"]
    assert summary(opts, 600) == "driver 1g, 1–2 executors × 2g; data: lakehouse, storage seaweedfs-spark:nfl/raw rw; at most 600s"


def test_a_failed_application_fails_the_task_with_sparks_reason_and_its_traceback_in_the_log(spark):
    _standin, url = spark
    result, rec = run(runner(url), spark_node("bad", "raise RuntimeError('the input table is empty')\n"))
    assert not result.success
    assert "failed: the driver exited with code 1" in result.error and "spark application" in result.error
    assert any("RuntimeError: the input table is empty" in m for *_x, s, m in rec.lines if s == "spark")


def test_spark_refusing_the_application_fails_the_task_with_its_message(spark):
    _standin, url = spark
    result, _ = run(runner(url), spark_node("s", "x = 1\n", spark={"conf": {"spark.driver.extraJavaOptions": "-Dx"}}))
    assert not result.success
    assert "booth-spark refused the application (HTTP 400): conf spark.driver.extraJavaOptions is not one a caller may set (conf_not_allowed)" in result.error


def test_admission_is_waited_out_and_then_fails_with_sparks_reason(spark, monkeypatch):
    standin, url = spark
    standin.capacity = 0
    import booth_pipeline.runners._spark_client as client  # noqa: F401 - the subprocess reads its own copy

    t0 = time.monotonic()
    result, rec = run(runner(url, admission_wait_seconds=0), spark_node("s", "x = 1\n"))
    assert not result.success and "booth-spark had no capacity for 0s: at capacity: runs.maxRunning 0 (at_capacity)" in result.error
    assert time.monotonic() - t0 < 30


def test_cancel_stops_the_application(spark):
    standin, url = spark
    cancel = Cancellation()
    done = {}

    def go():
        done["r"] = run(runner(url), spark_node("long", "import time\ntime.sleep(120)\n"), cancel=cancel)

    t = threading.Thread(target=go)
    t.start()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not any(a.state == "running" for a in standin.apps.values()):
        time.sleep(0.1)
    cancel.cancel()
    t.join(timeout=30)
    result, rec = done["r"]
    assert result.canceled
    (app,) = standin.apps.values()
    deadline = time.monotonic() + 10
    while app.state == "running" and time.monotonic() < deadline:
        time.sleep(0.1)
    assert app.stop_requested_at and app.state == "stopped"
    assert any("asked booth-spark to stop application" in m and "canceled" in m for *_x, s, m in rec.lines if s == "system")


def test_a_retry_waits_the_floor_after_a_run_that_started(spark):
    standin, url = spark
    src = "def run(ctx):\n    if ctx.attempt == 1:\n        raise SystemExit(3)\n    return 'second time'\n"
    t0 = time.monotonic()
    result, rec = run(runner(url, retry_min_delay_seconds=3), spark_node("w", src, retry={"maxRetries": 1, "delaySeconds": 0}))
    assert result.success, result.error
    a1, a2 = sorted(standin.apps.values(), key=lambda a: a.created_at)
    assert (a1.key, a2.key) == ("r1/w/1", "r1/w/2")
    gap = datetime.fromisoformat(a2.created_at) - datetime.fromisoformat(a1.finished_at)
    assert gap.total_seconds() >= 3
    assert any("waits at least 3s before its retry" in e[1] for e in rec.events if e[0] == "system")
    assert time.monotonic() - t0 < 60


def test_no_floor_when_the_application_never_started(spark):
    standin, url = spark
    standin.capacity = 0
    t0 = time.monotonic()
    result, rec = run(runner(url, admission_wait_seconds=0, retry_min_delay_seconds=30), spark_node("w", "x=1\n", retry={"maxRetries": 1, "delaySeconds": 0}))
    assert not result.success
    assert not any("waits at least" in e[1] for e in rec.events if e[0] == "system")
    assert time.monotonic() - t0 < 25


def test_the_previous_attempts_application_is_stopped_before_another_starts(spark):
    """A pod lost mid-run leaves its application running; the retry must not start a second writer."""
    standin, url = spark
    sr = runner(url)

    class LosesThePodOnce:
        id = "base"
        calls = 0

        def run(self, inv, log_, cancel):
            self.calls += 1
            if self.calls == 1:
                # submit, then "lose the pod" while the application runs
                th = threading.Thread(target=SubprocessRunner().run, args=(inv, log_, cancel), daemon=True)
                th.start()
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline and not any(a.state == "running" for a in standin.apps.values()):
                    time.sleep(0.1)
                raise TaskFailed("lost connection to the runner: RemoteProtocolError")
            return SubprocessRunner().run(inv, log_, cancel)

    sr._jobs = LosesThePodOnce()
    src = "import time\ndef run(ctx):\n    if ctx.attempt == 1:\n        time.sleep(120)\n    return 'ok'\n"
    result, rec = run(sr, spark_node("w", src, retry={"maxRetries": 1, "delaySeconds": 0}))
    assert result.success, result.error
    a1, a2 = sorted(standin.apps.values(), key=lambda a: a.created_at)
    assert a1.stop_requested_at and a1.state == "stopped"
    assert a1.stop_requested_at <= a2.created_at


def test_availability_is_asked_of_spark_as_the_caller_and_cached(spark):
    _standin, url = spark
    sr = runner(url)
    assert sr.availability("tok-user", "acme") is None
    calls = []
    sr._api._http = httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(404, json={})))
    assert sr.availability("tok-user", "acme") is None  # cached
    assert sr.availability("tok-user", "globex") == "booth-spark is not installed (core's gateway has no /modules/spark)"
    assert calls[0].headers["authorization"] == "Bearer tok-user" and calls[0].headers["x-workspace"] == "globex"


@pytest.mark.parametrize(
    ("cfg", "message", "field"),
    [
        ({"runner": "spark"}, "needs its Spark options", "spark"),
        ({"runner": "spark", "spark": {}, "requirements": ["requests"]}, "can't list them", "requirements"),
        ({"runner": "base", "spark": {}}, "only for a task that runs on Spark", "spark"),
        ({"runner": "spark", "spark": {}, "code": {"type": "catalog", "entryId": "e", "version": "1", "language": "sql", "source": "select 1"}}, "must be Python", "code"),
    ],
)
def test_save_time_rules(cfg, message, field):
    c = TaskConfig.model_validate({"code": {"type": "inline", "source": "x = 1"}, **cfg})
    with pytest.raises(ModelError, match=message) as e:
        validate_task_config(c)
    assert e.value.field == field


def test_a_spark_task_always_has_platform_access():
    c = TaskConfig.model_validate({"code": {"type": "inline", "source": "x"}, "runner": "spark", "spark": {}, "platformAccess": False})
    assert c.platform_access is True


def test_bad_resources_are_refused_at_save():
    with pytest.raises(ValueError, match="min is more than max"):
        TaskConfig.model_validate({"code": {"type": "inline", "source": "x"}, "runner": "spark", "spark": {"resources": {"executors": {"min": 3, "max": 1}}}})
    with pytest.raises(ValueError, match="String should match pattern"):
        TaskConfig.model_validate({"code": {"type": "inline", "source": "x"}, "runner": "spark", "spark": {"resources": {"driver": {"memory": "lots"}}}})


def test_viewers_get_a_spark_tasks_state_lines_but_not_its_driver_log():
    """ADR 0119 amendment 3 point 4: a driver log can carry what its code prints (credentials)."""
    from booth_pipeline.records import LogLine, Run

    from .harness import make_env

    env = make_env()
    with env.client:
        p = env.store.create_pipeline("acme", "p", "", "eddie")
        run = Run("run-1", "acme", p.id, 1, "succeeded", "manual", "eddie", datetime.now(UTC))
        env.store.create_run(run, ["s"])
        now = datetime.now(UTC)
        env.store.append_logs([
            LogLine("run-1", 1, now, "s", 1, "system", "spark: submitted application abc"),
            LogLine("run-1", 2, now, "s", 1, "spark", "my token is tok-acme-secret"),
            LogLine("run-1", 3, now, "s", 1, "system", "spark: running → succeeded"),
        ])  # fmt: skip
        (t,) = env.store.list_task_runs("run-1")
        t.external = {"kind": "spark", "id": "abc", "state": "succeeded", "reason": None}
        env.store.update_task_run(t)
        seen = {tok: [ln["message"] for ln in env.call("GET", "/runs/run-1/logs", tok).json()["items"]] for tok in ("tok-viewer", "tok-editor", "tok-owner")}
        assert seen["tok-viewer"] == ["spark: submitted application abc", "spark: running → succeeded"]
        assert "my token is tok-acme-secret" in seen["tok-editor"] and "my token is tok-acme-secret" in seen["tok-owner"]
        detail = env.call("GET", "/runs/run-1", "tok-viewer").json()
        assert detail["tasks"][0]["external"] == {"kind": "spark", "id": "abc", "state": "succeeded", "reason": None}


def test_a_spark_tasks_pod_gets_its_token_but_no_credential_sidecar():
    """It runs only the Spark client: Spark's own pods get the database and S3 leases."""
    from booth_pipeline.runners.base import TaskAccess, TaskInvocation
    from booth_pipeline.runners.kubejob import KubernetesJobRunner

    from .test_task_requirements import pip_template

    access = TaskAccess("acme", "tok", "s", "c")
    jr = KubernetesJobRunner(None, pip_template(sidecar=True))  # type: ignore[arg-type]
    plain = jr._job("j", TaskInvocation("r", "k", "", 1, "x", {}, {}, 60, access))
    spark = jr._job("j", TaskInvocation("r", "k", "", 1, "x", {}, {}, 60, access, spark={}))
    assert [c["name"] for c in plain["spec"]["template"]["spec"]["initContainers"]] == ["credential-sidecar-postgres"]
    assert "initContainers" not in spark["spec"]["template"]["spec"] or not spark["spec"]["template"]["spec"]["initContainers"]
    env = {e["name"] for e in spark["spec"]["template"]["spec"]["containers"][0].get("env", [])}
    assert "DATABASE_URL" not in env and "BOOTH_RUNNER_SIDECAR_HEALTHZ" not in env


def test_resources_are_memory_only_cores_are_refused_at_save_and_never_sent(spark):
    """booth-spark sizes runs by memory only; a body with cores is a 400 there."""
    import json as _json

    for where in ({"driver": {"cores": 1}}, {"executors": {"cores": 2}}):
        with pytest.raises(ValueError, match="Extra inputs are not permitted"):
            TaskConfig.model_validate({"code": {"type": "inline", "source": "x"}, "runner": "spark", "spark": {"resources": where}})
    standin, url = spark
    opts = {"resources": {"driver": {"memory": "1g"}, "executors": {"min": 1, "max": 2, "memory": "2g"}}}
    result, _ = run(runner(url), spark_node("s", "x = 1\n", spark=opts))
    assert result.success, result.error
    (app,) = standin.apps.values()
    assert "cores" not in _json.dumps(app.spec)


def test_the_standins_limits_have_booth_sparks_shape():
    """booth-spark's GET /v1/limits, as its own test pins it (internal/api/limits_test.go at
    booth-spark@0fcd236); spark-real.sh compares against the real image once the pin is 0.3.1."""
    real = {
        "maxExecutors": 2.0, "defaultExecutors": 1.0, "driverMemory": "512m", "executorMemory": "768m", "maxMemory": "2g",
        "maxDuration": "6h0m0s", "sessionIdleTimeout": "30m0s", "sessionMaxLifetime": "8h0m0s",
        "dataAccess": {"database": False, "lakehouse": True, "storage": True},
        "role": "editor", "canSubmit": True, "submitMinRole": "editor", "maxStorageLocations": 4.0,
    }  # fmt: skip

    def shape(v):
        if isinstance(v, dict):
            return {k: shape(x) for k, x in sorted(v.items())}
        return "number" if isinstance(v, (int, float)) and not isinstance(v, bool) else type(v).__name__

    got = StandIn().limits()
    assert shape(got) == shape(real)
    from datetime import timedelta as _td

    def go(d: str) -> _td:  # Go's time.Duration.String() for whole seconds: "6h0m0s"
        import re as _re

        h, m, s = (int(x) for x in _re.fullmatch(r"(\d+)h(\d+)m(\d+)s", d).groups())
        return _td(hours=h, minutes=m, seconds=s)

    assert go(got["maxDuration"]) == _td(hours=6)  # the chart's runs.maxDuration, in booth-spark's own format


def test_a_spark_task_can_skip_and_its_downstream_never_runs(spark):
    standin, url = spark
    src = "def run(ctx):\n    print('scheduled for', ctx.trigger.scheduled_for)\n    ctx.skip('no games today')\n"
    result, rec = run(runner(url), spark_node("agg", src), node("use", ["agg"]))
    assert result.skipped and result.error == "skipped by agg: no games today"
    assert rec.statuses("agg") == ["started", "skipped"] and rec.statuses("use") == []
    (app,) = standin.apps.values()
    assert app.state == "succeeded"  # a skip is a normal end of the application, not a failure
