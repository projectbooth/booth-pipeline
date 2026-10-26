"""Run retention (execution.retention.days): the store's batched prune, against both stores, and the nightly
pruner around it."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from booth_pipeline.config import Config, ConfigError
from booth_pipeline.records import (
    RUN_CANCELED,
    RUN_FAILED,
    RUN_QUEUED,
    RUN_RUNNING,
    RUN_SKIPPED,
    RUN_SUCCEEDED,
    LogLine,
    Run,
)
from booth_pipeline.retention import RunPruner, next_prune_at

WS = "acme"
NOW = datetime(2026, 10, 28, 8, 0, tzinfo=UTC)


def add_run(store, pipeline, status: str, finished_days_ago: float | None, logs: int = 3) -> str:
    created = NOW - timedelta(days=(finished_days_ago or 0) + 1)
    run = Run(str(uuid4()), WS, pipeline.id, 1, RUN_QUEUED, "schedule", "schedule", created)
    store.create_run(run, ["capture", "load"])
    if status != RUN_QUEUED:
        store.mark_running(run.id, "w", created)
    if finished_days_ago is not None:
        store.finish_run(run.id, status, None, NOW - timedelta(days=finished_days_ago))
    store.append_logs([LogLine(run.id, i + 1, created, "capture", 1, "stdout", f"line {i}") for i in range(logs)])
    return run.id


def test_only_finished_runs_older_than_the_cutoff_go_with_their_task_runs_and_logs(store):
    p = store.create_pipeline(WS, "capture", "", "a")
    old = {s: add_run(store, p, s, 40) for s in (RUN_SUCCEEDED, RUN_FAILED, RUN_CANCELED, RUN_SKIPPED)}
    recent = add_run(store, p, RUN_SUCCEEDED, 10)
    queued = add_run(store, p, RUN_QUEUED, None)  # created 41 days ago, never started: never pruned
    running = add_run(store, p, RUN_RUNNING, None)
    assert store.prune_runs(NOW - timedelta(days=30), 100) == 4
    for rid in old.values():
        assert store.get_run(WS, rid) is None
        assert store.list_task_runs(rid) == [] and store.list_logs(rid, None, 0, 100) == []
    for rid in (recent, queued, running):
        assert store.get_run(WS, rid) is not None and len(store.list_logs(rid, None, 0, 100)) == 3
    assert store.get_pipeline(WS, p.id) is not None  # the pipeline and its versions are untouched
    assert store.prune_runs(NOW - timedelta(days=30), 100) == 0


def test_a_batch_takes_the_oldest_first_and_stops_at_its_limit(store):
    p = store.create_pipeline(WS, "capture", "", "a")
    ids = [add_run(store, p, RUN_SUCCEEDED, 100 - i, logs=1) for i in range(5)]  # ids[0] is the oldest
    assert store.prune_runs(NOW, 2) == 2
    assert [store.get_run(WS, r) is None for r in ids] == [True, True, False, False, False]
    assert store.prune_runs(NOW, 2) == 2 and store.prune_runs(NOW, 2) == 1


def test_pruners_running_at_once_share_the_work_without_errors(store):
    p = store.create_pipeline(WS, "capture", "", "a")
    for i in range(60):
        add_run(store, p, RUN_SUCCEEDED, 50 + i, logs=1)
    counts: list[int] = []
    errors: list[BaseException] = []

    def go():
        try:
            n = 0
            while (got := store.prune_runs(NOW, 5)) > 0:
                n += got
            counts.append(n)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=go) for _ in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors and sum(counts) == 60
    assert store.list_runs(WS, p.id, None, 100, 0).total == 0


def test_a_run_another_transaction_holds_is_skipped_not_waited_for(store):
    from booth_pipeline.store.postgres import PostgresStore

    if not isinstance(store, PostgresStore):
        pytest.skip("row locks are PostgreSQL's")
    p = store.create_pipeline(WS, "capture", "", "a")
    held, free = add_run(store, p, RUN_SUCCEEDED, 60), add_run(store, p, RUN_SUCCEEDED, 50)
    with store._pool.connection() as conn, conn.transaction():
        conn.execute("SELECT id FROM runs WHERE id=%s FOR UPDATE", (held,))
        assert store.prune_runs(NOW, 10) == 1  # returns at once, having taken the other
    assert store.get_run(WS, free) is None and store.get_run(WS, held) is not None
    assert store.prune_runs(NOW, 10) == 1  # the next night gets it


# ---- the pruner ----------------------------------------------------------------------------------


class CountingStore:
    """Hands out ``backlog`` runs a batch at a time."""

    def __init__(self, backlog: int) -> None:
        self.backlog = backlog
        self.calls: list[tuple[datetime, int]] = []

    def prune_runs(self, before, limit):
        self.calls.append((before, limit))
        n = min(limit, self.backlog)
        self.backlog -= n
        return n


def test_a_prune_works_through_the_backlog_a_batch_at_a_time_from_the_cutoff():
    s = CountingStore(1250)
    assert RunPruner(s, 30, batch=500, pause_seconds=0).prune_once(NOW) == 1250  # type: ignore[arg-type]
    assert s.calls == [(NOW - timedelta(days=30), 500)] * 3


def test_a_prune_stops_at_its_budget_and_leaves_the_rest_for_the_next_night():
    s = CountingStore(10_000)
    assert RunPruner(s, 30, batch=500, pause_seconds=0, budget_seconds=0).prune_once(NOW) == 500  # type: ignore[arg-type]
    assert s.backlog == 9500


def test_the_prune_runs_at_the_hour_every_night():
    assert next_prune_at(datetime(2026, 10, 28, 7, 59, tzinfo=UTC), 8) == datetime(2026, 10, 28, 8, 0, tzinfo=UTC)
    assert next_prune_at(datetime(2026, 10, 28, 8, 0, tzinfo=UTC), 8) == datetime(2026, 10, 29, 8, 0, tzinfo=UTC)
    assert next_prune_at(datetime(2026, 10, 28, 23, 0, tzinfo=UTC), 3) == datetime(2026, 10, 29, 3, 0, tzinfo=UTC)


def test_retention_is_off_unless_days_are_set(monkeypatch):
    for k, v in {"BOOTH_OIDC_ISSUER_URL": "https://idp/x", "BOOTH_OIDC_REQUIRE_AUDIENCE": "false", "BOOTH_PIPELINE_DEV_MEMORY": "true"}.items():
        monkeypatch.setenv(k, v)
    assert Config.from_env().retention_days == 0
    monkeypatch.setenv("BOOTH_PIPELINE_RETENTION_DAYS", "30")
    assert Config.from_env().retention_days == 30
    monkeypatch.setenv("BOOTH_PIPELINE_RETENTION_HOUR_UTC", "24")
    with pytest.raises(ConfigError, match="0 to 23"):
        Config.from_env()


def test_the_app_starts_a_pruner_only_when_retention_is_on():
    from .harness import make_env

    for days, expect in ((0, False), (30, True)):
        env = make_env(start_scheduler=True, retention_days=days)
        try:
            with env.client:  # runs the lifespan: the scheduler and, when on, the pruner start
                names = {t.name for t in threading.enumerate()}
                assert ("run-retention" in names) is expect
        finally:
            env.client.close()
