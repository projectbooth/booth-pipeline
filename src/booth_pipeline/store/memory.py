"""In-memory ``Store``: for unit tests and a database-free local dev run (state vanishes on exit).

Behaviourally identical to the PostgreSQL store as far as ``tests/unit/test_store_contract.py``
can tell — that suite is the definition of "identical".
"""

from __future__ import annotations

import copy
import threading
from datetime import datetime
from uuid import uuid4

from .. import triggers
from ..model import PipelineSpec, TaskConfig
from ..records import (
    RUN_ACTIVE,
    RUN_QUEUED,
    RUN_RUNNING,
    RUN_TERMINAL,
    EventTriggerState,
    LogLine,
    Page,
    Pipeline,
    PipelineDraft,
    PipelineVersion,
    Run,
    TaskDraft,
    TaskEntity,
    TaskRun,
    TaskSecret,
    TaskVersionRecord,
)
from ..schedule import next_fire_trigger
from .base import Busy, Conflict, InUse
from .base import initial_task_status as _initial_task_status


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
        self._pipeline_drafts: dict[str, PipelineDraft] = {}
        self._task_drafts: dict[str, TaskDraft] = {}
        self._runs: dict[str, Run] = {}
        self._task_runs: dict[str, dict[str, TaskRun]] = {}
        self._task_secrets: dict[tuple[str, str], TaskSecret] = {}
        self._logs: dict[str, list[LogLine]] = {}
        # ADR 0123
        self._trigger_state: dict[str, EventTriggerState] = {}
        self._seen: dict[int, datetime] = {}
        self._bus: tuple[datetime, datetime] | None = None  # (created_at, last_contact_at)

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
        draft = self._pipeline_drafts.get(p.id)
        out.has_draft = draft is not None
        out.draft_updated_at = draft.updated_at if draft else None
        return out

    def get_pipeline(self, workspace, pipeline_id):
        with self._lock:
            p = self._pipeline(workspace, pipeline_id)
            return self._with_latest(p) if p else None

    def list_pipelines(self, workspace, q, limit, offset, projects=None):
        with self._lock:
            items = [
                p
                for p in self._pipelines.values()
                if p.workspace == workspace and _matches(q, p.name, p.description) and (projects is None or p.project in projects)
            ]
            items.sort(key=lambda p: (p.name.lower(), p.id))
            return Page([self._with_latest(p) for p in items[offset : offset + limit]], len(items))

    def set_pipeline_project(self, workspace, pipeline_id, project):
        with self._lock:
            p = self._pipeline(workspace, pipeline_id)
            if not p:
                return None
            p.project, p.updated_at = project, _now()
            return self._with_latest(p)

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
            self._trigger_state.pop(p.id, None)
            self._pipeline_drafts.pop(p.id, None)
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
            p.owner_name = pipeline.owner_name
            p.role_ceiling = pipeline.role_ceiling
            p.pinned_version = pipeline.pinned_version
            p.event_trigger = pipeline.event_trigger
            p.event_trigger_since = pipeline.event_trigger_since
            p.updated_at = _now()
            if (p.event_trigger is None or not p.event_trigger.enabled) and p.id in self._trigger_state:
                triggers.reset_pending(self._trigger_state[p.id])
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

    def get_pipeline_draft(self, workspace, pipeline_id):
        with self._lock:
            p = self._pipeline(workspace, pipeline_id)
            if not p:
                return None
            return copy.deepcopy(self._pipeline_drafts.get(p.id))

    def save_pipeline_draft(self, workspace, pipeline_id, spec: PipelineSpec, by):
        with self._lock:
            p = self._pipeline(workspace, pipeline_id)
            if not p:
                return None
            d = PipelineDraft(p.id, spec.model_copy(deep=True), by, _now())
            self._pipeline_drafts[p.id] = d
            return copy.deepcopy(d)

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
        draft = self._task_drafts.get(t.id)
        out.has_draft = draft is not None
        out.draft_updated_at = draft.updated_at if draft else None
        return out

    def get_task(self, workspace, task_id):
        with self._lock:
            t = self._task(workspace, task_id)
            return self._task_with_latest(t) if t else None

    def list_tasks(self, workspace, q, limit, offset, projects=None):
        with self._lock:
            items = [
                t
                for t in self._tasks.values()
                if t.workspace == workspace and _matches(q, t.name, t.description) and (projects is None or t.project in projects)
            ]
            items.sort(key=lambda t: (t.name.lower(), t.id))
            return Page([self._task_with_latest(t) for t in items[offset : offset + limit]], len(items))

    def set_task_project(self, workspace, task_id, project):
        with self._lock:
            t = self._task(workspace, task_id)
            if not t:
                return None
            t.project, t.updated_at = project, _now()
            return self._task_with_latest(t)

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
            self._task_drafts.pop(task_id, None)
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

    def get_task_draft(self, workspace, task_id):
        with self._lock:
            t = self._task(workspace, task_id)
            if not t:
                return None
            return copy.deepcopy(self._task_drafts.get(t.id))

    def save_task_draft(self, workspace, task_id, config: TaskConfig, by):
        with self._lock:
            t = self._task(workspace, task_id)
            if not t:
                return None
            d = TaskDraft(t.id, config.model_copy(deep=True), by, _now())
            self._task_drafts[t.id] = d
            return copy.deepcopy(d)

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

    # ---- event triggers (ADR 0123) ----
    def _state(self, p: Pipeline) -> EventTriggerState:
        return self._trigger_state.setdefault(p.id, EventTriggerState(p.id, p.workspace))

    def _active(self, pipeline_id: str) -> bool:
        return any(r.pipeline_id == pipeline_id and r.status in RUN_ACTIVE for r in self._runs.values())

    def ingest_trigger_event(self, ev, seq, now):
        with self._lock:
            if seq is not None:
                if seq in self._seen:
                    return None
                self._seen[seq] = now
            run_id = triggers.own_run_id(ev.principal, ev.workspace)
            own_pipeline = self._runs[run_id].pipeline_id if run_id and run_id in self._runs else None
            out: dict[str, str] = {}
            for p in sorted(self._pipelines.values(), key=lambda p: p.id):
                t = p.event_trigger
                if p.workspace != ev.workspace or t is None or not t.enabled or not triggers.concerns(t, p.event_trigger_since, ev):
                    continue
                out[p.id] = triggers.ingest(
                    self._state(p), t, ev, now, own_run=own_pipeline == p.id, run_active=self._active(p.id), concurrent=p.allow_concurrent_runs
                )
            return out

    def claim_due_event_triggers(self, now, limit):
        with self._lock:
            out = []
            due = sorted((st for st in self._trigger_state.values() if st.fire_at is not None and st.fire_at <= now), key=lambda s: (s.fire_at, s.pipeline_id))
            due += [st for st in self._trigger_state.values() if st.queued and st.fire_at is None and not self._active(st.pipeline_id)]
            for st in due[:limit]:
                p = self._pipelines.get(st.pipeline_id)
                if p is None or p.event_trigger is None or not p.event_trigger.enabled:
                    continue
                if st.queued and st.fire_at is None:  # owed by a run that ended without releasing it
                    triggers.on_run_finished(st, p.event_trigger, now)
                    if st.fire_at is None or st.fire_at > now:
                        continue
                fired = triggers.on_due(st, p.event_trigger, now, run_active=self._active(p.id), concurrent=p.allow_concurrent_runs)
                if fired is not None:
                    out.append((self._with_latest(p), copy.deepcopy(fired)))
            return out

    def requeue_event_trigger(self, pipeline_id, fired, now):
        with self._lock:
            p, st = self._pipelines.get(pipeline_id), self._trigger_state.get(pipeline_id)
            if p and st and p.event_trigger:
                triggers.requeue(st, p.event_trigger, fired, now)
                if not self._active(pipeline_id):
                    triggers.on_run_finished(st, p.event_trigger, now)

    def next_event_fire_at(self):
        with self._lock:
            times = [st.fire_at for st in self._trigger_state.values() if st.fire_at is not None]
            return min(times) if times else None

    def get_event_trigger_state(self, workspace, pipeline_id):
        with self._lock:
            st = self._trigger_state.get(pipeline_id)
            return copy.deepcopy(st) if st and st.workspace == workspace else None

    def event_triggers_missed_events(self, now):
        with self._lock:
            n = 0
            for p in self._pipelines.values():
                if p.event_trigger is not None and p.event_trigger.enabled:
                    triggers.on_missed_events(self._state(p), p.event_trigger, now)
                    n += 1
            return n

    def trigger_bus(self):
        with self._lock:
            return self._bus

    def record_trigger_bus_contact(self, now):
        with self._lock:
            self._bus = (self._bus[0], max(self._bus[1], now)) if self._bus else (now, now)

    def prune_seen_trigger_events(self, before):
        with self._lock:
            old = [s for s, at in self._seen.items() if at < before]
            for s in old:
                del self._seen[s]
            return len(old)

    # ---- runs ----
    def create_run(self, run, task_keys, exclusive=False):
        with self._lock:
            if exclusive and self.count_active_runs(run.workspace, run.pipeline_id) > 0:
                raise Busy("this pipeline already has an active run")
            self._runs[run.id] = copy.deepcopy(run)
            self._task_runs[run.id] = {k: TaskRun(run.id, k, _initial_task_status(run)) for k in task_keys}
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

    def prune_runs(self, before, limit):
        with self._lock:
            old = sorted(
                (r for r in self._runs.values() if r.status in RUN_TERMINAL and r.finished_at is not None and r.finished_at < before),
                key=lambda r: (r.finished_at, r.id),
            )[:limit]
            for r in old:
                del self._runs[r.id]
                self._task_runs.pop(r.id, None)
                self._logs.pop(r.id, None)
            return len(old)

    def finish_run(self, run_id, status, error, now):
        with self._lock:
            r = self._runs.get(run_id)
            if not r or r.status in RUN_TERMINAL:
                return False
            r.status, r.error, r.finished_at = status, error, now
            # ADR 0123: a follow-up owed by queue_one is due now that the run has ended.
            p, st = self._pipelines.get(r.pipeline_id), self._trigger_state.get(r.pipeline_id)
            if p and st and p.event_trigger and p.event_trigger.enabled:
                triggers.on_run_finished(st, p.event_trigger, now)
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

    # ---- task secrets (ADR 0128) ----
    def list_task_secrets(self, workspace):
        with self._lock:
            return copy.deepcopy(sorted((s for (ws, _), s in self._task_secrets.items() if ws == workspace), key=lambda s: s.name))

    def get_task_secret(self, workspace, name):
        with self._lock:
            return copy.deepcopy(self._task_secrets.get((workspace, name)))

    def put_task_secret(self, secret):
        with self._lock:
            self._task_secrets[(secret.workspace, secret.name)] = copy.deepcopy(secret)

    def delete_task_secret(self, workspace, name):
        with self._lock:
            return self._task_secrets.pop((workspace, name), None) is not None

    def task_secrets_sealed_with_other_keys(self, key_id):
        with self._lock:
            return copy.deepcopy([s for s in self._task_secrets.values() if s.key_id != key_id])

    # ---- logs ----
    def append_logs(self, lines):
        with self._lock:
            for ln in lines:
                existing = self._logs.get(ln.run_id)
                # Same (run, seq) twice is a retried flush, not a new line — as in Postgres.
                if existing is not None and all(e.seq != ln.seq for e in existing):
                    existing.append(copy.copy(ln))

    def list_logs(self, run_id, task_key, after_seq, limit, exclude_streams=()):
        with self._lock:
            out = [
                ln
                for ln in self._logs.get(run_id, [])
                if ln.seq > after_seq and (task_key is None or ln.task_key == task_key) and ln.stream not in exclude_streams
            ]
            out.sort(key=lambda ln: ln.seq)
            return copy.deepcopy(out[:limit])

    def ping(self):
        return None


def _matches(q: str, *fields: str) -> bool:
    q = q.strip().lower()
    return not q or any(q in f.lower() for f in fields)
