"""Shared modules from the catalog: a task's `modules: [{entryId, version, name}]`, pinned and snapshotted at
save like catalog code, written as modules/<name>.py beside its code and importable, in base and Spark
tasks alike, within a 256 KiB budget."""

from __future__ import annotations

import hashlib

import pytest

from booth_pipeline.engine import execute
from booth_pipeline.model import MAX_INLINE_SOURCE_BYTES, ModelError, TaskConfig, validate_task_config
from booth_pipeline.runners.base import Cancellation
from booth_pipeline.runners.registry import RunnerRegistry
from booth_pipeline.runners.subprocess_runner import SubprocessRunner

from .harness import make_env
from .helpers import ListRecorder, build, node
from .test_spark_runner import spark  # noqa: F401 - the stand-in booth-spark fixture

CALENDAR_V1 = "HOLIDAYS = ['2026-11-26']\n\ndef is_open(day):\n    return day not in HOLIDAYS\n"
CALENDAR_V2 = "HOLIDAYS = ['2026-11-26', '2026-12-25']\n\ndef is_open(day):\n    return day not in HOLIDAYS\n"
UNIVERSE = "from market_calendar import is_open\n\nTICKERS = ['SPY', 'QQQ']\n\ndef open_tickers(day):\n    return TICKERS if is_open(day) else []\n"
USES = "import market_calendar, universe\n\ndef run(ctx):\n    return [market_calendar.is_open('2026-12-25'), universe.open_tickers('2026-11-26')]\n"


def mod(name: str, source: str, **kw) -> dict:
    return {"name": name, "entryId": f"e-{name}", "version": "1.0.0", "source": source, "sha256": hashlib.sha256(source.encode()).hexdigest(), **kw}


# ---- at run time -----------------------------------------------------------------------------------


def test_a_task_imports_its_modules_and_they_import_each_other():
    spec, configs = build(node("a", source=USES, modules=[mod("market_calendar", CALENDAR_V1), mod("universe", UNIVERSE)]))
    rec = ListRecorder()
    res = execute(spec, configs, run_id="r1", registry=RunnerRegistry([SubprocessRunner()]), recorder=rec, cancel=Cancellation())
    assert res.success, rec.log_text("a")
    assert rec.outputs["a"] == [True, []]


def test_a_task_without_modules_has_no_modules_folder():
    src = "import os\ndef run(ctx):\n    return os.path.exists('modules')\n"
    spec, configs = build(node("a", source=src))
    rec = ListRecorder()
    execute(spec, configs, run_id="r1", registry=RunnerRegistry([SubprocessRunner()]), recorder=rec, cancel=Cancellation())
    assert rec.outputs["a"] is False


# ---- what may be declared --------------------------------------------------------------------------


def cfg(**kw) -> TaskConfig:
    return TaskConfig.model_validate({"code": {"type": "inline", "source": "pass\n"}, **kw})


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("json", "would shadow"),
        ("task", "would shadow"),
        ("booth_helpers", "would shadow"),
        ("pyspark", "would shadow"),
        ("class", "letters, digits"),
        ("market-calendar", "letters, digits"),
        ("1st", "letters, digits"),
    ],
)
def test_a_module_name_must_be_importable_and_shadow_nothing(name, message):
    with pytest.raises(ModelError, match=message) as e:
        validate_task_config(cfg(modules=[mod(name, "x = 1\n")]))
    assert e.value.field == "modules[0].name"


def test_a_name_imported_twice_is_refused():
    with pytest.raises(ModelError, match="imported twice"):
        validate_task_config(cfg(modules=[mod("cal", "x = 1\n"), {**mod("cal", "y = 2\n"), "entryId": "other"}]))


