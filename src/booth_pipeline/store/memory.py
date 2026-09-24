"""In-memory ``Store``: for unit tests and a database-free local dev run (state vanishes on exit).

Behaviourally identical to the PostgreSQL store as far as ``tests/unit/test_store_contract.py``
can tell — that suite is the definition of "identical".
"""

from __future__ import annotations

import copy
import threading
from datetime import datetime
from uuid import uuid4

from ..model import PipelineSpec, TaskConfig
from ..records import (
    RUN_ACTIVE,
    RUN_QUEUED,
    RUN_RUNNING,
    RUN_TERMINAL,
    TASK_PENDING,
    LogLine,
    Page,
    Pipeline,
    PipelineVersion,
    Run,
    TaskEntity,
    TaskRun,
    TaskVersionRecord,
)
from ..schedule import next_fire_trigger
from .base import Busy, Conflict, InUse


def _now() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)


class MemoryStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._pipelines: dict[str, Pipeline] = {}
        self._versions: dict[str, list[PipelineVersion]] = {}
        self._tasks: dict[str, TaskEntity] = {}
        self._task_versions: dict[str, list[TaskVersionRecord]] = {}
        self._runs: dict[str, Run] = {}
        self._task_runs: dict[str, dict[str, TaskRun]] = {}
        self._logs: dict[str, list[LogLine]] = {}

    # ---- pipelines ----
    def create_pipeline(self, workspace, name, description, by):
        with self._lock:
            if any(p.workspace == workspace and p.name == name for p in self._pipelines.values()):
                raise Conflict("a pipeline with that name already exists in this workspace", "name")
            now = _now()
            p = Pipeline(str(uuid4()), workspace, name, description, by, now, now)
            self._pipelines[p.id] = p
            self._versions[p.id] = []
            return copy.deepcopy(p)

    def _pipeline(self, workspace, pipeline_id):
        p = self._pipelines.get(pipeline_id)
        return p if p and p.workspace == workspace else None

    def _with_latest(self, p: Pipeline) -> Pipeline:
        out = copy.deepcopy(p)
        out.latest_version = len(self._versions.get(p.id, []))
        return out

    def get_pipeline(self, workspace, pipeline_id):
        with self._lock:
            p = self._pipeline(workspace, pipeline_id)
            return self._with_latest(p) if p else None

    def list_pipelines(self, workspace, q, limit, offset):
        with self._lock:
            items = [p for p in self._pipelines.values() if p.workspace == workspace and _matches(q, p.name, p.description)]
            items.sort(key=lambda p: (p.name.lower(), p.id))
            return Page([self._with_latest(p) for p in items[offset : offset + limit]], len(items))

    def update_pipeline(self, workspace, pipeline_id, name, description):
        with self._lock:
            p = self._pipeline(workspace, pipeline_id)
            if not p:
                return None
            if any(o.workspace == workspace and o.name == name and o.id != p.id for o in self._pipelines.values()):
                raise Conflict("a pipeline with that name already exists in this workspace", "name")
            p.name, p.description, p.updated_at = name, description, _now()
            return self._with_latest(p)

    def delete_pipeline(self, workspace, pipeline_id):
        with self._lock:
            p = self._pipeline(workspace, pipeline_id)
            if not p:
                return False
            del self._pipelines[p.id]
            self._versions.pop(p.id, None)
            # Pipeline now owns its run history directly (ADR 0071: no more Job to block or
            # survive this) — deleting it cascades, the same way pipeline_versions already did.
            for rid in [r.id for r in self._runs.values() if r.pipeline_id == p.id]:
                self._runs.pop(rid)
                self._task_runs.pop(rid, None)
                self._logs.pop(rid, None)
            return True

    def update_schedule(self, pipeline: Pipeline):
        with self._lock:
            p = self._pipeline(pipeline.workspace, pipeline.id)
            if not p:
                return None
            p.schedule = pipeline.schedule
            p.allow_concurrent_runs = pipeline.allow_concurrent_runs
            p.next_run_at = pipeline.next_run_at
            p.owner_sub = pipeline.owner_sub
            p.role_ceiling = pipeline.role_ceiling
            p.pinned_version = pipeline.pinned_version
            p.updated_at = _now()
            return self._with_latest(p)

    def add_version(self, workspace, pipeline_id, spec: PipelineSpec, notes, by):
        with self._lock:
            p = self._pipeline(workspace, pipeline_id)
            if not p:
                return None
            versions = self._versions[p.id]
            v = PipelineVersion(p.id, len(versions) + 1, spec.model_copy(deep=True), notes, by, _now())
            versions.append(v)
            p.updated_at = v.created_at
            return copy.deepcopy(v)

    def get_version(self, workspace, pipeline_id, version):
        with self._lock:
            p = self._pipeline(workspace, pipeline_id)
            if not p:
                return None
            versions = self._versions[p.id]
            if not versions:
                return None
            if version is None:
                return copy.deepcopy(versions[-1])
            return copy.deepcopy(versions[version - 1]) if 1 <= version <= len(versions) else None

    def list_versions(self, workspace, pipeline_id, limit, offset):
        with self._lock:
            p = self._pipeline(workspace, pipeline_id)
            if not p:
                return Page([], 0)
            items = list(reversed(self._versions[p.id]))
            return Page(copy.deepcopy(items[offset : offset + limit]), len(items))

    # ---- tasks (ADR 0071) ----
    def create_task(self, workspace, name, description, by):
        with self._lock:
            now = _now()
            t = TaskEntity(str(uuid4()), workspace, name, description, by, now, now)
            self._tasks[t.id] = t
            self._task_versions[t.id] = []
            return copy.deepcopy(t)

    def _task(self, workspace, task_id):
        t = self._tasks.get(task_id)
        return t if t and t.workspace == workspace else None

    def _task_with_latest(self, t: TaskEntity) -> TaskEntity:
        out = copy.deepcopy(t)
        out.latest_version = len(self._task_versions.get(t.id, []))
        return out

    def get_task(self, workspace, task_id):
        with self._lock:
            t = self._task(workspace, task_id)
            return self._task_with_latest(t) if t else None

    def list_tasks(self, workspace, q, limit, offset):
        with self._lock:
            items = [t for t in self._tasks.values() if t.workspace == workspace and _matches(q, t.name, t.description)]
            items.sort(key=lambda t: (t.name.lower(), t.id))
            return Page([self._task_with_latest(t) for t in items[offset : offset + limit]], len(items))

    def update_task(self, workspace, task_id, name, description):
        with self._lock:
            t = self._task(workspace, task_id)
            if not t:
                return None
            t.name, t.description, t.updated_at = name, description, _now()
            return self._task_with_latest(t)

    def delete_task(self, workspace, task_id):
        with self._lock:
            t = self._task(workspace, task_id)
            if not t:
                return False
            if any(ref.task_id == task_id for versions in self._versions.values() for v in versions for ref in v.spec.tasks):
                raise InUse("this task is still referenced by a pipeline version; it cannot be removed")
            del self._tasks[task_id]
            self._task_versions.pop(task_id, None)
            return True

    def add_task_version(self, workspace, task_id, config: TaskConfig, notes, by):
        with self._lock:
            t = self._task(workspace, task_id)
            if not t:
                return None
            versions = self._task_versions[t.id]
            v = TaskVersionRecord(t.id, len(versions) + 1, config.model_copy(deep=True), notes, by, _now())
            versions.append(v)
            t.updated_at = v.created_at
            return copy.deepcopy(v)

    def get_task_version(self, workspace, task_id, version):
        with self._lock:
            t = self._task(workspace, task_id)
            if not t:
                return None
            versions = self._task_versions[t.id]
            if not versions:
                return None
            if version is None:
                return copy.deepcopy(versions[-1])
            return copy.deepcopy(versions[version - 1]) if 1 <= version <= len(versions) else None

    def list_task_versions(self, workspace, task_id, limit, offset):
        with self._lock:
            t = self._task(workspace, task_id)
            if not t:
                return Page([], 0)
            items = list(reversed(self._task_versions[t.id]))
            return Page(copy.deepcopy(items[offset : offset + limit]), len(items))

    # ---- scheduling ----
    def claim_due_pipelines(self, now, limit):
        with self._lock:
            due = [p for p in self._pipelines.values() if p.schedule and p.schedule.enabled and p.next_run_at and p.next_run_at <= now]
            due.sort(key=lambda p: (p.next_run_at, p.id))
            out = []
            for p in due[:limit]:
                claimed = copy.deepcopy(p)
                p.next_run_at = next_fire_trigger(p.schedule, now)
                out.append(claimed)
            return out

    def list_upcoming(self, limit):
        with self._lock:
            due = [p for p in self._pipelines.values() if p.schedule and p.schedule.enabled and p.next_run_at]
            due.sort(key=lambda p: (p.next_run_at, p.id))
            return copy.deepcopy(due[:limit])

    # ---- runs ----
    def create_run(self, run, task_keys, exclusive=False):
        with self._lock:
            if exclusive and self.count_active_runs(run.workspace, run.pipeline_id) > 0:
                raise Busy("this pipeline already has an active run")
            self._runs[run.id] = copy.deepcopy(run)
            self._task_runs[run.id] = {k: TaskRun(run.id, k, TASK_PENDING) for k in task_keys}
            self._logs[run.id] = []
            return copy.deepcopy(run)

    def get_run(self, workspace, run_id):
        with self._lock:
            r = self._runs.get(run_id)
            return copy.deepcopy(r) if r and r.workspace == workspace else None

    def list_runs(self, workspace, pipeline_id, status, limit, offset):
        with self._lock:
            items = [
                r
                for r in self._runs.values()
                if r.workspace == workspace and (pipeline_id is None or r.pipeline_id == pipeline_id) and (status is None or r.status == status)
            ]
            items.sort(key=lambda r: (r.created_at, r.id), reverse=True)
            return Page(copy.deepcopy(items[offset : offset + limit]), len(items))

    def count_active_runs(self, workspace, pipeline_id):
        with self._lock:
            return sum(1 for r in self._runs.values() if r.workspace == workspace and r.pipeline_id == pipeline_id and r.status in RUN_ACTIVE)

    def mark_running(self, run_id, worker_id, now):
        with self._lock:
            r = self._runs.get(run_id)
            if r and r.status == RUN_QUEUED:
                r.status, r.started_at, r.worker_id, r.heartbeat_at = RUN_RUNNING, now, worker_id, now

    def heartbeat(self, run_id, now):
        with self._lock:
            r = self._runs.get(run_id)
            if r and r.status in RUN_ACTIVE:
                r.heartbeat_at = now

    def finish_run(self, run_id, status, error, now):
        with self._lock:
            r = self._runs.get(run_id)
            if not r or r.status in RUN_TERMINAL:
                return False
            r.status, r.error, r.finished_at = status, error, now
            return True

    def request_cancel(self, workspace, run_id):
        with self._lock:
            r = self._runs.get(run_id)
            if not r or r.workspace != workspace:
                return None
            if r.status in RUN_ACTIVE:
                r.cancel_requested = True
            return copy.deepcopy(r)

    def is_cancel_requested(self, run_id):
        with self._lock:
            r = self._runs.get(run_id)
            return bool(r and r.cancel_requested)

    def stale_runs(self, heartbeat_before, queued_before):
        with self._lock:
            return copy.deepcopy(
                [
                    r
                    for r in self._runs.values()
                    if (r.status == RUN_RUNNING and r.heartbeat_at and r.heartbeat_at < heartbeat_before)
                    or (r.status == RUN_QUEUED and r.created_at < queued_before)
                ]
            )

    def list_task_runs(self, run_id):
        with self._lock:
            return copy.deepcopy(list(self._task_runs.get(run_id, {}).values()))

    def update_task_run(self, task_run):
        with self._lock:
            if task_run.run_id in self._task_runs:
                self._task_runs[task_run.run_id][task_run.task_key] = copy.deepcopy(task_run)

    # ---- logs ----
    def append_logs(self, lines):
        with self._lock:
            for ln in lines:
                existing = self._logs.get(ln.run_id)
                # Same (run, seq) twice is a retried flush, not a new line — as in Postgres.
                if existing is not None and all(e.seq != ln.seq for e in existing):
                    existing.append(copy.copy(ln))

    def list_logs(self, run_id, task_key, after_seq, limit):
        with self._lock:
            out = [ln for ln in self._logs.get(run_id, []) if ln.seq > after_seq and (task_key is None or ln.task_key == task_key)]
            out.sort(key=lambda ln: ln.seq)
            return copy.deepcopy(out[:limit])

    def ping(self):
        return None


def _matches(q: str, *fields: str) -> bool:
    q = q.strip().lower()
    return not q or any(q in f.lower() for f in fields)
