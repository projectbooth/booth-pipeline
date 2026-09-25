"""The Store contract, run against the in-memory and PostgreSQL implementations alike."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from booth_pipeline.model import Schedule, TaskConfig
from booth_pipeline.records import (
    RUN_FAILED,
    RUN_QUEUED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    TASK_FAILED,
    TASK_RUNNING,
    LogLine,
    Pipeline,
    Run,
)
from booth_pipeline.store.base import Conflict, InUse

from .helpers import linear, task_config

WS, OTHER = "acme", "other"
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def new_run(pipeline: Pipeline, status=RUN_QUEUED) -> Run:
    return Run(str(uuid4()), pipeline.workspace, pipeline.id, 1, status, "manual", "alice", T0)


def scheduled(store, pipeline: Pipeline, **over) -> Pipeline:
    """Persist `pipeline` with the given schedule fields overridden — `update_schedule`'s own
    contract test fixture."""
    fields = {"schedule": None, "allow_concurrent_runs": False, "next_run_at": None, "owner_sub": "", "role_ceiling": "editor", **over}
    for k, v in fields.items():
        setattr(pipeline, k, v)
    return store.update_schedule(pipeline)


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


def test_spec_round_trips_exactly_including_task_refs(store):
    from booth_pipeline.model import PipelineSpec

    spec = PipelineSpec.model_validate(
        {
            "tasks": [
                {
                    "key": "a",
                    "taskId": "t-1",
                    "taskVersion": 3,
                    "dependsOn": [],
                    "position": {"x": 10.5, "y": -3},
                }
            ]
        }
    )
    p = store.create_pipeline(WS, "etl", "", "a")
    store.add_version(WS, p.id, spec, "", "a")
    assert store.get_version(WS, p.id, 1).spec == spec


def test_pipeline_draft_is_separate_from_and_survives_independently_of_versions(store):
    """ADR 0073: a mutable draft, distinct from the immutable version list — plain "Save" writes
    only this, no version created."""
    p = store.create_pipeline(WS, "etl", "", "a")
    assert store.get_pipeline_draft(WS, p.id) is None and store.get_pipeline(WS, p.id).has_draft is False
    d1 = store.save_pipeline_draft(WS, p.id, linear(), "alice")
    assert d1.pipeline_id == p.id and [t.key for t in d1.spec.tasks] == ["extract", "clean", "load"]
    assert store.get_pipeline(WS, p.id).has_draft is True
    assert store.get_pipeline(WS, p.id).latest_version == 0  # no version was created
    got = store.get_pipeline_draft(WS, p.id)
    assert got.updated_by == "alice" and [t.key for t in got.spec.tasks] == ["extract", "clean", "load"]
    d2 = store.save_pipeline_draft(WS, p.id, linear(), "bob")
    assert d2.updated_by == "bob"  # overwritten in place, not accumulated
    assert store.save_pipeline_draft(OTHER, p.id, linear(), "x") is None  # wrong workspace
    assert store.get_pipeline_draft(OTHER, p.id) is None


def test_deleting_a_pipeline_removes_its_runs_task_runs_and_logs(store):
    """ADR 0071: Pipeline owns its run history directly now — deleting it cascades, the same way
    pipeline_versions already did (there is no more separate Job to block or survive deletion)."""
    p = store.create_pipeline(WS, "etl", "", "a")
    r = store.create_run(new_run(p), ["a"])
    store.append_logs([LogLine(r.id, 1, T0, "a", 1, "stdout", "hi")])
    assert store.delete_pipeline(WS, p.id) is True
    assert store.get_run(WS, r.id) is None and store.list_task_runs(r.id) == [] and store.list_logs(r.id, None, 0, 10) == []


# ---- pipeline scheduling — folded onto Pipeline directly (ADR 0071) ---------------------------


def test_update_schedule_persists_schedule_and_ownership_fields(store):
    p = store.create_pipeline(WS, "etl", "", "a")
    assert p.schedule is None and p.owner_sub == "" and p.role_ceiling == "editor" and p.pinned_version is None
    saved = scheduled(store, p, schedule=Schedule(cron="0 9 * * *", timezone="America/Toronto"), owner_sub="sub-1", role_ceiling="viewer", allow_concurrent_runs=True)
    assert saved.schedule.cron == "0 9 * * *" and saved.schedule.timezone == "America/Toronto"
    assert saved.owner_sub == "sub-1" and saved.role_ceiling == "viewer" and saved.allow_concurrent_runs is True
    got = store.get_pipeline(WS, p.id)
    assert got.schedule.cron == "0 9 * * *" and got.owner_sub == "sub-1"
    # wrong workspace, and an absent pipeline, are both a no-op None
    other = Pipeline(p.id, OTHER, p.name, p.description, p.created_by, p.created_at, p.updated_at)
    assert scheduled(store, other) is None
    ghost = Pipeline(str(uuid4()), WS, "x", "", "a", T0, T0)
    assert scheduled(store, ghost) is None


def test_pinned_version_is_persisted_and_can_be_cleared(store):
    """ADR 0071's second "Open question, ruled 2026-09-24": a pipeline can pin every run to a
    specific saved version, independent of what is saved on top of it later."""
    p = store.create_pipeline(WS, "etl", "", "a")
    store.add_version(WS, p.id, linear(), "", "a")
    store.add_version(WS, p.id, linear(), "", "a")
    pinned = scheduled(store, p, pinned_version=1)
    assert pinned.pinned_version == 1
    assert store.get_pipeline(WS, p.id).pinned_version == 1
    cleared = scheduled(store, p, pinned_version=None)
    assert cleared.pinned_version is None


def test_claim_due_pipelines_claims_each_fire_once_and_advances_the_schedule(store):
    now = T0 + timedelta(minutes=1)
    due = store.create_pipeline(WS, "due", "", "a")
    scheduled(store, due, schedule=Schedule(cron="*/10 * * * *"), next_run_at=T0)
    future = store.create_pipeline(WS, "future", "", "a")
    scheduled(store, future, schedule=Schedule(cron="*/10 * * * *"), next_run_at=now + timedelta(hours=1))
    disabled = store.create_pipeline(WS, "disabled", "", "a")
    scheduled(store, disabled, schedule=Schedule(cron="*/10 * * * *", enabled=False), next_run_at=T0)
    store.create_pipeline(WS, "manual-only", "", "a")
    claimed = store.claim_due_pipelines(now, 10)
    assert [p.id for p in claimed] == [due.id]
    assert store.claim_due_pipelines(now, 10) == []  # already claimed: a second poller gets nothing
    assert store.get_pipeline(WS, due.id).next_run_at == datetime(2026, 1, 1, 12, 10, tzinfo=UTC)


def test_claim_due_pipelines_respects_limit_and_orders_oldest_first(store):
    for i in range(3):
        p = store.create_pipeline(WS, f"p{i}", "", "a")
        scheduled(store, p, schedule=Schedule(cron="0 * * * *"), next_run_at=T0 - timedelta(hours=i))
    first = store.claim_due_pipelines(T0, 2)
    assert [p.name for p in first] == ["p2", "p1"]
    assert [p.name for p in store.claim_due_pipelines(T0, 2)] == ["p0"]


# ---- tasks (ADR 0071): the direct counterpart of pipelines & versions above --------------------


def test_task_crud_and_workspace_isolation(store):
    t = store.create_task(WS, "loader", "loads things", "alice")
    assert store.get_task(WS, t.id).name == "loader"
    assert store.get_task(OTHER, t.id) is None  # another workspace cannot see it
    assert store.update_task(OTHER, t.id, "x", "") is None
    assert store.delete_task(OTHER, t.id) is False
    assert store.update_task(WS, t.id, "loader2", "d2").name == "loader2"
    assert store.delete_task(WS, t.id) is True
    assert store.get_task(WS, t.id) is None
    assert store.delete_task(WS, t.id) is False


def test_task_names_are_not_unique_even_within_one_workspace(store):
    """Unlike a Pipeline, a Task's name is a browsable label, not something a person types to
    navigate to it — and this repo's own ADR 0071 migration mints many same-named tasks."""
    a = store.create_task(WS, "loader", "", "a")
    b = store.create_task(WS, "loader", "", "a")
    assert a.id != b.id
    assert store.list_tasks(WS, "loader", 10, 0).total == 2


