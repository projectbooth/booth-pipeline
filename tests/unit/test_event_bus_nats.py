"""ADR 0123: event triggers' consumer against a real NATS JetStream server (hack/docker-compose.test.yml).

The burst rules are tested through both stores in test_event_triggers.py; this is the transport: the
shared durable consumer, new events only on first creation, a restart, a lost consumer, events lost to
an outage longer than the stream keeps them, two replicas, and a redelivery after a failed write."""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import UTC, datetime, timedelta

import pytest

from booth_pipeline.config import Config
from booth_pipeline.event_bus import DURABLE, STREAM, EventBus, TriggerIngest
from booth_pipeline.model import EventTrigger
from booth_pipeline.store.memory import MemoryStore

WS = "acme"


def nats_url() -> str:
    url = os.environ.get("BOOTH_TEST_NATS_URL")
    if url:
        return url
    if os.environ.get("BOOTH_TEST_REQUIRE_EMULATORS") == "1":
        pytest.fail("BOOTH_TEST_NATS_URL is not set but BOOTH_TEST_REQUIRE_EMULATORS=1")
    pytest.skip("BOOTH_TEST_NATS_URL not set; start hack/docker-compose.test.yml (CI always does)")


async def _admin(url: str, fn):
    import nats

    nc = await nats.connect(url)
    try:
        return await fn(nc.jetstream())
    finally:
        await nc.close()


def admin(url: str, fn):
    return asyncio.run(_admin(url, fn))


def fresh_stream(url: str, max_age_seconds: float = 7 * 24 * 3600) -> None:
    """BOOTH_EVENTS as core creates it (ADR 0026), empty, with no consumers."""
    from nats.js.api import StreamConfig
    from nats.js.errors import NotFoundError

    async def go(js):
        try:
            await js.delete_stream(STREAM)
        except NotFoundError:
            pass
        await js.add_stream(StreamConfig(name=STREAM, subjects=["booth.>"], max_age=max_age_seconds))

    admin(url, go)


def publish(url: str, paths: list[str], etype: str = "storage.object.created") -> None:
    async def go(js):
        for p in paths:
            data = {"backendId": "local", "path": p, "size": 1, "principal": "u-owner", "via": "workspace-user"}
            body = {"workspace": WS, "eventType": etype, "publishedAt": datetime.now(UTC).isoformat(), "publishedBy": "storage", "data": data}
            await js.publish(f"booth.{WS}.{etype}", json.dumps(body).encode())
        # Something the consumer must not see at all.
        await js.publish(f"booth.{WS}.dashboard.created", b"{}")

    admin(url, go)


def delete_consumer(url: str) -> None:
    admin(url, lambda js: js.delete_consumer(STREAM, DURABLE))


@pytest.fixture
def url():
    u = nats_url()
    fresh_stream(u)
    return u


def watched_store() -> tuple[MemoryStore, str]:
    store = MemoryStore()
    p = store.create_pipeline(WS, "silver", "", "olga")
    p.event_trigger = EventTrigger.model_validate({"match": [{"storage": {"backendId": "local", "prefix": "inbox/"}}], "debounceSeconds": 1})
    p.event_trigger_since, p.owner_sub = datetime.now(UTC) - timedelta(hours=1), "u-owner"
    store.update_schedule(p)
    return store, p.id


class Flaky(TriggerIngest):
    """Fails the first write of each of the given stream sequences, as a database blip would."""

    def __init__(self, store, fail: set[int]) -> None:
        super().__init__(store)
        self.fail = set(fail)

    def handle(self, subject, body, seq, now=None):
        if seq in self.fail:
            self.fail.discard(seq)
            raise RuntimeError("database away")
        return super().handle(subject, body, seq, now)


def started(url: str, store, ingest=None) -> EventBus:
    bus = EventBus(Config(dev_memory=True, event_bus_url_override=url, events_ack_wait_seconds=2), store, ingest or TriggerIngest(store))
    bus.start()
    assert bus.ready.wait(20), bus.last_error
    return bus


def counted(store, pid: str, n: int, timeout: float = 20) -> int:
    deadline = time.monotonic() + timeout
    count = 0
    while time.monotonic() < deadline:
        st = store.get_event_trigger_state(WS, pid)
        count = st.event_count if st else 0
        if count >= n:
            time.sleep(0.5)  # anything extra would show up now
            st = store.get_event_trigger_state(WS, pid)
            return st.event_count if st else 0
        time.sleep(0.1)
    return count


def fires(store) -> list[dict]:
    return [fired for _, fired in store.claim_due_event_triggers(datetime.now(UTC) + timedelta(hours=1), 100)]


