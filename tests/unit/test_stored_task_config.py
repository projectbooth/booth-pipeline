"""A task that uses none of the StockScanner features is stored with exactly the keys 0.3.0 wrote. booth-pipeline's
models refuse unknown fields, so a stored `secrets: []` would make every task saved on a newer release unreadable after
a rollback to 0.3.0. Configs stored either way keep reading."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from booth_pipeline.model import TaskConfig

from .harness import make_env

# What 0.3.0 stores for a task, spelled out: this set must not grow for a task that doesn't use a new feature.
KEYS_0_3_0 = {"code", "runner", "retry", "params", "timeoutSeconds", "platformAccess", "requirements", "spark"}
NEW_KEYS = {"secrets", "runnerImage", "modules"}
WS = "acme"
T0 = datetime(2026, 11, 1, tzinfo=UTC)
PLAIN = {"code": {"type": "inline", "source": "pass\n"}}
MODULE = {"name": "cal", "entryId": "e1", "version": "1.0.0", "source": "x = 1\n", "sha256": "0" * 64}


def dump(config: TaskConfig) -> dict:
    return config.model_dump(by_alias=True, mode="json")


def test_a_plain_task_dumps_exactly_the_0_3_0_keys():
    assert set(dump(TaskConfig.model_validate(PLAIN))) == KEYS_0_3_0


def test_explicitly_empty_values_are_not_stored_either():
    config = TaskConfig.model_validate({**PLAIN, "secrets": [], "runnerImage": None, "modules": []})
    assert set(dump(config)) == KEYS_0_3_0


def test_the_field_names_are_omitted_too_not_only_the_wire_names():
    assert not {"secrets", "runner_image", "modules"} & set(TaskConfig.model_validate(PLAIN).model_dump())


@pytest.mark.parametrize(
    ("extra", "key"),
    [
        ({"secrets": [{"name": "FMP", "env": "FMP_KEY"}]}, "secrets"),
        ({"runnerImage": "stocks-py"}, "runnerImage"),
        ({"modules": [MODULE]}, "modules"),
    ],
)
def test_a_feature_in_use_is_kept_and_only_that_key_is_added(extra, key):
    dumped = dump(TaskConfig.model_validate({**PLAIN, **extra}))
    assert set(dumped) == KEYS_0_3_0 | {key}
    assert TaskConfig.model_validate(dumped) == TaskConfig.model_validate({**PLAIN, **extra})


def test_configs_stored_either_way_read_back_with_the_defaults():
    old = dump(TaskConfig.model_validate(PLAIN))  # as 0.3.0 stored it
    assert not NEW_KEYS & set(old)
    from_0_3_0 = TaskConfig.model_validate(old)
    assert (from_0_3_0.secrets, from_0_3_0.runner_image, from_0_3_0.modules) == ([], None, [])
    stored_with_empties = TaskConfig.model_validate({**old, "secrets": [], "runnerImage": None, "modules": []})
    assert stored_with_empties == from_0_3_0


def test_the_store_round_trips_a_plain_task_and_its_draft_with_the_0_3_0_key_set(store):
    from booth_pipeline.store.postgres import PostgresStore

    task = store.create_task(WS, "plain", "", "a")
    config = TaskConfig.model_validate(PLAIN)
    store.add_task_version(WS, task.id, config, "", "a")
    store.save_task_draft(WS, task.id, config, "a")
    assert store.get_task_version(WS, task.id, 1).config == config
    assert store.get_task_draft(WS, task.id).config == config
    if isinstance(store, PostgresStore):  # the row itself: what a rollback would read
        with store._pool.connection() as conn:
            version = conn.execute("SELECT config FROM task_versions WHERE task_id=%s", (task.id,)).fetchone()["config"]
            draft = conn.execute("SELECT draft_config FROM tasks WHERE id=%s", (task.id,)).fetchone()["draft_config"]
        assert set(version) == KEYS_0_3_0 and set(draft) == KEYS_0_3_0


def test_the_store_keeps_a_feature_in_use(store):
    task = store.create_task(WS, "uses", "", "a")
    config = TaskConfig.model_validate({**PLAIN, "secrets": [{"name": "FMP", "env": "FMP_KEY"}], "runnerImage": "stocks-py", "modules": [MODULE]})
    store.add_task_version(WS, task.id, config, "", "a")
    got = store.get_task_version(WS, task.id, 1).config
    assert got == config and got.secrets[0].name == "FMP" and got.runner_image == "stocks-py" and got.modules[0].name == "cal"


def test_through_the_api_a_plain_task_has_the_0_3_0_keys_and_removing_a_secret_removes_the_key():
    env = make_env()
    try:
        r = env.call("POST", "/tasks", json={"name": "t", "config": PLAIN})
        assert r.status_code == 201, r.text
        t = r.json()["id"]
        saved = env.call("GET", f"/tasks/{t}/versions/1").json()["config"]
        assert set(saved) == KEYS_0_3_0
        withs = env.call("POST", f"/tasks/{t}/versions", json={"config": {**saved, "secrets": [{"name": "FMP", "env": "FMP_KEY"}]}, "notes": ""})
        assert withs.status_code == 201, withs.text
        assert "secrets" in withs.json()["config"]
        without = env.call("POST", f"/tasks/{t}/versions", json={"config": {**saved, "secrets": []}, "notes": ""})
        assert without.status_code == 201, without.text
        assert set(without.json()["config"]) == KEYS_0_3_0
    finally:
        env.client.close()


def test_a_plain_task_still_runs():
    env = make_env()
    try:
        t = env.call("POST", "/tasks", json={"name": "t", "config": {"code": {"type": "inline", "source": "def run(ctx):\n    return 1\n"}}}).json()["id"]
        spec = {"tasks": [{"key": "a", "taskId": t, "taskVersion": 1, "dependsOn": [], "position": {"x": 0, "y": 0}}]}
        p = env.call("POST", "/pipelines", json={"name": "p", "spec": spec}).json()
        run = env.wait_run(env.call("POST", f"/pipelines/{p['id']}/run").json()["id"])
        assert run["status"] == "succeeded", run
    finally:
        env.client.close()
