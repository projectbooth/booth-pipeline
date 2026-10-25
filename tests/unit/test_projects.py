"""ADR 0124 (contracts/projects.md): an optional project on pipelines and tasks, checked against a fake
booth-core registry; the ?project= filter with a parent's children; and the registry being away never
blocks listing, running or scheduling. A project is a label: nothing that decides access, schedules,
triggers or run-as reads it."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from booth_pipeline.projects import ProjectsClient

from .harness import Env, make_env, task

FINANCE = [
    {"slug": "finance", "name": "Finance", "parent": None, "archived": False},
    {"slug": "finance-dev", "name": "Finance dev", "parent": "finance", "archived": False},
    {"slug": "finance-prod", "name": "Finance prod", "parent": "finance", "archived": False},
    {"slug": "nfl", "name": "NFL", "parent": None, "archived": False},
    {"slug": "old", "name": "Old", "parent": None, "archived": True},
]


class FakeRegistry:
    """booth-core's GET /api/projects: answers `items`, or a status, or is unreachable."""

    def __init__(self) -> None:
        self.items = list(FINANCE)
        self.status = 200
        self.down = False
        self.calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/projects" and request.headers["X-Workspace"] == "acme"
        self.calls += 1
        if self.down:
            raise httpx.ConnectError("connection refused")
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "unavailable"})
        return httpx.Response(200, json=self.items)


@pytest.fixture
def registry() -> FakeRegistry:
    return FakeRegistry()


@pytest.fixture
def env(registry):
    client = ProjectsClient("http://core.test", transport=httpx.MockTransport(registry.handler), ttl=0)
    e = make_env(projects=client)
    with e.client:
        yield e


