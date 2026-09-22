"""In-memory ``Store``: for unit tests and a database-free local dev run (state vanishes on exit).

Behaviourally identical to the PostgreSQL store as far as ``tests/unit/test_store_contract.py``
can tell — that suite is the definition of "identical".
"""

from __future__ import annotations

import copy
import threading
from datetime import datetime
from uuid import uuid4

from ..model import PipelineSpec
from ..records import (
    RUN_ACTIVE,
    RUN_QUEUED,
    RUN_RUNNING,
    RUN_TERMINAL,
    TASK_PENDING,
    Job,
    LogLine,
    Page,
    Pipeline,
    PipelineVersion,
    Run,
    TaskRun,
)
from ..schedule import next_fire
from .base import Busy, Conflict, InUse


def _now() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)


class MemoryStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._pipelines: dict[str, Pipeline] = {}
        self._versions: dict[str, list[PipelineVersion]] = {}
        self._jobs: dict[str, Job] = {}
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
            return copy.copy(p)

    def _pipeline(self, workspace, pipeline_id):
        p = self._pipelines.get(pipeline_id)
        return p if p and p.workspace == workspace else None

    def _with_latest(self, p: Pipeline) -> Pipeline:
        out = copy.copy(p)
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
            if any(j.pipeline_id == p.id for j in self._jobs.values()):
                raise InUse("this pipeline still has jobs; delete them first")
            del self._pipelines[p.id]
            self._versions.pop(p.id, None)
            return True

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

    # ---- jobs ----
    def create_job(self, job):
        with self._lock:
            if any(j.workspace == job.workspace and j.name == job.name for j in self._jobs.values()):
                raise Conflict("a job with that name already exists in this workspace", "name")
            self._jobs[job.id] = copy.deepcopy(job)
            return copy.deepcopy(job)

    def get_job(self, workspace, job_id):
        with self._lock:
            j = self._jobs.get(job_id)
            return copy.deepcopy(j) if j and j.workspace == workspace else None

    def list_jobs(self, workspace, pipeline_id, q, limit, offset):
        with self._lock:
            items = [
                j
                for j in self._jobs.values()
                if j.workspace == workspace and (pipeline_id is None or j.pipeline_id == pipeline_id) and _matches(q, j.name, "")
            ]
            items.sort(key=lambda j: (j.name.lower(), j.id))
            return Page(copy.deepcopy(items[offset : offset + limit]), len(items))

    def update_job(self, job):
        with self._lock:
            cur = self._jobs.get(job.id)
            if not cur or cur.workspace != job.workspace:
                return None
            if any(o.workspace == job.workspace and o.name == job.name and o.id != job.id for o in self._jobs.values()):
                raise Conflict("a job with that name already exists in this workspace", "name")
            self._jobs[job.id] = copy.deepcopy(job)
            return copy.deepcopy(job)

    def delete_job(self, workspace, job_id):
        with self._lock:
            j = self._jobs.get(job_id)
            if not j or j.workspace != workspace:
                return False
            del self._jobs[job_id]
            for rid in [r.id for r in self._runs.values() if r.job_id == job_id]:
                self._runs.pop(rid)
                self._task_runs.pop(rid, None)
                self._logs.pop(rid, None)
            return True

    def claim_due_jobs(self, now, limit):
        with self._lock:
            due = [j for j in self._jobs.values() if j.schedule and j.schedule.enabled and j.next_run_at and j.next_run_at <= now]
            due.sort(key=lambda j: (j.next_run_at, j.id))
            out = []
            for j in due[:limit]:
                claimed = copy.deepcopy(j)
                j.next_run_at = next_fire(j.schedule.cron, j.schedule.timezone, now)
                out.append(claimed)
            return out

    # ---- runs ----
    def create_run(self, run, task_keys, exclusive=False):
        with self._lock:
            if exclusive and self.count_active_runs(run.workspace, run.job_id) > 0:
                raise Busy("this job already has an active run")
            self._runs[run.id] = copy.deepcopy(run)
            self._task_runs[run.id] = {k: TaskRun(run.id, k, TASK_PENDING) for k in task_keys}
            self._logs[run.id] = []
            return copy.deepcopy(run)

    def get_run(self, workspace, run_id):
        with self._lock:
            r = self._runs.get(run_id)
            return copy.deepcopy(r) if r and r.workspace == workspace else None

    def list_runs(self, workspace, job_id, status, limit, offset):
        with self._lock:
            items = [
                r
                for r in self._runs.values()
                if r.workspace == workspace and (job_id is None or r.job_id == job_id) and (status is None or r.status == status)
            ]
            items.sort(key=lambda r: (r.created_at, r.id), reverse=True)
            return Page(copy.deepcopy(items[offset : offset + limit]), len(items))

    def count_active_runs(self, workspace, job_id):
        with self._lock:
            return sum(1 for r in self._runs.values() if r.workspace == workspace and r.job_id == job_id and r.status in RUN_ACTIVE)

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

