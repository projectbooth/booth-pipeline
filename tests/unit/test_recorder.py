"""The run-log recorder: ordering, bounding, and never losing the run to a log problem."""

from __future__ import annotations

import threading
from datetime import UTC, datetime

from booth_pipeline.records import Run
from booth_pipeline.runs import StoreRecorder
from booth_pipeline.store.memory import MemoryStore

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def recorder(max_lines=1000, keys=("a", "b")):
    store = MemoryStore()
    p = store.create_pipeline("ws", "p", "", "u")
    from booth_pipeline.records import Job

    j = store.create_job(Job("j1", "ws", "j", p.id, None, None, None, False, "u", T0, T0))
    store.create_run(Run("r1", "ws", j.id, p.id, 1, "running", "manual", "u", T0), list(keys))
    return store, StoreRecorder(store, "r1", max_lines, flush_interval=0.05)


def test_lines_are_sequenced_and_all_persisted_on_close():
    store, rec = recorder()
    log = rec.task_log("a", 1)
    for i in range(500):  # more than one flush batch
        log.line("stdout", f"line {i}")
    rec.close()
    got = store.list_logs("r1", None, 0, 10_000)
    assert [ln.message for ln in got] == [f"line {i}" for i in range(500)]
    assert [ln.seq for ln in got] == list(range(1, 501))


def test_concurrent_writers_never_duplicate_or_skip_a_seq():
    """stdout and stderr readers (and the engine) all write at once."""
    store, rec = recorder(max_lines=5000)
    threads = [threading.Thread(target=lambda n=n: [rec.task_log("a", 1).line("stdout", f"{n}-{i}") for i in range(200)]) for n in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    rec.close()
    got = store.list_logs("r1", None, 0, 10_000)
    assert sorted(ln.seq for ln in got) == list(range(1, 1201))


def test_output_beyond_the_cap_is_dropped_with_one_notice_but_system_lines_still_land():
    store, rec = recorder(max_lines=100)
    log = rec.task_log("a", 1)
    for i in range(500):
        log.line("stdout", f"spam {i}")
    rec.system("run failed: something important")  # the outcome must never be lost to a chatty task
    rec.close()
    got = store.list_logs("r1", None, 0, 10_000)
    assert len(got) <= 102
    notices = [ln for ln in got if "log truncated" in ln.message]
    assert len(notices) == 1 and notices[0].stream == "system"
    assert got[-1].message == "run failed: something important"


def test_task_state_transitions_are_persisted():
    store, rec = recorder()
    rec.task_started("a", 1)
    assert {t.task_key: t.status for t in store.list_task_runs("r1")}["a"] == "running"
    rec.task_attempt_finished("a", 1, "retrying", "boom")
    rec.task_started("a", 2)
    rec.task_attempt_finished("a", 2, "succeeded", None)
    a = next(t for t in store.list_task_runs("r1") if t.task_key == "a")
    assert (a.status, a.attempts, a.error) == ("succeeded", 2, None) and a.started_at and a.finished_at


def test_settling_marks_unstarted_tasks_skipped_or_canceled_and_never_leaves_pending():
    store, rec = recorder(keys=("a", "b", "c"))
    rec.task_started("a", 1)
    rec.task_attempt_finished("a", 1, "failed", "x")
    rec.settle_unfinished("failed")
    assert {t.task_key: t.status for t in store.list_task_runs("r1")} == {"a": "failed", "b": "skipped", "c": "skipped"}
    store2, rec2 = recorder(keys=("a", "b"))
    rec2.task_started("a", 1)
    rec2.settle_unfinished("canceled")
    assert {t.task_key: t.status for t in store2.list_task_runs("r1")} == {"a": "canceled", "b": "canceled"}


def test_a_failing_store_never_raises_into_the_run():
    store, rec = recorder()

    def broken(*_a, **_k):
        raise RuntimeError("database gone")

    store.append_logs = broken  # type: ignore[method-assign]
    store.update_task_run = broken  # type: ignore[method-assign]
    rec.task_started("a", 1)  # must not raise
    rec.task_log("a", 1).line("stdout", "hello")
    rec.close()
