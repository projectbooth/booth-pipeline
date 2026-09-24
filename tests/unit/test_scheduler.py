"""The scheduler and dead-worker recovery, driven deterministically through ``tick(now)``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from booth_pipeline.model import MIN_INTERVAL_SECONDS

from .harness import Env, etl_spec, make_env, task

T0 = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


@pytest.fixture()
def env():
    e = make_env()
    with e.client:
        yield e


def scheduled_pipeline(env: Env, cron="0 9 * * *", spec=None, **kw):
    p = env.call("POST", "/pipelines", json={"name": "etl", "spec": spec or etl_spec()}).json()
    sched = env.call("PUT", f"/pipelines/{p['id']}/schedule", json={"schedule": {"cron": cron}, **kw}).json()
    return p, sched


def make_due(env: Env, pipeline_id: str, when: datetime) -> None:
    p = env.store.get_pipeline("acme", pipeline_id)
    p.next_run_at = when
    env.store.update_schedule(p)


def test_a_due_pipeline_runs_once_and_is_rescheduled(env):
    p, _ = scheduled_pipeline(env)
    make_due(env, p["id"], T0)
    sched = env.app.state.scheduler
    started = sched.tick(T0 + timedelta(seconds=30))
    assert len(started) == 1
    run = env.wait_run(started[0])
    assert run["status"] == "succeeded" and run["trigger"] == "schedule" and run["triggeredBy"] == "schedule"
    assert sched.tick(T0 + timedelta(seconds=45)) == []  # already claimed for this fire
    assert env.call("GET", f"/pipelines/{p['id']}").json()["nextRunAt"] == "2026-03-02T09:00:00+00:00"


def test_downtime_coalesces_missed_fires_into_one_run(env):
    p, _ = scheduled_pipeline(env, cron="*/10 * * * *")
    make_due(env, p["id"], T0)  # was due at 09:00; the service was down until 12:00
    started = env.app.state.scheduler.tick(T0 + timedelta(hours=3))
    assert len(started) == 1  # not eighteen
    env.wait_run(started[0])
    assert env.call("GET", f"/pipelines/{p['id']}").json()["nextRunAt"] == "2026-03-01T12:10:00+00:00"


def test_disabled_and_unscheduled_pipelines_never_fire(env):
    p, _ = scheduled_pipeline(env, cron="* * * * *")
    env.call("PUT", f"/pipelines/{p['id']}/schedule", json={"schedule": {"cron": "* * * * *", "enabled": False}})
    p2 = env.call("POST", "/pipelines", json={"name": "manual", "spec": etl_spec()}).json()
    assert env.app.state.scheduler.tick(T0 + timedelta(days=1)) == []
    assert p2["id"]  # created with no schedule at all: never fires either


def test_a_scheduled_fire_is_skipped_while_the_previous_run_is_still_active(env):
    slow = {"tasks": [task("a", source="import time\ntime.sleep(3)\n")]}
    p, _ = scheduled_pipeline(env, cron="* * * * *", spec=slow)
    sched = env.app.state.scheduler
    make_due(env, p["id"], T0)
    (first,) = sched.tick(T0)
    make_due(env, p["id"], T0 + timedelta(minutes=1))
    assert sched.tick(T0 + timedelta(minutes=1)) == []  # skipped, not queued up behind it
    assert env.call("GET", f"/runs?pipelineId={p['id']}").json()["total"] == 1
    env.wait_run(first)


def test_two_schedulers_polling_at_once_start_a_due_fire_exactly_once(env):
    """Multi-replica safety: the claim is atomic, so N pollers produce one run."""
    import threading

    p, _ = scheduled_pipeline(env)
    make_due(env, p["id"], T0)
    sched = env.app.state.scheduler
    results: list[list[str]] = []
    barrier = threading.Barrier(6)

    def go():
        barrier.wait()
        results.append(sched.tick(T0 + timedelta(seconds=5)))

    ts = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    started = [rid for r in results for rid in r]
    assert len(started) == 1
    env.wait_run(started[0])


def test_an_interval_pipeline_fires_via_tick_and_reschedules_by_simple_addition(env):
    p = env.call("POST", "/pipelines", json={"name": "etl", "spec": etl_spec()}).json()
    sched = env.call("PUT", f"/pipelines/{p['id']}/schedule", json={"schedule": {"type": "interval", "seconds": 30}}).json()
    assert sched["schedule"] == {"type": "interval", "seconds": 30, "enabled": True}
    make_due(env, p["id"], T0)
    # no calendar alignment: an interval reschedules from the moment it actually fired, not from
    # whenever it was due — tick with "now" exactly at T0 so the math is exact and predictable.
    (rid,) = env.app.state.scheduler.tick(T0)
    env.wait_run(rid)
    assert env.call("GET", f"/pipelines/{p['id']}").json()["nextRunAt"] == "2026-03-01T09:00:30+00:00"


def test_an_interval_below_the_minimum_is_rejected(env):
    p = env.call("POST", "/pipelines", json={"name": "etl", "spec": etl_spec()}).json()
    r = env.call("PUT", f"/pipelines/{p['id']}/schedule", json={"schedule": {"type": "interval", "seconds": MIN_INTERVAL_SECONDS - 1}})
    assert r.status_code == 422 and r.json()["field"] == "schedule.interval.seconds"


def test_a_scheduled_run_always_uses_the_latest_saved_version(env):
    p, _ = scheduled_pipeline(env)
    make_due(env, p["id"], T0)
    (rid,) = env.app.state.scheduler.tick(T0)
    env.call("POST", f"/pipelines/{p['id']}/versions", json={"spec": {"tasks": [task("a")]}})
    assert env.wait_run(rid)["pipelineVersion"] == 1  # a later save never rewrites a run in flight


# ---- dead workers ---------------------------------------------------------------------------


def stuck_run(env: Env, status: str, heartbeat: datetime | None, created: datetime):
    from booth_pipeline.records import Run

    p, _ = scheduled_pipeline(env)
    run = Run("r-stuck", "acme", p["id"], 1, status, "manual", "x", created, heartbeat_at=heartbeat, worker_id="dead-pod")
    env.store.create_run(run, ["extract", "clean", "load"])
    if status == "running":
        tr = env.store.list_task_runs("r-stuck")[0]
        tr.status = "running"
        env.store.update_task_run(tr)
    return p


def test_a_run_whose_worker_died_is_failed_by_the_sweep_and_frees_the_pipeline(env):
    now = datetime.now(UTC)
    p = stuck_run(env, "running", now - timedelta(minutes=10), now - timedelta(minutes=11))
    assert env.call("POST", f"/pipelines/{p['id']}/run").status_code == 409  # wedged: an "active" run that is not
    assert env.app.state.scheduler._manager.sweep() == 1
    run = env.call("GET", "/runs/r-stuck").json()
    assert run["status"] == "failed" and "worker lost" in run["error"]
    assert {t["taskKey"]: t["status"] for t in run["tasks"]} == {"extract": "failed", "clean": "skipped", "load": "skipped"}
    assert any("worker lost" in ln["message"] for ln in env.call("GET", "/runs/r-stuck/logs").json()["items"])
    assert env.call("POST", f"/pipelines/{p['id']}/run").status_code == 202  # unblocked
    assert env.app.state.scheduler._manager.sweep() == 0  # idempotent


def test_a_healthy_or_recently_queued_run_is_left_alone(env):
    now = datetime.now(UTC)
    stuck_run(env, "running", now - timedelta(seconds=5), now - timedelta(seconds=6))
    assert env.app.state.scheduler._manager.sweep() == 0
    assert env.call("GET", "/runs/r-stuck").json()["status"] == "running"


def test_a_run_that_never_started_is_eventually_failed(env):
    now = datetime.now(UTC)
    stuck_run(env, "queued", None, now - timedelta(hours=2))
    assert env.app.state.scheduler._manager.sweep() == 1
    assert "never started" in env.call("GET", "/runs/r-stuck").json()["error"]


# ---- the loop itself: index-driven, not a fixed-cadence poll (ADR 0065) ----------------------


def test_the_loop_does_not_claim_when_nothing_is_due():
    """The whole point of the redesign: no due pipelines means no locking write query, even
    across several refresh cycles — only the cheap read (list_upcoming) runs on the index cadence."""
    import time

    e = make_env()
    with e.client:
        sched = e.app.state.scheduler
        sched._index_refresh_seconds = 0.2
        claims = []
        original = e.store.claim_due_pipelines
        e.store.claim_due_pipelines = lambda *a, **k: (claims.append(1) or original(*a, **k))
        sched.start()
        time.sleep(0.9)  # several refresh cycles at 0.2s each
        sched.stop()
        assert claims == []


def test_a_due_pipeline_fires_promptly_even_with_a_long_refresh_cadence():
    """The sleep is driven by the index's earliest known fire time, not capped to the refresh
    cadence — a pipeline due in under a second fires in under a second even if the index only
    refreshes every 30s."""
    import time

    e = make_env()
    with e.client:
        p = e.call("POST", "/pipelines", json={"name": "etl", "spec": etl_spec()}).json()
        e.call("PUT", f"/pipelines/{p['id']}/schedule", json={"schedule": {"type": "interval", "seconds": MIN_INTERVAL_SECONDS}})
        make_due(e, p["id"], datetime.now(UTC))
        sched = e.app.state.scheduler
        sched._index_refresh_seconds = 30  # deliberately long
        t0 = time.monotonic()
        sched.start()
        try:
            for _ in range(100):
                if e.call("GET", f"/runs?pipelineId={p['id']}").json()["total"] >= 1:
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("the due pipeline never fired")
            assert time.monotonic() - t0 < 5  # nowhere near the 30s refresh cadence
        finally:
            sched.stop()


def test_a_short_interval_is_not_throttled_to_the_refresh_cadence():
    """After a claim, the index re-checks immediately — so a pipeline firing every few seconds
    keeps firing at its own pace even when the refresh cadence is much longer."""
    import time

    e = make_env()
    with e.client:
        p = e.call("POST", "/pipelines", json={"name": "etl", "spec": etl_spec()}).json()
        e.call("PUT", f"/pipelines/{p['id']}/schedule", json={"schedule": {"type": "interval", "seconds": MIN_INTERVAL_SECONDS}, "allowConcurrentRuns": True})
        make_due(e, p["id"], datetime.now(UTC))
        sched = e.app.state.scheduler
        sched._index_refresh_seconds = 60
        sched.start()
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if e.call("GET", f"/runs?pipelineId={p['id']}").json()["total"] >= 2:
                    break
                time.sleep(0.1)
            else:
                raise AssertionError("a second fire never happened — the loop is stuck waiting out the refresh cadence")
        finally:
            sched.stop()


def test_the_index_notices_a_pipeline_scheduled_after_the_loop_started():
    """A replica whose index was last refreshed before a pipeline was scheduled still picks it up
    — within one refresh cycle, via the coarser index read, not the claim query."""
    import time

    e = make_env()
    with e.client:
        sched = e.app.state.scheduler
        sched._index_refresh_seconds = 0.3
        sched.start()
        try:
            time.sleep(0.1)  # the loop is running with an empty index
            p = e.call("POST", "/pipelines", json={"name": "etl", "spec": etl_spec()}).json()
            e.call("PUT", f"/pipelines/{p['id']}/schedule", json={"schedule": {"type": "interval", "seconds": MIN_INTERVAL_SECONDS}})
            make_due(e, p["id"], datetime.now(UTC))
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if e.call("GET", f"/runs?pipelineId={p['id']}").json()["total"] >= 1:
                    break
                time.sleep(0.1)
            else:
                raise AssertionError("a pipeline scheduled after the loop started was never picked up")
        finally:
            sched.stop()
