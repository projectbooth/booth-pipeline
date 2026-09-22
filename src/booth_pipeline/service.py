"""The application service: every use case, independent of HTTP.

``api.py`` translates HTTP to these calls and their exceptions to status codes; the scheduler
calls ``start_run`` directly. Keeping the rules here — not in route handlers — is what lets the
scheduler and the API share exactly one implementation of "start a run".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from .auth import Identity
from .catalog_client import CatalogClient, CatalogDenied, CatalogNotFound, CatalogUnavailable
from .engine import compile_job
from .model import (
    BASE_RUNNER,
    CatalogCode,
    InlineCode,
    ModelError,
    PipelineSpec,
    sha256_text,
    validate_structure,
)
from .records import (
    RUN_QUEUED,
    TRIGGER_MANUAL,
    TRIGGER_SCHEDULE,
    Job,
    LogLine,
    Page,
    Pipeline,
    PipelineVersion,
    Run,
    TaskRun,
)
from .runners.base import Cancellation
from .runners.registry import RunnerRegistry
from .runs import RunManager
from .schedule import next_fire_trigger
from .schemas import JobInput
from .store.base import Busy, Store

log = logging.getLogger(__name__)


class NotFound(Exception):
    pass


class Unavailable(Exception):
    """A dependency the request needs (the catalog) is unreachable — retryable, not the caller's fault."""


class JobBusy(Exception):
    """The job already has an active run and does not allow concurrent runs."""


class _NullRecorder:
    """Compiling at save time needs a recorder only to satisfy the signature; nothing runs."""

    def task_started(self, key, attempt): ...
    def task_attempt_finished(self, key, attempt, status, error): ...
    def task_log(self, key, attempt): ...
    def system(self, message): ...