def test_task_list_search_and_paging(store):
    for n, d in [("alpha", "loads users"), ("beta", "50%_off report"), ("gamma", "")]:
        store.create_task(WS, n, d, "a")
    store.create_task(OTHER, "alpha-elsewhere", "", "a")
    page = store.list_tasks(WS, "", 10, 0)
    assert [t.name for t in page.items] == ["alpha", "beta", "gamma"] and page.total == 3
    assert [t.name for t in store.list_tasks(WS, "", 2, 1).items] == ["beta", "gamma"]
    assert [t.name for t in store.list_tasks(WS, "USERS", 10, 0).items] == ["alpha"]
    assert [t.name for t in store.list_tasks(WS, "50%_", 10, 0).items] == ["beta"]
    assert store.list_tasks(WS, "%", 10, 0).total == 1


def test_task_versions_are_sequential_immutable_and_latest_is_reported(store):
    t = store.create_task(WS, "loader", "", "a")
    assert store.get_task_version(WS, t.id, None) is None and store.get_task(WS, t.id).latest_version == 0
    cfg = TaskConfig.model_validate(task_config())
    v1 = store.add_task_version(WS, t.id, cfg, "first", "alice")
    v2 = store.add_task_version(WS, t.id, cfg, "second", "bob")
    assert (v1.version, v2.version) == (1, 2)
    assert store.get_task(WS, t.id).latest_version == 2
    assert store.get_task_version(WS, t.id, None).notes == "second"
    assert store.get_task_version(WS, t.id, 1).notes == "first"
    assert store.get_task_version(WS, t.id, 3) is None and store.get_task_version(WS, t.id, 0) is None
    assert [v.version for v in store.list_task_versions(WS, t.id, 10, 0).items] == [2, 1]
    assert store.add_task_version(OTHER, t.id, cfg, "", "x") is None
    assert store.get_task_version(OTHER, t.id, 1) is None
    assert store.list_task_versions(OTHER, t.id, 10, 0).total == 0


