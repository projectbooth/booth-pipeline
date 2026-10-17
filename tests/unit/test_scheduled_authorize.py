# ruff: noqa: F811 - `world` is the e2e fixture, imported here and requested by name as each test's parameter
"""ADR 0119 amendment 3 point 3: a scheduled run asks core once, at its start, whether it may act for
its owner, whatever its tasks need. If core refuses, the whole run fails before any task runs, for a
person owner and a service account alike. A manual run is unaffected."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from booth_pipeline.runs import RunManager

from .harness import make_env, task
from .test_workload_e2e import World, spec_with_platform, world  # noqa: F401 - the fixture


def tokenless(world: World) -> dict:
    """A pipeline none of whose tasks needs a token: before this rule, it ran even for a lapsed owner."""
    return world.pipeline([task("a"), task("b", ["a"])], schedule={"cron": "0 * * * *"})


def fire(world: World, pid: str) -> dict:
    stored = world.store.get_pipeline("acme", pid)
    stored.next_run_at = datetime.now(UTC) - timedelta(minutes=1)
    world.store.update_schedule(stored)
    (rid,) = world.app.state.scheduler.tick()
    return world.wait_run(rid)


def statuses(run: dict) -> dict[str, str]:
    return {t["taskKey"]: t["status"] for t in run["tasks"]}


def test_a_scheduled_run_whose_person_owner_is_refused_runs_nothing(world: World):
    p = tokenless(world)
    world.core.handler = lambda req: (403, {"error": "forbidden"})
    run = fire(world, p["id"])
    assert run["status"] == "failed"
    assert run["error"].startswith("not started: core would not authorize this scheduled run: booth-core would not issue a token for eddie:")
    assert statuses(run) == {"a": "skipped", "b": "skipped"}
    assert world.runner.seen == []  # no task's code ever ran
    assert len(world.mints()) == 1
    assert "not started: core would not authorize" in world.logs(run["id"])


def test_a_scheduled_run_whose_service_account_is_disabled_runs_nothing(world: World):
    p = tokenless(world)
    world.call("PUT", f"/pipelines/{p['id']}/owner", "tok-owner", json={"owner": "svc:acme:nfl-weekly"})
    world.core.handler = lambda req: (403, {"error": "forbidden", "code": "service_account_disabled"})
    run = fire(world, p["id"])
    assert run["status"] == "failed" and statuses(run) == {"a": "skipped", "b": "skipped"}
    assert "service account svc:acme:nfl-weekly, is disabled" in run["error"]


def test_an_authorized_scheduled_run_goes_ahead_and_its_tasks_still_mint_their_own(world: World):
    p = world.pipeline(spec_with_platform(), schedule={"cron": "0 * * * *"})
    run = fire(world, p["id"])
    assert run["status"] == "succeeded"
    assert len(world.mints()) == 2  # the start check, then the one task that opted in
    assert all(m.json()["subject"] == f"pipeline:acme:{run['id']}" for m in world.mints())
    assert "authorized: core will issue this run's tokens (role editor)" in world.logs(run["id"])


def test_a_manual_run_is_not_checked_at_start(world: World):
    p = tokenless(world)
    world.core.handler = lambda req: (403, {"error": "forbidden"})
    run = world.wait_run(world.call("POST", f"/pipelines/{p['id']}/run").json()["id"])
    assert run["status"] == "succeeded" and world.mints() == []


def test_core_unreachable_fails_the_scheduled_run_after_retrying(world: World, monkeypatch):
    monkeypatch.setattr(RunManager, "authorize_wait_seconds", 0.01)
    p = tokenless(world)
    world.core.handler = lambda req: (503, {"error": "down"})
    run = fire(world, p["id"])
    assert run["status"] == "failed" and statuses(run) == {"a": "skipped", "b": "skipped"}
    assert run["error"].startswith("not started: could not reach booth-core to authorize this scheduled run")
    assert len(world.mints()) == 3


def test_an_install_without_workload_identity_runs_scheduled_pipelines_unchecked():
    """No minting credential means no token can ever be issued, so there is nothing to ask."""
    env = make_env()
    with env.client:
        p = env.call("POST", "/pipelines", json={"name": "p", "spec": {"tasks": [task("a")]}}).json()
        env.call("PUT", f"/pipelines/{p['id']}/schedule", json={"schedule": {"cron": "0 * * * *"}})
        stored = env.store.get_pipeline("acme", p["id"])
        stored.next_run_at = datetime.now(UTC) - timedelta(minutes=1)
        env.store.update_schedule(stored)
        (rid,) = env.app.state.scheduler.tick()
        assert env.wait_run(rid)["status"] == "succeeded"