@dataclass
class PipelineService:
    store: Store
    registry: RunnerRegistry
    catalog: CatalogClient
    manager: RunManager

    # ---- pipelines ----
    def create_pipeline(self, ident: Identity, name: str, description: str, spec: PipelineSpec | None, notes: str) -> tuple[Pipeline, PipelineVersion | None]:
        if spec is not None:
            # Validate BEFORE creating anything, so a bad first version does not leave an empty pipeline behind.
            spec = self._prepare(ident, spec, previous=None)
        p = self.store.create_pipeline(ident.workspace, name.strip(), description.strip(), ident.display_name)
        v = self.store.add_version(ident.workspace, p.id, spec, notes, ident.display_name) if spec is not None else None
        return (self.store.get_pipeline(ident.workspace, p.id) or p), v

    def get_pipeline(self, ident: Identity, pipeline_id: str) -> Pipeline:
        return self._need(self.store.get_pipeline(ident.workspace, pipeline_id), "pipeline")

    def list_pipelines(self, ident: Identity, q: str, limit: int, offset: int) -> Page:
        return self.store.list_pipelines(ident.workspace, q, limit, offset)

    def update_pipeline(self, ident: Identity, pipeline_id: str, name: str, description: str) -> Pipeline:
        return self._need(self.store.update_pipeline(ident.workspace, pipeline_id, name.strip(), description.strip()), "pipeline")

    def delete_pipeline(self, ident: Identity, pipeline_id: str) -> None:
        if not self.store.delete_pipeline(ident.workspace, pipeline_id):
            raise NotFound("pipeline not found")

    def save_version(self, ident: Identity, pipeline_id: str, spec: PipelineSpec, notes: str) -> PipelineVersion:
        self.get_pipeline(ident, pipeline_id)
        previous = self.store.get_version(ident.workspace, pipeline_id, None)
        prepared = self._prepare(ident, spec, previous.spec if previous else None)
        return self._need(self.store.add_version(ident.workspace, pipeline_id, prepared, notes, ident.display_name), "pipeline")

    def get_version(self, ident: Identity, pipeline_id: str, version: int | None) -> PipelineVersion:
        return self._need(self.store.get_version(ident.workspace, pipeline_id, version), "pipeline version")

    def list_versions(self, ident: Identity, pipeline_id: str, limit: int, offset: int) -> Page:
        self.get_pipeline(ident, pipeline_id)
        return self.store.list_versions(ident.workspace, pipeline_id, limit, offset)

    def validate_draft(self, spec: PipelineSpec) -> None:
        """The canvas's live check: structure and runner availability, no side effects, and no
        catalog call (an unresolved reference is fine in a draft). Raises ``ModelError``."""
        validate_structure(spec, self.registry.available_ids())

    # ---- the save pipeline: resolve -> validate -> compile ----
    def _prepare(self, ident: Identity, spec: PipelineSpec, previous: PipelineSpec | None) -> PipelineSpec:
        validate_structure(spec, self.registry.available_ids())
        resolved = self._resolve_code(ident, spec, previous)
        validate_structure(resolved, self.registry.available_ids())  # re-check: the byte budget now includes catalog source
        # Compile now, not at run time: a structural problem must surface when the user saves,
        # not at 3am when the scheduler fires. (Nothing executes.)
        compile_job(resolved, run_id="validate", registry=self.registry, recorder=_NullRecorder(), cancel=Cancellation())
        return resolved

    def _resolve_code(self, ident: Identity, spec: PipelineSpec, previous: PipelineSpec | None) -> PipelineSpec:
        # Snapshots we already stored ourselves are trusted; anything a client *sends* claiming to
        # be a resolved snapshot is not (it would let a caller label arbitrary source as
        # "catalog entry X @ v1"). So a catalog reference is re-fetched unless it matches
        # something in our own previous version — which also lets an unrelated edit be saved
        # while the catalog is down.
        known: dict[tuple[str, str], CatalogCode] = {}
        if previous:
            for t in previous.tasks:
                if isinstance(t.code, CatalogCode) and t.code.resolved:
                    known[(t.code.entry_id, t.code.version)] = t.code
        fetched: dict[tuple[str, str], CatalogCode] = {}
        out = []
        for i, t in enumerate(spec.tasks):
            code = t.code
            field = f"tasks[{i}].code"
            if isinstance(code, InlineCode):
                code = InlineCode(source=code.source, sha256=sha256_text(code.source))
            else:
                key = (code.entry_id, code.version)
                snap = known.get(key) or fetched.get(key)
                if snap is None:
                    snap = self._fetch(ident, code, field)
                    fetched[key] = snap
                    fetched[(snap.entry_id, snap.version)] = snap  # the concrete label, if "latest" was asked
                code = snap
                if t.runner == BASE_RUNNER and (code.language or "") not in ("", "python"):
                    raise ModelError(
                        f"catalog entry {code.name!r} is {code.language!r} code; the base runner runs Python",
                        field,
                    )
            out.append(t.model_copy(update={"code": code}))
        return PipelineSpec(tasks=out)

    def _fetch(self, ident: Identity, ref: CatalogCode, field: str) -> CatalogCode:
        try:
            got = self.catalog.fetch(ident.token, ident.workspace, ref.entry_id, ref.version)
        except CatalogNotFound:
            raise ModelError(f"catalog code entry {ref.entry_id!r} version {ref.version!r} was not found", field) from None
        except CatalogDenied:
            raise ModelError("you are not permitted to read that catalog code entry", field) from None
        except CatalogUnavailable as e:
            raise Unavailable(f"cannot resolve catalog code at {field}: {e}. Inline code needs no catalog; otherwise retry shortly.") from e
        return CatalogCode(
            entry_id=got.entry_id,
            version=got.version,
            name=got.entry_name,
            language=got.language,
            sha256=sha256_text(got.source),
            source=got.source,
        )

    # ---- jobs ----
    def create_job(self, ident: Identity, body: JobInput) -> Job:
        self._check_job_target(ident, body)
        now = datetime.now(UTC)
        job = Job(
            id=str(uuid4()),
            workspace=ident.workspace,
            name=body.name.strip(),
            pipeline_id=body.pipeline_id,
            pipeline_version=body.pipeline_version,
            schedule=body.schedule,
            retry=body.retry,
            allow_concurrent_runs=body.allow_concurrent_runs,
            created_by=ident.display_name,
            created_at=now,
            updated_at=now,
            next_run_at=_next(body, now),
            # Whose live role caps this job workload tokens when it runs unattended (ADR 0058).
            owner_sub=ident.subject,
            role_ceiling=body.role_ceiling,
        )
        return self.store.create_job(job)

    def update_job(self, ident: Identity, job_id: str, body: JobInput) -> Job:
        cur = self.get_job(ident, job_id)
        self._check_job_target(ident, body)
        now = datetime.now(UTC)
        cur.name, cur.pipeline_id, cur.pipeline_version = body.name.strip(), body.pipeline_id, body.pipeline_version
        cur.schedule, cur.retry, cur.allow_concurrent_runs = body.schedule, body.retry, body.allow_concurrent_runs
        cur.updated_at, cur.next_run_at = now, _next(body, now)
        cur.role_ceiling = body.role_ceiling
        # The owner stays whoever created the job. A job from before workload identity has none, and
        # the first person to save it takes it over (the run log says to do exactly that).
        cur.owner_sub = cur.owner_sub or ident.subject
        return self._need(self.store.update_job(cur), "job")

    def get_job(self, ident: Identity, job_id: str) -> Job:
        return self._need(self.store.get_job(ident.workspace, job_id), "job")

    def list_jobs(self, ident: Identity, pipeline_id: str | None, q: str, limit: int, offset: int) -> Page:
        return self.store.list_jobs(ident.workspace, pipeline_id, q, limit, offset)

    def delete_job(self, ident: Identity, job_id: str) -> None:
        self.get_job(ident, job_id)
        self.manager_cancel_active(ident, job_id)
        if not self.store.delete_job(ident.workspace, job_id):
            raise NotFound("job not found")

    def manager_cancel_active(self, ident: Identity, job_id: str) -> None:
        """Deleting a job removes its runs; stop any this process is still executing first, or an
        orphaned worker would keep going with nothing left to record its result against."""
        for r in self.store.list_runs(ident.workspace, job_id, None, 1000, 0).items:
            if r.status in (RUN_QUEUED, "running"):
                self.store.request_cancel(ident.workspace, r.id)
                self.manager.cancel(r.id)

    def _check_job_target(self, ident: Identity, body: JobInput) -> None:
        self.get_pipeline(ident, body.pipeline_id)
        if self.store.get_version(ident.workspace, body.pipeline_id, body.pipeline_version) is None:
            what = f"version {body.pipeline_version}" if body.pipeline_version else "a saved version"
            raise ModelError(f"the pipeline has no {what} to run — save the pipeline in the builder first", "pipelineVersion")

    # ---- runs ----
    def start_run(self, workspace: str, job: Job, trigger: str, by: str, owner_sub: str) -> Run:
        """The single implementation of "start a run": used by Run-now and by the scheduler."""
        version = self.store.get_version(workspace, job.pipeline_id, job.pipeline_version)
        if version is None:
            raise ModelError("the pipeline has no such version to run", "pipelineVersion")
        run = Run(
            id=str(uuid4()),
            workspace=workspace,
            job_id=job.id,
            pipeline_id=job.pipeline_id,
            pipeline_version=version.version,  # pinned NOW: "latest" is resolved once, at start
            status=RUN_QUEUED,
            trigger=trigger,
            triggered_by=by,
            created_at=datetime.now(UTC),
            owner_sub=owner_sub,
        )
        try:
            self.store.create_run(run, [t.key for t in version.spec.tasks], exclusive=not job.allow_concurrent_runs)
        except Busy:
            raise JobBusy("this job already has an active run; wait for it to finish, cancel it, or allow concurrent runs on the job") from None
        self.manager.submit(run)
        return run

    def run_now(self, ident: Identity, job_id: str) -> Run:
        # A manual run belongs to the person who pressed the button: they are present and
        # authenticated, so their live role (not a possibly-lapsed job owner) is what caps it.
        return self.start_run(ident.workspace, self.get_job(ident, job_id), TRIGGER_MANUAL, ident.display_name, ident.subject)

    def start_scheduled(self, job: Job) -> Run | None:
        try:
            return self.start_run(job.workspace, job, TRIGGER_SCHEDULE, "schedule", job.owner_sub)
        except JobBusy:
            log.info("scheduled run of job %s skipped: previous run still active", job.id)
        except ModelError as e:
            log.warning("scheduled run of job %s could not start: %s", job.id, e.message)
        return None

    def get_run(self, ident: Identity, run_id: str) -> tuple[Run, list[TaskRun]]:
        run = self._need(self.store.get_run(ident.workspace, run_id), "run")
        return run, self.store.list_task_runs(run.id)

    def list_runs(self, ident: Identity, job_id: str | None, status: str | None, limit: int, offset: int) -> Page:
        return self.store.list_runs(ident.workspace, job_id, status, limit, offset)

    def cancel_run(self, ident: Identity, run_id: str) -> Run:
        run = self._need(self.store.request_cancel(ident.workspace, run_id), "run")
        self.manager.cancel(run_id)
        return run

    def logs(self, ident: Identity, run_id: str, task_key: str | None, after_seq: int, limit: int) -> list[LogLine]:
        self._need(self.store.get_run(ident.workspace, run_id), "run")
        return self.store.list_logs(run_id, task_key, after_seq, limit)

    @staticmethod
    def _need(value, what: str):
        if value is None:
            raise NotFound(f"{what} not found")
        return value


def _next(body: JobInput, now: datetime) -> datetime | None:
    s = body.schedule
    return next_fire_trigger(s, now) if s and s.enabled else None