def test_the_modules_share_a_256_kib_budget():
    half = "x = '" + "a" * (MAX_INLINE_SOURCE_BYTES // 2) + "'\n"
    validate_task_config(cfg(modules=[mod("a", half[: MAX_INLINE_SOURCE_BYTES // 2 - 10])]))
    with pytest.raises(ModelError, match="together they are limited to 256 KiB") as e:
        validate_task_config(cfg(modules=[mod("a", half), mod("b", half)]))
    assert e.value.field == "modules"


def test_a_spark_task_counts_its_modules_against_booth_sparks_inline_limit():
    big = "x = '" + "a" * (200 * 1024) + "'\n"
    on_spark = TaskConfig.model_validate({"code": {"type": "inline", "source": big}, "runner": "spark", "spark": {}, "modules": [mod("helpers", big)]})
    with pytest.raises(ModelError, match="travel to booth-spark inline"):
        validate_task_config(on_spark)


def test_only_python_tasks_import_modules():
    sql = TaskConfig.model_validate(
        {"code": {"type": "catalog", "entryId": "e", "version": "1", "language": "sql", "source": "select 1"}, "modules": [mod("cal", "x = 1\n")]}
    )
    with pytest.raises(ModelError, match="only a Python task"):
        validate_task_config(sql)
    with pytest.raises(ModelError, match="not Python"):
        validate_task_config(cfg(modules=[mod("cal", "select 1", language="sql")]))


# ---- through the API: pinned and snapshotted at save --------------------------------------------------


@pytest.fixture()
def env():
    e = make_env()
    e.catalog.add("cal", "market_calendar", {"1.0.0": CALENDAR_V1, "1.1.0": CALENDAR_V2})
    e.catalog.add("uni", "universe", {"2.0.0": UNIVERSE})
    yield e
    e.client.close()


def new_task(env, modules, source=USES):
    r = env.call("POST", "/tasks", json={"name": "capture", "config": {"code": {"type": "inline", "source": source}, "modules": modules}})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_latest_is_pinned_and_the_source_snapshotted_at_save(env):
    t = new_task(env, [{"name": "market_calendar", "entryId": "cal", "version": "latest"}, {"name": "universe", "entryId": "uni", "version": "2.0.0"}])
    mods = env.call("GET", f"/tasks/{t}/versions/1").json()["config"]["modules"]
    assert [(m["name"], m["version"], m["entryName"]) for m in mods] == [("market_calendar", "1.1.0", "market_calendar"), ("universe", "2.0.0", "universe")]
    assert mods[0]["source"] == CALENDAR_V2 and mods[0]["sha256"] == hashlib.sha256(CALENDAR_V2.encode()).hexdigest()


def test_a_run_uses_the_snapshot_and_never_calls_the_catalog(env):
    t = new_task(env, [{"name": "market_calendar", "entryId": "cal", "version": "1.0.0"}, {"name": "universe", "entryId": "uni", "version": "2.0.0"}])
    p = env.call("POST", "/pipelines", json={"name": "p", "spec": {"tasks": [{"key": "a", "taskId": t, "taskVersion": 1, "dependsOn": [], "position": {"x": 0, "y": 0}}]}}).json()
    env.catalog.requests.clear()
    env.catalog.down = True
    env.catalog.entries["cal"]["versions"]["1.0.0"] = "raise SystemExit('TAMPERED')\n"
    run = env.wait_run(env.call("POST", f"/pipelines/{p['id']}/run").json()["id"])
    assert run["status"] == "succeeded", run
    assert run["tasks"][0]["output"] == {"value": [True, []]}  # v1.0.0's calendar: Christmas open, Thanksgiving closed
    assert env.catalog.requests == []


def test_an_unrelated_edit_reuses_the_snapshot_while_the_catalog_is_down(env):
    t = new_task(env, [{"name": "market_calendar", "entryId": "cal", "version": "1.0.0"}])
    saved = env.call("GET", f"/tasks/{t}/versions/1").json()["config"]
    env.catalog.down = True
    r = env.call("POST", f"/tasks/{t}/versions", json={"config": {**saved, "timeoutSeconds": 120}, "notes": "slower"})
    assert r.status_code == 201, r.text
    assert r.json()["config"]["modules"][0]["source"] == CALENDAR_V1


def test_a_client_cannot_label_its_own_source_as_a_catalog_module(env):
    t = new_task(env, [{"name": "market_calendar", "entryId": "cal", "version": "1.0.0", "source": "EVIL = 1\n", "sha256": "0" * 64}])
    assert env.call("GET", f"/tasks/{t}/versions/1").json()["config"]["modules"][0]["source"] == CALENDAR_V1


def test_a_missing_entry_is_named_on_its_row(env):
    r = env.call("POST", "/tasks", json={"name": "x", "config": {"code": {"type": "inline", "source": "pass\n"}, "modules": [{"name": "m", "entryId": "nope", "version": "1"}]}})
    assert r.status_code == 422 and r.json()["field"] == "modules[0].entryId"


def test_the_export_names_each_module_and_its_pinned_version(env):
    t = new_task(env, [{"name": "market_calendar", "entryId": "cal", "version": "latest"}])
    p = env.call("POST", "/pipelines", json={"name": "p", "spec": {"tasks": [{"key": "a", "taskId": t, "taskVersion": 1, "dependsOn": [], "position": {"x": 0, "y": 0}}]}}).json()
    text = env.call("GET", f"/pipelines/{p['id']}/versions/1/export").text
    assert "market_calendar" in text and "1.1.0" in text


# ---- Spark ----------------------------------------------------------------------------------------


def test_a_spark_task_imports_its_modules_in_the_driver_and_ships_them_to_the_executors(spark):  # noqa: F811 - the fixture
    from .test_spark_runner import run as spark_run
    from .test_spark_runner import runner, spark_node

    standin, url = spark
    src = "import market_calendar\n\ndef run(ctx):\n    return [market_calendar.is_open('2026-11-26'), [p.rsplit('/', 1)[-1].rsplit(chr(92), 1)[-1] for p in ctx.spark.sparkContext.py_files]]\n"
    result, rec = spark_run(runner(url), spark_node("agg", src, modules=[mod("market_calendar", CALENDAR_V1)]))
    assert result.success, result.error
    assert rec.outputs["agg"] == [False, ["market_calendar.py"]]
    (app,) = standin.apps.values()
    assert "def is_open(day)" in app.spec["main"]["inlinePython"]  # the module travelled inline, with the code