def test_task_draft_is_separate_from_and_survives_independently_of_versions(store):
    """ADR 0073: a task can be "configured" via its draft alone, with zero saved versions."""
    t = store.create_task(WS, "loader", "", "a")
    assert store.get_task_draft(WS, t.id) is None and store.get_task(WS, t.id).has_draft is False
    cfg = TaskConfig.model_validate(task_config())
    d1 = store.save_task_draft(WS, t.id, cfg, "alice")
    assert d1.task_id == t.id and d1.config == cfg
    assert store.get_task(WS, t.id).has_draft is True
    assert store.get_task(WS, t.id).latest_version == 0  # no version was created
    got = store.get_task_draft(WS, t.id)
    assert got.updated_by == "alice" and got.config == cfg
    d2 = store.save_task_draft(WS, t.id, cfg, "bob")
    assert d2.updated_by == "bob"  # overwritten in place, not accumulated
    assert store.save_task_draft(OTHER, t.id, cfg, "x") is None  # wrong workspace
    assert store.get_task_draft(OTHER, t.id) is None


def test_task_config_round_trips_exactly_including_snapshotted_code(store):
    cfg = TaskConfig.model_validate(
        {
            "code": {"type": "catalog", "entryId": "e1", "version": "1.2.0", "name": "loader", "sha256": "ab" * 32, "source": "def run(ctx):\n    return 'é'\n"},
            "retry": {"maxRetries": 2, "delaySeconds": 1.5, "backoff": "linear"},
            "params": {"n": [1, {"x": None}]},
        }
    )
    t = store.create_task(WS, "loader", "", "a")
    store.add_task_version(WS, t.id, cfg, "", "a")
    assert store.get_task_version(WS, t.id, 1).config == cfg


