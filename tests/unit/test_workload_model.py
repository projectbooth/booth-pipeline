"""Workload identity's data: the per-task opt-in, the job's owner and ceiling, and their persistence."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from booth_pipeline.config import Config, ConfigError
from booth_pipeline.model import PipelineSpec
from booth_pipeline.records import Job, Run
from booth_pipeline.schemas import JobInput

from .helpers import spec, task

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def test_platform_access_is_off_by_default_and_round_trips_in_camel_case():
    plain = spec(task("a", "source"))
    assert plain.tasks[0].platform_access is False
    on = spec(task("a", "source", platformAccess=True))
    assert on.tasks[0].platform_access is True
    dumped = on.model_dump(by_alias=True)
    assert dumped["tasks"][0]["platformAccess"] is True
    assert PipelineSpec.model_validate(dumped) == on


def test_a_spec_saved_before_this_existed_still_loads_with_access_off():
    old = {"tasks": [{"key": "a", "kind": "source", "code": {"type": "inline", "source": "x = 1"}}]}
    assert PipelineSpec.model_validate(old).tasks[0].platform_access is False


def test_the_role_ceiling_defaults_to_editor_and_can_never_be_owner():
    assert JobInput(name="j", pipeline_id="p").role_ceiling == "editor"
    assert JobInput(name="j", pipeline_id="p", role_ceiling="viewer").role_ceiling == "viewer"
    for bad in ("owner", "admin", "", "EDITOR"):
        with pytest.raises(ValidationError):
            JobInput(name="j", pipeline_id="p", role_ceiling=bad)


def test_owner_and_ceiling_are_persisted_on_jobs_and_runs(store):
    p = store.create_pipeline("acme", "etl", "", "a")
    job = Job(str(uuid4()), "acme", "j", p.id, None, None, None, False, "alice", T0, T0, None, owner_sub="sub-alice", role_ceiling="viewer")
    store.create_job(job)
    got = store.get_job("acme", job.id)
    assert (got.owner_sub, got.role_ceiling) == ("sub-alice", "viewer")
    got.owner_sub, got.role_ceiling = "sub-bob", "editor"
    store.update_job(got)
    assert (store.get_job("acme", job.id).owner_sub, store.get_job("acme", job.id).role_ceiling) == ("sub-bob", "editor")
    run = Run(str(uuid4()), "acme", job.id, p.id, 1, "queued", "manual", "bob", T0, owner_sub="sub-bob")
    store.create_run(run, ["a"])
    assert store.get_run("acme", run.id).owner_sub == "sub-bob"


def test_jobs_created_before_workload_identity_default_to_no_owner_and_the_editor_ceiling(store):
    p = store.create_pipeline("acme", "etl", "", "a")
    job = store.create_job(Job(str(uuid4()), "acme", "j", p.id, None, None, None, False, "alice", T0, T0))
    got = store.get_job("acme", job.id)
    assert (got.owner_sub, got.role_ceiling) == ("", "editor")


# ---- config ----------------------------------------------------------------------------------


def test_storage_and_catalog_default_to_the_gateway_and_can_be_pointed_at_a_module_directly():
    c = Config(core_url="http://booth-core:8080")
    assert (c.storage_base, c.catalog_base) == ("http://booth-core:8080/modules/storage", "http://booth-core:8080/modules/catalog")
    direct = Config(core_url="http://booth-core:8080", storage_url="http://booth-storage:8080", catalog_url="http://booth-catalog:8080")
    assert (direct.storage_base, direct.catalog_base) == ("http://booth-storage:8080", "http://booth-catalog:8080")
    assert Config().storage_base == ""  # no core configured: tasks that opt in get nowhere to call and fail clearly


def test_a_runner_url_needs_its_shared_secret():
    base = dict(dev_memory=True, oidc_issuer_url="https://i/r", oidc_client_id="c")
    with pytest.raises(ConfigError, match="RUNNER_AUTH_TOKEN"):
        Config(**base, runner_url="http://runner:8080").validate()
    Config(**base, runner_url="http://runner:8080", runner_auth_token_file="/x").validate()


def test_the_runner_secret_is_read_from_its_file_when_given(tmp_path):
    f = tmp_path / "s"
    f.write_text("from-file\n")
    assert Config(runner_auth_token_file=str(f), runner_auth_token="from-env").runner_secret() == "from-file"
    assert Config(runner_auth_token="from-env").runner_secret() == "from-env"
