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
    StorageCode,
    TaskConfig,
    Trigger,
    code_language,
    sha256_text,
    validate_structure,
    validate_task_config,
)
from .records import (
    RUN_ACTIVE,
    RUN_QUEUED,
    TRIGGER_MANUAL,
    TRIGGER_SCHEDULE,
    LogLine,
    Page,
    Pipeline,
    PipelineVersion,
    Run,
    TaskEntity,
    TaskRun,
    TaskVersionRecord,
)
from .resolve import resolve_task_configs
from .runners import languages
from .runners.base import Cancellation
from .runners.registry import RunnerRegistry
from .runs import RunManager
from .schedule import next_fire_trigger
from .storage_client import StorageClient, StorageDenied, StorageNotFound, StorageUnavailable
from .store.base import Busy, Store

log = logging.getLogger(__name__)


class NotFound(Exception):
    pass


class Unavailable(Exception):
    """A dependency the request needs (the catalog) is unreachable — retryable, not the caller's fault."""


class PipelineBusy(Exception):
    """The pipeline already has an active run and does not allow concurrent runs."""


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
    storage: StorageClient

    # ---- pipelines ----
    def create_pipeline(self, ident: Identity, name: str, description: str, spec: PipelineSpec | None, notes: str) -> tuple[Pipeline, PipelineVersion | None]:
        if spec is not None:
            # Validate BEFORE creating anything, so a bad first version does not leave an empty pipeline behind.
            spec = self._prepare(ident, spec)
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
        self.get_pipeline(ident, pipeline_id)
        # Stop any run this process is still executing first: deletion cascades the run's own rows
        # (ADR 0071 — there is no more separate Job to have done this instead), so an orphaned
        # worker would keep going with nothing left to record its result against.
        self._cancel_active_runs(ident, pipeline_id)
        if not self.store.delete_pipeline(ident.workspace, pipeline_id):
            raise NotFound("pipeline not found")

    def _cancel_active_runs(self, ident: Identity, pipeline_id: str) -> None:
        for r in self.store.list_runs(ident.workspace, pipeline_id, None, 1000, 0).items:
            if r.status in RUN_ACTIVE:
                self.store.request_cancel(ident.workspace, r.id)
                self.manager.cancel(r.id)

    def save_version(self, ident: Identity, pipeline_id: str, spec: PipelineSpec, notes: str) -> PipelineVersion:
        self.get_pipeline(ident, pipeline_id)
        prepared = self._prepare(ident, spec)
        return self._need(self.store.add_version(ident.workspace, pipeline_id, prepared, notes, ident.display_name), "pipeline")

    def get_version(self, ident: Identity, pipeline_id: str, version: int | None) -> PipelineVersion:
        return self._need(self.store.get_version(ident.workspace, pipeline_id, version), "pipeline version")

    def list_versions(self, ident: Identity, pipeline_id: str, limit: int, offset: int) -> Page:
        self.get_pipeline(ident, pipeline_id)
        return self.store.list_versions(ident.workspace, pipeline_id, limit, offset)

    def validate_draft(self, ident: Identity, spec: PipelineSpec) -> None:
        """The canvas's live check: DAG structure, that every referenced task version exists, and
        that the whole thing compiles. No side effects, and no catalog/storage call — a task
        version's code is already snapshotted at its own save time (ADR 0071), so resolving a
        reference to it is a pure store read. Raises ``ModelError``."""
        validate_structure(spec)
        pinned = self._pin_task_versions(ident, spec)
        configs = resolve_task_configs(self.store, ident.workspace, pinned)
        compile_job(pinned, configs, run_id="validate", registry=self.registry, recorder=_NullRecorder(), cancel=Cancellation())

    # ---- the save pipeline: validate structure -> pin task versions -> compile ----
    def _prepare(self, ident: Identity, spec: PipelineSpec) -> PipelineSpec:
        validate_structure(spec)
        pinned = self._pin_task_versions(ident, spec)
        configs = resolve_task_configs(self.store, ident.workspace, pinned)
        # Compile now, not at run time: a structural problem must surface when the user saves,
        # not at 3am when the scheduler fires. (Nothing executes.)
        compile_job(pinned, configs, run_id="validate", registry=self.registry, recorder=_NullRecorder(), cancel=Cancellation())
        return pinned

    def _pin_task_versions(self, ident: Identity, spec: PipelineSpec) -> PipelineSpec:
        """Resolve every ``"latest"`` reference to the concrete version it means right now, and
        confirm every reference (already-pinned or not) actually points at something that exists —
        a real store lookup ``validate_structure`` cannot do on its own (ADR 0071)."""
        out = []
        for i, ref in enumerate(spec.tasks):
            field = f"tasks[{i}]"
            task = self.store.get_task(ident.workspace, ref.task_id)
            if task is None:
                raise ModelError(f"{field} references task {ref.task_id!r}, which does not exist", f"{field}.taskId")
            version = ref.task_version
            if version == "latest":
                if task.latest_version == 0:
                    raise ModelError(f"{field} references task {ref.task_id!r}, which has no saved version yet", f"{field}.taskId")
                version = task.latest_version
            elif self.store.get_task_version(ident.workspace, ref.task_id, version) is None:
                raise ModelError(f"{field} references task {ref.task_id!r} version {version}, which does not exist", f"{field}.taskVersion")
            out.append(ref.model_copy(update={"task_version": version}))
        return PipelineSpec(tasks=out)

    # ---- pipeline scheduling (folded from the retired Job entity, ADR 0071) ----
    def update_schedule(
        self, ident: Identity, pipeline_id: str, schedule: Trigger | None, allow_concurrent_runs: bool, role_ceiling: str, pinned_version: int | None
    ) -> Pipeline:
        p = self.get_pipeline(ident, pipeline_id)
        if schedule is not None and schedule.enabled and p.latest_version == 0:
            raise ModelError("the pipeline has no saved version to run — save it in the builder first", "schedule")
        if pinned_version is not None and self.store.get_version(ident.workspace, pipeline_id, pinned_version) is None:
            raise ModelError(f"version {pinned_version} does not exist", "pinnedVersion")
        p.schedule = schedule
        p.allow_concurrent_runs = allow_concurrent_runs
        p.role_ceiling = role_ceiling
        p.pinned_version = pinned_version
        p.next_run_at = _next(schedule, datetime.now(UTC))
        # The owner stays whoever first gave the pipeline a schedule. One saved before workload
        # identity existed has none, and the first person to save a schedule onto it takes it over.
        p.owner_sub = p.owner_sub or ident.subject
        return self._need(self.store.update_schedule(p), "pipeline")

    # ---- tasks (ADR 0071: standalone, versioned, reusable — the direct counterpart of pipelines) ----
    def create_task(self, ident: Identity, name: str, description: str, config: TaskConfig | None, notes: str) -> tuple[TaskEntity, TaskVersionRecord | None]:
        if config is not None:
            config = self._prepare_task_config(ident, config, previous=None)
        t = self.store.create_task(ident.workspace, name.strip(), description.strip(), ident.display_name)
        v = self.store.add_task_version(ident.workspace, t.id, config, notes, ident.display_name) if config is not None else None
        return (self.store.get_task(ident.workspace, t.id) or t), v

    def get_task(self, ident: Identity, task_id: str) -> TaskEntity:
        return self._need(self.store.get_task(ident.workspace, task_id), "task")

    def list_tasks(self, ident: Identity, q: str, limit: int, offset: int) -> Page:
        return self.store.list_tasks(ident.workspace, q, limit, offset)

    def update_task(self, ident: Identity, task_id: str, name: str, description: str) -> TaskEntity:
        return self._need(self.store.update_task(ident.workspace, task_id, name.strip(), description.strip()), "task")

    def delete_task(self, ident: Identity, task_id: str) -> None:
        if not self.store.delete_task(ident.workspace, task_id):
            raise NotFound("task not found")

    def save_task_version(self, ident: Identity, task_id: str, config: TaskConfig, notes: str) -> TaskVersionRecord:
        self.get_task(ident, task_id)
        previous = self.store.get_task_version(ident.workspace, task_id, None)
        prepared = self._prepare_task_config(ident, config, previous.config if previous else None)
        return self._need(self.store.add_task_version(ident.workspace, task_id, prepared, notes, ident.display_name), "task")

    def get_task_version(self, ident: Identity, task_id: str, version: int | None) -> TaskVersionRecord:
        return self._need(self.store.get_task_version(ident.workspace, task_id, version), "task version")

    def list_task_versions(self, ident: Identity, task_id: str, limit: int, offset: int) -> Page:
        self.get_task(ident, task_id)
        return self.store.list_task_versions(ident.workspace, task_id, limit, offset)

    # ---- the save task version: validate -> resolve code -> validate again ----
    def _prepare_task_config(self, ident: Identity, config: TaskConfig, previous: TaskConfig | None) -> TaskConfig:
        validate_task_config(config, self.registry.available_ids())
        resolved = self._resolve_task_code(ident, config, previous)
        validate_task_config(resolved, self.registry.available_ids())  # re-check: the byte budget now includes catalog/storage source
        return resolved

    def _resolve_task_code(self, ident: Identity, config: TaskConfig, previous: TaskConfig | None) -> TaskConfig:
        # A snapshot we already stored ourselves is trusted; anything a client *sends* claiming to
        # be a resolved snapshot is not (it would let a caller label arbitrary source as "catalog
        # entry X @ v1", or an arbitrary storage object as already fetched). So a reference is
        # re-fetched unless it matches this task's own previous version — which also lets an
        # unrelated edit be saved while the catalog or storage is down (see StorageCode's own
        # docstring in model.py for why that reuse rule is correct for storage too, despite a
        # storage object having no immutable version the way a catalog entry does).
        field = "code"
        code = config.code
        if isinstance(code, InlineCode):
            code = InlineCode(source=code.source, sha256=sha256_text(code.source))
        else:
            known = previous.code if previous and isinstance(previous.code, CatalogCode | StorageCode) and previous.code.resolved else None
            snap = known if known is not None and _ref_key(known) == _ref_key(code) else None
            if snap is None:
                snap = self._fetch_catalog(ident, code, field) if isinstance(code, CatalogCode) else self._fetch_storage(ident, code, field)
            code = snap
            if config.runner == BASE_RUNNER and code_language(code) not in languages.available():
                raise ModelError(
                    f"the code at {field} is {code_language(code)!r}; the base runner supports: {', '.join(languages.available())}",
                    field,
                )
        return config.model_copy(update={"code": code})

    def _fetch_catalog(self, ident: Identity, ref: CatalogCode, field: str) -> CatalogCode:
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

    def _fetch_storage(self, ident: Identity, ref: StorageCode, field: str) -> StorageCode:
        try:
            got = self.storage.fetch(ident.token, ident.workspace, ref.backend_id, ref.path)
        except StorageNotFound:
            raise ModelError(f"no object at {ref.path!r} in storage backend {ref.backend_id!r}", field) from None
        except StorageDenied:
            raise ModelError("you are not permitted to read that storage object", field) from None
        except StorageUnavailable as e:
            raise Unavailable(f"cannot resolve storage code at {field}: {e}. Inline code needs no storage; otherwise retry shortly.") from e
        return StorageCode(
            backend_id=got.backend_id,
            path=got.path,
            name=got.path.rsplit("/", 1)[-1],
            language=got.language or None,
            sha256=sha256_text(got.source),
            source=got.source,
        )

    # ---- runs ----
    def start_run(self, workspace: str, pipeline: Pipeline, trigger: str, by: str, owner_sub: str) -> Run:
        """The single implementation of "start a run": used by Run-now and by the scheduler.

        Runs the pipeline's `pinned_version` if it has one, otherwise the latest saved version —
        mirrors the old Job's `pipeline_version` pin, now living on Pipeline directly (ADR 0071).
        """
        version = self.store.get_version(workspace, pipeline.id, pipeline.pinned_version)
        if version is None:
            raise ModelError("the pipeline has no saved version to run", "pipeline")
        run = Run(
            id=str(uuid4()),
            workspace=workspace,
            pipeline_id=pipeline.id,
            pipeline_version=version.version,  # pinned NOW, once, for this run
            status=RUN_QUEUED,
            trigger=trigger,
            triggered_by=by,
            created_at=datetime.now(UTC),
            owner_sub=owner_sub,
        )
        try:
            self.store.create_run(run, [t.key for t in version.spec.tasks], exclusive=not pipeline.allow_concurrent_runs)
        except Busy:
            raise PipelineBusy("this pipeline already has an active run; wait for it to finish, cancel it, or allow concurrent runs") from None
        self.manager.submit(run)
        return run

    def run_now(self, ident: Identity, pipeline_id: str) -> Run:
        # A manual run belongs to the person who pressed the button: they are present and
        # authenticated, so their live role (not a possibly-lapsed owner) is what caps it.
        return self.start_run(ident.workspace, self.get_pipeline(ident, pipeline_id), TRIGGER_MANUAL, ident.display_name, ident.subject)

    def start_scheduled(self, pipeline: Pipeline) -> Run | None:
        try:
            return self.start_run(pipeline.workspace, pipeline, TRIGGER_SCHEDULE, "schedule", pipeline.owner_sub)
        except PipelineBusy:
            log.info("scheduled run of pipeline %s skipped: previous run still active", pipeline.id)
        except ModelError as e:
            log.warning("scheduled run of pipeline %s could not start: %s", pipeline.id, e.message)
        return None

    def get_run(self, ident: Identity, run_id: str) -> tuple[Run, list[TaskRun]]:
        run = self._need(self.store.get_run(ident.workspace, run_id), "run")
        return run, self.store.list_task_runs(run.id)

    def list_runs(self, ident: Identity, pipeline_id: str | None, status: str | None, limit: int, offset: int) -> Page:
        return self.store.list_runs(ident.workspace, pipeline_id, status, limit, offset)

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


def _next(schedule: Trigger | None, now: datetime) -> datetime | None:
    return next_fire_trigger(schedule, now) if schedule and schedule.enabled else None


def _ref_key(code: CatalogCode | StorageCode) -> tuple[str, str, str]:
    """What makes two code references "the same" for reuse-without-refetching (ADR 0063): a
    catalog reference by ``(entryId, version)``, a storage reference by ``(backendId, path)`` —
    each tagged with its own type so the two families can never collide."""
    if isinstance(code, CatalogCode):
        return ("catalog", code.entry_id, code.version)
    return ("storage", code.backend_id, code.path)