def test_cannot_delete_a_task_still_referenced_by_a_pipeline_version(store):
    from booth_pipeline.model import PipelineSpec

    t = store.create_task(WS, "loader", "", "a")
    store.add_task_version(WS, t.id, TaskConfig.model_validate(task_config()), "", "a")
    p = store.create_pipeline(WS, "etl", "", "a")
    spec = PipelineSpec.model_validate({"tasks": [{"key": "a", "taskId": t.id, "taskVersion": 1}]})
    store.add_version(WS, p.id, spec, "", "a")
    with pytest.raises(InUse):
        store.delete_task(WS, t.id)
    store.delete_pipeline(WS, p.id)
    assert store.delete_task(WS, t.id) is True


# ---- runs ------------------------------------------------------------------------------------


def make_run(store, ws=WS):
    p = store.create_pipeline(ws, f"etl-{uuid4().hex[:6]}", "", "a")
    return p, store.create_run(new_run(p), ["a", "b"])


def test_run_lifecycle_and_first_terminal_write_wins(store):
    p, r = make_run(store)
    assert store.get_run(OTHER, r.id) is None and store.count_active_runs(WS, p.id) == 1
    assert {t.task_key: t.status for t in store.list_task_runs(r.id)} == {"a": "pending", "b": "pending"}
    store.mark_running(r.id, "w1", T0)
    got = store.get_run(WS, r.id)
    assert (got.status, got.worker_id, got.started_at) == (RUN_RUNNING, "w1", T0)
    assert store.finish_run(r.id, RUN_SUCCEEDED, None, T0 + timedelta(seconds=5)) is True
    assert store.finish_run(r.id, RUN_FAILED, "late", T0) is False  # terminal states never change
    got = store.get_run(WS, r.id)
    assert (got.status, got.error, got.finished_at) == (RUN_SUCCEEDED, None, T0 + timedelta(seconds=5))
    assert store.count_active_runs(WS, p.id) == 0


def test_mark_running_only_moves_a_queued_run(store):
    _, r = make_run(store)
    store.finish_run(r.id, RUN_FAILED, "x", T0)
    store.mark_running(r.id, "w1", T0)
    assert store.get_run(WS, r.id).status == RUN_FAILED


def test_list_runs_filters_and_orders_newest_first(store):
    p = store.create_pipeline(WS, "etl", "", "a")
    ids = []
    for i in range(3):
        r = new_run(p)
        r.created_at = T0 + timedelta(minutes=i)
        ids.append(store.create_run(r, ["a"]).id)
    store.finish_run(ids[0], RUN_FAILED, "x", T0)
    page = store.list_runs(WS, p.id, None, 10, 0)
    assert [r.id for r in page.items] == ids[::-1] and page.total == 3
    assert [r.id for r in store.list_runs(WS, p.id, RUN_FAILED, 10, 0).items] == [ids[0]]
    assert store.list_runs(OTHER, p.id, None, 10, 0).total == 0
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
    first = store.create_run(new_run(p), ["a"], exclusive=True)
    with pytest.raises(Busy):
        store.create_run(new_run(p), ["a"], exclusive=True)
    store.create_run(new_run(p), ["a"], exclusive=False)  # a concurrent-allowed run is never refused
    store.finish_run(first.id, RUN_SUCCEEDED, None, T0)
    assert store.count_active_runs(WS, p.id) == 1  # only the non-exclusive one is left


def test_concurrent_exclusive_triggers_admit_exactly_one(store):
    """The race the lock exists for: N simultaneous triggers of a non-concurrent pipeline."""
    import threading

    from booth_pipeline.store.base import Busy

    p = store.create_pipeline(WS, "etl", "", "a")
    results: list[str] = []
    barrier = threading.Barrier(8)

    def go():
        barrier.wait()
        try:
            store.create_run(new_run(p), ["a"], exclusive=True)
            results.append("ok")
        except Busy:
            results.append("busy")

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count("ok") == 1 and results.count("busy") == 7
