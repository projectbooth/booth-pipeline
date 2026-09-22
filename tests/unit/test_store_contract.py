"""The Store contract, run against the in-memory and PostgreSQL implementations alike."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from booth_pipeline.model import RetryPolicy, Schedule
from booth_pipeline.records import (
    RUN_FAILED,
    RUN_QUEUED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    TASK_FAILED,
    TASK_RUNNING,
    Job,
    LogLine,
    Run,
)
from booth_pipeline.store.base import Conflict, InUse

from .helpers import linear

WS, OTHER = "acme", "other"
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def new_job(ws=WS, name="nightly", pipeline_id="p", version=None, schedule=None, next_run_at=None) -> Job:
    return Job(str(uuid4()), ws, name, pipeline_id, version, schedule, None, False, "alice", T0, T0, next_run_at)


def new_run(job: Job, status=RUN_QUEUED) -> Run:
    return Run(str(uuid4()), job.workspace, job.id, job.pipeline_id, 1, status, "manual", "alice", T0)


# ---- pipelines & versions --------------------------------------------------------------------


def test_pipeline_crud_and_workspace_isolation(store):
    p = store.create_pipeline(WS, "etl", "loads things", "alice")
    assert store.get_pipeline(WS, p.id).name == "etl"
    assert store.get_pipeline(OTHER, p.id) is None  # another workspace cannot see it
    assert store.update_pipeline(OTHER, p.id, "x", "") is None
    assert store.delete_pipeline(OTHER, p.id) is False
    assert store.update_pipeline(WS, p.id, "etl2", "d2").name == "etl2"
    assert store.delete_pipeline(WS, p.id) is True
    assert store.get_pipeline(WS, p.id) is None
    assert store.delete_pipeline(WS, p.id) is False


def test_pipeline_names_are_unique_per_workspace_only(store):
    p = store.create_pipeline(WS, "etl", "", "a")
    store.create_pipeline(OTHER, "etl", "", "a")  # same name, other workspace: fine
    with pytest.raises(Conflict) as ei:
        store.create_pipeline(WS, "etl", "", "a")
    assert ei.value.field == "name"
    q = store.create_pipeline(WS, "other", "", "a")
    with pytest.raises(Conflict):
        store.update_pipeline(WS, q.id, "etl", "")
    assert store.update_pipeline(WS, p.id, "etl", "renamed to itself is fine").description.startswith("renamed")


def test_pipeline_list_search_and_paging(store):
    for n, d in [("alpha", "loads users"), ("beta", "50%_off report"), ("gamma", "")]:
        store.create_pipeline(WS, n, d, "a")
    store.create_pipeline(OTHER, "alpha-elsewhere", "", "a")
    page = store.list_pipelines(WS, "", 10, 0)
    assert [p.name for p in page.items] == ["alpha", "beta", "gamma"] and page.total == 3
    assert [p.name for p in store.list_pipelines(WS, "", 2, 1).items] == ["beta", "gamma"]
    assert [p.name for p in store.list_pipelines(WS, "USERS", 10, 0).items] == ["alpha"]  # case-insensitive, matches description
    assert [p.name for p in store.list_pipelines(WS, "50%_", 10, 0).items] == ["beta"]  # LIKE metacharacters are literal
    assert store.list_pipelines(WS, "%", 10, 0).total == 1  # a bare % must not match everything


def test_versions_are_sequential_immutable_and_latest_is_reported(store):
    p = store.create_pipeline(WS, "etl", "", "a")
    assert store.get_version(WS, p.id, None) is None and store.get_pipeline(WS, p.id).latest_version == 0
    v1 = store.add_version(WS, p.id, linear(), "first", "alice")
    v2 = store.add_version(WS, p.id, linear(), "second", "bob")
    assert (v1.version, v2.version) == (1, 2)
    assert store.get_pipeline(WS, p.id).latest_version == 2
    assert store.get_version(WS, p.id, None).notes == "second"
    got = store.get_version(WS, p.id, 1)
    assert got.notes == "first" and [t.key for t in got.spec.tasks] == ["extract", "clean", "load"]
    assert store.get_version(WS, p.id, 3) is None and store.get_version(WS, p.id, 0) is None
    assert [v.version for v in store.list_versions(WS, p.id, 10, 0).items] == [2, 1]  # newest first
    assert store.add_version(OTHER, p.id, linear(), "", "x") is None  # wrong workspace
    assert store.get_version(OTHER, p.id, 1) is None
    assert store.list_versions(OTHER, p.id, 10, 0).total == 0


def test_spec_round_trips_exactly_including_snapshotted_code(store):
    from booth_pipeline.model import PipelineSpec

    spec = PipelineSpec.model_validate(
        {
            "tasks": [
                {
                    "key": "a",
                    "kind": "source",
                    "code": {"type": "catalog", "entryId": "e1", "version": "1.2.0", "name": "loader", "sha256": "ab" * 32, "source": "def run(ctx):\n    return 'é'\n"},
                    "retry": {"maxRetries": 2, "delaySeconds": 1.5, "backoff": "linear"},
                    "params": {"n": [1, {"x": None}]},
                    "position": {"x": 10.5, "y": -3},
                }
            ]
        }
    )
    p = store.create_pipeline(WS, "etl", "", "a")
    store.add_version(WS, p.id, spec, "", "a")
    assert store.get_version(WS, p.id, 1).spec == spec


def test_cannot_delete_a_pipeline_that_has_jobs(store):
    p = store.create_pipeline(WS, "etl", "", "a")
    j = store.create_job(new_job(pipeline_id=p.id))
    with pytest.raises(InUse):
        store.delete_pipeline(WS, p.id)
    store.delete_job(WS, j.id)
    assert store.delete_pipeline(WS, p.id) is True


# ---- jobs ------------------------------------------------------------------------------------


def test_job_crud_uniqueness_and_isolation(store):
    p = store.create_pipeline(WS, "etl", "", "a")
    j = store.create_job(new_job(pipeline_id=p.id, schedule=Schedule(cron="0 9 * * *", timezone="America/Toronto")))
    got = store.get_job(WS, j.id)
    assert got.schedule.cron == "0 9 * * *" and got.schedule.timezone == "America/Toronto" and got.pipeline_version is None
    assert store.get_job(OTHER, j.id) is None
    with pytest.raises(Conflict):
        store.create_job(new_job(pipeline_id=p.id))
    j2 = store.create_job(new_job(name="other", pipeline_id=p.id))
    j2.name = "nightly"
    with pytest.raises(Conflict):
        store.update_job(j2)
    j.retry, j.pipeline_version = RetryPolicy(maxRetries=3, backoff="exponential"), 1
    saved = store.update_job(j)
    assert saved.retry.max_retries == 3 and store.get_job(WS, j.id).pipeline_version == 1
    assert store.list_jobs(WS, p.id, "", 10, 0).total == 2
    assert store.list_jobs(WS, "nope", "", 10, 0).total == 0
    assert store.list_jobs(OTHER, None, "", 10, 0).total == 0
    assert store.delete_job(OTHER, j.id) is False and store.delete_job(WS, j.id) is True


def test_claim_due_jobs_claims_each_fire_once_and_advances_the_schedule(store):
    p = store.create_pipeline(WS, "etl", "", "a")
    now = T0 + timedelta(minutes=1)
    due = store.create_job(new_job(name="due", pipeline_id=p.id, schedule=Schedule(cron="*/10 * * * *"), next_run_at=T0))
    store.create_job(new_job(name="future", pipeline_id=p.id, schedule=Schedule(cron="*/10 * * * *"), next_run_at=now + timedelta(hours=1)))
    store.create_job(new_job(name="disabled", pipeline_id=p.id, schedule=Schedule(cron="*/10 * * * *", enabled=False), next_run_at=T0))
    store.create_job(new_job(name="manual-only", pipeline_id=p.id))
    claimed = store.claim_due_jobs(now, 10)
    assert [j.id for j in claimed] == [due.id]
    assert store.claim_due_jobs(now, 10) == []  # already claimed: a second poller gets nothing
    assert store.get_job(WS, due.id).next_run_at == datetime(2026, 1, 1, 12, 10, tzinfo=UTC)


def test_claim_due_jobs_respects_limit_and_orders_oldest_first(store):
    p = store.create_pipeline(WS, "etl", "", "a")
    for i in range(3):
        store.create_job(new_job(name=f"j{i}", pipeline_id=p.id, schedule=Schedule(cron="0 * * * *"), next_run_at=T0 - timedelta(hours=i)))
    first = store.claim_due_jobs(T0, 2)
    assert [j.name for j in first] == ["j2", "j1"]
    assert [j.name for j in store.claim_due_jobs(T0, 2)] == ["j0"]


# ---- runs ------------------------------------------------------------------------------------


def make_run(store, ws=WS):
    p = store.create_pipeline(ws, f"etl-{uuid4().hex[:6]}", "", "a")
    j = store.create_job(new_job(ws=ws, name=f"j-{uuid4().hex[:6]}", pipeline_id=p.id))
    return j, store.create_run(new_run(j), ["a", "b"])


def test_run_lifecycle_and_first_terminal_write_wins(store):
    j, r = make_run(store)
    assert store.get_run(OTHER, r.id) is None and store.count_active_runs(WS, j.id) == 1
    assert {t.task_key: t.status for t in store.list_task_runs(r.id)} == {"a": "pending", "b": "pending"}
    store.mark_running(r.id, "w1", T0)
    got = store.get_run(WS, r.id)
    assert (got.status, got.worker_id, got.started_at) == (RUN_RUNNING, "w1", T0)
    assert store.finish_run(r.id, RUN_SUCCEEDED, None, T0 + timedelta(seconds=5)) is True
    assert store.finish_run(r.id, RUN_FAILED, "late", T0) is False  # terminal states never change
    got = store.get_run(WS, r.id)
    assert (got.status, got.error, got.finished_at) == (RUN_SUCCEEDED, None, T0 + timedelta(seconds=5))
    assert store.count_active_runs(WS, j.id) == 0


def test_mark_running_only_moves_a_queued_run(store):
    _, r = make_run(store)
    store.finish_run(r.id, RUN_FAILED, "x", T0)
    store.mark_running(r.id, "w1", T0)
    assert store.get_run(WS, r.id).status == RUN_FAILED


def test_list_runs_filters_and_orders_newest_first(store):
    p = store.create_pipeline(WS, "etl", "", "a")
    j = store.create_job(new_job(pipeline_id=p.id))
    ids = []
    for i in range(3):
        r = new_run(j)
        r.created_at = T0 + timedelta(minutes=i)
        ids.append(store.create_run(r, ["a"]).id)
    store.finish_run(ids[0], RUN_FAILED, "x", T0)
    page = store.list_runs(WS, j.id, None, 10, 0)
    assert [r.id for r in page.items] == ids[::-1] and page.total == 3
    assert [r.id for r in store.list_runs(WS, j.id, RUN_FAILED, 10, 0).items] == [ids[0]]
    assert store.list_runs(OTHER, j.id, None, 10, 0).total == 0
    assert len(store.list_runs(WS, None, None, 2, 0).items) == 2


def test_cancel_request_only_applies_to_active_runs(store):
    _, r = make_run(store)
    assert store.is_cancel_requested(r.id) is False
    assert store.request_cancel(OTHER, r.id) is None
    assert store.request_cancel(WS, r.id).cancel_requested is True and store.is_cancel_requested(r.id)
    _, done = make_run(store)
    store.finish_run(done.id, RUN_SUCCEEDED, None, T0)
    assert store.request_cancel(WS, done.id).cancel_requested is False  # too late to cancel


def test_stale_runs_are_running_runs_whose_heartbeat_stopped(store):
    _, alive = make_run(store)
    _, dead = make_run(store)
    _, queued = make_run(store)
    store.mark_running(alive.id, "w", T0)
    store.mark_running(dead.id, "w", T0)
    store.heartbeat(alive.id, T0 + timedelta(minutes=5))
    # created_at is T0 for all three; a queued run is only stale once it is older than queued_before
    stale = store.stale_runs(T0 + timedelta(minutes=2), T0 - timedelta(minutes=1))
    assert [r.id for r in stale] == [dead.id]
    assert queued.id not in [r.id for r in stale]
    stale = store.stale_runs(T0 + timedelta(minutes=2), T0 + timedelta(minutes=1))
    assert sorted(r.id for r in stale) == sorted([dead.id, queued.id])  # the never-started run is now stale too


def test_task_runs_update(store):
    _, r = make_run(store)
    tr = next(t for t in store.list_task_runs(r.id) if t.task_key == "a")
    tr.status, tr.attempts, tr.started_at = TASK_RUNNING, 2, T0
    store.update_task_run(tr)
    tr.status, tr.error, tr.finished_at = TASK_FAILED, "boom", T0 + timedelta(seconds=1)
    store.update_task_run(tr)
    got = {t.task_key: t for t in store.list_task_runs(r.id)}
    assert (got["a"].status, got["a"].attempts, got["a"].error) == (TASK_FAILED, 2, "boom")
    assert got["b"].status == "pending"


def test_deleting_a_job_removes_its_runs_task_runs_and_logs(store):
    j, r = make_run(store)
    store.append_logs([LogLine(r.id, 1, T0, "a", 1, "stdout", "hi")])
    store.delete_job(WS, j.id)
    assert store.get_run(WS, r.id) is None and store.list_task_runs(r.id) == [] and store.list_logs(r.id, None, 0, 10) == []


# ---- logs ------------------------------------------------------------------------------------


def test_logs_page_by_seq_and_filter_by_task(store):
    _, r = make_run(store)
    lines = [LogLine(r.id, i, T0 + timedelta(seconds=i), "a" if i % 2 else "b", 1, "stdout", f"line {i}") for i in range(1, 7)]
    lines.append(LogLine(r.id, 7, T0, None, 0, "system", "run finished"))
    store.append_logs(lines)
    assert [ln.seq for ln in store.list_logs(r.id, None, 0, 100)] == [1, 2, 3, 4, 5, 6, 7]
    assert [ln.seq for ln in store.list_logs(r.id, None, 3, 2)] == [4, 5]  # the polling cursor
    assert [ln.message for ln in store.list_logs(r.id, "a", 0, 100)] == ["line 1", "line 3", "line 5"]
    assert store.list_logs(r.id, None, 7, 100) == []
    last = store.list_logs(r.id, None, 6, 100)[0]
    assert (last.task_key, last.stream, last.message) == (None, "system", "run finished")


def test_appending_the_same_seq_twice_is_idempotent_and_nul_bytes_survive(store):
    _, r = make_run(store)
    store.append_logs([LogLine(r.id, 1, T0, "a", 1, "stdout", "a\x00b")])
    store.append_logs([LogLine(r.id, 1, T0, "a", 1, "stdout", "again")])  # a retried flush must not duplicate or blow up
    got = store.list_logs(r.id, None, 0, 10)
    assert len(got) == 1 and got[0].message.startswith("a") and got[0].message.endswith("b")


def test_ping(store):
    store.ping()


def test_exclusive_run_creation_is_refused_while_another_is_active(store):
    from booth_pipeline.store.base import Busy

    p = store.create_pipeline(WS, "etl", "", "a")
    j = store.create_job(new_job(pipeline_id=p.id))
    first = store.create_run(new_run(j), ["a"], exclusive=True)
    with pytest.raises(Busy):
        store.create_run(new_run(j), ["a"], exclusive=True)
    store.create_run(new_run(j), ["a"], exclusive=False)  # a concurrent-allowed job is never refused
    store.finish_run(first.id, RUN_SUCCEEDED, None, T0)
    assert store.count_active_runs(WS, j.id) == 1  # only the non-exclusive one is left


def test_concurrent_exclusive_triggers_admit_exactly_one(store):
    """The race the lock exists for: N simultaneous triggers of a non-concurrent job."""
    import threading

    from booth_pipeline.store.base import Busy

    p = store.create_pipeline(WS, "etl", "", "a")
    j = store.create_job(new_job(pipeline_id=p.id))
    results: list[str] = []
    barrier = threading.Barrier(8)

    def go():
        barrier.wait()
        try:
            store.create_run(new_run(j), ["a"], exclusive=True)
            results.append("ok")
        except Busy:
            results.append("busy")

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count("ok") == 1 and results.count("busy") == 7
