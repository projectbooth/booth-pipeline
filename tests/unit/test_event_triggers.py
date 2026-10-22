"""ADR 0123: event triggers. The model, reading events, matching, and the burst rules, end to end
through both stores (``store`` runs every test against memory and PostgreSQL)."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from booth_pipeline import triggers
from booth_pipeline.event_bus import TriggerIngest
from booth_pipeline.model import EventTrigger
from booth_pipeline.records import RUN_SUCCEEDED, Pipeline, Run
from booth_pipeline.store.base import Busy
from booth_pipeline.triggers import BadEvent, TriggerEvent, matches, own_run_id, parse_event

WS = "acme"
T0 = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
INBOX = {"storage": {"backendId": "local", "prefix": "finance/dev/inbox/"}}


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def trigger(**kw) -> EventTrigger:
    return EventTrigger.model_validate({"match": [INBOX], **kw})


def file_event(path: str = "finance/dev/inbox/a.csv", principal: str = "streamlit:acme:finance-dev", ws: str = WS, published: str = "2026-10-21T12:00:00Z") -> TriggerEvent:
    return TriggerEvent(ws, triggers.STORAGE_CREATED, published, principal, backend_id="local", path=path, size=10, via="app-upload")


def envelope(etype: str, data: dict, ws: str = WS) -> bytes:
    return json.dumps({"workspace": ws, "eventType": etype, "publishedAt": "2026-10-21T12:00:00Z", "publishedBy": "storage", "data": data}).encode()


# ---- the model ----------------------------------------------------------------------------------


def test_the_model_cleans_folders_and_refuses_what_storage_would():
    t = EventTrigger.model_validate({"match": [{"storage": {"backendId": "local", "prefix": "finance/dev/inbox"}}, {"api": {"slugPrefix": "finance-dev-"}}]})
    assert t.match[0].storage.prefix == "finance/dev/inbox/" and t.match[1].api.slug_prefix == "finance-dev-"
    assert (t.debounce_seconds, t.max_wait_seconds, t.when_running, t.min_gap_seconds) == (10, 60, "queue_one", 5)
    assert EventTrigger.model_validate({"match": [{"storage": {"backendId": "local"}}]}).match[0].storage.prefix == ""
    for bad in (
        {"match": []},
        {"match": [{"storage": {"backendId": "local", "prefix": "/finance"}}]},
        {"match": [{"storage": {"backendId": "local", "prefix": "a/../b"}}]},
        {"match": [{"storage": {"backendId": "local"}, "api": {"slugs": ["x"]}}]},
        {"match": [{"api": {}}]},
        {"match": [{"api": {"slugs": ["x"], "slugPrefix": "y"}}]},
        {"match": [{"api": {"slugs": ["Not_A_Slug"]}}]},
        {"match": [INBOX], "debounceSeconds": 100, "maxWaitSeconds": 50},
        {"match": [INBOX], "minGapSeconds": 1},
        {"match": [INBOX], "whenRunning": "always"},
        {"match": [INBOX] * 11},
    ):
        with pytest.raises(ValidationError):
            EventTrigger.model_validate(bad)


def test_reading_events_off_the_bus():
    ev = parse_event("booth.acme.storage.object.created", envelope("storage.object.created", {"backendId": "local", "path": "a/b.csv", "size": 3, "principal": "u-1", "via": "workspace-user"}))
    assert (ev.workspace, ev.backend_id, ev.path, ev.size, ev.principal, ev.via) == ("acme", "local", "a/b.csv", 3, "u-1", "workspace-user")
    ev = parse_event("booth.acme.api.write.committed", envelope("api.write.committed", {"apiSlug": "tx", "dataset": "d1", "table": "t", "operation": "insert", "rows": 1, "principal": "streamlit:acme:app"}))
    assert ev.record() == {"type": "api.write.committed", "publishedAt": "2026-10-21T12:00:00Z", "principal": "streamlit:acme:app",
                           "apiSlug": "tx", "dataset": "d1", "table": "t", "operation": "insert", "rows": 1}  # fmt: skip
    for subject, body in (
        ("booth.other.storage.object.created", envelope("storage.object.created", {"backendId": "l", "path": "p", "principal": "u"})),  # workspace disagrees
        ("booth.acme.api.write.committed", envelope("storage.object.created", {"backendId": "l", "path": "p", "principal": "u"})),  # type disagrees
        ("booth.acme.dashboard.created", envelope("dashboard.created", {})),
        ("booth.acme.storage.object.created", b"not json"),
        ("booth.acme.storage.object.created", envelope("storage.object.created", {"path": "p", "principal": "u"})),  # no backendId
        ("booth.acme.storage.object.created", envelope("storage.object.created", {"backendId": "l", "path": "p", "principal": "u", "size": "big"})),
    ):
        with pytest.raises(BadEvent):
            parse_event(subject, body)


def test_matching():
    t = EventTrigger.model_validate({"match": [INBOX, {"api": {"slugs": ["finance-dev-tx"]}}, {"api": {"slugPrefix": "budget-"}}]})
    assert matches(t, file_event("finance/dev/inbox/2026-10.csv"))
    assert matches(t, file_event("finance/dev/inbox/sub/x.csv"))
    assert not matches(t, file_event("finance/dev/inbox2/x.csv"))  # a folder boundary, not a string prefix
    assert not matches(t, file_event("finance/dev/x.csv"))
    assert not matches(t, TriggerEvent(WS, triggers.STORAGE_CREATED, "", "u", backend_id="lake", path="finance/dev/inbox/a.csv"))
    api = lambda slug: TriggerEvent(WS, triggers.API_COMMITTED, "", "u", api_slug=slug)  # noqa: E731
    assert matches(t, api("finance-dev-tx")) and matches(t, api("budget-2026"))
    assert not matches(t, api("finance-dev-cat")) and not matches(t, api("budge"))
    whole = EventTrigger.model_validate({"match": [{"storage": {"backendId": "local"}}]})
    assert matches(whole, file_event("anything/at/all.txt"))
    assert own_run_id("pipeline:acme:r-1", "acme") == "r-1"
    assert own_run_id("pipeline:other:r-1", "acme") is None and own_run_id("u-owner", "acme") is None


# ---- through the store --------------------------------------------------------------------------


def pipeline(store, name: str = "silver", ws: str = WS, concurrent: bool = False, owner: str = "u-owner", **kw) -> Pipeline:
    p = store.create_pipeline(ws, name, "", "olga")
    p.event_trigger, p.event_trigger_since = trigger(**kw), T0 - timedelta(hours=1)
    p.allow_concurrent_runs, p.owner_sub = concurrent, owner
    return store.update_schedule(p)


class Bus:
    """Delivers events the way the consumer does, with stream sequences, and fires what is due."""

    def __init__(self, store) -> None:
        self.store = store
        self.ingest = TriggerIngest(store)
        self.seq = 0

    def send(self, ev: TriggerEvent, now: datetime, seq: int | None = None) -> dict | None:
        if seq is None:
            self.seq += 1
            seq = self.seq
        return self.store.ingest_trigger_event(ev, seq, now)

    def fire(self, now: datetime) -> list[Run]:
        runs = []
        for p, fired in self.store.claim_due_event_triggers(now, 100):
            run = Run(str(uuid4()), p.workspace, p.id, 1, "queued", "event", "event", now, owner_sub=p.owner_sub, trigger_events=fired)
            try:
                runs.append(self.store.create_run(run, ["a"], exclusive=not p.allow_concurrent_runs))
            except Busy:
                self.store.requeue_event_trigger(p.id, fired, now)
        return runs


def active_run(store, p: Pipeline, now: datetime = T0) -> Run:
    r = store.create_run(Run(str(uuid4()), p.workspace, p.id, 1, "queued", "manual", "olga", now), ["a"])
    store.mark_running(r.id, "w", now)
    return r


def test_a_burst_of_74_files_makes_one_run(store):
    p, bus = pipeline(store), Bus(store)
    for i in range(74):
        assert bus.send(file_event(f"finance/dev/inbox/{i:02}.csv"), at(i * 0.1)) == {p.id: "pending"}
    assert bus.fire(at(7.3 + 9)) == []  # 10 s of quiet hasn't passed yet
    (run,) = bus.fire(at(7.3 + 10))
    assert run.trigger == "event" and run.trigger_events["count"] == 74 and len(run.trigger_events["events"]) == 74
    assert run.trigger_events["events"][0]["path"] == "finance/dev/inbox/00.csv"
    assert store.get_run(WS, run.id).trigger_events["count"] == 74
    assert bus.fire(at(3600)) == []  # and nothing more


def test_a_run_is_told_the_first_100_events_and_the_exact_count(store):
    p, bus = pipeline(store, debounceSeconds=60, maxWaitSeconds=600), Bus(store)
    for i in range(150):
        bus.send(file_event(f"finance/dev/inbox/{i}.csv"), at(i * 0.01))
    (run,) = bus.fire(at(100))
    assert run.trigger_events["count"] == 150 and len(run.trigger_events["events"]) == triggers.MAX_RUN_EVENTS
    assert p.id == run.pipeline_id


def test_a_long_burst_fires_at_the_max_wait_and_then_keeps_its_minimum_gap(store):
    pipeline(store, debounceSeconds=10, maxWaitSeconds=60, minGapSeconds=90)
    bus = Bus(store)
    fired_at = []
    for s in range(0, 200, 5):  # an event every 5 s for 200 s: never 10 s of quiet
        bus.send(file_event(), at(s))
        if bus.fire(at(s)):
            fired_at.append(s)
        for r in store.list_runs(WS, None, None, 100, 0).items:
            store.finish_run(r.id, RUN_SUCCEEDED, None, at(s))
    assert fired_at == [60, 150]  # the cap, then the cap again but no sooner than 90 s after the last fire


def test_an_event_during_a_run_makes_exactly_one_follow_up_under_queue_one(store):
    p, bus = pipeline(store), Bus(store)
    running = active_run(store, p)
    for i in range(30):
        assert bus.send(file_event(f"finance/dev/inbox/{i}.csv"), at(i)) == {p.id: "queued"}
    assert bus.fire(at(100)) == []
    st = store.get_event_trigger_state(WS, p.id)
    assert st.queued and st.fire_at is None and st.event_count == 30
    store.finish_run(running.id, RUN_SUCCEEDED, None, at(101))  # releases the follow-up, in the same step
    (follow_up,) = bus.fire(at(101))
    assert follow_up.trigger_events["count"] == 30
    store.finish_run(follow_up.id, RUN_SUCCEEDED, None, at(110))
    assert bus.fire(at(500)) == []  # exactly one


def test_an_event_during_a_run_makes_none_under_skip(store):
    p, bus = pipeline(store, whenRunning="skip"), Bus(store)
    running = active_run(store, p)
    for i in range(5):
        assert bus.send(file_event(), at(i)) == {p.id: "skipped"}
    store.finish_run(running.id, RUN_SUCCEEDED, None, at(10))
    assert bus.fire(at(500)) == []
    assert store.get_event_trigger_state(WS, p.id).skipped_while_running == 5


def test_a_fire_that_comes_due_while_a_run_is_active(store):
    # The burst began before the run did (a cron run, or Run now, started in between).
    p, bus = pipeline(store), Bus(store)
    bus.send(file_event(), at(0))
    running = active_run(store, p, at(5))
    assert bus.fire(at(10)) == []  # due, but the pipeline is busy: queued
    store.finish_run(running.id, RUN_SUCCEEDED, None, at(20))
    (run,) = bus.fire(at(20))
    assert run.trigger_events["count"] == 1
    # Under skip the same burst is dropped.
    q = pipeline(store, "gold", whenRunning="skip")
    bus.send(file_event(), at(30))
    running = active_run(store, q, at(31))
    assert bus.fire(at(40)) == []
    store.finish_run(running.id, RUN_SUCCEEDED, None, at(50))
    assert [r.pipeline_id for r in bus.fire(at(500))] == []


def test_allow_concurrent_runs_ignores_when_running(store):
    p, bus = pipeline(store, concurrent=True), Bus(store)
    active_run(store, p)
    assert bus.send(file_event(), at(0)) == {p.id: "pending"}
    assert len(bus.fire(at(10))) == 1


def test_a_duplicate_delivery_adds_no_run(store):
    p, bus = pipeline(store), Bus(store)
    assert bus.send(file_event(), at(0), seq=41) == {p.id: "pending"}
    assert bus.send(file_event(), at(1), seq=41) is None  # redelivered before the fire
    (run,) = bus.fire(at(11))
    assert run.trigger_events["count"] == 1
    assert bus.send(file_event(), at(30), seq=41) is None  # and after it
    assert bus.fire(at(500)) == []


def test_the_loop_guard_ignores_this_pipelines_own_runs_only(store):
    p, other, bus = pipeline(store), pipeline(store, "other"), Bus(store)
    own = active_run(store, p)
    store.finish_run(own.id, RUN_SUCCEEDED, None, at(0))
    theirs = active_run(store, other)
    store.finish_run(theirs.id, RUN_SUCCEEDED, None, at(0))
    assert bus.send(file_event(principal=f"pipeline:{WS}:{own.id}"), at(1)) == {p.id: "own-run", other.id: "pending"}
    assert bus.send(file_event(principal=f"pipeline:{WS}:{theirs.id}"), at(2)) == {p.id: "pending", other.id: "own-run"}
    # The owner's own upload through the Storage page is exactly what should fire.
    assert bus.send(file_event(principal="u-owner"), at(3)) == {p.id: "pending", other.id: "pending"}
    assert store.get_event_trigger_state(WS, p.id).ignored_own_runs == 1


def test_events_stay_in_their_workspace_and_disabled_triggers_do_nothing(store):
    p, bus = pipeline(store), Bus(store)
    pipeline(store, ws="globex")
    assert bus.send(file_event(ws="other"), at(0)) == {}
    off = store.get_pipeline(WS, p.id)
    off.event_trigger = trigger(enabled=False)
    store.update_schedule(off)
    assert bus.send(file_event(), at(1)) == {}


def test_events_published_before_the_trigger_was_turned_on_are_not_its_business(store):
    p, bus = pipeline(store), Bus(store)  # turned on an hour before T0
    assert bus.send(file_event(published="2026-10-21T10:00:00Z"), at(0)) == {}
    assert bus.send(file_event(published="2026-10-21T11:30:00+00:00"), at(1)) == {p.id: "pending"}
    assert bus.send(file_event(published="not a time"), at(2)) == {p.id: "pending"}  # unreadable: let it through


def test_turning_a_trigger_off_drops_what_it_was_waiting_for(store):
    p, bus = pipeline(store), Bus(store)
    bus.send(file_event(), at(0))
    off = store.get_pipeline(WS, p.id)
    off.event_trigger = None
    store.update_schedule(off)
    assert store.get_pipeline(WS, p.id).event_trigger is None
    assert bus.fire(at(100)) == []


def test_a_fire_that_finds_the_pipeline_busy_after_its_claim_is_owed(store):
    p, bus = pipeline(store), Bus(store)
    bus.send(file_event(), at(0))
    ((claimed, fired),) = store.claim_due_event_triggers(at(10), 10)
    running = active_run(store, p, at(10))  # a run slipped in between the claim and the create
    store.requeue_event_trigger(p.id, fired, at(10))
    assert store.get_event_trigger_state(WS, p.id).queued
    store.finish_run(running.id, RUN_SUCCEEDED, None, at(20))
    (run,) = bus.fire(at(20))
    assert run.trigger_events["count"] == 1 and claimed.id == p.id


def test_an_outage_backlog_fires_once(store):
    # The consumer comes back and replays a day of events in a few seconds: one run per trigger.
    p, bus = pipeline(store), Bus(store)
    for i in range(500):
        bus.send(file_event(f"finance/dev/inbox/{i}.csv"), at(i * 0.005))
    runs = bus.fire(at(20))
    assert len(runs) == 1 and runs[0].trigger_events["count"] == 500
    assert p.id == runs[0].pipeline_id and bus.fire(at(600)) == []


def test_lost_events_fire_every_enabled_trigger_once(store):
    p, q = pipeline(store), pipeline(store, "gold")
    pipeline(store, "off", enabled=False)
    assert store.event_triggers_missed_events(at(0)) == 2
    runs = Bus(store).fire(at(0))
    assert sorted(r.pipeline_id for r in runs) == sorted([p.id, q.id])
    assert all(r.trigger_events == {"events": [], "count": 0, "missedEvents": True} for r in runs)
    assert store.get_event_trigger_state(WS, p.id).gap_detected_at == at(0)


def test_two_replicas_claiming_at_once_start_one_run(store):
    pipeline(store)
    Bus(store).send(file_event(), at(0))
    got: list = []
    barrier = threading.Barrier(4)

    def claim():
        barrier.wait()
        got.extend(store.claim_due_event_triggers(at(10), 10))

    threads = [threading.Thread(target=claim) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(got) == 1


def test_the_bus_record_and_pruning(store):
    assert store.trigger_bus() is None
    store.record_trigger_bus_contact(at(0))
    store.record_trigger_bus_contact(at(30))
    store.record_trigger_bus_contact(at(10))  # a slower replica never moves it back
    assert store.trigger_bus() == (at(0), at(30))
    pipeline(store)
    bus = Bus(store)
    bus.send(file_event(), at(0), seq=1)
    bus.send(file_event(), at(100), seq=2)
    assert store.prune_seen_trigger_events(at(50)) == 1


def test_a_restart_mid_burst_neither_loses_nor_doubles_the_fire(pg_store):
    """State is in PostgreSQL: a second store (another process, or this one restarted) sees the burst."""
    from booth_pipeline.store.postgres import PostgresStore

    from .conftest import postgres_dsn

    with pg_store._pool.connection() as conn:
        conn.execute("TRUNCATE run_logs, task_runs, runs, event_trigger_state, trigger_events_seen, trigger_bus, pipeline_versions, pipelines, task_versions, tasks")
    p, bus = pipeline(pg_store), Bus(pg_store)
    for i in range(10):
        bus.send(file_event(), at(i))
    restarted = PostgresStore(postgres_dsn())
    try:
        again = Bus(restarted)
        assert again.send(file_event(), at(9.5), seq=10) is None  # the redelivery of an event it had recorded
        (run,) = again.fire(at(20))
        assert run.trigger_events["count"] == 10 and run.pipeline_id == p.id
        assert bus.fire(at(30)) == [] and again.fire(at(30)) == []
    finally:
        restarted.close()
