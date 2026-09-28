"""ADR 0078: a pipeline's DAG node can override the referenced task's params.

The motivating case: one task (a script that fetches a stock quote), referenced by several nodes
of one pipeline, each node supplying its own `symbol`. Every test here goes through the real
API, the real resolution step and a real subprocess run — not a mocked engine.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import yaml

from booth_pipeline.model import MAX_PARAMS_BYTES, PipelineSpec, TaskConfig, TaskRef
from booth_pipeline.resolve import node_config

from .harness import Env, make_env

FETCH_QUOTE = (
    "def run(ctx):\n"
    "    p = ctx.params\n"
    "    print('quote', p['symbol'], p['exchange'])\n"
    "    return p['symbol']\n"
)
BASE_PARAMS = {"symbol": "BASE", "exchange": "NASDAQ"}


@pytest.fixture()
def env():
    e = make_env()
    with e.client:
        yield e


def fetch_quote_task(env: Env) -> dict[str, Any]:
    r = env.call("POST", "/tasks", json={"name": "fetch_quote", "config": {"code": {"type": "inline", "source": FETCH_QUOTE}, "params": BASE_PARAMS}})
    assert r.status_code == 201, r.text
    return r.json()


def node(key: str, task_id: str, overrides: dict[str, Any] | None = None, deps: list[str] | None = None) -> dict[str, Any]:
    n: dict[str, Any] = {"key": key, "taskId": task_id, "taskVersion": 1, "dependsOn": deps or []}
    if overrides is not None:
        n["paramOverrides"] = overrides
    return n


def run_and_logs(env: Env, pipeline_id: str) -> tuple[dict[str, Any], dict[str, list[str]]]:
    run = env.wait_run(env.call("POST", f"/pipelines/{pipeline_id}/run").json()["id"])
    lines = env.call("GET", f"/runs/{run['id']}/logs?limit=5000").json()["items"]
    by_task: dict[str, list[str]] = {}
    for ln in lines:
        if ln["taskKey"] and ln["stream"] == "stdout":
            by_task.setdefault(ln["taskKey"], []).append(ln["message"])
    return run, by_task


# ---- the headline case ---------------------------------------------------------------------


def test_each_node_referencing_the_same_task_runs_with_its_own_override(env):
    """Two nodes, one task, two different overrides — plus a node with no override. Each must run
    with ITS value: not the task's base value, and not the other node's."""
    t = fetch_quote_task(env)
    spec = {
        "tasks": [
            node("hood", t["id"], {"symbol": "HOOD"}),
            node("tslq", t["id"], {"symbol": "TSLQ"}),
            node("plain", t["id"]),  # no override: the task's own params
            # Downstream proof that each node's OUTPUT differed too, not just its log line.
            node("collect", t["id"], {"symbol": "ALL"}, deps=["hood", "tslq", "plain"]),
        ]
    }
    p = env.call("POST", "/pipelines", json={"name": "quotes", "spec": spec}).json()
    run, logs = run_and_logs(env, p["id"])

    assert run["status"] == "succeeded", run
    assert logs["hood"] == ["quote HOOD NASDAQ"]  # own symbol; exchange still the task's default
    assert logs["tslq"] == ["quote TSLQ NASDAQ"]
    assert logs["plain"] == ["quote BASE NASDAQ"]  # no override → the task's own params, unchanged
    # Exactly one Task entity exists: no per-value duplicates were needed.
    assert env.call("GET", "/tasks").json()["total"] == 1


def test_node_outputs_differ_per_override_as_seen_downstream(env):
    t = fetch_quote_task(env)
    collector = env.call(
        "POST",
        "/tasks",
        json={"name": "collector", "config": {"code": {"type": "inline", "source": "def run(ctx):\n    print('got', sorted(ctx.inputs.items()))\n"}}},
    ).json()
    spec = {
        "tasks": [
            node("a", t["id"], {"symbol": "HOOD"}),
            node("b", t["id"], {"symbol": "VOYG"}),
            {"key": "collect", "taskId": collector["id"], "taskVersion": 1, "dependsOn": ["a", "b"]},
        ]
    }
    p = env.call("POST", "/pipelines", json={"name": "fan-in", "spec": spec}).json()
    run, logs = run_and_logs(env, p["id"])
    assert run["status"] == "succeeded", run
    assert logs["collect"] == ["got [('a', 'HOOD'), ('b', 'VOYG')]"]


def test_the_tasks_own_params_stay_its_defaults(env):
    t = fetch_quote_task(env)
    spec = {"tasks": [node("hood", t["id"], {"symbol": "HOOD", "extra": 1})]}
    p = env.call("POST", "/pipelines", json={"name": "q", "spec": spec}).json()
    run_and_logs(env, p["id"])
    saved = env.call("GET", f"/tasks/{t['id']}/versions/1").json()
    assert saved["config"]["params"] == BASE_PARAMS  # never rewritten by a node's override
    assert env.call("GET", f"/tasks/{t['id']}").json()["latestVersion"] == 1  # nor versioned


# ---- versioned with the pipeline, like everything else in a spec (ADR 0071) ----------------


def test_overrides_are_saved_in_the_version_and_a_pinned_version_keeps_its_own(env):
    t = fetch_quote_task(env)
    p = env.call("POST", "/pipelines", json={"name": "q", "spec": {"tasks": [node("n", t["id"], {"symbol": "OLD"})]}}).json()
    env.call("POST", f"/pipelines/{p['id']}/versions", json={"spec": {"tasks": [node("n", t["id"], {"symbol": "NEW"})]}, "notes": ""})

    v1 = env.call("GET", f"/pipelines/{p['id']}/versions/1").json()
    assert v1["spec"]["tasks"][0]["paramOverrides"] == {"symbol": "OLD"}  # round-trips in the saved spec

    _, logs = run_and_logs(env, p["id"])
    assert logs["n"] == ["quote NEW NASDAQ"]  # latest version's override
    sched = {"schedule": None, "allowConcurrentRuns": False, "roleCeiling": "editor", "pinnedVersion": 1}
    assert env.call("PUT", f"/pipelines/{p['id']}/schedule", json=sched).status_code == 200
    _, logs = run_and_logs(env, p["id"])
    assert logs["n"] == ["quote OLD NASDAQ"]  # the pin runs v1 exactly as it was saved


def test_a_draft_can_carry_overrides_too(env):
    t = fetch_quote_task(env)
    p = env.call("POST", "/pipelines", json={"name": "q", "spec": {"tasks": [node("n", t["id"])]}}).json()
    r = env.call("PUT", f"/pipelines/{p['id']}/draft", json={"spec": {"tasks": [node("n", t["id"], {"symbol": "DRAFT"})]}})
    assert r.status_code == 200 and r.json()["spec"]["tasks"][0]["paramOverrides"] == {"symbol": "DRAFT"}


def test_a_spec_saved_before_adr_0078_loads_with_no_overrides():
    old = {"tasks": [{"key": "a", "taskId": "t1", "taskVersion": 1, "dependsOn": [], "position": {"x": 0, "y": 0}}]}
    assert PipelineSpec.model_validate(old).tasks[0].param_overrides == {}


# ---- export ---------------------------------------------------------------------------------


def test_export_includes_each_nodes_overrides_next_to_the_tasks_own_config(env):
    t = fetch_quote_task(env)
    spec = {"tasks": [node("hood", t["id"], {"symbol": "HOOD"}), node("plain", t["id"])]}
    p = env.call("POST", "/pipelines", json={"name": "q", "spec": spec}).json()
    doc = yaml.safe_load(env.call("GET", f"/pipelines/{p['id']}/versions/1/export").text)
    nodes = {n["key"]: n for n in doc["spec"]["tasks"]}
    assert nodes["hood"]["paramOverrides"] == {"symbol": "HOOD"}
    assert nodes["plain"]["paramOverrides"] == {}
    assert nodes["hood"]["task"]["config"]["params"] == BASE_PARAMS  # the task's own, un-merged


# ---- validation: the same shape params already accepts ---------------------------------------


def test_an_override_over_the_params_size_cap_is_refused(env):
    t = fetch_quote_task(env)
    huge = {"blob": "x" * (MAX_PARAMS_BYTES + 1)}
    r = env.call("POST", "/pipelines", json={"name": "q", "spec": {"tasks": [node("n", t["id"], huge)]}})
    assert r.status_code == 422
    assert "paramOverrides" in r.json().get("field", "") and "limited to" in r.json()["error"]


def test_overrides_that_push_the_merged_params_over_the_cap_are_refused_at_save(env):
    half = "x" * (MAX_PARAMS_BYTES // 2 + 10)
    t = env.call("POST", "/tasks", json={"name": "big", "config": {"code": {"type": "inline", "source": "pass\n"}, "params": {"a": half}}}).json()
    r = env.call("POST", "/pipelines", json={"name": "q", "spec": {"tasks": [node("n", t["id"], {"b": half})]}})
    assert r.status_code == 422
    assert r.json()["field"] == "tasks[0].paramOverrides", r.json()


def test_overrides_must_be_an_object():
    with pytest.raises(ValueError):
        TaskRef.model_validate({"key": "a", "taskId": "t", "paramOverrides": ["not", "an", "object"]})


# ---- the merge itself -----------------------------------------------------------------------


def _cfg(params: dict[str, Any]) -> TaskConfig:
    return TaskConfig.model_validate({"code": {"type": "inline", "source": "pass\n"}, "params": params})


def test_merge_is_shallow_key_by_key_and_never_mutates_the_shared_config():
    base = _cfg({"symbol": "BASE", "window": {"days": 5, "unit": "d"}, "keep": True})
    a = TaskRef.model_validate({"key": "a", "taskId": "t", "paramOverrides": {"symbol": "HOOD", "window": {"days": 1}}})
    b = TaskRef.model_validate({"key": "b", "taskId": "t", "paramOverrides": {"symbol": "TSLQ"}})
    ca, cb = node_config(base, a, "tasks[0]"), node_config(base, b, "tasks[1]")
    assert ca.params == {"symbol": "HOOD", "window": {"days": 1}, "keep": True}  # a nested value is replaced whole
    assert cb.params == {"symbol": "TSLQ", "window": {"days": 5, "unit": "d"}, "keep": True}
    assert base.params == {"symbol": "BASE", "window": {"days": 5, "unit": "d"}, "keep": True}  # untouched
    assert json.dumps(ca.params) != json.dumps(cb.params)


def test_a_node_without_overrides_gets_the_tasks_config_as_is():
    base = _cfg({"symbol": "BASE"})
    assert node_config(base, TaskRef.model_validate({"key": "a", "taskId": "t"}), "tasks[0]") is base
