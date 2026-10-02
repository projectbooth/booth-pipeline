"""Workload identity's data: the per-task opt-in, a pipeline's owner and ceiling, and their
persistence (ADR 0071 folds owner/ceiling directly onto Pipeline; there is no more separate Job)."""

from __future__ import annotations

from datetime import UTC, datetime

from booth_pipeline.config import Config, ConfigError
from booth_pipeline.records import Run

from .helpers import spec, task_config, task_ref

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def test_platform_access_is_off_by_default_and_round_trips_in_camel_case():
    from booth_pipeline.model import TaskConfig

    plain = TaskConfig.model_validate(task_config())
    assert plain.platform_access is False
    on = TaskConfig.model_validate(task_config(platformAccess=True))
    assert on.platform_access is True
    dumped = on.model_dump(by_alias=True)
    assert dumped["platformAccess"] is True
    assert TaskConfig.model_validate(dumped) == on


def test_owner_and_ceiling_are_persisted_on_pipelines_and_runs(store):
    p = store.create_pipeline("acme", "etl", "", "a")
    p.owner_sub, p.role_ceiling = "sub-alice", "viewer"
    store.update_schedule(p)
    got = store.get_pipeline("acme", p.id)
    assert (got.owner_sub, got.role_ceiling) == ("sub-alice", "viewer")
    got.owner_sub, got.role_ceiling = "sub-bob", "editor"
    store.update_schedule(got)
    assert (store.get_pipeline("acme", p.id).owner_sub, store.get_pipeline("acme", p.id).role_ceiling) == ("sub-bob", "editor")
    run = Run("r1", "acme", p.id, 1, "queued", "manual", "bob", T0, owner_sub="sub-bob")
    store.create_run(run, ["a"])
    assert store.get_run("acme", run.id).owner_sub == "sub-bob"


def test_pipelines_created_before_workload_identity_default_to_no_owner_and_the_editor_ceiling(store):
    p = store.create_pipeline("acme", "etl", "", "a")
    assert (p.owner_sub, p.role_ceiling) == ("", "editor")


# ---- config ----------------------------------------------------------------------------------


def test_storage_and_catalog_default_to_the_gateway_and_can_be_pointed_at_a_module_directly():
    c = Config(core_url="http://booth-core:8080")
    assert (c.storage_base, c.catalog_base) == ("http://booth-core:8080/modules/storage", "http://booth-core:8080/modules/catalog")
    direct = Config(core_url="http://booth-core:8080", storage_url="http://booth-storage:8080", catalog_url="http://booth-catalog:8080")
    assert (direct.storage_base, direct.catalog_base) == ("http://booth-storage:8080", "http://booth-catalog:8080")
    assert Config().storage_base == ""  # no core configured: tasks that opt in get nowhere to call and fail clearly


def test_task_jobs_are_configured_from_the_environment(monkeypatch):
    """ADR 0096: the chart points the API/scheduler pod at its rendered task Job template; there is
    no shared-runner URL any more."""
    for k, v in {
        "BOOTH_PIPELINE_DEV_MEMORY": "true",
        "BOOTH_OIDC_ISSUER_URL": "https://i/r",
        "BOOTH_OIDC_CLIENT_ID": "c",
        "BOOTH_PIPELINE_TASK_JOB_TEMPLATE": "/etc/booth/task-job/job.json",
        "BOOTH_PIPELINE_MAX_CONCURRENT_TASKS": "4",
        "BOOTH_PIPELINE_TASK_START_TIMEOUT_SECONDS": "120",
        "BOOTH_PIPELINE_TASK_NAME_PREFIX": "rel-booth-pipeline-task",
        "BOOTH_WORKLOAD_MINT_DIR": "/nonexistent",
    }.items():
        monkeypatch.setenv(k, v)
    c = Config.from_env()
    assert (c.task_job_template, c.max_concurrent_tasks, c.task_start_timeout_seconds, c.task_name_prefix) == (
        "/etc/booth/task-job/job.json",
        4,
        120,
        "rel-booth-pipeline-task",
    )
    assert not hasattr(c, "runner_url")


def test_the_task_concurrency_cap_must_be_at_least_one(monkeypatch):
    import pytest

    monkeypatch.setenv("BOOTH_PIPELINE_MAX_CONCURRENT_TASKS", "0")
    with pytest.raises(ConfigError, match="MAX_CONCURRENT_TASKS"):
        Config.from_env()


def test_a_pipelines_task_refs_have_no_kind_and_carry_no_execution_config_of_their_own():
    """ADR 0071: a PipelineSpec's nodes are references (key/taskId/taskVersion/dependsOn/position)
    only — platform access, retry, code, runner, timeout live on the referenced Task's own
    TaskConfig. The one ratified exception is ADR 0078's ``paramOverrides``: values layered over
    the task's params, never a node-level copy of any other execution setting."""
    from booth_pipeline.model import TaskConfig, TaskRef

    s = spec(task_ref("a", task_id="t1"))
    assert s.tasks[0].model_dump(by_alias=True).keys() == {"key", "taskId", "taskVersion", "dependsOn", "position", "paramOverrides"}
    execution = {"code", "runner", "retry", "timeout_seconds", "platform_access", "params"}
    assert execution <= set(TaskConfig.model_fields) and not execution & set(TaskRef.model_fields)
    assert "kind" not in TaskRef.model_fields and "kind" not in TaskConfig.model_fields
