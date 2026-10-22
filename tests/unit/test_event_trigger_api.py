# ruff: noqa: F811 - `world` is the e2e fixture, imported here and requested by name as a test's parameter
"""ADR 0123 through the HTTP API: saving an event trigger, an event-fired run end to end (with
``ctx.trigger`` in the task), what viewers see, and the start-time authorization event-fired runs share
with scheduled ones."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from booth_pipeline.event_bus import TriggerIngest

from .harness import Env, make_env, task
from .test_workload_e2e import World, world  # noqa: F401 - the fixture

INBOX = {"storage": {"backendId": "local", "prefix": "finance/dev/inbox/"}}
SHOW_TRIGGER = (
    "def run(ctx):\n"
    "    print('trigger', ctx.trigger.type, ctx.trigger.count, ctx.trigger.missed_events)\n"
    "    for e in ctx.trigger.events:\n"
    "        print('file', e['path'])\n"
)


@pytest.fixture
def env():
    e = make_env()
    e.app.state.service.event_bus = True  # as if core had granted the bus credential
    with e.client:
        yield e


def new_pipeline(env: Env, source: str = SHOW_TRIGGER) -> dict:
    r = env.call("POST", "/pipelines", json={"name": "silver", "spec": {"tasks": [task("load", source=source)]}})
    assert r.status_code == 201, r.text
    return r.json()


def put_trigger(env: Env, pid: str, event_trigger, token: str = "tok-editor", **body):
    return env.call("PUT", f"/pipelines/{pid}/schedule", token, json={"eventTrigger": event_trigger, **body})


def upload(path: str, principal: str = "streamlit:acme:finance-dev") -> tuple[str, bytes]:
    data = {"backendId": "local", "path": path, "size": 12, "principal": principal, "via": "app-upload"}
    env = {"workspace": "acme", "eventType": "storage.object.created", "publishedAt": datetime.now(UTC).isoformat(), "publishedBy": "storage", "data": data}
    return "booth.acme.storage.object.created", json.dumps(env).encode()


def test_saving_an_event_trigger(env: Env):
    p = new_pipeline(env)
    r = put_trigger(env, p["id"], {"match": [INBOX], "debounceSeconds": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["eventTrigger"] == {"type": "event", "enabled": True, "match": [{"storage": {"backendId": "local", "prefix": "finance/dev/inbox/"}}],
                                    "debounceSeconds": 5, "maxWaitSeconds": 60, "whenRunning": "queue_one", "minGapSeconds": 5}  # fmt: skip
    assert body["schedule"] is None and body["owner"]["name"] == "eddie"  # the first trigger sets the owner, as a schedule does
    # An older UI's body has no eventTrigger: the saved one is kept. null removes it.
    assert env.call("PUT", f"/pipelines/{p['id']}/schedule", json={"schedule": {"type": "interval", "seconds": 3600}}).json()["eventTrigger"]["debounceSeconds"] == 5
    assert put_trigger(env, p["id"], None).json()["eventTrigger"] is None
    assert put_trigger(env, p["id"], {"match": [INBOX]}, "tok-viewer").status_code == 403


def test_a_trigger_is_refused_for_an_unknown_backend_or_without_the_bus(env: Env):
    p = new_pipeline(env)
    r = put_trigger(env, p["id"], {"match": [{"storage": {"backendId": "nope", "prefix": "x/"}}]})
    assert r.status_code == 422 and r.json()["field"] == "eventTrigger.match[0].storage.backendId"
    env.storage.down = True
    assert put_trigger(env, p["id"], {"match": [{"storage": {"backendId": "lake"}}]}).status_code == 503
    env.storage.down = False
    assert put_trigger(env, p["id"], {"match": [{"api": {"slugPrefix": "finance-dev-"}}]}).status_code == 200
    env.app.state.service.event_bus = False
    r = put_trigger(env, p["id"], {"match": [INBOX]})
    assert r.status_code == 422 and "event bus" in r.json()["error"]
    assert put_trigger(env, p["id"], {"match": [INBOX], "enabled": False}).status_code == 200  # a paused one still saves
    assert env.call("GET", "/config").json()["eventTriggers"]["available"] is False


def test_an_event_fired_run_end_to_end(env: Env):
    p = new_pipeline(env)
    put_trigger(env, p["id"], {"match": [INBOX], "debounceSeconds": 1})
    ingest, now = TriggerIngest(env.store), datetime.now(UTC)
    for i, path in enumerate(["finance/dev/inbox/a.csv", "finance/dev/inbox/b.csv", "elsewhere/c.csv"]):
        ingest.handle(*upload(path), seq=i + 1, now=now)
    (rid,) = env.app.state.scheduler.tick(now + timedelta(seconds=2))
    run = env.wait_run(rid)
    assert run["status"] == "succeeded" and run["trigger"] == "event" and run["triggeredBy"] == "2 files"
    logs = [ln["message"] for ln in env.call("GET", f"/runs/{rid}/logs").json()["items"]]
    assert "trigger event 2 False" in logs and "file finance/dev/inbox/a.csv" in logs and "file finance/dev/inbox/b.csv" in logs
    assert [e["path"] for e in run["triggerEvents"]["events"]] == ["finance/dev/inbox/a.csv", "finance/dev/inbox/b.csv"]
    # Viewers get the count, not the paths.
    viewer = env.call("GET", f"/runs/{rid}", "tok-viewer").json()
    assert viewer["triggerEvents"] == {"count": 2, "missedEvents": False}
    assert "triggerEvents" in env.call("GET", f"/runs?pipelineId={p['id']}", "tok-viewer").json()["items"][0]


def test_a_manual_run_sees_a_manual_trigger(env: Env):
    p = new_pipeline(env)
    run = env.wait_run(env.call("POST", f"/pipelines/{p['id']}/run").json()["id"])
    assert "trigger manual 0 False" in [ln["message"] for ln in env.call("GET", f"/runs/{run['id']}/logs").json()["items"]]
    assert run["triggerEvents"] is None


def test_the_trigger_status_line(env: Env):
    p = new_pipeline(env)
    put_trigger(env, p["id"], {"match": [INBOX]})
    now = datetime.now(UTC)
    TriggerIngest(env.store).handle(*upload("finance/dev/inbox/a.csv"), seq=1, now=now)
    s = env.call("GET", f"/pipelines/{p['id']}/event-trigger").json()
    assert s["enabled"] and s["pendingEvents"] == 1 and not s["queued"]
    assert s["nextFireAt"] == (now + timedelta(seconds=10)).isoformat()
    assert s["lastEvent"]["path"] == "finance/dev/inbox/a.csv" and s["lastEvent"]["principal"] == "streamlit:acme:finance-dev"
    assert s["bus"]["state"] == "unavailable" and "no event-bus credential" in s["bus"]["reason"]  # this test app has no bus
    viewer = env.call("GET", f"/pipelines/{p['id']}/event-trigger", "tok-viewer").json()
    assert viewer["lastEvent"] == {"type": "storage.object.created"} and viewer["lastEventAt"] == s["lastEventAt"]


def test_an_event_fired_run_is_authorized_at_its_start_like_a_scheduled_one(world: World):
    world.app.state.service.event_bus = True
    p = world.pipeline([task("a")])
    r = world.call("PUT", f"/pipelines/{p['id']}/schedule", json={"eventTrigger": {"match": [{"api": {"slugs": ["finance-dev-tx"]}}], "debounceSeconds": 1}})
    assert r.status_code == 200, r.text
    world.core.handler = lambda req: (403, {"error": "forbidden"})
    data = {"apiSlug": "finance-dev-tx", "dataset": "d", "table": "t", "operation": "insert", "rows": 1, "principal": "streamlit:acme:app"}
    body = {"workspace": "acme", "eventType": "api.write.committed", "publishedAt": datetime.now(UTC).isoformat(), "publishedBy": "api", "data": data}
    now = datetime.now(UTC)
    TriggerIngest(world.store).handle("booth.acme.api.write.committed", json.dumps(body).encode(), seq=1, now=now)
    (rid,) = world.app.state.scheduler.tick(now + timedelta(seconds=2))
    run = world.wait_run(rid)
    assert run["status"] == "failed" and run["triggeredBy"] == "1 write"
    assert run["error"].startswith("not started: core would not authorize this event-fired run")
    assert world.runner.seen == []