def test_new_events_only_on_first_creation_and_74_make_one_fire(url):
    store, pid = watched_store()
    publish(url, ["inbox/old-1.csv", "inbox/old-2.csv"])  # before this module ever read the bus
    bus = started(url, store)
    try:
        publish(url, [f"inbox/{i:02}.csv" for i in range(74)])
        assert counted(store, pid, 74) == 74
    finally:
        bus.stop()
    (fired,) = fires(store)
    assert fired["count"] == 74 and fired["events"][0]["path"] == "inbox/00.csv"
    assert store.trigger_bus() is not None


def test_a_restart_continues_where_the_consumer_left_off(url):
    store, pid = watched_store()
    bus = started(url, store)
    publish(url, [f"inbox/a{i}.csv" for i in range(10)])
    assert counted(store, pid, 10) == 10
    bus.stop()
    publish(url, [f"inbox/b{i}.csv" for i in range(5)])  # while no replica is reading
    bus = started(url, store)
    try:
        assert counted(store, pid, 15) == 15
    finally:
        bus.stop()
    assert len(fires(store)) == 1


def test_a_lost_consumer_is_recreated_from_the_last_contact(url):
    store, pid = watched_store()
    bus = started(url, store)
    publish(url, ["inbox/1.csv", "inbox/2.csv"])
    assert counted(store, pid, 2) == 2
    bus.stop()
    delete_consumer(url)
    publish(url, ["inbox/3.csv", "inbox/4.csv", "inbox/5.csv"])
    bus = started(url, store)
    try:
        # Replayed from a little before the last contact: 1 and 2 come again and are recognised.
        assert counted(store, pid, 5) == 5
    finally:
        bus.stop()


def test_events_lost_to_a_long_outage_fire_each_trigger_once(url):
    fresh_stream(url, max_age_seconds=24 * 3600)
    store, pid = watched_store()
    long_ago = datetime.now(UTC) - timedelta(days=2)
    store._bus = (long_ago, long_ago)  # the last time any replica read: longer ago than the stream keeps events
    bus = started(url, store)
    bus.stop()
    (fired,) = fires(store)
    assert fired == {"events": [], "count": 0, "missedEvents": True}
    assert store.get_event_trigger_state(WS, pid).gap_detected_at is not None


def test_two_replicas_share_the_consumer_and_each_event_counts_once(url):
    store, pid = watched_store()
    one, two = started(url, store), started(url, store)
    try:
        publish(url, [f"inbox/{i}.csv" for i in range(74)])
        assert counted(store, pid, 74) == 74
    finally:
        one.stop()
        two.stop()
    assert len(fires(store)) == 1


def test_an_event_whose_write_failed_is_redelivered_and_counted_once(url):
    store, pid = watched_store()
    flaky = Flaky(store, fail={1, 2})
    bus = started(url, store, flaky)
    try:
        publish(url, ["inbox/1.csv", "inbox/2.csv", "inbox/3.csv"])
        assert counted(store, pid, 3, timeout=30) == 3
    finally:
        bus.stop()
    assert flaky.fail == set()


def test_the_app_wires_its_consumer_to_its_scheduler_end_to_end(url):
    """What the deployed pod does: the app's own consumer records the burst, wakes its own scheduler,
    and the run's task sees ctx.trigger. Only the bus URL is configured; nothing is called by hand."""
    from .harness import make_env, task

    env = make_env(start_scheduler=True, event_bus_url_override=url, scheduler_interval_seconds=30)
    with env.client:
        assert env.app.state.event_bus.ready.wait(20), env.app.state.event_bus.last_error
        source = "def run(ctx):\n    print('trigger', ctx.trigger.type, ctx.trigger.count)\n"
        r = env.call("POST", "/pipelines", json={"name": "silver", "spec": {"tasks": [task("load", source=source)]}})
        pid = r.json()["id"]
        r = env.call("PUT", f"/pipelines/{pid}/schedule", json={"eventTrigger": {"match": [{"storage": {"backendId": "local", "prefix": "inbox/"}}], "debounceSeconds": 1}})
        assert r.status_code == 200, r.text
        assert env.call("GET", "/config").json()["eventTriggers"] == {"available": True}
        publish(url, [f"inbox/{i:02}.csv" for i in range(74)])
        deadline, runs = time.monotonic() + 30, []
        while time.monotonic() < deadline and not runs:
            runs = env.call("GET", f"/runs?pipelineId={pid}").json()["items"]
            time.sleep(0.2)
        assert len(runs) == 1, runs
        run = env.wait_run(runs[0]["id"])
        assert run["status"] == "succeeded" and run["trigger"] == "event" and run["triggerEvents"]["count"] == 74
        assert "trigger event 74" in [ln["message"] for ln in env.call("GET", f"/runs/{run['id']}/logs").json()["items"]]
        status = env.call("GET", f"/pipelines/{pid}/event-trigger").json()
        assert status["bus"] == {"configured": True, "connected": True} and status["lastFiredAt"]
        time.sleep(3)
        assert len(env.call("GET", f"/runs?pipelineId={pid}").json()["items"]) == 1

