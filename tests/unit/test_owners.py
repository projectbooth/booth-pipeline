# ruff: noqa: F811 - `world` is the e2e fixture, imported here and requested by name as each test's parameter
"""Who a pipeline runs as (ADR 0119 amendment 3, ADR 0120): Take over, service-account owners and who
may set them, the subject a run's token names, and what the API and run records say."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from booth_pipeline.owners import describe, parse_service_account

from .test_workload_e2e import World, spec_with_platform, world  # noqa: F401 - the fixture


def owner(world: World, pid: str, token: str = "tok-editor") -> dict | None:
    return world.call("GET", f"/pipelines/{pid}", token).json()["owner"]


def set_owner(world: World, pid: str, to: str, token: str):
    return world.call("PUT", f"/pipelines/{pid}/owner", token, json={"owner": to})


def scheduled_pipeline(world: World, token: str = "tok-editor") -> dict:
    return world.pipeline(spec_with_platform(), token=token, schedule={"cron": "0 * * * *"})


def run_scheduled(world: World, pid: str) -> dict:
    stored = world.store.get_pipeline("acme", pid)
    stored.next_run_at = datetime.now(UTC) - timedelta(minutes=1)
    world.store.update_schedule(stored)
    (rid,) = world.app.state.scheduler.tick()
    return world.wait_run(rid)


def test_the_first_scheduler_is_the_owner_and_is_named(world: World):
    p = scheduled_pipeline(world, "tok-editor")
    assert owner(world, p["id"]) == {"kind": "person", "name": "eddie", "you": True}
    assert owner(world, p["id"], "tok-owner") == {"kind": "person", "name": "eddie", "you": False}
    world.call("PUT", f"/pipelines/{p['id']}/schedule", "tok-owner", json={"schedule": {"cron": "0 9 * * *"}})
    assert owner(world, p["id"])["name"] == "eddie"  # saving a schedule never moves the owner
    assert "u-editor" not in str(world.call("GET", f"/pipelines/{p['id']}", "tok-viewer").json())  # a person's sub is not shown


@pytest.mark.parametrize("taker", ["tok-editor", "tok-owner"])
def test_an_editor_or_owner_can_take_over_a_person_owned_pipeline(world: World, taker):
    p = scheduled_pipeline(world, "tok-owner" if taker == "tok-editor" else "tok-editor")
    r = set_owner(world, p["id"], "me", taker)
    assert r.status_code == 200, r.text
    assert r.json()["owner"]["you"] is True
    run = run_scheduled(world, p["id"])
    assert run["status"] == "succeeded"
    assert world.mints()[-1].json()["owner"] == {"tok-editor": "u-editor", "tok-owner": "u-owner"}[taker]


def test_a_viewer_cannot_take_over(world: World):
    p = scheduled_pipeline(world)
    assert set_owner(world, p["id"], "me", "tok-viewer").status_code == 403


def test_only_a_workspace_owner_can_make_a_service_account_the_owner(world: World):
    p = scheduled_pipeline(world)
    r = set_owner(world, p["id"], "svc:acme:nfl-weekly", "tok-editor")
    assert r.status_code == 403 and "only a workspace owner" in r.json()["error"]
    assert owner(world, p["id"])["kind"] == "person"  # unchanged
    r = set_owner(world, p["id"], "svc:acme:nfl-weekly", "tok-owner")
    assert r.status_code == 200, r.text
    assert r.json()["owner"] == {"kind": "service", "id": "svc:acme:nfl-weekly", "name": "nfl-weekly", "you": False}


def test_only_a_workspace_owner_can_take_a_pipeline_from_a_service_account(world: World):
    p = scheduled_pipeline(world)
    assert set_owner(world, p["id"], "svc:acme:nfl-weekly", "tok-owner").status_code == 200
    r = set_owner(world, p["id"], "me", "tok-editor")
    assert r.status_code == 403 and "only a workspace owner can take over" in r.json()["error"]
    assert set_owner(world, p["id"], "me", "tok-owner").status_code == 200


@pytest.mark.parametrize(
    ("to", "message"),
    [
        ("u-viewer", "never handed to another person"),
        ("svc:globex:nfl", "belongs to another workspace"),
        ("svc:acme:", "is not a service account id"),
        ("svc:acme:bad name", "is not a service account id"),
    ],
)
def test_nobody_hands_a_pipeline_to_another_person_or_a_foreign_account(world: World, to, message):
    p = scheduled_pipeline(world)
    r = set_owner(world, p["id"], to, "tok-owner")
    assert r.status_code == 422 and message in r.json()["error"] and r.json()["field"] == "owner"


def test_a_service_account_owned_schedule_mints_for_the_account_with_the_pipelines_subject(world: World):
    p = scheduled_pipeline(world)
    set_owner(world, p["id"], "svc:acme:nfl-weekly", "tok-owner")
    run = run_scheduled(world, p["id"])
    assert run["status"] == "succeeded"
    assert run["actsAs"] == {"kind": "service", "id": "svc:acme:nfl-weekly", "name": "nfl-weekly", "you": False}
    body = world.mints()[-1].json()
    assert body["owner"] == "svc:acme:nfl-weekly" and body["subject"] == f"pipeline:acme:{run['id']}"


def test_a_manual_run_acts_as_whoever_pressed_run_now_even_on_a_service_account_pipeline(world: World):
    p = scheduled_pipeline(world)
    set_owner(world, p["id"], "svc:acme:nfl-weekly", "tok-owner")
    r = world.call("POST", f"/pipelines/{p['id']}/run", "tok-editor")
    run = world.wait_run(r.json()["id"])
    assert run["actsAs"] == {"kind": "person", "name": "eddie", "you": True}
    assert world.mints()[-1].json()["owner"] == "u-editor"


def test_a_disabled_account_fails_the_run_with_its_reason(world: World):
    p = scheduled_pipeline(world)
    set_owner(world, p["id"], "svc:acme:nfl-weekly", "tok-owner")
    world.core.handler = lambda req: (403, {"error": "forbidden", "code": "service_account_disabled"})
    run = run_scheduled(world, p["id"])
    assert run["status"] == "failed"
    assert "the pipeline's owner, service account svc:acme:nfl-weekly, is disabled; a workspace owner can enable it or choose another owner" in run["error"]


def test_which_pipelines_run_as_each_service_account(world: World):
    a, b, c = (scheduled_pipeline(world) for _ in range(3))
    set_owner(world, a["id"], "svc:acme:nfl-weekly", "tok-owner")
    set_owner(world, b["id"], "svc:acme:nfl-weekly", "tok-owner")
    set_owner(world, c["id"], "svc:acme:reports", "tok-owner")
    items = world.call("GET", "/service-accounts/uses", "tok-viewer").json()["items"]
    assert [(i["account"], sorted(x["id"] for x in i["pipelines"])) for i in items] == [
        ("svc:acme:nfl-weekly", sorted([a["id"], b["id"]])),
        ("svc:acme:reports", [c["id"]]),
    ]
    assert all(x["scheduled"] for i in items for x in i["pipelines"])


def test_owner_helpers():
    assert parse_service_account("svc:acme:nfl-weekly").id == "svc:acme:nfl-weekly"
    assert parse_service_account("svc:acme") is None and parse_service_account("u-1") is None
    assert describe("svc:acme:x", "x") == "service account svc:acme:x"
    assert describe("u-1", "") == "the person who first scheduled it"


def test_an_account_is_checked_with_core_as_the_caller_before_it_gets_a_pipeline(world: World):
    p = scheduled_pipeline(world)
    assert set_owner(world, p["id"], "svc:acme:nfl-weekly", "tok-owner").status_code == 200
    (ask,) = [r for r in world.platform.requests if r.path.startswith("/api/service-accounts/")]
    assert ask.path == "/api/service-accounts/nfl-weekly" and ask.method == "GET"
    assert ask.headers["authorization"] == "Bearer tok-owner" and ask.headers["x-workspace"] == "acme"


def test_an_account_core_doesnt_have_is_refused(world: World):
    p = scheduled_pipeline(world)
    r = set_owner(world, p["id"], "svc:acme:nobody", "tok-owner")
    assert r.status_code == 422 and r.json() == {"error": "this workspace has no service account named 'nobody'", "field": "owner"}
    assert owner(world, p["id"])["kind"] == "person"


def test_a_disabled_account_cant_be_given_a_pipeline(world: World):
    p = scheduled_pipeline(world)
    world.platform.accounts["nfl-weekly"] = True
    r = set_owner(world, p["id"], "svc:acme:nfl-weekly", "tok-owner")
    assert r.status_code == 422 and "is disabled; enable it" in r.json()["error"]


def test_core_down_means_the_account_cant_be_checked_so_nothing_changes(world: World):
    p = scheduled_pipeline(world)
    world.platform.handler = lambda req: (502, {"error": "bad gateway"})
    r = set_owner(world, p["id"], "svc:acme:nfl-weekly", "tok-owner")
    assert r.status_code == 503 and "could not check service account 'nfl-weekly' with booth-core" in r.json()["error"]
    assert owner(world, p["id"])["kind"] == "person"