def pipeline(env: Env, name: str, project=None, **kw) -> dict:
    body = {"name": name, "spec": {"tasks": [task("a")]}, **kw}
    if project is not None:
        body["project"] = project
    r = env.call("POST", "/pipelines", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def names(env: Env, path: str) -> list[str]:
    return sorted(i["name"] for i in env.call("GET", path).json()["items"])


# ---- assigning ----------------------------------------------------------------------------------


def test_a_pipeline_and_a_task_carry_an_optional_project(env):
    p = pipeline(env, "finance-dev-silver", "finance-dev")
    assert p["project"] == "finance-dev" and env.call("GET", f"/pipelines/{p['id']}").json()["project"] == "finance-dev"
    assert pipeline(env, "loose")["project"] is None
    t = env.call("POST", "/tasks", json={"name": "t", "project": "nfl"}).json()
    assert t["project"] == "nfl" and env.call("GET", f"/tasks/{t['id']}").json()["project"] == "nfl"
    # Change, keep (an older UI sends no project), clear.
    r = env.call("PUT", f"/pipelines/{p['id']}", json={"name": "finance-dev-silver", "project": "finance-prod"})
    assert r.json()["project"] == "finance-prod"
    assert env.call("PUT", f"/pipelines/{p['id']}", json={"name": "renamed"}).json()["project"] == "finance-prod"
    assert env.call("PUT", f"/pipelines/{p['id']}", json={"name": "renamed", "project": None}).json()["project"] is None
    assert env.call("PUT", f"/tasks/{t['id']}", json={"name": "t", "project": None}).json()["project"] is None


@pytest.mark.parametrize(
    ("slug", "why"),
    [("nope", "no project 'nope'"), ("old", "archived"), ("Not A Slug", "not a project slug")],
)
def test_an_unknown_archived_or_malformed_project_is_refused(env, slug, why):
    r = env.call("POST", "/pipelines", json={"name": "p", "project": slug})
    assert r.status_code == 422 and r.json()["field"] == "project" and why in r.json()["error"], r.text
    r = env.call("POST", "/tasks", json={"name": "t", "project": slug})
    assert r.status_code == 422 and r.json()["field"] == "project"


def test_an_item_keeps_a_project_archived_after_it_was_assigned(env, registry):
    p = pipeline(env, "legacy", "nfl")
    registry.items = [{**i, "archived": True} if i["slug"] == "nfl" else i for i in registry.items]
    r = env.call("PUT", f"/pipelines/{p['id']}", json={"name": "legacy-2", "project": "nfl"})
    assert r.status_code == 200 and r.json()["project"] == "nfl"


@pytest.mark.parametrize("failure", ["503", "404", "down"])
def test_without_the_registry_assigning_is_refused_and_nothing_else_is(env, registry, failure):
    p = pipeline(env, "finance-prod", "finance-prod")
    if failure == "down":
        registry.down = True
    else:
        registry.status = int(failure)
    r = env.call("POST", "/pipelines", json={"name": "new", "project": "nfl"})
    assert r.status_code == 503 and "can't be assigned now" in r.json()["error"], r.text
    assert env.call("PUT", f"/pipelines/{p['id']}", json={"name": "finance-prod", "project": "nfl"}).status_code == 503
    # Clearing, keeping, creating without one, listing and running all carry on.
    assert env.call("PUT", f"/pipelines/{p['id']}", json={"name": "finance-prod", "project": "finance-prod"}).status_code == 200
    assert pipeline(env, "plain")["project"] is None
    assert names(env, "/pipelines") == ["finance-prod", "plain"]
    assert names(env, "/pipelines?project=finance-prod") == ["finance-prod"]  # exact slug, no children to ask about
    run = env.wait_run(env.call("POST", f"/pipelines/{p['id']}/run").json()["id"])
    assert run["status"] == "succeeded"
    assert env.call("PUT", f"/pipelines/{p['id']}", json={"name": "finance-prod", "project": None}).json()["project"] is None


def test_with_no_core_at_all_assigning_says_why(registry):
    e = make_env()  # no core.url: no registry client
    with e.client:
        r = e.call("POST", "/pipelines", json={"name": "p", "project": "nfl"})
        assert r.status_code == 503 and "no core.url" in r.json()["error"]
        assert e.call("POST", "/pipelines", json={"name": "p"}).status_code == 201


# ---- filtering ----------------------------------------------------------------------------------


def test_the_project_filter_includes_a_parents_children(env):
    pipeline(env, "finance-shared", "finance")
    pipeline(env, "finance-dev-silver", "finance-dev")
    pipeline(env, "finance-prod", "finance-prod")
    pipeline(env, "nfl-catchup", "nfl")
    pipeline(env, "loose")
    assert names(env, "/pipelines?project=finance") == ["finance-dev-silver", "finance-prod", "finance-shared"]
    assert names(env, "/pipelines?project=finance-dev") == ["finance-dev-silver"]
    assert names(env, "/pipelines?project=nfl") == ["nfl-catchup"]
    assert names(env, "/pipelines?project=nobody") == []
    assert names(env, "/pipelines?project=Bad%20Slug") == []
    assert len(names(env, "/pipelines")) == 5  # no filter: everything, unassigned included
    assert env.call("GET", "/pipelines?project=finance").json()["total"] == 3
    env.call("POST", "/tasks", json={"name": "dev-task", "project": "finance-dev"})
    env.call("POST", "/tasks", json={"name": "nfl-task", "project": "nfl"})
    assert [n for n in names(env, "/tasks?project=finance") if n.endswith("-task")] == ["dev-task"]


def test_the_registry_is_cached_briefly(registry):
    client = ProjectsClient("http://core.test", transport=httpx.MockTransport(registry.handler), ttl=60)
    e = make_env(projects=client)
    with e.client:
        for _ in range(3):
            e.call("GET", "/pipelines?project=finance")
        assert registry.calls == 1


def test_the_store_filter_and_the_migration(store):
    p = store.create_pipeline("acme", "a", "", "olga")
    q = store.create_pipeline("acme", "b", "", "olga")
    t = store.create_task("acme", "t", "", "olga")
    assert store.set_pipeline_project("acme", p.id, "finance-dev").project == "finance-dev"
    assert store.set_task_project("acme", t.id, "nfl").project == "nfl"
    assert store.get_pipeline("acme", q.id).project is None
    assert [x.id for x in store.list_pipelines("acme", "", 50, 0, frozenset({"finance", "finance-dev"})).items] == [p.id]
    assert store.list_pipelines("acme", "", 50, 0, frozenset()).total == 0
    assert store.list_pipelines("acme", "", 50, 0).total == 2
    assert [x.id for x in store.list_tasks("acme", "", 50, 0, frozenset({"nfl"})).items] == [t.id]
    assert store.set_pipeline_project("acme", p.id, None).project is None
    assert store.set_pipeline_project("other", p.id, "x") is None  # another workspace's row is not there


# ---- a label, never a decision ------------------------------------------------------------------


def test_a_scheduled_run_ignores_the_project_and_the_registry(registry):
    client = ProjectsClient("http://core.test", transport=httpx.MockTransport(registry.handler), ttl=0)
    e = make_env(projects=client)
    with e.client:
        p = pipeline(e, "finance-prod", "finance-prod")
        e.call("PUT", f"/pipelines/{p['id']}/schedule", json={"schedule": {"cron": "0 9 * * *"}})
        registry.down = True
        calls = registry.calls
        stored = e.store.get_pipeline("acme", p["id"])
        stored.next_run_at = datetime.now(UTC) - timedelta(minutes=1)
        e.store.update_schedule(stored)
        (rid,) = e.app.state.scheduler.tick()
        assert e.wait_run(rid)["status"] == "succeeded"
        assert registry.calls == calls  # a run never asks about projects


def test_nothing_that_decides_access_schedules_triggers_or_run_as_reads_a_project():
    src = Path(__file__).resolve().parents[2] / "src" / "booth_pipeline"
    deciding = ["auth.py", "owners.py", "workload.py", "runs.py", "scheduler.py", "schedule.py", "triggers.py", "event_bus.py", "engine.py", "service_accounts.py"]
    deciding += [str(Path("runners") / f.name) for f in (src / "runners").glob("*.py")]
    offenders = []
    for name in deciding:
        text = (src / name).read_text(encoding="utf-8").replace("projectbooth", "")
        if re.search(r"project", text, re.IGNORECASE):
            offenders.append(name)
    assert offenders == []
